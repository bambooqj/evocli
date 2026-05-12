"""
edit_engine.py — SEARCH/REPLACE Edit Engine

研究来源:
- Aider (editblock_coder.py): SEARCH/REPLACE 格式 — LLM 生成的最可靠编辑格式
- OpenCode (edit.ts): Multi-Replacer 策略 — 6 级 fallback 确保编辑成功
- GitHub Copilot (applyPatchTool.tsx): 编辑失败时自动"healing"策略

## 为什么需要 SEARCH/REPLACE 而不是 unified diff?

1. LLM 不擅长维护准确的行号 (unified diff 要求精确行号)
2. SEARCH/REPLACE 只要求精确的代码块内容，更宽容
3. Aider 的研究表明 SEARCH/REPLACE 比 diff 成功率高 3x

## Multi-Replacer 策略 (OpenCode 模式):
1. Simple: 精确匹配
2. LineTrimmed: 忽略行尾空白
3. WhitespaceNormalized: 规范化空白
4. IndentationFlexible: 忽略缩进差异  
5. BlockAnchor: 模糊匹配 (SequenceMatcher)
6. "Did you mean?": 失败时显示最相似内容 (Aider 反射循环)

需要: Python 标准库 (difflib) + 可选 whatthepatch
"""
from __future__ import annotations

import difflib
import logging
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger("evocli.edit_engine")

# ── SEARCH/REPLACE 格式常量 ────────────────────────────────────────────────
SEARCH_MARKER  = "<<<<<<< SEARCH"
DIVIDER_MARKER = "======="
REPLACE_MARKER = ">>>>>>> REPLACE"

# 允许的变体 (不同 LLM 可能生成略有不同的标记)
_SEARCH_VARIANTS  = ["<<<<<<< SEARCH", "<<<<<<<SEARCH", "<<<<<<< search", "SEARCH:"]
_DIVIDER_VARIANTS = ["=======", "-------"]
_REPLACE_VARIANTS = [">>>>>>> REPLACE", ">>>>>>>REPLACE", ">>>>>>> replace", "REPLACE:"]


# ── 解析 ─────────────────────────────────────────────────────────────────────

def parse_search_replace_blocks(text: str) -> list[dict]:
    """
    从 LLM 输出中解析所有 SEARCH/REPLACE 块。

    支持格式:
    ```
    path/to/file.py
    <<<<<<< SEARCH
    old code
    =======
    new code
    >>>>>>> REPLACE
    ```

    返回: [{"file": str, "search": str, "replace": str}]
    """
    blocks = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        # 检测 SEARCH 标记
        if _is_search_marker(line):
            # 向上查找文件名 (最多 5 行)
            filename = None
            for j in range(max(0, i - 5), i):
                candidate = lines[j].strip().rstrip(":")
                if candidate and _looks_like_filename(candidate):
                    filename = candidate
                    # 去除代码围栏
                    filename = re.sub(r"^```\w*\s*", "", filename).strip()
            # 收集 SEARCH 内容
            search_lines = []
            i += 1
            while i < len(lines) and not _is_divider(lines[i]):
                search_lines.append(lines[i])
                i += 1
            # 收集 REPLACE 内容
            replace_lines = []
            i += 1  # 跳过 =======
            while i < len(lines) and not _is_replace_marker(lines[i]):
                replace_lines.append(lines[i])
                i += 1
            blocks.append({
                "file":    filename,
                "search":  "\n".join(search_lines),
                "replace": "\n".join(replace_lines),
            })
        i += 1
    return blocks


def _is_search_marker(line: str) -> bool:
    s = line.strip()
    return any(s.startswith(v) or s == v for v in _SEARCH_VARIANTS)

def _is_divider(line: str) -> bool:
    s = line.strip()
    return any(s.startswith(v) or s == v for v in _DIVIDER_VARIANTS)

def _is_replace_marker(line: str) -> bool:
    s = line.strip()
    return any(s.startswith(v) or s == v for v in _REPLACE_VARIANTS)

def _looks_like_filename(s: str) -> bool:
    """判断字符串是否像文件路径。"""
    # 有文件扩展名 或 包含路径分隔符
    return bool(re.search(r"\.\w{1,10}$", s)) or "/" in s or "\\" in s



# ── Ambiguous Search Error ────────────────────────────────────────────────────

