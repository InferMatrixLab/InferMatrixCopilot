"""Coverage metrics for ``kb init`` (design: kb-init v3 §7).

Pure and deterministic. Two measures of how much of a repository its
knowledge routes reach:

* **module coverage** — the share of modules (``profiles.establish.
  scan_modules_at_depth``) whose every source file matches some owner's
  ``scope_prefixes`` in ``_routes.yaml``;
* **PR-weighted coverage** — over a window of merged PRs, the share of changed
  source files (counted once per PR that changed them) that route to an owner
  (*routed*), and whose MOST SPECIFIC owner's page carries rules
  (*rule-bearing*; see ``most_specific``).

Specificity matters because prefixes nest: a catch-all owner (``pkg/``) reaches
every file a component owner (``pkg/sub/``) reaches. If any matching owner's
rules counted, one rule on the catch-all page would make the whole package
rule-bearing and the metric would stop measuring anything (kb init pilot,
2026-09-30: 10% -> 100% from one module's rules).

An empty population counts as fully covered (ratio 1.0): there is nothing a
route could miss.

The owner table comes from ``owner_table``: a knowledge-side ``_routes.yaml``
when the repository has one, else the adapter manifest's ``review_routes``
(the precedence ``direct_routing`` applies), else nothing. A repository routed
by its manifest is measured exactly like one routed by a table; what differs
is that init never writes its routes (see ``init_stages``).
"""

from __future__ import annotations

import fnmatch
from collections import Counter
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable, Mapping, Sequence

import yaml

# equals profiles.establish.ROOT_MODULE (kb_service stays free of profile
# imports; a test pins the two together)
ROOT_MODULE = "./"


@dataclass(frozen=True)
class Owner:
    owner: str
    path: str
    prefixes: tuple[str, ...]


@dataclass(frozen=True)
class Coverage:
    covered: list[str]
    uncovered: list[str]
    ratio: float


@dataclass(frozen=True)
class PrCoverage:
    total: int
    routed: int
    rule_bearing: int
    routed_ratio: float
    rule_bearing_ratio: float
    uncovered_hot: list[tuple[str, int]]


def load_owners(routes_yaml_text: str) -> list[Owner]:
    """The ``owners`` of a ``_routes.yaml`` (schema_version 1); ``models`` are
    name-matched, not path-matched, and play no part in coverage."""
    data = yaml.safe_load(routes_yaml_text) or {}
    if not isinstance(data, dict):
        raise ValueError("_routes.yaml must be a mapping")
    if data.get("schema_version", 1) != 1:
        raise ValueError(f"unsupported _routes.yaml schema_version {data.get('schema_version')!r}")
    owners = data.get("owners") or []
    if not isinstance(owners, list):
        raise ValueError("_routes.yaml owners must be a list")
    out = []
    for item in owners:
        if not isinstance(item, dict) or not item.get("owner") or not item.get("path"):
            raise ValueError(f"malformed _routes.yaml owner: {item!r}")
        prefixes = item.get("scope_prefixes") or []
        if not isinstance(prefixes, list) or any(not isinstance(p, str) or not p for p in prefixes):
            raise ValueError(f"{item['owner']}: scope_prefixes must be a list of non-empty strings")
        out.append(Owner(str(item["owner"]), str(item["path"]), tuple(prefixes)))
    return out


def owners_from_review_routes(manifest: Mapping | None) -> list[Owner]:
    """The owner table an adapter manifest's ``review_routes`` declares
    (``{prefix, owner, doc}`` items, the form ``direct_routing`` falls back to
    when a repository has no knowledge-side ``_routes.yaml``): one owner per
    (owner, doc) pair in declaration order, its prefixes in declaration order.
    An owner name that recurs with a second doc is suffixed with that doc's
    stem so names stay unique. Items without a prefix or a doc are skipped."""
    routes = (manifest or {}).get("review_routes") or []
    if not isinstance(routes, list):
        return []
    grouped: dict[tuple[str, str], list[str]] = {}
    for item in routes:
        if not isinstance(item, dict):
            continue
        prefix = str(item.get("prefix") or "").replace("\\", "/").strip()
        doc = str(item.get("doc") or "").strip()
        if not prefix or not doc:
            continue
        owner = str(item.get("owner") or prefix.rstrip("/")).strip()
        grouped.setdefault((owner, doc), [])
        if prefix not in grouped[(owner, doc)]:
            grouped[(owner, doc)].append(prefix)
    taken: set[str] = set()
    out = []
    for (owner, doc), prefixes in grouped.items():
        name = owner if owner not in taken else f"{owner}/{PurePosixPath(doc).stem}"
        n = 1
        while name in taken:   # two docs with the same stem (components/a/rules.md, components/b/rules.md)
            n += 1
            name = f"{owner}/{PurePosixPath(doc).stem}-{n}"
        taken.add(name)
        out.append(Owner(name, doc, tuple(prefixes)))
    return out


