"""Repo-neutrality vocabulary containment (2026-08-01 audit).

`test_repo_neutral_core` pins the NARROW leak regex (the repo name +
`/rebase/`) with per-file ceilings. This suite pins the leak classes that
regex cannot see, so the audit's findings stay structural:

1. ZERO-anywhere classes — machine paths, vllm-omni MODULE names,
   branch/queue/pipeline/wheel-host literals: no core file may carry
   them at all (repo knowledge lives in `adapters/<repo>/`).
2. CONTAINED vocabulary — the parent-parity `omni`/`vllm` identifier
   naming inside the rebase engine (field names like `omni_path`, tool
   names like `git_show_omni_main`, prompt prose, the `imx-omni-pytest`
   script) is RECORDED naming debt: it is locked by the byte-parity
   goldens and the adapter's parent-verbatim tool schemas until the
   post-cutover cleanup (PR7). Enforcement is EXACT per-file occurrence
   ceilings (2026-08-01 GPT-audit hardening): growth inside a listed
   file fails, headroom is forbidden (count must equal ceiling), and
   unused exemptions must leave — the debt only shrinks.

The functional proof lives in test_second_repo_onboarding.py: a
synthetic non-vllm adapter (master branch, upstream remote, ci/pipelines
yaml, checks/ test roots, no wheel/venv/precommit) runs the production
pipeline end to end with ZERO src/ changes.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "infermatrix_copilot"

# class 1: never anywhere in the core, no ceilings
_FORBIDDEN = re.compile(
    r"/data/zhoutaichang"                       # machine paths
    # vllm-omni module names (NOT `model_config` — that collides
    # with pydantic's own attribute name)
    r"|worker_runner|model_executor|online_serving|input_output"
    r"|dev/vllm-align|releases/v0"              # branch names
    r"|wheels\.vllm\.ai"                        # wheel host
    r"|vllm-omni-rebase"                       # pipeline/external-agent names
    r"|gpu_1_queue|gpu_4_queue|mithril"         # queue names
    r"|VLLM_PRECOMPILED_WHEEL_COMMIT",          # pin env var
)

# class 1b: the delegation-by-design files carry EXACT counts (shrink-only
# ceilings, like test_repo_neutral_core's) rather than a blanket skip —
# they import/invoke the PARENT and die in PR7
_FORBIDDEN_CEILINGS: dict[str, int] = {
    # Direct's review-routing table used to sit here at 2; it now lives in
    # knowledge/repos/<repo>/_routes.yaml and the ceiling is gone.
}

# class 2: files ALLOWED to use the standalone `omni`/`vllm` parity
# vocabulary (identifier names, tool names, golden-pinned prompt prose) —
# with EXACT per-file occurrence ceilings so the debt can only SHRINK
# (a new occurrence inside an already-listed file fails too, per the
# 2026-08-01 GPT audit). The whole inventory sunsets at the PR7 cleanup.
_VOCAB_RX = re.compile(r"(?<![A-Za-z])(omni|vllm)(?![A-Za-z])",
                       re.IGNORECASE)
_VOCAB_CEILINGS = {
    # delegation (PR7 sunset)
    # parity vocabulary: parent-verbatim tool/handler names, prompt prose
    # locked by byte-parity goldens, imx-omni-pytest env contract
    "rebase_engine/module_rebase.py": 9,
    "rebase_engine/module_pytest.py": 7,
    "rebase_engine/prompt_builder.py": 34,
    "rebase_engine/rebase_tools.py": 15,
    "rebase_engine/test_manifest.py": 3,
    "rebase_engine/worktree.py": 2,
    "engine/steps/rebase_v3.py": 14,
    # ceilinged v1-era defaults (also under test_repo_neutral_core)
    "config.py": 2,      # default_repo; no external-agent default or exemption
    "task_spec.py": 2,   # default_repo literal
    "intent.py": 4,      # default_repo parameter defaults
    "__init__.py": 2,    # package docstring
    # env allowlist pass-through prefixes (VLLM_/TORCH_…) — PARITY-pinned
    # child-env behavior; adapter-configurable prefixes are the recorded
    # post-cutover refinement
    "testing/env_plan.py": 2,
    # thin MCP server: tool defaults + instructions prose. The routing table
    # that once shared this entry (52 in direct_routing.py) moved to
    # knowledge/repos/<repo>/_routes.yaml.
    "thin_mcp_server.py": 8,
}


def _core_files():
    return sorted(SRC.rglob("*.py"))


def test_forbidden_repo_literals_nowhere():
    for path in _core_files():
        rel = str(path.relative_to(SRC))
        hits = _FORBIDDEN.findall(path.read_text(encoding="utf-8"))
        ceiling = _FORBIDDEN_CEILINGS.get(rel, 0)
        # EXACT, headroom-free (same rule as the vocab ceilings): a
        # literal that disappears must take its ceiling with it, or a
        # stale ceiling would permit silent reintroduction
        assert len(hits) == ceiling, (
            f"{rel}: {len(hits)} repo-specific literal(s) "
            f"{sorted(set(hits))}, ceiling {ceiling} — zero outside the "
            "PR7-sunset delegation, and delegation ceilings track the "
            "EXACT current count (lower them when the count drops)")


def test_parity_vocabulary_is_contained():
    offenders = {}
    for path in _core_files():
        rel = str(path.relative_to(SRC))
        count = len(_VOCAB_RX.findall(path.read_text(encoding="utf-8")))
        if count > _VOCAB_CEILINGS.get(rel, 0):
            offenders[rel] = (count, _VOCAB_CEILINGS.get(rel, 0))
    assert not offenders, (
        f"standalone omni/vllm vocabulary GREW beyond the recorded "
        f"parity-debt ceilings {{file: (count, ceiling)}}: {offenders} — "
        "neutralize the naming or justify a golden-parity ceiling bump "
        "here (the inventory is meant to shrink toward the PR7 cleanup)")


def test_vocabulary_inventory_is_not_stale():
    """Every ceilinged file actually uses the vocabulary — a file that no
    longer needs the exemption must leave the list (the debt only
    shrinks); a ceiling far above the real count would hide growth."""
    for rel, ceiling in sorted(_VOCAB_CEILINGS.items()):
        path = SRC / rel
        assert path.exists(), rel
        count = len(_VOCAB_RX.findall(path.read_text(encoding="utf-8")))
        assert count > 0, (
            f"{rel} no longer uses the vocabulary — remove it from "
            "_VOCAB_CEILINGS (the inventory only shrinks)")
        assert count == ceiling, (
            f"{rel}: count {count} != ceiling {ceiling} — ceilings track "
            "the EXACT current debt; lower the ceiling when the count "
            "drops (never leave headroom)")
