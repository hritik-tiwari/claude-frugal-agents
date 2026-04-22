# form_filler_demo

A self-contained demo exercising every pattern in
[claude-frugal-agents](../../README.md) against a local HTML form.
No external site is touched.

## What it shows

| Pattern             | Where it fires in the demo                                   |
|---------------------|--------------------------------------------------------------|
| `AgentWatchdog`     | Wraps the entire agent run. Kills if >60s or stalled >20s.   |
| `FSA_KILL_JS`       | Injected into every page context before the agent starts.    |
| `AnswerCache`       | Captures the "Why do you want this role?" question as novel. |
| `ClaimValidator`    | Verifies `PRESUBMIT_CHECK` matches profile before submit.    |
| Browser teardown    | `browser.close()` in `finally` + server shutdown.            |

The demo intentionally **does not submit** on its first run -- the
novel-question handler flags the "Why do you want this role?" field as
not-in-the-profile and bails out rather than inventing an answer.
That's the whole point: an agent that doesn't guess.

## Run it

### Zero-API-key mode (default)

```bash
pip install claude-frugal-agents playwright
playwright install chromium
python examples/form_filler_demo/run_demo.py
```

This uses a hardcoded plan to drive the form, so it works offline. You
still get the full pattern exercise: watchdog, answer-cache, validator.

### Real-Claude mode

```bash
pip install 'claude-frugal-agents[example]'
playwright install chromium
export ANTHROPIC_API_KEY=sk-...
python examples/form_filler_demo/run_demo.py --mode anthropic
```

Now a real Claude call with tool-use decides what to fill. Same
patterns wrap it. Costs ~1 cent per run.

## Expected output

Roughly:

```
demo form at http://127.0.0.1:NNNNN/demo_form.html
=== running agent ===
agent: page loaded
NEW_QUESTION: {"question": "Why do you want this role?", "draft_answer": "skipped pending human review"}
PRESUBMIT_CHECK: {"email": "ada.lovelace@example.com", "years": "1-3 years"}
=== /agent ===
flagged 1 novel question(s) for review; will NOT submit
  pending: Why do you want this role?

=== summary ===
submitted: False
novel questions pending review: 1
cache file: .../examples/form_filler_demo/novel_questions.json
```

After approving the question via the `AnswerCache` API (or editing the
JSON file directly), re-run the demo and it will proceed through
validator + submit.

## Approve the pending question

```python
from claude_frugal_agents import AnswerCache
c = AnswerCache("examples/form_filler_demo/novel_questions.json")
c.approve("Why do you want this role?", "I want to learn and contribute.")
```

Then re-run. The scripted agent does not yet consume approved answers
(this is a demo, not a framework), but the `AnswerCache` holds the
canonical record and you can wire it into your own agent's profile.
