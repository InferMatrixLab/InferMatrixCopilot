"""Stable host entrypoint. Application processes always use a physical release."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .manager import UpdateManager
from .storage import UpgradeError, atomic_json, home_path, read_json


def _background(manager: UpdateManager) -> None:
    try:
        _spawn_background(manager)
    except (OSError, UpgradeError):
        # Update bookkeeping and network access are optional for app startup.
        return


def _spawn_background(manager: UpdateManager) -> None:
    if not manager.due():
        return
    launcher = manager.root / "launcher"
    from .manager import python_path

    interpreter = python_path(launcher / "venv")
    entry = launcher / "entry.py"
    if not interpreter.is_file() or not entry.is_file():
        return
    log = manager.root / "updates.log"
    if log.exists() and log.stat().st_size > 1024 * 1024:
        log.write_bytes(b"")
    try:
        with log.open("ab") as output:
            kwargs = {"start_new_session": True} if os.name != "nt" else {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
            subprocess.Popen([str(interpreter), str(entry), "--home", str(manager.root), "_update"],
                             stdin=subprocess.DEVNULL, stdout=output, stderr=output, **kwargs)
    except OSError:
        # An optional background check cannot prevent a working app from starting.
        return


def _application(manager: UpdateManager, mode: str, argv: list[str]) -> int:
    active = manager.active()
    if not active:
        from .bootstrap import install_distribution

        install_distribution(manager.root)
        active = manager.active()
    application = active.get("launcher_application")
    if application:
        from .manager import python_path

        target = manager.root / "launcher" / application
        if (Path(application).name != application or not application.startswith("updater-")
                or not application.endswith(".pyz") or not target.is_file()):
            raise UpgradeError("active recovery launcher is missing; rerun the installer")
        if Path(sys.argv[0]).absolute() != target.absolute():
            interpreter = python_path(manager.root / "launcher/venv")
            # The entry's snapshot may have changed before exec, or a portable
            # plugin may have entered through uvx. Select the authoritative
            # recovery environment before reading any task input.
            os.execv(str(interpreter), [str(interpreter), "-I", str(target),
                                       "--home", str(manager.root), mode, *argv])
    identity = active["release_id"]
    try:
        metadata = manager.installed(identity)
        # Probe before handing any user input to a candidate. Once the user
        # process starts, its exit code is never treated as a replay signal.
        manager.health(metadata)
    except UpgradeError:
        manager.rollback(failed=identity)
        print("Copilot restored the previous healthy release.", file=sys.stderr)
        # Re-select recovery code too, including when another launch already
        # rolled back or superseded this candidate. No task input was consumed.
        return _application(manager, mode, argv)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env.update(INFERMATRIX_COPILOT_HOME=str(manager.root), COPILOT_RELEASE_ID=identity,
               COPILOT_RELEASE_ROOT=str(manager.root / "releases" / identity),
               COPILOT_RUNTIME_PYTHON=metadata["python"])
    authoring = manager.policy().get("authoring_root")
    if authoring:
        env["COPILOT_AUTHORING_ROOT"] = authoring
        source_config = Path(authoring) / ".env"
        if mode == "cli" and source_config.is_file():
            env.setdefault("COPILOT_SOURCE_CONFIG", str(source_config))
    module = {"mcp": "infermatrix_copilot.thin_mcp_server",
              "workflow-mcp": "infermatrix_copilot.mcp_server",
              "cli": "infermatrix_copilot.cli"}[mode]
    lease = manager.root / "leases" / f"{uuid.uuid4().hex}.json"
    atomic_json(lease, {"pid": os.getpid(), "release_id": identity, "started": time.time()})
    child = None
    notified = ""
    previous_handlers = {}
    try:
        _background(manager)
        child = subprocess.Popen([metadata["python"], "-m", module, *argv], env=env)
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[sig] = signal.getsignal(sig)

            def forward(signum, frame):
                if os.name == "nt" and signum == signal.SIGINT:
                    # Console Ctrl-C already reaches the inherited child group.
                    return
                if child.poll() is None:
                    child.send_signal(signum)

            signal.signal(sig, forward)
        while True:
            try:
                return child.wait(timeout=60)
            except subprocess.TimeoutExpired:
                _background(manager)
                try:
                    latest = manager.active()
                    if latest and latest["release_id"] not in {identity, notified}:
                        version = manager.installed(latest["release_id"])["version"]
                        print(f"Copilot {version} is ready for your next session.", file=sys.stderr)
                        notified = latest["release_id"]
                except (OSError, UpgradeError):
                    pass
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        lease.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Managed InferMatrixCopilot launcher")
    parser.add_argument("--home", type=Path, default=None)
    parser.add_argument("mode", choices=("mcp", "workflow-mcp", "cli", "upgrade", "_update"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    manager = UpdateManager(args.home)
    try:
        if args.mode == "upgrade" or (args.mode == "cli" and args.arguments[:1] == ["upgrade"]):
            from .cli import main as upgrade_main

            return upgrade_main(args.arguments if args.mode == "upgrade" else args.arguments[1:], root=manager.root)
        if args.mode == "_update":
            manager.check(install=True, automatic=True)
            return 0
        return _application(manager, args.mode, args.arguments)
    except (UpgradeError, OSError) as exc:
        print(f"Copilot launcher: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
