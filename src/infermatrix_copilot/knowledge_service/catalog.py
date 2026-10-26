"""Contained knowledge rule-page discovery and capacity accounting."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from ..contracts.models import (
    InvalidRequestError, KnowledgeCatalogEntry, KnowledgeCurationError,
    RepositoryRef,
)
from .common import (
    _FRONTMATTER, _PAGE_MAX_BYTES, _PAGE_MAX_LINES, _PAGE_TYPE,
    _RULE_PAGE_NAME,
)


def repo_slug(repository: RepositoryRef) -> str:
    alias = repository.alias.strip().replace("_", "-")
    pure = PurePosixPath(alias)
    if (
        not alias
        or pure.is_absolute()
        or len(pure.parts) != 1
        or pure.parts[0] in {".", ".."}
    ):
        raise InvalidRequestError(
            "repository alias must be one knowledge repository slug"
        )
    return alias


def document_path(workspace: Path, document_id: str) -> Path:
    value = str(document_id).strip().replace("\\", "/")
    pure = PurePosixPath(value)
    if not value or pure.is_absolute() or ".." in pure.parts:
        raise KnowledgeCurationError("knowledge document ID is invalid")
    candidate = workspace / pure
    if candidate.is_symlink():
        raise KnowledgeCurationError(
            f"knowledge document is a symlink: {pure.as_posix()}"
        )
    path = candidate.resolve()
    try:
        path.relative_to(workspace)
    except ValueError as exc:
        raise KnowledgeCurationError(
            "knowledge document ID escapes the workspace"
        ) from exc
    if not path.is_file():
        raise KnowledgeCurationError(
            f"knowledge document is missing: {pure.as_posix()}"
        )
    return path


def _is_rule_page(path: Path) -> bool:
    """A rule page is named ``rules.md`` or ``rules-<topic>.md`` AND
    declares ``type: rule``; a topic page of another type (a guide that
    happens to share the prefix) is never a proposal target."""
    if not _RULE_PAGE_NAME.fullmatch(path.name):
        return False
    try:
        head = path.read_text(encoding="utf-8")[:4096]
    except OSError as exc:
        raise KnowledgeCurationError(
            f"knowledge catalog page is unreadable: {path.name}"
        ) from exc
    match = _FRONTMATTER.match(head)
    if match is None:
        return False
    page_type = _PAGE_TYPE.search(match.group("body"))
    return page_type is not None and page_type.group("type") == "rule"


def catalog(
    workspace: Path, repository: RepositoryRef | None = None, *, max_catalog_pages: int
) -> tuple[str, ...]:
    """Return sorted, relative IDs for allowed owner rule pages: each
    owner's ``rules.md`` entry page and its ``rules-<topic>.md`` topic
    pages (``type: rule``)."""
    return tuple(
        entry.document_id
        for entry in catalog_entries(workspace, repository, max_catalog_pages=max_catalog_pages)
    )


def catalog_entries(
    workspace: Path, repository: RepositoryRef | None = None, *, max_catalog_pages: int
) -> tuple[KnowledgeCatalogEntry, ...]:
    """The catalog with each page's remaining capacity."""
    prefixes = ("knowledge/general/", "knowledge/repos/")
    if repository is not None:
        slug = repo_slug(repository)
        prefixes = ("knowledge/general/", f"knowledge/repos/{slug}/")

    entries: list[KnowledgeCatalogEntry] = []
    for path in sorted((workspace / "knowledge").rglob("rules*.md")):
        if path.is_symlink():
            raise KnowledgeCurationError(
                "knowledge catalog contains a symlinked rules page"
            )
        if not _is_rule_page(path):
            continue
        resolved = path.resolve()
        try:
            document_id = resolved.relative_to(workspace).as_posix()
        except ValueError as exc:
            raise KnowledgeCurationError(
                "knowledge catalog contains an escaping rules page"
            ) from exc
        if not any(document_id.startswith(prefix) for prefix in prefixes):
            continue
        data = resolved.read_bytes()
        entries.append(KnowledgeCatalogEntry(
            document_id=document_id,
            size_bytes=len(data),
            free_bytes=max(0, _PAGE_MAX_BYTES - 1 - len(data)),
            free_lines=max(
                0, _PAGE_MAX_LINES - 1 - _non_empty_lines(data)
            ),
        ))
    if len(entries) > max_catalog_pages:
        raise KnowledgeCurationError(
            "knowledge rules catalog exceeds the configured page bound"
        )
    return tuple(entries)


def _non_empty_lines(data: bytes) -> int:
    return sum(
        1 for line in data.decode("utf-8", "replace").splitlines()
        if line.strip()
    )


def _appended_size(section: str, page_data: bytes) -> tuple[int, int]:
    """Bytes and non-empty lines ``apply`` adds for one section: what
    ``_updated_page`` renders, including the newline it first inserts
    when the page does not end with one (the ``updated:`` bump is a
    same-length replacement)."""
    rendered = "\n" + section.rstrip() + "\n"
    normalization = 0 if page_data.endswith((b"\n", b"\r")) else 1
    return (
        len(rendered.encode("utf-8")) + normalization,
        sum(1 for line in rendered.splitlines() if line.strip()),
    )
