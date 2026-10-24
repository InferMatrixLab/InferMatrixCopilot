"""PR5 — tier-3 shell-golden parity (Rev 8 §8.3): the one-time capture of
the parent's LIVE config.sh §10/§11 arrays + test_watchdog.sh arrays
(`adapters/vllm_omni/rebase/shell_golden.json`, isolated-env `declare -p`,
2026-08-01) versus this substrate's PRODUCTION pipeline.

The parity tests run the real code end to end: a fixture Buildkite
pipeline generated from the golden's §10 entries goes through
`build_manifest` (the production builder), `manifest_job_to_test_job`
(the production conversion the v3 loop uses), and the real `TestRunner`
dry_run — commands must come out byte-for-byte, env under shell
semantics, timeouts intact, and job→module routing must reproduce the
recorded behavioral replay (verified identical to the parent's own
`_assign_modules` output at capture time)."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

from infermatrix_copilot.engine.steps.rebase_v3 import manifest_job_to_test_job
from infermatrix_copilot.rebase_engine.test_manifest import (ManifestSpec,
                                                             build_manifest)
from infermatrix_copilot.testing.runner import TestRunner

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN = json.loads(
    (REPO_ROOT / "adapters/vllm_omni/rebase/shell_golden.json").read_text())
MANIFEST = yaml.safe_load(
    (REPO_ROOT / "adapters/vllm_omni/manifest.yaml").read_text())
WATCHDOG_YAML = yaml.safe_load(
    (REPO_ROOT / "adapters/vllm_omni/testing/watchdog_patterns.yaml")
    .read_text())


def _golden_jobs() -> dict:
    return {s: t for s, t in GOLDEN["ci_tests"].items()
            if s != "__precommit__"}


@pytest.fixture(scope="module")
def golden_built(tmp_path_factory):
    """The GOLDEN-DERIVED fixture pipeline driven through the PRODUCTION
    builder: every §10 job becomes a Buildkite step (env as export lines,
    command verbatim, timeout in minutes) in the real nested layout."""
    repo = tmp_path_factory.mktemp("golden-fixture") / "repo"
    (repo / ".buildkite" / "cuda").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    steps = []
    for slug, t in sorted(_golden_jobs().items()):
        cmds = [f"export {tok}" for tok in shlex.split(t["env"] or "")]
        cmds += list(t["cmd"].split("\n"))
        steps.append({"label": t["label"], "commands": cmds,
                      "timeout_in_minutes": int(t["timeout_sec"]) // 60})
    (repo / ".buildkite" / "cuda" / "test-ready.yml").write_text(
        yaml.safe_dump({"steps": steps}, allow_unicode=True))
    return build_manifest(repo, ManifestSpec.from_manifest(MANIFEST))


def test_golden_capture_sanity():
    """The capture is complete and sane: a real job population, every job
    has a non-empty command (the parent's EMPTY-command false-pass set was
    empty at capture time — new drift shows up as live-yaml slugs missing
    from here, enumerated in DRIFT_TRIAGE #4), full module maps, all four
    watchdog tiers, the routing sections."""
    jobs = GOLDEN["ci_tests"]
    assert len(jobs) >= 40
    assert all(t["cmd"].strip() for t in jobs.values())
    # every REAL job is labeled (__precommit__ is §10's phase-3.2 pseudo
    # entry, deliberately label-less in the parent)
    assert all(t["label"].strip() for slug, t in jobs.items()
               if slug != "__precommit__")
    assert set(GOLDEN["module_maps"]) == {
        "MODULE_VLLM_PATHS", "MODULE_OMNI_FILES", "MODULE_TEST_MAP",
        "MODULE_IMPORT_CHECK"}
    assert len(GOLDEN["module_maps"]["MODULE_VLLM_PATHS"]) == 8
    assert all(GOLDEN["watchdog"][k]
               for k in ("critical", "review", "simulation_allowlist",
                         "noise"))
    assert set(GOLDEN["assignment_map"]) - {"_provenance"} == \
        set(GOLDEN["module_maps"]["MODULE_VLLM_PATHS"])
    # sanitation held: no machine paths or credentials in the capture
    blob = json.dumps(GOLDEN)
    for needle in ("/PLACEHOLDER_ROOT", "/data/zhoutaichang", "sk-"):
        assert needle not in blob, needle


def test_golden_timeouts_and_gpus_are_wellformed():
    for slug, t in GOLDEN["ci_tests"].items():
        assert t["timeout_sec"].isdigit() and int(t["timeout_sec"]) > 0, slug
        assert int(t["timeout_sec"]) % 60 == 0, slug
        assert t["min_gpus"] == "" or t["min_gpus"].isdigit(), slug
        for pair in shlex.split(t["env"] or ""):
            assert "=" in pair, (slug, pair)


# ── tier 3: command echo through the PRODUCTION path ─────────────────────────

def test_command_echo_parity_production_path(golden_built, tmp_path):
    """THE tier-3 pin (and this PR's partial e2e): every golden §10 job,
    parsed from a fixture pipeline by the PRODUCTION builder and converted
    by the PRODUCTION `manifest_job_to_test_job`, yields a dry-run RunPlan
    whose child command is `set -e` + the parent's command BYTE-FOR-BYTE
    (expected literal computed independently — no shared helper), with the
    parent's timeout and env pairs under the shell's own word-split
    semantics."""
    by_slug = {j.slug: j for j in golden_built.jobs}
    runner = TestRunner(repo_root=tmp_path, tests_dir=tmp_path / "tests")
    assert set(by_slug) == set(_golden_jobs())      # nothing lost, no extras
    assert golden_built.dropped == []
    for slug, t in _golden_jobs().items():
        job = manifest_job_to_test_job(golden_built.to_dict()["jobs"][
            [j["slug"] for j in golden_built.to_dict()["jobs"]].index(slug)])
        plan = runner.run(job, {}, dry_run=True).plan
        assert plan is not None, slug
        assert plan.argv[:3] == ["bash", "--norc", "-c"], slug
        # independent expected literal: the sole wrapper is the fail-fast
        # prefix, absent only when the command manages -e itself
        if "set +e" in t["cmd"]:
            assert plan.argv[3] == t["cmd"], slug
        else:
            assert plan.argv[3] == "set -e\n" + t["cmd"], slug
        assert plan.timeout_sec == float(t["timeout_sec"]), slug
        for token in shlex.split(t["env"] or ""):
            k, v = token.split("=", 1)
            assert plan.env_overlay.get(k) == v, (slug, token)


def test_gpu_lock_semantics_via_production_converter():
    """min_gpus>0 holds the GPU lock (historical rule) through the
    production conversion — pinned per golden job with the §10 gpu
    counts (queue-independent)."""
    from infermatrix_copilot.testing.runner import TestRunner
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        runner = TestRunner(repo_root=Path(td),
                            tests_dir=Path(td) / "tests")
        for slug, t in _golden_jobs().items():
            min_gpus = int(t["min_gpus"] or 1)
            job = manifest_job_to_test_job(
                {"slug": slug, "command": t["cmd"],
                 "timeout_sec": float(t["timeout_sec"]),
                 "min_gpus": min_gpus, "env": t["env"],
                 "setup": t["setup"]})
            plan = runner.run(job, {}, dry_run=True).plan
            assert plan.needs_gpu_lock == (min_gpus > 0), slug


# ── routing: the behavioral replay golden ────────────────────────────────────

def test_module_routing_replay(golden_built):
    """Job→module routing through the PRODUCTION builder reproduces the
    recorded replay (verified identical to the parent's own
    `_assign_modules` output at capture time). This is the pin that fails
    if the assignment flavor is ever merged away again — dropping the
    broad prefixes silently rerouted jobs to `platform` (round-1
    finding)."""
    expected = {s: m for s, m in GOLDEN["assignment_routing"].items()
                if s != "_provenance"}
    ours = {j.slug: j.module for j in golden_built.jobs}
    assert ours == expected
    # the misrouting failure mode specifically: online_serving must own its
    # jobs, and `platform`'s broad tests/ must not swallow the set
    from collections import Counter
    hist = Counter(ours.values())
    assert hist["online_serving"] >= 20
    assert hist["platform"] < len(ours) / 2


def test_assignment_map_is_parent_verbatim():
    """The manifest's routing flavor (rebase.test_manifest
    .assignment_paths) is the parent test_manifest.py inline map VERBATIM
    — a DISTINCT flavor from §11 MODULE_TEST_MAP, never merged
    (DRIFT_TRIAGE #6)."""
    ours = MANIFEST["rebase"]["test_manifest"]["assignment_paths"]
    golden = {m: v for m, v in GOLDEN["assignment_map"].items()
              if m != "_provenance"}
    assert ours == golden


# ── module maps: adapter data == parent §11 ──────────────────────────────────

def test_module_map_parity_upstream_and_tests():
    """`modules.*.upstream_paths` / `test_paths` carry the parent's §11
    MODULE_VLLM_PATHS / MODULE_TEST_MAP verbatim (order preserved)."""
    mods = MANIFEST["modules"]
    golden_up = GOLDEN["module_maps"]["MODULE_VLLM_PATHS"]
    golden_tests = GOLDEN["module_maps"]["MODULE_TEST_MAP"]
    # review-scoping modules (local_paths-only, merged from main 2026-08)
    # carry no upstream/test maps and get zero commits at assignment
    # (assign.py marks them skip) — the parity contract binds exactly the
    # parent's rebase module set, which must carry upstream_paths verbatim
    rebase_mods = {m for m, spec in mods.items() if "upstream_paths" in spec}
    assert rebase_mods == set(golden_up) == set(golden_tests)
    for mod in rebase_mods:
        assert mods[mod]["upstream_paths"] == golden_up[mod].split(), mod
        assert mods[mod]["test_paths"] == golden_tests[mod].split(), mod


def test_module_local_paths_cover_parent_omni_files():
    """DRIFT_TRIAGE #2 (CLOSED) stays closed: every parent
    MODULE_OMNI_FILES entry is covered by the module's `local_paths`
    (prefix match — local_paths is the recorded UNION superset)."""
    mods = MANIFEST["modules"]
    for mod, files in GOLDEN["module_maps"]["MODULE_OMNI_FILES"].items():
        local = tuple(mods[mod]["local_paths"])
        for f in files.split():
            assert any(f == p or f.startswith(p.rstrip("/") + "/")
                       or f.startswith(p) for p in local), (mod, f)


def test_module_import_check_parity():
    """Per-module import smoke targets match the parent's
    MODULE_IMPORT_CHECK (adapter data: modules.*.import_check)."""
    mods = MANIFEST["modules"]
    for mod, target in GOLDEN["module_maps"]["MODULE_IMPORT_CHECK"].items():
        ours = mods[mod].get("import_check", "")
        assert ours == target, (mod, ours, target)


# ── watchdog inventories: yaml seed == parent arrays (translated) ────────────

_POSIX_TO_RE = {
    "[[:space:]]": r"\s",
    r"\/": "/",           # ERE-escaped slash is a plain slash in `re`
}


def _translate(pattern: str) -> str:
    for posix, py in _POSIX_TO_RE.items():
        pattern = pattern.replace(posix, py)
    return pattern


@pytest.mark.parametrize("tier", ["critical", "review",
                                  "simulation_allowlist", "noise"])
def test_watchdog_inventory_bijection(tier):
    """Every parent watchdog pattern exists in the adapter seed (after the
    documented POSIX-class/escape translation) and vice versa — an
    inventory drift in either direction fails offline. The PR5 sweep
    itself caught one: the parent's post-PR1 'Released CuMem memory pool'
    noise entry."""
    golden = {_translate(p) for p in GOLDEN["watchdog"][tier]}
    ours = set(WATCHDOG_YAML[tier])
    assert golden == ours, {
        "only_parent": sorted(golden - ours),
        "only_seed": sorted(ours - golden)}
    for p in ours:
        re.compile(p)     # every seed pattern is valid `re`


# ── tier 1: push policy bytes ────────────────────────────────────────────────

def test_push_policy_parity():
    """Commit-message template and the never-committed unstage set match
    the parent byte-for-byte (92_push_to_ci.sh literal +
    unstage_generated_outputs case patterns)."""
    push = MANIFEST["rebase"]["push"]
    assert push["commit_message_template"] == \
        GOLDEN["push"]["commit_message_template"]
    assert push["unstage_globs"] == GOLDEN["push"]["unstage_case_patterns"]


# ── DRIFT #4: the false-pass mechanism is dead AND visible ───────────────────

def test_empty_command_dropped_loudly(tmp_path):
    """The parent's §10 false-pass (missing slug -> empty command -> rc=0
    'pass') is structurally impossible AND never silent: labeled steps
    with no runnable command land in `BuiltManifest.dropped` (the run side
    classifies each structural); pure-export stripping never eats a
    compound `export X=1 && pytest` command; Buildkite's singular
    `command:` form is parsed."""
    repo = tmp_path / "repo"
    (repo / ".buildkite" / "cuda").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".buildkite" / "cuda" / "test-merge.yml").write_text(
        yaml.safe_dump({"steps": [
            {"label": "Real Job", "commands": ["pytest tests/x.py"]},
            {"label": "Block Step", "commands": []},
            {"label": "Export Only", "commands": ["export A=1"]},
            {"label": "Comment Only",
             "commands": ["# pytest temporarily disabled"]},
            {"label": "Export Plus Comment",
             "commands": ["export B=2", "# disabled while flaky"]},
            {"label": "Tab Export",
             "commands": ["export\tC=3", "pytest tests/t.py"]},
            {"label": "Compound Export",
             "commands": ["export X=1 && pytest tests/y.py"]},
            {"label": "Singular Form", "command": "pytest tests/z.py"},
            # the remaining valid setup-only false-pass forms (round-3):
            {"label": "Multi Export", "commands": ["export A=1 B=2"]},
            {"label": "Semicolon Export", "commands": ["export A=1;"]},
            {"label": "Commented Export",
             "commands": ["export A=1 # gpu knob"]},
            {"label": "Chained Export",
             "commands": ["export A=1; export B=2"]},
            {"label": "Wrapped Comment",
             "commands": ['timeout 20m bash -c "# disabled"']},
            {"label": "Wrapped Empty",
             "commands": ['timeout 20m bash -c ""']},
            {"label": "Wrapped Export",
             "commands": ['timeout 20m bash -c "export A=1"']},
            {"label": "Wrapped Set Only",
             "commands": ['timeout 20m bash -c "set -e"']},
            {"label": "Pipefail Set Only",
             "commands": ["set -euo pipefail"]},
            {"label": "Option Set Only", "commands": ["set -o pipefail"]},
            {"label": "Xtrace Set Only", "commands": ["set +o xtrace"]},
            {"label": "Commented Set", "commands": ["set -euo pipefail # strict"]},
            {"label": "Chained Set",
             "commands": ["set -e; set -o pipefail"]},
            {"label": "Set Then Export",
             "commands": ["set -e; export A=1"]},
            {"label": "Wrapped Pipefail",
             "commands": ['timeout 20m bash -c "set -euo pipefail"']},
            {"label": "Wrapped Chained Set",
             "commands": ['timeout 20m bash -c "set -e; set -o pipefail"']},
            {"label": "Rich Env Job",
             "commands": ["export A=1 B=2 # tuned", "pytest tests/r.py"]},
            {"label": "Quoted Hash Job",
             "commands": ["export NOTE='foo # bar' # knob",
                          "pytest tests/q.py"]},
        ]}))
    built = build_manifest(repo, ManifestSpec.from_manifest(MANIFEST))
    by_slug = {j.slug: j for j in built.jobs}
    assert set(by_slug) == {"real_job", "tab_export", "compound_export",
                            "singular_form", "rich_env_job",
                            "quoted_hash_job"}
    # the compound export line IS the command — never stripped into env
    assert by_slug["compound_export"].command == \
        "export X=1 && pytest tests/y.py"
    assert by_slug["compound_export"].env == ""
    assert by_slug["singular_form"].command == "pytest tests/z.py"
    # export extraction uses the SAME whitespace grammar as detection:
    # `export\tC=3` yields the clean token C=3, never a malformed key
    assert by_slug["tab_export"].env == "C=3"
    assert by_slug["tab_export"].command == "pytest tests/t.py"
    # multi-assignment/comment env extraction stays clean and complete
    assert by_slug["rich_env_job"].env == "A=1 B=2"
    assert by_slug["rich_env_job"].command == "pytest tests/r.py"
    # a `#` INSIDE a quoted value is value, not comment — the child env
    # must receive the intact assignment, while comment text after a real
    # boundary never masquerades as env
    assert by_slug["quoted_hash_job"].env == "NOTE='foo # bar'"
    import shlex as _shlex
    assert dict(t.split("=", 1) for t in _shlex.split(
        by_slug["quoted_hash_job"].env)) == {"NOTE": "foo # bar"}
    # dropped steps are SURFACED, not silent (run side marks structural);
    # comment-only bodies are rc=0 no-ops — the same false-pass class,
    # including the setup-only bash forms and the wrapped variants (whose
    # runnability is judged AFTER `timeout ... bash -c` normalization with
    # the SAME setup-only grammar — wrapped empty/export/set bodies are
    # no-op tests; a GPU-ineligible one would otherwise be hw-skipped
    # before the run-side guard and vanish from the push gate)
    assert sorted(built.dropped) == [
        "Block Step", "Chained Export", "Chained Set", "Comment Only",
        "Commented Export", "Commented Set", "Export Only",
        "Export Plus Comment", "Multi Export", "Option Set Only",
        "Pipefail Set Only", "Semicolon Export", "Set Then Export",
        "Wrapped Chained Set", "Wrapped Comment", "Wrapped Empty",
        "Wrapped Export", "Wrapped Pipefail", "Wrapped Set Only",
        "Xtrace Set Only"]
    assert built.to_dict()["dropped"] == built.dropped
    # ...but a `set` line with a substitution EXECUTES and must stay
    # runnable — the setup-only grammar admits option words only
    from infermatrix_copilot.rebase_engine.test_manifest import \
        is_runnable_command
    assert is_runnable_command("set $(./collect_args.sh)")
    assert is_runnable_command("set -e\npytest tests/x.py")
    assert is_runnable_command("set -e; pytest tests/x.py")
    assert not is_runnable_command("set -euo pipefail")
    assert not is_runnable_command("set -euo pipefail # strict")
    assert not is_runnable_command("set -e; set -o pipefail")
    assert not is_runnable_command("set +o xtrace")
    assert not is_runnable_command("set -o pipefail\nexport A=1\n# note")
