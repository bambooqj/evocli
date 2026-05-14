# pyright: reportMissingImports=false, reportMissingTypeArgument=false
"""
intent_profile.py — Goal-aware behavior profiles for EvoCLI

Every request is classified once at the loop entry point using
local_classifier.classify_by_similarity (semantic cosine similarity,
zero-shot, no LLM call). The resulting IntentProfile drives ALL
downstream behavior:

  - max_iterations: how many autonomous loop turns
  - context_depth:  how much context to build ("none" | "minimal" | "standard" | "full")
  - writes_allowed: whether file-write tools are permitted
  - forcing_enabled: whether the "act now" message is injected on idle turns
  - require_confirm: whether to pause and ask before executing risky ops
  - auto_commit:     whether to auto-commit after task_complete

Classification is semantic (embedding-based), not keyword-based.
Fallback to keyword matching when fastembed is unavailable.

User overrides: ~/.evocli/intent_profiles.toml can override any profile.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("evocli.intent_profile")


# ── Intent Profile dataclass ──────────────────────────────────────────────────

@dataclass
class IntentProfile:
    """Behavioral parameters derived from user's goal."""

    # Classification result
    intent: str               # "chat" | "question" | "researcher" | "planner"
                              #   | "reviewer" | "debugger" | "coder" | "risky"

    # Loop behavior
    max_iterations: int       # max autonomous loop turns
    forcing_enabled: bool     # inject forcing message when agent produces only text

    # Context depth
    context_depth: str        # "none" | "minimal" | "standard" | "full"

    # Safety
    writes_allowed: bool      # whether file-write tools should be offered
    require_confirm: bool     # pause before risky writes, ask user

    # Post-completion
    auto_commit: bool         # auto-commit after task_complete

    # Human-readable
    description: str = ""
    reason: str = ""          # why this profile was chosen (for logging)

    # Extra context hints passed to context_engine
    context_hints: dict[str, Any] = field(default_factory=dict)


# ── Canonical descriptions for semantic classification ────────────────────────
# These descriptions are embedded and compared against the user's prompt.
# Keeping them in one place makes it easy to tune classification accuracy.

INTENT_DESCRIPTIONS: dict[str, str] = {
    "chat": (
        "The user is greeting, saying hello, expressing gratitude, making small talk, "
        "or having a casual conversational exchange. No technical task is requested. "
        "Examples: 'hello', 'hi', 'thanks', 'good morning', 'how are you', "
        "'你好', '谢谢', '早上好', '好的', 'ok'"
    ),
    "question": (
        "The user is asking a specific question to get information or an explanation. "
        "No code changes are requested. They want to understand something. "
        "Examples: 'what is X', 'how does Y work', 'explain Z', 'why does A happen', "
        "'什么是', '怎么工作', '解释一下', '为什么'"
    ),
    "researcher": (
        "The user wants to find, search, explore, or understand existing code or "
        "documentation in the codebase. They need information gathered before taking action. "
        "Examples: 'find where X is defined', 'show me all usages of Y', "
        "'which file handles Z', 'what does this function do', "
        "'找到', '搜索', '在哪里', '显示', '找出'"
    ),
    "planner": (
        "The user wants to plan, design, architect, or think through an approach "
        "before writing any code. They want a roadmap or strategy, not implementation yet. "
        "Examples: 'how should I approach X', 'design a system for Y', "
        "'what is the best way to Z', 'create a plan for', "
        "'如何设计', '规划', '方案', '架构设计', '怎么实现好'"
    ),
    "reviewer": (
        "The user wants existing code reviewed, checked, audited, or quality assessed. "
        "They want feedback but not necessarily code changes. "
        "Examples: 'review this code', 'check for bugs', 'audit security', "
        "'is this good practice', 'lint and check', "
        "'审查', '检查', '审计', '代码质量', '有没有问题'"
    ),
    "debugger": (
        "The user has a bug, error, test failure, crash, or unexpected behavior "
        "that needs to be investigated and fixed. "
        "Examples: 'fix this error', 'tests are failing', 'something is broken', "
        "'why is X not working', 'debug this crash', "
        "'修复错误', '调试', '为什么不工作', '测试失败', '崩溃', 'bug'"
    ),
    "coder": (
        "The user wants new code written, a feature implemented, something created, "
        "or existing code refactored. They want working code as output. "
        "Examples: 'implement X', 'add feature Y', 'create a function for Z', "
        "'refactor this module', 'write tests for', "
        "'实现', '添加功能', '创建', '重构', '编写测试', '开发'"
    ),
    "risky": (
        "The user wants to perform a potentially destructive or irreversible operation: "
        "deleting files, dropping databases, removing code, overwriting critical files, "
        "resetting state, or any mass modification. "
        "Examples: 'delete all X', 'remove the entire Y', 'drop the database', "
        "'wipe Z', 'overwrite everything', "
        "'删除所有', '移除整个', '清空', '删掉', '覆盖所有'"
    ),
}

