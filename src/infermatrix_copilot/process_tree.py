"""Owned process identity, descendant discovery and termination.

Execution mechanisms share these primitives; domain policies choose when an
owned process must stop and how its outcome is interpreted.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Callable
import threading
from pathlib import Path


def _log(msg: str) -> None:
    print(f"[kill_test_tree] {msg}", flush=True)


def _pgrep(args: list[str]) -> list[int]:
    try:
        out = subprocess.run(["pgrep", *args], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(x) for x in out.split() if x.isdigit()]


def collect_descendants(pid: int,
                        children_of: Callable[[int], list[int]] | None = None
                        ) -> list[int]:
    """BFS over the child tree rooted at `pid` (inclusive), deduplicated."""
    children_of = children_of or (_proc_children if Path("/proc/self/stat").exists()
                                  else lambda p: _pgrep(["-P", str(p)]))
    tree, queue = {pid}, [pid]
    while queue:
        for child in children_of(queue.pop(0)):
            if child not in tree:
                tree.add(child)
                queue.append(child)
    return sorted(tree)


def _proc_children(pid: int) -> list[int]:
    """Read every thread's child list without spawning inspection subprocesses.

    Deadline cleanup must not wait for a ``pgrep`` command. A disappeared or
    unreadable task adds no guessed PIDs; previously verified identities remain
    in the owner snapshot. Non-procfs platforms keep the legacy discovery path.
    """
    children = set()
    try:
        tasks = list((Path("/proc") / str(pid) / "task").iterdir())
    except OSError:
        return []
    for task in tasks:
        try:
            children.update(int(p) for p in (task / "children").read_text().split()
                            if p.isdigit())
        except OSError:
            pass
    return sorted(children)


def _is_ancestor_of_self(pid: int) -> bool:
    """Walk our own parent chain via /proc/<pid>/stat — killing an ancestor
    would take this process down with the target."""
    current = os.getpid()
    while current > 1:
        if current == pid:
            return True
        ids = _proc_stat_ids(current)
        if ids is None:
            return False
        current = ids[0]
    return False


def _alive(pid: int) -> bool:
    status = _proc_status(pid)
    if status is not None and status[0] in {"Z", "X"}:
        # A zombie still has a PID until its parent reaps it, but has no work
        # left to terminate. kill(pid, 0) alone cannot distinguish that state.
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _proc_status(pid: int) -> tuple[str, int, int] | None:
    """Read state, parent and birth identity atomically from Linux procfs."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as stream:
            stat = stream.read().decode(errors="replace")
        # comm (field 2) may contain spaces/parens; split after the LAST ")".
        # Fields after it start at field 3; ppid is field 4 → index 1,
        # starttime is field 22 → index 19.
        after = stat.rsplit(")", 1)[1].split()
        return after[0], int(after[1]), int(after[19])
    except (OSError, ValueError, IndexError):
        return None


def _proc_stat_ids(pid: int) -> tuple[int, int] | None:
    """(ppid, starttime) from one canonical stat read, or None if unreadable.

    The birth identity and parent must come from the same observation for
    ancestry verification and PID reuse protection.
    """
    status = _proc_status(pid)
    return None if status is None else status[1:]


def _start_time(pid: int) -> int | None:
    ids = _proc_stat_ids(pid)
    return None if ids is None else ids[1]


