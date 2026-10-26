"""Portable discovery handoffs. References are localization evidence, not new approval."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def merge_ranges(ranges):
    """Union overlapping or adjacent inclusive ranges; callers validate bounds."""
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def selected_spans(selected):
    """Group already selected lines without changing text, path order or gaps."""
    return [{"path": path, "start": start, "end": end,
             "text": [numbered[n] for n in range(start, end + 1)]}
            for path, numbered in selected.items()
            for start, end in merge_ranges((n, n) for n in numbered)]


def build_evidence_bundle(index, feature_id, *, catalog_hash, refs, unit_ids=(), receipts=()):
    from .feature_discovery_index import validate_evidence

    if not isinstance(feature_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", feature_id):
        raise ValueError("invalid evidence bundle feature")
    if not re.fullmatch(r"[0-9a-f]{64}", catalog_hash):
        raise ValueError("evidence bundle requires frozen catalog hash")
    selected = {}
    for ref in refs:
        if not validate_evidence(index, ref):
            raise ValueError("discovery evidence reference is invalid")
        entry = index.entries[ref["path"]]
        item = {"path": ref["path"], "start": ref["start"], "end": ref["end"],
                "sha256": entry["sha256"], "kind": entry["kind"]}
        selected[(item["path"], item["start"], item["end"])] = item
    body = {"schema_version": 1, "feature_id": feature_id, "pin": index.identity["pin"],
            "scope_sha256": digest(index.identity["scope"]), "index_sha256": index.sha256,
            "catalog_sha256": catalog_hash, "refs": [selected[k] for k in sorted(selected)],
            "unit_ids": sorted(set(unit_ids)), "receipts": list(receipts)}
    return {**body, "bundle_sha256": digest(body)}


def materialize_bundle(bundle, tree, *, pin, catalog_hash):
    """Verify every referenced full-file hash before returning its exact numbered span."""
    if not isinstance(bundle, dict) or bundle.get("schema_version") != 1:
        raise ValueError("unsupported discovery evidence bundle")
    body = {k: v for k, v in bundle.items() if k != "bundle_sha256"}
    if bundle.get("bundle_sha256") != digest(body):
        raise ValueError("discovery evidence bundle hash mismatch")
    if bundle.get("pin") != pin or bundle.get("catalog_sha256") != catalog_hash:
        raise ValueError("discovery evidence bundle source/catalog mismatch")
    root = Path(tree).resolve()
    result = []
    for ref in bundle["refs"]:
        path = PurePosixPath(ref["path"])
        if path.is_absolute() or ".." in path.parts or "\\" in ref["path"]:
            raise ValueError("discovery evidence path escapes source")
        target = root.joinpath(*path.parts)
        if target.is_symlink() or not target.resolve().is_relative_to(root):
            raise ValueError("discovery evidence path escapes source")
        raw = target.read_bytes()
        if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
            raise ValueError("discovery evidence source hash mismatch")
        lines = raw.decode("utf-8").splitlines()
        start, end = ref["start"], ref["end"]
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
            raise ValueError("discovery evidence span is invalid")
        result.append({**ref, "total_lines": len(lines),
                       "text": "\n".join(f"{i}: {lines[i-1]}" for i in range(start, end + 1))})
    return result


def affected_features(bundles, changed_paths):
    """Conservative path invalidation; unmodelled dynamic relations remain outside this proof."""
    changed = set(changed_paths)
    return sorted(fid for fid, bundle in bundles.items()
                  if any(ref["path"] in changed for ref in bundle.get("refs", [])))


def bounded_spans(items, limit):
    """Keep complete numbered lines, preserving the original non-prefix location."""
    result, used, seen = [], 0, set()
    for item in items:
        key = (item["path"], item.get("start", 1), item["end"])
        if key in seen:
            continue
        seen.add(key)
        selected = []
        for line in item["text"].splitlines():
            size = len((line + "\n").encode())
            if used + size > limit:
                break
            selected.append(line)
            used += size
        if selected:
            result.append({**item, "end": item.get("start", 1) + len(selected) - 1,
                           "text": "\n".join(selected)})
    return result
