"""The machinery ``kb init`` stages share (design kb-init §2, §9, §10).

* ``InitRuntime`` — init's own runtime. It never builds ``KbRuntime`` (that
  opens the service ledger ``kb.db``) and touches no service state: its
  traces, upstream mirrors, knowledge clone and records all live under
  ``<state_dir>/init/``.
* ``InitRecord`` — the init record of one stage (design §2): the pin, seed
  provenance, per-rule evidence, judge verdicts, the checklist and the PR.
  Pages stay pure knowledge format 2; everything init knows beyond them is
  here and in the PR body.
* ``UpstreamPin`` — a bare mirror of the upstream, the pinned commit, a
  read-only export of its tree and a ``PinnedObserver`` on it.
* ``generate`` / ``judge`` — every model call, reserved against the stage
  budget first (``init_budget``).
* ``validate_change`` — the deterministic checks of design §9.3.
* ``InitPublisher`` — one commit on an exact base, pushed to a branch that
  must not exist yet, and a PR through the owner's ``gh`` login; or, in a
  dry run, the files and PR body written locally.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from ..knowledge_service.facts import FactsError
from ..knowledge_service.pinned_claims import Evidence, PinnedObserver, check_evidence, check_rules
from .init_budget import Budget, Price, generator_reservation, load_prices, price_for
from .models import ModelGateway, ModelReply, ModelRole, ModelUnavailable

INIT_DIR = "init"
STAGES = ("skeleton", "feature-discovery", "modules", "knowledge", "deepen", "pr-history", "harvest-calibration")
INDEPENDENT_STAGES = ("knowledge-deepen",)
KNOWLEDGE_PREFIX = "knowledge/"
ALLOW_PUSH_ENV = "ALLOW_PUSH"
ALLOW_POST_ENV = "ALLOW_POST"
AUTHOR_ENV = "KB_INIT_GIT_AUTHOR"
CLONE_ENV = "KB_INIT_KNOWLEDGE_CLONE"
_AUTHOR = re.compile(r"(?P<name>[^<>]+?)\s*<(?P<email>[^<>\s]+@[^<>\s]+)>")
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
# what an init PR may write, repository-relative
INIT_PATHS = (
    re.compile(r"knowledge/(?:repos/[A-Za-z0-9._-]+|general)/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+\.(?:md|yaml)"),
    re.compile(r"knowledge/repos/_index\.md"),   # the shared list a new repository is added to
    re.compile(r"adapters/[A-Za-z0-9_-]+/manifest\.yaml"),
    re.compile(r"adapters/[A-Za-z0-9_-]+/kb-calibration/cases/[A-Za-z0-9._-]+\.json"),
)
# These paths need an exact publication scope, never a generic init permission.
DISCOVERY_PATHS = (
    re.compile(r"adapters/[A-Za-z0-9_-]+/knowledge-coverage\.yaml"),
    re.compile(r"eval/feature-discovery/[A-Za-z0-9._-]+-[0-9a-f]{12}\.json"),
)
MAX_DOC_FILES = 40
MAX_DOC_BYTES = 200_000
VALIDATOR_TIMEOUT_S = 300


class InitError(RuntimeError):
    """The stage cannot run as configured, or its result failed a check."""


# -- records ---------------------------------------------------------------------

@dataclass
class InitRecord:
    stage: str
    repo: str
    pin: str = ""
    kb_base_sha: str = ""
    inputs_digest: str = ""
    started_at: float = 0.0
    status: str = "started"          # started | blocked | dry_run | published
    dry_run: bool = True
    spent_usd: float = 0.0
    seeds: list[dict] = field(default_factory=list)       # {origin, kb_sha, new_page, new_rule_ids}
    evidence: dict[str, list[dict]] = field(default_factory=dict)   # rule_id -> [Evidence.to_dict()]
    verdicts: dict[str, dict] = field(default_factory=dict)         # rule_id -> {verdict, reasons, model, text_sha}
    dropped: list[dict] = field(default_factory=list)     # {rule_id, why}
    checklist: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    unfinished: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)        # repository-relative paths the stage writes
    pr: dict = field(default_factory=dict)                # {number, head_sha, branch} or {dry_run_dir}
    notes: list[str] = field(default_factory=list)
    coverage: dict = field(default_factory=dict)          # the stage's coverage report (modules, deepen)
    history: dict = field(default_factory=dict)           # pr-history selection, checkpoints and commit plan
    review: dict = field(default_factory=dict)            # aggregate review, bound to the exact base/head
    depth: dict = field(default_factory=dict)             # independent semantic-depth checkpoints
    discovery: dict = field(default_factory=dict)         # feature discovery checkpoints and frozen catalog
    budget_calls: dict = field(default_factory=dict)      # original per-call accounting identities; legacy totals stay conservative
    native_completion_profiles: dict = field(default_factory=dict)  # host policy, independent of stage coverage reports

    @staticmethod
    def path(state_dir: Path, repo: str, stage: str) -> Path:
        return Path(state_dir) / INIT_DIR / repo / f"{stage}.json"

    def save(self, state_dir: Path) -> Path:
        path = self.path(state_dir, self.repo, self.stage)
        from ..persistence import atomic_write_bytes
        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        if not self.native_completion_profiles:
            data.pop('native_completion_profiles')
        atomic_write_bytes(path, json.dumps(data, ensure_ascii=False, indent=1,
                                            sort_keys=True).encode("utf-8"))
        return path

    @classmethod
    def load(cls, state_dir: Path, repo: str, stage: str) -> "InitRecord | None":
        path = cls.path(state_dir, repo, stage)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(**data)
        except (OSError, ValueError, TypeError) as exc:
            raise InitError(f"init record {path} is unreadable: {exc}") from exc


def checkpoint_budget(limit: float | None, record: InitRecord, state_dir: Path) -> Budget:
    """Use the init-record format for the shared budget's durable checkpoints."""
    if not isinstance(record.budget_calls, dict):
        raise ValueError("invalid init budget call journal")
    def checkpoint(spent: float, reserved: float) -> None:
        # A killed call's final cost is unknown; recovery charges its whole
        # outstanding reservation, without introducing a new record format.
        record.spent_usd = spent + reserved
        record.save(state_dir)

    return Budget(limit, spent_usd=record.spent_usd, checkpoint=checkpoint, calls=record.budget_calls)


