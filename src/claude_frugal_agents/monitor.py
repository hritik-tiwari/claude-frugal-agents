"""Scaffolding for a live Rich dashboard for an agent pipeline.

When you have a Claude agent doing long work, you want to see what it's
doing right now without it having to push structured events to a bus.
The pattern that works:

1. Agent writes to a SQLite DB (or log file) as normal side effects of
   its work.
2. A separate Python process reads from the same DB and tails the log.
3. Rich's :class:`rich.live.Live` re-renders on a timer.

This module gives you the third piece with pluggable data sources so
it's not tied to any specific schema. Point it at your own "how many
jobs pending?" and "what's the latest log line?" callbacks and run the
loop.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


@dataclass
class DashboardConfig:
    """Inputs required to render one frame.

    Attributes:
        status_counts: Callable returning a dict of
            ``status_name -> count``. Rendered as a small table at the top.
        active_rows: Callable returning an iterable of dicts for the
            currently-active workers / tasks. Each dict is rendered as
            a row; keys become column headers.
        log_tail: Callable returning the last N lines of the worker log.
            Defaults to returning nothing.
        title: Title shown on the main panel.
    """

    status_counts: Callable[[], dict[str, int]]
    active_rows: Callable[[], Iterable[dict]]
    log_tail: Callable[[], list[str]] = lambda: []  # noqa: E731
    title: str = "Agent pipeline"


def tail_file(path: str | Path, lines: int = 20) -> list[str]:
    """Return the last ``lines`` lines of ``path``.

    Small and dependency-free: loads the whole file and slices. Fine for
    the kind of log files a Claude agent writes (~MB at worst). For
    gigabyte logs, replace with a seek-and-scan implementation.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        return [line.rstrip("\n") for line in all_lines[-lines:]]
    except FileNotFoundError:
        return []


def _status_panel(counts: dict[str, int]) -> Panel:
    table = Table.grid(padding=(0, 2))
    table.add_column(justify="left")
    table.add_column(justify="right")
    for key, value in counts.items():
        table.add_row(Text(key, style="bold"), Text(str(value)))
    return Panel(table, title="Status", border_style="cyan")


def _active_panel(rows: Iterable[dict]) -> Panel:
    rows = list(rows)
    if not rows:
        return Panel(Text("(no active workers)", style="dim"),
                     title="Active", border_style="yellow")
    table = Table(expand=True, show_lines=False)
    for key in rows[0].keys():
        table.add_column(key)
    for r in rows:
        table.add_row(*[str(r.get(k, "")) for k in rows[0].keys()])
    return Panel(table, title="Active", border_style="yellow")


def _log_panel(lines: list[str]) -> Panel:
    if not lines:
        return Panel(Text("(log empty)", style="dim"),
                     title="Log tail", border_style="magenta")
    # Keep a reasonable visible window
    visible = lines[-15:]
    text = Text("\n".join(visible))
    return Panel(text, title="Log tail", border_style="magenta")


def build_layout(config: DashboardConfig) -> Layout:
    """Construct and return a fresh :class:`rich.layout.Layout` for one frame."""
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="log", size=17),
    )
    layout["header"].update(Panel(Text(config.title, style="bold"), border_style="green"))
    layout["body"].split_row(
        Layout(_status_panel(config.status_counts())),
        Layout(_active_panel(config.active_rows())),
    )
    layout["log"].update(_log_panel(config.log_tail()))
    return layout


def run_dashboard(
    config: DashboardConfig,
    refresh_per_second: float = 2.0,
    stop_after_s: float | None = None,
    console: Console | None = None,
) -> None:
    """Run the live-refresh loop until the user hits Ctrl-C.

    Args:
        config: Dashboard inputs.
        refresh_per_second: Redraws per second.
        stop_after_s: If set, exit the loop after this many seconds. Useful
            for smoke tests.
        console: Optional :class:`rich.console.Console`.
    """
    console = console or Console()
    start = time.time()
    with Live(build_layout(config), console=console, refresh_per_second=refresh_per_second) as live:
        try:
            while True:
                time.sleep(1 / max(refresh_per_second, 0.1))
                live.update(build_layout(config))
                if stop_after_s is not None and time.time() - start > stop_after_s:
                    return
        except KeyboardInterrupt:
            return


__all__ = ["DashboardConfig", "build_layout", "run_dashboard", "tail_file"]
