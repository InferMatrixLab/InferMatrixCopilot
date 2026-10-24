from pathlib import Path

from infermatrix_copilot.scopes import PathScope, ToolScope, post_plan_scope, pre_plan_scope, read_only_scope
from infermatrix_copilot.tools import dispatch


def test_pre_plan_scope_blocks_source_writes(tmp_path: Path):
    plan_dir = tmp_path / "plans"
    scope = pre_plan_scope(plan_dir)

    ok = scope.check("write_file", write_path=plan_dir / "plan-v0.md")
    assert ok.allowed and not ok.out_of_scope

    refused = scope.check("write_file", write_path=tmp_path / "src" / "core.py")
    assert not refused.allowed

    # run_shell is not in the pre-plan tool set at all
    assert not scope.check("run_shell").allowed


def test_post_plan_scope_records_out_of_scope(tmp_path: Path):
    ws = tmp_path / "ws"
    scope = post_plan_scope(ws, primary=(f"{ws.as_posix()}/mod_a*",))
    inside = scope.check("write_file", write_path=ws / "mod_a.py")
    assert inside.allowed and not inside.out_of_scope
    outside = scope.check("write_file", write_path=ws / "mod_b.py")
    assert outside.allowed and outside.out_of_scope
    wall = scope.check("write_file", write_path=tmp_path / "elsewhere.py")
    assert not wall.allowed


def test_read_only_scope_refuses_writes():
    scope = read_only_scope()
    assert scope.check("read_file").allowed
    assert not scope.check("write_file", write_path="/tmp/x").allowed
    assert not scope.check("edit_file", write_path="/tmp/x").allowed


def test_dispatch_enforces_scope_and_traces(tmp_path: Path, trace):
    plan_dir = tmp_path / "plans"
    scope = pre_plan_scope(plan_dir)

    out = dispatch("write_file", {"path": str(tmp_path / "src.py"), "content": "x"},
                   scope=scope, trace=trace)
    assert not out["ok"] and "refused" in out["error"]
    assert not (tmp_path / "src.py").exists()
    assert any(True for _ in trace.events("tool_refused"))

    out = dispatch("write_file", {"path": str(plan_dir / "plan.md"), "content": "p"},
                   scope=scope, trace=trace)
    assert out["ok"]
    assert (plan_dir / "plan.md").read_text() == "p"


def test_dispatch_out_of_scope_executes_and_records(tmp_path: Path, trace):
    ws = tmp_path / "ws"
    scope = ToolScope(
        name="post_plan", allowed_tools=frozenset({"write_file"}),
        path_scope=PathScope(writable=(f"{ws.as_posix()}/*",),
                             primary=(f"{ws.as_posix()}/mod_a*",)),
    )
    out = dispatch("write_file", {"path": str(ws / "mod_b.py"), "content": "b"},
                   scope=scope, trace=trace)
    assert out["ok"] and out["out_of_scope"]
    assert (ws / "mod_b.py").exists()
    events = list(trace.events("out_of_scope_edit"))
    assert len(events) == 1 and events[0]["path"].endswith("mod_b.py")


def test_edit_file_requires_unique_match(tmp_path: Path):
    f = tmp_path / "f.txt"
    f.write_text("aaa bbb aaa")
    out = dispatch("edit_file", {"path": str(f), "old": "aaa", "new": "ccc"})
    assert not out["ok"] and "matches 2 times" in out["error"]
    out = dispatch("edit_file", {"path": str(f), "old": "bbb", "new": "ccc"})
    assert out["ok"] and f.read_text() == "aaa ccc aaa"


def test_grep_is_literal_by_default(tmp_path: Path):
    """A bracketed expression must match itself, not act as a character class.

    The regression: under `grep -e` (basic regex) `xs[0]` matches "xs" followed by
    the character "0", so it returns `b = xs0` and misses `a = xs[0]` — a plausible
    WRONG line, offered to a lens as evidence. `_sweep_targets` injects exactly this
    shape (`items[0]`) and the contracts lens is told to find each one's consumers.
    """
    (tmp_path / "f.py").write_text("a = xs[0]\nb = xs0\n")
    out = dispatch("grep", {"pattern": "xs[0]", "path": str(tmp_path)})
    assert out["ok"]
    assert "a = xs[0]" in out["result"]
    assert "b = xs0" not in out["result"]


def test_grep_regex_is_opt_in(tmp_path: Path):
    (tmp_path / "f.py").write_text("a = xs[0]\nb = xs0\n")
    out = dispatch("grep", {"pattern": r"xs\[[0-9]\]", "path": str(tmp_path),
                            "regex": True})
    assert out["ok"] and "a = xs[0]" in out["result"]