def inputs_digest(**parts: Any) -> str:
    """A stable digest of everything a stage's output depends on."""
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# -- the upstream at a pin -------------------------------------------------------

def _run(cmd: list[str], *, cwd: Path | None = None, input: bytes | None = None,
         env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, cwd=cwd, input=input, capture_output=True, check=False,
                              env=None if env is None else {**os.environ, **env})
    except OSError as exc:
        raise InitError(f"{cmd[0]} unavailable: {exc}") from exc


class UpstreamPin:
    """A bare mirror of ``full_name`` under the init state directory."""

    def __init__(self, path: Path, full_name: str, *, remote: str | None = None):
        self.path = Path(path)
        self.full_name = full_name
        self.remote = remote or f"https://github.com/{full_name}.git"

    def _git(self, *args: str) -> bytes:
        proc = _run(["git", "--git-dir", str(self.path), *args])
        if proc.returncode != 0:
            raise InitError(f"git {args[0]} on the {self.full_name} mirror failed: "
                            f"{proc.stderr.decode(errors='replace')[:400]}")
        return proc.stdout

    def sync(self) -> None:
        if not (self.path / "HEAD").is_file():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            proc = _run(["git", "clone", "--quiet", "--bare", self.remote, str(self.path)])
            if proc.returncode != 0:
                raise InitError(f"cannot mirror {self.full_name}: {proc.stderr.decode(errors='replace')[:400]}")
        self._git("fetch", "--quiet", "--prune", "origin", "+refs/heads/*:refs/heads/*")
        # the mirror's HEAD follows the upstream's CURRENT default branch (a
        # fetch never moves a symbolic HEAD, and the default can change)
        listing = self._git("ls-remote", "--symref", "origin", "HEAD").decode(errors="replace")
        match = re.search(r"^ref:\s+(refs/heads/\S+)\s+HEAD$", listing, re.MULTILINE)
        if match is None:
            # A local source can intentionally have a detached HEAD and no origin.
            # Fetch that exact object; do not fabricate a default branch.
            self._git("fetch", "--quiet", "origin", "+HEAD:refs/kb/source-head")
            self._git("update-ref", "--no-deref", "HEAD", self._git("rev-parse", "refs/kb/source-head").decode().strip())
        else:
            self._git("symbolic-ref", "HEAD", match.group(1))

    def resolve(self, ref: str) -> str:
        sha = self._git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").decode().strip()
        if not _SHA.fullmatch(sha):
            raise InitError(f"{ref!r} is not a commit of {self.full_name}")
        return sha

    def export(self, pin: str, dest: Path) -> Path:
        """Materialize exact committed blobs, without Git archive attributes."""
        from .git_source import GitSource
        from .sources import SourceError
        try:
            return GitSource(self.path).export(pin, dest)
        except SourceError as exc:
            raise InitError(str(exc)) from exc

    def observer(self, pin: str, *, pull=None) -> PinnedObserver:
        return PinnedObserver(self.path, self.full_name, pin, pull=pull)

    def first_parent_changes(self, pin: str, *, count: int, max_age_days: int) -> list[list[str]]:
        """The files each of the last ``count`` first-parent commits up to
        ``pin`` changed (a merge commit against its first parent), no older
        than ``max_age_days`` before the PIN's commit time — so the window is
        a function of the pin, never of the clock. On a merge- or
        squash-based default branch each first-parent commit is one merged
        PR (design §7.3's PR window, read from the mirror, offline)."""
        if count <= 0:
            return []
        when = int(self._git("show", "-s", "--format=%ct", pin).decode().strip() or 0)
        since = when - max_age_days * 86400
        # -z: every path NUL-terminated and never quoted (non-ASCII names stay
        # names); \x01 opens each commit's "<sha> <commit time>" header
        out = self._git("log", "-z", "--first-parent", "-m", "--name-only", "--no-renames",
                        "--format=%x01%H %ct", f"--max-count={count}", pin)
        window: list[list[str]] = []
        for chunk in out.split(b"\x01")[1:]:
            fields = chunk.split(b"\0")
            _, _, stamp = fields[0].decode("ascii", "replace").partition(" ")
            if int(stamp.strip() or 0) < since:
                continue       # commit times need not decrease along first parents: skip, never stop
            paths = {f.lstrip(b"\n").decode("utf-8", "replace") for f in fields[1:]}
            window.append(sorted(p for p in paths if p))
        return window


