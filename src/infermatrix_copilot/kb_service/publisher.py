"""The publisher: performs the knowledge service's signed outbox items on GitHub.

It runs on the GPU box as the repository owner, through that user's existing
``gh`` login, which never leaves the box and never enters a model environment.
The knowledge service holds no GitHub write credential; this is the only place
knowledge PRs are opened, commented on, merged or closed.

One round (``kb publish --once``; without it, a round per interval):

1. read the service's signed control record over SSH (or a local directory);
   a stale one means the service may be down, and the round does nothing;
2. for every outbox item: verify it with ``outbox.check_item`` (signature,
   expiry, generation, pause, the repository's publication mode in BOTH the
   control record and the publisher's own adapter configuration), perform it,
   record the result locally, then write a signed ack (``kb-ack``) back;
3. an item already performed only has its ack re-sent (the service deletes an
   item once it collected the ack), so a crash between the two never performs
   an item twice. ``open_pr`` also reuses an open PR on its branch.

Writes are double-gated like every outward write in the copilot: GitHub is
only written when ``ALLOW_POST=1`` (and, for anything that pushes a branch,
``ALLOW_PUSH=1``); otherwise the round only records what it would do.

``open_pr`` never trusts a working tree: it rebuilds the change from the
signed file contents on top of the signed ``base_sha`` in a scratch index,
checks every path is a governed knowledge page, pushes the commit to the
item's branch and opens the PR.
"""

from __future__ import annotations

from ..persistence import atomic_write_bytes

import fcntl
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from ..knowledge_service.signing import sign
from .outbox import CONTROL_MAX_AGE, OutboxError, OutboxItem, check_item

