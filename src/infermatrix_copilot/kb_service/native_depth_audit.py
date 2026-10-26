#!/usr/bin/env python3
"""Verify current depth approvals against archived native model calls and blobs."""

from __future__ import annotations

from .model_identity import model_family as _family

import argparse
import hashlib
import json
import re
from bisect import bisect_right
from pathlib import Path
from urllib.parse import quote

import yaml

from infermatrix_copilot.kb_service.gate import DIMENSIONS
from infermatrix_copilot.kb_service.source_links import source_link
from infermatrix_copilot.kb_service.init_stages import neutral_headings
from infermatrix_copilot.kb_service.knowledge_coverage import load_policy, policy_path
from infermatrix_copilot.kb_service.knowledge_depth import depth_page, depth_acceptance_mode, validate_draft
from infermatrix_copilot.kb_service.models import ModelUnavailable, parse_json_object
from infermatrix_copilot.knowledge_service.lifecycle import (
    DEPTH_BLOCK, DEPTH_FACETS, depth_proof_basis, depth_sections, safe_source_path,
)
from infermatrix_copilot.trace_store import TraceStore

MODEL_FIELDS = ("role", "provider", "model", "effort", "served_model")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _payload(text: str) -> dict:
    match = re.fullmatch(r"<untrusted_data>\s*(.*?)\s*</untrusted_data>\s*", text, re.S)
    data = json.loads(match[1] if match else text)
    if not isinstance(data, dict):
        raise ValueError("native prompt must contain an object")
    return data


def _prose(block: str) -> str:
    # This is exactly the rendering used by depth_judge.review_facets.
    return re.sub(r"<!--.*?-->", "", block, flags=re.S).strip()



def _generated_prose(section: dict, repository: str, pin: str) -> str:
    """Replay only render_block's deterministic lightweight prose transforms."""
    validate_draft({"sections": [section]}, acceptance_mode="lightweight")
    title = " ".join(str(section.get("title") or section["facet"]).replace("#", "").split())
    body = neutral_headings(f"## {title}\n\n")
    if section.get("validation_kind") == "documented_manual":
        body += "文档中的人工验收步骤（本轮未执行）：\n\n"
    if section["interpretation"] == "inference":
        body += "设计推断（非作者历史意图）：\n\n"
    body += section["body"].strip() + "\n\n"
    body += "来源：" + ", ".join(
        f"[{e['path']}:L{e['start']}–L{e['end']}]({source_link(repository, pin, e['path'], e['start'], e['end'])})" for e in section["evidence"]) + "\n"
    return _prose(body)


def _generator_evidence(data: dict, repository: str, pin: str) -> dict:
    """Convert the actual offered files/docs to the existing exact-span verifier."""
    packet = []
    for kind in ("files", "docs"):
        items = data.get(kind, [])
        if not isinstance(items, list):
            raise ValueError("native generator offered spans are malformed")
        for item in items:
            if not isinstance(item, dict) or not safe_source_path(item.get("path")) \
                    or type(item.get("start")) is not int or type(item.get("end")) is not int \
                    or not 1 <= item["start"] <= item["end"] \
                    or not isinstance(item.get("text"), list) \
                    or len(item["text"]) != item["end"] - item["start"] + 1:
                raise ValueError("native generator offered spans are malformed")
            rows = item["text"] if kind == "files" else [f"{n}: {line}" for n, line in enumerate(item["text"], item["start"])]
            if any(not isinstance(line, str) for line in item["text"]):
                raise ValueError("native generator offered lines are malformed")
            packet.append({"kind": "upstream_text", "text": rows,
                           "source_reference": f"{repository}@{pin}:{item['path']}:L{item['start']}-L{item['end']}"})
    return {"evidence": packet}


