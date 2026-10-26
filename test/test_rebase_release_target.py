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
