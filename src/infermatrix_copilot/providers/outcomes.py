"""Classify transport uncertainty without interpreting a host's retry policy."""
from ..budgeting import native_outcome_unknown
from ..sandbox.base import SandboxPending
from ..process_execution import ProcessCleanupError
from .json_session import JSONSessionError


def requires_reconciliation(error: Exception) -> bool:
    """Stopping locally does not settle a possibly dispatched native call.

    Legacy host timeouts lack dispatch facts, so remain conservative. Explicit
    not-sent facts take precedence over that legacy timeout assumption.
    """
    return (isinstance(error, (SandboxPending, ProcessCleanupError))
            or getattr(error, "process_cleanup_error", None) is not None
            or native_outcome_unknown(error)
            or isinstance(error, JSONSessionError) and error.timeout
            and not hasattr(error, "native_call_sent"))
