"""Original requests share dispatch durability without inventing legacy input."""
import copy
import json

import pytest

from infermatrix_copilot.kb_service.models import ModelUnavailable
from infermatrix_copilot.kb_service.quality_review import DispatchJournal, drain_review_tasks


def journal(tmp_path, *, checkpoint=None):
    state, requests = {}, {}
    def save(update):
        update()
        (tmp_path/'state.json').write_text(json.dumps({'state': state, 'requests': requests}))
    return DispatchJournal(state, lambda task: {'owner': task['owner'], 'attempt': 1}, checkpoint or save,
                           requests=requests, snapshot=lambda task: task)


def test_request_is_durable_before_work_and_survives_settlement(tmp_path):
    j = journal(tmp_path)
    task = {'owner': 'a', 'evidence': ['original'], 'ranges': ((1, 3),)}
    scope = j.begin(task)
    durable = json.loads((tmp_path/'state.json').read_text())
    assert set(durable['requests']) == set(durable['state']) == {scope[0]}
    task['evidence'].append('changed')
    expected = {'owner': 'a', 'evidence': ['original'], 'ranges': [[1, 3]]}
    assert j.original_request(scope=scope) == expected
    j.original_request(scope=scope)['evidence'].append('detached')
    j.settle(task, {}, None, lambda *args: None, scope=scope)
    assert not j.state and j.original_request(scope=scope) == expected
    loaded = json.loads((tmp_path/'state.json').read_text())
    readonly = DispatchJournal(loaded['state'], None, None, requests=loaded['requests'])
    assert readonly.original_request(scope=scope) == expected
    with pytest.raises(ValueError, match='read-only'):
        readonly.identify = lambda _: {'owner': 'b'}
        readonly.begin({})


@pytest.mark.parametrize('damage', ['request', 'binding', 'digest', 'attempt'])
def test_request_identity_changes_are_refused(tmp_path, damage):
    j = journal(tmp_path); scope = j.begin({'owner': 'a', 'text': 'original'})
    before = (tmp_path/'state.json').read_bytes()
    if damage == 'request':
        j.requests[scope[0]]['request']['text'] = 'changed'
    elif damage == 'binding':
        j.requests[scope[0]]['binding']['owner'] = 'b'
    elif damage == 'digest':
        j.requests[scope[0]]['sha256'] = '0'*64
    else:
        scope = (scope[0], 2)
    with pytest.raises(ModelUnavailable):
        j.original_request(scope=scope)
    assert (tmp_path/'state.json').read_bytes() == before


def test_completed_original_request_cannot_be_replaced(tmp_path):
    j = journal(tmp_path); scope = j.begin({'owner': 'a', 'text': 'original'})
    j.settle({}, {}, None, lambda *args: None, scope=scope)
    before = copy.deepcopy(j.requests)
    with pytest.raises(ModelUnavailable, match='immutable'):
        j.begin({'owner': 'a', 'text': 'different'})
    assert not j.state and j.requests == before


def test_failed_checkpoint_cannot_dispatch_or_lose_original_request(tmp_path):
    def fail(update):
        update()
        raise OSError('checkpoint unavailable')
    j = journal(tmp_path, checkpoint=fail)
    calls = []
    with pytest.raises(OSError, match='checkpoint unavailable'):
        drain_review_tasks([{'owner': 'a'}], lambda task: calls.append(task), lambda *args: None,
                           concurrency=1, journal=j)
    assert not calls and set(j.state) == set(j.requests)
    with pytest.raises(ModelUnavailable, match='outcome unknown'):
        j.begin({'owner': 'a'})


def test_legacy_dispatch_keeps_identity_without_a_fabricated_request(tmp_path):
    old = DispatchJournal({}, lambda task: {'owner': task}, lambda update: update())
    scope = old.begin('a'); original = copy.deepcopy(old.state)
    resumed = DispatchJournal(old.state, lambda task: {'owner': task}, lambda update: update(), requests={})
    with pytest.raises(ModelUnavailable, match='unavailable'):
        resumed.original_request(scope=scope)
    with pytest.raises(ModelUnavailable, match='outcome unknown'):
        resumed.begin('a')
    assert resumed.state == original and resumed.requests == {}
    resumed.settle('a', {}, None, lambda *args: None, scope=scope)
    assert not resumed.state and not resumed.requests


@pytest.mark.parametrize('damage', ['removed', 'changed'])
def test_settlement_cannot_clear_a_task_with_damaged_original_request(tmp_path, damage):
    j = journal(tmp_path); scope = j.begin({'owner': 'a', 'text': 'original'})
    original = copy.deepcopy(j.state)
    if damage == 'removed':
        j.requests.pop(scope[0])
    else:
        j.requests[scope[0]]['request']['text'] = 'changed'
    settled = []
    with pytest.raises(ModelUnavailable):
        j.settle({}, {}, None, lambda *args: settled.append(1), scope=scope)
    assert j.state == original and not settled
