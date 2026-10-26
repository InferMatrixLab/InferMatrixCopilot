"""Owner-scoped explanatory knowledge, independent of rule-bearing coverage.

The optional ``knowledge`` init stage reads code and docs at the upstream pin
and appends evidence-backed sections to architecture pages. It visits every
source owner, including owners that already have rules. Missing facets and
unread files remain visible; a route or one rule is never proof of knowledge
depth. Existing sections are preserved and never silently re-pinned.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import yaml

from ..knowledge_service.facts import FactsError
from ..knowledge_service.l1 import Block
from ..knowledge_service.lifecycle import Page
from ..knowledge_service.ops import page_over_capacity
from ..knowledge_service.pinned_claims import check_rules, evidence_for
from .init_budget import BudgetExhausted
from .init_coverage import Owner
from .init_stages import _Chain, _Stage, _numbered, _one_line, _page_frontmatter, neutral_headings
from .init_support import InitRecord, classify_verdict, generate, judge
from .init_knowledge_inputs import (FOUNDATION_GENERATION_POLICY_VERSION, SYSTEM_KNOWLEDGE,
    knowledge_prompt, knowledge_system, source_owner, source_owners, foundation_evidence,
    foundation_uses_full_packet)
from .models import ModelUnavailable
from .init_knowledge_parallel import offered_ranges, parallelism, run_jobs, restore_jobs, preferred_sources, start_interval, range_is_offered, foundation_source_packet

FACETS = ("architecture", "api", "configuration", "tradeoffs", "features", "validation")
MAX_SOURCE_BYTES = 100_000
MAX_DOC_BYTES = 30_000
MAX_FILES = 60
_MARKER = re.compile(r"<!-- kb:knowledge owner=([a-z0-9-]+) facet=([a-z]+) pin=((?:[0-9a-f]{40}|[0-9a-f]{64}))"
                     r"(?: verdict=(pass|unsure|unjudged))? -->")
_OWNER = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")


def validate_sections(data: dict) -> None:
    sections = data.get("sections")
    if not isinstance(sections, list) or len(sections) > len(FACETS):
        raise ValueError("sections must be a list of at most six facets")
    seen = set()
    for section in sections:
        if not isinstance(section, dict) or section.get("facet") not in FACETS:
            raise ValueError("each section needs a known knowledge facet")
        facet = section["facet"]
        if facet in seen:
            raise ValueError("a facet may appear only once")
        seen.add(facet)
        body = section.get("body")
        if not isinstance(body, str) or not body.strip() or len(body) > 3000 \
                or re.search(r"(?m)^\s*#", body) or "<!-- kb:" in body:
            raise ValueError("a section needs bounded prose without headings or kb markers")
        if section.get("interpretation") not in ("fact", "inference"):
            raise ValueError("interpretation must be fact or inference")
        entries = section.get("evidence")
        if not isinstance(entries, list) or not entries or len(entries) > 8:
            raise ValueError("each facet needs one to eight evidence ranges")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) \
                    or type(entry.get("start")) is not int or type(entry.get("end")) is not int \
                    or not 1 <= entry["start"] <= entry["end"]:
                raise ValueError("evidence needs a path and a positive inclusive line range")


@dataclass
class _Knowledge(_Stage):

    GENERATION_TASK_JOURNALED = True

    STAGE = "knowledge"
    foundation_mode: str = "strict"
    foundation_record_path: Path | None = None

    def _chain(self) -> _Chain:
        if not self.from_existing:
            return super()._chain()
        # Explicit enrichment of a merged KB needs no fabricated stage records.
        # Existing records retain all their review and merge gates.
        if any(InitRecord.load(self.rt.state_dir, self.lifecycle.repo, stage)
               for stage in ("skeleton", "modules")):
            return super()._chain()
        from .init_coverage import owner_table

        base = self.rt.knowledge.knowledge_files(self._base_sha)
        try:
            manifest = yaml.safe_load(self.rt.knowledge.show(self._base_sha, self._manifest_path()) or "") or {}
            route_source, owners = owner_table(base.get(f"{self.repo_dir}/_routes.yaml"), manifest)
        except (ValueError, TypeError, yaml.YAMLError) as exc:
            return _Chain(problems=[f"--from-existing invalid owner routes: {exc}"])
        problems = []
        if not base.get(f"{self.repo_dir}/_index.md") or route_source == "none" or not owners:
            problems.append("--from-existing needs a merged repository index and owner routes")
        for owner in owners:
            if not owner.path.startswith(self.repo_dir + "/") or owner.path not in base:
                problems.append(f"--from-existing owner page missing or outside repository: {owner.path}")
        return _Chain(key=f"existing:{self._base_sha}", problems=problems)

    def _precheck(self) -> list[str]:
        from .knowledge_coverage import load_policy

        path = self._coverage_policy_path()
        text = self.overlay.get(path) or self.rt.knowledge.show(self._base_sha, path)
        if text:
            try:
                policy = load_policy(text, self.repo_dir)
            except (ValueError, TypeError) as exc:
                return [f"knowledge coverage policy: {exc}"]
            self.coverage_policy = policy
            if self.foundation_mode == "partial" and policy.semantic_depth_per_facet_gt is None:
                return ["partial foundation requires a final semantic-depth acceptance policy"]
            for feature in policy.features:
                existing = self.base.get(feature.page)
                if existing:
                    parsed = Page.parse(existing)
                    if parsed.frontmatter_field("type") not in ("architecture", "guide") or parsed.rules():
                        return [f"knowledge target {feature.page} must be an explanatory architecture or guide page"]
        return []

    def _input_options(self) -> dict:
        from .foundation_context import context_version
        path = self._coverage_policy_path()
        text = self.overlay.get(path) or self.rt.knowledge.show(self._base_sha, path) or ""
        options = {"knowledge_policy": hashlib.sha256(text.encode()).hexdigest(),
                   "from_existing": self.from_existing,
                   "knowledge_prompt_version": context_version(self.rt) if self.rt.unlimited_subscription else 3}
        # Bind source-only test metadata for capped and subscription jobs alike.
        # A prior execution fact must not be inferred from source localization.
        options["foundation_test_evidence_version"] = 2
        if self.STAGE == "knowledge":
            options["foundation_generation_policy_version"] = FOUNDATION_GENERATION_POLICY_VERSION
        if self.rt.unlimited_subscription:
            options["foundation_parallel"] = {"version": 1, "workers": parallelism(self.rt),
                                               "zcode_start_interval_s": start_interval(self.rt)}
        return options

    def _discovery_gate(self, chain: _Chain, pin: str) -> None:
        self._knowledge_run_pin = pin
        super()._discovery_gate(chain, pin)

    def _cache_reusable(self, previous: InitRecord) -> bool:
        if previous.status == "published":
            return True  # Preserve immutable historical publication records.
        if self.rt.unlimited_subscription and previous.status != "published":
            self._validate_unpublished_checkpoint(previous, previous.inputs_digest)
        if self.STAGE == "knowledge" and self.foundation_mode == "partial":
            return False  # Reconstruct a truthful publication-only scope from the saved native tasks.
        return not (self.rt.unlimited_subscription and previous.unfinished)

    def _resume_input_problems(self, previous: InitRecord, digest: str) -> list[str]:
        if self.rt.unlimited_subscription and previous.status != "published":
            # Raise before generic _blocked writes: restoring an archive must
            # leave the same genuine prepared publication resumable.
            self._validate_unpublished_checkpoint(previous, digest)
        if self.STAGE == "knowledge":
            from .foundation_publication import verify_prepared_receipt
            verify_prepared_receipt(self, previous)
        return super()._resume_input_problems(previous, digest)

    def _validate_unpublished_checkpoint(self, previous: InitRecord, digest: str) -> None:
        from .init_knowledge_parallel import validate_checkpoint
        validate_checkpoint(self, previous, digest)

    def _restore_progress(self, previous: InitRecord | None) -> list[str]:
        partial = self.STAGE == "knowledge" and self.foundation_mode == "partial"
        saved = previous.coverage.get("foundation_jobs", {}) if previous is not None else {}
        # A completed capped batch historically permits a new policy run. Keep
        # that behavior, but never discard the identity of unresolved dispatch.
        if (previous is not None and previous.inputs_digest != self.record.inputs_digest
                and saved.get("unlimited_subscription") is False and not saved.get("inflight")
                and not partial):
            return []
        if previous is not None and (partial or saved):
            self._validate_unpublished_checkpoint(previous, self.record.inputs_digest)
        if partial and previous is not None:
            from .foundation_publication import validate_initial_scope
            validate_initial_scope(self, previous)
        if (previous is not None
                and previous.coverage.get("foundation_jobs")
                and previous.inputs_digest != self.record.inputs_digest):
            from .init_support import InitError
            raise InitError("foundation inputs changed; create a new pinned batch")
        if (previous is not None and previous.coverage.get("foundation_jobs")
                and previous.inputs_digest == self.record.inputs_digest):
            from copy import deepcopy
            self.record.coverage["foundation_jobs"] = deepcopy(previous.coverage.get("foundation_jobs", {}))
            self.budget.spent_usd = previous.spent_usd
        return []

    def _prepare_publication(self, changed: dict[str, str]) -> InitRecord:
        targets = self.record.coverage.get("knowledge", {}).get("targets", {})
        if self.STAGE == "knowledge" and getattr(self.rt, "portable_spec", None):
            from .knowledge_coverage import coverage_targets_met

            owners = self.record.coverage.get("knowledge", {}).get("owners", {})
            self.record.coverage["foundation"] = {
                "mode": "partial", "tier": "foundation", "init_complete": False,
                "foundation_targets_met": targets.get("met") is True,
                "structural_targets_met": coverage_targets_met(targets, "partial"),
                "catalog_binding": self.record.discovery.get("catalog_binding", {}),
                "unknown_features": {key: list(item.get("missing_facets", []))
                    for key, item in targets.get("features", {}).get("items", {}).items()
                    if item.get("missing_facets")},
                "unknown_owners": {key: [facet for facet, status in item.get("facets", {}).items()
                    if status != "covered"] for key, item in owners.items()
                    if any(status != "covered" for status in item.get("facets", {}).values())},
                "unknown_core_files": list(targets.get("core", {}).get("missing_files", [])),
                "claim": "Accepted foundation retains supported context and explicit unknowns; "
                         "it does not establish seven-facet final acceptance or executed tests."}
            self.record.notes.append("portable foundation preview: unsupported facets remain unknown; "
                                     "final semantic-depth and structural targets are unchanged")
            return super()._prepare_publication(changed)
        if self.STAGE == "knowledge" and self.foundation_mode == "partial":
            from .foundation_publication import freeze_receipt
            freeze_receipt(self, changed)
            return super()._prepare_publication(changed)
        if (self.rt.unlimited_subscription and not self.dry_run
                and targets.get("required") is True and targets.get("met") is not True):
            return self._blocked(["foundation targets incomplete; native progress retained; "
                                  "resume this same pinned publication batch to fill missing knowledge"])
        return super()._prepare_publication(changed)

    def _resume(self, previous: InitRecord) -> InitRecord:
        if self.STAGE == "knowledge" and self.foundation_mode == "partial":
            from .init_stages import render_pr_body
            from .init_support import load_prepared, save_prepared
            # The common run path has already replayed native/source/receipt
            # and checked the frozen catalog, branch and generation identity.
            prepared = load_prepared(previous.pr["prepared"])
            body = render_pr_body(previous, self.lifecycle)
            if prepared["body"] != body:
                save_prepared(Path(previous.pr["prepared"]), **{**prepared, "body": body})
        return super()._resume(previous)

    def _build(self, tree: Path) -> InitRecord:
        if self.route_source == "none":
            return self._blocked(["knowledge needs owner routes: run and merge skeleton and modules first"])
        if any(not o.path.startswith(self.repo_dir + "/") for o in self.owners):
            return self._blocked(["knowledge owner pages must belong to this repository"])
        from .feature_discovery_index import build_for_stage

        self.source_index = build_for_stage(tree, self)
        publication_only = self.foundation_mode == "partial"
        if publication_only and not getattr(self, "coverage_policy", None):
            return self._blocked(["partial foundation publication requires an explicit coverage policy"])
        visited_owners, visited_features = [], []
        if publication_only and not self.record.coverage.get("foundation_jobs", {}).get("tasks"):
            return self._blocked(["partial foundation publication requires a genuine saved native batch"])
        files: dict[str, list[str]] = {}
        unrouted = []
        policy = getattr(self, "coverage_policy", None)
        paths = list(self.source_index.production)
        for failure in self.source_index.failures:
            self.record.checklist.append(f"source index {failure['path']}: {failure['operation']} failed; retained as unknown")
        routes = self.owners
        owners = source_owners(paths, routes, policy)
        self.owners = list(owners.values())
        restore_jobs(self)
        for rel in paths:
            hits = source_owner(rel, routes, owners)
            if hits:
                files.setdefault(hits[0].owner, []).append(rel)
            else:
                unrouted.append(rel)
        prs = self.upstream.first_parent_changes(self.record.pin, count=self.lifecycle.init.pr_window_count,
                                                 max_age_days=self.lifecycle.init.pr_window_max_age_days)
        churn = Counter(p for changed in prs for p in set(changed))
        order = sorted(files, key=lambda name: (-sum(churn[p] for p in files[name]), name))
        for name in order:
            if not _OWNER.fullmatch(name):
                return self._blocked([f"knowledge owner {name!r} is not a safe owner slug"])
            page = self._page_for(owners[name])
            if page in self.head:
                existing = Page.parse(self.head[page])
                if existing.frontmatter_field("type") not in ("architecture", "guide") or existing.rules():
                    return self._blocked([f"knowledge target {page} must be an explanatory architecture or guide page"])
        report = {}
        claims, evidence = {}, []
        owner_jobs = []
        for position, name in enumerate(order):
            visited_owners.append(name)
            owner = owners[name]
            page = self._page_for(owner)
            existing = self.head.get(page, "")
            existing_pages = self._existing_knowledge(owner, existing)
            markers = {(facet, pin, label) for text in existing_pages.values()
                       for own, facet, pin, label in _MARKER.findall(text)
                       if own == name}
            covered = [f for f in FACETS if any(facet == f and pin == self.record.pin and label in ("", "pass")
                                              for facet, pin, label in markers)]
            pending = [f for f in FACETS if f not in covered and any(facet == f and pin == self.record.pin
                                                                    for facet, pin, _ in markers)]
            stale = [f for f in FACETS if f not in covered + pending and any(facet == f for facet, _, _ in markers)]
            requested = [f for f in FACETS if f not in covered + pending + stale]
            for facet in stale:
                self.record.checklist.append(f"{name}/{facet}: existing knowledge has another pin; needs human refresh")
            report[name] = {"page": page, "facets": {f: "covered" if f in covered else "needs_review" if f in pending
                                                    else "stale" if f in stale
                                                    else "missing" for f in FACETS},
                            "source_files": len(files[name]), "shown_files": 0}
            if not requested:
                continue
            if publication_only:
                continue  # No fourth extraction or review: existing task proofs were replayed by restore_jobs.
            ordered_paths = sorted(files[name], key=lambda p: (-churn[p], p))
            source, test_count = foundation_source_packet(self, ordered_paths,
                read_sources=lambda limit: self._sources(tree, ordered_paths, limit),
                byte_limit=MAX_SOURCE_BYTES)
            docs = self._docs_for(tree, owner)
            offered = offered_ranges(source + docs)
            report[name]["shown_files"] = len(source) - test_count
            report[name]["partial_files"] = [item["path"] for item in source if not item.get("test_context") and item["end"] < item["total_lines"]]
            self.record.unfinished.extend(f"{name}: byte cap truncated {path}"
                                          for path in report[name]["partial_files"])
            if len(source) - test_count < len(files[name]):
                self.record.unfinished.append(f"{name}: source cap showed {len(source) - test_count} of {len(files[name])} files")
            payload = {"foundation_generation_policy_version": FOUNDATION_GENERATION_POLICY_VERSION,
                       "repository": self.lifecycle.full_name, "pin": self.record.pin, "owner": name,
                       "facets": requested, "files": source, "docs": docs,
                       "existing_knowledge": self._bounded_context(existing_pages),
                       "language_sample": self._language_sample(),
                       "related_owners": [{"owner": o.owner, "scope_prefixes": list(o.prefixes)}
                                          for o in self.owners]}
            if self.rt.unlimited_subscription:
                from .foundation_context import context_version
                payload["foundation_prompt_version"] = context_version(self.rt)
                owner_jobs.append({"owner": owner, "page": page, "payload": payload,
                                   "offered": offered, "requested": requested})
                continue
            try:
                # One job at a time preserves serial head/context updates while
                # using the same dispatch, validation and recovery coordinator.
                for _, results in run_jobs(self, [{"owner": owner, "page": page, "payload": payload,
                                                  "offered": offered, "requested": requested}]):
                    for key, text, entries, label in results:
                        claims[key] = text
                        evidence.extend(entries)
                        report[name]["facets"][key.rsplit(":", 1)[-1]] = "covered"
                    for facet in requested:
                        verdict = self.record.verdicts.get(f"knowledge:{name}:{facet}", {})
                        if verdict.get("verdict") in ("unsure", "unjudged"):
                            report[name]["facets"][facet] = "needs_review"
            except BudgetExhausted as exc:
                self.record.unfinished.extend(f"knowledge owner {n}: {exc}" for n in order[position:])
                self.record.notes.append("knowledge stopped: budget exhausted; remaining facets are incomplete")
                break
            except ModelUnavailable as exc:
                self.record.unfinished.append(f"knowledge owner {name}: unusable draft: {exc}")
        if owner_jobs:
            for job, results in run_jobs(self, owner_jobs):
                name = job["owner"].owner
                for key, text, entries, label in results:
                    facet = key.rsplit(":", 1)[-1]
                    claims[key] = text
                    evidence.extend(entries)
                    report[name]["facets"][facet] = "covered"
                for facet in job["requested"]:
                    verdict = self.record.verdicts.get(f"knowledge:{name}:{facet}", {})
                    if verdict.get("verdict") in ("unsure", "unjudged"):
                        report[name]["facets"][facet] = "needs_review"
        for name in order:
            report.setdefault(name, {"page": self._page_for(owners[name]),
                                     "facets": dict.fromkeys(FACETS, "missing"),
                                     "source_files": len(files[name]), "shown_files": 0})
        totals = {f: sum(r["facets"][f] == "covered" for r in report.values()) for f in FACETS}
        self.record.coverage["knowledge"] = {"owners": report, "covered_by_facet": totals,
                                              "total_owners": len(order), "unrouted_files": unrouted,
                                              "pin": self.record.pin}
        missing = [f"{name}/{facet}" for name, r in report.items() for facet, status in r["facets"].items()
                   if status != "covered"]
        self.record.unfinished.extend(f"missing knowledge: {item}" for item in missing)
        if not any(n.startswith("knowledge stopped:") for n in self.record.notes):
            self.record.notes.append(f"knowledge stopped: all {len(order)} source owners visited; "
                                     f"{len(missing)} facets remain unsupported or stale")
        from .knowledge_coverage import add_contract_pages, audit_coverage, load_policy, matches

        policy_path_ = self._coverage_policy_path()
        policy_text = self.overlay.get(policy_path_) or self.rt.knowledge.show(self._base_sha, policy_path_)
        if policy_text:
            try:
                policy = load_policy(policy_text, self.repo_dir)
            except ValueError as exc:
                return self._blocked([f"knowledge coverage policy: {exc}"])
            skipped = add_contract_pages(self.head, tree, policy, self.owners, repo_dir=self.repo_dir,
                                         full_name=self.lifecycle.full_name, pin=self.record.pin,
                                         today=self.today, tags=self.tags, link_page=self._link_page)
            targets = audit_coverage(self.head, tree, policy, full_name=self.lifecycle.full_name, pin=self.record.pin)
            feature_jobs = []
            for feature in policy.features:
                visited_features.append(feature.id)
                if publication_only:
                    continue
                if targets["features"]["items"][feature.id]["covered"]:
                    continue
                paths = [p.relative_to(tree).as_posix() for p in tree.rglob("*")
                         if p.is_file() and matches(p.relative_to(tree).as_posix(), feature.source_globs)]
                source_paths = list(dict.fromkeys(list(feature.entry_points) + sorted(paths)))
                source, _ = foundation_source_packet(self, source_paths,
                    read_sources=lambda limit: preferred_sources(self, tree, feature.id, source_paths, limit),
                    byte_limit=MAX_SOURCE_BYTES)
                docs = self._sources(tree, list(feature.docs), MAX_DOC_BYTES)
                discovery = self._discovery_catalog()
                bundle = discovery.get("evidence_bundles", {}).get(feature.id)
                if bundle:
                    from .evidence_bundle import materialize_bundle, bounded_spans
                    try:
                        spans = materialize_bundle(bundle, tree, pin=self.record.pin,
                                                   catalog_hash=discovery["catalog_sha256"])
                    except (ValueError, OSError, UnicodeError) as exc:
                        return self._blocked([f"discovery evidence handoff: {exc}"])
                    source = bounded_spans([s for s in spans if s["kind"] != "doc"] + source, MAX_SOURCE_BYTES)
                    docs = bounded_spans([s for s in spans if s["kind"] == "doc"] + docs, MAX_DOC_BYTES)
                if self.rt.unlimited_subscription:
                    from .foundation_context import context_version, feature_context
                    if context_version(self.rt) == 5:
                        source, docs = feature_context(self, feature, source, docs,
                            source_limit=MAX_SOURCE_BYTES, doc_limit=MAX_DOC_BYTES)
                if not source:
                    self.record.unfinished.append(f"feature {feature.id}: missing readable source")
                    continue
                offered = offered_ranges(source + docs)
                missing_facets = targets["features"]["items"][feature.id]["missing_facets"] or list(FACETS)
                payload = {"foundation_generation_policy_version": FOUNDATION_GENERATION_POLICY_VERSION,
                           "repository": self.lifecycle.full_name, "pin": self.record.pin,
                           "owner": "feature-" + feature.id, "feature": feature.title, "facets": missing_facets,
                           "files": source, "docs": docs,
                           "existing_knowledge": self._bounded_context({feature.page: self.head.get(feature.page, "")}),
                           "language_sample": self._language_sample()}
                if self.rt.unlimited_subscription:
                    payload["foundation_prompt_version"] = context_version(self.rt)
                    feature_jobs.append({"owner": Owner("feature-" + feature.id, feature.page, ()),
                                         "page": feature.page, "payload": payload,
                                         "offered": offered, "requested": missing_facets})
                    continue
                try:
                    owner = Owner("feature-" + feature.id, feature.page, ())
                    for _, results in run_jobs(self, [{"owner": owner, "page": feature.page, "payload": payload,
                                                      "offered": offered, "requested": missing_facets}]):
                        for key, text, entries, _ in results:
                            claims[key] = text
                            evidence.extend(entries)
                except BudgetExhausted:
                    self.record.unfinished.append(f"feature {feature.id}: budget exhausted")
                    break
                except ModelUnavailable as exc:
                    self.record.unfinished.append(f"feature {feature.id}: unusable draft: {exc}")
            if feature_jobs:
                for job, results in run_jobs(self, feature_jobs):
                    for key, text, entries, label in results:
                        claims[key] = text
                        evidence.extend(entries)
            targets = audit_coverage(self.head, tree, policy, full_name=self.lifecycle.full_name, pin=self.record.pin)
            targets["policy_sha256"] = hashlib.sha256(policy_text.encode()).hexdigest()
            self.record.coverage["knowledge"]["targets"] = targets
            self.record.unfinished.extend(f"core file without knowledge: {p}" for p in skipped)
            self.record.unfinished.extend(f"feature without complete knowledge: {id_}"
                                          for id_, f in targets["features"]["items"].items() if not f["covered"])
            if not targets["met"]:
                self.record.notes.append("knowledge targets incomplete: all features and core-file threshold are required")
        if policy_text:
            from .knowledge_coverage import feature_metadata
            for feature in policy.features:
                if feature.page in self.head:
                    self.head[feature.page] = feature_metadata(self.head[feature.page], feature)
        if publication_only:
            self.record.coverage["foundation_scope"] = {
                "version": 1, "kind": "publication-only reconstruction",
                "complete": visited_owners == order and visited_features == [f.id for f in policy.features],
                "owners": visited_owners, "features": visited_features,
                "index_sha256": self.source_index.sha256,
                "index_identity": self.source_index.identity,
                "index_failures": self.source_index.failures,
                "terminal_tasks": sorted(self.record.coverage["foundation_jobs"]["tasks"]),
                "native_dispatches": 0}
        return self._conclude(claims, evidence)

    def _page_for(self, owner: Owner) -> str:
        directory = PurePosixPath(owner.path).parent
        if str(directory) == self.repo_dir:
            directory = PurePosixPath(self.repo_dir) / "components" / owner.owner
            # A root route may share an established component with a narrower owner.
            return str(directory / f"knowledge-{owner.owner}.md")
        return str(directory / "knowledge.md")

    @staticmethod
    def _sources(tree: Path, paths: list[str], limit: int) -> list[dict]:
        out, used = [], 0
        for path in paths[:MAX_FILES]:
            source = tree / path
            if not source.is_file() or source.is_symlink():
                continue  # a feature policy may still name an entry removed at this pin
            try:
                raw = source.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue  # shared inventory records unreadable inputs explicitly
            if not raw.strip():
                continue
            text = _numbered(raw, max(0, limit - used))
            if not text:
                break
            # Never attest an incomplete last line that the byte cap cut off.
            original = raw.splitlines()
            numbered = text.splitlines()
            if numbered and numbered[-1] != f"{len(numbered)}: {original[len(numbered) - 1]}":
                numbered.pop()
            if not numbered:
                break
            text = "\n".join(numbered)
            end = len(numbered)
            from .feature_discovery_index import language_for
            out.append({"path": path, "text": text, "end": end, "total_lines": len(original),
                        "language": language_for(path)})
            used += len(text.encode("utf-8"))
        return out

    def _docs_for(self, tree: Path, owner: Owner) -> list[dict]:
        from ..profiles.establish import doc_files

        words = set(owner.owner.split("-")) - {"core", "common", "agent"}
        ranked = []
        # Rank the configured docs before applying the per-owner prompt cap;
        # the shared skeleton excerpt can end before this component's guide.
        if not hasattr(self, "_doc_index"):
            self._doc_index = []
            shared = getattr(self, "source_index", None)
            candidates = [tree / path for path in shared.docs] if shared else doc_files(tree, self.lifecycle.init.doc_globs)
            for file in candidates:
                if file.is_symlink():
                    continue
                if shared:
                    entry = shared.entries[file.relative_to(tree).as_posix()]
                    if entry["status"] != "ready":
                        continue
                    excerpt = entry["text"].lower()
                else:
                    with file.open("rb") as stream:
                        excerpt = stream.read(MAX_DOC_BYTES).decode("utf-8", "replace").lower()
                self._doc_index.append((file.relative_to(tree).as_posix(), file.stem.lower(), excerpt))
        for path, stem, excerpt in self._doc_index:
            adjacent = any(path.startswith(prefix) for prefix in owner.prefixes)
            linked = any(prefix.lower() in excerpt for prefix in owner.prefixes)
            named = any(word in path.lower() for word in words)
            discussed = any(re.search(r"\b" + re.escape(word) + r"\b", excerpt) for word in words)
            readme = "/" not in path and stem.startswith("readme")
            score = 4 * adjacent + 3 * linked + 2 * named + discussed
            # Prefer owner overviews and designs to a large set of individual
            # method cards with the same relevance, while retaining exact cards.
            if score and stem in ("readme", "readme_cn", "architecture", "design"):
                score += 2
            if score or readme:
                ranked.append((-score, path.count("/"), path))
        paths = [path for _, _, path in sorted(ranked)]
        return self._sources(tree, paths[:6], MAX_DOC_BYTES)

    def _existing_knowledge(self, owner: Owner, current: str) -> dict[str, str]:
        directory = PurePosixPath(owner.path).parent
        component = PurePosixPath(self.repo_dir) / "components" / owner.owner
        pages = {}
        for path, text in sorted(self.existing.items()):
            if (path == owner.path or PurePosixPath(path).parent == component
                    or (str(directory) != self.repo_dir and PurePosixPath(path).parent == directory)):
                pages[path] = text
        if current:
            pages[self._page_for(owner)] = current
        return pages

    @staticmethod
    def _bounded_context(pages: dict[str, str]) -> dict[str, str]:
        """Bound prompt context without hiding markers from duplicate detection."""
        context, used = {}, 0
        # Semantic owner pages precede voluminous static interface catalogs.
        for path, text in sorted(pages.items(), key=lambda item: (
                PurePosixPath(item[0]).name.startswith("source-"), item[0])):
            excerpt = text.encode("utf-8")[:max(0, MAX_DOC_BYTES - used)].decode("utf-8", "ignore")
            if excerpt:
                context[path] = excerpt
                used += len(excerpt.encode("utf-8"))
        return context

    def _sections(self, owner, page, payload, offered, requested):
        """Review each requested section immediately, preserving native receipt order."""
        data = generate(self.rt, self.budget, self.lifecycle.init, system=knowledge_system(payload),
                        prompt=knowledge_prompt(payload), validate=validate_sections).data
        for section in data["sections"]:
            if section["facet"] in requested:
                yield data, section, self._section(owner, page, data, section, offered)

    def _section(self, owner: Owner, page: str, data: dict, section: dict, offered: dict[str, int]):
        facet = section["facet"]
        key = f"knowledge:{owner.owner}:{facet}"
        entries = []
        for entry in section["evidence"]:
            ranges = offered.get(entry["path"], [])
            if not range_is_offered(ranges, entry["start"], entry["end"]):
                self.record.dropped.append({"rule_id": key, "page": page, "why": "evidence outside shown input"})
                return None
            try:
                entries.append(evidence_for(self.observer, entry["path"], entry["start"], entry["end"]))
            except FactsError as exc:
                self.record.dropped.append({"rule_id": key, "page": page, "why": str(exc)})
                return None
        title = _one_line(data.get("title")) or f"{owner.owner} knowledge"
        text = self._render_section(section, entries)
        problems = check_rules({key: text}, self.observer)
        if problems:
            self.record.dropped.append({"rule_id": key, "page": page, "why": "; ".join(problems)})
            return None
        front = _page_frontmatter(title, kind="architecture", today=self.today, tags=self.tags)
        verdict = judge(self.rt, self.budget, self.lifecycle.init, Block("prose", page, "", "prose",
                        hashlib.sha256(text.encode()).hexdigest()), base={},
                        head={**self.head, page: front + "\n" + text}, evidence=self._judge_evidence(entries))
        label = classify_verdict(verdict)
        self.record.verdicts[key] = {"verdict": label, "reasons": verdict.reasons, "model": verdict.model,
                                     "page": page, "kind": "prose", "facet": facet,
                                     **({"quality_receipt": verdict.quality_receipt} if verdict.quality_receipt else {})}
        if label != "pass":
            self.record.dropped.append({"rule_id": key, "page": page, "why": f"advisory judge: {verdict.reasons}"})
            if label != "fail":
                self.record.unfinished.append(f"{owner.owner}/{facet}: {label}; draft retained in model traces for review")
            return None
        return self._append_approved(owner, page, title, facet, text, entries, label)

    def _judge_evidence(self, entries):
        payload = getattr(self, "_foundation_payload", {})
        if self.rt.unlimited_subscription and foundation_uses_full_packet(payload):
            return foundation_evidence(self, payload)
        return super()._judge_evidence(entries)

    def _render_section(self, section, entries):
        facet = section["facet"]
        heading = _one_line(section.get("title")) or facet.capitalize()
        text = neutral_headings(f"## {heading}\n\n" + section["body"].strip())
        if section["interpretation"] == "inference":
            text = text.replace("\n\n", "\n\nInference / 设计推断（非作者历史意图）：\n\n", 1)
        citations = []
        for entry in entries:
            from .source_links import source_link
            url = source_link(self.lifecycle.full_name, self.record.pin, entry.path, entry.start, entry.end)
            citations.append(f"[{entry.path}:L{entry.start}–L{entry.end}]({url})")
        text += "\n\nSources / 来源：" + ", ".join(citations) + "\n"
        return text

    def _append_approved(self, owner, page, title, facet, text, entries, label="pass"):
        """Coordinator assembly of the exact independently approved prose."""
        key = f"knowledge:{owner.owner}:{facet}"
        front = _page_frontmatter(title, kind="architecture", today=self.today, tags=self.tags)
        marker = f"<!-- kb:knowledge owner={owner.owner} facet={facet} pin={self.record.pin} verdict={label} -->"
        current = self.head.get(page, front)
        proposed = current.rstrip() + "\n\n" + marker + "\n\n" + text + "\n"
        parsed = Page.parse(proposed).with_frontmatter_field("updated", self.today)
        parsed = parsed.with_sources(list(dict.fromkeys(parsed.sources() + [
            f"{self.lifecycle.full_name}@{self.record.pin}:{e.path}:L{e.start}-L{e.end}" for e in entries])))
        rendered = parsed.render()
        over = page_over_capacity(rendered)
        if over:
            self.record.dropped.append({"rule_id": key, "page": page, "why": f"page capacity: {over}"})
            return None
        self.head[page] = rendered
        self.record.evidence[key] = [e.to_dict() for e in entries]
        self._link_page(page, title)
        return key, text, entries, label

    def _link_page(self, page: str, title: str) -> None:
        directory = PurePosixPath(page).parent
        while str(directory).startswith(self.repo_dir):
            index = str(directory / "_index.md")
            if index not in self.head:
                self.head[index] = (_page_frontmatter(directory.name, kind="index", today=self.today, tags=self.tags)
                                    + "理解本目录对应源码 owner 的职责、接口与集成边界时查这里。源码接口记录说明静态声明；"
                                    "完整功能语义、配置和设计取舍沿下面的功能页查证。通用审查方法不放在这里。\n")
            relative = posixpath.relpath(page, str(directory))
            if f"]({relative})" not in self.head[index]:
                self.head[index] = self.head[index].rstrip() + f"\n- [{title}]({relative})\n"
            if str(directory) == self.repo_dir:
                break
            page, title = index, directory.name
            directory = directory.parent