def collect_docs(root: Path, globs: tuple[str, ...], *, max_files: int = MAX_DOC_FILES,
                 max_bytes: int = MAX_DOC_BYTES) -> list[tuple[str, str]]:
    """(repo-relative path, text) of the docs ``globs`` name, bounded."""
    from ..profiles.establish import doc_files

    out: list[tuple[str, str]] = []
    total = 0
    for path in doc_files(Path(root), globs)[:max_files]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        size = len(text.encode("utf-8"))
        if total + size > max_bytes:
            text = text.encode("utf-8")[: max(0, max_bytes - total)].decode("utf-8", "ignore")
            size = len(text.encode("utf-8"))
        if not text:
            break
        out.append((path.relative_to(root).as_posix(), text))
        total += size
    return out


# -- the runtime ------------------------------------------------------------------

@dataclass
class InitRuntime:
    settings: Any = field(repr=False)
    state_dir: Path
    registry: dict
    gateway: ModelGateway
    generator: ModelRole
    judge: ModelRole
    knowledge: Any                    # sources.KnowledgeRepo
    github: Any                       # sources.GitHubReader
    prices: dict[str, Price] = field(default_factory=load_prices)
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ), repr=False)
    clock: Callable[[], float] = time.time
    gh_run: Callable[..., subprocess.CompletedProcess] = subprocess.run
    upstream_remote: Callable[[str], str] | None = None   # full_name -> clone URL (tests)
    pull: Callable[[str, int], dict] | None = None        # PR lookup for pinned claims (tests)
    subscription_generator: bool = False
    unlimited_subscription: bool = False
    discovery_concurrency: int = 13
    portable_spec: Any = None
    discovery_paths: tuple[str, ...] | None = None

    @property
    def init_dir(self) -> Path:
        return Path(self.state_dir) / INIT_DIR

    def upstream(self, repo: str, full_name: str) -> UpstreamPin:
        remote = self.upstream_remote(full_name) if self.upstream_remote else None
        return UpstreamPin(self.init_dir / "upstream" / f"{repo}.git", full_name, remote=remote)

    def today(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(self.clock()))

    @classmethod
    def from_env(cls, settings, *, state_dir: Path) -> "InitRuntime":
        from ..resources import adapters_root
        from ..trace_store import TraceStore
        from .config import load_registry
        from .models import roles_from_env
        from .runtime import trace_recorder
        from .sources import GitHubReader, KnowledgeRepo

        state_dir = Path(state_dir).expanduser()
        generator, judge = roles_from_env()
        traces = TraceStore(state_dir / INIT_DIR / "traces")
        clone = Path(os.environ.get(CLONE_ENV) or state_dir / INIT_DIR / "knowledge-repo")
        ensure_knowledge_clone(clone)
        from .model_dispatch import SharedModelDispatch

        gateway = ModelGateway(settings, recorder=trace_recorder(traces))
        gateway.configure_dispatch(SharedModelDispatch())
        return cls(
            settings=settings, state_dir=state_dir,
            registry=load_registry(Path(os.environ.get("ADAPTERS_DIR") or adapters_root())),
            gateway=gateway,
            generator=generator, judge=judge,
            knowledge=KnowledgeRepo(clone), github=GitHubReader(),
        )


