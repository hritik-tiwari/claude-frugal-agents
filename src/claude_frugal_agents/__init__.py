"""claude-frugal-agents: patterns for building Claude agents that don't waste tokens.

Most production Claude-agent work is NOT LLM orchestration. It is the
deterministic scaffolding around the LLM call: validators, watchdogs,
caches, rule-based scorers, subprocess plumbing.

This package collects the patterns extracted from a real browser-automation
pipeline that submitted 52 end-to-end form submissions. Each module is
standalone and has no cross-module imports beyond what is documented.

Public API entry points:

- :class:`ClaimValidator` -- deterministic cross-check on an agent's
  ``PREFIX_CHECK: {...}`` JSON block before it takes an irreversible action.
- :class:`AgentWatchdog` -- three-layer kill switch for subprocess agents
  (wall-clock deadline, stdout-stall detector, psutil tree-kill fallback).
- :class:`AnswerCache` -- capture-and-flag for novel questions the agent
  hit that are not in the user's knowledge base.
- :class:`KeywordScorer` -- rule-based scorer with optional LLM fallback
  for borderline cases.
- :class:`ScraperBase` -- abstract fetch -> parse -> filter -> dedup ->
  store pipeline for any tabular data source.
- :mod:`browser_helpers` -- cross-platform process-tree kill, port-based
  zombie cleanup, the File System Access API kill JS preamble.
- :mod:`zombie_cleanup` -- standalone orphan-process killer.
- :mod:`monitor` -- Rich live-dashboard scaffolding with pluggable data
  sources.
"""

from __future__ import annotations

from claude_frugal_agents.answer_cache import AnswerCache, CachedEntry
from claude_frugal_agents.keyword_scorer import KeywordScorer, ScoreBreakdown, Rule
from claude_frugal_agents.scraper_base import Candidate, ScraperBase
from claude_frugal_agents.validator import (
    ClaimValidator,
    Comparator,
    ValidationResult,
    exact_comparator,
    polarity_comparator,
    regex_comparator,
    substring_comparator,
)
from claude_frugal_agents.watchdog import AgentWatchdog, WatchdogFired

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "AgentWatchdog",
    "AnswerCache",
    "CachedEntry",
    "Candidate",
    "ClaimValidator",
    "Comparator",
    "KeywordScorer",
    "Rule",
    "ScoreBreakdown",
    "ScraperBase",
    "ValidationResult",
    "WatchdogFired",
    "exact_comparator",
    "polarity_comparator",
    "regex_comparator",
    "substring_comparator",
]
