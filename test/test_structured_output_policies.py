"""Preserve legacy caller behavior while centralizing object extraction."""
import json
from pathlib import Path

import pytest

from infermatrix_copilot.improve.judges import parse_verdict
from infermatrix_copilot.rebase_engine.plan_review import _parse_review
from infermatrix_copilot.rebase_engine.worktree import load_decision_json
from infermatrix_copilot.structured_output import decode_json_object

_CASES = json.loads((Path(__file__).parent / 'fixtures/legacy-json-extraction.json').read_text())


@pytest.mark.parametrize('case', _CASES)
@pytest.mark.parametrize('caller', ['judge', 'plan', 'decision'])
def test_legacy_extraction_contract(case, caller, tmp_path):
    path = tmp_path / 'decision.json'
    path.write_text(case['input'])
    run = {'judge': lambda: parse_verdict(case['input']),
           'plan': lambda: _parse_review(case['input']),
           'decision': lambda: load_decision_json(path)}[caller]
    try:
        actual = {'value': run()}
    except Exception as exc:
        actual = {'error': type(exc).__name__, 'message': str(exc).replace(str(path), '<decision>')}
    assert actual == case['results'][caller]


def test_named_policies_do_not_share_acceptance_rules():
    text = '{invalid} {"verdict":"approve"}'
    assert decode_json_object(text) == {'verdict': 'approve'}
    with pytest.raises(json.JSONDecodeError):
        decode_json_object(text, policy='greedy_object')
    assert decode_json_object('prefix {} suffix', policy='greedy_object') == {}
    with pytest.raises(ValueError):
        decode_json_object('prefix {} suffix', policy='trailing_object')
