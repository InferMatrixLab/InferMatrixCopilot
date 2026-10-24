import json
import pytest

from verify_installed_arms import packet_comparison


def packet(data, **kwargs):
    return {'system': 'exact rubric', 'prompt': '<untrusted_data>\n' + json.dumps(data, **kwargs) + '\n</untrusted_data>\n'}


def test_input_comparison_retains_both_raw_hashes_for_key_order_drift():
    before = [packet({'source': 'frozen', 'claims': ['a', 'b']})]
    after = [packet({'source': 'frozen', 'claims': ['a', 'b']}, sort_keys=True)]
    result = packet_comparison(before, after)
    assert result['content_equal'] and not result['raw_bytes_equal']
    assert result['baseline_sha256'] != result['candidate_sha256']


@pytest.mark.parametrize('after', [
    {'source': 'changed', 'claims': ['a', 'b']},
    {'source': 'frozen', 'claims': ['b', 'a']},
    {'source': 'frozen'},
])
def test_conformance_rejects_actual_input_changes(after):
    with pytest.raises(AssertionError, match='content differs'):
        packet_comparison([packet({'source': 'frozen', 'claims': ['a', 'b']})], [packet(after)])


def test_conformance_rejects_rubric_types_and_duplicate_keys():
    for before, after in [
        (packet({'number': 1}), packet({'number': 1.0})),
        (packet({'number': True}), packet({'number': 1})),
        (packet({}), {**packet({}), 'system': 'changed rubric'}),
        (packet({'number': 1}), {'system': 'exact rubric', 'prompt': '<untrusted_data>\n{"number":2,"number":1}\n</untrusted_data>\n'}),
    ]:
        with pytest.raises(AssertionError):
            packet_comparison([before], [after])
