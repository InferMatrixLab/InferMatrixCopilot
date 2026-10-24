"""Immutable, pinned retrieval facts shared by isolated depth workers.

This is a positive localization index, not a runtime graph or absence proof.
Only the final source verifier can establish a supported knowledge claim.
"""

from __future__ import annotations

from ..persistence import atomic_write_bytes

import ast
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
from types import MappingProxyType
from collections.abc import Mapping

from ..knowledge_service.lifecycle import safe_source_path
from .knowledge_coverage import SUFFIXES, _LEXICAL_NO_CODE

INDEX_VERSION = "positive-depth-index-v3"
_DOC_SUFFIXES = {".md", ".mdx", ".rst", ".txt", ".adoc"}


def _sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class SharedDepthIndex:
    """Read-only facts; per-worker ranking and AST caches remain separate."""

    data: Mapping
    sha256: str

    @property
    def identity(self):
        return self.data["identity"]

    @property
    def files(self):
        return self.data["files"]


def _identity(pin, policy_sha256, production):
    if not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", pin or ""):
        raise ValueError("shared depth index needs a full immutable pin")
    if not re.fullmatch(r"[a-f0-9]{64}", policy_sha256 or ""):
        raise ValueError("shared depth index needs the policy SHA256")
    names = sorted(set(production))
    if any(not safe_source_path(path) for path in names):
        raise ValueError("shared depth index has unsafe production paths")
    return {"version": INDEX_VERSION, "pin": pin, "policy_sha256": policy_sha256,
            "production_sha256": _sha(names)}


def load_depth_index(cache_path: Path, *, pin: str, policy_sha256: str,
                     production: list[str] | None = None) -> SharedDepthIndex:
    """Read and validate one atomic snapshot, including its exact content hash."""
    try:
        envelope = json.loads(Path(cache_path).read_bytes())
        data = envelope["data"]
        checksum = envelope["sha256"]
        if not isinstance(data, dict) or checksum != _sha(data):
            raise ValueError("content hash differs")
        expected = _identity(pin, policy_sha256, production if production is not None else data["production"])
        if data["identity"] != expected or data["identity"]["production_sha256"] != _sha(data["production"]):
            raise ValueError("pin, policy, version or production inventory differs")
        if any(not safe_source_path(path) for path in data["tracked"]) or any(
                not safe_source_path(path) for path in data["files"]):
            raise ValueError("unsafe indexed path")
        if not set(data["production"]).issubset(data["tracked"]):
            raise ValueError("production inventory is not tracked")
    except (OSError, KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid shared depth index: {exc}") from exc
    return SharedDepthIndex(_freeze(data), checksum)


@contextmanager
def _build_lock(path):
    # Campaign supervisors publish before starting workers. The advisory lock
    # additionally makes simultaneous local build requests reuse that snapshot.
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _literal_target(path, literal, tracked, *, relative_to_file=False):
    if not isinstance(literal, str) or literal.startswith(("/", "http:", "https:")) or "\\" in literal:
        return None
    parent = PurePosixPath(path).parent
    choices = [(parent / literal).as_posix()]
    if not relative_to_file:
        if not literal.startswith("."):
            # Package-root entryPoints are common in frontend test scripts.
            # This is only a positive input hint, not a claim about process cwd.
            package = next((ancestor for ancestor in [parent, *parent.parents]
                            if (ancestor / "package.json").as_posix() in tracked), None)
            if package is not None:
                choices.insert(0, (package / literal).as_posix())
        choices.append(literal)
    for choice in choices:
        parts = []
        for part in choice.split("/"):
            if part == "..":
                if not parts:
                    break
                parts.pop()
            elif part not in ("", "."):
                parts.append(part)
        else:
            candidate = "/".join(parts)
            if safe_source_path(candidate) and candidate in tracked:
                return candidate
    return None


def _literal_links(path, raw, module, tracked):
    """Literal file/bundle inputs are retrieval hints, never execution edges."""
    found = []
    if module is not None:
        for node in ast.walk(module):
            if not isinstance(node, ast.Call):
                continue
            name = ast.unparse(node.func).rsplit(".", 1)[-1]
            if name not in {"open", "read_text", "read_bytes", "readFile", "readFileSync"} or not node.args:
                continue
            literal = node.args[0]
            if isinstance(literal, ast.Constant) and isinstance(literal.value, str):
                target = _literal_target(path, literal.value, tracked)
                if target:
                    found.append({"path": target, "kind": "literal_file_input", "line": node.lineno})
    elif PurePosixPath(path).suffix in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}:
        clean = _LEXICAL_NO_CODE.sub(lambda match: " " * len(match.group()), raw)
        pattern = re.compile(r"\b(?:readFile|readFileSync)\s*\(\s*(?:new\s+URL\s*\(\s*)?(['\"])([^'\"]+)\1")
        for match in pattern.finditer(raw):
            if not re.match(r"(?:readFile|readFileSync)\b", clean[match.start():]):
                continue
            target = _literal_target(path, match[2], tracked, relative_to_file="new" in match[0])
            if target:
                found.append({"path": target, "kind": "literal_file_input", "line": raw.count("\n", 0, match.start()) + 1})
        # A literal bundle entry is useful when a test executes a built output.
        # It is not claimed to resolve arbitrary build configuration or aliases.
        for match in re.finditer(r"\bentryPoints\s*:\s*\[([^\]]*)\]", raw):
            if not clean[match.start():].startswith("entryPoints"):
                continue
            for item in re.finditer(r"(['\"])([^'\"]+)\1", match[1]):
                target = _literal_target(path, item[2], tracked)
                if target:
                    found.append({"path": target, "kind": "literal_bundle_entry", "line": raw.count("\n", 0, match.start()) + 1})
    return found


