"""Bounded, authenticated transport for the knowledge feedback collector.

Credentials are operator configuration, never part of receipts or the outbox.
The caller commits local feedback before using this transport.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import socket
import time
from dataclasses import dataclass, field
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .agreement import AgreementRequiredError, AgreementStore

MAX_MESSAGE_BYTES = 4 * 1024 * 1024


class FeedbackSyncError(ValueError):
    """A deliberately sanitized transport or configuration failure."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


@dataclass(frozen=True)
class FeedbackSyncConfig:
    url: str
    token: str = field(repr=False)
    timeout_seconds: float = 5.0

    def __post_init__(self):
        try:
            parts = urlsplit(self.url)
            if (parts.scheme not in {"https", "http"} or not parts.hostname
                    or parts.username is not None or parts.password is not None
                    or parts.query or parts.fragment or "?" in self.url or "#" in self.url
                    or any(char.isspace() for char in self.url) or "\\" in self.url
                    or "%" in parts.hostname or parts.port == 0
                    or (parts.scheme == "http" and not _loopback(parts.hostname))):
                raise ValueError
            if (not isinstance(self.token, str) or not 32 <= len(self.token) <= 4096
                    or any(not 33 <= ord(char) <= 126 for char in self.token)):
                raise ValueError
            if (isinstance(self.timeout_seconds, bool)
                    or not math.isfinite(float(self.timeout_seconds))
                    or not 0.1 <= float(self.timeout_seconds) <= 30):
                raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise FeedbackSyncError("invalid knowledge feedback collector configuration") from exc
        object.__setattr__(self, "url", self.url.rstrip("/"))
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))

    @classmethod
    def from_environment(cls, environ: dict[str, str]):
        url = environ.get("KNOWLEDGE_FEEDBACK_URL", "").strip()
        token = environ.get("KNOWLEDGE_FEEDBACK_TOKEN", "")
        if not url and not token:
            return None
        return cls(url, token, environ.get("KNOWLEDGE_FEEDBACK_TIMEOUT_SECONDS", "5"))


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _encoded(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


class FeedbackSyncClient:
    def __init__(self, config: FeedbackSyncConfig, *, state_dir=None):
        self.config = config
        self.state_dir = state_dir

    def _agreement(self):
        return AgreementStore.from_env(state_dir=self.state_dir, feedback_url=self.config.url)

    def _request(self, path: str, payload=None) -> dict:
        agreement = self._agreement()
        agreement.require("feedback_sync" if payload is not None else "feedback_report")
        if payload is not None:
            from .learning_privacy import LearningPrivacyError, validate_distilled_text

            payload = {**payload, "agreement": agreement.attestation()}
            try:
                validate_distilled_text(payload)
            except LearningPrivacyError:
                raise FeedbackSyncError("privacy_blocked") from None
        config = self.config
        try:
            parts = urlsplit(config.url)
            # Do not permit an HTTP localhost alias to resolve off the machine.
            if parts.scheme == "http" and parts.hostname == "localhost":
                addresses = socket.getaddrinfo("localhost", parts.port or 80,
                                               type=socket.SOCK_STREAM)
                if not addresses or any(not ipaddress.ip_address(row[4][0]).is_loopback
                                        for row in addresses):
                    raise ValueError
            data = None if payload is None else _encoded(payload)
            if data is not None and len(data) > MAX_MESSAGE_BYTES:
                raise ValueError
            destination_url = config.url
            if parts.scheme == "http" and parts.hostname == "localhost":
                # Connect to the already checked literal address, avoiding a
                # second DNS resolution between validation and connection.
                address = addresses[0][4][0]
                host = "[" + address + "]" if ":" in address else address
                authority = host + (":" + str(parts.port) if parts.port else "")
                destination_url = parts._replace(netloc=authority).geturl()
            request = Request(destination_url + path, data=data,
                              headers={"Authorization": "Bearer " + config.token,
                                       "Accept": "application/json",
                                       "Content-Type": "application/json"},
                              method="GET" if data is None else "POST")
            # Disallow environment proxy routing and all redirects, so the token
            # is delivered only to the configured origin. HTTPS uses normal
            # stdlib certificate and hostname verification.
            opener = build_opener(ProxyHandler({}), _NoRedirect())
            deadline = time.monotonic() + config.timeout_seconds
            with opener.open(request, timeout=config.timeout_seconds) as response:
                if response.status != 200:
                    raise ValueError
                content_type = response.headers.get_content_type()
                if content_type != "application/json":
                    raise ValueError
                length = response.headers.get("Content-Length")
                if length is not None and (int(length) < 0 or int(length) > MAX_MESSAGE_BYTES):
                    raise ValueError
                chunks, size = [], 0
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    chunk = response.read1(min(65536, MAX_MESSAGE_BYTES + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_MESSAGE_BYTES:
                        raise ValueError
                    chunks.append(chunk)
            result = json.loads(b"".join(chunks).decode("utf-8"),
                                object_pairs_hook=_no_duplicate_keys,
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(result, dict):
                raise TypeError
            return result
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status == 409:
                raise FeedbackSyncError("feedback_conflict") from None
            if status in (401, 403):
                raise FeedbackSyncError("authentication_failed") from None
            if status == 428:
                raise AgreementRequiredError() from None
            raise FeedbackSyncError("knowledge feedback collector request failed") from None
        except Exception:  # noqa: BLE001 - transport errors must never echo private endpoint data
            # URLs, response bodies and exception details may contain secrets.
            raise FeedbackSyncError("knowledge feedback collector request failed") from None

    def send(self, *, receipt: dict, rating: dict, target: dict, feedback_id: str) -> None:
        from .learning_privacy import LearningPrivacyError, validate_distilled_text

        agreement = self._agreement()
        agreement.require("feedback_sync")
        event = {"receipt": receipt, "ratings": [rating], "agreement": agreement.attestation()}
        try:
            validate_distilled_text(event)
        except LearningPrivacyError:
            raise FeedbackSyncError("privacy_blocked") from None
        actual = self._request("/v1/feedback/events", event)
        expected = {"task_id": receipt["task_id"], "delivery_id": receipt["delivery_id"],
                    "ratings": [{**rating, "feedback_id": feedback_id, "target": target}]}
        try:
            # A global duplicate can retain the first provider's page/snapshot
            # provenance. All identity/version fields and the canonical vote
            # must still agree exactly; provenance need not match that provider.
            actual_ratings = actual["ratings"]
            if len(actual_ratings) != 1 or not isinstance(actual_ratings[0], dict):
                raise ValueError
            actual_target = actual_ratings[0]["target"]
            version_fields = ("target_id", "repository", "path", "kind", "content_sha256",
                              "unit_id", "block_id", "upstream_pins")
            wanted_target = {key: target[key] for key in version_fields if key in target}
            returned_target = {key: actual_target[key] for key in version_fields if key in actual_target}
            normalized = {"task_id": actual["task_id"], "delivery_id": actual["delivery_id"],
                          "ratings": [{**actual_ratings[0], "target": returned_target}]}
            expected["ratings"][0]["target"] = wanted_target
            matches = _encoded(normalized) == _encoded(expected)
        except (TypeError, ValueError, KeyError):
            matches = False
        if not matches:
            raise FeedbackSyncError("knowledge feedback collector acknowledgement did not match")

    def report(self, *, repository: str, limit: int) -> dict:
        result = self._request("/v1/feedback/report?" + urlencode({"repo": repository, "limit": limit}))
        # Never treat an error or arbitrary response as an empty team report.
        if (result.get("protocol") != "knowledge-feedback/1"
                or result.get("repository") != repository
                or type(result.get("total_targets")) is not int or result["total_targets"] < 0
                or type(result.get("total_ratings")) is not int or result["total_ratings"] < 0
                or not isinstance(result.get("targets"), list) or len(result["targets"]) > limit
                or type(result.get("truncated")) is not bool):
            raise FeedbackSyncError("knowledge feedback collector report is invalid")
        try:
            targets = result["targets"]
            total_targets, total_ratings = result["total_targets"], result["total_ratings"]
            if (len(targets) != min(total_targets, limit)
                    or result["truncated"] != (total_targets > limit)):
                raise ValueError
            seen, counted = set(), 0
            for target in targets:
                if not isinstance(target, dict):
                    raise TypeError
                for key in ("repository", "path", "content_sha256", "target_id"):
                    if not isinstance(target.get(key), str) or not target[key]:
                        raise ValueError
                if (repository and target["repository"] != repository
                        or target["target_id"] in seen
                        or not re.fullmatch(r"[0-9a-f]{64}", target["content_sha256"])
                        or target.get("kind") not in {"rule", "document"}):
                    raise ValueError
                version = {key: target[key] for key in ("repository", "path", "kind", "content_sha256")}
                if target["kind"] == "rule":
                    for key in ("unit_id", "block_id"):
                        if not isinstance(target.get(key), str) or not target[key]:
                            raise ValueError
                        version[key] = target[key]
                    pins = target["upstream_pins"]
                    if not isinstance(pins, list) or any(not isinstance(pin, str) for pin in pins) or pins != sorted(pins):
                        raise ValueError
                    version["upstream_pins"] = pins
                if hashlib.sha256(_encoded(version)).hexdigest() != target["target_id"]:
                    raise ValueError
                seen.add(target["target_id"])
                for key in ("positive_count", "negative_count", "rating_count", "distinct_tasks"):
                    if type(target.get(key)) is not int or target[key] < 0:
                        raise ValueError
                positive, negative, count = target["positive_count"], target["negative_count"], target["rating_count"]
                if (count <= 0 or count != positive + negative or target["distinct_tasks"] != count
                        or type(target.get("net_score")) is not int or target["net_score"] != positive - negative
                        or type(target.get("mean_score")) not in (int, float)
                        or not math.isfinite(target["mean_score"])
                        or not math.isclose(target["mean_score"], (positive - negative) / count,
                                            rel_tol=1e-12, abs_tol=1e-12)):
                    raise ValueError
                counted += count
            if counted > total_ratings or (not result["truncated"] and counted != total_ratings):
                raise ValueError
        except (ValueError, TypeError, KeyError, OverflowError):
            raise FeedbackSyncError("knowledge feedback collector report is invalid") from None
        return result
