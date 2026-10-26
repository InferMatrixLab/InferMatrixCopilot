"""Knowledge service after the gate: merge flow, activation, sweep, scheduler."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from infermatrix_copilot.kb_service import merge
from infermatrix_copilot.kb_service.activate import (
    ActivationError, active_link, activate, prune, rollback, snapshots_dir, verify_snapshot,
)
from infermatrix_copilot.kb_service.runtime import collect_events, publish, run_intake
from infermatrix_copilot.kb_service.scheduler import Scheduler
from infermatrix_copilot.kb_service.sweep import (
    UpstreamRepo, detect_release, index_fixes, run_sweep, structural_report,
)
from infermatrix_copilot.knowledge_service.l1 import check_changeset
from infermatrix_copilot.knowledge_service.lifecycle import Page
from infermatrix_copilot.knowledge_service.signing import (
    generate_private_key, load_public_key, public_key_text, sign, verify,
)
from infermatrix_copilot.knowledge_service.verdict import check_binding, manifests_equal
from infermatrix_copilot.knowledge_view import KnowledgeView
from test_kb_intake_gate import (
    PAGE, FakeGitHub, ScriptedGateway, _calibrate, _generator_then_judge, _git, _judge_all,
    _rule, _runtime,
)


def _open_pr(tmp_path, rt, lifecycle):
    publisher = generate_private_key(tmp_path / "publisher.pem")
    rt.publisher_public_key = load_public_key(public_key_text(publisher.public_key()))
    collect_events(rt, lifecycle)
    changeset_id = run_intake(rt, lifecycle)
    assert publish(rt, lifecycle, changeset_id) == "pr_requested"
    return changeset_id, publisher


def _ack(tmp_path, rt, publisher, **payload):
    """Answer the change set's CURRENT pending item (or an explicit item_id)."""
    acks = tmp_path / "state" / "inbox" / "acks"
    acks.mkdir(parents=True, exist_ok=True)
    pending = rt.ledger.changeset(payload["changeset_id"]).get("pending_item") or {}
    item = payload.pop("item_id", None) or pending.get("id", "none")
    (acks / f"{item}-{payload['kind']}.json").write_text(json.dumps(
        sign("kb-ack", {"item_id": item, **payload}, publisher)), encoding="utf-8")
    merge.apply_acks(rt, rt.outbox.collect_acks(rt.publisher_public_key))


class KnowledgeGitHub(FakeGitHub):
    """Upstream reads from FakeGitHub plus the knowledge repository's PR state."""

    def __init__(self):
        super().__init__()
        self.prs: dict[int, dict] = {}
        self.statuses: dict[str, str] = {}

    def _answer(self, url):
        if "/repos/JiusiServe/InferMatrixCopilot/pulls/" in url:
            return self.prs[int(url.rsplit("/", 1)[1])]
        if "/repos/JiusiServe/InferMatrixCopilot/commits/" in url:
            sha = url.split("/commits/")[1].split("/")[0]
            state = self.statuses.get(sha)
            return {"statuses": [{"context": "kb-gate", "state": state}] if state else []}
        return super()._answer(url)


def _flow_runtime(tmp_path):
    rt, lifecycle = _runtime(tmp_path, ScriptedGateway(_generator_then_judge()), mode="auto_merge", outbox=True)
    lifecycle = _calibrate(tmp_path, rt, lifecycle)
    rt.github = KnowledgeGitHub()
    return rt, lifecycle


def _items(tmp_path, kind):
    out = []
    for path in sorted((tmp_path / "state" / "outbox").glob("*.json")):
        if path.name == "control.json":
            continue
        envelope = json.loads(path.read_text(encoding="utf-8"))
        if envelope["payload"]["kind"] == kind:
            out.append(envelope["payload"])
    return out


# -- merge flow -------------------------------------------------------------------------


def test_happy_path_signs_a_verdict_bound_to_the_pr_and_hands_it_to_the_publisher(tmp_path):
    rt, lifecycle = _flow_runtime(tmp_path)
    changeset_id, publisher = _open_pr(tmp_path, rt, lifecycle)
    head = "d" * 40
    _ack(tmp_path, rt, publisher, kind="open_pr", changeset_id=changeset_id, ok=True, pr=42, head_sha=head)
    assert rt.ledger.changeset(changeset_id)["status"] == "pr_open"
    rt.github.prs[42] = {"state": "open", "merged": False, "head": {"sha": head}}

    assert merge.advance(rt, lifecycle) == [f"merge issued {changeset_id}"]
    assert rt.ledger.changeset(changeset_id)["status"] == "merge_requested"
    body = _items(tmp_path, "merge")[0]["body"]
    assert (body["changeset_id"], body["pr"], body["head_sha"]) == (changeset_id, 42, head)
    verdict = verify("kb-gate-verdict", body["verdict"], load_public_key(public_key_text(rt.outbox._key.public_key())))
    check_binding(verdict, repository="JiusiServe/InferMatrixCopilot", pr=42, head_sha=head, now=rt.clock())
    base = rt.knowledge.knowledge_files(rt.ledger.changeset(changeset_id)["detail"]["base_sha"])
    head_files = {**base, **rt.load_changeset_files(changeset_id)["files"]}
    from infermatrix_copilot.knowledge_service.verdict import manifest_from_files
    touched = set(rt.load_changeset_files(changeset_id)["files"])
    assert manifests_equal(verdict["manifest"], manifest_from_files(
        {k: v for k, v in base.items() if k in touched}, {k: v for k, v in head_files.items() if k in touched}))
    assert all(b["verdict"] == "pass" for b in verdict["blocks"])
    assert not _items(tmp_path, "post_verdict") and not _items(tmp_path, "enqueue")

    _ack(tmp_path, rt, publisher, kind="merge", changeset_id=changeset_id, ok=True, pr=42, head_sha=head,
         merge_sha="e" * 40, post_check="passed", problems=[])
    changeset = rt.ledger.changeset(changeset_id)
    assert changeset["status"] == "merged" and changeset["merge_sha"] == "e" * 40


