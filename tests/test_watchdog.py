"""Tests for :mod:`claude_frugal_agents.watchdog`."""

from __future__ import annotations

import threading
import time

import pytest

from claude_frugal_agents.watchdog import AgentWatchdog, WatchdogFired


def test_wall_clock_fires_after_budget() -> None:
    calls: list[str] = []
    with AgentWatchdog(
        kill=lambda reason: calls.append(reason),
        wall_clock_s=0.15,
        stall_s=0,  # disable stall watcher
    ) as wd:
        time.sleep(0.4)
        assert wd.hit is True
        assert wd.reason == "wall_clock"
    assert calls == ["wall_clock"]


def test_stall_fires_when_no_heartbeat() -> None:
    calls: list[str] = []
    with AgentWatchdog(
        kill=lambda reason: calls.append(reason),
        wall_clock_s=0,
        stall_s=0.2,
        poll_interval_s=0.05,
    ) as wd:
        time.sleep(0.5)
        assert wd.hit is True
        assert wd.reason.startswith("stdout_stall_")
    assert len(calls) == 1


def test_heartbeat_prevents_stall_fire() -> None:
    calls: list[str] = []
    with AgentWatchdog(
        kill=lambda reason: calls.append(reason),
        wall_clock_s=0,
        stall_s=0.2,
        poll_interval_s=0.05,
    ) as wd:
        # Heartbeat every 50ms for 0.4s; stall budget is 0.2s, so it should never fire.
        deadline = time.time() + 0.4
        while time.time() < deadline:
            wd.heartbeat()
            time.sleep(0.05)
        assert wd.hit is False
    assert calls == []


def test_fire_is_idempotent() -> None:
    calls: list[str] = []
    wd = AgentWatchdog(
        kill=lambda reason: calls.append(reason),
        wall_clock_s=0,
        stall_s=0,
    )
    with wd:
        wd.fire("manual_1")
        wd.fire("manual_2")  # should be ignored
    assert calls == ["manual_1"]
    assert wd.reason == "manual_1"


def test_raise_if_fired_raises() -> None:
    wd = AgentWatchdog(kill=lambda r: None, wall_clock_s=0, stall_s=0)
    with wd:
        wd.fire("custom_reason")
        with pytest.raises(WatchdogFired) as exc_info:
            wd.raise_if_fired()
        assert "custom_reason" in str(exc_info.value)


def test_kill_callback_exception_does_not_crash() -> None:
    def bad_kill(reason: str) -> None:
        raise RuntimeError("kill-handler boom")

    wd = AgentWatchdog(kill=bad_kill, wall_clock_s=0, stall_s=0)
    with wd:
        wd.fire("anything")  # should not propagate the RuntimeError
    assert wd.hit is True


def test_cleanup_cancels_wall_timer() -> None:
    """After __exit__, the wall-clock timer must not fire spuriously."""
    calls: list[str] = []
    with AgentWatchdog(
        kill=lambda reason: calls.append(reason),
        wall_clock_s=0.5,
        stall_s=0,
    ):
        pass  # exit immediately
    time.sleep(0.7)  # past the original budget
    assert calls == []  # wall timer was cancelled on __exit__
