"""Canonical knowledge and hash-bound mirrors in the existing snapshot format."""

from __future__ import annotations

from ..persistence import atomic_write_bytes

import hashlib
import json
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from .git_source import COMMIT_ID
from .repo_spec import REPO_ID, canonical_json, digest


class KnowledgeStoreError(RuntimeError):
    pass


def _pin(value: str) -> str:
    if not COMMIT_ID.fullmatch(value):
        raise KnowledgeStoreError("source/publication identity must be a full SHA1 or SHA256 commit")
    return value


def _path(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    target = (root / pure).resolve()
    if not relative or pure.is_absolute() or ".." in pure.parts or "\\" in relative or not target.is_relative_to(root):
        raise KnowledgeStoreError("knowledge path escapes its store")
    return target


def _hash_files(root: Path) -> dict[str, str]:
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise KnowledgeStoreError("knowledge stores cannot read through symlinks")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def immutable_json(path: Path, value: dict) -> str:
    data = canonical_json(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(data)
    except FileExistsError:
        if path.read_bytes() != data:
            raise KnowledgeStoreError("immutable acceptance artifact already exists with different bytes")
    return hashlib.sha256(data).hexdigest()


def local_acceptance_receipt(*, repo_id: str, source_pin: str, publication_head: str,
                             files: dict[str, str | bytes], checks: dict, accepted: bool,
                             review_receipts: tuple[Path, ...] = ()) -> dict:
    """Bind a genuinely accepted local publication without fabricating a PR.

    The caller runs the existing source/native/format gates. This receipt
    records their evidence; it never creates or rewrites model approvals.
    """
    if not REPO_ID.fullmatch(repo_id) or accepted is not True:
        raise KnowledgeStoreError("local publication needs explicit acceptance and a stable repo_id")
    if not checks or any(value is not True for value in checks.values()):
        raise KnowledgeStoreError("local publication checks must all pass; unknown is not acceptance")
    refs = []
    for path in review_receipts:
        path = Path(path).resolve()
        data = path.read_bytes()
        refs.append({"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    hashes = {}
    for path, value in sorted(files.items()):
        _path(Path("/"), path)
        if path.startswith("/") or ".." in PurePosixPath(path).parts:
            raise KnowledgeStoreError("accepted file path is invalid")
        hashes[path] = hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()
    if not hashes:
        raise KnowledgeStoreError("an empty local publication cannot be accepted")
    return {"schema_version": 1, "kind": "local_knowledge_acceptance", "repo_id": repo_id,
            "accepted": True, "forge": "none", "source_pin": _pin(source_pin),
            "publication_head": _pin(publication_head), "files": hashes,
            "files_sha256": digest(hashes), "checks": checks, "review_receipts": refs,
            "claim": "Local accepted publication; source evidence stays at S0, publication is S1. Not a forged PR or new native approval."}


def validate_local_acceptance(receipt: dict, *, repo_id: str, source_pin: str,
                              publication_head: str, files: dict[str, str | bytes]) -> None:
    if not isinstance(receipt, dict):
        raise KnowledgeStoreError("local acceptance receipt is malformed")
    paths = tuple(Path(row["path"]) for row in receipt.get("review_receipts", []))
    expected = local_acceptance_receipt(repo_id=repo_id, source_pin=source_pin,
                                        publication_head=publication_head, files=files,
                                        checks=receipt.get("checks", {}), accepted=receipt.get("accepted"),
                                        review_receipts=paths)
    if receipt != expected:
        raise KnowledgeStoreError("local acceptance does not bind current files, heads or review artifacts")


class KnowledgeStore:
    """``canonical_root`` is the full knowledge root in the existing format.

    Central: ``<central>/knowledge``. In-repo: ``<repo>/.infermatrix/knowledge``.
    Both contain ``repos/<repo_id>/`` and share unchanged owner/page schemas.
    Read-only mirrors use the normal MANIFEST.json + knowledge/ layout.
    """

    def __init__(self, canonical_root: str | Path, repo_id: str, *, mode: str = "central", readonly: bool = False):
        if not REPO_ID.fullmatch(repo_id) or mode not in ("central", "in_repo"):
            raise KnowledgeStoreError("invalid canonical knowledge store configuration")
        self.root = Path(canonical_root).resolve()
        self.repo_id = repo_id
        self.mode = mode
        self.readonly = readonly or (self.root.parent / "MIRROR.json").exists()

    @property
    def knowledge_slice(self) -> str:
        return f"repos/{self.repo_id}"

    @property
    def acceptance_path(self) -> Path:
        return self.root / f"_publication-{self.repo_id}.json"

    def _accepted_scope(self) -> dict:
        """Bind this repository and shared procedures, not another repo's tier."""
        hashes = _hash_files(self.root)
        files = {p: h for p, h in hashes.items() if p.startswith((self.knowledge_slice + "/", "general/"))
                 or p in ("AGENTS.md", "_format.yaml")}
        registry_path = self.root / "_repositories.yaml"
        registry_entry = None
        if registry_path.is_file():
            import yaml
            registry_entry = (yaml.safe_load(registry_path.read_text()) or {}).get("repos", {}).get(self.repo_id)
        return {"files": files, "registry_entry": registry_entry}

    def _validate_acceptance(self, acceptance: dict, source_pin: str) -> None:
        if not isinstance(acceptance, dict) or acceptance.get("tier") not in ("final", "foundation"):
            raise KnowledgeStoreError("publication acceptance tier must be explicit")
        final = acceptance["tier"] == "final"
        if acceptance.get("init_complete") is not final or acceptance.get("source_pin") != _pin(source_pin):
            raise KnowledgeStoreError("publication acceptance source or completion status differs")
        catalog = acceptance.get("catalog_sha256", "")
        if not isinstance(catalog, str) or len(catalog) != 64 or any(c not in "0123456789abcdef" for c in catalog):
            raise KnowledgeStoreError("publication acceptance needs the exact catalog hash")
        checks = acceptance.get("checks")
        required = ("source", "native", "structural", "semantic_depth", "retrieval") if final else ("source", "native", "structural")
        if not isinstance(checks, dict) or any(checks.get(name) is not True for name in required):
            raise KnowledgeStoreError("publication acceptance is missing required passing gates")

    def record_acceptance(self, *, source_pin: str, publication_head: str, acceptance: dict) -> dict:
        if self.readonly:
            raise KnowledgeStoreError("mirrors cannot record canonical acceptance")
        self._validate_acceptance(acceptance, source_pin)
        catalog = acceptance["catalog_sha256"]
        scope = self._accepted_scope()
        spec = scope["registry_entry"]
        if spec and (spec.get("source_pin") != source_pin or spec.get("catalog_hash") != catalog):
            raise KnowledgeStoreError("publication acceptance differs from the frozen served registry")
        record = {"schema_version": 1, "repo_id": self.repo_id, "source_pin": source_pin,
                  "publication_head": _pin(publication_head), "scope_sha256": digest(scope), "acceptance": acceptance}
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(self.acceptance_path, canonical_json(record))
        return record

    def _bound_acceptance(self, source_pin: str, publication_head: str) -> dict | None:
        if not self.acceptance_path.exists():
            return None
        try:
            record = json.loads(self.acceptance_path.read_text())
        except (ValueError, OSError) as exc:
            raise KnowledgeStoreError("canonical publication acceptance is unreadable") from exc
        if (not isinstance(record, dict) or record.get("schema_version") != 1 or record.get("repo_id") != self.repo_id
                or record.get("source_pin") != source_pin or record.get("publication_head") != publication_head
                or record.get("scope_sha256") != digest(self._accepted_scope())):
            raise KnowledgeStoreError("canonical publication acceptance no longer binds these heads/files")
        acceptance = record.get("acceptance")
        self._validate_acceptance(acceptance, source_pin)
        return acceptance

    def write_files(self, files: dict[str, str | bytes], *, expected_hashes: dict[str, str] | None = None) -> None:
        if self.readonly:
            raise KnowledgeStoreError("mirrors are read-only; publish through the canonical store")
        prepared = {}
        for relative, value in files.items():
            if not relative.startswith(self.knowledge_slice + "/"):
                raise KnowledgeStoreError("canonical publication crosses repository slices")
            target = _path(self.root, relative)
            if expected_hashes is not None:
                expected = expected_hashes.get(relative)
                actual = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else ""
                if expected is None or expected != actual:
                    raise KnowledgeStoreError("canonical file changed since review")
            prepared[target] = value.encode() if isinstance(value, str) else value
        # Validate every destination before writing any member.
        for target, data in prepared.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(target, data)

    def identity(self, *, source_pin: str, publication_head: str) -> dict:
        if not self.root.is_dir():
            raise KnowledgeStoreError("canonical knowledge root is missing")
        files = _hash_files(self.root)
        if not files:
            raise KnowledgeStoreError("canonical knowledge root is empty")
        from ..knowledge_view import SUPPORTED_FORMATS, knowledge_format
        if "AGENTS.md" not in files or knowledge_format(self.root) not in SUPPORTED_FORMATS:
            raise KnowledgeStoreError("canonical knowledge root lacks the supported existing format")
        value = {"repo_id": self.repo_id, "source_pin": _pin(source_pin),
                 "publication_head": _pin(publication_head), "files": files}
        accepted = self._bound_acceptance(source_pin, publication_head)
        if accepted is not None:
            value["acceptance"] = accepted
        return {**value, "snapshot": digest(value)}

    def snapshot(self, destination: str | Path, *, source_pin: str, publication_head: str,
                 acceptance: dict | None = None) -> dict:
        dest = Path(destination).resolve()
        if dest == self.root or dest.is_relative_to(self.root) or self.root.is_relative_to(dest):
            raise KnowledgeStoreError("snapshot cannot overlap its canonical knowledge root")
        if dest.exists():
            raise KnowledgeStoreError("snapshot destination already exists")
        if acceptance is not None:
            self.record_acceptance(source_pin=source_pin, publication_head=publication_head, acceptance=acceptance)
        identity = self.identity(source_pin=source_pin, publication_head=publication_head)
        dest.mkdir(parents=True)
        shutil.copytree(self.root, dest / "knowledge")
        from ..knowledge_view import build_manifest
        manifest = {**build_manifest(dest / "knowledge", identity["snapshot"]),
                    **identity, "knowledge_storage": self.mode}
        immutable_json(dest / "MANIFEST.json", manifest)
        if _hash_files(dest / "knowledge") != identity["files"]:
            raise KnowledgeStoreError("canonical files changed while snapshot was copied")
        return manifest

    @staticmethod
    def verify_mirror(destination: str | Path) -> dict:
        dest = Path(destination).resolve()
        try:
            manifest = json.loads((dest / "MANIFEST.json").read_text())
            marker = json.loads((dest / "MIRROR.json").read_text())
        except (OSError, ValueError) as exc:
            raise KnowledgeStoreError("mirror metadata is missing or unreadable") from exc
        if marker.get("readonly") is not True or marker.get("manifest_sha256") != hashlib.sha256((dest / "MANIFEST.json").read_bytes()).hexdigest():
            raise KnowledgeStoreError("mirror manifest binding is invalid")
        identity = {key: manifest[key] for key in ("repo_id", "source_pin", "publication_head", "files")}
        if "acceptance" in manifest:
            identity["acceptance"] = manifest["acceptance"]
        if "canonical_snapshot" in manifest:
            identity["canonical_snapshot"] = manifest["canonical_snapshot"]
        if manifest.get("schema_version") != 1 or digest(identity) != manifest.get("snapshot"):
            raise KnowledgeStoreError("mirror snapshot identity is invalid")
        if marker.get("repo_id") != manifest["repo_id"] or marker.get("canonical_snapshot") != manifest.get("canonical_snapshot", manifest["snapshot"]):
            raise KnowledgeStoreError("mirror authority does not match its canonical snapshot")
        if _hash_files(dest / "knowledge") != manifest["files"]:
            raise KnowledgeStoreError("mirror differs from its canonical accepted snapshot")
        from ..knowledge_view import build_manifest
        expected = build_manifest(dest / "knowledge", manifest["snapshot"])
        if any(manifest.get(key) != value for key, value in expected.items()):
            raise KnowledgeStoreError("mirror does not preserve the verified knowledge manifest format")
        if {p.name for p in dest.iterdir()} != {"knowledge", "MANIFEST.json", "MIRROR.json"}:
            raise KnowledgeStoreError("mirror contains unbound files")
        return manifest

    def sync_mirror(self, destination: str | Path, *, source_pin: str, publication_head: str,
                    acceptance: dict | None = None) -> dict:
        """Refuse edited/unbound mirrors; only accepted canonical bytes replace one."""
        if self.readonly:
            raise KnowledgeStoreError("a mirror cannot become a publication authority")
        if acceptance is not None and acceptance != self._bound_acceptance(source_pin, publication_head):
            raise KnowledgeStoreError("mirror acceptance must come from the canonical publication")
        dest = Path(destination).resolve()
        if dest.exists():
            old = self.verify_mirror(dest)
            if old["repo_id"] != self.repo_id:
                raise KnowledgeStoreError("mirror belongs to another repository")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="kb-mirror-", dir=dest.parent) as scratch:
            prepared = Path(scratch) / "snapshot"
            origin = self.identity(source_pin=source_pin, publication_head=publication_head)
            copied = prepared / "knowledge"
            copied.mkdir(parents=True)
            for relative in origin["files"]:
                if (relative.startswith((self.knowledge_slice + "/", "general/"))
                        or relative in ("AGENTS.md", "_format.yaml", self.acceptance_path.name)):
                    target = _path(copied, relative)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(self.root / relative, target)
            registry = self._accepted_scope()["registry_entry"]
            if registry is not None:
                (copied / "_repositories.yaml").write_bytes(canonical_json({"schema_version": 1, "repos": {self.repo_id: registry}}))
            navigation = (f'---\ntitle: "Repositories"\ncreated: 2026-10-06\nupdated: 2026-10-06\ntype: index\ntags: [general]\nsources: []\n---\n\n# Repositories\n\n- [{self.repo_id}]({self.repo_id}/_index.md)\n')
            (copied / "repos").mkdir(exist_ok=True)
            (copied / "repos/_index.md").write_text(navigation)
            # The public subset has its own identity. The full origin remains
            # hash-bound without disclosing other repository entries/content.
            subset = {"repo_id": self.repo_id, "source_pin": source_pin, "publication_head": publication_head,
                      "files": _hash_files(copied), "canonical_snapshot": origin["snapshot"]}
            if "acceptance" in origin:
                subset["acceptance"] = origin["acceptance"]
            from ..knowledge_view import build_manifest
            manifest = {**build_manifest(copied, digest(subset)), **subset,
                        "knowledge_storage": self.mode}
            immutable_json(prepared / "MANIFEST.json", manifest)
            if self.identity(source_pin=source_pin, publication_head=publication_head) != origin:
                raise KnowledgeStoreError("canonical files changed while mirror was prepared")
            marker = {"schema_version": 1, "repo_id": self.repo_id, "readonly": True,
                      "canonical_snapshot": origin["snapshot"],
                      "manifest_sha256": hashlib.sha256((prepared / "MANIFEST.json").read_bytes()).hexdigest()}
            immutable_json(prepared / "MIRROR.json", marker)
            self.verify_mirror(prepared)
            if dest.exists():
                # Check again immediately before replacement to catch edits
                # during preparation. The previous accepted mirror is retained
                # until the prepared copy has passed its full hash audit.
                self.verify_mirror(dest)
                backup = Path(scratch) / "previous"
                dest.rename(backup)
                try:
                    prepared.rename(dest)
                except OSError:
                    backup.rename(dest)
                    raise
            else:
                prepared.rename(dest)
        return manifest
