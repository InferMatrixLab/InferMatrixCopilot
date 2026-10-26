"""Git object parsers; callers retain transport, trust and decoding policies."""

from __future__ import annotations


def checked_read(error_type, reader, *args, **kwargs):
    """Translate malformed object replies at the caller's own error boundary."""
    try:
        return reader(*args, **kwargs)
    except ValueError as exc:
        raise error_type(str(exc)) from exc


def tree_entries(raw: bytes, *, malformed="raise"):
    """Yield ls-tree entries; tolerant lookups may skip malformed records.

    Skipping a record does not establish that a queried path is absent. Callers
    retain responsibility for caching only successful, empty lookups.
    """
    if malformed not in {"raise", "skip"}:
        raise ValueError("unsupported malformed Git tree policy")
    for entry in raw.split(b"\0"):
        if entry:
            try:
                meta, path = entry.split(b"\t", 1)
                mode, kind, oid = meta.decode("ascii").split()
            except ValueError:
                if malformed == "skip":
                    continue
                raise
            yield mode, kind, oid, path


def batch_blobs(raw: bytes, oids):
    """Read exactly the requested blobs, rejecting missing or corrupt replies."""
    offset = 0
    for oid in oids:
        end = raw.find(b"\n", offset)
        header = raw[offset:end].split() if end >= offset else []
        if len(header) != 3 or header[0].decode("ascii") != oid or header[1] != b"blob" or not header[2].isdigit():
            raise ValueError("committed source export object is invalid")
        size = int(header[2])
        content = raw[end + 1:end + 1 + size]
        offset = end + 1 + size
        if len(content) != size or raw[offset:offset + 1] != b"\n":
            raise ValueError("committed source export is incomplete")
        offset += 1
        yield content
    if offset != len(raw):
        raise ValueError("committed source export contains unexpected objects")


def raw_changes(raw: bytes) -> list[dict]:
    """Decode --raw -z --no-renames --no-abbrev; accept either Git hash width."""
    fields, result = raw.split(b"\0"), []
    for index in range(0, len(fields) - 1, 2):
        meta = fields[index].decode("ascii")
        if not meta.startswith(":"):
            raise ValueError(f"unexpected git diff output: {meta!r}")
        old_mode, new_mode, old_blob, new_blob, status = meta[1:].split()
        result.append({"path": fields[index + 1].decode("utf-8", "replace"), "status": status[0],
                       "old_blob": "" if not old_blob.strip("0") else old_blob,
                       "new_blob": "" if not new_blob.strip("0") else new_blob,
                       "old_mode": "" if old_mode == "000000" else old_mode,
                       "new_mode": "" if new_mode == "000000" else new_mode})
    return result


def tree_texts(run, revision: str, prefixes: tuple[str, ...], suffixes: tuple[str, ...] | None) -> dict[str, str]:
    """Governed text policy: 100644 only; skip undecodable UTF-8 blob bodies."""
    wanted = []
    for mode, kind, oid, path in tree_entries(run("ls-tree", "-r", "-z", "--full-tree", revision)):
        name = path.decode("utf-8", "replace")
        if kind == "blob" and mode == "100644" and name.startswith(prefixes) and (suffixes is None or name.endswith(suffixes)):
            wanted.append((name, oid))
    if not wanted:
        return {}
    raw = run("cat-file", "--batch", input="".join(f"{oid}\n" for _, oid in wanted).encode())
    result = {}
    for (name, _), content in zip(wanted, list(batch_blobs(raw, [oid for _, oid in wanted]))):
        try:
            result[name] = content.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return result
