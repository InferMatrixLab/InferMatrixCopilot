"""Immutable, content-addressed engine versions; no candidate code is imported here."""
from __future__ import annotations

from ..persistence import atomic_write_bytes

import fnmatch
import hashlib
import json
import subprocess
import shutil
import functools
from pathlib import Path, PurePosixPath

PROTECTED = ("eval/", "test/", "knowledge/", ".github/", "pyproject.toml", "src/infermatrix_copilot/improve/",
             "src/infermatrix_copilot/trace_store.py", "src/infermatrix_copilot/scopes.py",
             "src/infermatrix_copilot/config.py", "src/infermatrix_copilot/llm.py",
             "src/infermatrix_copilot/budgeting.py", "src/infermatrix_copilot/persistence.py",
             "src/infermatrix_copilot/source_fingerprint.py",
             "src/infermatrix_copilot/git_objects.py", "src/infermatrix_copilot/app/workflow_execution.py",
             "src/infermatrix_copilot/kb_service/init_execution.py",
             "playbooks/workflow-improve.yaml", "playbooks/evolution-overrides.json",
             "src/infermatrix_copilot/engine/executor.py", "src/infermatrix_copilot/engine/steps/improve.py",
             "src/infermatrix_copilot/kb_service/gate.py", "src/infermatrix_copilot/kb_service/models.py",
             "src/infermatrix_copilot/kb_service/scheduler.py", "src/infermatrix_copilot/kb_service/runtime.py",
             "src/infermatrix_copilot/knowledge_service/", "src/infermatrix_copilot/providers/")
SELF_EDITABLE = {"src/infermatrix_copilot/improve/forensics.py", "src/infermatrix_copilot/improve/lints.py"}
FORBIDDEN_SETTINGS = ("IMPROVE_", "ALLOW_", "TRACE_", "PYTHON", "PATH", "HOME", "KB_JUDGE", "OPENAI_", "ANTHROPIC_")

class ArtifactError(ValueError):
    pass

def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))

def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def safe_path(raw: str) -> str:
    p = PurePosixPath(raw)
    if not raw or any(ord(c) < 32 for c in raw) or p.is_absolute() or ".." in p.parts or "\\" in raw or any(x.startswith(".") for x in p.parts):
        raise ArtifactError(f"unsafe candidate path: {raw!r}")
    return p.as_posix()

def protected(path: str) -> bool:
    return path not in SELF_EDITABLE and any(path == p or path.startswith(p) for p in PROTECTED)

def validate_policy(policy: dict) -> None:
    import math
    for key in ("min_effect", "cost_per_unit_usd"):
        if key in policy and (not math.isfinite(float(policy[key])) or float(policy[key]) <= 0):
            raise ArtifactError(f"{key} must be positive and finite")
    if any(direction not in ("higher", "lower") for direction in policy.get("guards", {}).values()):
        raise ArtifactError("guardrail direction must be higher or lower")
    for raw in policy.get("paths", []):
        path = safe_path(raw)
        if protected(path) or not (path.startswith("src/infermatrix_copilot/") or path.startswith("playbooks/")):
            raise ArtifactError(f"protected mutation path: {path}")
    for key in policy.get("settings", []):
        if key != key.upper() or any(key.startswith(p) for p in FORBIDDEN_SETTINGS):
            raise ArtifactError(f"protected mutation setting: {key}")
    for test in policy.get("tests", []):
        if not safe_path(test).startswith("test/"):
            raise ArtifactError("regression tests must be trusted test/ paths")

def git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise ArtifactError(r.stderr.strip()[-500:])
    return r.stdout.strip()

def source_root() -> Path:
    return Path(__file__).resolve().parents[3]

def tree_hash(root: Path) -> str:
    from ..source_fingerprint import source_tree_hash
    return source_tree_hash(root, error_type=ArtifactError)

@functools.lru_cache(maxsize=1)
def dependency_hash() -> str:
    import importlib.metadata
    import sys
    packages = sorted((d.metadata.get("Name", ""), d.version) for d in importlib.metadata.distributions())
    return digest(json.dumps({"python": sys.version, "packages": packages}, sort_keys=True).encode())

