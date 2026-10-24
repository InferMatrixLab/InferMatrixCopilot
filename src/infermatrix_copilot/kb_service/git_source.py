"""Committed Git trees, independent of hosting and review providers."""

from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Callable

from ..git_objects import batch_blobs, checked_read, tree_entries

from .sources import SourceError

COMMIT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def public_locator(locator: str) -> str:
    """Remove HTTP credentials, queries and fragments from a public identity."""
    value = str(locator).strip()
    if re.match(r"^[A-Za-z]:[\\/]", value):
        return ""
    if "://" in value:
        url = urllib.parse.urlsplit(value)
        if url.scheme not in ("http", "https", "ssh", "git", "file"):
            raise SourceError("unsupported Git locator scheme")
        if url.scheme == "file":
            return ""  # host paths belong to runtime bindings
        host = url.hostname or ""
        if not host:
            raise SourceError("Git locator has no host")
        port = f":{url.port}" if url.port else ""
        return urllib.parse.urlunsplit((url.scheme, host.lower() + port, url.path.rstrip("/"), "", ""))
    match = re.fullmatch(r"(?:[^/@:\s]+@)?([^/:\s]+):(.+)", value)
    if match:
        return f"ssh://{match[1].lower()}/{match[2].rstrip('/')}"
    return ""  # ordinary local paths are never public locators


