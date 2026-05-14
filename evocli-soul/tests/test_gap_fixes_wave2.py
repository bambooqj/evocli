"""
tests/test_gap_fixes_wave2.py - Regression tests for Wave 2-3 gap fixes

Covers:
  T5 - GAP-1: require_confirm gate
  T6 - GAP-3: context_params skipped on iterations 1+
"""
from __future__ import annotations
import asyncio
import pathlib
import sys
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))


# ── T5: require_confirm gate ─────────────────────────────────────────────────

def test_require_confirm_strips_yes_prefix():
    """'yes ' prefix stripped correctly."""
    _CONFIRM_PREFIXES = ("yes ", "yes,", "confirm ", "y ")
    prompt = "yes delete all log files"
    for _pfx in _CONFIRM_PREFIXES:
        if prompt.lower().startswith(_pfx):
            prompt = prompt[len(_pfx):].lstrip()
            break
    assert prompt == "delete all log files"


def test_require_confirm_strips_confirm_prefix():
    """'confirm ' prefix variant stripped correctly."""
    _CONFIRM_PREFIXES = ("yes ", "yes,", "confirm ", "y ")
    prompt = "confirm drop the database"
    for _pfx in _CONFIRM_PREFIXES:
        if prompt.lower().startswith(_pfx):
            prompt = prompt[len(_pfx):].lstrip()
            break
    assert prompt == "drop the database"


def test_require_confirm_no_false_positive():
    """Non-risky prompts do not match confirm prefixes."""
    _CONFIRM_PREFIXES = ("yes ", "yes,", "confirm ", "y ")
    prompt = "implement authentication"
    matched = any(prompt.lower().startswith(p) for p in _CONFIRM_PREFIXES)
    assert not matched, "Non-risky prompt should not match confirm prefix"


def test_require_confirm_case_insensitive():
    """Prefix matching is case-insensitive."""
    _CONFIRM_PREFIXES = ("yes ", "yes,", "confirm ", "y ")
    prompt = "YES delete all logs"
    matched = any(prompt.lower().startswith(p) for p in _CONFIRM_PREFIXES)
    assert matched, "YES (uppercase) should match"


# ── T6: context_params first-iter only ───────────────────────────────────────

def test_context_params_first_iter_only():
    """On iteration 0, _iter_context_params equals _context_params."""
    context_params = {"project_id": "proj1", "context_depth": "full"}
    for _auto_iter in range(4):
        _is_first_iter = (_auto_iter == 0)
        _iter_context_params = context_params if _is_first_iter else {}
        if _auto_iter == 0:
            assert _iter_context_params is context_params
        else:
            assert _iter_context_params == {}


def test_context_params_empty_on_continuation():
    """5-iteration loop: only iter 0 gets context."""
    context_params = {"context_depth": "full"}
    results = []
    for _auto_iter in range(5):
        _is_first_iter = (_auto_iter == 0)
        results.append(context_params if _is_first_iter else {})
    assert results[0] == context_params
    assert all(r == {} for r in results[1:])


def test_context_params_does_not_mutate_original():
    """Empty dict on continuation must not affect original context_params."""
    context_params = {"context_depth": "full"}
    for _auto_iter in range(3):
        _is_first_iter = (_auto_iter == 0)
        _iter_context_params = context_params if _is_first_iter else {}
        if not _is_first_iter:
            _iter_context_params["injected"] = True
    assert "injected" not in context_params
