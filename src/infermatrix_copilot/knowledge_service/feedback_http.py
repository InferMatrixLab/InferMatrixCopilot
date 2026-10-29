"""Bounded bearer-authenticated HTTP transport for trusted feedback providers."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import socket
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .agreement import AgreementRequiredError
from .feedback import FeedbackError
from .feedback_hub import FeedbackConflict, FeedbackHubStore

MAX_BODY_BYTES = 4 * 1024 * 1024
REQUEST_TIMEOUT = 15


class _HTTPError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("nonfinite JSON number")


def _loopback(host):
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class FeedbackRequestHandler(BaseHTTPRequestHandler):
    server_version = "InferMatrixFeedback/1"
    sys_version = ""

    def setup(self):
        self.request.settimeout(REQUEST_TIMEOUT)
        super().setup()

    def log_message(self, format, *args):
        # Tokens, task identifiers, query strings and evidence are never logged.
        pass

    def _json(self, result, status=200):
        body = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        if status == 401:
            self.send_header("WWW-Authenticate", 'Bearer realm="knowledge-feedback"')
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        # BaseHTTPRequestHandler otherwise interpolates malformed request text.
        self._json({"error": {"code": "invalid_request", "message": "Invalid HTTP request"}}, code)

    def _authenticate(self):
        headers = self.headers.get_all("Authorization", [])
        candidate = ""
        if len(headers) == 1:
            scheme, separator, token = headers[0].partition(" ")
            if scheme.lower() == "bearer" and separator and 32 <= len(token) <= 4096:
                candidate = token
        digest = hashlib.sha256(candidate.encode("utf-8")).digest()
        matched = False
        for expected in self.server.token_digests:
            matched |= hmac.compare_digest(digest, expected)
        if not candidate or not matched:
            raise _HTTPError(401, "unauthenticated", "A valid provider bearer token is required")

    def _path(self):
        if len(self.path) > 4096:
            raise _HTTPError(400, "invalid_request", "Invalid request target")
        parts = urlsplit(self.path)
        if parts.scheme or parts.netloc or parts.fragment:
            raise _HTTPError(400, "invalid_request", "Invalid request target")
        query = parse_qs(parts.query, keep_blank_values=True, max_num_fields=8)
        if any(len(values) != 1 for values in query.values()):
            raise _HTTPError(400, "invalid_request", "Query parameters must not repeat")
        return parts.path, {key: values[0] for key, values in query.items()}

    def _body(self):
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get_all("Transfer-Encoding", []):
            raise _HTTPError(400, "invalid_request", "Transfer encoding is not supported")
        if not lengths:
            raise _HTTPError(411, "length_required", "Content length is required")
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
            raise _HTTPError(400, "invalid_request", "Invalid content length")
        length = int(lengths[0])
        if length > MAX_BODY_BYTES:
            raise _HTTPError(413, "body_too_large", "Feedback event exceeds the transport limit")
        if self.headers.get_content_type() != "application/json":
            raise _HTTPError(415, "unsupported_media_type", "Send an application/json request body")
        try:
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("short body")
            result = json.loads(body.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise _HTTPError(400, "invalid_request", "Invalid JSON request body") from exc
        if not isinstance(result, dict):
            raise _HTTPError(400, "invalid_request", "Feedback event must be a JSON object")
        return result

    def _get(self):
        path, query = self._path()
        if path == "/healthz" and not query:
            self._json({"ok": True})
            return
        self._authenticate()
        if path != "/v1/feedback/report":
            raise _HTTPError(404, "not_found", "Route not found")
        if set(query) - {"repo", "limit"}:
            raise _HTTPError(400, "invalid_request", "Unknown report parameter")
        raw_limit = query.get("limit", "100")
        if not re.fullmatch(r"[0-9]{1,4}", raw_limit):
            raise _HTTPError(400, "invalid_request", "Invalid report limit")
        self._json(self.server.store.report(repository=query.get("repo", ""), limit=int(raw_limit)))

    def _post(self):
        self._authenticate()  # Authenticate before reading or parsing any body.
        path, query = self._path()
        if path != "/v1/feedback/events":
            raise _HTTPError(404, "not_found", "Route not found")
        if query:
            raise _HTTPError(400, "invalid_request", "Event endpoint does not accept query parameters")
        event = self._body()
        if set(event) == {"receipt", "ratings"}:
            raise AgreementRequiredError()
        if set(event) != {"receipt", "ratings", "agreement"}:
            raise _HTTPError(400, "invalid_feedback", "Event requires exactly receipt, ratings and agreement")
        self._json(self.server.store.ingest(receipt=event["receipt"], ratings=event["ratings"],
                                          agreement=event["agreement"]))

    def _run(self, action):
        try:
            action()
        except _HTTPError as exc:
            self._json({"error": {"code": exc.code, "message": exc.message}}, exc.status)
        except AgreementRequiredError:
            self._json({"error": {"code": "agreement_required", "message":
                "A trusted provider must attest current user acceptance before uploading feedback."}}, 428)
        except FeedbackConflict:
            self._json({"error": {"code": "feedback_conflict", "message": "Feedback identity conflicts with an immutable earlier event"}}, 409)
        except (FeedbackError, ValueError, TypeError, RecursionError):
            self._json({"error": {"code": "invalid_feedback", "message": "Invalid feedback metadata or rating"}}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except TimeoutError:
            self._json({"error": {"code": "request_timeout", "message": "Request timed out"}}, 408)
        except (sqlite3.Error, OSError):
            self._json({"error": {"code": "storage_unavailable", "message": "Feedback storage is unavailable"}}, 503)
        except Exception:  # noqa: BLE001 - private provider state must never enter HTTP errors.
            self._json({"error": {"code": "internal_error", "message": "The request could not be completed"}}, 500)

    def do_GET(self):
        self._run(self._get)

    def do_HEAD(self):
        self._run(self._get)

    def do_POST(self):
        self._run(self._post)


def create_server(store: FeedbackHubStore, token: str | tuple[str, ...], host="127.0.0.1", port=0,
                  public_url: str | None = None) -> ThreadingHTTPServer:
    """Create a listener; remote bindings require an explicit HTTPS proxy origin.

    ``public_url`` declares the TLS reverse proxy deployment. This listener
    itself speaks HTTP and must be reachable only by that proxy or loopback.
    Tokens identify trusted provider operators and can attest arbitrary receipts.
    Multiple tokens share one ledger and support credential rotation.
    """
    tokens = (token,) if isinstance(token, str) else token
    if (not isinstance(tokens, tuple) or not tokens
            or any(not isinstance(item, str) or not 32 <= len(item) <= 4096
                   or any(ord(char) < 33 or ord(char) > 126 for char in item) for item in tokens)):
        raise ValueError("Configure a provider token of 32 to 4096 printable nonspace ASCII characters")
    if not isinstance(host, str) or not host or type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Invalid feedback listener address")
    if public_url is not None:
        parts = urlsplit(public_url)
        try:
            _ = parts.port
        except ValueError as exc:
            raise ValueError("Invalid feedback public URL") from exc
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.path not in {"", "/"} or parts.query or parts.fragment):
            raise ValueError("Feedback public URL must be an HTTPS origin")
    elif not _loopback(host):
        raise ValueError("A non-loopback listener requires an HTTPS reverse proxy public URL")

    class FeedbackServer(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 128

        def handle_error(self, request, client_address):
            # The stdlib default prints tracebacks containing request state.
            pass

    if ":" in host:
        class IPv6FeedbackServer(FeedbackServer):
            address_family = socket.AF_INET6

        server_class = IPv6FeedbackServer
    else:
        server_class = FeedbackServer
    server = server_class((host, port), FeedbackRequestHandler)
    server.store = store
    server.token_digests = tuple(hashlib.sha256(item.encode()).digest() for item in tokens)
    return server
