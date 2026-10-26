"""Translate old SDK completion objects to the canonical installed provider host.

No vendor protocol, application retry, usage decoder or model loop belongs here.
The old SDK owns its workflow; the frozen current host owns native execution.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ledger import Refused


def host_binding(plan):
    host = plan.get('legacy_provider_host')
    fields = {'python', 'python_sha256', 'script', 'script_sha256', 'package_sha256'}
    if not isinstance(host, dict) or set(host) != fields:
        raise Refused('freeze the canonical legacy provider host identity')
    for key in ('python', 'script'):
        value = host[key]
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise Refused('legacy provider host paths must be absolute')
        if hashlib.sha256(Path(value).read_bytes()).hexdigest() != host[key + '_sha256']:
            raise Refused('legacy provider host executable changed')
    sha = host['package_sha256']
    if not isinstance(sha, str) or len(sha) != 64 or set(sha) - set('0123456789abcdef'):
        raise Refused('freeze the canonical provider host package identity')
    return dict(host)


def run_host(command, packet, *, timeout, environment, observer=None):
    """One bounded host call; the extra channel carries callbacks, not CLI bytes."""
    def dispatch(extra=None):
        return subprocess.run(command, input=json.dumps(packet), text=True,
            capture_output=True, timeout=timeout, env=environment, **(extra or {}))
    if observer is None:
        return dispatch()
    if os.name != 'posix':
        raise Refused('native event forwarding requires inherited POSIX descriptors')
    parent, child = socket.socketpair()
    packet = {**packet, 'event_fd': child.fileno()}
    parent.settimeout(timeout + 1)
    error = None
    def launch():
        try:
            return dispatch({'pass_fds': (child.fileno(),)})
        finally:
            child.close()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(launch)
            with parent.makefile('rwb') as channel:
                while True:
                    line = channel.readline(64 * 1024 * 1024 + 1)
                    if not line:
                        break
                    try:
                        if len(line) > 64 * 1024 * 1024:
                            raise Refused('oversized native event frame')
                        frame = json.loads(line)
                        if not isinstance(frame, dict) or set(frame) != {'event'} or not isinstance(frame['event'], dict):
                            raise Refused('invalid native event frame')
                        if error is None:
                            observer(frame['event'])
                    except BaseException as exc:
                        if error is None:
                            error = exc
                    # Rejection closes callback admission. The canonical host
                    # receives the negative acknowledgement and stops its producer.
                    channel.write(json.dumps({'ok': error is None}).encode() + b'\n')
                    channel.flush()
            result = future.result()
        if error is not None:
            raise error
        return result
    finally:
        parent.close()


def event_observer(channel):
    lock = threading.Lock()
    def observe(event):
        with lock:
            channel.write(json.dumps({'event': event}).encode() + b'\n')
            channel.flush()
            reply = channel.readline(1024)
            if not reply or json.loads(reply) != {'ok': True}:
                raise Refused('legacy native event consumer rejected the producer')
    return observe


class LegacyTransport:
    """One-shot object/wire adaptation; uncertain results are never retried."""

    def __init__(self, config_path, provider, *, original_config=None):
        self.config_path = Path(config_path)
        self.original_config = self.config_path.read_bytes() if original_config is None else original_config
        if self.config_path.read_bytes() != self.original_config:
            raise Refused('original evaluation configuration changed')
        self.config = json.loads(self.original_config)
        self.host = host_binding(self.config['plan'])
        from role_profiles import roles
        if provider not in {row['provider'] for row in roles(self.config['plan']).values()}:
            raise Refused('unplanned legacy provider')
        self.provider = provider

    def invoke(self, operation, request=None, *, observer=None):
        if self.config_path.read_bytes() != self.original_config:
            raise Refused('original evaluation configuration changed')
        host_binding(self.config['plan'])
        from arm_runtime import _host_timeout
        from infermatrix_copilot.trace_store import current_context
        packet = {'operation': operation, 'provider': self.provider,
                  'request': request, 'context': current_context()}
        # This process is an SDK compatibility host, not a vendor CLI. Native
        # lifecycle/cancellation/accounting remain with its canonical supervisor.
        # Timeout or malformed response stays uncertain: this adapter never retries.
        result = run_host([self.host['python'], '-I', '-B', self.host['script'],
            str(self.config_path)], packet,
            timeout=_host_timeout(self.config['plan'], case_id=self.config.get('case_id')),
            environment={k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME')},
            observer=observer)
        if result.returncode:
            try:
                error = json.loads(result.stdout).get('error_type', '')
            except (ValueError, AttributeError):
                error = ''
            safe_type = error if isinstance(error, str) and error.isascii() and error.isidentifier() and len(error) <= 128 else 'unknown'
            raise Refused(f'canonical provider host failed ({safe_type}); original outcomes require reconciliation')
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or value.get('schema') != 'legacy-provider-host/1':
            raise Refused('invalid canonical provider host response; reconcile original outcomes')
        return value

    @property
    def subscription_billing(self):
        return self.invoke('admit')['subscription'] is True

    @property
    def stops_at_spend(self):
        return self.invoke('capabilities')['stops_at_spend'] is True

    @property
    def supports_native_events(self):
        return os.name == 'posix' and self.invoke('capabilities')['supports_native_events'] is True

    def native_snapshot(self, events):
        return self.invoke('snapshot', {'events': events})['snapshot']

    def complete(self, **request):
        observer = request.pop('native_event_sink', None)
        if observer is not None and not self.supports_native_events:
            raise Refused('canonical provider does not support native events')
        value = self.invoke('complete', request, observer=observer)
        reply = value['reply']
        from infermatrix_copilot.llm import Reply, Block
        return Reply(**{**reply, 'blocks': [Block(**block) for block in reply['blocks']]})


def serve(config_path, packet, *, observer=None):
    """Current installed host: compose and delegate one canonical operation."""
    import infermatrix_copilot
    from infermatrix_copilot.config import Settings
    from infermatrix_copilot.source_fingerprint import package_tree_hash
    from infermatrix_copilot.trace_store import trace_context
    from infermatrix_copilot.providers.registry import transport_for_id
    from infermatrix_copilot.providers.completion import complete_native
    from infermatrix_copilot.providers.base import flatten_messages
    from infermatrix_copilot.providers.subscription_admission import admit_subscription
    from arm_runtime import ArmRuntime
    config_path = Path(config_path)
    config = json.loads(config_path.read_bytes())
    binding = host_binding(config['plan'])
    if package_tree_hash(Path(infermatrix_copilot.__file__).parent) != binding['package_sha256']:
        raise Refused('canonical provider host package changed')
    if not isinstance(packet, dict) or set(packet) != {'operation', 'provider', 'request', 'context'}:
        raise Refused('invalid legacy provider host request')
    if not isinstance(packet['context'], dict):
        raise Refused('invalid legacy dispatch context')
    runtime = ArmRuntime(config_path, shim=config_path.parent / 'codex')
    provider = packet['provider']
    if provider not in runtime.profiles:
        raise Refused('unplanned legacy provider')
    settings = runtime.settings(Settings(_env_file=None, strict_backend_model='', metrics_enabled=False))
    transport = transport_for_id(settings, provider)
    response = {'schema': 'legacy-provider-host/1'}
    if packet['operation'] == 'decode_object':
        request = packet['request']
        if not isinstance(request, dict) or set(request) != {'text'} or not isinstance(request['text'], str):
            raise Refused('invalid presentation decode request')
        from infermatrix_copilot.structured_output import decode_json_object
        return {**response, 'object': decode_json_object(request['text'])}
    if packet['operation'] == 'capabilities':
        return {**response, 'stops_at_spend': getattr(transport, 'stops_at_spend', False) is True,
                'supports_native_events': getattr(transport, 'supports_native_events', False) is True}
    if packet['operation'] == 'snapshot':
        request = packet['request']
        if not isinstance(request, dict) or set(request) != {'events'} or not isinstance(request['events'], list):
            raise Refused('invalid native snapshot request')
        decode = getattr(transport, 'native_snapshot', None)
        return {**response, 'snapshot': decode(request['events']) if callable(decode) else {}}
    if packet['operation'] not in ('admit', 'complete'):
        raise Refused('unsupported legacy provider host operation')
    transport = runtime.transport(transport, transport.settings)
    if packet['operation'] == 'admit':
        return {**response, 'subscription': admit_subscription(settings, transport) is not None}
    request = packet['request']
    if not isinstance(request, dict) or set(request) - {'system','messages','model','effort','role','max_budget_usd'}:
        raise Refused('unsupported legacy completion request')
    from infermatrix_copilot.providers.completion_profiles import configured_credential_source, parse_completion_config
    if configured_credential_source(parse_completion_config(runtime.profiles[provider])) is not None:
        # RPC admission is not transferable by a boolean. Bind the canonical
        # account proof to this completion transport and execution context.
        transport = admit_subscription(settings, transport)
        if transport is None:
            raise Refused('configured legacy completion account was not admitted')
    with trace_context(**packet['context']), runtime.budget():
        reply, _, _, receipt = complete_native(lambda: transport, request=request,
            identity={'provider': provider},
            payload={'system': request['system'], 'prompt': flatten_messages('', request['messages'])},
            recorder=runtime.recorder(None), settings=settings, native_observer=observer)
    return {**response, 'reply': asdict(reply), 'receipt': receipt}


if __name__ == '__main__':
    stream = None
    channel = None
    try:
        packet = json.load(sys.stdin)
        fd = packet.pop('event_fd', None)
        if fd is not None:
            if type(fd) is not int or fd < 3 or packet.get('operation') != 'complete':
                raise Refused('invalid native event descriptor')
            stream = socket.socket(fileno=fd)
            from arm_runtime import _host_timeout
            config = json.loads(Path(sys.argv[1]).read_bytes())
            stream.settimeout(_host_timeout(config['plan'], case_id=config.get('case_id')))
            channel = stream.makefile('rwb')
        print(json.dumps(serve(sys.argv[1], packet,
                               observer=event_observer(channel) if channel is not None else None)))
    except Exception as exc:
        # Native stdout/stderr and exception text may contain credentials.
        print(json.dumps({'schema': 'legacy-provider-host/1', 'error_type': type(exc).__name__}))
        raise SystemExit(1)
    finally:
        if channel is not None:
            channel.close()
        if stream is not None:
            stream.close()
