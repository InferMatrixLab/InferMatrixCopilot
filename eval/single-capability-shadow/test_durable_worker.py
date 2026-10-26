"""Frozen host adapters work in both installed arms and fail before model admission."""
from pathlib import Path
import os
import subprocess
import sys

import pytest

from ledger import Refused
from strict_host import forge_cli, require_frozen_reads
from durable_worker import wait_result


def test_forge_cli_preserves_frozen_reads_and_refuses_unknown_requests(tmp_path):
    descriptor = {'kind': 'case', 'row': {'pr': 7, 'repository': 'acme/widget',
        'expected_head_sha': 'a' * 40, 'title': 'frozen', 'body': 'body'},
        'host': {'business_context': {'pr_review_context': []}}}
    before = {key: os.environ.get(key) for key in ('PATH', 'SHADOW_FORGE_REFUSAL')}
    with forge_cli(tmp_path, descriptor):
        require_frozen_reads()
        known = subprocess.run(['gh', 'repo', 'view'], capture_output=True, text=True)
        assert known.returncode == 0 and 'acme/widget' in known.stdout
        unknown = subprocess.run(['gh', 'unfrozen'], capture_output=True)
        assert unknown.returncode == 78
        with pytest.raises(Refused, match='unfrozen forge request'):
            require_frozen_reads()
    assert before == {key: os.environ.get(key) for key in before}


def test_forge_setup_does_not_import_current_product_into_the_original_arm(tmp_path):
    helper = Path(__file__).with_name('strict_host.py')
    program = '''import importlib.abc,runpy,sys
class DenyProduct(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,*args):
  if fullname.startswith('infermatrix_copilot'):raise RuntimeError('original SDK has no new persistence module')
sys.meta_path.insert(0,DenyProduct())
module=runpy.run_path(sys.argv[1])
with module['forge_cli'](sys.argv[2],{'kind':'case'}):pass
'''
    result = subprocess.run([sys.executable, '-I', '-B', '-c', program, str(helper), str(tmp_path)], capture_output=True)
    assert result.returncode == 0, result.stderr


def test_wait_timeout_never_starts_or_replaces_a_run(monkeypatch):
    from types import SimpleNamespace
    class Runtime:
        def get_result(self, run_id):
            assert run_id == 'original'
            return SimpleNamespace(terminal=False)
    with pytest.raises(Refused, match='original durable worker remains unresolved'):
        wait_result(Runtime(), 'original', 0)


def test_original_sdk_worker_gets_explicit_host_inputs_and_restores_environment(monkeypatch):
    from arm_runtime import sdk_runtime
    monkeypatch.setattr('arm_runtime.supports_shared_execution', lambda: False)
    monkeypatch.setenv('STRICT_BACKEND_MODEL', 'ambient-other')
    overrides = {'strict_backend': 'codex', 'strict_backend_cli': '/frozen/shim',
                 'strict_backend_model': 'pinned', 'strict_backend_timeout_s': 120}
    before = {key: os.environ.get(key) for key in (
        'STRICT_BACKEND', 'STRICT_BACKEND_CLI', 'STRICT_BACKEND_MODEL', 'STRICT_BACKEND_TIMEOUT_S')}
    with sdk_runtime(overrides, shim='/unused', for_worker=True):
        assert all(os.environ[key.upper()] == str(value) for key, value in overrides.items())
    assert before == {key: os.environ.get(key) for key in before}


def test_worker_review_policy_translation_preserves_types_and_restores_environment():
    from arm_runtime import _worker_environment
    import json
    overrides = {'ensemble_parallel': False, 'review_verify_concurrency': 1, 'review_lens_backends': {}}
    before = {key.upper(): os.environ.get(key.upper()) for key in overrides}
    with _worker_environment(overrides):
        assert all(json.loads(os.environ[key.upper()]) == value for key, value in overrides.items())
    assert before == {key: os.environ.get(key) for key in before}


@pytest.mark.parametrize('value', [None, 0, 330, 3929, 7201, True, '5100', float('nan')])
def test_strict_workflow_wait_requires_all_declared_call_allowances(value):
    from arm_runtime import _workflow_timeout
    plan = {'native_timeout_s': 180, 'host_timeout_s': 300,
            'native_call_limits': {'strict': 13}, 'workflow_timeout_s': value}
    with pytest.raises(Refused, match='whole-workflow'):
        _workflow_timeout(plan, require_explicit=True)


def test_workflow_wait_survives_single_call_observation_window_without_replacement(monkeypatch):
    from types import SimpleNamespace
    from arm_runtime import _workflow_timeout
    plan = {'native_timeout_s': 180, 'host_timeout_s': 300,
            'native_call_limits': {'strict': 13}, 'workflow_timeout_s': 5100}
    assert _workflow_timeout(plan, require_explicit=True) == 5100
    legacy = dict(plan); legacy.pop('workflow_timeout_s')
    assert _workflow_timeout(legacy) == 330
    clock = iter([0, 100, 350, 400])
    monkeypatch.setattr('durable_worker.time.monotonic', lambda: next(clock))
    monkeypatch.setattr('durable_worker.time.sleep', lambda _: None)
    class Runtime:
        calls = 0
        def get_result(self, run_id):
            assert run_id == 'original'
            self.calls += 1
            return SimpleNamespace(terminal=self.calls == 4)
    runtime = Runtime()
    assert wait_result(runtime, 'original', _workflow_timeout(plan)).terminal
    assert runtime.calls == 4


def test_worker_memory_path_is_explicit_isolated_and_restored(tmp_path,monkeypatch):
    from arm_runtime import _worker_environment
    from infermatrix_copilot.config import Settings
    ambient=str(tmp_path/'ambient.sqlite')
    selected=tmp_path/'case-state'/'debug_memory.db'
    monkeypatch.setenv('MEMORY_DB',ambient)
    for legacy in [False,True]:
        with _worker_environment({'memory_db':selected},legacy=legacy):
            assert os.environ['MEMORY_DB']==str(selected)
            assert Settings(_env_file=None).memory_db==selected
            child=subprocess.run([sys.executable,'-I','-c',
                'from infermatrix_copilot.config import Settings;print(Settings(_env_file=None).memory_db)'],
                capture_output=True,text=True,check=True)
            assert child.stdout.strip()==str(selected)
        assert os.environ['MEMORY_DB']==ambient
    assert not selected.exists() and not Path(ambient).exists()


def test_strict_observations_preserve_failed_steps_and_unknown_memory(tmp_path):
    import json
    from strict_review_case_arm import execution_observations
    trace=tmp_path/'run_trace.jsonl'
    trace.write_text('\n'.join(json.dumps(row) for row in [
        {'kind':'agent_output','step':'verify','ok':False,'status':None},
        {'kind':'agent_output','step':'investigate','ok':True,'status':'success'},
        {'kind':'agent_dispatch','step':'verify'},
        {'kind':'agent_dispatch','step':'investigate','memories':0},
        {'kind':'review_knowledge_injected','model_content':'not exported'}]))
    result=execution_observations(tmp_path)
    assert result['read_status']=='read' and result['reporting_only']
    assert result['agent_outputs'][0]['ok'] is False
    assert result['agent_outputs'][1]['ok'] is True
    assert result['memory_observations']==[{'step':'verify','memories':None},{'step':'investigate','memories':0}]
    assert 'not exported' not in json.dumps(result)
    assert 'quality_passed' not in result


def test_strict_observations_missing_trace_does_not_fabricate_complete(tmp_path):
    from strict_review_case_arm import execution_observations
    result=execution_observations(tmp_path)
    assert result['read_status']=='missing' and not result['agent_outputs']
