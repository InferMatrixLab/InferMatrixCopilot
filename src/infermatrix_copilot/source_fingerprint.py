"""Canonical content fingerprint for the executable source tree.

The ordered roots and serialization preserve existing artifact identities.
Callers may supply their public error type without importing domain policy here.
"""
import hashlib
import json
from pathlib import Path


def source_tree_hash(root: Path, *, error_type=ValueError) -> str:
    return _tree_hash(root, ("src", "playbooks", "adapters", "skills"), error_type)


def package_tree_hash(root: Path, *, error_type=ValueError) -> str:
    """Fingerprint installed code and bundled resources independently of location."""
    return _tree_hash(root, ("",), error_type)


def _tree_hash(root: Path, prefixes: tuple[str, ...], error_type) -> str:
    files = []
    for prefix in prefixes:
        for path in sorted((root / prefix).rglob("*")):
            if path.is_symlink():
                raise error_type(f"symlink in artifact: {path}")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                files.append((path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
    return hashlib.sha256(json.dumps(files, separators=(",", ":")).encode()).hexdigest()