def test_grep_error_is_not_reported_as_no_matches(tmp_path: Path):
    """A failed search must not look like a completed one that found nothing.

    Previously any non-zero exit produced empty stdout and returned "(no matches)",
    so a bad path told a lens "this API has no consumers" when nothing was searched.
    """
    out = dispatch("grep", {"pattern": "anything",
                            "path": str(tmp_path / "does_not_exist")})
    assert out["ok"] is False
    assert "grep failed" in out["error"]
    assert "no matches" not in out["error"]


def test_grep_signal_death_is_an_error(tmp_path: Path, monkeypatch):
    """Signal termination is a NEGATIVE return code, which no `> 1` guard catches."""
    from infermatrix_copilot import tools

    class Killed:
        returncode, stdout, stderr = -9, "", ""

    monkeypatch.setattr(tools, "run_command", lambda *a, **k: Killed())
    out = dispatch("grep", {"pattern": "x", "path": str(tmp_path)})
    assert out["ok"] is False and "exit -9" in out["error"]


def test_bounded_never_exceeds_its_limit_even_with_a_growing_hint():
    """`hint` is a caller-supplied callable, so its length need not shrink as the
    window shrinks; a single-pass estimate overshot by 197 chars before this."""
    from infermatrix_copilot.tools import bounded

    out = bounded("x" * 10_000, 500, "t",
                  hint=lambda kept: "!" * (200 if kept < 480 else 1))
    assert len(out) <= 500
    assert out.count("x") > 100  # and it did not collapse to a marker-only result


def test_bounded_does_not_throw_away_a_window_that_fits():
    """Stopping at the first merely-safe value is not the same as finding the
    largest one: a hint that is long only at kept==total drives the first estimate
    to 0, which then trivially "fits" while most of the budget goes unused."""
    from infermatrix_copilot.tools import bounded

    total = 10_000
    out = bounded("x" * total, 1_000, "t",
                  hint=lambda kept: "!" * (900 if kept >= total else 5))
    assert len(out) <= 1_000
    assert out.count("x") > 500  # the budget is actually used


def test_bounded_escapes_a_low_local_fixed_point():
    """An orbit-following search settles wherever the first estimate leads it. This
    hint is long around kept~100 (a safe fixed point) but short at kept~950, which
    also fits — following the orbit would discard ~85% of a usable budget."""
    from infermatrix_copilot.tools import bounded

    def hint(kept: int) -> str:
        return "!" * (860 if 60 <= kept <= 140 else 5)

    out = bounded("x" * 10_000, 1_000, "t", hint=hint)
    assert len(out) <= 1_000
    assert out.count("x") > 800  # found the large window, not the small one


def test_bounded_respects_the_cap_at_tiny_limits():
    """Two earlier bugs met here: a limit below the marker's length first produced a
    sliced, unreadable marker, then an over-cap one. Neither is acceptable — the cap
    is the contract, so a small limit falls back to a compact disclosure."""
    from infermatrix_copilot.tools import bounded

    out = bounded("x" * 1_000, 12, "t")
    assert len(out) <= 12          # the cap is never exceeded
    assert "+" in out              # and it still discloses that content was cut


def test_bounded_raises_when_no_disclosure_can_fit():
    """A cap too small for even "[+N]" is a caller misconfiguration; failing loudly
    beats returning something quietly wrong."""
    import pytest

    from infermatrix_copilot.tools import bounded

    with pytest.raises(ValueError, match="too small to hold any disclosure"):
        bounded("x" * 1_000, 2, "t")


def test_bounded_returns_short_text_untouched():
    from infermatrix_copilot.tools import bounded

    assert bounded("short", 500, "t") == "short"


def test_read_file_refuses_negative_offset(tmp_path: Path):
    """A negative offset would index from the end and make the paging hint count
    backwards; the schema types offset as a plain integer, so it is reachable."""
    f = tmp_path / "f.txt"
    f.write_text("abcdef")
    out = dispatch("read_file", {"path": str(f), "offset": -3})
    assert out["ok"] is False and "offset must be >= 0" in out["error"]


def test_grep_error_stderr_is_bounded_and_marked(tmp_path: Path, monkeypatch):
    """The stderr IS the diagnostic on a failed search, so it is bounded like any
    other model-visible cut rather than sliced silently."""
    from infermatrix_copilot import tools

    class Noisy:
        returncode, stdout = 2, ""
        stderr = "E" * 5_000

    monkeypatch.setattr(tools, "run_command", lambda *a, **k: Noisy())
    out = dispatch("grep", {"pattern": "x", "path": str(tmp_path)})
    assert out["ok"] is False
    assert "truncated" in out["error"]


