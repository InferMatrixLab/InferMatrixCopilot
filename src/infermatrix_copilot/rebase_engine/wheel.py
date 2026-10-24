"""Precompiled-wheel commit picker, installer, and CI-Dockerfile pin.

Port of the rebase agent's `10_pick_wheel_commit.sh` + `20_update_wheel_url.sh`,
local install path only — the remote/hybrid container path died with the CLI
agents. Repo specifics (index URL, wheel variant/arch, import-check module
names, pin patterns) come from the adapter manifest as a `WheelSpec`/`PinSpec`;
nothing in this module names a particular repo.

Behavioral parity notes (each pinned by a test):
- Walk order: newest-first over `git log --format=%H HEAD`, first commit whose
  variant listing mentions our arch wins; on reaching the last-known-good
  baseline without a hit, fall back to the baseline itself if it has a wheel,
  else keep searching older commits.
- A forced commit must belong to the target branch and (outside release mode)
  must have a wheel.
- Release mode uses the branch tip and skips the wheel probe.
- Install failures abort immediately; only *import-validation* failures retry
  (with a stale-artifact clean between attempts).
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from ..testing.process_tree import _pgrep, kill_tree


def _log(msg: str) -> None:
    print(f"[wheel] {msg}", flush=True)


class WheelPickError(RuntimeError):
    """No usable wheel commit could be selected."""


class WheelInstallError(RuntimeError):
    """The package could not be installed/validated at the selected commit."""


class PinError(RuntimeError):
    """The CI Dockerfile could not be pinned to the selected commit."""


@dataclass(frozen=True)
class WheelSpec:
    """Everything repo-specific about the precompiled-wheel workflow."""

    package: str                       # importable distribution name
    index_url_template: str            # e.g. ".../{commit}/{variant}/{package}/"
    variant: str
    arch: str
    # compiled-extension candidates; >=1 must import for a healthy install
    import_check_modules: tuple[str, ...] = ()
    # torch.ops namespace that must exist after import ("" disables the check)
    ops_namespace: str = ""
    stale_artifact_globs: tuple[str, ...] = ()
    install_env: Mapping[str, str] = field(default_factory=dict)
    reinstall_retries: int = 3

    @classmethod
    def from_manifest(cls, data: Mapping) -> "WheelSpec":
        return cls(
            package=data["package"],
            index_url_template=data["index_url_template"],
            variant=data["variant"],
            arch=data["arch"],
            import_check_modules=tuple(data.get("import_check_modules", ())),
            ops_namespace=data.get("ops_namespace", ""),
            stale_artifact_globs=tuple(data.get("stale_artifact_globs", ())),
            install_env=dict(data.get("install_env", {})),
            reinstall_retries=int(data.get("reinstall_retries", 3)),
        )


@dataclass(frozen=True)
class PinSpec:
    """How the CI Dockerfile pins the wheel commit (repo-specific patterns)."""

    dockerfile: str                    # repo-relative path
    url_pattern: str                   # regex matching the full pinned URL slug
    url_template: str                  # replacement with {commit}
    commit_env_var: str                # ENV/ARG var carrying a bare 40-hex pin

    @classmethod
    def from_manifest(cls, data: Mapping) -> "PinSpec":
        return cls(dockerfile=data["dockerfile"],
                   url_pattern=data["url_pattern"],
                   url_template=data["url_template"],
                   commit_env_var=data["commit_env_var"])


RunFn = Callable[..., tuple[int, str, str]]


def _run(cmd: list[str], *, cwd: Path | None = None,
         env: Mapping[str, str] | None = None,
         timeout: float = 600.0) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                           env=dict(env) if env is not None else None,
                           capture_output=True, text=True, errors="replace",
                           timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, "", str(e)
    return p.returncode, p.stdout, p.stderr


def _fetch_listing(url: str, timeout: float = 10.0) -> str:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return resp.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - any fetch failure means "no listing"
        return ""


def make_arch_probe(spec: WheelSpec,
                    fetch: Callable[[str], str] = _fetch_listing,
                    ) -> Callable[[str], bool]:
    """Probe for "commit has a wheel for our arch": the variant-specific index
    listing must be non-empty AND mention the arch tag. (A bare "does the
    commit dir exist" probe false-positives on commits whose variant listing
    contains only a foreign arch.)"""
    def probe(rev: str) -> bool:
        url = spec.index_url_template.format(
            commit=rev, variant=spec.variant, package=spec.package)
        listing = fetch(url)
        return bool(listing) and spec.arch in listing
    return probe


def pick_wheel_commit(repo: Path, target_branch: str, spec: WheelSpec, *,
                      probe: Callable[[str], bool],
                      baseline: str = "",
                      force_commit: str = "",
                      release_mode: bool = False,
                      run: RunFn = _run,
                      log: Callable[[str], None] = _log) -> str:
    """Reset the reference checkout to origin's branch tip, walk history for
    the newest commit with a usable wheel, and leave the checkout AT that
    commit (detached). Returns the selected full SHA."""
    repo = Path(repo)
    rc, _, err = run(["git", "check-ref-format",
                      f"refs/heads/{target_branch}"], cwd=repo)
    if rc != 0:
        raise WheelPickError(f"invalid upstream target branch {target_branch!r}")
    # Scratch clones can inherit a single-branch fetch refspec. Fetch the
    # requested branch explicitly, including its tracking ref, rather than
    # depending on remote.origin.fetch (the v0.31.0 campaign hit this).
    # A failed fetch must never fall through to a stale local tracking ref.
    remote_ref = f"refs/remotes/origin/{target_branch}"
    rc, _, err = run(["git", "fetch", "origin",
                      f"+refs/heads/{target_branch}:{remote_ref}"], cwd=repo)
    if rc != 0:
        raise WheelPickError(
            f"remote branch origin/{target_branch} does not exist or could "
            f"not be fetched in {repo}: {err.strip()}")

    rc, _, _ = run(["git", "rev-parse", "--verify", f"{remote_ref}^{{commit}}"],
                   cwd=repo)
    if rc != 0:
        raise WheelPickError(
            f"remote branch origin/{target_branch} does not exist in {repo}; "
            "check the configured target branch")

    log(f"Checking out {target_branch} (resetting to origin/{target_branch})...")
    rc, _, err = run(["git", "checkout", "-B", target_branch,
                      f"origin/{target_branch}"], cwd=repo)
    if rc != 0:
        raise WheelPickError(
            f"failed to reset {target_branch} to origin/{target_branch}: "
            f"{err.strip()} — working tree may be dirty; clean {repo} and retry")

    found = ""
    if force_commit:
        log(f"Using forced commit override: {force_commit}")
        rc, out, _ = run(["git", "rev-parse", "--verify", "--end-of-options",
                          f"{force_commit}^{{commit}}"],
                         cwd=repo)
        if rc != 0 or not out.strip():
            raise WheelPickError(
                f"forced commit '{force_commit}' could not be resolved in {repo}")
        found = out.strip()
        rc, _, err = run(["git", "merge-base", "--is-ancestor", found,
                          remote_ref], cwd=repo)
        if rc != 0:
            raise WheelPickError(
                f"forced commit {found} is not proven to belong to "
                f"origin/{target_branch}; choose a commit from the target "
                f"branch: {err.strip()}")
        if release_mode:
            log(f"Release mode: skipping wheel check for forced commit {found[:12]}")
        elif probe(found):
            log(f"Forced commit has a {spec.variant}/{spec.arch} wheel: {found}")
        else:
            raise WheelPickError(
                f"forced commit {found} has no {spec.variant}/{spec.arch} wheel")
    elif release_mode:
        # Release branches may not have precompiled wheels for every commit:
        # use the tip directly and let install fall back to a source build.
        rc, out, _ = run(["git", "rev-parse", "HEAD"], cwd=repo)
        if rc != 0 or not out.strip():
            raise WheelPickError(f"git rev-parse HEAD failed in {repo}")
        found = out.strip()
        log(f"Release mode: using latest {target_branch} commit {found[:12]} "
            "(wheel check skipped)")
    else:
        log(f"Searching for latest commit with a {spec.variant}/{spec.arch} wheel...")
        rc, out, err = run(["git", "log", "--format=%H", "HEAD"], cwd=repo)
        if rc != 0:
            raise WheelPickError(f"git log failed in {repo}: {err.strip()}")
        checked = 0
        for rev in out.split():
            checked += 1
            if probe(rev):
                log(f"Found wheel at commit: {rev[:12]} "
                    f"({spec.variant}/{spec.arch}, after {checked} commit(s))")
                found = rev
                break
            if checked % 20 == 0:
                log(f"  ... checked {checked} commits, last: {rev[:12]}")
            if baseline and rev == baseline:
                # Reached the last-known-good baseline without a newer wheel;
                # the baseline itself is expected to have one — fall back.
                log(f"  Reached last known good baseline {baseline[:12]} "
                    f"after {checked} commit(s).")
                if probe(baseline):
                    log("  Baseline has a wheel — falling back to it.")
                    found = baseline
                    break
                log("  Baseline also has no wheel; continuing to search further back...")
        if not found:
            raise WheelPickError(
                f"no commit with a {spec.variant}/{spec.arch} wheel from HEAD "
                f"back through baseline {baseline[:12] if baseline else '(none)'}")

    rc, _, err = run(["git", "checkout", found], cwd=repo)
    if rc != 0:
        raise WheelPickError(f"git checkout {found[:12]} failed: {err.strip()}")
    return found


# -- install ------------------------------------------------------------------

def build_import_check_snippet(spec: WheelSpec) -> str:
    """Python that validates the package imports AND its compiled extension
    loads (>=1 of the candidate modules — upstream renames the extension across
    versions, so a single hardcoded name false-fails). Importing the extension
    also surfaces real ABI breakage (undefined symbol / duplicate operator
    registration / core dump), which is the point of the check."""
    lines = [
        "import importlib",
        f"importlib.import_module({spec.package!r})",
        f"candidates = {tuple(spec.import_check_modules)!r}",
        "loaded = []",
        "for module_name in candidates:",
        "    try:",
        "        importlib.import_module(module_name)",
        "        loaded.append(module_name)",
        "    except ModuleNotFoundError:",
        "        continue",
        "if candidates and not loaded:",
        "    raise SystemExit(",
        "        'compiled kernel extension missing (tried: %s)' % ', '.join(candidates))",
    ]
    if spec.ops_namespace:
        lines += [
            "import torch",
            f"if not hasattr(torch.ops, {spec.ops_namespace!r}):",
            "    raise SystemExit('custom ops not registered with torch "
            f"(torch.ops.{spec.ops_namespace} absent)')",
        ]
    lines.append(
        f"print('import validation passed:', ', '.join([{spec.package!r}, *loaded]))")
    return "\n".join(lines) + "\n"


_INSTALLED_VERSION_SNIPPET = """\
import sys
try:
    from importlib.metadata import PackageNotFoundError, version
