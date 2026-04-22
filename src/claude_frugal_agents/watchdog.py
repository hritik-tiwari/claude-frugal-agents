"""Three-layer kill switch for subprocess-backed Claude agents.

Long-running Claude agents wedge in at least three distinct ways:

1. **Runaway**: the agent keeps generating tokens past any reasonable
   time budget.
2. **Stall**: the agent stops emitting output (dead MCP server, hung
   browser, network drop) but the subprocess hasn't exited.
3. **Zombie tree**: the agent itself exited but left child processes
   (Chromium, node, Playwright bridge) alive.

:class:`AgentWatchdog` addresses all three. You use it as a context
manager that wraps the region where the agent subprocess is running;
it fires a user-supplied kill callback when any of the three watchdog
layers trips, and the ``.hit`` flag plus ``.reason`` tell you which
one fired for post-mortem logging.

The watchdog does **not** know about :mod:`subprocess`. It just holds
a kill callback. That keeps it trivially testable -- you pass a mock
callable and assert it was invoked.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)


class WatchdogFired(RuntimeError):
    """Raised by :meth:`AgentWatchdog.raise_if_fired` when a watchdog tripped."""


@dataclass
class _State:
    hit: bool = False
    reason: str = ""
    last_activity: float = 0.0


class AgentWatchdog:
    """Context-manager watchdog with wall-clock + stall + explicit kill.

    Args:
        kill: Callable invoked when a watchdog layer fires. Typical use
            is a wrapper around :func:`claude_frugal_agents.browser_helpers.kill_process_tree`.
            The callable is given one positional argument: the reason
            string (e.g. ``"wall_clock"`` or ``"stdout_stall_420s"``).
        wall_clock_s: Maximum total wall time in seconds. 0 disables.
        stall_s: Maximum allowed gap between calls to :meth:`heartbeat`
            in seconds. 0 disables stall detection.
        poll_interval_s: How often the stall thread checks the idle
            timer. Defaults to 5s; lower = more responsive kill, higher
            = lower overhead.

    Attributes:
        hit: True when any watchdog layer has fired.
        reason: String describing which layer fired and when. Empty
            while ``hit`` is False.

    Example:
        >>> kill_calls = []
        >>> with AgentWatchdog(
        ...     kill=lambda reason: kill_calls.append(reason),
        ...     wall_clock_s=30,
        ...     stall_s=10,
        ... ) as wd:
        ...     for line in agent_proc.stdout:
        ...         wd.heartbeat()
        ...         if wd.hit:
        ...             break
        ...         handle(line)
    """

    def __init__(
        self,
        kill: Callable[[str], None],
        wall_clock_s: float = 900.0,
        stall_s: float = 300.0,
        poll_interval_s: float = 5.0,
    ) -> None:
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be > 0")
        self._kill = kill
        self._wall_clock_s = float(wall_clock_s)
        self._stall_s = float(stall_s)
        self._poll_interval_s = float(poll_interval_s)
        self._state = _State(last_activity=time.time())
        self._stop = threading.Event()
        self._wall_timer: threading.Timer | None = None
        self._stall_thread: threading.Thread | None = None
        self._fire_lock = threading.Lock()

    # -- public API ------------------------------------------------------

    @property
    def hit(self) -> bool:
        return self._state.hit

    @property
    def reason(self) -> str:
        return self._state.reason

    def heartbeat(self) -> None:
        """Signal that the agent is still alive.

        Call this on every stdout line or other sign of activity. Resets
        the stall timer; does nothing to the wall-clock timer.
        """
        self._state.last_activity = time.time()

    def fire(self, reason: str) -> None:
        """Mark the watchdog as fired and invoke the kill callback.

        Idempotent: calling this twice invokes ``kill`` only once.
        Safe to call from any thread.
        """
        with self._fire_lock:
            if self._state.hit:
                return
            self._state.hit = True
            self._state.reason = reason
        try:
            self._kill(reason)
        except Exception:
            log.exception("watchdog kill callback raised")

    def raise_if_fired(self) -> None:
        """Raise :class:`WatchdogFired` if any watchdog layer fired.

        Use inside the agent loop when you'd rather unwind via exception
        than check ``.hit`` on every iteration.
        """
        if self._state.hit:
            raise WatchdogFired(self._state.reason)

    # -- context-manager protocol ---------------------------------------

    def __enter__(self) -> "AgentWatchdog":
        self._state.last_activity = time.time()
        if self._wall_clock_s > 0:
            self._wall_timer = threading.Timer(
                self._wall_clock_s,
                self.fire,
                args=("wall_clock",),
            )
            self._wall_timer.daemon = True
            self._wall_timer.start()
        if self._stall_s > 0:
            self._stall_thread = threading.Thread(
                target=self._stall_loop,
                name="agent-watchdog-stall",
                daemon=True,
            )
            self._stall_thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: D401
        self._stop.set()
        if self._wall_timer is not None:
            self._wall_timer.cancel()
        # Don't join with a long timeout -- daemon thread will exit on its own.

    # -- internal stall loop --------------------------------------------

    def _stall_loop(self) -> None:
        while not self._stop.is_set():
            if self._stop.wait(timeout=self._poll_interval_s):
                return
            if self._state.hit:
                return
            idle = time.time() - self._state.last_activity
            if idle >= self._stall_s:
                self.fire(f"stdout_stall_{int(idle)}s")
                return


__all__ = ["AgentWatchdog", "WatchdogFired"]
