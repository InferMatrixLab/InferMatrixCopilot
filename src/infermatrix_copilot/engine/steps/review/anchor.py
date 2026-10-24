"""Compatibility exports for the canonical review diff domain."""

from ....review.diff import (
    AMBIGUOUS, INVALID, RESOLVED, UNMATCHED,
    normalize_path, FileIndex, diff_index, resolve_snippet, resolve_review_comments, patch_index,
)
