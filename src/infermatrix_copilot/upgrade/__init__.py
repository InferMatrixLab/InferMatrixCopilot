"""Managed application upgrades; importing the SDK never starts an updater."""

LAUNCHER_VERSION = 1
RUNTIME_SCHEMA = 1
SKILL_PROTOCOL = 1


def __getattr__(name: str):
    if name == "UpdateManager":
        from .manager import UpdateManager

        return UpdateManager
    raise AttributeError(name)


__all__ = ["UpdateManager"]
