#!/usr/bin/env python3
"""Claude calls and the operation schema that drives tailoring.

One LLM call analyzes the job and returns a sparse edit plan. Bullets not
mentioned in the plan are kept as-is. Payloads use compact JSON and short keys.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time

from resume_doc import TailorError

# ---------------------------------------------------------------------------
# Claude CLI transport
# ---------------------------------------------------------------------------


def compact_json(data: object) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def extract_json(text: str) -> dict | list:
    """Parse JSON from model output, stripping optional markdown fences."""
    cleaned = text.strip().lstrip("\ufeff")
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
    if fence:
        cleaned = fence.group(1).strip()

    candidates = [cleaned]
    if "\\'" in cleaned:
        candidates.append(cleaned.replace("\\'", "'"))

    normalized = (
        cleaned.replace(""", '"')
        .replace(""", '"')
        .replace("'", "'")
        .replace("'", "'")
    )
    if normalized != cleaned:
        candidates.append(normalized)
        if "\\'" in normalized:
            candidates.append(normalized.replace("\\'", "'"))

    errors: list[str] = []
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            errors.append(str(exc))
        for opener in ("{", "["):
            start = candidate.find(opener)
            if start == -1:
                continue
            try:
                value, _ = decoder.raw_decode(candidate[start:])
                return value
            except json.JSONDecodeError as exc:
                errors.append(str(exc))

    detail = errors[-1] if errors else "unknown parse error"
    raise TailorError(f"Malformed model JSON response ({detail}):\n{text[:800]}")


def _extract_claude_result(stdout: str) -> str:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise TailorError("Claude returned non-JSON output; try again.") from exc
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, str) or not result.strip():
        raise TailorError("Claude returned an empty result.")
    return result.strip()


def model_json(model: str, system: str, user: str) -> dict | list:
    """Call the local Claude Code CLI in print mode and parse JSON output."""
    if shutil.which("claude") is None:
        raise TailorError(
            "Claude CLI not found on PATH. Install Claude Code and ensure "
            "`claude` works in this shell."
        )

    command = [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--permission-mode",
        "dontAsk",
        "--tools",
        "",
        "--model",
        model,
        "--system-prompt",
        system,
        user,
    ]

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            raise TailorError(f"Failed to run Claude CLI: {exc}") from exc

        if proc.returncode == 0:
            return extract_json(_extract_claude_result(proc.stdout))

        detail = (proc.stderr or "").strip() or (proc.stdout or "").strip() or f"exit {proc.returncode}"
        last_error = TailorError(detail)

        if any(t in detail.lower() for t in ("429", "rate limit", "overloaded")) and attempt < 2:
            wait_s = 8 * (attempt + 1)
            print(f"  Claude rate limited; retrying in {wait_s}s...")
            time.sleep(wait_s)
            continue
        break

    raise TailorError(f"Claude CLI error: {last_error}")


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

TAILOR_SYSTEM = """Analyze a job posting and emit a sparse resume edit plan.
You do NOT output LaTeX or full resume text.

# BALANCE (important)

Keep each bullet's original wording and metrics. Do NOT synonym-swap (Developed→Built)
or restructure sentences. DO actively tailor by:
  1. `enrich` — weave posting-relevant facts into existing bullets (languages, tools).
  2. `add_entries` / `add_bullets` — pull bank content in when tags match the role.
  3. `drop_entries` / `drop_bullets` — cut less relevant items to make room.
  4. `skills` — reorder to lead with posting keywords (Python, UNIX/Linux, etc.).
  5. `reorder` — surface the most relevant jobs/projects first.

A good plan for a typical posting has several enrichments plus 0-2 bank adds.
An empty or nearly empty plan is a failure unless nothing in the bank fits.

Use the `enrich_hints` list when provided — those are pre-validated fact inserts.

# TRUTH

- New content must cite a bank id from the payload.
- Never invent employers, metrics, projects, or tools.
- Preserve every number exactly.
- Bank entry `n` (note) is context only; never copy verbatim.

# ENRICH

Each enrich item: {"id": "<bullet_id>", "fact": "<fact_id>", "text": "<optional>"}.
If `text` is omitted, the tool inserts the fact automatically. Prefer citing the
fact id; only supply `text` when you need a specific phrasing.

Weave facts naturally: "against Snowflake", "(Python/TypeScript)", "and Splunk alerts".
Apply every posting-relevant fact in scope — e.g. if the job asks for Python, enrich
Capital One bullets with co_languages where Python is not already named.

Facts: `in` = entry id, global, or skills. `hint` names a specific bullet when set.

# BANK SWAPS

When posting priorities match a bank entry's tags, add it and drop something less
relevant. Examples:
  - client-facing / communication / interfacing with clients → consulting entries
  - trading / markets / Python scripting → stock simulator project, Python facts
  - UNIX / Linux / systems → linux_jmu on skills, Linux on skills line
Check each bank entry's tags and `n` field against analysis priorities.

Respect `drop` on bank entries — adding that entry requires dropping one listed item.

# SPACE

Line budget given. est_lines = ceil(len/120) per bullet + 3 per entry.
Report `est_lines`. Aim between budget-4 and budget.

# OUTPUT

Return ONLY valid JSON (no markdown):

{
  "analysis": {
    "company": "", "job_title": "", "role_archetype": "",
    "seniority": "intern | new grad | mid | senior",
    "priorities": ["..."], "must_have_keywords": ["..."],
    "nice_to_have_keywords": ["..."], "responsibilities": ["..."],
    "signals_valued": ["..."], "deprioritize": ["..."]
  },
  "plan": {
    "strategy": "1-2 sentences",
    "est_lines": 45,
    "enrich": [{"id": "capital_one.b3", "fact": "co_splunk"}],
    "drop_bullets": ["ils.b3"],
    "drop_entries": ["parallel_nbody"],
    "add_bullets": [{"id": "cross_screen_debug", "entry": "cross_screen_media"}],
    "add_entries": [{"id": "2landmarks", "section": "WORK EXPERIENCE", "position": 0, "bullets": ["2landmarks_adoption"]}],
    "reorder": [{"section": "WORK EXPERIENCE", "order": ["2landmarks", "capital_one", "ils"]}],
    "skills": {"Programming Languages": "Python, ...", "Frameworks \\\\& Libraries": "...", "Cloud \\\\& Tools": "... Linux/UNIX ...", "Testing \\\\& Databases": "..."}
  }
}

Rules:
- Omit empty arrays/objects.
- `add_bullets[].id` / `add_entries[].bullets[]` = bank bullet ids.
- `add_entries[].id` = bank entry id. `drop_entries` = resume entry ids.
- `skills` keys must match resume payload labels exactly.
- Analysis lists: terms from posting only, ~5-10 items each.
- Plain bullet text; no structural LaTeX. Escaped \\% \\& \\_ OK."""


TRIM_SYSTEM = """The tailored resume overflows one page. Shorten it.

Return the smallest set of changes that makes it fit:
  1. Tighten wordy bullets just past a line boundary.
  2. Shorten the least relevant bullets.
  3. Drop the single least relevant bullet.

Never delete a metric while keeping the bullet. Never drop the last bullet of
an entry. Do not invent anything. Trim only; do not re-tailor.

Return ONLY valid JSON (no markdown):
{"shorten":[{"id":"ils.b1","text":"shorter version"}],"drop":["quickmarketz.b3"]}"""


# ---------------------------------------------------------------------------
# Response normalization
# ---------------------------------------------------------------------------

ANALYSIS_DEFAULTS: dict[str, object] = {
    "company": "",
    "job_title": "",
    "role_archetype": "",
    "seniority": "",
    "priorities": [],
    "must_have_keywords": [],
    "nice_to_have_keywords": [],
    "responsibilities": [],
    "signals_valued": [],
    "deprioritize": [],
}


def normalize_analysis(data: dict) -> dict:
    out = dict(ANALYSIS_DEFAULTS)
    for key, fallback in ANALYSIS_DEFAULTS.items():
        if key in data and isinstance(data[key], type(fallback)):
            out[key] = data[key]
    return out


def normalize_plan(data: dict) -> dict:
    """Accept sparse plan or legacy bullet_decisions format."""
    if not isinstance(data, dict):
        raise TailorError("Plan response was not a JSON object.")

    plan = dict(data)
    if "estimated_total_lines" in plan and "est_lines" not in plan:
        plan["est_lines"] = plan["estimated_total_lines"]

    # Legacy: drop_entries as [{id: ...}]
    raw_drops = plan.get("drop_entries") or plan.get("drops") or []
    if raw_drops and isinstance(raw_drops[0], dict):
        plan["drop_entries"] = [
            str(item.get("id") or item.get("entry") or "")
            for item in raw_drops
            if isinstance(item, dict) and (item.get("id") or item.get("entry"))
        ]

    # Legacy bullet_decisions -> sparse fields (for saved plans / older output)
    decisions = plan.get("bullet_decisions")
    if isinstance(decisions, list) and decisions:
        enrich = list(plan.get("enrich") or [])
        drop_bullets = list(plan.get("drop_bullets") or [])
        facts_used = list(plan.get("facts_used") or [])
        for item in decisions:
            if not isinstance(item, dict):
                continue
            bid = item.get("id")
            if not isinstance(bid, str):
                continue
            action = str(item.get("action") or "keep").lower()
            if action in {"rewrite", "enrich"} and item.get("text"):
                enrich.append({"id": bid, "text": item["text"], "fact": item.get("fact", "")})
                why = str(item.get("why") or "")
                for token in re.findall(r"[a-z][a-z0-9_]+", why):
                    if token.startswith(("co_", "cross_", "ils_", "quick", "git_", "linux", "database")):
                        facts_used.append(token)
            elif action == "drop":
                drop_bullets.append(bid)
        if enrich:
            plan["enrich"] = enrich
        if drop_bullets:
            plan["drop_bullets"] = drop_bullets
        if facts_used:
            plan["facts_used"] = facts_used

    return plan


def merge_enrich_hints(plan: dict, hints: list[dict]) -> dict:
    """Ensure posting-matched fact hints are in the plan (model may omit them)."""
    if not hints:
        return plan
    enrich = list(plan.get("enrich") or [])
    covered: set[str] = set()
    for item in enrich:
        if isinstance(item, dict):
            bid = item.get("id")
            if isinstance(bid, str):
                covered.add(bid)
    for hint in hints:
        bid = hint.get("id")
        fact = hint.get("fact")
        if not isinstance(bid, str) or bid in covered:
            continue
        enrich.append({"id": bid, "fact": fact} if fact else {"id": bid})
        covered.add(bid)
    plan["enrich"] = enrich
    return plan


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------


def analyze_and_plan(
    model: str,
    job: str,
    resume_payload: dict,
    bank_payload_data: dict,
    line_budget: int,
    enrich_hints: list[dict] | None = None,
) -> tuple[dict, dict]:
    """Single call: analyze the posting and return a sparse tailoring plan."""
    current_lines = resume_payload.get("lines") or resume_payload.get("current_est_lines") or 0
    hints_block = ""
    if enrich_hints:
        hints_block = f"\nEnrich_hints:{compact_json(enrich_hints)}\n"
    user = (
        f"Job description:\n{job}\n\n"
        f"Resume:{compact_json(resume_payload)}\n\n"
        f"Bank:{compact_json(bank_payload_data)}\n"
        f"{hints_block}\n"
        f"Budget:{current_lines} lines now, target {line_budget} "
        f"(aim {line_budget - 4}-{line_budget}). Return the JSON."
    )
    data = model_json(model, TAILOR_SYSTEM, user)
    if not isinstance(data, dict):
        raise TailorError("Tailoring response was not a JSON object.")

    analysis_raw = data.get("analysis")
    if not isinstance(analysis_raw, dict):
        raise TailorError("Response missing 'analysis' object.")
    analysis = normalize_analysis(analysis_raw)

    plan_raw = data.get("plan")
    if not isinstance(plan_raw, dict):
        raise TailorError("Response missing 'plan' object.")
    plan = normalize_plan(plan_raw)
    return analysis, plan


def trim_resume(model: str, bullets_payload: list[dict], overflow_lines: int) -> dict:
    user = (
        f"Bullets:{compact_json(bullets_payload)}\n"
        f"Remove ~{overflow_lines} rendered line(s). Return trim JSON."
    )
    data = model_json(model, TRIM_SYSTEM, user)
    if not isinstance(data, dict):
        raise TailorError("Trim response was not a JSON object.")
    return data
