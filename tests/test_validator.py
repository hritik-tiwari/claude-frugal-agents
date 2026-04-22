"""Tests for :mod:`claude_frugal_agents.validator`."""

from __future__ import annotations

import re

import pytest

from claude_frugal_agents.validator import (
    ClaimValidator,
    exact_comparator,
    polarity_comparator,
    regex_comparator,
    substring_comparator,
)


def test_substring_comparator_accepts_either_direction() -> None:
    assert substring_comparator("Alice Smith", "alice") is True
    assert substring_comparator("alice", "Alice Smith") is True
    assert substring_comparator("bob", "alice") is False


def test_substring_comparator_treats_none_as_unchecked() -> None:
    assert substring_comparator(None, "alice") is True
    assert substring_comparator("null", "alice") is True


def test_exact_comparator_case_insensitive() -> None:
    assert exact_comparator("ALICE@example.com", "alice@example.com") is True
    assert exact_comparator("alice@example.com", "bob@example.com") is False


def test_regex_comparator_matches_pattern() -> None:
    digits_only = regex_comparator(r"^\d{10}$")
    assert digits_only("7651234567", "any") is True
    assert digits_only("(765) 123-4567", "any") is False


def test_polarity_comparator_aligns_positive_phrases() -> None:
    work_auth = polarity_comparator(
        positive_phrases=("i am authorized", "authorized to work", "us citizen"),
        negative_phrases=("not authorized", "i am not"),
    )
    assert work_auth("I am authorized to work", "yes") is True
    assert work_auth("Not authorized", "yes") is False
    assert work_auth("Yes", "Yes") is True


def test_validator_extract_balanced_braces() -> None:
    v = ClaimValidator(expected={"x": "y"})
    text = (
        "agent thinking... more text\n"
        'PRESUBMIT_CHECK: {"email": "a@b.com", "metadata": {"nested": true}}\n'
        "more text after"
    )
    claim = v.extract(text)
    assert claim == {"email": "a@b.com", "metadata": {"nested": True}}


def test_validator_full_pass_path() -> None:
    v = ClaimValidator(
        expected={"email": "alice@example.com", "first_name": "Alice"},
        comparators={"email": exact_comparator, "first_name": substring_comparator},
    )
    result = v.validate(
        'PRESUBMIT_CHECK: {"email": "alice@example.com", "first_name": "Alice Smith"}'
    )
    assert result is not None
    assert result.ok is True
    assert result.stream_marker() == "PRESUBMIT_OK"


def test_validator_reports_specific_mismatch() -> None:
    v = ClaimValidator(
        expected={"email": "alice@example.com"},
        comparators={"email": exact_comparator},
    )
    result = v.validate('PRESUBMIT_CHECK: {"email": "bob@example.com"}')
    assert result is not None
    assert result.ok is False
    assert len(result.mismatches) == 1
    assert "email" in result.mismatches[0]
    assert result.reason == "1_field_mismatch"
    assert result.stream_marker().startswith("PRESUBMIT_FAIL:")


def test_validator_ignores_extra_agent_keys() -> None:
    v = ClaimValidator(expected={"email": "a@b.com"}, comparators={"email": exact_comparator})
    result = v.validate(
        'PRESUBMIT_CHECK: {"email": "a@b.com", "agent_whim": "whatever"}'
    )
    assert result is not None
    assert result.ok is True


def test_validator_returns_none_when_no_marker() -> None:
    v = ClaimValidator(expected={"email": "a@b.com"})
    assert v.validate("the agent has not produced a claim yet") is None


def test_validator_custom_prefix() -> None:
    v = ClaimValidator(expected={"amount": "100"}, prefix="PRE_TRANSACTION")
    result = v.validate('PRE_TRANSACTION: {"amount": "100"}')
    assert result is not None
    assert result.ok is True
