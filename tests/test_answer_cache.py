"""Tests for :mod:`claude_frugal_agents.answer_cache`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_frugal_agents.answer_cache import AnswerCache


def test_extract_from_text_parses_multiple_markers(tmp_path: Path) -> None:
    text = (
        'NEW_QUESTION: {"question": "How many years of Python?", "draft_answer": "2"}\n'
        "some other chatter\n"
        'NEW_QUESTION: {"question": "Willing to relocate?", "options": ["Yes","No"]}\n'
    )
    out = AnswerCache.extract_from_text(text, marker="NEW_QUESTION")
    assert len(out) == 2
    assert out[0]["question"] == "How many years of Python?"
    assert out[1]["options"] == ["Yes", "No"]


def test_extract_handles_nested_json_braces(tmp_path: Path) -> None:
    text = 'NEW_QUESTION: {"question": "Q", "meta": {"nested": {"deep": 1}}}\n'
    out = AnswerCache.extract_from_text(text, marker="NEW_QUESTION")
    assert len(out) == 1
    assert out[0]["meta"]["nested"]["deep"] == 1


def test_record_and_pending(tmp_path: Path) -> None:
    cache = AnswerCache(tmp_path / "cache.json")
    added = cache.record(
        "NEW_QUESTION",
        {"question": "Salary expectations?"},
        context={"url": "https://example.com/job/1"},
    )
    assert added is True

    pending = list(cache.pending())
    assert len(pending) == 1
    assert pending[0].question == "Salary expectations?"
    assert pending[0].context == {"url": "https://example.com/job/1"}
    assert pending[0].status == "pending"


def test_record_dedups_same_question(tmp_path: Path) -> None:
    cache = AnswerCache(tmp_path / "cache.json")
    assert cache.record("NEW_QUESTION", {"question": "Q?"}) is True
    assert cache.record("NEW_QUESTION", {"question": "q?"}) is False  # dup (case)
    assert len(list(cache.pending())) == 1


def test_approve_updates_status_and_attaches_answer(tmp_path: Path) -> None:
    cache = AnswerCache(tmp_path / "cache.json")
    cache.record("NEW_QUESTION", {"question": "Fav color?"})
    assert cache.approve("Fav color?", "blue") is True

    remaining = list(cache.pending())
    assert remaining == []

    all_entries = cache.all()
    assert len(all_entries) == 1
    assert all_entries[0].status == "approved"
    assert all_entries[0].approved_answer == "blue"


def test_reject_moves_out_of_pending(tmp_path: Path) -> None:
    cache = AnswerCache(tmp_path / "cache.json")
    cache.record("NEW_QUESTION", {"question": "Trick question?"})
    assert cache.reject("Trick question?") is True
    assert list(cache.pending()) == []
    assert cache.all()[0].status == "rejected"


def test_record_from_text_roundtrips_through_disk(tmp_path: Path) -> None:
    cache = AnswerCache(tmp_path / "cache.json")
    stdout = (
        "agent emitting:\n"
        'NEW_QUESTION: {"question": "Preferred city?", "draft_answer": "SF"}\n'
        'NEW_QUESTION: {"question": "Clearance?", "draft_answer": "none"}\n'
    )
    added = cache.record_from_text(stdout, marker="NEW_QUESTION")
    assert added == 2

    # Round-trip via a fresh instance reading the same file.
    reread = AnswerCache(tmp_path / "cache.json")
    assert len(list(reread.pending())) == 2


def test_marker_filter_on_pending(tmp_path: Path) -> None:
    cache = AnswerCache(tmp_path / "cache.json")
    cache.record("NEW_QUESTION", {"question": "A"})
    cache.record("ANSWER_MISMATCH", {"question": "B"})

    new_qs = list(cache.pending(marker="NEW_QUESTION"))
    mismatches = list(cache.pending(marker="ANSWER_MISMATCH"))
    assert len(new_qs) == 1 and new_qs[0].question == "A"
    assert len(mismatches) == 1 and mismatches[0].question == "B"
