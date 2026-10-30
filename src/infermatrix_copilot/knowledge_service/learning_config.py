"""Read learning options with the same .env precedence as Copilot settings."""

from __future__ import annotations

import os
from pathlib import Path

_OPTIONS = frozenset({
    "KNOWLEDGE_LEARNING_STATE_DIR", "KNOWLEDGE_LEARNING_WORKSPACE",
    "KNOWLEDGE_LEARNING_REPOSITORY", "KNOWLEDGE_LEARNING_BASE",
    "KNOWLEDGE_LEARNING_PUBLICATION", "ALLOW_PUSH", "ALLOW_POST",
    "KNOWLEDGE_FEEDBACK_URL", "KNOWLEDGE_FEEDBACK_TOKEN",
    "KNOWLEDGE_FEEDBACK_TIMEOUT_SECONDS",
})


def learning_environment() -> dict[str, str]:
    """Load only learning options; never export or return model credentials.

    Explicit environment values override source, user and cwd dotenv files.
    Constructing this mapping does not instantiate a model or mutate os.environ.
    """
    from dotenv import dotenv_values

    from ..config import Settings

    files = Settings.model_config.get("env_file") or ()
    if isinstance(files, (str, Path)):
        files = (files,)
    result = {}
    for path in files:
        for key, value in dotenv_values(path, interpolate=False).items():
            if key in _OPTIONS and value is not None:
                result[key] = value
    result.update({key: os.environ[key] for key in _OPTIONS if key in os.environ})
    return result
