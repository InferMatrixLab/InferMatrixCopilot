"""Campaign-only cross-process pacing for native Zcode subscription calls."""

from __future__ import annotations

from ..persistence import atomic_write_bytes

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import time
import uuid


class PacingStopped(RuntimeError):
    pass


@dataclass
class PacingTicket:
    id: str
    started_at: float
    rate_limited: bool = False


def is_native_rate_limit(event):
    """Only native errors/status diagnostics, never generated source prose."""
    if event.get("type") == "native.stderr":
        text = str(event.get("text", ""))
        return bool(re.search(r"\b(?:HTTP\s+)?Status(?:Code)?[\"']?\s*[:=]\s*429\b", text, re.I)
                    or "[1302][您的账户已达到速率限制" in text
                    or re.search(r"\btoo many requests\b|\brate limit exceeded\b", text, re.I))
    if event.get("type") not in {"turn.failed", "error", "request.failed"}:
        return False

    def failed(value):
        if isinstance(value, dict):
            return any((key.lower() in {"status", "statuscode", "status_code"} and str(item) == "429")
                       or (key.lower() in {"error", "message", "code"} and isinstance(item, str)
                           and ("[1302]" in item or "rate limit" in item.lower() or "too many requests" in item.lower()))
                       or isinstance(item, (dict, list)) and failed(item) for key, item in value.items())
        return isinstance(value, list) and any(failed(item) for item in value)

    return failed(event)


class SharedZcodePacer:
    """Reserve starts under flock; release it before stop-aware waiting.

    No call is retried here. Native 429s extend a shared cooldown and double
    future start spacing (maximum 60s). Four unthrottled completed calls permit
    one 5s recovery step, never earlier than an already reserved start.
    """

    def __init__(self, path: Path, *, start_interval=15.0, rate_cooldown=90.0,
                 max_interval=60.0, stop_file=None, clock=time.time, sleep=time.sleep):
        values = (start_interval, rate_cooldown, max_interval)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or
               not math.isfinite(value) or value <= 0 for value in values) or max_interval < start_interval:
            raise ValueError("Zcode pacing needs finite positive timing and maximum >= initial interval")
        self.path, self.stop_file = Path(path), Path(stop_file) if stop_file else None
        self.clock, self.sleep = clock, sleep
        self.config = {"start_interval_s": float(start_interval), "rate_cooldown_s": float(rate_cooldown),
                       "max_interval_s": float(max_interval)}

    @contextmanager
    def _locked(self):
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_name(self.path.name + ".lock").open("a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _load(self):
        if not self.path.exists():
            return {"schema": "zcode-pacing/1", "config": self.config, "interval_s": self.config["start_interval_s"],
                    "next_start": 0.0, "cooldown_until": 0.0, "last_rate_limit": 0.0,
                    "starts": 0, "rate_limited_calls": 0, "successful_calls": 0, "recovery_successes": 0}
        data = json.loads(self.path.read_bytes())
        if data.get("schema") != "zcode-pacing/1" or data.get("config") != self.config:
            raise ValueError("shared Zcode pacing identity/configuration differs")
        for name in ("interval_s", "next_start", "cooldown_until", "last_rate_limit"):
            if isinstance(data.get(name), bool) or not isinstance(data.get(name), (int, float)) or not math.isfinite(data[name]) or data[name] < 0:
                raise ValueError("invalid shared Zcode pacing timestamp")
        if not self.config["start_interval_s"] <= data["interval_s"] <= self.config["max_interval_s"]:
            raise ValueError("invalid shared Zcode pacing interval")
        return data

    def _save(self, data):
        atomic_write_bytes(self.path, json.dumps(data, sort_keys=True).encode("utf-8"))

    def prepare(self):
        with self._locked():
            self._save(self._load())

    def acquire(self, sink):
        last_wait = None
        while True:
            if self.stop_file and self.stop_file.exists():
                sink({"type": "native.throttle.stopped", "payload": {"at": self.clock()}})
                raise PacingStopped("campaign stop requested before Zcode dispatch")
            with self._locked():
                data = self._load()
                now = self.clock()
                target = max(data["next_start"], data["cooldown_until"])
                if now >= target:
                    ticket = PacingTicket(uuid.uuid4().hex, now)
                    data["next_start"] = now + data["interval_s"]
                    data["starts"] += 1
                    self._save(data)
                    event = {"type": "native.throttle.started", "payload": {"ticket": ticket.id, "at": now,
                             "interval_s": data["interval_s"], "cooldown_until": data["cooldown_until"]}}
                else:
                    event, ticket = None, None
            if ticket is not None:
                sink(event)
                return ticket
            if target != last_wait:
                sink({"type": "native.throttle.wait", "payload": {"at": now, "until": target,
                      "seconds": target - now, "interval_s": data["interval_s"]}})
                last_wait = target
            self.sleep(min(1.0, max(0.001, target - now)))

    def observe(self, ticket, event, sink):
        if not is_native_rate_limit(event):
            return
        with self._locked():
            data = self._load()
            now = self.clock()
            if not ticket.rate_limited:
                data["interval_s"] = min(self.config["max_interval_s"], data["interval_s"] * 2)
                data["rate_limited_calls"] += 1
                data["recovery_successes"] = 0
                ticket.rate_limited = True
            data["last_rate_limit"] = now
            data["cooldown_until"] = max(data["cooldown_until"], now + self.config["rate_cooldown_s"])
            data["next_start"] = max(data["next_start"], data["cooldown_until"])
            self._save(data)
        sink({"type": "native.throttle.cooldown", "payload": {"ticket": ticket.id, "at": now,
              "until": data["cooldown_until"], "interval_s": data["interval_s"],
              "cause": "native_429_or_account_1302", "event_sha256": hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()}})

    def finish(self, ticket, *, success, sink):
        if not success or ticket.rate_limited:
            return
        with self._locked():
            data = self._load()
            data["successful_calls"] += 1
            # Calls already running before a newer 429 cannot undo its slowdown.
            if ticket.started_at >= data["last_rate_limit"] and self.clock() >= data["cooldown_until"]:
                data["recovery_successes"] += 1
                if data["recovery_successes"] >= 4:
                    data["interval_s"] = max(self.config["start_interval_s"], data["interval_s"] - 5)
                    data["recovery_successes"] = 0
            self._save(data)
        sink({"type": "native.throttle.finished", "payload": {"ticket": ticket.id,
              "at": self.clock(), "interval_s": data["interval_s"], "success": True}})
