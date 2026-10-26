"""Module-prompt rendering — neutral scaffolding for the parent's
`prompts/builder.py`, with every repo-domain value arriving as adapter data
(templates + `prompt_data.yaml`). Renders are BYTE-IDENTICAL to the parent
given equal inputs — the prompt goldens pin two modules end-to-end, and the
prompt bytes are prompt-cache load-bearing (Rev 8 §4).

The parent resolved commits and commit lists with live git calls inside the
builder; those stay (same commands, injectable runner) so the golden can fix
them deterministically.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

import yaml


@dataclass(frozen=True)
class ModulePromptData:
    """The builder's module maps + template selection, loaded from the
    adapter's `prompt_data.yaml`. `pytest_wrapper` is the command PREFIX the
    test/import lines are built from ({script_dir} substituted) — goldens use
    the parent's script path to prove parity; live runs use the shipped
    wrapper."""

    templates_dir: Path
    template: str
    pytest_wrapper: str
    template_live: str = ""
    pytest_wrapper_live: str = ""
    debug_prompt_template: str = ""
    debug_prompt_template_live: str = ""
    module_vllm_paths: Mapping[str, str] = field(default_factory=dict)
    module_omni_files: Mapping[str, str] = field(default_factory=dict)
    module_test_map: Mapping[str, Sequence[str]] = field(default_factory=dict)
    module_import_check: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, rebase_data_dir: Path) -> "ModulePromptData":
        d = Path(rebase_data_dir)
        data = yaml.safe_load((d / "prompt_data.yaml").read_text(
            encoding="utf-8"))
        return cls(templates_dir=d / "templates",
                   template=data["template"],
                   pytest_wrapper=data["pytest_wrapper"],
                   template_live=data.get("template_live", ""),
                   pytest_wrapper_live=data.get("pytest_wrapper_live", ""),
                   debug_prompt_template=data.get("debug_prompt_template", ""),
                   debug_prompt_template_live=data.get(
                       "debug_prompt_template_live", ""),
                   module_vllm_paths=data.get("module_vllm_paths", {}),
                   module_omni_files=data.get("module_omni_files", {}),
                   module_test_map=data.get("module_test_map", {}),
                   module_import_check=data.get("module_import_check", {}))


def _run_git(cmd: list[str], cwd: str, timeout: int = 15) -> str:
    try:
        proc = subprocess.run(["git"] + cmd, capture_output=True, text=True,
                              timeout=timeout, cwd=cwd)
        return proc.stdout.strip()
    except Exception:  # noqa: BLE001 - parent parity: best-effort resolution
        return ""


def _plan_review_contract(log_dir: str = "", session: str = "module") -> str:
    """The plan-review contract with concrete paths injected (parent-verbatim
    text — the gate description the agent loop enforces)."""
    plan_dir = (f"{log_dir}/plans/{session}/dispatch_initial" if log_dir
                else "${LOG_DIR}/plans/${SESSION}/dispatch_initial")
    return f"""\
## CRITICAL: Plan-Review-Decision Gate (MANDATORY)

The `edit_file`, `run_pytest`, and `run_precommit` tools are LOCKED until you
complete the plan-review-decision pipeline. You CANNOT edit code or run tests
until you write a .decision.md file. Use `read_file`, `grep`, `run_shell`,
`git_show_upstream`, `git_show_omni_main` for exploration.

### YOUR FIRST TASK: Write plan files, then call `request_plan_review`

Step 1 — write_file → {plan_dir}/plan-v0-XXXX.json
  (JSON: {{"version":3,"plan_id":"v0-XXXX","intent":"...","changes":[...],"verify":[...],"risks":[...]}})

Step 1b — write_file → {plan_dir}/plan-v0-XXXX.md (full narrative)

Step 2 — request_plan_review tool:
  plan_json_path: "{plan_dir}/plan-v0-XXXX.json"
  plan_md_path: "{plan_dir}/plan-v0-XXXX.md"
  kind: "rebase"

Step 3 — write_file → {plan_dir}/plan-v0-XXXX.decision.md (accept|partial|reject per critique)

Step 4 — NOW edit_file, run_pytest, run_precommit are unlocked. Edit code.

