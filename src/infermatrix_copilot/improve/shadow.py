"""Shadow (experiment) isolation — the four capability-boundary layers of
design §8.2, each enforced before anything runs, never audited after:

1. tool boundary: a strict `ToolScope` (builtin reads + the workflow's
   declared shadow tools, read-only, reads fenced to the shadow checkout and
   the read-only knowledge/adapter roots, `.git/` denied, extras strict);
2. step boundary: the executor refuses non-read steps under
   `settings.improve_shadow` (executor.py);
3. credential boundary: the child environment is built from an allowlist —
   the tier's model credentials, the repo path (pointing at the shadow
   checkout), the trace root — with every `*_TOKEN`/`*_PAT`/`GH_*`/`GITHUB_*`
   dropped and `PATH` set to a one-off directory holding symlinks to the
   whitelisted executables only (no `gh`);
4. repository boundary: the shadow checkout is an independent `--shared`
   clone (alternates borrow the source's objects read-only), with no remote
   and the pinned base tip published as refs so both the explicit base
   binding and any name-based legacy lookup resolve the same base.

`ALLOW_POST=0 ALLOW_PUSH=0` are set as well, as belt and braces; they are not
a layer. `assert_boundaries` checks an environment before dispatch and
`smoke` exercises the read tools inside the checkout.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from ..scopes import READ_TOOLS, ToolScope, shadow_scope

DEFAULT_EXECUTABLES = ("python3", "git", "grep")
_SECRET_NAME = re.compile(r"(_TOKEN|_PAT|_SECRET|_PASSWORD|CREDENTIALS?)$", re.I)
_FORBIDDEN_PREFIXES = ("GH_", "GITHUB_")
# credentials a shadow run MAY carry: the model endpoints (tier-specific and
# shared); everything else that looks like a secret is dropped
_MODEL_ENV = re.compile(r"^(ANTHROPIC|OPENAI|ECO|PERFORMANCE|INTENT|REVIEWER)_(API_KEY|BASE_URL|MODEL)$")
_PASSTHROUGH = ("HOME", "LANG", "LC_ALL", "TMPDIR", "TZ", "PYTHONPATH", "REPO_FULL_NAMES",
                "ADAPTERS_DIR", "MODEL_MISMATCH_POLICY", "REVIEW_ENSEMBLE", "ENSEMBLE_PARALLEL",
                "PR_CONTEXT_MODE", "REVIEW_LENS_BACKENDS", "STRICT_BACKEND", "STRICT_BACKEND_MODEL")


def read_roots_for(settings: Any, shadow_dir: str | Path) -> tuple[str, ...]:
    """The shadow checkout plus the read-only knowledge and adapter roots
    (legitimate review inputs); nothing else on the host."""
    roots = [str(shadow_dir)]
    for candidate in (getattr(settings, "knowledge_dir", None), os.environ.get("ADAPTERS_DIR")):
        if candidate:
            roots.append(str(candidate))
    try:
        from ..resources import adapters_root

        roots.append(str(adapters_root()))
    except Exception:  # noqa: BLE001 - no packaged adapters: nothing to add
        pass
    return tuple(dict.fromkeys(roots))


def declared_shadow_tools(settings: Any, playbook: str, step: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """`(shadow_tools, shadow_executables)` from the workflow declaration of
    `<playbook>.<step>`, or `((), DEFAULT_EXECUTABLES)` when undeclared."""
    from .enroll import declarations_for, lookup

    # an ensemble lens runs as `<step>#<lens>`: the declaration is the step's
    step = step.split("#", 1)[0]
    decl = lookup(declarations_for(settings), playbook, step)
    if decl is None:
        return (), DEFAULT_EXECUTABLES
    return decl.shadow_tools, decl.shadow_executables


def harden_scope(scope: ToolScope, ctx: Any, step_name: str) -> ToolScope:
    """The scope a shadow run actually gets, whatever the step asked for:
    the declared shadow tools only, read-only, reads fenced to the checkout
    (`ctx.state['repo_path']`, the shadow clone) plus the knowledge/adapter
    roots, `.git/` denied, extras strict. Called by the agent runtime under
    `settings.improve_shadow`."""
    playbook = str(ctx.state.get("playbook") or "")
    tools, executables = declared_shadow_tools(ctx.settings, playbook, step_name)
    root = scope.root or str(ctx.state.get("repo_path") or ctx.run_dir)
    hardened = shadow_scope(root, tools=tools, executables=executables,
                            extra_read_roots=read_roots_for(ctx.settings, root)[1:])
    # never widen: only builtin reads and declared tools, whatever `scope` had
    return replace(hardened, name=f"shadow:{scope.name}",
                   allowed_tools=frozenset(hardened.allowed_tools & (READ_TOOLS | frozenset(tools))))


def make_executables_dir(dest: str | Path, executables: Iterable[str] = DEFAULT_EXECUTABLES) -> Path:
    """A one-off PATH directory holding symlinks to the whitelisted binaries,
    resolved from the CURRENT process PATH; a whitelisted binary that cannot
    be found is an error (the shadow would silently lack it)."""
    path = Path(dest)
    path.mkdir(parents=True, exist_ok=True)
    for name in executables:
        target = shutil.which(name)
        if not target:
            raise RuntimeError(f"shadow executable {name!r} not found on PATH")
        link = path / name
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(target)
    return path


def shadow_env(*, shadow_dir: str | Path, run_dir: str | Path, trace_root: str | Path,
               executables_dir: str | Path, repo_name: str, environ: dict | None = None,
               ledger_dir: str | Path | None = None, usd_week: float = 20.0,
               judge_calls_week: int = 300, input_context_limits: dict[str, int] | None = None,
               native_call_limits: dict | None = None) -> dict[str, str]:
    """The allowlisted environment for a shadow subprocess (layer 3). The
    child binds its own budget governor from IMPROVE_GOVERNED=1 and the
    ledger directory, against the same weekly files as the parent."""
    env = os.environ if environ is None else environ
    out: dict[str, str] = {}
    for key, value in env.items():
        if key.startswith(_FORBIDDEN_PREFIXES):
            continue
        if _MODEL_ENV.match(key) or key in _PASSTHROUGH:
            out[key] = value
            continue
        if _SECRET_NAME.search(key):
            continue
    out["PATH"] = str(executables_dir)
    # the JSON form Settings parses (alias -> checkout): the child's only repo
    out["REPO_PATHS"] = json.dumps({repo_name: str(shadow_dir)})
    out["TRACE_STORE_ROOT"] = str(trace_root)
    out["IMPROVE_SHADOW"] = "1"
    out["PR_CONTEXT_SOURCE"] = "snapshot"
    out["ALLOW_POST"] = "0"
    out["ALLOW_PUSH"] = "0"
    out["IMX_RUN_DIR"] = str(run_dir)
    out["IMPROVE_GOVERNED"] = "1"
    if ledger_dir is not None:
        out["IMPROVE_LEDGER_DIR"] = str(ledger_dir)
    out["IMPROVE_BUDGET_USD_WEEK"] = str(usd_week)
    out["IMPROVE_BUDGET_JUDGE_CALLS_WEEK"] = str(judge_calls_week)
    out["IMPROVE_INPUT_CONTEXT_LIMITS"] = json.dumps(input_context_limits or {}, sort_keys=True)
    out['IMPROVE_NATIVE_CALL_LIMITS'] = json.dumps(native_call_limits or {}, sort_keys=True)
    return out


def assert_boundaries(env: dict[str, str], expected: dict[str, str] | None = None) -> list[str]:
    """Violations of the credential/executable boundary in `env` (empty when
    the environment is safe to launch). Checked BEFORE dispatch; a
    non-empty list aborts the experiment as invalid. ``expected`` pins
    values the child must carry exactly (the parent's budget configuration)."""
    problems: list[str] = []
    for key, value in (expected or {}).items():
        if env.get(key) != value:
            problems.append(f"{key} is {env.get(key)!r}, expected {value!r}")
    for key in env:
        if key.startswith(_FORBIDDEN_PREFIXES):
            problems.append(f"forbidden variable {key}")
        elif _SECRET_NAME.search(key) and not _MODEL_ENV.match(key):
            problems.append(f"secret-shaped variable {key}")
    if env.get("ALLOW_POST", "0") != "0" or env.get("ALLOW_PUSH", "0") != "0":
        problems.append("outward writes enabled")
    if env.get("IMPROVE_SHADOW") != "1":
        problems.append("IMPROVE_SHADOW is not set")
    if env.get("IMPROVE_GOVERNED") != "1" or not env.get("IMPROVE_LEDGER_DIR"):
        problems.append("the child would run unmetered (IMPROVE_GOVERNED/IMPROVE_LEDGER_DIR missing)")
    path = env.get("PATH", "")
    if not path or os.pathsep in path:
        problems.append("PATH is not the single shadow executables directory")
    elif (Path(path) / "gh").exists():
        problems.append("gh is on the shadow PATH")
    return problems


def _git(repo: str | Path, *args: str, timeout: int = 300) -> tuple[int, str]:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    return proc.returncode, (proc.stdout or proc.stderr or "").strip()


def make_shadow_clone(source_repo: str | Path, dest: str | Path, *, head_sha: str,
                      base_tip: str, base_ref_name: str = "") -> tuple[bool, str]:
    """Layer 4: an independent `--shared` clone of `source_repo` at
    `head_sha` with no remote. The pinned base tip is published inside the
    clone as `refs/remotes/origin/<base>`, `refs/heads/<base>` (only when the
    name is known) and `refs/improve/base`, so the explicit base binding and
    any legacy name-based lookup resolve the same commit. The source
    repository's configuration is never touched (a linked worktree would
    share it — that is why this is a clone)."""
    dest = Path(dest)
    if dest.exists():
        return False, f"shadow dir exists: {dest}"
    code, out = _git(Path(source_repo), "rev-parse", "--git-dir")
    if code != 0:
        return False, f"source is not a git repository: {out[:200]}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(["git", "clone", "--quiet", "--shared", "--no-checkout", str(source_repo), str(dest)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    if proc.returncode != 0:
        return False, f"clone failed: {(proc.stderr or proc.stdout)[:300]}"
    for args in (("remote", "remove", "origin"),):
        code, out = _git(dest, *args)
        if code != 0:
            return False, f"git {' '.join(args)} failed: {out[:200]}"
    # the clone inherited the source's refs; the pinned commits are present
    # through the alternates, so update-ref needs no fetch
    refs = ["refs/improve/base"]
    if base_ref_name:
        refs += [f"refs/remotes/origin/{base_ref_name}", f"refs/heads/{base_ref_name}"]
    for ref in refs:
        code, out = _git(dest, "update-ref", ref, base_tip)
        if code != 0:
            return False, f"update-ref {ref} failed: {out[:200]}"
    code, out = _git(dest, "checkout", "--quiet", "--detach", head_sha)
    if code != 0:
        return False, f"checkout {head_sha[:12]} failed: {out[:200]}"
    code, out = _git(dest, "remote")
    if code != 0 or out.strip():
        return False, f"shadow clone still has a remote: {out[:100]}"
    return True, f"shadow clone @ {head_sha[:12]} (base {base_tip[:12]})"


def smoke(scope: ToolScope, shadow_dir: str | Path, extra: dict | None = None,
          env: dict[str, str] | None = None) -> dict:
    """Exercise the shadow: each builtin read tool and each declared extra once
    inside the checkout (must succeed), and `gh --version` under the shadow
    environment (must fail). Returns a report; `ok` is the verdict."""
    from ..tools import dispatch

    report: dict = {"ok": True, "tools": {}, "gh": ""}
    root = str(shadow_dir)
    calls = {"read_file": {"path": os.path.join(root, ".gitignore")} if os.path.exists(os.path.join(root, ".gitignore"))
             else {"path": next((os.path.join(root, f) for f in sorted(os.listdir(root))
                                 if os.path.isfile(os.path.join(root, f))), root)},
             "list_dir": {"path": root},
             "grep": {"pattern": "a", "path": root}}
    for name, args in calls.items():
        if name in scope.allowed_tools:
            out = dispatch(name, args, scope=scope)
            report["tools"][name] = bool(out.get("ok"))
            report["ok"] &= bool(out.get("ok"))
    for name in (extra or {}):
        if name in scope.allowed_tools and name in ("diff_stat",):
            out = dispatch(name, {}, scope=scope, extra=extra)
            report["tools"][name] = bool(out.get("ok"))
            report["ok"] &= bool(out.get("ok"))
    if env is not None:
        try:
            proc = subprocess.run(["gh", "--version"], env=env, capture_output=True, timeout=20)
            report["gh"] = f"exit {proc.returncode}"
            report["ok"] &= proc.returncode != 0
        except (FileNotFoundError, OSError):
            report["gh"] = "not found (expected)"
    return report
