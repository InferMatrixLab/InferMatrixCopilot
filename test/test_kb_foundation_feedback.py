"""Same-batch retries use advisory review data without granting recognition."""
import copy
import json
from types import SimpleNamespace

import pytest

from infermatrix_copilot.kb_service import init_knowledge_parallel as parallel
from infermatrix_copilot.kb_service.init_coverage import Owner
from infermatrix_copilot.kb_service.init_support import InitError, InitRecord
from infermatrix_copilot.trace_store import TraceStore
from test_kb_init_skeleton import world  # noqa: F401


def _stage_job(facets=("api", "configuration")):
    stage = SimpleNamespace(rt=SimpleNamespace(unlimited_subscription=True, gateway=None),
        record=InitRecord("knowledge", "toy", pin="a" * 40, inputs_digest="batch"),
        lifecycle=SimpleNamespace(full_name="o/toy"))
    payload = {"repository": "o/toy", "pin": "a" * 40, "owner": "sample", "facets": list(facets),
               "foundation_prompt_version": 4, "files": [], "docs": []}
    job = {"owner": Owner("sample", "repos/toy/sample.md", ()), "page": "repos/toy/sample.md",
           "requested": list(facets), "payload": payload, "offered": {}}
    return stage, job


def _task(job, sequence=0, *, verdict="unsure", error=""):
    payload = copy.deepcopy(job["payload"])
    value = {"owner": job["owner"].owner, "page": job["page"], "payload": payload,
        "requested": job["requested"][:], "offered": job["offered"], "artifacts": [], "dropped": [],
        "sequence": sequence, "unfinished": [], "error": error,
        "verdicts": {f"knowledge:{job['owner'].owner}:{facet}": {"verdict": verdict,
                     "page": job["page"], "facet": facet,
                     "reasons": {"faithful": "Caller obligation is not supported; narrow the claim."}}
                     for facet in job["requested"]}}
    value["input_sha256"] = parallel._hash({"payload": payload, "offered": value["offered"]})
    value["result_sha256"] = parallel._hash(value)
    return value


def test_feedback_matches_only_still_requested_owner_page_and_version():
    stage, job = _stage_job()
    initial = _task(job)
    saved = {"binding": "batch", "tasks": {"initial": initial}}
    for key, changed in (("owner", {"owner": "other"}), ("page", {"page": "other.md"})):
        saved["tasks"][key] = {**copy.deepcopy(initial), **changed}
    wrong_payload_owner = copy.deepcopy(initial)
    wrong_payload_owner["payload"]["owner"] = "other"
    saved["tasks"]["payloadowner"] = wrong_payload_owner
    legacy = copy.deepcopy(initial)
    legacy["payload"].pop("foundation_prompt_version")
    saved["tasks"]["legacy"] = legacy
    unrequested = copy.deepcopy(initial)
    unrequested["requested"] = ["validation"]
    saved["tasks"]["unrequested"] = unrequested
    before = copy.deepcopy(saved)
    job["requested"] = job["payload"]["facets"] = ["api"]
    retry = list(parallel._with_review_feedback(stage, [job], saved))[0]
    feedback = retry["payload"]["prior_review_feedback"]
    assert feedback["advisory_only"] is True and set(feedback["facets"]) == {"api"}
    assert feedback["facets"]["api"]["correction_round"] == 1
    assert feedback["facets"]["api"]["last_verdict"] == "unsure"
    assert feedback["facets"]["api"]["prior_attempts"] == [
        {k: initial[k] for k in ("input_sha256", "result_sha256")}]
    assert "Caller obligation" in feedback["facets"]["api"]["last_review_reasons"]["faithful"]
    assert saved == before and stage.record.evidence == {}


