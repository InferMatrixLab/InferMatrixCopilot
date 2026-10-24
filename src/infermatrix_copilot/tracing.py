"""Portable agent trace + timing recorder (zero external dependencies).

Records a run as a tree of **spans** (OpenTelemetry-shaped: trace_id, span_id,
parent, start, end, attributes) to an append-only JSONL file — one line per
span, written at span close, so a killed run keeps everything that finished.

Works in **both synchronous and asyncio** code: ``span()`` is a plain context
manager used with ``with`` and is safe wrapped around ``await``. Parent/child
nesting propagates through a ``contextvars.ContextVar``, which is copied per
asyncio task, so parallel agents get correct, independent trees.

    from infermatrix_copilot import tracing
    tracing.init(run_id, log_dir / "trace.jsonl")
    with tracing.span("phase", phase="phase2"):
        with tracing.span("agent", label="module:scheduler"):
            with tracing.span("llm", model=model) as sp:
                ... call the model ...
                sp.mark_ttft()                      # at first token
                sp.set(prompt_tokens=..., completion_tokens=...)

Enable with env ``AGENT_TRACE=1`` (the default). ``AGENT_TRACE=0`` turns every
``span()`` into a zero-cost no-op.

Copilot owns the recorder; application and worker callers use this package.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator, Optional

from .budgeting import usage_observation

_ENABLED = os.environ.get("AGENT_TRACE", "1").strip().lower() not in ("0", "false", "no", "off", "")
_current: "contextvars.ContextVar[Optional[Span]]" = contextvars.ContextVar("trace_span", default=None)

# Fine-grained I/O capture: the actual request messages, model output, and tool
# arguments/results. OFF by default — payloads are large and may echo secrets.
# ``AGENT_TRACE_IO=1`` writes them to a sibling ``events.jsonl`` (never the span
# trace). Each string field is clipped to ``AGENT_TRACE_IO_MAX`` bytes unless
# ``AGENT_TRACE_IO_FULL=1`` keeps it whole.
_IO = os.environ.get("AGENT_TRACE_IO", "0").strip().lower() in ("1", "true", "yes", "on")
_IO_MAX = 0 if os.environ.get("AGENT_TRACE_IO_FULL", "0").strip().lower() in ("1", "true", "yes", "on") \
    else int(os.environ.get("AGENT_TRACE_IO_MAX", "8192") or 8192)

# The run header (see ``run_meta``) is metadata, not payload: hard-bounded, and
# task params are recorded key-only unless the value is a known-safe scalar.
_META_MAX = 2048
_PARAM_ALLOWLIST = frozenset({
    "limit", "review_depth", "max_groups", "local_ci_only",
    "continue_on_module_failure",
})


class Span:
    """One timed node. ``set()`` adds attributes; ``mark_ttft()`` stamps the
    first-token time so decode throughput can be derived from ``gen_s``."""

    __slots__ = ("name", "trace_id", "span_id", "parent", "start", "end", "attr", "_ttft")

    def __init__(self, name: str, trace_id: str, parent: Optional[str], attr: dict):
        """Start a span, assigning a fresh span id and the start timestamp."""
        self.name = name
        self.trace_id = trace_id
        self.span_id = uuid.uuid4().hex[:12]
        self.parent = parent
        self.start = time.time()
        self.end: Optional[float] = None
        self.attr = attr
        self._ttft: Optional[float] = None

    @property
    def elapsed_s(self) -> float:
        """Wall-clock seconds from start to end (or now if still open)."""
        return (self.end or time.time()) - self.start

    @property
    def gen_s(self) -> float:
        """Seconds spent generating (since first token, if marked)."""
        base = self._ttft if self._ttft is not None else self.start
        return (self.end or time.time()) - base

    def set(self, **attrs: Any) -> "Span":
        """Merge `attrs` into the span's attributes; returns self for chaining."""
        self.attr.update(attrs)
        return self

    def mark_ttft(self) -> None:
        """Stamp the first-token time (once), recording ttft_ms."""
        if self._ttft is None:
            self._ttft = time.time()
            self.attr["ttft_ms"] = round((self._ttft - self.start) * 1000, 1)


