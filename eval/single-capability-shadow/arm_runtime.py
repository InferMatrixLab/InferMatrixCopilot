"""Evaluation composition; provider execution and archive rules stay in Copilot.

The original installed arm keeps its CLI translator. Current packages receive
trusted profiles before entering complete_native, and the evaluation ledger is
injected through the same account hooks used by application domains.
"""
from contextlib import contextmanager
from copy import copy
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from ledger import Ledger, Refused


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def supports_shared_execution():
    from infermatrix_copilot.config import Settings
    return 'native_completion_profiles' in Settings.model_fields


def application_history_payloads():
    """Old installed SDKs lack the application-owned history recording policy."""
    from infermatrix_copilot.kb_service import init_history
    return getattr(init_history, 'PAYLOADS_ENV', None)


def load_arm_config(shim, config_path=None):
    value = config_path or os.environ.get('SHADOW_ARM_CONFIG')
    if not value:
        raise Refused('current arm requires its frozen SHADOW_ARM_CONFIG')
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or path.parent.resolve() != Path(shim).resolve().parent:
        raise Refused('arm config must belong to its original evaluation shim')
    return path, json.loads(path.read_bytes())


def execution_profile(shim):
    """Legacy SDK drivers use the same frozen defaults as CLI translation."""
    return load_arm_config(shim)[1]['plan']['execution_profile']


def _host_timeout(plan, *, case_id=None):
    from shared_execution import plan_for_case, host_deadline
    if case_id is not None:
        plan = plan_for_case(plan, case_id)
    return host_deadline(plan)


def host_timeout(shim):
    config = load_arm_config(shim)[1]
    return _host_timeout(config['plan'], case_id=config.get('case_id'))


def _workflow_timeout(plan, *, case_id='strict', require_explicit=False):
    """A workflow wait is distinct from the deadline of each model call."""
    from shared_execution import plan_for_case, workflow_deadline
    return workflow_deadline(plan_for_case(plan, case_id), case_id=case_id,
                             require_explicit=require_explicit)


def workflow_timeout(shim):
    config = load_arm_config(shim)[1]
    return _workflow_timeout(config['plan'], case_id=config.get('case_id', 'strict'))


def _profile_model_roles(plan, *, require_explicit=False):
    from infermatrix_copilot.kb_service.models import ModelRole
    profile = plan['execution_profile']
    if require_explicit or 'roles' in profile:
        from role_profiles import roles
        configured = roles(plan)
        return tuple(ModelRole(name, **configured[name]) for name in ('generator', 'judge'))
    return tuple(ModelRole(name, 'codex', profile['model'], profile['reasoning_effort'])
                 for name in ('generator', 'judge'))


def model_roles(shim, model=None, effort=None, *, require_explicit=False):
    """Select original role pins; stage overrides cannot replace the frozen defaults."""
    plan = load_arm_config(shim)[1]['plan']
    profile = plan['execution_profile']
    if (model or profile['model'], effort or profile['reasoning_effort']) != (
            profile['model'], profile['reasoning_effort']):
        raise ValueError('stage roles differ from the frozen model/effort')
    return _profile_model_roles(plan, require_explicit=require_explicit)


def judgment_context(shim, model=None):
    """One frozen judge and host settings selection for judgment-only entries."""
    config = load_arm_config(shim)[1]
    plan = config['plan']
    judge = _profile_model_roles(plan)[1]
    if model is not None and model != judge.model:
        raise Refused('requested model differs from the frozen judge')
    return SimpleNamespace(strict_backend_cli=shim,
        strict_backend_model=plan['execution_profile']['model'],
        strict_backend_timeout_s=_host_timeout(plan, case_id=config.get('case_id'))), judge


