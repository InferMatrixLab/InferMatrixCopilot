"""Tool layer: atomic capabilities dispatched through one scope-enforcing choke point.

Tools are NOT steps (design §3.X.2): they only express "what can be done".
Every call is scope-checked and traced; out-of-scope writes execute but are
recorded — never silent.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import trace_store
from .process_execution import run_command
from .run_trace import RunTrace
from .scopes import ToolScope

# Every bound on a tool result is declared here rather than inline, so the tool
# DESCRIPTIONS can interpolate the real number instead of restating it. Hand-copied
# cap text drifts from its emitter — that is a live defect in the reference
# implementation we studied, in two separate places.
READ_MAX_BYTES = 48_000
GREP_MAX_CHARS = 20_000
SHELL_STDOUT_CHARS = 10_000
SHELL_STDERR_CHARS = 5_000

# Directories a repo-wide search must never descend into: `.git` alone can add
# megabytes of pack objects, which both drowns the signal and burns the cap below.
GREP_EXCLUDE_DIRS = (".git", "node_modules", "__pycache__", ".venv")


def bounded(text: str, limit: int, what: str,
            keep: str = "head",
            hint: str | Callable[[int], str] = "") -> str:
    """Return `text` cut to `limit` chars INCLUDING its marker, or unchanged if it fits.

    A bounded result must say it was bounded. A consumer that cannot tell a complete
    result from a clipped one will report a sweep it never finished — and nothing in
    the trace contradicts it.

    The marker is budgeted *inside* `limit` rather than appended past it, so a later
    slice at the same limit cannot shear the disclosure back off. That is not
    hypothetical: the chat turn loop caps every tool result a second time. The
    return value never exceeds `limit`; a limit too small for the full marker falls
    back to a compact `[+N]` form, and one too small even for that raises.

    keep="head"  drop the end; marker appended     (searches, file windows)
    keep="tail"  drop the start; marker prepended  (command output — the diagnostic
                 signal sits at the end, so the tail is the part worth keeping)
    hint         continuation advice folded into the marker; a callable receives the
                 number of chars actually kept, so paging offsets stay exact.
    """
    total = len(text)
    if total <= limit:
        return text

    def render(kept: int) -> str:
        advice = hint(kept) if callable(hint) else hint
        tail = f" — {advice}" if advice else ""
        if keep == "tail":
            return f"[{what}: head dropped, kept last {kept} of {total} chars{tail}]\n"
        return f"\n...[{what}: truncated at {kept} of {total} chars{tail}]"

    def compact(kept: int) -> str:
        """Last-resort disclosure for a limit too small for the full marker."""
        return f"[+{total - kept}]\n" if keep == "tail" else f"\n[+{total - kept}]"

    # The marker's own length depends on `kept`, which depends on the marker's
    # length, so `kept` has to be solved for rather than estimated. Two failure
    # modes were measured while getting here: a single-pass estimate overshot the
    # cap by 197 chars (`hint` is caller-supplied, so its length need not shrink
    # with `kept`), and chasing the fixed point iteratively settled on a low local
    # one, throwing away most of a usable budget.
    #
    # Binary search on "does a window of this size still fit its own marker?"
    # avoids both: it never returns an unsafe value, it terminates in log2(total)
    # steps, and for a marker that grows monotonically with `kept` — which is every
    # hint in this codebase, all of them constant or a decimal offset — it finds
    # the exact largest safe window.
    def largest_fit(mark: Callable[[int], str]) -> int:
        lo, hi, best = 0, total, -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if mid + len(mark(mid)) <= limit:
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        return best

    marker_of = render
    kept = largest_fit(render)
    if kept < 0:
        # `limit` cannot hold even a zero-length window plus the full marker. Fall
        # back to a compact disclosure rather than either slicing the marker into
        # something unreadable or silently blowing the cap.
        marker_of = compact
        kept = largest_fit(compact)
    if kept < 0:
        # Not even "[+N]" fits. That is a caller misconfiguration, not a runtime
        # condition — every real cap here is >= 500 — and silently returning
        # something wrong is the failure this whole function exists to prevent.
        raise ValueError(
            f"bounded(): limit {limit} is too small to hold any disclosure "
            f"for {what!r}")
    marker = marker_of(kept)
    return marker + text[total - kept:] if keep == "tail" else text[:kept] + marker


# which arg of each builtin tool names a filesystem path — used to resolve a
# RELATIVE path against the scope's repo root (so an agent's repo-relative
# path reaches a per-PR worktree, not the process cwd)
_PATH_ARGS: dict[str, str] = {
    "read_file": "path", "list_dir": "path", "grep": "path",
    "write_file": "path", "edit_file": "path", "run_shell": "cwd",
}


def _resolve_against_root(name: str, args: dict, root: str) -> dict:
    """Return `args` with its path arg made absolute under `root` when it is
    relative (absolute paths are left untouched). For `run_shell`, an unset
    `cwd` defaults to `root` so shell commands run in the repo tree."""
    key = _PATH_ARGS.get(name)
    if key is None:
        return args
    val = args.get(key)
    if name == "run_shell" and not val:
        return {**args, key: root}
    if isinstance(val, str) and val and not os.path.isabs(val):
        return {**args, key: os.path.join(root, val)}
    return args


@dataclass(frozen=True)
class ToolDef:
    """A single callable tool: its Anthropic `name`/`description`/`input_schema`
    plus the `handler` that executes it. `write_path_arg` names the argument
    holding the write target, so the dispatcher can scope-check writes."""

    name: str
    description: str
    input_schema: dict
    handler: Callable[..., str]
    write_path_arg: str | None = None  # arg holding the path a write lands on
    # arg holding the path a READ touches, for extras that read the tree:
    # under a scope with `read_roots` it is fenced like the builtin reads
    read_path_arg: str | None = None
    # the tool writes through an internal store rather than a path argument
    # (a knowledge candidate, a cache): a strict (shadow) scope refuses it
    internal_write: bool = False
    # Optional audit classifier for tools whose FAILURES are ordinary return
    # values (parent-shaped {"error": ...} strings): dispatch keeps the
    # transport payload ok=True (the bytes ARE the tool result) but records
    # the trace event with the classifier's verdict, so failure accounting
    # sees missing files/unwired backends as failures, not successes.
    audit_ok: Callable[[str], bool] | None = None


def _read_file(path: str, max_bytes: int = READ_MAX_BYTES, offset: int = 0,
               **_: Any) -> str:
    """Read `path` as UTF-8 (undecodable bytes replaced), returning a bounded
    window of `max_bytes` chars starting at char `offset`. Unbounded reads
    ballooned agent histories (a 50k-token file ingested whole per lens both
    multiplies uncached tokens and pushes conversations past the provider's
    reliable prompt-cache range) — read in windows and page with `offset`.

    An `offset` past EOF returns an explicit empty-window notice rather than `""`:
    a bare empty string arrives as a successful tool result carrying no signal, and
    the model cannot tell "nothing there" from "read did nothing"."""
    data = Path(path).read_text(encoding="utf-8", errors="replace")
    # A model supplies these, so neither the type nor the sign can be assumed. A
    # negative offset would index from the end and make the paging hint count
    # backwards; True would silently mean 1; a float would blow up in the slice.
    # bool is checked first because it subclasses int.
    for label, value in (("offset", offset), ("max_bytes", max_bytes)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{label} must be an integer, got {value!r}")
    if offset < 0:
        raise ValueError(f"offset must be >= 0, got {offset}")
    if max_bytes <= 0:
        raise ValueError(f"max_bytes must be > 0, got {max_bytes}")
    if offset and offset >= len(data):
        return (f"[empty: offset {offset} is past end of file "
                f"({len(data)} chars)]")
    # the paging hint is computed from chars ACTUALLY kept — the marker reserves
    # part of the budget, so `offset + max_bytes` would skip whatever it displaced
    return bounded(data[offset:], max_bytes, "read_file",
                   hint=lambda kept: f"call read_file again with offset={offset + kept}")


def _write_file(path: str, content: str, **_: Any) -> str:
    """Write `content` to `path`, creating parent dirs; overwrites any existing
    file. Returns a short confirmation string."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {path}"


