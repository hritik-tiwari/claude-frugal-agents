"""Tests for :mod:`claude_frugal_agents.scraper_base`."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from claude_frugal_agents.scraper_base import (
    Candidate,
    ScraperBase,
    normalize_url,
)


class _HtmlScraper(ScraperBase):
    source_name = "local_html"

    def __init__(self, conn: sqlite3.Connection, html_path: Path) -> None:
        super().__init__(conn)
        self._path = html_path

    def fetch(self) -> str:
        return self._path.read_text(encoding="utf-8")

    def parse(self, raw: str) -> list[Candidate]:
        import re
        results: list[Candidate] = []
        for m in re.finditer(r'<a href="([^"]+)">([^<]+)</a>', raw):
            results.append(Candidate(url=m.group(1), title=m.group(2)))
        return results


def test_normalize_url_strips_tracking_and_trailing_slash() -> None:
    out = normalize_url(
        "HTTPS://Example.COM/jobs/1/?utm_source=x&keep=me&fbclid=abc",
    )
    assert "utm_source" not in out
    assert "fbclid" not in out
    assert "keep=me" in out
    assert out.startswith("https://example.com/jobs/1")


def test_normalize_url_strip_suffixes() -> None:
    out = normalize_url(
        "https://example.com/jobs/1/apply/", strip_suffixes=["/apply"],
    )
    assert out == "https://example.com/jobs/1"


def test_scraper_pipeline_inserts_unique_rows(tmp_path: Path) -> None:
    html = tmp_path / "page.html"
    html.write_text(
        '<a href="https://example.com/a?utm_source=x">Job A</a>'
        '<a href="https://example.com/b">Job B</a>'
        '<a href="https://example.com/a/">Job A Duplicate</a>',
        encoding="utf-8",
    )
    conn = sqlite3.connect(":memory:")
    scraper = _HtmlScraper(conn, html)
    stats = scraper.scrape()
    assert stats.raw_rows == 3
    assert stats.dropped_duplicate == 1
    assert stats.inserted == 2


def test_scraper_filter_fn_drops_rows(tmp_path: Path) -> None:
    html = tmp_path / "page.html"
    html.write_text(
        '<a href="https://example.com/a">Analyst Intern</a>'
        '<a href="https://example.com/b">Senior Engineer</a>',
        encoding="utf-8",
    )
    conn = sqlite3.connect(":memory:")

    class OnlyInterns(_HtmlScraper):
        def filter_fn(self, candidate: Candidate) -> bool:
            return "intern" in candidate.title.lower()

    scraper = OnlyInterns(conn, html)
    stats = scraper.scrape()
    assert stats.inserted == 1
    assert stats.dropped_by_filter == 1


def test_scraper_dry_run_leaves_db_empty(tmp_path: Path) -> None:
    html = tmp_path / "page.html"
    html.write_text('<a href="https://example.com/a">Job A</a>', encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    scraper = _HtmlScraper(conn, html)
    stats = scraper.scrape(dry_run=True)
    assert stats.inserted == 1
    rows = conn.execute("SELECT COUNT(*) FROM scraped_candidates").fetchone()
    assert rows[0] == 0


def test_scraper_second_run_dedups_against_db(tmp_path: Path) -> None:
    html = tmp_path / "page.html"
    html.write_text('<a href="https://example.com/a">Job A</a>', encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    s1 = _HtmlScraper(conn, html)
    s1.scrape()
    s2 = _HtmlScraper(conn, html)
    stats = s2.scrape()
    assert stats.inserted == 0
    assert stats.dropped_duplicate == 1
