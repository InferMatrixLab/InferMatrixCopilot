"""Minimal tool-use agent loop, ToolScope-constrained and RunTrace-audited.

Agent Steps are the highest-risk step kind (design §3.X.7): the loop only ever
sees the tools its scope allows, and every call goes through tools.dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .llm import LLM, Reply
from .run_trace import RunTrace
from .scopes import ToolScope
from .tools import dispatch, tool_definitions_for


@dataclass
class AgentOutcome:
    """The result of an agent loop: the final `text`, how many `iterations` and
    `tool_calls` it took, whether it was `truncated` (budget exhausted), any
    scope `refusals`, token usage, and the `tools_used` sequence — the audit
    trail a step consumes."""

    text: str
    iterations: int
    tool_calls: int
    truncated: bool = False
    refusals: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    tools_used: list[str] = field(default_factory=list)
    # Explicit native facts, independent of legacy display counters. None means
    # the adapter has not attested usage; an empty mapping means no known counts.
    usage: dict | None = None


def run_agent(
    llm: LLM,
    *,
    system: str,
    prompt: str,
    scope: ToolScope,
    trace: RunTrace | None = None,
    model: str | None = None,
    max_iters: int = 40,
    extra_tools: dict | None = None,
) -> AgentOutcome:
    """Run a tool-use loop until the model stops calling tools or `max_iters` is
    hit. The model only ever sees tools its `scope` permits, and every call goes
    through `tools.dispatch` (scope-checked, `trace`-audited); refusals are
    collected, not fatal. On budget exhaustion it forces one final untooled
    answer from work-so-far rather than discarding the investigation, and marks
    the outcome `truncated`. Returns an AgentOutcome with the answer and audit
    counters."""
    messages: list[dict] = [{"role": "user", "content": prompt}]
    tools = tool_definitions_for(scope, extra_tools)
    tool_calls = 0
    refusals: list[str] = []
    tools_used: list[str] = []
    usage_in = usage_out = 0
    nudged_empty = False
    nudged_cut = False

    for i in range(1, max_iters + 1):
        reply: Reply = llm.create(system=system, messages=messages, tools=tools, model=model)
        if reply.usage:
            usage_in += reply.usage.get("input_tokens", 0)
            usage_out += reply.usage.get("output_tokens", 0)
        assistant_content: list[dict] = []
        for b in reply.blocks:
            if b.type == "text":
                assistant_content.append({"type": "text", "text": b.text})
            else:
                assistant_content.append(
                    {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
                )
        # an empty content list would 400 the next request — represent an
        # all-empty assistant turn honestly instead
        messages.append({"role": "assistant",
                         "content": assistant_content
                         or [{"type": "text", "text": "(empty)"}]})

        uses = reply.tool_uses
        if not uses:
            # nudge only when there is an INVESTIGATION to save — an empty
            # reply with zero tool calls behind it loses nothing, and
            # retrying every such empty would double spend on a degenerate
            # endpoint
            if not (reply.text or "").strip() and tool_calls > 0 \
                    and not nudged_empty and i < max_iters:
                # the model stopped with NO tools and NO text — the whole
                # investigation would be discarded (measured: a 32-round
                # adversary pass ended exactly this way and the sample was
                # lost). One loud nudge; a second empty ends the loop.
                nudged_empty = True
                messages.append({"role": "user", "content":
                                 "Your message was EMPTY. Emit your final "
                                 "answer per the OUTPUT CONTRACT now — "
                                 "partial and honest beats empty. Do not "
                                 "call tools."})
                continue
            if reply.stop_reason == "max_tokens" and tool_calls > 0 \
                    and not nudged_cut and i < max_iters:
                # the final answer hit the per-call token ceiling MID-JSON —
                # measured on the wave-3 gate: a docs pass emitted exactly
                # 16,000 completion tokens, the truncated contract failed
                # coercion, and every candidate died. Ask once for the same
                # answer, tighter; models compress well on demand.
                nudged_cut = True
                messages.append({"role": "user", "content":
                                 "Your final message was CUT at the token "
                                 "ceiling mid-JSON. Re-emit the SAME answer "
                                 "as one complete JSON object, tighter: "
                                 "evidence <= 2 quoted lines per comment, "
                                 "findings <= 25 one-line entries (drop the "
                                 "least decisive), no prose outside the "
                                 "JSON. Do not call tools."})
                continue
            return AgentOutcome(reply.text, i, tool_calls, refusals=refusals,
                                input_tokens=usage_in, output_tokens=usage_out,
                                tools_used=tools_used)

        results = []
        for use in uses:
            tool_calls += 1
            tools_used.append(use.name)
            out = dispatch(use.name, use.input, scope=scope, trace=trace,
                           extra=extra_tools)
            content = out.get("result") if out["ok"] else out.get("error", "error")
            if not out["ok"] and str(content).startswith("refused:"):
                refusals.append(str(content))
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": use.id,
                    "content": str(content),
                    "is_error": not out["ok"],
                }
            )
        if i == max_iters - 1:
            # final-round nudge: without it, agents burn the last round on
            # another tool call and the forced answer loses the contract
            # (T3: a 665-token final answer was discarded for a missing field).
            # MUST come AFTER the tool_result blocks — the API requires
            # tool_results immediately after tool_use ids (a leading text
            # block 400s the whole request).
            results.append({"type": "text",
                            "text": "FINAL ROUND: emit your complete final "
                                    "answer per the OUTPUT CONTRACT now. Do "
                                    "not call more tools."})
        messages.append({"role": "user", "content": results})

    # Budget exhausted: force a final answer from the work done so far instead
    # of discarding the whole investigation.
    messages.append({"role": "user", "content":
                     "Your tool budget is exhausted. Produce your FINAL answer "
                     "now from what you have already gathered (follow the "
                     "output contract if one was given). Do not call tools."})
    # same tools list on purpose: tools serialize BEFORE system in the
    # request, so switching to [] here busts the prompt-cache prefix on
    # the longest call of the loop (truncated runs paid a full re-read)
    reply = llm.create(system=system, messages=messages, tools=tools, model=model)
    if reply.usage:
        usage_in += reply.usage.get("input_tokens", 0)
        usage_out += reply.usage.get("output_tokens", 0)
    if not (reply.text or "").strip() or reply.stop_reason == "max_tokens":
        # Measured failures: a deep pass burned its whole budget then answered
        # the forced-final request with an EMPTY message (wave-2 pr5976 — 50+
        # tool calls discarded), or with a final CUT at the token ceiling
        # mid-JSON (wave-3 gate — 16,000 completion tokens, coercion failed).
        # Both discard the investigation, so each gets exactly one loud retry.
        cut = bool((reply.text or "").strip())
        assistant = [b.text for b in reply.blocks if b.type == "text"]
        messages.append({"role": "assistant",
                         "content": "\n".join(assistant) or "(empty)"})
        messages.append({"role": "user", "content":
                         ("Your final message was CUT at the token ceiling "
                          "mid-JSON. Re-emit the SAME answer as one complete "
                          "JSON object, tighter: evidence <= 2 quoted lines "
                          "per comment, findings <= 25 one-line entries. Do "
                          "not call tools.") if cut else
                         ("Your final message was EMPTY. That discards the "
                          "entire investigation. Emit the output-contract "
                          "JSON NOW, from findings you already gathered — "
                          "partial and honest beats empty. Do not call "
                          "tools.")})
        reply = llm.create(system=system, messages=messages, tools=tools,
                           model=model)
        if reply.usage:
            usage_in += reply.usage.get("input_tokens", 0)
            usage_out += reply.usage.get("output_tokens", 0)
    return AgentOutcome(reply.text or "(agent hit max iterations)", max_iters,
                        tool_calls, truncated=True, refusals=refusals,
                        input_tokens=usage_in, output_tokens=usage_out,
                        tools_used=tools_used)
