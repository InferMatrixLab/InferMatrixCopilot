"""Trusted completion envelope around the canonical installed provider adapter.

The sandbox owns process lifecycle. The adapter owns its native command, spool,
events and tool policy. This worker owns neither retries nor account settlement.
"""
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time
from .provider_options import option_fields

from ..llm import Block, Reply

SCHEMA = 'provider-completion-worker/1'
SETTINGS = {'strict_backend_cli', 'strict_backend_model', 'strict_backend_timeout_s'}
ARGUMENTS = {'system', 'messages', 'model', 'max_tokens', 'role', 'effort', 'max_budget_usd'}


def validate_request(value):
    from .registry import PROVIDERS, _construct_transport
    if (not isinstance(value, dict) or set(value) != {'schema', 'provider', 'settings', 'arguments'}
            or value['schema'] != SCHEMA or not isinstance(value['provider'], str)
            or value['provider'] not in PROVIDERS
            or 'completion_worker' not in PROVIDERS[value['provider']].capabilities
            or not isinstance(value['settings'], dict)
            or not isinstance(value['arguments'], dict) or set(value['arguments']) != ARGUMENTS):
        raise ValueError('invalid provider completion envelope')
    adapter = _construct_transport(None, value['provider'])
    allowed = SETTINGS | option_fields(adapter)
    if set(value['settings']) != allowed:
        raise ValueError('invalid provider completion settings')
    executable = value['settings']['strict_backend_cli']
    if not isinstance(executable, str) or not Path(executable).is_absolute() or '\0' in executable:
        raise ValueError('completion worker requires an explicit absolute native CLI')
    return value


def decode_reply(value):
    if (not isinstance(value, dict) or set(value) != {'blocks', 'stop_reason', 'usage', 'model', 'request_id'}
            or not isinstance(value['blocks'], list)
            or any(not isinstance(block, dict) or set(block) != {'type', 'text', 'id', 'name', 'input'}
                   or block['type'] != 'text' or not isinstance(block['text'], str)
                   or not isinstance(block['input'], dict)
                   or not all(isinstance(block[key], str) for key in ('id', 'name'))
                   for block in value['blocks'])
            or not all(isinstance(value[key], str) for key in ('stop_reason', 'model', 'request_id'))
            or value['usage'] is not None and not isinstance(value['usage'], dict)):
        raise ValueError('invalid provider completion result')
    return Reply(**{**value, 'blocks': [Block(**block) for block in value['blocks']]})


def frames(stdout):
    values = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    if any(not isinstance(value, dict) or set(value) != {'schema', 'kind', 'value'} or value['schema'] != SCHEMA
           or value['kind'] not in {'event', 'result', 'cleanup'} or not isinstance(value['value'], dict) for value in values):
        raise ValueError('invalid completion worker frame')
    cleanup = [value for value in values if value['kind'] == 'cleanup']
    if cleanup:
        if len(cleanup) != 1 or values[-1:] != cleanup or any(value['kind'] == 'result' for value in values):
            raise ValueError('invalid completion worker cleanup sequence')
        from ..process_execution import ProcessCleanupError
        ProcessCleanupError.from_facts(cleanup[0]['value'])
    return values


def process_receipt(outcome, *, native_snapshot=None):
    result = dict(outcome)
    values = frames(result['stdout'])
    replies = [frame for frame in values if frame['kind'] == 'result']
    complete = result['returncode'] == 0 and result['reason'] is None and len(replies) == 1 and values[-1:] == replies
    reply = decode_reply(replies[0]['value']) if len(replies) == 1 else None
    complete = complete and reply.stop_reason == 'end_turn'
    snapshot = native_snapshot([frame['value'] for frame in values if frame['kind'] == 'event']) if native_snapshot else {}
    cleanup = next((frame['value'] for frame in values if frame['kind'] == 'cleanup'), None)
    return {**result, 'native_complete': complete, 'terminal_failure': False,
            **({'cleanup_facts': cleanup, 'native_outcome_pending': True} if cleanup is not None else {}),
            'reply_text': reply.text if reply is not None else snapshot.get('text', ''),
            'usage': reply.usage if reply is not None else snapshot.get('usage'),
            'accounting_usage': snapshot.get('usage'), 'served_model': snapshot.get('served_model', '')}