def test_three_corrections_preserve_each_attempt_and_exhaust_only_that_facet():
    stage, job = _stage_job(("api",))
    initial = _task(job, error="ModelUnavailable: initial generator failed")
    saved = {"binding": "batch", "tasks": {initial["input_sha256"]: initial}}
    hashes = {initial["input_sha256"]}
    for number in range(1, 4):
        before = copy.deepcopy(saved)
        retry = list(parallel._with_review_feedback(stage, [job], saved))[0]
        detail = retry["payload"]["prior_review_feedback"]["facets"]["api"]
        assert detail["correction_round"] == number and len(detail["prior_attempts"]) == number
        if number == 1:
            assert "initial generator failed" in detail["last_error"]
        task = _task(retry, number, verdict="no")
        assert task["input_sha256"] not in hashes and saved == before
        hashes.add(task["input_sha256"])
        saved["tasks"][task["input_sha256"]] = task
    assert len(saved["tasks"]) == 4
    assert list(parallel._with_review_feedback(stage, [job], saved)) == []
    assert "three review corrections exhausted; remains unknown" in stage.record.unfinished[-1]
    stage2, wider = _stage_job(("api", "configuration"))
    remaining = list(parallel._with_review_feedback(stage2, [wider], saved))[0]
    assert remaining["requested"] == remaining["payload"]["facets"] == ["configuration"]
    assert "prior_review_feedback" not in remaining["payload"] and stage2.record.evidence == {}


def test_uncheckpointed_interruption_does_not_consume_a_correction():
    stage, job = _stage_job(("api",))
    initial = _task(job)
    saved = {"binding": "batch", "tasks": {"initial": initial, "duplicate-reference": initial}}
    first = list(parallel._with_review_feedback(stage, [job], saved))[0]
    second = list(parallel._with_review_feedback(stage, [job], saved))[0]
    assert first == second and first["payload"]["prior_review_feedback"]["facets"]["api"]["correction_round"] == 1
    completed = _task(first, 1)
    saved["tasks"][completed["input_sha256"]] = completed
    next_job = list(parallel._with_review_feedback(stage, [job], saved))[0]
    assert next_job["payload"]["prior_review_feedback"]["facets"]["api"]["correction_round"] == 2


@pytest.mark.parametrize("damage", ["page", "facet", "pass"])
def test_mismatched_inner_review_and_pass_are_not_rejection_feedback(damage):
    stage, job = _stage_job(("api",))
    prior = _task(job)
    row = prior["verdicts"]["knowledge:sample:api"]
    row["verdict" if damage == "pass" else damage] = "pass" if damage == "pass" else "other"
    prior["dropped"] = [
        {"rule_id": "knowledge:sample:api", "page": job["page"], "why": "shown anchor needs correction"},
        {"rule_id": "knowledge:sample:api", "page": "wrong.md", "why": "unrelated wrong page"},
        {"rule_id": "knowledge:sample:configuration", "page": job["page"], "why": "unrelated facet"}]
    prior["result_sha256"] = parallel._hash({k: v for k, v in prior.items() if k != "result_sha256"})
    retry = list(parallel._with_review_feedback(stage, [job], {"binding": "batch", "tasks": {"initial": prior}}))[0]
    detail = retry["payload"]["prior_review_feedback"]["facets"]["api"]
    assert detail["last_review_reasons"] == {} and detail["last_verdict"] == "unknown"
    assert detail["last_dropped_reasons"] == ["shown anchor needs correction"]
    assert detail["correction_round"] == 1  # The actual requested extraction still happened.
    assert stage.record.evidence == {}


@pytest.mark.parametrize("damage", ["result_hash", "validated_hash", "batch"])
def test_unvalidated_history_cannot_supply_retry_feedback(damage):
    stage, job = _stage_job()
    prior = _task(job)
    saved = {"binding": "batch", "tasks": {"initial": prior}}
    if damage in {"result_hash", "validated_hash"}:
        if damage == "validated_hash":
            stage._foundation_validated_results = {prior["result_sha256"]}
        prior["verdicts"]["knowledge:sample:api"]["reasons"] = {"fake": "approval"}
    else:
        saved["binding"] = "other batch"
    with pytest.raises(InitError, match="foundation"):
        list(parallel._with_review_feedback(stage, [job], saved))


