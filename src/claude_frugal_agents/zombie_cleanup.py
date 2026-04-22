"""Standalone orphan-process finder and killer.

When your Claude-agent pipeline dies unexpectedly (power loss, OOM,
Ctrl-C during subprocess spawn) it often leaves behind:

- Browser processes that were launched with a remote-debugging port
- Node processes hosting an MCP server
- Whatever other subprocess trees the agent had open

These eat RAM and sometimes keep CDP ports bound, preventing the next
run from starting. This module finds and kills them.

Run as:

.. code-block:: shell

    python -m claude_frugal_agents.zombie_cleanup --dry-run
    python -m claude_frugal_agents.zombie_cleanup
    python -m claude_frugal_agents.zombie_cleanup --pattern chrome --parent python
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass

from claude_frugal_agents.browser_helpers import kill_process_tree

log = logging.getLogger(__name__)


@dataclass
class ProcessInfo:
    """Small view of a single orphaned process."""

    pid: int
    name: str
    cmdline: str
    parent_name: str


def find_orphans(
    process_name_pattern: str,
    parent_must_be: list[str] | None = None,
    cmdline_contains: list[str] | None = None,
) -> list[ProcessInfo]:
    """Return processes matching ``process_name_pattern`` that are orphans.

    An "orphan" here is any process whose parent's name is not in
    ``parent_must_be`` -- i.e. the Python driver that was supposed to
    own it has already exited and the process was reparented to PID 1
    (Unix) or an unrelated process (Windows).

    Args:
        process_name_pattern: Case-insensitive substring of the
            process's executable name to match.
        parent_must_be: List of parent executable names that count as
            "still owned" (not orphaned). Case-insensitive substring
            match. If a process's parent matches any of these, it is
            NOT reported. Default: any parent is accepted (no process
            reported as orphan), which means you should always pass
            something like ``["python", "pythonw"]``.
        cmdline_contains: If set, also require the process's full
            command line to contain all of these substrings (case
            insensitive). Useful for narrowing to e.g. only Chromes
            launched with a ``--remote-debugging-port`` flag.

    Returns:
        List of :class:`ProcessInfo`. Empty if nothing matched or
        psutil is not installed.
    """
    try:
        import psutil
    except ImportError:
        log.warning("psutil not installed; cannot enumerate processes")
        return []

    name_pat = process_name_pattern.lower()
    parents_ok = [p.lower() for p in (parent_must_be or [])]
    cmd_required = [c.lower() for c in (cmdline_contains or [])]

    orphans: list[ProcessInfo] = []
    for p in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
        try:
            name = (p.info.get("name") or "").lower()
            if name_pat not in name:
                continue
            cmd = " ".join(p.info.get("cmdline") or [])
            cmd_lower = cmd.lower()
            if cmd_required and not all(c in cmd_lower for c in cmd_required):
                continue

            parent_name = ""
            try:
                parent = psutil.Process(p.info["ppid"])
                parent_name = (parent.name() or "").lower()
            except psutil.NoSuchProcess:
                parent_name = "<dead>"

            if parents_ok and any(allowed in parent_name for allowed in parents_ok):
                continue

            orphans.append(
                ProcessInfo(
                    pid=p.info["pid"],
                    name=name,
                    cmdline=cmd[:240],
                    parent_name=parent_name,
                )
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return orphans


def kill_orphans(orphans: list[ProcessInfo], dry_run: bool = False) -> int:
    """Kill every process in ``orphans`` (and its tree).

    Args:
        orphans: Processes to kill, typically from :func:`find_orphans`.
        dry_run: If True, log what would be killed but don't actually kill.

    Returns:
        Number of processes that were killed (or would have been, if dry_run).
    """
    count = 0
    for orph in orphans:
        if dry_run:
            print(f"[dry-run] would kill {orph.pid} {orph.name} -- {orph.cmdline[:80]}")
        else:
            print(f"killing {orph.pid} {orph.name} -- {orph.cmdline[:80]}")
            try:
                kill_process_tree(orph.pid)
            except Exception as e:
                log.warning("kill_process_tree(%d) raised: %s", orph.pid, e)
                continue
        count += 1
    return count


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns process exit code."""
    parser = argparse.ArgumentParser(
        prog="claude-frugal-agents.zombie_cleanup",
        description="Find and kill orphan subprocesses left by a dead agent run.",
    )
    parser.add_argument(
        "--pattern",
        default="chrome",
        help="Substring of the target process's name. Default: chrome",
    )
    parser.add_argument(
        "--parent",
        action="append",
        default=[],
        help=(
            "Parent executable names that mean 'not orphaned'. May be "
            "given multiple times. Example: --parent python --parent pythonw"
        ),
    )
    parser.add_argument(
        "--cmdline-contains",
        action="append",
        default=[],
        help="Require the process command line to contain this substring.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be killed without killing anything.",
    )
    args = parser.parse_args(argv)

    if not args.parent:
        print(
            "warning: no --parent passed; without it every matching process "
            "will be treated as orphan. Pass e.g. --parent python to narrow.",
            file=sys.stderr,
        )

    orphans = find_orphans(
        process_name_pattern=args.pattern,
        parent_must_be=args.parent or None,
        cmdline_contains=args.cmdline_contains or None,
    )
    print(f"found {len(orphans)} orphan(s) matching pattern={args.pattern!r}")
    killed = kill_orphans(orphans, dry_run=args.dry_run)
    print(f"{'would kill' if args.dry_run else 'killed'} {killed} process(es)")
    return 0


__all__ = ["ProcessInfo", "find_orphans", "kill_orphans", "main"]


if __name__ == "__main__":
    sys.exit(main())