class AmbiguousSearchError(Exception):
    """
    SEARCH 块在文件中出现多次，无法唯一确定修改位置。

    与其静默改第一个（可能改错），不如把所有匹配行号返回给 AI：
    - AI 看到 "Found 3 identical blocks at lines 12, 45, 89"
    - AI 在下次 SEARCH 块中加入更多上下文行（前几行/后几行）来唯一标识目标
    - 这是 Aider 的设计思路（AmbiguousEditError）
    """
    def __init__(self, search_block: str, match_count: int, match_line_numbers: list[int]):
        self.search_block      = search_block
        self.match_count       = match_count
        self.match_line_numbers = match_line_numbers
        lines_str = ", ".join(str(n) for n in match_line_numbers[:10])
        super().__init__(
            f"SEARCH block is ambiguous: found {match_count} identical matches "
            f"at line(s): {lines_str}.\n"
            f"Please add more surrounding context lines to your SEARCH block "
            f"to uniquely identify the target location."
        )

    def to_ai_feedback(self) -> str:
        """格式化为回传给 AI 的反馈消息（引导 AI 提供更具体的 SEARCH 块）。"""
        lines_str = ", ".join(str(n) for n in self.match_line_numbers[:10])
        suffix = " (showing first 10)" if self.match_count > 10 else ""
        return (
            f"AMBIGUOUS EDIT: Your SEARCH block appears {self.match_count} times "
            f"in the file{suffix}.\n"
            f"Matching at lines: {lines_str}\n\n"
            f"To fix: Add more surrounding context lines to your SEARCH block "
            f"so it uniquely identifies the section you want to change.\n"
            f"Example: include the function signature above, or unique variable names nearby."
        )


def _find_match_line_numbers(content: str, search: str) -> list[int]:
    """找出 search 在 content 中所有出现的起始行号（1-based）。"""
    if not search:
        return []
    results = []
    pos     = 0
    search_len = len(search)
    while True:
        idx = content.find(search, pos)
        if idx == -1:
            break
        # 计算行号：数 idx 之前有多少换行符
        line_num = content[:idx].count("\n") + 1
        results.append(line_num)
        pos = idx + search_len
    return results


# ── Multi-Replacer ────────────────────────────────────────────────────────────