class GitSource:
    """Read the fixed committed tree of a local normal or bare repository.

    Neither dirty working files nor an ``origin`` are required or consulted
    when reading evidence. Git's own object format determines the pin width.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self._inventories: dict[str, dict] = {}
        self._git("rev-parse", "--git-dir")

    @property
    def git_dir(self) -> Path:
        value = self._git("rev-parse", "--absolute-git-dir").decode().strip()
        return Path(value)

    @classmethod
    def acquire(cls, locator: str | Path, cachepath: str | Path) -> "GitSource":
        """Make a bare source mirror; local repositories need no origin."""
        target = Path(cachepath).resolve()
        if target.exists():
            raise SourceError("source acquisition destination already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(["git", "clone", "--quiet", "--mirror", "--", str(locator), str(target)],
                              capture_output=True, check=False)
        if proc.returncode:
            raise SourceError(f"Git source acquisition failed (exit {proc.returncode})")
        return cls(target)

    def _git(self, *args: str) -> bytes:
        proc = subprocess.run(["git", "-C", str(self.path), *args], capture_output=True, check=False)
        if proc.returncode:
            # stderr can contain credential-bearing remote URLs.
            raise SourceError(f"Git source operation {args[0]} failed (exit {proc.returncode})")
        return proc.stdout

    @property
    def object_format(self) -> str:
        value = self._git("rev-parse", "--show-object-format").decode().strip()
        if value not in ("sha1", "sha256"):
            raise SourceError("unsupported Git object format")
        return value

    def resolve(self, ref: str = "HEAD") -> str:
        if not isinstance(ref, str) or not ref or ref.startswith("-") or "\0" in ref:
            raise SourceError("invalid Git revision")
        pin = self._git("rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
        width = 40 if self.object_format == "sha1" else 64
        if not COMMIT_ID.fullmatch(pin) or len(pin) != width:
            raise SourceError("revision is not a full commit identity")
        return pin

    def head(self) -> str:
        return self.resolve()

    def origin(self) -> str:
        try:
            return public_locator(self._git("remote", "get-url", "origin").decode().strip())
        except SourceError:
            return ""

    def root_commits(self, pin: str) -> tuple[str, ...]:
        return tuple(sorted(self._git("rev-list", "--max-parents=0", self.resolve(pin)).decode().split()))

    def tree_id(self, pin: str) -> str:
        return self._git("rev-parse", f"{self.resolve(pin)}^{{tree}}").decode().strip()

    def changed_paths(self, old: str, new: str) -> list[str]:
        data = self._git("diff", "--name-only", "-z", "--no-renames", self.resolve(old), self.resolve(new))
        return sorted({path.decode("utf-8") for path in data.split(b"\0") if path})

    def parents(self, pin: str) -> tuple[str, ...]:
        return tuple(self._git("rev-list", "--parents", "-n", "1", self.resolve(pin)).decode().split()[1:])

    def files(self, pin: str) -> dict[str, dict[str, str]]:
        pin = self.resolve(pin)
        if pin in self._inventories:
            return {path: dict(meta) for path, meta in self._inventories[pin].items()}
        result = {}
        for mode, kind, oid, name in tree_entries(self._git("ls-tree", "-r", "-z", "--full-tree", pin)):
            path = name.decode("utf-8")
            if kind == "blob" and mode in ("100644", "100755"):
                result[path] = {"mode": mode, "oid": oid}
        self._inventories[pin] = result
        return {path: dict(meta) for path, meta in result.items()}

    def read(self, pin: str, path: str) -> bytes:
        pure = PurePosixPath(path)
        if not path or pure.is_absolute() or ".." in pure.parts or "\\" in path or "\0" in path:
            raise SourceError("source path escapes the fixed tree")
        files = self.files(pin)
        if path not in files:
            raise SourceError("source path is not a regular committed file")
        return self._git("cat-file", "blob", files[path]["oid"])

    def export(self, pin: str, destination: str | Path) -> Path:
        root = Path(destination).resolve()
        root.mkdir(parents=True, exist_ok=True)
        if any(root.iterdir()):
            raise SourceError("source export destination must be empty")
        inventory = self.files(pin)
        # Archive export attributes can remove or rewrite committed blobs.
        # Read the actual tree objects in one batch instead; export and read
        # therefore have identical evidence bytes and inventory membership.
        proc = subprocess.run(["git", "-C", str(self.path), "cat-file", "--batch"],
                              input="".join(f"{meta['oid']}\n" for meta in inventory.values()).encode(),
                              capture_output=True, check=False)
        if proc.returncode:
            raise SourceError(f"Git source export failed (exit {proc.returncode})")
        contents = checked_read(SourceError, list,
                                batch_blobs(proc.stdout, [meta["oid"] for meta in inventory.values()]))
        for (relative, meta), content in zip(inventory.items(), contents):
            target = (root / relative).resolve()
            if not target.is_relative_to(root):
                raise SourceError("committed source path escapes destination")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(0o755 if meta["mode"] == "100755" else 0o644)
        return root


class ForgeProvider:
    """Optional review transport. Source identity always comes from GitSource."""

    kind = "none"

    def __init__(self, project: str = "", *, host: str = "", token: str = "",
                 request: Callable | None = None):
        self.project = project
        self.host = host.rstrip("/")
        self._token = token  # runtime only; never in public spec/receipts
        self._request = request or self._http

    def _http(self, method: str, url: str, data: dict | None = None) -> dict:
        headers = {"Accept": "application/json"}
        if self._token:
            headers.update(self.auth_headers())
        body = None if data is None else json.dumps(data).encode()
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, body, headers, method=method), timeout=60) as reply:
                return json.loads(reply.read())
        except Exception as exc:
            raise SourceError(f"{self.kind} review transport failed") from exc

    def auth_headers(self) -> dict:
        return {}

    def open_review(self, *, title: str, body: str, head: str, base: str) -> dict:
        raise SourceError("this repository has no configured review forge; use explicit local acceptance")

    def review(self, number: int) -> dict:
        raise SourceError("this repository has no configured review forge")

    def acceptance(self, number: int, expected_head: str) -> dict:
        """Merged identity only; CI is separately checked, never inferred."""
        if not COMMIT_ID.fullmatch(expected_head):
            raise SourceError("expected review head must be a full commit")
        row = self.review(number)
        if self.kind == "github":
            merged = row.get("merged") is True
            head = (row.get("head") or {}).get("sha")
        elif self.kind == "gitlab":
            merged = row.get("state") == "merged"
            head = row.get("sha")
        else:
            raise SourceError("no forge acceptance is available; use local acceptance")
        publication = row.get("merge_commit_sha") or row.get("squash_commit_sha")
        if not merged or head != expected_head or not isinstance(publication, str) or not COMMIT_ID.fullmatch(publication):
            raise SourceError("forge review is not merged at the frozen head/publication commit")
        return {"schema_version": 1, "kind": "forge_review_acceptance", "forge": self.kind,
                "project": self.project, "number": int(number), "review_head": expected_head,
                "publication_head": publication, "merged": True,
                "ci_verified": False, "claim": "Merged review identity; independent source/native/CI gates remain separate."}


class GitHubForge(ForgeProvider):
    kind = "github"

    def __init__(self, project: str, **kwargs):
        super().__init__(project, host=kwargs.pop("host", "https://api.github.com"), **kwargs)
        if len(project.split("/")) != 2:
            raise SourceError("GitHub project must be owner/repository")

    def auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    def open_review(self, *, title: str, body: str, head: str, base: str) -> dict:
        return self._request("POST", f"{self.host}/repos/{self.project}/pulls",
                             {"title": title, "body": body, "head": head, "base": base})

    def review(self, number: int) -> dict:
        return self._request("GET", f"{self.host}/repos/{self.project}/pulls/{int(number)}")


class GitLabForge(ForgeProvider):
    kind = "gitlab"

    def auth_headers(self) -> dict:
        return {"PRIVATE-TOKEN": self._token}

    @property
    def project_url(self) -> str:
        if not self.host or not self.project or any(p in ("", ".", "..") for p in self.project.split("/")):
            raise SourceError("GitLab project/host is invalid")
        return f"{self.host}/api/v4/projects/{urllib.parse.quote(self.project, safe='')}"

    def open_review(self, *, title: str, body: str, head: str, base: str) -> dict:
        return self._request("POST", f"{self.project_url}/merge_requests",
                             {"title": title, "description": body, "source_branch": head, "target_branch": base})

    def review(self, number: int) -> dict:
        return self._request("GET", f"{self.project_url}/merge_requests/{int(number)}")


def forge_provider(kind: str, project: str = "", **kwargs) -> ForgeProvider:
    providers = {"none": ForgeProvider, "github": GitHubForge, "gitlab": GitLabForge}
    try:
        return providers[kind](project, **kwargs)
    except KeyError as exc:
        raise SourceError("unsupported review forge") from exc
