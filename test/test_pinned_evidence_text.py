from dataclasses import replace
from types import SimpleNamespace

import pytest

from infermatrix_copilot.knowledge_service.facts import FactsError
from infermatrix_copilot.knowledge_service.pinned_claims import evidence_for, evidence_text, check_evidence


def test_materialization_and_validation_share_range_and_hash_rules():
    source = {'code.py': 'first\r\nsecond\r\n'}
    observer = SimpleNamespace(head=lambda: 'a'*40, file_text=lambda pin,path: source.get(path))
    entry = evidence_for(observer, 'code.py', 1, 2)
    assert evidence_text(observer, entry) == 'first\nsecond'
    source['code.py'] = 'changed\nsecond\n'
    issue, = check_evidence([entry], observer)
    with pytest.raises(FactsError, match='content differs') as exc:
        evidence_text(observer, entry)
    assert str(exc.value) == issue
    # Harvest reads newer source intentionally; it does not assert the older hash.
    assert evidence_text(observer, entry, verify_hash=False) == 'changed\nsecond'
    assert evidence_text(observer, replace(entry,end=9), verify_hash=False) is None
    assert evidence_text(observer, replace(entry,path='missing'), verify_hash=False) is None


@pytest.mark.parametrize('verify_hash',[False,True])
def test_git_read_failures_are_not_treated_as_absent_context(verify_hash):
    def broken(pin,path): raise FactsError('cannot read original Git object')
    observer = SimpleNamespace(head=lambda: 'a'*40, file_text=broken)
    from infermatrix_copilot.knowledge_service.pinned_claims import Evidence
    with pytest.raises(FactsError, match='cannot read original Git object'):
        evidence_text(observer,Evidence('code.py',1,1,'0'*64),verify_hash=verify_hash)
