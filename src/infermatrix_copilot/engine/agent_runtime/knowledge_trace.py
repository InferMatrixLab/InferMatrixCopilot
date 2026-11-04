"""Strict knowledge-use capture at the rendered prompt and API boundaries.

The model reports assessments; only the runtime can attest which content and
tool results it supplied. Harness sessions expose the prompt handoff, while
their native file reads and internal model requests remain unobserved.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any
import uuid

from ..step import StepContext

_STATE_KEY = "_knowledge_use_trace"


class StrictKnowledgeTrace:
    def __init__(self, ctx: StepContext, mode: str):
        from ...knowledge_trace import KnowledgeTrace

        self.ctx, self.mode = ctx, mode
        self.root = Path(os.environ.get("KNOWLEDGE_TRACE_ROOT")
                         or ctx.run_dir / "knowledge_trace")
        self.service = KnowledgeTrace(root=self.root, mode=mode)
        self.error = ""
        spec = ctx.state.get("task_spec") or {}
        self.source_revision = str(ctx.state.get("pr_head_sha")
                                   or spec.get("expected_head_sha") or "")
        if not self.source_revision and ctx.state.get("repo_path"):
            try:
                result = subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=ctx.state["repo_path"],
                    capture_output=True, text=True, timeout=5, check=False)
                if result.returncode == 0:
                    self.source_revision = result.stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                pass  # An unknown revision is explicit session metadata.
        try:
            pin = json.loads((ctx.run_dir / "knowledge.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pin = {}
        from ...knowledge_view import KnowledgeView, _load_view
        from ...kb_service.repo_spec import resolve_snapshot_repo

        root = Path(ctx.settings.knowledge_dir).resolve()
        current = KnowledgeView.current()
        if current.root.resolve() == root:
            view = current
        elif (root.parent / "MANIFEST.json").is_file():
            view = _load_view(str(root.parent))
        else:
            view = KnowledgeView(root, f"unverified:{root}")
        self.snapshot = str(pin.get("snapshot") or view.public_snapshot)
        from .knowledge import _resolve_adapter

        adapter = _resolve_adapter(ctx)
        subdir = str(((adapter.manifest.get("knowledge") or {}).get("repo_subdir")
                      if adapter else "") or "")
        repository = (subdir.split("/")[1] if subdir.startswith("repos/")
                      else str(spec.get("repo") or "unknown").replace("_", "-"))
        binding = resolve_snapshot_repo(view, str(spec.get("repo") or ""))
        if binding is not None:
            repository = binding.repo_id
        self.repository = repository
        identity = hashlib.sha256(str(ctx.run_dir.resolve()).encode()).hexdigest()[:24]
        self.trace_id = "strict-" + identity + "-" + uuid.uuid4().hex[:8]
        started = self.service.start_session(
            repository=repository, mode="strict",
            run_id=ctx.run_dir.name, trace_id=self.trace_id,
            source_revision=self.source_revision, knowledge_snapshot=self.snapshot,
            resource_revision=str(pin.get("tree_sha256") or view.tree_sha256 or ""),
            instrumentation={"prompt_boundary": "observed",
                             "copilot_document_tools": "observed",
                             "native_file_reads": "unknown",
                             "applications": "host_reported"})
        ctx.state["knowledge_trace_id"] = self.trace_id
        ctx.state["knowledge_trace_root"] = str(self.root)
        ctx.state["knowledge_trace_status"] = "running"
        if started.get("status") == "degraded":
            self.failed("start_session: " + str(started.get("error", "storage unavailable")))
        ctx.trace.record("knowledge_trace", trace_id=self.trace_id,
                         trace_root=str(self.root), status=ctx.state["knowledge_trace_status"])

    def call(self, method: str, *args, **kwargs):
        try:
            result = getattr(self.service, method)(self.trace_id, *args, **kwargs)
            if isinstance(result, dict) and result.get("status") == "degraded" and result.get("error"):
                self.failed(method + ": " + str(result["error"]))
                return None
            return result
        except Exception as exc:
            self.failed(f"{method}: {type(exc).__name__}: {exc}")
            return None

    def failed(self, error: str):
        self.error = error
        self.ctx.state["knowledge_trace_status"] = "degraded"
        self.ctx.trace.record("knowledge_trace_error", trace_id=self.trace_id, error=error)
        self.check()

    def check(self):
        if self.error and self.mode == "required":
            raise RuntimeError("required knowledge trace failed: " + self.error)

    def finish(self, status: str):
        self.call("finish_session", status="degraded" if self.error else status)
        report = self.call("report")
        if report is None:
            return
        from ...knowledge_trace import markdown

        try:
            artifact = {**report, "trace_root": str(self.root), "trace_id": self.trace_id}
            self.ctx.run_dir.mkdir(parents=True, exist_ok=True)
            (self.ctx.run_dir / "KNOWLEDGE_TRACE.json").write_text(
                json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
            (self.ctx.run_dir / "KNOWLEDGE_TRACE.md").write_text(
                markdown(report) + f"\nTrace store: `{self.root}`.\n", encoding="utf-8")
        except OSError as exc:
            self.failed(f"artifact_export: {type(exc).__name__}: {exc}")
        self.ctx.state["knowledge_trace_status"] = "degraded" if self.error else status


def get_knowledge_trace(ctx: StepContext) -> StrictKnowledgeTrace | None:
    """Lazy per-run initialization; ensemble steps share the same trace object."""
    if _STATE_KEY in ctx.state:
        return ctx.state[_STATE_KEY]
    mode = str(getattr(ctx.settings, "knowledge_trace_mode", "")
               or os.environ.get("KNOWLEDGE_TRACE_MODE", "best_effort"))
    if mode == "off":
        ctx.state[_STATE_KEY] = None
        return None
    try:
        trace = StrictKnowledgeTrace(ctx, mode)
    except Exception as exc:
        ctx.state["knowledge_trace_status"] = "degraded"
        ctx.trace.record("knowledge_trace_error", error=f"{type(exc).__name__}: {exc}")
        if mode == "required":
            raise RuntimeError(f"required knowledge trace failed: {exc}") from exc
        trace = None
    ctx.state[_STATE_KEY] = trace
    return trace


def finish_knowledge_trace(state: dict, status: str) -> None:
    trace = state.get(_STATE_KEY)
    if trace is not None:
        trace.finish(status)


class StepKnowledgeTrace:
    """One step's pending content, observed evidence, and optional assessments."""

    def __init__(self, trace: StrictKnowledgeTrace, step_id: str):
        self.trace, self.step_id = trace, step_id
        # A playbook may invoke the same runtime step for multiple foreach
        # items or retries. Local evidence names (input.*, tool.*) belong to
        # one invocation, even when the caller reuses the step name or a tool
        # provider reuses its call IDs.
        self.invocation_id = "ki-" + uuid.uuid4().hex
        self.pending: list[dict] = []
        self.seen_tools: set[str] = set()
        self.evidence: dict[str, dict] = {}
        self.new_references: list[dict] = []

    def retrieve(self, entries: list[dict], mechanism: str, *, query: str = "",
                 status: str = "ready"):
        return self.trace.call("retrieve", step_id=self.step_id, entries=entries, mechanism=mechanism,
                               query=query, status=status)

    def queue(self, document_id: str, content: str, *, representation="excerpt",
              offset=None, truncated=False, match_text: str | list[str] | None = None,
              line: int | None = None, fingerprint=True,
              offset_unit="unicode_codepoints", source_length: int | None = None):
        if content:
            root = Path(self.trace.ctx.settings.knowledge_dir).resolve()
            path = (root / document_id).resolve()
            sha = ""
            if fingerprint and path.is_relative_to(root) and path.is_file():
                from ...knowledge_service.lifecycle import visible_text

                try:
                    raw = path.read_bytes()
                    sha = hashlib.sha256(raw).hexdigest()
                    visible = visible_text(raw.decode("utf-8", errors="replace"))
                    if line is not None:
                        offset = sum(len(part) for part in visible.splitlines(keepends=True)[:line - 1])
                    elif offset is None:
                        found = visible.find(content)
                        offset = found if found >= 0 and found == visible.rfind(content) else None
                except OSError as exc:
                    self.trace.failed(f"knowledge document fingerprint unavailable: {type(exc).__name__}")
            self.pending.append({"document_id": document_id, "content": content,
                                 "representation": representation, "offset": offset,
                                 "document_sha256": sha,
                                 "truncated": truncated,
                                 "offset_unit": offset_unit, "source_length": source_length,
                                 "match_text": content if match_text is None else match_text})

    def adaptive_packet(self, packet: dict, mechanism: str, *, query: str = ""):
        """Queue only provider-rendered fragments surviving the context budget.

        The context service already verified, scoped, and metered this payload.
        Reading its document references again would lose truncation and could
        introduce content which never reached the model.
        """
        from ...knowledge_trace.adaptive import packet_fragments
        from ...knowledge_trace.references import KnowledgeTraceError, document_scope

        try:
            fragments = packet_fragments(packet)
        except (ValueError, TypeError, KeyError) as exc:
            self.gap("adaptive_packet_invalid", detail=str(exc))
            return
        accepted = []
        for fragment in fragments:
            document_id = fragment["document_id"]
            try:
                document_scope(document_id, self.trace.repository)
            except KnowledgeTraceError:
                self.gap("context_cross_repository_unobserved", document_id=document_id,
                         repo_id=fragment.get("repo_id", ""),
                         detail="Context service authorized delivery; this trace supports one repository scope.")
                continue
            accepted.append(fragment)
        self.retrieve([{"document_id": f["document_id"],
                        "repo_id": f.get("repo_id", ""), "source_pins": f.get("source_pins", []),
                        "context_session_id": packet.get("session_id", "")}
                       for f in accepted], mechanism, query=query, status=packet.get("status", "ready"))
        for fragment in accepted:
            wire = fragment["wire_content"]
            # Briefings inject the fence directly; tool responses JSON-encode
            # the containing model_content field once more.
            self.queue(fragment["document_id"], fragment["content"],
                       representation="adaptive_excerpt", offset=fragment.get("offset"),
                       truncated=bool(fragment.get("truncated")), fingerprint=False,
                       offset_unit=fragment.get("offset_unit", "unicode_codepoints"),
                       source_length=fragment.get("source_length"),
                       match_text=[wire, json.dumps(wire, ensure_ascii=False)[1:-1]])

    def briefing_document(self, *, document_id: str, content: str, truncated: bool, offset=0):
        self.retrieve([{"document_id": document_id}], "briefing")
        self.queue(document_id, content, representation="briefing", truncated=truncated, offset=offset)

    def gap(self, reason: str, **details):
        self.trace.call("record_events", [{"event_type": "gap_recorded",
            "step_id": self.step_id, "gap": {"reason": reason, **details}}])

    def external_context(self, entries: list[dict], mechanism: str, query=""):
        self.retrieve(entries, mechanism, query=query,
                      status="ready" if entries else "no_matches")
        if entries:
            self.gap("external_context_not_a_knowledge_document", mechanism=mechanism,
                     detail="Skill/debug-memory identity is recorded; curated document provenance is unavailable.")

    def tool_result(self, name: str, arguments: dict, result: str):
        """Called by document tools; retrieval is distinct from later delivery."""
        if name == "doc_read":
            path = str(arguments["path"])
            self.retrieve([{"document_id": path}], "doc_read")
            self.queue(path, result, representation=("full_document" if not arguments.get("offset")
                       and not arguments.get("truncated") else "excerpt"), offset=arguments.get("offset", 0),
                       truncated=bool(arguments.get("truncated")))
        elif name == "doc_search":
            hits = arguments["hits"]
            self.retrieve([{"document_id": h["path"], "line": h["line"]} for h in hits],
                          name, query=arguments.get("query", ""),
                          status="ready" if hits else "no_matches")
            for hit in hits:
                self.queue(hit["path"], hit["text"], representation="search_hit",
                           match_text=result, line=hit["line"])
        elif name == "doc_related":
            documents = arguments.get("documents", [])
            self.retrieve([{"document_id": d["path"], "reason": d.get("match_reason", "")}
                           for d in documents], name, query=arguments.get("query", ""),
                          status=arguments.get("status", "ready"))
            for doc in documents:
                content = str(doc.get("content") or "")
                # Related context is encoded as JSON inside the prompt/tool
                # result; preserve its exact decoded excerpt and match wire text.
                encoded = json.dumps(content, ensure_ascii=False)[1:-1]
                self.queue(doc["path"], content, representation="related_excerpt",
                           truncated=bool(doc.get("more_available")), match_text=encoded)

    def observe(self, messages: list[dict], boundary="model_request"):
        self.trace.check()
        revision = {"source_revision": "", "workspace_state": "unknown", "commit_verified": False}
        repo = self.trace.ctx.state.get("repo_path")
        if repo:
            try:
                result = subprocess.run(
                    ["git", "status", "--porcelain=v2", "--branch", "--untracked-files=normal"],
                    cwd=repo, capture_output=True, text=True, timeout=5, check=False)
                if result.returncode == 0:
                    lines = result.stdout.splitlines()
                    head = next((line.removeprefix("# branch.oid ") for line in lines
                                 if line.startswith("# branch.oid ")), "")
                    dirty = any(line and not line.startswith("#") for line in lines)
                    revision.update(observed_head=head, workspace_state="dirty" if dirty else "clean",
                                    source_revision=head if not dirty and len(head) == 40 else "")
            except (OSError, subprocess.TimeoutExpired):
                pass
        for evidence in self.evidence.values():
            if "workspace_state" not in evidence:
                evidence.update(revision)
        texts: list[str] = []
        for message in messages:
            content = message.get("content", "")
            if isinstance(content, str):
                texts.append(content)
                continue
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result":
                    result = str(block.get("content", ""))
                    texts.append(result)
                    tool_id = str(block.get("tool_use_id", ""))
                    if tool_id and tool_id not in self.seen_tools:
                        self.seen_tools.add(tool_id)
                        self.evidence["tool." + tool_id] = {
                            "kind": "tool", "tool_call_id": tool_id, "content": result, **revision}
                elif block.get("type") == "text":
                    texts.append(str(block.get("text", "")))
        remaining = []
        for item in self.pending:
            needles = item["match_text"] if isinstance(item["match_text"], list) else [item["match_text"]]
            # The review briefing escapes '<' inside related context.
            if any(needle in text or needle.replace("<", "\\u003c") in text
                   for text in texts for needle in needles):
                fields = {k: v for k, v in item.items() if k != "match_text"}
                recorded = self.trace.call("provide", step_id=self.step_id, boundary=boundary,
                                           snapshot=self.trace.snapshot, **fields)
                if recorded:
                    result = recorded.get("result", {})
                    for ref in [{"knowledge_ref_id": result.get("knowledge_ref_id")},
                                *result.get("rule_refs", [])]:
                        self.new_references.append({"document_id": item["document_id"],
                            "knowledge_ref_id": ref.get("knowledge_ref_id"),
                            "rule_id": ref.get("rule_id", ""),
                            "exposure_ids": [result.get("exposure_id")]})
            else:
                remaining.append(item)
        self.pending = remaining

    def assessments(self, output: dict):
        assessments = output.get("knowledge_assessments")
        if not assessments:
            return
        if not isinstance(assessments, list):
            self.trace.call("record_events", [{"event_type": "gap_recorded",
                "step_id": self.step_id, "gap": {"reason": "knowledge_assessments must be a list"}}])
            return
        events, registered = [], set()
        decisions = {}
        for index, finding in enumerate(output.get("findings") or []):
            identity = str(finding.get("finding_id") or finding.get("id") or f"finding:{index}") \
                if isinstance(finding, dict) else f"finding:{index}"
            decisions[identity] = {"decision_id": identity, "finding": finding}
        for assessment in assessments:
            if not isinstance(assessment, dict):
                self.gap("invalid_knowledge_assessment", detail="assessment must be an object")
                continue
            if not isinstance(assessment.get("evidence_ids", []), list) or any(
                    not isinstance(eid, str) for eid in assessment.get("evidence_ids", [])):
                self.gap("invalid_knowledge_assessment", detail="evidence_ids must be a list of strings")
                continue
            updated = dict(assessment)
            updated["invocation_id"] = self.invocation_id
            decision_ids = assessment.get("decision_ids") or []
            if not isinstance(decision_ids, list) or any(not isinstance(did, str) for did in decision_ids):
                self.gap("invalid_decision_ids", detail="decision_ids must be a list of strings")
                decision_ids = []
            missing = [did for did in decision_ids if did not in decisions]
            if missing:
                self.gap("unknown_decision_reference", decision_ids=missing)
            updated["decision_ids"] = [did for did in decision_ids if did in decisions]
            updated["decision_refs"] = [decisions[did] for did in updated["decision_ids"]]
            evidence_key = lambda eid: "kevidence-" + hashlib.sha256(
                (self.invocation_id + ":" + str(eid)).encode()).hexdigest()[:32]
            updated["evidence_ids"] = [evidence_key(eid)
                                      for eid in assessment.get("evidence_ids", [])]
            for local_id in assessment.get("evidence_ids", []):
                evidence_id = evidence_key(local_id)
                if local_id in self.evidence and evidence_id not in registered:
                    registered.add(evidence_id)
                    events.append({"event_type": "evidence_registered", "step_id": self.step_id,
                        "evidence": {**self.evidence[local_id], "evidence_id": evidence_id,
                                     "invocation_id": self.invocation_id,
                                     "evidence_origin": "captured"}})
            events.append({"event_type": "assessment_recorded", "step_id": self.step_id,
                           "assessment": {**updated, "reported_by": "host"}})
        if events:
            for start in range(0, len(events), 100):
                self.trace.call("record_events", events[start:start + 100])

    def wrap_llm(self, llm):
        step_trace = self

        class ObservedLLM:
            def __getattr__(self, name: str) -> Any:
                return getattr(llm, name)

            def create(self, **kwargs):
                step_trace.observe(kwargs.get("messages", []))
                if step_trace.new_references and kwargs.get("messages"):
                    references = step_trace.new_references[:32]
                    if len(step_trace.new_references) > len(references):
                        step_trace.gap("reference_map_truncated", supplied=len(references),
                                       available=len(step_trace.new_references))
                    note = "Knowledge trace references (optional assessments may cite these exact IDs):\n" + json.dumps(
                        references, ensure_ascii=False)
                    step_trace.new_references.clear()
                    message = kwargs["messages"][-1]
                    if isinstance(message.get("content"), list):
                        message["content"].append({"type": "text", "text": note})
                    else:
                        message["content"] = str(message.get("content", "")) + "\n\n" + note
                return llm.create(**kwargs)

        return ObservedLLM()