def baseline(root: Path, dest: Path) -> dict:
    revision = git(root, "rev-parse", "HEAD")
    if git(root, "status", "--porcelain", "--untracked-files=no", "--", "src", "playbooks", "adapters", "skills"):
        raise ArtifactError("source has uncommitted changes; commit the intended baseline first")
    if dest.exists():
        meta = verify(dest)
        if meta["revision"] != revision: raise ArtifactError("source baseline advanced before candidate generation")
        return meta
    dest.mkdir(parents=True)
    # Extract regular tracked bytes only; never follow archive links.
    import io
    import tarfile
    prefixes = [p for p in ("src", "playbooks", "adapters", "skills") if (root / p).exists()]
    raw = subprocess.run(["git", "-C", str(root), "archive", revision, *prefixes], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for member in archive:
            if member.isdir(): continue
            if not member.isfile(): raise ArtifactError("source archive contains a non-regular file")
            p = dest / safe_path(member.name)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(archive.extractfile(member).read())
    meta = {"revision": revision, "tree_sha": tree_hash(dest), "dependency_sha": dependency_hash()}
    atomic_json(dest / "artifact.json", meta)
    return meta

def verify(root: Path) -> dict:
    if any(p.name not in {"src", "playbooks", "adapters", "skills", "artifact.json"} or p.is_symlink() for p in root.iterdir()):
        raise ArtifactError("source artifact carries unexpected material outside execution roots")
    meta = json.loads((root / "artifact.json").read_text())
    if tree_hash(root) != meta["tree_sha"]:
        raise ArtifactError("source artifact changed after creation")
    return meta

def apply_candidate(base: Path, dest: Path, proposal: dict, policy: dict, *, workflow: str = "") -> dict:
    validate_policy(policy)
    verify(base)
    files, overrides = proposal.get("files", []), proposal.get("overrides", {})
    if not isinstance(files, list) or len(files) > 8 or not isinstance(overrides, dict) or not (files or overrides):
        raise ArtifactError("candidate needs 1–8 files or configuration overrides")
    if set(overrides) - set(policy.get("settings", [])):
        raise ArtifactError("candidate overrides an undeclared setting")
    seen = set()
    for f in files:
        path = safe_path(f["path"])
        if path in seen or protected(path) or not any(fnmatch.fnmatchcase(path, p) for p in policy.get("paths", [])):
            raise ArtifactError(f"candidate path not allowed: {path}")
        seen.add(path)
        old = base / path
        if f.get("before_sha") != (digest(old.read_bytes()) if old.is_file() else "new"):
            raise ArtifactError(f"candidate has a stale preimage: {path}")
        if not isinstance(f.get("content"), str) or len(f["content"].encode()) > 1_000_000:
            raise ArtifactError("candidate file too large or not text")
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(base, dest)
    for f in files:
        p = dest / f["path"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f["content"], encoding="utf-8")
    if overrides:
        if not workflow: raise ArtifactError("configuration candidate requires a workflow identity")
        config = dest / "playbooks" / "evolution-overrides.json"
        adopted = json.loads(config.read_text()) if config.exists() else {}
        adopted[workflow] = overrides
        atomic_json(config, adopted)
        seen.add("playbooks/evolution-overrides.json")
    meta = {**verify(base), "tree_sha": tree_hash(dest), "paths": sorted(seen), "overrides": overrides}
    if meta["tree_sha"] == verify(base)["tree_sha"] and not overrides:
        raise ArtifactError("candidate contains no change")
    atomic_json(dest / "artifact.json", meta)
    return meta

def runtime_settings(settings, workflow: str):
    """Only human-merged overrides from the shipped tree, scoped to one workflow."""
    from .enroll import declarations_for
    from .experiments import settings_for
    root = Path(getattr(settings, "playbooks_dir", "") or source_root() / "playbooks")
    path = root / "evolution-overrides.json"
    if not path.is_file(): return settings
    overrides = json.loads(path.read_text()).get(workflow, {})
    decl = declarations_for(settings).get(workflow)
    if not overrides: return settings
    if decl is None or set(overrides) - set(decl.evolution.get("settings", [])):
        raise ArtifactError("adopted settings are outside the declared mutation policy")
    validate_policy(decl.evolution)
    return settings_for(settings, overrides)[0]

def patch(base: Path, arm: Path) -> str:
    import difflib
    verify(base); verify(arm)
    out = []
    for path in json.loads((arm / "artifact.json").read_text()).get("paths", []):
        before = (base / path).read_text().splitlines(True) if (base / path).exists() else []
        after = (arm / path).read_text().splitlines(True)
        out.append(f"diff --git a/{path} b/{path}\n")
        if not before and not (base / path).exists():
            out.append("new file mode 100644\n")
        for line in difflib.unified_diff(before, after, fromfile=f"a/{path}" if (base / path).exists() else "/dev/null", tofile=f"b/{path}"):
            out.append(line)
            if not line.endswith("\n"): out.append("\n\\ No newline at end of file\n")
    return "".join(out)