def test_head_change_after_signing_goes_to_people(tmp_path):
    rt, lifecycle = _flow_runtime(tmp_path)
    changeset_id, publisher = _open_pr(tmp_path, rt, lifecycle)
    _ack(tmp_path, rt, publisher, kind="open_pr", changeset_id=changeset_id, ok=True, pr=5, head_sha="1" * 40)
    rt.github.prs[5] = {"state": "open", "merged": False, "head": {"sha": "2" * 40}}
    assert merge.advance(rt, lifecycle) == [f"head_changed {changeset_id}"]
    assert rt.ledger.human_queue("demo")



def test_one_merge_in_flight_per_repository(tmp_path):
    rt, lifecycle = _flow_runtime(tmp_path)
    first, publisher = _open_pr(tmp_path, rt, lifecycle)
    rt.ledger.update_changeset(first, status="merge_requested", pr_number=1, head_sha="a" * 40)
    rt.github.prs[1] = {"state": "open", "merged": False, "head": {"sha": "a" * 40}}
    second = rt.ledger.new_changeset_id("demo", "intake")
    rt.ledger.stage_intake(rt.ledger.acquire_lease("t"), "demo", second, detail={}, status="pr_open",
                           verdicts=[], human_reason="", drafted_events=[])
    rt.ledger.update_changeset(second, pr_number=2, head_sha="b" * 40)
    rt.github.prs[2] = {"state": "open", "merged": False, "head": {"sha": "b" * 40}}
    assert merge.advance(rt, lifecycle) == []          # waits for the first merge to be answered
    assert not _items(tmp_path, "merge")


def test_refused_publisher_action_fails_and_is_queued_for_people(tmp_path):
    rt, lifecycle = _flow_runtime(tmp_path)
    changeset_id, publisher = _open_pr(tmp_path, rt, lifecycle)
    _ack(tmp_path, rt, publisher, kind="open_pr", changeset_id=changeset_id, ok=False, error="patch mismatch")
    assert rt.ledger.changeset(changeset_id)["status"] == "failed"
    assert any("patch mismatch" in row["reason"] for row in rt.ledger.human_queue("demo"))


def test_a_refused_merge_goes_by_what_the_local_gate_said(tmp_path):
    rt, lifecycle = _flow_runtime(tmp_path)
    changeset_id, publisher = _open_pr(tmp_path, rt, lifecycle)
    head = "d" * 40
    _ack(tmp_path, rt, publisher, kind="open_pr", changeset_id=changeset_id, ok=True, pr=42, head_sha=head)
    rt.github.prs[42] = {"state": "open", "merged": False, "head": {"sha": head}}

    def refused(**ack):
        merge.advance(rt, lifecycle)
        _ack(tmp_path, rt, publisher, kind="merge", changeset_id=changeset_id, ok=False, **ack)
        return rt.ledger.changeset(changeset_id)

    # the moment, not the change: signed again next pass
    assert refused(error="main kept moving while PR #42 was checked")["status"] == "pr_open"
    # context moved: rebuilt on current main
    assert refused(error="local gate: context changed since the verdict was judged: x",
                   problems=["context changed since the verdict was judged: x"])["status"] == "rebuild_needed"
    rt.ledger.update_changeset(changeset_id, status="pr_open")
    # the change itself: never merged; our own change is refined (test_kb_refine)
    changeset = refused(error="local gate: L1 dangling_reference", problems=["L1 dangling_reference a b"])
    assert changeset["status"] == "refine_needed"
    assert changeset["detail"]["gate_problems"] == ["L1 dangling_reference a b"]
    rt.ledger.update_changeset(changeset_id, status="pr_open")
    # how the repository is set up: refining cannot help, people decide
    changeset = refused(error="local gate: queued", problems=["PR #42 was queued ...: main must allow a direct "
                                                              "merge by the publisher"])
    assert changeset["status"] == "gate_failed"
    assert any("local gate refused" in row["reason"] for row in rt.ledger.human_queue("demo"))


def test_a_failed_post_merge_check_pauses_the_repository(tmp_path):
    rt, lifecycle = _flow_runtime(tmp_path)
    changeset_id, publisher = _open_pr(tmp_path, rt, lifecycle)
    head = "d" * 40
    _ack(tmp_path, rt, publisher, kind="open_pr", changeset_id=changeset_id, ok=True, pr=42, head_sha=head)
    rt.github.prs[42] = {"state": "open", "merged": False, "head": {"sha": head}}
    merge.advance(rt, lifecycle)
    _ack(tmp_path, rt, publisher, kind="merge", changeset_id=changeset_id, ok=True, pr=42, head_sha=head,
         merge_sha="e" * 40, post_check="failed", problems=["L1 dangling_reference x y"])
    assert rt.ledger.changeset(changeset_id)["status"] == "merged"
    assert rt.ledger.repo_state("demo")["paused"] == 1
    assert any("post-merge check failed" in row["reason"] for row in rt.ledger.human_queue("demo"))


def test_a_receipt_arriving_after_github_showed_the_merge_still_counts(tmp_path):
    rt, lifecycle = _flow_runtime(tmp_path)
    changeset_id, publisher = _open_pr(tmp_path, rt, lifecycle)
    head = "d" * 40
    _ack(tmp_path, rt, publisher, kind="open_pr", changeset_id=changeset_id, ok=True, pr=42, head_sha=head)
    rt.github.prs[42] = {"state": "open", "merged": False, "head": {"sha": head}}
    merge.advance(rt, lifecycle)
    item = rt.ledger.changeset(changeset_id)["pending_item"]["id"]
    rt.github.prs[42] = {"state": "closed", "merged": True, "merge_commit_sha": "e" * 40, "head": {"sha": head}}
    assert f"merged {changeset_id}" in merge.advance(rt, lifecycle)   # seen on GitHub first
    assert rt.ledger.repo_state("demo")["paused"] == 0
    _ack(tmp_path, rt, publisher, kind="merge", changeset_id=changeset_id, ok=True, pr=42, head_sha=head,
         merge_sha="e" * 40, post_check="failed", problems=["L1 dangling_reference x y"], item_id=item)
    assert rt.ledger.repo_state("demo")["paused"] == 1
    assert rt.ledger.changeset(changeset_id)["detail"]["post_check"] == "failed"


