"""Design v2 P0 — correctness fixes (doc/architecture/DESIGN.md §V2.1(a)) pinned by tests:

1. resume restores step-to-step state handoffs (state_updates contract);
2. foreach fan-out lifts per-item state_updates into the merged result;
3. `when:` reads state keys and fails loudly on unknown keys;
4. per-repo skills/debug-memory namespaces are actually consulted;
5. high-risk modules come from the repo adapter, not core settings.
"""

import asyncio
import json
import re
from pathlib import Path

from infermatrix_copilot.engine.steps import register_builtin_steps
from infermatrix_copilot.engine.steps._common import require_repo
from infermatrix_copilot.engine.executor import Executor
from infermatrix_copilot.engine.registry import StepRegistry
from infermatrix_copilot.engine.step import FailureKind, StepContext, StepResult, StepSpec
from infermatrix_copilot.llm import Block, Reply
from infermatrix_copilot.playbooks.store import Playbook, PlaybookStep, PlaybookStore
from infermatrix_copilot.review.diff_summary import DiffSummary
from infermatrix_copilot.review.triggers import evaluate_triggers

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_require_repo_guard(settings, trace, tmp_path):
    """The K3 guard helper (one choke point for 8 steps): missing repo -> BLOCKED,
    present repo -> the Path."""
    ctx = StepContext(settings=settings, state={}, params={},
                      run_dir=tmp_path, trace=trace)
    res = require_repo(ctx)
    assert isinstance(res, StepResult) and not res.ok
    assert res.failure is FailureKind.BLOCKED
    ctx.state["repo_path"] = str(tmp_path)          # exists
    assert require_repo(ctx) == tmp_path
    ctx.state["repo_path"] = str(tmp_path / "gone")  # missing dir
    assert isinstance(require_repo(ctx), StepResult)          # must_exist=True
    assert require_repo(ctx, must_exist=False) == tmp_path / "gone"  # None-only check


class ScriptedLLM:
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []
        self.available = True

    def create(self, *, system, messages, tools=None, model=None, max_tokens=None,
               on_text=None):
        self.calls.append({"system": system, "messages": [*messages], "tools": tools})
        return self._replies.pop(0)


def _registry():
    return register_builtin_steps(StepRegistry())


def _executor(settings, trace, run_dir, llm=None):
    return Executor(_registry(), settings, run_dir=run_dir, trace=trace, llm=llm)


def _contract_reply(extra=None, status="success"):
    return Reply(blocks=[Block(type="text", text=json.dumps({
        "status": status, "summary": "done", "findings": [], "files_read": [],
        "files_modified": [], "tests_requested": [], "tests_run": [],
        "assumptions": [], "blockers": [], "confidence": "high",
        "failure_kind": None, "next_action": "", **(extra or {}),
    }))])


def _empty_reply():
    return Reply(blocks=[Block(type="text", text="")])


def _pb(steps, name="pb"):
    return Playbook(name=name, version=1, status="active",
                    task_kinds=["pr_review"], repos=[], steps=steps)


# -- fix 1: resume restores state handoffs -------------------------------------

def test_resume_restores_state_handoffs(settings, trace, tmp_path, git_repo):
    """pr-review interrupted after fetch+gate must resume with diff_text and
    gate_report restored from the checkpoint — the v1 bug re-entered the
    review step with an empty state and blocked spuriously."""
    playbook = PlaybookStore(REPO_ROOT / "playbooks", _registry()).get("pr-review")
    run_dir = tmp_path / "run"
    diff = "diff --git a/mod_a.py b/mod_a.py\n+A = 1"
    spec = {"pr": 9, "post": False, "report_only": False, "repo": "r"}

    # run 1: review agent yields nothing contract-conformant -> run fails
    # AFTER fetch and gate are checkpointed
    llm1 = ScriptedLLM([_empty_reply()] * (1 + settings.max_step_retries))
    state1 = {"task_spec": spec, "repo_path": str(git_repo),
              "diff_text": diff, "gate_report": "gates clean"}
    outcome1 = asyncio.run(_executor(settings, trace, run_dir, llm1).run(playbook, state1))
    assert outcome1.status == "failed"
    completed = json.loads((run_dir / "progress.json").read_text())["completed"]
    assert set(completed) == {"fetch", "gate"}

    # run 2: FRESH state (nothing injected) — the checkpoint alone must feed
    # the review step
    llm2 = ScriptedLLM([_contract_reply({"review_comments": []})])
    state2 = {"task_spec": spec, "repo_path": str(git_repo)}
    outcome2 = asyncio.run(_executor(settings, trace, run_dir, llm2).run(playbook, state2))
    assert outcome2.status == "done", outcome2.blocked_reason
    assert state2["diff_text"] == diff
    assert state2["gate_report"] == "gates clean"
    assert diff in llm2.calls[0]["messages"][0]["content"]  # evidence reached the agent
    assert "review_text" in state2


