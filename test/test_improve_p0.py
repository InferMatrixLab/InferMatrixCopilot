"""Meta-improvement engine P0: trace/1 additions, the versioned index and its
gated migration, capture at the two choke points, workflow declarations and
the declared configuration fingerprint (design §3, §7.1)."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from infermatrix_copilot import tools
from infermatrix_copilot.improve import fingerprint
from infermatrix_copilot.improve.enroll import (
    BUILTIN_DIR, DeclarationError, item_for, load_declarations, lookup, parse_declaration)
from infermatrix_copilot.scopes import read_only_scope
from infermatrix_copilot.trace_store import (
    KINDS, SCHEMA_VERSION, IndexNotMigrated, MigrationError, TraceStore, bind_store,
    current_store, trace_context)

PAUSED = lambda: (True, "paused")          # noqa: E731
RUNNING = lambda: (False, "kb serve is live")  # noqa: E731


def _v0_store(tmp_path, n=3):
    """A store whose index was created by the pre-versioning code: the 13-column
    table, positional inserts, user_version 0 — and records that carry the
    new context keys (so backfill has something to fill)."""
    root = tmp_path / "traces"
    store = TraceStore(root, environ={})
    root.mkdir(parents=True)
    conn = sqlite3.connect(root / "index.db")
    conn.execute("""CREATE TABLE records (
        id TEXT PRIMARY KEY, kind TEXT, at REAL, run_id TEXT, playbook TEXT, repo TEXT,
        changeset_id TEXT, pr INTEGER, rule_ids TEXT, role TEXT, model TEXT, outcome TEXT,
        record TEXT)""")
    conn.commit()
    conn.close()
    ids = []
    for i in range(n):
        with trace_context(run_id=f"r{i}", playbook="pr-review", workflow="pr-review.agent.review_diff",
                           unit_id=f"u{i}", fingerprint="f" * 64 if i % 2 else "e" * 64):
            ids.append(store.append("decision", result={"status": "ok"})["id"])
    return store, ids


# -- schema and compatibility mode --------------------------------------------------

def test_tool_call_is_a_record_kind_and_a_fresh_index_is_versioned(tmp_path):
    assert "tool_call" in KINDS
    store = TraceStore(tmp_path / "t", environ={})
    with trace_context(run_id="r", workflow="w.s", unit_id="u1", fingerprint="abc", item="repo#1@sha"):
        rec = store.append("tool_call", inputs={"args": "{}"}, outputs={"result": "ok"},
                           result={"tool": "read_file", "ok": True, "refused": False})
    assert store.index_version() == SCHEMA_VERSION
    assert [r["id"] for r in store.query(workflow="w.s")] == [rec["id"]]
    assert [r["id"] for r in store.query(unit_id="u1", fingerprint="abc")] == [rec["id"]]
    assert store.query(fingerprint="other") == []


def test_old_index_runs_in_compatibility_mode_and_opening_never_migrates(tmp_path):
    store, ids = _v0_store(tmp_path)
    assert store.index_version() == 0                       # opening did not change it
    assert store.get(ids[0])["context"]["workflow"] == "pr-review.agent.review_diff"
    assert [r["id"] for r in store.query(run_id="r1")] == [ids[1]]
    with pytest.raises(IndexNotMigrated):
        store.query(workflow="pr-review.agent.review_diff")
    assert store.index_version() == 0
    # the v0 table has exactly the 13 legacy columns: new code wrote it by name
    conn = sqlite3.connect(store.index_path)
    assert len(conn.execute("PRAGMA table_info(records)").fetchall()) == 13
    conn.close()


# -- migration --------------------------------------------------------------------------

def test_migrate_requires_paused_writers_and_backfills_then_verifies(tmp_path):
    store, ids = _v0_store(tmp_path, n=4)
    with pytest.raises(MigrationError, match="not paused"):
        store.migrate_index(writers_paused=RUNNING)
    with pytest.raises(MigrationError, match="no writer-pause check"):
        store.migrate_index()
    assert store.index_version() == 0
    report = store.migrate_index(writers_paused=PAUSED)
    assert report["migrated"] and report["from"] == 0 and report["to"] == SCHEMA_VERSION
    assert report["backfilled"] == 4 and Path(report["backup"]).exists()
    assert store.index_version() == SCHEMA_VERSION
    assert {r["id"] for r in store.query(workflow="pr-review.agent.review_diff")} == set(ids)
    assert [r["id"] for r in store.query(fingerprint="f" * 64)] == [ids[1], ids[3]]
    # rows written after the migration carry the columns directly
    with trace_context(workflow="other.step", unit_id="u9"):
        new = store.append("decision", result={"status": "ok"})
    assert [r["id"] for r in store.query(workflow="other.step")] == [new["id"]]
    verify = store.verify_index()
    assert verify["ok"] and verify["indexed"] == verify["jsonl"] == 5
    # idempotent
    again = store.migrate_index(writers_paused=PAUSED, backup=False)
    assert again["migrated"] is False and again["from"] == SCHEMA_VERSION


def test_migration_failure_rolls_back_and_the_lock_is_exclusive(tmp_path):
    store, _ = _v0_store(tmp_path)
    with pytest.raises(RuntimeError, match="injected"):
        store.migrate_index(writers_paused=PAUSED, fail_after_alter=True)
    assert store.index_version() == 0
    conn = sqlite3.connect(store.index_path)
    assert len(conn.execute("PRAGMA table_info(records)").fetchall()) == 13   # ALTER rolled back
    conn.close()
    with trace_context(run_id="after"):
        store.append("decision", result={"status": "ok"})                    # still writable
    assert (store.root / "index.migrate.lock").read_bytes() == b"\0"   # released: payload cleared, file kept

    # two concurrent migrators: exactly one runs, the other is refused by the lock
    results: list = []
    started = threading.Barrier(2)

    def worker():
        started.wait()
        try:
            results.append(store.migrate_index(writers_paused=lambda: (_slow_paused(), ""), backup=False))
        except MigrationError as exc:
            results.append(exc)

    def _slow_paused():
        import time
        time.sleep(0.3)
        return True

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if isinstance(r, dict) and r["migrated"]) == 1
    assert sum(1 for r in results if isinstance(r, MigrationError)) == 1


def test_lock_file_is_an_os_lock_and_a_leftover_file_is_not_a_lock(tmp_path):
    import os

    from infermatrix_copilot.trace_store import _try_lock_exclusive, _unlock

    store, _ = _v0_store(tmp_path)
    lock = store.root / "index.migrate.lock"
    lock.write_text("\0" + json.dumps({"pid": 1, "at": 1.0}))
    os.utime(lock, (1.0, 1.0))                         # an old leftover file: nothing holds it
    assert store.migrate_index(writers_paused=PAUSED, backup=False)["migrated"]
    assert lock.exists() and lock.read_bytes() == b"\0"   # never unlinked, payload cleared on release
    # a lock HELD by another holder refuses, however old the file looks (portable backend)
    fd = os.open(lock, os.O_RDWR)
    assert _try_lock_exclusive(fd)
    os.lseek(fd, 1, os.SEEK_SET)
    os.write(fd, b'{"pid": 4242}')
    try:
        with pytest.raises(MigrationError, match="pid 4242"):
            store.rebuild_index(offline_confirmed=True)
    finally:
        _unlock(fd)
        os.close(fd)
    assert store.rebuild_index(offline_confirmed=True) == 3


def test_a_delayed_contender_never_joins_a_holder_in_the_critical_section(tmp_path):
    """The interleaving that broke unlink/rename recoveries: B arrives only
    after A is already inside. B must be refused, and at no time may two
    holders be inside together."""
    import time

    store, _ = _v0_store(tmp_path)
    a_inside = threading.Event()
    results: list = []
    active, peak, guard = [0], [0], threading.Lock()

    def slow_paused():                     # runs INSIDE the lock: counts concurrent holders
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        a_inside.set()
        time.sleep(0.3)
        with guard:
            active[0] -= 1
        return True, ""

    def a():
        results.append(("a", store.migrate_index(writers_paused=slow_paused, backup=False)))

    def b():
        assert a_inside.wait(5)
        try:
            results.append(("b", store.rebuild_index(writers_paused=slow_paused)))
        except MigrationError as exc:
            results.append(("b", exc))

    threads = [threading.Thread(target=a), threading.Thread(target=b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    outcome = dict(results)
    assert outcome["a"]["migrated"] and isinstance(outcome["b"], MigrationError)
    assert peak[0] == 1
    assert store.index_version() == SCHEMA_VERSION


def test_migration_catches_up_jsonl_only_records_and_rolls_back_on_verification_failure(tmp_path):
    store, ids = _v0_store(tmp_path)
    # a record the index never saw (a legacy writer whose insert failed)
    day = sorted((store.root / "records").glob("*.jsonl"))[0]
    with trace_context(run_id="lost", workflow="w.lost", unit_id="lost-1"):
        lost = store.append("decision", result={"status": "ok"})
    conn = sqlite3.connect(store.index_path)
    conn.execute("DELETE FROM records WHERE id=?", (lost["id"],))
    conn.commit()
    conn.close()
    assert not store.verify_index()["ok"]
    report = store.migrate_index(writers_paused=PAUSED, backup=False)
    assert report["caught_up"] == 1 and report["verify"]["ok"]
    assert [r["id"] for r in store.query(workflow="w.lost")] == [lost["id"]]

    # an index row with no JSONL record cannot be repaired: the whole migration
    # (schema change included) rolls back and the legacy schema stays writable
    store2, _ = _v0_store(tmp_path / "second")
    conn = sqlite3.connect(store2.index_path)
    conn.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("ghost", "decision", 1.0, "", "", "", "", None, "", "", "", "", "{}"))
    conn.commit()
    conn.close()
    with pytest.raises(MigrationError, match="rolled back"):
        store2.migrate_index(writers_paused=PAUSED, backup=False)
    conn = sqlite3.connect(store2.index_path)
    assert store2.index_version() == 0
    assert len(conn.execute("PRAGMA table_info(records)").fetchall()) == 13
    conn.close()
    assert day.exists()


# -- rebuild / rollback / compare ----------------------------------------------------

def test_rebuild_is_gated_atomic_and_can_target_schema_0_for_a_pin_downgrade(tmp_path):
    store, ids = _v0_store(tmp_path)
    store.migrate_index(writers_paused=PAUSED, backup=False)
    with trace_context(workflow="w.x", unit_id="k1"):
        extra = store.append("decision", result={"status": "ok"})
    with pytest.raises(MigrationError, match="not paused"):
        store.rebuild_index(writers_paused=RUNNING)
    with pytest.raises(MigrationError, match="unknown target"):
        store.rebuild_index(to_schema=1, offline_confirmed=True)
    assert store.rebuild_index(to_schema=0, writers_paused=PAUSED) == 4
    assert store.index_version() == 0
    assert store.get(extra["id"]) == extra                         # every record, including the late one
    assert not list(store.root.glob("index.db.tmp-*"))
    # an OLD pin (positional 13-column insert) can write the downgraded index
    old = sqlite3.connect(store.index_path)
    old.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("legacy-1", "decision", 1.0, "", "", "", "", None, "", "", "", "", "{}"))
    old.commit()
    old.close()
    # and back up to the current schema
    assert store.rebuild_index(to_schema=SCHEMA_VERSION, offline_confirmed=True) == 4
    assert store.index_version() == SCHEMA_VERSION
    assert [r["id"] for r in store.query(workflow="w.x")] == [extra["id"]]


def test_rebuild_into_an_empty_root_needs_no_pause_gate(tmp_path):
    store = TraceStore(tmp_path / "t", environ={})
    rec = store.append("decision", result={"status": "ok"})
    store.index_path.unlink()
    assert store.rebuild_index() == 1 and store.get(rec["id"]) == rec


def test_compare_is_read_only_and_flags_column_drift(tmp_path):
    store, ids = _v0_store(tmp_path)
    store.migrate_index(writers_paused=PAUSED, backup=False)
    before = store.index_path.read_bytes()
    assert store.compare_index()["ok"]
    assert store.index_path.read_bytes() == before
    conn = sqlite3.connect(store.index_path)
    conn.execute("UPDATE records SET workflow='drifted' WHERE id=?", (ids[0],))
    conn.commit()
    conn.close()
    report = store.compare_index()
    assert not report["ok"] and any("differ" in p or "index has" in p for p in report["problems"])


# -- capture at the choke points --------------------------------------------------

def test_dispatch_captures_every_tool_call_when_a_store_is_bound(tmp_path):
    store = TraceStore(tmp_path / "t", environ={})
    (tmp_path / "f.txt").write_text("hello")
    assert current_store() is None
    with bind_store(store), trace_context(run_id="r", unit_id="u"):
        ok = tools.dispatch("read_file", {"path": str(tmp_path / "f.txt")}, scope=read_only_scope())
        refused = tools.dispatch("write_file", {"path": str(tmp_path / "x"), "content": "y"},
                                 scope=read_only_scope())
        failed = tools.dispatch("read_file", {"path": str(tmp_path / "missing")}, scope=read_only_scope())
    assert ok["ok"] and not refused["ok"] and not failed["ok"]
    records = store.query(kind="tool_call", unit_id="u")
    assert [r["result"]["tool"] for r in records] == ["read_file", "write_file", "read_file"]
    assert [r["result"]["ok"] for r in records] == [True, False, False]
    assert [r["result"]["refused"] for r in records] == [False, True, False]
    assert store.blob(records[0]["outputs"]["result"]) == "hello"
    assert json.loads(store.blob(records[1]["inputs"]["args"]))["content"] == "y"
    assert records[2]["error"].startswith("FileNotFoundError")
    assert all(r["seconds"] is not None for r in records)
    # unbound again: nothing captured
    tools.dispatch("read_file", {"path": str(tmp_path / "f.txt")}, scope=read_only_scope())
    assert len(store.query(kind="tool_call")) == 3


class _FakeMessages:
    def __init__(self, fail=False):
        self.fail = fail

    def create(self, **kwargs):
        if self.fail:
            raise RuntimeError("provider down")
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="the reply"),
                     SimpleNamespace(type="tool_use", id="t1", name="read_file", input={"path": "a"})],
            usage=SimpleNamespace(input_tokens=11, output_tokens=7, cache_read_input_tokens=0,
                                  cache_creation_input_tokens=0),
            stop_reason="tool_use", model="served-model", id="req-1")


def _llm(settings, fail=False):
    from infermatrix_copilot.llm import LLM

    llm = LLM.__new__(LLM)
    llm.settings = settings
    llm._client = SimpleNamespace(messages=_FakeMessages(fail))
    llm._default_model = "m"
    llm._provider = "anthropic"
    llm._guard_served_model = lambda *a, **k: None
    return llm


def test_llm_create_captures_model_calls_including_failures(tmp_path, settings):
    store = TraceStore(tmp_path / "t", environ={})
    with bind_store(store), trace_context(run_id="r", unit_id="u"):
        reply = _llm(settings).create(system="sys", messages=[{"role": "user", "content": "hi"}],
                                      tools=[{"name": "read_file"}], max_tokens=50, role="lens")
        with pytest.raises(RuntimeError, match="provider down"):
            _llm(settings, fail=True).create(system="sys", messages=[{"role": "user", "content": "hi"}])
    assert reply.stop_reason == "tool_use"
    calls = store.query(kind="model_call", unit_id="u")
    assert len(calls) == 2
    good, bad = calls
    assert store.blob(good["inputs"]["system"]) == "sys"
    assert json.loads(store.blob(good["inputs"]["messages"]))[0]["content"] == "hi"
    assert json.loads(store.blob(good["inputs"]["tools"])) == [{"name": "read_file"}]   # replayable request
    assert "prompt" not in good["inputs"] and good["result"]["format"] == "messages/1"
    assert store.blob(good["outputs"]["reply"]) == "the reply"
    assert json.loads(store.blob(good["outputs"]["tool_calls"]))[0]["name"] == "read_file"
    assert good["model"] == {"role": "lens", "provider": "anthropic", "model": "m", "served_model": "served-model"}
    assert good["usage"]["input_tokens"] == 11 and good["result"]["max_tokens"] == 50
    assert good["result"]["stop_reason"] == "tool_use" and good["result"]["n_tool_calls"] == 1
    assert bad["error"] == "provider down" and bad["outputs"] == {} and bad["result"]["stop_reason"] == ""
    assert "tools" not in bad["inputs"]                       # none were offered on that call
    # the knowledge service's replay re-asks single-prompt records only and says so
    from infermatrix_copilot.kb_service.replay import replay
    with pytest.raises(ValueError, match="structured conversation"):
        replay(store, good["id"], gateway=None, role=None)


def test_harness_one_shot_and_session_are_captured(tmp_path, settings, monkeypatch):
    from infermatrix_copilot import providers
    from infermatrix_copilot.agent_loop import AgentOutcome
    from infermatrix_copilot.providers.harness_llm import HarnessLLM
    from infermatrix_copilot.scopes import read_only_scope

    store = TraceStore(tmp_path / "t", environ={})

    class Transport:
        spec = SimpleNamespace(id="codex", default_model="")

        def complete(self, *, system, messages, model, max_tokens, role):
            return SimpleNamespace(blocks=[SimpleNamespace(type="text", text="one-shot")], text="one-shot",
                                   model="cli-model", stop_reason="end_turn", usage={"input_tokens": 3, "output_tokens": 1})

        def run_session(self, request):
            return AgentOutcome(text="final answer", iterations=4, tool_calls=6, truncated=True,
                                refusals=["write_file"], input_tokens=100, output_tokens=20,
                                tools_used=["read_file", "grep"],
                                usage={"input_tokens": 100, "output_tokens": 20})

    transport = Transport()
    harness = HarnessLLM.__new__(HarnessLLM)
    harness.settings = settings
    harness._transport = transport
    monkeypatch.setattr(providers, "transport_for", lambda settings: transport)
    ctx = SimpleNamespace(settings=settings, state={"task_spec": {"repo": "demo"}}, run_dir=tmp_path / "run", trace=None)
    with bind_store(store), trace_context(run_id="r", unit_id="u-harness"):
        harness.create(system="s", messages=[{"role": "user", "content": "q"}])
        outcome = providers.run_harness_step(ctx, SimpleNamespace(model="m"), step_name="agent.review_diff",
                                             system="sys", prompt="the prompt", scope=read_only_scope(), max_iters=8)
    assert outcome.text == "final answer"
    one_shot, session = store.query(kind="model_call", unit_id="u-harness")
    assert one_shot["model"]["provider"] == "harness:codex" and store.blob(one_shot["outputs"]["reply"]) == "one-shot"
    assert session["model"]["provider"] == "harness:codex" and session["model"]["model"] == "m"
    assert session["result"]["session"] and session["result"]["iterations"] == 4
    assert session["result"]["truncated"] and session["result"]["tools_used"] == ["read_file", "grep"]
    assert session["usage"] == {"input_tokens": 100, "output_tokens": 20}
    assert json.loads(store.blob(session["inputs"]["messages"]))[0]["content"] == "the prompt"
    # the bridge spec written for the session carries the binding for the subprocess
    spec = json.loads((tmp_path / "run" / "bridge" / "agent.review_diff.json").read_text())
    assert spec["trace_store_root"] == str(store.root) and spec["trace_context"]["unit_id"] == "u-harness"


def test_bridge_subprocess_binds_store_and_context_from_the_spec(tmp_path):
    from infermatrix_copilot.tool_bridge import traced_call

    store = TraceStore(tmp_path / "t", environ={})
    seen = []

    def call(name, args):
        seen.append((name, current_store() is not None))
        current_store().append("decision", result={"status": "in-bridge"})
        return "ok"

    bound = traced_call(call, {"trace_store_root": str(store.root),
                               "trace_context": {"run_id": "r", "unit_id": "u-bridge", "workflow": "w.s"}})
    assert bound("read_file", {"path": "x"}) == "ok" and seen == [("read_file", True)]
    assert current_store() is None                                   # bound per call only
    rec = store.query(kind="decision", unit_id="u-bridge")[0]
    assert rec["context"]["workflow"] == "w.s" and rec["result"]["status"] == "in-bridge"
    identity = traced_call(call, {"trace_store_root": ""})
    assert identity is call


# -- declarations and fingerprint ------------------------------------------------------

def test_builtin_declarations_load_and_malformed_ones_are_refused(tmp_path):
    decls = load_declarations(environ={})
    review = decls["pr-review.agent.review_diff"]
    assert review.kind == "dynamic" and review.unit == "agent_loop" and review.capture == "full"
    assert review.playbook == "pr-review" and review.target == "agent.review_diff"
    assert review.tier2 and review.outcome_adapter.endswith(":ReviewEvalAdapter") and "diff_stat" in review.shadow_tools
    assert lookup(decls, "pr-review", "agent.review_diff") is review
    assert lookup(decls, "pr-review", "pr.fetch_diff") is None
    assert decls["rb-review.review"].shadow_tools == ()
    assert all(p.suffix == ".yaml" for p in BUILTIN_DIR.iterdir())
    # extra directory overrides the builtin one for the same workflow
    extra = tmp_path / "decls"
    extra.mkdir()
    (extra / "pr-review.yaml").write_text(
        "workflow: pr-review.agent.review_diff\nkind: static\nunit: step_call\nitem_key: '{repo}'\n"
        "fingerprint:\n  covers: [copilot_sha]\n")
    assert load_declarations([extra], environ={})["pr-review.agent.review_diff"].kind == "static"
    assert load_declarations(environ={"IMPROVE_WORKFLOWS_DIRS": str(extra)})[
        "pr-review.agent.review_diff"].unit == "step_call"
    for bad in ({"workflow": "noplaybook", "kind": "static", "unit": "step_call", "item_key": "x",
                 "fingerprint": {"covers": ["copilot_sha"]}},
                {"workflow": "a.b", "kind": "weird", "unit": "step_call", "item_key": "x",
                 "fingerprint": {"covers": ["copilot_sha"]}},
                {"workflow": "a.b", "kind": "static", "unit": "step_call", "item_key": "x",
                 "fingerprint": {"covers": []}},
                {"workflow": "a.b", "kind": "static", "unit": "step_call", "item_key": "x",
                 "fingerprint": {"covers": [{"settings": "not-a-list"}]}},
                {"workflow": "a.b", "kind": "static", "unit": "step_call", "item_key": "x",
                 "capture": "sometimes", "fingerprint": {"covers": ["copilot_sha"]}}):
        with pytest.raises(DeclarationError):
            parse_declaration(bad)


def test_item_key_formats_from_task_spec_and_state():
    decl = load_declarations(environ={})["pr-review.agent.review_diff"]
    state = {"task_spec": {"repo": "demo", "pr": 12}, "pr_head_sha": "abc123"}
    assert item_for(decl, state) == "demo#12@abc123"
    assert item_for(decl, {"task_spec": {"repo": "demo"}}) == "demo#@"


def test_fingerprint_is_deterministic_names_every_cover_and_diffs(tmp_path, settings):
    decl = parse_declaration({
        "workflow": "pr-review.agent.review_diff", "kind": "dynamic", "unit": "agent_loop",
        "item_key": "{repo}", "shadow_tools": ["grep", "calc"],
        "fingerprint": {"covers": [
            "playbook_yaml_sha", {"settings": ["review_second_round", "llm_max_tokens"]},
            {"prompts": ["engine/steps/review/prompts.py"]}, {"routing": ["ECO_MODEL"]},
            "tools", "knowledge_snapshot", "copilot_sha"]}})
    env = {"ECO_MODEL": "cheap-model"}
    digest, manifest = fingerprint.compute(decl, settings, state={"knowledge_snapshot": "snap-1"}, environ=env)
    assert manifest["complete"] and len(digest) == 64
    assert set(manifest["covers"]) == {"playbook_yaml_sha", "settings", "prompts", "routing", "tools",
                                       "knowledge_snapshot", "copilot_sha"}
    assert manifest["covers"]["tools"] == ["calc", "grep"]
    assert manifest["covers"]["routing"] == {"ECO_MODEL": "cheap-model"}
    assert "engine/steps/review/prompts.py" in manifest["covers"]["prompts"]
    again, _ = fingerprint.compute(decl, settings, state={"knowledge_snapshot": "snap-1"}, environ=env)
    assert again == digest
    # a settings change changes the fingerprint, and diff() names exactly it
    changed = settings.model_copy(update={"llm_max_tokens": settings.llm_max_tokens + 1})
    other, manifest_b = fingerprint.compute(decl, changed, state={"knowledge_snapshot": "snap-1"}, environ=env)
    assert other != digest
    assert fingerprint.diff(manifest, manifest_b) == {
        "settings": {"llm_max_tokens": (settings.llm_max_tokens, settings.llm_max_tokens + 1)}}
    # routing read from the environment first, then the same-named setting
    unset, manifest_c = fingerprint.compute(decl, settings, state={"knowledge_snapshot": "snap-1"}, environ={})
    assert unset != digest and manifest_c["covers"]["routing"]["ECO_MODEL"] == settings.eco_model


def test_fingerprint_covers_the_resolved_model_after_fallbacks(settings):
    decl = parse_declaration({
        "workflow": "pr-review.agent.review_diff", "kind": "dynamic", "unit": "agent_loop",
        "item_key": "{repo}", "fingerprint": {"covers": [{"routing": ["ECO_MODEL"]}, "resolved_models"]}})
    base = settings.model_copy(update={"eco_model": ""})
    first, manifest = fingerprint.compute(decl, base, environ={})
    assert manifest["covers"]["resolved_models"]["eco"]["model"] == base.agent_model
    # ECO_MODEL stays empty but the fallback model changes: the executed model changed
    second, manifest_b = fingerprint.compute(decl, base.model_copy(update={"agent_model": "other-model"}), environ={})
    assert second != first
    assert fingerprint.diff(manifest, manifest_b)["resolved_models"]["eco"][1]["model"] == "other-model"


def test_fingerprint_hashes_the_playbook_from_the_configured_directory(tmp_path, settings):
    decl = parse_declaration({
        "workflow": "pr-review.agent.review_diff", "kind": "dynamic", "unit": "agent_loop",
        "item_key": "{repo}", "fingerprint": {"covers": ["playbook_yaml_sha"]}})
    packaged, _ = fingerprint.compute(decl, settings, environ={})
    override = tmp_path / "playbooks"
    override.mkdir()
    (override / "pr-review.yaml").write_text("name: pr-review\nversion: 99\n")
    custom = settings.model_copy(update={"playbooks_dir": override})
    first, manifest = fingerprint.compute(decl, custom, environ={})
    assert manifest["complete"] and first != packaged
    (override / "pr-review.yaml").write_text("name: pr-review\nversion: 100\n")
    second, _ = fingerprint.compute(decl, custom, environ={})
    assert second != first                                   # the executed file's contents changed


def test_incomplete_fingerprint_names_the_missing_cover(settings):
    decl = parse_declaration({
        "workflow": "nope.step", "kind": "static", "unit": "step_call", "item_key": "{repo}",
        "fingerprint": {"covers": ["playbook_yaml_sha", {"settings": ["no_such_setting"]},
                                   {"prompts": ["nothing/here/*.py"]}]}})
    digest, manifest = fingerprint.compute(decl, settings, environ={})
    assert digest == "" and not manifest["complete"]
    assert manifest["missing"] == ["playbook:nope", "settings:no_such_setting", "prompts:nothing/here/*.py"]


# -- the executor binds the unit context ---------------------------------------------

def test_executor_binds_store_and_unit_context_per_step(tmp_path, settings):
    import asyncio

    from execution_helpers import application_executor as Executor
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepResult, StepSpec
    from infermatrix_copilot.playbooks.store import Playbook, PlaybookStep
    from infermatrix_copilot.run_trace import RunTrace
    from infermatrix_copilot.trace_store import current_context

    seen: list[dict] = []

    async def enrolled(ctx):
        seen.append(dict(current_context()))
        assert current_store() is not None
        current_store().append("decision", result={"status": "ok"})
        return StepResult(True, summary="ok")

    async def plain(ctx):
        seen.append(dict(current_context()))
        return StepResult(True, summary="ok")

    registry = StepRegistry()
    registry.register(StepSpec("agent.review_diff", "agent", "read", enrolled, "enrolled"))
    registry.register(StepSpec("report.final_summary", "report", "report", plain, "plain"))
    settings = settings.model_copy(update={"trace_store_root": str(tmp_path / "traces")})
    run_dir = tmp_path / "run-1"
    ex = Executor(registry, settings, run_dir=run_dir, trace=RunTrace(run_dir / "run_trace.jsonl"))
    playbook = Playbook(name="pr-review", version=1, status="active", task_kinds=["pr_review"], repos=[],
                        steps=[PlaybookStep(id="review", step="agent.review_diff"),
                               PlaybookStep(id="report", step="report.final_summary")])
    state = {"task_spec": {"repo": "demo", "pr": 7, "kind": "pr_review"}, "pr_head_sha": "deadbeef"}
    outcome = asyncio.run(ex.run(playbook, state))
    assert outcome.status == "done"
    review, report = seen
    assert review["workflow"] == "pr-review.agent.review_diff" and review["item"] == "demo#7@deadbeef"
    assert review["unit_id"] == "run-1:review" and review["step"] == "agent.review_diff"
    assert review["attempt"] == 1 and review["playbook"] == "pr-review"
    assert "fingerprint" in review or "fingerprint_missing" in review
    assert "workflow" not in report and report["unit_id"] == "run-1:report"   # undeclared: Tier 1
    store = TraceStore(tmp_path / "traces")
    assert store.query(unit_id="run-1:review")[0]["context"]["item"] == "demo#7@deadbeef"
    assert current_store() is None


# -- the operator commands ----------------------------------------------------------

def test_improve_cli_gates_migration_on_both_writers(tmp_path, monkeypatch, capsys):
    from infermatrix_copilot.improve import cli
    from infermatrix_copilot.kb_service.ledger import Ledger

    store, ids = _v0_store(tmp_path)
    kb_dir = tmp_path / "kb"
    ledger = Ledger(kb_dir / "kb.db")
    ledger.ensure_repo("demo", "shadow")
    marker = tmp_path / "rb.paused"
    monkeypatch.setenv("KB_STATE_DIR", str(kb_dir))
    monkeypatch.setenv("TRACE_STORE_ROOT", str(store.root))
    monkeypatch.delenv("RB_REVIEW_PAUSE_MARKER", raising=False)

    check = cli.writers_paused_check(environ={"KB_STATE_DIR": str(kb_dir)})
    paused, detail = check()
    assert not paused and "RB_REVIEW_PAUSE_MARKER" in detail
    assert cli.main(["migrate-index"]) == 1 and store.index_version() == 0
    monkeypatch.setenv("RB_REVIEW_PAUSE_MARKER", str(marker))
    assert cli.main(["migrate-index"]) == 1          # marker named but missing
    marker.write_text("")
    assert cli.main(["migrate-index"]) == 1          # marker present but no drain ack
    marker.write_text(json.dumps({"acked_at": "2026-09-30T00:00:00Z"}))
    assert cli.main(["migrate-index"]) == 1          # a live (unpaused) repository still blocks
    for row in ledger.all_repo_states():
        ledger.bump_generation(row["repo"], pause=True, reason="migration")
    owner = ledger.acquire_lease(ttl=600)             # paused, but a service instance is still running
    paused, detail = cli.writers_paused_check(environ={"KB_STATE_DIR": str(kb_dir),
                                                       "RB_REVIEW_PAUSE_MARKER": str(marker)})()
    assert not paused and "lease held" in detail
    assert cli.main(["migrate-index"]) == 1 and store.index_version() == 0
    ledger.release_lease(owner)
    assert cli.main(["migrate-index"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["migrated"] and store.index_version() == SCHEMA_VERSION
    assert cli.main(["compare-index"]) == 0 and cli.main(["verify-index"]) == 0
    assert cli.main(["rollback-index"]) == 0 and store.index_version() == 0
    assert cli.main(["rebuild-index", "--offline-confirmed"]) == 0 and store.index_version() == SCHEMA_VERSION
    assert {r["id"] for r in store.query(workflow="pr-review.agent.review_diff")} == set(ids)
