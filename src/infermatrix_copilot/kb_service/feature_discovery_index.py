"""Complete pinned inputs for feature discovery and breadth passes.

This is a localization index, never an execution, test or absence proof. Its
identity depends on source and scanning scope, not the eventual feature list.
"""
from __future__ import annotations

from ..persistence import atomic_write_bytes
from ..git_objects import tree_entries

import ast
from dataclasses import dataclass
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess

INDEX_VERSION = "feature-discovery-index-v2"
CONTRACT_UNIT_VERSION = "contract-units-v1"
MAX_CHUNK_CHARS = 12_000
DOC_SUFFIXES = {".md", ".mdx", ".rst", ".adoc", ".txt"}
_RESOURCE_SUFFIXES = {".json", ".yaml", ".yml", ".toml", ".ini", ".lock", ".xml", ".csv", ".svg", ".map", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
                      ".gz", ".zip", ".tar", ".woff", ".woff2", ".ttf", ".mp3", ".mp4", ".wav", ".so", ".dll"}
_LANGUAGES = {
    ".py": "python", ".rs": "rust", ".go": "go", ".js": "javascript", ".jsx": "javascript",
    ".ts": "javascript", ".tsx": "javascript", ".mts": "javascript", ".cts": "javascript",
    ".mjs": "javascript", ".cjs": "javascript", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp",
    ".hpp": "cpp", ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".swift": "swift",
    ".rb": "ruby", ".php": "php", ".cs": "csharp", ".scala": "scala", ".ex": "elixir", ".exs": "elixir",
    ".lua": "lua", ".dart": "dart", ".sh": "shell", ".ps1": "powershell", ".vue": "javascript",
    ".svelte": "javascript", ".html": "html", ".css": "css", ".scss": "css", ".less": "css",
}
_TEST_DIRS = {"test", "tests", "__tests__", "testing", "spec", "specs"}


def _sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _safe(path):
    return isinstance(path, str) and bool(path) and not path.startswith("/") and "\\" not in path \
        and all(part not in ("", ".", "..") for part in path.split("/"))


def language_for(path, text=""):
    language = _LANGUAGES.get(PurePosixPath(path).suffix.lower(), "unknown")
    if language != "unknown" or not text.startswith("#!"):
        return language
    first = text.splitlines()[0]
    for pattern, found in ((r"\bpython[0-9.]*\b", "python"), (r"\b(?:node|nodejs)\b", "javascript"),
                           (r"\b(?:sh|bash|zsh|fish)\b", "shell"), (r"\bruby\b", "ruby"), (r"\bperl\b", "perl")):
        if re.search(pattern, first):
            return found
    return "unknown"


def is_test(path):
    p = PurePosixPath(path)
    return any(part.lower() in _TEST_DIRS for part in p.parts[:-1]) or bool(
        re.search(r"(?:^test[_-]|[_-]test\.|\.(?:test|spec)\.|Test\.|Tests\.)", p.name))


def _test_only_exclusion(pattern):
    """Recognize production-only test filters without undoing vendor filters."""
    parts = pattern.casefold().split("/")
    outside = {"vendor", "third_party", "third-party", "node_modules", "generated", "dist", "build"}
    if any(part in outside for part in parts):
        return False
    return any(part in _TEST_DIRS for part in parts) or bool(re.search(
        r"(?:^|/)(?:test[_-][^/]*|[^/]*[_-]test\.[^/]*|[^/]*\.(?:test|spec)\.[^/]*)$", pattern.casefold()))


def _match(path, patterns):
    return any(fnmatch.fnmatchcase(path, pattern) or (
        "**/" in pattern and fnmatch.fnmatchcase(path, pattern.replace("**/", ""))) for pattern in patterns)


def _under(path, roots):
    return any(root in ("", ".", "./") or path == root.rstrip("/") or path.startswith(root.rstrip("/") + "/")
               for root in roots)


