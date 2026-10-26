"""Domain code consumes neutral contracts; public facades cannot become dependencies."""
import ast
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "src" / "infermatrix_copilot"


def test_implementation_layers_do_not_import_the_public_sdk():
    offenders = []
    for path in ROOT.rglob("*.py"):
        if "sdk" in path.relative_to(ROOT).parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            modules = ([node.module or ""] if isinstance(node, ast.ImportFrom) else
                       [item.name for item in node.names] if isinstance(node, ast.Import) else [])
            if any("sdk" in module.split(".") for module in modules):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == []


def test_contract_identity_and_cold_import_are_shared_across_facades():
    script = """
import sys
from infermatrix_copilot.contracts import models
from infermatrix_copilot.sdk.v1 import ReviewResult, ReviewRuntimeConfig, RepositoryRef
assert ReviewResult is models.ReviewResult
assert ReviewRuntimeConfig is models.StrictRuntimeConfig
ReviewRuntimeConfig(RepositoryRef('repo'), '/checkout', '/allowed')
assert not any(name.startswith(('infermatrix_copilot.app.', 'infermatrix_copilot.knowledge_service.',
                               'infermatrix_copilot.kb_service.')) for name in sys.modules)
"""
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)


def test_provider_facts_come_from_registry_without_enabling_other_direct_backends():
    from infermatrix_copilot.providers.registry import PROVIDERS
    from infermatrix_copilot.sdk.v1 import provider_descriptors, provider_worker_limit
    from infermatrix_copilot.config import MAX_STRICT_WORKERS_LIMIT

    descriptors = provider_descriptors()
    assert {item.provider_id for item in descriptors} == set(PROVIDERS)
    assert {item.provider_id for item in descriptors if "direct" in item.execution_modes} == {"cursor", "codex"}
    assert provider_worker_limit() == MAX_STRICT_WORKERS_LIMIT
    for item in descriptors:
        spec = PROVIDERS[item.provider_id]
        assert item.cli_candidates == spec.cli_names
        assert item.capabilities == tuple(sorted(spec.capabilities))


def test_public_capabilities_explicitly_version_durable_execution():
    from infermatrix_copilot.sdk.v1 import DURABLE_REVIEW_API_VERSION, get_capabilities

    capabilities = get_capabilities().to_dict()
    assert capabilities["supports_durable_direct_execution"] is True
    assert capabilities["durable_review_api_version"] == DURABLE_REVIEW_API_VERSION == "1.0.0"