def test_push_policy_survives_resume(settings, trace, tmp_path, git_repo):
    """Resuming pr-rebase at the push step must not see the deny-all default
    PushPolicy — the checkout step's derived policy comes from the checkpoint."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "progress.json").write_text(json.dumps({"completed": {"checkout": {
        "summary": "cached", "outputs": {"state_updates": {"push_policy": {
            "allowed": True, "remote": "origin", "branch": "feature-x",
            "force_with_lease": True}}},
    }}}))
    playbook = _pb([PlaybookStep("checkout", "pr.checkout_branch"),
                    PlaybookStep("push", "ci.push")])
    state = {"task_spec": {"pr": 9}, "repo_path": str(git_repo),
             "protected_branches": ["main"]}
    outcome = asyncio.run(_executor(settings, trace, run_dir).run(playbook, state))
    assert outcome.status == "done", outcome.blocked_reason
    push = outcome.step_results["push"]
    assert "dry-run" in push.summary
    assert "--force-with-lease" in push.summary and "feature-x" in push.summary


def test_steps_publish_state_updates(settings, trace, tmp_path, git_repo):
    """Every state key a later step consumes is published via state_updates
    (including the injected/offline paths, which resume also flows through)."""
    registry = _registry()
    cases = [
        ("pr.fetch_diff", {"diff_text": "d"}, "diff_text"),
        ("issue.fetch", {"issue_text": "i"}, "issue_text"),
        ("pr.gate_check", {"gate_report": "g"}, "gate_report"),
        ("pr.fetch_ci_failures", {"ci_failures": [{"name": "j", "log": ""}]},
         "ci_failures"),
        ("pr.group_failures", {"ci_failures": [{"name": "j", "log": ""}]},
         "failure_groups"),
    ]
    for step_name, state, key in cases:
        state = {"task_spec": {"pr": 1, "issue": 1}, "repo_path": str(git_repo),
                 **state}
        ctx = StepContext(settings=settings, state=state, params={},
                          run_dir=tmp_path / "run", trace=trace)
        result = asyncio.run(registry.get(step_name).handler(ctx))
        assert result.ok, f"{step_name}: {result.summary}"
        updates = result.outputs.get("state_updates") or {}
        assert key in updates, f"{step_name} must publish {key} in state_updates"


# -- fix 2: foreach fan-out merges state_updates --------------------------------

def test_foreach_merges_state_updates(settings, trace, tmp_path):
    registry = StepRegistry()

    async def producer(ctx):
        return StepResult(True, summary=str(ctx.item),
                          outputs={"state_updates": {f"made_{ctx.item}": ctx.item}})

    async def consumer(ctx):
        ok = ctx.state.get("made_a") == "a" and ctx.state.get("made_b") == "b"
        return StepResult(ok, summary="saw fan-out state" if ok else "missing")

    registry.register(StepSpec("t.produce", "deterministic", "read", producer))
    registry.register(StepSpec("t.consume", "deterministic", "read", consumer))
    executor = Executor(registry, settings, run_dir=tmp_path / "run", trace=trace)
    pb = _pb([PlaybookStep("p", "t.produce", foreach="items"),
              PlaybookStep("c", "t.consume")])
    outcome = asyncio.run(executor.run(pb, {"items": ["a", "b"]}))
    assert outcome.status == "done"
    assert outcome.step_results["c"].summary == "saw fan-out state"


# -- fix 3: `when:` semantics ----------------------------------------------------

def test_when_reads_state_keys(settings, trace, tmp_path):
    registry = StepRegistry()

    async def flag(ctx):
        return StepResult(True, outputs={"state_updates": {"has_conflicts": True}})

    async def resolve(ctx):
        return StepResult(True, summary="resolved")

    registry.register(StepSpec("t.flag", "deterministic", "read", flag))
    registry.register(StepSpec("t.resolve", "deterministic", "read", resolve))
    executor = Executor(registry, settings, run_dir=tmp_path / "run", trace=trace)
    pb = _pb([PlaybookStep("f", "t.flag"),
              PlaybookStep("r", "t.resolve", when="has_conflicts"),
              PlaybookStep("skip", "t.resolve", when="not has_conflicts")])
    outcome = asyncio.run(executor.run(pb, {"task_spec": {"post": False}}))
    assert outcome.status == "done"
    assert outcome.step_results["r"].summary == "resolved"
    assert "skipped" in outcome.step_results["skip"].summary


def test_when_unknown_key_blocks_loudly(settings, trace, tmp_path):
    registry = StepRegistry()

    async def step(ctx):  # pragma: no cover - must not run
        return StepResult(True)

    registry.register(StepSpec("t.step", "deterministic", "read", step))
    executor = Executor(registry, settings, run_dir=tmp_path / "run", trace=trace)
    pb = _pb([PlaybookStep("s", "t.step", when="no_such_key")])
    outcome = asyncio.run(executor.run(pb, {"task_spec": {}}))
    assert outcome.status == "blocked"
    assert "unknown `when:` key" in outcome.blocked_reason


# -- fix 4: per-repo knowledge namespaces ---------------------------------------

def _write_skill(directory: Path, name: str, description: str) -> None:
    skill_dir = directory / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\ntrigger: fix ci\n"
        f"modules: []\nstatus: active\nrun_count: 0\n---\n\nBody of {name}.\n")


def test_per_repo_skills_rank_first_and_receive_proposals(settings, trace,
                                                          tmp_path, git_repo):
    from infermatrix_copilot.engine.agent_runtime import _retrieve_skills

    _write_skill(settings.skills_dir, "shared-skill", "how to fix ci failures")
    adapter_root = settings.adapters_dir / "myrepo"
    (adapter_root).mkdir(parents=True)
    (adapter_root / "manifest.yaml").write_text(
        f"name: myrepo\nstatus: active\nrepo:\n  path: {git_repo}\n")
    _write_skill(adapter_root / "skills", "repo-skill", "how to fix ci failures")

    ctx = StepContext(settings=settings, params={}, run_dir=tmp_path / "run",
                      trace=trace,
                      state={"task_spec": {"repo": "myrepo"},
                             "repo_path": str(git_repo)})
    summaries, store = _retrieve_skills(ctx, "fix ci failures")
    names = [s["name"] for s in summaries]
    assert names[0] == "repo-skill" and "shared-skill" in names

    store.propose(name="new-lesson", description="d", body="b")
    # PR-boundary F3: proposals stay repo-scoped but land RUNTIME-side —
    # the checked-in adapter tree is read-only at run time (Rev 8 §10)
    from infermatrix_copilot.memory.paths import KnowledgePaths
    runtime_dir = KnowledgePaths.resolve(
        settings, "myrepo", adapter_root=adapter_root).skills_runtime_dir
    assert (runtime_dir / "_candidates.json").exists()
    assert not (adapter_root / "skills" / "_candidates.json").exists()
    assert not (settings.skills_dir / "_candidates.json").exists()


def test_no_adapter_falls_back_to_shared_pool(settings, trace, tmp_path, git_repo):
    from infermatrix_copilot.engine.agent_runtime import _retrieve_skills

    _write_skill(settings.skills_dir, "shared-skill", "how to fix ci failures")
    ctx = StepContext(settings=settings, params={}, run_dir=tmp_path / "run",
                      trace=trace,
                      state={"task_spec": {"repo": "unknown"},
                             "repo_path": str(git_repo)})
    summaries, store = _retrieve_skills(ctx, "fix ci failures")
    assert [s["name"] for s in summaries] == ["shared-skill"]
    store.propose(name="new-lesson", description="d", body="b")
    # PR-boundary F3: proposals are RUNTIME-side in every configuration —
    # the shared pool (like the adapter tree) is read-only at run time
    from infermatrix_copilot.memory.paths import KnowledgePaths
    runtime_dir = KnowledgePaths.resolve(
        settings, "unknown").skills_runtime_dir
    assert (runtime_dir / "_candidates.json").exists()
    assert not (settings.skills_dir / "_candidates.json").exists()


# -- fix 5: adapter-sourced high-risk modules ------------------------------------

def test_high_risk_modules_from_adapter_override():
    summary = DiffSummary(changed_files=["x.py"], insertions=1,
                          tests_run=["pytest"])
    from infermatrix_copilot.config import Settings
    settings = Settings(_env_file=None)
    assert "custom_mod" not in settings.high_risk_modules
    fired = evaluate_triggers(summary, settings, touched_modules=("custom_mod",),
                              high_risk_modules=["custom_mod"])
    assert "high_risk_modules" in fired
    fired = evaluate_triggers(summary, settings, touched_modules=("worker_runner",),
                              high_risk_modules=[])  # adapter says: none risky
    assert "high_risk_modules" not in fired


def test_adapter_zero_declares_risk_tiers():
    from infermatrix_copilot.adapters.base import load_adapter
    adapter = load_adapter(REPO_ROOT / "adapters" / "vllm_omni")
    assert set(adapter.high_risk_modules) == {
        "worker_runner", "model_executor", "scheduler",
        # extended from review-campaign forensics: platform code sets
        # process-wide defaults, diffusion is multi-model shared code, and
        # distributed owns cross-stage transport — small diffs there still
        # need the full review ensemble
        "platform", "diffusion", "distributed",
        "runtime_patch", "host_weight_runtime"}


# -- repo-neutral core guard (§V2.2.1) -------------------------------------------

# Known v1 leaks (doc/architecture/DESIGN.md §V2.1(b)), by source file: ceilings, so the
# list can only shrink. A new repo-specific literal anywhere else fails.
_KNOWN_LEAKS = {
    "__init__.py": 1,            # package docstring
    "config.py": 1,              # default_repo
    "intent.py": 2,              # parse_* default_repo parameter defaults
    "task_spec.py": 1,           # TaskSpec.repo default
    "thin_mcp_server.py": 4,
}
_LEAK = re.compile(r"vllm[_\- ]?omni|/rebase/", re.IGNORECASE)


def test_repo_neutral_core():
    """A debt RATCHET, not full enforcement of the repo-neutral invariant: files in
    `_KNOWN_LEAKS` are permitted their recorded count and no more.

    Every offender is collected before asserting. Asserting inside the loop stopped
    at the first file, so one over-ceiling file hid every later one — config.py's 3
    literals masked thin_mcp_server.py's 29 for as long as both were red, and the
    second only surfaced when the first was fixed.
    """
    src = REPO_ROOT / "src" / "infermatrix_copilot"
    over = []
    for path in sorted(src.rglob("*.py")):
        rel = str(path.relative_to(src))
        count = len(_LEAK.findall(path.read_text(encoding="utf-8")))
        ceiling = _KNOWN_LEAKS.get(rel, 0)
        if count > ceiling:
            over.append(f"{rel}: {count} repo-specific literal(s), ceiling {ceiling}")
    assert not over, (
        "repo knowledge belongs in adapters/<repo>/, not the core "
        "(doc/architecture/DESIGN.md §V2.2.1):\n  " + "\n  ".join(over))