def discovery_scope(init, policy=None):
    """Capture scanning policy without binding it to discovered features."""
    if policy is not None:
        return {"roots": list(policy.roots), "exclude": list(policy.exclude),
                "suffixes": list(policy.suffixes), "filenames": list(policy.filenames)}
    return {"roots": list(getattr(init, "source_roots", ()) or (".",)),
            "exclude": list(getattr(init, "exclude", ())), "suffixes": None,
            "filenames": ["Dockerfile", "Dockerfile.*", "Makefile", "gradlew"]}


@dataclass(frozen=True)
class DiscoveryIndex:
    data: dict
    sha256: str

    @property
    def identity(self): return self.data["identity"]
    @property
    def production(self): return self.data["production"]
    @property
    def tests(self): return self.data["tests"]
    @property
    def docs(self): return self.data["docs"]
    @property
    def entries(self): return self.data["files"]
    @property
    def files(self): return self.entries
    @property
    def failures(self): return self.data["failures"]
    @property
    def scope_suggestions(self): return self.data["scope_suggestions"]
    @property
    def chunks(self): return list(iter_chunks(self, self.docs + self.production + self.tests))
    def to_dict(self): return self.data


def _git_output(tree, *args):
    try:
        return subprocess.check_output(["git", "-C", str(tree), *args], stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as exc:
        raise ValueError("pinned Git inventory could not be read: " + exc.stderr.decode("utf-8", "replace")[:300]) from exc


def _tracked(tree, pin):
    if not (tree / ".git").exists():
        names = []
        def failed_directory(exc):
            raise exc  # unreadable directories make the whole inventory incomplete
        for directory, children, filenames in os.walk(tree, followlinks=False, onerror=failed_directory):
            children[:] = sorted(name for name in children if not (Path(directory) / name).is_symlink())
            names.extend((Path(directory) / name).relative_to(tree).as_posix() for name in sorted(filenames)
                         if not (Path(directory) / name).is_symlink() and (Path(directory) / name).is_file())
        names.sort()
        fingerprints = []
        for name in names:
            try:
                fingerprints.append((name, hashlib.sha256((tree / name).read_bytes()).hexdigest()))
            except OSError as exc:
                fingerprints.append((name, "read-error:" + str(exc)))
        return names, _sha(fingerprints)
    head = _git_output(tree, "rev-parse", "HEAD").decode().strip()
    if head != pin:
        raise ValueError("discovery index checkout does not match the fixed source SHA")
    if _git_output(tree, "diff", "HEAD", "--name-only").strip():
        raise ValueError("discovery index needs unchanged tracked files at the fixed source SHA")
    raw = _git_output(tree, "ls-tree", "-r", "-z", pin)
    names = []
    for mode, kind, oid, name in tree_entries(raw):
        # Submodules and symlinks are recorded separately by their parent; do
        # not read targets outside the pinned tree as repository evidence.
        if mode in ("100644", "100755"):
            names.append(name.decode("utf-8"))
    return sorted(names), hashlib.sha256(raw).hexdigest()


def _identity(pin, scope, doc_globs, source_digest):
    if not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", pin or ""):
        raise ValueError("discovery index needs a full immutable source SHA")
    roots = list(scope.get("roots") or (".",))
    for root in roots:
        if root not in (".", "./", "") and not _safe(root.rstrip("/")):
            raise ValueError("discovery scope needs safe repository-relative roots")
    return {"version": INDEX_VERSION, "pin": pin, "scope": {**scope, "roots": roots},
            "doc_globs": list(doc_globs), "source_inventory_sha256": source_digest}


def load_discovery_index(cache_path, *, identity=None):
    try:
        envelope = json.loads(Path(cache_path).read_text(encoding="utf-8"))
        data, checksum = envelope["data"], envelope["sha256"]
        if _sha(data) != checksum or data["identity"].get("version") != INDEX_VERSION:
            raise ValueError("content hash or index version differs")
        if identity is not None and data["identity"] != identity:
            raise ValueError("pinned source or scan scope differs")
        if any(not _safe(path) for path in data["files"]) or any(
                path not in data["files"] for kind in ("production", "tests", "docs") for path in data[kind]):
            raise ValueError("unsafe or missing indexed path")
        return DiscoveryIndex(data, checksum)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid discovery index: {exc}") from exc


def load_index_snapshot(cache_dir, *, sha256, pin, skip_invalid=False):
    """Read a retained immutable index; never rebuild or fetch missing evidence."""
    cache_dir = Path(cache_dir)
    if (not isinstance(sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', sha256)
            or cache_dir.is_symlink() or not cache_dir.is_dir()):
        raise ValueError('original discovery index is unavailable')
    for path in sorted(cache_dir.glob('*.json')):
        try:
            if path.is_symlink():
                raise ValueError('discovery index must be a regular local snapshot')
            index = load_discovery_index(path)
        except ValueError:
            if skip_invalid:
                continue
            raise
        if index.sha256 == sha256:
            if index.identity['pin'] != pin:
                raise ValueError('original discovery index pin differs')
            return index
    raise ValueError('original discovery index is unavailable')


def build_discovery_index(tree, *, pin, scope, doc_globs=(), cache_path=None):
    tree = Path(tree)
    names, source_digest = _tracked(tree, pin)
    identity = _identity(pin, scope, doc_globs, source_digest)
    if cache_path is not None and Path(cache_path).exists():
        try: return load_discovery_index(cache_path, identity=identity)
        except ValueError: pass  # stale/corrupt caches never alter this batch's source scope
    roots, excluded = identity["scope"]["roots"], scope.get("exclude", ())
    extensions, filenames = scope.get("suffixes"), scope.get("filenames", ())
    test_globs = tuple(scope.get("test_globs", ()))
    entries, production, tests, docs, failures, suggestions = {}, [], [], [], [], []
    for path in names:
        if not _safe(path): raise ValueError("unsafe tracked path in discovery inventory")
        suffix = PurePosixPath(path).suffix.lower()
        doc = _match(path, doc_globs)
        test = is_test(path) or _match(path, test_globs)
        exclusion_hits = [pattern for pattern in excluded if _match(path, [pattern])]
        # Test directories excluded from production remain available to the
        # test index. Other exclusions (vendor/build/generated) remain effective.
        def test_exclusion(pattern):
            hard = {"vendor", "third_party", "third-party", "node_modules", "generated", "dist", "build"}
            return _test_only_exclusion(pattern) or (pattern in test_globs and
                not any(part in hard for part in pattern.casefold().split("/")))
        if exclusion_hits and not (test and all(test_exclusion(p) for p in exclusion_hits)):
            continue
        known = suffix in _LANGUAGES or _match(PurePosixPath(path).name, filenames)
        unfamiliar = suffix not in DOC_SUFFIXES | _RESOURCE_SUFFIXES and bool(suffix) and not known
        shebang = False
        if not suffix and _under(path, roots) and not known:
            try:
                with (tree / path).open("rb") as stream:
                    shebang = stream.read(256).startswith(b"#!")
            except OSError as exc:
                suggestions.append({"path": path, "reason": "extensionless input could not be classified: " + str(exc)})
        source = _under(path, roots) and (known or unfamiliar or shebang)
        if extensions is not None:
            # An explicit suffix scope is authoritative, including declarative
            # formats explicitly declared as production code by the adapter.
            source = _under(path, roots) and (PurePosixPath(path).suffix in extensions
                                              or _match(PurePosixPath(path).name, filenames))
        if not doc and not test and not source:
            if (known or unfamiliar) and not _under(path, roots): suggestions.append({"path": path, "reason": "code outside declared production roots"})
            elif (known or unfamiliar or shebang) and _under(path, roots): suggestions.append({"path": path, "reason": "unsupported suffix outside declared suffix scope"})
            continue
        kind = "doc" if doc else "test" if test else "source"
        entry = {"path": path, "kind": kind, "language": language_for(path), "status": "ready",
                 "parse_status": "lexical", "text": "", "lines": [], "sha256": "", "error": ""}
        try:
            raw = (tree / path).read_bytes()
            entry["sha256"] = hashlib.sha256(raw).hexdigest()
            if b"\0" in raw: raise ValueError("binary input is not readable source evidence")
            text = raw.decode("utf-8")
            entry.update(text=text, lines=text.splitlines(), bytes=len(raw), language=language_for(path, text))
            if entry["language"] == "python":
                try:
                    parsed = ast.parse(text)
                    entry["parse_status"] = "parsed"
                    if extensions is not None and kind == "source" and PurePosixPath(path).suffix == ".py" and not any(
                            not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Constant)
                            or not isinstance(node.value.value, str) for node in parsed.body):
                        entry["classification"] = "empty_package_marker"
                    entry["symbols"] = [{"name": node.name, "start": node.lineno, "end": node.end_lineno or node.lineno}
                                        for node in ast.walk(parsed) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
                    entry["imports"] = [{"text": ast.unparse(node), "start": node.lineno}
                                        for node in ast.walk(parsed) if isinstance(node, (ast.Import, ast.ImportFrom))]
                except (SyntaxError, ValueError) as exc:
                    entry["parse_status"] = "failed"
                    entry["parse_error"] = str(exc)
                    failures.append({"path": path, "kind": kind, "operation": "parse", "error": str(exc)})
            else:
                entry["symbols"] = [{"name": m.group(1), "start": n, "end": n}
                                    for n, line in enumerate(entry["lines"], 1)
                                    for m in [re.search(r"\b(?:function|class|interface|struct|fn|func|fun|def|public|export)\s+([\w$]+)", line)] if m]
                entry["imports"] = [{"text": line, "start": n} for n, line in enumerate(entry["lines"], 1)
                                    if re.match(r"\s*(?:import\b|from\b|#include\b|use\b|require\b)", line)]
                if entry["language"] == "unknown": entry["parse_status"] = "unknown"
        except (OSError, UnicodeError, ValueError) as exc:
            entry.update(status="error", parse_status="unknown", error=str(exc))
            failures.append({"path": path, "kind": kind, "operation": "read", "error": str(exc)})
        entries[path] = entry
        if entry.get("classification") != "empty_package_marker":
            {"doc": docs, "test": tests, "source": production}[kind].append(path)
    data = {"identity": identity, "production": production, "tests": tests, "docs": docs, "files": entries,
            "failures": failures, "scope_suggestions": suggestions, "tracked": names}
    index = DiscoveryIndex(data, _sha(data))
    if cache_path is not None:
        destination = Path(cache_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(destination, json.dumps({"data": data, "sha256": index.sha256}, ensure_ascii=False).encode("utf-8"))
    return index


def iter_chunks(index, paths, max_chars=MAX_CHUNK_CHARS):
    """Offer every line, with explicit offsets when one line exceeds a packet."""
    if max_chars < 1: raise ValueError("chunk size must be positive")
    for path in paths:
        entry = index.entries[path]
        if entry["status"] != "ready":
            yield {"id": _sha([path, "error", entry["error"]]), "path": path, "start": 0, "end": 0,
                   "text": "", "sha256": entry["sha256"], "kind": entry["kind"], "language": entry["language"],
                   "status": "error", "error": entry["error"]}
            continue
        lines, current, start, size = entry["lines"], [], 1, 0
        def chunk(body, first, last, **extra):
            return {"id": _sha([path, entry["sha256"], first, last, body]), "path": path, "start": first,
                    "end": last, "text": body, "sha256": entry["sha256"], "kind": entry["kind"],
                    "language": entry["language"], "parse_status": entry["parse_status"], "status": "ready", "error": "", **extra}
        for number, line in enumerate(lines, 1):
            if len(line) > max_chars:
                if current:
                    yield chunk("\n".join(current), start, number - 1)
                    current, size = [], 0
                for offset in range(0, len(line), max_chars):
                    yield chunk(line[offset:offset + max_chars], number, number, partial_line=True,
                                character_start=offset, character_end=min(len(line), offset + max_chars))
                start = number + 1
            else:
                if current and size + len(line) + 1 > max_chars:
                    yield chunk("\n".join(current), start, number - 1)
                    current, size = [], 0
                if not current: start = number
                current.append(line)
                size += len(line) + bool(size)
        if current: yield chunk("\n".join(current), start, len(lines))
        elif not lines: yield chunk("", 0, 0, empty=True)


def validate_evidence(index, reference):
    if not isinstance(reference, dict): return False
    entry = index.entries.get(reference.get("path"))
    start, end = reference.get("start"), reference.get("end")
    return bool(entry and entry["status"] == "ready"
                and (not reference.get("pin") or reference["pin"] == index.identity["pin"])
                and type(start) is int and type(end) is int
                and 1 <= start <= end <= len(entry["lines"]) and
                (not reference.get("sha256") or reference["sha256"] == entry["sha256"]))


def evidence_excerpt(index, reference):
    if not validate_evidence(index, reference): raise ValueError("evidence is not a readable pinned line range")
    entry = index.entries[reference["path"]]
    return {"path": reference["path"], "start": reference["start"], "end": reference["end"],
            "text": "\n".join(entry["lines"][reference["start"] - 1:reference["end"]]),
            "sha256": entry["sha256"], "kind": entry["kind"]}


def contract_units(index):
    """Conservative public-entry/contract leads, never certified features.

    Unit identity is independent of the candidate/catalog set. Parsed Python
    spans and lexical declarations/registrations in other languages supply
    anchors; opaque or unreadable files retain an explicit module lead. The
    complete inventory scan remains authoritative even when these hints miss
    a dynamic registration or a language-specific construct.
    """
    units = []
    declaration = re.compile(
        r"\b(?:export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const|let)|"
        r"pub(?:\([^)]*\))?\s+(?:async\s+)?(?:fn|struct|trait)|"
        r"(?:public|open)\s+(?:static\s+)?(?:fun|class|interface)|"
        r"(?:function|class|interface|struct|fn|func|fun))\s+([\w$]+)")
    arrow = re.compile(r"\b(?:export\s+)?(?:const|let|var)\s+([\w$]+)\s*=.*(?:=>|function\b)")
    registration = re.compile(
        r"(?:\.(?:get|post|put|delete|patch|route|command|add_parser|register|"
        r"register_plugin|add_route|add_task|schedule)\s*\(|"
        r"@(?:\w+\.)*(?:route|command|task)\b|"
        r"\b(?:module\.exports|exports\.[\w$]+)\s*=)")
    ui_event = re.compile(r"\b(?:onClick|onSubmit|onChange|on:click|on:submit)\s*=|@(?:click|submit)=")
    for path in index.production:
        entry = index.entries[path]
        lines = entry.get("lines", [])
        anchors = []
        if entry.get("status") == "ready":
            if entry.get("language") == "python" and entry.get("parse_status") == "parsed":
                anchors.extend((s["start"], s.get("end", s["start"]), "public_contract", s["name"])
                               for s in entry.get("symbols", []) if not s["name"].startswith("_"))
            for number, line in enumerate(lines, 1):
                match = declaration.search(line) or arrow.search(line)
                if match and entry.get("language") != "python":
                    anchors.append((number, min(len(lines), number + 39), "public_contract", match[1]))
                if registration.search(line):
                    anchors.append((number, min(len(lines), number + 39), "registration", line.strip()[:120]))
                if ui_event.search(line):
                    anchors.append((number, min(len(lines), number + 19), "ui_entry", line.strip()[:120]))
        if not anchors:
            anchors.append((1 if lines else 0, len(lines), "module_contract", PurePosixPath(path).stem))
        for start, end, kind, name in sorted(set(anchors)):
            units.append({"id": _sha([CONTRACT_UNIT_VERSION, index.identity["pin"], path,
                                      entry.get("sha256"), start, end, kind, name]),
                          "path": path, "start": start, "end": end, "kind": kind,
                          "name": name, "language": entry.get("language", "unknown"),
                          "status": entry.get("status", "error"),
                          "parse_status": entry.get("parse_status", "unknown"),
                          "sha256": entry.get("sha256", "")})
    return units


def unit_test_lookup(index, names):
    """One shared pass over nested tests for all named entry leads."""
    wanted = {name for name in names if re.fullmatch(r"[A-Za-z_$][\w$]{2,}", name)}
    found = {name: [] for name in wanted}
    for path in index.tests:
        test = index.entries[path]
        if test.get("status") != "ready":
            continue
        seen = set()
        for number, line in enumerate(test["lines"], 1):
            hits = set(re.findall(r"[A-Za-z_$][\w$]*", line)) & wanted - seen
            if not hits:
                continue
            window = test["lines"][max(0, number - 6):number + 10]
            if re.search(r"\bassert\w*\b|\bexpect\s*\(|\braises\s*\(", "\n".join(window)):
                ref = {"path": path, "start": max(1, number - 5), "end": min(len(test["lines"]), number + 10)}
                for name in hits:
                    found[name].append(ref)
                seen.update(hits)
    return found


def unit_evidence(index, unit, *, max_chars=12000, test_lookup=None):
    """Bounded anchor/body plus directly named imports/tests, all real lines.

    Related text is only a localization hint. No lexical match establishes a
    runtime call, test association, executed test or verified absence.
    """
    if type(max_chars) is not int or max_chars < 1:
        raise ValueError("unit evidence budget must be positive")
    entry = index.entries[unit["path"]]
    if entry.get("status") != "ready" or unit["start"] < 1:
        return []
    result, used = [], 0

    def offer(path, start, end):
        nonlocal used
        item = evidence_excerpt(index, {"path": path, "start": start, "end": end})
        # Preserve whole lines. An oversize anchor stays an unresolved lead;
        # partial text must never be offered with a complete-line citation.
        while len(item["text"]) + used + 150 > max_chars and end > start:
            end -= 1
            item = evidence_excerpt(index, {"path": path, "start": start, "end": end})
        if len(item["text"]) + used + 150 > max_chars:
            return
        if any(p["path"] == path and p["start"] <= start and p["end"] >= end for p in result):
            return
        result.append(item)
        used += len(item["text"]) + 150

    start, end = unit["start"], unit["end"]
    offer(unit["path"], max(1, start - 3), min(end, start + 59))
    if end > start + 59:
        offer(unit["path"], max(start + 60, end - 19), end)
    offered_body = "\n".join(item["text"] for item in result)
    helpers = [s for s in entry.get("symbols", []) if s["start"] != start and
               re.search(r"(?<![\w$])" + re.escape(s["name"]) + r"\s*\(", offered_body)]
    for helper in helpers[:8]:
        offer(unit["path"], helper["start"], min(helper.get("end", helper["start"]), helper["start"] + 59))
    for imported in entry.get("imports", [])[:12]:
        number = imported["start"]
        offer(unit["path"], number, number)
    name = unit.get("name", "")
    related = unit_test_lookup(index, [name]) if test_lookup is None else test_lookup
    for ref in related.get(name, []):
        offer(ref["path"], ref["start"], ref["end"])
        if used >= max_chars - 150:
            break
    return result


def build_for_stage(tree, stage):
    from .knowledge_coverage import load_policy, policy_path
    policy = getattr(stage, "coverage_policy", None)
    if policy is None:
        path = stage._coverage_policy_path() if hasattr(stage, "_coverage_policy_path") else policy_path(stage.lifecycle.repo, stage.lifecycle.adapter_dir)
        text = getattr(stage, "overlay", {}).get(path) or getattr(stage, "head", {}).get(path) or stage.rt.knowledge.show(stage._base_sha, path)
        if text: policy = load_policy(text, stage.repo_dir)
    doc_globs = list(stage.lifecycle.init.doc_globs)
    if policy: doc_globs += list(policy.catalog_sources)
    scope = discovery_scope(stage.lifecycle.init, policy)
    manifest = getattr(stage, "manifest", {}) or {}
    portable = manifest.get("portable_scope") if isinstance(manifest, dict) else None
    if isinstance(portable, dict):
        if policy is None:
            scope.update(suffixes=list(portable.get("suffixes", [])), filenames=list(portable.get("filenames", [])))
        scope["test_globs"] = list(portable.get("test_globs", []))
    name = _sha([stage.record.pin, scope, sorted(set(doc_globs))])
    return build_discovery_index(tree, pin=stage.record.pin, scope=scope, doc_globs=sorted(set(doc_globs)),
                                 cache_path=stage.rt.state_dir / "feature-discovery-index" / f"{name}.json")
