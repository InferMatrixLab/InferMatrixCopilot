"""Portable RFC client; local application and HTTP are loaded only on use."""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from . import RFC_API_VERSION
from .models import Principal, RFCError, SourceRef
from ..contracts.models import SDKError

_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class RFCClientError(SDKError):
    """Stable transport/domain failure without credentials in its message."""

    def __init__(self, message: str, *, code: str = "rfc_error", status: int = 400,
                 request_id: str = ""):
        super().__init__(message)
        self.code = code
        self.status = status
        self.request_id = request_id


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # never forward a bearer credential to another endpoint


class RFCClient:
    """Use one explicit local state directory or a configured RFC service.

    A remote failure is an error; it never initializes unrelated local state.
    Construction and public SDK imports do not load Copilot configuration,
    application services, model providers, or either MCP server.
    """

    def __init__(self, *, state_dir: str | Path | None = None,
                 service_url: str = "", token: str = "",
                 config_path: str | Path | None = None, timeout: float = 30):
        self.service_url = str(service_url).rstrip("/")
        if self.service_url:
            try:
                parsed = parse.urlsplit(self.service_url)
                parsed.port
            except ValueError:
                raise RFCClientError("Invalid RFC service endpoint") from None
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise RFCClientError("RFC service URL must be an HTTP(S) endpoint")
            if parsed.scheme == "http":
                try:
                    loopback = ipaddress.ip_address(parsed.hostname).is_loopback
                except ValueError:
                    loopback = parsed.hostname.lower() == "localhost"
                if not loopback:
                    raise RFCClientError("An external RFC service requires HTTPS", code="insecure_endpoint")
        if timeout <= 0:
            raise RFCClientError("timeout must be positive")
        if not isinstance(token, str) or any(ch.isspace() for ch in token):
            raise RFCClientError("Invalid RFC user token", code="unauthenticated", status=401)
        self.state_dir = Path(state_dir).expanduser() if state_dir else None
        self.config_path = Path(config_path).expanduser() if config_path else None
        self._token = token
        self.timeout = timeout
        self._service = None
        self._version_checked = False

    @classmethod
    def from_env(cls) -> RFCClient:
        return cls(state_dir=os.environ.get("RFC_STATE_DIR"),
                   service_url=os.environ.get("RFC_SERVICE_URL", ""),
                   token=os.environ.get("RFC_TOKEN", ""),
                   config_path=os.environ.get("RFC_CONFIG"))

    def _local(self):
        if self._service is None:
            from .application import build_service

            state = self.state_dir or Path.home() / ".infermatrix-copilot" / "rfc"
            self._service = build_service(state, config_path=self.config_path)
        return self._service

    def _http(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(self.service_url + path, data=body, headers=headers)
        opener = request.build_opener(_NoRedirect())
        try:
            with opener.open(req, timeout=self.timeout) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except error.HTTPError as exc:
            # Only propagate structured application errors, not proxy HTML or
            # redirects that could echo private request headers.
            try:
                value = json.loads(exc.read(65536))
                detail = value.get("error", {})
            except (ValueError, AttributeError, OSError):
                detail = {}
            if not isinstance(detail, dict):
                detail = {}
            raise RFCClientError(str(detail.get("message") or "RFC service request refused"),
                                 code=str(detail.get("code") or "http_error"),
                                 status=exc.code, request_id=str(detail.get("request_id") or "")) from None
        except (error.URLError, TimeoutError, OSError):
            raise RFCClientError("RFC service unavailable", code="transport_error", status=503) from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise RFCClientError("RFC service response exceeds the client limit", code="invalid_response")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise RFCClientError("RFC service returned invalid JSON", code="invalid_response") from None
        if not isinstance(value, dict):
            raise RFCClientError("RFC service response must be an object", code="invalid_response")
        return value

    def capabilities(self) -> dict[str, Any]:
        if not self._token:
            raise RFCClientError("An RFC user token is required", code="unauthenticated", status=401)
        if self.service_url:
            result = self._http("/api/v1/capabilities")
        else:
            try:
                service = self._local()
                result = service.dispatch(service.authenticate(self._token), "capabilities", {})
            except RFCError as exc:
                raise RFCClientError(str(exc), code=exc.code, status=exc.status) from None
        version = result.get("rfc_api_version", result.get("api_version"))
        if not isinstance(version, str) or not version:
            raise RFCClientError("RFC service did not declare its API version", code="invalid_response")
        if version.split(".")[0] != RFC_API_VERSION.split(".")[0]:
            raise RFCClientError("Unsupported RFC service API version", code="incompatible_version")
        self._version_checked = True
        return result

    def dispatch(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(action, str) or not action or not all(
                part and part.replace("_", "").isalnum() for part in action.split(".")):
            raise RFCClientError("Invalid RFC action")
        if payload is not None and not isinstance(payload, dict):
            raise RFCClientError("RFC payload must be an object")
        if action == "capabilities":
            return self.capabilities()
        if not self._version_checked:
            self.capabilities()
        if self.service_url:
            return self._http("/api/v1/actions/" + parse.quote(action, safe="."), payload or {})
        try:
            service = self._local()
            return service.dispatch(service.authenticate(self._token), action, payload or {})
        except RFCError as exc:
            raise RFCClientError(str(exc), code=exc.code, status=exc.status) from None

    def list_rfcs(self, repo_id: str = "", **filters: Any) -> dict[str, Any]:
        """List authorized RFCs, optionally using server-side paging and filters."""
        return self.dispatch("rfcs.list", {**filters, **({"repo_id": repo_id} if repo_id else {})})

    def get(self, rfc_id: str) -> dict[str, Any]:
        return self.dispatch("rfcs.get", {"rfc_id": rfc_id})

    def status(self, rfc_id: str) -> dict[str, Any]:
        return self.dispatch("rfcs.status", {"rfc_id": rfc_id})

    def suggestions(self, rfc_id: str, *, offset: int = 0, limit: int = 50,
                    status: str = "", query: str = "") -> dict[str, Any]:
        payload = {"rfc_id": rfc_id, "offset": offset, "limit": limit}
        if status:
            payload["status"] = status
        if query:
            payload["query"] = query
        return self.dispatch("rfcs.suggestions", payload)

    def draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.dispatch("rfcs.draft", payload)

    def enroll(self, rfc_id: str, **settings: Any) -> dict[str, Any]:
        return self.dispatch("rfcs.enroll", {**settings, "rfc_id": rfc_id})

    def sync(self, rfc_id: str) -> dict[str, Any]:
        return self.dispatch("rfcs.sync", {"rfc_id": rfc_id})

    def publish(self, rfc_id: str, *, content_digest: str, idempotency_key: str,
                post: bool = False, expected_revision: str | None = None,
                path: str | None = None) -> dict[str, Any]:
        payload = {"rfc_id": rfc_id, "content_digest": content_digest,
                   "idempotency_key": idempotency_key, "post": post}
        if expected_revision is not None:
            payload["expected_revision"] = expected_revision
        if path is not None:
            payload["path"] = path
        return self.dispatch("rfcs.publish", payload)

    def decision(self, rfc_id: str, **decision: Any) -> dict[str, Any]:
        return self.dispatch("rfcs.decision", {**decision, "rfc_id": rfc_id})

    def work(self, rfc_id: str, **work: Any) -> dict[str, Any]:
        """Manage stable tasks, including explicit manual implementation marks."""
        return self.dispatch("rfcs.work", {**work, "rfc_id": rfc_id})

    def graph(self, rfc_id: str, **group: Any) -> dict[str, Any]:
        """Merge, rename or unmerge saved view groups without replacing tasks."""
        return self.dispatch("rfcs.graph", {**group, "rfc_id": rfc_id})

    def changes_preview(self, rfc_id: str, *, expected_revision: str,
                        **candidate: Any) -> dict[str, Any]:
        """Freeze a complete manual candidate and its exact source differences."""
        return self.dispatch("rfcs.changes.preview", {**candidate, "rfc_id": rfc_id,
            "expected_revision": expected_revision})

    def changes_apply(self, rfc_id: str, *, preview_id: str, candidate_digest: str,
                      idempotency_key: str, reason: str | None = None) -> dict[str, Any]:
        """Apply only the explicitly reviewed candidate; retries use the same key."""
        return self.dispatch("rfcs.changes.apply", {"rfc_id": rfc_id, "preview_id": preview_id,
            "candidate_digest": candidate_digest, "idempotency_key": idempotency_key,
            **({"reason": reason} if reason is not None else {})})

    def service_status(self) -> dict[str, Any]:
        return self.dispatch("service.status")

    def check_repository(self, repo_id: str) -> dict[str, Any]:
        return self.dispatch("repositories.check", {"repo_id": repo_id})

    def grants(self, **filters: Any) -> dict[str, Any]:
        return self.dispatch("grants.list", filters)

    def operation(self, operation_id: str) -> dict[str, Any]:
        return self.dispatch("operations.get", {"operation_id": operation_id})

    def export(self, rfc_id: str, format: str = "json") -> dict[str, Any]:
        return self.dispatch("rfcs.export", {"rfc_id": rfc_id, "format": format})

    def chat_create(self, rfc_id: str, *, title: str = "") -> dict[str, Any]:
        return self.dispatch("chat.create", {"rfc_id": rfc_id, "title": title})

    def chat_list(self, rfc_id: str, *, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        return self.dispatch("chat.list", {"rfc_id": rfc_id, "offset": offset, "limit": limit})

    def chat_get(self, thread_id: str, *, before: int = 0, limit: int = 50) -> dict[str, Any]:
        return self.dispatch("chat.get", {"thread_id": thread_id, "before": before, "limit": limit})

    def chat_send(self, thread_id: str, message: str, *, idempotency_key: str,
                  language: str = "en", **context: Any) -> dict[str, Any]:
        """Queue a round immediately; read its normalized events and final proposal."""
        return self.dispatch("chat.send", {**context, "thread_id": thread_id,
            "message": message, "language": language, "idempotency_key": idempotency_key})

    def chat_events(self, thread_id: str, *, cursor: int = 0, limit: int = 50) -> dict[str, Any]:
        return self.dispatch("chat.events", {"thread_id": thread_id, "cursor": cursor, "limit": limit})

    def chat_cancel(self, job_id: str) -> dict[str, Any]:
        return self.dispatch("chat.cancel", {"job_id": job_id})

    def chat_retry(self, job_id: str) -> dict[str, Any]:
        return self.dispatch("chat.retry", {"job_id": job_id})

    def chat_preview(self, proposal_id: str) -> dict[str, Any]:
        return self.dispatch("chat.proposals.preview", {"proposal_id": proposal_id})

    def chat_apply(self, proposal_id: str, *, candidate_digest: str,
                   reason: str = "", draft_digest: str | None = None) -> dict[str, Any]:
        payload = {"proposal_id": proposal_id, "candidate_digest": candidate_digest, "reason": reason}
        if draft_digest is not None:
            payload["draft_digest"] = draft_digest
        return self.dispatch("chat.proposals.apply", payload)

    def chat_reject(self, proposal_id: str) -> dict[str, Any]:
        return self.dispatch("chat.proposals.reject", {"proposal_id": proposal_id})

    def chat_delete(self, thread_id: str) -> dict[str, Any]:
        return self.dispatch("chat.delete", {"thread_id": thread_id})
