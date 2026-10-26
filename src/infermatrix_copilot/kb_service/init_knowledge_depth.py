"""Incremental feature implementation depth; existing rule deepen is unchanged."""

from __future__ import annotations

import json
import hashlib
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from ..knowledge_service.lifecycle import Page
from ..knowledge_service.ops import page_over_capacity
from ..knowledge_service.pinned_claims import Evidence, check_rules, evidence_for
from .depth_inputs import DepthContext, SYSTEM_DEPTH, prompt
from .depth_judge import review_facets
from .init_budget import BudgetExhausted
from .init_coverage import Owner
from .init_history import _checkpoint_budget
from .init_knowledge import MAX_DOC_BYTES, _Knowledge, _MARKER
from .init_stages import _one_line, _page_frontmatter
from .init_support import InitError, InitRecord, generate
from .knowledge_coverage import audit_coverage, coverage_targets_met, feature_metadata, inventory
from .knowledge_depth import (
    FACETS, audit_depth, build_absence_certificate, depth_page, digest,
    render_block, validate_draft, verified_blocks,
)
from .models import ModelUnavailable


def validate_container(data: dict) -> None:
    """Bound candidates; per-facet validation discards duplicates independently."""
    if not isinstance(data, dict) or not isinstance(data.get("sections"), list) or len(data["sections"]) > 2 * len(FACETS):
        raise ValueError("sections must be a bounded list of candidate facets")


