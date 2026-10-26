"""Canonical frozen byte inputs and sealed configuration paths.

Hosts supply data, never host-file mount paths. Backends own projection into
the sandbox; the original command/result binding owns these exact input bytes.
"""
import json
import os
from pathlib import Path, PurePosixPath
import stat
import base64
import hashlib
import posixpath
import re

from ..persistence import immutable_write_bytes

INPUT_DIRECTORY = '/tmp/inputs'
LEGACY_INPUT_PROTOCOL_VERSION = 3
SEALED_FILE_INPUT_PROTOCOL_VERSION = 4
BINARY_INPUT_PROTOCOL_VERSION = 5
INPUT_PROTOCOL_VERSION = 6
MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024


def input_protocol(value):
    if value and value.get('directories'):
        return INPUT_PROTOCOL_VERSION
    if value and any(isinstance(content, dict) for content in value['files'].values()):
        return BINARY_INPUT_PROTOCOL_VERSION
    return SEALED_FILE_INPUT_PROTOCOL_VERSION if value and value.get('sealed_files') else LEGACY_INPUT_PROTOCOL_VERSION


def normalize_inputs(value=None, *, projection=False):
    if value is None:
        return None
    if (not isinstance(value, dict) or not {'files', 'sealed_dirs'} <= set(value)
            or set(value) - {'files', 'sealed_dirs', 'sealed_files', 'directories'}):
        raise ValueError('invalid frozen sandbox inputs')
    files, sealed = value['files'], value['sealed_dirs']
    directories = value.get('directories', [])
    if not isinstance(directories, (list, tuple)) or len(directories) > 32768:
        raise ValueError('invalid frozen sandbox directory limits')
    extended = bool(directories) or isinstance(files, dict) and any(isinstance(content, dict) for content in files.values())
    if (not isinstance(files, dict) or len(files) > (32768 if extended else 32)
            or len(files) + len(directories) > 32768
            or not isinstance(sealed, (list, tuple)) or len(sealed) > 16):
        raise ValueError('invalid frozen sandbox input limits')
    sealed_files = value.get('sealed_files', [])
    if not isinstance(sealed_files, (list, tuple)) or len(sealed_files) > 32:
        raise ValueError('invalid sealed sandbox file limits')
    total = text_total = 0
    for name, content in files.items():
        if not isinstance(name, str) or not isinstance(content, (str, dict)):
            raise ValueError('sandbox files require text paths and UTF-8 content')
        path = PurePosixPath(name)
        if (not name.startswith(INPUT_DIRECTORY + '/') or str(path) != name
                or any(part in ('.', '..') for part in path.parts) or any(c in name for c in '\0\r\n,')
                or len(name) > 512):
            raise ValueError('sandbox file must have a canonical input path')
        if isinstance(content, str):
            try:
                size = len(content.encode('utf-8'))
            except UnicodeEncodeError as exc:
                raise ValueError('sandbox file must contain valid UTF-8') from exc
            text_total += size
        elif set(content) == {'link'}:
            target = content['link']
            if (not isinstance(target, str) or not target or target.startswith('/')
                    or any(c in target for c in '\0\r\n')
                    or not posixpath.normpath(str(path.parent) + '/' + target).startswith(INPUT_DIRECTORY + '/')):
                raise ValueError('sandbox link escapes frozen inputs')
            size = len(target.encode('utf-8'))
        else:
            required = {'sha256', 'size', 'executable'} if projection else {'base64', 'executable'}
            if set(content) != required or type(content['executable']) is not bool:
                raise ValueError('invalid frozen binary file')
            if projection:
                size = content['size']
                if (type(size) is not int or size < 0 or not isinstance(content['sha256'], str)
                        or not re.fullmatch('[0-9a-f]{64}', content['sha256'])):
                    raise ValueError('invalid frozen file identity')
            else:
                try:
                    raw = base64.b64decode(content['base64'], validate=True)
                    if base64.b64encode(raw).decode('ascii') != content['base64']:
                        raise ValueError('noncanonical binary encoding')
                except (TypeError, ValueError) as exc:
                    raise ValueError('invalid frozen file bytes') from exc
                size = len(raw)
        total += size
        if text_total > 4 * 1024 * 1024 or total > MAX_SNAPSHOT_BYTES:
            raise ValueError('sandbox input bytes exceed capacity')
    names = set(files)
    if any(str(parent) in names for name in names for parent in PurePosixPath(name).parents):
        raise ValueError('sandbox input file paths overlap')
    for name in directories:
        if (not isinstance(name, str) or not name.startswith(INPUT_DIRECTORY + '/')
                or str(PurePosixPath(name)) != name or len(name) > 512
                or any(part in ('.', '..') for part in PurePosixPath(name).parts)
                or any(c in name for c in '\0\r\n,')
                or name in names or any(str(parent) in names for parent in PurePosixPath(name).parents)):
            raise ValueError('sandbox directory must have an unshadowed canonical input path')
    if len(set(directories)) != len(directories):
        raise ValueError('sandbox input directories repeat')
    for name in sealed:
        if (not isinstance(name, str) or len(PurePosixPath(name).parts) != 3
                or not name.startswith('/etc/') or str(PurePosixPath(name)) != name
                or name.split('/')[-1] in ('.', '..') or any(c in name for c in '\0\r\n,')):
            raise ValueError('sealed configuration must name one /etc component directory')
    if len(set(sealed)) != len(sealed):
        raise ValueError('sealed sandbox directories repeat')
    for name in sealed_files:
        if (not isinstance(name, str) or len(PurePosixPath(name).parts) != 4
                or not name.startswith('/etc/') or str(PurePosixPath(name)) != name
                or any(part in ('.', '..') for part in PurePosixPath(name).parts)
                or any(c in name for c in '\0\r\n,') or len(name) > 512):
            raise ValueError('sealed configuration file must name one /etc component file')
        if str(PurePosixPath(name).parent) in sealed:
            raise ValueError('sealed sandbox paths overlap')
    if len(set(sealed_files)) != len(sealed_files):
        raise ValueError('sealed sandbox files repeat')
    return ({'files': dict(sorted(files.items())), 'sealed_dirs': sorted(sealed),
             **({'sealed_files': sorted(sealed_files)} if sealed_files else {}),
             **({'directories': sorted(directories)} if directories else {})}
            if files or sealed or sealed_files or directories else None)


