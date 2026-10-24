"""Keep role admission and qualified served identity policies distinct."""
import pytest

from infermatrix_copilot.kb_service.model_identity import model_family
from infermatrix_copilot.kb_service.maintenance_audit import model_family as maintenance_family
from infermatrix_copilot.kb_service.native_depth_audit import _family as depth_family


@pytest.mark.parametrize('name,role,served', [
    ('GPT-6-sol', 'gpt', 'gpt'),
    ('GLM-5.3', 'glm', 'glm'),
    ('vendor/GPT-6-sol', 'vendor/gpt', 'gpt'),
    ('codex:GPT-6-sol', 'codex:gpt', 'gpt'),
    ('vendor/GLM_5.3', 'vendor/glm_5.3', 'glm'),
    ('', '', ''),
])
def test_admission_and_native_served_policies_retain_their_contracts(name, role, served):
    assert model_family(name) == depth_family(name) == role
    assert model_family(name, policy='served') == maintenance_family(name) == served


def test_unknown_identity_policy_cannot_silently_change_admission():
    with pytest.raises(ValueError, match='identity policy'):
        model_family('gpt-6-sol', policy='other')
