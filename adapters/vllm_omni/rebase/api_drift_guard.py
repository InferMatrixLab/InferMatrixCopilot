#!/usr/bin/env python3
# api_drift_guard.py — Inheritance/constructor/unpack drift guard for vllm-omni.
# Ported from the rebase agent (agent/lib/api_drift_guard.py); runs
# standalone with the TARGET repo python (cwd or OMNI_PATH = vllm-omni root).
# Documented divergences cover the pooling constructor/import checks and
# current renderer/EngineClient contracts (see doc/DRIFT_TRIAGE.md).
# Repo-specific maps (INHERITANCE_MAP etc.) are the point of this file living
# in the adapter tree; the copilot core never imports it.
# Run with cwd = vllm-omni repo root (same as tasks/41_check_api_drift.sh).
from __future__ import annotations

import ast
import inspect
import os
import sys
import textwrap
from pathlib import Path

# ---------------------------------------------------------------------------
# Use real extensions by default: current vLLM refuses to import its flash
# attention package when both extensions are blocked, which skips worker and
# serving signatures. For a known incompatible binary, an operator may opt
# into the blocker with IMX_API_DRIFT_BLOCK_FLASH_ATTN=1. That run is partial.
# ---------------------------------------------------------------------------
class _BlockFlashAttnExtensions:
    @staticmethod
    def find_spec(fullname, path, target=None):
        if fullname in (
            "vllm.vllm_flash_attn._vllm_fa2_C",
            "vllm.vllm_flash_attn._vllm_fa3_C",
        ):
            raise ImportError(
                f"Blocked flash attention extension to prevent SIGBUS: {fullname}"
            )
        return None


_block_flash_attn = os.environ.get("IMX_API_DRIFT_BLOCK_FLASH_ATTN", "0")
if _block_flash_attn not in ("0", "1"):
    raise ValueError("IMX_API_DRIFT_BLOCK_FLASH_ATTN must be 0 or 1")
if _block_flash_attn == "1":
    sys.meta_path.insert(0, _BlockFlashAttnExtensions)
    print("PARTIAL  Flash attention extensions blocked by operator request; "
          "affected import checks may be skipped.")

# Ensure relative paths (vllm_omni/...) resolve when invoked from any cwd.
if "OMNI_PATH" in os.environ:
    os.chdir(os.environ["OMNI_PATH"])

INHERITANCE_MAP = [
    ("vllm.renderers", "BaseRenderer", "vllm_omni.inputs.preprocess", "omni_renderer_cls"),
    ("vllm.v1.engine.core_client", "AsyncMPClient", "vllm_omni.engine.stage_engine_core_client", "StageEngineCoreClient"),
    ("vllm.engine.protocol", "EngineClient", "vllm_omni.entrypoints.async_omni", "AsyncOmni"),
    ("vllm.v1.worker.gpu_model_runner", "GPUModelRunner", "vllm_omni.worker.gpu_model_runner", "OmniGPUModelRunner"),
    ("vllm.v1.worker.gpu_model_runner", "GPUModelRunner", "vllm_omni.worker.gpu_generation_model_runner", "GPUGenerationModelRunner"),
    ("vllm.v1.worker.gpu_model_runner", "GPUModelRunner", "vllm_omni.worker.gpu_ar_model_runner", "GPUARModelRunner"),
    ("vllm.v1.core.sched.scheduler", "Scheduler", "vllm_omni.core.sched.omni_ar_scheduler", "OmniARScheduler"),
    ("vllm.v1.core.sched.scheduler", "Scheduler", "vllm_omni.core.sched.omni_generation_scheduler", "OmniGenerationScheduler"),
    ("vllm.v1.engine.output_processor", "RequestState", "vllm_omni.outputs.output_processor", "OmniRequestState"),
    ("vllm.v1.engine.output_processor", "OutputProcessor", "vllm_omni.outputs.output_processor", "MultimodalOutputProcessor"),
    ("vllm.profiler.wrapper", "WorkerProfiler", "vllm_omni.profiler.omni_torch_profiler", "OmniTorchProfilerWrapper"),
]


def import_class(module_path: str, class_name: str):
    mod = __import__(module_path, fromlist=[class_name])
    return getattr(mod, class_name)


