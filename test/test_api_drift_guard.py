"""Live guard regressions for dynamically built renderers and engine clients."""

from abc import ABC, abstractmethod
from pathlib import Path
import runpy
import sys

import pytest


@pytest.mark.parametrize("setting,blocked", [(None, False), ("0", False), ("1", True)])
def test_flash_extension_blocking_requires_explicit_opt_in(monkeypatch, setting, blocked):
    monkeypatch.delenv("OMNI_PATH", raising=False)
    monkeypatch.delenv("IMX_API_DRIFT_BLOCK_FLASH_ATTN", raising=False)
    if setting is not None:
        monkeypatch.setenv("IMX_API_DRIFT_BLOCK_FLASH_ATTN", setting)
    monkeypatch.setattr(sys, "meta_path", list(sys.meta_path))
    path = Path(__file__).resolve().parents[1] / "adapters/vllm_omni/rebase/api_drift_guard.py"
    namespace = runpy.run_path(str(path))
    finder = namespace["_BlockFlashAttnExtensions"]
    assert (finder in sys.meta_path) is blocked
    if blocked:
        with pytest.raises(ImportError, match="Blocked flash attention extension"):
            finder.find_spec("vllm.vllm_flash_attn._vllm_fa2_C", None)


def test_invalid_flash_extension_setting_fails(monkeypatch):
    monkeypatch.delenv("OMNI_PATH", raising=False)
    monkeypatch.setenv("IMX_API_DRIFT_BLOCK_FLASH_ATTN", "yes")
    path = Path(__file__).resolve().parents[1] / "adapters/vllm_omni/rebase/api_drift_guard.py"
    with pytest.raises(ValueError, match="must be 0 or 1"):
        runpy.run_path(str(path))


@pytest.fixture
def guard(monkeypatch):
    monkeypatch.delenv("OMNI_PATH", raising=False)
    monkeypatch.setattr(sys, "meta_path", list(sys.meta_path))
    path = Path(__file__).resolve().parents[1] / "adapters/vllm_omni/rebase/api_drift_guard.py"
    namespace = runpy.run_path(str(path))
    return namespace["_check_inheritance"]


@pytest.mark.parametrize("accepts_cache_flag", [False, True])
def test_guard_checks_the_actual_dynamic_renderer(guard, accepts_cache_flag):
    class BaseRenderer:
        def _process_singleton(self, prompt, *, skip_mm_cache=False):
            pass

    class OldMixin:
        def _process_singleton(self, prompt):
            pass

    class CurrentMixin:
        def _process_singleton(self, prompt, *, skip_mm_cache=False):
            pass

    def factory(base):
        return type("OmniBaseRenderer", (CurrentMixin if accepts_cache_flag else OldMixin, base), {})

    guard.__globals__["INHERITANCE_MAP"] = [("upstream", "BaseRenderer", "omni", "omni_renderer_cls")]
    guard.__globals__["import_class"] = lambda module, name: BaseRenderer if module == "upstream" else factory
    findings = guard()
    if accepts_cache_flag:
        assert findings == []
    else:
        assert len(findings) == 1
        assert "skip_mm_cache" in findings[0]


def test_guard_rejects_a_new_abstract_engine_client_method(guard):
    class EngineClient(ABC):
        @abstractmethod
        async def release_kv_cache_memory(self):
            pass

    class MissingAPI(EngineClient):
        pass

    class CurrentAPI(EngineClient):
        async def release_kv_cache_memory(self):
            pass

    guard.__globals__["INHERITANCE_MAP"] = [("upstream", "EngineClient", "omni", "AsyncOmni")]
    guard.__globals__["import_class"] = lambda module, name: EngineClient if module == "upstream" else MissingAPI
    findings = guard()
    assert len(findings) == 1
    assert "release_kv_cache_memory" in findings[0]
    guard.__globals__["import_class"] = lambda module, name: EngineClient if module == "upstream" else CurrentAPI
    assert guard() == []


@pytest.mark.parametrize("keyword_only", [False, True])
def test_guard_preserves_optional_positional_parameters(guard, keyword_only):
    class Base:
        def make_request_output(self, tokens, error=None):
            pass

    class KeywordOnly(Base):
        def make_request_output(self, tokens, *, error=None):
            pass

    class Compatible(Base):
        def make_request_output(self, tokens, error=None):
            pass

    guard.__globals__["INHERITANCE_MAP"] = [("upstream", "Base", "omni", "Omni")]
    child = KeywordOnly if keyword_only else Compatible
    guard.__globals__["import_class"] = lambda module, name: Base if module == "upstream" else child
    findings = guard()
    if keyword_only:
        assert len(findings) == 1
        assert "positional" in findings[0] and "error" in findings[0]
    else:
        assert findings == []
