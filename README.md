# claude-frugal-agents

> Patterns I used to keep my Claude agents cheap, fast, and reliable.
>
> Extracted from a browser-automation pipeline I built and ran for 4 months.
> 52 real end-to-end runs, a lot of bugs I had to design my way out of.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Status: beta](https://img.shields.io/badge/status-beta-orange)](#)

## Why I built this

When I started writing a Claude agent to do a boring repetitive browser task for me, I did what most tutorials tell you to do: send the page to the model, let it reason about everything, trust the output.

That pipeline ran. It also cost me about $10 per task, took 8 to 15 minutes, and roughly one time in three it would confidently report success when the submit button had never actually been clicked.

Over the next four months I kept iterating while using it for real work. By the end I'd moved 14 separate sub-tasks out of the LLM and into plain Python. The per-task cost dropped from ~$10 to under $2, speed improved about 5x, and I stopped seeing false-positive completions.

This repo is the reusable patterns I pulled out of that project. They apply to any Claude agent doing browser work, not just mine.

| Metric                   | LLM-for-everything | After the cleanup |
|--------------------------|--------------------|-------------------|
| Tokens per pipeline run  | ~250K              | ~30K              |
| Cost per run             | ~$8                | ~$1               |
| Wall-clock speed         | 1x                 | ~5x               |
| Determinism              | LLM-variable       | Reproducible      |

For the full story, see [CASE_STUDY.md](CASE_STUDY.md).

## The 14 tasks I moved out of the LLM

| #  | Task                                         | Before        | After                                   |
|----|----------------------------------------------|---------------|-----------------------------------------|
| 1  | Parse HTML job-board tables                  | LLM reasoning | `ScraperBase` + `html.parser` / regex   |
| 2  | Rank items by relevance                      | LLM scoring   | `KeywordScorer` with weighted rules     |
| 3  | Escalate only uncertain rankings to the LLM  | (n/a)         | `KeywordScorer.llm_fallback` hook       |
| 4  | Deduplicate URLs against prior runs          | LLM compare   | `normalize_url` + SQLite UNIQUE index   |
| 5  | Validate form fields match profile           | LLM re-read   | `ClaimValidator` + `PRESUBMIT_CHECK`    |
| 6  | Polarity check ("yes authorized" vs "no")    | LLM judgment  | `polarity_comparator`                   |
| 7  | Detect agent stalled / hung                  | (paid to wait)| `AgentWatchdog` stall thread            |
| 8  | Cap wall-clock per agent                     | (none)        | `AgentWatchdog` wall-clock timer        |
| 9  | Kill browser + node tree on failure          | manual `ps`   | `kill_process_tree` (3-layer)           |
| 10 | Reclaim zombie CDP ports                     | manual        | `kill_on_port`                          |
| 11 | Reap orphan subprocesses after a crash       | OS tools      | `zombie_cleanup.find_orphans`           |
| 12 | Flag novel questions instead of hallucinate  | LLM invents   | `AnswerCache` + `NEW_QUESTION` marker   |
| 13 | Block the OS file-picker dialog              | agent hangs   | `FSA_KILL_JS` browser preamble          |
| 14 | Live pipeline monitoring                     | tail log      | `monitor.run_dashboard` (Rich)          |

Each row is something I originally wrote Claude-prompt code for, then realized was cheaper, faster, and more reliable as plain Python.

## The patterns

### ClaimValidator

The fix for "the agent reported success but didn't actually submit."

Tell the agent to emit a `PRESUBMIT_CHECK: {...}` JSON block right before any irreversible action. Your validator confirms every claimed value against expected. Mismatch, kill the agent mid-action, before the bad submit.

```python
from claude_frugal_agents import (
    ClaimValidator, exact_comparator, polarity_comparator,
)

validator = ClaimValidator(
    expected={
        "email": "alice@example.com",
        "work_auth": "yes",
    },
    comparators={
        "email": exact_comparator,
        "work_auth": polarity_comparator(
            positive_phrases=("authorized to work", "us citizen"),
            negative_phrases=("not authorized",),
        ),
    },
)

result = validator.validate(agent_stdout_chunk)
if result and not result.ok:
    kill_subprocess_tree(agent_pid)
```

### AgentWatchdog (3-layer kill switch)

Subprocess agents hang. This class wraps any long-running agent with:

1. **Wall-clock deadline** (default 15 min)
2. **Stdout-stall detector** (kill if silent for N minutes)
3. **User-supplied kill callback** that you can plug into `kill_process_tree` or any other teardown

```python
from claude_frugal_agents import AgentWatchdog
from claude_frugal_agents.browser_helpers import kill_process_tree

with AgentWatchdog(
    kill=lambda reason: kill_process_tree(proc.pid),
    wall_clock_s=900,
    stall_s=300,
) as wd:
    for line in proc.stdout:
        wd.heartbeat()
        if wd.hit:
            break
        handle(line)
```

### AnswerCache

When my agent hit a screening question it didn't have a saved answer for, the original version would just make something up. This module makes it flag and skip instead.

Captures `NEW_QUESTION` / `ANSWER_MISMATCH` markers from agent output, persists them to disk, gives you a small approve/reject API. After the run, I go through pending questions once and write real answers; those answers join the knowledge base for next time.

```python
from claude_frugal_agents import AnswerCache

cache = AnswerCache("novel_questions.json")
cache.record_from_text(agent_output, marker="NEW_QUESTION",
                       context={"session_id": sid})

for entry in cache.pending():
    answer = input(f"Answer for: {entry.question}\n> ")
    cache.approve(entry.question, answer)
```

### KeywordScorer

80% of ranking tasks don't need an LLM. Rule-based scorer with explicit weight breakdowns, plus an optional LLM-rescore fallback for borderline cases only.

```python
from claude_frugal_agents import KeywordScorer, Rule

scorer = KeywordScorer(
    rules=[
        Rule(r"\b(senior|staff)\b", weight=-3, category="seniority", target="title"),
        Rule(r"\b(intern|new grad)\b", weight=+2, category="seniority", target="title"),
        Rule(r"\b(python|sql)\b", weight=+1, category="skill", target="description"),
    ],
    base=5,
    borderline=(5, 6),
    llm_fallback=my_llm_rescore,  # called ONLY for borderline items
)

for item in items:
    result = scorer.score(item)
    print(item["title"], "->", result.score, result.reasoning())
```

In my real pipeline this ran on every candidate. About 90% got a final score from the rules alone. Only the 10% in the borderline band 5-6 got the LLM fallback.

### ScraperBase

Generic fetch, parse, filter, dedup, store pipeline. Subclass it for your source; base class handles URL normalization, SQLite dedup, and bulk insertion. Zero LLM calls.

```python
from claude_frugal_agents.scraper_base import ScraperBase, Candidate

class MySiteScraper(ScraperBase):
    source_name = "mysite"
    def fetch(self): return urllib.request.urlopen(self.url).read().decode()
    def parse(self, raw):
        return [Candidate(url=m["url"], title=m["name"])
                for m in parse_table(raw)]
    def filter_fn(self, c):
        return "intern" in c.title.lower()
```

### browser_helpers

Cross-platform process-tree kill, port-based zombie cleanup, and the File System Access API kill JavaScript preamble that prevents native OS file pickers from freezing your browser agent.

```python
from claude_frugal_agents.browser_helpers import (
    FSA_KILL_JS, kill_process_tree, kill_on_port,
)

await page.evaluate(FSA_KILL_JS)  # run on every fresh tab
```

### zombie_cleanup

Standalone CLI and importable API for finding and killing orphan subprocesses after your pipeline dies. I ran this at the start of every pipeline session because something always wedges eventually.

```bash
python -m claude_frugal_agents.zombie_cleanup \
    --pattern chrome --parent python --cmdline-contains remote-debugging-port --dry-run
```

### monitor

Rich-based live dashboard scaffolding. Plug in callbacks for status counts, active workers, and log tail; the module handles the redraw loop.

## Quick start

```bash
pip install claude-frugal-agents
```

```python
from claude_frugal_agents import AgentWatchdog, ClaimValidator

# ...in your agent driver...
with AgentWatchdog(kill=my_kill, wall_clock_s=900, stall_s=300) as wd:
    for chunk in agent.stream():
        wd.heartbeat()
        if (res := my_validator.validate(chunk)) and not res.ok:
            wd.fire("validation_mismatch")
            break
```

## Runnable example

[`examples/form_filler_demo/`](examples/form_filler_demo/) shows these patterns working together on a local HTML form. No external site involved. A Claude agent fills the form; the validator checks it; the watchdog ensures it can't hang; the answer-cache handles a novel question. Runs in under a minute.

## Case study

[CASE_STUDY.md](CASE_STUDY.md) has the anonymized story of the 52-submission pipeline this was extracted from: the naive starting point, the bugs I hit, what I'd do differently.

## Testing

```bash
pip install -e .[dev]
pytest
```

40 tests, runs in about 2 seconds.

## License

MIT © 2026 Hritik Tiwari, see [LICENSE](LICENSE).

## Disclaimer

This repo demonstrates agent-engineering patterns. The case study describes a browser-automation pipeline as the motivating use case. If you adapt these patterns for automated form submission on third-party sites, be aware that many platforms prohibit automation in their Terms of Service. You assume full responsibility for how you apply these techniques. Nothing here is legal or policy advice.

## Author

Built by [Hritik Tiwari](https://github.com/hritik-tiwari) during my MS in Statistics at Purdue while running the pipeline against real internship postings. If you find this useful or want to talk about Claude agents, open an issue or reach out.
