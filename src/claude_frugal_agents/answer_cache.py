"""Capture-and-flag cache for novel questions an agent encountered.

A well-behaved Claude agent running against unfamiliar external input
(web forms, knowledge-base queries, user messages) will encounter
questions that aren't in its profile / knowledge base. The two wrong
responses are:

1. **Hallucinate an answer**. Makes the submission irreversible and
   wrong.
2. **Crash**. Wastes the rest of the work.

The right response is: **skip and flag**. Tell the agent to emit a
machine-readable marker with the question and a draft answer, then stop.
A human reviews the captured entries, approves or edits, and on the next
run the answer is part of the knowledge base.

This module implements the capture side: parse markers out of an agent's
output, persist them, and expose a small review API.

Protocol the agent follows (put this in your prompt)::

    NEW_QUESTION: {"question": "...", "options": [...], "draft_answer": "..."}
    ANSWER_MISMATCH: {"question": "...", "saved_value": "...", "options": [...]}

...followed by the agent stopping. The calling Python code scans the
output with :meth:`AnswerCache.extract_from_text` and stores entries.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)


@dataclass
class CachedEntry:
    """One stored capture.

    Attributes:
        question: The question text as the agent reported it.
        marker: The marker name that produced this entry (e.g.
            ``"NEW_QUESTION"`` or ``"ANSWER_MISMATCH"``).
        data: The full parsed JSON payload from the agent. Includes at
            minimum the ``question``; may also include ``options``,
            ``draft_answer``, ``saved_value``, etc.
        context: Caller-provided metadata. Typical keys: ``url``,
            ``job_id``, ``session_id``.
        timestamp: UTC ISO-8601 when the entry was recorded.
        status: Lifecycle state. One of:
            ``"pending"``, ``"approved"``, ``"rejected"``, ``"merged"``.
        approved_answer: Filled when a human approves with an edit.
    """

    question: str
    marker: str
    data: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)
    timestamp: str = ""
    status: str = "pending"
    approved_answer: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "CachedEntry":
        return cls(
            question=d.get("question", ""),
            marker=d.get("marker", ""),
            data=d.get("data", {}),
            context=d.get("context", {}),
            timestamp=d.get("timestamp", ""),
            status=d.get("status", "pending"),
            approved_answer=d.get("approved_answer"),
        )


class AnswerCache:
    """Persist novel-question captures and manage their review lifecycle.

    Args:
        path: File path where entries are stored as a JSON list. Parent
            directories are created on first write.

    Example:
        >>> cache = AnswerCache(Path("~/.my_agent/answers.json").expanduser())
        >>> added = cache.record_from_text(agent_stdout, marker="NEW_QUESTION")
        >>> for entry in cache.pending(marker="NEW_QUESTION"):
        ...     print(entry.question)
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    # -- extraction -----------------------------------------------------

    @staticmethod
    def extract_from_text(text: str, marker: str) -> list[dict]:
        """Return every ``<marker>: {...}`` JSON payload found in ``text``.

        The parser is balanced-brace aware: it won't be confused by JSON
        values that themselves contain ``}`` characters.

        Args:
            text: Agent output to scan.
            marker: Marker name, e.g. ``"NEW_QUESTION"``.
        """
        pattern = re.compile(rf"{re.escape(marker)}:\s*(\{{.*?\}})(?:\s|$)", re.DOTALL)
        results: list[dict] = []
        for m in pattern.finditer(text):
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
                results.append(json.loads(blob))
            except json.JSONDecodeError as e:
                log.warning(
                    "Failed to parse %s JSON: %s (input: %s)",
                    marker, e, blob[:200],
                )
        return results

    # -- storage --------------------------------------------------------

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception as e:
            log.warning("Failed to read %s: %s", self.path, e)
            return []

    def _save(self, entries: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2, ensure_ascii=False)

    def _is_duplicate(
        self, entries: list[dict], question: str, marker: str
    ) -> bool:
        q = question.strip().lower()
        if not q:
            return False
        for existing in entries:
            if (
                existing.get("marker") == marker
                and existing.get("question", "").strip().lower() == q
            ):
                return True
        return False

    # -- public record / query -----------------------------------------

    def record(
        self,
        marker: str,
        payload: dict,
        context: dict | None = None,
    ) -> bool:
        """Persist a single parsed marker payload.

        Returns True if the entry was new, False if a duplicate (same
        marker + same question text) was already present.
        """
        question = (payload.get("question") or "").strip()
        if not question:
            return False
        entries = self._load()
        if self._is_duplicate(entries, question, marker):
            return False
        entry = CachedEntry(
            question=question,
            marker=marker,
            data=payload,
            context=dict(context or {}),
            timestamp=datetime.now(timezone.utc).isoformat(),
            status="pending",
        )
        entries.append(asdict(entry))
        self._save(entries)
        return True

    def record_from_text(
        self,
        text: str,
        marker: str,
        context: dict | None = None,
    ) -> int:
        """Scan ``text``, persist every new capture. Returns count added."""
        added = 0
        for payload in self.extract_from_text(text, marker):
            if self.record(marker, payload, context=context):
                added += 1
        return added

    def pending(self, marker: str | None = None) -> Iterator[CachedEntry]:
        """Yield every entry currently in ``pending`` status.

        If ``marker`` is set, only yield entries from that marker.
        """
        for d in self._load():
            if d.get("status") != "pending":
                continue
            if marker is not None and d.get("marker") != marker:
                continue
            yield CachedEntry.from_dict(d)

    def all(self) -> list[CachedEntry]:
        """Return every stored entry regardless of status."""
        return [CachedEntry.from_dict(d) for d in self._load()]

    # -- lifecycle operations ------------------------------------------

    def approve(
        self, question: str, answer: str, marker: str | None = None
    ) -> bool:
        """Mark the first matching pending entry as approved.

        Args:
            question: Question text. Case-insensitive exact match.
            answer: The human-approved answer text to attach.
            marker: If set, only match entries from this marker.

        Returns True if an entry was updated.
        """
        return self._mark(question, marker, "approved", approved_answer=answer)

    def reject(self, question: str, marker: str | None = None) -> bool:
        """Mark the first matching pending entry as rejected."""
        return self._mark(question, marker, "rejected")

    def mark_merged(self, question: str, marker: str | None = None) -> bool:
        """Mark an approved entry as having been merged into a downstream store."""
        return self._mark(question, marker, "merged")

    def _mark(
        self,
        question: str,
        marker: str | None,
        new_status: str,
        approved_answer: str | None = None,
    ) -> bool:
        entries = self._load()
        q = question.strip().lower()
        for e in entries:
            if marker is not None and e.get("marker") != marker:
                continue
            if e.get("question", "").strip().lower() == q:
                e["status"] = new_status
                if approved_answer is not None:
                    e["approved_answer"] = approved_answer
                self._save(entries)
                return True
        return False


__all__ = ["AnswerCache", "CachedEntry"]
