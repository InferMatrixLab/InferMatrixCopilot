"""Knowledge-domain model family identity with explicit compatibility policies.

Role configuration historically compares the case-folded prefix before a hyphen.
Served identities additionally accept qualified provider/model and provider:model
names and use punctuation-delimited families. Keep these contracts distinct:
normalizing role names as served identities would change admission decisions.
This module parses identity only; callers own independence, waivers and rejection.
"""
from __future__ import annotations

import re
from typing import Literal


def model_family(model: str, *, policy: Literal["role", "served"] = "role") -> str:
    if policy == "role":
        return model.casefold().split("-")[0]
    if policy == "served":
        return re.split(r"[^a-z0-9]+", str(model).casefold().split("/")[-1].split(":")[-1])[0]
    raise ValueError("unsupported model family identity policy")


def served_model_family(model: str) -> str:
    """Compatibility entry for maintenance's qualified native identities."""
    return model_family(model, policy="served")