def _build(tree, production, identity):
    # Imported lazily: DepthContext can consume an index without a cycle.
    from .depth_inputs import DepthContext, _TEST_PATH, _TEST_SUFFIXES, _FACET_PATTERNS

    if (tree / ".git").exists():
        actual = subprocess.check_output(["git", "-C", str(tree), "rev-parse", "HEAD"], text=True).strip()
        if actual != identity["pin"]:
            raise ValueError("source checkout differs from the index pin")
        if subprocess.check_output(["git", "-C", str(tree), "status", "--porcelain", "--untracked-files=no"], text=True):
            raise ValueError("shared index source checkout has tracked modifications")
    context = DepthContext(tree, production)
    tracked = context._tracked()
    tracked_set = set(tracked)
    if not set(production).issubset(tracked_set):
        raise ValueError("production inventory is not tracked")
    tests = [path for path in tracked if PurePosixPath(path).suffix in _TEST_SUFFIXES and
             (_TEST_PATH.search(path) or re.search(r"(?:^|/)test-[^/]+\.[^/]+$", path)
              or PurePosixPath(path).name == "conftest.py")]
    indexed = [path for path in tracked if path in production or PurePosixPath(path).suffix in set(SUFFIXES) | _DOC_SUFFIXES
               or PurePosixPath(path).name == "package.json"]
    files, reverse = {}, {}
    facet_patterns = {facet: re.compile(pattern, re.I) for facet, pattern in _FACET_PATTERNS.items()}
    for path in indexed:
        value = context._file(path)
        if value is None:
            files[path] = {"sha256": None, "lines": [], "definitions": [], "imports": [],
                           "unresolved": [path + ": unreadable indexed source"], "called_symbols": [],
                           "assertion_lines": [], "has_entry": False, "literal_links": [], "facet_lines": {}}
            continue
        lines, module, _ = value
        raw = "\n".join(lines)
        is_code = PurePosixPath(path).suffix in SUFFIXES or path in production
        imports, unresolved = context._import_targets(path) if is_code else (set(), set())
        definitions = context._definitions(path) if is_code else []
        for definition in definitions:
            node = context.definition_cache.get(path, {}).get(definition["symbol"])
            if isinstance(node, ast.ClassDef):
                methods = [child.lineno for child in node.body if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))]
                definition["retrieval_end"] = min(definition["end"], min(methods) - 1 if methods else definition["end"], definition["start"] + 39)
        facet_lines = {facet: [number for number, line in enumerate(lines, 1) if pattern.search(line)]
                       for facet, pattern in facet_patterns.items()} if is_code else {}
        clean = _LEXICAL_NO_CODE.sub(lambda match: "\n" * match.group().count("\n"), raw)
        if module is not None:
            nodes = list(ast.walk(module))
            has_entry = any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test")
                            or isinstance(node, ast.Assert) for node in nodes)
            assertion_lines = sorted({node.lineno for node in nodes if isinstance(node, ast.Assert) or
                                      isinstance(node, ast.Call) and re.search(r"(?:assert|expect|raises)", ast.unparse(node.func))})
            called = sorted({node.func.id if isinstance(node.func, ast.Name) else node.func.attr
                             for node in nodes if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))})
        else:
            has_entry = bool(re.search(r"\b(?:test|it|describe|assert|expect)\s*[.(]", clean))
            assertion_lines = [i for i, line in enumerate(clean.splitlines(), 1) if re.search(r"\b(?:assert|expect|raises)\s*[.(]", line)]
            called = sorted(set(re.findall(r"\b([\w$]+)\s*\(", clean)))
        literal_links = _literal_links(path, raw, module, tracked_set) if is_code else []
        files[path] = {"sha256": context.file_hashes[path], "lines": lines, "definitions": definitions,
                       "imports": sorted(imports), "unresolved": sorted(unresolved), "called_symbols": called,
                       "assertion_lines": assertion_lines, "has_entry": has_entry, "literal_links": literal_links,
                       "facet_lines": facet_lines}
        for target in imports:
            reverse.setdefault(target, []).append(path)
    conftests = {PurePosixPath(path).parent.as_posix(): path for path in tests if PurePosixPath(path).name == "conftest.py"}
    associations, by_source, by_symbol, by_stem, by_doc = {}, {}, {}, {}, {}
    for path in tests:
        parents = [PurePosixPath(path).parent, *PurePosixPath(path).parent.parents]
        ancestors = [conftests[parent.as_posix()] for parent in parents if parent.as_posix() in conftests]
        pending, closure, unresolved = [path, *ancestors], set(), set()
        while pending:
            target = pending.pop()
            if target in closure:
                continue
            closure.add(target)
            fact = files.get(target)
            if fact is None:
                unresolved.add(target + ": input has no indexed parser")
                continue
            unresolved.update(fact["unresolved"])
            pending.extend(set(fact["imports"]) | {item["path"] for item in fact["literal_links"]})
        associations[path] = {"closure": sorted(closure), "unresolved": sorted(unresolved), "conftest_ancestors": ancestors}
        for target in closure:
            by_source.setdefault(target, []).append(path)
        fact = files[path]
        for symbol in fact["called_symbols"]:
            by_symbol.setdefault(symbol, []).append(path)
        stem = PurePosixPath(path).stem.removeprefix("test_").removesuffix("_test")
        stem = re.sub(r"[._](?:test|spec)$", "", stem)
        by_stem.setdefault(stem, []).append(path)
    test_set = set(tests)
    for path, fact in files.items():
        if PurePosixPath(path).suffix not in _DOC_SUFFIXES:
            continue
        # Exact repository-relative references; no inferred runtime association.
        tokens = set(re.findall(r"[\w.-]+(?:/[\w.-]+)+", "\n".join(fact["lines"])))
        by_doc[path] = sorted(tokens & test_set)
    data = {"identity": identity, "production": sorted(production), "tracked": tracked, "files": files,
            "reverse_imports": {key: sorted(value) for key, value in reverse.items()},
            "tests": associations, "tests_by_source": by_source, "tests_by_symbol": by_symbol,
            "tests_by_stem": by_stem, "tests_by_doc": by_doc,
            "test_inventory_sha256": _sha([(path, files[path]["sha256"]) for path in tests]),
            "source_snapshot_sha256": _sha([(path, fact["sha256"]) for path, fact in sorted(files.items())]),
            "limitations": "Positive static localization only; dynamic dispatch, unsupported syntax and test absence remain unknown."}
    return data


def build_depth_index(tree: Path, production: list[str], *, pin: str, policy_sha256: str,
                      cache_path: Path) -> SharedDepthIndex:
    """Publish once under a build lock; later workers load the immutable file."""
    tree, cache_path = Path(tree), Path(cache_path)
    if cache_path.resolve().is_relative_to(tree.resolve()):
        raise ValueError("shared depth index cache must be outside the pinned source tree")
    identity = _identity(pin, policy_sha256, production)
    if cache_path.exists():
        return load_depth_index(cache_path, pin=pin, policy_sha256=policy_sha256, production=production)
    with _build_lock(cache_path.with_name(cache_path.name + ".lock")):
        if cache_path.exists():
            return load_depth_index(cache_path, pin=pin, policy_sha256=policy_sha256, production=production)
        data = _build(tree, sorted(set(production)), identity)
        atomic_write_bytes(cache_path, json.dumps({"data": data, "sha256": _sha(data)},
                                                 separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    return load_depth_index(cache_path, pin=pin, policy_sha256=policy_sha256, production=production)
