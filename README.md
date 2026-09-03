# Resume Tailor

Tailors your LaTeX resume to a job description using the local Claude Code CLI,
then compiles a PDF. Existing bullet wording stays unless a job-relevant tool
or keyword can be inserted into it. The tailor can also **add or swap work
experience and projects you keep in a content bank but not on your base
resume** — deciding per job whether they are worth the space. Your master
resume is never modified.

## Requirements

1. Python 3.11+
2. A LaTeX distribution with `pdflatex` (MiKTeX or TeX Live)
3. Claude Code CLI installed and authenticated (`claude --help` should work)

## Setup

```bash
pip install -r requirements.txt
```

Optional `.env` to pin models:

```text
CLAUDE_MODEL=sonnet        # analysis and trimming
CLAUDE_PLAN_MODEL=opus     # the tailoring plan (worth the better model)
```

## Usage

1. Master resume in `base_resume.tex` (new grad / 0 years required) and
   `1yo_experience_base_resume.tex` (1+ years required) — see below.
2. Job description in `job_description.txt`.
3. Fill in the content bank (see below).
4. Run:

```bash
python tailor.py
```

Each run gets its own folder under `output/`, named from the company and job
title the analysis step pulls out of the job description:

```
output/hudson_river_trading_electronic_trading_support_engineer/Michael_Aho_Resume_2026.{tex,pdf}
```

If extraction comes up empty (an anonymized or vague posting), it falls back to
`output/job_<timestamp>/`. Pass `--company` / `--job-title` to set the folder
name yourself instead.

### Base resume auto-selection

Unless `--resume` is given explicitly, the tool scans the job description for
a years-of-experience requirement and picks the master resume for you:

- Posting asks for **1+ years** of experience → `1yo_experience_base_resume.tex`
  (ILS, the current job, leads WORK EXPERIENCE).
- No such requirement, or an explicit new-grad/entry-level signal →
  `base_resume.tex`.

The choice and reason are printed at the start of the run (e.g. `Auto-selected
base resume: 1yo_experience_base_resume.tex (posting asks for 1+ year(s) of
experience)`).

### Running multiple instances at once

This is safe - open two terminals and tailor two jobs at the same time, or
script a batch over several job descriptions. Nothing is shared between runs:

- Every run claims its output folder with an atomic `mkdir`. If two runs land
  on the same company + title at the same instant, one wins the plain name and
  the other gets `-2` (verified with a 20-way concurrent race in testing) - so
  concurrent runs never overwrite each other's `.tex`/`.pdf`, no locking needed.
- Each run compiles in its own OS temp directory (`tempfile.TemporaryDirectory`
  gives every process a unique path), so parallel `pdflatex` invocations don't
  collide either.
- `base_resume.tex` and the content bank are read-only to every run.

The one thing outside this tool's control is the `claude` CLI itself - if it
serializes concurrent invocations internally, parallel runs would queue rather
than truly run side by side. Nothing in this pipeline adds that limitation.

### Options

| Flag | Effect |
|---|---|
| `--job PATH` | Use a different job description file |
| `--resume PATH` | Use a specific master resume (skips auto-selection) |
| `--output DIR` | Base output directory (default `output`) |
| `--company NAME` | Override the folder's company name |
| `--job-title TITLE` | Override the folder's job title |
| `--list-ids` | Print resume entry/bullet ids (for `extra_bullets.yaml`) |
| `--save-plan PATH` | Dump the raw plan JSON for inspection |
| `--dry-run` | Write the `.tex` but skip PDF compilation |
| `--no-bank` | Ignore the content bank this run |
| `--no-drop-entries` | Never remove an existing experience or project |
| `--max-pages N` | Page ceiling (default 1) |
| `--line-budget N` | Space target the planner aims at (default 47) |

## The content bank

This is the part that lets the tailor add material that is not on your resume.

### `content/experience_bank.yaml`

Whole work experiences and projects that are **not** on `base_resume.tex`. For
example, consulting experience you leave off a backend-focused resume but want
pulled in for a forward-deployed or solutions role.

```yaml
experiences:
  - id: acme_consulting
    company: Acme Consulting
    title: Technical Consultant
    dates: January 2025 - August 2025
    location: Remote
    tags: [client-facing, forward-deployed, consulting]
    notes: >
      Context for the model's judgement only. Never copied onto the resume.
    bullets:
      - id: acme_scoping
        text: Embedded with 3 enterprise clients to scope requirements...
        tags: [client-facing, requirements]

projects:
  - id: llm_agent_project
    name: LLM Agent Project
    dates: February 2026 - Present
    tech: Python, LangChain, FastAPI, Docker
    link: https://github.com/you/example
    tags: [ai, llm, agents]
    bullets: [...]
```

#### Forcing a tradeoff with `requires_drop`

The resume is one page, so some bank entries should only be addable if
something else gets cut to make room. Declare that as data, not a hope that the
model remembers:

```yaml
    requires_drop: [fantasy_stock_league, jmu_coursework_and_awards]
```

This is enforced in code after the plan runs, not left to the model's
discretion. The semantics are a counting rule: each added entry with a
`requires_drop` list demands exactly one removal from that ordered list of
preferences, and what matters is how many of those targets end up missing in
total - not which one, and not whether the model or this rule removed it. So:

- Add one such entry → the first target still present is removed (Fantasy Stock
  League, here).
- Add two entries that both list the same first preference → it's removed
  once, and the second entry's requirement falls through to its next
  preference (`jmu_coursework_and_awards`).
- If the model already dropped Fantasy Stock League on its own for unrelated
  reasons, that already satisfies one requirement - nothing extra is cut.

