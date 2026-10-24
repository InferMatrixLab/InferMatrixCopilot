"""Companion PRs update citations outside knowledge/ before a retirement lands."""

from __future__ import annotations

import json
import subprocess
import sqlite3
from dataclasses import replace

import pytest

from infermatrix_copilot.kb_service import merge
from infermatrix_copilot.kb_service.companion import rewrite
from infermatrix_copilot.kb_service.runtime import collect_events, run_intake
from test_kb_flow import _ack, _flow_runtime, _items
from test_kb_intake_gate import PAGE, ScriptedGateway, _judge_all


def test_rewrites_point_to_the_replacement_or_drop_the_retired_rule():
    text = "Intro cites DEMO-1a here.\n- DEMO-1a: bounded queue\n| DEMO-1a | x |\nKeep DEMO-1ab alone.\n"
    assert rewrite(text, "DEMO-1a", "DEMO-9z") == (
        "Intro cites DEMO-9z here.\n- DEMO-9z: bounded queue\n| DEMO-9z | x |\nKeep DEMO-1ab alone.\n")
    assert rewrite(text, "DEMO-1a", None) == "Intro cites here.\nKeep DEMO-1ab alone.\n"
    import yaml

    nested = "tools:\n  check:\n    description: Consult DEMO-1a  before merging\n"
    rewritten = rewrite(nested, "DEMO-1a", None)
    assert rewritten == "tools:\n  check:\n    description: Consult before merging\n"
    assert yaml.safe_load(rewritten)["tools"]["check"]["description"] == "Consult before merging"


def _retiring_runtime(tmp_path):
    rt, lifecycle = _flow_runtime(tmp_path)
    judge = _judge_all("yes")

    def answer(role, prompt):
        if role.name == "generator":
            return {"operations": [{"kind": "retire", "page": PAGE, "rule_id": "DEMO-1a",
                                    "reason": "upstream-removed", "evidence": "PR #11"}], "rationale": "r"}
        return judge(role, prompt)

    rt.gateway = ScriptedGateway(answer)
    lifecycle = replace(lifecycle, retire_ratio=1.0)      # retiring the only rule is the point here
    rt.registry["demo"] = lifecycle
    rt.lease_owner = rt.ledger.acquire_lease("scheduler")
    return rt, lifecycle


def test_a_retirement_cited_by_a_skill_waits_for_its_companion_then_lands(tmp_path):
    from infermatrix_copilot.knowledge_service.signing import generate_private_key, load_public_key, public_key_text

    rt, lifecycle = _retiring_runtime(tmp_path)
    publisher = generate_private_key(tmp_path / "publisher.pem")
    rt.publisher_public_key = load_public_key(public_key_text(publisher.public_key()))
    collect_events(rt, lifecycle)
    knowledge_id = run_intake(rt, lifecycle)
    knowledge = rt.ledger.changeset(knowledge_id)
    assert knowledge["status"] == "companion_pending"
    companion_id = knowledge["detail"]["companion"]
    (item,) = _items(tmp_path, "open_companion_pr")
    assert set(item["body"]["files"]) == {"skills/x.md"}
    assert "DEMO-1a" not in item["body"]["files"]["skills/x.md"]
    assert _items(tmp_path, "open_pr") == []                 # the retirement itself waits
    # the publisher opened the draft; people merge it
    _ack(tmp_path, rt, publisher, kind="open_companion_pr", changeset_id=companion_id, ok=True,
         pr=50, head_sha="c" * 40)
    assert rt.ledger.changeset(companion_id)["status"] == "companion_open"
    rt.github.prs[50] = {"state": "open", "merged": False, "head": {"sha": "c" * 40}}
    assert merge.advance(rt, lifecycle) == []
    origin = tmp_path / "origin"                             # the merged companion is on main
    (origin / "skills" / "x.md").write_text(item["body"]["files"]["skills/x.md"])
    subprocess.run(["git", "-C", str(origin), "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qam",
                    "companion"], check=True)
    rt.github.prs[50] = {"state": "closed", "merged": True, "merge_commit_sha": "d" * 40,
                         "head": {"sha": "c" * 40}}
    events = merge.advance(rt, lifecycle)
    assert events[0] == f"companion_merged {companion_id}" and events[1].startswith(f"rebuilt {knowledge_id} as ")
    new_id = events[1].rsplit(" ", 1)[1]
    assert rt.ledger.changeset(knowledge_id)["status"] == "superseded"
    assert rt.ledger.changeset(new_id)["status"] == "pr_requested"     # gated and handed over
    assert any(i["body"]["changeset_id"] == new_id for i in _items(tmp_path, "open_pr"))