def project_inputs(value):
    """Worker proof carries hashes, not another copy of large source blobs."""
    value = normalize_inputs(value)
    if value is None:
        return None
    files = {}
    for name, content in value['files'].items():
        if isinstance(content, dict) and 'base64' in content:
            raw = base64.b64decode(content['base64'])
            files[name] = {'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw),
                           'executable': content['executable']}
        else:
            files[name] = content
    return {**value, 'files': files}


def _snapshot_file(path, listed, remaining):
    # Opening a file can refresh cached path attributes on network filesystems.
    # Compare content stability on one opened inode, not against a pre-open mtime.
    identity = ('st_dev', 'st_ino', 'st_mode')
    stable = (*identity, 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or any(
                getattr(before, key) != getattr(listed, key) for key in identity):
            raise ValueError('source changed during snapshot')
        if before.st_size > remaining:
            raise ValueError('sandbox input bytes exceed capacity')
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, remaining - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > remaining:
                raise ValueError('sandbox input bytes exceed capacity')
            chunks.append(chunk)
        after, current = os.fstat(fd), path.lstat()
        if (size != before.st_size
                or any(getattr(after, key) != getattr(before, key) for key in stable)
                or any(getattr(current, key) != getattr(after, key) for key in identity)):
            raise ValueError('source changed during snapshot')
        return b''.join(chunks), before.st_mode
    finally:
        os.close(fd)


def snapshot_directory(root, destination='/tmp/inputs/repository', *, exclude=()):
    """Freeze regular files and contained relative links, including Git bytes."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('snapshot requires a regular source directory')
    excluded = tuple(PurePosixPath(name) for name in exclude)
    if any(path.is_absolute() or '..' in path.parts or str(path) in ('', '.') for path in excluded):
        raise ValueError('snapshot exclusions require relative contained paths')
    def omitted(path):
        relative = PurePosixPath(path.relative_to(root).as_posix())
        return any(relative == item or item in relative.parents for item in excluded)
    files, directories, total = {}, [], 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        directory = Path(directory)
        dirs[:] = [name for name in dirs if not omitted(directory / name)]
        links = [name for name in dirs if (directory / name).is_symlink()]
        dirs[:] = [name for name in dirs if name not in links]
        if not dirs and not any(not omitted(directory / name) for name in [*names, *links]):
            relative = directory.relative_to(root).as_posix()
            directories.append(destination if relative == '.' else destination + '/' + relative)
        for name in sorted([*names, *links]):
            path = directory / name
            if omitted(path):
                continue
            target = destination + '/' + path.relative_to(root).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                link = os.readlink(path)
                if not path.resolve().is_relative_to(root.resolve()) or omitted(path.resolve()):
                    raise ValueError('source snapshot link escapes its root')
                files[target] = {'link': link}
            elif stat.S_ISREG(info.st_mode):
                data, mode = _snapshot_file(path, info, MAX_SNAPSHOT_BYTES - total)
                total += len(data)
                files[target] = {'base64': base64.b64encode(data).decode('ascii'),
                                 'executable': bool(mode & 0o111)}
            else:
                raise ValueError('snapshot contains a special file')
    return normalize_inputs({'files': files, 'sealed_dirs': [], 'directories': directories})


def merge_inputs(*values):
    result = {'files': {}, 'sealed_dirs': [], 'sealed_files': [], 'directories': []}
    for value in values:
        value = normalize_inputs(value)
        if value is None:
            continue
        for name, content in value['files'].items():
            if name in result['files'] and result['files'][name] != content:
                raise ValueError('frozen input owners conflict')
            result['files'][name] = content
        for key in ('sealed_dirs', 'sealed_files', 'directories'):
            result[key] = sorted(set(result[key]) | set(value.get(key, [])))
    return normalize_inputs(result)


def _directory(path):
    try:
        path.mkdir(mode=0o755)
        path.chmod(0o755)
    except FileExistsError:
        pass
    value = path.lstat()
    if not stat.S_ISDIR(value.st_mode) or value.st_uid != os.getuid() or value.st_mode & 0o022:
        raise ValueError('frozen sandbox staging must be an owned directory')


def stage_inputs(root, value):
    """Publish bytes once; return generic read-only bind mounts."""
    value = normalize_inputs(value)
    if value is None:
        return []
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    value_root = root.lstat()
    if not stat.S_ISDIR(value_root.st_mode) or value_root.st_uid != os.getuid() or value_root.st_mode & 0o022:
        raise ValueError('frozen sandbox inputs require an owned execution directory')
    immutable_write_bytes(root / 'frozen-inputs.json', json.dumps(value, sort_keys=True).encode(), exist_ok=True)
    directory, empty = root / 'input-files', root / 'empty-config'
    _directory(directory)
    _directory(empty)
    if any(empty.iterdir()):
        raise ValueError('sealed sandbox configuration staging is not empty')
    for name in value.get('directories', []):
        parent = directory
        for part in PurePosixPath(name).relative_to(INPUT_DIRECTORY).parts:
            parent /= part
            _directory(parent)
    for name, content in value['files'].items():
        path = directory / PurePosixPath(name).relative_to(INPUT_DIRECTORY)
        parents = list(path.parent.relative_to(directory).parts)
        parent = directory
        for part in parents:
            parent /= part
            _directory(parent)
        if isinstance(content, dict) and 'link' in content:
            if path.is_symlink() and os.readlink(path) == content['link']:
                continue
            path.symlink_to(content['link'])
        else:
            if path.is_symlink():
                raise ValueError('frozen sandbox input cannot be a symlink')
            raw = content.encode('utf-8') if isinstance(content, str) else base64.b64decode(content['base64'])
            executable = isinstance(content, dict) and content['executable']
            immutable_write_bytes(path, raw, mode=0o555 if executable else 0o444, exist_ok=True)
    mounts = [(directory, INPUT_DIRECTORY)] if value['files'] or value.get('directories') else []
    mounts += [(empty, name) for name in value['sealed_dirs']]
    if value.get('sealed_files'):
        blank = root / 'empty-config-file'
        if blank.is_symlink():
            raise ValueError('sealed sandbox file staging cannot be a symlink')
        immutable_write_bytes(blank, b'', mode=0o444, exist_ok=True)
        mounts += [(blank, name) for name in value['sealed_files']]
    return mounts


def verify_inputs(value):
    """The worker verifies projected bytes before launching the native command."""
    value = normalize_inputs(value, projection=True)
    if value is None:
        return
    for name in value.get('directories', []):
        path = Path(name)
        if path.is_symlink() or not path.is_dir():
            raise ValueError('projected sandbox directory differs from its frozen request')
    for name, content in value['files'].items():
        path = Path(name)
        if isinstance(content, dict) and 'link' in content:
            if not path.is_symlink() or os.readlink(path) != content['link']:
                raise ValueError('projected sandbox link differs from its frozen request')
            continue
        if path.is_symlink():
            raise ValueError('projected sandbox input differs from its frozen request')
        if isinstance(content, str):
            valid = path.read_bytes() == content.encode('utf-8')
        else:
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            mode = path.stat().st_mode
            valid = (digest == content['sha256'] and path.stat().st_size == content['size']
                     and bool(mode & 0o111) == content['executable'] and not mode & 0o222)
        if not valid:
            raise ValueError('projected sandbox input differs from its frozen request')
    for name in value['sealed_dirs']:
        path = Path(name)
        if path.is_symlink() or not path.is_dir() or any(path.iterdir()):
            raise ValueError('sandbox configuration directory is not sealed')
    for name in value.get('sealed_files', []):
        path = Path(name)
        if path.is_symlink() or not path.is_file() or path.read_bytes() != b'':
            raise ValueError('sandbox configuration file is not sealed')
