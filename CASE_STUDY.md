# Case study: patterns from a 52-submission Claude browser-agent pipeline

> Anonymized. No companies, URLs, ATS tenants, or specific dollar
> amounts appear here. This is about the engineering patterns, not the
> target sites.

## What I was trying to build

A Claude-agent pipeline that would, end to end:

1. Pull a curated list of new job postings from a handful of public
   sources that refresh daily.
2. Score each posting against my background and drop the non-targets.
3. Launch Chrome on the survivors, fill out each application form with
   my resume and screening answers, and submit.
4. Record the result, flag anything that needed my attention, and move
   on to the next.

The goal wasn't "automate away the search" -- it was "spend my time on
the roles that are actually worth a custom cover letter, and let a
cheap automated first pass handle the rest." A grad student's time is
better spent reading papers and writing tailored applications than
clicking 'I do not wish to self-identify' for the 400th time.

## The naive starting point

The first version was what you'd expect if you'd read one blog post
about Claude agents and then sat down to write code on a Saturday.

```
Python driver
  -> Claude (one giant prompt)
     -> browser via Playwright MCP
```

The driver handed Claude the entire job posting HTML and the entire
resume JSON and said: *figure it out.* Claude reasoned about whether
the role was a good fit. Claude parsed the scraped HTML. Claude
reasoned about which field on the form held the first name. Claude
wrote the cover letter. Claude decided "is this submitted yet" by
re-reading the page.

It worked. Sort of. On a single end-to-end submission it cost me in
the neighborhood of $10, took 8-15 minutes, and maybe one time in
three it would confidently declare `APPLIED` without actually having
clicked the submit button, because Claude had hallucinated a
confirmation screen it had never seen.

That wasn't a pipeline. That was a very expensive, very slow,
occasionally-wrong script.

## What broke, and what I built in response

Over about four months I kept iterating on the same system while
actually using it -- it ran for real against real job postings, every
weekday. This is the short list of things that broke and the pattern
I pulled out of the fix.

### 1. The LLM was parsing HTML tables

Four different public data sources dump their job tables as either
markdown or HTML in a `README.md` file. I was asking Claude to read
each one. That was ~30K tokens before we'd even opened a browser.

**Fix:** Write a `html.parser` subclass. Write a regex for the
markdown variant. The "interpretation" of a table column is not
actually a thing that needs a language model. It needs the author of
the scraper to look at the table once and write down which column is
the company name.

This is now [`ScraperBase`](src/claude_frugal_agents/scraper_base.py):
abstract pipeline (fetch -> parse -> filter -> dedup -> store),
concrete subclasses per source.

### 2. The LLM was ranking items

For every job I was sending Claude the full posting text plus a
long-form "here is my background" blob and asking for a fit score
1-10. It almost always returned 7 or 8. You can hit that accuracy
with thirty regex rules and a +/- weight each. The LLM was acting as
a very expensive reflex.

**Fix:** [`KeywordScorer`](src/claude_frugal_agents/keyword_scorer.py)
with explicit weighted rules. But I still wanted the LLM for the
genuinely ambiguous cases, so the scorer has an `llm_fallback` hook
that only fires when the rule-based score lands in a configurable
"borderline" band (5 or 6 out of 10 in my case).

The real finding: the LLM-fallback ran on maybe 10% of items. The
other 90% got a free, reproducible, auditable score.

### 3. The 'RESULT: APPLIED' false positives

This was the single worst bug in the first month. The agent was
supposed to, at the end of a run, emit a line like
`RESULT:APPLIED:<confirmation_id>`. Sometimes it would emit `APPLIED`
without actually having clicked submit -- it had gotten confused by a
prior page, or a validation error had surfaced that it didn't
recognize, and it would declare victory anyway.

From my side I couldn't tell the difference. The database just showed
a jump in applied counts. It took me a week to notice the true
applied count didn't match what I was seeing in my inbox
confirmations.

**Fix:** Don't trust the LLM to self-report success on an
irreversible action. Instead, have the agent emit a
`PRESUBMIT_CHECK: {...}` block right before the submit click, and
verify in Python that the claimed field values match what the profile
says they should be. Mismatch -> kill the subprocess before the
submit click can land.

