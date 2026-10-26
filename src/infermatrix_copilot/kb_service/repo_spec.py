"""Portable repository proposals, registration and snapshot-scoped resolution.

Public specs contain no local paths or credentials. Runtime bindings live in
the host registry and are not copied into a served knowledge snapshot.
"""

from __future__ import annotations

from ..persistence import atomic_write_bytes

import fnmatch
import hashlib
import json
import re
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .git_source import COMMIT_ID, GitSource, public_locator

REGISTRY_FILE = "_repositories.yaml"
REPO_ID = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
DEFAULT_EXCLUDE = ("**/vendor/**", "**/third_party/**", "**/node_modules/**", "**/.venv/**",
                   "**/dist/**", "**/build/**", "**/target/**", ".infermatrix/knowledge/**")
DEFAULT_DOCS = ("README*", "**/*.md", "**/*.rst", "**/*.adoc")
DEFAULT_TESTS = ("**/tests/**", "**/test/**", "**/__tests__/**", "**/test_*", "**/*_test.*",
                 "**/*.test.*", "**/*.spec.*", "**/spec/**", "**/cypress/**")
LANGUAGES = {".py": "python", ".pyi": "python", ".ts": "typescript", ".tsx": "typescript",
             ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
             ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
             ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp",
             ".rb": "ruby", ".php": "php", ".swift": "swift", ".sh": "shell", ".ps1": "powershell",
             ".vue": "vue", ".svelte": "svelte", ".scala": "scala", ".ex": "elixir", ".erl": "erlang"}


class RepoSpecError(ValueError):
    pass


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _relative(path: str) -> str:
    pure = PurePosixPath(path)
    if not path or pure.is_absolute() or re.match(r"^[A-Za-z]:", path) or "\\" in path or ".." in pure.parts or "\0" in path:
        raise RepoSpecError("repository scope/slice must be a relative path")
    return pure.as_posix()


def glob_matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) or
               (pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:]))
               for pattern in patterns)


