"""Execution environment policies, independent of SDK and provider transports.

Read-only agents retain model authentication but cannot inherit the publishing
host's Git identity. Subscription-provider allowlists remain separate policies;
when both apply, apply the allowlist first, then this credential boundary.
"""
from __future__ import annotations

import os
from collections.abc import Mapping

_CREDENTIAL_KEYS = frozenset({
    "GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
    "GITHUB_PAT", "GITHUB_APP_PRIVATE_KEY", "GITHUB_APP_PRIVATE_KEY_PATH",
    "GITHUB_APP_CLIENT_SECRET", "GIT_ASKPASS", "SSH_ASKPASS", "SSH_AUTH_SOCK",
    "GIT_SSH_COMMAND", "VSCODE_GIT_IPC_HANDLE", "VSCODE_GIT_ASKPASS_NODE",
    "VSCODE_GIT_ASKPASS_EXTRA_ARGS", "VSCODE_GIT_ASKPASS_MAIN",
    "CURSOR_GIT_IPC_HANDLE", "CURSOR_GIT_ASKPASS_NODE",
    "CURSOR_GIT_ASKPASS_EXTRA_ARGS", "CURSOR_GIT_ASKPASS_MAIN",
})


def readonly_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Keep model authentication while removing the publisher's Git identity."""
    env = dict(os.environ if source is None else source)
    for key in tuple(env):
        if key in _CREDENTIAL_KEYS or key.startswith("GIT_CONFIG_"):
            env.pop(key, None)
    env.update(GH_CONFIG_DIR=os.devnull, GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    return env