def _edit_file(path: str, old: str, new: str, **_: Any) -> str:
    """Replace `old` with `new` in `path`, requiring exactly one match — zero or
    multiple matches raise (forcing the caller to re-read and disambiguate)
    rather than editing the wrong span. Returns a confirmation string."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    n = text.count(old)
    if n == 0:
        raise ValueError("old text not found — edit rejected, re-read the file")
    if n > 1:
        raise ValueError(f"old text matches {n} times — must match exactly once")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    return f"edited {path}"


def _list_dir(path: str, **_: Any) -> str:
    """List `path`'s entries, one per line and sorted, with a trailing `/` on
    directories. Returns the newline-joined listing."""
    return "\n".join(sorted(x.name + ("/" if x.is_dir() else "") for x in Path(path).iterdir()))


def search_files(pattern: str, path: str, *, regex: bool = False,
                 max_chars: int = GREP_MAX_CHARS, label: str = "grep") -> str:
    """Recursively search `path` for `pattern`, returning `file:line:text` matches.

    `pattern` is a LITERAL string unless `regex=True`. The default matters: this
    tool is handed patterns like `items[0]` — `_sweep_targets` extracts exactly that
    shape and the contracts lens is told to find each one's consumers. Under a regex
    default, `xs[0]` is a character class, so searching it returns the line `b = xs0`
    and NOT `a = xs[0]`: a plausible wrong line, offered as evidence. Literal is
    therefore the safe default and regex the opt-in, not the reverse.

    `regex=True` selects POSIX extended regex (`-E`), which is portable across the
    grep implementations this may shell out to (GNU, BSD, ugrep). PCRE (`-P`) is not
    — BSD grep has none — so it is deliberately not offered here.

    Exit codes are the contract: 0 matches, 1 no matches, anything else a real
    failure that RAISES. Previously every non-zero code produced empty stdout and
    was reported as "(no matches)", so a bad path or malformed pattern told the
    caller a search succeeded and found nothing."""
    out = run_command(
        ["grep", "-rn", "-E" if regex else "-F",
         *(f"--exclude-dir={d}" for d in GREP_EXCLUDE_DIRS),
         "-e", pattern, path],
        encoding="utf-8", errors="replace", timeout=60,
    )
    # membership, not `> 1`: subprocess reports signal death as a NEGATIVE code
    # (SIGKILL -> -9), which no `> 1` guard catches
    if out.returncode not in (0, 1):
        # the stderr is the whole diagnostic here, so bound it the same way as any
        # other model-visible cut rather than with a bare slice
        raise RuntimeError(
            f"grep failed (exit {out.returncode}): "
            f"{bounded(out.stderr.strip(), 500, 'grep stderr') or 'no stderr'}")
    if out.returncode == 1:
        return "(no matches)"
    return bounded(out.stdout, max_chars, label,
                   hint="narrow the pattern or search a subdirectory")


def _grep(pattern: str, path: str, regex: bool = False, **_: Any) -> str:
    return search_files(pattern, path, regex=regex)


def _run_shell(cmd: str, cwd: str | None = None, timeout: int = 600, **_: Any) -> str:
    """Run `cmd` in a shell (optionally in `cwd`, bounded by `timeout`).
    Returns the exit code with the tail of stdout (10k) and stderr (5k) — tails
    because the signal is usually at the end of long build/test output, and each
    tail says so when it dropped a head, so a clipped log is not read as a whole one."""
    out = run_command(
        cmd, shell=True, cwd=cwd,
        encoding="utf-8", errors="replace", timeout=timeout
    )
    stdout = bounded(out.stdout, SHELL_STDOUT_CHARS, "stdout", keep="tail")
    stderr = bounded(out.stderr, SHELL_STDERR_CHARS, "stderr", keep="tail")
    return f"exit={out.returncode}\n{stdout}\n{stderr}"


def _schema(props: dict, required: list[str]) -> dict:
    """Build a minimal JSON-Schema object from `props` and `required` — the
    boilerplate shared by every builtin tool's `input_schema`."""
    return {"type": "object", "properties": props, "required": required}


