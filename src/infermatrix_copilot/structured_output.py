"""JSON-object extraction with explicit legacy presentation policies.

Schema meaning and retry decisions belong to callers. This module only
decodes presentation wrappers; it never repairs or invents fields.
"""
from __future__ import annotations

import json
import re


def decode_json_object(text: str, *, policy="first_object", strict=True) -> dict:
    text = text.strip()
    if policy in {"greedy_object", "trailing_object"}:
        # Legacy callers reject multiple objects and malformed leading brace
        # spans; they must not silently adopt first-valid-object recovery.
        pattern = r"\{.*\}" + (r"\s*$" if policy == "trailing_object" else "")
        match = re.search(pattern, text, re.DOTALL)
        if match is None:
            raise ValueError("no JSON object in the reply")
        return json.loads(match.group(0), strict=strict)
    if policy == "first_object":
        fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
        raw = fence.group(1) if fence else text
        try:
            value = json.loads(raw, strict=strict)
        except json.JSONDecodeError as original:
            decoder = json.JSONDecoder(strict=strict)
            for match in re.finditer(r"\{", raw):
                try:
                    value, _ = decoder.raw_decode(raw, match.start())
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    return value
            raise original
        if not isinstance(value, dict):
            raise ValueError("top-level value is not an object")
        return value
    if policy == "first_fence":
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        candidates = ([fence.group(1)] if fence else []) + ([brace.group(0)] if brace else [])
    elif policy == "last_fence":
        candidates = [text] + [match.group(1) for match in re.finditer(
            r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)][::-1]
    else:
        raise ValueError(f"unknown JSON presentation policy {policy!r}")
    for candidate in candidates:
        try:
            value = json.loads(candidate, strict=strict)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("reply is not a JSON object")
