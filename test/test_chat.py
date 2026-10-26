"""Conversational interface: tool round-trips, confirmation, read jail, history."""

import shutil

import pytest

from infermatrix_copilot.chat import ChatSession, _MAX_HISTORY_MESSAGES
from infermatrix_copilot.cli import Copilot
from infermatrix_copilot.config import _REPO_ROOT
from infermatrix_copilot.llm import Block, Reply


class ScriptedChatLLM:
    """Returns scripted replies in order; records what it was asked."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []
        self.available = True

    def create(self, *, system, messages, tools=None, model=None,
               max_tokens=None, on_text=None, role=""):
        self.calls.append({"messages": [*messages], "tools": tools})
        reply = self._replies.pop(0)
        if on_text is not None:
            for b in reply.blocks:
                if b.type == "text" and b.text:
                    on_text(b.text)
        return reply


def text(t):
    return Reply(blocks=[Block(type="text", text=t)])


def tool_use(name, args, tid="t1", preamble=""):
    blocks = []
    if preamble:
        blocks.append(Block(type="text", text=preamble))
    blocks.append(Block(type="tool_use", id=tid, name=name, input=args))
    return Reply(blocks=blocks)


@pytest.fixture()
def copilot(settings, git_repo):
    from conftest import install_mini_rebase_playbook
    install_mini_rebase_playbook(settings.playbooks_dir)
    settings.repo_paths = {"vllm-omni": str(git_repo)}
    return Copilot(settings)


def _session(copilot, replies, assume_yes=True):
    copilot.llm = ScriptedChatLLM(replies)
    out_buf = []
    session = ChatSession(copilot, assume_yes=assume_yes,
                          out=lambda s: out_buf.append(s))
    return session, out_buf


def test_chat_runs_task_and_reports_result(copilot):
    session, out = _session(copilot, [
        tool_use("run_task", {"kind": "repo_rebase"},
                 preamble="I'll rebase the repo now."),
        text("Rebase finished: all steps green."),
    ])
    final = session.turn("please rebase the repo")
    assert final == "Rebase finished: all steps green."
    # the tool result fed back to the model carries the real outcome
    tool_result = session.messages[-2]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert "exit=0" in tool_result["content"]
    assert "completed_steps=['guard', 'report']" in tool_result["content"]
    # a real run dir exists — the task actually executed
    assert copilot.last_run_dir and (copilot.last_run_dir / "RUN_REPORT.md").exists()
    # the tool call was surfaced to the user
    assert any("run_task" in s for s in out)


def test_chat_confirmation_decline_is_respected(copilot, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: "n")  # user declines
    session, _ = _session(copilot, [
        tool_use("run_task", {"kind": "repo_rebase"}),
        text("Understood, not running it."),
    ], assume_yes=False)  # repo_rebase (write-capable) must ask
    session.turn("rebase the repo")
    tool_result = session.messages[-2]["content"][0]
    assert "exit=1" in tool_result["content"]  # aborted, nothing ran
    assert not copilot.settings.run_root.exists() or \
        not list(copilot.settings.run_root.iterdir())


def test_chat_read_jail(copilot, git_repo, tmp_path):
    session, _ = _session(copilot, [])
    # inside the repo: allowed
    assert "A = 1" in session._dispatch_tool("repo_read",
                                             {"path": str(git_repo / "mod_a.py")})
    # relative paths resolve against the default repo root, not the cwd
    assert "A = 1" in session._dispatch_tool("repo_read", {"path": "mod_a.py"})
    # offset/limit return a numbered line range
    ranged = session._dispatch_tool("repo_read",
                                    {"path": "mod_a.py", "offset": 1, "limit": 1})
    assert ranged == "1: A = 1"
    # outside any allowed root: refused
    secret = tmp_path / "outside.txt"
    secret.write_text("nope")
    assert session._dispatch_tool("repo_read", {"path": str(secret)}).startswith("refused")
    # .env anywhere: refused
    env = git_repo / ".env"
    env.write_text("KEY=1")
    assert "secret" in session._dispatch_tool("repo_read", {"path": str(env)})


def test_chat_status_logs_playbooks_tools(copilot):
    session, _ = _session(copilot, [])
    assert "no runs yet" in session._dispatch_tool("get_status", {})
    assert "repo-rebase-mini@1 [locked]" in session._dispatch_tool("list_playbooks", {})
    assert session._dispatch_tool("unknown_tool", {}) == "unknown tool: unknown_tool"


def test_chat_multi_round_tools_then_answer(copilot):
    session, _ = _session(copilot, [
        tool_use("list_playbooks", {}, tid="t1"),
        tool_use("get_status", {}, tid="t2"),
        text("You have the locked repo-rebase playbook; no runs yet."),
    ])
    final = session.turn("what can you do and what's the current state?")
    assert "locked repo-rebase" in final
    assert len(session.messages) == 6  # user + 2*(assistant+tool_result) + assistant


def test_chat_history_trimming_keeps_pairs(copilot):
    session, _ = _session(copilot, [])
    for i in range(_MAX_HISTORY_MESSAGES + 20):
        session.messages.append({"role": "user" if i % 2 == 0 else "assistant",
                                 "content": f"m{i}"})
    session.messages.append({"role": "user", "content": "latest"})
    session._trim_history()
    assert len(session.messages) <= _MAX_HISTORY_MESSAGES
    assert session.messages[0]["role"] == "user"
    assert isinstance(session.messages[0]["content"], str)


def test_chat_session_trace_persisted(copilot):
    session, _ = _session(copilot, [text("hello there")])
    session.turn("hi")
    events = list(session.trace.events())
    kinds = [e["kind"] for e in events]
    assert kinds == ["user", "assistant"]


# -- bounded results must say they were bounded --------------------------------
# Every model-visible cut in this module gets a marker. The layer that makes this
# non-obvious is turn()'s outer re-cap: a marker added by a handler is worthless if
# the outer slice shears it back off, so the last test asserts on what the model
# was actually handed rather than on a handler's return value.


def test_repo_grep_is_literal_and_skips_vcs(copilot, git_repo):
    session, _ = _session(copilot, [])
    (git_repo / "idx.py").write_text("a = xs[0]\nb = xs0\n")
    (git_repo / ".git" / "packed.py").write_text("xs[0] in git internals\n")
    out = session._dispatch_tool("repo_grep", {"pattern": "xs[0]"})
    assert "a = xs[0]" in out       # the literal match
    assert "b = xs0" not in out     # not the character-class match
    assert "packed.py" not in out   # .git excluded


def test_repo_grep_error_is_not_no_matches(copilot, monkeypatch):
    """Pin the exit-code branch specifically. Accepting "refused OR grep failed"
    would let the read jail satisfy the assertion without the branch ever running."""
    from infermatrix_copilot import tools

    class Failed:
        returncode, stdout = 2, ""
        stderr = "grep: permission denied"

    monkeypatch.setattr(tools, "run_command", lambda *a, **k: Failed())
    session, _ = _session(copilot, [])
    out = session._dispatch_tool("repo_grep", {"pattern": "x"})
    assert "grep failed (exit 2)" in out
    assert "permission denied" in out
    assert "no matches" not in out


def test_repo_grep_marks_truncation(copilot, git_repo):
    from infermatrix_copilot.chat import _REPO_GREP_CHARS
    session, _ = _session(copilot, [])
    (git_repo / "big.py").write_text("".join(f"HIT {i}\n" for i in range(4000)))
    out = session._dispatch_tool("repo_grep", {"pattern": "HIT"})
    assert "truncated" in out and len(out) <= _REPO_GREP_CHARS


def test_repo_read_marks_truncation_on_both_branches(copilot, git_repo):
    """Whole-file and offset/limit are separate return statements — one test
    exercises only one of them."""
    from infermatrix_copilot.chat import _REPO_READ_CHARS
    session, _ = _session(copilot, [])
    (git_repo / "huge.py").write_text("".join(f"line {i}\n" for i in range(6000)))

    whole = session._dispatch_tool("repo_read", {"path": "huge.py"})
    assert "truncated" in whole and len(whole) <= _REPO_READ_CHARS

    ranged = session._dispatch_tool("repo_read",
                                    {"path": "huge.py", "offset": 1, "limit": 6000})
    assert "truncated" in ranged and len(ranged) <= _REPO_READ_CHARS


def test_read_run_report_marks_truncation(copilot):
    from infermatrix_copilot.chat import _REPORT_CHARS
    session, _ = _session(copilot, [])
    run_dir = copilot.settings.run_root / "run-x"
    run_dir.mkdir(parents=True)
    (run_dir / "RUN_REPORT.md").write_text("R" * (_REPORT_CHARS * 2))
    copilot.last_run_dir = run_dir
    out = session._dispatch_tool("read_run_report", {})
    assert "truncated" in out


def test_run_outcome_marks_escalation_truncation(copilot):
    from infermatrix_copilot.chat import _ESCALATION_CHARS
    session, _ = _session(copilot, [])
    run_dir = copilot.settings.run_root / "run-y"
    run_dir.mkdir(parents=True)
    (run_dir / "ESCALATION.md").write_text("E" * (_ESCALATION_CHARS * 3))
    copilot.last_run_dir = run_dir
    out = session._run_outcome(3)
    assert "truncated" in out


def test_turn_marks_its_own_outer_cap(copilot):
    """The layer test: turn() re-caps EVERY tool result, so the outer cut needs its
    own disclosure — an inner marker cannot describe a slice taken after it.

    `read_run_report` is the case that actually reaches the outer cap: it joins up
    to three reports capped at 8k each (~24k), above turn()'s 20k. A handler whose
    own cap equals the outer one (repo_read) can never exercise this, which is why
    this test does not use it — with a plain outer slice such a test passes while
    the defect is fully present.
    """
    from infermatrix_copilot.chat import _TOOL_RESULT_CHARS
    session, _ = _session(copilot, [
        tool_use("read_run_report", {}),
        text("read them"),
    ])
    run_dir = copilot.settings.run_root / "run-z"
    run_dir.mkdir(parents=True)
    for f in ("RUN_REPORT.md", "ESCALATION.md", "COMPARISON.md"):
        (run_dir / f).write_text(f[0] * 9_000)
    copilot.last_run_dir = run_dir

    session.turn("show me the reports")
    delivered = session.messages[-2]["content"][0]
    assert delivered["type"] == "tool_result"
    assert len(delivered["content"]) <= _TOOL_RESULT_CHARS
    # the OUTER cut names itself; inner per-report markers cannot describe it
    assert "read_run_report result" in delivered["content"]


def test_repo_read_rejects_invalid_pagination(copilot, git_repo):
    """offset/limit are 1-based counts. Unvalidated they failed silently and
    wrongly: offset=-3 normalized to line 1, limit=-1 sliced backwards, and
    limit=0 is falsy so it became the 200-line default."""
    session, _ = _session(copilot, [])
    for args in ({"path": "mod_a.py", "offset": -3},
                 {"path": "mod_a.py", "limit": -1},
                 {"path": "mod_a.py", "limit": 0}):
        out = session._dispatch_tool("repo_read", args)
        assert "must be >= 1" in out, f"{args} was silently accepted: {out!r}"


def test_repo_grep_description_states_the_literal_contract(copilot):
    """Changing the matching semantics without telling the model turns a fix into
    a new silent-wrong-answer path."""
    from infermatrix_copilot.chat import TOOL_DEFS
    desc = next(t["description"] for t in TOOL_DEFS if t["name"] == "repo_grep")
    assert "LITERALLY" in desc and ".git" in desc


def test_repo_read_schema_pins_pagination_minimums(copilot):
    """The runtime check and the schema are two halves of the same regression;
    a schema without minima invites the model to send the values at all."""
    from infermatrix_copilot.chat import TOOL_DEFS
    props = next(t["input_schema"]["properties"] for t in TOOL_DEFS
                 if t["name"] == "repo_read")
    assert props["offset"]["minimum"] == 1
    assert props["limit"]["minimum"] == 1


def test_repo_read_rejects_non_integer_pagination(copilot):
    """int(True) is 1 and int(1.9) is 1 — coercion would hand back the wrong window."""
    session, _ = _session(copilot, [])
    for bad in (True, 1.9, "2", []):
        out = session._dispatch_tool("repo_read", {"path": "mod_a.py", "offset": bad})
        assert "must be an integer" in out, f"offset={bad!r} accepted: {out!r}"
    assert session._dispatch_tool(
        "repo_read", {"path": "mod_a.py", "offset": 1, "limit": 1}) == "1: A = 1"


def test_repo_read_treats_explicit_null_pagination_as_omitted(copilot):
    """An explicit JSON null is the same as an absent key — standard JSON-RPC
    semantics, and the host has no way to express "absent" otherwise. Pinned so the
    equivalence is a decision rather than an accident of `args.get()`."""
    session, _ = _session(copilot, [])
    assert "A = 1" in session._dispatch_tool(
        "repo_read", {"path": "mod_a.py", "offset": None, "limit": None})