# Subclasses whose method bodies call self.<name>(...) on an upstream vLLM base.
# The removed-base-method check below flags calls to base methods that upstream
# has since removed or renamed — e.g. _prepare_extra_chat_template_kwargs ->
# _effective_chat_template_kwargs after vLLM #44285. INHERITANCE_MAP's signature
# check only covers methods omni *overrides*; calls that pass *through* to a
# now-removed base method (which omni does NOT override) slip past it. This list
# closes that gap. Each entry is (omni_module, omni_class); the upstream base is
# discovered from the class MRO so it survives upstream module moves.
CALL_CHECK_MAP = [
    ("vllm_omni.entrypoints.openai.serving_chat", "OmniOpenAIServingChat"),
]


def _self_assigned_attrs(cls) -> set[str]:
    """Instance attributes set via ``self.X = ...`` or ``setattr(self, "X", ...)``
    in *cls*'s own source. These are provided at runtime even though they are
    absent from ``dir(cls)`` — e.g. a base that does ``self.online_renderer = ...``
    in ``__init__``. Without this, such attributes would false-positive."""
    names: set[str] = set()
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(cls)))
    except (OSError, TypeError, SyntaxError):
        return names
    for node in ast.walk(tree):
        targets = (
            node.targets if isinstance(node, ast.Assign)
            else [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign))
            else []
        )
        for t in targets:
            if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                names.add(t.attr)
        if (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "setattr" and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name) and node.args[0].id == "self"
            and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
        ):
            names.add(node.args[1].value)
    return names


def removed_base_method_calls(cls) -> list[tuple[str, int]]:
    """Find ``self.<name>(...)`` calls in *cls*'s own source where <name> is
    provided by neither the class/its MRO (``dir``) nor any runtime instance
    attribute. Such a call targets a base method that upstream removed or
    renamed. Returns sorted ``(name, abs_lineno)`` pairs.

    Only *calls* are flagged (not bare attribute reads): removed/renamed base
    *methods* are always called, and restricting to calls keeps false positives
    from dynamically-set instance attributes near zero.
    """
    provided: set[str] = set(dir(cls))
    for klass in cls.__mro__:
        provided |= _self_assigned_attrs(klass)

    try:
        src_lines, start = inspect.getsourcelines(cls)
        tree = ast.parse(textwrap.dedent("".join(src_lines)))
    except (OSError, TypeError, SyntaxError):
        return []

    found: dict[str, int] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        fn = node.func
        if not (isinstance(fn.value, ast.Name) and fn.value.id == "self"):
            continue
        name = fn.attr
        if name.startswith("__") or name in provided:
            continue
        found.setdefault(name, start + fn.lineno - 1)
    return sorted(found.items(), key=lambda kv: kv[1])


def _check_removed_base_attrs() -> list[str]:
    """For each CALL_CHECK_MAP subclass, report ``self.<method>()`` calls that no
    longer resolve against the class or its upstream vLLM base."""
    findings: list[str] = []
    for omni_mod, omni_cls_name in CALL_CHECK_MAP:
        try:
            OmniCls = import_class(omni_mod, omni_cls_name)
        except Exception as e:  # noqa: BLE001
            print(f"SKIP  {omni_cls_name} removed-base-method check: import error — {e}")
            continue
        vllm_bases = [
            c for c in OmniCls.__mro__[1:] if getattr(c, "__module__", "").startswith("vllm.")
        ]
        if not vllm_bases:
            print(f"SKIP  {omni_cls_name} removed-base-method check: no upstream vLLM base in MRO")
            continue
        base = vllm_bases[0]
        omni_file = omni_mod.replace(".", "/") + ".py"
        bad = removed_base_method_calls(OmniCls)
        if not bad:
            print(
                f"  OK   {omni_cls_name}: all self.*() calls resolve against "
                f"{base.__module__}.{base.__name__}"
            )
        for name, line in bad:
            msg = (
                f"MISMATCH {omni_cls_name} calls self.{name}() at {omni_file}:{line} "
                f"but '{name}' exists on neither {omni_cls_name} nor its upstream base "
                f"{base.__module__}.{base.__name__} — upstream removed/renamed it. "
                f"Port the call to the new API (inspect the base under $VLLM_PATH)."
            )
            print(msg)
            findings.append(msg)
    return findings


