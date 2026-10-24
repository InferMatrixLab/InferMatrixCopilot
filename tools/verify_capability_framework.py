"""Reproduce the framework handoff checks against the paired source trees.

Model/GitHub fixtures exercise the host and workflow boundaries without paid
calls or publication. Native authentication, model quality, deep recovery and
release acceptance remain separate gates in the full completion plan.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


PROVIDER_CHECKS = {
    "boundaries": ["test_capability_ownership.py", "test_step_registration_boundary.py"],
    "completion_configuration": ["test_completion_profile_configuration.py", "test_sandbox_inputs.py",
                                 "test_worker_composition.py",
                                 "test_native_workflow_composition.py",
                                 "test_reserved_execution_identity.py",
                                 "test_codex_configuration_control.py", "test_codex_policy_binding.py",
                                 "test_sandbox_agent_session.py", "test_session_usage.py",
                                 "test_session_usage_projection.py",
                                 "test_zcode_sandbox_completion.py",
                                 "test_native_identity.py",
                                 "test_provider_options.py",
                                 "test_native_credential_sources.py", "test_worker_credentials.py",
                                 "test_zcode_node_arguments.py",
                                 "test_worker_environment.py",
                                 "test_subscription_admission.py",
                                 "test_improve_p0.py::test_harness_one_shot_and_session_are_captured"],
    "auxiliary_processes": ["test_auxiliary_processes.py", "test_process_command_policy.py"],
    "shared_native_exchange": ["test_hosted_native_execution.py",
        "../eval/single-capability-shadow/test_arm_runtime.py",
        "../eval/single-capability-shadow/test_durable_worker.py",
        "../eval/single-capability-shadow/test_payload_retention.py",
        "../eval/single-capability-shadow/test_installed_arm_driver.py",
        "../eval/single-capability-shadow/test_init_native_arm.py",
        "../eval/single-capability-shadow/test_discovery_relationship_workflow.py",
        "../eval/single-capability-shadow/test_scripted_provider_composition.py",
        "../eval/single-capability-shadow/test_role_profiles.py",
        "../eval/single-capability-shadow/test_mixed_provider_runtime.py",
        "../eval/single-capability-shadow/test_legacy_provider_host.py",
        "../eval/single-capability-shadow/test_judgment_context.py",
        "../eval/single-capability-shadow/test_depth_missing_evidence.py",
        "../eval/single-capability-shadow/test_maintenance_source_case.py",
        "../eval/single-capability-shadow/test_history_source_composition.py",
        "../eval/single-capability-shadow/test_depth_proof_input.py",
        "../eval/single-capability-shadow/test_quality_plan.py",
        "../eval/single-capability-shadow/test_quality_stage_arm.py",
        "../eval/single-capability-shadow/test_discovery_source_case.py",
        "../eval/single-capability-shadow/test_shared_execution.py",
        "../eval/single-capability-shadow/test_subscription_control.py",
        "../eval/single-capability-shadow/test_codex_guard.py",
        "../eval/single-capability-shadow/test_ledger.py",
        "../eval/single-capability-shadow/test_run_paired.py"],
    "entries_and_review": ["test_cli.py", "test_mcp.py", "test_sdk_v1.py",
        "test_e2e_direct_runtime.py", "test_e2e_strict_mock.py", "test_workflow_execution.py",
        "test_review_publication_policy.py", "test_review_schema_references.py"],
    "shared_quality": ["test_quality_review_runner.py", "test_quality_dispatch_binding.py",
        "test_discovery_task_reconciliation.py",
        "test_kb_init_execution.py", "test_kb_init_skeleton.py::test_skeleton_dry_run_builds_a_valid_bootstrap",
        "test_kb_init_deepen.py::test_deepen_writes_rules_for_hot_modules_and_flips_to_shadow",
        "test_kb_init_modules.py::test_modules_dry_run_chains_on_the_skeleton_and_routes_every_module",
        "test_kb_init_knowledge.py::test_knowledge_visits_every_owner_even_with_rules_and_low_rule_target",
        "test_kb_knowledge_depth.py::test_depth_visits_rule_bearing_owner_and_tracks_separate_metrics",
        "test_kb_feature_discovery_stage.py::test_first_source_only_catalog_has_native_review_and_no_knowledge_rewrite",
        "test_kb_init_history.py::test_history_default_replays_one_call_per_pr_and_reviews_the_full_series",
        "test_kb_maintenance_flow.py::test_full_shadow_cycle_records_contradiction_gate_and_no_publication"],
    "knowledge_contract": ["test_sdk_knowledge_v1.py", "test_knowledge_validation.py",
        "test_shared_knowledge_evidence.py"],
}
BOT_CHECKS = {
    "boundaries": ["test_provider_boundary.py", "test_publication_boundary.py",
        "test_classification_model_boundary.py"],
    "review_hosts_and_publication": ["test_e2e_provider_review.py", "test_strict_backend.py",
        "test_remote_direct.py", "test_remote_strict.py", "test_host_client.py",
        "test_mixed_mode_pipeline.py", "test_worker_server.py", "test_strict_execution_binding.py",
        "test_provider_run_recovery.py"],
    "knowledge_host": ["test_knowledge_distiller.py", "test_knowledge_maintenance_sdk.py"],
}


def manifest(root, directories=("src", "playbooks", "adapters", "skills")):
    paths = [path for name in directories for path in (root / name).rglob("*")
             if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"]
    paths += [root / "pyproject.toml"]
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths) if path.is_file()}


def run_checks(name, root, provider, python, output, groups):
    before = manifest(root)
    (output / (name + "-source.json")).write_text(json.dumps(before, indent=2) + "\n")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    tests_dir = "test" if name == "copilot" else "tests"
    test_directories = (tests_dir, 'eval/single-capability-shadow') if name == 'copilot' else (tests_dir,)
    test_sources = manifest(root, test_directories)
    (output / (name + "-test-source.json")).write_text(json.dumps(test_sources, indent=2) + "\n")
    selected = [tests_dir + "/" + item for values in groups.values() for item in values]
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys([str(root / "src"), str(provider / "src")]))
    runtime = json.loads(subprocess.check_output([str(python), "-c",
        "import json,sys,importlib.metadata as m; print(json.dumps({'python':sys.executable,"
        "'packages':{n:m.version(n) for n in ('pytest','infermatrix-copilot','omni-reviewbot')}}))"],
        cwd=root, env=env, text=True))
    report = output / (name + ".xml")
    command = [str(python), "-m", "pytest", "-q", "--junitxml=" + str(report), *selected]
    with (output / (name + ".log")).open("w") as log:
        completed = subprocess.run(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
    after = manifest(root)
    changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
    after_tests = manifest(root, test_directories)
    changed_tests = sorted(path for path in test_sources.keys() | after_tests.keys()
                           if test_sources.get(path) != after_tests.get(path))
    suites = ET.parse(report).getroot().findall("testsuite") if report.exists() else []
    cases = [case for suite in suites for case in suite.findall("testcase")]
    return {"head": head, "root": str(root), "runtime": runtime, "exit_code": completed.returncode,
            "source_changed_during_validation": changed, "test_source_changed_during_validation": changed_tests,
            "groups": groups,
            "checks": {key: sum(int(s.get(key, "0")) for s in suites)
                       for key in ("tests", "failures", "errors", "skipped")},
            "executed_cases": [case.get("classname", "") + "::" + case.get("name", "") for case in cases],
            "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest() if report.exists() else ""}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--bot-root", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--full-source", action="store_true",
                        help="run both complete source suites; skips remain unverified gates")
    args = parser.parse_args()
    provider, bot = args.provider_root.resolve(), args.bot_root.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "verification-driver.py").write_bytes(Path(__file__).read_bytes())
    with ThreadPoolExecutor(max_workers=2) as pool:
        # Resolving a venv's python symlink selects the system environment.
        provider_groups = {"full_source": ["", "../eval/single-capability-shadow"]} if args.full_source else PROVIDER_CHECKS
        bot_groups = {"full_source": [""]} if args.full_source else BOT_CHECKS
        futures = {name: pool.submit(run_checks, name, root, provider, args.python.absolute(), output, groups)
                   for name, root, groups in (("copilot", provider, provider_groups), ("reviewbot", bot, bot_groups))}
        results = {name: future.result() for name, future in futures.items()}
    passed = all(result["exit_code"] == 0 and not result["source_changed_during_validation"]
                 and not result["test_source_changed_during_validation"]
                 and result["checks"]["tests"] > 0 and result["checks"]["skipped"] == 0
                 for result in results.values())
    status_key = "full_source_checks_passed" if args.full_source else "framework_checks_passed"
    result = {status_key: passed, "repositories": results,
              "scope": "full paired source suites" if args.full_source else
                       "paired source framework conformance with simulated model and GitHub boundaries",
              "full_release_acceptance": False}
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({status_key: passed, "output": str(output)}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
