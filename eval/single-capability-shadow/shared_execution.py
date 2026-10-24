"""Translate frozen legacy requests into the canonical native/sandbox workflow."""
import hashlib
import json
from pathlib import Path
import time

from infermatrix_copilot.config import Settings
from infermatrix_copilot.persistence import immutable_write_bytes
from infermatrix_copilot.providers.base import AgentSessionRequest
from infermatrix_copilot.providers.completion_profiles import (normalize_completion_profiles,
    parse_completion_config, sandbox_profile_for_config, configured_credential_source)
from infermatrix_copilot.providers.hosted_execution import execute_native_process
from infermatrix_copilot.providers.registry import transport_for_id
from infermatrix_copilot.providers.session_inputs import freeze_session, snapshot_source, map_prompt_paths, SOURCE
from infermatrix_copilot.tool_bridge import load_bridge_spec

from ledger import Refused


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def shared_profiles(plan):
    """Keep v1 pins intact; v2 binds each provider's configuration and binary."""
    value = plan.get('shared_execution')
    common = {'schema', 'source_roots_by_arm', 'knowledge_by_arm'}
    if not isinstance(value, dict):
        raise Refused('freeze a shared sandbox execution profile')
    schema = value.get('schema')
    fields = common | ({'profile'} if schema == 'shared-sandbox/1' else {'profiles'})
    if (schema not in {'shared-sandbox/1', 'shared-sandbox/2'} or not fields <= set(value)
            or set(value) - fields - {'schema_files_by_arm'}):
        raise Refused('invalid frozen shared sandbox profile')
    profiles = {'codex': value['profile']} if schema == 'shared-sandbox/1' else value['profiles']
    try:
        result = normalize_completion_profiles(profiles)
    except (ValueError, TypeError) as exc:
        raise Refused('invalid frozen shared execution profile') from exc
    if not result:
        raise Refused('freeze at least one native provider')
    if schema == 'shared-sandbox/2':
        from role_profiles import roles, fixture_providers
        if {row['provider'] for row in roles(plan).values()} != set(result) | fixture_providers(plan):
            raise Refused('native profiles differ from frozen role providers')
        hashes = plan.get('provider_binary_sha256_by_provider')
        if (not isinstance(hashes, dict) or set(hashes) != set(result)
                or any(not isinstance(h, str) or len(h) != 64
                       or set(h) - set('0123456789abcdef') for h in hashes.values())):
            raise Refused('freeze every native provider binary identity')
        if plan.get('execution_kind') == 'live':
            from infermatrix_copilot.providers.provider_options import option_fields
            from infermatrix_copilot.providers.registry import _construct_transport
            for provider, profile in result.items():
                options = profile.get('provider_options', {})
                if (set(options) != option_fields(_construct_transport(None, provider))
                        or any(not isinstance(value, str) or not value for value in options.values())):
                    raise Refused('live mixed execution requires explicit frozen provider options')
    return result


def native_deadline(plan):
    value = plan.get('native_timeout_s', 180)
    maximum = 600 if 'case_deadlines' in plan else 180
    if type(value) not in (int, float) or not 0 < value <= maximum:
        raise Refused(f'native deadline must be positive and at most {maximum} seconds')
    return value


def host_deadline(plan):
    value = plan.get('host_timeout_s', plan.get('native_timeout_s', 180))
    if type(value) not in (int, float) or not plan.get('native_timeout_s', 180) <= value <= 7200:
        raise Refused('freeze an orchestration deadline covering the native deadline')
    return value


def workflow_deadline(plan, *, case_id, require_explicit=False):
    host = host_deadline(plan)
    value = plan.get('workflow_timeout_s')
    if value is None and not require_explicit:
        return host + (20 if case_id == 'direct' else 30)
    calls = plan.get('native_call_limits', {}).get(case_id, 1)
    if (type(calls) is not int or calls < 1
            or type(value) not in (int, float)
            or not calls * host + 30 <= value <= 7200):
        raise Refused('freeze a whole-workflow deadline covering all declared review calls')
    return value


