"""Public compatibility exports for the shared RFC application client."""
from ...rfc_service.client import (
    RFC_API_VERSION, RFCClient, RFCClientError, Principal, SourceRef,
)

__all__ = ["RFC_API_VERSION", "RFCClient", "RFCClientError", "Principal", "SourceRef"]