def _forbidden_test_imports() -> list[str]:
    """Catch test-suite drift: symbols removed from vllm.utils.torch_utils, etc.

    Task 42 also resolves all ``from vllm.*`` imports under ``tests/``; this AST
    guard flags the common ``cuda_device_count_stateless`` regression even if
    import resolution order changes.
    """
    findings: list[str] = []
    tests_dir = Path("tests")
    if not tests_dir.is_dir():
        return findings

    forbidden_torch_utils_names = frozenset({"cuda_device_count_stateless"})
    for path in sorted(tests_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if node.module != "vllm.utils.torch_utils":
                continue
            for alias in node.names or []:
                name = alias.name
                if name == "*":
                    findings.append(
                        f"FORBIDDEN  {path}:{node.lineno}: "
                        "wildcard import from vllm.utils.torch_utils in tests/; "
                        "import only stable helpers or use vllm.platforms.current_platform"
                    )
                    continue
                if name in forbidden_torch_utils_names:
                    findings.append(
                        f"FORBIDDEN  {path}:{node.lineno}: "
                        f"`from {node.module} import {name}` — removed upstream; "
                        "use `from vllm.platforms import current_platform` and "
                        "`current_platform.device_count()`"
                    )
    return findings


def _check_inheritance() -> list[str]:
    mismatches: list[str] = []
    for up_mod, up_cls_name, omni_mod, omni_cls_name in INHERITANCE_MAP:
        try:
            UpCls = import_class(up_mod, up_cls_name)
            OmniCls = import_class(omni_mod, omni_cls_name)
            # OmniRenderer is a mixin; the actual upstream subclass is
            # constructed by this factory rather than declared statically.
            if omni_cls_name == "omni_renderer_cls":
                OmniCls = OmniCls(UpCls)
                omni_cls_name = OmniCls.__name__
        except Exception as e:
            print(f"SKIP  {omni_cls_name}: import error — {e}")
            continue

        if not issubclass(OmniCls, UpCls):
            print(f"SKIP  {omni_cls_name}: not a subclass of {up_cls_name}")
            continue

        if up_cls_name == "EngineClient" and inspect.isabstract(OmniCls):
            msg = (
                f"MISMATCH {omni_cls_name}: missing EngineClient implementations "
                f"{sorted(OmniCls.__abstractmethods__)}; the engine cannot be instantiated"
            )
            print(msg)
            mismatches.append(msg)

        for name in dir(OmniCls):
            if name.startswith("__") and name != "__init__":
                continue
            omni_method = getattr(OmniCls, name, None)
            base_method = getattr(UpCls, name, None)
            if not (callable(omni_method) and callable(base_method)):
                continue
            if omni_method is base_method:
                continue
            try:
                base_sig = inspect.signature(base_method)
                omni_sig = inspect.signature(omni_method)
            except (ValueError, TypeError):
                continue

            omni_has_var_keyword = any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in omni_sig.parameters.values()
            )

            missing = []
            for pname, param in base_sig.parameters.items():
                if pname in omni_sig.parameters:
                    continue
                if omni_has_var_keyword and param.kind in (
                    inspect.Parameter.KEYWORD_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                ):
                    continue
                missing.append(pname)
            positional_kinds = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            base_positional = [p.name for p in base_sig.parameters.values() if p.kind in positional_kinds]
            omni_positional = [p.name for p in omni_sig.parameters.values() if p.kind in positional_kinds]
            omni_has_var_positional = any(
                p.kind == inspect.Parameter.VAR_POSITIONAL for p in omni_sig.parameters.values()
            )
            lost_positional = [] if omni_has_var_positional else base_positional[len(omni_positional):]
            if missing or lost_positional:
                msg = (
                    f"MISMATCH {omni_cls_name}.{name}(): missing params {missing}; "
                    f"incompatible positional params {lost_positional}\n"
                    f"  Base : {base_sig}\n"
                    f"  Omni : {omni_sig}"
                )
                print(msg)
                mismatches.append(msg)
            else:
                print(f"  OK   {omni_cls_name}.{name}")

    return mismatches


