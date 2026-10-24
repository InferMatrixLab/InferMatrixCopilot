from copy import deepcopy
import pytest
from ledger import Refused
from role_profiles import roles, role, native_plan


def plan():
    return {'execution_profile': {'model': 'judge-model', 'reasoning_effort': 'high',
        'config_overrides': ['model_reasoning_effort="high"'], 'roles': {
            'generator': {'provider': 'codex', 'model': 'extract-model', 'effort': 'medium'},
            'judge': {'provider': 'codex', 'model': 'judge-model', 'effort': 'high'}}}}


def test_native_role_selection_preserves_original_plan_and_account_identity():
    original=plan();before=deepcopy(original)
    selected=native_plan(original,['exec','--model','extract-model','-'])
    assert original==before
    assert selected['execution_profile']['model']=='extract-model'
    assert selected['execution_profile']['reasoning_effort']=='medium'
    assert selected['execution_profile']['config_overrides']==['model_reasoning_effort="medium"']
    assert role(original,'judge')['model']=='judge-model'


@pytest.mark.parametrize('arguments',[['exec','-'],['exec','--model','unknown','-'],
    ['exec','--model','extract-model','-m','judge-model','-']])
def test_unplanned_or_ambiguous_native_identity_refuses(arguments):
    with pytest.raises(Refused):native_plan(plan(),arguments)


@pytest.mark.parametrize('change',['provider','role','effort'])
def test_invalid_roles_refuse_before_preparation(change):
    value=plan();profiles=value['execution_profile']['roles']
    if change=='provider':profiles['generator']['provider']='unknown-provider'
    elif change=='role':profiles['unplanned']=profiles.pop('generator')
    else:profiles['generator']['effort']=None
    with pytest.raises((Refused,ValueError)):roles(value)


def test_mixed_role_codec_preserves_empty_provider_effort_and_refuses_codex_translation():
    value = plan()
    value['execution_profile']['roles']['generator'] = {'provider': 'zcode', 'model': 'GLM-5.3', 'effort': ''}
    assert role(value, 'generator')['effort'] == ''
    with pytest.raises(Refused, match='another provider'):
        native_plan(value, ['exec', '-m', 'GLM-5.3', '-'])



def test_identical_model_roles_share_execution_config_without_merging_roles():
    original = plan()
    original['execution_profile']['roles']['generator'] = deepcopy(original['execution_profile']['roles']['judge'])
    before = deepcopy(original)
    selected = native_plan(original, ['exec', '-m', 'judge-model', '-'])
    assert original == before
    assert selected['execution_profile']['reasoning_effort'] == 'high'
    assert set(roles(selected)) == {'generator', 'judge'}


@pytest.mark.parametrize('field,value', [('effort','medium'), ('provider','zcode')])
def test_same_model_with_conflicting_execution_identity_still_refuses(field, value):
    original = plan()
    original['execution_profile']['roles']['generator'] = deepcopy(original['execution_profile']['roles']['judge'])
    original['execution_profile']['roles']['generator'][field] = value
    with pytest.raises(Refused, match='execution profile'):
        native_plan(original, ['exec', '-m', 'judge-model', '-'])


def test_explicit_auxiliary_binding_preserves_primary_pins_and_unknown_refusal():
    plan = {'execution_profile': {'roles': {
        'generator': {'provider': 'codex', 'model': 'gpt-6-sol', 'effort': 'high'},
        'judge': {'provider': 'codex', 'model': 'gpt-6-sol', 'effort': 'high'}},
        'role_bindings': {'pr-reviewer': 'judge'}}}
    from role_profiles import role
    assert role(plan, 'pr-reviewer') == role(plan, 'judge')
    assert set(roles(plan)) == {'generator', 'judge'}
    with pytest.raises(Refused, match='unplanned model role'):
        role(plan, 'other-reviewer')
    plan['execution_profile']['role_bindings']['judge'] = 'generator'
    with pytest.raises(Refused, match='auxiliary role bindings'):
        roles(plan)


def test_step_name_binding_is_explicit_and_case_kind_scoped():
    from role_profiles import invocation_role, role
    plan={'native_call_limits':{'strict':1,'history':1},'execution_profile':{'roles':{
        'generator':{'provider':'codex','model':'gpt-6-sol','effort':'high'},
        'judge':{'provider':'codex','model':'gpt-6-sol','effort':'high'}},
        'invocation_roles_by_case':{'strict':{'harness_session':'judge'}}}}
    assert invocation_role(plan,name='reviewer.delegate-7',kind='harness_session',case_id='strict')==role(plan,'judge')
    for case,kind in [('history','harness_session'),('strict','harness')]:
        with pytest.raises(Refused,match='unplanned model role'):
            invocation_role(plan,name='reviewer.delegate-7',kind=kind,case_id=case)