_S = {"type": "string"}
TOOLS: dict[str, ToolDef] = {
    t.name: t
    for t in [
        ToolDef("read_file",
                f"Read a text file (windowed: {READ_MAX_BYTES:,} chars per call; "
                "page with offset).",
                _schema({"path": _S, "offset": {"type": "integer"}}, ["path"]),
                _read_file),
        ToolDef("write_file", "Write/overwrite a file.",
                _schema({"path": _S, "content": _S}, ["path", "content"]), _write_file, "path"),
        ToolDef("edit_file", "Replace exactly-once-matching text in a file.",
                _schema({"path": _S, "old": _S, "new": _S}, ["path", "old", "new"]), _edit_file, "path"),
        ToolDef("list_dir", "List a directory.", _schema({"path": _S}, ["path"]), _list_dir),
        ToolDef("grep",
                "Recursive text search. The pattern is matched LITERALLY — pass "
                "regex:true for POSIX extended regex. Search a literal expression "
                "like items[0] with the default; brackets, dots and parentheses "
                f"need no escaping. Skips {', '.join(GREP_EXCLUDE_DIRS)}. Results "
                f"capped at {GREP_MAX_CHARS:,} chars.",
                _schema({"pattern": _S, "path": _S,
                         "regex": {"type": "boolean"}}, ["pattern", "path"]), _grep),
        ToolDef("run_shell",
                f"Run a shell command. Returns the last {SHELL_STDOUT_CHARS:,} chars "
                f"of stdout and {SHELL_STDERR_CHARS:,} of stderr.",
                _schema({"cmd": _S, "cwd": _S}, ["cmd"]), _run_shell),
    ]
}