def plan_for_case(plan, case_id):
    """Select host-frozen inputs/policies without changing ledger authority.

    Original plans return unchanged; only new manifests can declare case-specific
    policies. Each caller still uses the same registered provider and normalizer.
    """
    table = plan.get('native_profiles_by_case')
    knowledge_declared = 'native_knowledge_by_case' in plan
    knowledge = plan.get('native_knowledge_by_case')
    deadlines_declared = 'case_deadlines' in plan
    deadlines = plan.get('case_deadlines')
    bridges_declared = 'scoped_bridge_by_case' in plan
    bridges = plan.get('scoped_bridge_by_case')
    if table is None and not knowledge_declared and not deadlines_declared and not bridges_declared:
        return plan
    limits = plan.get('native_call_limits')
    if not isinstance(limits, dict) or case_id not in limits:
        raise Refused('freeze case policies against the native call plan')
    if table is not None and (not isinstance(table, dict) or set(table) != set(limits)
            or plan.get('shared_execution', {}).get('schema') != 'shared-sandbox/2'):
        raise Refused('freeze one native profile set for every case')
    if knowledge_declared and (not isinstance(knowledge, dict) or set(knowledge) != set(limits)
            or any(not isinstance(arms, dict) or set(arms) != {'baseline', 'candidate'}
                   or any(not isinstance(path, str) or not Path(path).is_absolute()
                          for path in arms.values()) for arms in knowledge.values())):
        raise Refused('freeze explicit absolute knowledge paths for both arms of every case')
    if deadlines_declared and (not isinstance(deadlines, dict) or set(deadlines) != set(limits)
            or any(not isinstance(row, dict) or set(row) != {
                'native_timeout_s', 'host_timeout_s', 'workflow_timeout_s'} for row in deadlines.values())):
        raise Refused('freeze native, host and workflow deadlines for every case')
    if bridges_declared and (not isinstance(bridges, dict) or set(bridges) - set(limits)
            or any(not isinstance(arms, dict) or not arms or set(arms) - {'baseline', 'candidate'}
                   or any(not isinstance(row, dict) or set(row) != {'repo'}
                          or not isinstance(row['repo'], str) or not row['repo']
                          or any(c in row['repo'] for c in '\0\r\n') for row in arms.values())
                   for arms in bridges.values())):
        raise Refused('freeze explicit scoped bridge repositories and arms for planned cases')
    from copy import deepcopy
    selected = None
    for case in limits:
        candidate = deepcopy(plan)
        if table is not None:
            candidate['shared_execution']['profiles'] = deepcopy(table[case])
        if knowledge_declared:
            candidate['shared_execution']['knowledge_by_arm'] = dict(knowledge[case])
        if deadlines_declared:
            candidate.update(deadlines[case])
            native_deadline(candidate)
            host_deadline(candidate)
            workflow_deadline(candidate, case_id=case, require_explicit=True)
        if bridges_declared:
            candidate['execution_profile']['scoped_bridge_by_arm'] = deepcopy(bridges.get(case, {}))
        # All cases are validated at admission, including dormant overrides.
        shared_profiles(candidate)
        if case == case_id:
            selected = candidate
    return selected


def provider_binary_hash(plan, provider):
    if plan.get('shared_execution', {}).get('schema', 'shared-sandbox/1') == 'shared-sandbox/1' and provider == 'codex':
        return plan['provider_binary_sha256']
    return plan['provider_binary_sha256_by_provider'][provider]


def validate_shared(plan, arm, provider='codex'):
    value = plan.get('shared_execution')
    profiles = shared_profiles(plan)
    if provider not in profiles or arm not in ('baseline', 'candidate'):
        raise Refused('unplanned native provider or arm')
    profile = profiles[provider]
    if not isinstance(value['source_roots_by_arm'], dict):
        raise Refused('freeze per-arm source roots')
    roots = value['source_roots_by_arm'].get(arm)
    if (not isinstance(roots, list) or not roots or any(not isinstance(path, str) or not Path(path).is_absolute() for path in roots)
            or not isinstance(value['knowledge_by_arm'], dict)):
        raise Refused('freeze each arm source roots and knowledge snapshot')
    schemas = value.get('schema_files_by_arm', {})
    if (not isinstance(schemas, dict) or set(schemas) - {'baseline', 'candidate'}
            or any(not isinstance(files, dict) or any(
                not isinstance(path, str) or not Path(path).is_absolute()
                or not isinstance(sha, str) or len(sha) != 64 or set(sha) - set('0123456789abcdef')
                for path, sha in files.items()) for files in schemas.values())):
        raise Refused('freeze exact SDK schema paths and SHA-256 hashes per arm')
    return profile, [Path(path).resolve() for path in roots], value['knowledge_by_arm'].get(arm)


def contained(path, roots, *, exists=True):
    path = Path(path).resolve(strict=exists)
    if not any(path == root or root in path.parents for root in roots):
        raise Refused('native input escapes its frozen arm workspace')
    return path


def native_cwd(args, cwd):
    result = Path(cwd)
    for index, arg in enumerate(args):
        key, separator, value = arg.partition('=')
        if key in ('-C', '--cd'):
            result = Path(cwd) / (value if separator else args[index + 1])
    return result


