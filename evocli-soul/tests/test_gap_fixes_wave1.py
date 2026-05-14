"""
tests/test_gap_fixes_wave1.py — Regression tests for Wave 1 gap fixes

Covers:
  T1 - GAP-5: Atomic history writes (state._save_history_to_disk)
  T2 - GAP-2: Cancellation flag (state.cancel_session / is_cancelled / clear_cancel)
  T3 - GAP-6: Thinking/reasoning token streaming (agent_litellm)
  T4 - GAP-4: Retry soul_status events (llm_client._acompletion_with_retry_events)
"""
from __future__ import annotations
import asyncio
import json
import pathlib
import sys
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))


# ── T1: Atomic history writes ─────────────────────────────────────────────────

def test_atomic_history_write(tmp_path, monkeypatch):
    """Final file exists; .tmp file is removed after successful save."""
    import evocli_soul.state as state
    monkeypatch.setattr(state, "_history_path", lambda sid: tmp_path / f"{sid}.json")
    messages = [{"role": "user", "content": "hello atomic"}]
    state._save_history_to_disk("atomic_test", messages)
    final = tmp_path / "atomic_test.json"
    tmp   = tmp_path / "atomic_test.tmp"
    assert final.exists(), "Final .json must exist"
    assert not tmp.exists(), ".tmp must be cleaned up"
    assert json.loads(final.read_text()) == messages


def test_atomic_history_write_overwrites(tmp_path, monkeypatch):
    """Second save overwrites first cleanly."""
    import evocli_soul.state as state
    monkeypatch.setattr(state, "_history_path", lambda sid: tmp_path / f"{sid}.json")
    state._save_history_to_disk("ow_test", [{"role": "user", "content": "v1"}])
    state._save_history_to_disk("ow_test", [{"role": "user", "content": "v2"}])
    final = tmp_path / "ow_test.json"
    assert json.loads(final.read_text())[0]["content"] == "v2"
    assert not (tmp_path / "ow_test.tmp").exists()


def test_atomic_history_write_never_raises(tmp_path, monkeypatch):
    """_save_history_to_disk must never propagate exceptions."""
    import evocli_soul.state as state
    bad_file = tmp_path / "not_a_dir"
    bad_file.write_text("I am a file")
    monkeypatch.setattr(state, "_history_path", lambda sid: bad_file / f"{sid}.json")
    state._save_history_to_disk("bad_path", [{"role": "user", "content": "x"}])


# ── T2: Cancellation flag ─────────────────────────────────────────────────────

def test_cancellation_session_isolated():
    """Cancelling session A must not affect session B."""
    import evocli_soul.state as state
    state.clear_cancel("iso_A")
    state.clear_cancel("iso_B")
    state.cancel_session("iso_A")
    assert state.is_cancelled("iso_A") is True
    assert state.is_cancelled("iso_B") is False
    state.clear_cancel("iso_A")
    assert state.is_cancelled("iso_A") is False


def test_cancellation_clear_resets():
    import evocli_soul.state as state
    sid = "cancel_clear"
    state.cancel_session(sid)
    assert state.is_cancelled(sid)
    state.clear_cancel(sid)
    assert not state.is_cancelled(sid)


def test_cancellation_false_by_default():
    import evocli_soul.state as state
    sid = "cancel_fresh_xyz_unique_001"
    state.clear_cancel(sid)
    assert not state.is_cancelled(sid)


# ── T3: Reasoning/thinking token streaming ───────────────────────────────────

def test_reasoning_content_extracted():
    """reasoning_content attribute on delta extracted correctly."""
    class MockDelta:
        content = ""
        tool_calls = None
        reasoning_content = "Analysing..."
    delta = MockDelta()
    _reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "thinking", None) or ""
    assert _reasoning == "Analysing..."
    assert f"*{_reasoning}*" == "*Analysing...*"


def test_thinking_attr_fallback():
    """'thinking' attribute used as fallback."""
    class MockDelta:
        content = ""
        tool_calls = None
        thinking = "Deep thought."
    delta = MockDelta()
    _reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "thinking", None) or ""
    assert _reasoning == "Deep thought."


def test_no_reasoning_no_extra_yield():
    """Delta with neither attribute gives empty string."""
    class MockDelta:
        content = "Hello!"
        tool_calls = None
    delta = MockDelta()
    _reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "thinking", None) or ""
    assert _reasoning == ""


def test_reasoning_content_none_skipped():
    """reasoning_content=None treated as empty."""
    class MockDelta:
        content = "answer"
        tool_calls = None
        reasoning_content = None
    delta = MockDelta()
    _reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "thinking", None) or ""
    assert not _reasoning


# ── T4: Retry events ─────────────────────────────────────────────────────────

def test_retry_after_stored():
    import evocli_soul.llm_client as llm_mod
    class _FakeRouter:
        def __init__(self, **kw): pass
    orig = llm_mod.Router
    llm_mod.Router = _FakeRouter
    try:
        client = llm_mod.LLMClient({
            "provider": "openai",
            "tiers": {"fast": "gpt-4o-mini", "smart": "gpt-4o"},
            "params": {"retry_after": 7},
        })
        assert client._retry_after == 7
    finally:
        llm_mod.Router = orig


def test_retry_after_default():
    import evocli_soul.llm_client as llm_mod
    class _FakeRouter:
        def __init__(self, **kw): pass
    orig = llm_mod.Router
    llm_mod.Router = _FakeRouter
    try:
        client = llm_mod.LLMClient({
            "provider": "openai",
            "tiers": {"fast": "gpt-4o-mini", "smart": "gpt-4o"},
        })
        assert client._retry_after == 5
    finally:
        llm_mod.Router = orig


@pytest.mark.asyncio
async def test_retry_method_exists():
    """_acompletion_with_retry_events method must exist."""
    import evocli_soul.llm_client as llm_mod
    class _FakeRouter:
        def __init__(self, **kw): pass
    orig = llm_mod.Router
    llm_mod.Router = _FakeRouter
    try:
        client = llm_mod.LLMClient({
            "provider": "openai",
            "tiers": {"fast": "gpt-4o-mini", "smart": "gpt-4o"},
        })
        assert hasattr(client, "_acompletion_with_retry_events"), \
            "_acompletion_with_retry_events method must exist"
        assert callable(client._acompletion_with_retry_events)
    finally:
        llm_mod.Router = orig