def kill_tree(pids: list[int], *, term_grace: float = 2.0,
              kill_grace: float = 1.0,
              kill: Callable[[int, int], None] | None = None,
              sleep: Callable[[float], None] = time.sleep,
              identity: dict[int, int | None] | None = None,
              log: Callable[[str], None] = _log) -> list[int]:
    """SIGTERM every pid and its descendants, wait `term_grace`, SIGKILL the
    survivors, wait `kill_grace`. Returns pids still alive (ideally empty).

    `identity` optionally maps pid → the /proc starttime recorded when the pid
    was *observed* (e.g. by the runner's descendant-snapshot thread). A pid
    whose current starttime no longer matches was reused by an unrelated
    process and is silently dropped — this is what makes accumulate-only
    snapshots safe to kill from."""
    kill = kill or (lambda p, s: os.kill(p, s))
    provided = dict(identity or {})
    # a reused ROOT must be rejected before walking: its children belong to
    # the unrelated new holder and carry no recorded identity of their own
    roots = []
    for p in dict.fromkeys(pids):
        recorded = provided.get(p)
        if recorded is not None:
            born_now = _start_time(p)
            if born_now is not None and born_now != recorded:
                continue
        roots.append(p)
    candidates = sorted({d for p in roots for d in collect_descendants(p)})

    # a root can pass the pre-walk check, exit, and be reused DURING the
    # walk — its stranger children would enter as identity-less candidates.
    # So walked-up candidates are accepted only on an ancestry chain anchored
    # at a root whose identity holds AFTER the walk (re-read here); pids with
    # their own recorded identity are exempt (the per-pid baseline below
    # drops reused ones). Legacy roots without recorded identity keep the
    # historical behavior. The residual window is the stat-read-to-signal
    # gap, irreducible without pidfds.
    info = {p: _proc_stat_ids(p) for p in candidates}
    # `chain` holds ancestry ANCHORS: legacy roots (no identity knowledge —
    # historical behavior) and pids whose recorded identity matches their
    # post-walk stat read. A merely-recorded pid is still a TARGET (the
    # per-pid baseline below drops it if reused) but must not anchor
    # children without this re-verification.
    chain: set[int] = {r for r in roots if provided.get(r) is None}
    for p in candidates:
        rec = provided.get(p)
        if rec is not None and info.get(p) is not None and info[p][1] == rec:
            chain.add(p)
    changed = True
    while changed:
        changed = False
        for p in candidates:
            if p in chain or p in provided:
                continue
            ids = info.get(p)
            if ids is not None and ids[0] in chain:
                chain.add(p)
                changed = True
    candidates = [p for p in candidates if p in chain or p in provided]
    # capture identity BEFORE signalling: after the grace sleep a target's pid
    # may have been reaped and reused, and escalating SIGKILL by bare pid would
    # then hit an unrelated process
    baseline: dict[int, int | None] = {}
    targets = []
    for p in candidates:
        born_now = _start_time(p)
        recorded = provided.get(p)
        if recorded is not None and born_now is not None and born_now != recorded:
            continue  # pid reused since it was recorded — not our process
        baseline[p] = recorded if recorded is not None else born_now
        targets.append(p)
    if not targets:
        return []

    def same_process(pid: int) -> bool:
        born = _start_time(pid)
        if born is None or baseline[pid] is None:
            return _alive(pid)  # identity unverifiable — legacy pid-only check
        return born == baseline[pid]

    log(f"Killing {len(targets)} process(es): {' '.join(map(str, targets))}")
    for pid in targets:
        try:
            kill(pid, signal.SIGTERM)
        except OSError:
            pass
    sleep(term_grace)
    survivors = [p for p in targets if _alive(p) and same_process(p)]
    if survivors:
        log(f"{len(survivors)} survivor(s) after SIGTERM, sending SIGKILL...")
        for pid in survivors:
            try:
                kill(pid, signal.SIGKILL)
            except OSError:
                pass
        sleep(kill_grace)
    still_alive = [p for p in targets if _alive(p) and same_process(p)]
    if still_alive:
        log(f"WARNING: {len(still_alive)} process(es) still alive: "
             f"{' '.join(map(str, still_alive))}")
    else:
        log("All processes terminated successfully.")
    return still_alive


def kill_by_pattern(pattern: str, **kw) -> list[int]:
    """`pgrep -f pattern`, excluding this process's own ancestor chain, then
    `kill_tree`. Returns survivors ([] also when nothing matched)."""
    matches = [p for p in _pgrep(["-f", pattern]) if not _is_ancestor_of_self(p)]
    if not matches:
        _log(f"No processes matching pattern: {pattern}")
        return []
    return kill_tree(matches, **kw)


def _record_walk(idmap: dict[int, int], walked: "list[int]", *,
                 root: int, root_parent: int, root_birth: int | None = None,
                 stat_ids: "Callable[[int], tuple[int, int] | None]" = None,
                 ) -> None:
    """Fold one descendant walk into the accumulate-only snapshot map.

    Identity is bound to DISCOVERY: a pid can exit and be reused between the
    pgrep walk and the /proc read, and recording the unrelated new holder's
    (perfectly valid) starttime would launder it past the kill-time identity
    check. One stat read yields (ppid, starttime) atomically, and a pid is
    recorded — or an existing record overwritten — only when it sits on a
    fully VALIDATED ancestry chain: the leader must be parented by the
    runner itself AND (once captured) match its immutable `root_birth` —
    the runner spawns many short-lived children (pgrep, git, the next job),
    so a recycled leader pid can wear the right ppid and only the birth
    time distinguishes the stranger. Every other node's parent must itself
    be validated: merely appearing in the walked set is not enough — when
    an intermediate pid is reused, its stranger children still name a
    "walked" parent, and accepting them would launder the stranger's
    subtree. Unverifiable pids (died mid-walk, reparented in the gap) are
    skipped; earlier records for them survive."""
    stat_ids = stat_ids or _proc_stat_ids
    info = {p: stat_ids(p) for p in dict.fromkeys(walked)}
    validated: set[int] = set()
    if (info.get(root) is not None and info[root][0] == root_parent
            and (root_birth is None or info[root][1] == root_birth)):
        validated.add(root)
    # fixpoint over parent links (walk order is not topological)
    changed = True
    while changed:
        changed = False
        for p, ids in info.items():
            if p in validated or ids is None:
                continue
            if ids[0] in validated:
                validated.add(p)
                changed = True
    for p in validated:
        idmap[p] = info[p][1]


