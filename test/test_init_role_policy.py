"""Public planning policy agrees with the actual initialization gates."""
import pytest

from infermatrix_copilot.sdk.v1 import knowledge_init_role_error


def policy(stage='feature-discovery', **overrides):
    return knowledge_init_role_error(stage, **{
        'generator_provider': 'codex', 'generator_model': 'gpt-6-sol',
        'judge_provider': 'codex', 'judge_model': 'gpt-6.1-sol', **overrides})


@pytest.mark.parametrize('provider', ['codex', 'other'])
def test_discovery_refuses_shared_family_even_between_providers(provider):
    assert 'independent model families' in policy(generator_provider=provider)


def test_discovery_accepts_distinct_families_and_capped_knowledge_keeps_its_policy():
    assert policy(generator_provider='zcode', generator_model='GLM-5.3') is None
    assert policy('knowledge') is None


@pytest.mark.parametrize('role', ['generator', 'judge'])
def test_unlimited_fallback_refusal_keeps_precedence(role):
    assert 'without fallback' in policy('knowledge', unlimited_subscription=True,
                                       **{role + '_has_fallback': True})


def test_unlimited_preserves_its_explicit_pair_requirement():
    assert 'Zcode GLM-5.3' in policy('knowledge', unlimited_subscription=True)
    assert policy('knowledge', generator_provider='zcode', generator_model='GLM-5.3',
                  unlimited_subscription=True) is None