def validate_payload_retention(config, config_path):
    """Explicit frozen shadow policy; never infer retention from containment."""
    policy = config['plan'].get('payload_retention')
    if policy is None:
        return None
    fields = {'schema', 'mode', 'publication', 'case_ids', 'root',
              'input_manifest', 'input_manifest_sha256'}
    if (not isinstance(policy, dict) or set(policy) != fields
            or policy['schema'] != 'shadow-payload-retention/1'
            or policy['mode'] != 'retain-request-result' or policy['publication'] != 'disabled'
            or not isinstance(policy['case_ids'], list) or not policy['case_ids']
            or any(not isinstance(case, str) or not case for case in policy['case_ids'])
            or len(set(policy['case_ids'])) != len(policy['case_ids'])
            or not isinstance(config['plan'].get('native_call_limits'), dict)
            or not set(policy['case_ids']) <= set(config['plan']['native_call_limits'])
            or config.get('case_id') not in config['plan']['native_call_limits']
            or config['plan'].get('execution_kind') not in {'fixture', 'live'}):
        raise Refused('invalid explicit frozen evaluation payload-retention policy')
    try:
        root, manifest = Path(policy['root']), Path(policy['input_manifest'])
        area, ledger = Path(config_path).parent, Path(config['ledger_path'])
        if (not root.is_absolute() or root.is_symlink() or root.resolve() != root
                or root != ledger.parent.resolve() or root not in area.resolve().parents
                or root.stat().st_uid != os.geteuid() or root.stat().st_mode & 0o077
                or not manifest.is_absolute() or manifest.is_symlink()
                or root not in manifest.resolve().parents
                or hashlib.sha256(manifest.read_bytes()).hexdigest() != policy['input_manifest_sha256']):
            raise ValueError('retention scope or frozen inputs changed')
        inputs = json.loads(manifest.read_bytes())
        if (set(inputs) != {'schema', 'files'} or inputs['schema'] != 'shadow-retained-inputs/1'
                or not isinstance(inputs['files'], dict) or not inputs['files']):
            raise ValueError('freeze exact retained evaluation inputs')
        for name, expected in inputs['files'].items():
            path = Path(name)
            if (not path.is_absolute() or path.is_symlink()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != expected):
                raise ValueError('retained evaluation input changed')
    except (OSError, TypeError, ValueError, KeyError) as exc:
        raise Refused('evaluation payload retention needs its private original root and frozen inputs') from exc
    return dict(policy) if config.get('case_id') in policy['case_ids'] else None


def legacy_settings(settings, shim):
    if not os.environ.get('SHADOW_ARM_CONFIG'):
        return settings
    plan = load_arm_config(shim)[1]['plan']
    if 'host_timeout_s' not in plan and 'case_deadlines' not in plan:
        return settings
    changes = {'strict_backend_timeout_s': host_timeout(shim)}
    if hasattr(settings, 'model_copy'):
        return settings.model_copy(deep=True, update=changes)
    result = copy(settings)
    result.strict_backend_timeout_s = changes['strict_backend_timeout_s']
    return result


@dataclass
class Ticket:
    request_digest: str
    call_id: str
    capture_root: Path
    prepare_required: bool = True
    binding: dict | None = None
    ledger: object = None
    reserved: bool = False
    intent: dict | None = None
    provider_id: str = 'codex'


