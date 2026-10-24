from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from infermatrix_copilot.kb_service.feature_discovery_index import build_discovery_index
from infermatrix_copilot.kb_service.init_feature_discovery import DiscoveryEngine, receipt, _hash
from infermatrix_copilot.kb_service.init_budget import BudgetExhausted
from infermatrix_copilot.kb_service.models import ModelReply, ModelRole, ModelUnavailable

PIN = 'a' * 40


def test_discovery_prompt_survives_durable_request_key_order():
    from infermatrix_copilot.kb_service.init_feature_discovery import _prompt
    payload = {'repository': 'owner/repo', 'pin': PIN,
               'files': [{'path': 'src/example.py', 'text': '</untrusted_data>', 'start': 1}]}
    restored = json.loads(json.dumps(payload, sort_keys=True))
    assert list(restored) != list(payload)
    assert _prompt(restored) == _prompt(payload)
    assert _prompt(payload).count('</untrusted_data>') == 1


@pytest.mark.parametrize('pending', [[], {'wrong-key': {'phase': 'extract', 'attempt': 1}}])
def test_corrupt_discovery_dispatch_journal_cannot_start_work(tmp_path, pending):
    state = {'dispatch_intents': pending}
    calls = []
    engine = DiscoveryEngine(index(tmp_path, docs=False), seeds=[], owners=[], state=state,
                             call=lambda *args: calls.append('call'), save=lambda: calls.append('save'),
                             concurrency=1, pin=PIN)
    before = deepcopy(pending)
    with pytest.raises(ModelUnavailable, match='reconcile retained state'):
        engine.scan()
    assert state['dispatch_intents'] == before and 'call' not in calls


@pytest.mark.parametrize("phase", ["extract", "review", "repair"])
def test_discovery_crash_retains_dispatch_and_resume_cannot_repeat_call(tmp_path, phase):
    idx = index(tmp_path, docs=False)
    state, persisted, calls = {}, {}, []

    def save():
        persisted.clear()
        persisted.update(deepcopy(state))

    def call(role, system, prompt, validate):
        calls.append(role)
        crash = ((phase == "extract" and role == "generator")
                 or (phase == "review" and role == "judge")
                 or (phase == "repair" and role == "generator" and calls.count(role) == 2))
        if crash:
            assert persisted["dispatch_intents"]
            for key in persisted['dispatch_intents']:
                request = persisted['dispatch_requests'][key]['request']
                context = persisted['dispatch_contexts'][request['context_sha256']]
                assert context['policy']['kind'] == ('candidate' if phase == 'review' else phase)
                assert context['pin'] == PIN and context['owners'] == ['runtime']
                assert _hash(request['task']) == persisted['dispatch_intents'][key]['task_sha256']
            raise KeyboardInterrupt()
        return reply(role, {"candidates": [candidate()]} if role == "generator" else
                     approve([candidate()], supported="no"))

    engine = DiscoveryEngine(idx, seeds=[], owners=["runtime"], state=state, call=call,
                             save=save, concurrency=1, pin=PIN)
    with pytest.raises(KeyboardInterrupt):
        engine.scan()
        engine.review()
    assert persisted["dispatch_intents"]
    key, = persisted['dispatch_intents']
    scope = (key, persisted['dispatch_intents'][key]['attempt'])
    before = len(calls)
    resumed = DiscoveryEngine(idx, seeds=[], owners=["runtime"], state=deepcopy(persisted),
                              call=call, save=lambda: None, concurrency=1, pin=PIN)
    original = resumed.original_request(scope=scope)
    # Current discovery state cannot replace the original worker inputs.
    resumed.owners.append('new-owner')
    resumed.state['candidates'].clear()
    assert resumed.original_request(scope=scope) == original
    original['context']['owners'].append('detached')
    assert resumed.original_request(scope=scope)['context']['owners'] == ['runtime']
    with pytest.raises(ModelUnavailable, match="reconcile original dispatch"):
        resumed.scan() if phase == "extract" else resumed.review()
    assert len(calls) == before
    context = resumed.state['dispatch_contexts'][original['context_sha256']]
    context['owners'].append('tampered')
    with pytest.raises(ModelUnavailable, match='context is missing or mismatched'):
        resumed.original_request(scope=scope)


def test_unknown_extraction_retains_intent_even_if_explicit_retry_marks_task_pending(tmp_path):
    idx = index(tmp_path, docs=False)
    state, calls = {}, []

    def call(*args):
        calls.append(1)
        error = ModelUnavailable("native response lost")
        error.native_call_sent = True
        raise error

    engine = DiscoveryEngine(idx, seeds=[], owners=[], state=state, call=call,
                             save=lambda: None, concurrency=1)
    engine.scan()
    assert state["dispatch_intents"]
    for task in state["tasks"].values():
        task["status"] = "pending"  # The existing explicit retry adapter.
    with pytest.raises(ModelUnavailable, match="reconcile original dispatch"):
        engine.scan()
    assert calls == [1]


