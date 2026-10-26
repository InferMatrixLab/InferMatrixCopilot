"""Full-stage relation acceptance using the shared frozen native response policy."""
import json
from dataclasses import replace

import yaml

from discovery_native_fixture import RELATIONSHIPS, response
from infermatrix_copilot.llm import Block
from test_kb_feature_discovery_stage import NativeScript, _setup, _run, _report, _full_report, world  # noqa: F401


class DiscoveryScript(NativeScript):
    def __init__(self, **variant):
        super().__init__()
        self.variant = variant

    def complete(self, **kwargs):
        reply = super().complete(**kwargs)
        data = response(kwargs['messages'][0]['content'], **self.variant)
        return replace(reply, blocks=[Block(type='text', text=json.dumps(data))])


def test_full_relationship_workflow_preserves_canonical_catalog_and_native_proofs(world):
    script = DiscoveryScript(relationships=True)
    runtime, lifecycle, _, store = _setup(world, script=script)
    record = _run(runtime, lifecycle)
    assert record.status == 'dry_run', (record.problems, record.unfinished)
    assert record.discovery['done']
    assert record.discovery['catalog_boundary_audit']['done']
    assert record.discovery['catalog_consolidation_audit']['done']
    files, compact, _ = _report(record)
    catalog = yaml.safe_load(files['adapters/toy/knowledge-coverage.yaml'])
    assert [row['id'] for row in catalog['features']] == ['engine-step']
    assert compact['feature_ids'] == ['engine-step']
    assert compact['facet_denominator'] == 7
    candidates = {row['id']: row for row in _full_report(record)['candidates']}
    assert set(candidates) == set(RELATIONSHIPS)
    for key, (relation, _) in RELATIONSHIPS.items():
        row = candidates[key]
        assert row['relation'] == relation
        assert row['status'] == ('outdated' if relation == 'outdated' else 'accepted')
        for receipt in row['generator_receipts'] + [row['judge_receipt']]:
            archived = store.get(receipt['trace_id'])
            assert not archived['error']
            assert archived['outputs']['reply'] == 'sha256:' + receipt['reply_sha256']
    calls = len(script.calls)
    resumed = _run(runtime, lifecycle)
    assert resumed.status == 'dry_run' and len(script.calls) == calls
    assert _report(resumed)[0] == files


def test_independent_boundary_rejection_excludes_primary_approved_outside_claim(world):
    script = DiscoveryScript(boundary_exclusion=True)
    runtime, lifecycle, _, store = _setup(world, script=script)
    record = _run(runtime, lifecycle)
    assert record.status == 'dry_run', (record.problems, record.unfinished)
    assert record.discovery['done']
    primary = record.discovery['reviews']['outside-step']
    boundary = record.discovery['boundary_reviews']['outside-step']
    assert primary['supported'] == 'yes' and boundary['supported'] == 'no'
    assert primary['judge_receipt']['trace_id'] != boundary['judge_receipt']['trace_id']
    rows = {row['id']:row for row in _full_report(record)['candidates']}
    assert rows['engine-step']['status'] == 'accepted'
    assert rows['outside-step']['status'] == 'unknown'
    files, compact, _ = _report(record)
    catalog = yaml.safe_load(files['adapters/toy/knowledge-coverage.yaml'])
    assert [row['id'] for row in catalog['features']] == ['engine-step']
    assert compact['facet_denominator'] == 7
    assert all(not call['payload'].get('repair_reason') for call in script.calls if call['role']=='generator')
    for decision in (primary, boundary):
        receipt=decision['judge_receipt'];archived=store.get(receipt['trace_id'])
        assert not archived['error'] and archived['outputs']['reply']=='sha256:'+receipt['reply_sha256']
    calls = len(script.calls)
    resumed = _run(runtime, lifecycle)
    assert resumed.status=='dry_run' and len(script.calls)==calls and _report(resumed)[0]==files
