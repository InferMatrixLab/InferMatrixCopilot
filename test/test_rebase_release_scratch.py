"""Release selection fetches the real upstream through a disposable clone."""

import asyncio
import subprocess
from pathlib import Path

import pytest

from infermatrix_copilot.engine import lifecycle
from infermatrix_copilot.engine.step import StepContext
from infermatrix_copilot.engine.steps import rebase_v3
from infermatrix_copilot.rebase_engine.wheel import WheelSpec, pick_wheel_commit


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.mark.parametrize("remote", ["origin", "publisher"])
@pytest.mark.parametrize("adopted", [False, True])
def test_scratch_fetches_release_absent_from_canonical_local_branches(
        tmp_path, settings, trace, remote, adopted):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    git(source, "-c", "user.name=Fixture", "-c",
        "user.email=fixture@example.invalid", "commit", "--allow-empty",
        "-qm", "main")
    git(source, "checkout", "-qb", "releases/v0.31.0")
    git(source, "-c", "user.name=Fixture", "-c",
        "user.email=fixture@example.invalid", "commit", "--allow-empty",
        "-qm", "release")
    release = git(source, "rev-parse", "HEAD")
    git(source, "checkout", "-q", "main")

    canonical = tmp_path / "canonical"
    subprocess.run(
        ["git", "clone", "-q", "--single-branch", "--branch", "main",
         "--origin", remote, str(source), str(canonical)], check=True,
    )
    if remote != "origin":
        # The default remote has no release branch. Only the recorded custom
        # upstream can supply the requested campaign target.
        git(canonical, "remote", "add", "origin", str(canonical))
    assert git(canonical, "branch", "--list") == "* main"
    original_head = git(canonical, "rev-parse", "HEAD")
    original_refs = git(canonical, "show-ref")
    original_config = (canonical / ".git" / "config").read_bytes()

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    scratch_path = run_dir / "upstream_scratch"
    state = {"upstream_origin_path": str(canonical),
             "upstream_remote": remote}
    if adopted:
        # A surviving scratch from an earlier process still points at the
        # local checkout and needs the same remote repair as a new scratch.
        subprocess.run(
            ["git", "clone", "-q", "--shared", "--no-checkout",
             str(canonical), str(scratch_path)], check=True,
        )
        state["upstream_path"] = str(scratch_path)
    context = StepContext(settings=settings, params={}, run_dir=run_dir,
                          trace=trace, state=state)
    try:
        scratch = rebase_v3._ensure_upstream_scratch(context)
        assert scratch == str(scratch_path)
        spec = WheelSpec(
            package="fixture", variant="cpu", arch="x86_64",
            index_url_template="https://example.invalid/{commit}",
        )
        selected = pick_wheel_commit(
            Path(scratch), "releases/v0.31.0", spec,
            force_commit=release, probe=lambda commit: commit == release,
        )
        assert selected == release
        assert git(Path(scratch), "rev-parse", "HEAD") == release
        assert git(canonical, "rev-parse", "HEAD") == original_head
        assert git(canonical, "show-ref") == original_refs
        assert (canonical / ".git" / "config").read_bytes() == original_config
        assert git(canonical, "status", "--porcelain") == ""
    finally:
        asyncio.run(lifecycle.finalize(run_dir, None))
