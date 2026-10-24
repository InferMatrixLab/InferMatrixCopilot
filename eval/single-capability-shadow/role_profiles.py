"""Frozen evaluation model roles; native execution remains in the provider."""
from copy import deepcopy
from ledger import Refused


def roles(plan):
    from infermatrix_copilot.providers.registry import PROVIDERS
    configured = plan['execution_profile'].get('roles')
    if (not isinstance(configured, dict) or set(configured) != {'generator', 'judge'}
            or any(not isinstance(row, dict) or set(row) != {'provider', 'model', 'effort'}
                or not isinstance(row['provider'], str) or row['provider'] not in PROVIDERS
                or PROVIDERS[row['provider']].kind != 'harness'
                or not isinstance(row['model'], str) or not row['model']
                or not isinstance(row['effort'], str) for row in configured.values())):
        raise Refused('freeze supported generator and judge model roles')
    aliases = plan['execution_profile'].get('role_bindings', {})
    if (not isinstance(aliases, dict)
            or any(not isinstance(name, str) or not name or name in configured
                   or not isinstance(target, str) or target not in configured
                   for name, target in aliases.items())):
        raise Refused('freeze auxiliary role bindings to primary generator/judge profiles')
    invocation_roles = plan['execution_profile'].get('invocation_roles_by_case', {})
    if (not isinstance(invocation_roles, dict)
            or any(case not in plan.get('native_call_limits', {}) or not isinstance(table, dict)
                   or not table or set(table) - {'harness', 'harness_session'}
                   or any(not isinstance(target, str) or target not in configured for target in table.values())
                   for case, table in invocation_roles.items())):
        raise Refused('freeze invocation roles for declared cases and native kinds')
    # Vendor policy is validated by the canonical provider before dispatch.
    # This codec also loads under SDKs predating that policy module.
    return deepcopy(configured)


def role(plan, name):
    configured = roles(plan)
    selected = plan['execution_profile'].get('role_bindings', {}).get(name, name)
    if selected not in configured:
        raise Refused('unplanned model role')
    return configured[selected]


def invocation_role(plan, *, name, kind, case_id):
    """Native step names remain telemetry; the host selects their fixed profile."""
    roles(plan)  # Validate explicit case/kind bindings before model preparation.
    selected = plan['execution_profile'].get('invocation_roles_by_case', {}).get(case_id, {}).get(kind, name)
    return role(plan, selected)


def native_plan(plan, args):
    """Legacy CLI translation selects exactly one frozen model; never substitutes."""
    models = []
    for i, arg in enumerate(args):
        if arg in ('--model', '-m'):
            if i + 1 >= len(args):
                raise Refused('missing model option')
            models.append(args[i + 1])
        elif arg.startswith(('--model=', '-m=')):
            models.append(arg.split('=', 1)[1])
    if len(models) != 1:
        raise Refused('role-bound native calls need one explicit model')
    matches = [row for row in roles(plan).values() if row['model'] == models[0]]
    # Logical roles may intentionally share exactly one execution configuration.
    if not matches or any(row != matches[0] for row in matches[1:]):
        raise Refused('native model has no unique frozen execution profile')
    selected = matches[0]
    if selected['provider'] != 'codex':
        raise Refused('legacy Codex arguments cannot dispatch another provider')
    value = deepcopy(plan)
    profile = value['execution_profile']
    profile.update(model=selected['model'], reasoning_effort=selected['effort'])
    profile['config_overrides'] = [arg for arg in profile['config_overrides']
                                    if not arg.startswith('model_reasoning_effort=')]
    import json
    profile['config_overrides'].append('model_reasoning_effort=' + json.dumps(selected['effort']))
    return value


def fixture_providers(plan):
    """An explicitly frozen fixture may simulate only its separate generator."""
    value = plan.get('scripted_providers', [])
    if value == []:
        return set()
    configured = roles(plan)
    generator = configured['generator']['provider']
    profiles = plan.get('shared_execution', {}).get('profiles', {})
    if (not isinstance(value, list) or value != [generator]
            or plan.get('execution_kind') != 'fixture'
            or generator == configured['judge']['provider'] or generator in profiles):
        raise Refused('only an unprofiled fixture generator may be scripted')
    return {generator}
