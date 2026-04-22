"""End-to-end demo that exercises every pattern in claude-frugal-agents.

What happens when you run this:

1. Launches a local web server serving ``demo_form.html``.
2. Launches Chromium via Playwright and navigates to the form.
3. Spins up an :class:`AgentWatchdog` wrapping the form-fill driver.
4. Fills the form fields step by step. In ``--mode=anthropic`` it calls
   Claude with tool-use to decide each field; in ``--mode=scripted``
   (the default) it uses a hard-coded plan so the demo runs offline
   without any API key.
5. Hits a deliberately-novel screening question ("Why do you want this
   role?") and flags it via :class:`AnswerCache` rather than inventing
   an answer.
6. Emits a ``PRESUBMIT_CHECK: {...}`` line; a :class:`ClaimValidator`
   verifies it against the expected profile before we actually click
   submit.
7. If the validator passes, click submit. If it fails, the watchdog
   fires ``validation_mismatch`` and we stop without submitting.

Requires (for ``--mode=anthropic`` only)::

    pip install claude-frugal-agents[example]
    playwright install chromium

Scripted mode needs only::

    pip install claude-frugal-agents playwright
    playwright install chromium
"""

from __future__ import annotations

import argparse
import http.server
import json
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path

# Make src/ importable when the package isn't pip-installed (handy for
# hacking on the repo without a reinstall).
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from claude_frugal_agents import (
    AgentWatchdog,
    AnswerCache,
    ClaimValidator,
    exact_comparator,
    substring_comparator,
)
from claude_frugal_agents.browser_helpers import FSA_KILL_JS


HERE = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Local file server (so Playwright can navigate to a real URL)
# ---------------------------------------------------------------------------


def _start_server() -> tuple[socketserver.TCPServer, int]:
    """Serve the current directory on a random free port."""

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(HERE), **kwargs)

        def log_message(self, *_):  # silence default request spam
            return

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = socketserver.TCPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


# ---------------------------------------------------------------------------
# The "agent"
# ---------------------------------------------------------------------------


PROFILE = {
    "email": "ada.lovelace@example.com",
    "password": "correct-horse-battery-staple",
    "years": "1-3 years",
    # Intentionally not including "why" -- we want the AnswerCache path
    # to fire on that question.
}


def run_scripted_agent(page, logs: list[str]) -> None:
    """A minimal hard-coded plan so the demo works offline.

    In a real project this is what your Claude agent would do; here we
    simulate the key behaviors (emit a ``NEW_QUESTION`` for the novel
    field, emit a ``PRESUBMIT_CHECK`` right before submit) so the
    frugal-agents patterns get exercised end to end.
    """
    logs.append("agent: page loaded")
    page.fill("#email", PROFILE["email"])
    page.fill("#password", PROFILE["password"])
    page.select_option("#years", PROFILE["years"])

    # Novel question path: this key is not in the profile. The agent
    # emits a marker and LEAVES THE FIELD BLANK so it gets flagged.
    logs.append(
        'NEW_QUESTION: {"question": "Why do you want this role?", '
        '"draft_answer": "skipped pending human review"}'
    )

    # Presubmit check: tell Python what we are ABOUT to submit. Python
    # verifies against the profile. This is the critical safety gate.
    claim = {
        "email": PROFILE["email"],
        "years": PROFILE["years"],
    }
    logs.append("PRESUBMIT_CHECK: " + json.dumps(claim))