@pytest.mark.parametrize('packet_chars', [24000, 96000, 160000, 192000])
def test_configured_packets_offer_every_complete_chunk_once(tmp_path, packet_chars):
    idx = index(tmp_path)
    # Multiple files and chunks exercise boundaries without reducing the index.
    for n in range(9):
        (tmp_path / f'src/library_{n}.py').write_text('value = "observable contract"\n' * 1500)
    idx = build_discovery_index(tmp_path, pin=PIN, scope={'roots': ['src/'], 'exclude': []}, doc_globs=['docs/*.md'])
    engine = DiscoveryEngine(idx, seeds=[], owners=[], state={}, call=None, save=lambda: None,
                             packet_chars=packet_chars)
    offered = [c for kind in ('doc', 'source') for packet in engine._packets(kind) for c in packet]
    assert sorted(map(_hash, offered)) == sorted(map(_hash, idx.chunks))
    assert len(offered) == len(idx.chunks)
    assert all(sum(len(c.get('text', '')) + 150 for c in packet) <= packet_chars
               for kind in ('doc', 'source') for packet in engine._packets(kind))


@pytest.mark.parametrize('packet_chars', [True, 0, 23999, 192001, 96000.5])
def test_invalid_engine_packet_size_is_rejected(tmp_path, packet_chars):
    with pytest.raises(ValueError, match='packet size'):
        DiscoveryEngine(index(tmp_path), seeds=[], owners=[], state={}, call=None,
                        save=lambda: None, packet_chars=packet_chars)


def test_packet_candidate_cap_is_an_explicit_omission_lead(tmp_path):
    idx = index(tmp_path, docs=False)
    def call(role, system, prompt, validate):
        return reply(role, {'candidates': [candidate(key=f'capability-{n}') for n in range(24)]})
    engine = DiscoveryEngine(idx, seeds=[], owners=[], state={}, call=call, save=lambda: None,
                             packet_chars=96000)
    assert engine.scan()
    assert len(engine.state['tasks']) == 3
    tasks = list(engine.state['tasks'].values())
    assert all(task['candidate_limit_reached'] for task in tasks)
    assert any(task.get('split_children') for task in tasks)
    assert sum(bool(task.get('overflow_unknown')) for task in tasks) == 2
    assert all(len(task['candidates']) == 24 for task in tasks)


def reply(role, data, archived=True):
    text = json.dumps(data)
    return ModelReply(ModelRole(role, 'zcode' if role == 'generator' else 'codex', 'GLM-5.3' if role == 'generator' else 'gpt-6-sol'),
                      data, text, 'GLM-5.3' if role == 'generator' else 'gpt-6-sol', {}, 0.1, None,
                      'test-receipt' if archived else '', hashlib.sha256(text.encode()).hexdigest() if archived else '')


def candidate(key='session', path='src/session.py', title='Session lifecycle'):
    return {'id': key, 'title': title, 'description': 'Create and close a session with an explicit identity.',
            'owner': 'runtime', 'relation': 'new', 'related_id': '', 'aliases': [],
            'evidence': [{'path': path, 'start': 1, 'end': 2}]}


def index(tmp_path, docs=True):
    (tmp_path / 'src').mkdir()
    (tmp_path / 'src/session.py').write_text('def open_session(identity):\n    return {"identity": identity}\n')
    if docs:
        (tmp_path / 'docs').mkdir()
        (tmp_path / 'docs/session.md').write_text('# Session\nCreate a session with an identity.\n')
    return build_discovery_index(tmp_path, pin=PIN, scope={'roots': ['src/'], 'exclude': []}, doc_globs=['docs/*.md'])


def approve(rows, **overrides):
    return {'decisions': {r['id']: {'supported': 'yes', 'relation': r['relation'],
                                 'related_id': r.get('related_id', ''), 'reason': 'Implementation returns the identity.', **overrides} for r in rows}}