@dataclass(frozen=True)
class RepoSpec:
    repo_id: str
    title: str = ""
    aliases: tuple[str, ...] = ()
    source_locator: str = ""
    source_pin: str = ""
    object_format: str = "sha1"
    forge_kind: str = "none"
    forge_host: str = ""
    forge_project: str = ""
    source_roots: tuple[str, ...] = (".",)
    exclude: tuple[str, ...] = DEFAULT_EXCLUDE
    doc_globs: tuple[str, ...] = DEFAULT_DOCS
    test_globs: tuple[str, ...] = DEFAULT_TESTS
    languages: tuple[str, ...] = ()
    knowledge_storage: str = "central"
    knowledge_slice: str = ""
    coverage_target: float = 0.85
    per_facet_gt: float = 0.90
    catalog_hash: str = ""
    policy_hash: str = ""
    dependencies: tuple[dict, ...] = ()

    def __post_init__(self):
        if not REPO_ID.fullmatch(self.repo_id):
            raise RepoSpecError("repo_id must be a stable safe lowercase identifier")
        if self.object_format not in ("sha1", "sha256"):
            raise RepoSpecError("unsupported source object format")
        if self.source_pin and (not COMMIT_ID.fullmatch(self.source_pin) or
                                len(self.source_pin) != (40 if self.object_format == "sha1" else 64)):
            raise RepoSpecError("source_pin must match the repository object format")
        if self.forge_kind not in ("none", "github", "gitlab"):
            raise RepoSpecError("unsupported forge kind")
        if self.knowledge_storage not in ("central", "in_repo"):
            raise RepoSpecError("knowledge_storage must be central or in_repo")
        if self.source_locator and public_locator(self.source_locator) != self.source_locator:
            raise RepoSpecError("public source locator must contain no local paths or credentials")
        if self.forge_host and public_locator(self.forge_host) != self.forge_host:
            raise RepoSpecError("public forge host must contain no credentials")
        if not self.source_roots or not all(isinstance(x, str) for x in self.source_roots):
            raise RepoSpecError("source_roots must contain relative paths")
        for root in self.source_roots:
            _relative(root)
        target = self.knowledge_slice or f"repos/{self.repo_id}"
        if _relative(target) != f"repos/{self.repo_id}":
            raise RepoSpecError("knowledge slice must be repos/<repo_id>")
        object.__setattr__(self, "knowledge_slice", target)
        for name in ("aliases", "exclude", "doc_globs", "test_globs", "languages"):
            values = getattr(self, name)
            if not isinstance(values, (list, tuple)) or any(not isinstance(x, str) or not x for x in values):
                raise RepoSpecError(f"{name} must contain non-empty strings")
            object.__setattr__(self, name, tuple(values))
        if isinstance(self.coverage_target, bool) or not isinstance(self.coverage_target, (int, float)) or not 0.85 <= self.coverage_target <= 1:
            raise RepoSpecError("coverage_target must be at least 0.85 and no greater than 1")
        if isinstance(self.per_facet_gt, bool) or not isinstance(self.per_facet_gt, (int, float)) or not 0.90 <= self.per_facet_gt < 1:
            raise RepoSpecError("per_facet_gt must be at least 0.90 and less than 1")
        for value in (self.catalog_hash, self.policy_hash):
            if value and not re.fullmatch(r"[0-9a-f]{64}", value):
                raise RepoSpecError("catalog/policy hashes must be SHA256")
        if not isinstance(self.dependencies, (list, tuple)):
            raise RepoSpecError("dependencies must be public versioned relation records")
        for link in self.dependencies:
            allowed = {"repo_id", "status", "source_pin", "catalog_hash", "policy_hash", "relation", "evidence"}
            if not isinstance(link, dict) or set(link) - allowed or not REPO_ID.fullmatch(str(link.get("repo_id", ""))):
                raise RepoSpecError("dependency contains invalid public identity or runtime fields")
            if not isinstance(link.get("status", "unknown"), str):
                raise RepoSpecError("dependency status must be explicit text")
            confirmed = link.get("status") == "confirmed"
            pin = link.get("source_pin", "")
            if not isinstance(pin, str) or (pin and not COMMIT_ID.fullmatch(pin)) or (confirmed and not pin):
                raise RepoSpecError("confirmed dependency needs its exact source pin")
            for name in ("catalog_hash", "policy_hash"):
                value = link.get(name, "")
                if not isinstance(value, str) or (value and not re.fullmatch(r"[0-9a-f]{64}", value)) or (confirmed and name == "catalog_hash" and not value):
                    raise RepoSpecError("confirmed dependency needs its exact catalog hash")
            evidence = link.get("evidence", [])
            if not isinstance(evidence, (list, tuple)):
                raise RepoSpecError("dependency evidence must be public source references")
            for ref in evidence:
                if not isinstance(ref, dict) or set(ref) - {"path", "start", "end", "pin", "sha256"}:
                    raise RepoSpecError("dependency evidence contains runtime or unknown fields")
                _relative(ref.get("path", ""))
                if (any(isinstance(ref.get(n), bool) or not isinstance(ref.get(n), int) or ref[n] < 1 for n in ("start", "end") if n in ref)
                        or ("start" in ref and "end" in ref and ref["end"] < ref["start"])):
                    raise RepoSpecError("dependency evidence line range is invalid")
                if ref.get("pin") and not COMMIT_ID.fullmatch(ref["pin"]):
                    raise RepoSpecError("dependency evidence pin is invalid")
                if ref.get("sha256") and not re.fullmatch(r"[0-9a-f]{64}", ref["sha256"]):
                    raise RepoSpecError("dependency evidence hash is invalid")
        object.__setattr__(self, "dependencies", tuple(self.dependencies))

    def to_dict(self) -> dict:
        return json.loads(canonical_json(asdict(self)))

    @classmethod
    def from_dict(cls, value: dict) -> "RepoSpec":
        if not isinstance(value, dict):
            raise RepoSpecError("repository spec must be a mapping")
        try:
            return cls(**value)
        except TypeError as exc:
            raise RepoSpecError("repository spec has invalid or unknown fields") from exc

    @property
    def sha256(self) -> str:
        return digest(self.to_dict())


