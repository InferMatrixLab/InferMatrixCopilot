"""infermatrix-copilot — conversational CLI (design §3.Y, phases A+B).

REPL + one-shot (-p). NL -> intent -> TaskSpec(s) (echoed; write/push tasks
need confirmation) -> inline plan review for adapted/generated plans ->
planner (reuse > adapt > generate) -> executor. Compound commands ("rebase
pr 12, then review it") become an ordered task queue. Built-ins: /status
/logs /playbooks /resume /quit. Blocked runs exit 3.

This was one 406-line module; it is now a package — `copilot` (the Copilot
orchestrator), `entry` (argparse + REPL wiring, exposes `main`), and `utils`
(pure formatters). The public surface (`Copilot`, `main`) is re-exported so
the `infermatrix_copilot.cli:main` entry point and `from infermatrix_copilot.cli import
Copilot` importers are unchanged.
"""

from __future__ import annotations

from .entry import main

__all__ = ["Copilot", "main"]


def __getattr__(name: str):
    if name == "Copilot":
        from .copilot import Copilot

        return Copilot
    raise AttributeError(name)


if __name__ == "__main__":
    import sys

    sys.exit(main())
