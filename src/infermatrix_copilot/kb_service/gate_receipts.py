"""Settle completed quality reviews without constructing a model runtime."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def settle_gate_result(ledger, state_dir: Path, owner: str, changeset_id: str):
    """Use the same persisted result for live settlement and explicit recovery.

    Recovery cannot invent missing results or recreate a generation. Prepared
    companion edits are staged atomically with their parent; publication is separate.
    """
    if not changeset_id or Path(changeset_id).name != changeset_id or changeset_id in {'.', '..'}:
        raise ValueError('invalid changeset identity')
    path = state_dir / 'gate-results' / f'{changeset_id}.json'
    draft = state_dir / 'changesets' / f'{changeset_id}.json'
    if path.is_symlink() or draft.is_symlink():
        raise ValueError('review receipt and draft must be regular local files')
    raw = path.read_bytes()
    receipt = json.loads(raw)
    if not isinstance(receipt, dict):
        raise ValueError('review receipt must be an object')
    row = ledger.changeset(changeset_id)
    if (receipt.get('version') != 1 or receipt.get('changeset_id') != changeset_id
            or receipt.get('repo') != row['repo'] or receipt.get('kind') != row['kind']
            or receipt.get('generation') != row['generation']
            or receipt.get('draft_sha256') != hashlib.sha256(draft.read_bytes()).hexdigest()):
        raise ValueError('review receipt does not bind the original changeset and draft')
    result = receipt['settlement']
    intent = receipt['intent_detail']
    if not isinstance(result, dict) or not isinstance(intent, dict) or not isinstance(result.get('detail'), dict):
        raise ValueError('invalid review receipt structure')
    decision = result['detail']['decision']
    if not isinstance(decision, dict):
        raise ValueError('invalid review decision')
    expected = {'pass': {'gated', 'sweep_held'}, 'fail': {'failed'}, 'human': {'human'}}
    if (result['kind'] != row['kind'] or result['expected_status'] != 'review_pending'
            or result['status'] not in expected.get(decision['status'], set())
            or result['drafted_events'] != intent['event_ids']
            or result['detail'] != {**intent, 'decision': decision}):
        raise ValueError('invalid review settlement binding')
    receipt_hash = hashlib.sha256(raw).hexdigest()
    if row['status'] != 'review_pending':
        if row['detail'].get('gate_result_sha256') == receipt_hash:
            return row  # committed, including a lost commit reply; no duplicate verdicts
        raise ValueError('changeset is not pending this review result')
    if row['detail'] != intent:
        raise ValueError('review intent changed')
    # A null prepared companion means there were no automatically rewritable
    # references. A missing field in an older receipt does not establish that.
    required = receipt.get('companion_required', bool(decision.get('external_refs')))
    if required and 'companion' not in receipt:
        raise ValueError('legacy review lacks prepared companion continuation')
    companion = receipt.get('companion')
    if companion is not None:
        from .companion import COMPANION_DIRS
        from ..knowledge_service.signing import canonical_json
        from ..persistence import immutable_write_bytes

        if not required or not isinstance(companion, dict) or result['status'] != 'human':
            raise ValueError('invalid prepared companion')
        child_id = companion['id']
        artifact = companion['artifact']
        refs = decision['external_refs']
        if (not isinstance(child_id, str) or Path(child_id).name != child_id
                or not child_id.startswith(row['repo'] + '-companion-')
                or child_id == changeset_id
                or companion['detail'] != {'for_changeset': changeset_id, 'base_sha': intent['base_sha'], 'refs': refs}
                or artifact['base_sha'] != intent['base_sha'] or artifact['deleted'] or artifact['evidence']
                or not isinstance(artifact['files'], dict) or not artifact['files']
                or not all(isinstance(p, str) and p.startswith(COMPANION_DIRS)
                           and p in {ref[1] for ref in refs} and isinstance(text, str)
                           for p, text in artifact['files'].items())):
            raise ValueError('prepared companion does not bind the original review')
        immutable_write_bytes(state_dir / 'changesets' / f'{child_id}.json',
                              canonical_json(artifact), exist_ok=True)
    if row['kind'] in {'sweep', 'purge'} and not all(
            intent.get(k) for k in ('sweep_instance', 'sweep_key', 'sweep_unit')):
        raise ValueError('legacy sweep lacks a recoverable unit binding')
    if row['kind'] in {'correction', 'maintenance_calibration'} and not intent.get('correction_request'):
        raise ValueError('legacy correction lacks a recoverable request binding')
    ledger.stage_intake(owner, row['repo'], changeset_id, kind=result['kind'],
        status=result['status'], verdicts=result['verdicts'], human_reason=result['human_reason'],
        drafted_events=result['drafted_events'], expected_status='review_pending',
        expected_generation=row['generation'], expected_detail=intent,
        detail={**result['detail'], 'gate_result_sha256': receipt_hash}, companion=companion)
    return ledger.changeset(changeset_id)
