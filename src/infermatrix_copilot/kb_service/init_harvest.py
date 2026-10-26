"""``kb init`` stage ``harvest-calibration``: a calibration set from the
owner's review of the init PRs (design kb-init v3 §11).

It runs after the deepen PR merged (a dry run chains on the dry-run
snapshots) and compares what the advisory judge said about every rule init
wrote (``InitRecord.verdicts`` of skeleton, modules and deepen) with what
the owner merged:

======================================  =====  =======================
verdict / what the merged tree holds    label  tags
======================================  =====  =======================
pass, merged unchanged                  good   source: owner
fail (stripped before review)           bad    source: judge
unsure, merged unchanged                good   source: owner
unsure, deleted or rewritten            bad    source: owner
pass, deleted or rewritten              bad    source: owner, override
unjudged, merged unchanged              good   source: owner
unjudged, deleted or rewritten          bad    source: owner
======================================  =====  =======================

"Rewritten" means the rule is on main but its words differ from the text the
judge saw: the case is about THAT text, which the owner did not accept.

Each case is self-contained in the ``calibration.load_cases`` shape: the
rule's page without the rule (``base``), the page with the judged text
added through ``ops.apply_operations`` (``head``), and the rule's pinned
evidence re-read from the upstream mirror. When bad cases are fewer than
good ones, deterministic mutations of good rules fill in (``synthetic``):
a broken path claim, a shifted evidence range, a negated invariant, and a
sibling module's evidence; they also top the owner's bad cases up to five,
because ``source: judge`` cases are the judge grading itself. The stage
refuses to open a PR with fewer than five bad cases from the owner or from
mutations. The PR adds only
``adapters/<adapter>/kb-calibration/cases/*.json`` and, when missing,
``knowledge_lifecycle.calibration_set: kb-calibration`` (a textual edit,
checked by ``lifecycle_flip.check_lifecycle_flip``); ``auto_merge`` still
needs ``kb calibrate`` and the shadow period.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import yaml

from ..knowledge_service.facts import FactsError
from ..knowledge_service.l1 import check_changeset
from ..knowledge_service.lifecycle import LifecycleError, Page
from ..knowledge_service.pinned_claims import Evidence, evidence_text
from .gate import changes_between
from .init_stages import MAX_EXCERPT_BYTES, _Candidate, _Stage, adapter_missing
from .init_support import STAGES, InitError, InitRecord

CALIBRATION_DIR = "kb-calibration"
MIN_TRUSTED_BAD = 5
RULE_STAGES = tuple(s for s in STAGES if s not in ("feature-discovery", "knowledge", "harvest-calibration"))
MUTATIONS = ("broken_path", "shifted_range", "negated", "sibling_evidence")
_CALIBRATION_LINE = re.compile(r"^(?P<indent>[ \t]+)calibration_set:(?P<space>[ \t]*)(?P<value>[^#\n]*?)"
                               r"(?P<comment>[ \t]+#[^\n]*)?(?P<eol>\r?\n|)$")
_CASE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_PATH_SPAN = re.compile(r"`(?P<path>(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+)(?P<rest>(?:::[A-Za-z0-9_.]+)?)`")
_NEGATIONS = (("强制", "禁止"), ("MUST NOT", "MUST"), ("must not", "must"), ("never", "always"))


def _normal(text: str) -> str:
    return text.rstrip() + "\n"


def _sha(text: str) -> str:
    return hashlib.sha256(_normal(text).encode("utf-8")).hexdigest()


def _seed(*parts: str) -> int:
    return int(hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:8], 16)


def label_for(verdict: str, state: str) -> tuple[str, dict] | None:
    """(expected, tags) for one judged rule whose judged text is ``state``
    on main (``kept`` | ``changed`` | ``absent``); None: no label (a fail
    the owner re-added by hand teaches nothing about the judged text)."""
    if verdict == "fail":
        return ("reject", {"source": "judge"}) if state != "kept" else None
    if state == "kept":
        return "pass", {"source": "owner"}
    tags = {"source": "owner"}
    if verdict == "pass":
        tags["override"] = True
    return "reject", tags


def set_calibration_set(text: str, value: str = CALIBRATION_DIR) -> str:
    """The manifest with ``knowledge_lifecycle.calibration_set: <value>``,
    edited in place: one line changes or is appended to the block; every
    other line is kept byte for byte. Raises ``InitError`` when the manifest
    names another calibration set (init only writes ``kb-calibration``)."""
    from .init_deepen import _lifecycle_block
    lines, header, end, indent = _lifecycle_block(text)
    for i in range(header + 1, end):
        match = _CALIBRATION_LINE.match(lines[i])
        if not match or match.group("indent") != indent:
            continue
        current = match.group("value").strip().strip("'\"")
        if current == value:
            return text
        if current:
            raise InitError(f"the manifest already names calibration set {current!r}; init only writes {value!r}")
        lines[i] = (f"{indent}calibration_set:{match.group('space') or ' '}{value}"
                    f"{match.group('comment') or ''}{match.group('eol')}")
        return "".join(lines)
    last = max((i for i in range(header, end) if lines[i].strip() and not lines[i].lstrip().startswith("#")),
               default=header)
    eol = "\r\n" if lines[header].endswith("\r\n") else "\n"
    if not lines[last].endswith(("\n", "\r\n")):
        lines[last] += eol
    lines.insert(last + 1, f"{indent}calibration_set: {value}{eol}")
    return "".join(lines)


@dataclass
class _Judged:
    """One rule init wrote and the judge graded, as its stage recorded it."""

    rule_id: str
    stage: str
    pin: str
    verdict: str
    page: str
    section: str
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class _Harvest(_Stage):

    # Calibration has checkpointed judgments, no generation calls.
    GENERATION_TASK_JOURNALED = True
    STAGE = "harvest-calibration"

    def _precheck(self) -> list[str]:
        problems = []
        for stage in RULE_STAGES:
            record = InitRecord.load(self.rt.state_dir, self.lifecycle.repo, stage)
            if record is not None and record.verdicts and \
                    any("section" not in v for v in record.verdicts.values()):
                problems.append(f"the {stage} record predates the harvest (its verdicts carry no rule text); "
                                "re-run that stage's dry run to regenerate it")
        return problems

    # -- inputs ------------------------------------------------------------------------
    def _judged(self) -> list[_Judged]:
        out = []
        for stage in RULE_STAGES:
            record = InitRecord.load(self.rt.state_dir, self.lifecycle.repo, stage)
            if record is None:
                continue
            for rule_id, v in sorted(record.verdicts.items()):
                try:
                    evidence = [Evidence.from_dict(e) for e in v.get("evidence") or []]
                except (KeyError, TypeError, ValueError) as exc:
                    self.record.notes.append(f"{rule_id}: unreadable evidence in the {stage} record ({exc})")
                    continue
                out.append(_Judged(rule_id, stage, record.pin, str(v["verdict"]), str(v.get("page") or ""),
                                   str(v["section"]), evidence))
        return out

    def _state(self, judged: _Judged) -> tuple[str, str | None]:
        """(kept | changed | absent, the page on main that holds the rule)."""
        for path in [judged.page] + sorted(p for p in self.base if p.startswith(self.repo_dir + "/")):
            text = self.base.get(path)
            if not text or not path.endswith(".md"):
                continue
            try:
                page = Page.parse(text)
                if not page.has_rule(judged.rule_id):
                    continue
                section = page.rule(judged.rule_id)
            except LifecycleError:
                continue
            same = _sha(section.body_without_footer) == _sha(judged.section)
            return ("kept" if same else "changed"), path
        return "absent", None

    def _observer(self, pin: str):
        observer = self._observers.get(pin)
        if observer is None:
            observer = self._observers[pin] = self.upstream.observer(pin, pull=self.rt.pull)
        return observer

    def _excerpt(self, pin: str, entry: Evidence) -> dict | None:
        # Harvest intentionally inspects changed source against older evidence;
        # it shares range materialization without asserting the old content hash.
        text = evidence_text(self._observer(pin), entry, verify_hash=False)
        if text is None:
            return None
        excerpt = text.encode("utf-8")[:MAX_EXCERPT_BYTES]
        return {"source_reference": f"{self.lifecycle.full_name}@{pin[:12]}:{entry.path}:"
                                    f"L{entry.start}-L{entry.end}",
                "kind": "upstream_text", "text": excerpt.decode("utf-8", "ignore")}

    # -- one case ----------------------------------------------------------------------
    def _case(self, judged: _Judged, section: str, holder: str | None, evidence: list[dict]
              ) -> tuple[dict, dict] | None:
        """(base, head) for ``section`` added to ``holder`` (the page that
        holds the rule on main, without it) or to the page it was judged on."""
        page = holder or judged.page
        work: dict[str, str] = {}
        index = str(PurePosixPath(page).with_name("_index.md"))
        for path in (page, index):
            if path in self.base:
                work[path] = self.base[path]
        if page in work:
            try:
                parsed = Page.parse(work[page])
                kept = tuple(s for s in parsed.sections if s.rule_id != judged.rule_id)
                work[page] = Page(parsed.frontmatter, parsed.head, kept).render()
            except LifecycleError as exc:
                self.record.notes.append(f"{judged.rule_id}: {page} does not parse ({exc})")
                return None
        base = dict(work)
        candidate = _Candidate(judged.rule_id, page, "", section, [])
        try:
            head = self._apply([candidate], dict(work))
        except LifecycleError as exc:
            self.record.notes.append(f"{judged.rule_id}: the judged text cannot be re-applied ({exc})")
            return None
        head = {p: t for p, t in head.items() if base.get(p) != t}
        base = {p: t for p, t in base.items() if p in head}
        if not check_changeset(base, head, changes_between(base, head)).ok:
            self.record.notes.append(f"{judged.rule_id}: its case does not pass L1 on its own; left out")
            return None
        return base, head

    def _evidence_dicts(self, judged: _Judged, entries: list[Evidence] | None = None) -> list[dict] | None:
        out = []
        for entry in entries if entries is not None else judged.evidence:
            try:
                item = self._excerpt(judged.pin, entry)
            except FactsError as exc:
                self.record.notes.append(f"{judged.rule_id}: evidence unreadable at the pin ({exc})")
                return None
            if item is None:
                return None
            out.append(item)
        return out

    # -- the stage -----------------------------------------------------------------------
    def _build(self, tree: Path) -> InitRecord:
        self._observers: dict[str, object] = {}
        judged = self._judged()
        cases: list[dict] = []
        good: list[tuple[_Judged, str | None, list[dict]]] = []
        counts = {"good": 0, "bad_owner": 0, "bad_judge": 0, "bad_synthetic": 0, "skipped": 0}
        for item in judged:
            state, holder = self._state(item)
            labelled = label_for(item.verdict, state)
            if labelled is None:
                counts["skipped"] += 1
                continue
            expected, tags = labelled
            evidence = self._evidence_dicts(item)
            built = self._case(item, item.section, holder, evidence or []) if evidence is not None else None
            if built is None:
                counts["skipped"] += 1
                continue
            base, head = built
            kind = "good" if expected == "pass" else "bad"
            cases.append({"id": f"init-{kind}-{item.stage}-{item.rule_id}", "expected": expected,
                          "note": f"kb init {item.stage}: judge said {item.verdict}; on main the text is {state}",
                          "rule_id": item.rule_id, "stage": item.stage, "verdict": item.verdict, **tags,
                          "base": base, "head": head, "evidence": evidence})
            if expected == "pass":
                counts["good"] += 1
                good.append((item, holder, evidence))
            else:
                counts["bad_" + tags["source"]] += 1
        bad = counts["bad_owner"] + counts["bad_judge"]
        # enough bad cases to balance the good ones, and enough trusted ones
        # (owner or synthetic) for the floor: the judge's own fails count for neither
        wanted = max(counts["good"] - bad, MIN_TRUSTED_BAD - counts["bad_owner"], 0)
        for mutated in self._mutations(good, wanted):
            cases.append(mutated)
            counts["bad_synthetic"] += 1
        trusted = counts["bad_owner"] + counts["bad_synthetic"]
        self._replace_coverage({})
        self.record.notes.append(
            "calibration cases: {good} good; bad {bad_owner} from the owner, {bad_judge} from the judge, "
            "{bad_synthetic} synthetic; {skipped} rule(s) not labelled".format(**counts))
        if not counts["good"]:
            return self._blocked(["no rule survived the owner's review unchanged, so there is no good case; a "
                                  "calibration set needs both (kb calibrate never passes without one)"])
        if trusted < MIN_TRUSTED_BAD:
            return self._blocked([f"only {trusted} bad case(s) come from the owner or from mutations; at least "
                                  f"{MIN_TRUSTED_BAD} are needed before a calibration set is proposed (the judge's "
                                  "own fails cannot calibrate it)"])
        return self._write(cases)

    def _write(self, cases: list[dict]) -> InitRecord:
        adapter_dir = PurePosixPath(self._manifest_path()).parent
        other: dict[str, tuple[str | None, str | None]] = {}
        for case in cases:
            name = _CASE_NAME.sub("-", case["id"]) + ".json"
            path = f"{adapter_dir}/{CALIBRATION_DIR}/cases/{name}"
            if path in other:
                return self._blocked([f"two cases would be written to {path}"])
            before = self._repo_text(path)
            other[path] = (before, json.dumps(case, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
        manifest_path = self._manifest_path()
        before = self._manifest_text()
        if before is None:
            return self._blocked([adapter_missing(manifest_path)])
        try:
            after = set_calibration_set(before)
        except InitError as exc:
            return self._blocked([f"{manifest_path}: {exc}"])
        if after != before:
            other[manifest_path] = (before, after)
        self._case_paths = {p for p in other if p != manifest_path}
        return self._conclude({}, [], other=other, check_other=self._check_other)

    def _repo_text(self, path: str) -> str | None:
        if path in self.overlay:
            return self.overlay[path]
        return self.rt.knowledge.show(self.record.kb_base_sha, path)

    def _check_other(self, path: str, before: str | None, after: str | None) -> list[str]:
        if path == self._manifest_path():
            return self._check_manifest(path, before, after)
        if path not in self._case_paths:
            return [f"harvest may only write calibration cases and {self._manifest_path()} (got {path})"]
        if before is not None:
            return [f"{path} already exists; a harvest never overwrites a calibration case"]
        with tempfile.TemporaryDirectory(prefix="kb-init-cases-") as scratch:
            cases = Path(scratch) / "cases"
            cases.mkdir()
            (cases / PurePosixPath(path).name).write_text(after or "", encoding="utf-8")
            from .calibration import load_cases

            try:
                load_cases(scratch)
            except (ValueError, KeyError) as exc:
                return [f"{path}: {exc}"]
        return []

    def _check_manifest(self, path: str, before: str | None, after: str | None) -> list[str]:
        from ..adapters import RepoAdapter
        from .config import LifecycleConfigError, parse_lifecycle
        from .lifecycle_flip import CALIBRATION_KEYS, check_lifecycle_flip

        if before is None or after is None:
            return [f"{path}: the manifest must exist before and after"]
        problems = [f"{path}: {p}" for p in check_lifecycle_flip(before, after, allowed=CALIBRATION_KEYS)]
        try:
            manifest = yaml.safe_load(after)
        except yaml.YAMLError as exc:
            return problems + [f"{path} is not valid YAML: {exc}"]
        if not isinstance(manifest, dict):
            return problems + [f"{path} is not a mapping"]
        root = self.lifecycle.adapter_dir
        with tempfile.TemporaryDirectory(prefix="kb-init-adapter-") as scratch:
            adapter = RepoAdapter(name=str(manifest.get("name") or ""), root=Path(root or scratch), manifest=manifest)
            try:
                lifecycle = parse_lifecycle(adapter)
            except LifecycleConfigError as exc:
                return problems + [f"{path}: {exc}"]
        if lifecycle is None or lifecycle.calibration_set != CALIBRATION_DIR:
            problems.append(f"{path}: the lifecycle does not name {CALIBRATION_DIR} after the edit")
        return problems

    # -- mutations -----------------------------------------------------------------------
    def _mutations(self, good: list[tuple[_Judged, str | None, list[dict]]], wanted: int) -> list[dict]:
        """Up to ``wanted`` synthetic bad cases, deterministically: rules in a
        rule-ID-seeded order, each trying the mutation kinds from a seeded
        start, at most one case per (rule, kind)."""
        out: list[dict] = []
        if wanted <= 0 or not good:
            return out
        order = sorted(good, key=lambda g: (_seed("rule", g[0].rule_id), g[0].rule_id))
        for round_ in range(len(MUTATIONS)):
            for item, holder, evidence in order:
                if len(out) >= wanted:
                    return out
                start = _seed("kind", item.rule_id) % len(MUTATIONS)
                kind = MUTATIONS[(start + round_) % len(MUTATIONS)]
                case = self._mutate(kind, item, holder, evidence, good)
                if case is not None:
                    out.append(case)
        return out

    def _mutate(self, kind: str, item: _Judged, holder: str | None, evidence: list[dict],
                good: list[tuple[_Judged, str | None, list[dict]]]) -> dict | None:
        section, new_evidence, note = item.section, evidence, ""
        if kind == "broken_path":
            match = _PATH_SPAN.search(section)
            if match is None:
                return None
            path = match.group("path")
            broken = str(PurePosixPath(path).with_name("removed-" + PurePosixPath(path).name))
            try:
                if self._observer(item.pin).path_exists(item.pin, broken):
                    return None   # the "broken" claim would still hold: not a bad case
            except FactsError:
                return None
            section = section[:match.start()] + f"`{broken}{match.group('rest')}`" + section[match.end():]
            note = f"path claim `{path}` replaced by `{broken}`, which the upstream does not have"
        elif kind == "negated":
            for positive, negative in _NEGATIONS:
                if positive in section or negative in section:
                    placeholder = "\0"
                    section = section.replace(positive, placeholder).replace(negative, positive) \
                        .replace(placeholder, negative)
                    note = f"invariant negated ({positive} <-> {negative})"
                    break
            else:
                return None
        elif kind == "shifted_range":
            shifted = []
            for entry in item.evidence:
                span = entry.end - entry.start + 1
                forward = Evidence(entry.path, entry.start + span + 5, entry.end + span + 5, "")
                backward = Evidence(entry.path, entry.start - span - 5, entry.end - span - 5, "")
                moved = next((m for m in (forward, backward)
                              if m.start >= 1 and self._evidence_dicts(item, [m])), None)
                if moved is None:
                    return None
                shifted.append(moved)
            new_evidence = self._evidence_dicts(item, shifted)
            if not new_evidence or new_evidence == evidence:
                return None
            supported = {e["text"].strip() for e in evidence}
            if any(e["text"].strip() in supported for e in new_evidence):
                return None   # the moved range still shows the same supporting text
            note = "evidence moved off the lines the rule describes"
        elif kind == "sibling_evidence":
            others = [g for g in good if g[0].page != item.page or g[0].rule_id != item.rule_id]
            others = [g for g in others if g[2] and g[2] != evidence and g[0].page != item.page] or \
                [g for g in others if g[2] and g[2] != evidence]
            if not others:
                return None
            donor = others[_seed("sibling", item.rule_id) % len(others)]
            new_evidence = donor[2]
            note = f"evidence swapped for {donor[0].rule_id}'s (another module)"
        if section == item.section and new_evidence == evidence:
            return None
        built = self._case(item, section, holder, new_evidence)
        if built is None:
            return None
        base, head = built
        return {"id": f"init-synthetic-{kind}-{item.stage}-{item.rule_id}", "expected": "reject",
                "note": f"kb init synthetic bad case from good rule {item.rule_id}: {note}",
                "rule_id": item.rule_id, "stage": item.stage, "verdict": item.verdict, "source": "synthetic",
                "synthetic": True, "mutation": kind, "base": base, "head": head, "evidence": new_evidence}