def execution_snapshot(rt, init=None) -> dict:
    """Portable original admission/model/accounting inputs, without credentials."""
    value = {
        "generator": asdict(rt.generator) if hasattr(rt, "generator") else None,
        "judge": asdict(rt.judge) if hasattr(rt, "judge") else None,
        "unlimited_subscription": rt.unlimited_subscription,
        "subscription_generator": getattr(rt, "subscription_generator", False),
        "prices": {name: asdict(price) for name, price in getattr(rt, "prices", {}).items()},
        "init": asdict(init) if init is not None else None,
        "zcode_reasoning_level": getattr(rt.gateway, "zcode_reasoning_level", None),
        "native_completion_profiles": getattr(rt.gateway, "native_completion_profiles", {}),
    }
    return json.loads(json.dumps(value, ensure_ascii=False))


def restore_execution(rt, snapshot):
    """Restore frozen settings on a detached runtime; never create a gateway."""
    from copy import copy
    from .config import InitConfig

    required = {"generator", "judge", "prices", "init", "subscription_generator",
                "unlimited_subscription", "zcode_reasoning_level"}
    if not required.issubset(snapshot) or snapshot["init"] is None:
        raise InitError("original init execution settings are unavailable")
    def role(value):
        return ModelRole(**{**value, "fallback": role(value["fallback"]) if value.get("fallback") else None})
    restored = copy(rt)
    restored.generator, restored.judge = role(snapshot["generator"]), role(snapshot["judge"])
    restored.subscription_generator = snapshot["subscription_generator"]
    restored.unlimited_subscription = snapshot["unlimited_subscription"]
    restored.prices = {name: Price(**price) for name, price in snapshot["prices"].items()}
    return restored, InitConfig(**snapshot["init"])