def test_document_round_precedes_source_expansion_and_shared_catalog(tmp_path):
    idx = index(tmp_path)
    calls = []
    def call(role, system, prompt, validate):
        payload = json.loads(prompt.split('\n', 1)[1].rsplit('\n', 1)[0]); calls.append((role, payload))
        if role == 'generator':
            row = candidate(path='docs/session.md' if payload['round'] == 'doc' else 'src/session.py')
            if payload['round'] == 'source':
                assert payload['baseline'][0]['id'] == 'session'
            data = {'candidates': [row]}
        else:
            data = approve(payload['candidates'])
        validate(data)
        return reply(role, data)
    engine = DiscoveryEngine(idx, seeds=[], owners=['runtime'], state={}, call=call, save=lambda: None, concurrency=13)
    assert engine.scan() and engine.review()
    features, outcomes = engine.catalog('repos/demo')
    assert len(features) == 1
    assert features[0]['docs'] == ['docs/session.md']
    assert features[0]['source_globs'] == ['src/session.py']
    assert outcomes[0]['origin_rounds'] == ['doc', 'source']
    assert [p['round'] for role, p in calls if role == 'generator'] == ['doc', 'source']


def test_source_only_feature_is_discovered_without_documentation(tmp_path):
    idx = index(tmp_path, docs=False)
    def call(role, system, prompt, validate):
        payload = json.loads(prompt.split('\n', 1)[1].rsplit('\n', 1)[0])
        data = {'candidates': [candidate()]} if role == 'generator' else approve(payload['candidates'])
        return reply(role, data)
    engine = DiscoveryEngine(idx, seeds=[], owners=[], state={}, call=call, save=lambda: None)
    assert engine.scan() and engine.review()
    features, _ = engine.catalog('repos/demo')
    assert features[0]['docs'] == []


def test_document_only_claim_does_not_enter_implemented_catalog(tmp_path):
    idx = index(tmp_path)
    row = candidate(path='docs/session.md'); row['generator_receipts'] = [{'trace_id': 'g'}]
    engine = DiscoveryEngine(idx, seeds=[], owners=[], state={'candidates': {'session': row},
        'reviews': {'session': {'supported': 'yes', 'relation': 'new', 'reason': 'documented', 'candidate_sha256': _hash(row), 'judge_receipt': {'trace_id': 'judge'}}}},
        call=None, save=lambda: None)
    features, outcomes = engine.catalog('repos/demo')
    assert not features
    assert outcomes[0]['implementation_status'] == 'documented_unconfirmed'


def test_missing_native_receipt_never_accepts_candidate():
    with pytest.raises(ModelUnavailable, match='archive'):
        receipt(reply('generator', {'candidates': []}, archived=False))
    changed = reply('generator', {'candidates': []})
    object.__setattr__(changed, 'text', changed.text + ' changed')
    with pytest.raises(ModelUnavailable): receipt(changed)


def test_invalid_citation_remains_unknown_despite_positive_review(tmp_path):
    idx = index(tmp_path, docs=False)
    def call(role, system, prompt, validate):
        payload = json.loads(prompt.split('\n', 1)[1].rsplit('\n', 1)[0])
        row = candidate(); row['evidence'][0]['end'] = 900
        return reply(role, {'candidates': [row]} if role == 'generator' else approve(payload['candidates']))
    engine = DiscoveryEngine(idx, seeds=[], owners=[], state={}, call=call, save=lambda: None)
    assert engine.scan() and engine.review()
    features, outcomes = engine.catalog('repos/demo')
    assert not features and outcomes[0]['status'] == 'unknown'


@pytest.mark.parametrize('relation', ['implementation_supplement', 'alias', 'subcapability', 'shared_component'])
def test_seed_id_owner_and_entry_points_are_preserved_during_relation_merge(tmp_path, relation):
    idx = index(tmp_path, docs=False)
    seed = {'id': 'session', 'title': 'Existing session', 'owner': 'original', 'source_globs': ['src/original.py'],
            'entry_points': ['src/original.py'], 'docs': [], 'page': 'repos/demo/components/original/feature-session.md'}
    row = candidate(); row['generator_receipts'] = [{'trace_id': 'native-generator'}]
    state = {'candidates': {'session': row}, 'reviews': {'session': {'supported': 'yes', 'relation': relation,
             'related_id': 'session', 'reason': 'Same lifecycle', 'candidate_sha256': _hash(row), 'judge_receipt': {'trace_id': 'native-judge'}}}}
    engine = DiscoveryEngine(idx, seeds=[seed], owners=[], state=state, call=None, save=lambda: None)
    features, _ = engine.catalog('repos/demo')
    assert features[0]['owner'] == 'original' and features[0]['title'] == 'Existing session'
    assert features[0]['source_globs'] == ['src/original.py', 'src/session.py']
    assert features[0]['entry_points'] == ['src/original.py']
    assert seed['source_globs'] == ['src/original.py']


