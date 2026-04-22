"""Rule-based keyword scorer with optional LLM fallback for borderline cases.

The thesis: **80% of "rank these items" tasks do not need an LLM.** A
few dozen regex rules with explicit weights produces a score that's
deterministic, auditable, free, and identical across reruns. You only
escalate to Claude for the 20% of items that fall into a tight
borderline score band where the rule system genuinely can't tell.

Usage sketch::

    scorer = KeywordScorer(
        rules=[
            Rule(pattern=r"\\b(senior|staff|principal)\\b", weight=-3,
                 category="seniority", target="title"),
            Rule(pattern=r"\\b(intern|new grad)\\b", weight=+2,
                 category="seniority", target="title"),
            Rule(pattern=r"\\b(python|sql)\\b", weight=+1,
                 category="skill", target="description"),
        ],
        base=5,
        borderline=(5, 6),
    )

    breakdown = scorer.score({"title": "Senior ML Engineer",
                              "description": "Python and SQL required"})
    # breakdown.score is an int in 1..10; breakdown.components lists matches
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

log = logging.getLogger(__name__)


@dataclass
class Rule:
    """One scoring rule.

    Attributes:
        pattern: Regex applied with :data:`re.IGNORECASE`. Word-boundary
            anchors (``\\b``) are recommended; the scorer does not add
            them for you.
        weight: Points to add if the pattern matches. Can be negative
            (penalty).
        category: Short label shown in :meth:`ScoreBreakdown.reasoning`.
            Used for grouping components in output.
        target: Which field to match against. Default ``"combined"`` =
            all string fields concatenated. Other values look up the
            named key in the item dict.
        cap: Max total points this rule can contribute across multiple
            matches in the same item. Default 1 = fire once, even if
            the pattern appears many times.
    """

    pattern: str
    weight: int
    category: str = "rule"
    target: str = "combined"
    cap: int | None = None

    def __post_init__(self) -> None:
        self._compiled = re.compile(self.pattern, re.IGNORECASE)

    def apply(self, text: str) -> tuple[int, list[str]]:
        """Match ``text`` against this rule.

        Returns a tuple ``(points, matched_substrings)``. ``points`` is
        weight * number-of-hits (capped by ``cap`` if set, else by 1).
        """
        if not text:
            return 0, []
        hits = self._compiled.findall(text)
        if not hits:
            return 0, []
        n = len(hits)
        cap = self.cap if self.cap is not None else 1
        n = min(n, cap)
        # Normalize findall output: it returns strings or tuples depending
        # on capture groups. Flatten to strings for display.
        samples: list[str] = []
        for h in hits[:3]:  # first 3 for display only
            samples.append(h if isinstance(h, str) else " ".join(h))
        return self.weight * n, samples


@dataclass
class ScoreBreakdown:
    """Return value from :meth:`KeywordScorer.score`.

    Attributes:
        score: Final integer score, clamped to ``[1, 10]``.
        raw_score: Pre-clamp sum of base + all components. Useful if
            you want to know "how far outside the 1-10 band" the item
            landed.
        components: List of ``(category, points, matched_sample)``
            tuples -- every rule that contributed points.
        borderline: True if the score is in the scorer's configured
            borderline band.
    """

    score: int
    raw_score: int
    components: list[tuple[str, int, str]] = field(default_factory=list)
    borderline: bool = False

    def reasoning(self) -> str:
        """Human-readable one-line explanation of the score."""
        if not self.components:
            return f"score={self.score} (no rules fired, base only)"
        parts = [
            f"{category} ({pts:+d}: {sample!r})"
            for category, pts, sample in self.components
        ]
        return f"score={self.score} | " + " | ".join(parts)


LLMFallback = Callable[[dict, ScoreBreakdown], ScoreBreakdown | None]
"""Shape of an optional LLM rescore callback.

Receives the original item and the rule-based breakdown. Returns a
revised :class:`ScoreBreakdown`, or ``None`` to keep the rule score.
"""


class KeywordScorer:
    """Compose a list of :class:`Rule` into a scoring function.

    Args:
        rules: The list of rules to apply. Evaluated top to bottom,
            but the order doesn't change the final score because all
            matches contribute additively.
        base: Starting score before any rule fires. Default 5 puts
            unmatched items at the midpoint of the 1-10 band.
        score_range: Final clamp range. Default ``(1, 10)``.
        borderline: Score values considered "uncertain". Used by the
            optional ``llm_fallback``. Default ``(5, 6)``.
        llm_fallback: Optional callable invoked only for items whose
            rule-based score lands in the borderline range.
    """

    def __init__(
        self,
        rules: Iterable[Rule],
        base: int = 5,
        score_range: tuple[int, int] = (1, 10),
        borderline: tuple[int, ...] = (5, 6),
        llm_fallback: LLMFallback | None = None,
    ) -> None:
        self.rules = list(rules)
        self.base = int(base)
        self.score_min, self.score_max = score_range
        self.borderline_set = set(int(x) for x in borderline)
        self.llm_fallback = llm_fallback

    @staticmethod
    def _combined_text(item: dict) -> str:
        """Concatenate all string values in the item for ``target='combined'``."""
        parts: list[str] = []
        for v in item.values():
            if isinstance(v, str):
                parts.append(v)
            elif isinstance(v, (list, tuple)):
                parts.extend(str(x) for x in v if x is not None)
        return " ".join(parts)

    def score(self, item: dict) -> ScoreBreakdown:
        """Score a single item.

        Args:
            item: Dict of fields. String keys, values usually strings.

        Returns:
            :class:`ScoreBreakdown`. If an ``llm_fallback`` is set and
            the rule-based score is borderline, the fallback is called
            once and its result is returned instead (when non-None).
        """
        combined = self._combined_text(item)
        running = self.base
        components: list[tuple[str, int, str]] = []

        for rule in self.rules:
            if rule.target == "combined":
                text = combined
            else:
                val = item.get(rule.target) or ""
                text = val if isinstance(val, str) else str(val)
            points, samples = rule.apply(text)
            if points != 0:
                sample = samples[0] if samples else ""
                components.append((rule.category, points, sample))
                running += points

        clamped = max(self.score_min, min(self.score_max, running))
        breakdown = ScoreBreakdown(
            score=clamped,
            raw_score=running,
            components=components,
            borderline=clamped in self.borderline_set,
        )

        if breakdown.borderline and self.llm_fallback is not None:
            try:
                rescored = self.llm_fallback(item, breakdown)
                if rescored is not None:
                    return rescored
            except Exception as e:
                log.warning("llm_fallback raised: %s", e)

        return breakdown

    def score_many(self, items: Iterable[dict]) -> list[ScoreBreakdown]:
        """Convenience: score every item in a collection."""
        return [self.score(x) for x in items]


__all__ = ["KeywordScorer", "Rule", "ScoreBreakdown", "LLMFallback"]
