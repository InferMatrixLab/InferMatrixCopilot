"""Take durable ownership of frozen review input before its caller releases it."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from ..contracts.models import InvalidRequestError
from ..persistence import atomic_write_bytes


def snapshot_direct_workspace(run_dir: Path, raw: dict, *, full_name: str) -> Path:
    """Copy Git objects and patch evidence, independent of the caller's checkout.

    Git's worktree metadata and alternate object stores may disappear when the
    caller releases its checkout. A dissociated clone owns its objects and can
    survive both that cleanup and a coordinator restart.
    """
    target = run_dir / "workspace"
    marker = run_dir / "workspace.json"
    expected = raw["review"]["expected_head_sha"]
    source = Path(raw["repo_path"]).resolve()
    from ..providers.json_session import readonly_environment

    def git(*args):
        result = subprocess.run(["git", *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180, check=True,
            env=readonly_environment())
        return result.stdout.strip()

    identity = {"head_sha": expected, "repository": full_name}
    if marker.is_file() or target.exists():
        if target.is_symlink() or not target.is_dir():
            raise InvalidRequestError("Direct workspace is missing or is a symlink")
        if marker.is_file() and json.loads(marker.read_text()) != identity:
            raise InvalidRequestError("Direct workspace identity differs from its reservation")
        if (git("-C", str(target), "rev-parse", "HEAD") != expected or
                git("-C", str(target), "remote", "get-url", "origin") != f"https://github.com/{full_name}.git"):
            raise InvalidRequestError("Direct workspace does not match its frozen repository and head")
        # The directory rename publishes a complete clone atomically. A crash
        # before its marker is written can safely finish that publication.
        if not marker.is_file():
            atomic_write_bytes(marker, json.dumps(identity).encode())
        return target

    if git("-C", str(source), "rev-parse", "HEAD") != expected:
        raise InvalidRequestError("Direct checkout head changed before reservation")
    with tempfile.TemporaryDirectory(prefix=".review-workspace-", dir=run_dir) as temporary:
        clone = Path(temporary) / "checkout"
        git("clone", "--quiet", "--no-hardlinks", "--dissociate", "--no-checkout", "--", str(source), str(clone))
        git("-C", str(clone), "checkout", "--quiet", "--detach", expected)
        git("-C", str(clone), "remote", "set-url", "origin", f"https://github.com/{full_name}.git")
        evidence = (raw.get("business_context") or {}).get("patch_evidence") or {}
        if isinstance(evidence, dict) and evidence.get("path"):
            relative = Path(evidence["path"])
            original = (source / relative).resolve()
            destination = clone / relative
            if relative.is_absolute() or not original.is_relative_to(source) or not destination.resolve().is_relative_to(clone):
                raise InvalidRequestError("Direct patch evidence must stay inside the frozen checkout")
            if not original.is_file():
                raise InvalidRequestError("Direct patch evidence is missing before reservation")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, destination)
        clone.replace(target)
    atomic_write_bytes(marker, json.dumps(identity).encode())
    return target
