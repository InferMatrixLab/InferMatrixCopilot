"""Wire compatibility and one review severity owner across computation paths."""
import pytest

from infermatrix_copilot.contracts.models import ResultDecodeError
from infermatrix_copilot.review.direct_prompt import direct_review_schema
from infermatrix_copilot.review.result_decode import strict_finding
from infermatrix_copilot.review.severity import (
    STRICT_SEVERITY_DEMOTION, STRICT_SEVERITY_ORDER, STRICT_TO_PRIORITY,
)


@pytest.mark.parametrize('label,priority', [
    ('blocker', 'P0'), ('major', 'P1'), ('minor', 'P2'), ('nit', 'P3'),
])
def test_strict_projection_preserves_explicit_wire_severity(label, priority):
    # No heuristic reclassification of historical or new model output. Even
    # a poorly justified blocker stays P0; calibration belongs in generation.
    finding = strict_finding({'severity': label.upper(), 'file': 'a.py',
                              'line': 18, 'comment': 'Bounded defect'})
    assert finding.severity == priority
    assert finding.line == 18
    enum = direct_review_schema()['properties']['findings']['items']['properties']['severity']['enum']
    assert tuple(enum) == tuple(STRICT_TO_PRIORITY.values())


def test_unknown_strict_severity_is_not_guessed():
    with pytest.raises(ResultDecodeError, match='unknown severity'):
        strict_finding({'severity': 'critical-ish', 'file': 'a.py',
                        'comment': 'Some defect', 'line': 1})


def test_order_and_existing_unverifiable_demotion_share_the_wire_levels():
    from infermatrix_copilot.engine.steps.review.utils import _SEVERITY_ORDER
    assert _SEVERITY_ORDER is STRICT_SEVERITY_ORDER
    assert list(STRICT_SEVERITY_ORDER) == ['blocker', 'major', 'minor', 'nit']
    assert dict(STRICT_SEVERITY_DEMOTION) == {
        'blocker': 'major', 'major': 'minor', 'minor': 'nit', 'nit': 'nit',
    }
    with pytest.raises(TypeError):
        STRICT_TO_PRIORITY['blocker'] = 'P2'


@pytest.mark.parametrize('label,expected', [
    ('blocker', 1.0), ('major', 1.0), ('minor', 1.0), ('nit', 1.0),
    ('P0', 0.0), ('unknown', 0.0),
    ([], 0.0), ({}, 0.0), (None, 0.0), (1, 0.0),
])
def test_replay_objective_preserves_legacy_vocabulary(label, expected):
    from infermatrix_copilot.improve.objectives import score
    row = {'payload': {'head_files': {'a.py': 'return value\n'}}}
    output = {'review_text': 'Supported comment', 'review_comments': [{
        'file': 'a.py', 'line': 1, 'severity': label, 'comment': 'Check value',
    }]}
    assert score('pr-review', row, output) == expected
