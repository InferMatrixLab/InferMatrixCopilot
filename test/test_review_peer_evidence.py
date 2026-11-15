"""Offline verifier evidence tests; scripted verdicts are not model-quality results."""
from __future__ import annotations

import ast
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

PATH = Path(__file__).resolve().parents[1] / "src/infermatrix_copilot/engine/steps/review/refinement.py"
RUNTIME_PATH = Path(__file__).resolve().parents[1] / "src/infermatrix_copilot/engine/agent_runtime/utils.py"


def load_build_evidence():
    tree = ast.parse(RUNTIME_PATH.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_build_evidence")
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node]
    namespace = {"hashlib": hashlib, "threading": threading}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 str(RUNTIME_PATH), "exec"), namespace)
    return namespace["_build_evidence"]


def load_verify(runner):
    # Execute the exact function and guidance while injecting its one runtime
    # dependency. No provider initialization, network or model calls.
    tree = ast.parse(PATH.read_text())
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_VERIFY_GUIDANCE" for t in node.targets
        ):
            nodes.append(node)
        elif isinstance(node, ast.AsyncFunctionDef) and node.name == "_verify_comments":
            node.body = [s for s in node.body if not isinstance(s, ast.ImportFrom)]
            nodes.append(node)
    namespace = {"asyncio": asyncio, "json": json, "run_agent_step": runner,
                 "_SEVERITY_ORDER": {"blocker": 0, "major": 1, "minor": 2, "nit": 3}}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(PATH), "exec"), namespace)
    return namespace


def context(enabled=True):
    return SimpleNamespace(
        settings=SimpleNamespace(review_verify_comments=enabled,
                                 review_verify_concurrency=2, review_verify_max_iters=3),
        trace=SimpleNamespace(record=lambda *a, **kw: None),
    )


