"""Recover the original aggregate judgment, never a replacement paid review."""
import hashlib
import json
import os
import subprocess
import sys

import pytest

from infermatrix_copilot.kb_service.init_support import InitRecord
from infermatrix_copilot.trace_store import TraceStore

SCRIPT = '''
import hashlib, json, os, sys
from pathlib import Path
from types import SimpleNamespace
from infermatrix_copilot.config import Settings
from infermatrix_copilot.kb_service.init_support import InitRecord, checkpoint_budget
from infermatrix_copilot.kb_service.init_history import SYSTEM_REVIEW, _validate_review, aggregate_review_result
from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole
from infermatrix_copilot.kb_service.quality_review import QualityPolicy, review_call, checkpoint_review
from infermatrix_copilot.kb_service.runtime import trace_recorder
from infermatrix_copilot.llm import Reply, Block
from infermatrix_copilot.trace_store import TraceStore, NativeCallArchive
root = Path(sys.argv[1])
record = InitRecord(stage='pr-history', repo='demo')
budget = checkpoint_budget(1, record, root)
store = TraceStore(root/'init/traces', environ={})
role = ModelRole('pr-reviewer', 'codex', 'fixture')
binding = {'base_sha': 'a'*40, 'head_sha': 'b'*40, 'diff_sha256': hashlib.sha256(b'frozen diff').hexdigest(),
           'requested': role.label()}
original_finish = NativeCallArchive.finish
def finish(self, *args, **kwargs):
    original_finish(self, *args, **kwargs)
    os._exit(75)
NativeCallArchive.finish = finish
def complete(**kwargs):
    with (root/'dispatches').open('a') as handle:
        handle.write('dispatch\\n')
    data = {'verdict': sys.argv[2], 'findings': [] if sys.argv[2] == 'approve' else ['fix this'], 'summary': 'original'}
    return Reply(blocks=[Block(type='text', text=json.dumps(data))], stop_reason='end_turn', usage={'cost_usd': .1})
gateway = ModelGateway(Settings(_env_file=None), recorder=trace_recorder(store),
                       transport_factory=lambda _: SimpleNamespace(complete=complete))
def run():
    reply = review_call(gateway, role, policy=QualityPolicy('pr-history-aggregate'),
        system=SYSTEM_REVIEW, prompt='frozen evidence', validate=_validate_review, budget=budget, charge_usd=.5)
    return aggregate_review_result(reply)
checkpoint_review(record.history.setdefault('aggregate_quality', {}), run, lambda: record.save(root), binding=binding)
'''