def ensure_knowledge_clone(path: Path, *, repository: str | None = None) -> None:
    """A non-bare, no-checkout clone of the knowledge repository at ``path``
    (``KnowledgeRepo`` reads ``origin/main`` from it)."""
    from .merge import knowledge_repository

    path = Path(path)
    if (path / ".git").exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    remote = f"https://github.com/{repository or knowledge_repository()}.git"
    proc = _run(["git", "clone", "--quiet", "--no-checkout", remote, str(path)])
    if proc.returncode != 0:
        raise InitError(f"cannot clone the knowledge repository: {proc.stderr.decode(errors='replace')[:400]}")


# -- model calls ------------------------------------------------------------------

def generate(rt: InitRuntime, budget: Budget, init, *, system: str, prompt: str,
             validate: Callable[[dict], None] | None = None, record_payload: bool = True) -> ModelReply:
    """One generator call, reserved first: threshold + one worst-case request.
    Raises ``PriceError`` (no price: nothing is dispatched), ``BudgetExhausted``
    or ``ModelUnavailable``."""
    if rt.subscription_generator or rt.unlimited_subscription:
        if rt.unlimited_subscription and rt.generator.fallback is not None:
            raise ModelUnavailable("unlimited subscription generation must not configure a fallback", allow_fallback=False)
        if not rt.gateway.subscription_billing(rt.generator):
            raise ModelUnavailable("subscription generator needs an authenticated subscription backend", allow_fallback=False)
        # No API spend threshold applies to this explicitly selected coding
        # subscription. Preserve unreported USD and token usage; subscription
        # fees are outside the stage's separate judge USD accounting.
        return rt.gateway.call_json(rt.generator, system=system, prompt=prompt, validate=validate,
                                    **({"record_payload": False} if not record_payload else {}))
    price = price_for(rt.prices, rt.generator.model)
    input_bytes = len(system.encode("utf-8")) + len(prompt.encode("utf-8")) + init.harness_overhead_bytes
    amount = generator_reservation(price, init.generator_call_usd, input_bytes)
    with budget.reserve(amount) as reservation:
        reply = rt.gateway.call_json(rt.generator, system=system, prompt=prompt, validate=validate,
                                     max_budget_usd=init.generator_call_usd,
                                     **({"record_payload": False} if not record_payload else {}))
        reservation.charge(reply.cost_usd)
    return reply


def judge(rt: InitRuntime, budget: Budget, init, block, *, base: Mapping[str, str],
          head: Mapping[str, str], evidence: list[dict], record: InitRecord | None = None):
    """The advisory L2 verdict for one rule block (``gate.judge_block``), at a
    fixed accounted cost of ``judge_call_usd``. An unavailable judge comes back
    as verdict ``human`` with ``reasons["model"]`` set."""
    from .gate import BlockVerdict, judge_block

    from .quality_review import QualityPolicy, checkpoint_review

    def run():
        return judge_block(block, base=dict(base), head=dict(head), evidence=evidence,
                           gateway=rt.gateway, judge=rt.judge, budget=budget, charge_usd=init.judge_call_usd,
                           quality_policy=QualityPolicy("init-block", subscription=rt.unlimited_subscription,
                                                        reject_fallback=rt.unlimited_subscription))

    if record is None:
        return run()  # Foundation's worker task owns its encompassing checkpoint.
    identity = inputs_digest(block=block.to_dict(), base=base, head=head, evidence=evidence,
                             judge=rt.judge.label(), policy="init-block-v1",
                             subscription=rt.unlimited_subscription)
    state = record.coverage.setdefault("quality_reviews", {}).setdefault(identity, {})
    binding = {"input_sha256": identity, "block": block.to_dict(),
               "requested": rt.judge.label(), "role": rt.judge.name}
    cached = state.get("checked")
    if isinstance(cached, dict) and cached.get("input_sha256") == identity and "requested" not in cached:
        # Completed legacy judgments remain reusable under their original full
        # input digest. Never invent a new dispatch identity for an old receipt.
        # The shared checkpoint validator still checks any retained binding.
        binding = {"input_sha256": identity}
    result = checkpoint_review(state, lambda: run().to_dict(), budget.checkpoint_now,
                               binding=binding)
    return BlockVerdict(block, result["verdict"], result.get("dimensions", {}), result.get("reasons", {}),
                        result.get("model", ""), result.get("quality_receipt", {}))