def run_anthropic_agent(page, logs: list[str]) -> None:
    """Minimal Claude-driven agent using tool-use to pick values.

    Demonstrates the pattern only; a real production agent would use a
    bigger tool set (Playwright MCP is the obvious choice). Here we
    expose just ``fill`` and ``select`` so the demo stays small.
    """
    try:
        import anthropic
    except ImportError:
        print("Install 'anthropic' to run this mode: pip install 'claude-frugal-agents[example]'")
        raise

    client = anthropic.Anthropic()
    tools = [
        {
            "name": "fill",
            "description": "Fill a form field by CSS selector.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["selector", "value"],
            },
        },
        {
            "name": "select",
            "description": "Choose a <select> option.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "option": {"type": "string"},
                },
                "required": ["selector", "option"],
            },
        },
    ]
    system = (
        "You are filling a form. Profile: " + json.dumps(PROFILE) + ".\n"
        "Fields: #email, #password, #years (select), #why (textarea).\n"
        "If a field is not in the profile, emit a single line starting with "
        "'NEW_QUESTION: ' and STOP. When you have filled all profile fields "
        "successfully, emit a single line 'PRESUBMIT_CHECK: {...}' with the "
        "values you filled, then stop."
    )
    messages: list[dict] = [{"role": "user", "content": "Fill the form."}]
    for _ in range(8):
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            system=system,
            tools=tools,
            messages=messages,
        )
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        text_blocks = [b.text for b in resp.content if b.type == "text"]
        for t in text_blocks:
            logs.append(t)
        if not tool_uses:
            break
        tool_results = []
        for tu in tool_uses:
            if tu.name == "fill":
                page.fill(tu.input["selector"], tu.input["value"])
                tool_results.append({"tool_use_id": tu.id, "type": "tool_result", "content": "ok"})
            elif tu.name == "select":
                page.select_option(tu.input["selector"], tu.input["option"])
                tool_results.append({"tool_use_id": tu.id, "type": "tool_result", "content": "ok"})
        messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user", "content": tool_results})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["scripted", "anthropic"],
        default="scripted",
        help="scripted = offline hardcoded plan; anthropic = call the API.",
    )
    parser.add_argument("--wall-clock-s", type=float, default=60.0)
    parser.add_argument("--stall-s", type=float, default=20.0)
    parser.add_argument("--cache", default=str(HERE / "novel_questions.json"))
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Install playwright: pip install 'claude-frugal-agents[example]' "
              "&& playwright install chromium")
        return 1

    cache = AnswerCache(args.cache)
    validator = ClaimValidator(
        expected={"email": PROFILE["email"], "years": PROFILE["years"]},
        comparators={"email": exact_comparator, "years": substring_comparator},
    )

    logs: list[str] = []
    agent_done = threading.Event()

    def kill(reason: str) -> None:
        logs.append(f"!! watchdog fired: {reason}")
        agent_done.set()

    server, port = _start_server()
    url = f"http://127.0.0.1:{port}/demo_form.html"
    print(f"demo form at {url}")

    submitted_ok = False

    try:
        with sync_playwright() as pw, AgentWatchdog(
            kill=kill, wall_clock_s=args.wall_clock_s, stall_s=args.stall_s
        ) as wd:
            browser = pw.chromium.launch(headless=args.headless)
            context = browser.new_context()
            # FSA guard: belt-and-braces; the demo form never triggers it
            # but showing the pattern is the point.
            context.add_init_script(FSA_KILL_JS)
            page = context.new_page()
            page.goto(url)
            wd.heartbeat()

            print("=== running agent ===")
            if args.mode == "scripted":
                run_scripted_agent(page, logs)
            else:
                run_anthropic_agent(page, logs)
            wd.heartbeat()

            agent_output = "\n".join(logs)
            print(agent_output)
            print("=== /agent ===")

            # Capture any novel questions before deciding to submit.
            added = cache.record_from_text(
                agent_output,
                marker="NEW_QUESTION",
                context={"url": url},
            )
            if added:
                print(f"flagged {added} novel question(s) for review; will NOT submit")
                pending = list(cache.pending(marker="NEW_QUESTION"))
                for p in pending:
                    print(f"  pending: {p.question}")
            else:
                # Only validate + submit if we didn't flag novel questions.
                result = validator.validate(agent_output)
                if result is None:
                    print("no PRESUBMIT_CHECK emitted by agent -- refusing to submit")
                elif not result.ok:
                    print(f"presubmit FAILED: {result.reason}")
                    for m in result.mismatches:
                        print(f"  {m}")
                    wd.fire("validation_mismatch")
                else:
                    print("presubmit OK -- clicking submit")
                    page.click("#submit-btn")
                    page.wait_for_selector("#result:not([style*='display: none'])", timeout=5000)
                    submitted = page.evaluate("window.__LAST_SUBMISSION__")
                    print(f"form accepted: {submitted}")
                    submitted_ok = True

            browser.close()
    finally:
        server.shutdown()
        server.server_close()

    print("\n=== summary ===")
    print(f"submitted: {submitted_ok}")
    print(f"novel questions pending review: {len(list(cache.pending()))}")
    print(f"cache file: {args.cache}")
    return 0 if submitted_ok or not agent_done.is_set() else 2


if __name__ == "__main__":
    raise SystemExit(main())
