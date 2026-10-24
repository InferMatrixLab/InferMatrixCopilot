"""Review domain; implementations load only when their capability is requested."""
from importlib import import_module

_EXPORTS = {
    "DiffSummary": "diff_summary", "build_diff_summary": "diff_summary",
    "evaluate_triggers": "triggers", "ReviewVerdict": "reviewer", "run_patch_review": "reviewer",
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    value = getattr(import_module("." + _EXPORTS[name], __name__), name)
    globals()[name] = value
    return value