def test_merged_retirements_become_purge_candidates_next_release(tmp_path):
    rt, lifecycle = _flow_runtime(tmp_path)
    cs = rt.ledger.new_changeset_id("demo", "sweep")
    rt.ledger.stage_intake(rt.ledger.acquire_lease("t"), "demo", cs, kind="sweep", status="merge_requested",
                           verdicts=[], human_reason="", drafted_events=[],
                           detail={"release": "v1", "operations": [
                               {"kind": "retire", "page": PAGE, "rule_id": "DEMO-1a"}]})
    rt.ledger.update_changeset(cs, pr_number=9, head_sha="c" * 40)
    rt.github.prs[9] = {"state": "closed", "merged": True, "merge_commit_sha": "f" * 40, "head": {"sha": "c" * 40}}
    merge.advance(rt, lifecycle)
    assert [r["rule_id"] for r in rt.ledger.purge_eligible("demo", "v2")] == ["DEMO-1a"]
    assert rt.ledger.purge_eligible("demo", "v1") == []


# -- activation ---------------------------------------------------------------------------

def _commit_change(rt, rel: str, text: str) -> str:
    origin = Path(rt.knowledge.path).parent / "origin"
    (origin / "knowledge" / rel).write_text(text, encoding="utf-8")
    _git(origin, "add", ".")
    _git(origin, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "change")
    return rt.knowledge.fetch()


def _add_agents(rt):
    origin = Path(rt.knowledge.path).parent / "origin"
    (origin / "knowledge" / "AGENTS.md").write_text("# agents\n", encoding="utf-8")
    _git(origin, "add", ".")
    _git(origin, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "agents")


def test_activation_switches_atomically_and_rolls_back(tmp_path, monkeypatch):
    rt, lifecycle = _flow_runtime(tmp_path)
    _add_agents(rt)
    first = rt.knowledge.fetch()
    activate(rt, first, verify_provenance=False)
    assert Path(active_link(rt.state_dir)).resolve().name == first
    second = _commit_change(rt, "repos/demo/guide.md", "# guide\n")
    activate(rt, second, verify_provenance=False)
    monkeypatch.setenv("KNOWLEDGE_ROOT", str(active_link(rt.state_dir)))
    from infermatrix_copilot.knowledge_view import _load_view
    _load_view.cache_clear()
    assert KnowledgeView.current().snapshot == second
    rollback(rt, first)
    _load_view.cache_clear()
    assert KnowledgeView.current().snapshot == first
    assert rt.ledger.active_snapshot() == first


def test_activation_refuses_a_broken_tree(tmp_path):
    rt, lifecycle = _flow_runtime(tmp_path)
    _add_agents(rt)
    good = rt.knowledge.fetch()
    activate(rt, good, verify_provenance=False)
    bad = _commit_change(rt, "repos/demo/_routes.yaml",
                         "schema_version: 1\nowners:\n  - owner: core\n    path: repos/demo/missing.md\n")
    with pytest.raises(ActivationError):
        activate(rt, bad, verify_provenance=False)
    assert Path(active_link(rt.state_dir)).resolve().name == good  # unchanged


def test_prune_keeps_the_active_snapshot(tmp_path):
    state = tmp_path / "state"
    import os

    for n in range(4):
        path = snapshots_dir(state) / f"s{n}"
        path.mkdir(parents=True)
        os.utime(path, (1000 + n, 1000 + n))  # s3 newest
    active_link(state).symlink_to((snapshots_dir(state) / "s0").resolve())
    prune(state, keep=1)
    remaining = sorted(p.name for p in snapshots_dir(state).iterdir())
    assert remaining == ["s0", "s3"]  # newest kept, and the active one


# -- sweep --------------------------------------------------------------------------------