def test_grep_marks_truncation(tmp_path: Path):
    from infermatrix_copilot.tools import GREP_MAX_CHARS

    (tmp_path / "big.py").write_text("".join(f"HIT line {i}\n" for i in range(4000)))
    out = dispatch("grep", {"pattern": "HIT", "path": str(tmp_path)})
    assert out["ok"]
    assert "truncated" in out["result"]
    assert len(out["result"]) <= GREP_MAX_CHARS  # marker budgeted INSIDE the cap


def test_grep_excludes_vcs_and_vendor_dirs(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "packed.py").write_text("NEEDLE in git internals\n")
    (tmp_path / "real.py").write_text("NEEDLE in source\n")
    out = dispatch("grep", {"pattern": "NEEDLE", "path": str(tmp_path)})
    assert out["ok"]
    assert "real.py" in out["result"]
    assert ".git" not in out["result"]


def test_read_file_marks_empty_window_past_eof(tmp_path: Path):
    """An offset past EOF returned "" with is_error False — a successful-looking
    tool result carrying no signal at all."""
    f = tmp_path / "short.txt"
    f.write_text("tiny\n")
    out = dispatch("read_file", {"path": str(f), "offset": 10_000})
    assert out["ok"]
    assert "past end of file" in out["result"]


def test_read_file_marker_keeps_exact_paging_offset(tmp_path: Path):
    """The paging hint must name where the window ACTUALLY stopped: the marker
    consumes part of the budget, so `offset + max_bytes` would skip what it displaced."""
    f = tmp_path / "big.txt"
    f.write_text("x" * 5_000)
    out = dispatch("read_file", {"path": str(f), "max_bytes": 1_000})
    assert out["ok"] and "offset=" in out["result"]
    body = out["result"].split("\n...[")[0]
    assert f"offset={len(body)}" in out["result"]  # resumes exactly where it stopped


def test_run_shell_marks_dropped_head_and_keeps_tail(tmp_path: Path):
    """stdout keeps its TAIL (the signal is at the end) and says the head went."""
    from infermatrix_copilot.tools import SHELL_STDERR_CHARS, SHELL_STDOUT_CHARS

    out = dispatch(
        "run_shell",
        {"cmd": "echo DECOY_HEAD; python3 -c \"print('f'*20000)\"; echo TAIL_SENTINEL",
         "cwd": str(tmp_path)})
    assert out["ok"]
    assert "TAIL_SENTINEL" in out["result"]      # diagnostic tail survived
    assert "DECOY_HEAD" not in out["result"]     # head was the part dropped
    assert "head dropped" in out["result"]       # and it says so
    assert len(out["result"]) <= SHELL_STDOUT_CHARS + SHELL_STDERR_CHARS + 200


def test_relative_paths_resolve_against_scope_root(tmp_path):
    """A scope.root makes the agent's repo-relative tool paths resolve against
    the repo tree (a per-PR worktree), not the process cwd — the read_file/grep
    failures on PR-added files. Absolute paths are left untouched."""
    from dataclasses import replace

    from infermatrix_copilot.scopes import read_only_scope
    from infermatrix_copilot.tools import dispatch

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "new_file.py").write_text("MARKER = 1\n")
    scope = replace(read_only_scope(), root=str(tmp_path))

    rel = dispatch("read_file", {"path": "pkg/new_file.py"}, scope=scope)
    assert rel["ok"] and "MARKER = 1" in rel["result"]

    g = dispatch("grep", {"pattern": "MARKER", "path": "pkg"}, scope=scope)
    assert g["ok"] and "new_file.py" in g["result"]

    ab = dispatch("read_file", {"path": str(tmp_path / "pkg" / "new_file.py")},
                  scope=scope)
    assert ab["ok"] and "MARKER = 1" in ab["result"]  # absolute untouched

    # no root -> legacy behavior (resolves against cwd; relative miss is fine)
    bare = replace(read_only_scope(), root="")
    miss = dispatch("read_file", {"path": "pkg/new_file.py"}, scope=bare)
    assert not miss["ok"]  # not found against cwd


def test_read_file_rejects_non_integer_pagination(tmp_path: Path):
    """A model supplies these. True would silently mean offset 1, and a float would
    raise deep inside the slice instead of at the boundary."""
    f = tmp_path / "f.txt"
    f.write_text("abcdef")
    for bad in (True, 1.5, "3", None):
        out = dispatch("read_file", {"path": str(f), "offset": bad})
        assert out["ok"] is False, f"offset={bad!r} was accepted"
        assert "must be an integer" in out["error"]
    assert dispatch("read_file", {"path": str(f), "max_bytes": 0})["ok"] is False
    assert dispatch("read_file", {"path": str(f), "offset": 2})["ok"] is True