def tool_definitions_for(scope: ToolScope | None,
                         extra: dict[str, ToolDef] | None = None) -> list[dict]:
    """Anthropic-format tool defs: builtin tools filtered to the scope's
    allowed set, plus step-provided extra tools (already vetted by the step)."""
    names = scope.allowed_tools if scope else set(TOOLS)
    defs = [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in TOOLS.values()
        if t.name in names
    ]
    for t in (extra or {}).values():
        if scope is not None and scope.strict_extras and (
                t.name not in scope.allowed_tools or t.internal_write):
            continue  # never advertised: a strict scope filters BEFORE the model sees it
        defs.append({"name": t.name, "description": t.description,
                     "input_schema": t.input_schema})
    return defs


def _refuse(name: str, reason: str, trace: RunTrace | None) -> dict:
    if trace:
        trace.record("tool_refused", tool=name, reason=reason)
    return {"ok": False, "error": f"refused: {reason}", "out_of_scope": False}


def dispatch(
    name: str,
    args: dict,
    *,
    scope: ToolScope | None = None,
    trace: RunTrace | None = None,
    extra: dict[str, ToolDef] | None = None,
) -> dict:
    """Returns {"ok": bool, "result"|"error": str, "out_of_scope": bool}.

    The single choke point for every agent tool call (invariant 5). When a
    trace/1 store is bound (``trace_store.bind_store``), every call — allowed,
    failed or refused — is also captured as a ``tool_call`` record with its
    arguments and result as blobs; capture never changes the payload."""
    started = time.monotonic()
    payload = _dispatch(name, args, scope=scope, trace=trace, extra=extra)
    _capture_tool_call(name, args, payload, time.monotonic() - started)
    return payload


def _capture_tool_call(name: str, args: dict, payload: dict, seconds: float) -> None:
    store = trace_store.current_store()
    if store is None:
        return
    try:
        error = str(payload.get("error") or "")
        result_text = payload.get("result") if payload.get("ok") else error
        store.append(
            "tool_call",
            inputs={"args": json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)},
            outputs={"result": str(result_text if result_text is not None else "")},
            result={"tool": name, "ok": bool(payload.get("ok")),
                    "refused": error.startswith("refused:"),
                    "out_of_scope": bool(payload.get("out_of_scope")),
                    "bytes": len(str(result_text or ""))},
            seconds=round(seconds, 6), error="" if payload.get("ok") else error)
    except Exception:  # noqa: BLE001 - capture never breaks a tool call
        pass