# ── Behavior profiles keyed by intent ────────────────────────────────────────

_PROFILES: dict[str, IntentProfile] = {
    "chat": IntentProfile(
        intent="chat",
        max_iterations=1,
        forcing_enabled=False,
        context_depth="none",
        writes_allowed=False,
        require_confirm=False,
        auto_commit=False,
        description="Conversational exchange — single turn, no tools, no context",
    ),
    "question": IntentProfile(
        intent="question",
        max_iterations=1,
        forcing_enabled=False,
        context_depth="minimal",
        writes_allowed=False,
        require_confirm=False,
        auto_commit=False,
        description="Information request — single turn, minimal context, no writes",
    ),
    "researcher": IntentProfile(
        intent="researcher",
        max_iterations=3,
        forcing_enabled=True,
        context_depth="standard",
        writes_allowed=False,
        require_confirm=False,
        auto_commit=False,
        description="Code exploration — short loop, standard context, read-only",
    ),
    "planner": IntentProfile(
        intent="planner",
        max_iterations=3,
        forcing_enabled=True,
        context_depth="standard",
        writes_allowed=False,
        require_confirm=False,
        auto_commit=False,
        description="Planning and design — short loop, standard context, no code writes",
    ),
    "reviewer": IntentProfile(
        intent="reviewer",
        max_iterations=4,
        forcing_enabled=True,
        context_depth="full",
        writes_allowed=False,
        require_confirm=False,
        auto_commit=False,
        description="Code review — medium loop, full context, read-only",
    ),
    "debugger": IntentProfile(
        intent="debugger",
        max_iterations=8,
        forcing_enabled=True,
        context_depth="full",
        writes_allowed=True,
        require_confirm=False,
        auto_commit=True,
        description="Bug investigation — full loop, full context, writes OK",
    ),
    "coder": IntentProfile(
        intent="coder",
        max_iterations=8,
        forcing_enabled=True,
        context_depth="full",
        writes_allowed=True,
        require_confirm=False,
        auto_commit=True,
        description="Implementation task — full loop, full context, writes OK",
    ),
    "risky": IntentProfile(
        intent="risky",
        max_iterations=8,
        forcing_enabled=True,
        context_depth="full",
        writes_allowed=True,
        require_confirm=True,   # pause and ask user before executing
        auto_commit=False,      # don't auto-commit destructive ops
        description="Destructive operation — requires explicit user confirmation",
    ),
}

# ── User override support ─────────────────────────────────────────────────────

def _load_user_overrides() -> dict[str, dict]:
    """Load ~/.evocli/intent_profiles.toml if present."""
    p = Path.home() / ".evocli" / "intent_profiles.toml"
    if not p.exists():
        return {}
    try:
        try:
            import tomllib
            with open(p, "rb") as f:
                return tomllib.load(f)
        except ImportError:
            import tomli  # type: ignore[import]
            with open(p, "rb") as f:
                return tomli.load(f)
    except Exception as e:
        log.debug("Failed to load user intent_profiles.toml: %s", e)
        return {}


def _build_profiles() -> dict[str, IntentProfile]:
    """Build profiles with user overrides applied."""
    profiles = dict(_PROFILES)
    overrides = _load_user_overrides()
    for intent, override in overrides.items():
        if intent in profiles:
            base = profiles[intent]
            profiles[intent] = IntentProfile(
                intent=intent,
                max_iterations=override.get("max_iterations", base.max_iterations),
                forcing_enabled=override.get("forcing_enabled", base.forcing_enabled),
                context_depth=override.get("context_depth", base.context_depth),
                writes_allowed=override.get("writes_allowed", base.writes_allowed),
                require_confirm=override.get("require_confirm", base.require_confirm),
                auto_commit=override.get("auto_commit", base.auto_commit),
                description=base.description,
            )
    return profiles


# ── Classification ────────────────────────────────────────────────────────────