def test_alias_uses_accepted_target_and_does_not_inflate_denominator(tmp_path):
    idx = index(tmp_path, docs=False)
    first = candidate(); second = candidate('conversation', title='Conversation session')
    for row in (first,second): row['generator_receipts'] = [{'trace_id': 'g'}]
    state = {'candidates': {'session': first, 'conversation': second}, 'reviews': {
        'session': {'supported': 'yes', 'relation': 'new', 'reason': 'Implemented', 'candidate_sha256': _hash(first), 'judge_receipt': {'trace_id': 'j'}},
        'conversation': {'supported': 'yes', 'relation': 'alias', 'related_id': 'session', 'reason': 'Same behavior', 'candidate_sha256': _hash(second), 'judge_receipt': {'trace_id': 'j'}}}}
    engine = DiscoveryEngine(idx, seeds=[], owners=[], state=state, call=None, save=lambda: None)
    features, outcomes = engine.catalog('repos/demo')
    assert len(features) == 1 and all(r['status'] == 'accepted' for r in outcomes)


def test_rejected_candidate_gets_at_most_three_corrections(tmp_path):
    idx = index(tmp_path, docs=False)
    calls = []
    def call(role, system, prompt, validate):
        payload = json.loads(prompt.split('\n', 1)[1].rsplit('\n', 1)[0]); calls.append(role)
        return reply(role, {'candidates': [candidate()]} if role == 'generator' else approve(payload['candidates'], supported='unsure'))
    engine = DiscoveryEngine(idx, seeds=[], owners=[], state={}, call=call, save=lambda: None)
    assert engine.scan() and engine.review()
    assert calls.count('generator') == 4 and calls.count('judge') == 4
    assert engine.state['reviews']['session']['attempts'] == 4
    assert not engine.catalog('repos/demo')[0]


def test_budget_interruption_keeps_pending_and_resumes_without_repeating_success(tmp_path):
    idx = index(tmp_path)
    state, calls = {}, []
    def call(role, system, prompt, validate):
        payload = json.loads(prompt.split('\n', 1)[1].rsplit('\n', 1)[0]); calls.append(payload['round'])
        if payload['round'] == 'source': raise BudgetExhausted('stop')
        return reply(role, {'candidates': [candidate(path='docs/session.md')]})
    engine = DiscoveryEngine(idx, seeds=[], owners=[], state=state, call=call, save=lambda: None, concurrency=1)
    assert not engine.scan() and state['scan_complete'] is False
    def resumed(role, system, prompt, validate):
        payload = json.loads(prompt.split('\n', 1)[1].rsplit('\n', 1)[0]); calls.append(payload['round'])
        return reply(role, {'candidates': [candidate()]})
    second = DiscoveryEngine(idx, seeds=[], owners=[], state=state, call=resumed, save=lambda: None)
    assert second.scan()
    assert calls.count('doc') == 1


def test_unknown_related_feature_does_not_silently_create_a_feature(tmp_path):
    idx = index(tmp_path, docs=False)
    state = {'candidates': {'session': candidate()}, 'reviews': {'session': {'supported': 'yes', 'relation': 'alias',
             'related_id': 'missing', 'reason': 'alias', 'judge_receipt': {'trace_id': 'j'}}}}
    engine = DiscoveryEngine(idx, seeds=[], owners=[], state=state, call=None, save=lambda: None)
    features, outcomes = engine.catalog('repos/demo')
    assert not features and outcomes[0]['status'] == 'unknown'


def test_successful_repair_survives_checkpoint_resume_without_calls(tmp_path):
    idx = index(tmp_path, docs=False)
    calls = []
    def call(role, system, prompt, validate):
        payload = json.loads(prompt.split('\n', 1)[1].rsplit('\n', 1)[0]); calls.append(role)
        if role == 'generator':
            row = candidate()
            if payload.get('round') == 'repair': row['description'] = 'Return the supplied session identity; no cleanup guarantee.'
            return reply(role, {'candidates': [row]})
        supported = 'yes' if 'no cleanup guarantee' in payload['candidates'][0]['description'] else 'no'
        return reply(role, approve(payload['candidates'], supported=supported))
    state = {}
    first = DiscoveryEngine(idx, seeds=[], owners=[], state=state, call=call, save=lambda: None)
    assert first.scan() and first.review()
    assert len(first.catalog('repos/demo')[0]) == 1
    count = len(calls)
    resumed = DiscoveryEngine(idx, seeds=[], owners=[], state=json.loads(json.dumps(state)), call=call, save=lambda: None)
    assert resumed.scan() and resumed.review()
    assert len(resumed.catalog('repos/demo')[0]) == 1 and len(calls) == count