def generated_knowledge_roots(source: GitSource, pin: str) -> tuple[str, ...]:
    """Detect committed KB artifacts; exclusion still requires scope review.

    A package named ``knowledge`` alone is never sufficient. Canonical roots
    require the existing format/AGENTS/repository-index layout. Mirror roots
    additionally bind their actual manifest and committed knowledge bytes.
    """
    files = source.files(pin)
    roots = set()
    for path in files:
        if not path.endswith("/_format.yaml"):
            continue
        root = path.rsplit("/", 1)[0]
        if root + "/AGENTS.md" not in files or root + "/repos/_index.md" not in files:
            continue
        try:
            value = yaml.safe_load(source.read(pin, path))
        except yaml.YAMLError:
            continue
        if isinstance(value, dict) and value.get("format_version") == 2:
            roots.add(root)
    for path in files:
        if not path.endswith("/MIRROR.json"):
            continue
        root = path.rsplit("/", 1)[0]
        if root + "/knowledge" not in roots or root + "/MANIFEST.json" not in files:
            continue
        raw = source.read(pin, root + "/MANIFEST.json")
        try:
            marker = json.loads(source.read(pin, path))
            manifest = json.loads(raw)
        except ValueError:
            continue
        if (not isinstance(marker, dict) or not isinstance(manifest, dict)
                or marker.get("readonly") is not True
                or marker.get("manifest_sha256") != hashlib.sha256(raw).hexdigest()
                or manifest.get("schema_version") != 1 or manifest.get("knowledge_format") != 2
                or not isinstance(manifest.get("files"), dict)):
            continue
        prefix = root + "/knowledge/"
        actual = {p.removeprefix(prefix): hashlib.sha256(source.read(pin, p)).hexdigest()
                  for p in files if p.startswith(prefix)}
        if actual == manifest["files"]:
            roots.add(root)
    return tuple(root for root in sorted(roots)
                 if not any(root.startswith(parent + "/") for parent in roots if parent != root))


def repository_inventory(source: GitSource, spec: RepoSpec) -> dict:
    """All declared committed files are classified once; no prefix truncation."""
    files = source.files(spec.source_pin)
    groups = {"production": [], "tests": [], "docs": [], "assets": [], "excluded": [], "outside_scope": []}
    languages = {}
    for path in sorted(files):
        if glob_matches(path, spec.exclude):
            groups["excluded"].append(path)
        elif glob_matches(path, spec.test_globs):
            groups["tests"].append(path)
        elif glob_matches(path, spec.doc_globs):
            groups["docs"].append(path)
        elif not any(root == "." or path == root.rstrip("/") or path.startswith(root.rstrip("/") + "/")
                     for root in spec.source_roots):
            groups["outside_scope"].append(path)
        else:
            language = LANGUAGES.get(PurePosixPath(path).suffix.lower(), "unknown")
            if language == "unknown":
                raw = source.read(spec.source_pin, path)
                try:
                    raw.decode("utf-8")
                except UnicodeDecodeError:
                    groups["assets"].append(path)
                    continue
                if b"\0" in raw:
                    groups["assets"].append(path)
                    continue
            groups["production"].append(path)
            languages[path] = language
    generated = generated_knowledge_roots(source, spec.source_pin)
    suggestions = [root + "/**" for root in generated
                   if any(not glob_matches(p, spec.exclude) for p in files if p.startswith(root + "/"))]
    return {"source_pin": spec.source_pin, "source_tree": source.tree_id(spec.source_pin),
            "scope_hash": digest({"roots": spec.source_roots, "exclude": spec.exclude,
                                  "docs": spec.doc_globs, "tests": spec.test_globs}),
            **groups, "languages": languages,
            "generated_knowledge_roots": list(generated),
            "generated_knowledge_exclusion_proposals": suggestions,
            "unknown_language_paths": sorted(path for path, language in languages.items() if language == "unknown"),
            "assets_claim": "Binary first-party assets are inventoried separately, not parsed implementation evidence; scope classifications require proposal acceptance.",
            "scope_expansion_proposals": groups["outside_scope"],
            "claim": "Declared committed inventory, not an assertion that all features were discovered."}


