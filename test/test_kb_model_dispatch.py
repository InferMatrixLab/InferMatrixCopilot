"""Real shared slots across threads/processes; no model or network calls."""

import multiprocessing
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from infermatrix_copilot.kb_service.model_dispatch import DispatchConfigError, SharedModelDispatch
from infermatrix_copilot.kb_service.models import ModelGateway, ModelRole, ModelUnavailable


def _process_call(directory, ready, active, maximum, mutex):
    dispatch = SharedModelDispatch(directory, limit=3)
    ready.wait(5)
    with dispatch.slot():
        with mutex:
            active.value += 1
            maximum.value = max(maximum.value, active.value)
        time.sleep(.15)
        with mutex:
            active.value -= 1


def _process_hold(directory, ready):
    with SharedModelDispatch(directory, limit=1).slot():
        ready.set()
        time.sleep(30)


def test_threads_share_thirteen_slots_across_distinct_limiter_instances(tmp_path):
    active = maximum = 0
    mutex = threading.Lock()
    saturated = threading.Event()
    release = threading.Event()
    def call(_):
        nonlocal active, maximum
        # Separate stage/gateway instances must still share the same pool.
        with SharedModelDispatch(tmp_path).slot() as ticket:
            assert ticket.limit == 13 and ticket.wait_seconds >= 0
            with mutex:
                active += 1
                maximum = max(maximum, active)
                if active == 13:
                    saturated.set()
            assert release.wait(5)
            with mutex:
                active -= 1
    with ThreadPoolExecutor(max_workers=26) as pool:
        futures = [pool.submit(call, n) for n in range(26)]
        try:
            assert saturated.wait(5)
        finally:
            release.set()
        for future in futures:
            future.result(timeout=5)
    assert maximum == 13 and active == 0


def test_processes_share_cap_and_release_every_completed_call(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    ready, active, maximum, mutex = ctx.Event(), ctx.Value("i", 0), ctx.Value("i", 0), ctx.Lock()
    processes = [ctx.Process(target=_process_call, args=(str(tmp_path), ready, active, maximum, mutex)) for _ in range(8)]
    try:
        for process in processes:
            process.start()
        ready.set()
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
    assert 1 < maximum.value <= 3 and active.value == 0


def test_crashed_process_releases_kernel_lock_without_stale_pid_cleanup(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    child = ctx.Process(target=_process_hold, args=(str(tmp_path), ready))
    try:
        child.start()
        assert ready.wait(5)
        child.terminate()
        child.join(5)
        assert not child.is_alive()
        started = time.monotonic()
        with SharedModelDispatch(tmp_path, limit=1).slot():
            assert time.monotonic() - started < 1
    finally:
        if child.is_alive():
            child.terminate()
            child.join(5)


@pytest.mark.parametrize("limit", [0, 14, True, "13.0", "", "bad"])
def test_cap_config_is_bounded_and_conflicting_namespace_refused(tmp_path, limit):
    with pytest.raises(DispatchConfigError):
        SharedModelDispatch(tmp_path, limit=limit)


def test_default_host_namespace_shared_across_batch_state_directories(tmp_path):
    env = {"XDG_CACHE_HOME": str(tmp_path), "KB_INIT_GLOBAL_CONCURRENCY": "3"}
    first = SharedModelDispatch(environ=env)
    second = SharedModelDispatch(environ=env)
    assert first.directory == second.directory == tmp_path / "infermatrix-copilot/kb-init-dispatch"
    with pytest.raises(DispatchConfigError, match="conflicts"):
        SharedModelDispatch(first.directory, limit=4)
    assert SharedModelDispatch(environ={"KB_INIT_DISPATCH_DIR": str(tmp_path / "explicit")}).limit == 13


def test_adapter_init_batches_share_dispatch_with_portable_batches(tmp_path, monkeypatch):
    from infermatrix_copilot.kb_service.init_support import InitRuntime

    monkeypatch.setenv("KB_INIT_DISPATCH_DIR", str(tmp_path / "dispatch"))
    monkeypatch.setenv("KB_INIT_GLOBAL_CONCURRENCY", "3")
    monkeypatch.setenv("KB_INIT_KNOWLEDGE_CLONE", str(tmp_path / "knowledge"))
    monkeypatch.setattr("infermatrix_copilot.kb_service.init_support.ensure_knowledge_clone", lambda _: None)
    first = InitRuntime.from_env(SimpleNamespace(), state_dir=tmp_path / "foundation")
    second = InitRuntime.from_env(SimpleNamespace(), state_dir=tmp_path / "depth")
    portable = SharedModelDispatch()
    assert first.gateway._dispatch.directory == second.gateway._dispatch.directory == portable.directory
    assert first.gateway._dispatch.limit == second.gateway._dispatch.limit == portable.limit == 3


def test_native_gateway_fallbacks_and_judges_use_same_slots_and_keep_recording(tmp_path):
    active = maximum = 0
    mutex = threading.Lock()
    records, providers = [], []
    def factory(provider):
        class Transport:
            def complete(self, **kwargs):
                nonlocal active, maximum
                with mutex:
                    active += 1
                    maximum = max(maximum, active)
                    providers.append((provider, kwargs["role"]))
                try:
                    time.sleep(.03)
                    if provider == "unavailable":
                        return SimpleNamespace(model=kwargs["model"], stop_reason="end_turn", usage={},
                                               blocks=[SimpleNamespace(text="completed unusable response")])
                    return SimpleNamespace(model=kwargs["model"], stop_reason="end_turn", usage={},
                                           blocks=[SimpleNamespace(text='{"supported":true}')])
                finally:
                    with mutex:
                        active -= 1
        return Transport()
    fallback = ModelRole("generator", "fallback", "scripted")
    generator = ModelRole("generator", "unavailable", "scripted", fallback=fallback)
    judge = ModelRole("judge", "independent", "scripted")
    # Different native gateways/roles all use the same host namespace.
    gateways = [ModelGateway(SimpleNamespace(), transport_factory=factory, recorder=records.append) for _ in range(4)]
    for gateway in gateways:
        gateway.configure_dispatch(SharedModelDispatch(tmp_path, limit=2))
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(gateways[n % 4].call_json, generator if n % 2 else judge,
                               system="source-bound", prompt="scripted evidence") for n in range(8)]
        assert all(future.result(timeout=5).data == {"supported": True} for future in futures)
    assert maximum <= 2 and active == 0
    assert len(records) == 12 and sum(p == "fallback" for p, _ in providers) == 4
    assert all(row["system"] == "source-bound" and row["prompt"] == "scripted evidence" for row in records)
    def reject(_):
        raise ValueError("schema refused")
    with pytest.raises(ModelUnavailable, match="schema"):
        gateways[0].call_json(judge, system="source-bound", prompt="bad schema", validate=reject)
    # Exception and schema rejection release slots for later independent calls.
    assert gateways[0].call_json(judge, system="source-bound", prompt="resume").data["supported"]
