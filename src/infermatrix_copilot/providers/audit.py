"""Post-run session audit for harness backends whose native tools cannot be
disabled (cursor-agent today).

Productized from the Composer eval arm (`eval/dataset/run_cursor_arm.py`),
which caught a live out-of-bounds read (`~/.claude/skills/...`) only through
exactly this check. Product policy audited here: file reads stay inside the
session's containment roots (the PR-time worktree + the run dir), and a
read-only scope means no write/edit tool calls. The eval arm layers its own
extra rule (no PR-discussion access) on top — that is a ground-truth-leakage
concern, not a product one, and deliberately does NOT live here.

Detective, not preventive — the disclosed fallback of the tool-governance
decision in doc/features/provider-registry.md. Findings are surfaced as
violations for the caller to trace and render in RUN_REPORT, never silently
dropped.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# cursor-agent spools MCP tool RESULTS into its per-project state dir and the
# model reads them back with its native read tool — that read-back is how
# bridge output is consumed, not an exfiltration, so it is exempt from root
# containment (live-smoke finding: every bridge-using session was flagged).
_CLI_TOOL_SPOOL = re.compile(r"/\.cursor/projects/[^/]+/agent-tools/[^/]+$")


@dataclass
class SessionAudit:
    """The audited facts of one harness session: rule `violations` (empty =
    clean) and activity counters for the trace/report."""

    violations: list[str] = field(default_factory=list)
    shell_commands: int = 0
    file_reads: int = 0
    writes: int = 0
    other_tool_calls: int = 0
    tools_used: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def tool_calls(self) -> int:
        return (self.shell_commands + self.file_reads + self.writes
                + self.other_tool_calls)


def contained_in(path: str, roots: tuple[str, ...]) -> bool:
    """True when `path` lies under any of `roots`. realpath both sides: this
    machine reaches the same worktree via /home and /data prefixes, and a
    symlinked HOME must not read as a violation. Shared with the tool
    bridge's preventive read containment."""
    real = os.path.realpath(path)
    return any(real == r or real.startswith(r + os.sep)
               for r in (os.path.realpath(r) for r in roots if r))


def assert_tool_less(events: list[dict]) -> None:
    """Blind completions may not use even read-only tools or unknown blocks."""
    used = set()
    for event in events:
        kind = str(event.get("type", ""))
        if "tool" in kind.lower():
            used.add(kind)
        message = event.get("message") or {}
        for block in (message.get("content") or []) if isinstance(message, dict) else []:
            if isinstance(block, dict) and block.get("type") not in (None, "text"):
                used.add(str(block.get("type")))
        item = event.get("item")
        if isinstance(item, dict):
            kind = str(item.get("item_type") or item.get("type") or "")
            if kind and "agent_message" not in kind and "reasoning" not in kind:
                used.add(kind)
    if used:
        raise RuntimeError(f"tool-less completion attempted tool calls {sorted(used)[:3]} — reply discarded")


def audit_events(events: list[dict], *, roots: tuple[str, ...],
                 read_only: bool = True, cwd: str = "") -> SessionAudit:
    """Audit a cursor-agent stream-json event list. Each tool_call event
    appears twice (issue + result); only the issue (no "result" key) is
    counted so calls are not double-counted.

    Containment is checked on EVERY native tool call that names a path —
    read, grep, ls, glob, whatever the CLI grows next — not just reads: the
    live smoke showed grepToolCall events sailing past a read-only audit.

    `cwd` is the SESSION's working directory: the CLI emits worktree-relative
    paths for in-tree accesses, and resolving those against the auditing
    process's own cwd flagged every relative in-tree read as a violation
    (live finding, composer cursor-backend arm). Defaults to the first root."""
    audit = SessionAudit()
    base = cwd or (roots[0] if roots else "")
    for event in events:
        if event.get("type") != "tool_call":
            continue
        tc = event.get("tool_call") or {}
        for key, call in tc.items():
            if not isinstance(call, dict) or "result" in call:
                continue
            name = key.removesuffix("ToolCall")
            audit.tools_used.append(name)
            if name == "shell":
                audit.shell_commands += 1
                continue
            if name in ("write", "edit"):
                audit.writes += 1
                if read_only:
                    audit.violations.append(
                        "write attempted in a read-only session")
                continue
            if name == "read":
                audit.file_reads += 1
            else:
                audit.other_tool_calls += 1
            path = str((call.get("args") or {}).get("path") or "")
            if path and not os.path.isabs(path) and base:
                path = os.path.join(base, path)
            if (path and roots and not contained_in(path, roots)
                    and not _CLI_TOOL_SPOOL.search(os.path.realpath(path))):
                audit.violations.append(
                    f"{name} outside session roots: {path[:160]}")
            break
    return audit