@pytest.mark.parametrize("mode", ["initial", "finite", "legacy"])
def test_initial_finite_and_legacy_jobs_are_unchanged(mode):
    stage, job = _stage_job()
    saved = {"binding": "batch", "tasks": {}}
    if mode == "finite":
        stage.rt.unlimited_subscription = False
        saved["tasks"]["ignored"] = _task(job)
    elif mode == "legacy":
        job["payload"].pop("foundation_prompt_version")
        saved["tasks"]["ignored"] = _task(job)
    assert list(parallel._with_review_feedback(stage, [job], saved)) == [job]
    assert "prior_review_feedback" not in job["payload"]


def test_actual_native_retry_prompt_contains_only_failed_facet_feedback_and_retains_old_proofs(world, monkeypatch):
    from test_kb_foundation_publication import test_incomplete_native_publication_resumes_only_missing_facet
    jobs, original = [], parallel._worker
    def capture(stage, job):
        jobs.append(copy.deepcopy(job))
        return original(stage, job)
    monkeypatch.setattr(parallel, "_worker", capture)
    # The real offline native generator/judge fixture checks old approved tasks
    # are identical on resume, only the failed facet runs, and publication
    # stays blocked until independent validation also passes.
    test_incomplete_native_publication_resumes_only_missing_facet(world)
    corrected = [job for job in jobs if "prior_review_feedback" in job["payload"]]
    assert len(corrected) == 1
    job = corrected[0]
    assert job["owner"].owner == "feature-demo" and job["requested"] == ["validation"]
    feedback = job["payload"]["prior_review_feedback"]["facets"]
    assert set(feedback) == {"validation"} and feedback["validation"]["correction_round"] == 1
    assert "scripted missing validation support" in feedback["validation"]["last_review_reasons"]["faithful"]
    state = world["tmp"] / "required-foundation"
    record = InitRecord.load(state, "toy", "knowledge")
    task = next(t for t in record.coverage["foundation_jobs"]["tasks"].values()
                if "prior_review_feedback" in t["payload"])
    traces = TraceStore(state / "init" / "traces")
    receipt = task["artifacts"][0]["generator_receipt"]
    native = traces.get(receipt["native_trace_id"])
    prompt = traces.blob(native["inputs"]["prompt"])
    payload = json.loads(prompt.split("\n", 1)[1].rsplit("</untrusted_data>", 1)[0])
    assert payload["prior_review_feedback"] == job["payload"]["prior_review_feedback"]
    assert payload["files"] and task["artifacts"][0]["verdict"]["verdict"] == "pass"
    from infermatrix_copilot.kb_service.init_knowledge_inputs import knowledge_system
    from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole
    from test_kb_init_skeleton import _runtime
    assert traces.blob(native["inputs"]["system"]) == knowledge_system(task["payload"])
    rt = _runtime(world, ModelGateway(None, transport_factory=lambda _: pytest.fail("native retry replay dispatched a model")),
                  state_dir=state, generator=ModelRole("generator", "zcode", "GLM-5.3"))
    rt.unlimited_subscription = True
    stage = SimpleNamespace(rt=rt, record=record, lifecycle=SimpleNamespace(full_name="o/toy"), tags=["toy"],
        observer=rt.upstream("toy", "o/toy").observer(record.pin))
    before = InitRecord.path(state, "toy", "knowledge").read_bytes()
    parallel._validate_native(stage, task["artifacts"][0], task)
    altered = copy.deepcopy(task)
    altered["payload"]["prior_review_feedback"]["facets"]["validation"]["last_review_reasons"] = {"fake": "approval"}
    altered["input_sha256"] = parallel._hash({"payload": altered["payload"], "offered": altered["offered"]})
    altered["result_sha256"] = parallel._hash({k: v for k, v in altered.items() if k != "result_sha256"})
    with pytest.raises(InitError, match="native approval binding mismatch"):
        parallel._validate_native(stage, altered["artifacts"][0], altered)
    assert InitRecord.path(state, "toy", "knowledge").read_bytes() == before
