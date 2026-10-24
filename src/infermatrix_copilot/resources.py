"""Resolve immutable provider resources in source and installed wheels.

This is runtime resource plumbing, not a public API.  ``importlib.resources`` is the
authority for installed artifacts; the source-tree candidate exists only for
editable development checkouts where the forced-included wheel data is not
materialized under ``src/``.
"""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path
from typing import Final

_PACKAGE: Final = "infermatrix_copilot"
_SOURCE_ROOT = Path(__file__).resolve().parents[2]
_MARKERS: Final = {
    "knowledge": "AGENTS.md",
    "playbooks": "pr-review.yaml",
    "adapters": "*/manifest.yaml",
    "skills": "code-quality-review/SKILL.md",
}
def _filesystem_path(value: object) -> Path | None:
    """Return an unpacked-resource path, or ``None`` for a zip traversable."""
    try:
        return Path(os.fspath(value))  # type: ignore[arg-type]
    except TypeError:
        return None


def _has_marker(path: Path, marker: str) -> bool:
    return path.is_dir() and any(item.is_file() for item in path.glob(marker))


def resource_dir(name: str) -> Path:
    """Resolve one checked runtime-data directory, failing closed if absent."""
    if name not in _MARKERS:
        raise KeyError(f"unknown InferMatrixCopilot resource: {name!r}")

    candidates: list[Path] = []
    package_root = resources.files(_PACKAGE)
    for traversable in (
        package_root.joinpath("_runtime", name),
        package_root.joinpath(name),
    ):
        path = _filesystem_path(traversable)
        if path is not None:
            candidates.append(path)

    # Editable installs expose src/infermatrix_copilot through
    # importlib.resources, while the data trees remain at the repository root.
    candidates.append(_SOURCE_ROOT / name)

    marker = _MARKERS[name]
    for candidate in candidates:
        if _has_marker(candidate, marker):
            return candidate.resolve()
    attempted = ", ".join(str(path) for path in candidates)
    raise FileNotFoundError(
        f"InferMatrixCopilot {name} resources are missing ({marker}); "
        f"checked: {attempted}"
    )


def knowledge_root() -> Path:
    return resource_dir("knowledge")


def adapters_root() -> Path:
    return resource_dir("adapters")
