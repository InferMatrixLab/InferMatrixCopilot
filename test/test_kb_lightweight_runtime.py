"""Lightweight retries remain bounded across partial acceptance and interruption."""

import json
from dataclasses import replace

import pytest

from infermatrix_copilot.kb_service.depth_inputs import SYSTEM_DEPTH, system_prompt
from infermatrix_copilot.kb_service.depth_judge import SYSTEM_DEPTH_REVIEW
from infermatrix_copilot.kb_service.init_stages import run_stage
from infermatrix_copilot.kb_service.init_support import InitRecord
from infermatrix_copilot.kb_service.knowledge_depth import FACETS
from test_kb_knowledge_depth import DepthGateway, _baseline, _complete_breadth
from test_kb_init_modules import _modules_lifecycle
from test_kb_init_skeleton import _runtime, world  # noqa: F401


class LightGateway(DepthGateway):
    def call_json(self, role, **kwargs):
        generation = kwargs["system"] == system_prompt("lightweight")
        if generation:
            kwargs = {**kwargs, "system": SYSTEM_DEPTH}
        reply = super().call_json(role, **kwargs)
        if generation:
            for section in reply.data["sections"]:
                if section["facet"] == "validation":
                    section["validation_kind"] = "automated_runtime"
        return reply


def run_light(world, gateway, *, retry=False, stop_file=None):
    rt = _runtime(world, gateway, state_dir=world["tmp"] / "light")
    lifecycle = replace(_modules_lifecycle(), init=replace(_modules_lifecycle().init, budget_usd=10))
    return run_stage(rt, lifecycle, "knowledge-deepen", dry_run=True, from_existing=True,
                     subscription_generator=True, acceptance_mode="lightweight",
                     retry_unfinished=retry, stop_file=stop_file)


def test_one_rejected_facet_has_four_lifetime_attempts_and_preserves_other_passes(world):
    policy = _baseline(world)
    _complete_breadth(world, policy)
    gateway = LightGateway(reject="api")
    record = run_light(world, gateway)
    entry = record.depth["features"]["step0"]
    assert entry["facets"]["api"]["total_attempts"] == 4
    assert record.coverage["semantic_depth"]["lightweight_recognized_facets"] == 6
    assert len(gateway.depth_calls) == 4
    assert gateway.depth_calls[0]["facets"] == list(FACETS)
    assert all(p["facets"] == ["api"] for p in gateway.depth_calls[1:])
    resumed = run_light(world, gateway, retry=True)
    assert len(gateway.depth_calls) == 4
    assert resumed.depth["features"]["step0"]["facets"]["api"]["total_attempts"] == 4


def test_interrupted_dispatched_review_retains_original_round_until_reconciled(world):
    policy = _baseline(world)
    _complete_breadth(world, policy)
    gateway = LightGateway(interrupt=True)
    with pytest.raises(KeyboardInterrupt):
        run_light(world, gateway)
    pending = InitRecord.load(world["tmp"] / "light", "toy", "knowledge-deepen")
    assert pending.depth["features"]["step0"]["review_dispatched"]
    assert len(gateway.depth_calls) == 1
    record = run_light(world, gateway)
    assert len(gateway.depth_calls) == 1
    assert record.coverage["semantic_depth"]["lightweight_recognized_facets"] == 0
    entry = record.depth["features"]["step0"]
    assert entry["review_dispatched"] and "checked" not in entry
    assert entry["draft"] == pending.depth["features"]["step0"]["draft"]
    assert all(slot["total_attempts"] == 1 for slot in entry["facets"].values())
    assert not record.depth["done"]


def test_stop_before_dispatch_keeps_zero_attempts(world):
    policy = _baseline(world)
    _complete_breadth(world, policy)
    stop = world["tmp"] / "STOP"
    stop.touch()
    gateway = LightGateway()
    record = run_light(world, gateway, stop_file=stop)
    assert not gateway.depth_calls
    assert record.coverage["semantic_depth"]["recognized_facets"] == 0
    assert all(not slot.get("total_attempts", 0) for slot in record.depth["features"]["step0"]["facets"].values())