def owner_table(routes_text: str | None, manifest: Mapping | None) -> tuple[str, list[Owner]]:
    """Where a repository's routes come from and the owners they define, with
    the precedence ``direct_routing`` applies: ``("routes_file", ...)`` for a
    knowledge-side ``_routes.yaml`` that names owners, else ``("manifest", ...)``
    for adapter ``review_routes``, else ``("none", [])``. Direct falls back to
    the manifest when the routes file has NO owners, so a valid but empty file
    defers to manifest routes too (and must stay untouched: filling it would
    override the manifest); only without manifest routes is an empty file the
    knowledge-side table init fills. A malformed routes file raises
    (``load_owners``); it is never silently replaced by the manifest."""
    file_owners = load_owners(routes_text) if routes_text is not None else None
    if file_owners:
        return "routes_file", file_owners
    manifest_owners = owners_from_review_routes(manifest)
    if manifest_owners:
        return "manifest", manifest_owners
    return ("routes_file", []) if file_owners is not None else ("none", [])


def reaches(path: str, prefix: str) -> bool:
    """``prefix`` covers ``path``. A directory prefix ``pkg/`` also covers the
    directory itself named without its slash (``facts.claims_in`` strips it
    from a rule's backticked ``pkg/``), so a rule about a directory reaches
    the owner of that directory."""
    return path.startswith(prefix) or (prefix.endswith("/") and path == prefix[:-1])


def routes_file(path: str, owners: Sequence[Owner]) -> list[Owner]:
    """Every owner whose scope prefixes reach ``path``, in routing order."""
    return [o for o in owners if any(reaches(path, p) for p in o.prefixes)]


def _page_depth(owner: Owner) -> int:
    return len(PurePosixPath(owner.path).parts)


def most_specific(path: str, owners: Sequence[Owner]) -> list[Owner]:
    """The owners that own ``path`` most specifically, in routing order: those
    whose longest matching prefix is the longest of all matches; among those,
    the ones whose page lies deepest in the knowledge tree (a component's own
    page, not an aggregate entry page that lists the same area). Ties on both
    return every tied owner; no match returns []."""
    best: tuple[int, int] | None = None
    out: list[Owner] = []
    for owner in owners:
        length = max((len(p) for p in owner.prefixes if reaches(path, p)), default=-1)
        if length < 0:
            continue
        key = (length, _page_depth(owner))
        if best is None or key > best:
            best, out = key, [owner]
        elif key == best:
            out.append(owner)
    return out



def owner_for_paths(paths: Sequence[str], owners: Sequence[Owner]) -> Owner | None:
    """Most-specific path coverage, then routing order; shared by init callers."""
    counts: dict[str, int] = {}
    by_name = {o.owner: o for o in owners}
    for path in paths:
        for owner in most_specific(path, owners):
            counts[owner.owner] = counts.get(owner.owner, 0) + 1
    if not counts:
        return None
    order = [o.owner for o in owners]
    best = max(counts, key=lambda name: (counts[name], -order.index(name)))
    return by_name[best]

