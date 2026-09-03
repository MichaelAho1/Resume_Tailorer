#!/usr/bin/env python3
"""Tailor a LaTeX resume to a job description via the local Claude Code CLI.

Pipeline:
  1. Parse base_resume.tex into sections -> entries -> bullets.
  2. Load the YAML content bank (off-resume experiences, projects, bullets).
  3. Analyze the job and plan edits in one LLM call (sparse plan: only changes).
  4. Validate and apply the plan; paraphrase-only rewrites are rejected.
  5. Compile, read the real page count, and trim until it fits one page.

The master resume is never modified.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

import planner
from apply_plan import (
    SKILL_LINE_RE,
    apply_plan,
    apply_trim,
    enforce_entry_requirements,
    suggest_enrichments,
)
from bank import bank_payload, load_bank
from resume_doc import (
    MUTABLE_SECTIONS,
    Document,
    TailorError,
    parse_resume,
    render_document,
    slugify,
)

RESUME_NEW_GRAD = "base_resume.tex"
RESUME_1YO = "1yo_experience_base_resume.tex"
DEFAULT_JOB = "job_description.txt"
DEFAULT_OUTPUT = "output"
DEFAULT_BANK = "content"
DEFAULT_MODEL = "sonnet"
DEFAULT_PLAN_MODEL = "opus"
OUTPUT_BASENAME = "Michael_Aho_Resume_2026"
MAX_PAGES = 1
MAX_TRIM_PASSES = 3
# Calibrated for this template by padding the resume until pdflatex reported a
# second page: the base resume estimates 42 rendered lines and it spills between
# 48 and 52. 47 lets the planner use most of the page while landing under the
# ceiling often enough that the trim loop stays a fallback rather than the norm.
LINE_BUDGET = 47

PAGE_COUNT_RE = re.compile(r"\((\d+)\s+pages?,", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Base resume auto-selection
# ---------------------------------------------------------------------------

_NUMBER_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}

# Matches phrasing like "1+ years of experience", "2-4 years experience",
# "at least one year of relevant experience".
_YEARS_EXPERIENCE_RE = re.compile(
    r"(?P<num>\d+|zero|one|two|three|four|five)\s*\+?\s*(?:-\s*\d+\s*)?\+?\s*"
    r"years?\s+(?:of\s+)?(?:[a-z]+\s+){0,4}?experience",
    re.IGNORECASE,
)

_NEW_GRAD_SIGNS_RE = re.compile(
    r"\b(new grad(?:uate)?s?|entry[- ]level|recent graduate|early[- ]career|"
    r"no prior experience(?:\s+required)?)\b",
    re.IGNORECASE,
)


def min_years_required(job_text: str) -> int | None:
    """Lowest years-of-experience figure mentioned in the posting, if any."""
    years = []
    for m in _YEARS_EXPERIENCE_RE.finditer(job_text):
        raw = m.group("num").lower()
        years.append(int(raw) if raw.isdigit() else _NUMBER_WORDS[raw])
    return min(years) if years else None


def select_base_resume(job_text: str) -> str:
    """Pick which master resume to tailor from based on the posting's experience bar.

    Postings that ask for 1+ years of experience use the resume where ILS (the
    current job) leads WORK EXPERIENCE; postings with no such requirement (or an
    explicit new-grad/entry-level signal) use the plain new-grad template.
    """
    years = min_years_required(job_text)
    if years is not None and years >= 1:
        return RESUME_1YO
    return RESUME_NEW_GRAD


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------


def resume_payload(doc: Document) -> dict:
    sections = []
    for section in doc.mutable_sections:
        sections.append(
            {
                "s": section.name,
                "e": [
                    {
                        "id": entry.id,
                        "hdr": " | ".join(f for f in entry.fields if f),
                        "b": [{"id": b.id, "t": b.text} for b in entry.bullets],
                    }
                    for entry in section.entries
                ],
            }
        )
    return {
        "lines": doc.est_lines,
        "budget_note": "est_lines = ceil(len/120) per bullet + 3 per entry",
        "skills": current_skills(doc),
        "sections": sections,
    }


def current_skills(doc: Document) -> dict[str, str]:
    section = doc.section("SKILLS")
    if section is None:
        return {}
    raw = doc.source[section.start : section.end]
    return {m.group(2).strip(): m.group(4).strip() for m in SKILL_LINE_RE.finditer(raw)}


def trim_payload(doc: Document) -> list[dict]:
    return [
        {"id": b.id, "e": entry.id, "t": b.text, "n": len(entry.bullets)}
        for section in doc.mutable_sections
        for entry in section.entries
        for b in entry.bullets
    ]


# ---------------------------------------------------------------------------
# PDF compilation
# ---------------------------------------------------------------------------


def compile_pdf(tex_source: str, pdf_dest: Path | None) -> tuple[int, str]:
    """Compile LaTeX. Returns (page_count, log_tail). page_count is 0 on failure."""
    if shutil.which("pdflatex") is None:
        raise TailorError("pdflatex not found on PATH. Install MiKTeX or TeX Live.")

    with tempfile.TemporaryDirectory(prefix="resume_tailor_") as tmp:
        tmp_dir = Path(tmp)
        tmp_tex = tmp_dir / f"{OUTPUT_BASENAME}.tex"
        tmp_tex.write_text(tex_source, encoding="utf-8")

        try:
            proc = subprocess.run(
                ["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
                 f"{OUTPUT_BASENAME}.tex"],
                cwd=tmp_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            raise TailorError(f"Failed to run pdflatex: {exc}") from exc

        tmp_pdf = tmp_dir / f"{OUTPUT_BASENAME}.pdf"
        log_path = tmp_dir / f"{OUTPUT_BASENAME}.log"
        log_text = (
            log_path.read_text(encoding="utf-8", errors="replace")
            if log_path.exists()
            else (proc.stdout or "") + (proc.stderr or "")
        )

        if proc.returncode != 0 or not tmp_pdf.exists():
            return 0, "\n".join(log_text.splitlines()[-40:])

        match = PAGE_COUNT_RE.search(log_text) or PAGE_COUNT_RE.search(proc.stdout or "")
        pages = int(match.group(1)) if match else 1

        if pdf_dest is not None:
            shutil.copy2(tmp_pdf, pdf_dest)
        return pages, ""


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def build_run_slug(company: str, job_title: str) -> str:
    """Folder name for one tailoring run: <company>_<job-title>, best effort."""
    parts = []
    if company.strip():
        parts.append(slugify(company))
    if job_title.strip():
        parts.append(slugify(job_title))
    if parts:
        return "_".join(parts)
    return f"job_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def claim_run_dir(base: Path, slug: str) -> Path:
    """Atomically create a fresh, never-before-used directory under `base`.

    Directory creation (mkdir on a not-yet-existing path) is atomic at the OS
    level, so two `tailor.py` processes started at the same instant for the
    same company + title race safely here: only one wins a given candidate
    name, the other's mkdir raises FileExistsError and it retries the next
    suffix. This - not a lock file - is what makes running several instances
    of the tool in parallel safe: every run gets its own untouched folder and
    none of them ever overwrite another run's .tex/.pdf.
    """
    base.mkdir(parents=True, exist_ok=True)
    candidate = base / slug
    suffix = 2
    while True:
        try:
            candidate.mkdir(parents=False, exist_ok=False)
            return candidate
        except FileExistsError:
            candidate = base / f"{slug}-{suffix}"
            suffix += 1


def read_text(path: Path, label: str) -> str:
    if not path.exists():
        raise TailorError(f"Missing {label}: {path}")
    return path.read_text(encoding="utf-8")


def print_changes(log, doc: Document) -> None:
    print()
    print("Plan applied:")
    print(f"  {len(log.rewritten):>3} bullets enriched with a fact/keyword")
    print(f"  {len(log.kept):>3} bullets kept as-is")
    if log.dropped_bullets:
        print(f"  {len(log.dropped_bullets):>3} bullets dropped: {', '.join(log.dropped_bullets)}")
    for line in log.added_bullets:
        print(f"    + bullet {line}")
    for line in log.added_entries:
        print(f"    + entry  {line}")
    for entry_id in log.dropped_entries:
        print(f"    - entry  {entry_id}")
    for name in log.reordered:
        print(f"    ~ reordered {name}")
    if log.skills_updated:
        print("    ~ skills retuned")
    if log.facts_used:
        print(f"    ~ facts woven in: {', '.join(log.facts_used)}")
    for warning in log.warnings:
        print(f"  ! {warning}")
    print(f"  estimated {doc.est_lines} rendered lines")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Tailor a LaTeX resume to a job description using the Claude Code CLI."
    )
    parser.add_argument(
        "--resume",
        default=None,
        help="Path to master .tex resume. If omitted, auto-selected from the job "
        f"description: {RESUME_1YO} when 1+ years of experience is required, "
        f"otherwise {RESUME_NEW_GRAD}.",
    )
    parser.add_argument("--job", default=DEFAULT_JOB, help="Path to job description text file")
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="Base output directory; each run gets its own <company>_<job-title> "
        "subfolder inside it (default 'output')",
    )
    parser.add_argument(
        "--company",
        default="",
        help="Override the company name used for the output folder (skips model extraction)",
    )
    parser.add_argument(
        "--job-title",
        default="",
        help="Override the job title used for the output folder (skips model extraction)",
    )
    parser.add_argument("--bank", default=DEFAULT_BANK, help="Content bank directory")
    parser.add_argument(
        "--max-pages", type=int, default=MAX_PAGES, help="Page ceiling (default 1)"
    )
    parser.add_argument(
        "--line-budget",
        type=int,
        default=LINE_BUDGET,
        help=f"Estimated rendered lines the planner should target (default {LINE_BUDGET})",
    )
    parser.add_argument(
        "--no-bank", action="store_true", help="Ignore the content bank for this run"
    )
    parser.add_argument(
        "--no-drop-entries",
        action="store_true",
        help="Never remove an existing experience or project",
    )
    parser.add_argument(
        "--list-ids", action="store_true", help="Print resume entry/bullet ids and exit"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Write the .tex but skip PDF compilation"
    )
    parser.add_argument("--save-plan", help="Write the raw plan JSON to this path")
    args = parser.parse_args(argv)

    model = os.getenv("CLAUDE_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    plan_model = os.getenv("CLAUDE_PLAN_MODEL", DEFAULT_PLAN_MODEL).strip() or DEFAULT_PLAN_MODEL

    try:
        job_text: str | None = None
        if args.resume:
            resume_path = Path(args.resume)
        elif args.list_ids:
            # No job description needed just to list ids; companies/ids are the
            # same across templates, so the new-grad template is a fine default.
            resume_path = Path(RESUME_NEW_GRAD)
        else:
            job_text = read_text(Path(args.job), "job description").strip()
            if not job_text:
                raise TailorError(f"Job description is empty: {args.job}")
            resume_path = Path(select_base_resume(job_text))
            years = min_years_required(job_text)
            if years is not None:
                reason = f"posting asks for {years}+ year(s) of experience"
            elif _NEW_GRAD_SIGNS_RE.search(job_text):
                reason = "posting signals new grad / entry level"
            else:
                reason = "no experience requirement detected"
            print(f"Auto-selected base resume: {resume_path} ({reason})")

        resume_source = read_text(resume_path, "resume")
        doc = parse_resume(resume_source)
        baseline_lines = doc.est_lines

        if args.list_ids:
            for section in doc.mutable_sections:
                print(f"\n[{section.name}]")
                for entry in section.entries:
                    print(f"  {entry.id}   ({entry.fields[0]})")
                    for bullet in entry.bullets:
                        print(f"      {bullet.id}")
            print(
                "\nUse these entry ids in content/extra_bullets.yaml under `entry:`"
                "\nand in content/facts.yaml under `scope:`."
            )
            return 0

        line_budget = max(args.line_budget, baseline_lines)
        total_bullets = sum(len(e.bullets) for e in doc.all_entries())
        print(
            f"Resume: {len(doc.all_entries())} entries, {total_bullets} bullets, "
            f"~{baseline_lines} rendered lines (budget {line_budget})."
        )

        bank = load_bank(Path(args.bank))
        if args.no_bank:
            bank.entries.clear()
            bank.bullets.clear()
            bank.facts.clear()
        entry_ids = [e.id for e in doc.all_entries()]
        payload_bank = bank_payload(bank, entry_ids)
        if bank.is_empty:
            print("Content bank: empty (nothing to pull in).")
        else:
            print(
                f"Content bank: {len(bank.entries)} extra entries, "
                f"{len(bank.bullets)} extra bullets, {len(bank.facts)} facts."
            )

        if job_text is None:
            job_text = read_text(Path(args.job), "job description").strip()
            if not job_text:
                raise TailorError(f"Job description is empty: {args.job}")

        enrich_hints = suggest_enrichments(doc, bank, [], job_text)
        if enrich_hints:
            print(f"  Posting-matched fact hints: {len(enrich_hints)}")

        print(f"\nTailoring ({plan_model})...")
        analysis, plan = planner.analyze_and_plan(
            plan_model,
            job_text,
            resume_payload(doc),
            payload_bank,
            line_budget,
            enrich_hints=enrich_hints,
        )
        plan = planner.merge_enrich_hints(plan, enrich_hints)
        archetype = analysis.get("role_archetype") or "unspecified"
        print(f"  Role archetype: {archetype}")
        print(f"  Seniority: {analysis.get('seniority') or 'unspecified'}")
        must = analysis.get("must_have_keywords") or []
        if must:
            print(f"  Must-have keywords: {', '.join(must[:8])}")

        company = args.company.strip() or analysis.get("company", "").strip()
        job_title = args.job_title.strip() or analysis.get("job_title", "").strip()
        run_slug = build_run_slug(company, job_title)
        run_dir = claim_run_dir(Path(args.output), run_slug)
        print(f"  Output folder: {run_dir}")

        if args.save_plan:
            Path(args.save_plan).write_text(
                json.dumps({"analysis": analysis, "plan": plan}, indent=2),
                encoding="utf-8",
            )
            print(f"  Plan saved to {args.save_plan}")
        strategy = plan.get("strategy")
        if isinstance(strategy, str) and strategy.strip():
            print(f"\n  Strategy: {strategy.strip()}")

        log = apply_plan(
            doc,
            bank,
            plan,
            allow_drop_entries=not args.no_drop_entries,
            keywords=list(must) + list(analysis.get("nice_to_have_keywords") or []),
            job_text=job_text,
        )
        enforce_entry_requirements(
            doc, bank, log, allow_drop_entries=not args.no_drop_entries
        )
        print_changes(log, doc)
        claimed = plan.get("est_lines") or plan.get("estimated_total_lines")
        if isinstance(claimed, int) and abs(claimed - doc.est_lines) > 4:
            print(f"  ! planner estimated {claimed} lines but the plan measures {doc.est_lines}")

        for name in MUTABLE_SECTIONS:
            section = doc.section(name)
            if section is not None and not section.entries:
                raise TailorError(f"Section '{name}' ended up empty; aborting.")

        # The planner reliably underestimates its own line total, but our measured
        # estimate is trustworthy. Trimming from the accurate number here usually
        # saves a whole compile-and-retry cycle.
        if doc.est_lines > line_budget:
            excess = doc.est_lines - line_budget
            print(f"\nPlan is ~{excess} line(s) over budget. Pre-trimming...")
            try:
                trim = planner.trim_resume(model, trim_payload(doc), excess)
                shortened, dropped = apply_trim(doc, trim)
                print(f"  Shortened {shortened}, dropped {dropped} bullet(s); "
                      f"now ~{doc.est_lines} lines.")
            except TailorError as exc:
                print(f"  Pre-trim failed ({exc}); continuing.")

        tailored = render_document(doc)

        tex_out = run_dir / f"{OUTPUT_BASENAME}.tex"
        pdf_out = run_dir / f"{OUTPUT_BASENAME}.pdf"

        if args.dry_run:
            tex_out.write_text(tailored, encoding="utf-8")
            print(f"\nDry run: wrote {tex_out} (skipped PDF).")
            return 0

        print("\nCompiling...")
        pages, log_tail = compile_pdf(tailored, None)
        if pages == 0:
            tex_out.write_text(tailored, encoding="utf-8")
            raise TailorError(
                "PDF compilation failed. The .tex was saved at "
                f"{tex_out}\nCompiler output (tail):\n{log_tail}"
            )
        print(f"  {pages} page(s).")

        # --- fit loop: real page count is the ground truth ------------------
        for attempt in range(1, MAX_TRIM_PASSES + 1):
            if pages <= args.max_pages:
                break
            # Target the measured excess over the calibrated budget, escalating a
            # little each pass. A fixed target either overshoots (needlessly
            # cutting bullets) or crawls, depending on how far over we are.
            overflow = max(3, doc.est_lines - line_budget) + 2 * (attempt - 1)
            print(f"\nOver budget. Trim pass {attempt} (target: -{overflow} lines)...")
            try:
                trim = planner.trim_resume(model, trim_payload(doc), overflow)
            except TailorError as exc:
                print(f"  Trim pass failed ({exc}); keeping current version.")
                break
            shortened, dropped = apply_trim(doc, trim)
            if shortened == 0 and dropped == 0:
                print("  Trim pass produced no changes; stopping.")
                break
            print(f"  Shortened {shortened}, dropped {dropped} bullet(s).")
            tailored = render_document(doc)
            pages, log_tail = compile_pdf(tailored, None)
            if pages == 0:
                raise TailorError(f"Compilation failed after trimming:\n{log_tail}")
            print(f"  Now {pages} page(s).")

        pages, log_tail = compile_pdf(tailored, pdf_out)
        if pages == 0:
            tex_out.write_text(tailored, encoding="utf-8")
            raise TailorError(f"Final compilation failed:\n{log_tail}")

        tex_out.write_text(tailored, encoding="utf-8")

        if resume_path.read_text(encoding="utf-8") != resume_source:
            raise TailorError("Master resume changed during the run; aborting.")

        print()
        if pages > args.max_pages:
            print(
                f"WARNING: still {pages} pages after {MAX_TRIM_PASSES} trim passes. "
                "Consider trimming the content bank selections manually."
            )
        print("Created:")
        print(f"  {tex_out}")
        print(f"  {pdf_out}  ({pages} page)")
        return 0

    except TailorError as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
