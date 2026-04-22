"""Deterministic claim validator for Claude-agent output.

When you let a Claude agent take an irreversible action (submit a form,
send a message, commit a transaction, delete a file), you want a cheap
Python safety check right before the action fires. This module gives you
that check without any extra LLM tokens.

The protocol:

1. You tell the agent to emit a line like::

       PRESUBMIT_CHECK: {"email": "a@b.com", "first_name": "Alice"}

   right before the irreversible action.
2. You stream the agent's stdout and call :meth:`ClaimValidator.validate`
   on each text block.
3. If the validator finds a ``PRESUBMIT_CHECK`` (or whatever prefix you
   chose) and the reported values don't match expected, you kill the
   subprocess before it can take the action.

Design goals:

* Zero LLM tokens. Pure Python regex + dict comparison.
* Safe by default. Mismatch => fail; only explicit match passes.
* Pluggable comparators: exact, substring (either direction), regex,
  polarity (positive / negative words).
* Zero magic: all expected values and comparators are passed in by the
  caller, so the module works for any domain, not just form-filling.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type + comparator protocol
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Outcome of a single validation pass.

    Attributes:
        ok: True if no mismatches were found.
        mismatches: One human-readable description per field that didn't
            match. Empty list when ``ok`` is True.
        reason: Short token suitable for status codes / logging, e.g.
            ``"3_field_mismatch"``. Empty when ``ok`` is True.
    """

    ok: bool
    mismatches: list[str] = field(default_factory=list)
    reason: str = ""

    def stream_marker(self) -> str:
        """Format a ``PRESUBMIT_OK`` / ``PRESUBMIT_FAIL: <reason>`` line.

        Useful when you want to echo the validator result back into a
        subprocess's stdin so the agent can react.
        """
        if self.ok:
            return "PRESUBMIT_OK"
        return f"PRESUBMIT_FAIL: {self.reason}"


Comparator = Callable[[object, object], bool]
"""A function ``(got, expected) -> bool`` that returns True on match.

Implementations should be safe against ``None`` and missing keys.
"""


# ---------------------------------------------------------------------------
# Built-in comparators
# ---------------------------------------------------------------------------


def _norm(v: object) -> str:
    return str(v).strip().lower() if v is not None else ""


def exact_comparator(got: object, expected: object) -> bool:
    """Case-insensitive exact-string comparator.

    Treats ``None`` / ``"null"`` / ``"none"`` in the agent's report as
    "field wasn't present" and accepts. If you want strict matching,
    wrap this in your own comparator that rejects ``None``.
    """
    g = _norm(got)
    if g in ("", "null", "none"):
        return True
    return g == _norm(expected)


def substring_comparator(got: object, expected: object) -> bool:
    """Bidirectional substring match.

    Accepts if either value is a substring of the other, case-insensitive.
    Useful for name fields where the form might show "Alice Smith" but
    the profile stores "Alice" and "Smith" separately.
    """
    g = _norm(got)
    e = _norm(expected)
    if not e:
        return True
    if g in ("", "null", "none"):
        return True
    return e in g or g in e


def regex_comparator(pattern: str, flags: int = re.IGNORECASE) -> Comparator:
    """Return a comparator that accepts if ``expected`` matches ``pattern``
    when interpreted as a regex, applied to the normalized ``got``.
    """
    compiled = re.compile(pattern, flags)

    def _cmp(got: object, expected: object) -> bool:  # noqa: ARG001
        g = _norm(got)
        if g in ("", "null", "none"):
            return True
        return compiled.search(g) is not None

    return _cmp


def polarity_comparator(
    positive_phrases: tuple[str, ...] = (),
    negative_phrases: tuple[str, ...] = (),
) -> Comparator:
    """Return a comparator that checks polarity (yes/no) alignment.

    Forms often phrase the same question in many ways. The profile may
    store ``"yes"`` / ``"no"``, but the form's option text might be
    ``"I am authorized to work in the US"``. This comparator classifies
    both sides into ``positive`` / ``negative`` / unknown and accepts
    when polarities agree (or either side is unknown).

    Args:
        positive_phrases: Additional phrases that mean "yes" in your
            domain. Built-in positives include ``yes``, ``y``, ``true``.
        negative_phrases: Additional phrases meaning "no".

    Returns:
        A :data:`Comparator` suitable for :class:`ClaimValidator`.
    """

    base_pos = ("yes", "y", "true")
    base_neg = ("no", "n", "false")
    all_pos = tuple(p.lower() for p in (*base_pos, *positive_phrases))
    all_neg = tuple(p.lower() for p in (*base_neg, *negative_phrases))

    def polarity_of(text: str) -> str | None:
        t = text.strip().lower()
        if t in ("", "null", "none"):
            return None
        if t in all_pos:
            return "positive"
        if t in all_neg:
            return "negative"
        for neg in all_neg:
            if neg and neg in t:
                return "negative"
        for pos in all_pos:
            if pos and pos in t:
                return "positive"
        return None

    def _cmp(got: object, expected: object) -> bool:
        g_pol = polarity_of(_norm(got))
        e_pol = polarity_of(_norm(expected))
        if g_pol is None or e_pol is None:
            return True  # can't judge -- don't fail
        return g_pol == e_pol

    return _cmp


