"""Durable structured calls delegate to the canonical native call workflow."""
import hashlib
import json
import time
from dataclasses import asdict, replace
from types import SimpleNamespace

from ..trace_store import redact
from ..sandbox.base import SandboxPending
from .schema_projection import schema_inputs
from .completion import complete_native
from .completion_profiles import normalize_completion_profiles
from .registry import transport_for_id
from .original_execution import canonical, run_original_native


def execute_readonly(prompt, *, provider, model, schema, config, root, idle, maximum,
                     effort='', frozen_inputs=None, validator=None, event_sink=None, settings=None,
                     session_request=None):
    """One original intent/result; repeated or unknown work never dispatches again."""
    from ..config import Settings
    from .json_session import decode_object, JSONSessionError
    import jsonschema

    jsonschema.Draft202012Validator.check_schema(schema)

    profiles = normalize_completion_profiles({provider: config})
    session = None
    if session_request is not None:
        if settings is None or frozen_inputs is not None:
            raise ValueError('scoped structured sessions require settings and own their frozen inputs')
        if session_request.output_schema is not None and session_request.output_schema != schema:
            raise ValueError('structured session schema differs from its readonly request')
        if 'session_schema' not in transport_for_id(settings, provider).spec.capabilities:
            raise ValueError(f'{provider} does not support structured agent sessions')
        from .session_inputs import freeze_session
        session = freeze_session(replace(session_request, prompt=prompt, model=model, effort=effort,
            timeout_s=maximum, idle_timeout_s=idle, output_schema=schema), knowledge_dir=settings.knowledge_dir)
        frozen_inputs, prompt = session.frozen_inputs, session.prompt
    inputs = schema_inputs(frozen_inputs, schema)
    identity = {'provider': provider, 'model': model, 'effort': effort, 'profile': profiles[provider],
                'prompt': prompt, 'schema': schema, 'idle': idle, 'maximum': maximum,
                'inputs_sha256': hashlib.sha256(canonical(inputs)).hexdigest()}
    if session is not None:
        identity['session_scope'] = {**asdict(session.scope), 'allowed_tools': sorted(session.scope.allowed_tools)}
        identity['session_system'] = session.system
    system = 'Read-only structured execution. Frozen input identity: ' + identity['inputs_sha256']
    if any(name.startswith('/tmp/inputs/repository/') for name in inputs['files']):
        system += '\nThe exact read-only source checkout is /tmp/inputs/repository.'
    if session is not None:
        system += '\n' + session.system
        session = replace(session, system=system, prompt=prompt, frozen_inputs=inputs)
    expected_inputs = {name: 'sha256:' + hashlib.sha256(redact(value).encode()).hexdigest()
                       for name, value in {'system': system, 'prompt': prompt}.items()}
    def validate(reply):
        import jsonschema
        if getattr(reply, 'stop_reason', None) == 'truncated':
            raise JSONSessionError('structured execution exceeded maximum runtime',
                                   failure_class='invalid_result', timeout=True)
        payload = decode_object(reply.text)
        try:
            jsonschema.validate(payload, schema)
            reason = validator(payload) if validator else None
            if reason:
                raise ValueError(reason)
        except (ValueError, jsonschema.ValidationError) as exc:
            raise JSONSessionError('invalid structured result: ' + str(exc), failure_class='invalid_result') from exc
        return payload

    def project(record, store, saved=None):
        attempt = store.validate_native_attempt(record)
        if (record['inputs'] != expected_inputs or record['model'].get('provider') != provider
                or record['model'].get('model') != model or record['model'].get('effort', '') != effort):
            raise ValueError('original readonly receipt differs from its input identity')
        text = store.blob(record['outputs']['reply'])
        payload = validate(SimpleNamespace(text=text, stop_reason='truncated'
            if (record['result'].get('session_outcome') or {}).get('truncated') else 'end_turn'))
        from .base import json_events
        adapter = transport_for_id(SimpleNamespace(strict_backend_model=model), provider)
        native = [event['payload'] for event in json_events(store.blob(record['outputs']['native_events']))
                  if event.get('type') == 'native.codex.event']
        decoded = [adapter.decode_event(event) for event in native]
        if event_sink:
            for event in native:
                event_sink(event)
        return {'payload': payload, 'text': text, 'stdout': '\n'.join(json.dumps(event) for event in native),
            'session_id': next((event.session_id for event in reversed(decoded) if event.session_id), None),
            'served_model': attempt['served_model'] or None,
            'usage': next((event.usage for event in reversed(decoded) if event.usage is not None), None),
            'accounting_usage': record['usage'], 'trace_id': record['id'], 'native_attempt_id': attempt['id']}

    def dispatch(recorder, store):
        options = dict(trace_store_root=str(store.root),
                       strict_backend_model=model, strict_backend_timeout_s=maximum)
        options['native_session_profiles' if session is not None else 'native_completion_profiles'] = profiles
        selected = settings.model_copy(update=options) if settings is not None else Settings(_env_file=None, **options)
        request = {'system': system, 'messages': [{'role': 'user', 'content': prompt}], 'model': model,
                   'effort': effort, 'readonly': True, 'output_schema': schema, 'frozen_inputs': inputs,
                   'idle_timeout_s': idle, 'absolute_deadline': time.monotonic() + maximum}
        _, _, _, receipt = complete_native(lambda: transport_for_id(selected, provider),
            request=request, session_request=replace(session, absolute_deadline=request['absolute_deadline'])
                if session is not None else None,
            identity={'provider': provider}, payload={'system': system, 'prompt': prompt},
            recorder=recorder, validate=validate, settings=selected,
            passthrough_errors=(SandboxPending, TimeoutError, JSONSessionError),
            make_error=lambda message, allow_fallback: JSONSessionError(message))
        return receipt

    return run_original_native(root=root, name='readonly', identity=identity,
                               dispatch=dispatch, project=project)
