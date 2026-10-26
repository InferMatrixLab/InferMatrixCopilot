"""Planning consumers must distinguish native observations from legacy aliases."""
import pytest

from infermatrix_copilot.improve.lints import unit_usd
from infermatrix_copilot.improve.reader import Unit


def call(*, provider='harness:claude-code', result=None, usage=None, model=None):
    return {'kind': 'model_call', 'id': 'call', 'at': 1, 'seconds': 1,
            'model': model or {'provider': provider, 'model': 'claude-sonnet-5'},
            'usage': usage or {}, 'result': result or {}}


def unit(*calls):
    return Unit('u', 'workflow', 'playbook', 'step', True, records=list(calls))


def test_reported_native_cost_is_used_instead_of_legacy_account_zero():
    row = call(result={'cost_usd': .75, 'usd': 0, 'subscription': True,
                       'accounting_convention': 'judge-calls/v1'})
    assert unit_usd(unit(row)) == .75


@pytest.mark.parametrize('result', [{}, {'cost_usd': None}, {'cost_usd': -1},
                                 {'cost_usd': float('nan')}, {'usd': 0, 'subscription': True}])
def test_native_unknown_cost_cannot_be_a_free_planning_sample(result):
    assert unit_usd(unit(call(result=result))) is None


def test_api_prices_served_model_and_cache_facts_without_fabricating_missing_usage():
    observed = call(provider='api', model={'provider': 'api', 'model': 'unknown-alias',
                                         'served_model': 'mimo-v2.5'},
                    usage={'input_tokens': 11430, 'cache_read_input_tokens': 1664,
                           'output_tokens': 16000})
    assert unit_usd(unit(observed)) == pytest.approx(.026459)
    assert unit_usd(unit(call(provider='api', usage={'input_tokens': 4}))) is None
    observed['model']['served_model'] = 'unpriced-model'
    assert unit_usd(unit(observed)) is None


def test_partial_unit_retains_known_subtotal_but_has_no_complete_price():
    from infermatrix_copilot.improve.lints import unit_cost
    cost = unit_cost(unit(call(result={'cost_usd': .75}), call()))
    assert cost.known_usd == .75 and cost.unknown_calls == 1 and cost.usd is None
    assert unit_usd(unit(call(result={'cost_usd': 0}))) == 0
    assert unit_usd(unit()) == 0  # no model calls is different from missing usage


def test_legacy_cli_aliases_and_explicit_unknown_report_do_not_become_api_estimates():
    row = call(provider='claude', result={'usd': 0, 'subscription': True},
               usage={'input_tokens': 1000, 'output_tokens': 100})
    assert unit_usd(unit(row)) is None
    row['result'] = {'cost_usd': None}
    row['usage']['cost_usd'] = 9
    assert unit_usd(unit(row)) is None


def test_unknown_price_skips_cost_outlier_but_still_checks_latency():
    from infermatrix_copilot.improve.lints import Baseline, l11_outliers
    row = call()
    row['seconds'] = 100
    found = l11_outliers(unit(row), None, Baseline(usd=[0] * 10, seconds=[1] * 10))
    assert len(found) == 1 and found[0].detail.startswith('seconds ')


def test_new_planning_baseline_preserves_old_rows_and_distinguishes_zero_from_unknown(tmp_path):
    from copy import deepcopy
    from infermatrix_copilot.improve.ledger import Ledger
    from infermatrix_copilot.improve.experiments import _planning_unit_usd
    from infermatrix_copilot.improve.lints import COST_POLICY
    ledger = Ledger(tmp_path)
    old = ledger.load('workflow')
    old.baseline['usd'] = [0, 4]
    old.cycles = [{'at': 1, 'usd': 0, 'units': 1}]
    ledger.save(old)
    original = deepcopy(old.cycles)
    assert _planning_unit_usd(tmp_path, 'workflow') == 4
    args = dict(at=2, declared=True, tier=1, units=1, quarantined=0, usd=0, seconds=1,
                lints={}, seconds_samples=[1], parse_failure_rate=0, cost_policy=COST_POLICY)
    current = ledger.record_cycle('workflow', **args, usd_samples=[], cost_unknown_calls=1)
    assert current.cycles[:1] == original and current.baseline['usd'] == [0, 4]
    assert current.baseline['planning_usd'] == []
    assert _planning_unit_usd(tmp_path, 'workflow') == .5
    current = ledger.record_cycle('workflow', **{**args, 'at': 3}, usd_samples=[0])
    assert current.baseline['planning_usd'] == [0]
    assert _planning_unit_usd(tmp_path, 'workflow') == 0