class _NullSpan:
    """Zero-cost stand-in when tracing is disabled or uninitialized."""

    __slots__ = ()

    def set(self, **attrs: Any) -> "_NullSpan":
        """No-op; returns self."""
        return self

    def mark_ttft(self) -> None:
        """No-op."""
        pass

    @property
    def gen_s(self) -> float:
        """Always 0.0 (tracing disabled)."""
        return 0.0

    @property
    def elapsed_s(self) -> float:
        """Always 0.0 (tracing disabled)."""
        return 0.0


_NULL = _NullSpan()


class Tracer:
    """Owns the JSONL trace file and writes spans on close (thread-safe)."""

    def __init__(self, run_id: str, out_path: os.PathLike | str):
        """Create the tracer for `run_id`, ensuring `out_path`'s parent exists."""
        self.run_id = run_id
        self.path = Path(out_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Heavy request/response/tool payloads land here, beside the span trace,
        # so trace.jsonl stays a lean metrics stream. Correlated by span_id.
        self.events_path = self.path.with_name("events.jsonl")
        self._lock = threading.Lock()
        self._inflight = 0  # concurrent llm spans — the effective batch size

    def _write(self, span: Span) -> None:
        """Append one closed span as a JSON line (locked)."""
        rec = {
            "t": "span",
            "trace_id": span.trace_id,
            "span_id": span.span_id,
            "parent": span.parent,
            "name": span.name,
            "start": round(span.start, 6),
            "end": round(span.end or span.start, 6),
            "dur_ms": round(((span.end or span.start) - span.start) * 1000, 1),
            "attr": span.attr,
        }
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _write_event(self, rec: dict) -> None:
        """Append one I/O event to the sibling events.jsonl (locked, best-effort)."""
        try:
            line = json.dumps(rec, ensure_ascii=False, default=str)
            with self._lock, self.events_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass  # tracing must never break the agent

    def _write_meta(self, rec: dict) -> None:
        """Append the run header to trace.jsonl (locked, best-effort). It rides
        in the span file rather than a sibling so a trace stays self-describing
        when it is copied out of its run directory on its own."""
        try:
            line = json.dumps(rec, ensure_ascii=False, default=str)
            if len(line) > _META_MAX:  # never let a header crowd out the spans
                rec = dict(rec, truncated=True)
                rec.pop("params", None)
                line = json.dumps(rec, ensure_ascii=False, default=str)
            with self._lock, self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass  # tracing must never break the agent

    @contextlib.contextmanager
    def span(self, name: str, **attr: Any) -> Iterator[Any]:
        """Open a child span named `name`, nesting via the contextvar and
        writing it on exit; a no-op _NULL span when tracing is disabled."""
        if not _ENABLED:
            yield _NULL
            return
        parent = _current.get()
        sp = Span(name, self.run_id, parent.span_id if parent else None, dict(attr))
        is_llm = name == "llm"
        if is_llm:  # snapshot concurrency at request start
            with self._lock:
                self._inflight += 1
                sp.attr["inflight"] = self._inflight
        token = _current.set(sp)
        try:
            yield sp
        finally:
            sp.end = time.time()
            if is_llm:
                with self._lock:
                    self._inflight -= 1
            _current.reset(token)
            try:
                self._write(sp)
            except Exception:
                pass  # tracing must never break the agent


# ── module-level default tracer + helpers ─────────────────────────────────────
_default: Optional[Tracer] = None


def init(run_id: str, out_path: os.PathLike | str) -> Optional[Tracer]:
    """Install the default tracer for this run. No-op (returns None) when
    tracing is disabled via AGENT_TRACE=0."""
    global _default
    if not _ENABLED:
        return None
    _default = Tracer(run_id, out_path)
    return _default


def span(name: str, **attr: Any):
    """Open a span on the default tracer. A plain ``with`` context manager,
    safe in sync and async code. No-op if tracing is off or uninitialized."""
    if _default is None or not _ENABLED:
        return contextlib.nullcontext(_NULL)
    return _default.span(name, **attr)


def enabled() -> bool:
    """True when tracing is on and a default tracer is installed."""
    return _ENABLED and _default is not None


def redact_params(params: Any) -> dict:
    """Keep every param *key* but only allowlisted, known-scalar *values*.

    A trace is the artifact we hand around for workload analysis, so it has to
    be safe to share on its own — and params are free-form: steps take a
    ``command`` to run and ``state_file``/``baseline_status``/``repo_path``
    filesystem paths. Knowing a param was set is what explains the workload;
    its value usually is not. Mirrors the allowlist precedent in mcp_policy."""
    if not isinstance(params, dict):
        return {}
    return {str(k): (v if k in _PARAM_ALLOWLIST and isinstance(v, (int, float, bool, str))
                     and len(str(v)) <= 64 else "<redacted>")
            for k, v in params.items()}


def run_meta(**fields: Any) -> None:
    """Record what workflow produced this trace: playbook, task kind, repo,
    tier, params — whatever the caller knows at init time.

    Written as a ``{"t": "run"}`` header line rather than a root span, so span
    parenting is untouched and every existing reader ignores it (``load_spans``
    filters on ``t == "span"``). Resuming a run re-inits onto the same file and
    appends a second header; that is legal and meaningful — the trace really
    does hold spans from two executions. Readers take the last as current."""
    if _default is None or not _ENABLED:
        return
    rec = {"t": "run", "trace_id": _default.run_id, "ts": round(time.time(), 6)}
    if "params" in fields:
        fields = dict(fields, params=redact_params(fields["params"]))
    rec.update(fields)
    _default._write_meta(rec)


def usage_counts(usage: Any) -> dict:
    """Extract the token counts from an Anthropic-shaped ``usage`` (SDK object or
    plain dict) as plain ints, tolerant of missing fields.

    These endpoints report token *counts* but never token *ids*, so a replayable
    record of a call is the request/response text (captured in ``events.jsonl``)
    paired with these counts — hence ``llm.response`` events carry both."""
    def _g(name: str) -> int:
        """Read int field `name` from the usage object or dict; 0 if absent."""
        if usage is None:
            return 0
        if isinstance(usage, dict):
            return int(usage.get(name, 0) or 0)
        return int(getattr(usage, name, 0) or 0)

    return {
        "input_tokens": _g("input_tokens"),
        "output_tokens": _g("output_tokens"),
        "cache_read_tokens": _g("cache_read_input_tokens"),
        "cache_creation_tokens": _g("cache_creation_input_tokens"),
    }


def set_usage(sp: Any, usage: Any, stop_reason: str = "") -> None:
    """Convenience: copy Anthropic ``response.usage`` onto an ``llm`` span and
    derive tokens/sec. Accepts the SDK usage object or a plain dict; tolerant of
    missing fields (cache token names differ across providers)."""
    c = usage_counts(usage)
    out = c["output_tokens"]
    sp.set(
        prompt_tokens=c["input_tokens"],
        completion_tokens=out,
        cache_read_tokens=c["cache_read_tokens"],
        cache_creation_tokens=c["cache_creation_tokens"],
        usage=usage_observation(usage),
    )
    if stop_reason:
        sp.set(stop_reason=stop_reason)
    gen = getattr(sp, "gen_s", 0.0)
    if out and gen > 0:
        sp.set(tokens_per_sec=round(out / gen, 1))


# ── fine-grained I/O events (request / response / tool call payloads) ─────────
def io_enabled() -> bool:
    """True when payload capture is on and a tracer is installed."""
    return _IO and enabled()


def _clip(obj: Any) -> Any:
    """Recursively clip long strings to _IO_MAX bytes, marking how much was cut.
    Structure (dict/list nesting) is preserved so payloads stay inspectable."""
    if isinstance(obj, str):
        if _IO_MAX and len(obj) > _IO_MAX:
            return obj[:_IO_MAX] + f"…⟨+{len(obj) - _IO_MAX} chars⟩"
        return obj
    if isinstance(obj, dict):
        return {k: _clip(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clip(v) for v in obj]
    return obj


def _attr(b: Any, k: str, default: Any = None) -> Any:
    """Read `k` from an Anthropic block whether it's a dict or an SDK object."""
    return b.get(k, default) if isinstance(b, dict) else getattr(b, k, default)


def _block_summary(b: Any) -> dict:
    """Reduce one content block (text / tool_use / tool_result) to plain JSON."""
    bt = _attr(b, "type", "")
    if bt == "text":
        return {"type": "text", "text": _attr(b, "text", "")}
    if bt == "tool_use":
        return {"type": "tool_use", "id": _attr(b, "id"),
                "name": _attr(b, "name"), "input": _attr(b, "input", {})}
    if bt == "tool_result":
        return {"type": "tool_result", "tool_use_id": _attr(b, "tool_use_id"),
                "is_error": _attr(b, "is_error", False),
                "content": _attr(b, "content", "")}
    return {"type": bt} if bt else {"raw": b}


def summarize_content(content: Any) -> Any:
    """Flatten an Anthropic message ``content`` (str, or a list of blocks that
    may be SDK objects or dicts) into plain JSON-able summaries."""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        return [_block_summary(b) for b in content]
    return _block_summary(content)


def summarize_messages(messages: Any) -> list:
    """Flatten a full Anthropic ``messages`` list for request-side capture."""
    return [{"role": _attr(m, "role", "?"),
             "content": summarize_content(_attr(m, "content", ""))}
            for m in (messages or [])]


def event(kind: str, *, span: Any = None, payload: Any = None, **fields: Any) -> None:
    """Record a fine-grained I/O event — an LLM request/response or a tool call —
    to the sibling ``events.jsonl``, correlated to the span tree by ``span_id``.
    No-op unless ``AGENT_TRACE_IO`` is on, so it is free in normal runs. String
    fields (and everything under ``payload``) are clipped to ``AGENT_TRACE_IO_MAX``
    bytes (``AGENT_TRACE_IO_FULL=1`` keeps them whole)."""
    if _default is None or not _IO or not _ENABLED:
        return
    sp = span if isinstance(span, Span) else _current.get()
    rec = {
        "t": "event",
        "kind": kind,
        "trace_id": _default.run_id,
        "span_id": getattr(sp, "span_id", None),
        "ts": round(time.time(), 6),
    }
    rec.update(_clip(fields))
    if payload is not None:
        rec["payload"] = _clip(payload)
    _default._write_event(rec)


# ── reporting ─────────────────────────────────────────────────────────────────
def load_spans(trace_path: os.PathLike | str) -> list[dict]:
    """Read span records from a JSONL trace file, skipping blank/bad lines."""
    spans = []
    p = Path(trace_path)
    if not p.exists():
        return spans
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("t") == "span":
            spans.append(rec)
    return spans


def load_run_metas(trace_path: os.PathLike | str) -> list[dict]:
    """Every ``{"t": "run"}`` header in the trace, in file order. More than one
    means the run was resumed onto the same file — each header opens a distinct
    execution, so the count is itself part of why the trace looks as it does."""
    metas = []
    p = Path(trace_path)
    if not p.exists():
        return metas
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("t") == "run":
            metas.append(rec)
    return metas


def load_run_meta(trace_path: os.PathLike | str) -> dict:
    """The current run header — the *last* one written, so a resumed run
    reports the execution that actually produced the latest spans. Empty dict
    for traces written before headers existed."""
    metas = load_run_metas(trace_path)
    return metas[-1] if metas else {}


def _pctl(values: list[float], q: float) -> float:
    """Return the `q` quantile (0..1) of `values`; 0.0 when empty."""
    if not values:
        return 0.0
    s = sorted(values)
    i = min(len(s) - 1, int(q * len(s)))
    return s[i]


def report(trace_path: os.PathLike | str) -> str:
    """Human-readable optimization rollup: per-phase wall/LLM/tool time, the
    inference-engine view (TTFT, decode rate, cache %, concurrency, context
    growth), and the top time sinks."""
    spans = load_spans(trace_path)
    if not spans:
        return f"no spans in {trace_path}"

    by_name: dict[str, list[dict]] = {}
    for s in spans:
        by_name.setdefault(s["name"], []).append(s)
    llm = by_name.get("llm", [])
    tools = by_name.get("tool", [])

    lines = [f"TRACE {spans[0]['trace_id']}   spans={len(spans)}"]

    # what workflow produced this trace (absent on traces predating run headers)
    metas = load_run_metas(trace_path)
    if metas:
        m = metas[-1]
        desc = "  ".join(f"{k}={v}" for k, v in m.items()
                         if k not in ("t", "trace_id", "ts") and v not in (None, "", {}))
        lines.append(f"  {desc}")
        if len(metas) > 1:
            lines.append(f"  resumed ×{len(metas)} — spans below span several executions")

    # per-phase rollup, falling back to per-step for playbook runs (which have
    # no phase spans — this table silently rendered nothing for them before)
    groups = by_name.get("phase", []) or by_name.get("step", [])
    if groups:
        gname = "phase" if by_name.get("phase") else "step"
        lines.append(f"\n  {gname:<11}  wall_s   llm_calls  tool_calls  in_tok    out_tok")
        span_by_id = {s["span_id"]: s for s in spans}

        def _group_of(s: dict) -> Optional[str]:
            """Walk parents (bounded) for the enclosing group span, keyed by
            span_id. Keying by name would merge foreach siblings and retries —
            which share a step name — into one row and double-count them."""
            cur = s
            seen = 0
            while cur and seen < 12:
                if cur["name"] == gname:
                    return cur["span_id"]
                cur = span_by_id.get(cur.get("parent"))
                seen += 1
            return None

        for g in groups:
            a = g["attr"]
            label = a.get("phase") or a.get("step_id") or a.get("step") or g["span_id"]
            if a.get("item"):            # foreach fan-out: which item drove this
                label = f"{label}[{a['item']}]"
            if (a.get("attempt") or 1) > 1:
                label = f"{label}#{a['attempt']}"
            glls = [s for s in llm if _group_of(s) == g["span_id"]]
            gtools = [s for s in tools if _group_of(s) == g["span_id"]]
            in_tok = sum(s["attr"].get("prompt_tokens", 0) for s in glls)
            out_tok = sum(s["attr"].get("completion_tokens", 0) for s in glls)
            lines.append(f"  {label:<11} {g['dur_ms']/1000:>7.1f}  {len(glls):>9}  "
                         f"{len(gtools):>10}  {in_tok:>7}  {out_tok:>7}")

    # inference-engine view
    if llm:
        ttfts = [s["attr"]["ttft_ms"] for s in llm if "ttft_ms" in s["attr"]]
        rates = [s["attr"]["tokens_per_sec"] for s in llm if "tokens_per_sec" in s["attr"]]
        prompts = [s["attr"].get("prompt_tokens", 0) for s in llm]
        comps = [s["attr"].get("completion_tokens", 0) for s in llm]
        cache_read = sum(s["attr"].get("cache_read_tokens", 0) for s in llm)
        cache_write = sum(s["attr"].get("cache_creation_tokens", 0) for s in llm)
        # Legacy counters describe uncached prefill. New API observations also
        # retain providers whose input count already includes the cache split.
        total_in = sum(s['attr'].get('prompt_tokens', 0) +
                        (0 if usage_observation(s['attr'].get('usage')).get('input_tokens_include_cache') is True
                        else s['attr'].get('cache_read_tokens', 0) + s['attr'].get('cache_creation_tokens', 0))
                       for s in llm) or 1
        inflight = [s["attr"].get("inflight", 1) for s in llm]
        lines.append("\nLLM (inference-engine view)")
        lines.append(f"  calls              {len(llm)}")
        if ttfts:
            lines.append(f"  TTFT     p50 {_pctl(ttfts,0.5):.0f}ms   p95 {_pctl(ttfts,0.95):.0f}ms")
        if rates:
            lines.append(f"  decode   p50 {_pctl(rates,0.5):.0f} tok/s   p95 {_pctl(rates,0.95):.0f} tok/s")
        lines.append(f"  prompt tokens      median {int(_pctl(prompts,0.5))}   max {max(prompts) if prompts else 0}")
        lines.append(f"  completion tokens  median {int(_pctl(comps,0.5))}   max {max(comps) if comps else 0}")
        lines.append(f"  prompt-cache read  {100*cache_read/total_in:.1f}%   "
                     f"({'recomputing prefill each turn — caching opportunity' if cache_read/total_in < 0.2 else 'ok'})")
        lines.append(f"  concurrency        peak {max(inflight)}   mean {sum(inflight)/len(inflight):.1f}   (effective batch size)")

    # top time sinks
    sinks = sorted(spans, key=lambda s: s.get("dur_ms", 0), reverse=True)
    lines.append("\nTop time sinks")
    for s in sinks[:8]:
        a = s["attr"]
        # every span kind names itself under a different key — `step` spans
        # carry `step`, `phase` spans `phase`, so a tool/model-only lookup
        # rendered the biggest sinks (whole steps) as blank rows.
        label = (a.get("label") or a.get("step") or a.get("tool")
                 or a.get("phase") or a.get("model") or "")
        lines.append(f"  {s['name']:<8} {s['dur_ms']/1000:>7.1f}s  {label}")
    return "\n".join(lines)


def load_events(trace_path: os.PathLike | str) -> list[dict]:
    """Read fine-grained I/O events. Accepts a run dir, a trace.jsonl path, or the
    events.jsonl itself — the payloads always live in ``events.jsonl``."""
    p = Path(trace_path)
    if p.is_dir():
        p = p / "events.jsonl"
    elif p.name != "events.jsonl":
        p = p.with_name("events.jsonl")
    events = []
    if not p.exists():
        return events
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("t") == "event":
            events.append(rec)
    return events


def _oneline(v: Any, n: int = 400) -> str:
    """Render a value on one line, escaped and length-capped, for the timeline."""
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    s = s.replace("\n", "\\n").replace("\r", "")
    return s if len(s) <= n else s[:n] + f"…(+{len(s) - n})"


def render_events(trace_path: os.PathLike | str, width: int = 400) -> str:
    """A chronological transcript of the run: every request sent to the model,
    every response (text + tool calls), and every tool's arguments and result —
    the fine-grained view the span metrics omit."""
    events = load_events(trace_path)
    if not events:
        return (f"no events for {trace_path}\n"
                "(re-run with AGENT_TRACE_IO=1 to capture request/response/tool payloads)")
    events.sort(key=lambda e: e.get("ts", 0))
    t0 = events[0].get("ts", 0)
    lines = [f"EVENTS  {events[0].get('trace_id', '')}   n={len(events)}"]
    for e in events:
        rel = e.get("ts", 0) - t0
        kind = e.get("kind", "")
        tag = f"[{rel:8.1f}s]"
        if kind == "llm.request":
            msgs = e.get("payload") or []
            lines.append(f"{tag} → REQ   turn={e.get('turn', '-')} model={e.get('model', '')} "
                         f"msgs={len(msgs)} tools={e.get('n_tools', '-')}")
            for m in msgs[-3:]:  # last few turns of context; earlier is cache-stable
                lines.append(f"           {str(m.get('role', '?')):9} {_oneline(m.get('content'), width)}")
        elif kind == "llm.response":
            # counts ride on the response event so events.jsonl is self-contained
            # (no join back to trace.jsonl needed to size a call)
            tok = ""
            if e.get("input_tokens") is not None:
                tok = (f" in={e.get('input_tokens', 0)} out={e.get('output_tokens', 0)}"
                       f" cached={e.get('cache_read_tokens', 0)}")
            lines.append(f"{tag} ← RESP  turn={e.get('turn', '-')} "
                         f"stop={e.get('stop_reason', '')}{tok}")
            if e.get("text"):
                lines.append(f"           text     {_oneline(e['text'], width)}")
            for c in (e.get("tool_calls") or []):
                lines.append(f"           call     {c.get('name', '')}({_oneline(c.get('input'), width)})")
        elif kind == "tool.call":
            lines.append(f"{tag} ⚙ CALL  {e.get('tool', '')}  {_oneline(e.get('input'), width)}")
        elif kind == "tool.result":
            status = "ok" if e.get("ok") else f"ERR {e.get('error', '')}"
            body = e.get("payload") if e.get("payload") is not None else ""
            lines.append(f"{tag} ⚙ RESULT {e.get('tool', '')} [{status}] {e.get('dur_ms', '')}ms "
                         f"{_oneline(body, width)}")
        else:
            lines.append(f"{tag} {kind} {_oneline({k: v for k, v in e.items() if k not in ('t', 'kind', 'trace_id', 'ts')}, width)}")
    return "\n".join(lines)


def _main() -> int:
    """CLI entry: print the report (or --io transcript) for a trace path/run id."""
    import sys

    pos = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not pos:
        print("usage: python -m infermatrix_copilot.tracing <trace.jsonl | run_id> [--io]")
        return 1
    arg = pos[0]
    path = Path(arg)
    if not path.exists():
        # allow passing a run_id — look under .copilot/runs/<id>/trace.jsonl
        cand = Path("runs") / arg / "trace.jsonl"
        if cand.exists():
            path = cand
    if "--io" in sys.argv or "-i" in sys.argv:
        print(render_events(path))
    else:
        print(report(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
