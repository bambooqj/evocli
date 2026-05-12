"""
auto_commit.py — Aider 风格自动提交 + 会话 compaction RPC

研究来源:
- Aider (repo.py): 每次 AI 修改后自动提交，使用 weak model 生成提交信息
- Aider (commands.py): /run 命令执行 shell 并可选将输出加入上下文
- Aider (commands.py): /test 只有测试失败才加入上下文 (节省 tokens)
- Claude Code: /rewind 通过 checkpoint 恢复任意历史状态

功能:
1. auto_commit(): 提交 AI 做出的修改，使用 fast model 生成 Conventional Commit 消息
2. RPC handlers for: session.auto_commit, session.run_and_capture, session.compact
"""
from __future__ import annotations
import logging

log = logging.getLogger("evocli.auto_commit")

# Conventional Commit prefix for AI-generated commits (Aider 模式)
AI_COMMIT_PREFIX = "ai: "


async def generate_commit_message(diff_text: str, llm_client, goal: str = "") -> str:
    """
    Use a fast/cheap LLM to generate a Conventional Commit message from a diff.
    Research: Aider uses a "weak model" (GPT-4o-mini/Haiku) for commit messages.
    """
    if not diff_text.strip():
        return f"{AI_COMMIT_PREFIX}minor changes"

    prompt = (
        f"Generate a concise git commit message for this diff.\n"
        f"Format: <type>(<scope>): <description>\n"
        f"Types: feat, fix, refactor, docs, test, chore\n"
        f"Max 72 chars. No trailing period.\n"
        f"{'Context: ' + goal[:100] if goal else ''}\n\n"
        f"```diff\n{diff_text[:2000]}\n```\n\n"
        f"Reply with ONLY the commit message, no quotes or explanation."
    )
    try:
        msg = await llm_client.complete_for_task("commit", prompt)
        msg = msg.strip().strip('"\'').strip()
        if not msg.startswith(("feat", "fix", "refactor", "docs", "test", "chore")):
            msg = AI_COMMIT_PREFIX + msg
        return msg[:100]  # Git subject line limit
    except Exception as e:
        log.debug("commit message generation failed: %s", e)
        return f"{AI_COMMIT_PREFIX}applied AI edits"