class ArmRuntime:
    def __init__(self, config_path, *, shim):
        from shared_execution import contained, validate_shared, shared_profiles, plan_for_case
        path, self.config = load_arm_config(shim, config_path)
        self.plan, self.arm = self.config['plan'], self.config['arm']
        self.execution_plan = plan_for_case(self.plan, self.config['case_id'])
        self.profiles = shared_profiles(self.execution_plan)
        self.profile, roots, self.knowledge = validate_shared(self.execution_plan, self.arm)
        self.root = contained(path.parent, roots)
        # Reconstructed gateways need fresh zero-model controls. Paid intent,
        # bindings and accounting remain under the fixed original root.
        self.control_root = self.root / 'native-controls' / uuid4().hex
        self.ledger_path = Path(self.config['ledger_path'])
        if not self.ledger_path.is_absolute():
            raise Refused('evaluation ledger requires an absolute original path')
        self.model = self.plan['execution_profile']['model']
        self.effort = self.plan['execution_profile']['reasoning_effort']
        from shared_execution import native_deadline
        self.timeout = native_deadline(self.execution_plan)
        from infermatrix_copilot.providers.codex_policy import validate_effort
        validate_effort(self.effort)
        self.proofs = {}
        self.entries = {}

    def settings(self, settings):
        changes = dict(native_completion_profiles=self.profiles,
            trace_store_root=str(self.root / 'evaluation-traces'),
            strict_backend_model=self.model,
            strict_backend_timeout_s=self.timeout)
        if settings.strict_backend_model and settings.strict_backend_model != self.model:
            raise Refused('arm model differs from its frozen execution profile')
        if hasattr(settings, 'model_copy'):
            return settings.model_copy(deep=True, update=changes)
        result = copy(settings)
        for name, value in changes.items():
            setattr(result, name, value)
        return result

    def recorder(self, observer):
        from infermatrix_copilot.trace_store import NativeCompletionRecorder, TraceStore
        if callable(getattr(observer, 'durable', None)):
            delegate, callback = observer.durable(), None
        else:
            delegate = NativeCompletionRecorder(TraceStore(self.root / 'evaluation-traces'), strict=True)
            callback = observer
        entries = self.entries

        class Recorder:
            @property
            def store(self):
                return delegate.store

            def durable(self):
                return self

            def begin_call(self, entry):
                return delegate.begin_call(entry)

            def __call__(self, entry):
                receipt = delegate(entry)
                entries[entry['native_attempt_id']] = entry
                if callback is not None:
                    callback(entry)
                return receipt

        return Recorder()

    def transport(self, transport, settings):
        from infermatrix_copilot.providers.completion_profiles import parse_completion_config, sandbox_profile_for_config
        from infermatrix_copilot.persistence import immutable_write_bytes
        from shared_execution import provider_binary_hash
        provider = transport.spec.id
        if provider not in self.profiles:
            raise Refused('transport provider is not frozen in the evaluation plan')
        if provider not in self.proofs:
            self.control_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            suffix = '' if self.plan['shared_execution']['schema'] == 'shared-sandbox/1' else '-' + provider
            profile = sandbox_profile_for_config(parse_completion_config(self.profiles[provider]),
                                                self.control_root / ('native-identity' + suffix))
            proof = transport.identity_probe(profile)
            if proof['sha256'] != provider_binary_hash(self.plan, provider):
                raise Refused('sandbox native binary differs from the frozen provider identity')
            immutable_write_bytes(self.control_root / ('native-identity-proof' + suffix + '.json'),
                                  (json.dumps(proof, sort_keys=True) + '\n').encode(), exist_ok=True)
            self.proofs[provider] = proof
        transport.settings = settings
        transport.required_execution_context = self.proofs[provider]['context']
        return transport

    def acquire(self, request):
        if os.environ.get('SHADOW_FORGE_REFUSAL'):
            from strict_host import require_frozen_reads
            require_frozen_reads()
        expected_provider, expected_model, expected_effort = 'codex', self.model, self.effort
        if 'roles' in self.plan['execution_profile']:
            from role_profiles import invocation_role
            selected = invocation_role(self.plan, name=request.get('role'),
                                       kind=request.get('kind'), case_id=self.config['case_id'])
            expected_provider, expected_model, expected_effort = selected['provider'], selected['model'], selected['effort']
        if request.get('provider_id') != expected_provider:
            raise Refused('evaluation account only admits its frozen role provider')
        if request.get('model') != expected_model or request.get('effort', '') != expected_effort:
            raise Refused('native request differs from the frozen model/effort')
        request_digest = digest({key: value for key, value in request.items() if key != 'absolute_deadline'})
        intent = None
        protocol = self.plan.get('call_identity')
        if protocol is not None:
            from infermatrix_copilot.trace_store import current_context
            context = current_context()
            if protocol == 'agent-step/1':
                intent = {key: context[key] for key in ('unit_id', 'attempt', 'agent_step') if key in context}
            elif protocol == 'knowledge-dispatch/1':
                scope = context.get('knowledge_dispatch')
                if (not isinstance(scope, dict) or set(scope) != {'dispatch_sha256', 'attempt'}
                        or not isinstance(scope['dispatch_sha256'], str)
                        or len(scope['dispatch_sha256']) != 64
                        or set(scope['dispatch_sha256']) - set('0123456789abcdef')
                        or type(scope['attempt']) is not int or scope['attempt'] < 1):
                    raise Refused('freeze the original knowledge dispatch and attempt before calling a model')
                intent = {'knowledge_dispatch': dict(scope)}
            else:
                raise Refused('unsupported frozen native call identity protocol')
        call_id = digest({'arm': self.arm, 'case': self.config['case_id'], 'request': request_digest,
                          **({'intent': intent} if intent is not None else {})})
        return Ticket(request_digest, call_id, self.root / 'evaluation-traces', intent=intent,
                      provider_id=expected_provider)

    def prepare(self, ticket, request, execution):
        from infermatrix_copilot.persistence import immutable_write_bytes
        from infermatrix_copilot.providers.completion_profiles import normalize_completion_profiles
        from shared_execution import contained, validate_shared
        provider = ticket.provider_id
        proof = self.proofs.get(provider)
        if execution.get('provider_id') != provider or proof is None:
            raise Refused('evaluation dispatch lacks a prepared native identity')
        binding = execution.get('native_binding')
        if not isinstance(binding, dict) or binding.get('schema') != 'native-budget-binding/1':
            raise Refused('evaluation requires the original canonical native binding')
        if ticket.intent is not None and any(binding['context'].get(key) != value
                                             for key, value in ticket.intent.items()):
            raise Refused('logical call identity differs from its original native context')
        _, roots, _ = validate_shared(self.plan, self.arm)
        trace_root = contained(binding['trace_root'], roots)
        attempt = trace_root / 'attempts' / binding['attempt_id']
        descriptor = json.loads((attempt / 'execution-profile.json').read_bytes())
        if (descriptor['native_attempt_id'] != binding['attempt_id']
                or normalize_completion_profiles({provider: descriptor['config']})[provider] != self.profiles[provider]
                or descriptor.get('required_execution_context') != proof['context']):
            raise Refused('prepared native execution differs from the evaluation profile')
        ticket.binding = binding
        # A changed attempt cannot replace an original binding, including a
        # crash before the ledger reservation. No model has been sent yet.
        try:
            immutable_write_bytes(self.root / (ticket.call_id + '.binding.json'),
                                  (json.dumps(binding, sort_keys=True) + '\n').encode())
        except FileExistsError as exc:
            raise Refused('original evaluation attempt cannot be replaced; reuse or reconcile its receipt') from exc
        ticket.ledger = Ledger(self.ledger_path, self.plan)
        cached = ticket.ledger.reserve(ticket.call_id, case_id=self.config['case_id'], arm=self.arm,
                                       request_digest=ticket.request_digest)
        if cached is not None:
            raise Refused('completed evaluation call must reuse its original application receipt')
        ticket.reserved = True

    def finish(self, ticket, facts):
        if ticket.ledger is None:
            return
        try:
            if not ticket.reserved or not facts['sent']:
                return
            if (getattr(facts, 'native_outcome_pending', False)
                    or not getattr(facts, 'native_receipt_complete', False)):
                return
            from infermatrix_copilot.trace_store import TraceStore
            from infermatrix_copilot.providers.native_result import native_process_result
            from infermatrix_copilot.providers.registry import transport_for_id
            store = TraceStore(ticket.binding['trace_root'])
            rows = [row for row, _ in store.iter_records(kind='model_call')
                    if row.get('result', {}).get('native_attempt_id') == ticket.binding['attempt_id']]
            if len(rows) != 1:
                raise Refused('original evaluation native receipt is missing or ambiguous')
            record = rows[0]
            process = native_process_result(store, record, entry=self.entries.get(ticket.binding['attempt_id']))
            receipt = transport_for_id(None, ticket.provider_id).process_receipt(process)
            receipt.update(trace_root=str(store.root), trace_id=record['id'],
                native_attempt_id=ticket.binding['attempt_id'], cost_usd=record['result'].get('cost_usd'),
                request_sha256=ticket.request_digest)
            if receipt['native_complete']:
                ticket.ledger.complete(ticket.call_id, receipt)
            else:
                from infermatrix_copilot.persistence import immutable_write_bytes
                immutable_write_bytes(self.ledger_path.parent / (ticket.call_id + '.partial.json'),
                                      (json.dumps(receipt, sort_keys=True) + '\n').encode(), exist_ok=True)
            # Failed or unknown native outcomes keep their reservation. Domain
            # reply/schema rejection does not erase a completed native charge.
        finally:
            ticket.ledger.close()

    @contextmanager
    def budget(self):
        from infermatrix_copilot.budgeting import bind_call_budget
        with bind_call_budget('evaluation-native', self.acquire, self.finish, prepare=self.prepare):
            yield