except Exception:
    from importlib_metadata import PackageNotFoundError, version
try:
    print(version(sys.argv[1]))
except Exception:
    print("")
"""

_BROKEN_EXTENSION_RE = re.compile(
    r"undefined symbol|Duplicate registration|register an operator .* multiple times")


def version_matches_commit(installed_version: str, commit: str) -> bool:
    """PEP 440 local segments embed a git hash that is often *shorter* than 12
    chars (e.g. +g5af684c31.precompiled), so substring checks against a fixed
    short form alone false-negative reinstalls; also accept a `+g<hex>` segment
    that prefixes the full commit."""
    if not installed_version:
        return False
    if commit in installed_version or commit[:12] in installed_version:
        return True
    m = re.search(r"\+g([0-9a-fA-F]+)", installed_version)
    return bool(m) and commit.startswith(m.group(1))


def read_installed_version(spec: WheelSpec, *, python: str,
                           run: RunFn = _run) -> str:
    rc, out, _ = run([python, "-c", _INSTALLED_VERSION_SNIPPET, spec.package])
    return out.strip() if rc == 0 else ""


def _append(path: Path, text: str) -> None:
    with open(path, "a", encoding="utf-8", errors="replace") as f:
        f.write(text)


def _proc_cwd(pid: int) -> Path | None:
    try:
        return Path(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        return None


def release_editable_install_locks(
        repo: Path, *,
        pattern: str = r"[u]v pip install",
        pgrep: Callable[[list[str]], list[int]] = _pgrep,
        proc_cwd: Callable[[int], Path | None] = _proc_cwd,
        kill: Callable[[list[int]], list[int]] = kill_tree) -> list[int]:
    """Terminate a stale editable install still holding THIS repo's project
    lock (its parent died; the lock never releases). Scoped strictly to
    processes whose cwd is inside `repo` — the parent's single-tenant
    pkill-by-pattern would take down active installs in unrelated checkouts,
    which a multi-repo copilot host cannot afford. Returns the pids killed."""
    repo = Path(repo).resolve()
    matches = []
    for pid in pgrep(["-f", pattern]):
        cwd = proc_cwd(pid)
        if cwd is None:
            continue
        try:
            cwd.resolve().relative_to(repo)
        except ValueError:
            continue
        matches.append(pid)
    if matches:
        kill(matches)
    return matches


def _clean_stale_artifacts(repo: Path, spec: WheelSpec,
                           log: Callable[[str], None]) -> None:
    """Remove stale compiled extensions so a clean reinstall can place a single
    correct .so — a leftover/partial one is the common cause of import-time
    "undefined symbol" / duplicate-registration crashes."""
    for pattern in spec.stale_artifact_globs:
        for p in repo.rglob(pattern):
            try:
                p.unlink()
            except OSError:
                pass
    build_dir = repo / "build"
    if build_dir.is_dir():
        import shutil
        shutil.rmtree(build_dir, ignore_errors=True)
    log("Cleared stale compiled artifacts before reinstall.")


def ensure_wheel_installed(repo: Path, commit: str, spec: WheelSpec, *,
                           python: str,
                           uv: str = "uv",
                           install_log: Path,
                           import_check_log: Path,
                           pre_checkout_head: str = "",
                           release_mode: bool = False,
                           run: RunFn = _run,
                           sleep: Callable[[float], None] = time.sleep,
                           log: Callable[[str], None] = _log) -> bool:
    """Install `spec.package` at `commit` (precompiled wheel via editable
    install) unless the current install is already healthy for exactly this
    commit. Returns True when a (re)install happened, False on skip.

    Skip requires all three: the checkout was already at `commit` before the
    pick, the import check passes, and the installed version embeds the commit
    hash (checkout alone lies when the package was never reinstalled)."""
    repo = Path(repo)
    check_snippet = build_import_check_snippet(spec)

    def import_ok() -> bool:
        rc, out, err = run([python, "-c", check_snippet], cwd=repo)
        import_check_log.write_text(out + err, encoding="utf-8")
        return rc == 0

    installed_version = read_installed_version(spec, python=python, run=run)
    matches = version_matches_commit(installed_version, commit)
    healthy = import_ok()

    if pre_checkout_head == commit and healthy and matches:
        log(f"{spec.package} already at target commit {commit[:12]}, importable, "
            f"and installed version matches ({installed_version}). Skipping reinstall.")
        return False

    if not installed_version:
        log(f"{spec.package} is not installed or version unreadable; reinstalling.")
    elif not matches:
        log(f"Installed {spec.package} version ({installed_version}) does not "
            f"match target commit {commit[:12]}; reinstalling.")
    elif pre_checkout_head != commit:
        log(f"{spec.package} checkout changed "
            f"({pre_checkout_head[:12] or '?'} -> {commit[:12]}); reinstalling.")
    else:
        log(f"{spec.package} import check failed; reinstalling.")

    retries = max(1, spec.reinstall_retries)
    for attempt in range(1, retries + 1):
        if attempt > 1:
            try:
                if _BROKEN_EXTENSION_RE.search(
                        import_check_log.read_text(encoding="utf-8",
                                                   errors="replace")):
                    log("Broken compiled extension detected; clearing stale "
                        "artifacts before reinstall.")
            except OSError:
                pass
            _clean_stale_artifacts(repo, spec, log)
            log(f"Clean-reinstalling {spec.package} for {commit[:12]} "
                f"(attempt {attempt}/{retries})...")

        log(f"Installing {spec.package} (pre-compiled) for commit {commit[:12]} "
            f"(attempt {attempt}/{retries}). Full install log: {install_log}")
        install_log.write_text("", encoding="utf-8")

        # A concurrent editable install holds the project lock forever if its
        # parent died; release it before uninstalling — scoped to THIS repo
        release_editable_install_locks(repo)

        # Every uv call must name the TARGET venv explicitly: without
        # --python, uv resolves the environment from VIRTUAL_ENV / a .venv in
        # cwd, i.e. from the LAUNCHER's shell state — a copilot started from a
        # clean shell got "No virtual environment found" while the same run
        # worked when the operator happened to have the venv activated. The
        # target venv is adapter data (`python`); the shell must not matter.
        rc, out, err = run([uv, "pip", "uninstall", "--python", python,
                            spec.package], cwd=repo)
        _append(install_log, f"--- uv pip uninstall {spec.package} ---\n{out}{err}")

        env = dict(os.environ)
        env.update(spec.install_env)
        rc, out, err = run([uv, "pip", "install", "--python", python,
                            "-e", "."], cwd=repo, env=env,
                           timeout=3600)
        _append(install_log, "--- uv pip install -e . (precompiled) ---\n"
                             f"{out}{err}")
        if rc != 0:
            if release_mode:
                # The release-branch tip may have no precompiled wheel: fall
                # back to a source build (only path where that is expected).
                log("Precompiled wheel not available; building from source...")
                rc, out, err = run([uv, "pip", "install", "--python",
                                    python, "-e", "."], cwd=repo,
                                   env=dict(os.environ), timeout=7200)
                _append(install_log, "--- uv pip install -e . (source) ---\n"
                                     f"{out}{err}")
                if rc != 0:
                    raise WheelInstallError(
                        f"source build failed for commit {commit[:12]}; "
                        f"see {install_log}")
            else:
                raise WheelInstallError(
                    f"wheel install failed for commit {commit[:12]}; "
                    f"see {install_log}")

        if import_ok():
            log(f"{spec.package} install completed and import validation passed.")
            return True
        log(f"Import validation failed (attempt {attempt}/{retries}); "
            f"see {import_check_log}")

    hint = ""
    try:
        if _BROKEN_EXTENSION_RE.search(
                import_check_log.read_text(encoding="utf-8", errors="replace")):
            hint = (" — broken compiled extension persisted across reinstalls "
                    "(stale/incompatible/duplicate .so)")
    except OSError:
        pass
    raise WheelInstallError(
        f"import validation still fails after {retries} reinstall attempt(s) "
        f"for {commit[:12]}{hint}; see {import_check_log}")


# -- Dockerfile pin -----------------------------------------------------------

def is_pinned(text: str, commit: str, pin: PinSpec) -> bool:
    url = re.escape(pin.url_template.format(commit=commit))
    var = re.escape(pin.commit_env_var)
    return bool(
        re.search(url, text)
        or re.search(rf"^[ \t]*(ENV|ARG)[ \t]+{var}={commit}[ \t]*$",
                     text, re.MULTILINE))


def pin_present(repo: Path, pin: PinSpec) -> bool:
    """True when the CI Dockerfile exists and carries ANY wheel pin (URL or
    ENV/ARG form) — the local_ci/remote_ci precondition (Rev 8 §2.2): those
    modes operate on an already-prepared tree and must refuse one whose pin
    step never ran."""
    path = Path(repo) / pin.dockerfile
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    var = re.escape(pin.commit_env_var)
    return bool(
        re.search(pin.url_pattern, text)
        or re.search(rf"^[ \t]*(ENV|ARG)[ \t]+{var}=[0-9a-f]{{40}}[ \t]*$",
                     text, re.MULTILINE))


def pin_dockerfile(repo: Path, commit: str, pin: PinSpec, *,
                   log: Callable[[str], None] = _log) -> bool:
    """Rewrite the CI Dockerfile's wheel pin to `commit`, supporting both the
    direct-URL form and the ENV/ARG variable form. The substitutions ALWAYS
    run over every form (shell parity: the seds are unconditional) — one
    already-current form must never shield a stale sibling from the rewrite.
    Returns True when the file changed, False when every pin was already
    current (`is_pinned` alone is only the signal-file short-circuit's job)."""
    path = Path(repo) / pin.dockerfile
    if not path.is_file():
        raise PinError(f"CI Dockerfile not found at: {path}")
    text = path.read_text(encoding="utf-8")

    var = re.escape(pin.commit_env_var)
    new = re.sub(pin.url_pattern, pin.url_template.format(commit=commit), text)
    new = re.sub(rf"^[ \t]*ENV[ \t]+{var}=[0-9a-f]{{40}}[ \t]*$",
                 f"ENV {pin.commit_env_var}={commit}", new, flags=re.MULTILINE)
    new = re.sub(rf"^[ \t]*ARG[ \t]+{var}=[0-9a-f]{{40}}[ \t]*$",
                 f"ARG {pin.commit_env_var}={commit}", new, flags=re.MULTILINE)

    if not is_pinned(new, commit, pin):
        raise PinError(f"failed to update wheel pin in {path}")
    if new == text:
        log(f"Wheel pin already matches commit ({commit[:12]}). Skipping.")
        return False
    path.write_text(new, encoding="utf-8")
    log(f"Wheel pin in {pin.dockerfile} updated to commit {commit[:12]}.")
    return True