def interrupted(root, verdict='approve'):
    proc = subprocess.run([sys.executable, '-c', SCRIPT, str(root), verdict], env=os.environ.copy(),
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 75, proc.stderr
    store = TraceStore(root/'init/traces')
    (trace, _), = list(store.iter_records())
    return store, trace


def test_auth_control_does_not_duplicate_original_quality_checkpoint(tmp_path):
    from infermatrix_copilot.kb_service.init_recovery import reconcile_review
    store, trace = interrupted(tmp_path)
    store.append('auth_probe', context=trace['context'], result={'model_calls': 0})
    logs = {p: p.read_bytes() for p in (store.root/'records').glob('*.jsonl')}
    for _ in range(2):
        result = reconcile_review(tmp_path, repo='demo', stage='pr-history',
                                  trace_id=trace['id'], offline_confirmed=True)
        assert result['status'] == 'reconciled'
    record = InitRecord.load(tmp_path, 'demo', 'pr-history')
    assert record.review['verdict'] == 'approve' and record.spent_usd == .5
    assert not record.history['aggregate_quality']['review_dispatched']
    assert (tmp_path/'dispatches').read_text() == 'dispatch\n'
    assert all(p.read_bytes() == value for p, value in logs.items())


def command(root, trace, stage='pr-history'):
    return subprocess.run([sys.executable, '-c',
        'import sys; from infermatrix_copilot.kb_service.cli import main; sys.exit(main(sys.argv[1:]))',
        '--state-dir', str(root), 'reconcile-init-review', '--repo', 'demo', '--stage', stage,
        '--record', trace['id'], '--offline-confirmed'], env=os.environ.copy(), capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize('verdict', ['approve', 'request_changes'])
def test_original_aggregate_result_is_recovered_without_index_dispatch_or_rebilling(tmp_path, verdict):
    store, trace = interrupted(tmp_path, verdict)
    store.index_path.unlink()
    result = command(tmp_path, trace)
    assert result.returncode == 0, result.stderr
    record = InitRecord.load(tmp_path, 'demo', 'pr-history')
    state = record.history['aggregate_quality']
    assert not state['review_dispatched'] and state['review_attempts'] == 1
    assert state['checked'] == record.review
    assert record.review['verdict'] == verdict and record.review['head_sha'] == 'b' * 40
    assert record.review['quality_receipt']['native']['trace_id'] == trace['id']
    assert record.spent_usd == .5
    assert not store.index_path.exists()
    path = InitRecord.path(tmp_path, 'demo', 'pr-history'); before = path.read_bytes()
    again = command(tmp_path, trace)
    assert again.returncode == 0, again.stderr
    assert path.read_bytes() == before and (tmp_path/'dispatches').read_text() == 'dispatch\n'


@pytest.mark.parametrize('damage', ['runtime', 'legacy_runtime', 'binding', 'duplicate', 'policy'])
def test_unproven_original_review_cannot_change_checkpoint_or_account(tmp_path, damage):
    store, trace = interrupted(tmp_path)
    if damage in ('runtime', 'legacy_runtime', 'policy'):
        archive, = (store.root/'attempts').glob('*/attempt.json')
        native = json.loads(archive.read_text())
        if damage == 'runtime':
            trace['env']['runtime_package_sha256'] = native['env']['runtime_package_sha256'] = '0' * 64
        elif damage == 'legacy_runtime':
            trace['env'].pop('runtime_package_sha256'); native['env'].pop('runtime_package_sha256')
        else:
            trace['context']['quality_review']['policy'] = native['context']['quality_review']['policy'] = 'other-policy'
        archive.write_text(json.dumps(native))
        journal, = (store.root/'records').glob('*.jsonl'); journal.write_text(json.dumps(trace)+'\n')
    elif damage == 'binding':
        record = InitRecord.load(tmp_path, 'demo', 'pr-history')
        retained = record.history['aggregate_quality']['review_dispatched_binding']
        retained['inputs']['head_sha'] = 'c' * 40
        retained['sha256'] = hashlib.sha256(json.dumps(retained['inputs'], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        record.save(tmp_path)
    else:
        journal, = (store.root/'records').glob('*.jsonl')
        with journal.open('a') as handle:
            handle.write(json.dumps(trace)+'\n')
    path = InitRecord.path(tmp_path, 'demo', 'pr-history'); before = path.read_bytes()
    result = command(tmp_path, trace)
    assert result.returncode == 1, result.stdout
    assert path.read_bytes() == before and (tmp_path/'dispatches').read_text() == 'dispatch\n'


def test_crash_between_budget_settlement_and_review_checkpoint_is_recoverable(tmp_path, monkeypatch):
    from infermatrix_copilot.kb_service.init_recovery import reconcile_review
    _, trace = interrupted(tmp_path)
    original_save = InitRecord.save
    calls = []
    def save(record, *args, **kwargs):
        calls.append(record.spent_usd)
        if len(calls) == 2:
            raise OSError('review checkpoint unavailable')
        return original_save(record, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(InitRecord, 'save', save)
        with pytest.raises(OSError, match='review checkpoint unavailable'):
            reconcile_review(tmp_path, repo='demo', stage='pr-history', trace_id=trace['id'], offline_confirmed=True)
    pending = InitRecord.load(tmp_path, 'demo', 'pr-history')
    assert pending.history['aggregate_quality']['review_dispatched']
    assert pending.spent_usd == .5
    result = command(tmp_path, trace)
    assert result.returncode == 0, result.stderr
    assert InitRecord.load(tmp_path, 'demo', 'pr-history').review['verdict'] == 'approve'
    assert (tmp_path/'dispatches').read_text() == 'dispatch\n'

DEPTH_SCRIPT = '''
import json, os, sys
from pathlib import Path
from types import SimpleNamespace
from infermatrix_copilot.config import Settings
from infermatrix_copilot.kb_service.init_support import InitRecord, checkpoint_budget
from infermatrix_copilot.kb_service.depth_judge import facet_review_binding, review_facets
from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole
from infermatrix_copilot.kb_service.quality_review import checkpoint_review
from infermatrix_copilot.kb_service.runtime import trace_recorder
from infermatrix_copilot.llm import Reply, Block
from infermatrix_copilot.trace_store import TraceStore, NativeCallArchive
root, mode, packet = Path(sys.argv[1]), sys.argv[2], json.loads(sys.argv[3])
record = InitRecord(stage='knowledge-deepen', repo='demo')
budget = checkpoint_budget(1, record, root)
store = TraceStore(root/'init/traces', environ={})
role = ModelRole('judge', 'codex', 'fixture')
blocks = {facet: '<!-- kb:depth-proof ' + json.dumps({'acceptance_mode': mode}) + ' -->\\n' + facet
          for facet in ('api', 'flow')}
binding = facet_review_binding('a'*64, blocks, role)
original_finish = NativeCallArchive.finish
def finish(self, *args, **kwargs):
    original_finish(self, *args, **kwargs)
    os._exit(75)
NativeCallArchive.finish = finish
def complete(**kwargs):
    with (root/'dispatches').open('a') as handle:
        handle.write('dispatch\\n')
    return Reply(blocks=[Block(type='text', text=json.dumps(packet))], stop_reason='end_turn', usage={'cost_usd': .1})
gateway = ModelGateway(Settings(_env_file=None), recorder=trace_recorder(store),
                       transport_factory=lambda _: SimpleNamespace(complete=complete))
rt = SimpleNamespace(gateway=gateway, judge=role)
def run():
    return review_facets(rt, budget, SimpleNamespace(judge_call_usd=.5), feature='feature', pin='b'*40,
                         blocks=blocks, existing={}, evidence=[])
state = record.depth.setdefault('features', {}).setdefault('feature', {})
checkpoint_review(state, run, lambda: record.save(root), binding=binding)
'''


def interrupted_depth(root, mode, packet):
    proc = subprocess.run([sys.executable, '-c', DEPTH_SCRIPT, str(root), mode, json.dumps(packet)],
                          env=os.environ.copy(), capture_output=True, text=True, timeout=30)
    assert proc.returncode == 75, proc.stderr
    store = TraceStore(root/'init/traces')
    (trace, _), = list(store.iter_records())
    return store, trace


def facet(dim='yes'):
    return {'dimensions': {'faithful': dim, 'non_contradictory': 'yes', 'does_not_weaken': 'yes'},
            'reason': 'original evidence'}


@pytest.mark.parametrize('mode,packet,verdicts', [
    ('strict', {'facets': {'api': facet(), 'flow': facet('no')}}, {'api': 'pass', 'flow': 'fail'}),
    ('lightweight', {'facets': {'api': facet('unsure')}}, {'api': 'unsure', 'flow': 'unjudged'}),
])
def test_depth_recovery_preserves_stage_acceptance_and_original_receipt(tmp_path, mode, packet, verdicts):
    from infermatrix_copilot.kb_service.init_recovery import reconcile_review
    store, trace = interrupted_depth(tmp_path, mode, packet)
    store.index_path.unlink()
    result = command(tmp_path, trace, stage='knowledge-deepen')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['facets'] == verdicts
    record = InitRecord.load(tmp_path, 'demo', 'knowledge-deepen')
    state = record.depth['features']['feature']
    assert state['review_attempts'] == 1 and not state['review_dispatched']
    assert state['checked']['native_trace_id'] == trace['id']
    assert state['checked']['acceptance_modes'] == {'api': mode, 'flow': mode}
    assert not record.review and record.spent_usd == .5
    assert not store.index_path.exists()
    path = InitRecord.path(tmp_path, 'demo', 'knowledge-deepen')
    before = path.read_bytes()
    reconcile_review(tmp_path, repo='demo', stage='knowledge-deepen', trace_id=trace['id'], offline_confirmed=True)
    assert path.read_bytes() == before and (tmp_path/'dispatches').read_text() == 'dispatch\n'


@pytest.mark.parametrize('damage', ['strict_partial', 'binding', 'ambiguous', 'consumed'])
def test_depth_recovery_refuses_unproven_or_consumed_packet(tmp_path, damage):
    from copy import deepcopy
    packet = {'facets': {'api': facet(), 'flow': facet()}}
    if damage == 'strict_partial':
        packet['facets'].pop('flow')
    _, trace = interrupted_depth(tmp_path, 'strict', packet)
    record = InitRecord.load(tmp_path, 'demo', 'knowledge-deepen')
    state = record.depth['features']['feature']
    if damage == 'binding':
        state['review_dispatched_binding']['inputs']['acceptance_modes']['api'] = 'lightweight'
    elif damage == 'ambiguous':
        record.depth['features']['other'] = deepcopy(state)
    elif damage == 'consumed':
        state['review_dispatched'] = False
    record.save(tmp_path)
    path = InitRecord.path(tmp_path, 'demo', 'knowledge-deepen'); before = path.read_bytes()
    result = command(tmp_path, trace, stage='knowledge-deepen')
    assert result.returncode == 1, result.stdout
    assert path.read_bytes() == before and (tmp_path/'dispatches').read_text() == 'dispatch\n'

BLOCK_SCRIPT = '''
import json, os, sys
from pathlib import Path
from types import SimpleNamespace
from infermatrix_copilot.config import Settings
from infermatrix_copilot.kb_service.init_support import InitRecord, checkpoint_budget, judge
from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole
from infermatrix_copilot.kb_service.runtime import trace_recorder
from infermatrix_copilot.knowledge_service.l1 import Block as KnowledgeBlock
from infermatrix_copilot.llm import Reply, Block
from infermatrix_copilot.trace_store import TraceStore, NativeCallArchive
root, stage, answer = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
record = InitRecord(stage=stage, repo='demo')
budget = checkpoint_budget(1, record, root)
store = TraceStore(root/'init/traces', environ={})
original_finish = NativeCallArchive.finish
def finish(self, *args, **kwargs):
    original_finish(self, *args, **kwargs)
    os._exit(75)
NativeCallArchive.finish = finish
def complete(**kwargs):
    with (root/'dispatches').open('a') as handle:
        handle.write('dispatch\\n')
    data = {'dimensions': {'faithful': answer, 'does_not_weaken': 'yes', 'non_contradictory': 'yes'},
            'reasons': {'faithful': 'original judgment'}}
    return Reply(blocks=[Block(type='text', text=json.dumps(data))], stop_reason='end_turn', usage={'cost_usd': .1})
gateway = ModelGateway(Settings(_env_file=None), recorder=trace_recorder(store),
                       transport_factory=lambda _: SimpleNamespace(complete=complete))
rt = SimpleNamespace(gateway=gateway, judge=ModelRole('judge', 'codex', 'fixture'), unlimited_subscription=False)
judge(rt, budget, SimpleNamespace(judge_call_usd=.5), KnowledgeBlock('prose', 'page.md', '', 'prose', 'a'*64),
      base={}, head={'page.md': 'original text'}, evidence=[], record=record)
'''


@pytest.mark.parametrize('stage,answer,verdict', [('modules', 'yes', 'pass'), ('deepen', 'no', 'fail'),
                                                ('pr-history', 'unsure', 'human')])
def test_original_init_block_result_recovers_and_resumes_without_dispatch(tmp_path, stage, answer, verdict):
    from types import SimpleNamespace
    from infermatrix_copilot.kb_service.init_support import checkpoint_budget, judge
    from infermatrix_copilot.kb_service.models import ModelRole
    from infermatrix_copilot.knowledge_service.l1 import Block
    proc = subprocess.run([sys.executable, '-c', BLOCK_SCRIPT, str(tmp_path), stage, answer],
                          env=os.environ.copy(), capture_output=True, text=True, timeout=30)
    assert proc.returncode == 75, proc.stderr
    store = TraceStore(tmp_path/'init/traces')
    (trace, _), = list(store.iter_records())
    result = command(tmp_path, trace, stage=stage)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['verdict'] == verdict
    record = InitRecord.load(tmp_path, 'demo', stage)
    assert not record.review  # A block review is not the aggregate publication review.
    state, = record.coverage['quality_reviews'].values()
    assert state['checked']['quality_receipt']['native']['trace_id'] == trace['id']
    assert not state['review_dispatched'] and state['review_attempts'] == 1
    assert record.spent_usd == .5
    budget = checkpoint_budget(1, record, tmp_path)
    rt = SimpleNamespace(judge=ModelRole('judge', 'codex', 'fixture'), unlimited_subscription=False)
    # No gateway is provided: normal stage resume must use the recovered result.
    resumed = judge(rt, budget, SimpleNamespace(judge_call_usd=.5), Block('prose', 'page.md', '', 'prose', 'a'*64),
                    base={}, head={'page.md': 'original text'}, evidence=[], record=record)
    assert resumed.verdict == verdict and resumed.reasons == {'faithful': 'original judgment'}
    path = InitRecord.path(tmp_path, 'demo', stage); before = path.read_bytes()
    result = command(tmp_path, trace, stage=stage)
    assert result.returncode == 0, result.stderr
    assert path.read_bytes() == before and (tmp_path/'dispatches').read_text() == 'dispatch\n'


def test_malformed_original_block_judgment_stays_pending(tmp_path):
    proc = subprocess.run([sys.executable, '-c', BLOCK_SCRIPT, str(tmp_path), 'modules', 'invalid'],
                          env=os.environ.copy(), capture_output=True, text=True, timeout=30)
    assert proc.returncode == 75, proc.stderr
    store = TraceStore(tmp_path/'init/traces')
    (trace, _), = list(store.iter_records())
    path = InitRecord.path(tmp_path, 'demo', 'modules'); before = path.read_bytes()
    result = command(tmp_path, trace, stage='modules')
    # The original gateway already rejected this schema, so archive validation
    # refuses it before recovery can interpret a successful judgment.
    assert result.returncode == 1 and 'native attempt is not a successful' in result.stderr
    assert path.read_bytes() == before and (tmp_path/'dispatches').read_text() == 'dispatch\n'


def test_completed_legacy_block_checkpoint_reuses_original_digest(tmp_path):
    from types import SimpleNamespace
    from infermatrix_copilot.kb_service.init_support import checkpoint_budget, judge, inputs_digest
    from infermatrix_copilot.kb_service.models import ModelRole
    from infermatrix_copilot.knowledge_service.l1 import Block
    from infermatrix_copilot.kb_service.quality_review import checkpoint_review
    record = InitRecord(stage='modules', repo='demo')
    budget = checkpoint_budget(1, record, tmp_path)
    block = Block('prose', 'page.md', '', 'prose', 'a'*64)
    role = ModelRole('judge', 'codex', 'fixture')
    identity = inputs_digest(block=block.to_dict(), base={}, head={'page.md': 'old text'}, evidence=[],
                             judge=role.label(), policy='init-block-v1', subscription=False)
    state = record.coverage.setdefault('quality_reviews', {}).setdefault(identity, {})
    checkpoint_review(state, lambda: {**block.to_dict(), 'verdict': 'human',
                                     'reasons': {'faithful': 'original uncertainty'}},
                      lambda: record.save(tmp_path), binding={'input_sha256': identity})
    path = InitRecord.path(tmp_path, 'demo', 'modules'); before = path.read_bytes()
    verdict = judge(SimpleNamespace(judge=role, unlimited_subscription=False), budget,
                    SimpleNamespace(judge_call_usd=.5), block, base={}, head={'page.md': 'old text'},
                    evidence=[], record=record)
    assert verdict.verdict == 'human' and verdict.reasons == {'faithful': 'original uncertainty'}
    assert path.read_bytes() == before and state['review_attempts'] == 1 and budget.spent_usd == 0
