"""Weekly off-machine archive of the trace store.

Traces are kept forever; a single bot host must not be their only copy. Once
a week the scheduler packs everything not yet archived in
``<state_dir>/traces`` into ``<state_dir>/archive/traces-<seq>-<time>.tar.gz``
with a ``.sha256`` manifest (the archive's hash, then one line per member),
written last to mark it complete. What was archived is tracked explicitly in
``archive/archived.json`` (path -> content hash), never by timestamps: a
blob renamed into place while an archive was being made is simply picked up by
the next one, and a record file that grew since is archived again. Names carry
a monotonic sequence FIRST, so name order is creation order whatever the
wall clock does, and are never reused or overwritten. ``index.db`` is
rebuilt from the records and never archived.

The publisher on the GPU box, which already reaches the bot host over SSH,
pulls every archive it does not have and keeps it only if the hash verifies.
Restoring is extracting the archives in name order into an empty trace root
(later copies of a record file replace earlier ones) and running
``TraceStore.rebuild_index``.
"""

from __future__ import annotations

from ..persistence import atomic_write_bytes

import hashlib
import io
import json
import re
import tarfile
import time
from pathlib import Path

SEQUENCE = "trace_archive_seq"
NAME = re.compile(r"traces-\d{6}-\d{8}-\d{6}\.tar\.gz")  # sequence first: name order = restore order
INDEX = "archived.json"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_archive(rt) -> Path | None:
    """Pack everything not yet archived; None when nothing is new."""
    root = rt.state_dir / "traces"
    out_dir = rt.state_dir / "archive"
    index_path = out_dir / INDEX
    archived: dict[str, str] = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    members: list[tuple[str, bytes]] = []
    for path in sorted(root.rglob("*")) if root.is_dir() else []:
        if not path.is_file() or path.name == "index.db" or path.name.endswith(".tmp"):
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith("blobs/") and rel in archived:
            continue  # content-addressed: never changes once written
        data = path.read_bytes()
        digest = _sha(data)
        if archived.get(rel) != digest:
            members.append((rel, data))
            archived[rel] = digest
    if not members:
        return None
    sequence = int(rt.ledger.get_cursor("*", SEQUENCE) or 0) + 1
    rt.ledger.set_cursor("*", SEQUENCE, str(sequence))  # consumed even if packing fails: never reused
    name = f"traces-{sequence:06d}-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime(rt.clock())) + ".tar.gz"
    out_dir.mkdir(parents=True, exist_ok=True)
    if (out_dir / name).exists():
        raise FileExistsError(f"refusing to overwrite archive {name}")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for rel, data in members:
            info = tarfile.TarInfo(rel)
            info.size, info.mode, info.mtime = len(data), 0o644, int(rt.clock())
            tar.addfile(info, io.BytesIO(data))
    blob = buffer.getvalue()
    manifest = f"{_sha(blob)}  {name}\n" + "".join(f"{_sha(data)}  {rel}\n" for rel, data in members)
    atomic_write_bytes(out_dir / name, blob)
    (out_dir / f"{name}.sha256").write_text(manifest, encoding="utf-8")  # written last: marks it complete
    atomic_write_bytes(index_path, json.dumps(archived, sort_keys=True).encode("utf-8"))  # after the complete archive
    return out_dir / name


def verify_archive(data: bytes, manifest: str, name: str) -> bool:
    first = (manifest.splitlines() or [""])[0].split()
    return len(first) == 2 and first[1] == name and _sha(data) == first[0]