def main() -> int:
    mismatches = _check_inheritance()
    constructor_call_checks = [
        (
            "vllm.entrypoints.openai.responses.serving",
            "OpenAIServingResponses",
            "OpenAIServingResponses",
            "vllm_omni/entrypoints/openai/api_server.py",
        ),
        (
            "vllm.entrypoints.openai.completion.serving",
            "OpenAIServingCompletion",
            "OpenAIServingCompletion",
            "vllm_omni/entrypoints/openai/api_server.py",
        ),
        (
            # Deliberate divergence from the parent guard, which named
            # OpenAIServingPooling for both: upstream's class is
            # ServingPooling and omni's api_server imports/calls it under
            # that name, so the parent's entry always import-failed into a
            # silent SKIP and pooling constructor drift was never checked.
            "vllm.entrypoints.pooling.pooling.serving",
            "ServingPooling",
            "ServingPooling",
            "vllm_omni/entrypoints/openai/api_server.py",
        ),
        (
            "vllm.entrypoints.pooling.embed.serving",
            "ServingEmbedding",
            "OpenAIServingEmbedding",
            "vllm_omni/entrypoints/openai/api_server.py",
        ),
    ]

    def required_params_without_self(sig: inspect.Signature) -> list[inspect.Parameter]:
        required: list[inspect.Parameter] = []
        for p in sig.parameters.values():
            if p.name == "self":
                continue
            if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            if p.default is inspect._empty:
                required.append(p)
        return required

    for mod_path, import_class_name, call_name, rel_file in constructor_call_checks:
        try:
            cls = import_class(mod_path, import_class_name)
            init_sig = inspect.signature(cls.__init__)
            required = required_params_without_self(init_sig)
        except Exception as e:
            # Deliberate divergence from the parent, which SKIPped here: an
            # unimportable checked class IS the drift this guard exists to
            # catch (upstream moved/renamed it), and the silent SKIP is how
            # the dead pooling entry went unnoticed. INHERITANCE_MAP keeps
            # its SKIP (platform-legitimate absences); these four serving
            # classes are unconditional in any installed env.
            msg = (f"MISMATCH {call_name}.__init__ call-check: cannot import "
                   f"{mod_path}.{import_class_name} — upstream moved/renamed "
                   f"it (or the env is not installed): {e}")
            print(msg)
            mismatches.append(msg)
            continue

        target_file = Path(rel_file)
        if not target_file.exists():
            msg = f"MISMATCH {call_name}.__init__ call-check: target file missing: {target_file}"
            print(msg)
            mismatches.append(msg)
            continue

        try:
            tree = ast.parse(target_file.read_text(), filename=str(target_file))
        except Exception as e:
            msg = f"MISMATCH {call_name}.__init__ call-check: parse error in {target_file}: {e}"
            print(msg)
            mismatches.append(msg)
            continue

        calls: list[ast.Call] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Name) and fn.id == call_name:
                    calls.append(node)
                elif isinstance(fn, ast.Attribute) and fn.attr == call_name:
                    calls.append(node)

        if not calls:
            msg = f"MISMATCH {call_name}.__init__ call-check: no constructor call found in {target_file}"
            print(msg)
            mismatches.append(msg)
            continue

        for call in calls:
            positional_count = len(call.args)
            keyword_names = {kw.arg for kw in call.keywords if kw.arg is not None}
            missing_required: list[str] = []

            for idx, param in enumerate(required):
                if idx < positional_count:
                    continue
                if param.name in keyword_names:
                    continue
                missing_required.append(param.name)

            if missing_required:
                msg = (
                    f"MISMATCH {call_name}.__init__ call at "
                    f"{target_file}:{getattr(call, 'lineno', '?')}: "
                    f"missing required args {missing_required}\n"
                    f"  Required by upstream: {init_sig}\n"
                    f"  Call positional args: {positional_count}, keywords: {sorted(keyword_names)}"
                )
                print(msg)
                mismatches.append(msg)
            else:
                print(
                    f"  OK   {call_name}.__init__ call at "
                    f"{target_file}:{getattr(call, 'lineno', '?')}"
                )

    try:
        from vllm.v1.engine.utils import launch_core_engines as _launch_core_engines_fn

        launch_src = inspect.getsource(_launch_core_engines_fn)
        launch_tree = ast.parse(launch_src)
        yield_tuple_sizes: set[int] = set()
        for node in ast.walk(launch_tree):
            if isinstance(node, ast.Yield) and isinstance(node.value, ast.Tuple):
                yield_tuple_sizes.add(len(node.value.elts))

        expected_unpack_arity = max(yield_tuple_sizes) if yield_tuple_sizes else None
    except Exception as e:
        expected_unpack_arity = None
        print(f"SKIP  launch_core_engines unpack-check: failed to inspect upstream source — {e}")

    if expected_unpack_arity is not None:
        target_file = Path("vllm_omni/engine/async_omni_engine.py")
        if not target_file.exists():
            msg = f"MISMATCH launch_core_engines unpack-check: target file missing: {target_file}"
            print(msg)
            mismatches.append(msg)
        else:
            try:
                target_src = target_file.read_text()
            except Exception as e:
                msg = f"MISMATCH launch_core_engines unpack-check: read error in {target_file}: {e}"
                print(msg)
                mismatches.append(msg)
                target_src = ""

            if target_src and ("launch_core_engines" not in target_src and "launch_cm" not in target_src):
                print(
                    "SKIP  launch_core_engines unpack-check: "
                    f"{target_file} does not inline launch_core_engines (omni stage client path)"
                )
                tree = None
            elif target_src:
                try:
                    tree = ast.parse(target_src, filename=str(target_file))
                except Exception as e:
                    msg = f"MISMATCH launch_core_engines unpack-check: parse error in {target_file}: {e}"
                    print(msg)
                    mismatches.append(msg)
                    tree = None
            else:
                tree = None

            if tree is not None:
                found_any = False

                def _check_unpack_arity(targets, lineno):
                    """Check one unpack target tuple's arity and report."""
                    nonlocal found_any
                    for target in targets:
                        if isinstance(target, ast.Tuple):
                            found_any = True
                            actual = len(target.elts)
                            if actual != expected_unpack_arity:
                                msg = (
                                    "MISMATCH launch_core_engines unpack-check at "
                                    f"{target_file}:{lineno}: "
                                    f"expected {expected_unpack_arity} values, got {actual}"
                                )
                                print(msg)
                                mismatches.append(msg)
                            else:
                                print(
                                    "  OK   launch_core_engines unpack arity at "
                                    f"{target_file}:{lineno} "
                                    f"({actual})"
                                )

                # Pattern 1: launch_cm.__enter__() → unpack
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Assign):
                        continue
                    call = node.value
                    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
                        continue
                    if call.func.attr != "__enter__":
                        continue
                    if not (isinstance(call.func.value, ast.Name) and call.func.value.id == "launch_cm"):
                        continue
                    _check_unpack_arity(node.targets, getattr(node, "lineno", "?"))

                # Pattern 2: with launch_cm as <target>:  → unpack <target> in body
                for node in ast.walk(tree):
                    if not isinstance(node, ast.With):
                        continue
                    for item in (node.items or []):
                        ctx = item.context_expr
                        if not (isinstance(ctx, ast.Name) and ctx.id == "launch_cm"):
                            continue
                        target_var = item.optional_vars
                        if target_var is None:
                            continue
                        target_name = None
                        if isinstance(target_var, ast.Name):
                            target_name = target_var.id
                        # Look inside the with-body for assignment unpacking target_var
                        for body_node in ast.walk(node):
                            if not isinstance(body_node, ast.Assign):
                                continue
                            if isinstance(body_node.value, ast.Name) and body_node.value.id == target_name:
                                _check_unpack_arity(body_node.targets, getattr(body_node, "lineno", "?"))

                if not found_any:
                    msg = (
                        "MISMATCH launch_core_engines unpack-check: "
                        f"no launch_cm.__enter__ unpack assignment found in {target_file}"
                    )
                    print(msg)
                    mismatches.append(msg)

    for msg in _forbidden_test_imports():
        print(msg)
        mismatches.append(msg)

    # Removed/renamed base-method calls: omni subclasses calling self.<name>()
    # where the upstream base no longer defines <name> (e.g. #44285 renamed
    # _prepare_extra_chat_template_kwargs -> _effective_chat_template_kwargs).
    # The function prints its own OK/MISMATCH lines.
    mismatches.extend(_check_removed_base_attrs())

    if mismatches:
        print(f"\n❌ Found {len(mismatches)} signature mismatch(es). Fix before proceeding.")
        return 1
    print("\n✅ All overridden methods are signature-compatible.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
