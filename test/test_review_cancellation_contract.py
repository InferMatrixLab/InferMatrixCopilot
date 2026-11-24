"""Offline prompt wiring and cancellation-model controls, not model-quality evals."""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
import runpy
import unittest

import anyio

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "src/infermatrix_copilot/engine/steps/review"


def guidance():
    # prompts.py is dependency-free. Evaluate only the verifier's declared
    # guidance expression, avoiding provider initialization/model access.
    namespace = runpy.run_path(str(REVIEW / "prompts.py"))
    tree = ast.parse((REVIEW / "refinement.py").read_text())
    assignment = next(
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_VERIFY_GUIDANCE"
                for t in node.targets)
    )
    verifier = eval(compile(ast.Expression(assignment.value), "guidance", "eval"), namespace)
    return namespace, verifier


class CancellationReviewContract(unittest.TestCase):
    def test_both_passes_receive_the_same_cancellation_contract(self):
        namespace, verifier = guidance()
        contract = namespace["_CANCELLATION_REVIEW_GUIDANCE"]
        self.assertIn(contract, namespace["_REVIEW_SYSTEM"])
        self.assertIn(contract, verifier)
        self.assertIn("dependency version", contract)
        self.assertIn("reachable repeated/scope cancellation", contract)
        self.assertIn("mark the claim unverifiable", contract)

    def test_sibling_difference_is_a_candidate_not_automatic_defect(self):
        namespace, _ = guidance()
        prompt = namespace["_REVIEW_SYSTEM"]
        self.assertIn("not automatically a finding", prompt)
        self.assertIn("same obligation and a reachable adverse effect", prompt)


class CancellationMechanisms(unittest.IsolatedAsyncioTestCase):
    async def test_single_task_cancel_allows_yielding_cleanup(self):
        started = asyncio.Event()
        completed = []

        async def work():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                completed.append("released")

        task = asyncio.create_task(work())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(completed, ["released"])

    async def test_second_task_cancel_can_interrupt_cleanup(self):
        started = asyncio.Event()
        cleaning = asyncio.Event()
        completed = []

        async def work():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await asyncio.Event().wait()
                completed.append("released")

        task = asyncio.create_task(work())
        await started.wait()
        task.cancel()
        await cleaning.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(completed, [])

    async def test_scope_cancel_interrupts_unshielded_cleanup(self):
        for shield in (False, True):
            with self.subTest(shield=shield):
                started = anyio.Event()
                completed = []

                async def work():
                    try:
                        started.set()
                        await anyio.sleep_forever()
                    finally:
                        with anyio.CancelScope(shield=shield):
                            await anyio.sleep(0)
                            completed.append("released")

                async with anyio.create_task_group() as group:
                    group.start_soon(work)
                    await started.wait()
                    group.cancel_scope.cancel()
                self.assertEqual(completed, ["released"] if shield else [])


if __name__ == "__main__":
    unittest.main()