def _lightweight_generator(candidates, records, *, before: float, feature: str, facet: str,
                           block: str, proof: dict, repository: str, pin: str) -> dict:
    """Find the actual retained draft, which may precede a later unrelated retry."""
    expected = [{key: e[key] for key in ("path", "start", "end")} for e in proof["evidence"]]
    for candidate in reversed(candidates):
        native = records.get(candidate["id"])
        if candidate["at"] > before or native is None:
            continue
        record, store = native
        model = record.get("model", {})
        requested, served = model.get("model"), model.get("served_model")
        if model.get("provider") != "zcode" or not isinstance(requested, str) or requested.casefold() != "glm-5.3" \
                or model.get("fallback_from") or record.get("result", {}).get("fallback_from") \
                or served not in (None, "") and (not isinstance(served, str) or served.casefold() != "glm-5.3"):
            continue
        try:
            data = _payload(store.blob(record["inputs"]["prompt"]))
            requested_facets = data.get("facets")
            if data.get("repository") != repository or data.get("feature", {}).get("id") != feature \
                    or data.get("pin") != pin or data.get("acceptance_mode") != "lightweight" \
                    or not isinstance(requested_facets, list) or requested_facets.count(facet) != 1:
                continue
            draft = parse_json_object(store.blob(record["outputs"]["reply"]))
            sections = draft.get("sections")
            if not isinstance(sections, list):
                continue
            matching = [section for section in sections if isinstance(section, dict) and section.get("facet") == facet]
            if len(matching) != 1:
                continue
            section = matching[0]
            spans = [{key: e[key] for key in ("path", "start", "end")} for e in section["evidence"]]
            if spans != expected or section.get("validation_kind") != proof.get("validation_kind") \
                    or _generated_prose(section, repository, pin) != _prose(block):
                continue
            _judged_evidence(_generator_evidence(data, repository, pin), block, repository, pin, facet)
            return record
        except (OSError, ValueError, TypeError, KeyError, AttributeError, ModelUnavailable):
            continue
    raise ValueError("no exact successful Zcode GLM-5.3 lightweight draft and offered evidence before this judgment")


def _judged_evidence(data: dict, block: str, repository: str, pin: str, facet: str) -> None:
    """Bind the actual judge context to each persisted source witness.

    Historical packets merge adjoining evidence spans and omit line endings.
    Source-span hashes normalize CRLF through read_text, so replay their UTF-8
    text with either a final LF or no terminator for a source's final line.
    """
    match = re.search(r"\n<!-- kb:depth-proof (.*?) -->\s*\n<!-- /kb:depth -->$", block, re.S)
    if match is None:
        raise ValueError("current facet has no evidence proof")
    proof = json.loads(match[1])
    basis = depth_proof_basis(proof, facet=facet, pin=pin)
    evidence = data.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("native judgment has no source evidence packet")
    lines, certificates = {}, []
    for item in evidence:
        if not isinstance(item, dict):
            raise ValueError("native judgment has malformed evidence")
        if item.get("kind") == "replayed_absence_certificate":
            certificates.append(item.get("certificate"))
            continue
        if item.get("kind") != "upstream_text":
            raise ValueError("native judgment has an unknown evidence kind")
        reference = item.get("source_reference")
        prefix = repository + "@" + pin + ":"
        if not isinstance(reference, str) or not reference.startswith(prefix):
            raise ValueError("native evidence reference differs from the repository or pin")
        span = re.fullmatch(r"(.+):L([1-9][0-9]*)-L([1-9][0-9]*)", reference[len(prefix):])
        if span is None or not safe_source_path(span[1]):
            raise ValueError("native evidence has an invalid source reference")
        path, start, end = span[1], int(span[2]), int(span[3])
        text = item.get("text")
        if end < start or not isinstance(text, list) or len(text) != end - start + 1:
            raise ValueError("native numbered evidence does not cover its declared span")
        for number, row in enumerate(text, start):
            marker = f"{number}: "
            if not isinstance(row, str) or not row.startswith(marker) or "\n" in row or "\r" in row:
                raise ValueError("native numbered evidence is malformed or noncontiguous")
            value = row[len(marker):]
            key = (path, number)
            if key in lines and lines[key] != value:
                raise ValueError("native evidence contains conflicting source lines")
            lines[key] = value
    for span in proof["evidence"]:
        try:
            shown = "\n".join(lines[(span["path"], number)] for number in range(span["start"], span["end"] + 1))
        except KeyError as exc:
            raise ValueError("native judgment omitted a current proof span") from exc
        if span["sha256"] not in {_sha(shown.encode()), _sha((shown + "\n").encode())}:
            raise ValueError("native source text differs from its current proof hash")
    if basis == "verified_absent" and certificates != [proof["absence_certificate"]]:
        raise ValueError("native judgment omitted or changed the replayed absence certificate")