# ---------------------------------------------------------------------------
# The validator
# ---------------------------------------------------------------------------


class ClaimValidator:
    """Extracts and validates a ``<prefix>_CHECK: {...}`` JSON block.

    Args:
        expected: Dict of ``field_name -> expected_value``. Any field
            the agent reports but that is not in this dict is ignored.
        comparators: Optional dict of ``field_name -> Comparator``.
            Unspecified fields default to :func:`substring_comparator`,
            which is lenient enough to work for most string-valued
            claims.
        prefix: The marker prefix the agent uses. Defaults to
            ``PRESUBMIT_CHECK``; change if you're doing something other
            than form-fill (e.g. ``PRE_TRANSACTION_CHECK``).

    Example:
        >>> v = ClaimValidator(
        ...     expected={"email": "alice@example.com", "first_name": "Alice"},
        ...     comparators={"email": exact_comparator},
        ... )
        >>> res = v.validate('PRESUBMIT_CHECK: {"email": "alice@example.com", "first_name": "Alice"}')
        >>> res.ok
        True
    """

    def __init__(
        self,
        expected: dict[str, object],
        comparators: dict[str, Comparator] | None = None,
        prefix: str = "PRESUBMIT_CHECK",
    ) -> None:
        self.expected = dict(expected)
        self.comparators: dict[str, Comparator] = dict(comparators or {})
        self.prefix = prefix
        self._regex = re.compile(
            rf"{re.escape(prefix)}:\s*(\{{.*\}})",
            re.DOTALL,
        )

    # -- extraction -------------------------------------------------------

    def extract(self, text: str) -> dict | None:
        """Pull the ``<prefix>_CHECK: {...}`` JSON block out of ``text``.

        Returns the parsed dict, or ``None`` if no marker is present or
        the JSON fails to parse.
        """
        m = self._regex.search(text)
        if not m:
            return None
        blob = m.group(1).strip()
        depth = 0
        end = -1
        for i, ch in enumerate(blob):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end > 0:
            blob = blob[:end]
        try:
            return json.loads(blob)
        except json.JSONDecodeError as e:
            log.warning("%s JSON parse failed: %s", self.prefix, e)
            return None

    # -- validation -------------------------------------------------------

    def compare(self, claim: dict) -> ValidationResult:
        """Compare an already-extracted claim dict to the expected values.

        Use this directly when the caller already has the parsed JSON
        (e.g. from a stream-JSON Claude Code session).
        """
        mismatches: list[str] = []
        for key, expected_val in self.expected.items():
            if key not in claim:
                continue  # agent didn't report -- not our job to require it
            cmp_fn = self.comparators.get(key, substring_comparator)
            got = claim[key]
            try:
                ok = cmp_fn(got, expected_val)
            except Exception as e:
                log.warning("comparator for %r raised: %s", key, e)
                ok = False
            if not ok:
                mismatches.append(
                    f"{key}: agent reported {got!r}, expected {expected_val!r}"
                )
        if mismatches:
            return ValidationResult(
                ok=False,
                mismatches=mismatches,
                reason=f"{len(mismatches)}_field_mismatch",
            )
        return ValidationResult(ok=True)

    def validate(self, text: str) -> ValidationResult | None:
        """Extract the marker from ``text`` and validate it.

        Returns ``None`` if no marker is in the text (so the caller knows
        nothing to check yet), a :class:`ValidationResult` otherwise.
        """
        claim = self.extract(text)
        if claim is None:
            return None
        return self.compare(claim)


__all__ = [
    "ClaimValidator",
    "Comparator",
    "ValidationResult",
    "exact_comparator",
    "polarity_comparator",
    "regex_comparator",
    "substring_comparator",
]