def test_executor_has_no_domain_imports():
    tree = ast.parse((ROOT / "engine/executor.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not set((node.module or "").split(".")) & {
                "review", "improve", "knowledge_service", "kb_service", "sdk"}


def test_pure_contracts_and_rules_do_not_depend_on_execution_or_public_facades():
    paths = ("contracts/models.py", "knowledge_service/format.py", "knowledge_service/evidence.py",
             "review/diff.py", "review/rechecks.py", "review/review_result.py",
             "review/publication_policy.py", "review/dispositions.py", "review/severity.py")
    forbidden = {"providers", "llm", "engine", "app", "sdk"}
    offenders = []
    for relative in paths:
        for node in ast.walk(ast.parse((ROOT / relative).read_text())):
            modules = ([node.module or ""] if isinstance(node, ast.ImportFrom) else
                       [item.name for item in node.names] if isinstance(node, ast.Import) else [])
            if any(set(module.split(".")) & forbidden for module in modules):
                offenders.append(f"{relative}:{node.lineno}")
    assert not offenders


def test_shared_parser_preserves_explicit_legacy_policies():
    import pytest
    from infermatrix_copilot.structured_output import decode_json_object
    from infermatrix_copilot.llm import parse_json_reply
    from infermatrix_copilot.kb_service.models import ModelUnavailable, parse_json_object
    from infermatrix_copilot.providers.json_session import decode_object

    wrapped = 'lead {"choice":"first"} tail {"choice":"second"}'
    assert decode_object(wrapped) == {"choice": "first"}
    assert parse_json_reply(wrapped) is None  # legacy greedy outer-brace policy
    with pytest.raises(ModelUnavailable):
        parse_json_object(wrapped)  # knowledge accepts objects or fences, not free prose
    multiline = '{"diagram":"first\nsecond"}'
    assert parse_json_object(multiline) == {"diagram": "first\nsecond"}
    with pytest.raises(ValueError):
        decode_object(multiline)
    assert decode_json_object('```json\n{"ok":true}\n```', policy="last_fence") == {"ok": True}


def test_auxiliary_reply_parsers_delegate_extraction():
    for relative, symbol in (
        ("improve/judges.py", "parse_verdict"),
        ("rebase_engine/plan_review.py", "_parse_review"),
        ("rebase_engine/worktree.py", "load_decision_json"),
    ):
        module = ast.parse((ROOT / relative).read_text())
        function = next(node for node in module.body
                        if isinstance(node, ast.FunctionDef) and node.name == symbol)
        calls = [node.func for node in ast.walk(function) if isinstance(node, ast.Call)]
        assert any(isinstance(call, ast.Name) and call.id == "decode_json_object" for call in calls), relative
        assert not any(isinstance(call, ast.Attribute) and isinstance(call.value, ast.Name)
                       and call.value.id == "re" for call in calls), relative


def test_vendor_adapters_do_not_own_process_supervision():
    for path in (ROOT / "providers").glob("*.py"):
        relative = path.relative_to(ROOT)
        tree = ast.parse(path.read_text())
        calls = {node.func.attr for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        assert calls.isdisjoint({"Popen", "killpg"}), relative
        assert "Timer" not in calls, relative
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
                assert not {alias.name for alias in node.names} & {
                    "Popen", "run", "call", "check_call", "check_output"}, relative
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess":
                    assert node.func.attr not in {"run", "call", "check_call", "check_output"}, relative


def test_evolution_sandbox_delegates_process_and_probe_lifecycle():
    tree=ast.parse((ROOT/'improve/isolation.py').read_text())
    calls={node.func.attr for node in ast.walk(tree)
           if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute)}
    assert calls.isdisjoint({'Popen','killpg','wait','poll','select','terminate','kill','Timer'})
    imported={alias.name for node in ast.walk(tree) if isinstance(node,ast.ImportFrom)
              and node.module=='process_execution' for alias in node.names}
    assert {'ProcessInputChannel','run_process'}.issubset(imported)


def test_sandbox_input_contract_has_one_owner_below_providers():
    from infermatrix_copilot import container_worker, sandbox_agent
    from infermatrix_copilot.providers import container_execution
    from infermatrix_copilot.sandbox import inputs, remote
    assert container_worker.normalize_inputs is sandbox_agent.normalize_inputs is inputs.normalize_inputs
    assert container_execution.normalize_inputs is remote.normalize_inputs is inputs.normalize_inputs
    assert container_worker.verify_inputs is inputs.verify_inputs
    assert container_execution.stage_inputs is inputs.stage_inputs
    for node in ast.walk(ast.parse((ROOT / 'sandbox/inputs.py').read_text())):
        if isinstance(node, ast.ImportFrom):
            assert not set((node.module or '').split('.')) & {
                'providers', 'knowledge_service', 'kb_service', 'review', 'sdk', 'app'}


def test_testing_lifecycle_and_legacy_modules_delegate_to_neutral_owners():
    from infermatrix_copilot import process_execution, process_tree
    from infermatrix_copilot.providers import process
    from infermatrix_copilot.testing import process_tree as legacy_tree
    from infermatrix_copilot.testing import runner

    assert process is process_execution
    assert legacy_tree is process_tree
    assert runner.run_process is process_execution.run_process
    assert runner._record_walk is process_tree._record_walk
    tree = ast.parse((ROOT / "testing/runner.py").read_text())
    calls = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert calls.isdisjoint({"Popen", "wait", "poll", "killpg", "Timer", "terminate", "kill"})
    for path in (ROOT / "process_execution.py", ROOT / "process_tree.py"):
        for n in ast.walk(ast.parse(path.read_text())):
            if isinstance(n, ast.ImportFrom):
                assert not set((n.module or "").split(".")) & {
                    "providers", "testing", "sdk", "review", "kb_service", "knowledge_service", "improve"}


def test_auxiliary_commands_delegate_process_ownership():
    from infermatrix_copilot import process_execution
    from infermatrix_copilot.improve import experiments
    from infermatrix_copilot.rebase_engine import rebase_tools, wheel
    from infermatrix_copilot import chat, tools

    assert experiments.run_command is rebase_tools.run_command is wheel.run_command is tools.run_command is process_execution.run_command
    assert chat.search_files is tools.search_files
    for relative in ("improve/experiments.py", "rebase_engine/rebase_tools.py", "tools.py", "chat.py", "rebase_engine/wheel.py"):
        tree = ast.parse((ROOT / relative).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"Popen", "killpg", "Timer"}, relative
                if isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess":
                    assert node.func.attr not in {"run", "call", "check_call", "check_output"}, relative
            if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
                assert not {a.name for a in node.names} & {"Popen", "run", "call", "check_call", "check_output"}, relative


def test_quality_stages_do_not_reimplement_judge_dispatch_or_pool_lifecycle():
    for relative in ("kb_service/gate.py", "kb_service/depth_judge.py",
                     "kb_service/init_history.py", "kb_service/init_feature_discovery.py"):
        tree = ast.parse((ROOT / relative).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                assert not (isinstance(node.func, ast.Attribute) and node.func.attr == "call_json"), relative
                assert not (isinstance(node.func, ast.Name) and node.func.id == "ThreadPoolExecutor"), relative
    tree = ast.parse((ROOT / "kb_service/init_knowledge_parallel.py").read_text())
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id == "ThreadPoolExecutor" for node in ast.walk(tree))


def test_common_agent_runtime_does_not_select_improvement_policy():
    for relative in ("engine/agent_runtime/runner.py", "engine/agent_runtime/knowledge.py"):
        tree = ast.parse((ROOT / relative).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert "improve" not in (node.module or "").split("."), relative
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value != "improve_shadow", relative


def test_completion_worker_delegates_to_the_registry_without_vendor_or_domain_loops():
    tree = ast.parse((ROOT / 'providers/completion_worker.py').read_text())
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    modules |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert not any(part in {'subprocess', 'process', 'sdk', 'kb_service', 'improve', 'codex', 'zcode'}
                   for module in modules for part in module.split('.'))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == 'complete']
    assert len(calls) == 1 and isinstance(calls[0].func.value, ast.Name)
    assert calls[0].func.value.id == 'adapter'


def test_native_tool_policy_compiler_has_one_owner_in_runtime_and_maintained_eval():
    # Frozen artifacts keep their original policies; maintained source cannot
    # retain a second compiler after the provider owns this capability.
    paths = list(ROOT.rglob("*.py"))
    paths += list((ROOT.parents[1]/"eval/single-capability-shadow").glob("*.py"))
    owners = []
    metadata_fields = {"apply_patch_tool_type", "experimental_supported_tools",
                       "supports_search_tool", "tool_mode", "multi_agent_version"}
    for path in paths:
        if path.name.startswith("test_"):
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Dict):
                keys = {key.value for key in node.keys if isinstance(key, ast.Constant)
                        and isinstance(key.value, str)}
                if metadata_fields <= keys:
                    owners.append(path.resolve())
    assert owners == [(ROOT/"providers/codex_policy.py").resolve()]