def shadowing(owners: Sequence[Owner]) -> list[tuple[str, str, str, str]]:
    """``(owner, prefix, other owner, other prefix)`` for every prefix that is a
    strict ancestor of another owner's prefix: the broader owner still routes
    those files, but only the more specific one counts for them (and gets
    their rules)."""
    out = []
    for owner in owners:
        for prefix in owner.prefixes:
            if not prefix.endswith("/"):
                continue   # a file prefix names one file, never an ancestor
            for other in owners:
                if other is owner:
                    continue
                for inner in other.prefixes:
                    if inner != prefix and inner.startswith(prefix):
                        out.append((owner.owner, prefix, other.owner, inner))
    return out


def module_coverage(modules: Mapping[str, Mapping], owners: Sequence[Owner]) -> Coverage:
    covered, uncovered = [], []
    for key in sorted(modules):
        files = modules[key].get("files") or []
        (covered if all(routes_file(f, owners) for f in files) else uncovered).append(key)
    total = len(covered) + len(uncovered)
    return Coverage(covered, uncovered, len(covered) / total if total else 1.0)


def _normalize(path: str) -> str:
    """Repo-relative POSIX form without leading ``./`` or slashes; the repository
    root is ``""`` (same rule as ``profiles.establish.normalize_root``)."""
    text = PurePosixPath(path.strip().strip("/") or ".").as_posix()
    return "" if text == "." else text


def make_include(source_roots: Sequence[str], exclude: Sequence[str],
                 suffixes: Sequence[str] = ()) -> Callable[[str], bool]:
    """A filter for changed paths: under a source root (any path when none are
    given), with a source suffix (any when none are given), matching no
    ``exclude`` fnmatch glob. Tests, docs and generated files are the caller's
    ``exclude`` globs; nothing repository-specific is built in."""
    normalized = {_normalize(r) for r in source_roots}
    # the repository root (".", "./", "") admits every path
    roots = () if "" in normalized else tuple(sorted(r + "/" for r in normalized))
    sfx = tuple(suffixes)

    def include(path: str) -> bool:
        path = _normalize(path)
        if roots and not path.startswith(roots):
            return False
        if sfx and not path.endswith(sfx):
            return False
        return not any(fnmatch.fnmatch(path, glob) for glob in exclude)

    return include


def pr_weighted_coverage(prs: Sequence[Sequence[str]], owners: Sequence[Owner], *,
                         include: Callable[[str], bool],
                         rule_pages: set[str] | None = None) -> PrCoverage:
    """Each file a PR changes counts once for that PR. A file is *routed* when
    any owner reaches it, and *rule-bearing* when one of its ``most_specific``
    owners has its path in ``rule_pages`` (the knowledge paths, as
    ``_routes.yaml`` names them, holding at least one active rule; without
    them nothing is rule-bearing). A broader owner's rules never count for a
    file a more specific owner reaches."""
    pages = rule_pages or set()
    total = routed = rule_bearing = 0
    missed: Counter[str] = Counter()
    for files in prs:
        for path in sorted({_normalize(f) for f in files}):
            if not include(path):
                continue
            total += 1
            hits = routes_file(path, owners)
            if hits:
                routed += 1
                if any(o.path in pages for o in most_specific(path, hits)):
                    rule_bearing += 1
            else:
                missed[path] += 1
    hot = sorted(missed.items(), key=lambda item: (-item[1], item[0]))
    return PrCoverage(total, routed, rule_bearing,
                      routed / total if total else 1.0,
                      rule_bearing / total if total else 1.0, hot)


def module_of(path: str, modules: Mapping[str, Mapping]) -> str | None:
    """The module owning ``path``: the longest module key that prefixes it (a
    file in a folded directory belongs to the ancestor it folded into)."""
    best = None
    for key in modules:
        prefix = "" if key == ROOT_MODULE else key
        if path.startswith(prefix) and (best is None or len(prefix) > len(best[1])):
            best = (key, prefix)
    return best[0] if best else None


def churn_by_module(prs: Sequence[Sequence[str]], modules: Mapping[str, Mapping]) -> list[tuple[str, int]]:
    """File changes per module over the PR window, highest first (ties by
    module path). Paths no module owns are not counted."""
    counts: Counter[str] = Counter()
    for files in prs:
        for path in sorted({_normalize(f) for f in files}):
            key = module_of(path, modules)
            if key is not None:
                counts[key] += 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))