def test_cycle_reports_partial_cost_and_does_not_seed_a_free_baseline(tmp_path):
    from itertools import count
    from infermatrix_copilot.config import Settings
    from infermatrix_copilot.improve.cycle import run_cycle
    from infermatrix_copilot.improve.ledger import Ledger
    from infermatrix_copilot.trace_store import TraceStore, trace_context
    ticks = count(1_800_000_000)
    store = TraceStore(tmp_path / 'traces', environ={}, clock=lambda: next(ticks))
    workflow = 'pr-review.agent.review_diff'
    with trace_context(unit_id='u', run_id='r', workflow=workflow, fingerprint='f' * 64,
                       playbook='pr-review', step='agent.review_diff', item='demo#1'):
        for amount in [.75, None]:
            store.append('model_call', model={'provider': 'harness:claude-code', 'model': 'm'},
                         result={'cost_usd': amount}, seconds=1, outputs={'reply': 'fixture'})
        store.append('decision', result={'status': 'ok'})
    settings = Settings(_env_file=None, improve_enabled=True)
    ledger = tmp_path / 'ledger'
    report = run_cycle(store, settings, ledger, now=1_800_003_600)
    row = report['workflows'][workflow]
    assert row['usd'] == .75 and row['cost_unknown_calls'] == 1
    assert Ledger(ledger).load(workflow).baseline['planning_usd'] == []
    assert 'unknown (1 calls)' in next((ledger / 'reports').glob('cycle-*.md')).read_text()


def test_observation_excludes_an_entire_item_with_unknown_cost(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from infermatrix_copilot.improve import reader, enroll, evolution_publish
    rows = {}
    for side, at, revision in [('before', 10, 'base'), ('after', 30, 'target')]:
        for i in range(8):
            row = call(result={'cost_usd': .75})
            row.update(at=at, env={'source_tree_sha': revision})
            u = unit(row)
            u.unit_id = f'{side}-{i}'
            u.item = f'item-{i}'
            rows[u.unit_id] = u
    extra = unit(call())
    extra.unit_id, extra.item = 'unknown', 'item-0'
    extra.records[0].update(at=31, env={'source_tree_sha': 'target'})
    rows[extra.unit_id] = extra
    monkeypatch.setattr(reader, 'units_between', lambda *args: rows)
    declaration = SimpleNamespace(evolution={'guards': {'usd_review': 'lower'}}, outcome_adapter='')
    monkeypatch.setattr(enroll, 'declarations_for', lambda *args: {'workflow': declaration})
    candidate = {'workflow': 'workflow', 'incumbent_sha': 'base', 'artifact': {'tree_sha': 'target'},
                 'created_at': 15, 'merged_at': 20, 'deployed_at': 20}
    evolution_publish.observe(SimpleNamespace(), None, candidate)
    report = candidate['observation']
    assert report['state'] == 'waiting-for-cost-evidence'
    assert report['unknown_cost_items']['after'] == ['item-0']
    assert 'usd_review' not in report['metrics']
    extra.records[0]['result']['cost_usd'] = .75
    evolution_publish.observe(SimpleNamespace(), None, candidate)
    assert candidate['observation']['state'] == 'observed'
    assert candidate['observation']['metrics']['usd_review']['regressed'] is False


def test_api_span_and_account_use_the_same_inclusive_cache_facts(tmp_path):
    import json
    from types import SimpleNamespace
    from infermatrix_copilot import tracing
    from infermatrix_copilot.improve.budget import actual_usd
    from infermatrix_copilot.metrics import cost_from_spans
    usage = {'input_tokens': 1000, 'output_tokens': 10, 'input_tokens_include_cache': True,
             'cache_read_input_tokens': 800, 'cache_creation_input_tokens': 0,
             'unrelated_secret': 'must-not-enter-pricing-facts'}
    attrs = {'model': 'claude-sonnet-5'}
    span = SimpleNamespace(set=lambda **fields: attrs.update(fields), gen_s=0)
    tracing.set_usage(span, usage)
    assert 'unrelated_secret' not in attrs['usage']
    path = tmp_path / 'spans.jsonl'
    path.write_text(json.dumps({'t': 'span', 'trace_id': 'fixture', 'span_id': 's',
                               'name': 'llm', 'attr': attrs, 'dur_ms': 1}) + '\n')
    result = cost_from_spans(path)
    assert result['usd'] == pytest.approx(actual_usd('claude-sonnet-5', usage), abs=1e-6)
    assert result['cost_partial'] is False
    assert 'prompt-cache read  80.0%' in tracing.report(path)


def test_span_preserves_reported_zero_and_does_not_fabricate_missing_usage(tmp_path):
    import json
    from infermatrix_copilot.metrics import cost_from_spans
    path = tmp_path / 'spans.jsonl'
    rows = [{'name': 'llm', 'attr': {'model': 'unknown', 'usage': {'cost_usd': 0}}},
            {'name': 'llm', 'attr': {'model': 'claude-sonnet-5', 'usage': {}}}]
    path.write_text('\n'.join(json.dumps(x) for x in rows) + '\n')
    result = cost_from_spans(path)
    assert result['usd'] == 0 and result['llm_calls'] == 2
    assert result['unpriced_calls'] == 1 and result['cost_partial'] is True


def test_shared_usage_price_prefers_reports_and_marks_malformed_reports_unknown():
    from infermatrix_copilot.budgeting import usage_cost
    usage = {'input_tokens': 1000, 'output_tokens': 100, 'cost_usd': 0}
    assert usage_cost(usage, 3, 15) == 0
    usage['cost_usd'] = float('inf')
    assert usage_cost(usage, 3, 15) is None
