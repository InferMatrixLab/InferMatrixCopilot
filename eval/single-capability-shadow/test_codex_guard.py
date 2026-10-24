"""Exercise real subprocess admission with a local fake Codex executable."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from codex_guard import dispatch
from ledger import Refused
from shadow_test_support import shared_plan, simulate_shared, bootstrap_guard


@pytest.fixture(autouse=True)
def execution_boundary(monkeypatch):
    monkeypatch.setattr("codex_guard.execute_shared", simulate_shared)


def fixture(tmp_path, body):
    binary = tmp_path / "codex"
    binary.write_text(f"#!{sys.executable}\n" + body)
    binary.chmod(0o700)
    return dict(ledger_path=tmp_path / "calls.sqlite",
                plan={"native_call_limits": {"screening": 2},
                      "provider_binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                      "supply_profile_defaults": True,
                      "execution_profile": {"model": "fixture", "reasoning_effort": "high",
                          "config_overrides": ['model_reasoning_effort="high"']},
                      "shared_execution": shared_plan(tmp_path, binary)},
                case_id="screening", arm="baseline", binary=binary,
                args=["exec", "--json", "--sandbox", "read-only", "-"], cwd=tmp_path,
                env={"PATH": "/usr/bin:/bin"}, prompt="frozen prompt")


def test_native_output_is_replayed_without_another_process(tmp_path):
    args = fixture(tmp_path, 'from pathlib import Path\n'
                   'p=Path("dispatches"); p.write_text(p.read_text()+"x" if p.exists() else "x")\n'
                   'print(\'{"type":"turn.completed","usage":{"input_tokens":3}}\')\n')
    first_lines, replay_lines = [], []
    first = dispatch(**args, on_line=lambda *line: first_lines.append(line))
    second = dispatch(**args, on_line=lambda *line: replay_lines.append(line))
    assert first == second
    assert first_lines == replay_lines
    assert first["usage"] == {"input_tokens": 3}
    assert first["cost_usd"] is None
    assert (tmp_path / "dispatches").read_text() == "x"


@pytest.mark.parametrize('mode', ['default', 'explicit', 'wrong-model', 'wrong-binary', 'no-defaults'])
def test_legacy_direct_call_uses_frozen_mixed_role_and_provider_identity(tmp_path, monkeypatch, mode):
    args = fixture(tmp_path, 'raise AssertionError("boundary is simulated")\n')
    plan = args['plan'];shared = plan['shared_execution'];profile = shared.pop('profile')
    shared.update(schema='shared-sandbox/2', profiles={'codex':profile, 'zcode':profile})
    plan['execution_profile']['roles'] = {'generator':{'provider':'zcode','model':'GLM-5.3','effort':''},
        'judge':{'provider':'codex','model':'fixture','effort':'high'}}
    plan['provider_binary_sha256_by_provider'] = {'codex':plan.pop('provider_binary_sha256'),'zcode':'b'*64}
    calls = []
    def boundary(**request):
        calls.append(request)
        return {'stdout':'{"type":"turn.completed"}', 'stderr':'', 'native_complete':True, 'usage':None, 'cost_usd':None}
    monkeypatch.setattr('codex_guard.execute_shared', boundary)
    if mode in ['explicit','wrong-model']:args['args'][-1:-1] = ['--model','fixture' if mode=='explicit' else 'unplanned']
    elif mode=='wrong-binary':plan['provider_binary_sha256_by_provider']['codex'] = 'f'*64
    elif mode=='no-defaults':plan['supply_profile_defaults'] = False
    if mode in ['wrong-model','wrong-binary','no-defaults']:
        with pytest.raises(Refused):dispatch(**args)
        assert not args['ledger_path'].exists() and not calls
    else:
        receipt = dispatch(**args);assert receipt['native_complete']
        assert receipt['effective_args'][receipt['effective_args'].index('--model')+1] == 'fixture'
        assert receipt['supplied_args'] == args['args'] and len(calls) == 1


def test_unfrozen_forge_read_refuses_original_arm_before_reservation(tmp_path, monkeypatch):
    args = fixture(tmp_path, 'raise AssertionError("model must not run")\n')
    marker = tmp_path / 'unfrozen.json';marker.write_text('{}')
    monkeypatch.setenv('SHADOW_FORGE_REFUSAL', str(marker))
    with pytest.raises(Refused, match='unfrozen forge request'):
        dispatch(**args)
    assert not args['ledger_path'].exists()


def test_guard_retains_raw_usage_but_projects_canonical_multi_turn_accounting(tmp_path):
    args = fixture(tmp_path, 'import json\n'
                   'for u in [(10,2,3),(20,4,5)]:print(json.dumps({"type":"turn.completed","usage":{'
                   '"input_tokens":u[0],"output_tokens":u[1],"cached_input_tokens":u[2],"cache_write_input_tokens":0}}))\n')
    result = dispatch(**args)
    assert result['usage'] == {'input_tokens': 20, 'output_tokens': 4, 'cached_input_tokens': 5,
                               'cache_write_input_tokens': 0}
    assert result['accounting_usage'] == {'input_tokens': 30, 'output_tokens': 6,
        'cache_read_input_tokens': 8, 'cache_creation_input_tokens': 0, 'input_tokens_include_cache': True}
    assert result['cost_usd'] is None and dispatch(**args) == result


@pytest.mark.parametrize('events', [
    [{'type': 'error', 'message': 'native refused'}, {'type': 'turn.completed'}],
    [{'type': 'turn.failed'}, {'type': 'turn.completed'}],
    [{'type': 'turn.completed'}, {'type': 'turn.started'}],
])
def test_guard_uses_the_canonical_terminal_contract_and_keeps_original_pending(tmp_path, events):
    args = fixture(tmp_path, 'import json\nfor event in '+repr(events)+':print(json.dumps(event))\n')
    assert dispatch(**args)['native_complete'] is False
    with pytest.raises(Refused, match='unknown'):
        dispatch(**args)
    with sqlite3.connect(args['ledger_path']) as db:
        assert db.execute('SELECT status FROM calls').fetchall() == [('reserved',)]


@pytest.mark.parametrize("body", [
    'print("not a terminal reply")\n',
    'print(\'{"type":"turn.failed"}\')\n',
    'print(\'{"type":"turn.completed"}\'); raise SystemExit(1)\n',
])
def test_process_exit_does_not_prove_native_completion(tmp_path, body):
    args = fixture(tmp_path, body)
    dispatch(**args)
    with pytest.raises(Refused, match="unknown"):
        dispatch(**args)
    assert len(list(tmp_path.glob("*.partial.json"))) == 1
    with sqlite3.connect(args["ledger_path"]) as db:
        assert db.execute("SELECT status FROM calls").fetchall() == [("reserved",)]


def test_deadline_keeps_partial_output_and_pending_identity(tmp_path):
    args = fixture(tmp_path, 'import time\nprint("partial", flush=True)\ntime.sleep(30)\n')
    args["plan"]["native_timeout_s"] = 2
    result = dispatch(**args)
    assert result["reason"] == "absolute"
    assert "partial" in result["stdout"]
    with pytest.raises(Refused, match="unresolved"):
        dispatch(**{**args, "arm": "candidate"})


def test_changed_binary_refused_before_dispatch(tmp_path):
    args = fixture(tmp_path, 'print("unused")\n')
    args["binary"].write_text("changed")
    with pytest.raises(Refused, match="binary differs"):
        dispatch(**args)
    assert not args["ledger_path"].exists()


def test_direct_result_file_is_restored_on_replay_and_schema_is_bound(tmp_path):
    args = fixture(tmp_path, 'from pathlib import Path\n'
                   'Path("result.json").write_text("native result")\n'
                   'print(\'{"type":"item.completed","item":{"type":"agent_message","text":"native result"}}\')\n'
                   'print(\'{"type":"turn.completed"}\')\n')
    schema = tmp_path / "schema.json"
    schema.write_text('{"type":"object"}')
    args["args"][-1:-1] = ["--output-schema", str(schema), "--output-last-message", "result.json"]
    first = dispatch(**args)
    output = tmp_path / "result.json"
    output.unlink()
    assert dispatch(**args) == first
    assert output.read_text() == "native result"
    output.write_text("different")
    with pytest.raises(Refused, match="result file differs"):
        dispatch(**args)
    output.unlink()
    schema.write_text('{"type":"string"}')
    second = dispatch(**args)
    assert second["request_sha256"] != first["request_sha256"]


def test_executable_boundary_filters_credentials_and_reuses_receipt(tmp_path):
    args = fixture(tmp_path, 'import os, sys\n'
                   'assert "GH_TOKEN" not in os.environ\n'
                   'assert "OPENAI_API_KEY" not in os.environ\n'
                   'assert sys.stdin.read() == "frozen prompt"\n'
                   'print(\'{"type":"turn.completed","usage":null}\')\n')
    config = tmp_path / "config.json"
    config.write_text(json.dumps({key: str(args[key]) if isinstance(args[key], Path) else args[key]
                                 for key in ("ledger_path", "plan", "case_id", "arm", "binary")}))
    command = [sys.executable, str(bootstrap_guard(tmp_path / "launcher")),
               "--config", str(config), "--", "exec", "--json", "--sandbox", "read-only", "-"]
    for _ in range(2):
        result = subprocess.run(command, input="frozen prompt", text=True, capture_output=True,
                                cwd=tmp_path, timeout=10,
                                env={**os.environ, "GH_TOKEN": "fake", "OPENAI_API_KEY": "fake"})
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["type"] == "turn.completed"
    with sqlite3.connect(args["ledger_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM calls").fetchone() == (1,)


def live_fixture(tmp_path):
    args = fixture(tmp_path, 'print(\'{"type":"turn.completed"}\')\n')
    args['plan'].pop('supply_profile_defaults', None)
    args['plan'].update(execution_kind='live', execution_profile={
        'model': 'frozen-model', 'reasoning_effort': 'high',
        'config_overrides': ['model_reasoning_effort="high"']})
    args['args'] = ['exec', '--json', '--ignore-user-config', '--strict-config',
                    '--model', 'frozen-model', '--sandbox', 'read-only',
                    '-c', 'model_reasoning_effort="high"', '-']
    return args


def test_live_profile_is_enforced_and_same_profile_replays(tmp_path):
    args = live_fixture(tmp_path)
    receipt = dispatch(**args)
    assert receipt['native_complete']
    assert dispatch(**args) == receipt


@pytest.mark.parametrize('change', [
    'model', 'effort', 'sandbox', 'duplicate_model', 'profile', 'unknown',
    'user_config', 'no_profile', 'config_model', 'positional_prompt',
])
def test_profile_changes_refuse_before_reservation(tmp_path, change):
    args = live_fixture(tmp_path)
    argv = args['args']
    if change == 'model':
        argv[argv.index('frozen-model')] = 'other-model'
    elif change == 'effort':
        argv[argv.index('model_reasoning_effort="high"')] = 'model_reasoning_effort="low"'
    elif change == 'sandbox':
        argv[argv.index('read-only')] = 'danger-full-access'
    elif change == 'duplicate_model':
        argv[-1:-1] = ['-m', 'frozen-model']
    elif change == 'profile':
        argv[-1:-1] = ['--profile', 'unfrozen']
    elif change == 'unknown':
        argv[-1:-1] = ['--dangerously-bypass-approvals-and-sandbox']
    elif change == 'user_config':
        argv.remove('--ignore-user-config')
    elif change == 'no_profile':
        del args['plan']['execution_profile']
    elif change == 'config_model':
        argv[-1:-1] = ['-c', 'model="other-model"']
    else:
        argv[-1] = 'unfrozen positional prompt'
    with pytest.raises(Refused):
        dispatch(**args)
    assert not args['ledger_path'].exists()


@pytest.mark.parametrize('overrides', [
    ['model_reasoning_effort="low"'],
    ['model_reasoning_effort="high"', 'model="other-model"'],
    ['model_reasoning_effort="high"', 'model_reasoning_effort = "high"'],
    ['not valid TOML'],
])
def test_frozen_profile_cannot_hide_conflicting_overrides(tmp_path, overrides):
    args = live_fixture(tmp_path)
    args['plan']['execution_profile']['config_overrides'] = overrides
    with pytest.raises(Refused):
        dispatch(**args)
    assert not args['ledger_path'].exists()


def test_legacy_driver_gets_frozen_defaults_and_receipt_records_both_commands(tmp_path):
    args = live_fixture(tmp_path)
    args['plan']['supply_profile_defaults'] = True
    supplied = ['exec', '--json', '--sandbox', 'read-only', '-']
    args['args'] = supplied
    receipt = dispatch(**args)
    assert receipt['supplied_args'] == supplied
    assert '--model' in receipt['effective_args']
    assert 'frozen-model' in receipt['effective_args']
    assert 'model_reasoning_effort="high"' in receipt['effective_args']
    assert '--ignore-user-config' in receipt['effective_args']
    assert args['args'] == supplied
    assert dispatch(**args) == receipt


@pytest.mark.parametrize('option', [['--model','other-model'], ['-c','model_reasoning_effort="low"']])
def test_defaults_do_not_override_a_conflicting_caller(tmp_path, option):
    args = live_fixture(tmp_path)
    args['plan']['supply_profile_defaults'] = True
    args['args'] = ['exec','--json','--sandbox','read-only',*option,'-']
    with pytest.raises(Refused):
        dispatch(**args)
    assert not args['ledger_path'].exists()


def test_legacy_request_identity_still_replays_after_profile_support(tmp_path):
    from codex_guard import digest
    from ledger import Ledger
    args = fixture(tmp_path, 'raise AssertionError("must replay without dispatch")\n')
    args['plan'] = {key: args['plan'][key] for key in ('native_call_limits', 'provider_binary_sha256')}
    request = {'binary_sha256': args['plan']['provider_binary_sha256'],
               'args': args['args'], 'cwd': str(tmp_path.resolve()),
               'prompt': args['prompt'], 'environment_sha256': digest(args['env'])}
    request_digest = digest(request)
    call_id = digest({'case': args['case_id'], 'arm': args['arm'], 'request': request_digest})
    ledger = Ledger(args['ledger_path'], args['plan'])
    ledger.reserve(call_id, case_id=args['case_id'], arm=args['arm'], request_digest=request_digest)
    receipt = {'stdout': '', 'stderr': '', 'native_complete': True, 'usage': None, 'cost_usd': None}
    ledger.complete(call_id, receipt)
    ledger.close()
    assert dispatch(**args) == receipt


def bridge_fixture(tmp_path):
    args = live_fixture(tmp_path)
    args['plan']['supply_profile_defaults'] = True
    run_root=tmp_path/'runs'; bridge=run_root/'run-1/bridge'; bridge.mkdir(parents=True)
    workspace=tmp_path/'worktrees/repo'; workspace.mkdir(parents=True)
    spec=bridge/'review.json'
    data={'version':2,'repo':'toy','run_dir':str(bridge.parent),
          'trace_path':str(bridge.parent/'bridge_trace.jsonl'),'trace_store_root':'',
          'scope':{'name':'review','read_only':True,'allowed_tools':['read_file','grep','list_dir'],
                   'root':str(workspace),'path_scope':None,'read_roots':[],'executables':[]}}
    spec.write_text(json.dumps(data))
    args['plan']['execution_profile']['mcp_by_arm']={'baseline':{
        'python':sys.executable,'package_root':'/frozen/site-packages','run_root':str(run_root),
        'workspace_root':str(workspace.parent),'repository':'toy','allowed_tools':['read_file','grep','list_dir']}}
    config=[f'mcp_servers.infermatrix-tools.command={json.dumps(sys.executable)}',
            'mcp_servers.infermatrix-tools.args='+json.dumps(['-m','infermatrix_copilot.tool_bridge','--spec',str(spec)]),
            'mcp_servers.infermatrix-tools.env={PYTHONPATH="/frozen/site-packages"}']
    args['args']=['exec','--json','--sandbox','read-only',*[item for value in config for item in ('-c',value)],'-']
    return args,spec,data


def test_strict_bridge_scope_is_bound_and_missing_effort_is_supplied(tmp_path):
    args,spec,_=bridge_fixture(tmp_path)
    receipt=dispatch(**args)
    assert receipt['native_complete']
    assert receipt['bridge_spec']['sha256']==hashlib.sha256(spec.read_bytes()).hexdigest()
    assert 'model_reasoning_effort="high"' in receipt['effective_args']
    assert dispatch(**args)==receipt


@pytest.mark.parametrize('change',['write','tools','root','run','trace','extra_read','rebase','env'])
def test_strict_bridge_policy_drift_refuses_before_dispatch(tmp_path,change):
    args,spec,data=bridge_fixture(tmp_path)
    if change=='write': data['scope']['read_only']=False
    elif change=='tools': data['scope']['allowed_tools'].append('write_file')
    elif change=='root': data['scope']['root']=str(tmp_path)
    elif change=='run': data['run_dir']=str(tmp_path)
    elif change=='trace': data['trace_path']=str(tmp_path/'outside.jsonl')
    elif change=='extra_read': data['scope']['read_roots']=[str(tmp_path)]
    elif change=='rebase': data['rebase']={}
    else:
        args['args']=[v.replace('/frozen/site-packages','/unfrozen') for v in args['args']]
    spec.write_text(json.dumps(data))
    with pytest.raises(Refused): dispatch(**args)
    assert not args['ledger_path'].exists()
