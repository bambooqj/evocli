"""
DEPRECATED — evocli_soul.evolution_engine

This module is superseded by the evocli_soul.evolution package
(evolution/__init__.py). All active code uses the package:

  from evocli_soul.evolution import EvolutionEngine  <- correct
  from evocli_soul.evolution_engine import ...       <- WRONG (this file)

The package (evolution/__init__.py) includes:
  - pattern_detector.py   -- PrefixSpan + sliding-window detection
  - skill_draft.py        -- TOML skill draft generation
  - failure_miner.py      -- failure chain mining
  - knowledge_classifier.py -- cross-project knowledge transfer
  - decay_detector.py     -- skill decay detection
  - scheduler.py          -- background scheduling
  - circuit_breaker.py    -- circuit breaker

This file is kept only to prevent ImportError in any external code that
may still reference it. New callers MUST use evocli_soul.evolution.
This file will be deleted in the next major cleanup.
"""
from __future__ import annotations
import warnings as _warnings

_warnings.warn(
    "evocli_soul.evolution_engine is deprecated. "
    "Use 'from evocli_soul.evolution import EvolutionEngine' instead.",
    DeprecationWarning,
    stacklevel=2,
)

# Re-export from the active package so existing callers don't break immediately
from evocli_soul.evolution import EvolutionEngine  # noqa: F401

__all__ = ["EvolutionEngine"]
