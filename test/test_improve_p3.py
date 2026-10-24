"""Meta-improvement engine P3: the weekly budget governor and its hook in
LLM.create, and pre-registered experiments (registration refusals, shadow
runs through an injected unit runner, paired verdicts)."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from infermatrix_copilot.improve import budget, experiments as exps
from infermatrix_copilot.improve.budget import BudgetBreach, BudgetRefused, Governor, governed
from infermatrix_copilot.improve.ledger import Ledger
from infermatrix_copilot.trace_store import TraceStore, bind_store, trace_context

from test_improve_p1 import CLEAN, T0, Clock, _call, _decision, _store, _unit  # noqa: E402
from test_improve_p2 import REVIEW, _curated, _gt  # noqa: E402


def _gov(tmp_path, usd=1.0, calls=3, clock=None):
    return Governor(tmp_path / "ledger", usd_week=usd, judge_calls_week=calls, clock=clock or (lambda: T0))


# -- the governor ------------------------------------------------------------------------------

def test_governor_reserves_worst_case_settles_and_refuses(tmp_path):
    gov = _gov(tmp_path, usd=1.0)
    kwargs = {"system": "s" * 1000, "messages": [{"role": "user", "content": "中文" * 500}], "tools": []}
    nbytes = budget.request_bytes(kwargs)
    assert nbytes > 2000                                                    # bytes, not chars: CJK counts 3 each
    rid = gov.reserve_call("claude-sonnet-5", nbytes, 1000, purpose="lens")
    est = budget.worst_case_usd("claude-sonnet-5", nbytes, 1000)
    rem = gov.remaining()
    assert abs(rem["usd_reserved"] - est) < 1e-6 and rem["usd_settled"] == 0
    actual = gov.settle_call(rid, {"input_tokens": 300, "output_tokens": 100}, "claude-sonnet-5")
    assert actual < est and abs(gov.remaining()["usd_settled"] - actual) < 1e-6 and gov.remaining()["usd_reserved"] == 0
    # an unpriced model cannot be reserved
    with pytest.raises(BudgetRefused, match="no price"):
        gov.reserve_call("mystery-model-9000", 100, 10)
    # the envelope is hard: settled + reserved never exceeds it
    big = gov.reserve_call("claude-opus-5", 10, 10_000)                     # 10k output tokens of opus = $0.75
    with pytest.raises(BudgetRefused, match="exceed the weekly envelope"):
        gov.reserve_call("claude-opus-5", 10, 10_000)
    assert gov.remaining()["refused"] == 1
    gov.release_call(big)
    assert gov.remaining()["usd_reserved"] == 0
    # judge calls have their own envelope
    tokens = [gov.reserve_judge_call() for _ in range(3)]
    with pytest.raises(BudgetRefused, match="judge-call envelope"):
        gov.reserve_judge_call()
    gov.settle_judge_call(tokens[0])
    assert gov.remaining()["judge_calls_used"] == 1 and gov.remaining()["judge_calls_reserved"] == 2
    # state persists per ISO week across governor instances
    again = _gov(tmp_path, usd=1.0)
    assert again.remaining()["judge_calls_used"] == 1
    next_week = _gov(tmp_path, usd=1.0, clock=lambda: T0 + 8 * 86400)
    assert next_week.remaining()["usd_settled"] == 0


def test_settlement_is_cache_aware_and_pinned_to_the_reservations_week(tmp_path):
    from infermatrix_copilot.metrics import CACHE_CREATE_FACTOR, model_price

    pin, pout = model_price("claude-sonnet-5")
    gov = _gov(tmp_path, usd=5.0)
    rid = gov.reserve_call("claude-sonnet-5", 4000, 100)
    reserved = budget.worst_case_usd("claude-sonnet-5", 4000, 100)
    assert abs(reserved - (4000 / 1e6 * pin * CACHE_CREATE_FACTOR + 100 / 1e6 * pout)) < 1e-12   # writes priced dearest
    usage = {"input_tokens": 1000, "output_tokens": 50, "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 500}
    actual = gov.settle_call(rid, usage, "claude-sonnet-5")
    expected = 1000 / 1e6 * pin + 50 / 1e6 * pout + 2000 / 1e6 * pin * 0.1 + 500 / 1e6 * pin * CACHE_CREATE_FACTOR
    assert abs(actual - expected) < 1e-12 and actual > 1000 / 1e6 * pin + 50 / 1e6 * pout      # cache charged
    # a call reserved in week A and settled in week B is charged to week A
    week_a = budget.iso_week(T0)
    clock = {"t": T0}
    gov2 = Governor(tmp_path / "ledger2", usd_week=5.0, judge_calls_week=3, clock=lambda: clock["t"])
    rid = gov2.reserve_call("claude-sonnet-5", 100, 10)
    assert rid.startswith(week_a + "|")
    clock["t"] = T0 + 8 * 86400
    week_b = gov2.week()
    assert week_b != week_a
    gov2.settle_call(rid, {"input_tokens": 10, "output_tokens": 1}, "claude-sonnet-5")
    assert gov2.remaining(week_a)["usd_settled"] > 0 and gov2.remaining(week_a)["usd_reserved"] == 0
    assert gov2.remaining(week_b)["usd_settled"] == 0 and not (tmp_path / "ledger2" / "budget" / f"{week_b}.json").exists()
    rid2 = gov2.reserve_call("claude-sonnet-5", 100, 10)
    clock["t"] = T0 + 16 * 86400
    gov2.release_call(rid2)
    assert gov2.remaining(week_b)["usd_reserved"] == 0
    # a judge call reserved in one week settles in that week, whatever the clock says now
    clock["t"] = T0 + 16 * 86400
    week_c = gov2.week()
    token = gov2.reserve_judge_call()
    assert token.startswith(week_c + "|judge|")
    clock["t"] = T0 + 24 * 86400
    gov2.settle_judge_call(token)
    assert gov2.remaining(week_c)["judge_calls_used"] == 1 and gov2.remaining(week_c)["judge_calls_reserved"] == 0
    assert gov2.remaining()["judge_calls_used"] == 0
    # named holds: registered experiments keep their funds until they run
    gov.reserve_named("exp:one", 3.0)
    assert gov.remaining()["named"] == {"exp:one": 3.0}
    with pytest.raises(BudgetRefused, match="would exceed"):
        gov.reserve_named("exp:two", 3.0)
    gov.reserve_named("exp:one", 1.0)                                    # re-holding the same name replaces it
    assert gov.release_named("exp:one") == 1.0 and gov.remaining()["named"] == {}


def test_a_recorded_breach_stops_every_governor_instance(tmp_path):
    gov = _gov(tmp_path, usd=5.0)
    rid = gov.reserve_call("claude-sonnet-5", 100, 10)
    with pytest.raises(BudgetBreach):
        gov.settle_call(rid, {"input_tokens": 1_000_000, "output_tokens": 0}, "claude-sonnet-5")
    fresh = _gov(tmp_path, usd=5.0)                                     # a new process: same week file
    assert fresh.remaining()["breached"]
    with pytest.raises(BudgetRefused, match="breached"):
        fresh.reserve_call("claude-sonnet-5", 10, 10)
    with pytest.raises(BudgetRefused, match="breached"):
        fresh.reserve_judge_call()
    fresh.clear_breach()
    assert fresh.reserve_call("claude-sonnet-5", 10, 10)


def test_settlement_above_reservation_is_a_breach_that_aborts(tmp_path):
    store = TraceStore(tmp_path / "t", environ={})
    gov = _gov(tmp_path, usd=5.0)
    rid = gov.reserve_call("claude-sonnet-5", 100, 10)
    with bind_store(store), pytest.raises(BudgetBreach):
        gov.settle_call(rid, {"input_tokens": 1_000_000, "output_tokens": 0}, "claude-sonnet-5")
    assert gov.breached and gov.remaining()["breached"]
    with pytest.raises(BudgetRefused, match="breached"):
        gov.reserve_call("claude-sonnet-5", 10, 10)
    incident = store.query(kind="decision")[0]
    assert incident["result"]["type"] == "incident" and incident["result"]["kind"] == "budget_breach"


def test_concurrent_reservations_never_exceed_the_envelope(tmp_path):
    gov = _gov(tmp_path, usd=0.05)
    results: list = []
    start = threading.Barrier(6, timeout=5)

    def worker():
        start.wait()
        try:
            results.append(gov.reserve_call("claude-sonnet-5", 1000, 1000))   # ~$0.018 each: at most 2 fit
        except BudgetRefused:
            results.append(None)
    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    granted = [r for r in results if r]
    assert len(granted) == 2 and gov.remaining()["usd_reserved"] <= 0.05


def test_a_governed_run_binds_the_governor_in_a_real_subprocess(tmp_path, settings):
    """The actual entry: a child process configured only through the
    environment (IMPROVE_GOVERNED=1, IMPROVE_LEDGER_DIR) runs a playbook
    through the executor and its steps find a bound governor whose
    reservations land in the shared weekly file."""
    import subprocess
    import sys

    script = r'''
import asyncio, json, os
from pathlib import Path
from infermatrix_copilot.config import Settings
from infermatrix_copilot.app.workflow_execution import WorkflowExecution
from infermatrix_copilot.engine.registry import StepRegistry
from infermatrix_copilot.engine.step import StepResult, StepSpec
from infermatrix_copilot.playbooks.store import Playbook, PlaybookStep
from infermatrix_copilot.run_trace import RunTrace
from infermatrix_copilot.improve.budget import current_governor

async def step(ctx):
    gov = current_governor()
    assert gov is not None, "no governor bound in the child"
    rid = gov.reserve_call("claude-sonnet-5", 500, 50, purpose="child")
    gov.settle_call(rid, {"input_tokens": 10, "output_tokens": 5}, "claude-sonnet-5")
    return StepResult(True, summary="governed")

settings = Settings(_env_file=None, run_root=Path(os.environ["RUN_ROOT"]), repo_paths={})
registry = StepRegistry()
registry.register(StepSpec("x.governed", "deterministic", "read", step, ""))
run_dir = Path(os.environ["RUN_ROOT"]) / "run-1"
ex = WorkflowExecution(settings, registry)
pb = Playbook(name="p", version=1, status="active", task_kinds=["pr_review"], repos=[],
              steps=[PlaybookStep(id="g", step="x.governed")])
out = asyncio.run(ex.execute(pb, run_dir=run_dir, state={"task_spec": {"repo": "demo"}}))
print(json.dumps({"status": out.status, "reason": out.blocked_reason}))
'''
    env = {**os.environ, "IMPROVE_GOVERNED": "1", "IMPROVE_LEDGER_DIR": str(tmp_path / "ledger"),
           "IMPROVE_BUDGET_USD_WEEK": "1.0", "RUN_ROOT": str(tmp_path / "runs"),
           "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    proc = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert json.loads(proc.stdout.strip().splitlines()[-1])["status"] == "done"
    files = list((tmp_path / "ledger" / "budget").glob("*.json"))
    assert files and json.loads(files[0].read_text())["usd_settled"] > 0
    # the parent reads the same week file
    gov = Governor(tmp_path / "ledger", usd_week=1.0, judge_calls_week=3)
    assert gov.remaining()["usd_settled"] > 0


def test_llm_create_reserves_before_sending_and_settles_after(tmp_path, settings):
    from infermatrix_copilot.llm import LLM

    sent = []

    class Messages:
        def create(self, **kw):
            sent.append(kw)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")],
                                   usage=SimpleNamespace(input_tokens=5, output_tokens=2, cache_read_input_tokens=0,
                                                         cache_creation_input_tokens=0),
                                   stop_reason="end_turn", model="claude-sonnet-5", id="r")

    llm = LLM.__new__(LLM)
    llm.settings = settings
    llm._client = SimpleNamespace(messages=Messages())
    llm._default_model = "claude-sonnet-5"
    llm._provider = "anthropic"
    llm._guard_served_model = lambda *a, **k: None
    gov = _gov(tmp_path, usd=0.001)                                     # room for a small call only
    with governed(gov):
        llm.create(system="s", messages=[{"role": "user", "content": "hi"}], max_tokens=20)
        assert len(sent) == 1 and gov.remaining()["usd_settled"] > 0 and gov.remaining()["usd_reserved"] == 0
        with pytest.raises(BudgetRefused):
            llm.create(system="s", messages=[{"role": "user", "content": "hi"}], max_tokens=100_000)
        assert len(sent) == 1                                            # the refused call was never sent
    llm.create(system="s", messages=[{"role": "user", "content": "hi"}], max_tokens=100_000)   # unmetered without a governor
    assert len(sent) == 2

    class Failing:
        def create(self, **kw):
            raise RuntimeError("provider down")
    llm._client = SimpleNamespace(messages=Failing())
    gov2 = _gov(tmp_path / "g2", usd=1.0)
    with governed(gov2), pytest.raises(RuntimeError):
        llm.create(system="s", messages=[{"role": "user", "content": "hi"}], max_tokens=20)
    rem = gov2.remaining()
    assert rem["usd_reserved"] == 0 and rem["usd_settled"] > 0            # sent, usage unknown: forfeited, not released
    assert abs(rem["usd_settled"] - budget.worst_case_usd("claude-sonnet-5", budget.request_bytes(
        {"system": "s", "messages": [{"role": "user", "content": "hi"}], "tools": []}), 20)) < 1e-6
    # a failure AFTER the provider answered settles the real usage
    class AnswersThenOnTextFails:
        def create(self, **kw):
            return Messages().create(**kw)
    llm._client = SimpleNamespace(messages=AnswersThenOnTextFails())
    llm._provider = "openai"
    llm._create_openai = lambda **kw: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None),
                                                                                finish_reason="stop")],
                                                      usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3), model="claude-sonnet-5", id="r")
    gov3 = _gov(tmp_path / "g3", usd=1.0)

    def boom(delta):
        raise RuntimeError("on_text failed")
    with governed(gov3), pytest.raises(RuntimeError, match="on_text"):
        llm.create(system="s", messages=[{"role": "user", "content": "hi"}], max_tokens=20, on_text=boom)
    rem3 = gov3.remaining()
    assert rem3["usd_reserved"] == 0 and abs(rem3["usd_settled"] - budget.actual_usd(
        "claude-sonnet-5", {"input_tokens": 7, "output_tokens": 3})) < 1e-6
    # a failure BEFORE sending (an unpriced-model refusal happens before, but so does a client-less call) releases
    llm._provider = "anthropic"
    llm._client = None
    gov4 = _gov(tmp_path / "g4", usd=1.0)
    with governed(gov4), pytest.raises(RuntimeError, match="not configured"):
        llm.create(system="s", messages=[{"role": "user", "content": "hi"}], max_tokens=20)
    assert gov4.remaining()["usd_settled"] == 0 and gov4.remaining()["usd_reserved"] == 0


# -- experiments -------------------------------------------------------------------------------------

def _exp_settings(settings, tmp_path, store, gt):
    return settings.model_copy(update={"trace_store_root": str(store.root), "improve_gt_dir": str(gt),
                                       "improve_ledger_dir": str(tmp_path / "ledger"), "eco_model": "claude-sonnet-5",
                                       "improve_judge": "api:claude-sonnet-5"})


def test_registration_refuses_what_the_protocol_cannot_adjudicate(tmp_path, settings):
    gt, _ = _gt(tmp_path)
    gold = _curated(gt)
    store = _store(tmp_path)
    st = _exp_settings(settings, tmp_path, store, gt)
    items = ["demo#1@abc"]
    ok = dict(workflow="pr-review.agent.review_diff", hypothesis="h", metric="recall_review", items=items,
              arm_overrides={"review_second_round": "true"})
    with pytest.raises(exps.ExperimentError, match="descriptive-only"):
        exps.register(store, st, tmp_path / "ledger", **{**ok, "workflow": "rb-review.review"})
    with pytest.raises(exps.ExperimentError, match="not a Tier 2"):
        exps.register(store, st, tmp_path / "ledger", **{**ok, "workflow": "pr-review.report.final_summary"})
    with pytest.raises(exps.ExperimentError, match="same fingerprint"):
        exps.register(store, st, tmp_path / "ledger", **{**ok, "arm_overrides": {}})
    with pytest.raises(exps.ExperimentError, match="harness-not-isolated"):
        exps.register(store, st, tmp_path / "ledger", **{**ok, "arm_overrides": {"STRICT_BACKEND": "cursor"}})
    with pytest.raises(exps.ExperimentError, match="engine's own configuration"):
        exps.register(store, st, tmp_path / "ledger", **{**ok, "arm_overrides": {"improve_gt_dir": "eval/dataset/meta/x"}})
    with pytest.raises(exps.ExperimentError, match="meta-benchmark"):
        exps.register(store, st, tmp_path / "ledger", **{**ok, "arm_overrides": {"knowledge_dir": "eval/dataset/meta/x"}})
    with pytest.raises(exps.ExperimentError, match="no curated gold"):
        exps.register(store, st, tmp_path / "ledger", **{**ok, "items": ["demo#2@x"]})
    with pytest.raises(exps.ExperimentError, match="duplicates"):
        exps.register(store, st, tmp_path / "ledger", **{**ok, "items": items * 2})
    gov = _gov(tmp_path, usd=0.01)
    with pytest.raises(exps.ExperimentError, match="could not be held"):
        exps.register(store, st, tmp_path / "ledger", **ok, governor=gov, cost_per_unit_usd=1.0)
    holder = _gov(tmp_path / "g", usd=50.0)
    exp = exps.register(store, st, tmp_path / "ledger", **ok, governor=holder, cost_per_unit_usd=0.5, now=T0)
    assert holder.remaining()["named"] == {f"exp:{exp.experiment_id}": 3.0}      # held until it runs
    with pytest.raises(exps.ExperimentError, match="could not be held"):          # the same funds cannot be claimed twice
        exps.register(store, st, tmp_path / "ledger", **ok, governor=_gov(tmp_path / "g", usd=5.0), cost_per_unit_usd=0.5)
    assert exp.state == "registered" and exp.n_required == 53 and not exp.adequately_powered   # prior sd .13, effect .05
    assert exp.fingerprint_diff == {"settings": {"review_second_round": (False, True)}}
    assert exp.arm_fingerprint != exp.incumbent_fingerprint and exp.cost_estimate_usd == 3.0   # 1 item x 3 reps x 2 x 0.5
    assert exp.gold_versions == {"demo#1@abc": gold.version} and exp.budget_reserved
    rec = [r for r in store.query(kind="decision") if r["result"]["type"] == "experiment_registered"][0]
    assert rec["result"]["experiment_id"] == exp.experiment_id and rec["result"]["n_required"] == 53
    assert json.loads(store.blob(rec["inputs"]["fingerprint_diff"]))["settings"]["review_second_round"][1] is True
    assert json.loads(store.blob(exp.manifests["arm"]))["covers"]["settings"]["review_second_round"] is True
    listed = exps.list_experiments(tmp_path / "ledger", state="registered")
    assert [e.experiment_id for e in listed] == [exp.experiment_id]


def _scripted_runner(store):
    """A unit runner standing in for the copilot subprocess: it writes what
    the executor would (a traced review unit stamped with the run's tag and
    the fingerprint the child computed) and returns the subprocess shape."""

    def run_unit(*, side, item, replicate, env, snapshot_path, shadow_dir, run_root, playbook, repo, pr):
        assert env["IMPROVE_SHADOW"] == "1" and env["PR_CONTEXT_SOURCE"] == "snapshot" and "GH_TOKEN" not in env
        assert env["IMPROVE_GOVERNED"] == "1" and env["IMPROVE_LEDGER_DIR"]
        uid = f"{side}-{item}-r{replicate}:review"
        fp = env.get("_fingerprint", "f" * 64)
        with trace_context(run_id=uid.split(":")[0], playbook="pr-review", step="agent.review_diff", unit_id=uid,
                           workflow="pr-review.agent.review_diff", fingerprint=fp, item=item,
                           unit_tag=env["IMPROVE_UNIT_TAG"]):
            store.append("model_call", **_call(error=env.get("_error", "")))
            store.append("decision", outputs={"review": f"{REVIEW}\n\n<!-- {side.upper()}-REVIEW -->"},
                         result={"status": "ok", "type": "step_result"})
        return {"rc": int(env.get("_rc", 0)), "tag": env["IMPROVE_UNIT_TAG"]}
    return run_unit


class PairedJudge:
    """A fake paired judge: finds which blinded candidate is the arm by its
    marker and scores it as told."""

    def __init__(self, arm_recall=0.8, inc_recall=0.6):
        self.arm_recall, self.inc_recall, self.calls = arm_recall, inc_recall, 0

    def create(self, **kw):
        from infermatrix_copilot.llm import Block, Reply

        self.calls += 1
        prompt = kw["messages"][0]["content"]
        x = prompt.split("## Candidate X")[1].split("## Candidate Y")[0]
        arm_is_x = "ARM-REVIEW" in x
        arm = {"recall": self.arm_recall, "precision": 0.5, "actionability": 0.5}
        inc = {"recall": self.inc_recall, "precision": 0.5, "actionability": 0.5}
        verdict = {"x": arm if arm_is_x else inc, "y": inc if arm_is_x else arm,
                   "winner": ("X" if arm_is_x else "Y") if self.arm_recall != self.inc_recall else "tie", "margin": "clear"}
        return Reply(blocks=[Block(type="text", text=json.dumps(verdict))], stop_reason="end_turn", usage={}, model="m")


def _register(store, st, tmp_path, items, sd_item=0.05, **kw):
    exp = exps.register(store, st, tmp_path / "ledger", workflow="pr-review.agent.review_diff", hypothesis="h",
                        metric="recall_review", items=items, arm_overrides={"review_second_round": "true"},
                        cost_per_unit_usd=0.01, sd_item=sd_item, min_effect=0.05, now=T0, **kw)
    return exp


def test_experiment_runs_shadow_units_and_adjudicates_supported(tmp_path, settings, monkeypatch):
    gt, _ = _gt(tmp_path)
    _curated(gt)
    store = _store(tmp_path)
    st = _exp_settings(settings, tmp_path, store, gt)
    # eight items all with curated gold (copies of the same PR's gold under other PR numbers)
    items = []
    for n in range(1, 9):
        raw = json.loads((gt / "pr1.inline.json").read_text())
        (gt / f"pr{n}.inline.json").write_text(json.dumps(raw))
        item = f"demo#{n}@abc"
        if n > 1:
            from infermatrix_copilot.improve import gold as goldmod
            path = goldmod.write_draft(gt, item)
            d = json.loads(path.read_text())
            d["status"] = "curated"
            path.write_text(json.dumps(d))
        items.append(item)
    holder = _gov(tmp_path, usd=50.0)
    exp = _register(store, st, tmp_path, items, governor=holder)
    assert exp.n_required == 8 and exp.adequately_powered and holder.remaining()["named"]
    shadow = TraceStore(tmp_path / "shadow-traces", environ={})
    fps = {"arm": exp.arm_fingerprint, "incumbent": exp.incumbent_fingerprint}

    def stage(settings_, item, *, shadow_root, run_dir):
        d = shadow_root / item.replace("#", "-").replace("@", "-")
        d.mkdir(parents=True, exist_ok=True)
        (run_dir / "snapshots").mkdir(parents=True, exist_ok=True)
        (run_dir / "snapshots" / f"{item}.json").write_text("{}")
        return {"repo": "demo", "pr": int(item.split("#")[1].split("@")[0]), "shadow_dir": str(d),
                "snapshot_path": str(run_dir / "snapshots" / f"{item}.json")}

    def runner_env_fp(run_unit):
        def wrapped(**kw):
            env = dict(kw["env"])
            env["_fingerprint"] = fps[kw["side"]]
            return run_unit(**{**kw, "env": env})
        return wrapped
    run_unit = runner_env_fp(_scripted_runner(shadow))
    judge = PairedJudge(0.8, 0.6)
    done = exps.run(store, st, tmp_path / "ledger", exp.experiment_id, run_unit=run_unit, stage=stage,
                    shadow_store=shadow, now=T0 + 100, judge_llm=judge, governor=holder)
    assert done.state == "supported", done.result
    r = done.result
    assert r["n_retained"] == 8 and r["n_excluded"] == 0 and r["adequately_powered_final"] and r["n_verdicts"] == 24
    assert abs(r["mean"] - 0.2) < 1e-6 and r["lo"] > 0 and len(r["units"]) == 48
    assert judge.calls == 24                                                  # one blind paired call per (item, replicate)
    assert holder.remaining()["named"] == {}                                  # the hold became per-call reservations
    verdicts = shadow.query(kind="outcome", limit=10_000)
    assert len(verdicts) == 48 and {v["result"]["blinded_as"] for v in verdicts} == {"X", "Y"}
    # the child's budget is pinned to the governor even when settings name another directory
    other = TraceStore(tmp_path / "shadow-2", environ={})
    holder.settings = st.model_copy(update={"improve_input_context_limits": {"mimo-v2.5": 1048576}})
    st2 = st.model_copy(update={"improve_ledger_dir": str(tmp_path / "elsewhere"),
                                "improve_input_context_limits": {"mimo-v2.5": 1}})
    seen_env: list[dict] = []

    def spy(**kw):
        seen_env.append(dict(kw["env"]))
        return run_unit(**kw)
    exp2 = _register(store, st2, tmp_path, items, governor=holder)
    exps.run(store, st2, tmp_path / "ledger", exp2.experiment_id, run_unit=spy, stage=stage, shadow_store=other,
             now=T0 + 200, judge_llm=PairedJudge(0.8, 0.6), governor=holder)
    assert seen_env and all(e["IMPROVE_LEDGER_DIR"] == str(holder.dir.parent) and e["IMPROVE_BUDGET_USD_WEEK"] == "50.0"
                            for e in seen_env)
    assert all(json.loads(e["IMPROVE_INPUT_CONTEXT_LIMITS"]) == {"mimo-v2.5": 1048576} for e in seen_env)
    # an arm that tries to override the engine's own configuration is refused at registration
    with pytest.raises(exps.ExperimentError, match="engine's own configuration"):
        exps.register(store, st, tmp_path / "ledger", workflow="pr-review.agent.review_diff", hypothesis="h",
                      metric="recall_review", items=items[:8], arm_overrides={"review_second_round": "true",
                                                                                "IMPROVE_BUDGET_USD_WEEK": "9999"},
                      cost_per_unit_usd=0.01, sd_item=0.05, now=T0)
    # overrides are parsed as the child parses them: a JSON dict override becomes a dict in the fingerprint
    exp4 = exps.register(store, st, tmp_path / "ledger", workflow="pr-review.agent.review_diff", hypothesis="h",
                         metric="recall_review", items=items[:8],
                         arm_overrides={"REVIEW_LENS_BACKENDS": '{"contracts": "api"}'}, cost_per_unit_usd=0.01,
                         sd_item=0.05, now=T0)
    manifest = json.loads(store.blob(exp4.manifests["arm"]))
    assert manifest["covers"]["settings"]["review_lens_backends"] == {"contracts": "api"}
    with pytest.raises(exps.ExperimentError, match="Settings validation"):
        exps.register(store, st, tmp_path / "ledger", workflow="pr-review.agent.review_diff", hypothesis="h",
                      metric="recall_review", items=items[:8], arm_overrides={"REVIEW_LENS_BACKENDS": "not json"},
                      cost_per_unit_usd=0.01, sd_item=0.05, now=T0)
    # a terminal failure releases the planning hold, and a waiter never reruns a finished experiment
    held = _gov(tmp_path / "h2", usd=50.0)
    exp5 = _register(store, st.model_copy(update={"improve_judge": ""}), tmp_path, items[:8], governor=held)
    assert held.remaining()["named"]
    bad = exps.run(store, st.model_copy(update={"improve_judge": ""}), tmp_path / "ledger", exp5.experiment_id,
                   run_unit=spy, stage=stage, shadow_store=other, governor=held)
    assert bad.state == "invalid" and bad.result["hold_released"] and held.remaining()["named"] == {}
    with pytest.raises(exps.ExperimentError, match="is invalid, not registered"):
        exps.run(store, st, tmp_path / "ledger", exp5.experiment_id, run_unit=spy, stage=stage, shadow_store=other)
    verdict = [x for x in store.query(kind="decision") if x["result"]["type"] == "experiment_verdict"][0]
    assert verdict["result"]["label"] == "supported" and verdict["result"]["experiment_id"] == exp.experiment_id
    with pytest.raises(exps.ExperimentError, match="not registered"):
        exps.run(store, st, tmp_path / "ledger", exp.experiment_id, run_unit=run_unit, stage=stage, shadow_store=shadow)
    # without a configured judge nothing can be scored: invalid, and it says why
    unjudged = _register(store, st.model_copy(update={"improve_judge": ""}), tmp_path, items[:8])
    bad = exps.run(store, st.model_copy(update={"improve_judge": ""}), tmp_path / "ledger", unjudged.experiment_id,
                   run_unit=run_unit, stage=stage, shadow_store=shadow)
    assert bad.state == "invalid" and "no judge configured" in bad.result["error"]


def test_exclusions_recompute_power_and_a_mismatched_fingerprint_is_quarantined(tmp_path, settings):
    gt, _ = _gt(tmp_path)
    _curated(gt)
    store = _store(tmp_path)
    st = _exp_settings(settings, tmp_path, store, gt)
    from infermatrix_copilot.improve import gold as goldmod
    items = []
    for n in range(1, 12):
        (gt / f"pr{n}.inline.json").write_text((gt / "pr1.inline.json").read_text())
        item = f"demo#{n}@abc"
        if n > 1:
            path = goldmod.write_draft(gt, item)
            d = json.loads(path.read_text())
            d["status"] = "curated"
            path.write_text(json.dumps(d))
        items.append(item)
    exp = _register(store, st, tmp_path, items, sd_item=0.06)     # n_required 12, 11 available
    shadow = TraceStore(tmp_path / "shadow-traces", environ={})
    fps = {"arm": exp.arm_fingerprint, "incumbent": exp.incumbent_fingerprint}

    def stage(settings_, item, *, shadow_root, run_dir):
        if item == "demo#11@abc":
            raise RuntimeError("gh pr view failed")                        # cannot be staged: excluded
        d = shadow_root / item.replace("#", "-").replace("@", "-")
        d.mkdir(parents=True, exist_ok=True)
        (run_dir / "snapshots").mkdir(parents=True, exist_ok=True)
        (run_dir / "snapshots" / f"{item}.json").write_text("{}")
        return {"repo": "demo", "pr": int(item.split("#")[1].split("@")[0]), "shadow_dir": str(d),
                "snapshot_path": str(run_dir / "snapshots" / f"{item}.json")}

    base = _scripted_runner(shadow)
    tags_seen: list[str] = []

    def run_unit(**kw):
        env = dict(kw["env"])
        tags_seen.append(env["IMPROVE_UNIT_TAG"])
        env["_fingerprint"] = "0" * 64 if kw["item"] == "demo#8@abc" else fps[kw["side"]]   # drifted config
        if kw["item"] == "demo#7@abc" and kw["side"] == "arm":
            env["_error"] = "HTTP 402 Insufficient Balance"                                   # L13
        if kw["item"] == "demo#6@abc" and kw["side"] == "incumbent" and kw["replicate"] == 2:
            env["_rc"] = "1"                                                                  # the subprocess failed
        return base(**{**kw, "env": env})
    done = exps.run(store, st, tmp_path / "ledger", exp.experiment_id, run_unit=run_unit, stage=stage,
                    shadow_store=shadow, now=T0 + 100, judge_llm=PairedJudge(0.7, 0.7))
    r = done.result
    assert exp.n_required == 12
    assert done.state == "underpowered" and r["n_retained"] == 8 and r["n_excluded"] == 3      # 8 >= floor, < required
    assert any("staging" in v for v in r["excluded"].values())
    assert any("fingerprint mismatch" in v for v in r["excluded"].values())
    assert any("provider error" in v for v in r["excluded"].values())
    assert any("subprocess failed rc=1" in v for v in r["excluded"].values())
    assert "demo#6@abc/incumbent/2" not in r["units"] and "demo#6@abc/incumbent/1" in r["units"]   # tag identity
    assert len(tags_seen) == len(set(tags_seen))                                             # every run its own tag
    assert not r["adequately_powered_final"]


def test_malformed_paired_verdicts_score_nobody(tmp_path, settings):
    from infermatrix_copilot.improve.adapters.review_eval import ReviewEvalAdapter, _valid_paired_verdict
    from infermatrix_copilot.improve.judges import JudgeSpec
    from infermatrix_copilot.improve.reader import units_between
    from infermatrix_copilot.llm import Block, Reply

    gt, _ = _gt(tmp_path)
    gold = _curated(gt)
    shadow = TraceStore(tmp_path / "shadow", environ={})
    for side in ("arm", "incumbent"):
        with trace_context(run_id=side, playbook="pr-review", step="agent.review_diff", unit_id=f"{side}:review",
                           workflow="pr-review.agent.review_diff", fingerprint="f" * 64, item="demo#1@abc"):
            shadow.append("decision", outputs={"review": f"{REVIEW} {side}"}, result={"status": "ok"})
    units = units_between(shadow, 0.0, float("inf"), grace=0.0, lookback=0.0)
    replies = iter(["{}", '{"x": {"recall": 0.5}, "y": {"recall": 0.5, "precision": 0.5, "actionability": 0.5}, "winner": "X"}',
                    '{"x": {"recall": 1.5, "precision": 0.5, "actionability": 0.5}, "y": {"recall": 0.5, "precision": 0.5, "actionability": 0.5}, "winner": "X"}',
                    '{"x": {"recall": 0.5, "precision": 0.5, "actionability": 0.5}, "y": {"recall": 0.5, "precision": 0.5, "actionability": 0.5}, "winner": "Z"}',
                    '{"x": {"recall": true, "precision": 0.5, "actionability": 0.5}, "y": {"recall": 0.5, "precision": 0.5, "actionability": 0.5}, "winner": "tie"}',
                    'not json at all',
                    '{"x": {"recall": 0.5, "precision": 0.5, "actionability": 0.5}, "y": {"recall": 0.4, "precision": 0.5, "actionability": 0.5}, "winner": "tie"}'])

    class Scripted:
        def create(self, **kw):
            return Reply(blocks=[Block(type="text", text=next(replies))], stop_reason="end_turn", usage={}, model="m")
    adapter = ReviewEvalAdapter(gt_dir=gt, judge=JudgeSpec("api", "m"), llm=Scripted())
    for _ in range(6):
        assert adapter.paired_verdict(units["arm:review"], units["incumbent:review"], gold, shadow, experiment_id="e", replicate=1) is None
    assert shadow.query(kind="outcome", limit=100) == []                      # nothing written for unusable replies
    assert adapter.paired_verdict(units["arm:review"], units["incumbent:review"], gold, shadow, experiment_id="e", replicate=1)
    assert len(shadow.query(kind="outcome", limit=100)) == 2
    assert not _valid_paired_verdict({"x": {"recall": float("nan"), "precision": 0, "actionability": 0},
                                      "y": {"recall": 0, "precision": 0, "actionability": 0}, "winner": "X"})


def test_snapshot_file_handover_and_experiments_step(tmp_path, settings):
    import asyncio

    from infermatrix_copilot.engine.steps import improve as steps
    from infermatrix_copilot.engine.steps.pr import fetch

    shadow_dir = tmp_path / "clone"
    shadow_dir.mkdir()
    snap = {"repo": "demo", "pr": 7, "head_sha": "a" * 40, "base_sha": "b" * 40, "base_ref": "main", "diff": "d",
            "context_text": "c", "gate_report": "g", "pr_state": "OPEN", "ci_checks": [], "shadow_dir": str(shadow_dir),
            "snapshot_sha256": "c" * 64}
    path = tmp_path / "snap.json"
    path.write_text(json.dumps(snap))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    from infermatrix_copilot.run_trace import RunTrace
    ctx = SimpleNamespace(settings=settings.model_copy(update={"pr_context_source": "snapshot"}),
                          state={"task_spec": {"repo": "demo", "pr": 7}}, params={}, run_dir=run_dir,
                          trace=RunTrace(run_dir / "t.jsonl"), llm=None, item=None)
    import os
    os.environ["PR_SNAPSHOT_FILE"] = str(path)
    try:
        result = asyncio.run(fetch._pr_fetch_diff(ctx))
    finally:
        del os.environ["PR_SNAPSHOT_FILE"]
    assert result.ok and result.outputs["state_updates"]["repo_path"] == str(shadow_dir)
    # the experiments step: nothing registered -> a no-op; dry_run lists
    store = _store(tmp_path)
    st = settings.model_copy(update={"trace_store_root": str(store.root), "improve_ledger_dir": str(tmp_path / "ledger")})
    sctx = SimpleNamespace(settings=st, state={}, params={"dry_run": True}, run_dir=run_dir,
                           trace=SimpleNamespace(record=lambda *a, **k: None), llm=None)
    out = asyncio.run(steps._experiments(sctx))
    assert out.ok and out.outputs["pending"] == []
    sctx.params = {}
    assert asyncio.run(steps._experiments(sctx)).ok


def test_cli_budget_and_experiment_list(tmp_path, monkeypatch, capsys):
    from infermatrix_copilot.improve import cli

    store = _store(tmp_path)
    monkeypatch.setenv("TRACE_STORE_ROOT", str(store.root))
    monkeypatch.setenv("IMPROVE_LEDGER_DIR", str(tmp_path / "ledger"))
    assert cli.main(["budget", "--ledger-dir", str(tmp_path / "ledger")]) == 0
    assert json.loads(capsys.readouterr().out)["usd_envelope"] == 20.0
    assert cli.main(["experiment", "list", "--ledger-dir", str(tmp_path / "ledger")]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert cli.main(["experiment", "register", "--ledger-dir", str(tmp_path / "ledger"), "--workflow", "rb-review.review",
                     "--items", "demo#1@abc", "--arm", "X=1"]) == 1
    assert "descriptive-only" in capsys.readouterr().err


def test_a_waiting_caller_never_reruns_a_finished_experiment(tmp_path, settings):
    import threading
    import time as _time

    gt, _ = _gt(tmp_path)
    _curated(gt)
    store = _store(tmp_path)
    st = _exp_settings(settings, tmp_path, store, gt)
    from infermatrix_copilot.improve import gold as goldmod
    items = []
    for n in range(1, 9):
        (gt / f"pr{n}.inline.json").write_text((gt / "pr1.inline.json").read_text())
        item = f"demo#{n}@abc"
        if n > 1:
            path = goldmod.write_draft(gt, item)
            d = json.loads(path.read_text())
            d["status"] = "curated"
            path.write_text(json.dumps(d))
        items.append(item)
    exp = _register(store, st, tmp_path, items)
    shadow = TraceStore(tmp_path / "shadow-traces", environ={})
    fps = {"arm": exp.arm_fingerprint, "incumbent": exp.incumbent_fingerprint}
    entered = threading.Event()

    def stage(settings_, item, *, shadow_root, run_dir):
        entered.set()
        _time.sleep(0.05)
        d = shadow_root / item.replace("#", "-").replace("@", "-")
        d.mkdir(parents=True, exist_ok=True)
        (run_dir / "snapshots").mkdir(parents=True, exist_ok=True)
        (run_dir / "snapshots" / f"{item}.json").write_text("{}")
        return {"repo": "demo", "pr": int(item.split("#")[1].split("@")[0]), "shadow_dir": str(d),
                "snapshot_path": str(run_dir / "snapshots" / f"{item}.json")}
    base = _scripted_runner(shadow)

    def run_unit(**kw):
        env = dict(kw["env"])
        env["_fingerprint"] = fps[kw["side"]]
        return base(**{**kw, "env": env})
    results: dict = {}

    def first():
        results["first"] = exps.run(store, st, tmp_path / "ledger", exp.experiment_id, run_unit=run_unit, stage=stage,
                                    shadow_store=shadow, judge_llm=PairedJudge(0.8, 0.6)).state

    def second():
        entered.wait(5)
        try:
            exps.run(store, st, tmp_path / "ledger", exp.experiment_id, run_unit=run_unit, stage=stage,
                     shadow_store=shadow, judge_llm=PairedJudge(0.8, 0.6))
            results["second"] = "ran"
        except exps.ExperimentError as exc:
            results["second"] = str(exc)
    t1, t2 = threading.Thread(target=first), threading.Thread(target=second)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert results["first"] == "supported" and "already running" in results["second"]
    with pytest.raises(exps.ExperimentError, match="is supported, not registered"):
        exps.run(store, st, tmp_path / "ledger", exp.experiment_id, run_unit=run_unit, stage=stage, shadow_store=shadow)
