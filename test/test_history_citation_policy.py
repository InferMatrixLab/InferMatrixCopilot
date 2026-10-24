"""Generation context and deterministic application share citation ownership."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json

import pytest

from infermatrix_copilot.kb_service.init_coverage import Owner, owner_for_paths
from infermatrix_copilot.kb_service.init_history_routes import CitationPolicy
from infermatrix_copilot.kb_service import init_history
from infermatrix_copilot.kb_service.init_stages import run_stage
from test_kb_init_history import _setup, _runtime, _modules_lifecycle, _payload
from test_kb_init_skeleton import _commit, world  # noqa: F401
from test_history_extraction_checkpoint import runtime


def test_nested_component_and_context_only_witness_share_the_gate():
    root = Owner('core', 'repos/r/rules.md', ('pkg/',))
    nested = Owner('hot', 'repos/r/hot/rules.md', ('pkg/hot/',))
    policy = CitationPolicy([root, nested], [])
    paths = ['pkg/a.py', 'pkg/hot/a.py', 'tests/test_a.py']
    assert policy.describe(paths)['component_evidence_owners'] == {
        'pkg/a.py': ['core'], 'pkg/hot/a.py': ['hot'], 'tests/test_a.py': []}
    assert policy.issue(root, 'preserve behavior', ['pkg/a.py']) is None
    assert policy.issue(root, 'preserve behavior', ['pkg/hot/a.py'])['unowned_paths'] == ['pkg/hot/a.py']
    assert policy.issue(root, 'preserve behavior', ['pkg/a.py', 'tests/test_a.py'])['unowned_paths'] == ['tests/test_a.py']


def test_collective_routing_ties_are_not_replaced_with_per_file_winners():
    first = Owner('first', 'repos/r/first/rules.md', ('pkg/a.py',))
    common = Owner('common', 'repos/r/common/rules.md', ('pkg/a.py', 'pkg/b.py'))
    last = Owner('last', 'repos/r/last/rules.md', ('pkg/b.py',))
    policy = CitationPolicy([first, common, last], [])
    assert policy.describe(['pkg/a.py', 'pkg/b.py'])['component_evidence_owners'] == {
        'pkg/a.py': ['first', 'common'], 'pkg/b.py': ['common', 'last']}
    assert owner_for_paths(['pkg/a.py'], policy.owners) == first
    assert owner_for_paths(['pkg/a.py', 'pkg/b.py'], policy.owners) == common
    assert policy.issue(common, 'combined contract', ['pkg/a.py', 'pkg/b.py']) is None
    assert policy.issue(common, 'single contract', ['pkg/a.py'])['selected_owner'] == 'first'


def test_model_scope_keeps_its_distinct_witness_policy():
    core = Owner('core', 'repos/r/rules.md', ('pkg/',))
    model = Owner('model:quartz-decoder', 'repos/r/models/quartz-decoder/rules.md', ())
    models = [{'owner': model.owner, 'model_name': 'quartz-decoder'}]
    policy = CitationPolicy([core], models)
    paths = ['pkg/quartz_decoder.py', 'tests/test_quartz_decoder.py']
    assert policy.describe(paths)['component_evidence_owners']['tests/test_quartz_decoder.py'] == []
    assert policy.issue(model, 'QuartzDecoder cache contract', paths) is None
    assert policy.issue(core, 'QuartzDecoder cache contract', paths)['kind'] == 'model_scope'
    assert policy.issue(Owner('model:missing', 'missing.md', ()), 'missing model', paths)['kind'] == 'model_scope'


@pytest.mark.parametrize('include_context_only_citation', [False, True])
def test_stage_exposes_context_only_paths_and_still_rejects_their_citation(world, include_context_only_citation):
    _commit(world['upstream'], {'tests/test_external.py': 'def test_external():\n    assert True\n'})
    gateway, source = _setup(world)
    original_read = source.history_evidence
    def evidence(repo, number):
        row = original_read(repo, number)
        if number == 101:
            row['files'].append({'filename': 'tests/test_external.py', 'patch': '+def test_external()'})
        return row
    source.history_evidence = evidence
    original_call = gateway.call_json
    packets = []
    def call(role, *, system, prompt, **request):
        reply = original_call(role, system=system, prompt=prompt, **request)
        if system == init_history.SYSTEM_HISTORY:
            packet = _payload(prompt); packets.append(packet)
            if packet['pr']['number'] == 101 and include_context_only_citation:
                data = deepcopy(reply.data)
                data['rules'][0]['evidence'].append({'path': 'tests/test_external.py', 'start': 1, 'end': 2})
                reply = replace(reply, data=data, text=json.dumps(data))
        return reply
    gateway.call_json = call
    result = run_stage(_runtime(world, gateway, github=source), _modules_lifecycle(), 'pr-history', dry_run=True)
    assert result.status == 'dry_run', result.problems
    packet = next(p for p in packets if p['pr']['number'] == 101)
    assert any(p['path'] == 'tests/test_external.py' for p in packet['current_source'])
    assert packet['citation_policy']['component_evidence_owners']['tests/test_external.py'] == []
    assert packet['citation_policy']['component_evidence_owners']['pkg/core.py'] == ['core']
    completed = next(p for p in result.history['completed'] if p['number'] == 101)
    assert completed['status'] == ('no_upgrade' if include_context_only_citation else 'upgraded')
    if include_context_only_citation:
        drop = next(p for p in result.dropped if p['rule_id'] == 'upstream PR #101')
        assert drop['citation_issue']['kind'] == 'component_scope'
        assert drop['citation_issue']['unowned_paths'] == ['tests/test_external.py']
    assert gateway.extract_calls == [101, 102, 103]


def test_version_one_unknown_retains_its_exact_prompt_and_no_new_descriptor(world, monkeypatch):
    rt, calls, store = runtime(world)
    with monkeypatch.context() as patch:
        patch.setattr(init_history, 'HISTORY_SCOPE_POLICY_VERSION', 1)
        first = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    states = deepcopy(first.coverage['generation_execution'])
    request = states['history:101']['generation_dispatched_binding']['inputs']
    assert request['system_sha256'] == '60d1993c6dfa58e7dd6c5de961aa05b1ac6e278d0d7d8e04e7b00ee8b37760bb'
    record = next(store.iter_records(kind='model_call'))[0]
    packet = _payload(store.blob(record['inputs']['prompt']))
    assert 'citation_policy' not in packet
    again = run_stage(rt, _modules_lifecycle(), 'pr-history', dry_run=True)
    assert again.status == 'blocked' and calls == [101]
    assert again.history['scope_policy_version'] == 1
    assert again.coverage['generation_execution'] == states


def test_legacy_prompts_and_aggregate_remain_byte_exact():
    assert hashlib.sha256(init_history.SYSTEM_HISTORY_V1.encode()).hexdigest() == '60d1993c6dfa58e7dd6c5de961aa05b1ac6e278d0d7d8e04e7b00ee8b37760bb'
    assert hashlib.sha256(init_history.SYSTEM_HISTORY_LEGACY.encode()).hexdigest() == '64ae0ce04c64fc68a843904bb853406c22c5c682a425059c144373d898836c8d'
    assert hashlib.sha256(init_history.SYSTEM_REVIEW.encode()).hexdigest() == '70df5bf06a2281a49e5fd904cfc17841f672892f4ed93cae6d43ba5ae655ccda'
    for version in (2, 3):
        assert hashlib.sha256(init_history.history_system(version).encode()).hexdigest() == 'd747a77114a6f8e97262b94153d20202947f8d69342659ab2439e4753d709f11'


@pytest.mark.parametrize('version', [2, 3, 4])
def test_history_renders_checked_sources_only_under_the_new_output_policy(world, monkeypatch, version):
    from test_kb_init_history import _tree
    monkeypatch.setattr(init_history, 'HISTORY_SCOPE_POLICY_VERSION', version)
    gateway, source = _setup(world)
    original_call = gateway.call_json
    aggregate = []
    def call(role, *, system, prompt, **request):
        reply = original_call(role, system=system, prompt=prompt, **request)
        if system == init_history.history_system(version) and _payload(prompt)['pr']['number'] == 101:
            data = deepcopy(reply.data)
            for field in ['title', 'trigger', 'must', 'forbid', 'acceptance']:
                data['rules'][0][field] = data['rules'][0][field].replace('`', '')
            reply = replace(reply, data=data, text=json.dumps(data))
        elif system == init_history.SYSTEM_REVIEW:
            aggregate.append(_payload(prompt))
        return reply
    gateway.call_json = call
    result = run_stage(_runtime(world, gateway, github=source), _modules_lifecycle(), 'pr-history', dry_run=True)
    assert result.status == 'dry_run', result.problems
    rid = next(c['rule_ids'][0] for c in result.history['commits'] if c['number'] == 101)
    page = _tree(result)['knowledge/repos/toy/rules.md']
    row = next(l for l in page.splitlines() if '| ' + rid + ' |' in l)
    if version >= 3:
        assert row.endswith('| `pkg/core.py` |')
        assert '- 源码： `pkg/core.py`' in page
        assert '- 源码： `pkg/core.py`' in aggregate[0]['complete_diff']
    else:
        assert row.endswith('| — |')
        from infermatrix_copilot.knowledge_service.lifecycle import Page
        assert '- 源码：' not in Page.parse(page).rule(rid).text
    assert gateway.extract_calls == [101, 102, 103]