def test_inflight_budget_reservations_are_journaled(tmp_path):
    from infermatrix_copilot.kb_service.init_feature_discovery import _discovery_budget
    from infermatrix_copilot.kb_service.init_budget import Budget
    journal = tmp_path / 'reservation.json'
    wrapper = _discovery_budget(Budget(1), journal, 'batch')
    with wrapper.reserve(.5) as reservation:
        saved = json.loads(journal.read_text())
        assert len(reservation.identity) == 32
        assert saved == {'schema': 'discovery-budget/2', 'identity': 'batch',
                         'reserved_usd': .5, 'spent_usd': 0,
                         'calls': {reservation.identity: {'status': 'reserved', 'reserved_usd': .5,
                                                         'accounted_usd': .5, 'accounting': 'actual'}}}
        reservation.charge(.2)
    saved = json.loads(journal.read_text())
    assert saved['reserved_usd'] == 0 and saved['spent_usd'] == .2
    assert saved['calls'] == {reservation.identity: {'status': 'settled', 'reserved_usd': .5,
                                                     'accounted_usd': .2, 'accounting': 'actual'}}


def test_malformed_candidate_does_not_discard_valid_sibling(tmp_path):
    idx = index(tmp_path, docs=False)
    def call(role, system, prompt, validate):
        payload = json.loads(prompt.split('\n', 1)[1].rsplit('\n', 1)[0])
        data = {'candidates': [{'id': '../bad'}, candidate()]} if role == 'generator' else approve(payload['candidates'])
        validate(data)
        return reply(role, data)
    engine = DiscoveryEngine(idx, seeds=[], owners=[], state={}, call=call, save=lambda: None)
    assert engine.scan() and engine.review()
    assert len(engine.catalog('repos/demo')[0]) == 1
    task = next(iter(engine.state['tasks'].values()))
    assert task['invalid_candidates'][0]['status'] == 'unknown'


def test_successful_repair_extraction_is_reused_after_review_budget_stop(tmp_path):
    idx = index(tmp_path, docs=False)
    calls, state = [], {}
    stopped = False
    def call(role, system, prompt, validate):
        nonlocal stopped
        payload = json.loads(prompt.split('\n', 1)[1].rsplit('\n', 1)[0])
        calls.append((role, payload.get('round')))
        if role == 'generator':
            row = candidate()
            if payload.get('round') == 'repair': row['description'] = 'Only return the session identity.'
            return reply(role, {'candidates': [row]})
        if payload['candidates'][0]['description'].startswith('Only'):
            if not stopped:
                stopped = True
                raise BudgetExhausted('review budget unavailable')
            return reply(role, approve(payload['candidates']))
        return reply(role, approve(payload['candidates'], supported='no'))
    first = DiscoveryEngine(idx, seeds=[], owners=[], state=state, call=call, save=lambda: None)
    assert first.scan() and not first.review()
    assert state['repair_drafts']['session']['attempt'] == 2
    resumed = DiscoveryEngine(idx, seeds=[], owners=[], state=json.loads(json.dumps(state)), call=call, save=lambda: None)
    assert resumed.scan() and resumed.review()
    assert len(resumed.catalog('repos/demo')[0]) == 1
    assert calls.count(('generator', 'repair')) == 1


def test_long_line_packets_have_distinct_resume_keys(tmp_path, monkeypatch):
    import infermatrix_copilot.kb_service.init_feature_discovery as discovery
    monkeypatch.setattr(discovery, 'MAX_PACKET_CHARS', 12500)
    (tmp_path / 'src').mkdir()
    (tmp_path / 'src/large.custom').write_text('x' * 50000)
    idx = build_discovery_index(tmp_path, pin=PIN, scope={'roots': ['src/'], 'exclude': []})
    calls = []
    def call(role, system, prompt, validate):
        calls.append(prompt)
        return reply(role, {'candidates': []})
    engine = DiscoveryEngine(idx, seeds=[], owners=[], state={}, call=call, save=lambda: None)
    assert engine.scan()
    assert len(calls) == len(engine.state['tasks']) == 5
    assert engine.scan() and len(calls) == 5


def test_document_only_unknown_does_not_repeat_same_evidence_for_repairs(tmp_path):
    idx = index(tmp_path)
    calls = []
    def call(role, system, prompt, validate):
        payload = json.loads(prompt.split('\n', 1)[1].rsplit('\n', 1)[0]); calls.append(role)
        if role == 'generator':
            return reply(role, {'candidates': [candidate(path='docs/session.md')]} if payload['round'] == 'doc' else {'candidates': []})
        return reply(role, approve(payload['candidates'], supported='unsure'))
    engine = DiscoveryEngine(idx, seeds=[], owners=[], state={}, call=call, save=lambda: None)
    assert engine.scan() and engine.review()
    assert calls == ['generator', 'generator', 'judge']
    assert engine.state['reviews']['session']['attempts'] == 1
    assert engine.catalog('repos/demo')[1][0]['implementation_status'] == 'documented_unconfirmed'