Max {{PLAN_REVIEW_MAX_ROUNDS}} revision rounds. If review fails, proceed anyway."""


def _broken_imports_section(imports: list[dict]) -> str:
    if not imports:
        return ""
    lines = [
        "## Pre-diagnosed: Upstream architectural changes (Phase 1 detected)",
        "",
        "These imports/functions were CHANGED or REMOVED by upstream vLLM commits.",
        "The diff excerpts show what changed — do a proper port matching the new API.",
        "Do NOT create no-op stubs or compat shims.",
        "",
    ]
    seen = set()
    for imp in imports:
        key = (imp.get("upstream_module", ""), imp.get("symbol", ""))
        if key in seen:
            continue
        seen.add(key)
        omni_file = imp.get("omni_file", "?")
        symbol = imp.get("symbol", "?")
        upstream_mod = imp.get("upstream_module", "?")
        lines.append(f"### {omni_file}: `{symbol}`")
        lines.append(f"**Removed/moved from**: `{upstream_mod}`")
        for c in imp.get("vllm_commits", [])[:2]:
            lines.append(f"**Commit**: `{c['hash']}` {c['message']}")
            if c.get("diff"):
                lines.append("**Diff excerpt**:")
                lines.append("```diff")
                lines.append(c["diff"][:2000])
                lines.append("```")
        call_sites = imp.get("call_sites", [])
        if call_sites:
            lines.append("**All affected call sites** (must be updated):")
            for cs in call_sites[:12]:
                lines.append(f"  - `{cs}`")
        lines.append("")
    return "\n".join(lines) + "\n"


def _format_module_test_plan(plan: dict,
                             baseline_ref: str = "origin/main"
                             ) -> str:
    if not plan:
        return ""
    lines = ["## Tests you must pass", ""]
    ci = plan.get("ci_tests", [])
    if ci:
        lines.append("### From Buildkite CI (must pass)")
        for slug in ci:
            lines.append(f"- `{slug}`")
        lines.append("")
    changes = plan.get("upstream_changes", [])
    if changes:
        lines.append("### Upstream test changes (compare with "
                     f"{baseline_ref})")
        for c in changes:
            ct = c.get("type", "?")
            path = c.get("path", "?")
            new_path = c.get("new_path", "")
            if ct == "renamed":
                lines.append(f"- **RENAMED**: `{path}` → `{new_path}`")
            elif ct == "deleted":
                lines.append(f"- **DELETED**: `{path}` — check if omni still needs it")
            elif ct == "modified":
                lines.append(f"- **MODIFIED**: `{path}` — `git_show_test_baseline` to see {baseline_ref} version")
            else:
                lines.append(f"- **{ct.upper()}**: `{path}`")
        lines.append("")
    return "\n".join(lines) + "\n"


_KNOWLEDGE_GUIDANCE = (
    "**Knowledge base.** Before exploring an unfamiliar component, model or CI\n"
    "behaviour with shell, run `doc_search` with the component/model/symbol name\n"
    "and `doc_read` the best match by its reported path. A curated page often\n"
    "states design intent or the owning component outright, which grep cannot\n"
    "tell you. This is documented design; `search_debug_memory` is incident\n"
    "history — consult both. Cite the page path in your plan when it informed a\n"
    "decision. If a page and the code disagree, the CODE is authoritative for\n"
    "this rebase: say so in your decision file rather than editing to match."
)


def _with_knowledge_guidance(adaptive_guidance: str) -> str:
    """Render the ADAPTIVE_GUIDANCE slot with the knowledge-base instruction.

    Carried here rather than in the templates on purpose: the shipped templates
    are DATA held byte-identical to the parent agent's copies (enforced by
    test_adapter_knowledge.test_templates_are_parent_verbatim), so behaviour
    changes belong in builder-supplied tokens.
    """
    rest = (adaptive_guidance or "").strip()
    return f"{_KNOWLEDGE_GUIDANCE}\n\n{rest}" if rest else _KNOWLEDGE_GUIDANCE


def build_module_prompt(
    module: str,
    data: ModulePromptData,
    *,
    vllm_path: str,
    omni_path: str,
    script_dir: str,
    last_rebase_vllm_commit: str = "",
    cuda_devices: str = "0,1",
    hf_home: str = "/model",
    max_debug_retries: int = 3,
    log_dir: str = "",
    signal_dir: str = "",
    rebase_run_id: str = "",
    plan_review_max_rounds: int = 2,
    broken_imports: list[dict] | None = None,
    module_test_plan: dict | None = None,
    adaptive_guidance: str = "",
    live: bool = False,
    baseline_ref: str = "origin/main",
    target_branch: str = "",
    upstream_commit: str = "",
    run_git: Callable[[list[str], str], str] | None = None,
) -> str:
    """Byte-parity render of the parent's `build_module_prompt`. `script_dir`
    is what the parent derived from its own file location (the agent root) —
    injected here so goldens can pin the parent's exact paths."""
    git = run_git or (lambda cmd, cwd: _run_git(cmd, cwd))

    vllm_commit = git(["rev-parse", "--short", "HEAD"], vllm_path)
    omni_commit = git(["rev-parse", "--short", "HEAD"], omni_path)

    upstream_paths = data.module_vllm_paths.get(module, "")
    commit_list = ""
    if last_rebase_vllm_commit and upstream_paths:
        commit_list = git(
            ["log", "--oneline", f"{last_rebase_vllm_commit}..HEAD", "--"]
            + upstream_paths.split(), vllm_path)
    if not commit_list:
        commit_list = "(no relevant commits)"

    # live mode: the shipped wrapper replaces the retired shell script, and
    # the import-check gets PROPER quoting — the parent single-quoted a
    # payload containing single quotes, so bash stripped them and the check
    # died with NameError: OK. Parity mode reproduces the parent bytes
    # exactly (goldens); both divergences are recorded in DRIFT_TRIAGE.
    wrapper_template = (data.pytest_wrapper_live
                        if live and data.pytest_wrapper_live
                        else data.pytest_wrapper)
    wrapper = wrapper_template.format(script_dir=script_dir)
    pytest_cmds = [f"{wrapper} -vv -s {test_path}"
                   for test_path in data.module_test_map.get(module, [])]
    test_commands = "\n".join(pytest_cmds)
    check = data.module_import_check.get(module, 'print("OK")')
    if live:
        import shlex
        import_check_cmd = f"{wrapper} python -c {shlex.quote(check)}"
    else:
        import_check_cmd = f"{wrapper} python -c '{check}'"

    template_name = (data.template_live if live and data.template_live
                     else data.template)
    template_file = Path(data.templates_dir) / template_name
    if not template_file.exists():
        return f"Error: template not found at {template_file}"
    template = template_file.read_text()

    vars_map = {
        "MODULE_NAME":       module,
        "MODULE_KEY":        module,
        "VLLM_PATH":         vllm_path,
        "OMNI_PATH":         omni_path,
        "VLLM_COMMIT":       vllm_commit,
        "OMNI_COMMIT":       omni_commit,
        "CUDA_DEVICES":      cuda_devices,
        "HF_HOME":           hf_home,
        "COMMIT_LIST":       commit_list,
        "OMNI_FILES":        data.module_omni_files.get(module, ""),
        "UPSTREAM_FILES":    upstream_paths,
        "IMPORT_CHECK":      import_check_cmd,
        "PYTHON3_CMD":       "python3",
        "PYTEST_COMMANDS":   test_commands,
        "SIGNAL_DIR":        signal_dir,
        "MAX_DEBUG_RETRIES": str(max_debug_retries),
        "PROMPT_SOURCE":     "",
        "ADAPTIVE_GUIDANCE": _with_knowledge_guidance(adaptive_guidance),
        "KILL_TEST_SCRIPT":  f"{script_dir}/lib/kill_test_tree.sh",
        "REMOTE_CONTEXT":    "### Execution mode: LOCAL",
        "DEBUG_MEMORY":      "Use the `search_debug_memory` tool to query past fixes. Do NOT read the debug_memory.md file directly.",
        "DEBUG_MEMORY_FILE": f"{script_dir}/memory/debug_memory.md",
        "DEBUG_MEMORY_CLI":  "Use `search_debug_memory` tool instead of CLI.",
        "RUN_ID":            rebase_run_id,
        "PLAN_REVIEW_CONTRACT": _plan_review_contract(
            log_dir=log_dir, session=f"module-{module}"),
        "BROKEN_IMPORTS_SECTION": _broken_imports_section(broken_imports or []),
        "MODULE_TEST_PLAN": _format_module_test_plan(
            module_test_plan or {}, baseline_ref=baseline_ref),
        "PLAN_REVIEW_MAX_ROUNDS": str(plan_review_max_rounds),
        "SCRIPT_DIR":        script_dir,
        "SESSION":           f"module-{module}",
    }
    if live:
        # Use the run's pin even when the checkout has drifted; displaying
        # both lets the agent verify the environment before making a port.
        # Legacy callers without run context use observed checkout facts,
        # never the release or prior-alignment SHA from an earlier campaign.
        vars_map.update({
            "TARGET_BRANCH": (target_branch
                              or git(["symbolic-ref", "--quiet", "--short", "HEAD"],
                                     vllm_path)
                              or "unspecified (detached checkout)"),
            "UPSTREAM_COMMIT": (upstream_commit
                                or git(["rev-parse", "HEAD"], vllm_path)
                                or "unresolved (verify upstream checkout HEAD)"),
            "LAST_REBASE_UPSTREAM_COMMIT": (last_rebase_vllm_commit
                                            or "not supplied"),
            "BASELINE_REF": baseline_ref,
        })
    for key, value in vars_map.items():
        template = template.replace("{" + key + "}", value)
    return template


def build_debug_prompt(module: str, traceback: str,
                       debug_template: str, test_path: str = "",
                       **kwargs) -> str:
    """Debug prompt from the adapter's template (domain text is data; the
    parent inlined it — parity pinned by golden)."""
    return debug_template.format(module=module, traceback=traceback,
                                 test_path=test_path)