def _raise_cleanup(receipt):
    if 'cleanup_facts' in receipt:
        from ..process_execution import ProcessCleanupError
        error = ProcessCleanupError.from_facts(receipt['cleanup_facts'])
        error.native_outcome_pending = True
        error.allow_fallback = False
        raise error


def read_result(transport, *, recover=False):
    """Decode only the original bound worker result, without model dispatch."""
    outcome = transport.completion_profile.read_result(recover=recover)
    transport.native_process_outcome = outcome
    receipt = process_receipt({'stdout': outcome.stdout, 'stderr': outcome.stderr,
        'returncode': outcome.returncode, 'reason': outcome.reason}, native_snapshot=transport.native_snapshot)
    _raise_cleanup(receipt)
    if not receipt['native_complete']:
        raise ValueError('original completion worker result is incomplete')
    return decode_reply(frames(outcome.stdout)[-1]['value'])


def complete(transport, arguments, **controls):
    """An unconfirmed worker exchange never authorizes a replacement call."""
    try:
        return _complete(transport, arguments, **controls)
    except BaseException as exc:
        exc.native_outcome_pending = True
        raise


def _complete(transport, arguments, *, frozen_inputs=None, idle_timeout_s=None, absolute_deadline=None,
              native_event_sink=None):
    profile = transport.completion_profile
    profile.validate()
    settings = transport.settings
    fields = SETTINGS | option_fields(transport)
    options = {key: getattr(settings, key, None) for key in fields}
    options['strict_backend_cli'] = profile.native_cli
    request = validate_request({'schema': SCHEMA, 'provider': transport.spec.id,
                                'settings': options, 'arguments': arguments})
    streamed = []

    def observe(channel, line):
        if channel == 'stdout':
            for frame in frames(line):
                streamed.append(frame)
                if frame['kind'] == 'event' and native_event_sink is not None:
                    native_event_sink(frame['value'])

    controls = {'frozen_inputs': frozen_inputs} if frozen_inputs is not None else {}
    if idle_timeout_s is not None:
        controls.update(idle_timeout_s=idle_timeout_s, termination_grace_s=5)
    outcome = profile.execute(['/usr/local/bin/python', '-I', '-B', '-m', __name__],
        cwd='/tmp/work', env={}, input=json.dumps(request),
        deadline=absolute_deadline if absolute_deadline is not None else time.monotonic() + settings.strict_backend_timeout_s,
        on_line=observe if native_event_sink is not None else None, **controls)
    transport.native_process_outcome = outcome
    values = frames(outcome.stdout)
    if streamed and streamed != values:
        raise ValueError('completion worker stream differs from its original result')
    if native_event_sink is not None and not streamed:
        for frame in values:
            if frame['kind'] == 'event':
                native_event_sink(frame['value'])
    receipt = process_receipt({'stdout': outcome.stdout, 'stderr': outcome.stderr,
                               'returncode': outcome.returncode, 'reason': outcome.reason},
                              native_snapshot=transport.native_snapshot)
    _raise_cleanup(receipt)
    if not receipt['native_complete']:
        error = RuntimeError('completion worker did not return a complete original result')
        raise error
    return decode_reply(values[-1]['value'])


def main():
    from ..config import Settings
    from .registry import _construct_transport
    request = validate_request(json.load(sys.stdin))
    settings = Settings(_env_file=None, strict_backend=request['provider'],
                        native_completion_profiles={}, native_session_profiles={},
                        **{key: value for key, value in request['settings'].items() if value is not None})
    adapter = _construct_transport(settings, request['provider'])

    def emit(kind, value):
        print(json.dumps({'schema': SCHEMA, 'kind': kind, 'value': value}), flush=True)

    try:
        reply = adapter.complete(**request['arguments'], native_event_sink=lambda event: emit('event', event))
    except BaseException as error:
        from ..process_execution import process_cleanup_facts
        facts = process_cleanup_facts(error)
        if not facts:
            raise
        emit('cleanup', facts)
        return 1
    emit('result', asdict(reply))


if __name__ == '__main__':
    raise SystemExit(main())