@pytest.mark.parametrize('malformed', [[], None, 'yes'])
def test_malformed_judge_container_stays_unknown_without_crashing_batch(tmp_path, malformed):
    idx = index(tmp_path, docs=False)
    def call(role, system, prompt, validate):
        return reply(role, {'candidates': [candidate()]} if role == 'generator' else {'decisions': malformed})
    engine = DiscoveryEngine(idx, seeds=[], owners=[], state={}, call=call, save=lambda: None)
    assert engine.scan() and engine.review()
    assert engine.state['reviews']['session']['supported'] == 'unsure'
    assert not engine.catalog('repos/demo')[0]


def test_review_channel_failure_does_not_regenerate_successful_extraction(tmp_path):
    idx = index(tmp_path, docs=False)
    calls = []
    def call(role, system, prompt, validate):
        calls.append(role)
        if role == 'judge': raise ModelUnavailable('subscription channel unavailable')
        return reply(role, {'candidates': [candidate()]})
    engine = DiscoveryEngine(idx, seeds=[], owners=[], state={}, call=call, save=lambda: None)
    assert engine.scan() and engine.review()
    assert calls == ['generator', 'judge']
    assert engine.state['reviews']['session']['repair_blocked']


@pytest.mark.parametrize('field,value', [('id',123),('relation',[])])
def test_bad_candidate_scalar_remains_unknown_without_discarding_sibling(tmp_path, field, value):
    idx = index(tmp_path, docs=False)
    def call(role, system, prompt, validate):
        payload = json.loads(prompt.split('\n', 1)[1].rsplit('\n', 1)[0])
        bad = candidate('bad');bad[field]=value
        return reply(role, {'candidates':[bad,candidate()]} if role=='generator' else approve(payload['candidates']))
    engine=DiscoveryEngine(idx,seeds=[],owners=[],state={},call=call,save=lambda:None)
    assert engine.scan() and engine.review()
    assert len(engine.catalog('repos/demo')[0])==1


def test_proposal_self_supplement_is_repaired_using_formal_baseline(tmp_path):
    idx=index(tmp_path,docs=False)
    seed={'id':'runtime','title':'Runtime','owner':'runtime','source_globs':['src/*'],'docs':[],
          'page':'repos/demo/components/runtime/feature-runtime.md'}
    calls=[]
    def call(role,system,prompt,validate):
        payload=json.loads(prompt.split('\n',1)[1].rsplit('\n',1)[0]);calls.append(role)
        assert next(r for r in payload['baseline'] if r['id']=='runtime')['catalog_status']=='existing_feature'
        if role=='generator':
            row=candidate()
            if payload.get('round')=='repair':row.update(relation='implementation_supplement',related_id='runtime')
            return reply(role,{'candidates':[row]})
        row=payload['candidates'][0]
        assert next(r for r in payload['baseline'] if r['id']=='session')['catalog_status']=='unreviewed_candidate'
        return reply(role,approve([row],relation='implementation_supplement',related_id=row.get('related_id') or 'session'))
    engine=DiscoveryEngine(idx,seeds=[seed],owners=['runtime'],state={},call=call,save=lambda:None)
    assert engine.scan() and engine.review()
    features,outcomes=engine.catalog('repos/demo')
    assert len(features)==1 and outcomes[0]['status']=='accepted'
    assert calls==['generator','judge','generator','judge']


def test_repair_review_channel_failure_retains_draft_for_explicit_retry(tmp_path):
    idx=index(tmp_path,docs=False);calls=[];stopped=False;state={}
    def call(role,system,prompt,validate):
        nonlocal stopped
        payload=json.loads(prompt.split('\n',1)[1].rsplit('\n',1)[0]);calls.append(role)
        if role=='generator':
            row=candidate()
            if payload.get('round')=='repair':row['description']='Only return the supplied identity.'
            return reply(role,{'candidates':[row]})
        revised=payload['candidates'][0]['description'].startswith('Only')
        if revised and not stopped:
            stopped=True;raise ModelUnavailable('review channel unavailable')
        return reply(role,approve(payload['candidates'],supported='yes' if revised else 'no'))
    first=DiscoveryEngine(idx,seeds=[],owners=[],state=state,call=call,save=lambda:None)
    assert first.scan() and first.review()
    assert state['reviews']['session']['repair_blocked']
    assert 'session' in state['repair_drafts'] and calls.count('generator')==2
    state['reviews']['session'].pop('repair_blocked')  # Explicit retry-unfinished transition.
    resumed=DiscoveryEngine(idx,seeds=[],owners=[],state=state,call=call,save=lambda:None)
    assert resumed.scan() and resumed.review()
    assert calls.count('generator')==2 and len(resumed.catalog('repos/demo')[0])==1