def classify_verdict(verdict) -> str:
    """pass | fail | unsure | unjudged (design §8)."""
    if verdict.verdict == "human":
        return "unjudged" if "model" in (verdict.reasons or {}) else "unsure"
    return verdict.verdict


# -- deterministic checks (design §9.3) --------------------------------------------

def knowledge_changes(base: Mapping[str, str], head: Mapping[str, str]):
    from ..knowledge_service.l1 import Change

    changes = []
    for path in sorted(set(base) | set(head)):
        if base.get(path) == head.get(path):
            continue
        status = "A" if path not in base else "D" if path not in head else "M"
        changes.append(Change(KNOWLEDGE_PREFIX + path, status, "100644" if status != "A" else "",
                              "100644" if status != "D" else ""))
    return changes


def claim_problems(observer: PinnedObserver, rules: Mapping[str, str], evidence: list[Evidence]) -> list[str]:
    """Design §9.1: the rules' claims and their evidence at the pin."""
    try:
        return ([f"claim {p}" for p in check_rules(rules, observer)]
                + [f"evidence {p}" for p in check_evidence(evidence, observer)])
    except FactsError as exc:
        return [f"the upstream could not be read at the pin: {exc}"]


def other_path_problems(other: Mapping[str, tuple[str | None, str | None]],
                        check_other: Callable[[str, str | None, str | None], list[str]] | None) -> list[str]:
    """Paths outside ``knowledge/`` an init change writes: each must be one
    init may write, and each gets its own check (design §9.3)."""
    problems: list[str] = []
    for path, (before, after) in sorted(other.items()):
        if path.startswith(KNOWLEDGE_PREFIX) or not any(p.fullmatch(path) for p in INIT_PATHS + DISCOVERY_PATHS):
            problems.append(f"init may not write {path}")
        elif check_other is None:
            problems.append(f"no check for {path}")
        else:
            problems += check_other(path, before, after)
    return problems


def run_knowledge_validators(knowledge, base_sha: str, files: Mapping[str, str | None]) -> list[str]:
    """``knowledge/tools/check_knowledge_tree.py`` and ``check_wiki_lint.py``
    (what CI runs) on the base tree with ``files`` (repository-relative path ->
    text, None = deleted) applied. Problems are the tools' error lines."""
    from ..knowledge_service.validation import validate_snapshot

    return validate_snapshot(knowledge.path, base_sha, files,
                             timeout_seconds=VALIDATOR_TIMEOUT_S).problems()


# -- publishing ---------------------------------------------------------------------

def parse_author(value: str) -> tuple[str, str]:
    match = _AUTHOR.fullmatch((value or "").strip())
    if not match:
        raise InitError(f"{AUTHOR_ENV} must be 'Name <email>' to publish")
    return match.group("name"), match.group("email")