def test_a_closed_companion_hands_the_retirement_to_people(tmp_path):
    from infermatrix_copilot.knowledge_service.signing import generate_private_key, load_public_key, public_key_text

    rt, lifecycle = _retiring_runtime(tmp_path)
    publisher = generate_private_key(tmp_path / "publisher.pem")
    rt.publisher_public_key = load_public_key(public_key_text(publisher.public_key()))
    collect_events(rt, lifecycle)
    knowledge_id = run_intake(rt, lifecycle)
    companion_id = rt.ledger.changeset(knowledge_id)["detail"]["companion"]
    _ack(tmp_path, rt, publisher, kind="open_companion_pr", changeset_id=companion_id, ok=True,
         pr=51, head_sha="c" * 40)
    rt.github.prs[51] = {"state": "closed", "merged": False, "head": {"sha": "c" * 40}}
    assert merge.advance(rt, lifecycle) == [f"companion_closed {companion_id}"]
    assert rt.ledger.changeset(knowledge_id)["status"] == "human"
    assert rt.ledger.changesets("demo", ("closed",))[0]["kind"] == "companion"   # not an overturn


def test_the_publisher_keeps_companions_to_their_whitelist_and_never_merges_them(tmp_path):
    from test_kb_publisher import _setup

    rt, lifecycle, changeset_id, pub, gh = _setup(tmp_path)
    for path in list((tmp_path / "state" / "outbox").glob("[0-9]*.json")):
        path.unlink()
    base = rt.load_changeset_files(changeset_id)["base_sha"]
    for i, bad in enumerate(("src/x.py", "knowledge/repos/demo/core/rules.md", ".github/x.yml", "tools/x.py")):
        rt.outbox.issue("demo", "open_companion_pr", {
            "changeset_id": f"c{i}", "base_sha": base, "branch": f"kb/demo/c{i}", "files": {bad: "x\n"},
            "deleted": [], "title": "t", "body": "b"})
    assert pub.run_once()["failed"] == 4 and gh.created() == 0
    rt.outbox.issue("demo", "open_companion_pr", {
        "changeset_id": "cok", "base_sha": base, "branch": "kb/demo/cok", "files": {"skills/x.md": "ok\n"},
        "deleted": [], "title": "t", "body": "b"})
    assert pub.run_once()["performed"] == 1
    (number,) = [n for n, pr in gh.prs.items() if pr["headRefName"] == "kb/demo/cok"]
    assert gh.prs[number]["isDraft"] is True and "kb:companion" in gh.prs[number]["labels"]
    head = gh.prs[number]["headRefOid"]
    rt.outbox.refresh_control()
    rt.outbox.issue("demo", "merge", {"changeset_id": "cok", "pr": number, "head_sha": head})
    assert pub.run_once()["failed"] == 1
    assert gh.prs[number]["isDraft"] is True and gh.prs[number]["state"] == "OPEN"
    acks = [json.loads(p.read_text())["payload"] for p in (tmp_path / "state" / "inbox" / "acks").glob("*.json")]
    assert all("only by people" in a["error"] for a in acks if a["kind"] == "merge")


def test_a_rebuild_that_fails_to_fetch_is_retried_and_the_companion_stays_open(tmp_path):
    from infermatrix_copilot.knowledge_service.signing import generate_private_key, load_public_key, public_key_text

    rt, lifecycle = _retiring_runtime(tmp_path)
    publisher = generate_private_key(tmp_path / "publisher.pem")
    rt.publisher_public_key = load_public_key(public_key_text(publisher.public_key()))
    collect_events(rt, lifecycle)
    knowledge_id = run_intake(rt, lifecycle)
    companion_id = rt.ledger.changeset(knowledge_id)["detail"]["companion"]
    _ack(tmp_path, rt, publisher, kind="open_companion_pr", changeset_id=companion_id, ok=True,
         pr=52, head_sha="c" * 40)
    rt.github.prs[52] = {"state": "closed", "merged": True, "merge_commit_sha": "d" * 40,
                         "head": {"sha": "c" * 40}}
    real_fetch = rt.knowledge.fetch

    def down():
        raise RuntimeError("git fetch failed")

    rt.knowledge.fetch = down
    events = merge.advance(rt, lifecycle)
    assert any(e.startswith(f"rebuild_retry {knowledge_id}") for e in events)
    assert rt.ledger.changeset(companion_id)["status"] == "companion_open"      # still followed
    rt.knowledge.fetch = real_fetch
    events = merge.advance(rt, lifecycle)
    assert any(e.startswith(f"rebuilt {knowledge_id}") or e == f"rebuild_failed {knowledge_id}" for e in events)
    assert rt.ledger.changeset(companion_id)["status"] == "merged"