def validate_shared_request(plan, arm, args, cwd, schema, output=None):
    profile, roots, knowledge = validate_shared(plan, arm)
    from infermatrix_copilot.providers.codex_policy import validate_effort
    try:
        validate_effort(plan['execution_profile']['reasoning_effort'])
    except ValueError as exc:
        raise Refused('unsupported frozen native effort') from exc
    source = contained(native_cwd(args, cwd), roots)
    if not source.is_dir():
        raise Refused('native source must be a frozen directory')
    if schema is not None:
        resolved = Path(schema).resolve(strict=True)
        if not any(resolved == root or root in resolved.parents for root in roots):
            expected = plan['shared_execution'].get('schema_files_by_arm', {}).get(arm, {}).get(str(resolved))
            if expected != hashlib.sha256(resolved.read_bytes()).hexdigest():
                raise Refused('native schema is outside the workspace and not an exact frozen SDK resource')
        import jsonschema
        jsonschema.Draft202012Validator.check_schema(json.loads(schema.read_text()))
    if output is not None:
        contained(output, roots, exists=False)
        if not output.parent.is_dir():
            raise Refused('native output parent is unavailable')
    return profile, source, knowledge


def configured_transport(plan, profile, root):
    """Construct only the canonical registered adapter with the frozen settings."""
    normalized = plan['execution_profile']
    model, effort = normalized['model'], normalized['reasoning_effort']
    timeout = native_deadline(plan)
    # Frozen profiles must not recursively merge ambient BaseSettings maps.
    # Use the canonical compiled Settings rules without reloading host sources.
    settings = Settings.model_construct()
    Settings.__pydantic_validator__.validate_python({
        'strict_backend': 'codex', 'strict_backend_model': model,
        'strict_backend_timeout_s': timeout, 'metrics_enabled': False,
        'native_completion_profiles': {'codex': profile},
        'native_session_profiles': {'codex': profile},
        'trace_store_root': str(root / 'call' / 'traces')}, self_instance=settings)
    return transport_for_id(settings, 'codex')


def prepared_transport(plan, profile, root):
    """One canonical adapter/configuration/identity preparation for controls and calls."""
    transport = configured_transport(plan, profile, root)
    # This zero-model control proves that the selected sandbox contains the
    # manifest-bound native executable, then binds the actual execution context.
    probe_profile = sandbox_profile_for_config(parse_completion_config(profile), root / 'identity')
    proof = transport.identity_probe(probe_profile)
    if proof['sha256'] != provider_binary_hash(plan, 'codex'):
        raise Refused('sandbox native binary differs from the frozen provider identity')
    immutable_write_bytes(root / 'identity-proof.json', canonical(proof))
    transport.required_execution_context = proof['context']
    return transport, proof