def _dispatch(
    name: str,
    args: dict,
    *,
    scope: ToolScope | None = None,
    trace: RunTrace | None = None,
    extra: dict[str, ToolDef] | None = None,
) -> dict:
    tool = (extra or {}).get(name) or TOOLS.get(name)
    if tool is None:
        return {"ok": False, "error": f"unknown tool: {name}", "out_of_scope": False}
    if extra and name in extra:
        # Extra tools bypass the BUILTIN allowlist only (the step vetted
        # them). Opt-in scoping extension: an extra ToolDef that declares
        # `write_path_arg` gets the same write-path enforcement as builtins
        # (read-only refusal, writable wall, out-of-scope recording); extras
        # without the declaration keep the historical bypass unchanged —
        # EXCEPT under a strict scope (shadow runs), where an extra must be
        # in the allowlist, must not write through an internal store, and
        # has its declared read path fenced like a builtin read.
        if scope is not None and scope.strict_extras:
            if name not in scope.allowed_tools:
                return _refuse(name, f"extra tool '{name}' not allowed in strict scope '{scope.name}'", trace)
            if tool.internal_write:
                return _refuse(name, f"extra tool '{name}' writes through an internal store", trace)
        if tool.read_path_arg and scope is not None and scope.read_roots:
            read_path = args.get(tool.read_path_arg)
            if isinstance(read_path, str) and read_path:
                if not os.path.isabs(read_path) and scope.root:
                    read_path = os.path.join(scope.root, read_path)
                    args = {**args, tool.read_path_arg: read_path}  # the handler sees the resolved path
                decision = scope.check_read(read_path)
                if not decision.allowed:
                    return _refuse(name, decision.reason, trace)
        write_path = (args.get(tool.write_path_arg)
                      if tool.write_path_arg else None)
        out_of_scope = False
        if write_path is not None and scope is not None:
            if scope.read_only:
                if trace:
                    trace.record("tool_refused", tool=name,
                                 reason="write in read-only scope")
                return {"ok": False, "error": "refused: write in read-only scope",
                        "out_of_scope": False}
            if scope.path_scope is not None:
                decision = scope.path_scope.check_write(write_path)
                if not decision.allowed:
                    if trace:
                        trace.record("tool_refused", tool=name,
                                     reason=decision.reason)
                    return {"ok": False, "error": f"refused: {decision.reason}",
                            "out_of_scope": False}
                out_of_scope = decision.out_of_scope
        try:
            result = tool.handler(**args)
            audit = True
            if tool.audit_ok is not None:
                try:
                    audit = bool(tool.audit_ok(result))
                except Exception:  # noqa: BLE001 - classifier never breaks dispatch
                    audit = False
            if trace:
                trace.record("tool_call", tool=name, ok=audit,
                             out_of_scope=out_of_scope,
                             path=str(write_path) if write_path else None)
                if out_of_scope:
                    trace.record("out_of_scope_edit", tool=name,
                                 path=str(write_path))
                # same audit event as the builtin branch: a whole-.py rewrite
                # through an extra write tool must still arm the
                # full-file-fallback review trigger
                if name == "write_file" and write_path and \
                        Path(write_path).suffix == ".py":
                    trace.record("full_file_write", path=str(write_path))
            return {"ok": True, "result": result, "out_of_scope": out_of_scope}
        except Exception as exc:
            if trace:
                trace.record("tool_call", tool=name, ok=False,
                             out_of_scope=out_of_scope,
                             path=str(write_path) if write_path else None)
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "out_of_scope": out_of_scope}

    # resolve a relative path arg against the repo root before scoping/exec, so
    # the agent's repo-relative paths reach the actual tree (e.g. a PR worktree)
    if scope is not None and scope.root:
        args = _resolve_against_root(name, args, scope.root)

    write_path = args.get(tool.write_path_arg) if tool.write_path_arg else None
    out_of_scope = False
    if scope is not None:
        decision = scope.check(name, write_path=write_path)
        if not decision.allowed:
            return _refuse(name, decision.reason, trace)
        out_of_scope = decision.out_of_scope
        # the read fence: a builtin read/exec path must resolve inside the
        # scope's read roots (realpath first, so `..` and symlinks cannot
        # escape) — a shadow run may read its checkout and nothing else
        read_path = args.get(_PATH_ARGS.get(name, ""))
        if scope.read_roots and write_path is None and isinstance(read_path, str) and read_path:
            decision = scope.check_read(read_path)
            if not decision.allowed:
                return _refuse(name, decision.reason, trace)

    try:
        result = tool.handler(**args)
        ok = True
        payload: dict = {"ok": True, "result": result, "out_of_scope": out_of_scope}
    except Exception as exc:  # errors are observations, not crashes
        ok = False
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "out_of_scope": out_of_scope}

    if trace:
        # record the operative path (write target, or the resolved read/exec
        # path) so a failed read isn't a blind spot in the trace
        traced_path = write_path or args.get(_PATH_ARGS.get(name, ""))
        trace.record(
            "tool_call", tool=name, ok=ok, out_of_scope=out_of_scope,
            path=str(traced_path) if traced_path else None,
        )
        if out_of_scope:
            trace.record("out_of_scope_edit", tool=name, path=str(write_path))
        if name == "write_file" and write_path and Path(write_path).suffix == ".py":
            trace.record("full_file_write", path=str(write_path))
    return payload