@dataclass
class InitPublisher:
    """Writes one init change: a PR through the owner's ``gh`` login, or (dry
    run) the files and PR body under ``dry_run_dir``."""

    clone: Path                      # the knowledge repository clone (KnowledgeRepo.path)
    repository: str                  # owner/name of the knowledge repository
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run
    # A discovery publication may write only these two exact repo-relative files.
    allowed_paths: tuple[str, ...] = ()

    def _git(self, *args: str, env: Mapping[str, str] | None = None, input: bytes | None = None,
             auth: bool = False) -> str:
        prefix = ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"] if auth else []
        proc = self.run(["git", "-C", str(self.clone), *prefix, *args], input=input,
                        env={**os.environ, **(env or {})}, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        check=False, cwd=str(self.clone))
        if proc.returncode != 0:
            raise InitError(f"git {args[0]} failed: {proc.stderr.decode(errors='replace')[:300]}")
        return proc.stdout.decode().strip()

    def _gh(self, *args: str, input: str | None = None) -> str:
        proc = self.run(["gh", *args], input=None if input is None else input.encode(),
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, cwd=str(self.clone))
        if proc.returncode != 0:
            raise InitError(f"gh {' '.join(args[:3])} failed: {proc.stderr.decode(errors='replace')[:300]}")
        return proc.stdout.decode(errors="replace")

    @staticmethod
    def dry_run(dest: Path, files: Mapping[str, str | None], *, title: str, body: str) -> Path:
        """Replace ``dest`` with exactly this change: files under ``tree/``
        (a deletion as ``<name>.DELETED``) and ``PR_BODY.md``."""
        from .preview import file_hashes
        file_hashes(files)  # Reject unsafe/colliding paths before replacing a prior preview.
        dest = Path(dest)
        if dest.exists():
            shutil.rmtree(dest)   # never let an earlier run's pages linger in this snapshot
        dest.mkdir(parents=True)
        root = dest.resolve()
        for rel, text in files.items():
            target = (dest / "tree" / rel).resolve()
            if not target.is_relative_to(root):
                raise InitError(f"refusing to write outside the dry-run directory: {rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if text is None:
                (target.parent / (target.name + ".DELETED")).write_text("", encoding="utf-8")
            else:
                target.write_text(text, encoding="utf-8")
        (dest / "PR_BODY.md").write_text(f"# {title}\n\n{body}", encoding="utf-8")
        return dest

    def build_commit(self, base_sha: str, files: Mapping[str, str | None], *, title: str,
                     author: tuple[str, str], when: float) -> str:
        """The change as ONE commit on ``base_sha`` (scratch index; dates from
        ``when`` so a retry rebuilds the same SHA)."""
        for path in files:
            allowed = path in self.allowed_paths if self.allowed_paths else any(p.fullmatch(path) for p in INIT_PATHS)
            if not allowed or ".." in path.split("/"):
                raise InitError(f"refusing to write {path}")
        if self._git("cat-file", "-t", base_sha) != "commit":
            raise InitError(f"base {base_sha[:12]} is not a commit")
        with tempfile.TemporaryDirectory() as scratch:
            env = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
            self._git("read-tree", base_sha, env=env)
            for path, text in sorted(files.items()):
                if text is None:
                    self._git("update-index", "--force-remove", path, env=env)
                    continue
                oid = self._git("hash-object", "-w", "--stdin", input=text.encode("utf-8"))
                self._git("update-index", "--add", "--cacheinfo", f"100644,{oid},{path}", env=env)
            tree = self._git("write-tree", env=env)
        name, email = author
        date = f"@{int(when)} +0000"  # "@": an epoch git never mistakes for another date format
        return self._git("commit-tree", tree, "-p", base_sha, "-m", title, env={
            "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email, "GIT_COMMITTER_DATE": date})

    def open_pr(self, *, base_sha: str, branch: str, files: Mapping[str, str | None], title: str,
                body: str, author: tuple[str, str], when: float,
                commits: list[dict] | None = None, draft: bool = False) -> dict:
        """Push ``branch`` (which must be absent, or already carry exactly
        this commit) and open the PR, or reuse the open PR that has it."""
        commit = self.build_series(base_sha, commits, author=author, when=when) if commits is not None else \
            self.build_commit(base_sha, files, title=title, author=author, when=when)
        existing = json.loads(self._gh("pr", "list", "--repo", self.repository, "--head", branch,
                                       "--state", "open", "--json", "number,headRefOid") or "[]")
        if existing:
            if len(existing) != 1 or existing[0].get("headRefOid") != commit:
                raise InitError(f"an open PR on {branch} does not carry this change")
            return {"number": int(existing[0]["number"]), "head_sha": commit, "branch": branch}
        remote = f"https://github.com/{self.repository}.git"
        listed = self._git("ls-remote", remote, f"refs/heads/{branch}", auth=True).split()
        if not listed:  # never overwrite: the lease expects the branch to be absent
            self._git("push", "--quiet", remote, f"{commit}:refs/heads/{branch}",
                      f"--force-with-lease=refs/heads/{branch}:", auth=True)
        elif listed[0] != commit:
            raise InitError(f"branch {branch} already exists with other content")
        self._gh("pr", "create", "--repo", self.repository, "--base", "main", "--head", branch,
                 "--title", title, "--body-file", "-", *(["--draft"] if draft else []), input=body)
        opened = json.loads(self._gh("pr", "list", "--repo", self.repository, "--head", branch,
                                     "--state", "open", "--json", "number,headRefOid") or "[]")
        if len(opened) != 1 or opened[0].get("headRefOid") != commit:
            raise InitError(f"the PR for {branch} could not be confirmed after creation")
        return {"number": int(opened[0]["number"]), "head_sha": commit, "branch": branch}

    def build_series(self, base_sha: str, commits: list[dict], *, author: tuple[str, str], when: float) -> str:
        """One nonempty upgrade per upstream PR, in the supplied order.

        Plumbing uses a scratch index and deterministic dates; it neither
        checks out nor rewrites the owner's working tree or branch.
        """
        if not commits:
            raise InitError("an upgrade series needs at least one commit")
        head = base_sha
        for index, item in enumerate(commits):
            new = self.build_commit(head, item["files"], title=item["title"], author=author, when=when + index)
            if self._git("rev-parse", f"{head}^{{tree}}") == self._git("rev-parse", f"{new}^{{tree}}"):
                raise InitError("an upstream PR upgrade must change knowledge (empty commit refused)")
            head = new
        return head

    def ready(self, number: int, *, head_sha: str) -> None:
        """Only the aggregate head actually reviewed may leave draft state."""
        data = self.pr_metadata(number)
        if data.get("headRefOid") != head_sha or data.get("state") != "OPEN":
            raise InitError("the PR head or state changed after the aggregate review")
        if data.get("isDraft"):
            self._gh("pr", "ready", str(number), "--repo", self.repository)

    def pr_metadata(self, number: int) -> dict:
        return json.loads(self._gh("pr", "view", str(number), "--repo", self.repository,
                                   "--json", "headRefOid,state,isDraft"))

    def pr_state(self, number: int) -> str:
        data = json.loads(self._gh("pr", "view", str(number), "--repo", self.repository,
                                   "--json", "state"))
        return str(data.get("state") or "")


PREPARED_KEYS = ("base_sha", "branch", "files", "title", "body", "author", "when")
PREPARED_OPTIONAL = {"commits", "draft"}


def save_prepared(path: Path, **publication: Any) -> Path:
    """Persist a publication BEFORE it is pushed, so a retry after a partial
    failure (pushed, but the PR was not opened) rebuilds the very same commit
    instead of re-running the stage."""
    if not set(PREPARED_KEYS).issubset(publication) or set(publication) - set(PREPARED_KEYS) - PREPARED_OPTIONAL:
        raise ValueError(f"a prepared publication needs exactly {PREPARED_KEYS}")
    path = Path(path)
    from ..persistence import atomic_write_bytes
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, json.dumps({**publication, "author": list(publication["author"])},
                                       ensure_ascii=False).encode("utf-8"))
    return path


def load_prepared(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InitError(f"the prepared publication {path} is unreadable: {exc}") from exc
    if not isinstance(data, dict) or not set(PREPARED_KEYS).issubset(data) \
            or set(data) - set(PREPARED_KEYS) - PREPARED_OPTIONAL:
        raise InitError(f"the prepared publication {path} is malformed")
    data["author"] = tuple(data["author"])
    return data


def publishing_allowed(environ: Mapping[str, str]) -> bool:
    """Writes need BOTH flags (the repository's double gate)."""
    return environ.get(ALLOW_PUSH_ENV) == "1" and environ.get(ALLOW_POST_ENV) == "1"


def is_knowledge_path(path: str) -> bool:
    return PurePosixPath(path).parts[:1] == ("knowledge",)
