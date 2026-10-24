#!/usr/bin/env python3
"""检查沉淀层页面的 frontmatter、标签分类法、孤页与陈旧度（SCHEMA.md 机制）。

范围：general/ 与 repos/ 下的沉淀层页面。证据层（incidents/、history/、
results/）与 repos/jianghan-roleplay-data-pipeline/ 不检查。
结构/索引/链接/错题校验属于 check_knowledge_tree.py，这里不重复。
"""

from __future__ import annotations

import datetime
import re
import sys
from importlib.resources import files
from pathlib import Path

import yaml

from ..format import frontmatter_value

ROOT = Path.cwd() / "knowledge"
# SCHEMA.md is documentation about the tree, not a page in it, so it moved to
# doc/knowledge/ with the rest of the authoring docs. The taxonomy it defines is
# still enforced here — the rule follows the file rather than being dropped.
SCHEMA = ROOT.parent / "doc" / "knowledge" / "SCHEMA.md"
SYNTH_ROOTS = (ROOT / "general", ROOT / "repos")
RAW_PARTS = {"incidents", "history", "results"}
SKIP_PARTS: set[str] = set()
SKIP_SUBTREES = (ROOT / "repos" / "jianghan-roleplay-data-pipeline",)
TYPES = {"rule", "guide", "architecture", "index"}
CONFIDENCE = {"high", "medium", "low"}
REQUIRED = ("title", "created", "updated", "type", "tags")
ADAPTER_KNOWLEDGE_KEYS = {
    "source",
    "repo_subdir",
    "briefing_docs",
    "briefing_docs_extra",
    "performance_briefing_docs",
    # single knowledge page injected as the Strict reviewer's repo-specific
    # checklist (first 8k chars); resolved relative to the knowledge root
    "review_checklist",
}
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
STALE_DAYS = 365

errors: list[str] = []
warnings: list[str] = []


