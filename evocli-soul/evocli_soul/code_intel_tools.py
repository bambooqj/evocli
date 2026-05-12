"""
Code Intelligence Tools — Python Soul 侧，提供代码智能分析。

WIRE-2: jedi 接入 Python 静态分析
  - 对 .py 文件使用 jedi（快速、精准、无需 LSP 服务器）
  - 对其他语言通过 bridge 调用 Rust LSP 层

jedi 提供（不需要任何服务器进程）：
  - 符号定义位置（goto_definition）
  - 所有引用（find_references）  
  - 补全候选（completions）
  - 函数签名（signatures）

需要：pip install "evocli-soul[code]"
"""
from __future__ import annotations

import importlib.util
import logging
import shutil
from typing import Optional

log = logging.getLogger("evocli.code_intel")

_JEDI_AVAILABLE = importlib.util.find_spec("jedi") is not None


class JediAnalyzer:
    """
    WIRE-2: jedi 静态分析器（Python 专用）。
    不需要 LSP 服务器，直接在进程内分析。
    """

    @staticmethod
    def goto_definition(file: str, line: int, column: int) -> list[dict]:
        """跳转到定义位置"""
        if not _JEDI_AVAILABLE:
            return []
        try:
            import jedi
            script = jedi.Script(path=file)
            defs   = script.goto(line=line, column=column)
            return [
                {
                    "file":   str(d.module_path) if d.module_path else file,
                    "line":   d.line,
                    "column": d.column,
                    "name":   d.name,
                    "type":   d.type,
                }
                for d in defs if d.line
            ]
        except Exception as e:
            log.debug("jedi goto_definition failed: %s", e)
            return []

    @staticmethod
    def find_references(file: str, line: int, column: int) -> list[dict]:
        """找到所有引用"""
        if not _JEDI_AVAILABLE:
            return []
        try:
            import jedi
            script = jedi.Script(path=file)
            refs   = script.get_references(line=line, column=column, include_builtins=False)
            return [
                {
                    "file":   str(r.module_path) if r.module_path else file,
                    "line":   r.line,
                    "column": r.column,
                    "name":   r.name,
                }
                for r in refs if r.line
            ]
        except Exception as e:
            log.debug("jedi find_references failed: %s", e)
            return []

    @staticmethod
    def get_signatures(file: str, line: int, column: int) -> list[dict]:
        """获取函数签名（调用时的参数提示）"""
        if not _JEDI_AVAILABLE:
            return []
        try:
            import jedi
            script = jedi.Script(path=file)
            sigs   = script.get_signatures(line=line, column=column)
            return [
                {
                    "name":   s.name,
                    "params": [p.description for p in s.params],
                    "docstring": s.docstring(raw=True)[:300] if s.docstring() else "",
                }
                for s in sigs
            ]
        except Exception as e:
            log.debug("jedi get_signatures failed: %s", e)
            return []

    @staticmethod
    def get_completions(file: str, line: int, column: int) -> list[dict]:
        """获取补全候选（用于 LLM 上下文注入）"""
        if not _JEDI_AVAILABLE:
            return []
        try:
            import jedi
            script      = jedi.Script(path=file)
            completions = script.complete(line=line, column=column)
            return [
                {
                    "name":        c.name,
                    "type":        c.type,
                    "description": c.description[:100],
                }
                for c in completions[:20]
            ]
        except Exception as e:
            log.debug("jedi completions failed: %s", e)
            return []

    @staticmethod
    def analyze_module(file: str) -> dict:
        """
        分析整个 Python 模块，返回所有顶层符号（函数、类、变量）。
        用于代码索引和 symbol.lookup 的 Python 路径。
        """
        if not _JEDI_AVAILABLE:
            return {"symbols": [], "engine": "jedi-unavailable"}
        try:
            import jedi
            script = jedi.Script(path=file)
            names  = script.get_names(all_scopes=False, definitions=True, references=False)
            symbols = []
            for n in names:
                if n.type in ("function", "class", "statement"):
                    symbols.append({
                        "name":   n.name,
                        "type":   n.type,
                        "line":   n.line,
                        "column": n.column,
                        "full_name": n.full_name or n.name,
                        "description": n.description[:200] if n.description else "",
                    })
            return {"symbols": symbols, "engine": "jedi", "file": file}
        except Exception as e:
            log.debug("jedi analyze_module failed for %s: %s", file, e)
            return {"symbols": [], "engine": "jedi-error", "error": str(e)}


class CodeIntelTools:
    """
    代码智能工具代理。

    策略（WIRE-2）：
    - Python 文件 → jedi（进程内，快速，不需要 LSP 服务器）
    - 其他语言   → Rust Host LSP 层（需要安装对应语言服务器）
    """

    def __init__(self, bridge):
        self.bridge = bridge
        self.jedi   = JediAnalyzer()

    async def analyze_function(self, file: str, line: int, character: int) -> dict:
        """分析函数的完整调用层次（incoming + outgoing）。"""
        if file.endswith(".py") and _JEDI_AVAILABLE:
            # Python: 用 jedi 直接分析
            defs = self.jedi.goto_definition(file, line, character)
            refs = self.jedi.find_references(file, line, character)
            return {
                "engine":     "jedi",
                "definition": defs[:1],
                "references": refs[:20],
            }
        # 其他语言：走 Rust LSP
        return await self.bridge.call(
            "lsp.analyze_function",
            {"file": file, "line": line, "character": character},
        )

    async def find_references(self, file: str, line: int, character: int) -> list:
        """找到符号的所有引用。"""
        if file.endswith(".py") and _JEDI_AVAILABLE:
            return self.jedi.find_references(file, line, character)
        result = await self.bridge.call(
            "lsp.find_references",
            {"file": file, "line": line, "character": character},
        )
        return result.get("locations", [])

    async def goto_definition(self, file: str, line: int, character: int) -> Optional[dict]:
        """跳转到符号定义。"""
        if file.endswith(".py") and _JEDI_AVAILABLE:
            defs = self.jedi.goto_definition(file, line, character)
            return defs[0] if defs else None
        return await self.bridge.call(
            "lsp.goto_definition",
            {"file": file, "line": line, "character": character},
        )

    async def analyze_python_module(self, file: str) -> dict:
        """WIRE-2: 用 jedi 分析整个 Python 模块的符号。"""
        return self.jedi.analyze_module(file)

    async def check_lsp_available(self, language: str) -> bool:
        """检查语言服务器是否安装。"""
        servers = {
            "rust":       "rust-analyzer",
            "python":     "pyright-langserver",
            "typescript": "typescript-language-server",
            "go":         "gopls",
        }
        server = servers.get(language)
        if not server:
            return False
        return shutil.which(server) is not None

    @staticmethod
    def status() -> dict:
        """返回可用分析器状态（用于 evocli doctor）。"""
        return {
            "jedi":     _JEDI_AVAILABLE,
            "grep_ast": importlib.util.find_spec("grep_ast") is not None,
            "note":     "Install evocli-soul[code] for full Python intelligence",
        }

