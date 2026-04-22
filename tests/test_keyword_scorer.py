"""Tests for :mod:`claude_frugal_agents.keyword_scorer`."""

from __future__ import annotations

from claude_frugal_agents.keyword_scorer import KeywordScorer, Rule, ScoreBreakdown


def _rules():
    return [
        Rule(pattern=r"\b(senior|staff|principal)\b",
             weight=-3, category="seniority", target="title"),
        Rule(pattern=r"\b(intern|new\s*grad)\b",
             weight=+2, category="seniority", target="title"),
        Rule(pattern=r"\b(python|sql)\b",
             weight=+1, category="skill", target="description", cap=2),
        Rule(pattern=r"\b(remote|anywhere)\b",
             weight=+1, category="location", target="location"),
    ]


def test_base_score_with_no_rules_fires() -> None:
    scorer = KeywordScorer(rules=_rules(), base=5)
    result = scorer.score({"title": "Brand Strategist", "description": "", "location": ""})
    assert result.score == 5
    assert result.components == []


def test_positive_rules_lift_score() -> None:
    scorer = KeywordScorer(rules=_rules(), base=5)
    result = scorer.score({
        "title": "Data Analyst Intern",
        "description": "Python and SQL required",
        "location": "Remote",
    })
    assert result.score > 5
    # intern +2, python +1, sql +1, remote +1 -> base 5 + 5 = 10
    assert result.score == 10
    categories = [c[0] for c in result.components]
    assert "seniority" in categories
    assert "skill" in categories
    assert "location" in categories


def test_negative_rules_drop_score() -> None:
    scorer = KeywordScorer(rules=_rules(), base=5)
    result = scorer.score({"title": "Senior Staff Engineer",
                           "description": "", "location": ""})
    # "senior" matches the pattern, "staff" also matches. cap=1 default
    # means the rule fires once per rule. Only the seniority rule matches.
    # The regex has an alternation so findall returns matches separately
    # but the rule caps them to 1. Final: base 5 + (-3) = 2.
    assert result.score == 2


def test_score_is_clamped_to_range() -> None:
    scorer = KeywordScorer(rules=_rules(), base=5, score_range=(1, 10))
    # Pile on positives
    result = scorer.score({
        "title": "Intern New Grad",
        "description": "Python SQL Python SQL Python",
        "location": "Remote Anywhere",
    })
    assert 1 <= result.score <= 10


def test_borderline_triggers_llm_fallback() -> None:
    calls: list[dict] = []

    def fallback(item: dict, rule_result: ScoreBreakdown) -> ScoreBreakdown:
        calls.append({"item": item, "rule_score": rule_result.score})
        return ScoreBreakdown(score=9, raw_score=9, components=[("llm", 4, "boosted")])

    scorer = KeywordScorer(
        rules=_rules(),
        base=5,
        borderline=(5,),
        llm_fallback=fallback,
    )
    result = scorer.score({"title": "Unclear role", "description": "", "location": ""})
    assert len(calls) == 1
    assert result.score == 9  # fallback's score used
    assert ("llm", 4, "boosted") in result.components


def test_llm_fallback_not_called_when_score_not_borderline() -> None:
    calls: list[dict] = []

    def fallback(item, rule_result):
        calls.append(item)
        return None

    scorer = KeywordScorer(
        rules=_rules(),
        base=5,
        borderline=(5,),
        llm_fallback=fallback,
    )
    # Positive rules push score away from 5 -> fallback not invoked
    scorer.score({"title": "Data Analyst Intern", "description": "", "location": "Remote"})
    assert calls == []


def test_rule_cap_limits_contribution() -> None:
    rule = Rule(pattern=r"\bpython\b", weight=1, category="skill", target="description", cap=2)
    text = "python python python python python"
    points, samples = rule.apply(text)
    assert points == 2
    # samples is a first-3 display list; cap only bounds the points.
    assert samples[:2] == ["python", "python"]


def test_score_many_returns_same_length_list() -> None:
    scorer = KeywordScorer(rules=_rules(), base=5)
    results = scorer.score_many([
        {"title": "Intern", "description": "", "location": ""},
        {"title": "Senior", "description": "", "location": ""},
    ])
    assert len(results) == 2
    assert results[0].score > results[1].score