def _kill_group(pgid: int, sig: int) -> None:
    """Signal the group captured when its owned leader was launched."""
    try:
        os.killpg(pgid, sig)
    except OSError:
        pass


def terminate_tree(pid: int, pgid: int, snapshot=None, *, group_kill=None,
                   term_grace=None, descendant_grace=0, wait=None) -> list[int]:
    """One group and identity-checked descendant termination mechanism.

    An unspecified grace preserves the legacy explicit-tree operation. Owned
    subprocess supervision supplies its grace and bounded wait. Discovery is
    performed only by ``kill_tree``; a walked PID is never promoted to a root.
    """
    signal_group = group_kill or _kill_group
    snapshot = dict(snapshot or {})
    survivors = []
    if term_grace is None:
        signal_group(pgid, signal.SIGTERM)
        if snapshot:
            survivors = kill_tree(sorted(snapshot), identity=snapshot) or []
    else:
        # Reaped leaders must not impose a grace sleep on every successful CLI
        # call. Recorded, still-owned descendants remain eligible after reparenting.
        if term_grace or descendant_grace:
            signal_group(pgid, signal.SIGTERM)
            if term_grace and wait is not None:
                try:
                    wait(term_grace)
                except subprocess.TimeoutExpired:
                    pass
        else:
            signal_group(pgid, signal.SIGKILL)
        live = {p: born for p, born in snapshot.items()
                if _start_time(p) == born and _alive(p)}
        if live:
            survivors = kill_tree(sorted(live), identity=live, term_grace=descendant_grace,
                                  kill_grace=1.0, log=lambda _: None) or []
    signal_group(pgid, signal.SIGKILL)
    return survivors


class OwnedProcessTree:
    """Accumulate only ancestry-verified PID/start-time identities for one run.

    The snapshot interval retains the existing bounded discovery window: a child
    created and orphaned between observations may escape per-PID discovery.
    The group remains owned independently; explicit external resources use the
    supervisor's cleanup hook rather than becoming guessed descendant PIDs.
    """
    def __init__(self, proc, interval: float):
        self.proc = proc
        self.interval = interval
        self.identity = {}
        self._parent = os.getpid()
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        _record_walk(self.identity, [proc.pid], root=proc.pid,
                     root_parent=self._parent)
        self._birth = self.identity.get(proc.pid)
        self._thread = threading.Thread(target=self._observe, daemon=True)

    def capture(self):
        # Without the original birth, a later same-parent process cannot be
        # proved to be the leader. Preserve group-only degradation in that case.
        if self._birth is not None and self.proc.poll() is None:
            walked = collect_descendants(self.proc.pid)
            with self._lock:
                _record_walk(self.identity, walked, root=self.proc.pid,
                             root_parent=self._parent, root_birth=self._birth)

    def start(self):
        self._thread.start()

    def _observe(self):
        while not self._stopped.is_set() and self.proc.poll() is None:
            self.capture()
            self._stopped.wait(self.interval)

    def stop_observing(self):
        """Stop observation without signalling children released by command policy."""
        self._stopped.set()
        if self._thread.ident is not None:
            self._thread.join(timeout=10)

    def terminate(self, grace, descendant_grace):
        self.capture()
        self.stop_observing()
        with self._lock:
            snapshot = dict(self.identity)
        survivors = terminate_tree(self.proc.pid, self.proc.pid, snapshot,
                                  term_grace=grace, descendant_grace=descendant_grace,
                                  wait=lambda seconds: self.proc.wait(timeout=seconds))
        # A newly discovered descendant can lack a retained birth identity.
        # Preserve that uncertainty rather than inventing recovery authority.
        return {pid: snapshot.get(pid) for pid in survivors}

