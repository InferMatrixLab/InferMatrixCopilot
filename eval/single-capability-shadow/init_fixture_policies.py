"""Explicit scripted init policies; execution and recovery live in init_native_arm.

These fixtures never claim native generation or independently labeled quality.
"""
from dataclasses import replace
import json
from types import SimpleNamespace

from arm_runtime import model_gateway


def history_world(fixture, world):
    """Frozen cases own PR evidence; preserve the original fixture translation."""
    if 'prepare_history' in fixture:
        return fixture['prepare_history'](world)
    gateway, source = fixture['_setup'](world)
    original = source.history_evidence

    def evidence(full_name, number):
        row = original(full_name, number)
        if number == 102:
            row['files'] = [{'filename': 'tools/fmt/z.py', 'patch': '+fix'}]
        return row

    source.history_evidence = evidence
    return gateway, source


def scripted_gateway(gateway, native, judge_system, *, transform=None, before_judge=None):
    """One routing adapter; fixture policies supply data or pre-dispatch failure."""
    original = gateway.call_json
    def call(role, *, system, prompt, **kwargs):
        if system == judge_system:
            if before_judge is not None:
                before_judge(system, prompt)
            return native.call_json(role, system=system, prompt=prompt, **kwargs)
        reply = original(role, system=system, prompt=prompt, **kwargs)
        return transform(system, prompt, reply, kwargs) if transform is not None else reply
    gateway.call_json = call
    return gateway


def correction_runtime(args, fixture, world, settings, packets, generations, judge):
    """Keep the old simulated generator/auth policy; never execute a stage here."""
    from infermatrix_copilot.providers.registry import transport_for_id
    from infermatrix_copilot.kb_service.models import ModelRole
    from infermatrix_copilot.kb_service.runtime import trace_recorder
    from infermatrix_copilot.llm import Block, Reply
    from infermatrix_copilot.trace_store import TraceStore
    state = fixture['_runtime'](world).state_dir
    traces = TraceStore(state / 'init' / 'traces')

    class ScriptedGenerator:
        subscription_billing = True  # Simulation: no generator service is called.
        supports_native_events = True

        def complete(self, *, messages, model, **kwargs):
            prompt = messages[0]["content"]
            payload = json.loads(prompt.split("\n", 1)[1].rsplit("</untrusted_data>", 1)[0])
            generations.append(payload)
            source = payload["files"][0]
            evidence = [{"path": source["path"], "start": source.get("start", 1), "end": source["end"]}]
            identities = {"pkg/core.py": ("Engine.step", "1", "only its self parameter"),
                          "tools/fmt/z.py": ("fmt", "3", "no parameters")}
            if source["path"] not in identities:
                raise ValueError("foundation fixture source changed; refreeze the case")
            symbol, constant, parameters = identities[source["path"]]
            claims = {
                "architecture": f"The shown `{symbol}` implementation in `{source['path']}` consists of a constant return of {constant}.",
                "api": f"The shown `{symbol}` signature declares {parameters}; its return value is the integer {constant}.",
                "configuration": f"Within the shown `{symbol}` body, the returned {constant} is a literal; the body contains no configuration or environment lookup.",
                "tradeoffs": f"The constant return in `{symbol}` makes the shown operation deterministic, but this implementation cannot vary its result in response to runtime state.",
                "features": f"The behavior demonstrated by the shown `{symbol}` implementation is returning {constant}; the snippet does not demonstrate parsing or GPU scheduling.",
                "validation": f"The shown `{symbol}` body contains no assertions or input checks. This source excerpt alone does not establish whether separate tests cover the function.",
            }
            sections = []
            for facet in payload["facets"]:
                body = claims[facet]
                if facet == "architecture" and payload["owner"] == "core" and not payload.get("prior_review_feedback"):
                    body = "Engine.step parses JSON documents and schedules them on a GPU."
                sections.append({"facet": facet, "title": facet.capitalize(), "interpretation": "inference" if facet == "tradeoffs" else "fact",
                                 "body": body, "evidence": evidence})
            return Reply(blocks=[Block(type="text", text=json.dumps({"title": payload["owner"] + " knowledge",
                                                                     "sections": sections}))], model=model)

    class CapturedRecorder:
        def __init__(self, recorder):
            self.recorder = recorder

        def __getattr__(self, name):
            return getattr(self.recorder, name)

        def durable(self):
            return type(self)(self.recorder.durable())

        def __call__(self, entry):
            if entry.get('role') == 'judge':
                packets.append({key: entry[key] for key in ('system', 'prompt')})
            return self.recorder(entry)

    recorder = CapturedRecorder(trace_recorder(traces))
    from arm_runtime import load_arm_config
    plan = load_arm_config(args.shim)[1]['plan']
    if plan.get('shared_execution', {}).get('schema') == 'shared-sandbox/2':
        # The real judge admission and dispatch belong to its frozen provider.
        # Only generation is simulated; native profiles cannot be replaced.
        gateway = model_gateway(settings, shim=settings.strict_backend_cli,
            scripted_transports={'zcode': ScriptedGenerator()}, recorder=recorder)
    else:
        class LegacySubscription:
            """Translate historical fixture auth inputs; delegate all invocation."""
            def __init__(self, transport):
                self.transport = transport

            def __getattr__(self, name):
                return getattr(self.transport, name)

            @property
            def settings(self):
                return self.transport.settings

            @settings.setter
            def settings(self, value):
                self.transport.settings = value

            @property
            def subscription_billing(self):
                if args.simulate_subscription_auth:
                    return True
                if args.subscription_native:
                    probe = SimpleNamespace(**{**vars(self.settings), 'strict_backend_cli': str(args.subscription_native)})
                    return transport_for_id(probe, 'codex').subscription_billing
                return self.transport.subscription_billing

        gateway = model_gateway(settings, shim=settings.strict_backend_cli,
            scripted_providers=('zcode',), transport_factory=lambda provider:
                ScriptedGenerator() if provider == 'zcode' else LegacySubscription(transport_for_id(settings, provider)),
            recorder=recorder)
    return fixture['_runtime'](world, gateway,
        generator=ModelRole('generator', 'zcode', 'GLM-5.3'), judge=judge,
        environ={'KB_KNOWLEDGE_CONCURRENCY': '1', 'KB_KNOWLEDGE_START_INTERVAL_S': '0.001'})