This is now [`ClaimValidator`](src/claude_frugal_agents/validator.py).
The discipline it enforces is: the last thing before an irreversible
action is a deterministic check. The agent describes what it's about
to do; Python verifies. If they don't agree, the action doesn't
happen. This is a safety property the agent can't talk its way out of.

The related insight: the validator also made the agent more careful
about filling the form. Once you know there's going to be a
checkpoint, you stop cutting corners.

### 4. The $5-per-submit budget cap

Halfway into the project, Claude introduced per-session budget caps.
I started setting $5/job. About 10% of jobs would then die
mid-submit because they exceeded the budget while the agent was
waiting on a slow portal. Some of them died AFTER the submit click
had technically gone through but before the confirmation page had
loaded -- so the DB would record it as failed, and I'd later find
the confirmation email.

**Fix (half of it):** Add a confirmation-signal fallback. If the
agent's stdout contains phrases like "application submitted",
"thank you for applying", etc. in the last ~4K characters of output,
treat that as `APPLIED` even if the agent didn't emit the canonical
`RESULT:APPLIED` line. Explicit confirmation signals from the page
are more trustworthy than the agent's summary of what happened.

**Fix (the other half):** Budget 8, not 5. It's an internship
application. Penny-wise is pound-foolish.

### 5. The Chrome-profile binding problem

For the agent to submit jobs behind an authenticated ATS (many of
them require you to log in before you can apply), it had to use
a Chrome profile where those sites already had session cookies. But
two workers can't share a profile directory -- Chrome puts a single
lock file in there and the second instance crashes.

**Fix:** Clone the real Chrome profile to a per-worker directory on
first setup, reuse on subsequent runs, and have a "reclone" command
for when the user has added new credentials to their real Chrome.
Plus: whenever you kill Chrome with `taskkill`, it writes
`exit_type=Crashed` to its prefs and on next launch pops a "restore
pages?" bubble that sits on top of the page and blocks the
automation. Patch the prefs file to set it back to `Normal`.

That Chrome preferences patching lives in
[`browser_helpers.suppress_chrome_restore_nag`](src/claude_frugal_agents/browser_helpers.py).

### 6. The native file-picker trap

Some form uploads would briefly and occasionally trigger the
browser's showOpenFilePicker / showSaveFilePicker API. When that
fires, Chrome shows the OS native file dialog -- which exists
outside the DOM, which means Playwright cannot interact with it at
all, and the agent just sits there forever waiting for a click
target that will never be clickable.

**Fix:** At the start of every page the agent lands on, evaluate a
JavaScript snippet that replaces `showOpenFilePicker`,
`showSaveFilePicker`, and `showDirectoryPicker` with no-ops. Gone.

This is the `FSA_KILL_JS` constant in
[`browser_helpers`](src/claude_frugal_agents/browser_helpers.py). I
know of at least three other agent projects that have rediscovered
this same trap independently.

### 7. The subprocess-that-won't-die problem

The agent is a Claude CLI subprocess. That subprocess spawns a Node
process for the Playwright MCP server. That Node process spawns
browser instances. When anything in that chain wedges and you try
to kill the Python driver with Ctrl-C, you can easily leave a tree
of orphaned browser + Node processes still holding the CDP port,
and your next run can't start.

**Fix:** Three-layer kill. First `psutil` walks the descendants and
kills each by PID. If that fails or psutil isn't installed, fall
back to `taskkill /F /T` on Windows or `killpg` on Unix. If that
also fails, direct single-PID kill.

This is [`kill_process_tree`](src/claude_frugal_agents/browser_helpers.py)
and the matching [`zombie_cleanup`](src/claude_frugal_agents/zombie_cleanup.py)
standalone sweeper. I run the sweeper automatically at the start of
every pipeline run, because no matter how careful you are, something
eventually wedges.

### 8. The "it's been 20 minutes, what is it doing?" problem

Sometimes the agent would get into a loop. Polling the same page for
an element that wasn't going to appear. Waiting for a page that was
never going to load. The wall-clock budget (15 min/job) eventually
catches this, but I wanted faster detection when the symptom was
"the agent just stopped saying anything" rather than "the agent is
burning tokens in a loop."

