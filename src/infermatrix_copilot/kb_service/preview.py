"""Canonical checked preview reads for init cache, chaining and local review."""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat

from .init_support import InitError, load_prepared


def file_hashes(files):
    """Hash logical UTF-8 text with universal newlines; None marks deletion."""
    if (not isinstance(files, Mapping) or any(not isinstance(name, str) or
            (text is not None and not isinstance(text, str)) for name, text in files.items())):
        raise InitError("preview files require text paths and UTF-8 content or deletions")
    try:
        hashes = {name: hashlib.sha256(text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")).hexdigest() if text is not None else None
                  for name, text in sorted(files.items())}
    except UnicodeError as exc:
        raise InitError("preview files require valid UTF-8 content") from exc
    _physical_paths(hashes)
    return hashes


def _physical_paths(expected):
    if not isinstance(expected, dict):
        raise InitError("preview checked file receipt is malformed")
    paths = {}
    for name, digest in expected.items():
        if (not isinstance(name, str) or not name or not PurePosixPath(name).parts or str(PurePosixPath(name)) != name
                or PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts
                or any(c in name for c in "\\\0\r\n")):
            raise InitError("preview receipt has an unsafe file path")
        if digest is not None and (not isinstance(digest, str) or len(digest) != 64
                                   or any(c not in "0123456789abcdef" for c in digest)):
            raise InitError("preview checked file receipt is malformed")
        physical = name + ".DELETED" if digest is None else name
        if physical in paths:
            raise InitError("preview receipt has colliding deletion paths")
        paths[physical] = name
    return paths


def _read_text(path, root):
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise InitError("preview contains a link or escaped file")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise InitError("preview contains a non-regular file")
            # Preserve the original read_text universal-newline contract on
            # legacy Windows previews; returned/published text is what is hashed.
            return stream.read().decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except (OSError, UnicodeError) as exc:
        raise InitError("preview file cannot be read as regular UTF-8 text") from exc


def _legacy_discovery_hashes(record):
    """Translate persisted old catalog/report hashes, never current disk bytes."""
    if record.stage != "feature-discovery":
        return None
    catalogs = [path for path in record.files if isinstance(path, str)
                and path.endswith("/knowledge-coverage.yaml")]
    report = record.discovery.get("report_path")
    if len(catalogs) != 1 or not isinstance(report, str) or report == catalogs[0]:
        return None
    if not record.discovery.get("catalog_sha256") or not record.discovery.get("report_sha256"):
        return None
    return {catalogs[0]: record.discovery["catalog_sha256"], report: record.discovery["report_sha256"]}


def read_preview(record, *, require_checked=False, owner_review=False):
    """Return an immutable text/deletion mapping, or refuse without repairing it.

    Older nonportable records can bind to their original prepared publication.
    Missing legacy proof refuses: never bless current bytes as the old approval.
    owner_review returns untrusted human edits for a new draft or calibration;
    it never produces or updates a checked receipt. Publication stays checked.
    """
    if owner_review and require_checked:
        raise InitError("owner review input cannot be treated as a checked preview")
    folder = Path(str(record.pr.get("dry_run_dir") or ""))
    root = folder / "tree"
    if not record.pr.get("dry_run_dir") or folder.is_symlink() or root.is_symlink() or not root.is_dir():
        raise InitError("preview snapshot is missing or is a link")
    if owner_review and "checked_files_sha256" not in record.pr:
        expected = {}  # Untrusted input needs no invented historical approval.
    elif "checked_files_sha256" in record.pr:
        expected = record.pr["checked_files_sha256"]
    else:
        if require_checked:
            raise InitError("preview files changed after source and independent review checks: no checked receipt")
        expected = _legacy_discovery_hashes(record)
        if expected is None:
            prepared_path = folder.with_name(record.stage + "-publish.json")
            if prepared_path.is_symlink():
                raise InitError("legacy preview receipt is a link")
            prepared = load_prepared(prepared_path)
            if prepared["base_sha"] != record.kb_base_sha or prepared["when"] != record.started_at:
                raise InitError("legacy preview receipt belongs to another stage input")
            expected = file_hashes(prepared["files"])
    physical = _physical_paths(expected)
    if not owner_review and sorted(expected) != sorted(record.files):
        raise InitError("preview file set differs from the checked stage")
    actual = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise InitError("preview contains a link or escaped file")
        if path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        if relative not in physical and not owner_review:
            raise InitError("preview file set differs from the checked stage")
        name = physical.get(relative, relative)
        text = _read_text(path, root)
        if owner_review and relative not in physical and relative.endswith(".DELETED"):
            continue  # Original unreceipted dry-run projection omits deletion markers.
        if name in expected and expected[name] is None:
            if text:
                raise InitError("preview deletion marker changed after checks")
            actual[name] = None
        else:
            actual[name] = text
    actual_hashes = file_hashes(actual)
    if not owner_review and actual_hashes != expected:
        raise InitError("preview files changed after source and independent review checks")
    return actual