def rejected_repair_state(count):
    rows = [candidate(f'capability-{n}', title=f'Capability {n}') for n in range(count)]
    for row in rows:
        row.update(generator_receipts=[{'trace_id': 'original'}], origin_rounds=['source'])
    return {'candidates': {r['id']: r for r in rows}, 'reviews': {
        r['id']: {'supported': 'no', 'relation': 'unknown', 'related_id': '',
                  'reason': 'Needs a narrower claim.', 'attempts': 1} for r in rows}}


def test_repairs_share_thirteen_slots_and_checkpoint_before_judge(tmp_path):
    import threading
    import time
    idx = index(tmp_path, docs=False)
    state = rejected_repair_state(13)
    coordinator = threading.get_ident()
    barrier = threading.Barrier(13)
    lock = threading.Lock()
    active = peak = 0
    durable = {}
    def save():
        assert threading.get_ident() == coordinator
        with lock:
            durable.clear()
            durable.update(deepcopy(state))
    def call(role, system, prompt, validate):
        nonlocal active, peak
        payload = json.loads(prompt.split('\n', 1)[1].rsplit('\n', 1)[0])
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            if role == 'generator':
                barrier.wait(timeout=5)
                row = deepcopy(payload['candidate'])
                row['description'] = 'Only return the supplied identity.'
                return reply(role, {'candidates': [row]})
            row = payload['candidates'][0]
            with lock:
                assert _hash(durable['repair_drafts'][row['id']]['candidate']) == _hash(row)
            time.sleep(.01)
            return reply(role, approve([row]))
        finally:
            with lock:
                active -= 1
    engine = DiscoveryEngine(idx, seeds=[], owners=[], state=state, call=call, save=save, concurrency=13)
    assert engine.review()
    assert peak == 13
    assert all(r['supported'] == 'yes' and r['attempts'] == 2 for r in state['reviews'].values())
    assert not state['repair_drafts']


def test_parallel_repairs_keep_three_corrections_and_deterministic_content(tmp_path):
    from collections import Counter
    idx = index(tmp_path, docs=False)
    results = []
    for concurrency in (1, 3):
        state = rejected_repair_state(7)
        calls = Counter()
        def call(role, system, prompt, validate):
            payload = json.loads(prompt.split('\n', 1)[1].rsplit('\n', 1)[0])
            row = deepcopy(payload['candidate'] if role == 'generator' else payload['candidates'][0])
            calls[(role, row['id'])] += 1
            if role == 'generator':
                row['description'] += ' Corrected.'
                return reply(role, {'candidates': [row]})
            return reply(role, approve([row], supported='no'))
        engine = DiscoveryEngine(idx, seeds=[], owners=[], state=state, call=call, save=lambda: None,
                                 concurrency=concurrency)
        assert engine.review()
        assert all(r['attempts'] == 4 for r in state['reviews'].values())
        assert all(calls[(role, key)] == 3 for role in ('generator', 'judge') for key in state['candidates'])
        previous_calls = calls.copy()
        assert engine.review() and calls == previous_calls
        results.append(state)
    assert results[0] == results[1]


