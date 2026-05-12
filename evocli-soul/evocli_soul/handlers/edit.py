"""
Edit handlers — SEARCH/REPLACE 编辑和反射循环 (Aider/OpenCode 模式)

研究来源:
- Aider (editblock_coder.py): SEARCH/REPLACE 格式 + 反射循环 (最多 3 次重试)
- Aider (linter.py): 编辑后自动运行 lint，失败则回馈 LLM
- OpenCode (edit.ts): Multi-Replacer 策略链
- Cursor (shadow workspace): 编辑前在隔离环境验证代码

新增 RPC 方法:
  fs.apply_search_replace   — 应用 SEARCH/REPLACE 块到文件
  fs.apply_all_blocks       — 从 LLM 输出解析并应用所有块
  fs.lint_file              — 对文件运行 lint，返回错误 (用于反射循环)
"""
from __future__ import annotations
import logging

log = logging.getLogger("evocli.handlers.edit")


def register(router) -> None:
    router.add("fs.apply_search_replace", handle_apply_search_replace)
    router.add("fs.apply_all_blocks",     handle_apply_all_blocks)
    router.add("fs.lint_file",            handle_lint_file)


async def handle_apply_search_replace(req_id: str, params: dict, send, state) -> None:
    """
    Apply a SEARCH/REPLACE block to a file via the MultiReplacer engine.
    Architecture fix: uses bridge.call("fs.read/write") for file IO (Rust security layer),
    applies SEARCH/REPLACE logic in Python (edit_engine.py pure logic).
    """
    path    = params.get("path", "")
    search  = params.get("search", "")
    replace = params.get("replace", "")
    if not path or search is None:
        await send.error(req_id, -32600, "path and search are required")
        return
    try:
        bridge = state.get_bridge()
        # Read via Rust (security checks + correct path resolution)
        content = await bridge.call("fs.read", {"path": path})
        if not isinstance(content, str):
            await send.response(req_id, {"ok": False, "error": f"Could not read: {path}"})
            return
        # Apply in Python (pure logic, no direct IO)
        from evocli_soul.edit_engine import apply_search_replace, AmbiguousSearchError
        try:
            new_content, strategy = apply_search_replace(content, search, replace)
        except AmbiguousSearchError as amb:
            # Return match locations to AI — let AI add more context and retry
            await send.response(req_id, {
                "ok":              False,
                "strategy":        "ambiguous",
                "ambiguous":       True,
                "match_count":     amb.match_count,
                "match_lines":     amb.match_line_numbers,
                "ai_feedback":     amb.to_ai_feedback(),
                "error":           str(amb),
            })
            return
        # Write via Rust
        await bridge.call("fs.write", {"path": path, "content": new_content})
        await send.response(req_id, {"ok": True, "strategy": strategy, "path": path})
    except ValueError as e:
        await send.response(req_id, {"ok": False, "strategy": "all_failed", "error": str(e)})
    except Exception as e:
        log.exception("fs.apply_search_replace failed")
        await send.error(req_id, -32603, str(e))


async def handle_apply_all_blocks(req_id: str, params: dict, send, state) -> None:
    """
    Parse ALL SEARCH/REPLACE blocks from LLM output and apply them ATOMICALLY.

    研究来源: Aider apply_updates() 原子编辑模式
    - 编辑前创建 git checkpoint（snapshot）
    - 顺序应用所有 SEARCH/REPLACE 块
    - 任何块失败 → 自动回滚到 checkpoint（Aider: git reset --hard）
    
    transactional=True (default): git checkpoint before, rollback on failure
    transactional=False: apply sequentially, no rollback (legacy behavior)

    params:
      llm_output:    str   Full LLM response text containing SEARCH/REPLACE blocks
      base_dir:      str   Base directory for relative paths (default ".")
      transactional: bool  Use git checkpoint + rollback (default True)
    """
    llm_output    = params.get("llm_output", "")
    params.get("base_dir", ".")
    transactional = params.get("transactional", True)
    if not llm_output:
        await send.error(req_id, -32600, "llm_output is required")
        return
    try:
        bridge = state.get_bridge()
        from evocli_soul.edit_engine import parse_search_replace_blocks, apply_search_replace, AmbiguousSearchError
        blocks = parse_search_replace_blocks(llm_output)

        # ── Atomic: create git checkpoint before any edits ───────────
        checkpoint_ref = None
        if transactional and blocks:
            try:
                snap = await bridge.call("git.snapshot", {})
                checkpoint_ref = snap.get("stash_ref") if isinstance(snap, dict) else None
                log.debug("Atomic edit: checkpoint created (%s)", checkpoint_ref)
            except Exception as e:
                log.debug("Atomic edit: no git checkpoint (non-fatal): %s", e)

        results = []
        failed  = False
        for block in blocks:
            filename = block.get("file") or ""
            if not filename:
                results.append({"file": "(unknown)", "ok": False, "error": "Could not determine file path"})
                failed = True
                continue
            try:
                content = await bridge.call("fs.read", {"path": filename})
                if not isinstance(content, str):
                    results.append({"file": filename, "ok": False, "error": f"Could not read: {filename}"})
                    failed = True
                    continue
                try:
                    new_content, strategy = apply_search_replace(content, block["search"], block["replace"])
                    await bridge.call("fs.write", {"path": filename, "content": new_content})
                    results.append({"file": filename, "ok": True, "strategy": strategy})
                except AmbiguousSearchError as amb:
                    # Return match info — do NOT write, let caller decide/retry with more context
                    results.append({
                        "file": filename, "ok": False, "strategy": "ambiguous",
                        "ambiguous": True, "match_count": amb.match_count,
                        "match_lines": amb.match_line_numbers,
                        "error": amb.to_ai_feedback(),
                    })
                    failed = True
            except ValueError as e:
                results.append({"file": filename, "ok": False, "strategy": "all_failed", "error": str(e)})
                failed = True
            except Exception as e:
                results.append({"file": filename, "ok": False, "error": str(e)})
                failed = True

        # ── Atomic rollback on failure (Aider: git reset --hard) ─────
        if failed and transactional and checkpoint_ref:
            try:
                await bridge.call("git.restore", {"stash_ref": checkpoint_ref})
                log.warning("Atomic edit: rolled back to checkpoint %s (%d failures)",
                            checkpoint_ref, sum(1 for r in results if not r.get("ok")))
                for r in results:
                    if r.get("ok"):
                        r["rolled_back"] = True
            except Exception as e:
                log.error("Atomic edit: rollback failed: %s", e)

        await send.response(req_id, {
            "results":    results,
            "applied":    sum(1 for r in results if r.get("ok") and not r.get("rolled_back")),
            "failed":     sum(1 for r in results if not r.get("ok")),
            "rolled_back": failed and transactional and checkpoint_ref is not None,
            "checkpoint":  checkpoint_ref,
        })
    except Exception as e:
        log.exception("fs.apply_all_blocks failed")
        await send.error(req_id, -32603, str(e))