def test_a_staged_companion_is_published_after_a_crash(tmp_path):
    from infermatrix_copilot.kb_service import companion as companion_module
    from infermatrix_copilot.kb_service.scheduler import Scheduler

    rt, lifecycle = _retiring_runtime(tmp_path)
    real = companion_module.publish_companion
    companion_module.publish_companion = lambda *a, **k: "crashed"            # dies before handing over
    try:
        collect_events(rt, lifecycle)
        knowledge_id = run_intake(rt, lifecycle)
    finally:
        companion_module.publish_companion = real
    companion_id = rt.ledger.changeset(knowledge_id)["detail"]["companion"]
    assert rt.ledger.changeset(companion_id)["status"] == "companion_staged"
    assert _items(tmp_path, "open_companion_pr") == []
    Scheduler(rt)._repo_tick(lifecycle)                                        # the next tick recovers it
    assert rt.ledger.changeset(companion_id)["status"] == "pr_requested"
    assert len(_items(tmp_path, "open_companion_pr")) == 1


def test_an_unlabelled_companion_is_still_never_merged_by_automation(tmp_path):
    from test_kb_publisher import _setup

    rt, lifecycle, changeset_id, pub, gh = _setup(tmp_path)
    number = 77
    gh.prs[number] = {"number": number, "state": "OPEN", "isDraft": True, "headRefOid": "e" * 40,
                      "headRefName": "kb/demo/demo-companion-0123456789ab", "id": "PR_77", "labels": [],
                      "queued": False, "body": ""}
    for path in list((tmp_path / "state" / "outbox").glob("[0-9]*.json")):
        path.unlink()
    rt.outbox.issue("demo", "merge", {"changeset_id": "x", "pr": number, "head_sha": "e" * 40})
    assert pub.run_once()["failed"] == 1 and gh.prs[number]["isDraft"] and gh.prs[number]["state"] == "OPEN"


def _companion_open(tmp_path, rt, lifecycle, publisher, knowledge_id, pr):
    companion_id = [cs for cs in rt.ledger.changesets("demo", ("pr_requested", "companion_staged"))
                    if cs["kind"] == "companion"][0]["id"]
    _ack(tmp_path, rt, publisher, kind="open_companion_pr", changeset_id=companion_id, ok=True,
         pr=pr, head_sha="c" * 40)
    rt.github.prs[pr] = {"state": "closed", "merged": True, "merge_commit_sha": "d" * 40,
                         "head": {"sha": "c" * 40}}
    return companion_id


def test_a_companion_merged_during_a_pause_is_rebuilt_after_resume(tmp_path):
    from infermatrix_copilot.knowledge_service.signing import generate_private_key, load_public_key, public_key_text

    rt, lifecycle = _retiring_runtime(tmp_path)
    publisher = generate_private_key(tmp_path / "publisher.pem")
    rt.publisher_public_key = load_public_key(public_key_text(publisher.public_key()))
    collect_events(rt, lifecycle)
    knowledge_id = run_intake(rt, lifecycle)
    companion_id = _companion_open(tmp_path, rt, lifecycle, publisher, knowledge_id, 53)
    rt.outbox.transition(lambda: rt.ledger.bump_generation("demo", pause=True, reason="drill"))
    assert merge.advance(rt, lifecycle) == []
    assert rt.ledger.changeset(knowledge_id)["status"] == "companion_pending"
    rt.outbox.transition(lambda: rt.ledger.resume("demo"))
    events = merge.advance(rt, lifecycle)
    assert any(e.startswith(f"rebuilt {knowledge_id}") or e == f"rebuild_failed {knowledge_id}" for e in events)
    assert rt.ledger.changeset(companion_id)["status"] == "merged"