ITEM_ID = re.compile(r"[0-9]+-[0-9a-f]{12}")
ARCHIVE_NAME = re.compile(r"traces-\d{6}-\d{8}-\d{6}\.tar\.gz")
GOVERNED_PATH = re.compile(r"knowledge/(?:repos/[A-Za-z0-9._-]+|general)/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+\.(?:md|yaml)")
BRANCH = re.compile(r"kb/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
# a companion may only touch these (never knowledge/, .github/, src/, tools/)
COMPANION_PATH = re.compile(r"(?:skills|plugins|adapters|doc|playbooks)/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+")
COMPANION_LABEL = "kb:companion"
# companions are recognized by their branch too (kb/<repo>/<repo>-companion-<id>),
# so a label that failed to apply cannot make one mergeable by automation
COMPANION_BRANCH = re.compile(r"kb/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+-companion-[0-9a-f]+")
PUSHING = frozenset({"open_pr", "open_companion_pr", "open_revert_pr"})
# retried until done, never acked as failed: a stop (close) and reports
# (open_issue, post_findings: a transient GitHub error must not lose a sweep
# report or an author's findings)
RETRIED = frozenset({"close", "open_issue", "post_findings"})


class MergeUncertain(RuntimeError):
    """`gh pr merge` failed in a way that does not say whether GitHub merged:
    the intent stays and the next round's recovery asks GitHub (no receipt now)."""


class GateRefused(RuntimeError):
    """The local gate found problems: the PR is not merged (the service refines)."""

    def __init__(self, problems: list[str]):
        super().__init__("local gate: " + "; ".join(problems))
        self.problems = problems


MAIN_RETRIES = 3  # main moved between the check and the merge: check again at most this often


class PublishError(RuntimeError):
    """An item could not be performed; it is acked as failed with this reason."""


# -- where the outbox lives ----------------------------------------------------

class Transport(Protocol):
    def list_items(self) -> list[str]: ...
    def read(self, rel: str) -> bytes: ...
    def write_ack(self, item_id: str, data: bytes) -> None: ...
    def list_archives(self) -> list[str]: ...
    def write_containment(self, identity: str, data: bytes) -> None: ...


@dataclass
class LocalTransport:
    """The service state directory on this machine (tests, co-located setups)."""

    root: Path

    def list_items(self) -> list[str]:
        folder = self.root / "outbox"
        return sorted(p.stem for p in folder.glob("*.json") if ITEM_ID.fullmatch(p.stem)) \
            if folder.is_dir() else []

    def read(self, rel: str) -> bytes:
        return (self.root / rel).read_bytes()

    def list_archives(self) -> list[str]:
        folder = self.root / "archive"
        return sorted(p.name for p in folder.glob("traces-*.tar.gz")
                      if ARCHIVE_NAME.fullmatch(p.name) and (folder / f"{p.name}.sha256").exists()) \
            if folder.is_dir() else []

    def write_ack(self, item_id: str, data: bytes) -> None:
        folder = self.root / "inbox" / "acks"
        folder.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(folder / f"{item_id}.json", data)

    def write_containment(self, identity: str, data: bytes) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise OutboxError("invalid containment envelope identity")
        folder = self.root / "inbox" / "containment"
        folder.mkdir(parents=True, exist_ok=True)
        from ..knowledge_service.containment import atomic_json
        atomic_json(folder / f"{identity}.json", json.loads(data))


@dataclass
class SshTransport:
    """``host:/path/to/state_dir`` on the bot host, through the box's ssh config."""

    host: str
    root: str
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run

    @classmethod
    def parse(cls, remote: str) -> "SshTransport":
        host, sep, root = remote.partition(":")
        if not sep or not host or not root.startswith("/"):
            raise SystemExit(f"--remote must be host:/absolute/state_dir, not {remote!r}")
        return cls(host, root.rstrip("/"))

    def _ssh(self, command: str, data: bytes | None = None) -> bytes:
        proc = self.run(["ssh", "-o", "BatchMode=yes", self.host, command], input=data,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if proc.returncode != 0:
            raise OutboxError(f"ssh {self.host}: {proc.stderr.decode(errors='replace')[:300]}")
        return proc.stdout

    def list_items(self) -> list[str]:
        out = self._ssh(f"ls -1 {shlex.quote(self.root + '/outbox')} 2>/dev/null || true").decode()
        return sorted(name[:-5] for name in out.split() if name.endswith(".json") and ITEM_ID.fullmatch(name[:-5]))

    def read(self, rel: str) -> bytes:
        return self._ssh(f"cat {shlex.quote(self.root + '/' + rel)}")

    def list_archives(self) -> list[str]:
        out = self._ssh(f"ls -1 {shlex.quote(self.root + '/archive')} 2>/dev/null || true").decode().split()
        names = set(out)
        return sorted(n for n in names if ARCHIVE_NAME.fullmatch(n) and f"{n}.sha256" in names)

    def write_ack(self, item_id: str, data: bytes) -> None:
        folder = shlex.quote(self.root + "/inbox/acks")
        tmp, final = shlex.quote(f"{self.root}/inbox/acks/.{item_id}.tmp"), \
            shlex.quote(f"{self.root}/inbox/acks/{item_id}.json")
        self._ssh(f"mkdir -p {folder} && cat > {tmp} && mv {tmp} {final}", data)

    def write_containment(self, identity: str, data: bytes) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise OutboxError("invalid containment envelope identity")
        folder = shlex.quote(self.root + "/inbox/containment")
        tmp = shlex.quote(f"{self.root}/inbox/containment/.{identity}.tmp")
        final = shlex.quote(f"{self.root}/inbox/containment/{identity}.json")
        self._ssh(f"umask 077 && mkdir -p {folder} && cat > {tmp} && mv {tmp} {final}", data)


# -- GitHub through the owner's gh login ------------------------------------------

@dataclass
class Gh:
    """``gh`` and ``git`` as the box owner. ``cwd`` must be inside the owner's
    directory: the machine's gh wrapper picks the login from it."""

    repository: str
    cwd: Path
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run

    def gh(self, *args: str, input: str | None = None, ok_fail: bool = False) -> str:
        proc = self.run(["gh", *args], cwd=self.cwd, input=None if input is None else input.encode(),
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if proc.returncode != 0 and not ok_fail:
            raise PublishError(f"gh {' '.join(args[:3])} failed: {proc.stderr.decode(errors='replace')[:300]}")
        return proc.stdout.decode(errors="replace")

    def pr(self, number: int) -> dict:
        return json.loads(self.gh("pr", "view", str(number), "--repo", self.repository,
                                  "--json", "number,state,isDraft,headRefOid,headRefName,baseRefName,id,"
                                  "labels,mergeCommit,autoMergeRequest"))


# -- the publisher -------------------------------------------------------------------

@dataclass
class Publisher:
    transport: Transport
    service_public_key: Any
    publisher_key: Any
    github: Gh
    state_dir: Path
    repo_flags: Mapping[str, tuple[bool, bool]]  # repo -> (publishes, auto_merge), from OUR adapter config
    allow_post: bool = False
    allow_push: bool = False
    author: tuple[str, str] = ("", "")          # (name, email) of the commits, the gh login's owner
    git_remote: str = ""                         # default https://github.com/<repository>.git
    clock: Callable[[], float] = time.time
    log: list[dict] = field(default_factory=list)
    upstreams: Mapping[str, str] = field(default_factory=dict)  # repo -> public upstream, from OUR config
    observer: Callable[[str, str], Any] | None = None           # (repo, upstream) -> facts observer

    containment_targets: Mapping[str, dict] | None = None
    containment_run: Callable[..., subprocess.CompletedProcess] = subprocess.run

    def sync_containment(self) -> dict:
        from .containment_transport import sync
        return sync(self)

    # local records -------------------------------------------------------------
    def _done_path(self, item_id: str) -> Path:
        return self.state_dir / "done" / f"{item_id}.json"

    def _trace(self, **entry) -> None:
        entry = {"at": self.clock(), **entry}
        self.log.append(entry)
        path = self.state_dir / "traces" / "publisher.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")

    def _send_ack(self, ack: dict) -> bool:
        """False when the ack could not be delivered; the completion record
        stays, so the next round re-sends it without repeating the action."""
        envelope = sign("kb-ack", ack, self.publisher_key)
        try:
            self.transport.write_ack(ack["item_id"], json.dumps(envelope, ensure_ascii=False).encode())
            return True
        except (OutboxError, OSError) as exc:
            self._trace(event="ack_undelivered", item=ack["item_id"], error=str(exc))
            return False

    # one round -----------------------------------------------------------------
    def sync_archives(self) -> dict:
        """Pull every weekly trace archive not yet here, keeping only those whose
        hash verifies (the GPU box is the off-machine copy)."""
        from .archive import verify_archive

        dest = self.state_dir / "archive"
        counts = {"pulled": 0, "rejected": 0}
        try:
            names = self.transport.list_archives()
        except (OutboxError, OSError) as exc:
            self._trace(event="archive_unreachable", error=str(exc))
            return counts
        for name in names:
            if (dest / f"{name}.sha256").exists():
                continue
            try:
                data = self.transport.read(f"archive/{name}")
                manifest = self.transport.read(f"archive/{name}.sha256").decode("utf-8", "replace")
            except (OutboxError, OSError) as exc:
                self._trace(event="archive_unreachable", archive=name, error=str(exc))
                continue
            if not verify_archive(data, manifest, name):
                counts["rejected"] += 1
                self._trace(event="archive_rejected", archive=name)
                continue
            dest.mkdir(parents=True, exist_ok=True)
            (dest / f".{name}.tmp").write_bytes(data)
            (dest / f".{name}.tmp").replace(dest / name)
            (dest / f"{name}.sha256").write_text(manifest, encoding="utf-8")  # last: marks it complete
            counts["pulled"] += 1
            self._trace(event="archive_pulled", archive=name, bytes=len(data))
        return counts

    def run_once(self) -> dict:
        """One round under a cross-process lock: a second publisher (a timer
        firing while a manual run is going) does nothing at all."""
        summary = {"performed": 0, "failed": 0, "resent": 0, "undelivered": 0, "dry_run": 0, "skipped": 0}
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.state_dir / "publisher.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._trace(event="locked", detail="another publisher is running")
                return {**summary, "locked": 1}
            try:
                self.sync_containment()
                self._recover_merges()
                return self._round(summary)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _round(self, summary: dict) -> dict:
        try:
            control = json.loads(self.transport.read("outbox/control.json"))
            # a quick look before verifying (check_item verifies it for real)
            control_issued = float(control["payload"]["issued_at"])
        except (OutboxError, OSError, ValueError, KeyError, TypeError) as exc:
            self._trace(event="no_control", error=str(exc))
            return summary
        if self.clock() - control_issued > CONTROL_MAX_AGE:
            self._trace(event="stale_control", issued_at=control_issued)
            return summary  # the service may be down: act on nothing
        try:
            item_ids = self.transport.list_items()
        except (OutboxError, OSError) as exc:
            self._trace(event="outbox_unreachable", error=str(exc))
            return summary
        for item_id in item_ids:
            done = self._done_path(item_id)
            if done.is_file():
                try:
                    record = json.loads(done.read_text(encoding="utf-8"))
                except ValueError:
                    # never guess whether it was performed: set it aside for people
                    done.replace(done.with_suffix(".corrupt"))
                    self._trace(event="corrupt_record", item=item_id)
                    summary["skipped"] += 1
                    continue
                summary["resent" if self._send_ack(record) else "undelivered"] += 1
                continue
            if done.with_suffix(".corrupt").exists():
                summary["skipped"] += 1  # waits for a person to decide (see the trace)
                continue
            if self._intent_path(item_id).is_file():
                # a merge whose outcome recovery could not settle yet: never
                # perform it again until GitHub has said what happened
                self._trace(event="awaiting_recovery", item=item_id)
                summary["skipped"] += 1
                continue
            summary[self._handle(item_id, control)] += 1
        return summary

    def _record_done(self, item_id: str, ack: dict) -> None:
        """Atomically: a crash leaves either no record or a complete one."""
        path = self._done_path(item_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, json.dumps(ack, ensure_ascii=False, sort_keys=True).encode("utf-8"), mode=0o600)

    def _intent_path(self, item_id: str) -> Path:
        return self.state_dir / "intents" / f"{item_id}.json"

    def _recover_merges(self) -> None:
        """A merge intent without a completion record: the publisher stopped
        between asking GitHub to merge and recording it. Ask GitHub what
        happened and finish the record, so the service gets its receipt."""
        folder = self.state_dir / "intents"
        if not folder.is_dir():
            return
        for path in sorted(folder.glob("*.json")):
            item_id = path.stem
            if self._done_path(item_id).is_file():
                path.unlink(missing_ok=True)
                continue
            try:
                intent = json.loads(path.read_text(encoding="utf-8"))
                pr = self.github.pr(int(intent["pr"]))
            except (PublishError, OSError, ValueError, KeyError, TypeError) as exc:
                self._trace(event="recovery_pending", item=item_id, error=str(exc))
                continue
            merged = str((pr.get("mergeCommit") or {}).get("oid") or "")
            ack = {"item_id": item_id, "kind": "merge", "changeset_id": intent["changeset_id"],
                   "pr": int(intent["pr"]), "head_sha": intent["head_sha"], "branch": "", "error": "",
                   "merge_sha": "", "problems": [], "ok": True, "recovered": True,
                   "post_check": "unknown"}
            if pr.get("state") == "MERGED" and merged and pr.get("headRefOid") == intent["head_sha"]:
                ack["merge_sha"] = merged
            elif pr.get("state") == "OPEN":
                if not self._cancel_pending_merge(int(intent["pr"])):
                    self._trace(event="recovery_pending", item=item_id, error="still queued or auto-merging")
                    continue  # GitHub may still merge it: keep the intent
                path.unlink()  # the merge did not happen: the item is simply tried again
                self._trace(event="intent_dropped", item=item_id)
                continue
            else:
                ack.update(ok=False, error=f"PR #{intent['pr']} is {pr.get('state')} (not merged by us)")
            self._record_done(item_id, ack)
            path.unlink(missing_ok=True)
            self._trace(event="merge_recovered", item=item_id, ack=ack)
            self._send_ack(ack)

    def _handle(self, item_id: str, control: Any) -> str:
        try:
            envelope = json.loads(self.transport.read(f"outbox/{item_id}.json"))
            payload = envelope.get("payload") if isinstance(envelope, dict) else None
            claimed = str(payload.get("repo", "")) if isinstance(payload, dict) else ""
            publishes, auto_merge = self.repo_flags.get(claimed, (False, False))
            item = check_item(envelope, control, self.service_public_key, now=self.clock(),
                              repo_publishes=publishes, repo_auto_merge=auto_merge)
            self._envelope, self._flags, self._control = envelope, (publishes, auto_merge), control
        except (OutboxError, OSError, ValueError, KeyError, TypeError) as exc:
            # not performed and not acked: an unverifiable item is never
            # answered, and a stale one is superseded by the service itself
            self._trace(event="refused", item=item_id, error=str(exc))
            return "skipped"
        if not self.allow_post or (item.kind in PUSHING and not self.allow_push):
            self._trace(event="dry_run", item=item.id, kind=item.kind, repo=item.repo,
                        body={k: v for k, v in item.body.items() if k not in ("files", "comment", "body")})
            return "dry_run"
        ack = {"item_id": item.id, "kind": item.kind, "changeset_id": str(item.body.get("changeset_id", "")),
               "ok": True, "pr": None, "head_sha": "", "branch": "", "error": ""}
        try:
            ack.update(getattr(self, f"_do_{item.kind}")(item))
        except MergeUncertain as exc:
            self._trace(event="merge_uncertain", item=item.id, repo=item.repo, error=str(exc))
            return "failed"  # no record, no ack: recovery settles it from GitHub
        except GateRefused as exc:
            ack.update(ok=False, error=str(exc)[:500], problems=exc.problems[:50])
        except (PublishError, OSError, ValueError, KeyError, TypeError) as exc:
            ack.update(ok=False, error=str(exc)[:500])
        if not ack["ok"] and item.kind in RETRIED:
            # a stop that did not happen must not be answered as done: the item
            # stays in the outbox and is retried every round until it succeeds
            self._trace(event="retry", item=item.id, kind=item.kind, repo=item.repo, error=ack["error"])
            return "failed"
        self._record_done(item.id, ack)
        self._intent_path(item.id).unlink(missing_ok=True)  # the completion record supersedes it
        self._trace(event="performed" if ack["ok"] else "failed", item=item.id, kind=item.kind,
                    repo=item.repo, ack=ack)
        self._send_ack(ack)
        return "performed" if ack["ok"] else "failed"

    # the actions ---------------------------------------------------------------
    def _do_open_pr(self, item: OutboxItem, *, draft: bool = False, allowed: re.Pattern = GOVERNED_PATH,
                    what: str = "governed knowledge pages") -> dict:
        body = item.body
        branch = str(body["branch"])
        if not BRANCH.fullmatch(branch):
            raise PublishError(f"refusing branch name {branch!r}")
        files: dict[str, str] = dict(body.get("files") or {})
        deleted = [str(p) for p in body.get("deleted") or []]
        for path in [*files, *deleted]:
            if not allowed.fullmatch(path) or ".." in path.split("/"):
                raise PublishError(f"refusing to write outside {what}: {path}")
        commit = self._build_commit(str(body["base_sha"]), files, deleted, str(body["title"]),
                                    when=item.issued_at)
        existing = json.loads(self.github.gh("pr", "list", "--repo", self.github.repository, "--head", branch,
                                             "--state", "open", "--json", "number,headRefOid,isDraft"))
        if existing:  # performed before a crash: reuse it only if it is exactly this change
            pr = existing[0]
            if len(existing) != 1 or pr["headRefOid"] != commit:
                raise PublishError(f"an open PR on {branch} does not carry the signed change")
            if draft and not pr.get("isDraft"):
                raise PublishError(f"the PR on {branch} must be a draft")
            return {"pr": int(pr["number"]), "head_sha": commit, "branch": branch}
        remote = self.git_remote or f"https://github.com/{self.github.repository}.git"
        listed = self._git("ls-remote", remote, f"refs/heads/{branch}", auth=True).split()
        if not listed:  # expect the branch to be absent: never overwrite anything
            self._git("push", "--quiet", remote, f"{commit}:refs/heads/{branch}",
                      f"--force-with-lease=refs/heads/{branch}:", auth=True)
        elif listed[0] != commit:
            raise PublishError(f"branch {branch} already exists with other content")
        args = ["pr", "create", "--repo", self.github.repository, "--base", "main", "--head", branch,
                "--title", str(body["title"]), "--body-file", "-"]
        self.github.gh(*args, *(["--draft"] if draft else []), input=str(body.get("body") or ""))
        opened = json.loads(self.github.gh("pr", "list", "--repo", self.github.repository, "--head", branch,
                                           "--state", "open", "--json", "number,headRefOid"))
        if len(opened) != 1 or opened[0]["headRefOid"] != commit:
            raise PublishError(f"the PR for {branch} could not be confirmed after creation")
        return {"pr": int(opened[0]["number"]), "head_sha": commit, "branch": branch}

    def _do_open_revert_pr(self, item: OutboxItem) -> dict:
        """A revert of an unknown change: governed pages only, never merged by us."""
        return self._do_open_pr(item)

    def _provenance_problems(self, main_sha: str) -> list[str]:
        """Every governed knowledge commit on main since the active snapshot
        must be trusted: listed in the signed control record, or a merge this
        publisher made itself (its receipt may not have reached the service)."""
        payload = (getattr(self, "_control", None) or {}).get("payload") or {}
        provenance = payload.get("provenance") or {}
        active = str(provenance.get("active") or "")
        if not active:
            return []  # nothing activated yet: no trusted baseline to compare with
        trusted = set(provenance.get("trusted") or [])
        done = self.state_dir / "done"
        for path in (done.glob("*.json") if done.is_dir() else []):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if record.get("kind") == "merge" and record.get("merge_sha"):
                trusted.add(record["merge_sha"])
        commits = self._git("rev-list", "--first-parent", f"{active}..{main_sha}", "--",
                            "knowledge/repos", "knowledge/general").split()
        return [sha for sha in commits if sha not in trusted]

    def _do_open_companion_pr(self, item: OutboxItem) -> dict:
        """Always a draft, labelled kb:companion, and only within the companion
        whitelist: people review, ready and merge it; automation never does."""
        result = self._do_open_pr(item, draft=True, allowed=COMPANION_PATH,
                                  what="the companion whitelist (skills, plugins, adapters, doc, playbooks)")
        self.github.gh("pr", "edit", str(result["pr"]), "--repo", self.github.repository,
                       "--add-label", COMPANION_LABEL, ok_fail=True)
        return result

    def _refuse_companion(self, pr: dict) -> None:
        labelled = COMPANION_LABEL in {str(label.get("name")) for label in pr.get("labels") or []}
        if labelled or COMPANION_BRANCH.fullmatch(str(pr.get("headRefName") or "")):
            raise PublishError("a companion PR is readied and merged only by people")

    def _do_open_issue(self, item: OutboxItem) -> dict:
        """One issue per title (a sweep report): an existing one is reused."""
        title = str(item.body["title"])
        found = json.loads(self.github.gh("issue", "list", "--repo", self.github.repository, "--state", "all",
                                          "--search", f'in:title "{title}"', "--json", "number,title"))
        for issue in found:
            if issue.get("title") == title:
                return {"pr": int(issue["number"])}
        args = ["issue", "create", "--repo", self.github.repository, "--title", title, "--body-file", "-"]
        for label in item.body.get("labels") or []:
            args += ["--label", str(label)]
        out = self.github.gh(*args, input=str(item.body.get("body") or ""), ok_fail=True)
        if not out.strip():  # a missing label must not lose the report
            out = self.github.gh("issue", "create", "--repo", self.github.repository, "--title", title,
                                 "--body-file", "-", input=str(item.body.get("body") or ""))
        number = out.strip().rsplit("/", 1)[-1]
        return {"pr": int(number) if number.isdigit() else None}

    def _recheck_control(self) -> None:
        """Right before merging: a pause, rollback or mode change issued while
        the gate ran voids the item (the control record is re-read)."""
        try:
            control = json.loads(self.transport.read("outbox/control.json"))
            publishes, auto_merge = self._flags
            check_item(self._envelope, control, self.service_public_key, now=self.clock(),
                       repo_publishes=publishes, repo_auto_merge=auto_merge)
        except (OutboxError, OSError, ValueError, KeyError, TypeError) as exc:
            raise PublishError(f"not merging: {exc}") from exc

    def _recheck_facts(self, repo: str, verdict: dict) -> None:
        """Observe every signed upstream fact again from the upstream itself (our
        own mirror and ``gh api``, never the service's). Any difference, or an
        upstream we cannot read, means no merge this round (design v8 §8.2)."""
        from ..knowledge_service.facts import FactsError, recheck

        facts = verdict.get("facts") or []
        if not facts:
            return
        upstream = self.upstreams.get(repo, "")
        if not upstream:
            raise PublishError(f"not merging: upstream facts: {repo} has no public upstream in our configuration")
        observer = (self.observer or self._mirror_observer)(repo, upstream)
        try:
            problems = recheck(verdict.get("upstream") or {}, facts, observer)
        except FactsError as exc:
            raise PublishError(f"not merging: upstream facts: {exc}") from exc
        if problems:
            raise PublishError("not merging: upstream facts: " + "; ".join(problems[:5]))

    def _mirror_observer(self, repo: str, upstream: str):
        from .upstream_facts import MirrorObserver

        return MirrorObserver(self.state_dir / "upstream" / f"{repo}.git", upstream,
                              lambda number: json.loads(self.github.gh("api", f"repos/{upstream}/pulls/{number}")))

    def _recheck_maintenance(self, item: OutboxItem) -> None:
        body = item.body
        if not body.get("maintenance_required") and "maintenance" not in body:
            return
        from ..knowledge_service.containment import MAX_AGE, MAX_RUNTIME_AGE, policy_identity
        from ..knowledge_service.signing import verify
        try:
            proof = verify("kb-maintenance-merge", body.get("maintenance"), self.service_public_key)
            policy = verify("kb-containment-policy", json.loads(self.transport.read("outbox/containment-policy.json")), self.service_public_key)
            now = self.clock()
            if proof.get("schema_version") != 1 or proof.get("changeset_id") != body["changeset_id"] \
                    or proof.get("head_sha") != body["head_sha"] \
                    or not re.fullmatch(r"[0-9a-f]{64}", proof.get("policy_sha256", "")) \
                    or not proof["issued_at"] <= now < proof["expires_at"] \
                    or not 0 < proof["expires_at"] - proof["issued_at"] <= 60 \
                    or proof["generation"] != policy["generation"] \
                    or proof["policy_digest"] != policy_identity(policy) \
                    or not policy["issued_at"] <= now < policy["expires_at"] \
                    or not 0 < policy["expires_at"] - policy["issued_at"] <= MAX_AGE:
                raise ValueError("expired or changed correction policy")
            acks = proof["acks"]
            if not isinstance(acks, list) or not policy["consumers"] \
                    or sorted(a["consumer_id"] for a in acks) != sorted(policy["consumers"]):
                raise ValueError("correction consumer roster is incomplete")
            for ack in acks:
                if ack.get("protocol") != 1 or ack.get("ready") is not True or not ack.get("release_id") \
                        or ack.get("generation") != proof["generation"] \
                        or ack.get("policy_digest") != proof["policy_digest"] \
                        or ack.get("runtime_ready") is not True \
                        or not re.fullmatch(r"[0-9a-f]{64}",ack.get("runtime_config_sha256", "")) \
                        or not 0 <= now-ack["runtime_checked_at"] < MAX_RUNTIME_AGE \
                        or not 0 <= now - ack["checked_at"] < MAX_AGE:
                    raise ValueError("correction consumer ACK is stale")
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise PublishError("not merging: maintenance authorization unavailable or expired") from exc

    def _do_merge(self, item: OutboxItem) -> dict:
        """The local gate on the exact merged tree, then the merge (design v8 §8.2)."""
        from . import local_gate

        body = item.body
        number, head = int(body["pr"]), str(body["head_sha"])
        pr = self.github.pr(number)
        self._refuse_companion(pr)  # whatever the item carries
        verdict = local_gate.check_verdict(body.get("verdict"), self.service_public_key,
                                           repository=self.github.repository, pr=number, head_sha=head,
                                           now=self.clock())
        if pr.get("state") != "OPEN":
            raise PublishError(f"PR #{number} is {pr.get('state')}")
        if pr.get("baseRefName") != "main":
            raise GateRefused([f"PR #{number} targets {pr.get('baseRefName')}, not main"])
        if pr.get("headRefOid") != head:
            raise PublishError(f"PR #{number} head moved to {str(pr.get('headRefOid'))[:12]}")
        if pr.get("isDraft"):  # its author marked it work in progress: never readied by automation
            raise PublishError(f"not merging: PR #{number} is a draft")
        remote = self._ensure_clone()
        rest = {"number": number, "state": "open", "head": {"sha": head}, "draft": bool(pr.get("isDraft")),
                "labels": pr.get("labels") or []}
        for _attempt in range(MAIN_RETRIES):
            self._git("fetch", "--quiet", remote, "+refs/heads/main:refs/remotes/origin/main",
                      f"+refs/pull/{number}/head:refs/kb/pr-{number}", auth=True)
            if self._git("rev-parse", f"refs/kb/pr-{number}") != head:
                raise PublishError(f"fetched head of PR #{number} is not {head[:12]}")
            main_sha = self._git("rev-parse", "refs/remotes/origin/main")
            unknown = self._provenance_problems(main_sha)
            if unknown:
                raise PublishError(f"not merging: knowledge changes on main that passed no gate "
                                   f"({', '.join(s[:12] for s in unknown[:3])}); the service pauses and reverts them")
            problems = local_gate.gate(self.clone, repository=self.github.repository, repo=item.repo, pr=rest,
                                       head_sha=head, main_sha=main_sha, verdict=verdict,
                                       public_key=self.service_public_key, now=self.clock())
            if problems:
                raise GateRefused(problems)
            self._git("fetch", "--quiet", remote, "+refs/heads/main:refs/remotes/origin/main", auth=True)
            if self._git("rev-parse", "refs/remotes/origin/main") == main_sha:
                break
        else:
            raise PublishError(f"main kept moving while PR #{number} was checked; retried next round")
        self._recheck_facts(item.repo, verdict)
        self._recheck_control()
        self._recheck_maintenance(item)
        intent = self._intent_path(item.id)
        intent.parent.mkdir(parents=True, exist_ok=True)
        intent.write_text(json.dumps({"pr": number, "head_sha": head, "changeset_id": body["changeset_id"],
                                      "main_sha": main_sha, "at": self.clock()}), encoding="utf-8")
        try:
            self.github.gh("pr", "merge", str(number), "--repo", self.github.repository, "--merge",
                           "--match-head-commit", head)
        except PublishError as exc:
            raise MergeUncertain(f"gh pr merge #{number}: {exc}") from exc
        # only a PR GitHub shows as MERGED is a merge: with a merge queue or
        # auto-merge on main, `gh pr merge` succeeds by queueing instead
        try:
            merged = self.github.pr(number)
        except (PublishError, OSError, ValueError, KeyError, TypeError) as exc:
            raise MergeUncertain(f"PR #{number} state after merging: {exc}") from exc
        if merged.get("state") == "OPEN":
            if not self._cancel_pending_merge(number):
                # GitHub could still merge it later: keep the intent, so recovery
                # keeps trying to cancel, or picks up the merge if it happens
                raise MergeUncertain(f"PR #{number} was queued for auto-merge and could not be cancelled yet")
            raise GateRefused([f"PR #{number} was queued or set to auto-merge, not merged: main must allow "
                               "a direct merge by the publisher (no merge queue); auto-merge was disabled"])
        if merged.get("state") != "MERGED":
            raise PublishError(f"PR #{number} is {merged.get('state')} after merging")
        # merged from here on: nothing below may turn this into a failure ack
        try:
            merge_sha = str((merged.get("mergeCommit") or {}).get("oid") or "")
            if not merge_sha:
                return {"pr": number, "head_sha": head, "merge_sha": "", "post_check": "unknown",
                        "problems": ["merged, but GitHub has not reported the merge commit yet"]}
            self._git("fetch", "--quiet", remote, "+refs/heads/main:refs/remotes/origin/main", auth=True)
            post = local_gate.post_merge_problems(self.clone, repository=self.github.repository, pr=rest,
                                                  head_sha=head, merge_sha=merge_sha, verdict=verdict,
                                                  public_key=self.service_public_key, now=self.clock())
        except (PublishError, local_gate.LocalGateError, OSError, ValueError, KeyError, TypeError) as exc:
            return {"pr": number, "head_sha": head, "merge_sha": "", "post_check": "unknown",
                    "problems": [f"merged, but the post-merge check could not run: {exc}"]}
        return {"pr": number, "head_sha": head, "merge_sha": merge_sha,
                "post_check": "failed" if post else "passed", "problems": post[:50]}

    _PENDING_QUERY = ("query=query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name)"
                      "{pullRequest(number:$number){id state mergeQueueEntry{id} autoMergeRequest{enabledAt}}}}")

    def _pending_merge(self, number: int) -> dict:
        owner, name = self.github.repository.split("/", 1)
        data = json.loads(self.github.gh("api", "graphql", "-f", self._PENDING_QUERY, "-F", f"owner={owner}",
                                         "-F", f"name={name}", "-F", f"number={number}"))
        return data["data"]["repository"]["pullRequest"]

    def _cancel_pending_merge(self, number: int) -> bool:
        """Take the PR out of the merge queue AND turn auto-merge off, then
        confirm both on GitHub. True only when GitHub shows the PR open with
        neither: any failure (a command, the network, a malformed answer)
        leaves the merge unresolved."""
        try:
            pr = self._pending_merge(number)
            if pr.get("mergeQueueEntry"):
                self.github.gh("api", "graphql", "-f", "query=mutation($id:ID!){dequeuePullRequest("
                               "input:{id:$id}){clientMutationId}}", "-f", f"id={pr['id']}", ok_fail=True)
            if pr.get("autoMergeRequest"):
                self.github.gh("pr", "merge", str(number), "--repo", self.github.repository, "--disable-auto",
                               ok_fail=True)
            after = self._pending_merge(number)
        except (PublishError, OSError, ValueError, KeyError, TypeError) as exc:
            self._trace(event="cancel_unconfirmed", pr=number, error=str(exc))
            return False
        return after.get("state") == "OPEN" and not after.get("mergeQueueEntry") \
            and not after.get("autoMergeRequest")

    def _login(self) -> str:
        if not getattr(self, "_me", ""):
            self._me = json.loads(self.github.gh("api", "user"))["login"]
        return self._me

    def _do_post_findings(self, item: OutboxItem) -> dict:
        """ONE findings comment per PR, edited in place on every daily check:
        the comment of ours that carries the marker is updated, else created."""
        number, marker = int(item.body["pr"]), str(item.body["marker"])
        comment = str(item.body["comment"])
        if marker not in comment:
            raise PublishError("a findings comment must carry its marker")
        revision = float(item.body.get("revision") or item.issued_at)
        posted = self.state_dir / "findings" / f"{number}.json"
        try:
            latest = float(json.loads(posted.read_text(encoding="utf-8"))["revision"]) if posted.is_file() else 0.0
        except (OSError, ValueError, KeyError, TypeError):
            latest = 0.0  # unreadable (written atomically, so only by hand): this item decides
            self._trace(event="findings_record_unreadable", pr=number)
        if latest > revision:
            return {"pr": number, "superseded": True}  # a newer findings comment is already there
        existing = json.loads(self.github.gh("api", "--paginate", "--slurp",
                                             f"repos/{self.github.repository}/issues/{number}/comments"))
        flat = [c for page in existing for c in (page if isinstance(page, list) else [page])]
        mine = [c for c in flat if marker in str(c.get("body") or "")
                and (c.get("user") or {}).get("login") == self._login()]
        if mine:
            self.github.gh("api", "-X", "PATCH", f"repos/{self.github.repository}/issues/comments/{mine[0]['id']}",
                           "-F", "body=@-", input=comment)
        else:
            self.github.gh("pr", "comment", str(number), "--repo", self.github.repository, "--body-file", "-",
                           input=comment)
        posted.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(posted, json.dumps({"revision": revision, "item": item.id}).encode("utf-8"))
        return {"pr": number}

    def _do_close(self, item: OutboxItem) -> dict:
        number = str(item.body["pr"])
        if self.github.pr(int(number)).get("state") == "OPEN":
            self.github.gh("pr", "close", number, "--repo", self.github.repository,
                           "--comment", str(item.body.get("reason") or "closed by the knowledge service"))
        return {"pr": int(number)}

    # git ------------------------------------------------------------------------
    @property
    def clone(self) -> Path:
        return self.state_dir / "clones" / self.github.repository.replace("/", "__")

    def _git(self, *args: str, env: Mapping[str, str] | None = None, input: bytes | None = None,
             auth: bool = False) -> str:
        prefix = ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"] if auth else []
        proc = subprocess.run(["git", "-C", str(self.clone), *prefix, *args], input=input,
                              env={**os.environ, **(env or {})}, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, check=False, cwd=self.github.cwd)
        if proc.returncode != 0:
            raise PublishError(f"git {args[0]} failed: {proc.stderr.decode(errors='replace')[:300]}")
        return proc.stdout.decode().strip()

    def _ensure_clone(self) -> str:
        """The publisher's own bare clone (never a working tree); returns the remote."""
        remote = self.git_remote or f"https://github.com/{self.github.repository}.git"
        if not (self.clone / ".git").exists() and not (self.clone / "HEAD").exists():
            self.clone.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(["git", "clone", "--quiet", "--bare", remote, str(self.clone)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, cwd=self.github.cwd)
            if proc.returncode != 0:
                raise PublishError(f"git clone failed: {proc.stderr.decode(errors='replace')[:300]}")
        return remote

    def _build_commit(self, base_sha: str, files: Mapping[str, str], deleted: list[str], title: str,
                      *, when: float) -> str:
        """The change as ONE commit on ``base_sha``, built in a scratch index.
        Deterministic (dates from the item), so a retry rebuilds the same SHA."""
        remote = self._ensure_clone()
        self._git("fetch", "--quiet", remote, "+refs/heads/main:refs/remotes/origin/main")
        if self._git("cat-file", "-t", base_sha) != "commit":
            raise PublishError(f"base {base_sha[:12]} is not a commit")
        with tempfile.TemporaryDirectory() as scratch:
            env = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
            self._git("read-tree", base_sha, env=env)
            for path, text in sorted(files.items()):
                oid = self._git("hash-object", "-w", "--stdin", input=text.encode("utf-8"))
                self._git("update-index", "--add", "--cacheinfo", f"100644,{oid},{path}", env=env)
            for path in deleted:
                self._git("update-index", "--force-remove", path, env=env)
            tree = self._git("write-tree", env=env)
        name, email = self.author
        if not name or not email:
            raise PublishError("no commit author configured (KB_PUBLISHER_GIT_AUTHOR='Name <email>')")
        date = f"{int(when)} +0000"
        return self._git("commit-tree", tree, "-p", base_sha, "-m", title, env={
            "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email, "GIT_COMMITTER_DATE": date})

    def serve(self, *, interval: float, stop=None) -> None:
        import threading

        stop = stop or threading.Event()
        from .containment_transport import targets
        round_interval = min(interval,30) if targets(self) else interval
        # The policy heartbeat is independent of a slow GitHub gate round.
        # Default targets are empty, so existing installations stay inert.
        def heartbeat():
            while not stop.is_set():
                started = time.monotonic()
                try:
                    self.sync_containment()
                except (RuntimeError, OSError, ValueError, KeyError, TypeError):
                    self._trace(event="containment_sync_failed")
                stop.wait(max(1,45-(time.monotonic()-started)))
        worker = threading.Thread(target=heartbeat, name="kb-containment-sync", daemon=True)
        worker.start()
        try:
            while not stop.is_set():
                self._trace(event="round", **self.run_once(), **self.sync_archives())
                stop.wait(round_interval)
        finally:
            stop.set()
            worker.join(timeout=25)