Targets are resume entry ids (`--list-ids`) or the sentinel
`jmu_coursework_and_awards`, which is the one narrow, hard-coded exception to
EDUCATION being completely off-limits to the model: it removes JMU's "Relevant
Coursework" and "Awards" bullets together as a unit (and cleans up the now-empty
bullet list around them). `--no-drop-entries` disables this rule too - you'll
get a warning instead of a forced cut.

### `content/extra_bullets.yaml`

Extra accomplishments from jobs/projects **already** on the resume, big enough to
stand as their own bullet. `entry:` must be a resume entry id — run
`python tailor.py --list-ids` to see them.

```yaml
bullets:
  - id: cross_screen_debug
    entry: cross_screen_media
    text: Debugged 12+ full-stack tickets using React and Django...
    tags: [debugging, react, django, full-stack]
```

### `content/facts.yaml`

Details **too small to be their own bullet** — a tool, a language, a workflow, a
system some work touched. Instead of adding a line, the tailor weaves these
*into* a bullet it is already rewriting, which costs almost no space:

> "Migrated payments MCP tooling across 5 repos to TypeScript"
> → "…across 5 repos to TypeScript **using Claude Code**"

```yaml
facts:
  - id: co_claude_code
    scope: capital_one
    text: Used Claude Code as a development tool
    tags: [claude, ai-tooling, llm, ai-assisted-development]

  - id: co_splunk
    scope: capital_one
    bullet_hint: the Integrity / CodeDeploy rollback automation bullet
    text: The Integrity rollback automation included writing a Splunk query
    tags: [splunk, observability, monitoring]
```

`scope` controls where a fact may be used:

| `scope` | Usable in |
|---|---|
| an entry id | only bullets belonging to that entry |
| `global` | any bullet, and the skills lines |
| `skills` | the skills lines only |

`bullet_hint` optionally names the one bullet a fact is true of, for details that
apply to a specific piece of work rather than the whole job.

Facts only appear when the posting makes them relevant — a fact no job asks about
never shows up, so add every tool, language, and workflow you can honestly claim.

### Tips

- Add **more** bullets than could ever fit. The tailor picks a subset per job;
  unused bullets cost nothing, missing ones cost you matches.
- Make `tags` words a job posting would actually contain (`kubernetes`,
  `client-facing`, `ci-cd`), since that is what the matching keys off.
- Use `notes` to explain when something is and isn't worth including.
- Every `id` must be unique across all three files.
- Rule of thumb: own accomplishment → `extra_bullets.yaml`; detail that belongs
  inside an existing accomplishment → `facts.yaml`; whole job or project →
  `experience_bank.yaml`.

## How it works

1. Parses `base_resume.tex` into sections → entries → bullets with stable ids.
2. Loads the YAML content bank.
3. Sends the job description, a compact resume snapshot, and a compact bank
   snapshot to Claude in **one call**. The model returns an analysis plus a
   **sparse plan** — only bullets/entries to enrich, drop, or add. Everything
   else stays as-is.
4. Validates and applies the plan. Cosmetic paraphrases of existing bullets are
   discarded; the original wording is kept.
5. Enforces any `requires_drop` tradeoffs the plan didn't already satisfy.
6. Compiles, reads the real page count from `pdflatex`, and runs trim passes
   until it fits one page.

## Guardrails

There is no LLM "factuality" or realism check — those blocked most tailoring.
Truthfulness is enforced structurally instead:

- New content can only enter the resume by citing a bank `source_id` that
  actually exists. A hallucinated id is a hard error, not a warning.
- Bullets added under a bank entry must belong to that entry.
- Facts are scoped, so a Capital One detail cannot be attached to an ILS bullet.
  Citing a fact id that doesn't exist raises a warning naming it.
- Skills tokens are checked against everything in your resume, bank, and facts.
  Anything unsupported is flagged — this catches the model importing keywords
  straight from the job posting.
- `requires_drop` tradeoffs (see above) are enforced in code after the plan
  runs, not trusted to the model's compliance — it already tends to undercount
  its own line budget, so a hard space tradeoff isn't left to its arithmetic.
- Wording changes to your existing bullets are restricted: a rewrite is applied
  only if it keeps the original metrics and most of the original sentence, and
  actually inserts an in-scope fact the posting makes relevant (e.g. adding
  Snowflake to a data-pipeline bullet, Python to Capital One bullets when the
  job asks for scripting, Splunk when it asks about alerts/monitoring). Synonym
  swaps, clause reshuffles, and "stronger verb" rewrites are discarded.
- The planner is steered to enrich multiple bullets and swap bank entries (e.g.
  client-facing consulting) when posting signals match — not to leave the resume
  nearly unchanged.
- The tool refuses to empty a section, and (unless the whole entry is being
  dropped) refuses to strip every bullet from an entry.
- Structural LaTeX in bullet text is rejected.

**What this means for you:** existing bullets should come back looking like
yours, plus a tool or keyword the posting actually asks for. Review the output
anyway - a fact insert can still be slightly clumsy, and bank swaps are a
judgement call. Skim the diff before sending:

```bash
git diff --no-index base_resume.tex output/Michael_Aho_Resume_2026.tex
```

## Calibration

`CHARS_PER_LINE` (120) and `ENTRY_OVERHEAD_LINES` (3) in `resume_doc.py`, and
`LINE_BUDGET` (47) in `tailor.py`, are calibrated for this template's margins and
font size by padding the resume until `pdflatex` reported a second page. If you
change the geometry or font, re-measure or just adjust `--line-budget` — the
compile-and-measure loop still enforces the real page limit either way.