def classify(prompt: str, config: dict | None = None) -> IntentProfile:
    """
    Classify the user's intent from their prompt.

    Uses semantic cosine similarity (local_classifier) when fastembed is
    available, falls back to keyword matching otherwise. Result is used to
    configure the entire downstream behavior — loop iterations, context
    depth, write permissions, etc.

    Never raises — always returns a valid IntentProfile.

    Args:
        prompt: raw user input
        config: optional config dict (for future per-project overrides)

    Returns:
        IntentProfile matching the user's detected goal
    """
    profiles = _build_profiles()

    if not prompt or not prompt.strip():
        return profiles["chat"]

    stripped = prompt.strip()

    # ── Stage 1 removed: No length-based shortcuts ────────────────────────────
    # Previously had a "≤10 chars → chat" fast path. This was WRONG because
    # Chinese text is semantically dense: "帮我分析下当前工程" (9 chars) is a
    # project analysis request, not a greeting.
    # The semantic classifier below handles all cases correctly. Let it run.

    # ── Stage 2: Semantic classification (embedding-based, zero-shot) ─────
    try:
        from evocli_soul.local_classifier import classify_by_similarity, record_label
        intent = classify_by_similarity(
            prompt,
            INTENT_DESCRIPTIONS,
            threshold=0.22,   # slightly lower than default for broader coverage
            fallback="",
        )
        if intent and intent in profiles:
            record_label(prompt, intent, extra={"source": "intent_profile"})
            profile = profiles[intent]
            log.debug("intent: semantic '%s...' → %s", prompt[:40], intent)
            return _with_reason(profile, f"semantic similarity (intent={intent})")
    except Exception as e:
        log.debug("semantic classification failed, using keyword fallback: %s", e)

    # ── Stage 3: Keyword fallback (when fastembed unavailable) ────────────
    intent = _keyword_classify(prompt)
    log.debug("intent: keyword '%s...' → %s", prompt[:40], intent)
    profile = profiles.get(intent, profiles["coder"])
    return _with_reason(profile, f"keyword matching (intent={intent})")


def _with_reason(profile: IntentProfile, reason: str) -> IntentProfile:
    """Return a copy of the profile with the reason field set."""
    import copy
    p = copy.copy(profile)
    p.reason = reason
    return p


def _keyword_classify(prompt: str) -> str:
    """Keyword-based fallback classifier."""
    p = prompt.lower()

    # Destructive first (highest priority — safety)
    destructive = {"delete all", "remove all", "wipe", "drop database", "overwrite all",
                   "删除所有", "移除所有", "清空", "删掉所有", "覆盖所有"}
    if any(kw in p for kw in destructive):
        return "risky"

    # Greetings
    greetings = {"hello", "hi ", "hey ", "thanks", "thank you", "good morning",
                 "你好", "谢谢", "早上好", "晚上好", "嗨"}
    if any(kw in p for kw in greetings) and len(p) < 30:
        return "chat"

    # Questions (no action implied)
    questions = {"what is", "what are", "how does", "explain", "why does", "where is",
                 "什么是", "怎么工作", "解释", "为什么", "在哪里"}
    if any(kw in p for kw in questions) and not _has_action_verb(p):
        return "question" if len(p) < 60 else "researcher"

    # Review
    review = {"review", "check", "audit", "lint", "审查", "检查", "审计"}
    if any(kw in p for kw in review):
        return "reviewer"

    # Debug
    debug = {"debug", "fix", "error", "crash", "fail", "broken", "bug",
             "调试", "修复", "错误", "崩溃", "失败", "不工作"}
    if any(kw in p for kw in debug):
        return "debugger"

    # Plan/design
    plan = {"plan", "design", "architect", "roadmap", "approach", "strategy",
            "规划", "设计", "架构", "方案", "策略"}
    if any(kw in p for kw in plan) and not _has_impl_verb(p):
        return "planner"

    # Research/find
    research = {"find", "search", "where", "show me", "list all",
                "找到", "搜索", "哪里", "显示", "列出"}
    if any(kw in p for kw in research):
        return "researcher"

    # Implementation (default for action verbs)
    if _has_impl_verb(p):
        return "coder"

    return "coder"  # safe default: treat as task


def _has_action_verb(p: str) -> bool:
    verbs = {"implement", "create", "write", "add", "build", "refactor",
             "实现", "创建", "编写", "添加", "重构", "开发"}
    return any(v in p for v in verbs)


def _has_impl_verb(p: str) -> bool:
    verbs = {"implement", "create", "write", "add", "build", "refactor",
             "generate", "make", "develop", "update", "modify",
             "实现", "创建", "编写", "添加", "重构", "开发", "生成", "修改"}
    return any(v in p for v in verbs)


# ── Context depth → context_engine params ────────────────────────────────────

def context_params_for(profile: IntentProfile) -> dict:
    """
    Translate IntentProfile.context_depth into context_engine.build() params.

    depth="none"     → system + recent history only
    depth="minimal"  → system + recent history + constraints
    depth="standard" → tier 0 + compact symbol navigation
    depth="full"     → tier 0 + compact symbol navigation + current file
    """
    depth = profile.context_depth
    if depth == "none":
        return {
            "context_depth": "none",
            "skip_repomap": True,
            "skip_memory": True,
            "skip_skills": True,
            "skip_constraints": True,
            "lightweight": True,
        }
    if depth == "minimal":
        return {
            "context_depth": "minimal",
            "skip_repomap": True,
            "skip_memory": True,
            "skip_skills": True,
            "lightweight": True,
        }
    if depth == "standard":
        return {
            "context_depth": "standard",
            "skip_repomap": False,
            "skip_memory": True,
            "skip_skills": True,
            "compact_symbols": True,
            "lightweight": False,
        }
    return {
        "context_depth": "full",
        "skip_memory": True,
        "skip_skills": True,
        "compact_symbols": True,
        "lightweight": False,
    }
