#!/usr/bin/env python3
"""检查 general/ 和 repos/ 的目录、索引与 Markdown 链接。

贡献规范（CONTRIBUTING.md、SCHEMA.md、contributing/）已迁到 doc/knowledge/：
它们是**文档**，不是知识页，因此不再由本校验器扫描。知识树本身只剩数据面。"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

from ..format import PAGE_MAX_BYTES, PAGE_MAX_LINES, PAGE_WARN_BYTES, PAGE_WARN_LINES


ROOT = Path.cwd() / "knowledge"
KNOWLEDGE_ROOTS = (ROOT / "general", ROOT / "repos")
INDEX_NAME = "_index.md"
SPECIAL_PAGES = {INDEX_NAME, "rules.md", "architecture.md"}
GROUP_DIRS = {"guides", "history", "incidents", "references", "results", "rfcs"}
SOURCE_OWNER_DIRS = {"components", "models"}
FILESYSTEM_CHILD_INDEX = "<!-- children: filesystem -->"
INCIDENT_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]*\.md$")
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
INCIDENT_FIELDS = ("- 编号：", "- 归属：", "- 状态：", "- 搜索词：", "- 影响范围：")
INCIDENT_STATES = ("待归类", "处理中", "已验证", "已提炼", "仅历史")
PRIVATE_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
WINDOWS_USER_PATH = re.compile(r"[A-Za-z]:\\Users\\[^\\\s`]+", re.IGNORECASE)
REMOTE_USER_PATH = re.compile(
    r"/(?:home/(?!models(?:/|\b)|<)|data/(?!models?(?:/|\b)|<))"
    r"[A-Za-z0-9_-]+"
)
DANGEROUS_TEXT_PATTERNS = (
    (
        re.compile(r"StrictHostKeyChecking\s*=\s*no", re.IGNORECASE),
        "关闭 SSH host key 校验",
    ),
    (
        re.compile(r"safe\.directory\s+['\"]?\*", re.IGNORECASE),
        "全局放行 Git safe.directory",
    ),
    (re.compile(r"--gpus\s+all\b", re.IGNORECASE), "让容器绕过调度器暴露全部 GPU"),
    (re.compile(r"偷(?:空闲)?卡|偷空闲 GPU", re.IGNORECASE), "使用未分配 GPU"),
)
DANGEROUS_CODE_PATTERNS = (
    (re.compile(r"\bpkill\b", re.IGNORECASE), "按进程名全局终止"),
    (re.compile(r"\brm\s+-[^\n]*r[^\n]*f", re.IGNORECASE), "递归强制删除"),
    (
        re.compile(r"\bfind\b[^\n]*-exec[^\n]*\brm\b", re.IGNORECASE),
        "find 批量递归删除",
    ),
)


errors: list[str] = []
warnings: list[str] = []
incident_ids: dict[str, Path] = {}


def display(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def markdown_links(path: Path) -> list[str]:
    """提取代码块之外的 Markdown 链接目标。"""
    links: list[str] = []
    in_fence = False
    fence_marker = ""
    for line in read_text(path).splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
                fence_marker = ""
            continue
        if in_fence:
            continue
        links.extend(match.group(1).strip() for match in MARKDOWN_LINK.finditer(line))
    return links


def fenced_code_lines(path: Path) -> list[tuple[int, str]]:
    """返回 Markdown fenced code block 中的可执行文本。"""
    code_lines: list[tuple[int, str]] = []
    in_fence = False
    fence_marker = ""
    for line_number, line in enumerate(read_text(path).splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
                fence_marker = ""
            continue
        if in_fence:
            code_lines.append((line_number, line))
    return code_lines


def local_target(raw_target: str) -> str | None:
    target = raw_target.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    elif " " in target:
        target = target.split(" ", 1)[0]
    if target.startswith(("http://", "https://", "repo://", "mailto:", "#")):
        return None
    return unquote(target.split("#", 1)[0])


def check_links(path: Path) -> None:
    for raw_target in markdown_links(path):
        target = local_target(raw_target)
        if not target:
            continue
        if target.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", target):
            errors.append(f"绝对路径链接：{display(path)} -> {target}")
            continue
        resolved = (path.parent / target).resolve()
        if not resolved.exists():
            errors.append(f"链接不存在：{display(path)} -> {target}")


def resolved_local_links(path: Path) -> list[Path]:
    """返回页面中所有相对 Markdown 链接解析后的真实路径。"""
    resolved: list[Path] = []
    for raw_target in markdown_links(path):
        target = local_target(raw_target)
        if not target:
            continue
        if target.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", target):
            continue
        resolved.append((path.parent / target).resolve())
    return resolved


def has_markdown(path: Path) -> bool:
    return any(path.rglob("*.md"))


def uses_filesystem_child_index(directory: Path, index_text: str) -> bool:
    """Allow model families to be discovered from the directory itself."""
    return (
        directory.name == "models"
        and FILESYSTEM_CHILD_INDEX in index_text
    )


def check_file_size(path: Path, index_text: str) -> None:
    text = read_text(path)
    non_empty_lines = sum(1 for line in text.splitlines() if line.strip())
    byte_size = path.stat().st_size
    if non_empty_lines >= PAGE_MAX_LINES or byte_size >= PAGE_MAX_BYTES:
        has_exception = path.name in index_text and "暂不拆分" in index_text
        if not has_exception:
            errors.append(
                f"文件必须拆分：{display(path)} "
                f"({non_empty_lines} 个非空行，{byte_size} bytes)"
            )
    elif non_empty_lines >= PAGE_WARN_LINES or byte_size >= PAGE_WARN_BYTES:
        warnings.append(
            f"文件接近拆分线：{display(path)} "
            f"({non_empty_lines} 个非空行，{byte_size} bytes)"
        )


def check_sensitive_text(path: Path) -> None:
    text = read_text(path)
    for match in PRIVATE_IPV4.finditer(text):
        if not match.group(0).startswith("127."):
            errors.append(f"页面包含真实 IPv4，请改成 <REMOTE_HOST>：{display(path)}")
            break
    if WINDOWS_USER_PATH.search(text):
        errors.append(f"页面包含用户目录，请改成 <USER_HOME>：{display(path)}")
    if REMOTE_USER_PATH.search(text):
        errors.append(
            f"页面包含远端用户目录，请改成 <REMOTE_WORK_ROOT>：{display(path)}"
        )
    if "-----BEGIN PRIVATE KEY-----" in text:
        errors.append(f"页面包含私钥内容：{display(path)}")


def check_dangerous_commands(path: Path) -> None:
    text = read_text(path)
    for pattern, description in DANGEROUS_TEXT_PATTERNS:
        if pattern.search(text):
            errors.append(f"页面包含危险操作（{description}）：{display(path)}")
    for line_number, line in fenced_code_lines(path):
        for pattern, description in DANGEROUS_CODE_PATTERNS:
            if pattern.search(line):
                errors.append(
                    f"代码块包含危险操作（{description}）："
                    f"{display(path)}:{line_number}"
                )


def check_incident(path: Path) -> None:
    if not INCIDENT_NAME.match(path.name):
        errors.append(f"错题文件名不符合 YYYY-MM-DD-short-name.md：{display(path)}")
    text = read_text(path)
    for field in INCIDENT_FIELDS:
        if field not in text:
            errors.append(f"错题缺少字段 {field}：{display(path)}")
    status_line = next(
        (line for line in text.splitlines() if line.startswith("- 状态：")), ""
    )
    if status_line and not any(state in status_line for state in INCIDENT_STATES):
        errors.append(f"错题状态不合法：{display(path)}")
    id_match = re.search(r"^- 编号：`([^`]+)`", text, re.MULTILINE)
    if id_match:
        incident_id = id_match.group(1)
        previous = incident_ids.get(incident_id)
        if previous is not None:
            errors.append(
                f"错题编号重复：{incident_id} 同时出现在 "
                f"{display(previous)} 和 {display(path)}"
            )
        else:
            incident_ids[incident_id] = path


def check_directory(directory: Path) -> None:
    direct_pages = sorted(directory.glob("*.md"))
    child_dirs = sorted(
        child for child in directory.iterdir() if child.is_dir() and has_markdown(child)
    )
    if not direct_pages and not child_dirs:
        return

    index = directory / INDEX_NAME
    if not index.is_file():
        errors.append(f"目录缺少 {INDEX_NAME}：{display(directory)}")
        index_text = ""
    else:
        index_text = read_text(index)
    index_targets = resolved_local_links(index) if index.is_file() else []

    for page in direct_pages:
        check_links(page)
        check_file_size(page, index_text)
        check_sensitive_text(page)
        check_dangerous_commands(page)
        if page.name != INDEX_NAME:
            registrations = index_targets.count(page.resolve())
            if registrations == 0:
                errors.append(f"页面没有登记到当前目录索引：{display(page)}")
            elif registrations > 1:
                errors.append(
                    f"页面在当前目录索引中重复登记：{display(page)} "
                    f"({registrations} 个链接)"
                )
        if directory.name == "incidents" and page.name != INDEX_NAME:
            check_incident(page)

    for child in child_dirs:
        child_index = (child / INDEX_NAME).resolve()
        registrations = index_targets.count(child_index)
        if registrations == 0 and not uses_filesystem_child_index(
            directory, index_text
        ):
            errors.append(
                f"子目录没有登记到上一层索引：{display(child)} "
                f"(应在 {display(index)} 链接 {child.name}/{INDEX_NAME})"
            )
        elif registrations > 1:
            errors.append(
                f"子目录在上一层索引中重复登记：{display(child)} "
                f"({registrations} 个链接)"
            )

    ordinary_pages = [page for page in direct_pages if page.name not in SPECIAL_PAGES]
    is_group = directory.name in GROUP_DIRS or any(
        parent.name in GROUP_DIRS for parent in directory.parents
    )
    if len(ordinary_pages) > 7 and not is_group:
        warnings.append(
            f"目录有 {len(ordinary_pages)} 个普通页面，应该考虑分类：{display(directory)}"
        )
    if is_group and len(ordinary_pages) > 20:
        errors.append(
            f"分类目录有 {len(ordinary_pages)} 个页面，必须继续按主题整理：{display(directory)}"
        )


def owner_axis_violations(repo: Path) -> list[str]:
    """Return deterministic workflow/source-owner nesting violations."""
    violations: list[str] = []

    for owner_name in SOURCE_OWNER_DIRS:
        for candidate in repo.rglob(owner_name):
            if candidate.is_dir() and candidate.parent != repo:
                violations.append(
                    f"源码 owner 目录必须直属仓库，不能被工作主题或其他 owner 包住："
                    f"{display(candidate)}"
                )

    return violations


def source_owner_guide_violations(repo: Path) -> list[str]:
    """Keep component/model feature pages directly under their owner."""
    violations: list[str] = []
    for axis in SOURCE_OWNER_DIRS:
        axis_root = repo / axis
        if not axis_root.is_dir():
            continue
        for owner in axis_root.iterdir():
            guides = owner / "guides"
            if owner.is_dir() and guides.is_dir() and has_markdown(guides):
                violations.append(
                    "源码 owner 下不使用 guides 中转层；特性正文直接放 owner "
                    f"根目录：{display(guides)}"
                )
    return violations


def check_repo_owner_axes() -> None:
    repos_root = ROOT / "repos"
    if not repos_root.is_dir():
        return
    for repo in sorted(path for path in repos_root.iterdir() if path.is_dir()):
        errors.extend(owner_axis_violations(repo))
        errors.extend(source_owner_guide_violations(repo))


def exact_page_duplicate_violations(paths: list[Path]) -> list[str]:
    """Return byte-for-byte duplicate non-index pages across different owners."""
    pages_by_body: dict[str, list[Path]] = {}
    for path in paths:
        if path.name == INDEX_NAME:
            continue
        body = read_text(path).strip()
        if len(body) < 200:
            continue
        pages_by_body.setdefault(body, []).append(path)

    violations: list[str] = []
    for duplicate_paths in pages_by_body.values():
        owners = {path.parent.resolve() for path in duplicate_paths}
        if len(owners) < 2:
            continue
        rendered = ", ".join(display(path) for path in sorted(duplicate_paths))
        violations.append(
            f"整页正文在多个 owner 下完全重复；只保留最近 owner 的正文，"
            f"其他入口只链接：{rendered}"
        )
    return violations


def check_exact_page_duplicates() -> None:
    paths = [
        path
        for root in KNOWLEDGE_ROOTS
        if root.is_dir()
        for path in root.rglob("*.md")
    ]
    errors.extend(exact_page_duplicate_violations(paths))


def check_local_is_untracked() -> None:
    result = subprocess.run(
        ["git", "ls-files", "--", "local"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        errors.append(f"无法检查 local/ 的 Git 状态：{result.stderr.strip()}")
        return
    tracked = [line for line in result.stdout.splitlines() if line.strip()]
    for path in tracked:
        errors.append(f"local/ 中存在被 Git 跟踪的文件：{path}")


def check_short_contributing_entry() -> None:
    """贡献入口的长度门。

    入口已迁到 doc/knowledge/CONTRIBUTING.md（它是文档，不是知识页）。这条规则跟着
    它走而不是被删掉：让入口保持短、细则下沉到 contributing/ 专题，是这棵树能被
    按需导航的原因；删掉这道门，入口会重新长回一份没人读完的全文规范。
    仓库根不可得时（例如只装了 wheel 的知识树）跳过，不误报。
    """
    entry = ROOT.parent / "doc" / "knowledge" / "CONTRIBUTING.md"
    if not entry.is_file():
        return
    text = read_text(entry)
    non_empty_lines = sum(1 for line in text.splitlines() if line.strip())
    byte_size = entry.stat().st_size
    if non_empty_lines > 100 or byte_size > 8 * 1024:
        errors.append(
            "贡献入口过长，细则必须下沉到 doc/knowledge/contributing/："
            f"{non_empty_lines} 个非空行，{byte_size} bytes"
        )


def configure(root: Path) -> None:
    global ROOT, KNOWLEDGE_ROOTS
    ROOT = root.resolve()
    KNOWLEDGE_ROOTS = (ROOT / "general", ROOT / "repos")
    errors.clear()
    warnings.clear()
    incident_ids.clear()


def main() -> int:
    for root in KNOWLEDGE_ROOTS:
        if not root.is_dir():
            errors.append(f"缺少知识根目录：{display(root)}")
            continue
        for directory in sorted(path for path in root.rglob("*") if path.is_dir()):
            check_directory(directory)
        check_directory(root)

    excluded_parts = {
        ".git",
        "artifacts",
        "general",
        "local",
        "outputs",
        "repos",
    }
    for path in ROOT.rglob("*.md"):
        relative_parts = set(path.relative_to(ROOT).parts)
        if relative_parts & excluded_parts:
            continue
        check_links(path)
        check_sensitive_text(path)

    check_local_is_untracked()
    check_short_contributing_entry()
    check_repo_owner_axes()
    check_exact_page_duplicates()

    for message in warnings:
        print(f"提醒：{message}")
    for message in errors:
        print(f"错误：{message}")

    if errors:
        print(f"检查失败：{len(errors)} 个错误，{len(warnings)} 个提醒")
        return 1
    print(f"知识目录检查通过：0 个错误，{len(warnings)} 个提醒")
    return 0


if __name__ == "__main__":
    sys.exit(main())
