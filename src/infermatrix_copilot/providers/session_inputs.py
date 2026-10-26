"""Project a read-only agent bridge onto canonical frozen sandbox inputs."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import re

from ..sandbox.inputs import merge_inputs, snapshot_directory, normalize_inputs
from ..run_status import mutable_status_paths
from ..tool_bridge import load_bridge_spec, READ_ONLY_BRIDGE_TOOLS

SOURCE = '/tmp/inputs/repository'
RUN = '/tmp/inputs/run'
KNOWLEDGE = '/tmp/inputs/knowledge'
BRIDGE = '/tmp/inputs/bridge.json'
BRIDGE_PYTHON = '/usr/local/bin/python'


def snapshot_source(root, exclude=()):
    """Reuse byte snapshots, materializing a linked worktree's Git metadata.

    Config, hooks and remote credentials are not part of a source snapshot.
    Object alternates would require an unbound external object store and refuse.
    """
    root = Path(root)
    git = root / '.git'
    result = snapshot_directory(root, SOURCE, exclude=(*exclude, '.git'))
    if '.git' in exclude:
        return result
    if not git.exists():
        return result
    if git.is_symlink():
        raise ValueError('agent source Git directory cannot be a symlink')
    if git.is_file():
        pointer = git.read_text().strip()
        if not pointer.startswith('gitdir: '):
            raise ValueError('invalid linked Git worktree')
        git = (root / pointer[8:]).resolve()
    common = (git / (git / 'commondir').read_text().strip()).resolve() if (git / 'commondir').exists() else git
    if (common / 'objects/info/alternates').exists():
        raise ValueError('source Git object alternates are not a contained snapshot')
    metadata = snapshot_directory(common, SOURCE + '/.git',
        exclude=('config', 'config.worktree', 'hooks', 'logs', 'worktrees', 'HEAD', 'index', 'commondir'))
    # Snapshot the selected worktree's HEAD, not the common checkout's HEAD.
    selected = snapshot_directory(git, SOURCE + '/.git',
        exclude=tuple(path.name for path in git.iterdir() if path.name not in ('HEAD', 'index')))
    return merge_inputs(result, metadata, selected, {'files': {
        SOURCE + '/.git/config': '[core]\nrepositoryformatversion = 0\nbare = false\n'}, 'sealed_dirs': []})


def map_prompt_paths(value, roots):
    """Map only declared filesystem prefixes, leaving the other prompt bytes."""
    for original, destination in sorted(roots.items(), key=lambda item: len(str(item[0])), reverse=True):
        value = re.sub(re.escape(str(original)) + r'(?=/|\s|[\x22\x27`),:;]|$)',
                       lambda _: destination, value)
    return value


def freeze_session(request, *, knowledge_dir):
    """Return the same request with mapped paths and its immutable byte inputs.

    This is a read-only bridge capability. Writable/rebase sessions retain their
    explicit host contract until a writable sandbox policy is implemented.
    """
    scope = request.scope
    if not scope.read_only or scope.allowed_tools - READ_ONLY_BRIDGE_TOOLS:
        raise ValueError('sandbox agent sessions require a supported read-only tool scope')
    if not scope.root or not Path(scope.root).is_absolute():
        raise ValueError('sandbox agent sessions require an absolute source root')
    source, run, knowledge = (Path(value).resolve() for value in (scope.root, request.run_dir, knowledge_dir))
    if source == run or source in run.parents or run in source.parents:
        raise ValueError('agent source and run state must have separate roots')
    roots = {source: SOURCE, run: RUN, knowledge: KNOWLEDGE}

    def mapped(value):
        path = Path(value).resolve()
        # A nested knowledge slice uses its more specific mapping.
        for original, destination in sorted(roots.items(), key=lambda item: len(str(item[0])), reverse=True):
            if path == original or original in path.parents:
                suffix = path.relative_to(original).as_posix()
                return destination if suffix == '.' else destination + '/' + suffix
        raise ValueError('agent scope contains an unmapped host path')

    deny = [Path(path).resolve() for path in scope.deny_prefixes]
    if any(root == path or path in root.parents for root in roots for path in deny):
        raise ValueError('agent scope denies an entire mapped snapshot')
    def exclusions(root):
        return tuple(path.relative_to(root).as_posix() for path in deny if root in path.parents)

    # The verification sidecar travels as opaque input bytes alongside the
    # knowledge tree. Domain readers remain responsible for interpreting it.
    manifest = knowledge.parent / 'MANIFEST.json'
    metadata = (snapshot_directory(knowledge.parent, '/tmp/inputs',
        exclude=tuple(path.name for path in knowledge.parent.iterdir()
                      if path.name != 'MANIFEST.json')) if manifest.is_file() else None)
    from ..execution_artifacts import NATIVE_SESSIONS_DIR
    inputs = merge_inputs(snapshot_source(source, exclusions(source)),
        snapshot_directory(run, RUN, exclude=(NATIVE_SESSIONS_DIR, 'bridge', 'run_trace.jsonl', 'trace.jsonl',
            'bridge_trace.jsonl', *mutable_status_paths(), *exclusions(run))),
        snapshot_directory(knowledge, KNOWLEDGE, exclude=exclusions(knowledge)), metadata)
    frozen_scope = replace(scope, root=SOURCE, path_scope=None,
        read_roots=tuple(mapped(path) for path in scope.read_roots),
        deny_prefixes=tuple(mapped(path) for path in scope.deny_prefixes))
    bridge_path = None
    if request.bridge_spec_path is not None:
        restored, spec = load_bridge_spec(request.bridge_spec_path)
        if restored != scope or Path(spec['run_dir']).resolve() != run or spec.get('rebase'):
            raise ValueError('agent bridge differs from the requested read-only scope')
        if spec.get('knowledge_dir') and Path(spec['knowledge_dir']).resolve() != knowledge:
            raise ValueError('agent bridge knowledge differs from the selected snapshot')
        spec.update(scope={**asdict(frozen_scope), 'allowed_tools': sorted(frozen_scope.allowed_tools)},
            run_dir='/tmp/work/session', input_run_dir=RUN, knowledge_dir=KNOWLEDGE,
            trace_path='/tmp/work/session/bridge_trace.jsonl',
            trace_store_root='/tmp/work/session/traces' if spec.get('trace_store_root') else '')
        inputs = merge_inputs(inputs, {'files': {BRIDGE: json.dumps(spec, sort_keys=True)}, 'sealed_dirs': []})
        bridge_path = Path(BRIDGE)
    denied = tuple(mapped(path) for path in deny)
    inputs = normalize_inputs({**inputs, 'files': {name: value for name, value in inputs['files'].items()
        if not any(name == path or name.startswith(path + '/') for path in denied)}})

    return replace(request, system=map_prompt_paths(request.system, roots), prompt=map_prompt_paths(request.prompt, roots), scope=frozen_scope,
                   run_dir=Path('/tmp/work/session'), bridge_spec_path=bridge_path, frozen_inputs=inputs)
