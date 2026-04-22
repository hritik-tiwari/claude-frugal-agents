"""Abstract scrape -> parse -> filter -> dedup -> store pipeline.

Every scraper looks the same in outline: fetch a document, extract a
list of candidate records, drop the ones that don't match, deduplicate
against what you already stored, insert the rest. :class:`ScraperBase`
makes that shape explicit so subclasses only write the parts that
actually differ between sources.

Zero LLM calls. This is pure Python for a reason: scraping is
cheap, deterministic, and the LLM's "understanding" of a tabular
document is strictly worse than a regex that you know matches.

Subclass contract::

    class MySiteScraper(ScraperBase):
        source_name = "mysite"

        def fetch(self) -> str:
            return urllib.request.urlopen(self.url).read().decode("utf-8")

        def parse(self, raw: str) -> list[Candidate]:
            # parse `raw` into a list of Candidate objects
            ...

        def filter_fn(self, candidate: Candidate) -> bool:
            # return False to drop
            return True

Call :meth:`scrape` to run the full pipeline.
"""

from __future__ import annotations

import logging
import sqlite3
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    """A scraped record before filtering and insertion.

    Attributes:
        url: The primary URL for the record. Used for deduplication
            after normalization.
        title: Short human-readable title.
        payload: Arbitrary extra fields. Whatever the subclass wants
            to carry through to the storage step.
        source: Short provenance label the base class fills in from
            ``source_name``.
    """

    url: str
    title: str = ""
    payload: dict = field(default_factory=dict)
    source: str = ""


@dataclass
class ScrapeStats:
    """Counters returned by :meth:`ScraperBase.scrape`."""

    fetched_chars: int = 0
    raw_rows: int = 0
    dropped_by_filter: int = 0
    dropped_duplicate: int = 0
    inserted: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"raw={self.raw_rows} "
            f"drop(filter={self.dropped_by_filter}, dup={self.dropped_duplicate}) "
            f"inserted={self.inserted} "
            f"errors={len(self.errors)}"
        )


# ---------------------------------------------------------------------------
# URL normalization (shared)
# ---------------------------------------------------------------------------

_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "source", "gclid", "fbclid",
})


def normalize_url(url: str, strip_suffixes: Iterable[str] = ()) -> str:
    """Return a canonical form of ``url`` suitable for deduplication.

    Steps:

    1. Strip tracking query params (``utm_*``, ``ref``, ``fbclid``, ...).
    2. Lowercase scheme, host, and path.
    3. Strip trailing slash.
    4. Strip any path suffix in ``strip_suffixes`` (e.g. ``/apply``).

    Args:
        url: Input URL. Empty-safe: empty input returns ``""``.
        strip_suffixes: Tail path components to remove if present.
    """
    if not url:
        return ""
    parsed = urllib.parse.urlsplit(url.strip())
    kept = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]
    query = urllib.parse.urlencode(kept)
    path = (parsed.path or "").rstrip("/").lower()
    for suffix in strip_suffixes:
        s = suffix.lower().rstrip("/")
        if s and path.endswith(s):
            path = path[: -len(s)].rstrip("/")
            break
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), path, query, "")
    )


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class ScraperBase(ABC):
    """Base class implementing the scrape-insert pipeline.

    Subclasses must set :attr:`source_name` and implement :meth:`fetch`
    and :meth:`parse`. They may override :meth:`filter_fn`,
    :meth:`normalize_for_dedup`, and :meth:`insert_row`.

    Args:
        conn: Open SQLite connection. The base class stores inserted
            candidates in a table named ``scraped_candidates`` that
            it creates on first use.
        table: Override the default table name.
    """

    #: Short label used for ``Candidate.source`` and logging.
    source_name: str = "scraper"

    def __init__(self, conn: sqlite3.Connection, table: str = "scraped_candidates") -> None:
        self.conn = conn
        self.table = table
        self._ensure_schema()

    # -- subclass hooks -------------------------------------------------

    @abstractmethod
    def fetch(self) -> str:
        """Return the raw document to parse. Network I/O lives here."""

    @abstractmethod
    def parse(self, raw: str) -> list[Candidate]:
        """Convert the raw document into a list of candidates."""

    def filter_fn(self, candidate: Candidate) -> bool:
        """Return False to drop this candidate. Default: keep everything."""
        return True

    def normalize_for_dedup(self, url: str) -> str:
        """Canonicalize ``url`` for the duplicate check.

        Override if the source uses unusual URL suffixes. Default strips
        tracking params and trailing slashes only.
        """
        return normalize_url(url)

    def insert_row(self, candidate: Candidate) -> None:
        """Persist one candidate. Default = insert into the base table."""
        self.conn.execute(
            f"INSERT OR IGNORE INTO {self.table} "
            "(url, normalized_url, title, source, payload_json, discovered_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                candidate.url,
                self.normalize_for_dedup(candidate.url),
                candidate.title,
                candidate.source or self.source_name,
                _json_dumps_safe(candidate.payload),
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    # -- pipeline -------------------------------------------------------

    def scrape(self, dry_run: bool = False) -> ScrapeStats:
        """Run the full fetch -> parse -> filter -> dedup -> store pipeline.

        Args:
            dry_run: If True, run every step except ``insert_row``.
        """
        stats = ScrapeStats()

        try:
            raw = self.fetch()
        except Exception as e:
            stats.errors.append(f"fetch: {e}")
            log.warning("%s fetch failed: %s", self.source_name, e)
            return stats
        stats.fetched_chars = len(raw or "")

        try:
            candidates = self.parse(raw)
        except Exception as e:
            stats.errors.append(f"parse: {e}")
            log.warning("%s parse failed: %s", self.source_name, e)
            return stats

        stats.raw_rows = len(candidates)
        known = self._known_normalized_urls()
        seen: set[str] = set()

        for c in candidates:
            if not c.source:
                c.source = self.source_name
            try:
                if not self.filter_fn(c):
                    stats.dropped_by_filter += 1
                    continue
            except Exception as e:
                stats.errors.append(f"filter {c.url}: {e}")
                stats.dropped_by_filter += 1
                continue

            key = self.normalize_for_dedup(c.url)
            if not key or key in known or key in seen:
                stats.dropped_duplicate += 1
                continue
            seen.add(key)

            if dry_run:
                stats.inserted += 1
                continue

            try:
                self.insert_row(c)
                stats.inserted += 1
            except Exception as e:
                stats.errors.append(f"insert {c.url}: {e}")

        if not dry_run:
            self.conn.commit()
        log.info("%s: %s", self.source_name, stats.summary())
        return stats

    # -- schema helpers -------------------------------------------------

    def _ensure_schema(self) -> None:
        self.conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self.table} ("
            "  url TEXT NOT NULL,"
            "  normalized_url TEXT NOT NULL UNIQUE,"
            "  title TEXT,"
            "  source TEXT,"
            "  payload_json TEXT,"
            "  discovered_at TEXT"
            ")"
        )
        self.conn.commit()

    def _known_normalized_urls(self) -> set[str]:
        return {
            row[0]
            for row in self.conn.execute(
                f"SELECT normalized_url FROM {self.table}"
            ).fetchall()
        }


def _json_dumps_safe(payload: dict) -> str:
    import json
    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps({k: str(v) for k, v in payload.items()}, ensure_ascii=False)


__all__ = ["Candidate", "ScrapeStats", "ScraperBase", "normalize_url"]