def model_gateway(settings, *, shim, recorder=None, transport_factory=None, scripted_providers=(),
                  scripted_transports=None):
    """One composition for maintained knowledge drivers; old packages delegate."""
    from infermatrix_copilot.kb_service.models import ModelGateway
    base_gateway = ModelGateway
    retention = None
    if os.environ.get('SHADOW_ARM_CONFIG'):
        config_path, config = load_arm_config(shim)
        retention = validate_payload_retention(config, config_path)
    from role_profiles import fixture_providers
    declared = fixture_providers(config['plan']) if os.environ.get('SHADOW_ARM_CONFIG') else set()
    scripted = dict(scripted_transports or {})
    if scripted_transports is not None:
        if (transport_factory is not None or scripted_providers or not scripted
                or set(scripted) != declared):
            raise Refused('scripted transports require one explicit frozen fixture policy')
        scripted_providers = tuple(scripted)
        def transport_factory(provider):
            if provider in scripted:
                return scripted[provider]
            from infermatrix_copilot.providers.registry import transport_for_id
            return transport_for_id(settings, provider)

    elif declared:
        raise Refused('frozen fixture generator requires its explicit scripted transport')

    if retention is not None:
        config_sha = hashlib.sha256(config_path.read_bytes()).hexdigest()

        class PlannedGateway(ModelGateway):
            # This is an evaluation host input policy, not a provider or a
            # second application loop. Production ModelGateway stays unchanged.
            def call_json(self, role, **kwargs):
                if hashlib.sha256(config_path.read_bytes()).hexdigest() != config_sha:
                    raise Refused('original evaluation configuration changed')
                validate_payload_retention(config, config_path)
                selected = dict(kwargs)
                if selected.get('record_payload', True) is False:
                    if application_history_payloads() is not None:
                        raise Refused('current application must select its payload policy before the model gateway')
                    # Legacy installed SDK translation only; current application
                    # workflows select recording before entering this gateway.
                    selected['record_payload'] = True
                from infermatrix_copilot.trace_store import trace_context
                with trace_context(shadow_payload_retention=digest(retention)):
                    return super().call_json(role, **selected)

        base_gateway = PlannedGateway
    if not supports_shared_execution():
        if (os.environ.get('SHADOW_ARM_CONFIG') and
                load_arm_config(shim)[1]['plan'].get('shared_execution', {}).get('schema') == 'shared-sandbox/2'):
            plan = config['plan']
            frozen_config = config_path.read_bytes()
            if json.loads(frozen_config) != config:
                raise Refused('original evaluation configuration changed')
            if 'legacy_provider_host' not in plan:
                raise Refused('installed package cannot execute frozen mixed native providers')
            if (transport_factory is not None or scripted_providers) and scripted_transports is None:
                raise Refused('legacy mixed execution must use only its frozen canonical host')
            from legacy_provider_host import LegacyTransport, host_binding
            host_binding(plan)
            class LegacyGateway(base_gateway):
                def call_json(self, role, **kwargs):
                    if kwargs.get('record_payload', True) is False and retention is None:
                        raise Refused('durable legacy execution requires explicit payload retention')
                    return super().call_json(role, **kwargs)

            result = LegacyGateway(settings, transport_factory=lambda provider: scripted[provider] if provider in scripted
                                   else LegacyTransport(config_path, provider, original_config=frozen_config),
                                   recorder=recorder)
            result.evaluation_payload_retention = retention
            return result
        settings = legacy_settings(settings, shim)
        if transport_factory is None:
            from infermatrix_copilot.providers.codex import CodexTransport
            transport_factory = lambda _: CodexTransport(settings)
        else:
            supplied_factory = transport_factory
            def transport_factory(provider):
                result = supplied_factory(provider)
                if hasattr(result, 'settings'):
                    result.settings = settings
                return result
        result = base_gateway(settings, transport_factory=transport_factory, recorder=recorder)
        result.evaluation_payload_retention = retention
        return result
    path = os.environ.get('SHADOW_ARM_CONFIG')
    if not path:
        raise Refused('current arm requires its frozen SHADOW_ARM_CONFIG')
    runtime = ArmRuntime(path, shim=shim)
    settings = runtime.settings(settings)
    from infermatrix_copilot.providers.registry import transport_for_id

    def factory(provider):
        result = transport_factory(provider) if transport_factory is not None else transport_for_id(settings, provider)
        return runtime.transport(result, settings) if provider in runtime.profiles else result

    class Gateway(base_gateway):
        def call_json(self, role, **kwargs):
            if role.provider not in runtime.profiles:
                if role.provider not in scripted_providers or runtime.plan.get('execution_kind') != 'fixture':
                    raise Refused('unplanned non-Codex execution in evaluation arm')
                # Only the caller's explicit fixture factory can supply these
                # scripted generations. They are excluded from quality claims.
                return super().call_json(role, **kwargs)
            defaults = {'model': runtime.model, 'effort': runtime.effort}
            if 'roles' in runtime.plan['execution_profile']:
                from role_profiles import role as frozen_role
                defaults = frozen_role(runtime.plan, role.name)
            selected = replace(role, model=role.model or defaults['model'], effort=role.effort or defaults['effort'])
            with runtime.budget():
                return super().call_json(selected, **kwargs)

    result = Gateway(settings, transport_factory=factory, recorder=runtime.recorder(recorder))
    result.evaluation_runtime = runtime
    result.evaluation_payload_retention = retention
    return result


