"""The rebase agent's tool pack — the parent dispatcher's 20 tools as copilot
`ToolDef`s, dispatched through `tools.dispatch(..., extra=...)` (constraint
C5: one choke point; the write tools opt into path scoping via
`write_path_arg`).

Schemas are DATA: the parent's 20 verbatim tool definitions (descriptions
full of repo-domain text) live in the adapter's rebase data directory as
`tool_schemas.json` (ported byte-identically from
`agent/tools/*.py` + `agent/tools/dispatcher.py` in the parent's dispatcher
order — the PR4b request-shape goldens depend on both bytes and order). This
module supplies the NEUTRAL handler implementations and pairs them with the
loaded schemas by tool name. Handlers keep the parent's result shapes (`{"content": ...}`,
`{"error": ...}`, `{"exit_code": ...}`) serialized as JSON strings — the loop
forwards them untouched as `tool_result` content (prompt parity).

Repo specifics arrive as `RebasePaths` (checkout locations, venv, subprocess
env from the caller's env plan); knowledge-plane and plan-review tools call
injected `RebaseBackends` callables and FAIL CLOSED with a visible error
until PR4c wires the real stores — an unwired backend must never look like an
empty search result.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from ..tools import ToolDef

Handler = Callable[..., dict]


@dataclass(frozen=True)
class RebasePaths:
    """Where the target/upstream checkouts and the run env live — the values
    the parent read from env/config, now explicit. `baseline_ref` and
    `test_roots` are ADAPTER data (repo.remote/default_branch and the
    manifest's test-change roots): a repo on `master`, another remote, or a
    non-`tests/` layout supplies its own (2026-08-01 neutrality audit —
    these were hardcoded `origin/main` / `tests/`)."""

    omni_path: str
    vllm_path: str
    env: Mapping[str, str] = field(default_factory=dict)  # full child env
    baseline_ref: str = "origin/main"
    test_roots: tuple = ("tests/",)


def _unwired(name: str) -> Handler:
    def handler(**_: Any) -> dict:
        return {"error": f"{name} backend is not wired in this build "
                         "(lands with the assembly PR); do not retry"}
    return handler


@dataclass(frozen=True)
class RebaseBackends:
    """Injected implementations for the knowledge-plane + plan-review tools.
    Each takes the tool's kwargs and returns the parent-shaped dict."""

    doc_search: Handler = field(default_factory=lambda: _unwired("doc_search"))
    doc_read: Handler = field(default_factory=lambda: _unwired("doc_read"))
    search_debug_memory: Handler = field(
        default_factory=lambda: _unwired("search_debug_memory"))
    record_debug_memory: Handler = field(
        default_factory=lambda: _unwired("record_debug_memory"))
    skill_manage: Handler = field(default_factory=lambda: _unwired("skill_manage"))
    search_skills: Handler = field(default_factory=lambda: _unwired("search_skills"))
    request_plan_review: Handler = field(
        default_factory=lambda: _unwired("request_plan_review"))
    run_pytest: Handler = field(default_factory=lambda: _unwired("run_pytest"))
    reproduce: Handler = field(default_factory=lambda: _unwired("reproduce"))
    run_precommit: Handler = field(default_factory=lambda: _unwired("run_precommit"))


# ── Handlers (parent semantics, paths injected) ───────────────────────────────

def _handle_read_file(file_path: str, offset: int = 0,
                      limit: int | None = None, **_: Any) -> dict:
    path = Path(file_path)
    if not path.exists():
        return {"error": f"File not found: {file_path}"}
    if not path.is_file():
        return {"error": f"Not a file: {file_path}"}
    try:
        content = path.read_text()
        lines = content.split("\n")
        if offset:
            lines = lines[offset:]
        if limit:
            lines = lines[:limit]
        # deliberate divergence from the parent, which labeled the first
        # returned line as `offset` while skipping `offset` lines — physical
        # line offset+1 was mislabeled and paginated reads misled the agent
        start = offset + 1
        numbered = [f"{start + i}\t{line}" for i, line in enumerate(lines)]
        return {"content": "\n".join(numbered),
                "total_lines": len(content.split("\n"))}
    except Exception as exc:  # noqa: BLE001 - parent parity: errors are results
        return {"error": str(exc)}


def _handle_write_file(file_path: str, content: str, **_: Any) -> dict:
    path = Path(file_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return {"written": str(path), "size": len(content)}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def _handle_edit_file(file_path: str, old_string: str, new_string: str,
                      replace_all: bool = False, **_: Any) -> dict:
    path = Path(file_path)
    if not path.exists():
        return {"error": f"File not found: {file_path}"}
    try:
        content = path.read_text()
        count = content.count(old_string)
        if count == 0:
            return {"error": "old_string not found in file"}
        if count > 1 and not replace_all:
            return {"error": f"old_string found {count} times in file. "
                             "Use replace_all=true or provide more context."}
        new_content = (content.replace(old_string, new_string) if replace_all
                       else content.replace(old_string, new_string, 1))
        path.write_text(new_content)
        return {"replaced": str(path),
                "occurrences": count if replace_all else 1}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def load_tool_schemas(path: Path) -> list[dict]:
    """The adapter's ordered, verbatim tool definitions. Order is meaningful
    (request-shape parity), so the JSON array is used as-is; a schema naming
    an unknown handler fails loudly in `build_rebase_tools`."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(
            isinstance(d, dict) and d.get("name") for d in data):
        raise ValueError(f"malformed tool schema file: {path}")
    return data


def build_rebase_tools(tool_defs: list[dict], paths: RebasePaths,
                       backends: RebaseBackends | None = None,
                       ) -> dict[str, ToolDef]:
    """The full pack as `extra` ToolDefs for `tools.dispatch`, pairing the
    adapter's ordered schemas with the neutral handlers by name. Handlers
    close over `paths`; write tools declare `write_path_arg` (opt-in
    scoping). An unknown schema name raises — a tool the model can call but
    nothing implements must fail at build time, not mid-run."""
    backends = backends or RebaseBackends()

    def _run(cmd: list[str], timeout: int) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(cmd, capture_output=True, text=True,
                              errors="replace", timeout=timeout, check=False)

    def handle_grep(pattern: str, path: str = "", include: str = "*.py",
                    **_: Any) -> dict:
        target = path or paths.omni_path
        try:
            proc = _run(["grep", "-rn", "--include", include, pattern, target],
                        timeout=30)
            output = proc.stdout[-10000:]
            lines = [ln for ln in output.split("\n") if ln.strip()]
            return {"matches": lines, "count": len(lines)}
        except subprocess.TimeoutExpired:
            return {"error": "grep timed out", "matches": []}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "matches": []}

    def handle_run_shell(command: str, workdir: str | None = None,
                         timeout: int = 600, **_: Any) -> dict:
        cwd = workdir or paths.omni_path
        try:
            proc = subprocess.run(["bash", "--norc", "-c", command], capture_output=True,
                                  text=True, errors="replace", timeout=timeout,
                                  cwd=cwd, env=dict(paths.env) or None)
            return {"exit_code": proc.returncode,
                    "stdout": proc.stdout[-5000:],
                    "stderr": proc.stderr[-5000:]}
        except subprocess.TimeoutExpired:
            return {"exit_code": -1, "stdout": "",
                    "stderr": f"Command timed out after {timeout}s"}
        except Exception as exc:  # noqa: BLE001
            return {"exit_code": -1, "stdout": "", "stderr": str(exc)}

    def _git_show(repo: str, spec: str) -> dict:
        try:
            proc = _run(["git", "-C", repo, "show", spec], timeout=30)
            if proc.returncode != 0:
                return {"error": proc.stderr.strip(), "content": ""}
            return {"content": proc.stdout,
                    "lines": len(proc.stdout.split("\n"))}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "content": ""}

    def handle_git_show_upstream(file_path: str, ref: str = "HEAD",
                                 **_: Any) -> dict:
        return _git_show(paths.vllm_path, f"{ref}:{file_path}")

    def handle_git_show_omni_main(file_path: str, **_: Any) -> dict:
        return _git_show(paths.omni_path,
                         f"{paths.baseline_ref}:{file_path}")

    def handle_git_show_test_baseline(test_path: str, **_: Any) -> dict:
        return _git_show(paths.omni_path,
                         f"{paths.baseline_ref}:{test_path}")

    def handle_git_log_upstream(path: str, max_count: int = 10,
                                **_: Any) -> dict:
        try:
            proc = _run(["git", "-C", paths.vllm_path, "log",
                         f"--max-count={max_count}", "--oneline", "--", path],
                        timeout=30)
            commits = [ln.strip() for ln in proc.stdout.split("\n")
                       if ln.strip()]
            return {"commits": commits, "count": len(commits)}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "commits": []}

    def handle_git_diff(path: str = "", **_: Any) -> dict:
        cmd = ["git", "-C", paths.omni_path, "diff"]
        if path:
            cmd.extend(["--", path])
        try:
            proc = _run(cmd, timeout=30)
            names = _run(["git", "-C", paths.omni_path, "diff", "--name-only"],
                         timeout=10)
            changed = [ln.strip() for ln in names.stdout.split("\n")
                       if ln.strip()]
            return {"diff": proc.stdout[-10000:], "changed_files": changed}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "diff": ""}

    def handle_git_diff_tests_upstream(**_: Any) -> dict:
        try:
            proc = _run(["git", "-C", paths.omni_path, "diff",
                         "--name-status", paths.baseline_ref, "--",
                         *paths.test_roots], timeout=30)
            changes = [ln.strip() for ln in proc.stdout.split("\n")
                       if ln.strip()]
            return {"changes": changes, "count": len(changes)}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "changes": []}

    def handle_run_import_check(import_code: str, **_: Any) -> dict:
        try:
            proc = subprocess.run(["python3", "-c", import_code],
                                  capture_output=True, text=True,
                                  errors="replace", timeout=120,
                                  cwd=paths.omni_path,
                                  env=dict(paths.env) or None)
            return {"exit_code": proc.returncode,
                    "stdout": proc.stdout[-5000:],
                    "stderr": proc.stderr[-5000:]}
        except subprocess.TimeoutExpired:
            return {"exit_code": -1, "stdout": "",
                    "stderr": "Import check timed out"}
        except Exception as exc:  # noqa: BLE001
            return {"exit_code": -1, "stdout": "", "stderr": str(exc)}

    def _wrap(handler: Handler) -> Callable[..., str]:
        def call(**kwargs: Any) -> str:
            return json.dumps(handler(**kwargs))
        return call

    def _audit_ok(result: str) -> bool:
        # parent-shaped failures are ordinary strings — classify them for
        # dispatch's trace so failure accounting stays accurate
        try:
            return "error" not in json.loads(result)
        except (TypeError, ValueError):
            return False

    handlers: dict[str, tuple[Handler, str | None]] = {
        "run_shell": (handle_run_shell, None),
        "read_file": (_handle_read_file, None),
        "write_file": (_handle_write_file, "file_path"),
        "edit_file": (_handle_edit_file, "file_path"),
        "grep": (handle_grep, None),
        "run_pytest": (backends.run_pytest, None),
        "run_import_check": (handle_run_import_check, None),
        "git_show_test_baseline": (handle_git_show_test_baseline, None),
        "reproduce": (backends.reproduce, None),
        "run_precommit": (backends.run_precommit, None),
        "git_show_upstream": (handle_git_show_upstream, None),
        "git_show_omni_main": (handle_git_show_omni_main, None),
        "git_log_upstream": (handle_git_log_upstream, None),
        "git_diff": (handle_git_diff, None),
        "git_diff_tests_upstream": (handle_git_diff_tests_upstream, None),
        "request_plan_review": (backends.request_plan_review, None),
        "doc_search": (backends.doc_search, None),
        "doc_read": (backends.doc_read, None),
        "search_debug_memory": (backends.search_debug_memory, None),
        "record_debug_memory": (backends.record_debug_memory, None),
        "skill_manage": (backends.skill_manage, None),
        "search_skills": (backends.search_skills, None),
    }
    out: dict[str, ToolDef] = {}
    for d in tool_defs:
        if d["name"] not in handlers:
            raise ValueError(f"tool schema {d['name']!r} has no handler")
        handler, write_arg = handlers[d["name"]]
        out[d["name"]] = ToolDef(d["name"], d["description"],
                                 d["input_schema"], _wrap(handler),
                                 write_path_arg=write_arg,
                                 audit_ok=_audit_ok)
    return out
