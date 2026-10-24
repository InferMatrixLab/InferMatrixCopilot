"""DeepSeek Harness (`dsh`) transport — Strict on DeepSeek's own agent harness.

Driven through the published Python SDK (`pip install deepseek-harness-sdk`),
which is itself a subprocess SDK: it launches the bundled `dsh-jsonrpc-agent`
runtime and speaks JSON-RPC over stdio. The bundled wheel carries the runtime,
so the target machine needs no Node.js and there is no CLI on PATH — hence the
`cli_path` override below.

TWO WAYS THIS BREAKS THE REGISTRY'S HARNESS ASSUMPTIONS, both deliberate:

1. **It is API-keyed, not subscription-authed.** `base.py` states that harness
   providers "hold their subscription auth inside the vendor CLI's own state
   and this codebase never sees it", and `Settings.tier_target` returns an
   empty key for every harness for exactly that reason. dsh is the exception:
   it needs `DEEPSEEK_API_KEY`, so this transport resolves the credential the
   api path would use and hands it over `DeepSeekHarnessConfig.api_key`. For
   cursor/claude-code/codex, injecting a key would be a bug; here it is the
   only way the harness runs at all.
2. **It cannot use our tool bridge, and that is a measured fact, not a
   choice.** The bundled runtime compiles in 122 plugins and
   `@deepseek-ai/dsh-mcp-client` is not one of them (verified by scanning the
   executable; adding it means editing `python/sdk-runtime/package.json` in a
   checkout and rebuilding the exe). So dsh lenses run on the harness's own
   `bash` + `str_replace_editor`, and our scoped tools — including the
   archaeology set `show_commit` / `search_history` / `file_at_base` — are
   NOT reachable as named tools. bash subsumes most of them (they are thin
   git wrappers) but those calls fall outside our audit trail, so a
   session requesting a bridge spec is rejected before SDK construction. An arm must never be labelled "tools bridged" when
   it ran on native bash; this campaign has already measured three arms that
   were not the configuration their label claimed.

   The composition is still ours: upstream minimal's shape (no compaction, no
   runtime-context injection, no skills) generated per session so the sandbox
   can be pinned per scope.

Sandbox: upstream minimal ships `mode: danger-full-access` and its own README
says to run it "only against a disposable checkout or container". This machine
is shared — `/data/<name>/` belongs to different people and our `.env` holds
live credentials — so the generated composition pins the mode to the session's
own scope (`read-only` for reviews, `workspace-write` otherwise) with
`workspaceRoot` at the scope root. dsh's vocabulary is exactly
`read-only | workspace-write | danger-full-access`, and its own default is
`read-only`; we never emit the third.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import time
from types import SimpleNamespace
from pathlib import Path

from ..agent_loop import AgentOutcome
from ..llm import Reply
from .base import (
    AgentSessionRequest,
    HarnessTransport,
    SessionUsage,
    flatten_messages,
    sanitized_env,
    stream_cli,
)
from .registry import PROVIDERS

# Env the dsh runtime subprocess keeps, on top of `sanitized_env()`. The
# The credential travels through stdin to the SDK config, not this worker env.
# The vendor SDK supplies that credential to its own runtime as required.
_DSH_ENV_KEEP = ("DSH_HOME",)

# How far past our own iteration budget a dsh session may wander before we
# stop it. The harness plans its own steps and legitimately uses more of them
# than our in-process loop does for the same work (two healthy lenses on
# pr4816 took ~40 steps against a budget of 14), so a 1:1 cap would truncate
# normal sessions. 6x still stops a runaway an order of magnitude short of the
# 558 steps measured.
_STEP_CAP_FACTOR = 6


_PLUGIN_RE = re.compile(r"name: '(@deepseek-ai/[a-z0-9-]+)'")
_bundled_plugins: set[str] | None = None


def _runtime_plugins() -> set[str]:
    """Plugin ids compiled into the bundled runtime executable, scanned once.

    The runtime boots only what its config lists, but it can only IMPORT what
    was compiled in; naming an absent plugin is a boot failure the SDK reports
    as a request timeout minutes later rather than an error. Scanning the exe
    for its plugin ids is crude but exact, and it runs once per process.
    """
    global _bundled_plugins
    if _bundled_plugins is not None:
        return _bundled_plugins
    found: set[str] = set()
    try:
        from deepseek_harness_runtime import bundled_runtime_path

        blob = Path(bundled_runtime_path()).read_bytes()
        found = {m.decode() for m in
                 re.findall(rb"@deepseek-ai/dsh-[a-z0-9-]+", blob)}
    except Exception:  # noqa: BLE001 — an unscannable runtime must not block
        found = set()   # empty ⇒ the assertion below degrades to a no-op
    _bundled_plugins = found
    return found


def _assert_plugins_bundled(composition: str) -> None:
    """Fail NOW, naming the plugin, instead of timing out in `initialize`."""
    available = _runtime_plugins()
    if not available:
        return
    missing = sorted({p for p in _PLUGIN_RE.findall(composition)
                      if p not in available})
    if missing:
        raise RuntimeError(
            "dsh composition names plugin(s) absent from the bundled runtime: "
            + ", ".join(missing)
            + " — the runtime compiles in a fixed set, so this would boot-fail "
              "and surface only as an initialize timeout. Add the dependency "
              "to python/sdk-runtime/package.json in a deepseek-harness "
              "checkout and rebuild the exe, or drop the plugin.")


class DeepSeekHarnessTransport(HarnessTransport):
    """`dsh` (DeepSeek Harness) as a Strict backend, via its Python SDK."""

    spec = PROVIDERS["deepseek"]

    # -- availability --------------------------------------------------------
    def cli_path(self) -> str | None:
        """The bundled runtime, or None when the SDK is not installed.

        Overridden because dsh ships no PATH binary: the runtime executable
        lives inside the `deepseek-harness-runtime-bin` wheel that
        `deepseek-harness-sdk` pulls in. An explicit `STRICT_BACKEND_CLI`
        still wins, so a source build can be pointed at directly.
        """
        override = getattr(self.settings, "strict_backend_cli", "")
        if override:
            return override
        try:
            import deepseek_harness  # noqa: F401
        except ImportError:
            return None
        return sys.executable  # the SDK spawns its own runtime from here

    def require_cli(self) -> str:
        cli = self.cli_path()
        if not cli:
            raise RuntimeError(
                "deepseek backend selected but the SDK is not installed — "
                "run: pip install 'infermatrix-copilot[deepseek]' (deepseek-harness-sdk carries the "
                "bundled runtime; no Node.js needed)")
        from .deepseek_worker import validate_sdk
        validate_sdk()
        return cli

    def _credential(self) -> tuple[str, str]:
        """(api_key, base_url) for dsh. `tier_target` deliberately blanks
        credentials for harness backends, so resolve from the api-side
        settings the same key would serve — our `.env` holds the DeepSeek key
        under the Anthropic names because the api path talks to DeepSeek's
        Anthropic-compatible gateway. `base_url` stays empty unless explicitly
        set: dsh's own `llm-deepseek` plugin speaks to DeepSeek's native
        endpoint, which is NOT the `/anthropic` gateway URL.
        """
        key = (getattr(self.settings, "deepseek_harness_api_key", "")
               or getattr(self.settings, "anthropic_api_key", "")
               or getattr(self.settings, "openai_api_key", ""))
        return key, getattr(self.settings, "deepseek_harness_base_url", "")

    def auth_gap(self) -> str | None:
        if not self.cli_path():
            return None  # the SDK-missing gap is reported by require_cli
        key, _ = self._credential()
        if not key:
            return ("no DeepSeek credential for the dsh backend — set "
                    "DEEPSEEK_HARNESS_API_KEY (or ANTHROPIC_API_KEY, which "
                    "this machine points at DeepSeek) in "
                    "~/.infermatrix-copilot/.env")
        return None

    # -- composition ---------------------------------------------------------
    def _composition(self, *, run_dir: Path, step_name: str, cwd: Path,
                     read_only: bool, bridge_spec_path: Path | None,
                     tool_free: bool = False) -> Path:
        """Write this session's Cordis composition and return its path.

        Upstream `minimal.cordis.yml` verbatim in shape — one system prompt,
        optional persistent bash + str_replace_editor, JSONL persistence, no
        compaction, no runtime-context injection, no skills. The sandbox mode
        is pinned to the session's scope instead of `danger-full-access`.
        The bundled runtime cannot mount our MCP bridge; session preflight
        rejects requests requiring it.
        """
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", step_name or "step") or "step"
        out_dir = Path(run_dir) / "dsh"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{stem}.cordis.yml"
        mode = "read-only" if read_only else "workspace-write"
        package_root = Path(__file__).resolve().parents[2]

        blocks = [
            # maxTokensAsSuccess:false — a turn that burns the completion
            # ceiling must surface as `max-tokens`, not as a quietly accepted
            # empty final. That silent-drop is a bug class this codebase has
            # already paid for once (ensemble.py zero-yield retry).
            "- id: sdk-jsonrpc-server\n"
            "  name: '@deepseek-ai/dsh-sdk-jsonrpc-server'\n"
            "  config:\n"
            "    maxTokensAsSuccess: false\n",
            "- id: llm-deepseek\n"
            "  name: '@deepseek-ai/dsh-llm-deepseek'\n"
            "  config:\n"
            "    apiKeyEnv: DEEPSEEK_API_KEY\n"
            "    streamIdleTimeoutMs: 172800000\n"
            "    models:\n"
            "      - id: !!js process.env.DSH_MODEL\n"
            "        contextWindow: !!js Number(process.env."
            "DSH_CONTEXT_WINDOW ?? 1000000)\n",
            "- id: sandbox\n  name: '@deepseek-ai/dsh-sandbox-local'\n",
            f"- id: sandbox-policy\n"
            f"  name: '@deepseek-ai/dsh-sandbox-policy'\n"
            f"  config:\n"
            f"    mode: {mode}\n"
            f"    workspaceRoot: {json.dumps(str(cwd))}\n",
            "- id: subprocess\n  name: '@deepseek-ai/dsh-subprocess-local'\n",
            "- id: pty\n  name: '@deepseek-ai/dsh-terminal'\n",
            "- id: terminal-bash\n"
            "  name: '@deepseek-ai/dsh-terminal-bash'\n"
            "  config:\n    timeoutMs: 300000\n",
            f"- id: fs-local\n"
            f"  name: '@deepseek-ai/dsh-fs-local'\n"
            f"  config:\n    cwd: {json.dumps(str(cwd))}\n",
            "- id: agent-spine\n"
            "  name: '@deepseek-ai/dsh-agent-spine-demo'\n"
            "  config:\n"
            "    includeHarnessIdentity: false\n"
            "    includeRuntimeContext: false\n"
            "    persona: !!js process.env.DSH_SYSTEM_PROMPT\n"
            "    workspaceContext: false\n"
            "    skills:\n      enabled: false\n"
            "    toolBash: false\n    toolJobs: false\n",
            *([] if tool_free else ["- id: persistent-bash\n"
            "  name: '@deepseek-ai/dsh-tool-bash-persistent'\n"
            "  config:\n    timeoutMs: 300000\n",
            "- id: str-replace-editor\n"
            "  name: '@deepseek-ai/dsh-tool-str-replace-editor'\n"
            "  config:\n    maxOutputChars: 16000\n"]),
            "- id: sessions\n"
            "  name: '@deepseek-ai/dsh-session-persistence-jsonl'\n"
            "  config:\n"
            "    root: !!js process.env.DSH_SESSION_ROOT\n"
            "    compression: none\n",
        ]
        # NO MCP layer: `@deepseek-ai/dsh-mcp-client` is not among the 122
        # plugins compiled into the bundled runtime, and a composition naming
        # an absent plugin does not fail fast — the runtime dies on "plugin
        # tree failed to load" while the SDK sits in `initialize` until its
        # request timeout. Measured 2026-08-17: 30 minutes per lens, silent.
        # `_assert_plugins_bundled` below turns that into an instant error.
        header = ("# Generated per session by providers/deepseek.py — upstream\n"
                  "# minimal composition, sandbox pinned to this step's scope.\n"
                  "# Native tool plugins depend on this call's execution policy.\n"
                  "# The bundled runtime has no MCP client for our tool bridge.\n"
                  "# Do not hand-edit.\n")
        body = "\n".join(blocks)
        _assert_plugins_bundled(body)
        path.write_text(header + body, encoding="utf-8")
        return path

    def _env(self, *, cwd: Path, model: str, system: str,
             session_root: Path) -> dict[str, str]:
        """Explicit environment for the runtime subprocess. Built from the
        allowlist, never `os.environ` passthrough — an inherited
        `ANTHROPIC_BASE_URL` (a DeepSeek gateway on this machine) or a stray
        `OPENAI_API_KEY` is exactly the class of leak the RFC calls out."""
        env = sanitized_env()
        for name in _DSH_ENV_KEEP:
            if name in env:
                continue
        env.update({
            "DSH_CWD": str(cwd),
            "DSH_MODEL": model or self.settings.strict_backend_model
            or self.spec.default_model,
            "DSH_SYSTEM_PROMPT": system,
            "DSH_SESSION_ROOT": str(session_root),
        })
        return env

    # -- result mapping ------------------------------------------------------
    @staticmethod
    def _activity(events: list) -> tuple[int, list[str], SessionUsage]:
        """Decode native activity once for sessions, completions and recovery.

        The pinned runtime emits usage chunks and a final assistant message
        for the same (turn, step). Prefer the message; retain a chunk if the
        worker stops before that message. Legacy aggregate envelopes are used
        only when no native per-step reports exist, never added to them.
        """
        usage = SessionUsage()
        tools: list[str] = []
        calls = 0
        reports, legacy = {}, []
        for index, event in enumerate(events or []):
            if not isinstance(event, dict):
                continue
            kind = str(event.get("kind") or event.get("type") or "")
            data = event.get("data") if isinstance(event.get("data"), dict) else event
            if "tool" in kind and "result" not in kind:
                calls += 1
                name = data.get("name") or data.get("tool") or ""
                if name:
                    tools.append(str(name))
            message = data.get("message")
            source = message.get("source") if isinstance(message, dict) else None
            served = data.get("model") or (source.get("model") if isinstance(source, dict) else None)
            if isinstance(served, str) and served:
                usage.served_model = served
            raw, priority = data.get("usage"), 0
            if kind == "assistant/message":
                priority = 2
            elif kind == "assistant/chunk" and isinstance(data.get("chunk"), dict):
                chunk = data["chunk"]
                if chunk.get("type") == "usage":
                    raw, priority = chunk.get("usage"), 1
            if priority:
                # Without coordinates we cannot distinguish repeated reports
                # from different paid steps. Do not attest their sum.
                bound = type(data.get("turn")) is int and type(data.get("step")) is int
                key = (data["turn"], data["step"]) if bound else ("unbound", index)
                if not bound:
                    raw = {}
                if priority >= reports.get(key, (0, {}))[0]:
                    reports[key] = (priority, raw if isinstance(raw, dict) else {})
            elif isinstance(raw, dict):
                legacy.append(raw)
        costs = []
        for raw in ([value[1] for value in reports.values()] if reports else legacy):
            usage.add_token_usage({target: raw.get(target, raw.get(alias)) for target, alias in (
                ("input_tokens", "inputTokens"), ("output_tokens", "outputTokens"),
                ("cache_read_input_tokens", "cacheReadInputTokens"),
                ("cache_creation_input_tokens", "cacheCreationInputTokens"))})
            costs.append(SessionUsage.normalized_cost(raw.get("cost_usd", raw.get("costUsd"))))
        usage.set_reported_cost(sum(costs) if costs and all(cost is not None for cost in costs) else None)
        usage.tools_used = tools
        return calls, tools, usage

    supports_native_events = True

    @classmethod
    def native_snapshot(cls, events):
        native = [event["payload"] for event in events
                  if event.get("type") == "native.deepseek.event" and isinstance(event.get("payload"), dict)]
        _, _, activity = cls._activity(native)
        return {"text": "", "served_model": activity.served_model,
                "usage": dict(activity.reported_usage) or None}

    def _invoke(self, *, options, env, prompt, timeout_s, step_cap=None, native_event_sink=None):
        """The SDK inherits only this worker's filtered environment.

        Startup, turn execution and SDK shutdown share the CLI supervisor's
        absolute deadline and process group; no adapter watchdog is needed.
        Credentials travel over stdin, never in argv or a request file.
        """
        response = None

        def line(channel, raw):
            nonlocal response
            if channel != "stdout" or not raw.strip():
                return
            frame = json.loads(raw)
            if not isinstance(frame, dict) or frame.get("protocol") != 1:
                raise ValueError("invalid DeepSeek worker protocol")
            if "event" in frame:
                if native_event_sink is not None:
                    native_event_sink({"type": "native.deepseek.event", "payload": frame["event"]})
            else:
                response = frame

        _, _, code, reason = stream_cli(
            [sys.executable, "-I", "-m", "infermatrix_copilot.providers.deepseek_worker"],
            cwd=options["cwd"], env=env, deadline=time.monotonic() + timeout_s,
            on_line=line,
            input=json.dumps({"options": {**options, "env": env}, "prompt": prompt, "step_cap": step_cap}))
        if reason:
            raise RuntimeError(f"DeepSeek worker {reason} deadline or cancellation")
        if code:
            raise RuntimeError("DeepSeek worker failed before returning a result")
        try:
            value = response
            if not isinstance(value, dict) or value.get("protocol") != 1:
                raise ValueError("invalid protocol")
            if value.get("failure"):
                raise RuntimeError(f"DeepSeek SDK failed ({value['failure']})")
            return SimpleNamespace(**value["result"])
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("DeepSeek worker returned an invalid result") from exc

    # -- transport contract --------------------------------------------------
    def validate_completion(self) -> None:
        """Reject missing/incompatible SDKs before accounting marks dispatch."""
        self.require_cli()

    def validate_session(self, req: AgentSessionRequest):
        super().validate_session(req)
        self.require_cli()

    def run_session(self, req: AgentSessionRequest) -> AgentOutcome:
        self.validate_session(req)

        cwd = Path(req.scope.root or req.run_dir)
        session_root = Path(req.run_dir) / "dsh" / "sessions"
        session_root.mkdir(parents=True, exist_ok=True)
        cordis = self._composition(
            run_dir=req.run_dir, step_name=req.step_name, cwd=cwd,
            read_only=req.scope.read_only,
            bridge_spec_path=req.bridge_spec_path)
        key, base_url = self._credential()
        # `or self.spec.default_model`: tier_target already resolves it, but a
        # direct transport caller must not be able to construct dsh with an
        # empty model — that dies mid-session with "has no provider/model".
        model = (req.model or self.settings.strict_backend_model
                 or self.spec.default_model)

        finish = ""
        result = None
        error = ""
        try:
            result = self._invoke(
                options=dict(provider="deepseek-official", model=model,
                             cwd=str(cwd), session_root=str(session_root),
                             cordis=str(cordis), api_key=key, base_url=base_url or None,
                             request_timeout_seconds=req.timeout_s),
                env=self._env(cwd=cwd, model=model, system=req.system, session_root=session_root),
                prompt=req.prompt, timeout_s=req.timeout_s,
                step_cap=max(1, int(req.max_iters)) * _STEP_CAP_FACTOR,
                **({"native_event_sink": req.native_event_sink} if req.native_event_sink is not None else {}))
            finish = str(result.finish_reason or "")
            error = result.error
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:300]

        events = list(getattr(result, "events", None) or [])
        calls, tools, usage = self._activity(events)
        text = str(getattr(result, "final_response", "") or "").strip()
        # `max-tokens` is a TRUNCATION, not a success: surfacing it lets the
        # ensemble's zero-yield retry fire on a real signal instead of
        # inferring the ceiling from an empty final after the fact.
        truncated = finish == "max-tokens"
        refusals = []
        if error:
            refusals.append(f"dsh session failed: {error}")
        elif finish and finish not in ("completed", "max-tokens"):
            refusals.append(f"dsh finish_reason={finish}")

        if req.trace is not None:
            req.trace.record(
                "harness_session", provider=self.spec.id, step=req.step_name,
                finish_reason=finish or None, error=error or None,
                tool_calls=calls, truncated=truncated,
                served_model=usage.served_model,
                mcp_bridged=False,
                sandbox_mode=("read-only" if req.scope.read_only
                              else "workspace-write"),
                # first-run instrumentation: the SDK does not document the
                # per-kind event schema, so record what actually arrived
                event_kinds=sorted({str(e.get("kind") or e.get("type") or "?")
                                    for e in events
                                    if isinstance(e, dict)})[:20])
        # The harness does not expose its round count.
        return usage.outcome(text, tool_calls=calls, tools_used=tools,
                             truncated=truncated, refusals=refusals)

    def complete(self, *, system: str, messages: list[dict],
                 model: str = "", max_tokens: int | None = None,
                 role: str = "", effort: str = "",
                 max_budget_usd: float | None = None, native_event_sink=None) -> Reply:
        """One-shot completion without bash/editor tool plugin registration.

        An empty scratch directory and a read-only sandbox are supplementary
        controls, not substitutes for removing the tool providers.
        """
        self.validate_completion()

        key, base_url = self._credential()
        selected = (model or self.settings.strict_backend_model
                    or self.spec.default_model)
        text, finish = "", ""
        with tempfile.TemporaryDirectory(prefix="imc-dsh-oneshot-", ignore_cleanup_errors=True) as directory:
            scratch = Path(directory)
            cordis = self._composition(
                run_dir=scratch, step_name="oneshot", cwd=scratch,
                read_only=True, bridge_spec_path=None, tool_free=True)
            result = self._invoke(
                options=dict(provider="deepseek-official", model=selected,
                             cwd=str(scratch), session_root=str(scratch / "sessions"),
                             cordis=str(cordis), api_key=key, base_url=base_url or None,
                             request_timeout_seconds=self.settings.strict_backend_timeout_s),
                env=self._env(cwd=scratch, model=selected, system=system,
                              session_root=scratch / "sessions"),
                prompt=flatten_messages(system, messages),
                timeout_s=self.settings.strict_backend_timeout_s,
                native_event_sink=native_event_sink)
            if result.error:
                raise RuntimeError(result.error)
            text = str(getattr(result, "final_response", "") or "").strip()
            finish = str(getattr(result, "finish_reason", "") or "")
            from .audit import assert_tool_less

            assert_tool_less([
                {**event, "type": event.get("kind") or event.get("type") or ""}
                for event in result.events if isinstance(event, dict)
            ])
            _, _, usage = self._activity(
                list(getattr(result, "events", None) or []))
        return usage.reply(text, stop_reason="max_tokens" if finish == "max-tokens" else "end_turn")