def native_completion(settings, *, shim, system, messages, model='', effort=''):
    """Preserve the completion driver's Reply contract through one producer."""
    from infermatrix_copilot.providers.codex import CodexTransport
    if not supports_shared_execution():
        settings = legacy_settings(settings, shim)
        return CodexTransport(settings).complete(system=system, messages=messages, model=model, effort=effort)
    path = os.environ.get('SHADOW_ARM_CONFIG')
    if not path:
        raise Refused('current arm requires its frozen SHADOW_ARM_CONFIG')
    runtime = ArmRuntime(path, shim=shim)
    settings = runtime.settings(settings)
    from infermatrix_copilot.providers.base import flatten_messages
    from infermatrix_copilot.providers.completion import complete_native
    request = dict(system=system, messages=messages, model=model or runtime.model,
                   effort=effort or runtime.effort, role='shadow-completion')
    with runtime.budget():
        reply, _, _, _ = complete_native(lambda: runtime.transport(CodexTransport(settings), settings),
            request=request, identity={'provider': 'codex'},
            payload={'system': system, 'prompt': flatten_messages('', messages)},
            recorder=runtime.recorder(None), settings=settings)
    return reply


@contextmanager
def _worker_environment(overrides, *, legacy=False):
    """Translate host inputs in one isolated evaluation driver process."""
    names = {'ensemble_parallel', 'review_verify_concurrency', 'review_lens_backends', 'memory_db'}
    if legacy:
        names.update(('strict_backend', 'strict_backend_cli', 'strict_backend_model',
                      'strict_backend_timeout_s', 'strict_backend_effort'))
    changes = {key.upper(): json.dumps(value) if isinstance(value, (dict, list, bool)) else str(value)
               for key, value in overrides.items() if key in names}
    previous = {key: os.environ.get(key) for key in changes}
    os.environ.update(changes)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def sdk_runtime(overrides, *, shim, for_worker=False):
    """Compose current SDK sessions and auxiliary completions before construction."""
    if not supports_shared_execution():
        # The original SDK predates effective backend projection to its worker.
        # Translate trusted host inputs only; retain its standard queue/worker.
        with _worker_environment(overrides if for_worker else {}, legacy=True):
            yield overrides
        return
    from infermatrix_copilot.config import Settings
    from infermatrix_copilot.providers.registry import bind_transport_policy, transport_for_id
    runtime = ArmRuntime(os.environ.get('SHADOW_ARM_CONFIG'), shim=shim)
    settings = runtime.settings(Settings(**overrides)).model_copy(update={
        'native_session_profiles': {'codex': runtime.profile},
        'strict_backend_effort': runtime.effort})
    if runtime.knowledge is not None:
        knowledge = Path(runtime.knowledge)
        if not knowledge.is_absolute() or not knowledge.is_dir():
            raise Refused('SDK sessions require the declared frozen knowledge directory')
        settings = settings.model_copy(update={'knowledge_dir': knowledge})
    def prepare(transport):
        if transport.spec.id != 'codex':
            raise Refused('unplanned provider in the frozen SDK evaluation workflow')
        return runtime.transport(transport, transport.settings)

    changes = {name: getattr(settings, name) for name in (
        'native_completion_profiles', 'native_session_profiles', 'strict_backend_effort',
        'strict_backend_model', 'strict_backend_timeout_s', 'trace_store_root')}
    if runtime.knowledge is not None:
        changes['knowledge_dir'] = settings.knowledge_dir
    if for_worker:
        # Reservation/queueing is administrative. Model work belongs to the
        # independently running child, which installs this same composition.
        from infermatrix_copilot.budgeting import bind_call_budget
        def refuse_parent(request):
            raise Refused('durable evaluation model calls must execute in their worker')
        with _worker_environment(overrides), bind_call_budget('evaluation-native', refuse_parent, lambda *args: None):
            yield {**overrides, **changes}
        return
    # Establish the identity before concurrent workflow tasks construct adapters.
    runtime.transport(transport_for_id(settings, 'codex'), settings)
    with bind_transport_policy(prepare), runtime.budget():
        yield {**overrides, **changes}
