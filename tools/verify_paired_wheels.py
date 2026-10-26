"""Build and exercise the paired wheels outside both source checkouts.

Run with the provider development environment (which includes ``build``), e.g.
python tools/verify_paired_wheels.py --bot-root ../ReviewBot --output /tmp/paired-wheels
The only simulated boundary is the model CLI / GitHub fixture; durable child
execution, SDK loading, packaged resources and consumer adapters are real.
"""
from __future__ import annotations

import argparse
import ast
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv
import xml.etree.ElementTree as ET
import zipfile

import yaml


def verify_wheel_source(wheel: Path, manifest: dict[str, str]) -> dict:
    """Bind every production Python file to the wheel and verify its RECORD."""
    with zipfile.ZipFile(wheel) as archive:
        record, = [name for name in archive.namelist() if name.endswith(".dist-info/RECORD")]
        checked = 0
        for name, digest, size in csv.reader(io.StringIO(archive.read(record).decode())):
            if not digest:
                continue
            algorithm, expected = digest.split("=", 1)
            payload = archive.read(name)
            actual = base64.urlsafe_b64encode(hashlib.new(algorithm, payload).digest()).rstrip(b"=").decode()
            if actual != expected or len(payload) != int(size):
                raise ValueError(f"wheel RECORD mismatch: {name}")
            checked += 1
        sources = 0
        for name, expected in manifest.items():
            if name.startswith("src/") and name.endswith(".py"):
                if hashlib.sha256(archive.read(name[4:])).hexdigest() != expected:
                    raise ValueError(f"wheel source mismatch: {name}")
                sources += 1
    return {"sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "record_entries": checked, "python_sources": sources}


def prepare_foundation_checks(provider: Path, output: Path) -> tuple[Path, list[str]]:
    return prepare_workflow_checks(provider, output, "foundation", [
        "test_kb_foundation_feedback.py", "test_kb_foundation_parallel.py",
        "test_kb_foundation_publication.py"])


def prepare_depth_checks(provider: Path, output: Path) -> tuple[Path, list[str]]:
    return prepare_workflow_checks(provider, output, "depth", [
        "test_kb_lightweight_runtime.py", "test_kb_depth_completion_proofs.py"])


def prepare_recovery_checks(provider: Path, output: Path) -> tuple[Path, list[str]]:
    return prepare_workflow_checks(provider, output, "recovery", [
        "test_kb_gate_receipts.py", "test_kb_review_admission.py",
        "test_kb_intake_gate.py", "test_kb_refine.py", "test_kb_flow.py",
        "test_kb_companion.py", "test_kb_generator_fallback.py",
        "test_kb_replay_capture.py", "test_kb_maintenance_flow.py",
        "test_quality_review_runner.py", "test_quality_dispatch_binding.py", "test_dispatch_request_snapshots.py",
        "test_native_attempt_recovery.py", "test_init_budget_call_identity.py", "test_init_cost_recovery.py",
        "test_init_review_recovery.py", "test_init_task_replay.py", "test_discovery_task_replay.py",
        "test_discovery_task_reconciliation.py",
        "test_completion_profile_configuration.py",
        "test_kb_init_budget_checkpoint.py", "test_kb_init_parallel_budget.py", "test_container_completion.py",
        "test_container_execution.py", "test_container_worker.py", "test_worker_credentials.py",
        "test_native_credential_sources.py", "test_zcode_sandbox_completion.py",
        "test_network_relay.py", "test_sandbox_execution.py", "test_sandbox_control.py",
        "test_sandbox_inputs.py", "test_sandbox_snapshot_controls.py", "test_sandbox_agent_session.py", "test_readonly_sandbox.py", "test_codex_configuration_control.py", "test_codex_policy_binding.py"])


def prepare_ownership_checks(provider: Path, output: Path) -> tuple[Path, list[str]]:
    return prepare_workflow_checks(provider, output, "ownership", [
        "test_source_fingerprint.py", "test_git_objects.py", "test_replay_capture_pr_review.py",
        "test_kb_pinned_observer_cache.py", "test_kb_pinned_claims.py",
        "test_feature_discovery_index.py", "test_kb_reconcile_reviewed.py",
        "test_kb_feature_discovery.py", "test_kb_feature_discovery_boundary.py",
        "test_kb_feature_discovery_consolidation.py", "test_kb_foundation_related_tests.py",
        "test_kb_discovery_contract_units.py", "test_kb_maintenance_flow.py",
        "test_kb_maintenance_store.py", "test_quality_review_runner.py"])


def prepare_policy_checks(provider: Path, output: Path) -> tuple[Path, list[str]]:
    return prepare_workflow_checks(provider, output, "policy", [
        "test_agent_runtime.py", "test_improve_p0b.py", "test_workflow_execution.py",
        "test_moa.py", "test_auxiliary_processes.py", "test_process_command_policy.py", "test_scopes_tools.py",
        "test_review_schema_references.py"])


def prepare_evaluation_checks(provider: Path, output: Path) -> tuple[Path, list[str]]:
    return prepare_workflow_checks(provider, output, "evaluation", [
        "test_init_native_arm.py", "test_discovery_relationship_workflow.py",
        "test_scripted_provider_composition.py", "test_legacy_provider_host.py",
        "test_arm_runtime.py", "test_role_profiles.py", "test_shared_execution.py", "test_codex_guard.py", "test_depth_missing_evidence.py", "test_judgment_context.py",
        "test_ledger.py", "test_run_paired.py", "test_durable_worker.py", "test_maintenance_source_case.py",
        "test_history_source_composition.py", "test_subscription_control.py", "test_init_role_policy.py", "test_block_contract.py",
        "test_depth_proof_input.py", "test_quality_plan.py", "test_quality_stage_arm.py",
        "test_discovery_source_case.py"],
        module_roots=[provider / "eval/single-capability-shadow", provider / "test"])


def prepare_workflow_checks(provider: Path, output: Path, group: str,
                            selected: list[str], *, module_roots: list[Path] | None = None) -> tuple[Path, list[str]]:
    """Copy only test helpers; all product imports must resolve to the wheel."""
    checks = output / (group + "-checks")
    checks.mkdir(parents=True, exist_ok=False)
    roots = module_roots or [provider / "test"]
    pending, copied = list(selected), set()
    while pending:
        name = pending.pop()
        if name in copied:
            continue
        candidates = [root / name for root in roots if (root / name).is_file()]
        if not candidates:
            raise FileNotFoundError(f"missing frozen check module: {name}")
        source = candidates[0].read_text()
        if any(path.read_text() != source for path in candidates[1:]):
            raise ValueError(f"ambiguous frozen check module: {name}")
        (checks / name).write_text(source)
        copied.add(name)
        for node in ast.walk(ast.parse(source)):
            modules = ([node.module] if isinstance(node, ast.ImportFrom) else
                       [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
            pending.extend(module + ".py" for module in modules
                           if module and "." not in module and module != "conftest"
                           and any((root / (module + ".py")).is_file() for root in roots))
    validators = output / "knowledge/tools"
    validators.mkdir(parents=True, exist_ok=True)
    for name in ("check_knowledge_tree.py", "check_wiki_lint.py"):
        shutil.copyfile(provider / "knowledge/tools" / name, validators / name)
    (checks / "pytest.ini").write_text("[pytest]\naddopts = -q\n")
    shutil.copyfile(provider / "test/conftest.py", checks / "provider_fixtures.py")
    (checks / "conftest.py").write_text('''import importlib
import os
from provider_fixtures import settings, trace, git_repo, _no_real_run_roots

def pytest_sessionstart(session):
    assert "PYTHONPATH" not in os.environ
    for name in ("infermatrix_copilot.kb_service.init_knowledge_parallel",
                 "infermatrix_copilot.kb_service.quality_review", "omni_reviewbot"):
        assert "site-packages" in importlib.import_module(name).__file__
''')
    manifest = {str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
                for directory in (checks, validators) for path in sorted(directory.rglob("*")) if path.is_file()}
    (output / (group + "-checks-manifest.json")).write_text(json.dumps(manifest, indent=2) + "\n")
    return checks, selected


def freeze_checkout(root, destination):
    """Freeze working-tree source and tests before any slow build/install work."""
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root).decode().split("\0")
    manifest = {}
    for name in sorted(set(filter(None, names))):
        # Snapshot build inputs, runtime resources and tests. Architecture
        # progress reports and evaluation archives are not wheel inputs.
        path = Path(name)
        if len(path.parts) > 1 and path.parts[0] not in {
                "src", "knowledge", "playbooks", "adapters", "skills",
                "test", "tests", "eval", "scripts", "deploy", ".github"} and not name.startswith("doc/knowledge/"):
            continue
        source = root / name
        if not source.is_file() and not source.is_symlink():
            continue  # tracked deletion
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)
        manifest[name] = hashlib.sha256(
            os.readlink(target).encode() if target.is_symlink() else target.read_bytes()).hexdigest()
    # Detect edits during the copy rather than silently validating a mixed tree.
    for name, expected in manifest.items():
        source = root / name
        actual = hashlib.sha256(os.readlink(source).encode() if source.is_symlink() else source.read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError(f"source changed during validation snapshot: {name}")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bot-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--constraints", type=Path,
                        help="Pinned dependency versions for reproducible paired validation")
    args = parser.parse_args()
    provider = Path(__file__).resolve().parents[1]
    bot, output = args.bot_root.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    snapshot = output / "source"
    if snapshot.exists():
        raise ValueError("validation output already has a source snapshot; use a new output directory")
    manifest = {"provider": freeze_checkout(provider, snapshot / "provider"),
                "bot": freeze_checkout(bot, snapshot / "bot")}
    (output / "source-manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    shutil.copyfile(__file__, output / "validation-driver.py")
    (output / "validation-driver.sha256").write_text(
        hashlib.sha256(Path(__file__).read_bytes()).hexdigest() + "\n")
    provider, bot = snapshot / "provider", snapshot / "bot"
    # These are workflow fixture inputs, never Python import roots.
    for name in ("adapters", "playbooks"):
        shutil.copytree(provider / name, output / name)
    shutil.copytree(bot / "deploy", output / "deploy")
    wheels, tests = output / "wheels", output / "tests"
    wheels.mkdir(exist_ok=True)
    tests.mkdir(exist_ok=True)
    (tests / "fixtures").mkdir()
    shutil.copyfile(provider / "test/fixtures/legacy-json-extraction.json",
                    tests / "fixtures/legacy-json-extraction.json")
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["INFERMATRIX_WHEEL_VALIDATION"] = "1"

    def run(argv, *, cwd=output):
        subprocess.run([str(arg) for arg in argv], cwd=cwd, env=environment, check=True)

    for root in (provider, bot):
        run([sys.executable, "-m", "build", "--wheel", "--outdir", wheels, root])
    venv.EnvBuilder(with_pip=True, clear=True).create(output / "env")
    python = output / "env/bin/python"
    provider_wheel, = wheels.glob("infermatrix_copilot-*.whl")
    bot_wheel, = wheels.glob("omni_reviewbot-*.whl")
    (output / "wheel-integrity.json").write_text(json.dumps({
        "provider": verify_wheel_source(provider_wheel, manifest["provider"]),
        "bot": verify_wheel_source(bot_wheel, manifest["bot"])}, indent=2) + "\n")
    constraints = []
    if args.constraints:
        shutil.copyfile(args.constraints.resolve(), output / "dependency-pins.txt")
        constraints = ["--constraint", output / "dependency-pins.txt"]
    # Exercise the real consumer dependency closure before MCP/dev extras can
    # accidentally supply dependencies required by the public SDK itself.
    run([python, "-I", "-m", "pip", "install", "--no-compile", *constraints,
         provider_wheel, bot_wheel])
    minimal_probe = output / "minimal-sdk-probe.py"
    minimal_probe.write_text('''import importlib.util
import json
import sys
from pathlib import Path
from infermatrix_copilot.sdk.v1 import ReviewRuntime

root = Path(sys.argv[1])
assert importlib.util.find_spec("mcp") is None, "MCP must not mask base dependencies"
execution = root / "minimal-sdk-must-not-dispatch"
try:
    ReviewRuntime.run_session("invalid schema must fail before dispatch",
        cwd=root, command=["/must-not-execute"], provider="codex", model="fixture",
        output_schema={"type": "invalid-json-schema-type"},
        idle_timeout_s=1, absolute_timeout_s=2,
        execution_config={}, execution_root=execution)
except Exception as exc:
    assert type(exc).__name__ == "SchemaError", repr(exc)
else:
    raise AssertionError("invalid schema was accepted")
assert not execution.exists(), "invalid schema must not reserve provider work"
(root / "minimal-sdk-result.json").write_text(json.dumps({
    "public_sdk_schema_preflight": True, "mcp_installed": False,
    "provider_dispatched": False, "source_path_injection": False}) + "\\n")
''')
    run([python, "-I", minimal_probe, output])
    run([python, "-m", "pip", "install", "--no-compile", *constraints,
         str(provider_wheel) + "[kb,mcp,dev]", bot_wheel])
    run([python, "-m", "pip", "check"])
    (output / "installed-dependencies.json").write_bytes(subprocess.check_output(
        [str(python), "-I", "-m", "pip", "list", "--format=json"], cwd=output, env=environment))
    (output / "wheel-hashes.json").write_text(json.dumps({
        wheel.name: hashlib.sha256(wheel.read_bytes()).hexdigest()
        for wheel in (provider_wheel, bot_wheel)}, indent=2) + "\n")

    for name in (
        "conftest.py", "provider_review_fake.py", "sdk_session_helpers.py",
        "test_e2e_provider_review.py", "test_knowledge_maintenance_sdk.py",
        "test_knowledge_maintenance.py", "test_pipeline.py",
        "test_provider_run_recovery.py", "test_strict_workflow_detail.py",
        "test_strict_execution_binding.py", "test_mixed_mode_pipeline.py",
        "test_worker_server.py", "test_sweep_arms.py", "test_route_by_arm.py",
        "test_host_client.py", "test_fallback_arm.py", "test_remote_strict.py",
        "test_review_attempts.py", "test_worker_client.py",
    ):
        shutil.copyfile(bot / "tests" / name, tests / name)
    shutil.copyfile(provider / "test/conftest.py", tests / "provider_fixtures.py")
    with (tests / "conftest.py").open("a") as stream:
        stream.write("\nfrom provider_fixtures import settings, trace, git_repo, _no_real_run_roots, install_mini_rebase_playbook\n")
    validators = output / "knowledge/tools"
    validators.mkdir(parents=True)
    for name in ("check_knowledge_tree.py", "check_wiki_lint.py"):
        shutil.copyfile(provider / "knowledge/tools" / name, validators / name)
    for name in ("test_sdk_knowledge_v1.py", "test_e2e_direct_runtime.py", "test_e2e_strict_mock.py",
                 "test_cli.py", "test_mcp.py", "test_kb_init_execution.py", "test_knowledge_wheel_resources.py",
                 "test_kb_init_skeleton.py", "test_knowledge_view_routing.py",
                 "test_execution_environment.py", "test_provider_process.py", "test_parsed_diff.py",
                 "test_review_publication_policy.py", "test_quality_review_runner.py",
                 "test_deepseek_worker.py", "test_idempotency.py", "test_json_session.py",
                 "test_llm_providers.py", "test_provider_claude_code.py", "test_provider_codex.py",
                 "test_provider_completion.py", "test_provider_cursor.py", "test_provider_deepseek.py",
                 "test_provider_descriptors.py", "test_provider_version.py", "test_provider_zcode.py", "test_session_usage.py",
                 "test_shared_budgeting.py", "test_structured_output_policies.py", "test_schema_projection.py",
                 "test_kb_intake_gate.py", "test_kb_maintenance_flow.py"):
        source = (provider / "test" / name).read_text()
        # Source tests make child interpreters import their checkout. In this
        # harness the installed wheel must provide every child module.
        source = source.replace(
            '    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"))\n', "")
        (tests / name).write_text(source)
    for name in ("test_e2e_direct_runtime.py", "test_e2e_provider_review.py"):
        path = tests / name
        source = path.read_text()
        needle = "prompt = sys.stdin.read()\n"
        assert source.count(needle) >= 1
        source = source.replace(needle, needle +
            'import os\nimport infermatrix_copilot.sdk.v1 as installed_sdk\n'
            'assert "site-packages" in installed_sdk.__file__\n'
            'assert "PYTHONPATH" not in os.environ\n')
        path.write_text(source)
    source = (provider / "test/test_sdk_v1.py").read_text()
    names = {"test_old_strict_serialization_keeps_its_wire_shape",
             "test_new_direct_proofs_survive_result_and_nested_poll_serialization"}
    functions = [ast.get_source_segment(source, node) for node in ast.parse(source).body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(functions) == len(names)
    (tests / "test_sdk_wire_golden.py").write_text(
        'from infermatrix_copilot.sdk.v1 import RepositoryRef\nHEAD = "a" * 40\n\n' +
        "\n\n".join(functions) + "\n")
    (tests / "test_wheel_origin.py").write_text('''import importlib
import importlib.metadata as metadata
import json
import os

def test_production_modules_come_from_installed_wheels():
    assert "PYTHONPATH" not in os.environ
    for name in ("infermatrix_copilot.sdk.v1", "infermatrix_copilot.app.run_service",
                 "infermatrix_copilot.knowledge_service.validators.tree",
                 "infermatrix_copilot.knowledge_service.validators.wiki",
                 "infermatrix_copilot.providers.json_session", "omni_reviewbot.pipeline"):
        assert "site-packages" in importlib.import_module(name).__file__
    for name in ("infermatrix-copilot", "omni-reviewbot"):
        origin = json.loads(metadata.distribution(name).read_text("direct_url.json"))
        assert origin["url"].endswith(".whl")
        assert not origin.get("dir_info", {}).get("editable", False)
''')
    (tests / "pytest.ini").write_text("[pytest]\naddopts = -q\n")
    (output / "main-checks-manifest.json").write_text(json.dumps({
        str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(tests.rglob("*")) if path.is_file()
    }, indent=2) + "\n")
    shutil.copyfile(bot / "scripts/verify-release-sdk.py", output / "verify-release-sdk.py")
    expected = yaml.safe_load((bot / ".github/workflows/deploy.yml").read_text())["env"]
    run([python, "-I", output / "verify-release-sdk.py", "--expect-direct-api", expected["EXPECTED_DIRECT_API"],
         "--expect-strict-api", expected["EXPECTED_STRICT_API"], "--expect-knowledge-api", expected["EXPECTED_KNOWLEDGE_API"],
         "--strict-run-root", output / "smoke-runs", "--output", output / "release-smoke.json"])
    run([python, "-I", "-m", "pytest", "--junitxml=" + str(output / "tests.xml"),
         "--ignore=" + str(tests / "test_kb_init_skeleton.py"),
         "--ignore=" + str(tests / "test_knowledge_view_routing.py"), tests], cwd=tests)
    foundation, selected = prepare_foundation_checks(provider, output)
    run([python, "-I", "-m", "pytest", "--junitxml=" + str(output / "foundation-tests.xml"),
         *selected], cwd=foundation)
    depth, selected = prepare_depth_checks(provider, output)
    run([python, "-I", "-m", "pytest", "--junitxml=" + str(output / "depth-tests.xml"),
         *selected], cwd=depth)
    recovery, selected = prepare_recovery_checks(provider, output)
    run([python, "-I", "-m", "pytest", "--junitxml=" + str(output / "recovery-tests.xml"),
         *selected], cwd=recovery)
    for group, prepare in (("ownership", prepare_ownership_checks), ("policy", prepare_policy_checks),
                           ("evaluation", prepare_evaluation_checks)):
        checks, selected = prepare(provider, output)
        run([python, "-I", "-m", "pytest", "--junitxml=" + str(output / (group + "-tests.xml")),
             *selected], cwd=checks)
    reports = {name: output / filename for name, filename in (
        ("main", "tests.xml"), ("foundation", "foundation-tests.xml"),
        ("depth", "depth-tests.xml"), ("recovery", "recovery-tests.xml"),
        ("ownership", "ownership-tests.xml"), ("policy", "policy-tests.xml"),
        ("evaluation", "evaluation-tests.xml"))}
    results = {}
    for name, path in reports.items():
        suites = ET.parse(path).getroot().findall("testsuite")
        results[name] = {
            "checks": {key: sum(int(suite.get(key, "0")) for suite in suites)
                       for key in ("tests", "failures", "errors", "skipped")},
            "report_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    for name, result in results.items():
        if any(result["checks"][key] for key in ("failures", "errors", "skipped")):
            raise RuntimeError(f"installed group {name} has failures, errors or skips; retain its raw report")
    (output / "result.json").write_text(json.dumps({
        "paired_installed_checks_passed": True,
        "scope": "frozen paired wheels; installed main flows and selected conformance suites with simulated model/GitHub boundaries",
        "full_release_acceptance": False,
        "source_manifest_sha256": hashlib.sha256((output / "source-manifest.json").read_bytes()).hexdigest(),
        "wheel_integrity": json.loads((output / "wheel-integrity.json").read_text()),
        "groups": results,
    }, indent=2) + "\n")
    print(json.dumps({"wheels": [str(provider_wheel), str(bot_wheel)],
                      "result": str(output / "result.json"),
                      "tests": str(output / "tests.xml"),
                      "foundation_tests": str(output / "foundation-tests.xml"),
                      "depth_tests": str(output / "depth-tests.xml"),
                      "recovery_tests": str(output / "recovery-tests.xml"),
                      "ownership_tests": str(output / "ownership-tests.xml"),
                      "policy_tests": str(output / "policy-tests.xml"),
                      "evaluation_tests": str(output / "evaluation-tests.xml")}, indent=2))


if __name__ == "__main__":
    main()