**Fix:** Two-watchdog system. Layer 1 is a `threading.Timer` for the
wall-clock budget. Layer 2 is a thread that polls every 30s and if
there's been no stdout for > N minutes, assumes the agent is wedged
and kills the subprocess.

This is [`AgentWatchdog`](src/claude_frugal_agents/watchdog.py). It's
the piece I wish I'd built from day one -- it's saved me from a lot
of "checked on the pipeline six hours later, turns out the first job
had hung and nothing else ran" mornings.

### 9. Novel questions

Every portal has at least one screening question I hadn't anticipated.
"How did you hear about this position?" "Are you willing to relocate
within 30 days of offer?" "Which of the following best describes your
citizenship?"

The first version of the agent would confidently invent answers. The
invented answer was almost always plausible and almost always wrong
in some non-obvious way -- "how did you hear about this position"
got answered with "LinkedIn" 80% of the time even though the truth
was "a GitHub scraper you've never heard of."

**Fix:** Teach the agent a `NEW_QUESTION: {...}` marker. When it hits
a question that isn't in its current knowledge base, it emits the
marker and stops. A Python-side cache captures the marker; after the
run I review each pending question, write a canonical answer, and
that answer joins the knowledge base for next time.

This is [`AnswerCache`](src/claude_frugal_agents/answer_cache.py). The
important property is that a novel question **skips the job** rather
than guessing. I'd rather re-queue the job after I've answered the
question than silently submit a wrong answer, because a wrong answer
is irreversible and a skipped job isn't.

### 10. The live dashboard

Running the pipeline blind is a bad experience. The DB tells you
after the fact which jobs finished in what state, but it doesn't
tell you "right now, worker 0 is looking at this job and clicking
this button." For that you want a Rich-based live dashboard that
reads the DB and tails the log.

None of this involves the LLM either. I see a lot of Claude-agent
projects that reach for some elaborate structured-event bus for
telemetry when the answer is just "write side effects to SQLite
and tail the log file from a second Python process."

This is the [`monitor`](src/claude_frugal_agents/monitor.py) module.

## The numbers

Final figures from the real run:

- **52 applications** actually submitted end to end
- **~$90** total Anthropic spend across the whole run, give or take,
  across a few hundred jobs that made it through scoring (many failed
  before submit due to closed reqs, CAPTCHA, login requirements, etc.)
- **5 calendar days** of runtime (mostly unattended, a few checks per
  day)
- Roughly **$1.70 per submitted application** once I'd pushed through
  all the frugality patterns. The first naive pass was $10+.

I am not going to claim this is a miracle. A human filling out 52
forms does it for $0 of API spend. The interesting number is the
cost *arc*: $10+ per job at the start, under $2 per job at the end,
and most of the drop came from deterministic Python, not from any
Claude pricing change.

## What I'd do differently

**Build the validator first.** The biggest class of wasted effort was
debugging false-positive submissions. If I'd had `ClaimValidator` in
place from day one I'd have saved a week.

**Build the watchdog first.** Same reason. You don't realize how
often a long-running agent hangs until you've built the tool that
measures it.

**Build a small deterministic eval harness.** I mostly tested against
real sites, which is slow and non-reproducible. A local HTML fixture
that the agent fills in with known-good fields (exactly what
`examples/form_filler_demo/` in this repo is) would have shaved days
off the iteration.

**Don't overbuild the scraper.** My first scraper tried to handle
every possible variant of every data source. It was 2000 lines of
Python I mostly threw away. Start with "what's the minimum that
works for today's data" and let real edge cases justify new code.

**Write down the patterns as you find them.** That's what this repo
is. If I'd been extracting these modules into a clean file from the
beginning instead of cleaning up all at once at the end, the private
project would've been smaller and this public one would have been
easier to pull out.

## Why open-source this

Two reasons.

One, because almost none of the patterns in this repo are about the
job-application use case. They're about running a Claude subprocess
agent that does browser work. Any such project ends up
re-discovering `kill_process_tree`, `AgentWatchdog`, `FSA_KILL_JS`,
and `ClaimValidator`. Not discovering them is a tax on the field.

Two, because the most visible Claude-agent work right now is the
demo-reel kind -- "look, it booked me a flight." That's cool but it
leaves out the boring 90% of a real production pipeline. This repo
is the boring 90%. The interesting 10% is still the language model.
This is what surrounds it.