def _archives(paths: list[Path]) -> tuple[dict, dict, list[dict], list[str]]:
    records, generators, provenance, problems = {}, {}, [], []
    for root in paths:
        store = TraceStore(root)
        record_files = sorted((root / "records").glob("*.jsonl"))
        if not record_files:
            problems.append(f"{root.name}: no native trace record archive")
            continue
        record_bytes = {path: path.read_bytes() for path in record_files}
        hashed = hashlib.sha256()
        blob_files = sorted((root / "blobs").rglob("*.gz"))
        for path in sorted(record_files + blob_files):
            raw = record_bytes[path] if path in record_bytes else path.read_bytes()
            hashed.update(path.relative_to(root).as_posix().encode() + b"\0" + raw)
        provenance.append({"name": root.name, "archive_sha256": hashed.hexdigest(),
                           "record_files": len(record_files), "blob_files": len(blob_files)})
        for path in record_files:
            for number, line in enumerate(record_bytes[path].decode().splitlines(), 1):
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict) or record.get("schema") != "trace/1" \
                            or not isinstance(record.get("id"), str) or not record["id"]:
                        raise ValueError("invalid native trace record")
                    record_id = record["id"]
                    if record_id in records:
                        problems.append(f"duplicate native trace ID: {record_id}")
                        records[record_id] = None
                        continue
                    records[record_id] = (record, store)
                    if record.get("kind") != "model_call" or record.get("error") != "" \
                            or record.get("model", {}).get("role") != "generator":
                        continue
                    data = _payload(store.blob(record["inputs"]["prompt"]))
                    feature = data.get("feature")
                    feature = feature.get("id") if isinstance(feature, dict) else feature
                    pin = data.get("pin")
                    if isinstance(feature, str) and isinstance(pin, str) \
                            and isinstance(record.get("at"), (int, float)):
                        generators.setdefault((feature, pin), []).append(record)
                except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                    problems.append(f"{root.name}/{path.name}:{number}: unreadable native record or blob ({type(exc).__name__})")
    for rows in generators.values():
        rows.sort(key=lambda row: (row["at"], row["id"]))
    return records, generators, provenance, problems


def _receipts(baselines: list[Path], checkpoints: list[Path], pages: dict) -> tuple[dict, list[dict], list[str]]:
    receipts, provenance, problems = {}, [], []
    for kind, paths in (("baseline_report", baselines), ("checkpoint", checkpoints)):
        for path in paths:
            try:
                raw = path.read_bytes()
                data = json.loads(raw)
                provenance.append({"kind": kind, "name": path.name, "sha256": _sha(raw)})
                if kind == "baseline_report":
                    rows = data.get("approvals", data.get("native_execution", {}).get("approvals"))
                    if not isinstance(rows, list):
                        raise ValueError("baseline report must contain an approvals list")
                else:
                    rows = []
                    for key, verdict in data.get("verdicts", {}).items():
                        if not key.startswith("depth:"):
                            continue
                        feature = key.removeprefix("depth:")
                        for facet, result in verdict.get("facets", {}).items():
                            if result.get("verdict") == "pass":
                                rows.append({**result, "feature": feature, "facet": facet,
                                             "page": pages.get(feature), "pin": data.get("pin")})
                for row in rows:
                    if not isinstance(row, dict) or not isinstance(row.get("feature"), str) \
                            or row.get("facet") not in DEPTH_FACETS:
                        raise ValueError("malformed native approval receipt")
                    key = (row["feature"], row["facet"])
                    if key in receipts:
                        problems.append("duplicate native approval receipt: " + "/".join(key))
                        receipts[key] = None
                    else:
                        receipts[key] = row
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                problems.append(f"{path.name}: unreadable {kind} ({type(exc).__name__})")
    return receipts, provenance, problems


