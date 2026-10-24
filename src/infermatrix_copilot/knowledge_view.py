"""Per-request view of the knowledge tree: the packaged copy or an activated snapshot.

Knowledge used to be resolved once, at import, from the wheel. The knowledge
service activates a new snapshot by atomically swapping a symlink, and a module
constant captured at import would keep serving the old tree (retired rules
included) until the process restarted. A view is therefore resolved at the START
of each request and every read inside that request goes through it.

Resolution:

* ``KNOWLEDGE_ROOT`` unset -> the packaged tree (``resources``), exactly as
  before. ``snapshot`` is ``"packaged"``.
* ``KNOWLEDGE_ROOT`` set -> ``realpath`` of it, taken ONCE per view, so a swap
  mid-request cannot mix two trees. A directory holding ``MANIFEST.json`` is an
  activated snapshot: its ``knowledge/`` subtree is the root and every read is
  checked against the manifest's sha256 (fail closed on any mismatch). A plain
  directory holding ``AGENTS.md`` is served unverified, for development only.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Mapping

from .resources import knowledge_root

KNOWLEDGE_ROOT_ENV = "KNOWLEDGE_ROOT"
MANIFEST_NAME = "MANIFEST.json"
MANIFEST_SCHEMA_VERSION = 1
# The knowledge tree declares the format it is written in (rule lifecycle
# footers, typed operations, _routes.yaml, _tombstones.yaml). A Copilot only
# activates snapshots in a format it reads. The file sits at the top of
# knowledge/, outside the auto-merge whitelist: changing it takes a person.
FORMAT_FILE = "_format.yaml"
SUPPORTED_FORMATS = frozenset({2})


def knowledge_format(root: Path) -> int | None:
    """The declared format of the knowledge tree at ``root`` (None: undeclared or unreadable)."""
    import yaml

    try:
        data = yaml.safe_load((Path(root) / FORMAT_FILE).read_text(encoding="utf-8"))
        value = data["format_version"]
    except (OSError, TypeError, KeyError, yaml.YAMLError):
        return None
    # exactly an integer: 2.9, "2", true or .inf are malformed, never coerced
    return value if type(value) is int else None


class KnowledgeViewError(RuntimeError):
    """The configured knowledge root is missing, malformed, or tampered with."""


@dataclass(frozen=True)
class KnowledgeView:
    root: Path
    snapshot: str
    files: Mapping[str, str] | None = field(default=None, compare=False)

    @classmethod
    def for_root(cls, root: str | Path) -> "KnowledgeView":
        """Select the canonical pinned view for an explicitly supplied tree."""
        root = Path(root).resolve()
        configured = os.environ.get(KNOWLEDGE_ROOT_ENV, '').strip()
        # A projected sandbox tree must not inherit an unrelated host path.
        if not configured or Path(configured).expanduser().resolve() in (root, root.parent):
            current = cls.current()
            if current.root.resolve() == root:
                return current
        if (root.parent / "MANIFEST.json").is_file():
            view = _load_view(str(root.parent))
            if view.root.resolve() != root:
                raise KnowledgeViewError("snapshot manifest belongs to a different knowledge root")
            return view
        return cls(root, f"unverified:{root}")

    @classmethod
    def current(cls) -> "KnowledgeView":
        configured = os.environ.get(KNOWLEDGE_ROOT_ENV, "").strip()
        if not configured:
            return _packaged_view()
        try:
            resolved = Path(configured).expanduser().resolve(strict=True)
        except OSError as exc:
            raise KnowledgeViewError(
                f"{KNOWLEDGE_ROOT_ENV} does not resolve: {configured}"
            ) from exc
        return _load_view(str(resolved))

    @property
    def verified(self) -> bool:
        return self.files is not None

    @property
    def public_snapshot(self) -> str:
        """The snapshot id safe to record anywhere: the activated snapshot's id,
        ``packaged``, or ``unverified`` (a development tree's path is never exposed)."""
        return "unverified" if self.snapshot.startswith("unverified:") else self.snapshot

    @property
    def tree_sha256(self) -> str:
        """The manifest tree hash of a verified snapshot ("" otherwise)."""
        if self.files is None:
            return ""
        tree = hashlib.sha256()
        for rel in sorted(self.files):
            tree.update(f"{rel}\0{self.files[rel]}\n".encode("utf-8"))
        return tree.hexdigest()

    def relative(self, path: str | Path) -> str:
        """Return the knowledge-relative id of an absolute path inside this view."""
        try:
            return Path(path).resolve().relative_to(self.root).as_posix()
        except ValueError as exc:
            raise KnowledgeViewError(
                f"path is outside the knowledge view: {path}"
            ) from exc

    def path(self, relative_path: str) -> Path:
        """Resolve one knowledge-relative file, refusing escapes and tampering."""
        return self._integrity_path(relative_path, containment=True)

    def _integrity_path(self, relative_path: str, *, containment=False) -> Path:
        """Validate navigation targets without reading their serving content."""
        value = str(relative_path).strip().replace("\\", "/")
        pure = PurePosixPath(value)
        if not value or pure.is_absolute() or ".." in pure.parts:
            raise ValueError(f"knowledge route escapes the knowledge root: {relative_path}")
        path = (self.root / pure).resolve()
        try:
            rel = path.relative_to(self.root).as_posix()
        except ValueError as exc:
            raise ValueError(
                f"knowledge route escapes the knowledge root: {relative_path}"
            ) from exc
        if self.files is not None and rel in self.files and not path.is_file():
            # listed by the manifest but gone: the snapshot is corrupted, which is
            # not the same as an optional file that was never there
            raise KnowledgeViewError(f"snapshot file listed in manifest is missing: {rel}")
        if not path.is_file():
            raise FileNotFoundError(f"knowledge route is missing: {path}")
        if self.files is not None:
            expected = self.files.get(rel)
            if expected is None:
                raise KnowledgeViewError(f"file is not in snapshot manifest: {rel}")
            if _file_sha256(path) != expected:
                raise KnowledgeViewError(f"snapshot file does not match manifest: {rel}")
        if containment:
            from .knowledge_service.containment import assert_page_available
            assert_page_available(self, rel)
        return path

    def read_text(self, relative_path: str) -> str:
        return self.path(relative_path).read_text(encoding="utf-8")


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@lru_cache(maxsize=1)
def _packaged_view() -> KnowledgeView:
    return KnowledgeView(root=knowledge_root(), snapshot="packaged")


@lru_cache(maxsize=16)
def _load_view(resolved: str) -> KnowledgeView:
    """Load a root once per process. Snapshots are immutable, so caching by the
    resolved directory is safe: a new activation is a different directory."""
    base = Path(resolved)
    manifest_path = base / MANIFEST_NAME
    if manifest_path.is_file():
        return _load_snapshot(base, manifest_path)
    if (base / "AGENTS.md").is_file():
        return KnowledgeView(root=base, snapshot=f"unverified:{base}")
    raise KnowledgeViewError(
        f"{KNOWLEDGE_ROOT_ENV} is neither a snapshot (no {MANIFEST_NAME}) "
        f"nor a knowledge tree (no AGENTS.md): {base}"
    )


def _load_snapshot(base: Path, manifest_path: Path) -> KnowledgeView:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise KnowledgeViewError(f"unreadable snapshot manifest: {manifest_path}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise KnowledgeViewError(f"unsupported snapshot manifest: {manifest_path}")
    snapshot = str(manifest.get("snapshot") or "").strip()
    files = manifest.get("files")
    if not snapshot or not isinstance(files, dict) or not files:
        raise KnowledgeViewError(f"snapshot manifest lacks snapshot/files: {manifest_path}")
    root = (base / "knowledge").resolve()
    if not (root / "AGENTS.md").is_file():
        raise KnowledgeViewError(f"snapshot has no knowledge tree: {root}")
    on_disk = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*") if path.is_file()
    }
    if on_disk != set(files):
        extra = sorted(on_disk - set(files))[:3]
        missing = sorted(set(files) - on_disk)[:3]
        raise KnowledgeViewError(
            f"snapshot tree differs from manifest (extra={extra}, missing={missing})"
        )
    tree = hashlib.sha256()
    for rel in sorted(files):
        tree.update(f"{rel}\0{files[rel]}\n".encode("utf-8"))
    if manifest.get("tree_sha256") != tree.hexdigest():
        raise KnowledgeViewError("snapshot manifest tree_sha256 does not match its files")
    return KnowledgeView(
        root=root,
        snapshot=snapshot,
        files=MappingProxyType({str(k): str(v) for k, v in files.items()}),
    )


def build_manifest(root: Path, snapshot: str) -> dict:
    """Manifest for a knowledge tree at ``root`` (the snapshot's ``knowledge/``)."""
    files = {
        path.relative_to(root).as_posix(): _file_sha256(path)
        for path in sorted(root.rglob("*")) if path.is_file()
    }
    tree = hashlib.sha256()
    for rel in sorted(files):
        tree.update(f"{rel}\0{files[rel]}\n".encode("utf-8"))
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "snapshot": snapshot,
        "knowledge_format": knowledge_format(root),
        "files": files,
        "tree_sha256": tree.hexdigest(),
    }