def _stable_prompt(payload: dict) -> str:
    # Checkpoint JSON sorts keys. Canonicalize before the first dispatch too,
    # so a saved successful extraction resumes byte-for-byte without a call.
    value = json.loads(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    return prompt(value, mode=value.get("acceptance_mode", "strict")) if "acceptance_mode" in value else prompt(value)


@dataclass
class _KnowledgeDepth(_Knowledge):
    STAGE = "knowledge-deepen"
    retry_unfinished: bool = False
    feature_ids: tuple[str, ...] = ()  # execution partition, never a smaller audit denominator
    acceptance_mode: str = "strict"
    depth_index_path: Path | None = None
    stop_file: Path | None = None

    def _chain_boundary(self) -> str:
        return "knowledge"  # same prerequisites as explanatory knowledge, no shadow flip

    def _cache_reusable(self, previous: InitRecord) -> bool:
        return bool(previous.depth.get("done")) and not self.retry_unfinished

    def _mode_identity(self) -> bool:
        return False  # promote a checked preview without repeating extraction

    def _init_identity(self) -> str:
        identity = repr(replace(self.lifecycle.init, budget_usd=0.0))
        if not self.lifecycle.init.feature_discovery_required:
            identity = identity.replace(", feature_discovery_required=False", "")
        return identity + (
            ":subscription-generator" if self.rt.subscription_generator else "") + (
            ":unlimited-subscription" if getattr(self.rt, "unlimited_subscription", False) else "")

    def _input_options(self) -> dict:
        foundation = {}
        if self.foundation_mode == "partial":
            if getattr(self.rt, "portable_spec", None):
                from .portable_init import check_local_acceptance
                record = InitRecord.load(self.rt.state_dir, self.lifecycle.repo, "knowledge")
                if record is None or record.status != "accepted" or record.pin != self._knowledge_run_pin:
                    raise InitError("portable depth requires an accepted foundation at the same source pin")
                head = check_local_acceptance(self.rt, record)
                binding = {"local_head_sha": head, "inputs_digest": record.inputs_digest,
                           "catalog_binding": record.discovery.get("catalog_binding"), "init_complete": False}
            else:
                from .foundation_publication import foundation_handoff
                binding = foundation_handoff(self.foundation_record_path, knowledge=self.rt.knowledge,
                            baseline=self._base_sha, repo=self.lifecycle.repo, pin=self._knowledge_run_pin,
                            publisher=self._publisher())
            self._foundation_binding = binding
            foundation = {"foundation_mode": "partial", "foundation": binding}
        if self.acceptance_mode == "lightweight":
            from .depth_index import INDEX_VERSION
            index_options = {"acceptance_mode": self.acceptance_mode, "depth_index_version": INDEX_VERSION}
            if self.depth_index_path and self.depth_index_path.exists():
                index_options["depth_index_sha256"] = hashlib.sha256(self.depth_index_path.read_bytes()).hexdigest()
        else:
            index_options = {}
        guidance_path = os.environ.get("KB_DEPTH_REPAIR_GUIDANCE")
        if guidance_path:
            from .depth_inputs import load_repair_guidance
            self._repair_guidance = load_repair_guidance(Path(guidance_path))
            expected_hash = os.environ.get("KB_DEPTH_REPAIR_GUIDANCE_SHA256")
            if expected_hash and self._repair_guidance["guidance_sha256"] != expected_hash:
                raise InitError("repair guidance differs from the campaign's immutable input")
            index_options["repair_guidance_sha256"] = self._repair_guidance["guidance_sha256"]
        return {**super()._input_options(), "depth_version": 5 if self.acceptance_mode == "lightweight" else 4,
                "depth_feature_ids": list(self.feature_ids),
                **index_options, **foundation}

    def _base_for_run(self, latest: str) -> str:
        previous = InitRecord.load(self.rt.state_dir, self.lifecycle.repo, self.STAGE)
        if previous and previous.depth:
            if self.retry_unfinished and previous.status == "published":
                raise InitError("published depth batch is immutable; use a new state directory after merging its PR")
            if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", previous.kb_base_sha):
                raise InitError("depth checkpoint has an invalid knowledge baseline")
            if self.rt.knowledge._git("cat-file", "-t", previous.kb_base_sha).decode().strip() != "commit":
                raise InitError("depth checkpoint knowledge baseline is unavailable")
            if not self.pin:
                self.pin = previous.pin
            return previous.kb_base_sha
        return latest

    def _restore_progress(self, previous: InitRecord | None) -> list[str]:
        if previous and previous.depth:
            current = self.record.inputs_digest
            self.record = previous
            if previous.inputs_digest != current:
                return ["depth inputs changed; use a new state directory for a new pinned batch"]
            self.record.status = "started"
            self.record.problems = []
            self.record.dry_run = self.dry_run
        self.budget = _checkpoint_budget(None if getattr(self.rt, "unlimited_subscription", False)
                                       else self.lifecycle.init.budget_usd, self.record, self.rt.state_dir)
        return []

    def _precheck(self) -> list[str]:
        problems = super()._precheck()
        if not getattr(self, "coverage_policy", None):
            problems.append("knowledge-deepen requires an explicit feature/production coverage policy")
        elif set(self.feature_ids) - {f.id for f in self.coverage_policy.features}:
            problems.append("depth execution partition contains unknown policy features")
        if self.acceptance_mode not in ("strict", "lightweight"):
            problems.append("acceptance_mode must be strict or lightweight")
        if getattr(self, "_repair_guidance", None) and self.acceptance_mode != "lightweight":
            problems.append("repair guidance requires lightweight recognition")
        return problems

    def _refresh_quick_maps(self) -> list[str]:
        # Depth adds explanatory pages and index links, without changing routes
        # or review rules. Keep existing quick maps, including manual additions
        # inside generated sections; _conclude still checks presence/capacity.
        return []

    def _build(self, tree: Path) -> InitRecord:
        policy = self.coverage_policy
        self.today = time.strftime("%Y-%m-%d", time.gmtime(self.record.started_at))
        state = self.record.depth
        if self.foundation_mode == "partial":
            state["foundation_mode"] = "partial"
            state["foundation"] = self._foundation_binding
            state["execution_scope"] = {"feature_ids": list(dict.fromkeys(self.feature_ids))
                if self.feature_ids else [f.id for f in policy.features], "complete": False}
        state.setdefault("version", 1)
        states = state.setdefault("features", {})
        accepted = state.setdefault("accepted", {})
        expected = {depth_page(f) for f in policy.features}
        if set(accepted) - expected or set(states) - {f.id for f in policy.features}:
            return self._blocked(["depth checkpoint contains pages or features outside the current policy"])
        self._block_cache = {}
        for feature in policy.features:
            page = depth_page(feature)
            if page in accepted:
                blocks, problems = verified_blocks(accepted[page], feature, tree, self.record.pin, policy=policy)
                entry = states.get(feature.id, {})
                if problems or not blocks or entry.get("accepted_sha256") != digest(accepted[page]):
                    return self._blocked(["depth checkpoint accepted page failed its pinned proof"])
                self.head[page] = accepted[page]
                self._block_cache[feature.id] = blocks
                self._link_page(page, _one_line(feature.title) + "：实现深读")
            elif page in self.head:
                blocks, problems = verified_blocks(self.head[page], feature, tree, self.record.pin, policy=policy)
                if problems:
                    return self._blocked(problems + ["refresh stale/edited depth explicitly before extending it"])
                self._block_cache[feature.id] = blocks
                # A new batch may retain independently approved depth pages
                # while its frozen foundation navigation stays unchanged.
                # Reconnect those verified pages even when no facet needs a
                # fresh extraction; do not rewrite their accepted blocks.
                if blocks:
                    self._link_page(page, _one_line(feature.title) + "：实现深读")
        initial = audit_depth(self.head, tree, policy, self.record.pin)
        if initial["problems"]:
            return self._blocked(initial["problems"])
        breadth = audit_coverage(self.head, tree, policy, full_name=self.lifecycle.full_name, pin=self.record.pin)
        if policy.semantic_depth_per_facet_gt is not None and not coverage_targets_met(breadth, self.foundation_mode):
            self._replace_coverage({"semantic_depth": initial, "breadth": breadth})
            state["done"] = False
            return self._blocked(["structural coverage target is unmet; restore feature and production-file coverage before depth extraction"])
        if self.acceptance_mode == "lightweight":
            policy_path = self._coverage_policy_path()
            policy_text = self.overlay.get(policy_path) or self.rt.knowledge.show(self._base_sha, policy_path)
            context = DepthContext(tree, inventory(tree, policy), mode="lightweight",
                                   cache_path=self.depth_index_path, pin=self.record.pin,
                                   policy_sha256=digest(policy_text), discovery=self._discovery_catalog())
            if getattr(self, "_repair_guidance", None):
                from .depth_inputs import load_repair_guidance
                checked_guidance = load_repair_guidance(
                    Path(os.environ["KB_DEPTH_REPAIR_GUIDANCE"]), pin=self.record.pin,
                    policy_sha256=digest(policy_text), baseline=self._base_sha)
                if checked_guidance["guidance_sha256"] != self._repair_guidance["guidance_sha256"]:
                    raise InitError("repair guidance changed after checkpoint identity was captured")
        else:
            context = DepthContext(tree, inventory(tree, policy), discovery=self._discovery_catalog())
        by_id = {f.id: f for f in policy.features}
        selected_features = tuple(by_id[fid] for fid in dict.fromkeys(self.feature_ids)) if self.feature_ids else policy.features
        # Finish a fair first pass before targeted repairs. Subscription mode
        # stops on converged evidence rather than on an artificial USD ceiling.
        unlimited = getattr(self.rt, "unlimited_subscription", False)
        ceilings = {f.id: states.get(f.id, {}).get("attempts", 0) + 2 if self.retry_unfinished else 2
                    for f in policy.features}
        for feature in policy.features:
            slots = states.setdefault(feature.id, {}).setdefault("facets", {})
            for facet in initial["features"][feature.id]["unknown_facets"]:
                slot = slots.setdefault(facet, {"status": "unknown", "attempts": 0, "evidence_visits": {}})
                if self.retry_unfinished and self.acceptance_mode == "strict":
                    slot.pop("blocked", None)
                    slot["evidence_visits"] = {}
        stopped = False
        evidence_round = 0
        if self.acceptance_mode == "lightweight":
            stopped = self._run_lightweight(tree, context, states, initial, selected_features)
        while True:
            if self.acceptance_mode == "lightweight":
                break
            progress = audit_depth(self.head, tree, policy, self.record.pin)
            order = sorted(selected_features, key=lambda f: bool(progress["features"][f.id]["recognized_facets"]))
            attempted = False
            for feature in order:
                entry = states.setdefault(feature.id, {})
                if self._pending_dispatch(entry):
                    stopped = True
                    continue
                current = progress["features"][feature.id]
                missing = current["unknown_facets"]
                if not missing:
                    entry["status"] = "complete"
                    continue
                attempts = entry.get("attempts", 0)
                pending_draft = "draft" in entry
                if not unlimited and not pending_draft and ((not evidence_round and attempts >= ceilings[feature.id] - 1)
                                                            or attempts >= ceilings[feature.id]):
                    if entry.get("status") == "extracting":
                        entry["status"] = "interrupted"
                        entry["reason"] = "extraction interrupted before a durable draft"
                    continue
                available = [f for f in missing if not entry["facets"][f].get("blocked")]
                if not available:
                    continue
                if not self.budget.can_reserve(self.lifecycle.init.judge_call_usd):
                    stopped = True
                    break
                if not pending_draft:
                    priority = sorted(available, key=lambda f: (
                        entry["facets"][f]["attempts"], progress["facet_counts"][f]["recognized"], FACETS.index(f)))
                    entry["requested_facets"] = priority if evidence_round == 0 or not unlimited else priority[:2]
                    entry["evidence_round"] = attempts
                attempted = True
                try:
                    self._attempt(tree, context, feature, entry)
                except BudgetExhausted:
                    stopped = True
                    break
                except (ModelUnavailable, ValueError) as exc:
                    entry["status"] = "unavailable"
                    entry["reason"] = str(exc)[:1000]
                    for facet in entry.get("requested_facets", []):
                        self._slot_result(entry, facet, "unjudged", entry["reason"])
                    entry.pop("draft", None)
                    entry.pop("payload", None)
                finally:
                    self.record.save(self.rt.state_dir)
            if stopped:
                break
            evidence_round += 1
            if not attempted or (not unlimited and evidence_round >= 2):
                break
        if accepted:
            for feature in policy.features:
                for page in (feature.page, depth_page(feature)):
                    if page in self.head:
                        self.head[page] = feature_metadata(self.head[page], feature)
        depth = audit_depth(self.head, tree, policy, self.record.pin)
        if self.foundation_mode == "partial":
            state["execution_scope"] = {"feature_ids": [f.id for f in selected_features], "complete": not stopped}
        state["target_met"] = depth["target_met"]
        state["all_resolved"] = not any(x["unknown_facets"] for x in depth["features"].values())
        breadth = audit_coverage(self.head, tree, policy, full_name=self.lifecycle.full_name, pin=self.record.pin)
        state["done"] = not stopped and (depth["target_met"] and coverage_targets_met(breadth, self.foundation_mode)
                                         if policy.semantic_depth_per_facet_gt is not None else True)
        self._replace_coverage({"semantic_depth": depth, "breadth": breadth})
        if self.foundation_mode == "partial":
            self.record.coverage["foundation"] = {"mode": "partial", "init_complete": False,
                "foundation_targets_met": breadth["met"],
                "structural_targets_met": coverage_targets_met(breadth, "partial")}
        self.record.unfinished = [f"depth {id_}/{facet}" for id_, item in depth["features"].items()
                                  for facet in item["unknown_facets"]]
        self.record.notes = [n for n in self.record.notes if not n.startswith("knowledge depth stopped:")]
        self.record.notes.append("knowledge depth stopped: " + ("budget exhausted; checkpoints preserve remaining work"
                                  if stopped else "all features visited; unsupported facets remain explicit"))
        self.record.save(self.rt.state_dir)
        claims = {page: text for page, text in accepted.items()}
        entries = [Evidence.from_dict(e) for group in self.record.evidence.values() for e in group]
        return self._conclude(claims, entries)

    @staticmethod
    def _pending_dispatch(entry):
        pending = ((entry.get("extraction_dispatched") and "draft" not in entry)
                   or (entry.get("review_dispatched") and "checked" not in entry))
        if pending:
            entry["status"] = "pending"
            entry["reason"] = "native execution outcome unknown; reconcile original dispatch before correction"
        return pending

    def _begin_light_round(self, entry, requested):
        """Charge a durable logical round before dispatch, including interrupted reviews."""
        entry["acceptance_mode"] = "lightweight"
        entry["light_round_id"] = entry.get("light_round_id", 0) + 1
        entry["requested_facets"] = list(requested)
        entry["review_dispatched"] = False
        for facet in requested:
            slot = entry["facets"][facet]
            slot["total_attempts"] = slot.get("total_attempts", 0) + 1
        self.record.save(self.rt.state_dir)

    def _run_lightweight(self, tree, context, states, initial, selected_features):
        from ..knowledge_service.drafting import AttemptPolicy

        corrections = AttemptPolicy(4)
        counts = {f: initial["facet_counts"][f]["recognized"] for f in FACETS}
        positions = {feature.id: i for i, feature in enumerate(selected_features)}
        while True:
            attempted = False
            held = False
            order = sorted(selected_features, key=lambda feature: (
                bool(self._block_cache.get(feature.id)),
                min((counts[f] for f in FACETS if f not in self._block_cache.get(feature.id, {})), default=80),
                positions[feature.id]))
            for feature in order:
                if self.stop_file and self.stop_file.exists():
                    return True
                entry = states.setdefault(feature.id, {})
                if self._pending_dispatch(entry):
                    held = True
                    continue
                old = set(self._block_cache.get(feature.id, {}))
                finishing = ("draft" in entry and (not entry.get("review_dispatched") or "checked" in entry))
                requested = [f for f in FACETS if f not in old and (
                    corrections.allows(entry["facets"][f].get("total_attempts", 0)) or
                    finishing and f in entry.get("payload", {}).get("facets", []))]
                if not requested:
                    entry["status"] = "complete" if len(old) == len(FACETS) else "exhausted"
                    continue
                attempted = True
                # A saved draft with no dispatched review continues its original round.
                if "draft" not in entry or entry.get("review_dispatched") and "checked" not in entry:
                    self._begin_light_round(entry, requested)
                else:
                    entry["requested_facets"] = requested
                entry["evidence_round"] = max(entry["facets"][f].get("total_attempts", 1) for f in requested) - 1
                try:
                    self._attempt(tree, context, feature, entry)
                except BudgetExhausted:
                    return True
                except (ModelUnavailable, ValueError, OSError, SyntaxError) as exc:
                    entry["status"] = "unavailable"
                    entry["reason"] = str(exc)[:1000]
                    for facet in requested:
                        self._slot_result(entry, facet, "unjudged", entry["reason"])
                    entry.pop("draft", None)
                    entry.pop("payload", None)
                    entry.pop("checked", None)
                finally:
                    self.record.save(self.rt.state_dir)
                for facet in set(self._block_cache.get(feature.id, {})) - old:
                    counts[facet] += 1
            if not attempted:
                return held

    def _prepare_publication(self, changed):
        depth = self.record.coverage.get("semantic_depth", {})
        unmet = self.coverage_policy.semantic_depth_per_facet_gt is not None and (
            not depth.get("target_met") or not coverage_targets_met(self.record.coverage.get("breadth", {}), self.foundation_mode))
        if unmet and not self.dry_run:
            return self._blocked(["semantic depth target is unmet; publication is blocked, checkpoint retained"])
        return super()._prepare_publication(changed)

    def _publish(self):
        if not self.dry_run:
            return super()._publish()
        result = super()._publish()
        depth = self.record.coverage.get("semantic_depth", {})
        unmet = self.coverage_policy.semantic_depth_per_facet_gt is not None and (
            not depth.get("target_met") or not coverage_targets_met(self.record.coverage.get("breadth", {}), self.foundation_mode))
        if unmet and result.status == "dry_run":
            result.status = "partial"
            result.notes.append("reviewable partial preview; semantic depth target is not met")
            result.save(self.rt.state_dir)
        return result

    @staticmethod
    def _slot_result(entry, facet, status, reason):
        from ..knowledge_service.drafting import AttemptPolicy

        slot = entry.setdefault("facets", {}).setdefault(facet, {"attempts": 0, "evidence_visits": {}})
        event = [entry.get("attempts", 0), entry.get("review_attempts", 0)]
        if entry.get("acceptance_mode") == "lightweight":
            event.append(entry.get("light_round_id", 0))
        if slot.get("last_attempt") == event and status != "pass":
            return
        history = slot.setdefault("history", [])
        outcome = {"attempt": event[0], "review_attempt": event[1], "status": status,
                   "reason": reason, "evidence_sha256": entry.get("evidence_sha256"),
                   "context_sha256": entry.get("context_sha256")}
        if not history or history[-1] != outcome:
            history.append(outcome)
        slot["last_attempt"] = event
        slot.update(status=status, reason=reason)
        if status == "pass":
            slot.pop("blocked", None)
            return
        if entry.get("acceptance_mode") == "lightweight":
            if not AttemptPolicy(4).allows(slot.get("total_attempts", 0)):
                slot["blocked"] = "initial attempt and three corrections exhausted; evidence remains unknown"
            return
        key = entry.get("evidence_sha256", "no-durable-evidence")
        visits = slot.setdefault("evidence_visits", {})
        visits[key] = visits.get(key, 0) + 1
        if not AttemptPolicy(3).allows(visits[key]):
            slot["blocked"] = "three unsuccessful repairs with the same pinned evidence; needs new evidence or a corrected candidate"

    def _docs(self, tree: Path, feature, owner, *, context=None, facets=(),
              previous_review=None, evidence_round=0) -> list[dict]:
        if context is not None and context.mode == "lightweight" and context.index is not None:
            # A source-only feature can still have a README/SKILL/design guide
            # beside its implementation. Select only real indexed ancestor
            # documents, never another component's sibling directory.
            by_parent = {}
            for path, fact in context.index.files.items():
                document = PurePosixPath(path)
                if (document.suffix.lower() not in
                        {".md", ".mdx", ".rst", ".txt", ".adoc"} or fact["sha256"] is None):
                    continue
                name = document.stem.lower()
                priority = next((number for number, prefix in enumerate(
                    ("skill", "readme", "design", "architecture", "reference"))
                    if name.startswith(prefix)), 5)
                by_parent.setdefault(document.parent.as_posix(), []).append((priority, path))
            adjacent = []
            for source in context._scope(feature):
                for distance, parent in enumerate(PurePosixPath(source).parents):
                    siblings = by_parent.get(parent.as_posix(), ())
                    adjacent.extend((distance, priority, path) for priority, path in siblings)
                    if any(priority < 4 for priority, _ in siblings):
                        break  # the nearest component guide is the local boundary
            paths = list(dict.fromkeys([*feature.docs, *(path for _, _, path in sorted(adjacent))]))
            filenames = {PurePosixPath(path).name for path in context._scope(feature)}
            preferred = []
            for path in paths:
                matches, examples, fenced = [], [], False
                for number, line in enumerate(context.index.files.get(path, {}).get("lines", ()), 1):
                    if line.lstrip().startswith(("```", "~~~")):
                        fenced = not fenced
                    if any(name in line for name in filenames):
                        matches.append(number)
                        if fenced:
                            examples.append(number)
                # Existing executable examples beat prose lists of unrelated
                # tools. Nearby output shapes/criteria remain real line ranges.
                preferred.extend((path, max(1, number - 6),
                                  min(len(context.index.files[path]["lines"]), number + 6))
                                 for number in (examples or matches))
            # The immutable index supplies complete real line ranges, including
            # late manual acceptance steps. The existing 8KB packet cap applies.
            docs, used = [], 0
            for number, path in enumerate(paths):
                selected = context.document_slices([], paths=(path,),
                           limit=(8000 - used) // (len(paths) - number), facets=facets,
                           previous_review=previous_review, evidence_round=evidence_round,
                           preferred_ranges=preferred)
                docs.extend(selected)
                used += sum(len(line.encode("utf-8")) + 1 for item in selected for line in item["text"])
            if docs:
                # build() rereads whole indexed documents. Offering a generic
                # owner path here would let that reread crowd out the local
                # command/criteria despite this first selection's byte cap.
                return docs
            generic = [item for item in self._docs_for(tree, owner) if item["path"] not in paths]
            return context.document_slices(generic, limit=8000, facets=facets,
                         previous_review=previous_review, evidence_round=evidence_round)
        docs = self._sources(tree, list(feature.docs), MAX_DOC_BYTES // 2)
        used = sum(len(d["text"].encode("utf-8")) for d in docs)
        for item in self._docs_for(tree, owner):
            if any(d["path"] == item["path"] for d in docs):
                continue
            lines, size = [], 0
            for line in item["text"].splitlines():
                amount = len(line.encode("utf-8"))
                if used + size + amount > MAX_DOC_BYTES:
                    break
                lines.append(line)
                size += amount
            if lines:
                docs.append({**item, "text": "\n".join(lines), "end": len(lines)})
                used += size
        return [{**d, "start": 1, "text": d["text"].splitlines()} for d in docs]

    def _judge_evidence(self, entries: list[Evidence]) -> list[dict]:
        # Supply every attested line, without the rule judge's 8KB truncation.
        # Merge overlaps so seven facets do not repeat the same large excerpt.
        from .evidence_bundle import merge_ranges

        out = []
        for path in dict.fromkeys(e.path for e in entries):
            lines = (self.observer.file_text(self.record.pin, path) or "").splitlines()
            for start, end in merge_ranges((e.start, e.end) for e in entries if e.path == path):
                out.append({"source_reference": f"{self.lifecycle.full_name}@{self.record.pin}:"
                                                 f"{path}:L{start}-L{end}",
                            "kind": "upstream_text", "text": [f"{n}: {lines[n - 1]}" for n in range(start, end + 1)]})
        return out

    def _feature_existing_context(self, feature, requested, background):
        """Keep this feature's supported foundation before owner background.

        Complete foundation sections are context, never evidence. In particular,
        a late validation section must not disappear behind frontmatter, indexes
        or another feature in the same component's alphabetic page order.
        """
        current = self.head.get(feature.page, "")
        markers = list(_MARKER.finditer(current))
        facets = {"flow": "architecture", "dependencies": "features",
                  "failure_modes": "api"}
        wanted = list(dict.fromkeys(facets.get(f, f) for f in requested))
        sections = []
        for number, marker in enumerate(markers):
            owner, facet, pin, verdict = marker.groups()
            if owner != "feature-" + feature.id or pin != self.record.pin or verdict not in (None, "pass"):
                continue
            end = markers[number + 1].start() if number + 1 < len(markers) else len(current)
            for boundary in ("<!-- kb:file ", "<!-- kb:depth "):
                found = current.find(boundary, marker.end(), end)
                if found != -1:
                    end = found
            sections.append((wanted.index(facet) if facet in wanted else len(wanted),
                             number, current[marker.start():end]))
        text = ""
        for _, _, section in sorted(sections):
            if len((text + section).encode()) <= 4000:
                text += section
        if not markers:
            text = current
        ordered = {feature.page: text} if text else {}
        ordered.update({p: t for p, t in self._bounded_context(background).items()
                        if p != feature.page and t != current})
        return self._bounded_texts(ordered, 4000)

    def _block_evidence(self, blocks):
        evidence = []
        for block in blocks.values():
            proof = json.loads(re.search(r"<!-- kb:depth-proof (.*?) -->", block, re.S).group(1))
            evidence += [evidence_for(self.observer, e["path"], e["start"], e["end"]) for e in proof["evidence"]]
        return evidence

    def _depth_content(self, prefix, blocks):
        text = prefix + "\n\n".join(blocks[f] for f in FACETS if f in blocks) + "\n"
        evidence = self._block_evidence(blocks)
        sources = [f"{self.lifecycle.full_name}@{self.record.pin}:{e.path}:L{e.start}-L{e.end}" for e in evidence]
        return Page.parse(text).with_sources(list(dict.fromkeys(sources))).render(), evidence

    def _attempt(self, tree, context, feature, entry):
        page = depth_page(feature)
        old = self.head.get(page, "")
        lightweight = self.acceptance_mode == "lightweight"
        old_blocks, problems = (self._block_cache.get(feature.id, {}), []) if lightweight else (
            verified_blocks(old, feature, tree, self.record.pin, policy=self.coverage_policy) if old else ({}, []))
        if problems:
            raise ValueError("; ".join(problems))
        owner = next((o for o in self.owners if o.owner == feature.owner),
                     Owner(feature.owner, feature.page, tuple(p.split("*", 1)[0] for p in feature.source_globs)))
        guidance = getattr(self, "_repair_guidance", None)
        requested = entry.get("requested_facets") or [f for f in FACETS if f not in old_blocks]
        docs = [] if guidance else self._docs(tree, feature, owner, context=context,
                    facets=requested, previous_review=entry.get("reason"),
                    evidence_round=entry.get("evidence_round", 0))
        existing = self._existing_knowledge(owner, self.head.get(feature.page, ""))
        if lightweight:
            docs = self._bounded_slices(docs, 8000)
            existing = self._feature_existing_context(feature, requested, existing)
        else:
            existing = self._bounded_context(existing)
        retrieval = context.guided(feature, requested, guidance) if guidance else context.build(
            feature, self.head.get(feature.page, ""), docs, facets=requested,
            previous_review=entry.get("reason"), evidence_round=entry.get("evidence_round", 0))
        if lightweight:
            if context.index is not None and not guidance:
                # Preserve the already selected local document ranges; a second
                # full-index rerank can replace their commands/criteria. Bind
                # the actual final packet, without changing any saved payload.
                retrieval["docs"] = docs
                retrieval["context_sha256"] = hashlib.sha256(json.dumps(
                    {key: value for key, value in retrieval.items() if key != "context_sha256"},
                    sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
            else:
                docs = retrieval.get("docs", docs)
        files = retrieval["files"]
        if not files and not (lightweight and docs):
            entry["attempts"] = entry.get("attempts", 0) + 1
            raise ModelUnavailable("feature has no implementation source slices")
        payload = {"repository": self.lifecycle.full_name, "pin": self.record.pin,
                   "feature": {"id": feature.id, "title": feature.title, "owner": feature.owner},
                   "facets": requested, **retrieval, "docs": docs,
                   "existing_knowledge": {p: text.splitlines() for p, text in existing.items()},
                   "language_sample": self._language_sample()}
        if lightweight:
            payload["acceptance_mode"] = "lightweight"
        if entry.get("reason"):
            payload["previous_review"] = entry["reason"]
        search = retrieval.get("test_search", {})
        certificate = build_absence_certificate(tree, self.coverage_policy, feature, self.record.pin) \
            if not lightweight and "validation" in requested and search.get("complete") and not search.get("matched_files") else None
        if certificate:
            payload["verified_absences"] = {"validation": certificate}
        if "draft" not in entry:
            entry["payload"] = payload
            entry["evidence_sha256"] = digest(json.dumps(
                {"files": sorted(files, key=lambda f: (f["path"], f["start"], f["end"])),
                 "docs": sorted(docs, key=lambda f: (f["path"], f["start"], f["end"])),
                 "absence": certificate}, sort_keys=True, ensure_ascii=False))
            entry["attempts"] = entry.get("attempts", 0) + 1
            for facet in requested:
                slot = entry.setdefault("facets", {}).setdefault(facet, {"attempts": 0, "evidence_visits": {}})
                slot["attempts"] += 1
            entry["status"] = "extracting"
            entry["context_sha256"] = digest(_stable_prompt(payload))
            self.record.save(self.rt.state_dir)
            from .depth_inputs import system_prompt
            from .quality_review import checkpoint_review
            checkpoint_review(entry, lambda: generate(self.rt, self.budget, self.lifecycle.init,
                system=system_prompt(self.acceptance_mode) if lightweight else SYSTEM_DEPTH,
                prompt=_stable_prompt(payload), validate=validate_container).data,
                lambda: self.record.save(self.rt.state_dir), binding={"input_sha256": entry["context_sha256"]},
                result_key="draft", dispatch_key="extraction_dispatched", attempts_key="extraction_calls")
            entry["status"] = "extracted"
            self.record.save(self.rt.state_dir)
        else:
            payload = entry.get("payload", payload)
            if entry.get("context_sha256") != digest(_stable_prompt(payload)):
                raise ValueError("checkpoint draft context changed")
            files, docs = payload["files"], payload["docs"]
            requested = [f for f in payload["facets"] if f in requested] if lightweight else payload["facets"]
            certificate = payload.get("verified_absences", {}).get("validation")
        data = entry["draft"]
        validate_container(data)
        sections = list(data["sections"])
        if certificate and not any(s.get("facet") == "validation" for s in sections if isinstance(s, dict)):
            anchor = files[0]
            sections.append({"facet": "validation", "title": "验证入口：已核验缺失", "basis": "verified_absent",
                             "interpretation": "fact", "absence_certificate": certificate,
                             "body": "在该固定源码版本及完整的已跟踪测试检查范围内，未找到可静态关联到本功能入口的自动化测试。"
                                     "这是测试入口缺口；动态或外部测试覆盖仍未验证，也不代表测试已执行或通过。",
                             "evidence": [{"path": anchor["path"], "start": anchor["start"],
                                           "end": min(anchor["end"], anchor["start"] + 20)}]})
        offered = files + docs
        blocks = dict(old_blocks)
        duplicates = Counter(s.get("facet") for s in sections if isinstance(s, dict) and isinstance(s.get("facet"), str))
        skipped = []
        offered_facets = set()
        for section in sections:
            try:
                if not isinstance(section, dict):
                    raise ValueError("depth section must be an object")
                if section.get("basis") == "verified_absent":
                    if section.get("facet") != "validation" or certificate is None:
                        raise ValueError("no independently replayable absence certificate")
                    section = {**section, "absence_certificate": certificate}
                validate_draft({"sections": [section]}, acceptance_mode=self.acceptance_mode)
                if duplicates[section["facet"]] > 1 or section["facet"] not in requested:
                    raise ValueError("duplicate or unrequested depth facet")
                for evidence in section["evidence"]:
                    if not any(item["path"] == evidence["path"] and item["start"] <= evidence["start"]
                               <= evidence["end"] <= item["end"] for item in offered):
                        raise ValueError("depth evidence crosses an unshown source gap")
                if not lightweight and section["facet"] == "flow" and any(s["path"] not in context.production for s in section["trace"]):
                    raise ValueError("flow must trace production implementation")
                blocks[section["facet"]] = render_block(feature, section, tree, self.lifecycle.full_name,
                                                       self.record.pin, policy=self.coverage_policy,
                                                       acceptance_mode=self.acceptance_mode)
                offered_facets.add(section["facet"])
            except (ValueError, TypeError, KeyError, SyntaxError) as exc:
                facet = str(section.get("facet", "invalid")) if isinstance(section, dict) else "invalid"
                skipped.append(facet + ": " + str(exc))
                if facet in requested:
                    self._slot_result(entry, facet, "invalid", str(exc))
        for facet in requested:
            if facet not in offered_facets and not any(s.startswith(facet + ":") for s in skipped):
                unknown = data.get("unknown_facets", {})
                if isinstance(unknown, list):
                    unknown = {u["facet"]: u.get("reason", "explicitly unsupported") for u in unknown
                               if isinstance(u, dict) and u.get("facet") in requested}
                if not isinstance(unknown, dict):
                    unknown = {}
                reason = unknown.get(facet, "generator omitted requested facet")
                self._slot_result(entry, facet, "unknown" if facet in unknown else "omitted", reason)
        entry["skipped_facets"] = skipped
        if blocks == old_blocks:
            raise ModelUnavailable("no supported new depth facets; " + "; ".join(skipped))
        front = _page_frontmatter(_one_line(feature.title) + "：实现深读", kind="architecture",
                                  today=self.today, tags=self.tags)
        related = f"[功能概览]({Path(feature.page).name}) · [owner 入口](_index.md)\n\n"
        if self.foundation_mode == "partial" and feature.page not in self.head:
            related = "[owner 入口](_index.md)\n\n"
        # Retrieve exactly the attested spans, including retained prior facets.
        proposed, evidence = self._depth_content(front + related, blocks)
        issues = check_rules({feature.id: proposed}, self.observer)
        if issues or page_over_capacity(proposed):
            raise ValueError("; ".join(issues) or "depth page exceeds capacity")
        hashed = digest(proposed)
        cached = entry.get("checked")
        if not cached or cached.get("text_sha256") != hashed:
            new = {f: block for f, block in blocks.items() if f not in old_blocks}
            if lightweight and self.stop_file and self.stop_file.exists():
                return  # durable draft is reviewed after resuming this same round
            if lightweight:
                shown = self._judge_evidence(self._block_evidence(new))
            else:
                shown = self._judge_evidence(evidence)
            for block in new.values():
                proof = json.loads(re.search(r"<!-- kb:depth-proof (.*?) -->", block, re.S).group(1))
                if proof.get("basis") == "verified_absent":
                    shown.append({"kind": "replayed_absence_certificate", "certificate": proof["absence_certificate"]})
            from .quality_review import checkpoint_review

            from .depth_judge import facet_review_binding

            checkpoint_review(entry, lambda: review_facets(self.rt, self.budget, self.lifecycle.init,
                feature=feature.id, pin=self.record.pin, blocks=new,
                existing={**existing, page: old}, evidence=shown,
                acceptance_modes={f: self.acceptance_mode for f in new}),
                lambda: self.record.save(self.rt.state_dir),
                binding=facet_review_binding(hashed, new, self.rt.judge,
                                             {f: self.acceptance_mode for f in new}),
                dispatch_key="review_dispatched")
        checked = entry["checked"]
        passed = {f for f, result in checked["facets"].items() if result["verdict"] == "pass"}
        labels = {result["verdict"] for result in checked["facets"].values()}
        label = "pass" if passed else "fail" if "fail" in labels else "unsure" if "unsure" in labels else "unjudged"
        key = "depth:" + feature.id
        prior = self.record.verdicts.setdefault(key, {"facets": {}, "calls": []})
        for f, result in checked["facets"].items():
            prior["facets"][f] = {**result, "block_sha256": digest(blocks[f]), "model": checked["model"],
                                  "native_trace_id": checked.get("native_trace_id", ""),
                                  "native_reply_sha256": checked.get("native_reply_sha256", "")}
            if lightweight:
                prior["facets"][f]["acceptance_mode"] = "lightweight"
            self._slot_result(entry, f, result["verdict"], result.get("reason", ""))
        prior["calls"].append(checked)
        entry["shown_source_files"] = sorted({f["path"] for f in files})
        entry["status"] = label
        entry["reason"] = json.dumps({"review": checked["facets"], "unsupported_facets": skipped}, ensure_ascii=False)
        if labels == {"unjudged"}:
            entry.pop("checked", None)
            return
        entry.pop("draft", None)
        entry.pop("checked", None)
        entry.pop("payload", None)
        if label != "pass":
            return
        blocks = {f: block for f, block in blocks.items() if f in old_blocks or f in passed}
        proposed, kept_evidence = self._depth_content(front + related, blocks)
        self.head[page] = proposed
        if lightweight:
            self._block_cache[feature.id] = blocks
        self.record.depth["accepted"][page] = proposed
        entry["accepted_sha256"] = digest(proposed)
        self.record.evidence[key] = [e.to_dict() for e in kept_evidence]
        self._link_page(page, _one_line(feature.title) + "：实现深读")

    @staticmethod
    def _bounded_slices(items, limit):
        out, used = [], 0
        for item in items:
            lines = item["text"] if isinstance(item["text"], list) else item["text"].splitlines()
            kept = []
            for line in lines:
                size = len(line.encode("utf-8")) + 1
                if used + size > limit:
                    break
                kept.append(line)
                used += size
            if kept:
                out.append({**item, "text": kept, "end": item.get("start", 1) + len(kept) - 1})
        return out

    @staticmethod
    def _bounded_texts(items, limit):
        out, used = {}, 0
        for path, value in items.items():
            raw = value.encode("utf-8")[:max(0, limit - used)]
            text = raw.decode("utf-8", errors="ignore")
            if text:
                out[path] = text
                used += len(text.encode("utf-8"))
        return out