async def handle_lint_file(req_id: str, params: dict, send, state) -> None:
    """
    Run a linter on a file and return errors for the reflection loop.

    Research (Aider lint integration):
    - After every edit, Aider runs a linter
    - If lint fails, captures stdout/stderr with grep-ast context
    - Feeds error back to LLM: "Tests failed with this error, please fix it"
    - Max 3 reflection attempts to prevent infinite loops

    params:
      path:     str        File to lint
      cmd:      str        Lint command template (use {path} placeholder)
      language: str        Auto-detect linter if cmd not provided ("python"/"rust"/"typescript")
    """
    path     = params.get("path", "")
    cmd      = params.get("cmd", "")
    language = params.get("language", "")
    if not path:
        await send.error(req_id, -32600, "path is required")
        return
    try:
        result = await _run_lint(path, cmd, language, state)
        await send.response(req_id, result)
    except Exception as e:
        log.exception("fs.lint_file failed")
        await send.error(req_id, -32603, str(e))


async def _run_lint(path: str, cmd: str, language: str, state) -> dict:
    """
    Execute linter and return structured result.
    Auto-detects lint command from file extension if not provided.
    """
    from pathlib import Path
    p = Path(path)

    # Auto-detect linter (Aider pattern: built-in for common languages)
    if not cmd:
        ext = p.suffix.lower()
        if ext == ".py" or language == "python":
            cmd = "python -m py_compile {path}"
        elif ext == ".rs" or language == "rust":
            # Rust: use cargo check for the workspace
            cmd = "cargo check --message-format short 2>&1 | head -20"
        elif ext in (".ts", ".tsx") or language == "typescript":
            cmd = "npx tsc --noEmit --skipLibCheck"
        elif ext in (".js", ".jsx"):
            cmd = "node --check {path}"
        else:
            return {"ok": True, "errors": [], "output": "",
                    "message": f"No linter configured for {ext} files"}

    # Substitute {path} placeholder
    if "{path}" in cmd:
        cmd = cmd.format(path=path)

    # Execute via bridge (goes through Rust shell safety checks)
    try:
        result = await state.get_bridge().call("shell.run", {
            "cmd": cmd, "cwd": ".", "timeout_s": 30, "dry_run": False,
        })
        stdout = result.get("stdout", "") if isinstance(result, dict) else str(result)
        stderr = result.get("stderr", "") if isinstance(result, dict) else ""
        exit_code = result.get("exit_code", 0) if isinstance(result, dict) else 0

        output = (stdout + stderr).strip()
        ok = (exit_code == 0)

        # Extract errors with context (Aider uses grep-ast here for line context)
        errors = _extract_errors(output, path)

        return {
            "ok":      ok,
            "errors":  errors,
            "output":  output[:2000],  # Cap for context window
            "command": cmd,
            # Reflection prompt: feed this back to LLM if not ok
            "reflection_prompt": (
                f"Linting `{path}` failed with:\n\n```\n{output[:1000]}\n```\n\n"
                f"Please fix these errors." if not ok else ""
            ),
        }
    except Exception as e:
        return {"ok": False, "errors": [], "output": str(e), "command": cmd,
                "reflection_prompt": f"Could not run linter: {e}"}


def _extract_errors(output: str, path: str) -> list[dict]:
    """Extract structured error locations from lint output."""
    import re
    errors = []
    # Common lint output patterns: "file.py:line:col: error message"
    patterns = [
        r"([^:]+):(\d+):(\d+):\s*(error|warning|note):\s*(.+)",  # rustc, tsc
        r"([^:]+):(\d+):\s*(error|warning|E\d+|W\d+):\s*(.+)",   # flake8, pylint
        r"([^:]+):(\d+):\s*(SyntaxError|TypeError|NameError):\s*(.+)",  # Python
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, output):
            try:
                errors.append({
                    "file":    m.group(1).strip(),
                    "line":    int(m.group(2)),
                    "message": m.group(m.lastindex).strip(),
                })
            except (IndexError, ValueError):
                pass
    return errors[:10]  # Cap at 10 errors