def test_parallel_repair_budget_stop_drains_and_resumes_durable_drafts(tmp_path, monkeypatch):
    import threading
    from collections import Counter
    from concurrent.futures import wait
    import infermatrix_copilot.kb_service.init_feature_discovery as discovery
    idx = index(tmp_path, docs=False)
    state = rejected_repair_state(4)
    barrier = threading.Barrier(3)
    save_seen = threading.Event()
    calls = Counter()
    def save():
        if any(r['supported'] == 'yes' for r in state['reviews'].values()):
            save_seen.set()
    def budget_first(futures):
        wait(futures)
        return iter(sorted(futures, key=lambda f: not isinstance(f.exception(), BudgetExhausted)))
    # Deterministically observe the budget failure before in-flight successes.
    monkeypatch.setattr(discovery, 'as_completed', budget_first)
    # Three already-durable drafts start independent reviews. One budget stop
    # must preserve its draft, drain the other approvals and never dispatch #4.
    for key, row in list(state['candidates'].items())[:3]:
        revised = deepcopy(row); revised['description'] += ' Corrected.'
        state.setdefault('repair_drafts', {})[key] = {'base_sha256': _hash(row), 'attempt': 2, 'candidate': revised}
    def call(role, system, prompt, validate):
        payload = json.loads(prompt.split('\n', 1)[1].rsplit('\n', 1)[0])
        row = payload['candidates'][0]
        calls[(role, row['id'])] += 1
        barrier.wait(timeout=5)
        if row['id'] == 'capability-0':
            raise BudgetExhausted('stop new dispatch')
        return reply(role, approve([row]))
    engine = DiscoveryEngine(idx, seeds=[], owners=[], state=state, call=call, save=save, concurrency=3)
    assert not engine.review()
    assert save_seen.is_set()
    assert state['reviews']['capability-1']['supported'] == 'yes'
    assert state['reviews']['capability-2']['supported'] == 'yes'
    assert state['repair_drafts']['capability-0']['attempt'] == 2
    assert state['reviews']['capability-0']['attempts'] == 1
    assert not any(key == 'capability-3' for role, key in calls)
    def resumed_call(role, system, prompt, validate):
        payload = json.loads(prompt.split('\n', 1)[1].rsplit('\n', 1)[0])
        row = payload['candidate'] if role == 'generator' else payload['candidates'][0]
        calls[(role, row['id'])] += 1
        return reply(role, {'candidates': [row]} if role == 'generator' else approve([row]))
    resumed = DiscoveryEngine(idx, seeds=[], owners=[], state=json.loads(json.dumps(state)), call=resumed_call,
                              save=lambda: None, concurrency=3)
    assert resumed.review()
    assert calls[('generator', 'capability-0')] == 0
    assert calls[('judge', 'capability-1')] == calls[('judge', 'capability-2')] == 1
    assert all(r['supported'] == 'yes' for r in resumed.state['reviews'].values())


def test_parallel_extraction_budget_stop_saves_other_drafts_without_judging(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import wait
    import infermatrix_copilot.kb_service.init_feature_discovery as discovery
    idx = index(tmp_path, docs=False)
    state = rejected_repair_state(4)
    barrier = threading.Barrier(3)
    called = []
    def budget_first(futures):
        wait(futures)
        return iter(sorted(futures, key=lambda f: not isinstance(f.exception(), BudgetExhausted)))
    monkeypatch.setattr(discovery, 'as_completed', budget_first)
    def call(role, system, prompt, validate):
        payload = json.loads(prompt.split('\n', 1)[1].rsplit('\n', 1)[0])
        assert role == 'generator'
        row = payload['candidate']; called.append(row['id'])
        barrier.wait(timeout=5)
        if row['id'] == 'capability-0':
            raise BudgetExhausted('stop new dispatch')
        row['description'] += ' Corrected.'
        return reply(role, {'candidates': [row]})
    engine = DiscoveryEngine(idx, seeds=[], owners=[], state=state, call=call, save=lambda: None, concurrency=3)
    assert not engine.review()
    assert sorted(called) == ['capability-0', 'capability-1', 'capability-2']
    assert set(state['repair_drafts']) == {'capability-1', 'capability-2'}
    assert all(r['attempts'] == 1 for r in state['reviews'].values())
    assert all(d['attempt'] == 2 for d in state['repair_drafts'].values())


@pytest.mark.parametrize('failure_phase', ['generator', 'judge'])
def test_parallel_repair_single_channel_or_content_failure_stays_local(tmp_path, failure_phase):
    from collections import Counter
    idx = index(tmp_path)
    state = rejected_repair_state(5)
    state['candidates']['capability-3']['evidence'] = [{'path': 'docs/session.md', 'start': 1, 'end': 2}]
    state['reviews']['capability-4']['attempts'] = 4
    calls = Counter()
    def call(role, system, prompt, validate):
        payload = json.loads(prompt.split('\n', 1)[1].rsplit('\n', 1)[0])
        row = payload['candidate'] if role == 'generator' else payload['candidates'][0]
        calls[(role, row['id'])] += 1
        if row['id'] == 'capability-0' and role == failure_phase:
            raise ModelUnavailable('one native channel failed')
        if role == 'generator':
            row = deepcopy(row)
            if row['id'] == 'capability-2':
                row['id'] = 'wrong-id'
            return reply(role, {'candidates': [row]})
        return reply(role, approve([row]))
    engine = DiscoveryEngine(idx, seeds=[], owners=[], state=state, call=call, save=lambda: None, concurrency=3)
    assert engine.review()
    assert state['reviews']['capability-0']['repair_blocked']
    assert state['reviews']['capability-0']['attempts'] == 1
    assert ('capability-0' in state.get('repair_drafts', {})) == (failure_phase == 'judge')
    assert state['reviews']['capability-1']['supported'] == 'yes'
    assert state['reviews']['capability-2']['attempts'] == 4
    assert calls[('generator', 'capability-2')] == 3
    assert not any(key in ('capability-3', 'capability-4') for role, key in calls)