def test_agent_and_readonly_calls_share_native_and_original_execution_owners():
    # These guards protect the requested deletion: compatibility entries may
    # compose requests, but cannot grow another accounting/dispatch loop.
    for name in ('providers/__init__.py', 'providers/readonly_execution.py', 'providers/session_execution.py'):
        tree = ast.parse((ROOT / name).read_text())
        imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert 'budgeting' not in imports
        assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                       and node.func.id in ('call_budget', 'run_process', 'run_cli') for node in ast.walk(tree))
    for name in ('readonly_execution.py', 'session_execution.py'):
        tree = ast.parse((ROOT / 'providers' / name).read_text())
        calls = {node.func.id for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        assert {'run_original_native', 'complete_native'} <= calls


def test_shadow_guard_delegates_dispatch_and_native_failure_interpretation():
    folder = ROOT.parents[1] / 'eval' / 'single-capability-shadow'
    guard = ast.parse((folder / 'codex_guard.py').read_text())
    modules = [node.module or '' for node in ast.walk(guard) if isinstance(node, ast.ImportFrom)]
    assert not any(module.endswith(('process', 'process_execution', 'subprocess')) for module in modules)
    calls = {node.func.id for node in ast.walk(guard) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert 'execute_shared' in calls and not {'run_process', 'Popen'} & calls
    coordinator = ast.parse((folder / 'run_paired.py').read_text())
    failure = next(node for node in coordinator.body if isinstance(node, ast.FunctionDef) and node.name == 'terminal_failure')
    assert any(isinstance(node, ast.Attribute) and node.attr == 'terminal_failure' for node in ast.walk(failure))
    assert not any(isinstance(node, (ast.For, ast.While)) for node in ast.walk(failure))


def test_knowledge_shadow_drivers_use_one_application_composition():
    folder = ROOT.parents[1] / 'eval' / 'single-capability-shadow'
    for name in ('block', 'depth', 'strict_depth', 'discovery', 'maintenance'):
        tree = ast.parse((folder / (name + '_arm.py')).read_text())
        calls = {node.func.id for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        assert 'model_gateway' in calls and 'ModelGateway' not in calls
    for name, stage in (('foundation', 'foundation'), ('foundation_native', 'foundation'),
                        ('history', 'history'), ('history_native', 'history'),
                        ('screening', 'screening'), ('screening_native', 'screening'),
                        ('modules', 'modules'), ('modules_native', 'modules'), ('discovery_native', 'discovery')):
        tree = ast.parse((folder / (name + '_arm.py')).read_text())
        assert any(isinstance(node, ast.ImportFrom) and node.module == 'init_native_arm'
                   and any(alias.name == 'main' for alias in node.names) for node in tree.body)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        assert len(calls) == 1 and isinstance(calls[0].func, ast.Name) and calls[0].func.id == 'main'
        assert isinstance(calls[0].args[0], ast.Constant) and calls[0].args[0].value == stage
        assert not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                                        ast.For, ast.AsyncFor, ast.While)) for node in ast.walk(tree))
    for name in ('init_native_arm.py', 'init_fixture_policies.py'):
        tree = ast.parse((folder / name).read_text())
        calls = {node.func.id for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        assert 'model_gateway' in calls and 'ModelGateway' not in calls
    composition = ast.parse((folder / 'arm_runtime.py').read_text())
    imports = {node.module for node in ast.walk(composition) if isinstance(node, ast.ImportFrom)}
    imports |= {alias.name for node in ast.walk(composition) if isinstance(node, ast.Import) for alias in node.names}
    assert 'subprocess' not in imports
    calls = {node.func.id for node in ast.walk(composition)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert not {'Popen', 'run_process', 'run_cli'} & calls
    assert {'complete_native', 'bind_call_budget'} <= calls


def test_generic_agent_runner_does_not_select_review_context_policy():
    tree = ast.parse((ROOT / "engine/agent_runtime/runner.py").read_text())
    for node in ast.walk(tree):
        modules = ([node.module or ""] if isinstance(node, ast.ImportFrom) else
                   [item.name for item in node.names] if isinstance(node, ast.Import) else [])
        assert not any("review" in module.split(".") for module in modules)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value not in {"pr_diff", "doc_related", "review_knowledge_context"}


def test_rfc_public_client_is_the_shared_application_client():
    from infermatrix_copilot.rfc_service.client import RFCClient, RFCClientError
    from infermatrix_copilot.sdk.v1 import RFCClient as PublicClient, RFCClientError as PublicError
    assert PublicClient is RFCClient
    assert PublicError is RFCClientError
