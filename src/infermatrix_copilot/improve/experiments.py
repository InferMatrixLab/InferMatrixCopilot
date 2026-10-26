"""Pre-registered experiments (design §8).

An experiment compares two configuration fingerprints of one Tier 2
workflow on a pre-registered item set, in shadow, and ends with one of five
labels. Registration writes the whole decision rule down BEFORE any arm runs
(a `decision(type=experiment_registered)` record) and refuses anything the
protocol cannot adjudicate:

* the workflow must be comparative (an adapter that is descriptive-only —
  the bot's — registers nothing);
* both arms must resolve to API backends (a harness keeps a native shell
  outside the shadow fences: ``harness-not-isolated``);
* the fingerprint diff must be non-empty and must be exactly the declared
  overrides — the manifests are stored so a reviewer can diff them;
* a diff that touches the frozen meta-benchmark is refused;
* the worst-case cost is reserved against the weekly envelope.

The item count required comes from the metric's historical item-level
spread (a conservative prior when there is none); an experiment may be
registered underpowered, but its verdict can then only be ``underpowered``.

Running stages every item (the shadow snapshot + clone, before isolation),
then runs arm and incumbent per item and replicate through `run_unit` in
the shadow environment, scores each unit through the workflow's adapter,
and adjudicates: paired within item, replicates averaged, power recomputed
on the RETAINED items after quarantine (L13, fingerprint mismatch, budget
cut). The verdict is a `decision(type=experiment_verdict)` naming every unit.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..trace_store import TraceStore, current_store, file_lock
from ..process_execution import run_command
from . import stats
from .adapters import load_adapter, scores_from
from .budget import BudgetRefused, Governor
from .enroll import WorkflowDeclaration, declarations_for
from .fingerprint import compute, diff as fingerprint_diff
from .reader import Unit, units_between

PRIOR_SD = 0.13          # wave-4 item-level sd of the paired recall delta (design §8.1)
STATES = ("registered", "running", "supported", "refuted", "neutral", "underpowered", "invalid")


class ExperimentError(RuntimeError):
    pass


@dataclass
class Experiment:
    experiment_id: str
    workflow: str
    hypothesis: str
    metric: str
    direction: str
    min_effect: float
    item_set: list[str]
    item_set_sha: str
    replicates: int
    alpha: float
    power: float
    n_required: int
    n_available: int
    adequately_powered: bool
    arm_overrides: dict
    incumbent_overrides: dict
    arm_fingerprint: str
    incumbent_fingerprint: str
    fingerprint_diff: dict
    manifests: dict                 # {"arm": blob ref, "incumbent": blob ref}
    cost_estimate_usd: float
    budget_reserved: bool
    proposal_id: str = ""
    registered_by: str = "engine"
    registered_at: float = 0.0
    state: str = "registered"
    gold_versions: dict = field(default_factory=dict)
    result: dict = field(default_factory=dict)
    source_artifacts: dict = field(default_factory=dict)
    snapshot_versions: dict = field(default_factory=dict)


# -- settings under overrides -----------------------------------------------------------

def _coerce(value: str) -> Any:
    v = str(value)
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


def _arm_settings_class(base_cls: Any, env_overrides: dict[str, str]) -> Any:
    """A Settings subclass whose ONLY environment is `env_overrides` — the
    pydantic-settings env source itself (JSON decoding, validators, the
    complex-field rules the child applies), fed a mapping instead of the
    process environment, which this module never mutates (D8 guardrail)."""
    from pydantic_settings import EnvSettingsSource

    class _ArmEnvSource(EnvSettingsSource):
        def _load_env_vars(self):
            # the same normalization pydantic-settings applies to os.environ
            out: dict[str, str | None] = {}
            for k, v in env_overrides.items():
                if self.env_ignore_empty and v == "":
                    continue
                if self.env_parse_none_str is not None and v == self.env_parse_none_str:
                    v = None
                out[k if self.case_sensitive else k.lower()] = v
            return out

    class _ArmSettings(base_cls):  # type: ignore[misc,valid-type]
        @classmethod
        def settings_customise_sources(cls, settings_cls, init_settings, env_settings,
                                       dotenv_settings, file_secret_settings):
            return (init_settings, _ArmEnvSource(settings_cls))

    _ArmSettings.__name__ = base_cls.__name__
    return _ArmSettings


def settings_for(settings: Any, overrides: dict[str, str]) -> tuple[Any, dict[str, str]]:
    """``(settings, environ)`` an arm runs with. Overrides are applied the
    way the shadow CHILD will see them — as environment variables parsed by
    the Settings class's own env source (JSON for dicts and lists, validators
    and all) — never through `model_copy`, which skips validation and would
    let a string stand where the child gets a dict, so the registered
    fingerprint could not match the actual run. The process environment is
    not touched: the env source is handed the overrides as a mapping. Every
    override is also the arm's environment (routing knobs are read from it by
    the fingerprint and by the subprocess)."""
    fields = getattr(type(settings), "model_fields", {})
    overridden = {k.lower() for k in overrides if k.lower() in fields}
    if overridden:
        base = {k: v for k, v in settings.model_dump().items() if k not in overridden}
        env_overrides = {k.upper(): str(v) for k, v in overrides.items() if k.lower() in overridden}
        try:
            arm_cls = _arm_settings_class(type(settings), env_overrides)
            patched = arm_cls(_env_file=None, **base)
        except Exception as exc:  # noqa: BLE001 - reported as a registration refusal
            raise ExperimentError(f"override rejected by Settings validation: {exc}") from exc
    else:
        patched = settings
    environ = {**{k: v for k, v in os.environ.items() if k in ("ECO_MODEL", "PERFORMANCE_MODEL", "STRICT_BACKEND")},
               **{k: str(v) for k, v in overrides.items()}}
    return patched, environ


def _harness_targets(settings: Any) -> list[str]:
    out = []
    for mode in ("eco", "performance"):
        try:
            target = settings.tier_target(mode)
        except Exception:  # noqa: BLE001 - an unconfigured tier is not a harness
            continue
        if getattr(target, "kind", "") == "harness":
            out.append(f"{mode}:{target.provider_id or target.source}")
    return out


def _touches_meta(diff: dict, overrides: dict) -> bool:
    text = json.dumps(diff, default=str) + json.dumps(overrides, default=str)
    return "eval/dataset/meta" in text or "/meta/" in text


# -- persistence --------------------------------------------------------------------------

def _dir(ledger_dir: Path) -> Path:
    d = Path(ledger_dir) / "experiments"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save(ledger_dir: Path, exp: Experiment) -> Path:
    path = _dir(ledger_dir) / f"{exp.experiment_id}.json"
    from .artifacts import atomic_json
    atomic_json(path, json.loads(json.dumps(asdict(exp), default=str)))
    return path


def load(ledger_dir: Path, experiment_id: str) -> Experiment:
    path = _dir(ledger_dir) / f"{experiment_id}.json"
    if not path.exists():
        raise ExperimentError(f"no experiment {experiment_id}")
    return Experiment(**json.loads(path.read_text(encoding="utf-8")))


def list_experiments(ledger_dir: Path, state: str | None = None) -> list[Experiment]:
    out = []
    for path in sorted(_dir(ledger_dir).glob("exp-*.json")):
        exp = Experiment(**json.loads(path.read_text(encoding="utf-8")))
        if state is None or exp.state == state:
            out.append(exp)
    return out


# -- registration ---------------------------------------------------------------------------

def register(store: TraceStore, settings: Any, ledger_dir: str | Path, *, workflow: str, hypothesis: str,
             metric: str, items: list[str], arm_overrides: dict[str, str], incumbent_overrides: dict[str, str] | None = None,
             direction: str = "higher", min_effect: float = 0.05, replicates: int = 3, proposal_id: str = "",
             cost_per_unit_usd: float | None = None, governor: Governor | None = None, registered_by: str = "engine",
             sd_item: float | None = None, now: float | None = None,
             source_artifacts: dict | None = None, snapshot_versions: dict | None = None) -> Experiment:
    now = time.time() if now is None else now
    ledger_dir = Path(ledger_dir)
    decls = declarations_for(settings)
    decl = decls.get(workflow)
    if decl is None or not decl.tier2:
        raise ExperimentError(f"{workflow!r} is not a Tier 2 workflow (no outcome adapter): nothing to adjudicate")
    adapter = load_adapter(decl.outcome_adapter, **_adapter_kwargs(settings, decl))
    if getattr(adapter, "descriptive_only", False):
        raise ExperimentError(f"{workflow!r} is descriptive-only (proxy labels): v1 registers no experiment on it")
    if direction not in ("higher", "lower"):
        raise ExperimentError("direction must be 'higher' or 'lower'")
    if min_effect <= 0 or replicates < 1:
        raise ExperimentError("min_effect must be positive and replicates >= 1")
    items = [str(i).strip() for i in items if str(i).strip()]
    if len(set(items)) != len(items) or not items:
        raise ExperimentError("the item set must be non-empty and free of duplicates")
    incumbent_overrides = dict(incumbent_overrides or {})
    arm_overrides = dict(arm_overrides or {})
    forbidden = sorted(k for k in (*arm_overrides, *incumbent_overrides) if k.upper().startswith("IMPROVE_"))
    if forbidden:
        raise ExperimentError(f"overrides may not touch the engine's own configuration: {forbidden}")
    arm_settings, arm_env = settings_for(settings, arm_overrides)
    inc_settings, inc_env = settings_for(settings, incumbent_overrides)
    for label, st in (("arm", arm_settings), ("incumbent", inc_settings)):
        harness = _harness_targets(st)
        if harness:
            raise ExperimentError(f"harness-not-isolated: the {label} resolves to {harness}; v1 shadow experiments are API-only")
    arm_fp, arm_manifest = compute(decl, arm_settings, environ=arm_env)
    inc_fp, inc_manifest = compute(decl, inc_settings, environ=inc_env)
    source_artifacts = dict(source_artifacts or {})
    if source_artifacts:
        from .artifacts import verify
        if set(source_artifacts) != {"arm", "incumbent"}:
            raise ExperimentError("versioned experiments need both source artifacts")
        for side, st, env in (("arm", arm_settings, arm_env), ("incumbent", inc_settings, inc_env)):
            root = Path(source_artifacts[side])
            artifact = verify(root)
            fp, manifest = compute(decl, st, environ=env, package_root=root / "src" / "infermatrix_copilot")
            manifest["covers"]["source_tree_sha"] = artifact["tree_sha"]
            from .evolution import public_settings
            manifest["covers"]["execution_settings"] = public_settings(st)
            manifest["covers"]["experiment_driver"] = decl.experiment_driver
            fp = hashlib.sha256(json.dumps(manifest["covers"], sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest() if manifest["complete"] else ""
            if side == "arm": arm_fp, arm_manifest = fp, manifest
            else: inc_fp, inc_manifest = fp, manifest
    if not arm_fp or not inc_fp:
        raise ExperimentError(f"incomplete fingerprint: arm missing {arm_manifest.get('missing')}, "
                              f"incumbent missing {inc_manifest.get('missing')}")
    diff = fingerprint_diff(inc_manifest, arm_manifest)
    if _touches_meta(diff, arm_overrides) or _touches_meta(diff, incumbent_overrides):
        raise ExperimentError("the fingerprint diff (or an override) touches the frozen meta-benchmark: refused")
    if not diff:
        raise ExperimentError("the arm and the incumbent have the same fingerprint: nothing to compare")
    # gold must exist for every item (a comparative experiment needs the matrix or the judge's paired verdicts)
    gold_versions = {}
    for item in items:
        if source_artifacts and snapshot_versions and item in snapshot_versions:
            gold_versions[item] = snapshot_versions[item]
        else:
            gold = adapter.gold(item)
            if gold is None:
                raise ExperimentError(f"no curated gold for {item}: it cannot be scored")
            gold_versions[item] = gold.version
    if sd_item is None:
        sd_item = _historical_sd(ledger_dir, workflow, metric) or PRIOR_SD
    n_required = stats.items_required(sd_item, min_effect)
    n_available = len(items)
    if cost_per_unit_usd is None:
        cost_per_unit_usd = _planning_unit_usd(ledger_dir, workflow)
    cost = round(n_available * replicates * 2 * float(cost_per_unit_usd), 4)
    reserved = False
    experiment_id = f"exp-{int(now)}-{uuid.uuid4().hex[:6]}"
    if governor is not None:
        # a persisted, named hold: two registrations cannot claim the same
        # funds, and nothing else can spend them before the experiment runs
        try:
            governor.reserve_named(f"exp:{experiment_id}", cost)
        except BudgetRefused as exc:
            raise ExperimentError(f"the planning estimate ${cost:.2f} could not be held: {exc}") from exc
        reserved = True
    manifests = {"arm": store.put_blob(json.dumps(arm_manifest, sort_keys=True, ensure_ascii=False)),
                 "incumbent": store.put_blob(json.dumps(inc_manifest, sort_keys=True, ensure_ascii=False))}
    exp = Experiment(
        experiment_id=experiment_id, workflow=workflow, hypothesis=hypothesis,
        metric=metric, direction=direction, min_effect=float(min_effect), item_set=items,
        item_set_sha=hashlib.sha256(json.dumps({"items": items, "gold": gold_versions}, sort_keys=True).encode()).hexdigest(),
        replicates=int(replicates), alpha=0.05, power=0.8, n_required=n_required, n_available=n_available,
        adequately_powered=n_available >= n_required, arm_overrides=arm_overrides,
        incumbent_overrides=incumbent_overrides, arm_fingerprint=arm_fp, incumbent_fingerprint=inc_fp,
        fingerprint_diff=diff, manifests=manifests, cost_estimate_usd=cost, budget_reserved=reserved,
        proposal_id=proposal_id, registered_by=registered_by, registered_at=now, gold_versions=gold_versions,
        source_artifacts=source_artifacts, snapshot_versions=dict(snapshot_versions or {}))
    save(ledger_dir, exp)
    store.append("decision", inputs={"fingerprint_diff": json.dumps(diff, sort_keys=True, default=str)},
                 context={"playbook": "workflow-improve", "workflow": workflow},
                 result={"type": "experiment_registered", **{k: v for k, v in asdict(exp).items()
                                                            if k not in ("fingerprint_diff", "result")}})
    if proposal_id:
        from .ledger import Ledger

        try:
            Ledger(ledger_dir, store, clock=lambda: now).transition(workflow, proposal_id, "experiment-registered",
                                                                     experiment_id=exp.experiment_id)
        except Exception as exc:  # noqa: BLE001 - a proposal state problem does not unregister the experiment
            exp.result["proposal_note"] = str(exc)[:200]
            save(ledger_dir, exp)
    return exp


def _historical_sd(ledger_dir: Path, workflow: str, metric: str) -> float | None:
    from .ledger import Ledger

    wl = Ledger(ledger_dir).load(workflow)
    sds = (wl.baseline.get("delta_sd") or {}).get(metric)
    return float(sds) if sds else None


def _median_unit_usd(ledger_dir: Path, workflow: str) -> float | None:
    import statistics

    from .ledger import Ledger

    wl = Ledger(ledger_dir).load(workflow)
    key = 'planning_usd' if 'planning_usd' in wl.baseline else 'usd'
    samples = [float(x) for x in (wl.baseline.get(key) or []) if x is not None and (key != 'usd' or x)]
    return statistics.median(samples) if samples else None


def _planning_unit_usd(ledger_dir: Path, workflow: str) -> float:
    cost = _median_unit_usd(ledger_dir, workflow)
    return .5 if cost is None else cost


def _adapter_kwargs(settings: Any, decl: WorkflowDeclaration) -> dict:
    if decl.outcome_adapter.endswith(":ReviewEvalAdapter"):
        return {"gt_dir": str(getattr(settings, "improve_gt_dir", "") or "eval/dataset/gt"),
                "judgments_dir": getattr(settings, "improve_judgments_dir", "") or None,
                "arm": str(getattr(settings, "improve_eval_arm", "") or "")}
    if decl.outcome_adapter.endswith(":MetaBenchAdapter"):
        return {"meta_dir": str(getattr(settings, "improve_meta_dir", "") or "eval/dataset/meta")}
    return {}


def _meta_dir(settings: Any) -> Path:
    return Path(str(getattr(settings, "improve_meta_dir", "") or "eval/dataset/meta")).expanduser()


# -- running ----------------------------------------------------------------------------------

RunUnit = Callable[..., dict]


def _run_shadow_unit(argv, *, shadow_dir, child_env, timeout):
    proc = run_command(argv, cwd=str(shadow_dir), env=child_env, timeout=timeout)
    return {"rc": proc.returncode, "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-2000:],
            "tag": child_env.get("IMPROVE_UNIT_TAG", "")}


def default_run_unit(*, side: str, item: str, replicate: int, env: dict, snapshot_path: Path, shadow_dir: Path,
                     run_root: Path, playbook: str, repo: str, pr: int, timeout: int = 3600) -> dict:
    """Run one shadow unit: the copilot as a subprocess in the shadow
    environment (`python -m infermatrix_copilot --yes --playbook ...`), the
    snapshot handed over through PR_SNAPSHOT_FILE. Returns rc/stdout tail."""
    argv = [sys.executable, "-m", "infermatrix_copilot", "--yes", "--playbook", playbook,
            "--task-param", f"repo={repo}", "--task-param", f"pr={pr}"]
    child_env = {**env, "PR_SNAPSHOT_FILE": str(snapshot_path), "RUN_ROOT": str(run_root)}
    return _run_shadow_unit(argv, shadow_dir=shadow_dir, child_env=child_env, timeout=timeout)


def run(store: TraceStore, settings: Any, ledger_dir: str | Path, experiment_id: str, *, run_unit: RunUnit | None = None,
        stage: Callable[..., dict] | None = None, governor: Governor | None = None, now: float | None = None,
        shadow_store: TraceStore | None = None, judge_llm: Any = None, sandbox: Any = None) -> Experiment:
    """Execute a registered experiment end to end and adjudicate it."""
    now = time.time() if now is None else now
    ledger_dir = Path(ledger_dir)
    with file_lock(ledger_dir / "experiments" / f"{experiment_id}.lock", blocking=False) as held:
        if not held:
            raise ExperimentError(f"{experiment_id} is already running")
        # the state is read INSIDE the lock: a caller that waited for the
        # lock must see the verdict the previous holder wrote, never rerun
        exp = load(ledger_dir, experiment_id)
        if exp.source_artifacts and (not settings.improve_enabled or not settings.improve_evolve_enabled):
            raise ExperimentError("code experiments require IMPROVE_ENABLED and IMPROVE_EVOLVE_ENABLED")
        if exp.state != "registered" and not (exp.source_artifacts and exp.state == "running"):
            raise ExperimentError(f"{experiment_id} is {exp.state}, not registered")
        exp.state = "running"
        save(ledger_dir, exp)
        try:
            return _run_locked(store, settings, ledger_dir, exp, run_unit=run_unit, stage=stage, governor=governor,
                               now=now, shadow_store=shadow_store, judge_llm=judge_llm, sandbox=sandbox)
        except BaseException as exc:
            exp = load(ledger_dir, experiment_id)
            from .isolation import SandboxUnavailable
            resumable = exp.source_artifacts and isinstance(exc, (BudgetRefused, SandboxUnavailable, KeyboardInterrupt, SystemExit))
            if exp.state == "running" and not resumable:
                _terminal_invalid(ledger_dir, exp, governor, settings, "the run raised before adjudication")
            raise


def _terminal_invalid(ledger_dir: Path, exp: Experiment, governor, settings, error: str) -> Experiment:
    """Mark an experiment invalid on a terminal failure and release its
    planning hold: a terminal experiment must never keep consuming the
    weekly envelope."""
    exp.state = "invalid"
    exp.result = {**exp.result, "error": error, "promotable": False}
    if exp.budget_reserved:
        try:
            if governor is None:
                from .cycle import governor_for

                governor = governor_for(settings, ledger_dir)
            governor.release_named(f"exp:{exp.experiment_id}", iso_week_of(exp.registered_at))
            exp.result["hold_released"] = True
        except Exception as exc:  # noqa: BLE001 - the invalid verdict stands even if the release fails
            exp.result["hold_release_error"] = str(exc)[:200]
    save(ledger_dir, exp)
    return exp


def meta_argv(playbook: str, repo: str, case: str, *extra: str) -> list[str]:
    """The child's command line. The child's REPO_PATHS exposes exactly one
    alias (shadow_env), so the task names it: without `repo=` run_playbook
    falls back to default_repo and refuses the run as an unknown alias
    before any step."""
    return [sys.executable, "-m", "infermatrix_copilot", "--yes", "--playbook", playbook,
            "--task-param", f"repo={repo}", "--task-param", f"meta_case={case}", *extra]


def meta_run_unit(*, side: str, item: str, replicate: int, env: dict, snapshot_path: Path, shadow_dir: Path,
                  run_root: Path, playbook: str, repo: str, pr: int, timeout: int = 3600) -> dict:
    """Run one self-experiment unit: the copilot as a subprocess in the
    shadow environment on ONE staged meta case (`--task-param meta_case=`),
    reading only the staged copy through IMPROVE_META_DIR."""
    argv = meta_argv(playbook, repo, item[len("meta:"):])
    child_env = {**env, "IMPROVE_META_DIR": str(Path(shadow_dir) / "meta"), "RUN_ROOT": str(run_root)}
    return _run_shadow_unit(argv, shadow_dir=shadow_dir, child_env=child_env, timeout=timeout)


def _meta_stage(settings, item: str, *, shadow_root: Path, run_dir: Path) -> dict:
    """Stage one meta case for a self-experiment: a copy of the case and the
    lint samples under the shadow directory (nothing narrative), the
    snapshot file only naming the item."""
    from .meta import stage_case

    if not item.startswith("meta:"):
        raise ValueError(f"a self-experiment item is 'meta:<case>', got {item!r}")
    case = item[len("meta:"):]
    shadow_dir = shadow_root / re.sub(r"[^A-Za-z0-9._-]+", "_", case)
    shadow_dir.mkdir(parents=True, exist_ok=True)
    stage_case(_meta_dir(settings), case, shadow_dir / "meta")
    snapshots = run_dir / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    snapshot_path = snapshots / f"{re.sub(r'[^A-Za-z0-9._-]+', '_', item)}.json"
    snapshot_path.write_text(json.dumps({"item": item, "meta_case": case}), encoding="utf-8")
    return {"repo": "meta", "pr": 0, "shadow_dir": str(shadow_dir), "snapshot_path": str(snapshot_path),
            "meta_dir": str(shadow_dir / "meta")}


def _run_locked(store, settings, ledger_dir: Path, exp: Experiment, *, run_unit, stage, governor, now,
                shadow_store, judge_llm, sandbox=None) -> Experiment:
    from .judges import judge_spec_from
    from .shadow import DEFAULT_EXECUTABLES, assert_boundaries, make_executables_dir, shadow_env

    decls = declarations_for(settings)
    decl = decls[exp.workflow]
    if exp.source_artifacts:
        from .evolution import evaluate
        from .isolation import Sandbox
        return evaluate(store, settings, ledger_dir, exp, llm=judge_llm or _api_llm(settings),
                        sandbox=sandbox or Sandbox(), governor=governor)
    if decl.experiment_driver and decl.experiment_driver not in ("pr-review", "meta") and run_unit is None:
        return _terminal_invalid(ledger_dir, exp, governor, settings,
                                 f"{decl.experiment_driver} requires pinned dataset/source artifacts; use improve evolve run")
    adapter = load_adapter(decl.outcome_adapter, **_adapter_kwargs(settings, decl))
    if governor is None:
        from .cycle import governor_for

        governor = governor_for(settings, ledger_dir)
    # a human-labelled benchmark (the engine's own) needs no judge: the
    # labels are the gold; every other workflow is scored by the configured
    # judge (api:<model> / cli:<provider>:<model>) and is invalid without one
    human_labelled = bool(getattr(adapter, "human_labelled", False))
    if human_labelled:
        stage = stage or _meta_stage
        run_unit = run_unit or meta_run_unit
    else:
        judge = judge_spec_from(settings)
        if judge is None:
            return _terminal_invalid(ledger_dir, exp, governor, settings,
                                     "no judge configured (settings.improve_judge): the shadow reviews cannot be scored")
        adapter.judge = judge
        adapter.governor = governor
        if judge.kind == "api":
            adapter.llm = judge_llm if judge_llm is not None else _api_llm(settings)
    # the child's budget is THIS governor's: same ledger directory, same
    # envelopes — and no arm may override the budget configuration
    budget_keys = {k for k in (*exp.arm_overrides, *exp.incumbent_overrides) if k.upper().startswith("IMPROVE_")}
    if budget_keys:
        return _terminal_invalid(ledger_dir, exp, governor, settings,
                                 f"overrides may not touch the engine's own configuration: {sorted(budget_keys)}")
    if exp.budget_reserved:
        # the planning hold becomes per-call reservations from here on
        governor.release_named(f"exp:{exp.experiment_id}", iso_week_of(exp.registered_at))
    shadow_root = Path(getattr(settings, "trace_store_root", "") or store.root) / "improve" / "shadow" / exp.experiment_id
    shadow_store = shadow_store or TraceStore(shadow_root / "traces")
    run_root = shadow_root / "runs"
    run_root.mkdir(parents=True, exist_ok=True)
    exe_dir = make_executables_dir(shadow_root / "bin", decl.shadow_executables or DEFAULT_EXECUTABLES)
    excluded: dict[str, str] = {}
    staged: dict[str, dict] = {}
    for item in exp.item_set:
        try:
            staged[item] = (stage or _default_stage)(settings, item, shadow_root=shadow_root / "clones", run_dir=run_root)
        except Exception as exc:  # noqa: BLE001 - an item that cannot be staged is excluded, never approximated
            excluded[item] = f"staging: {exc}"[:200]
    scores: dict[str, dict[str, dict[int, float]]] = {}   # item -> side -> replicate -> metric value
    units_by_key: dict[str, str] = {}
    produced: dict[str, dict[str, dict[int, Unit]]] = {}     # item -> side -> replicate -> unit
    input_limits = getattr(governor.settings, "improve_input_context_limits", {}) or {}
    native_limits = getattr(governor.settings, 'improve_native_call_limits', {}) or {}
    budget_env = {"IMPROVE_LEDGER_DIR": str(governor.dir.parent), "IMPROVE_BUDGET_USD_WEEK": str(governor.usd_week),
                  "IMPROVE_BUDGET_JUDGE_CALLS_WEEK": str(governor.judge_calls_week),
                  "IMPROVE_INPUT_CONTEXT_LIMITS": json.dumps(input_limits, sort_keys=True),
                  'IMPROVE_NATIVE_CALL_LIMITS': json.dumps(native_limits, sort_keys=True)}
    for item, snap in staged.items():
        repo, pr = snap["repo"], int(snap["pr"])
        for side, overrides, expected_fp in (("arm", exp.arm_overrides, exp.arm_fingerprint),
                                             ("incumbent", exp.incumbent_overrides, exp.incumbent_fingerprint)):
            env = shadow_env(shadow_dir=snap["shadow_dir"], run_dir=run_root, trace_root=shadow_store.root,
                             executables_dir=exe_dir, repo_name=repo, ledger_dir=governor.dir.parent,
                             usd_week=governor.usd_week, judge_calls_week=governor.judge_calls_week,
                             input_context_limits=input_limits, native_call_limits=native_limits)
            env.update({k: str(v) for k, v in overrides.items()})
            problems = assert_boundaries(env, expected=budget_env)
            if problems:
                return _terminal_invalid(ledger_dir, exp, governor, settings,
                                         f"shadow boundary violated before dispatch: {problems}")
            for rep in range(1, exp.replicates + 1):
                key = f"{item}/{side}/{rep}"
                # every unit carries a unique tag: the child stamps it into its
                # trace context, so a fast failed replicate can never be mistaken
                # for the previous one
                tag = f"{exp.experiment_id}:{side}:{item}:{rep}:{uuid.uuid4().hex[:6]}"
                try:
                    outcome = (run_unit or default_run_unit)(
                        side=side, item=item, replicate=rep, env={**env, "IMPROVE_UNIT_TAG": tag},
                        snapshot_path=Path(snap["snapshot_path"]), shadow_dir=Path(snap["shadow_dir"]),
                        run_root=run_root, playbook=decl.playbook, repo=repo, pr=pr)
                except BudgetRefused as exc:
                    excluded[key] = f"budget_cut: {exc}"[:200]
                    continue
                except Exception as exc:  # noqa: BLE001 - a crashed unit is excluded, never scored
                    excluded[key] = f"crashed: {exc}"[:200]
                    continue
                rc = int((outcome or {}).get("rc", 0) or 0)
                if rc != 0:
                    excluded[key] = f"subprocess failed rc={rc}: {str((outcome or {}).get('stderr') or '')[-160:]}"
                    continue
                unit = _find_unit(shadow_store, tag, workflow=decl.workflow)
                if unit is None:
                    excluded[key] = "no traced unit carrying this run's tag"
                    continue
                if unit.fingerprint != expected_fp:
                    excluded[key] = f"fingerprint mismatch: {unit.fingerprint[:12]} != {expected_fp[:12]}"
                    continue
                if any(str(r.get("error") or "") and _provider_error(str(r["error"])) for r in unit.model_calls):
                    excluded[key] = "provider error (L13): quarantined"
                    continue
                produced.setdefault(item, {}).setdefault(side, {})[rep] = unit
                units_by_key[key] = unit.unit_id
    # scoring: the paired judge needs both sides of an (item, replicate);
    # the gold matrix needs each unit on its own
    for item, sides in produced.items():
        gold = adapter.gold(item)
        if gold is None or gold.version != exp.gold_versions.get(item, gold.version):
            for side, reps in sides.items():
                for rep in reps:
                    excluded[f"{item}/{side}/{rep}"] = "gold missing or changed since registration"
            continue
        for rep in sorted(set(sides.get("arm", {})) | set(sides.get("incumbent", {}))):
            arm_unit, inc_unit = sides.get("arm", {}).get(rep), sides.get("incumbent", {}).get(rep)
            if exp.metric.endswith("_review") and arm_unit is not None and inc_unit is not None \
                    and hasattr(adapter, "paired_verdict"):
                try:
                    adapter.paired_verdict(arm_unit, inc_unit, gold, shadow_store,
                                           experiment_id=exp.experiment_id, replicate=rep)
                except BudgetRefused as exc:
                    excluded[f"{item}/arm/{rep}"] = excluded[f"{item}/incumbent/{rep}"] = f"budget_cut (judge): {exc}"[:200]
                    continue
            for side, unit in (("arm", arm_unit), ("incumbent", inc_unit)):
                if unit is None:
                    continue
                try:
                    value = _score_unit(adapter, unit, shadow_store, exp.metric, gold)
                except BudgetRefused as exc:
                    excluded[f"{item}/{side}/{rep}"] = f"budget_cut (judge): {exc}"[:200]
                    continue
                if value is None:
                    excluded[f"{item}/{side}/{rep}"] = f"metric {exp.metric} unavailable for the unit"
                    units_by_key.pop(f"{item}/{side}/{rep}", None)
                    continue
                scores.setdefault(item, {}).setdefault(side, {})[rep] = value
    exp = _adjudicate(store, ledger_dir, exp, scores, excluded, units_by_key, now)
    return exp


def iso_week_of(at: float) -> str:
    from .budget import iso_week

    return iso_week(at)


def _api_llm(settings: Any):
    from ..llm import LLM

    return LLM(settings)


def _default_stage(settings, item: str, *, shadow_root: Path, run_dir: Path) -> dict:
    from types import SimpleNamespace

    from ..run_trace import RunTrace
    from .staging import stage_item

    ctx = SimpleNamespace(settings=settings, state={}, run_dir=run_dir, trace=RunTrace(run_dir / "staging_trace.jsonl"))
    snap = stage_item(ctx, item, shadow_root=shadow_root)
    snap["snapshot_path"] = str(run_dir / "snapshots" / (
        __import__("re").sub(r"[^A-Za-z0-9._-]+", "_", item) + ".json"))
    return snap


_PROVIDER_ERR = ("402", "429", "insufficient balance", "rate limit", "overloaded", "provider down")


def _provider_error(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in _PROVIDER_ERR) or any(f" {c} " in f" {low} " for c in ("500", "502", "503", "504"))


def _find_unit(shadow_store: TraceStore, tag: str, *, workflow: str = "") -> Unit | None:
    """The unit whose trace context carries exactly this run's tag (the
    executor stamps IMPROVE_UNIT_TAG into every unit of a shadow child):
    the one enrolled in the experiment's workflow when there is one (a
    child run has other, undeclared units too), else the latest."""
    from .reader import records_between, unit_key

    ids = {unit_key(r) for r in records_between(shadow_store, 0.0, float("inf"))
           if (r.get("context") or {}).get("unit_tag") == tag}
    if not ids:
        return None
    units = units_between(shadow_store, 0.0, float("inf"), grace=0.0, lookback=0.0)
    matching = [units[i] for i in ids if i in units and workflow and units[i].workflow == workflow] or \
        [units[i] for i in ids if i in units and units[i].step == "agent.review_diff"] or \
        [units[i] for i in ids if i in units]
    return max(matching, key=lambda u: u.ended) if matching else None


def _score_unit(adapter, unit: Unit, shadow_store: TraceStore, metric: str, gold) -> float | None:
    if metric == "recall_gold" and getattr(adapter, "judge", None) is not None and hasattr(adapter, "gold_match"):
        adapter.gold_match(unit, gold, shadow_store)     # BudgetRefused propagates: the unit is cut, not guessed
    outcome = adapter.fetch(unit, shadow_store)
    if outcome is None:
        return None
    scores = scores_from(adapter.match(unit, gold, outcome), adapter.findings(unit, outcome),
                         adapter.review_scores(unit, outcome))
    return scores.values.get(metric)


def _adjudicate(store, ledger_dir: Path, exp: Experiment, scores: dict, excluded: dict, units_by_key: dict,
                now: float) -> Experiment:
    deltas: dict[str, list[float]] = {}
    for item, sides in scores.items():
        arm, inc = sides.get("arm", {}), sides.get("incumbent", {})
        for rep in sorted(set(arm) & set(inc)):
            deltas.setdefault(item, []).append(arm[rep] - inc[rep])
    result = stats.paired(deltas, exp.metric)
    n_retained = result.n_items
    n_excluded = len(exp.item_set) - n_retained
    invalid = exp.state == "invalid"
    label = stats.label(result, n_required=exp.n_required, direction=exp.direction, invalid=invalid)
    exp.state = label
    exp.result = {
        "label": label, "metric": exp.metric, "mean": round(result.mean, 6), "lo": round(result.lo, 6),
        "hi": round(result.hi, 6), "sd": round(result.sd, 6), "n_registered": exp.n_available,
        "n_retained": n_retained, "n_excluded": n_excluded, "n_required": exp.n_required,
        "adequately_powered_final": n_retained >= exp.n_required, "n_verdicts": result.n_verdicts,
        "per_item": {k: round(v, 6) for k, v in result.per_item.items()}, "excluded": excluded,
        "units": units_by_key, "adjudicated_at": now,
    }
    save(ledger_dir, exp)
    store.append("decision", context={"playbook": "workflow-improve", "workflow": exp.workflow},
                 result={"type": "experiment_verdict", "experiment_id": exp.experiment_id, **exp.result})
    if exp.proposal_id:
        from .ledger import Ledger

        try:
            Ledger(ledger_dir, store, clock=lambda: now).transition(
                exp.workflow, exp.proposal_id, label if label != "invalid" else "underpowered",
                experiment_id=exp.experiment_id)
        except Exception as exc:  # noqa: BLE001 - the verdict record is authoritative
            exp.result["proposal_note"] = str(exc)[:200]
            save(ledger_dir, exp)
    return exp