def execute_shared(*, plan, arm, args, cwd, prompt, bridge, schema, root, native_observer=None, hidden_paths=()):
    """No vendor process ownership here: inputs and results cross shared owners."""
    if 'roles' in plan.get('execution_profile', {}):
        from role_profiles import native_plan
        plan = native_plan(plan, args)
    profile, source, knowledge = validate_shared_request(plan, arm, args, cwd, schema)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    generated_bridge = plan['execution_profile'].get('scoped_bridge_by_arm', {}).get(arm)
    if schema is not None and (bridge is not None or generated_bridge is not None):
        from infermatrix_copilot.providers.registry import PROVIDERS
        if 'session_schema' not in PROVIDERS['codex'].capabilities:
            raise Refused('structured bridge execution requires the declared session schema capability')
    if generated_bridge is not None:
        if bridge is not None:
            raise Refused('explicit CLI bridge conflicts with the frozen host bridge policy')
        if knowledge is None or not Path(knowledge).is_absolute():
            raise Refused('scoped bridge requires the frozen knowledge snapshot')
        from dataclasses import replace
        from infermatrix_copilot.scopes import read_only_scope
        from infermatrix_copilot.tool_bridge import write_bridge_spec, READ_ONLY_BRIDGE_TOOLS
        frame = root / 'bridge-frame';frame.mkdir(mode=0o700, exist_ok=True)
        scope = replace(read_only_scope('shadow-structured'), root=str(source),
            allowed_tools=READ_ONLY_BRIDGE_TOOLS, strict_extras=True,
            read_roots=(str(source), str(frame), str(Path(knowledge).resolve())))
        path = write_bridge_spec(run_dir=frame, step_name='shadow-structured', scope=scope,
                                 repo=generated_bridge['repo'], knowledge_dir=knowledge)
        bridge = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    transport, proof = prepared_transport(plan, profile, root)
    model, effort = plan['execution_profile']['model'], plan['execution_profile']['reasoning_effort']
    timeout = native_deadline(plan)
    request = {'system': prompt, 'messages': [], 'model': model, 'effort': effort,
        'role': 'shadow-native', 'readonly': True, 'absolute_deadline': time.monotonic() + timeout}
    session = None
    if bridge:
        if generated_bridge is None and '\n\n' not in prompt:
            raise Refused('unsupported legacy bridge prompt/schema contract')
        scope, spec = load_bridge_spec(Path(bridge['path']))
        if Path(scope.root).resolve() != source:
            raise Refused('bridge source differs from the frozen native cwd')
        if knowledge is None or not Path(knowledge).is_absolute():
            raise Refused('agent exchange requires a frozen knowledge snapshot')
        system, user = (('Read-only structured execution through the scoped tool bridge.', prompt)
                        if generated_bridge is not None else prompt.split('\n\n', 1))
        session = AgentSessionRequest(system=system, prompt=user, scope=scope, model=model,
            max_iters=1, timeout_s=timeout, run_dir=Path(spec['run_dir']), step_name='shadow-native',
            bridge_spec_path=Path(bridge['path']), absolute_deadline=request['absolute_deadline'], effort=effort,
            **({'output_schema': json.loads(schema.read_text())} if schema is not None else {}))
        session = freeze_session(session, knowledge_dir=knowledge)
        request.update(system=session.system, messages=[{'role': 'user', 'content': session.prompt}],
                       frozen_inputs=session.frozen_inputs)
        if schema is not None:
            request['output_schema'] = session.output_schema
        archived_prompt = session.prompt
    else:
        # State is outside the model's source snapshot. Completion scratch
        # directories and review checkouts both use the canonical byte codec.
        exclusions = []
        for candidate in (root, root.parent, *map(Path, hidden_paths)):
            if source in candidate.parents:
                exclusions.append(candidate.relative_to(source).as_posix())
        request['frozen_inputs'] = snapshot_source(source, tuple(exclusions))
        request['system'] = map_prompt_paths(prompt, {source: SOURCE})
        if schema is not None:
            request['output_schema'] = json.loads(schema.read_text())
        archived_prompt = ''
    # Legacy subscription generation also dispatches through the canonical
    # admission. A completion admission never authorizes an agent session.
    config = parse_completion_config(profile)
    if not bridge and configured_credential_source(config) is not None:
        from infermatrix_copilot.providers.subscription_admission import admit_subscription
        from infermatrix_copilot.sandbox.context import verify_context
        admitted = admit_subscription(transport.settings, transport)
        if admitted is None:
            raise Refused('frozen completion account is not an authenticated subscription')
        verify_context(proof['context'], admitted.configured_subscription_admission['context'])
        transport = admitted
    identity = {'prompt': archived_prompt, 'arm': arm, 'profile': profile,
        'native_binary_sha256': proof['sha256'], 'execution_context': proof['context'],
        'frozen_inputs_sha256': hashlib.sha256(canonical(request['frozen_inputs'])).hexdigest()}
    if bridge and schema is not None:
        identity['output_schema_sha256'] = hashlib.sha256(canonical(request['output_schema'])).hexdigest()
    receipt = execute_native_process(transport, root=root / 'call', identity=identity,
        request=request, session_request=session, native_observer=native_observer)
    if generated_bridge is not None:
        receipt['scoped_bridge_sha256'] = bridge['sha256']
    return receipt


def subscription_control(*, plan, arm, root):
    """Project canonical auth proof for legacy login deadlines; never admit a model.

    A completed zero-model preflight can be read again through the canonical
    proof validator. Actual model calls still obtain a fresh admission and bind
    the current execution context in execute_shared / complete_native.
    """
    from infermatrix_copilot.providers.subscription_admission import admit_subscription, admitted_context
    from infermatrix_copilot.sandbox.context import verify_context
    from uuid import uuid4
    profile, roots, _ = validate_shared(plan, arm)
    root = contained(root, roots, exists=False)
    marker = root / 'completed-control.json'
    if marker.exists():
        if marker.is_symlink():
            raise Refused('original subscription control cannot be replaced')
        saved = json.loads(marker.read_bytes())
        if (set(saved) != {'binary_sha256', 'admission'}
                or saved['binary_sha256'] != provider_binary_hash(plan, 'codex')):
            raise Refused('subscription control identity differs from the frozen provider')
        store = contained(saved['admission']['store_root'], [root])
        transport = configured_transport(plan, profile, store.parent.parent)
        transport.configured_subscription_admission = saved['admission']
        config = parse_completion_config(profile)
        return admitted_context(transport.settings, transport, config, store_root=store) is not None
    attempt = root / uuid4().hex
    attempt.mkdir(parents=True, exist_ok=False, mode=0o700)
    transport, proof = prepared_transport(plan, profile, attempt)
    admitted = admit_subscription(transport.settings, transport)
    if admitted is None:
        return False
    verify_context(proof['context'], admitted.configured_subscription_admission['context'])
    immutable_write_bytes(marker, canonical({'binary_sha256': proof['sha256'],
        'admission': admitted.configured_subscription_admission}), exist_ok=True)
    return True
