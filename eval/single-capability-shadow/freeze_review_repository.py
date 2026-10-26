"""Freeze a self-contained Git bundle for both exact review revisions.

No source refs or worktrees are changed. The restored checkout uses no object
alternates, so source repository cleanup cannot invalidate a later shadow run.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from infermatrix_copilot.execution_environment import readonly_environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    env = readonly_environment()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        env.pop(key, None)
    env["GIT_LFS_SKIP_SMUDGE"] = "1"

    def git(*command, cwd=None):
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *map(str, command)],
                              cwd=cwd, env=env, check=True, capture_output=True).stdout

    source = args.source.resolve()
    base = git("rev-parse", "--verify", args.base + "^{commit}", cwd=source).decode().strip()
    head = git("rev-parse", "--verify", args.head + "^{commit}", cwd=source).decode().strip()
    bundle = output / "source.bundle"
    with tempfile.TemporaryDirectory(prefix="shadow-git-stage-") as temp:
        staging = Path(temp) / "source.git"
        git("clone", "--quiet", "--bare", "--shared", "--", source, staging)
        git("update-ref", "refs/heads/shadow-base", base, cwd=staging)
        git("update-ref", "refs/heads/shadow-head", head, cwd=staging)
        git("bundle", "create", bundle, "refs/heads/shadow-base", "refs/heads/shadow-head", cwd=staging)
    restored = output / "repository"
    git("clone", "--quiet", "--no-checkout", "--", bundle, restored)
    assert not (restored / ".git/objects/info/alternates").exists()
    git("bundle", "verify", bundle, cwd=restored)
    git("fsck", "--full", "--no-reflogs", cwd=restored)
    git("checkout", "--quiet", "--detach", head, cwd=restored)
    git("remote", "set-url", "origin", "https://github.com/" + args.repository + ".git", cwd=restored)
    trees = {}
    for rev in (base, head):
        original = git("ls-tree", "-r", "-z", rev, cwd=source)
        copied = git("ls-tree", "-r", "-z", rev, cwd=restored)
        assert copied == original
        trees[rev] = {"entries": len(copied.rstrip(b"\0").split(b"\0")),
                      "inventory_sha256": hashlib.sha256(copied).hexdigest()}
    diff = git("diff", "--no-ext-diff", "--binary", base, head, cwd=restored)
    (output / "review.diff").write_bytes(diff)
    manifest = {"repository": args.repository, "base": base, "head": head,
                "bundle": {"path": bundle.name, "bytes": bundle.stat().st_size,
                           "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest()},
                "diff_sha256": hashlib.sha256(diff).hexdigest(), "trees": trees,
                "git_fsck": "passed", "object_alternates": False,
                "status": "source snapshot only; knowledge/evidence request and labels are separate"}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
