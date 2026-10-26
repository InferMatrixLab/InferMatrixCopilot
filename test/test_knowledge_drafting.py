"""Pure shared attempts and source packing preserve their callers' contracts."""

import pytest

from infermatrix_copilot.knowledge_service.drafting import bounded_attempts
from infermatrix_copilot.kb_service.evidence_bundle import merge_ranges, selected_spans


@pytest.mark.parametrize("repairs", [0, 1, 2])
def test_attempts_never_dispatch_beyond_the_requested_repair_bound(repairs):
    seen = []

    def refuse(feedback, index):
        seen.append((feedback, index))
        return index, None, f"repair-{index}", "refused"

    reply, accepted, observations = bounded_attempts(refuse, max_repairs=repairs)
    assert reply is accepted is None and len(seen) == repairs + 1
    assert seen == [("" if i == 0 else f"repair-{i-1}", i) for i in range(repairs + 1)]
    assert observations == [{"attempt": i, "error": "refused"} for i in range(repairs + 1)]


def test_empty_acceptance_and_terminal_refusal_are_distinct():
    assert bounded_attempts(lambda *_: ({}, (), "", "")) == ({}, (), [])
    assert bounded_attempts(lambda *_: ({}, None, None, "terminal")) == (
        None, None, [{"attempt": 0, "error": "terminal"}])


def test_attempt_exception_does_not_implicitly_retry():
    calls = []

    def unavailable(*_):
        calls.append(1)
        raise RuntimeError("dispatch unavailable")

    with pytest.raises(RuntimeError, match="dispatch unavailable"):
        bounded_attempts(unavailable)
    assert calls == [1]


@pytest.mark.parametrize("value", [True, -1, 3, 1.5])
def test_attempt_bounds_are_validated_before_dispatch(value):
    with pytest.raises(ValueError, match="0..2"):
        bounded_attempts(lambda *_: pytest.fail("must not dispatch"), max_repairs=value)


def test_source_packing_keeps_gaps_unicode_and_first_path_order():
    selected = {"z.py": {8: "8: 終", 2: "2: α", 1: "1: ", 7: "7: 開"},
                "a.md": {4: "", 2: "本文"}}
    assert selected_spans(selected) == [
        {"path": "z.py", "start": 1, "end": 2, "text": ["1: ", "2: α"]},
        {"path": "z.py", "start": 7, "end": 8, "text": ["7: 開", "8: 終"]},
        {"path": "a.md", "start": 2, "end": 2, "text": ["本文"]},
        {"path": "a.md", "start": 4, "end": 4, "text": [""]},
    ]
    assert merge_ranges([(8, 10), (2, 3), (1, 2), (6, 7), (9, 11), (1, 1)]) == [(1, 3), (6, 11)]