def propose_repository(source: GitSource, *, repo_id: str | None = None, pin: str | None = None,
                       storage: str = "central", forge_kind: str | None = None) -> dict:
    fixed = source.resolve(pin or "HEAD")
    locator = source.origin()
    parsed = urllib.parse.urlsplit(locator)
    project_path = parsed.path.strip("/").removesuffix(".git")
    identity = (f"{parsed.netloc}/{project_path.casefold() if parsed.hostname == 'github.com' else project_path}"
                if locator else "roots:" + ",".join(source.root_commits(fixed)))
    repo_id = repo_id or "repo-" + hashlib.sha256(identity.encode()).hexdigest()[:16]
    kind = forge_kind or ("github" if parsed.hostname == "github.com" else "none")
    project = parsed.path.strip("/").removesuffix(".git") if kind != "none" else ""
    host = ("https://api.github.com" if kind == "github" else
            f"https://{parsed.netloc}" if kind == "gitlab" and parsed.netloc else "")
    title = project_path.split("/")[-1] if locator else source.path.name
    aliases = tuple(dict.fromkeys((title, project_path))) if locator else (title,)
    spec = RepoSpec(repo_id=repo_id, title=title, aliases=aliases,
                    source_locator=locator, source_pin=fixed, object_format=source.object_format,
                    forge_kind=kind, forge_host=host, forge_project=project, knowledge_storage=storage)
    generated = generated_knowledge_roots(source, fixed)
    if generated:
        spec = RepoSpec.from_dict({**spec.to_dict(), "exclude": list(dict.fromkeys((*spec.exclude, *(root + "/**" for root in generated))))})
    inventory = repository_inventory(source, spec)
    spec = RepoSpec.from_dict({**spec.to_dict(), "languages": sorted(set(inventory["languages"].values()))})
    return {"schema_version": 1, "kind": "repository_registration_proposal", "spec": spec.to_dict(),
            "spec_sha256": spec.sha256, "source_head": fixed, "source_tree": source.tree_id(fixed),
            "inventory": inventory, "inventory_sha256": digest(inventory), "accepted": False}


def acceptance_receipt(spec: RepoSpec, source: GitSource, registration_head: str, *, accepted: bool) -> dict:
    """Explicit acceptance of a scope proposal, not knowledge/native approval."""
    if accepted is not True:
        raise RepoSpecError("registration requires explicit proposal acceptance")
    if source.resolve(spec.source_pin) != spec.source_pin:
        raise RepoSpecError("registration source pin mismatch")
    head = source.resolve(registration_head)
    return {"schema_version": 1, "kind": "repository_registration_acceptance", "accepted": True,
            "repo_id": spec.repo_id, "spec_sha256": spec.sha256, "source_pin": spec.source_pin,
            "source_tree": source.tree_id(spec.source_pin), "registration_head": head,
            "claim": "Accepted repository identity and scope only; knowledge requires its own independent review."}


class RepoRegistry:
    def __init__(self, state_dir: str | Path):
        self.path = Path(state_dir) / "repositories" / "registry.json"

    def _read(self) -> dict:
        if not self.path.exists():
            return {"schema_version": 1, "repos": {}}
        value = json.loads(self.path.read_text())
        if value.get("schema_version") != 1 or not isinstance(value.get("repos"), dict):
            raise RepoSpecError("host repository registry is malformed")
        return value

    def register(self, spec: RepoSpec, receipt: dict, *, source_path: str | Path,
                 knowledge_root: str | Path) -> None:
        source = GitSource(source_path)
        expected = acceptance_receipt(spec, source, receipt.get("registration_head", ""), accepted=True)
        if receipt != expected:
            raise RepoSpecError("registration acceptance receipt does not bind the exact spec/head")
        value = self._read()
        row = {"spec": spec.to_dict(), "receipt": receipt,
               "bindings": {"source_path": str(Path(source_path).resolve()),
                            "knowledge_root": str(Path(knowledge_root).resolve())}}
        prior = value["repos"].get(spec.repo_id)
        if prior and prior != row:
            raise RepoSpecError("registered repository conflicts; explicit migration is required")
        value["repos"][spec.repo_id] = row
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(self.path, canonical_json(value))

    def get(self, repo_id: str) -> dict | None:
        return self._read()["repos"].get(repo_id)

    def resolve(self, selector: str) -> dict | None:
        rows = []
        for repo_id, row in self._read()["repos"].items():
            spec = RepoSpec.from_dict(row["spec"])
            if selector.casefold() in {x.casefold() for x in (repo_id, spec.source_locator, *spec.aliases) if x}:
                rows.append(row)
        if len(rows) > 1:
            raise RepoSpecError("repository selector is ambiguous")
        return rows[0] if rows else None

    def public_snapshot(self) -> dict:
        return {"schema_version": 1, "repos": {key: row["spec"] for key, row in sorted(self._read()["repos"].items())}}