def display(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def taxonomy() -> set[str]:
    """SCHEMA.md 标签分类法：分类法小节里所有反引号 token。"""
    text = SCHEMA.read_text(encoding="utf-8")
    m = re.search(r"## 标签分类法(.*?)\n## ", text, re.DOTALL)
    if not m:
        errors.append("SCHEMA.md 缺少 ## 标签分类法 小节")
        return set()
    return set(re.findall(r"`([^`]+)`", m.group(1)))


def synthesized_pages() -> list[Path]:
    pages = []
    for root in SYNTH_ROOTS:
        for p in sorted(root.rglob("*.md")):
            parts = set(p.relative_to(ROOT).parts)
            if parts & RAW_PARTS or parts & SKIP_PARTS:
                continue
            if any(p.is_relative_to(s) for s in SKIP_SUBTREES):
                continue
            pages.append(p)
    return pages


def check_adapter_briefings() -> None:
    adapter_root = ROOT.parent / "adapters"
    for manifest in sorted(adapter_root.glob("*/manifest.yaml")):
        try:
            data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            errors.append(f"adapter manifest YAML 无法解析：{manifest}: {exc}")
            continue
        knowledge = data.get("knowledge") or {}
        if not isinstance(knowledge, dict):
            errors.append(f"adapter knowledge 必须是对象：{manifest}")
            continue
        unknown = sorted(set(knowledge) - ADAPTER_KNOWLEDGE_KEYS)
        if unknown:
            errors.append(
                f"adapter knowledge 出现未授权字段 {unknown}：{manifest}")
        for field in (
            "briefing_docs",
            "briefing_docs_extra",
            "performance_briefing_docs",
        ):
            docs = knowledge.get(field) or []
            if (not isinstance(docs, list)
                    or not all(isinstance(d, str) for d in docs)):
                errors.append(f"adapter {field} 必须是字符串列表：{manifest}")
                continue
            for doc in docs:
                if set(Path(doc).parts) & RAW_PARTS:
                    errors.append(
                        f"adapter {field} 禁止加载原始证据层页面（{doc}）：{manifest}")
        checklist = knowledge.get("review_checklist")
        if checklist is not None:
            if not isinstance(checklist, str):
                errors.append(
                    f"adapter review_checklist 必须是字符串：{manifest}")
            elif set(Path(checklist).parts) & RAW_PARTS:
                errors.append(
                    f"adapter review_checklist 禁止加载原始证据层页面"
                    f"（{checklist}）：{manifest}")
            elif not (ROOT / checklist).is_file():
                errors.append(
                    f"adapter review_checklist 指向不存在的页面"
                    f"（{checklist}）：{manifest}")


def frontmatter(path: Path) -> dict | None:
    text = path.read_text(encoding="utf-8-sig")
    try:
        meta = frontmatter_value(text)
    except (ValueError, yaml.YAMLError):
        return None
    return meta if isinstance(meta, dict) else None


def check_page(path: Path, tags_allowed: set[str]) -> dict | None:
    meta = frontmatter(path)
    if meta is None:
        errors.append(f"沉淀层页面缺少 frontmatter：{display(path)}")
        return None
    for field in REQUIRED:
        if field not in meta:
            errors.append(f"frontmatter 缺少字段 {field}：{display(path)}")
    if meta.get("type") not in TYPES:
        errors.append(f"type 不合法（{meta.get('type')}）：{display(path)}")
    for d in ("created", "updated"):
        v = str(meta.get(d, ""))
        if not DATE.match(v):
            errors.append(f"{d} 不是 YYYY-MM-DD（{v}）：{display(path)}")
    tags = meta.get("tags") or []
    if not isinstance(tags, list) or not tags:
        errors.append(f"tags 缺失或为空：{display(path)}")
    else:
        for t in tags:
            if t not in tags_allowed:
                errors.append(f"标签不在 SCHEMA 分类法中（{t}）：{display(path)}")
    if "confidence" in meta and meta["confidence"] not in CONFIDENCE:
        errors.append(f"confidence 不合法（{meta['confidence']}）：{display(path)}")
    if meta.get("confidence") == "low" or meta.get("contested"):
        warnings.append(f"待复核（confidence: low / contested）：{display(path)}")
    return meta


def inbound_links() -> set[Path]:
    """全树（含证据层）Markdown 链接指向的目标集合。"""
    targets: set[Path] = set()
    for p in ROOT.rglob("*.md"):
        if ".git" in p.parts:
            continue
        in_fence = False
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            s = line.lstrip()
            if s.startswith(("```", "~~~")):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            for m in MARKDOWN_LINK.finditer(line):
                t = m.group(1).split("#", 1)[0].strip()
                if t and not t.startswith(("http://", "https://", "repo://", "mailto:")):
                    targets.add((p.parent / t).resolve())
    return targets


def configure(root: Path, *, schema_path: Path | None = None) -> None:
    global ROOT, SCHEMA, SYNTH_ROOTS, SKIP_SUBTREES
    ROOT = root.resolve()
    local_schema = ROOT.parent / "doc" / "knowledge" / "SCHEMA.md"
    SCHEMA = schema_path if schema_path is not None else (
        local_schema if local_schema.is_file()
        else files("infermatrix_copilot").joinpath("_runtime", "knowledge-schema.md")
    )
    SYNTH_ROOTS = (ROOT / "general", ROOT / "repos")
    SKIP_SUBTREES = (ROOT / "repos" / "jianghan-roleplay-data-pipeline",)
    errors.clear()
    warnings.clear()


def main() -> int:
    tags_allowed = taxonomy()
    pages = synthesized_pages()
    today = datetime.date.today()
    linked = inbound_links()
    check_adapter_briefings()
    for p in pages:
        meta = check_page(p, tags_allowed)
        if meta is None:
            continue
        updated = str(meta.get("updated", ""))
        if DATE.match(updated):
            age = (today - datetime.date.fromisoformat(updated)).days
            if age > STALE_DAYS:
                warnings.append(f"超过 {STALE_DAYS} 天未更新：{display(p)}")
        if p.name != "_index.md" and p.resolve() not in linked:
            warnings.append(f"孤页（没有任何入链）：{display(p)}")

    for message in warnings:
        print(f"提醒：{message}")
    for message in errors:
        print(f"错误：{message}")
    if errors:
        print(f"wiki lint 失败：{len(errors)} 个错误，{len(warnings)} 个提醒")
        return 1
    print(f"wiki lint 通过：0 个错误，{len(warnings)} 个提醒（共 {len(pages)} 页）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