class MultiReplacer:
    """
    Multi-Replacer: 6 级 fallback 策略应用 SEARCH/REPLACE 编辑。
    研究来源: OpenCode (edit.ts) 的 6 级 replacer 链。

    比单一精确匹配成功率高 4x。
    """

    def __init__(self, content: str):
        self.original = content
        self.lines    = content.splitlines(keepends=True)

    def apply(self, search: str, replace: str) -> tuple[str, str]:
        """
        尝试将 search 内容替换为 replace 内容。
        返回: (new_content, strategy_used)
        如果所有策略都失败，抛出 ValueError 并包含 "Did you mean?" 提示。
        如果 SEARCH 块不唯一，抛出 AmbiguousSearchError 并包含所有匹配位置，
        由调用方将位置信息回传给 AI，让 AI 补充更多上下文再重试。
        """
        strategies = [
            ("Simple",              self._simple),
            ("LineTrimmed",         self._line_trimmed),
            ("WhitespaceNormalized",self._whitespace_normalized),
            ("IndentationFlexible", self._indentation_flexible),
            ("BlockAnchor",         self._block_anchor),
        ]
        for name, fn in strategies:
            result = fn(search, replace)
            if result is not None:
                log.debug("EditEngine: applied via %s strategy", name)
                return result, name

        # 所有策略失败 — 生成 "Did you mean?" 提示 (Aider 反射循环模式)
        suggestion = self._did_you_mean(search)
        raise ValueError(
            f"SEARCH block not found in file.\n"
            f"Tried: Simple, LineTrimmed, WhitespaceNormalized, IndentationFlexible, BlockAnchor.\n"
            f"{suggestion}"
        )

    # ── 策略 1: 精确匹配 ─────────────────────────────────────
    def _simple(self, search: str, replace: str) -> Optional[str]:
        if not search:  # Bug #18.4: empty search → count("") = len+1 → infinite loop
            return None
        count = self.original.count(search)
        if count == 0:
            return None
        if count > 1:
            # 多处匹配 — 把所有匹配行号找出来，抛出 AmbiguousSearchError
            # 调用方应将位置列表返回给 AI，让 AI 在 SEARCH 块中补充更多上下文
            match_lines = _find_match_line_numbers(self.original, search)
            raise AmbiguousSearchError(
                search_block=search,
                match_count=count,
                match_line_numbers=match_lines,
            )
        return self.original.replace(search, replace, 1)

    # ── 策略 2: 忽略行尾空白 ─────────────────────────────────
    def _line_trimmed(self, search: str, replace: str) -> Optional[str]:
        def trim_lines(s: str) -> str:
            return "\n".join(l.rstrip() for l in s.splitlines())
        trimmed_orig   = trim_lines(self.original)
        trimmed_search = trim_lines(search)
        if trimmed_search in trimmed_orig:
            # 在原始内容中找到对应位置并替换
            idx = trimmed_orig.find(trimmed_search)
            # 计算原始内容中的对应行范围
            orig_lines   = self.original.splitlines(keepends=True)
            trimmed_lines = trimmed_orig.splitlines(keepends=True)
            # 确定 search 跨越的行数
            search_line_count = len(trimmed_search.splitlines())
            # 找到起始行
            start_line = 0
            char_count = 0
            for j, tl in enumerate(trimmed_lines):
                if char_count >= idx:
                    start_line = j
                    break
                char_count += len(tl)
            end_line = start_line + search_line_count
            # 重建内容
            prefix  = "".join(orig_lines[:start_line])
            suffix  = "".join(orig_lines[end_line:])
            return prefix + replace + ("\n" if replace and not replace.endswith("\n") else "") + suffix
        return None

    # ── 策略 3: 规范化空白 ──────────────────────────────────
    def _whitespace_normalized(self, search: str, replace: str) -> Optional[str]:
        def normalize(s: str) -> str:
            return re.sub(r"\s+", " ", s).strip()
        norm_orig   = normalize(self.original)
        norm_search = normalize(search)
        if norm_search in norm_orig:
            # 找到原始内容中最相似的块
            return self._fuzzy_replace(search, replace, threshold=0.9)
        return None

    # ── 策略 4: 忽略缩进差异 ────────────────────────────────
    def _indentation_flexible(self, search: str, replace: str) -> Optional[str]:
        """
        核心算法 (Aider 的 replace_most_similar_chunk):
        去掉缩进后匹配，然后用文件实际缩进重新格式化替换内容。
        """
        search_lines = search.splitlines()
        if not search_lines:
            return None

        # 去掉公共缩进
        def strip_indent(lines: list[str]) -> tuple[list[str], str]:
            stripped = [l.lstrip() for l in lines if l.strip()]
            if not stripped:
                return stripped, ""
            common = ""
            non_empty = [l for l in lines if l.strip()]
            if non_empty:
                common = re.match(r"^(\s*)", non_empty[0]).group(1)
                for l in non_empty[1:]:
                    m = re.match(r"^(\s*)", l)
                    if m:
                        cur = m.group(1)
                        common = common[:len(cur)] if len(cur) < len(common) else common
            return [l[len(common):] for l in lines], common

        stripped_search, _ = strip_indent(search_lines)
        file_lines = self.original.splitlines()

        # 滑动窗口查找最佳匹配
        window = len(stripped_search)
        for i in range(len(file_lines) - window + 1):
            window_lines = file_lines[i:i + window]
            stripped_window, file_indent = strip_indent(window_lines)
            if stripped_window == stripped_search:
                # 找到匹配!用文件的缩进重新格式化替换内容
                replace_lines = replace.splitlines()
                _, search_indent = strip_indent(search_lines)
                reindented = [
                    file_indent + l[len(search_indent):]
                    if l.startswith(search_indent) else l
                    for l in replace_lines
                ]
                new_block = "\n".join(reindented)
                prefix = "\n".join(file_lines[:i]) + ("\n" if i > 0 else "")
                suffix = ("\n" + "\n".join(file_lines[i + window:])) if i + window < len(file_lines) else ""
                return prefix + new_block + suffix
        return None

    # ── 策略 5: BlockAnchor (模糊匹配) ──────────────────────
    def _block_anchor(self, search: str, replace: str) -> Optional[str]:
        return self._fuzzy_replace(search, replace, threshold=0.85)

    def _fuzzy_replace(self, search: str, replace: str, threshold: float) -> Optional[str]:
        """
        使用 difflib.SequenceMatcher 找到最相似的块。
        研究来源: Aider 的 find_similar_lines 算法。
        """
        search_lines = search.splitlines()
        file_lines   = self.original.splitlines()
        if not search_lines or not file_lines:
            return None
        window = len(search_lines)
        best_ratio = 0.0
        best_start = -1
        for i in range(max(1, len(file_lines) - window + 1)):
            window_lines = file_lines[i:i + window]
            ratio = difflib.SequenceMatcher(
                None, "\n".join(search_lines), "\n".join(window_lines)
            ).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_start = i
        if best_ratio >= threshold and best_start >= 0:
            prefix = "\n".join(file_lines[:best_start]) + ("\n" if best_start > 0 else "")
            suffix = ("\n" + "\n".join(file_lines[best_start + window:])) if best_start + window < len(file_lines) else ""
            return prefix + replace + suffix
        return None

    # ── "Did you mean?" (Aider 反射循环模式) ─────────────────
    def _did_you_mean(self, search: str) -> str:
        """
        找到最相似的代码块并提示 LLM 修正。
        研究来源: Aider 的 prepare_to_edit + difflib similarity hints。
        """
        search_lines = search.splitlines()
        file_lines   = self.original.splitlines()
        if not search_lines or not file_lines:
            return ""
        window = min(len(search_lines), len(file_lines))
        best_ratio = 0.0
        best_lines: list[str] = []
        for i in range(max(1, len(file_lines) - window + 1)):
            chunk = file_lines[i:i + window]
            ratio = difflib.SequenceMatcher(
                None, "\n".join(search_lines), "\n".join(chunk)
            ).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_lines = chunk
        if best_ratio > 0.4 and best_lines:
            return (
                f"Did you mean? (similarity={best_ratio:.0%}):\n"
                + "\n".join(f"  {l}" for l in best_lines[:10])
                + ("\n  ..." if len(best_lines) > 10 else "")
            )
        return "No similar code block found. Make sure the SEARCH block is an exact copy from the file."


