# pyright: reportMissingTypeArgument=false
"""
history_utils.py — Conversation history utilities

Implements Cline's ContextManager deduplication pattern:
when the same file is read/written multiple times in a conversation,
older occurrences are replaced with a placeholder to prevent context bloat.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("evocli.history_utils")

Message = dict[str, object]

# Patterns that indicate file content in a message
_FILE_PATH_PATTERNS = [
    re.compile(r'fs\.read[^\n]*?["\']([^"\']+\.[a-zA-Z0-9]+)["\']'),
    re.compile(r'fs\.write[^\n]*?["\']([^"\']+\.[a-zA-Z0-9]+)["\']'),
    re.compile(r'"path"\s*:\s*"([^"]+\.[a-zA-Z0-9]+)"'),
    re.compile(r'@file:([^\s\]]+)'),
]

_PLACEHOLDER = "[File content superseded by a later version in this conversation]"


def _extract_file_paths(msg: Message) -> set[str]:
    """Extract file paths referenced in a conversation message."""
    paths: set[str] = set()
    content = msg.get("content", "")
    if not isinstance(content, str):
        # Handle list-type content (tool results)
        if isinstance(content, list):
            content = " ".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
            )
        else:
            content = str(content)
    for pattern in _FILE_PATH_PATTERNS:
        for match in pattern.finditer(content):
            path = match.group(1).strip()
            if path and len(path) > 2:  # ignore trivially short paths
                paths.add(path)
    return paths


def deduplicate_file_reads(history: list[Message]) -> list[Message]:
    """
    Replace older file reads with a placeholder, keeping only the latest version.

    Implements Cline's ContextManager optimization:
    - Scans history for messages that contain file content
    - For files that appear multiple times, replaces all but the LAST occurrence
    - Preserves message structure (role, tool_call_id, etc.) — only content is replaced
    - Result: prevents context bloat in iterative editing sessions

    Args:
        history: list of conversation message dicts

    Returns:
        Deduplicated history (may be shorter in effective token count)
    """
    if not history:
        return history

    # Pass 1: find the LAST occurrence index for each file path
    file_last_index: dict[str, int] = {}
    for i, msg in enumerate(history):
        role = msg.get("role", "")
        # Only check tool results and user messages (where file content appears)
        if role in ("tool", "user"):
            for path in _extract_file_paths(msg):
                file_last_index[path] = i

    if not file_last_index:
        return history

    # Pass 2: replace older occurrences with placeholder
    result: list[Message] = []
    replaced_count = 0

    for i, msg in enumerate(history):
        role = msg.get("role", "")
        if role in ("tool", "user"):
            paths = _extract_file_paths(msg)
            # Check if ANY path in this message has a later occurrence
            has_later = any(
                file_last_index.get(p, i) > i
                for p in paths
            )
            if has_later and paths:
                # Replace content with placeholder, keep message structure
                replaced_msg = dict(msg)
                old_content = msg.get("content", "")
                if isinstance(old_content, str) and len(old_content) > 100:
                    replaced_msg["content"] = _PLACEHOLDER
                    replaced_count += 1
                result.append(replaced_msg)
                continue
        result.append(msg)

    if replaced_count > 0:
        log.debug("deduplicate_file_reads: replaced %d old file reads with placeholder", replaced_count)

    return result