@dataclass(frozen=True)
class RepoSnapshotBinding:
    repo_id: str
    knowledge_slice: str
    catalog_hash: str = ""
    source_pin: str = ""
    policy_hash: str = ""
    owner_routes: tuple[dict, ...] = ()
    dependencies: tuple[dict, ...] = ()
    registry_hash: str = ""


def resolve_snapshot_repo(view, selector: str) -> RepoSnapshotBinding | None:
    """Resolve entirely inside one KnowledgeView; never use the host registry."""
    try:
        text = view.read_text(REGISTRY_FILE)
    except FileNotFoundError:
        if not REPO_ID.fullmatch(selector):
            return None
        try:
            view.path(f"repos/{selector}/_index.md")
        except FileNotFoundError:
            return None
        spec = RepoSpec(repo_id=selector)
        registry_hash = ""
    else:
        value = yaml.safe_load(text)
        if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("repos"), dict):
            raise RepoSpecError("snapshot repository registry is malformed")
        matches = []
        for key, row in value["repos"].items():
            spec = RepoSpec.from_dict(row)
            if key != spec.repo_id:
                raise RepoSpecError("snapshot registry key does not match repo_id")
            if selector.casefold() in {x.casefold() for x in (key, spec.source_locator, *spec.aliases) if x}:
                matches.append(spec)
        if len(matches) > 1:
            raise RepoSpecError("snapshot repository selector is ambiguous")
        if not matches:
            return None
        spec = matches[0]
        registry_hash = hashlib.sha256(text.encode()).hexdigest()
        view.path(f"{spec.knowledge_slice}/_index.md")
    try:
        routes = yaml.safe_load(view.read_text(f"{spec.knowledge_slice}/_routes.yaml"))
    except FileNotFoundError:
        routes = {"owners": []}
    if not isinstance(routes, dict) or not isinstance(routes.get("owners"), list):
        raise RepoSpecError("snapshot owner routes are malformed")
    for row in routes["owners"]:
        if not isinstance(row, dict) or not isinstance(row.get("owner"), str) or not isinstance(row.get("path"), str):
            raise RepoSpecError("snapshot owner route is malformed")
        _relative(row["path"])
        if not row["path"].startswith(spec.knowledge_slice + "/"):
            raise RepoSpecError("snapshot owner route crosses repository slices")
        view.path(row["path"])
    return RepoSnapshotBinding(spec.repo_id, spec.knowledge_slice, spec.catalog_hash, spec.source_pin,
                               spec.policy_hash, tuple(routes["owners"]), spec.dependencies, registry_hash)


def list_snapshot_repos(view) -> tuple[RepoSnapshotBinding, ...]:
    """Enumerate only this request's served registry (legacy slices are exact)."""
    try:
        value = yaml.safe_load(view.read_text(REGISTRY_FILE))
    except FileNotFoundError:
        if view.files is not None:
            ids = {p.split("/")[1] for p in view.files
                   if p.startswith("repos/") and p.endswith("/_index.md") and len(p.split("/")) == 3}
        else:
            base = view.root / "repos"
            ids = {p.name for p in base.iterdir() if p.is_dir() and REPO_ID.fullmatch(p.name)} if base.is_dir() else set()
    else:
        if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("repos"), dict):
            raise RepoSpecError("snapshot repository registry is malformed")
        ids = set(value["repos"])
    return tuple(binding for key in sorted(ids) if (binding := resolve_snapshot_repo(view, key)) is not None)