class PeerEvidence(unittest.IsolatedAsyncioTestCase):
    async def test_archived_peers_remain_private_to_each_verifier(self):
        build_evidence = load_build_evidence()
        all_archived = asyncio.Event()
        calls = []
        candidates = [{"comment": f"candidate-{i}: " + "x" * 200, "severity": "minor"}
                      for i in range(3)]

        async def runner(ctx, **kwargs):
            _, refs = build_evidence(ctx, kwargs["evidence"])
            calls.append(refs)
            if len(calls) == len(candidates):
                all_archived.set()
            await all_archived.wait()  # Read only after all competing writes.
            i = int(kwargs["step_name"].rsplit("#", 1)[1])
            peers = json.loads(Path(refs["sibling_draft_comments"]).read_text())
            self.assertEqual([p["comment"] for p in peers],
                             [c["comment"] for j, c in enumerate(candidates) if i != j])
            own = json.loads(Path(refs["draft_comment"]).read_text())
            self.assertEqual(own["comment"], candidates[i]["comment"])
            return SimpleNamespace(ok=True), {"verdict": "confirmed"}

        with tempfile.TemporaryDirectory() as directory:
            ctx = context()
            ctx.run_dir = Path(directory)
            ctx.settings.review_verify_concurrency = 3
            ctx.settings.evidence_caps = {}
            ctx.settings.evidence_item_chars = 80
            result = await load_verify(runner)["_verify_comments"](
                ctx, {"review_comments": candidates}, {})
            self.assertEqual(len(result["review_comments"]), 3)
            self.assertEqual(len({c["sibling_draft_comments"] for c in calls}), 3)

    async def test_each_verifier_sees_other_candidates_without_self_or_routing_tags(self):
        calls = []

        async def runner(ctx, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(ok=True), {"verdict": "confirmed"}

        ns = load_verify(runner)
        candidates = [
            {"file": "tests/test_feature.py", "line": 9, "severity": "major",
             "comment": "The test fails; no CI lane collects it.", "corroborated_by": ["a"], "evidence": "Long original evidence."},
            {"file": "ci/ready.yml", "line": 4, "severity": "major",
             "comment": "The thread is wrong: the directory sweep selects this failing test."},
            {"file": "model.py", "line": 2, "severity": "minor", "comment": "Separate real defect."},
        ]
        original = json.loads(json.dumps(candidates))
        result = await ns["_verify_comments"](context(), {"review_comments": candidates},
                                             {"evidence": {"pr_diff": "frozen diff"}, "extra_tools": {}})
        self.assertEqual(candidates, original)
        self.assertEqual(len(calls), 3)
        for i, call in enumerate(calls):
            evidence = call["evidence"]
            peers = json.loads(evidence["sibling_draft_comments"])
            self.assertEqual([p["comment"] for p in peers],
                             [c["comment"] for j, c in enumerate(candidates) if i != j])
            self.assertTrue(all("corroborated_by" not in p and "evidence" not in p for p in peers))
            self.assertEqual(json.loads(evidence["draft_comment"])["comment"], candidates[i]["comment"])
            self.assertEqual(evidence["pr_diff"], "frozen diff")
            self.assertIn("UNPUBLISHED candidates", call["guidance"])
            self.assertIn("directory/glob collection", call["guidance"])
            self.assertIn("whether that lane actually", call["guidance"])
        self.assertEqual(len(result["review_comments"]), 3)  # No heuristic deletion.

    async def test_explicit_verdict_still_owns_repair_and_removal(self):
        async def runner(ctx, **kwargs):
            if kwargs["step_name"].endswith("#0"):
                verdict = {"verdict": "confirmed", "comment": "Real test failure; ready lane selects it."}
            else:
                verdict = {"verdict": "not_actionable", "evidence": "Only corrects our own draft."}
            return SimpleNamespace(ok=True), verdict

        ns = load_verify(runner)
        result = await ns["_verify_comments"](context(), {"review_comments": [
            {"comment": "Incorrect absence rationale", "severity": "major"},
            {"comment": "Correct the review's own rationale", "severity": "major"},
        ]}, {})
        self.assertEqual([c["comment"] for c in result["review_comments"]],
                         ["Real test failure; ready lane selects it."])

    async def test_disabled_verification_makes_no_calls(self):
        async def runner(*args, **kwargs):
            self.fail("Disabled verifier must not call the runner")
        ns = load_verify(runner)
        output = {"review_comments": [{"comment": "untouched"}]}
        self.assertIs(await ns["_verify_comments"](context(False), output, {}), output)


class EvidenceArchives(unittest.TestCase):
    def test_caps_and_stable_content_references(self):
        build_evidence = load_build_evidence()
        with tempfile.TemporaryDirectory() as directory:
            ctx = context()
            ctx.run_dir = Path(directory)
            ctx.settings.evidence_item_chars = 80
            ctx.settings.evidence_caps = {"item": 120}
            small, refs = build_evidence(ctx, {"item": "s" * 100})
            self.assertEqual(small["item"], "s" * 100)
            self.assertEqual(refs, {})
            self.assertFalse((ctx.run_dir / "evidence").exists())
            text = "α" * 160 + "TAIL"
            capped, first = build_evidence(ctx, {"item": text})
            _, again = build_evidence(ctx, {"item": text})
            _, other = build_evidence(ctx, {"item": "different" * 30})
            self.assertEqual(first, again)
            self.assertNotEqual(first, other)
            self.assertEqual(Path(first["item"]).read_text(), text)
            self.assertIn("44 chars omitted", capped["item"])
            self.assertTrue(capped["item"].endswith("TAIL"))
            _, explicit = build_evidence(ctx, {"item": "s" * 100}, cap=40)
            self.assertEqual(Path(explicit["item"]).read_text(), "s" * 100)

    def test_concurrent_archives_preserve_distinct_and_shared_content(self):
        build_evidence = load_build_evidence()
        with tempfile.TemporaryDirectory() as directory:
            ctx = context()
            ctx.run_dir = Path(directory)
            ctx.settings.evidence_item_chars = 40
            ctx.settings.evidence_caps = {}
            texts = [(f"claim-{i % 4}: " + "x" * 200) for i in range(16)]
            with ThreadPoolExecutor(max_workers=8) as pool:
                refs = list(pool.map(lambda text: build_evidence(ctx, {"item": text})[1]["item"], texts))
            for text, ref in zip(texts, refs):
                self.assertEqual(Path(ref).read_text(), text)
            self.assertEqual(len(set(refs)), 4)
            self.assertEqual(list((ctx.run_dir / "evidence").glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
