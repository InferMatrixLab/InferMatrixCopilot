"""Domain verdict parsing over the shared API and tool-less native providers.

API calls draw on the weekly dollar account; subscription judges draw on
its separate call-count account. Native events, permission enforcement and
model-call receipts use the same production transport as other consumers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from ..budgeting import copy_native_outcome
from ..structured_output import decode_json_object


class JudgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class JudgeSpec:
    kind: str                 # "api" | "cli"
    model: str
    provider: str = ""        # cli: cursor | codex | claude | zcode ; api: the LLM's provider
    max_tokens: int = 2000


def parse_verdict(text: str) -> dict:
    """Decode the legacy greedy object span, allowing prose and code fences."""
    try:
        return decode_json_object(text or "", policy="greedy_object")
    except json.JSONDecodeError as exc:
        raise JudgeError(f"judge reply is not valid JSON: {exc}") from exc
    except ValueError as exc:
        raise JudgeError("no JSON object in the judge reply") from exc


def run_judge(spec: JudgeSpec, *, system: str, prompt: str, llm: Any = None, governor: Any = None,
              role: str = "judge", runner: Callable[..., Any] | None = None,
              completion_profile=None) -> dict:
    """Return a verdict through the production completion and accounting paths."""
    if spec.kind == "api":
        if completion_profile is not None:
            raise JudgeError('API judges do not support a native completion profile')
        if llm is None:
            raise JudgeError("an API judge needs an LLM")
        reply = llm.create(system=system, messages=[{"role": "user", "content": prompt}],
                           model=spec.model, max_tokens=spec.max_tokens, role=role)
        return parse_verdict("".join(b.text for b in reply.blocks if b.type == "text"))
    if spec.kind != "cli":
        raise JudgeError(f"unknown judge kind {spec.kind!r}")
    from ..config import Settings
    from ..providers.completion import complete_native
    from ..providers.registry import PROVIDERS, transport_for_id
    from .budget import BudgetBreach, BudgetRefused, current_governor, judge_governed

    provider = "claude-code" if spec.provider == "claude" else spec.provider
    settings = Settings(**({"strict_backend_cli": PROVIDERS[provider].cli_names[0]}
                           if runner and provider in PROVIDERS else {}))
    transport = transport_for_id(settings, provider,
                                 **({'completion_profile': completion_profile} if completion_profile is not None else {}))
    if runner is not None:
        transport.runner = runner

    def validate(reply):
        text = "".join(b.text for b in reply.blocks if b.type == "text")
        if not text:
            raise JudgeError(f"empty {spec.provider} judge reply")
        return parse_verdict(text)

    governor = governor if governor is not None else current_governor()
    try:
        with judge_governed(governor):
            _, verdict, _, _ = complete_native(
                lambda: transport,
                request={"system": system, "messages": [{"role": "user", "content": prompt}],
                         "model": spec.model, "role": role},
                identity={"provider": provider}, validate=validate,
                capture={"provider": spec.provider,
                         "extra_result": {"usd": 0.0, "subscription": True,
                                          "accounting_convention": "judge-calls/v1"}}, settings=settings)
    except (JudgeError, BudgetRefused, BudgetBreach):
        raise
    except Exception as exc:
        raise copy_native_outcome(JudgeError(
            f"{spec.provider} judge unavailable: {type(exc).__name__}: {exc}"), exc) from exc
    return verdict


def judge_spec_from(settings: Any, override: str = "") -> JudgeSpec | None:
    """The gold_match / paired judge from ``settings.improve_judge`` (or an
    explicit override): ``api:<model>`` or ``cli:<provider>:<model>`` where
    the provider is cursor | codex | claude | zcode; None when unset."""
    raw = str(override or getattr(settings, "improve_judge", "") or "")
    if not raw:
        return None
    parts = raw.split(":")
    if parts[0] == "api" and len(parts) == 2:
        return JudgeSpec("api", parts[1])
    if parts[0] == "cli" and len(parts) == 3:
        if parts[1] not in ("cursor", "codex", "claude", "zcode"):
            raise JudgeError(f"unknown CLI judge provider {parts[1]!r}")
        return JudgeSpec("cli", parts[2], provider=parts[1])
    raise JudgeError(f"improve_judge must be api:<model> or cli:<provider>:<model>, got {raw!r}")
