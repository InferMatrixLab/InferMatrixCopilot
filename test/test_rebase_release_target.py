"""Release targeting must work with narrow clones and reject main-only pins."""

from __future__ import annotations

import subprocess

import pytest

from infermatrix_copilot.rebase_engine.wheel import (
    WheelPickError,
    WheelSpec,
    pick_wheel_commit,
)


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def release_checkout(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q")
    git(source, "checkout", "-q", "-B", "main")
    git(source, "config", "user.name", "fixture")
    git(source, "config", "user.email", "fixture@example.com")
    (source / "api.py").write_text("base\n")
    git(source, "add", "api.py")
    git(source, "commit", "-qm", "common API")
    base = git(source, "rev-parse", "HEAD")
    git(source, "checkout", "-qb", "releases/v0.31.0")
    (source / "api.py").write_text("release\n")
    git(source, "commit", "-qam", "release API")
    release = git(source, "rev-parse", "HEAD")
    git(source, "checkout", "-q", "main")
    (source / "api.py").write_text("main\n")
    git(source, "commit", "-qam", "main-only API")
    main = git(source, "rev-parse", "HEAD")
    checkout = tmp_path / "scratch"
    subprocess.run(
        ["git", "clone", "-q", "--single-branch", "--branch", "main",
         str(source), str(checkout)], check=True
    )
    spec = WheelSpec(
        package="fixture", variant="cpu", arch="x86_64",
        index_url_template="https://example.invalid/{commit}",
    )
    return source, checkout, spec, base, release, main


def test_release_branch_is_fetched_despite_single_branch_refspec(release_checkout):
    _, checkout, spec, _, release, _ = release_checkout
    original_refspec = git(checkout, "config", "--get-all", "remote.origin.fetch")
    assert "refs/heads/main:" in original_refspec
    picked = pick_wheel_commit(
        checkout, "releases/v0.31.0", spec, probe=lambda sha: sha == release
    )
    assert picked == release
    assert git(checkout, "rev-parse", "HEAD") == release
    assert git(checkout, "config", "--get-all", "remote.origin.fetch") == original_refspec


@pytest.mark.parametrize("release_mode", [False, True])
def test_forced_main_commit_cannot_override_release_target(release_checkout, release_mode):
    _, checkout, spec, _, _, main = release_checkout
    probed = []
    with pytest.raises(WheelPickError, match="not proven to belong to origin/releases/v0.31.0"):
        pick_wheel_commit(
            checkout, "releases/v0.31.0", spec,
            probe=lambda sha: probed.append(sha) or True,
            force_commit=main, release_mode=release_mode,
        )
    assert probed == []


@pytest.mark.parametrize("pin", ["base", "release"])
def test_forced_release_ancestor_and_tip_are_allowed(release_checkout, pin):
    _, checkout, spec, base, release, _ = release_checkout
    expected = base if pin == "base" else release
    picked = pick_wheel_commit(
        checkout, "releases/v0.31.0", spec,
        probe=lambda sha: sha == expected, force_commit=expected[:10],
    )
    assert picked == expected
    assert git(checkout, "rev-parse", "HEAD") == expected


def test_deleted_remote_branch_cannot_reuse_stale_tracking_ref(release_checkout):
    source, checkout, spec, _, release, _ = release_checkout
    pick_wheel_commit(
        checkout, "releases/v0.31.0", spec, probe=lambda sha: sha == release
    )
    git(source, "branch", "-D", "releases/v0.31.0")
    assert git(checkout, "rev-parse", "origin/releases/v0.31.0") == release
    probed = []
    with pytest.raises(WheelPickError, match="could not be fetched"):
        pick_wheel_commit(
            checkout, "releases/v0.31.0", spec,
            probe=lambda sha: probed.append(sha) or True,
        )
    assert probed == []


def test_explicit_fetch_updates_rewritten_release_branch(release_checkout):
    source, checkout, spec, base, release, _ = release_checkout
    pick_wheel_commit(
        checkout, "releases/v0.31.0", spec, probe=lambda sha: sha == release
    )
    git(source, "branch", "-f", "releases/v0.31.0", base)
    assert pick_wheel_commit(
        checkout, "releases/v0.31.0", spec, probe=lambda sha: sha == base
    ) == base


def test_invalid_branch_is_rejected_before_fetch(release_checkout):
    _, checkout, spec, _, _, _ = release_checkout
    before = git(checkout, "rev-parse", "HEAD")
    with pytest.raises(WheelPickError, match="invalid upstream target branch"):
        pick_wheel_commit(checkout, "main:refs/heads/other", spec, probe=lambda sha: True)
    assert git(checkout, "rev-parse", "HEAD") == before


# -- baseline normalization ----------------------------------------------------

def test_unresolvable_baseline_fails_fast(release_checkout):
    """A baseline that resolves to nothing is a configuration error. The old
    raw-string comparison never matched it, the walk fell past the
    last-known-good commit, and an ancient wheel could be pinned silently."""
    _, checkout, spec, _, _, _ = release_checkout
    with pytest.raises(WheelPickError, match="does not resolve"):
        pick_wheel_commit(
            checkout, "main", spec,
            probe=lambda sha: False, baseline="0" * 40)


def test_abbreviated_baseline_still_resolves(release_checkout):
    """Short SHAs and tags are legitimate spellings; resolving them keeps the
    walk anchored instead of failing the raw full-SHA comparison."""
    _, checkout, spec, base, _, _ = release_checkout
    picked = pick_wheel_commit(
        checkout, "main", spec,
        probe=lambda sha: sha == base, baseline=base[:12])
    assert picked == base


def test_scratch_build_artifacts_do_not_block_branch_reset(release_checkout):
    """An interrupted wheel install leaves untracked build artifacts in the
    disposable scratch (run-202601012 resume); the picker must clean them and
    reset the branch instead of aborting."""
    _, checkout, spec, _, _, _ = release_checkout
    # pick once so the checkout sits on a main commit, then drop artifacts
    # exactly where the vllm editable build leaves them
    pick_wheel_commit(checkout, "main", spec, probe=lambda sha: True)
    artifact = checkout / "rust" / "src" / "parser" / "benches" / "qwen3_xml.rs"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("build artifact\n", encoding="utf-8")
    tracked_change = checkout / "api.py"
    tracked_change.write_text("locally modified\n", encoding="utf-8")

    picked = pick_wheel_commit(
        checkout, "main", spec, probe=lambda sha: True)

    assert picked  # no WheelPickError; branch reset succeeded
    assert not artifact.exists()
    assert tracked_change.read_text(encoding="utf-8") != "locally modified\n"


# -- busy-target install hardening ---------------------------------------------

class _FakeRun:
    """run() double for ensure_wheel_installed: pip metadata unreadable (forces
    reinstall), scripted install results, healthy import checks."""

    def __init__(self, install_results, install_stderr):
        self.install_results = list(install_results)
        self.install_stderr = install_stderr
        self.install_attempts = 0

    def __call__(self, cmd, *, cwd=None, env=None, timeout=600.0):
        joined = " ".join(cmd)
        if "uninstall" in joined:
            return 0, "", ""
        if "install" in joined and "pip" in joined:
            self.install_attempts += 1
            rc = (self.install_results.pop(0)
                  if self.install_results else 0)
            if rc:
                return rc, "", self.install_stderr
            return 0, "installed", ""
        # python -c snippets: version probe, import check
        return 0, "unparseable-version", ""


_BUSY_STDERR = ("Failed to install: fixture-1.0.0\n"
                "failed to rename .../cv2/.tmpXYZ/cv2.abi3.so to "
                ".../cv2/cv2.abi3.so: Device or resource busy (os error 16)")
_PLAIN_STDERR = "error: no matching distribution found for fixture"


def _install_spec():
    return WheelSpec(
        package="fixture", variant="cpu", arch="x86_64",
        index_url_template="https://example.invalid/{commit}",
        reinstall_retries=3,
    )


def test_busy_install_releases_holders_and_retries(tmp_path, monkeypatch, capsys):
    from infermatrix_copilot.rebase_engine import wheel
    from infermatrix_copilot.rebase_engine.wheel import ensure_wheel_installed
    releases = []
    monkeypatch.setattr(
        wheel, "release_venv_holders",
        lambda venv, **k: releases.append(str(venv)) or [4242])
    fake = _FakeRun(install_results=[1, 0], install_stderr=_BUSY_STDERR)

    installed = ensure_wheel_installed(
        tmp_path, "a" * 40, _install_spec(), python="python",
        install_log=tmp_path / "install.log",
        import_check_log=tmp_path / "import.log", run=fake)

    assert installed is True
    assert fake.install_attempts == 2
    assert len(releases) >= 2  # once before each attempt (incl. the busy one)
    # the install log is truncated per attempt by design; the busy event is
    # visible on the run console
    assert "hit a busy target" in capsys.readouterr().out


def test_busy_install_exhausts_attempts_and_raises(tmp_path, monkeypatch):
    from infermatrix_copilot.rebase_engine import wheel
    from infermatrix_copilot.rebase_engine.wheel import (
        WheelInstallError, ensure_wheel_installed,
    )
    monkeypatch.setattr(wheel, "release_venv_holders", lambda venv, **k: [])
    fake = _FakeRun(install_results=[1, 1, 1], install_stderr=_BUSY_STDERR)

    with pytest.raises(WheelInstallError, match="wheel install failed"):
        ensure_wheel_installed(
            tmp_path, "a" * 40, _install_spec(), python="python",
            install_log=tmp_path / "install.log",
            import_check_log=tmp_path / "import.log", run=fake)
    assert fake.install_attempts == 3


def test_non_busy_install_failure_still_fails_fast(tmp_path, monkeypatch):
    from infermatrix_copilot.rebase_engine import wheel
    from infermatrix_copilot.rebase_engine.wheel import (
        WheelInstallError, ensure_wheel_installed,
    )
    monkeypatch.setattr(wheel, "release_venv_holders", lambda venv, **k: [])
    fake = _FakeRun(install_results=[1], install_stderr=_PLAIN_STDERR)

    with pytest.raises(WheelInstallError, match="wheel install failed"):
        ensure_wheel_installed(
            tmp_path, "a" * 40, _install_spec(), python="python",
            install_log=tmp_path / "install.log",
            import_check_log=tmp_path / "import.log", run=fake)
    assert fake.install_attempts == 1


def test_scan_venv_holders_matches_fd_and_cwd_under_venv(tmp_path):
    from infermatrix_copilot.rebase_engine.wheel import scan_venv_holders
    venv = tmp_path / "venv"
    proc = tmp_path / "proc"

    def listdir(path):
        if path == str(proc):
            return ["1", "42", "99"]
        # fd directories of the three pids: <proc>/<pid>/fd
        return {"1": ["0"], "42": ["3", "7"], "99": ["0"]}[path.rsplit("/", 2)[-2]]

    def readlink(path):
        table = {
            f"{proc}/42/cwd": f"{venv}/bin",
            f"{proc}/42/fd/3": f"{venv}/lib/python/site-packages/cv2/cv2.abi3.so",
            f"{proc}/42/fd/7": "/elsewhere/log.txt",
            f"{proc}/99/cwd": "/home/someone",
            f"{proc}/99/fd/0": "/dev/null",
        }
        return table.get(path, "/")

    holders = scan_venv_holders(
        venv, listdir=listdir, readlink=readlink, self_pid=1,
        proc_root=str(proc))
    assert holders == {42: f"{venv}/bin"}


def test_derive_venv_root_is_logical_not_resolved(tmp_path):
    """The regression that killed run-202601012's third resume: Path("") is
    TRUTHY, the falsy-guard never fired, and resolve() turned it into the
    process cwd (the copilot checkout) — the scan then matched and killed
    the campaign's own launcher. The root must be the LOGICAL venv dir and
    must never follow the interpreter symlink out of the venv."""
    import sys
    from pathlib import Path

    from infermatrix_copilot.rebase_engine.wheel import derive_venv_root
    venv = tmp_path / "ws" / ".venv"
    (venv / "bin").mkdir(parents=True)
    interp = venv / "bin" / "python"
    interp.symlink_to(sys.executable)

    assert derive_venv_root(str(interp)) == venv
    assert derive_venv_root(str(interp), Path("later")) == Path("later")
    assert derive_venv_root(str(interp)) != Path(interp.resolve()).parent.parent


def _plausible_venv(tmp_path):
    venv = tmp_path / "venv"
    (venv / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    return venv


def test_release_refuses_implausible_venv_root(tmp_path):
    """A wrong root is worse than no scan: no venv anatomy, no kills — even
    when the scan itself would match."""
    from infermatrix_copilot.rebase_engine import wheel
    killed = []
    monkeypatched_scan_calls = []
    import pytest

    def scan(venv, **kwargs):
        monkeypatched_scan_calls.append(venv)
        return {4242: str(venv)}

    # not a venv: no pyvenv.cfg, no site-packages
    out = wheel.release_venv_holders(
        tmp_path / "not-a-venv", scan=scan,
        kill=lambda pids: killed.extend(pids))
    assert out == [] and killed == []
    assert monkeypatched_scan_calls == []  # refused BEFORE scanning


def test_release_exempts_caller_ancestors(tmp_path, monkeypatch):
    from infermatrix_copilot.rebase_engine import wheel
    venv = _plausible_venv(tmp_path)
    monkeypatch.setattr(wheel, "_proc_ancestors", lambda pid, **k: {7})
    killed = []

    out = wheel.release_venv_holders(
        venv, scan=lambda v, **k: {7: str(venv), 9: str(venv) + "/x"},
        kill=lambda pids: killed.extend(pids))

    assert out == [9] and killed == [9]


# -- baseline runtime (pre-existing classification for main campaigns) --------

def test_baseline_venv_reuses_healthy_clone(tmp_path):
    """A healthy clone from a previous attempt short-circuits — no rebuild."""
    from infermatrix_copilot.rebase_engine.wheel import ensure_baseline_venv
    target = tmp_path / "venv"
    target.mkdir()
    (tmp_path / ".git").mkdir()
    bv = tmp_path / ("venv-baseline-" + "a" * 12)
    (bv / "bin").mkdir(parents=True)
    (bv / "pyvenv.cfg").write_text("home=/usr\n", encoding="utf-8")

    def run(cmd, **kw):
        # version probe reports the baseline commit's version
        if cmd[:2] == [str(bv / "bin" / "python"), "-c"]:
            return 0, f"1.0+g{'a'*9}\n", ""
        return 0, "", ""

    cloned = []
    out = ensure_baseline_venv(
        target, _install_spec(), tmp_path, "a" * 40, run=run,
        clone=lambda s, d: cloned.append(d) or True)
    assert out == bv and cloned == []


def test_baseline_venv_failure_returns_none(tmp_path):
    """Any failure keeps the loop's regression-preserving default (None),
    never a silent pass."""
    from infermatrix_copilot.rebase_engine import wheel
    target = tmp_path / "venv"
    target.mkdir()
    (tmp_path / ".git").mkdir()

    def run(cmd, **kw):
        if "worktree" in " ".join(cmd) and "add" in cmd:
            return 1, "", "bad commit"
        return 0, "", ""

    cloned = []

    def clone(src, dst):
        cloned.append(dst)
        (dst / "bin").mkdir(parents=True)
        (dst / "pyvenv.cfg").write_text("home=/usr\n", encoding="utf-8")
        return True

    out = wheel.ensure_baseline_venv(
        target, _install_spec(), tmp_path, "a" * 40, run=run, clone=clone)
    assert out is None
    assert cloned and not cloned[0].exists()  # failed clone cleaned up


def test_baseline_venv_builds_and_installs_baseline_wheel(tmp_path):
    from infermatrix_copilot.rebase_engine.wheel import ensure_baseline_venv
    target = tmp_path / "venv"
    target.mkdir()
    (tmp_path / ".git").mkdir()
    calls = []

    def run(cmd, *, cwd=None, env=None, timeout=600.0):
        joined = " ".join(cmd)
        calls.append(joined)
        if "worktree" in joined:
            return (0, "", "") if "add" in joined or "remove" in joined \
                else (1, "", "unexpected")
        if joined.startswith("uv pip uninstall"):
            return 0, "", ""
        if joined.startswith("uv pip install"):
            assert env["VLLM_PRECOMPILED_WHEEL_COMMIT"] == "a" * 40
            assert cwd == tmp_path / ("venv-baseline-" + "a" * 12 + "-src")
            return 0, "", ""
        # version/import probes against the clone python
        return 0, "0.1\n", ""   # version mismatch -> build; import ok

    def clone(src, dst):
        (dst / "bin").mkdir(parents=True)
        (dst / "pyvenv.cfg").write_text("home=/usr\n", encoding="utf-8")
        return True

    spec = WheelSpec(
        package="fixture", variant="cpu", arch="x86_64",
        index_url_template="https://example.invalid/{commit}",
        install_env={"VLLM_PRECOMPILED_WHEEL_COMMIT": "{commit}"},
    )
    out = ensure_baseline_venv(target, spec, tmp_path, "a" * 40,
                               run=run, clone=clone)
    assert out == tmp_path / ("venv-baseline-" + "a" * 12)
    assert any("worktree" in c and "remove" in c for c in calls)


def test_hardlinked_baseline_launchers_use_clone_without_modifying_target(tmp_path):
    import venv

    from infermatrix_copilot.rebase_engine.wheel import _clone_tree_hardlinked, _relocate_venv_scripts

    target, baseline = tmp_path / "venv", tmp_path / "venv-baseline"
    venv.EnvBuilder(with_pip=False).create(target)
    launcher = target / "bin" / "runtime-probe"
    original = f"#!{target}/bin/python\nimport sys\nprint(sys.prefix)\n"
    launcher.write_text(original)
    launcher.chmod(0o755)
    assert _clone_tree_hardlinked(target, baseline)
    _relocate_venv_scripts(target, baseline)  # Cached reuse must be idempotent.
    result = subprocess.run([str(baseline / "bin" / launcher.name)],
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip() == str(baseline)
    assert launcher.read_text() == original
    assert str(target) in (target / "bin" / "activate").read_text()
    assert str(baseline) in (baseline / "bin" / "activate").read_text()


def test_baseline_package_survives_temporary_source_removal(tmp_path):
    import shutil
    import venv

    from infermatrix_copilot.rebase_engine import wheel

    target = tmp_path / "target"
    venv.EnvBuilder(with_pip=False).create(target)
    (tmp_path / ".git").mkdir()

    def run(cmd, *, cwd=None, env=None, timeout=600):
        if cmd[0] == "git":
            source = tmp_path / ("target-baseline-" + "a" * 12 + "-src")
            if "add" in cmd:
                (source / "fixture").mkdir(parents=True)
                (source / "fixture" / "__init__.py").write_text("VALUE = 'baseline'\n")
            else:
                shutil.rmtree(source)
            return 0, "", ""
        if cmd[:3] == ["fixture-uv", "pip", "uninstall"]:
            return 0, "", ""
        if cmd[:3] == ["fixture-uv", "pip", "install"]:
            python = cmd[cmd.index("--python") + 1]
            site = subprocess.check_output(
                [python, "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                text=True).strip()
            # Simulate editable/non-editable installation without network or a
            # build backend. The former becomes unimportable after git cleanup.
            from pathlib import Path
            if "-e" in cmd:
                (Path(site) / "fixture.pth").write_text(str(cwd) + "\n")
            else:
                shutil.copytree(cwd / "fixture", Path(site) / "fixture")
            return 0, "", ""
        return wheel._run(cmd, cwd=cwd, env=env, timeout=timeout)

    spec = WheelSpec(package="fixture", variant="cpu", arch="x86_64",
                     index_url_template="https://example.invalid/{commit}")
    baseline = wheel.ensure_baseline_venv(target, spec, tmp_path, "a" * 40,
                                           uv="fixture-uv", run=run)
    assert baseline is not None
    assert not (tmp_path / (baseline.name + "-src")).exists()
    result = subprocess.run([str(baseline / "bin/python"), "-c",
                             "import fixture; print(fixture.VALUE)"],
                            cwd=tmp_path, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "baseline"


def test_cached_baseline_with_matching_metadata_but_missing_code_is_rejected(tmp_path):
    from infermatrix_copilot.rebase_engine import wheel

    target = tmp_path / "target"
    baseline = tmp_path / ("target-baseline-" + "a" * 12)
    baseline.mkdir()
    (baseline / "pyvenv.cfg").write_text("home=/usr\n")
    (tmp_path / ".git").mkdir()

    def run(cmd, **kwargs):
        if len(cmd) > 3 and cmd[-1] == "fixture":
            return 0, "1.0+gaaaaaaaaa\n", ""
        return 1, "", "missing fixture source"

    result = wheel.ensure_baseline_venv(target, _install_spec(), tmp_path,
                                       "a" * 40, run=run,
                                       clone=lambda src, dst: False)
    assert result is None