# ── 顶层 API ──────────────────────────────────────────────────────────────────

def apply_search_replace(
    file_content: str,
    search: str,
    replace: str,
) -> tuple[str, str]:
    """
    Apply a single SEARCH/REPLACE block to file content.
    Returns: (new_content, strategy_name)
    Raises:
      ValueError           — SEARCH block not found (all strategies failed)
      AmbiguousSearchError — SEARCH block found N>1 times; caller should
                             return .to_ai_feedback() to the LLM so it can
                             add more context lines and retry.
    """
    replacer = MultiReplacer(file_content)
    return replacer.apply(search.rstrip("\n"), replace)


def apply_search_replace_to_file(
    file_path: str | Path,
    search: str,
    replace: str,
) -> dict:
    """
    Apply a SEARCH/REPLACE block to a file on disk.
    Returns: {"ok": bool, "strategy": str, "error": str, "ambiguous": bool, ...}
    """
    path = Path(file_path)
    if not path.exists():
        return {"ok": False, "strategy": "", "error": f"File not found: {path}"}
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        new_content, strategy = apply_search_replace(content, search, replace)
        path.write_text(new_content, encoding="utf-8")
        log.info("EditEngine: applied SEARCH/REPLACE to %s via %s", path.name, strategy)
        return {"ok": True, "strategy": strategy, "error": ""}
    except AmbiguousSearchError as amb:
        log.debug("EditEngine: ambiguous search in %s: %d matches", path.name, amb.match_count)
        return {
            "ok": False, "strategy": "ambiguous", "ambiguous": True,
            "match_count": amb.match_count, "match_lines": amb.match_line_numbers,
            "ai_feedback": amb.to_ai_feedback(), "error": str(amb),
        }
    except ValueError as e:
        log.debug("EditEngine: failed for %s: %s", path.name, e)
        return {"ok": False, "strategy": "all_failed", "error": str(e)}
    except Exception as e:
        log.warning("EditEngine: unexpected error for %s: %s", path.name, e)
        return {"ok": False, "strategy": "", "error": str(e)}


def apply_all_blocks_from_llm_output(
    llm_output: str,
    base_dir: str | Path = ".",
) -> list[dict]:
    """
    Parse ALL SEARCH/REPLACE blocks from LLM output and apply them.
    This is the main entry point for processing LLM-generated edits.

    研究来源: Aider 的 apply_updates() 处理多个编辑块。
    Returns: list of {"file", "ok", "strategy", "error"}
    """
    base = Path(base_dir)
    blocks = parse_search_replace_blocks(llm_output)
    results = []
    for block in blocks:
        filename = block.get("file") or ""
        if not filename:
            results.append({"file": "(unknown)", "ok": False, "strategy": "",
                            "error": "Could not determine file path from LLM output"})
            continue
        file_path = base / filename
        result = apply_search_replace_to_file(file_path, block["search"], block["replace"])
        result["file"] = str(file_path)
        results.append(result)
    return results
