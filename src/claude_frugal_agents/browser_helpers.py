"""Browser-agent helpers: process-tree kill, port-based cleanup, FSA guard.

These are the small bits of plumbing every Playwright / Selenium / CDP-based
Claude-agent project ends up re-implementing. Pulling them out here lets
you just import them.

Nothing here is Claude-specific; it all works for any subprocess-driven
browser automation.
"""

from __future__ import annotations

import json
import logging
import platform
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FSA_KILL_JS
# ---------------------------------------------------------------------------

FSA_KILL_JS: str = r"""
// claude-frugal-agents: File System Access API kill switch.
//
// Chromium-based browsers expose showOpenFilePicker / showSaveFilePicker
// to sites; when a form triggers these, the OS file-picker dialog
// appears OUTSIDE the DOM, where Playwright / CDP cannot click it, and
// the automation session hangs forever.
//
// Run this snippet on every new tab before the agent starts interacting.
// It replaces both entry points with no-ops that throw a benign error
// the page can catch. The override persists through SPA navigations
// within the same tab; re-run it after any full reload.
(() => {
  const noop = (name) => function () {
    throw new Error(name + ' blocked by claude-frugal-agents FSA guard');
  };
  try {
    Object.defineProperty(window, 'showOpenFilePicker', {
      configurable: true, writable: true, value: noop('showOpenFilePicker'),
    });
    Object.defineProperty(window, 'showSaveFilePicker', {
      configurable: true, writable: true, value: noop('showSaveFilePicker'),
    });
    Object.defineProperty(window, 'showDirectoryPicker', {
      configurable: true, writable: true, value: noop('showDirectoryPicker'),
    });
  } catch (e) {
    return 'FSA_KILL_FAILED ' + e.message;
  }
  return 'FSA_KILLED open=' + typeof window.showOpenFilePicker
       + ' save=' + typeof window.showSaveFilePicker;
})();
"""
"""JavaScript snippet that disables the File System Access API on the
current page.

The agent's Playwright wrapper should :func:`eval` this on every fresh
tab before starting to click. The return value starts with
``FSA_KILLED`` on success; anything else means the override did not
take and the agent should abort rather than risk a native OS dialog
that hangs the session.
"""


# ---------------------------------------------------------------------------
# Process-tree kill
# ---------------------------------------------------------------------------


def kill_process_tree(pid: int) -> None:
    """Kill ``pid`` and every descendant process.

    Uses a three-layer strategy:

    1. **psutil walk** -- most reliable for deep Chromium trees. Kills
       each descendant by PID directly rather than relying on the OS's
       tree-kill, which sometimes misses processes whose parent PID has
       already been reaped.
    2. **Native OS tree-kill** -- ``taskkill /F /T`` on Windows,
       ``killpg`` on Unix. Used as a fallback when psutil is not
       installed or fails.
    3. **Direct PID kill** -- last-resort single-PID kill.

    Each layer is wrapped in a short timeout so a hung OS tool cannot
    block the caller forever.

    Args:
        pid: Parent PID whose entire process tree should be killed.
    """
    import signal as _signal

    # Layer 1: psutil recursive kill.
    try:
        import psutil
        try:
            parent = psutil.Process(pid)
            children = parent.children(recursive=True)
        except psutil.NoSuchProcess:
            return
        for child in children:
            try:
                child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        try:
            parent.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        try:
            psutil.wait_procs([parent] + children, timeout=3)
        except Exception:
            pass
        try:
            psutil.Process(pid)  # still alive?
        except psutil.NoSuchProcess:
            return
    except ImportError:
        pass  # fall through to OS tools
    except Exception as e:
        log.debug("psutil kill layer failed for PID %d: %s", pid, e)

    # Layer 2: OS-native tree-kill.
    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        else:
            import os
            try:
                os.killpg(os.getpgid(pid), _signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(pid, _signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
    except subprocess.TimeoutExpired:
        log.warning("taskkill timed out on PID %d", pid)
    except Exception:
        log.debug("OS-native kill failed for PID %d", pid, exc_info=True)


# ---------------------------------------------------------------------------
# Port-based cleanup
# ---------------------------------------------------------------------------


def kill_on_port(port: int) -> None:
    """Find any process listening on ``port`` and kill its tree.

    Uses ``netstat -ano`` on Windows, ``lsof -ti`` on macOS/Linux.
    Silently no-ops if neither tool is available.

    Typical use: call at the start of a worker to clean up a zombie
    browser from a previous crashed run that's still holding the CDP
    port the new browser wants to use.
    """
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    if pid.isdigit():
                        kill_process_tree(int(pid))
        else:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=10,
            )
            for pid_str in result.stdout.strip().splitlines():
                pid_str = pid_str.strip()
                if pid_str.isdigit():
                    kill_process_tree(int(pid_str))
    except FileNotFoundError:
        log.debug("port-kill tool not found (netstat/lsof) for port %d", port)
    except Exception:
        log.debug("port kill failed for %d", port, exc_info=True)


# ---------------------------------------------------------------------------
# Chrome restore-bubble suppression
# ---------------------------------------------------------------------------


def suppress_chrome_restore_nag(profile_dir: str | Path) -> bool:
    """Edit the Chrome Preferences file to suppress the restore-tabs bubble.

    When you kill Chrome with ``taskkill`` or SIGKILL, it writes
    ``exit_type=Crashed`` to its preferences. On the next launch it
    shows a 'Restore pages?' bubble that sits on top of the page and
    blocks automation clicks.

    This function patches the exit_type back to 'Normal' and sets the
    startup preference to 'open blank page' so the agent always lands
    on a clean tab.

    Args:
        profile_dir: The ``--user-data-dir`` root. The function looks
            for ``<profile_dir>/Default/Preferences``.

    Returns:
        True if the Preferences file was found and patched. False if
        it doesn't exist yet (brand-new profile) or couldn't be read.
    """
    prefs_file = Path(profile_dir) / "Default" / "Preferences"
    if not prefs_file.exists():
        return False

    try:
        prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
        prefs.setdefault("profile", {})["exit_type"] = "Normal"
        session = prefs.setdefault("session", {})
        session["restore_on_startup"] = 4  # 4 = open blank page
        session.pop("startup_urls", None)
        prefs_file.write_text(json.dumps(prefs), encoding="utf-8")
        return True
    except Exception:
        log.debug("Could not patch Chrome preferences at %s", prefs_file, exc_info=True)
        return False


__all__ = [
    "FSA_KILL_JS",
    "kill_process_tree",
    "kill_on_port",
    "suppress_chrome_restore_nag",
]
