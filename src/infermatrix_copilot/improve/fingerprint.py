"""The declared configuration fingerprint (design §3.1, decision D3).

Two units are comparable only when their fingerprints were computed from the
same declared inputs. The fingerprint is ``sha256`` over a canonical JSON
manifest whose sections mirror ``fingerprint.covers``:

* ``playbook_yaml_sha`` — sha256 of the resolved playbook file
* ``settings: [key, ...]`` — the named Settings values
* ``prompts: [glob, ...]`` — sha256 per matching package file
* ``routing: [NAME, ...]`` — the named routing knobs (environment first, then
  the same-named Settings attribute, lower-cased)
* ``tools: declared_tool_set`` — the declaration's ``shadow_tools``
* ``knowledge_snapshot`` — the plan's pinned knowledge snapshot id, if any
* ``copilot_sha`` — the running code's commit
* ``resolved_models`` — the eco and performance targets after every fallback
  (``Settings.tier_target``): model, provider id, kind and source, so a change
  to a fallback such as ``agent_model`` changes the fingerprint even when the
  raw routing knobs are unchanged

A cover that cannot be resolved (missing file, unknown setting) makes the
manifest *incomplete*: the unit is recorded without a fingerprint and stays in
Tier 1 (the engine never compares runs whose configuration it cannot name).
The manifest itself is stored as a blob so two fingerprints can be diffed.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from ..trace_store import environment_fingerprint
from .enroll import WorkflowDeclaration

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _playbook_file(name: str, settings: Any) -> Path | None:
    """The playbook file the run actually executes: ``settings.playbooks_dir``
    (the supported override) first, the packaged resource only as a fallback."""
    configured = getattr(settings, "playbooks_dir", None)
    if configured:
        candidate = Path(configured) / f"{name}.yaml"
        if candidate.is_file():
            return candidate
    try:
        from ..resources import resource_dir

        candidate = resource_dir("playbooks") / f"{name}.yaml"
    except Exception:  # noqa: BLE001 - resolved below by the source tree fallback
        candidate = PACKAGE_ROOT.parents[1] / "playbooks" / f"{name}.yaml"
    return candidate if candidate.is_file() else None


def compute(decl: WorkflowDeclaration, settings: Any, *, state: dict | None = None,
            environ: dict | None = None, package_root: Path | None = None) -> tuple[str, dict]:
    """``(fingerprint_hex, manifest)``; ``fingerprint_hex`` is ``""`` when the
    manifest is incomplete (``manifest["missing"]`` names what was unresolved)."""
    env = os.environ if environ is None else environ
    root = package_root or PACKAGE_ROOT
    state = state or {}
    manifest: dict[str, Any] = {"workflow": decl.workflow, "covers": {}, "missing": []}
    covers = manifest["covers"]
    for entry in decl.fingerprint_covers:
        if entry == "playbook_yaml_sha":
            path = root.parents[1] / "playbooks" / f"{decl.playbook}.yaml" if package_root else _playbook_file(decl.playbook, settings)
            if path is not None and not path.is_file():
                path = None
            if path is None:
                manifest["missing"].append(f"playbook:{decl.playbook}")
            else:
                covers["playbook_yaml_sha"] = _sha_file(path)
        elif entry == "tools":
            covers["tools"] = sorted(decl.shadow_tools)
        elif entry == "knowledge_snapshot":
            covers["knowledge_snapshot"] = str(state.get("knowledge_snapshot") or "")
        elif entry == "copilot_sha":
            covers["copilot_sha"] = environment_fingerprint().get("copilot_sha", "")
        elif entry == "resolved_models":
            # the models a run would actually execute with, after every
            # fallback (ECO_MODEL empty -> agent_model, harness defaults...):
            # Settings.tier_target is the single place that resolution lives
            resolved: dict[str, Any] = {}
            for mode in ("eco", "performance"):
                try:
                    target = settings.tier_target(mode)
                    resolved[mode] = {"model": target.model, "provider_id": target.provider_id,
                                      "kind": target.kind, "source": target.source}
                except Exception as exc:  # noqa: BLE001 - an unconfigured tier is itself configuration
                    resolved[mode] = {"unconfigured": type(exc).__name__}
            covers["resolved_models"] = resolved
        elif isinstance(entry, dict):
            (section, names), = entry.items()
            if section == "settings":
                values = {}
                for key in names:
                    if not hasattr(settings, key):
                        manifest["missing"].append(f"settings:{key}")
                        continue
                    values[key] = json.loads(json.dumps(getattr(settings, key), default=str, sort_keys=True))
                covers["settings"] = values
            elif section == "prompts":
                files: dict[str, str] = {}
                for pattern in names:
                    matched = sorted(root.glob(pattern))
                    if not matched:
                        manifest["missing"].append(f"prompts:{pattern}")
                    for path in matched:
                        if path.is_file():
                            files[path.relative_to(root).as_posix()] = _sha_file(path)
                covers["prompts"] = files
            elif section == "routing":
                routing = {}
                for name in names:
                    value = env.get(name)
                    if value is None:
                        attr = name.lower()
                        value = getattr(settings, attr, None) if hasattr(settings, attr) else None
                    routing[name] = json.loads(json.dumps(value, default=str, sort_keys=True))
                covers["routing"] = routing
            elif section == "tools":
                covers["tools"] = sorted(decl.shadow_tools)
            else:
                manifest["missing"].append(f"unknown:{section}")
        else:
            manifest["missing"].append(f"unknown:{entry}")
    manifest["complete"] = not manifest["missing"]
    if not manifest["complete"]:
        return "", manifest
    canonical = json.dumps(manifest["covers"], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), manifest


def diff(manifest_a: dict, manifest_b: dict) -> dict[str, dict]:
    """Which cover entries differ between two manifests: ``{section: {key: (a, b)}}``
    (section-level for scalar sections). Empty when the fingerprints agree."""
    out: dict[str, dict] = {}
    a, b = manifest_a.get("covers") or {}, manifest_b.get("covers") or {}
    for section in sorted(set(a) | set(b)):
        va, vb = a.get(section), b.get(section)
        if va == vb:
            continue
        if isinstance(va, dict) and isinstance(vb, dict):
            changed = {k: (va.get(k), vb.get(k)) for k in sorted(set(va) | set(vb)) if va.get(k) != vb.get(k)}
            out[section] = changed
        else:
            out[section] = {"": (va, vb)}
    return out