def test_legacy_unlinked_companion_still_rebuilds_parent_after_merge(tmp_path):
    from infermatrix_copilot.knowledge_service.signing import generate_private_key, load_public_key, public_key_text

    rt, lifecycle = _retiring_runtime(tmp_path)
    publisher = generate_private_key(tmp_path / "publisher.pem")
    rt.publisher_public_key = load_public_key(public_key_text(publisher.public_key()))
    collect_events(rt, lifecycle)
    knowledge_id = run_intake(rt, lifecycle)
    # Persisted legacy rows can have a child without the parent link. New gate
    # settlement creates both atomically, so construct that old shape explicitly.
    detail = dict(rt.ledger.changeset(knowledge_id)['detail'])
    detail.pop('companion')
    rt.ledger.update_changeset(knowledge_id, status='human', detail=detail)
    (knowledge,) = [cs for cs in rt.ledger.changesets("demo", ("human",)) if cs["kind"] == "intake"]
    assert "companion" not in knowledge["detail"]
    _companion_open(tmp_path, rt, lifecycle, publisher, knowledge["id"], 54)
    events = merge.advance(rt, lifecycle)
    assert any(e.startswith(f"rebuilt {knowledge['id']}") or e == f"rebuild_failed {knowledge['id']}"
               for e in events)


@pytest.mark.parametrize('failure_at', ['artifact', 'transaction', 'commit_reply'])
def test_companion_receipt_recovers_one_atomic_parent_child_without_publication(tmp_path, monkeypatch, failure_at):
    from infermatrix_copilot import persistence
    from infermatrix_copilot.kb_service.cli import main
    from infermatrix_copilot.kb_service.companion import publish_companion

    rt, lifecycle = _retiring_runtime(tmp_path)
    original_write = persistence.immutable_write_bytes
    original_stage = rt.ledger.stage_intake

    def fail_write(path, *args, **kwargs):
        if path.parent.name == 'changesets' and '-companion-' in path.name:
            raise OSError('companion artifact unavailable')
        return original_write(path, *args, **kwargs)

    def lose_reply(*args, **kwargs):
        value = original_stage(*args, **kwargs)
        if kwargs.get('expected_status'):
            raise OSError('settlement reply lost')
        return value

    if failure_at == 'artifact':
        monkeypatch.setattr(persistence, 'immutable_write_bytes', fail_write)
    elif failure_at == 'transaction':
        rt.ledger._conn.execute("CREATE TRIGGER fail_companion BEFORE INSERT ON changesets "
                                "WHEN NEW.kind='companion' BEGIN SELECT RAISE(ABORT, 'companion insert failed'); END")
    else:
        monkeypatch.setattr(rt.ledger, 'stage_intake', lose_reply)
    collect_events(rt, lifecycle)
    with pytest.raises((OSError, sqlite3.IntegrityError)):
        run_intake(rt, lifecycle)
    parent, = rt.ledger.changesets_of_kind('demo', 'intake')
    calls = list(rt.gateway.calls)
    if failure_at != 'commit_reply':
        assert parent['status'] == 'review_pending'
        assert rt.ledger.changesets_of_kind('demo', 'companion') == []
        assert rt.ledger._conn.execute('SELECT count(*) FROM verdicts').fetchone()[0] == 0
    assert not _items(tmp_path, 'open_companion_pr')
    monkeypatch.setattr(persistence, 'immutable_write_bytes', original_write)
    monkeypatch.setattr(rt.ledger, 'stage_intake', original_stage)
    rt.ledger._conn.execute('DROP TRIGGER IF EXISTS fail_companion')
    rt.ledger.release_lease(rt.lease_owner)
    args = ['--state-dir', str(rt.state_dir), 'reconcile-review', parent['id']]
    assert main(args) == 0
    assert main(args) == 0
    parent = rt.ledger.changeset(parent['id'])
    child, = rt.ledger.changesets_of_kind('demo', 'companion')
    assert parent['status'] == 'companion_pending'
    assert parent['detail']['companion'] == child['id']
    assert child['status'] == 'companion_staged'
    assert child['detail']['for_changeset'] == parent['id']
    assert 'DEMO-1a' not in rt.load_changeset_files(child['id'])['files']['skills/x.md']
    assert rt.gateway.calls == calls
    assert not _items(tmp_path, 'open_companion_pr')
    publish_companion(rt, lifecycle, child['id'])
    publish_companion(rt, lifecycle, child['id'])
    assert len(_items(tmp_path, 'open_companion_pr')) == 1