def audit_native_approvals(root: Path, repo: str, *, baseline_reports: list[Path],
                           trace_dirs: list[Path], checkpoints: list[Path]) -> dict:
    """No model calls or archive/index writes; reports contain no raw payloads.

    This verifies native judgment bindings. The separate pinned depth auditor
    owns source-span, flow and absence-certificate replay against upstream.
    """
    root = root.resolve()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", repo):
        raise ValueError("repo must be a canonical adapter slug")
    manifest = yaml.safe_load((root / f"adapters/{repo}/manifest.yaml").read_text())
    repository = manifest.get("repo", {}).get("full_name")
    if not isinstance(repository, str) or not repository or any(c in repository for c in ("\n", "\r", "\0")):
        raise ValueError("adapter must declare its source repository full_name")
    policy_file = root / policy_path(repo)
    policy = load_policy(policy_file.read_text(), manifest["knowledge"]["repo_subdir"])
    pages = {feature.id: depth_page(feature) for feature in policy.features}
    records, generators, archive_provenance, problems = _archives(trace_dirs)
    receipts, receipt_provenance, receipt_problems = _receipts(baseline_reports, checkpoints, pages)
    problems += receipt_problems
    approvals, current_keys, knowledge_hash = [], set(), hashlib.sha256()
    dimensions = {key: "yes" for key in DIMENSIONS["prose"]}
    for feature in policy.features:
        page = pages[feature.id]
        path = root / "knowledge" / page
        if not path.is_file():
            continue
        text = path.read_text()
        knowledge_hash.update(page.encode() + b"\0" + text.encode())
        matches = list(DEPTH_BLOCK.finditer(text))
        valid = {(row["feature"], row["facet"]) for row in depth_sections(text)}
        if text.count("<!-- kb:depth ") != len(matches):
            problems.append(f"{page}: malformed depth block")
        for match in matches:
            fid, facet, pin, body_hash, body = match.groups()
            key, label = (fid, facet), f"{fid}/{facet}"
            if key in current_keys:
                problems.append(label + ": duplicate current depth block")
                approvals = [row for row in approvals if (row["feature"], row["facet"]) != key]
                continue
            current_keys.add(key)
            try:
                if fid != feature.id or key not in valid or _sha(body.encode()) != body_hash:
                    raise ValueError("invalid, edited, or misplaced current depth block")
                block_hash = _sha(match[0].encode())
                proof = json.loads(re.search(r"<!-- kb:depth-proof (.*?) -->", match[0], re.S)[1])
                mode = depth_acceptance_mode(proof)
                receipt = receipts.get(key)
                if not receipt or receipt.get("page") != page or receipt.get("block_sha256") != block_hash \
                        or receipt.get("pin", pin) != pin:
                    raise ValueError("missing or mismatched current-block approval receipt")
                if receipt.get("acceptance_mode", "strict") != mode:
                    raise ValueError("approval receipt acceptance mode differs from current block")
                native = records.get(receipt.get("native_trace_id"))
                if not native:
                    raise ValueError("missing or duplicate native judgment trace")
                record, store = native
                model = record.get("model", {})
                if record.get("kind") != "model_call" or record.get("error") != "" \
                        or model.get("role") != "judge" or model.get("provider") != "codex" \
                        or not isinstance(model.get("model"), str) or not model["model"]:
                    raise ValueError("native record is not a successful Codex judge call")
                served = model.get("served_model")
                if served not in (None, "") and (not isinstance(served, str)
                        or not re.match(r"^(?:gpt-|codex(?:-|$)|o[0-9]+(?:-|$))", served, re.I)):
                    raise ValueError("native record reports an incompatible Codex judge served identity")
                reply_ref = record.get("outputs", {}).get("reply")
                expected_ref = "sha256:" + str(receipt.get("native_reply_sha256", ""))
                if reply_ref != expected_ref:
                    raise ValueError("native reply hash differs from approval receipt")
                response = parse_json_object(store.blob(reply_ref))
                result = response.get("facets", {}).get(facet)
                if not isinstance(result, dict) or result.get("dimensions") != dimensions \
                        or not isinstance(result.get("reason"), str):
                    raise ValueError("native facet judgment is not exactly three yes dimensions")
                data = _payload(store.blob(record["inputs"]["prompt"]))
                if data.get("feature") != fid or data.get("pin") != pin \
                        or data.get("sections", {}).get(facet) != _prose(match[0]):
                    raise ValueError("native judgment prompt does not bind the exact current facet prose and pin")
                modes = data.get("acceptance_modes", {})
                if not isinstance(modes, dict) or modes.get(facet, "strict") != mode:
                    raise ValueError("native judgment prompt does not bind the current acceptance mode")
                validation_kind = proof.get("validation_kind")
                if validation_kind is not None and (receipt.get("validation_kind") != validation_kind or
                        data.get("validation_kinds", {}).get(facet) != validation_kind):
                    raise ValueError("native judgment and receipt do not bind the validation category")
                _judged_evidence(data, match[0], repository, pin, facet)
                sections, results = data["sections"], response["facets"]
                if mode == "strict" and (set(sections) != set(results) or any(
                        name not in DEPTH_FACETS or not isinstance(item, dict)
                        or not isinstance(item.get("reason"), str) or not isinstance(item.get("dimensions"), dict)
                        or set(item["dimensions"]) != set(dimensions)
                        or any(value not in ("yes", "no", "unsure") for value in item["dimensions"].values())
                        for name, item in results.items())):
                    raise ValueError("native response does not answer the complete judged packet")
                candidates = generators.get((fid, pin), [])
                index = bisect_right([row["at"] for row in candidates], record["at"]) - 1
                if index < 0:
                    raise ValueError("no successful native generator context before this judgment")
                generator = _lightweight_generator(candidates, records, before=record["at"], feature=fid,
                    facet=facet, block=match[0], proof=proof, repository=repository, pin=pin) \
                    if mode == "lightweight" else candidates[index]
                if records.get(generator["id"]) is None:
                    raise ValueError("generator trace ID is duplicated")
                generator_model = generator.get("model", {})
                gen_name = generator_model.get("served_model") or generator_model.get("model")
                judge_name = model.get("served_model") or model["model"]
                if not isinstance(gen_name, str) or not gen_name or not isinstance(judge_name, str) \
                        or not isinstance(generator_model.get("provider"), str) or not generator_model["provider"] \
                        or _family(gen_name) == _family(judge_name):
                    raise ValueError("native generator and judge are not independently identified model families")
                approvals.append({"feature": fid, "facet": facet, "page": page, "pin": pin,
                                  "acceptance_mode": mode,
                                  **({"validation_kind": validation_kind} if validation_kind is not None else {}),
                                  "block_sha256": block_hash, "dimensions": result["dimensions"],
                                  "native_trace_id": record["id"], "native_reply_sha256": reply_ref.removeprefix("sha256:"),
                                  "model": {key: model.get(key, "") for key in MODEL_FIELDS},
                                  "native_generator_trace_id": generator["id"],
                                  "generator_model": {key: generator_model.get(key, "") for key in MODEL_FIELDS}})
            except (OSError, ValueError, TypeError, KeyError, AttributeError, ModelUnavailable) as exc:
                detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                problems.append(label + ": " + detail)
    for key in sorted(set(receipts) - current_keys):
        problems.append("approval receipt has no current depth block: " + "/".join(key))
    approvals.sort(key=lambda row: (row["feature"], DEPTH_FACETS.index(row["facet"])))
    return {"schema_version": 1, "repo": repo, "approvals": approvals, "problems": problems,
            "provenance": {"policy_sha256": _sha(policy_file.read_bytes()),
                           "depth_pages_sha256": knowledge_hash.hexdigest(), "trace_archives": archive_provenance,
                           "input_reports": receipt_provenance,
                           "approval_set_sha256": _sha(json.dumps(approvals, ensure_ascii=False, sort_keys=True).encode())},
            "interpretation": "Three yes dimensions were read from native reply blobs bound to current prose. "
                              "Unreported served-model identities remain unreported; requested identity and provider "
                              "come from the native record. Lightweight generator provenance also binds an exact "
                              "Zcode GLM-5.3 draft and its actually offered source/document spans without fallback; "
                              "strict historical provenance retains its existing model-family check. "
                              "Source and absence proofs require the pinned depth audit."}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--repo", required=True)
    parser.add_argument("--baseline-report", type=Path, action="append", default=[])
    parser.add_argument("--trace-dir", type=Path, action="append", required=True)
    parser.add_argument("--checkpoint", type=Path, action="append", default=[])
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.report and args.report.resolve().is_relative_to((args.root / "knowledge").resolve()):
        parser.error("approval reports belong outside product knowledge")
    try:
        report = audit_native_approvals(args.root, args.repo, baseline_reports=args.baseline_report,
                                        trace_dirs=args.trace_dir, checkpoints=args.checkpoint)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.error(f"cannot read native approval inputs: {type(exc).__name__}")
    raw = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(raw)
    print(json.dumps({"approvals": len(report["approvals"]), "problems": report["problems"],
                      "report_sha256": _sha(raw.encode())}, ensure_ascii=False, indent=2))
    return 1 if report["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