def _upstream(tmp_path) -> tuple[UpstreamRepo, str, str]:
    work = tmp_path / "up-work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    (work / "demo" / "core").mkdir(parents=True)
    (work / "demo" / "core" / "q.py").write_text("LIMIT = 10\n", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "v1")
    _git(work, "tag", "v1")
    (work / "demo" / "core" / "q.py").write_text("# unbounded now\n", encoding="utf-8")
    _git(work, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-am", "v2")
    _git(work, "tag", "v2")
    bare = tmp_path / "up.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    repo = UpstreamRepo(bare, "org/demo")
    repo.sync = lambda: None  # offline
    return repo, repo.resolve("v1"), repo.resolve("v2")


def test_first_release_records_a_baseline_then_the_next_is_swept(tmp_path):
    rt, lifecycle = _flow_runtime(tmp_path)
    lifecycle = replace(lifecycle, release=replace(lifecycle.release, trigger="github_release"))
    upstream, v1, v2 = _upstream(tmp_path)
    tags = iter(["v1", "v2"])
    rt.github.latest_release = lambda full_name: {"tag": next(tags), "published_at": ""}
    assert detect_release(rt, lifecycle, upstream) is None
    assert rt.ledger.get_cursor("demo", "sweep_baseline") == v1
    assert detect_release(rt, lifecycle, upstream) == {"tag": "v2", "from_sha": v1, "to_sha": v2,
                                                      "reason": "release"}


def test_sweep_retires_an_invalidated_rule_through_the_gate(tmp_path):
    def answer(role, prompt):
        if role.name == "generator":
            assert "unbounded now" in prompt  # the release diff reached the generator
            return {"operations": [{"kind": "retire", "page": PAGE, "rule_id": "DEMO-1a",
                                    "reason": "upstream-removed", "evidence": "release v2"}]}
        return _judge_all("yes")(role, prompt)

    rt, lifecycle = _flow_runtime(tmp_path)
    rt.gateway = ScriptedGateway(answer)
    lifecycle = replace(lifecycle, retire_ratio=1.0)
    upstream, v1, v2 = _upstream(tmp_path)
    owner = rt.ledger.acquire_lease("sweeper")
    report = run_sweep(rt, lifecycle, owner, {"tag": "v2", "from_sha": v1, "to_sha": v2, "reason": "release"},
                       upstream)
    [changeset_id] = report["changesets"]
    changeset = rt.ledger.changeset(changeset_id)
    assert changeset["kind"] == "sweep"
    # the fixture's skills/x.md cites DEMO-1a: retiring it needs a companion PR,
    # so the gated change set goes to people instead of the merge queue
    assert changeset["status"] == "human"
    assert any("companion PR" in r for r in changeset["detail"]["decision"]["reasons"])
    assert rt.ledger.get_cursor("demo", "sweep_baseline") == v2


@pytest.mark.parametrize('fault', ['unknown', 'crash'])
def test_sweep_generation_retains_dispatch_identity_across_interruption(tmp_path, fault):
    from infermatrix_copilot.kb_service.models import ModelUnavailable
    rt, lifecycle = _flow_runtime(tmp_path)
    upstream, v1, v2 = _upstream(tmp_path)
    owner = rt.ledger.acquire_lease('sweeper')
    sweep = {'tag': 'v2', 'from_sha': v1, 'to_sha': v2, 'reason': 'release'}

    def interrupted(role, prompt):
        if fault == 'crash':
            raise KeyboardInterrupt('worker interrupted')
        error = ModelUnavailable('native outcome unknown', allow_fallback=False)
        error.native_call_sent, error.native_call_completed = True, False
        raise error

    rt.gateway = ScriptedGateway(interrupted)
    with pytest.raises(KeyboardInterrupt if fault == 'crash' else ModelUnavailable):
        run_sweep(rt, lifecycle, owner, sweep, upstream)
    calls = list(rt.gateway.calls)
    assert len(calls) == 1
    states = rt.ledger.cursors_with_prefix('demo', 'draft:sweep:')
    state, = [json.loads(value) for value in states.values()]
    assert state['draft_dispatched'] and 'draft_result' not in state
    with pytest.raises(ModelUnavailable, match='reconcile original dispatch'):
        run_sweep(rt, lifecycle, owner, sweep, upstream)
    assert rt.gateway.calls == calls
    assert rt.ledger.changesets_of_kind('demo', 'sweep') == []


def test_sweep_reuses_completed_generation_before_gate_reservation(tmp_path, monkeypatch):
    from infermatrix_copilot.kb_service import runtime
    rt, lifecycle = _flow_runtime(tmp_path)
    upstream, v1, v2 = _upstream(tmp_path)
    owner = rt.ledger.acquire_lease('sweeper')
    sweep = {'tag': 'v2', 'from_sha': v1, 'to_sha': v2, 'reason': 'release'}

    def answer(role, prompt):
        if role.name == 'generator':
            return {'operations': [{'kind': 'retire', 'page': PAGE, 'rule_id': 'DEMO-1a',
                                    'reason': 'upstream-removed', 'evidence': 'release v2'}]}
        return _judge_all('yes')(role, prompt)

    rt.gateway = ScriptedGateway(answer)
    original_gate = runtime.gate_and_stage

    def interrupted(*args, **kwargs):
        raise OSError('interrupted before gate reservation')

    monkeypatch.setattr(runtime, 'gate_and_stage', interrupted)
    with pytest.raises(OSError, match='before gate reservation'):
        run_sweep(rt, lifecycle, owner, sweep, upstream)
    generator_calls = [c for c in rt.gateway.calls if c[0] == 'generator']
    assert len(generator_calls) == 1
    assert rt.ledger.changesets_of_kind('demo', 'sweep') == []
    monkeypatch.setattr(runtime, 'gate_and_stage', original_gate)
    report = run_sweep(rt, lifecycle, owner, sweep, upstream)
    assert report['complete'] and len(report['changesets']) == 1
    assert [c for c in rt.gateway.calls if c[0] == 'generator'] == generator_calls


@pytest.mark.parametrize('failure_at', ['gate_return', 'release', 'settlement'])
def test_sweep_resume_reuses_settled_gate_before_progress_write(tmp_path, monkeypatch, failure_at):
    from infermatrix_copilot.kb_service import runtime, sweep as sweep_module

    def answer(role, prompt):
        if role.name == 'generator':
            return {'operations': [{'kind': 'retire', 'page': PAGE, 'rule_id': 'DEMO-1a',
                                    'reason': 'upstream-removed', 'evidence': 'release v2'}]}
        return _judge_all('yes')(role, prompt)

    rt, lifecycle = _flow_runtime(tmp_path)
    rt.gateway = ScriptedGateway(answer)
    upstream, v1, v2 = _upstream(tmp_path)
    owner = rt.ledger.acquire_lease('sweeper')
    sweep = {'tag': 'v2', 'from_sha': v1, 'to_sha': v2, 'reason': 'release'}
    module, name = ((runtime, 'gate_and_stage') if failure_at == 'gate_return'
                    else (sweep_module, '_release_held'))
    if failure_at == 'settlement':
        module, name = rt.ledger, 'stage_intake'
    original = getattr(module, name)

    def interrupt(*args, **kwargs):
        if failure_at == 'settlement':
            if kwargs.get('expected_status'):
                raise OSError('response lost before progress checkpoint')
            return original(*args, **kwargs)
        original(*args, **kwargs)
        raise OSError('response lost before progress checkpoint')

    monkeypatch.setattr(module, name, interrupt)
    with pytest.raises(OSError, match='response lost'):
        run_sweep(rt, lifecycle, owner, sweep, upstream)
    row, = rt.ledger.changesets_of_kind('demo', 'sweep')
    calls = list(rt.gateway.calls)
    monkeypatch.setattr(module, name, original)
    if failure_at == 'settlement':
        from infermatrix_copilot.kb_service.gate_receipts import settle_gate_result
        assert row['status'] == 'review_pending'
        settle_gate_result(rt.ledger, rt.state_dir, owner, row['id'])
    report = run_sweep(rt, lifecycle, owner, sweep, upstream)
    assert rt.gateway.calls == calls
    assert report['changesets'] == [row['id']]
    assert report['complete']
    assert len(rt.ledger.changesets_of_kind('demo', 'sweep')) == 1


def test_sweep_circuit_breaker_routes_everything_to_people(tmp_path):
    def answer(role, prompt):
        if role.name == "generator":
            return {"operations": [{"kind": "retire", "page": PAGE, "rule_id": "DEMO-1a",
                                    "reason": "upstream-removed", "evidence": "release v2"}]}
        return _judge_all("yes")(role, prompt)

    rt, lifecycle = _flow_runtime(tmp_path)
    rt.gateway = ScriptedGateway(answer)
    upstream, v1, v2 = _upstream(tmp_path)
    report = run_sweep(rt, lifecycle, rt.ledger.acquire_lease("s"),
                       {"tag": "v2", "from_sha": v1, "to_sha": v2, "reason": "release"}, upstream)
    assert "circuit breaker" in report["breaker"]  # 1 of 1 active rules > 10 %
    assert rt.ledger.changeset(report["changesets"][0])["status"] == "human"


def test_t1_index_repair_passes_l1_for_existing_unlisted_pages():
    base = {
        "repos/demo/core/_index.md": "# core\n\n- [rules](rules.md)\n",
        "repos/demo/core/rules.md": "---\ntitle: \"r\"\ntype: rule\nsources: []\n---\n\n# r\n",
        "repos/demo/core/notes.md": "---\ntitle: \"Notes\"\n---\n\n# notes\n",
    }
    report = structural_report(base, "repos/demo")
    assert report["unlisted"] == ["repos/demo/core/notes.md"]
    head = {**base, **index_fixes(base, report["unlisted"])}
    from infermatrix_copilot.kb_service.gate import changes_between
    result = check_changeset(base, head, changes_between(base, head))
    assert result.ok, result.issues


# -- scheduler ----------------------------------------------------------------------------

def test_scheduler_tick_runs_intake_and_signs_control(tmp_path):
    rt, lifecycle = _flow_runtime(tmp_path)
    _add_agents(rt)
    scheduler = Scheduler(rt)
    scheduler.serve(once=True)
    control = json.loads((tmp_path / "state" / "outbox" / "control.json").read_text(encoding="utf-8"))
    assert control["purpose"] == "kb-control"
    assert _items(tmp_path, "open_pr")
    assert rt.ledger.active_snapshot() == rt.knowledge.main_sha()
    assert any(e["event"] == "intake" for e in scheduler.log)


def test_overturn_breaker_pauses_and_signs_nothing_more(tmp_path):
    rt, lifecycle = _flow_runtime(tmp_path)
    owner = rt.ledger.acquire_lease("t")
    for n in (1, 2):
        cs = rt.ledger.new_changeset_id("demo", "intake")
        rt.ledger.stage_intake(owner, "demo", cs, detail={}, status="closed", verdicts=[],
                               human_reason="", drafted_events=[])
    open_cs = rt.ledger.new_changeset_id("demo", "intake")
    rt.ledger.stage_intake(owner, "demo", open_cs, detail={}, status="pr_open", verdicts=[],
                           human_reason="", drafted_events=[])
    rt.ledger.update_changeset(open_cs, pr_number=77, head_sha="a" * 40)
    rt.ledger.release_lease(owner)
    Scheduler(rt)._overturn_breaker(lifecycle)
    assert rt.ledger.repo_state("demo")["paused"] == 1
    rt.github.prs[77] = {"state": "open", "merged": False, "head": {"sha": "a" * 40}}
    assert merge.advance(rt, lifecycle) == [] and not _items(tmp_path, "merge")
def test_scheduler_isolates_a_failing_repository(tmp_path):
    rt, lifecycle = _flow_runtime(tmp_path)
    broken = replace(lifecycle, repo="broken", full_name="org/broken", knowledge_dir="repos/broken")
    rt.ledger.ensure_repo("broken", "auto_merge")
    rt.registry = {"broken": broken, "demo": lifecycle}
    original = merge.advance

    def advance(rt_, lc):
        if lc.repo == "broken":
            raise RuntimeError("boom")
        return original(rt_, lc)

    merge_advance = pytest.MonkeyPatch()
    merge_advance.setattr(merge, "advance", advance)
    try:
        scheduler = Scheduler(rt)
        scheduler.serve(once=True)
    finally:
        merge_advance.undo()
    assert any(e["repo"] == "broken" and e["event"] == "error" for e in scheduler.log)
    assert any(e["repo"] == "demo" and e["event"] == "intake" for e in scheduler.log)


# -- review regressions ---------------------------------------------------------------------


def test_pending_actions_are_not_reissued_and_stale_acks_change_nothing(tmp_path):
    rt, lifecycle = _flow_runtime(tmp_path)
    changeset_id, publisher = _open_pr(tmp_path, rt, lifecycle)
    head = "d" * 40
    _ack(tmp_path, rt, publisher, kind="open_pr", changeset_id=changeset_id, ok=True, pr=42, head_sha=head)
    rt.github.prs[42] = {"state": "open", "merged": False, "head": {"sha": head}}
    assert merge.advance(rt, lifecycle) == [f"merge issued {changeset_id}"]
    assert merge.advance(rt, lifecycle) == []      # still pending: no duplicate merge item
    assert len(_items(tmp_path, "merge")) == 1
    first = rt.ledger.changeset(changeset_id)["pending_item"]["id"]
    _ack(tmp_path, rt, publisher, kind="merge", changeset_id=changeset_id, ok=True, pr=42, head_sha=head,
         merge_sha="e" * 40, post_check="passed", problems=[])
    assert rt.ledger.changeset(changeset_id)["status"] == "merged"
    # a late REJECTION of the same item must not undo the merge
    _ack(tmp_path, rt, publisher, kind="merge", changeset_id=changeset_id, ok=False,
         error="late duplicate", item_id=first)
    assert rt.ledger.changeset(changeset_id)["status"] == "merged"



def test_expired_pending_action_may_be_reissued(tmp_path):
    rt, lifecycle = _flow_runtime(tmp_path)
    changeset_id, publisher = _open_pr(tmp_path, rt, lifecycle)
    head = "d" * 40
    _ack(tmp_path, rt, publisher, kind="open_pr", changeset_id=changeset_id, ok=True, pr=42, head_sha=head)
    rt.github.prs[42] = {"state": "open", "merged": False, "head": {"sha": head}}
    merge.advance(rt, lifecycle)
    now = rt.clock()
    rt.clock = lambda: now + 36 * 60             # 30 min expiry + clock-skew margin
    assert merge.advance(rt, lifecycle) == [f"merge_expired {changeset_id}"]
    assert merge.advance(rt, lifecycle) == [f"merge issued {changeset_id}"]
    assert len(_items(tmp_path, "merge")) == 2


def test_overturn_breaker_publishes_the_pause_at_once(tmp_path):
    rt, lifecycle = _flow_runtime(tmp_path)
    owner = rt.ledger.acquire_lease("t")
    for _ in (1, 2):
        cs = rt.ledger.new_changeset_id("demo", "intake")
        rt.ledger.stage_intake(owner, "demo", cs, detail={}, status="closed", verdicts=[],
                               human_reason="", drafted_events=[])
    rt.ledger.release_lease(owner)
    Scheduler(rt)._overturn_breaker(lifecycle)
    public = load_public_key(public_key_text(rt.outbox._key.public_key()))
    control = verify("kb-control", json.loads((tmp_path / "state" / "outbox" / "control.json").read_text()), public)
    assert control["repos"]["demo"]["paused"] is True


def test_scheduler_does_not_undo_a_rollback_until_main_moves(tmp_path):
    rt, lifecycle = _flow_runtime(tmp_path)
    _add_agents(rt)
    first = rt.knowledge.fetch()
    activate(rt, first, verify_provenance=False)
    second = _commit_change(rt, "repos/demo/guide.md", "# guide\n")
    activate(rt, second, verify_provenance=False)
    rollback(rt, first)
    scheduler = Scheduler(rt)
    scheduler.intake_every = scheduler.release_every = 10 ** 9
    scheduler._last = {f"intake:demo": rt.clock(), f"release:demo": rt.clock()}
    scheduler.tick()
    assert Path(active_link(rt.state_dir)).resolve().name == first      # rollback respected
    third = _commit_change(rt, "repos/demo/guide.md", "# guide v3\n")
    owner = rt.ledger.acquire_lease("t")
    for sha in (second, third):                                         # both passed the gate
        merged = rt.ledger.new_changeset_id("demo", "intake")
        rt.ledger.stage_intake(owner, "demo", merged, detail={}, status="merged",
                               verdicts=[], human_reason="", drafted_events=[])
        rt.ledger.update_changeset(merged, merge_sha=sha)
    scheduler.tick()
    assert Path(active_link(rt.state_dir)).resolve().name == third      # a newer main activates


def test_activation_rejects_a_snapshot_whose_content_was_altered(tmp_path):
    rt, lifecycle = _flow_runtime(tmp_path)
    _add_agents(rt)
    sha = rt.knowledge.fetch()
    snapshot = activate(rt, sha, verify_provenance=False)
    (snapshot / "knowledge" / "AGENTS.md").write_text("# tampered\n", encoding="utf-8")
    with pytest.raises(ActivationError, match="does not match its manifest"):
        verify_snapshot(snapshot)
    with pytest.raises(ActivationError):
        rollback(rt, sha)


def test_failed_page_evaluations_are_retried_not_dropped(tmp_path):
    calls = {"n": 0}

    def answer(role, prompt):
        if role.name == "generator":
            calls["n"] += 1
            return {"operations": "not a list"}  # always malformed
        return _judge_all("yes")(role, prompt)

    rt, lifecycle = _flow_runtime(tmp_path)
    rt.gateway = ScriptedGateway(answer)
    upstream, v1, v2 = _upstream(tmp_path)
    sweep = {"tag": "v2", "from_sha": v1, "to_sha": v2, "reason": "release"}
    first = run_sweep(rt, lifecycle, rt.ledger.acquire_lease("s"), sweep, upstream)
    assert first["failed_pages"] == [PAGE] and not first["complete"]
    assert rt.ledger.get_cursor("demo", "sweep_baseline") is None  # baseline NOT advanced
    assert detect_release(rt, lifecycle, upstream)["to_sha"] == v2  # resumes the same sweep
    run_sweep(rt, lifecycle, rt.lease_owner or rt.ledger.acquire_lease("s"), sweep, upstream)
    third = run_sweep(rt, lifecycle, rt.ledger.acquire_lease("s"), sweep, upstream)
    assert third["complete"]                                        # given up after 3 attempts ...
    assert any("could not evaluate" in r["reason"] for r in rt.ledger.human_queue("demo"))  # ... to people
    assert rt.ledger.get_cursor("demo", "sweep_baseline") == v2


def test_v1_ledger_is_migrated_in_place(tmp_path):
    import sqlite3

    from infermatrix_copilot.kb_service.ledger import Ledger

    path = tmp_path / "kb.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version', '1');
        CREATE TABLE changesets (
            id TEXT PRIMARY KEY, repo TEXT NOT NULL, kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open', generation INTEGER NOT NULL,
            branch TEXT NOT NULL DEFAULT '', pr_number INTEGER, head_sha TEXT NOT NULL DEFAULT '',
            merge_sha TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL, updated_at REAL NOT NULL);
        INSERT INTO changesets (id, repo, kind, generation, created_at, updated_at)
            VALUES ('old', 'demo', 'intake', 1, 0, 0);
    """)
    conn.commit()
    conn.close()
    ledger = Ledger(path)
    assert ledger.changeset("old")["pending_item"] is None
    ledger.update_changeset("old", pending_item={"id": "x", "kind": "open_pr", "expires_at": 1})
    assert ledger.changeset("old")["pending_item"]["id"] == "x"
    Ledger(path)  # re-opening a migrated ledger is a no-op


def test_sweep_breaker_counts_every_attempt_before_releasing_anything(tmp_path):
    rt, lifecycle = _flow_runtime(tmp_path)
    second = "repos/demo/core/rules-limits.md"
    origin = Path(rt.knowledge.path).parent / "origin"
    (origin / "knowledge" / second).write_text(
        "---\ntitle: \"Limits\"\ncreated: 2026-09-01\nupdated: 2026-09-01\ntype: rule\ntags: [demo]\n"
        "sources: [\"PR #10\"]\n---\n\n# Limits\n\n" + _rule("DEMO-5a"), encoding="utf-8")
    index = origin / "knowledge" / "repos" / "demo" / "core" / "_index.md"
    index.write_text(index.read_text(encoding="utf-8") + "- [Limits](rules-limits.md)\n", encoding="utf-8")
    _git(origin, "add", ".")
    _git(origin, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "second page")
    state = {"limits_calls": 0}

    def answer(role, prompt):
        if role.name == "generator":
            if "rules-limits.md" in prompt:
                state["limits_calls"] += 1
                if state["limits_calls"] <= 3:  # every repair round of the first run fails
                    return {"operations": "malformed"}
                return {"operations": [{"kind": "edit_same_meaning", "page": second, "rule_id": "DEMO-5a",
                                        "section_markdown": _rule("DEMO-5a").replace("keep the demo", "keep the demo's")}]}
            return {"operations": [{"kind": "edit_same_meaning", "page": PAGE, "rule_id": "DEMO-1a",
                                    "section_markdown": _rule("DEMO-1a").replace("keep the demo", "keep the demo's")}]}
        return _judge_all("yes")(role, prompt)

    rt.gateway = ScriptedGateway(answer)
    lifecycle = replace(lifecycle, max_files=1, retire_ratio=1.0)
    upstream, v1, v2 = _upstream(tmp_path)
    sweep = {"tag": "v2", "from_sha": v1, "to_sha": v2, "reason": "release"}
    first = run_sweep(rt, lifecycle, rt.ledger.acquire_lease("s"), sweep, upstream)
    assert not first["complete"] and first["changesets"] == []
    held = rt.ledger.changesets("demo", ("sweep_held",))
    assert len(held) == 1 and publish(rt, lifecycle, held[0]["id"]) == "sweep_held"  # not publishable yet
    second_run = run_sweep(rt, lifecycle, rt.ledger.acquire_lease("s"), sweep, upstream)
    assert second_run["complete"] and "circuit breaker" in second_run["breaker"]
    statuses = {rt.ledger.changeset(cid)["status"] for cid in second_run["changesets"]}
    assert statuses == {"human"}  # BOTH pages held back: 2 files > max_files=1


def test_rollback_and_a_concurrent_tick_are_serialised(tmp_path, monkeypatch):
    import threading
    import time as _time

    from infermatrix_copilot.kb_service import activate as activate_module

    rt, lifecycle = _flow_runtime(tmp_path)
    _add_agents(rt)
    first = rt.knowledge.fetch()
    activate(rt, first, verify_provenance=False)
    second = _commit_change(rt, "repos/demo/guide.md", "# guide\n")
    activate(rt, second, verify_provenance=False)
    original = activate_module.verify_snapshot
    entered = threading.Event()

    def slow_verify(snapshot):
        entered.set()
        _time.sleep(0.5)
        return original(snapshot)

    monkeypatch.setattr(activate_module, "verify_snapshot", slow_verify)
    from infermatrix_copilot.kb_service.ledger import Ledger
    from infermatrix_copilot.kb_service.runtime import KbRuntime

    def do_rollback():
        other = KbRuntime(**{**rt.__dict__, "ledger": Ledger(rt.ledger.path, clock=rt.clock)})
        rollback(other, first)

    thread = threading.Thread(target=do_rollback)
    thread.start()
    assert entered.wait(5)
    scheduler = Scheduler(rt)
    scheduler._last = {"intake:demo": rt.clock(), "release:demo": rt.clock()}
    scheduler.tick()          # blocks on the activation lock until the rollback is done
    thread.join(5)
    assert Path(active_link(rt.state_dir)).resolve().name == first
    assert json.loads(rt.ledger.get_cursor("*", "rollback_pin"))["main"] == second



def test_resume_signs_a_fresh_verdict(tmp_path):
    rt, lifecycle = _flow_runtime(tmp_path)
    changeset_id, publisher = _open_pr(tmp_path, rt, lifecycle)
    _ack(tmp_path, rt, publisher, kind="open_pr", changeset_id=changeset_id, ok=True, pr=42, head_sha="d" * 40)
    rt.ledger.bump_generation("demo", pause=True, reason="drill")
    rt.github.prs[42] = {"state": "open", "merged": False, "head": {"sha": "d" * 40}}
    assert merge.advance(rt, lifecycle) == []                             # no verdict while paused
    rt.ledger.resume("demo")
    assert merge.advance(rt, lifecycle) == [f"merge issued {changeset_id}"]  # fresh verdict
def test_gated_change_sets_are_published_after_an_interruption(tmp_path):
    def answer(role, prompt):
        if role.name == "generator":
            return {"operations": [{"kind": "edit_same_meaning", "page": PAGE, "rule_id": "DEMO-1a",
                                    "section_markdown": _rule("DEMO-1a").replace("keep the demo", "keep the demo's")}]}
        return _judge_all("yes")(role, prompt)

    rt, lifecycle = _flow_runtime(tmp_path)
    _add_agents(rt)
    rt.gateway = ScriptedGateway(answer)
    upstream, v1, v2 = _upstream(tmp_path)
    report = run_sweep(rt, lifecycle, rt.ledger.acquire_lease("s"),
                       {"tag": "v2", "from_sha": v1, "to_sha": v2, "reason": "release"}, upstream)
    rt.ledger.release_lease(rt.ledger.acquire_lease("s"))
    [changeset_id] = report["changesets"]
    assert rt.ledger.changeset(changeset_id)["status"] == "gated"   # ... and then the process died
    scheduler = Scheduler(rt)
    scheduler._last = {"intake:demo": rt.clock(), "release:demo": rt.clock()}
    scheduler.serve(once=True)
    assert rt.ledger.changeset(changeset_id)["status"] == "pr_requested"



def test_global_pause_is_respected_by_advance(tmp_path):
    rt, lifecycle = _flow_runtime(tmp_path)
    changeset_id, publisher = _open_pr(tmp_path, rt, lifecycle)
    _ack(tmp_path, rt, publisher, kind="open_pr", changeset_id=changeset_id, ok=True, pr=42, head_sha="d" * 40)
    rt.ledger.bump_generation("*", pause=True, reason="all stop")
    rt.github.prs[42] = {"state": "open", "merged": False, "head": {"sha": "d" * 40}}
    assert merge.advance(rt, lifecycle) == []                             # no verdict while paused
    assert rt.ledger.changeset(changeset_id)["status"] == "pr_open"
def test_prune_keeps_snapshots_pinned_by_unfinished_runs(tmp_path):
    import json
    import os

    from infermatrix_copilot import run_status as rs

    state = tmp_path / "state"
    for n in range(4):
        path = snapshots_dir(state) / f"s{n}"
        path.mkdir(parents=True)
        os.utime(path, (1000 + n, 1000 + n))
    active_link(state).symlink_to((snapshots_dir(state) / "s3").resolve())
    runs = tmp_path / "runs"
    for n in range(4, 6):
        path = snapshots_dir(state) / f"s{n}"
        path.mkdir(parents=True)
        os.utime(path, (900 + n, 900 + n))
    for name, snapshot, status in (("running", "s1", {"state": rs.RUNNING}), ("queued", "s0", {"state": rs.QUEUED}),
                                   ("done", "s2", {"state": rs.DONE}),
                                   ("never-launched", "s4", {"state": rs.INTERRUPTED, "child_pid": None}),
                                   ("partly-ran", "s5", {"state": rs.INTERRUPTED, "child_pid": 4242})):
        run_dir = runs / name
        run_dir.mkdir(parents=True)
        (run_dir / "knowledge.json").write_text(json.dumps(
            {"snapshot": snapshot, "knowledge_root": str(snapshots_dir(state) / snapshot)}))
        (run_dir / rs.STATUS_NAME).write_text(json.dumps(status))
    prune(state, keep=1, roots=[runs])
    remaining = sorted(p.name for p in snapshots_dir(state).iterdir())
    # live runs and a reclaimable (never-launched) reservation keep their pins;
    # a finished run's, or one that ran and was interrupted, does not
    assert remaining == ["s0", "s1", "s3", "s4"]


def test_run_roots_come_from_the_environment(monkeypatch, tmp_path):
    import os

    from infermatrix_copilot.kb_service.activate import run_roots

    monkeypatch.setenv("KB_RUN_ROOTS", os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")]))
    assert run_roots() == [tmp_path / "a", tmp_path / "b"]
    monkeypatch.delenv("KB_RUN_ROOTS")
    monkeypatch.setenv("RUN_ROOT", str(tmp_path / "c"))
    assert run_roots() == [tmp_path / "c"]                  # Settings reads RUN_ROOT too


def test_a_snapshot_switched_away_from_is_kept_through_the_pin_grace_period(tmp_path):
    import os
    import time

    from infermatrix_copilot.kb_service.activate import DEACTIVATED, PIN_GRACE, switch_active

    state = tmp_path / "state"
    for n in range(3):
        path = snapshots_dir(state) / f"s{n}"
        path.mkdir(parents=True)
        os.utime(path, (1000 + n, 1000 + n))
    switch_active(state, snapshots_dir(state) / "s0")
    switch_active(state, snapshots_dir(state) / "s2")     # s0 just stopped being active
    prune(state, keep=1, roots=[])
    assert sorted(p.name for p in snapshots_dir(state).iterdir() if not p.name.startswith(".")) == ["s0", "s2"]
    marker = snapshots_dir(state) / DEACTIVATED.format(name="s0")
    old = time.time() - PIN_GRACE - 1
    os.utime(marker, (old, old))                          # the grace period has passed
    prune(state, keep=1, roots=[])
    assert sorted(p.name for p in snapshots_dir(state).iterdir() if not p.name.startswith(".")) == ["s2"]
    assert not marker.exists()


def test_run_roots_fall_back_to_the_effective_settings(monkeypatch, tmp_path):
    from infermatrix_copilot import config
    from infermatrix_copilot.kb_service.activate import run_roots

    monkeypatch.delenv("KB_RUN_ROOTS")

    class FakeSettings:
        run_root = tmp_path / "from-dotenv"

    monkeypatch.setattr(config, "Settings", FakeSettings)
    assert run_roots() == [tmp_path / "from-dotenv"]


def test_pin_discovery_reads_each_run_status_once(tmp_path, monkeypatch):
    import json

    from infermatrix_copilot import run_status as rs
    from infermatrix_copilot.kb_service.activate import pinned_snapshots

    run_dir = tmp_path / "runs" / "r"
    run_dir.mkdir(parents=True)
    (run_dir / "knowledge.json").write_text(json.dumps({"snapshot": "s", "knowledge_root": str(tmp_path / "s")}))
    answers = iter([{"state": rs.INTERRUPTED, "child_pid": None}, {"state": rs.QUEUED}])
    monkeypatch.setattr(rs, "read_status", lambda _d: next(answers))   # a reclaim lands between reads
    assert pinned_snapshots([tmp_path / "runs"]) == {(tmp_path / "s").resolve()}


def test_activation_refuses_a_knowledge_format_this_copilot_does_not_read(tmp_path):
    import json

    rt, lifecycle = _flow_runtime(tmp_path)
    _add_agents(rt)
    good = rt.knowledge.fetch()
    snapshot = activate(rt, good, verify_provenance=False)
    assert json.loads((snapshot / "MANIFEST.json").read_text())["knowledge_format"] == 2
    newer = _commit_change(rt, "_format.yaml", "format_version: 3\n")
    with pytest.raises(ActivationError, match="knowledge format 3"):
        activate(rt, newer, verify_provenance=False)
    origin = Path(rt.knowledge.path).parent / "origin"
    (origin / "knowledge" / "_format.yaml").unlink()
    _git(origin, "add", "-A")
    _git(origin, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "undeclared")
    with pytest.raises(ActivationError, match="undeclared"):
        activate(rt, rt.knowledge.fetch(), verify_provenance=False)
    assert Path(active_link(rt.state_dir)).resolve().name == good     # still the good one


def test_the_packaged_knowledge_declares_a_supported_format():
    from infermatrix_copilot.knowledge_view import SUPPORTED_FORMATS, KnowledgeView, knowledge_format

    assert knowledge_format(KnowledgeView.current().root) in SUPPORTED_FORMATS


@pytest.mark.parametrize("declared", ["2.9", "'2'", "true", ".inf", "[2]", "{}"])
def test_a_malformed_format_declaration_is_refused_not_coerced(tmp_path, declared):
    rt, lifecycle = _flow_runtime(tmp_path)
    _add_agents(rt)
    good = rt.knowledge.fetch()
    activate(rt, good, verify_provenance=False)
    bad = _commit_change(rt, "_format.yaml", f"format_version: {declared}\n")
    with pytest.raises(ActivationError, match="undeclared"):
        activate(rt, bad, verify_provenance=False)
    assert Path(active_link(rt.state_dir)).resolve().name == good
