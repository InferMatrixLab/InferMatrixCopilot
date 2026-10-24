"""Durable scoped agent sessions use the common native call and sandbox owners."""
from dataclasses import asdict, replace
import hashlib
import math
from pathlib import Path
import time

from ..agent_loop import AgentOutcome
from ..trace_store import redact
from .completion import complete_native
from .completion_profiles import normalize_completion_profiles
from .original_execution import canonical, run_original_native
from .session_inputs import freeze_session


def execute_session(transport, request):
    from ..sandbox.base import execution_controls
    execution_controls(idle_timeout_s=request.idle_timeout_s)
    if (not request.step_name or not math.isfinite(request.timeout_s) or request.timeout_s <= 0
            or request.absolute_deadline is not None and not math.isfinite(request.absolute_deadline)):
        raise ValueError('agent session requires an original step identity and finite deadline')
    settings, provider = transport.settings, transport.spec.id
    profiles = normalize_completion_profiles(settings.native_session_profiles)
    frozen = freeze_session(request, knowledge_dir=settings.knowledge_dir)
    identity = {'provider': provider, 'profile': profiles[provider], 'model': request.model,
        'effort': request.effort, 'step': request.step_name, 'system': request.system, 'prompt': request.prompt,
        'scope': {**asdict(request.scope), 'allowed_tools': sorted(request.scope.allowed_tools)},
        'max_iters': request.max_iters, 'timeout_s': request.timeout_s, 'idle_timeout_s': request.idle_timeout_s,
        'absolute_deadline': request.absolute_deadline,
        'inputs_sha256': hashlib.sha256(canonical(frozen.frozen_inputs)).hexdigest()}
    if getattr(request, 'output_schema', None) is not None:
        identity['output_schema'] = request.output_schema
    from ..execution_artifacts import native_session_dir
    root = native_session_dir(request.run_dir, request.step_name)
    expected_inputs = {name: 'sha256:' + hashlib.sha256(redact(value).encode()).hexdigest()
                       for name, value in {'system': frozen.system, 'prompt': frozen.prompt}.items()}

    def project(record, store, saved=None):
        store.validate_native_attempt(record)
        if (record['inputs'] != expected_inputs or record['model']['provider'] != provider
                or record['model']['model'] != request.model or record['model'].get('effort', '') != request.effort):
            raise ValueError('original agent receipt differs from its request identity')
        return asdict(AgentOutcome(text=store.blob(record['outputs']['reply']),
                                   **record['result']['session_outcome']))

    def dispatch(recorder, store):
        deadline = time.monotonic() + request.timeout_s
        original = replace(frozen, absolute_deadline=min(deadline, request.absolute_deadline)
                           if request.absolute_deadline is not None else deadline)
        _, _, _, receipt = complete_native(lambda: transport,
            request={'system': frozen.system, 'messages': [{'role': 'user', 'content': frozen.prompt}],
                     'model': request.model, 'effort': request.effort, 'role': request.step_name},
            identity={'provider': provider}, payload={'system': frozen.system, 'prompt': frozen.prompt},
            recorder=recorder, session_request=original, settings=settings)
        return receipt

    result = run_original_native(root=root, name='session', identity=identity, dispatch=dispatch, project=project)
    return AgentOutcome(**result)
