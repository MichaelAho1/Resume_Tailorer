#!/usr/bin/env python3
"""Local CLI: tailor LaTeX resume bullets to a job description via Claude Code, then compile PDF."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_RESUME = "base_resume.tex"
DEFAULT_JOB = "job_description.txt"
DEFAULT_OUTPUT = "output"
DEFAULT_MODEL = "sonnet"
OUTPUT_BASENAME = "Michael_Aho_Resume_2026"
WORD_TOLERANCE = 0.15
CHAR_TOLERANCE = 0.20

STRUCTURAL_LATEX_PATTERNS = [
    r"\\documentclass\b",
    r"\\begin\s*\{",
    r"\\end\s*\{",
    r"\\section\b",
    r"\\subsection\b",
    r"\\resumeItem\b",
    r"\\resumeSubheading\b",
    r"\\resumeProjectHeading\b",
    r"\\resumeItemListStart\b",
    r"\\resumeItemListEnd\b",
    r"\\resumeSubHeadingListStart\b",
    r"\\resumeSubHeadingListEnd\b",
    r"\\usepackage\b",
    r"\\input\b",
    r"\\include\b",
]

SPECIAL_CHARS = {"&", "%", "$", "#", "_", "{", "}"}


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class Bullet:
    id: str
    text: str
    content_start: int
    content_end: int

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    @property
    def char_count(self) -> int:
        return len(self.text)


class TailorError(Exception):
    """User-facing fatal error."""


# ---------------------------------------------------------------------------
# Parsing / applying
# ---------------------------------------------------------------------------


def find_balanced_brace_content(source: str, open_brace_index: int) -> tuple[str, int]:
    """Given index of '{', return (inner content, index after closing '}')."""
    if open_brace_index >= len(source) or source[open_brace_index] != "{":
        raise TailorError("Expected '{' when parsing \\resumeItem.")

    depth = 0
    i = open_brace_index
    while i < len(source):
        ch = source[i]
        if ch == "\\" and i + 1 < len(source):
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[open_brace_index + 1 : i], i + 1
        i += 1
    raise TailorError("Unbalanced braces while parsing \\resumeItem{...}.")


def parse_resume_items(source: str) -> list[Bullet]:
    """Find every \\resumeItem{...} in the document body (brace-aware)."""
    # Ignore macro definitions before \begin{document}
    begin = re.search(r"\\begin\{document\}", source)
    end = re.search(r"\\end\{document\}", source)
    search_start = begin.end() if begin else 0
    search_end = end.start() if end else len(source)

    bullets: list[Bullet] = []
    pattern = re.compile(r"\\resumeItem\s*\{")
    for match in pattern.finditer(source, search_start, search_end):
        open_brace = match.end() - 1
        content, _ = find_balanced_brace_content(source, open_brace)
        content_start = open_brace + 1
        content_end = content_start + len(content)
        bullets.append(
            Bullet(
                id=f"bullet_{len(bullets) + 1}",
                text=content,
                content_start=content_start,
                content_end=content_end,
            )
        )
    return bullets


def escape_latex_specials(text: str) -> str:
    """Escape raw LaTeX specials without double-escaping already-escaped ones."""
    result: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            nxt = text[i + 1]
            if nxt.isalpha():
                # Control word (e.g. \textbf): copy the command name verbatim, then
                # pass its balanced {...} argument through untouched at the brace
                # level (those braces are syntax, not literal text) while still
                # escaping any specials inside the argument.
                j = i + 1
                while j < n and text[j].isalpha():
                    j += 1
                result.append(text[i:j])
                i = j
                if i < n and text[i] == "{":
                    inner, after = find_balanced_brace_content(text, i)
                    result.append("{")
                    result.append(escape_latex_specials(inner))
                    result.append("}")
                    i = after
                continue
            # Control symbol (e.g. \%, \&, \_, \{, \}): already escaped, keep as-is.
            result.append(text[i : i + 2])
            i += 2
            continue
        if ch in SPECIAL_CHARS:
            result.append("\\" + ch)
        else:
            result.append(ch)
        i += 1
    return "".join(result)


def prepare_replacement_text(original: str, proposed: str) -> str:
    """Escape newly introduced specials; preserve existing LaTeX in proposed text."""
    return escape_latex_specials(proposed)


def apply_replacements(source: str, bullets: list[Bullet], replacements: dict[str, str]) -> str:
    """Splice replacement texts into original LaTeX (right-to-left to keep offsets)."""
    by_id = {b.id: b for b in bullets}
    ordered = sorted(
        ((by_id[bid], text) for bid, text in replacements.items() if bid in by_id),
        key=lambda pair: pair[0].content_start,
        reverse=True,
    )
    result = source
    for bullet, text in ordered:
        safe = prepare_replacement_text(bullet.text, text)
        result = result[: bullet.content_start] + safe + result[bullet.content_end :]
    return result


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def word_count(text: str) -> int:
    return len(text.split())


def within_length(original: Bullet, new_text: str) -> tuple[bool, str]:
    ow, oc = original.word_count, original.char_count
    nw, nc = word_count(new_text), len(new_text)
    if ow == 0:
        word_ok = nw == 0
    else:
        word_ok = abs(nw - ow) / ow <= WORD_TOLERANCE
    if oc == 0:
        char_ok = nc == 0
    else:
        char_ok = abs(nc - oc) / oc <= CHAR_TOLERANCE
    if word_ok and char_ok:
        return True, ""
    reasons = []
    if not word_ok:
        reasons.append(f"words {nw} vs {ow} (±{int(WORD_TOLERANCE * 100)}%)")
    if not char_ok:
        reasons.append(f"chars {nc} vs {oc} (±{int(CHAR_TOLERANCE * 100)}%)")
    return False, "; ".join(reasons)


def has_structural_latex(text: str) -> bool:
    return any(re.search(pat, text) for pat in STRUCTURAL_LATEX_PATTERNS)


def validate_replacements(
    bullets: list[Bullet],
    replacements: dict[str, str],
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Return (accepted, length_failures, hard_errors)."""
    by_id = {b.id: b for b in bullets}
    accepted: dict[str, str] = {}
    length_failures: dict[str, str] = {}
    hard_errors: list[str] = []

    for bid, text in replacements.items():
        if bid not in by_id:
            hard_errors.append(f"Unknown bullet ID: {bid}")
            continue
        if not text or not text.strip():
            hard_errors.append(f"Empty replacement text for {bid}")
            continue
        if has_structural_latex(text):
            hard_errors.append(f"Structural LaTeX not allowed in {bid}")
            continue
        ok, _reason = within_length(by_id[bid], text)
        if not ok:
            length_failures[bid] = text
            continue
        accepted[bid] = text

    return accepted, length_failures, hard_errors


# ---------------------------------------------------------------------------
# Claude helpers
# ---------------------------------------------------------------------------


def extract_json(text: str) -> dict | list:
    """Parse JSON from model output, stripping optional markdown fences."""
    cleaned = text.strip().lstrip("\ufeff")
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
    if fence:
        cleaned = fence.group(1).strip()

    candidates = [cleaned]

    # LLMs sometimes emit invalid \' escapes inside JSON strings.
    if "\\'" in cleaned:
        candidates.append(cleaned.replace("\\'", "'"))

    # Normalize curly quotes that occasionally break parsers.
    normalized = (
        cleaned.replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
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
        # Parse first JSON value, ignore trailing junk
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
    raise TailorError(
        f"Malformed model JSON response ({detail}):\n{text[:800]}"
    )

def _extract_claude_result(stdout: str) -> str:
    """Read Claude --output-format json payload and return its result field."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise TailorError(
            "Claude returned non-JSON output; rerun with a simpler prompt or try again."
        ) from exc
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, str) or not result.strip():
        raise TailorError("Claude returned an empty result.")
    return result.strip()


def model_json(model: str, system: str, user: str, max_tokens: int = 8192) -> dict | list:
    """Call local Claude Code CLI in print mode and parse structured JSON output."""
    del max_tokens  # Claude CLI handles output limits internally.

    if shutil.which("claude") is None:
        raise TailorError(
            "Claude CLI not found on PATH. Install Claude Code and ensure `claude` works in this shell."
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
            raw = _extract_claude_result(proc.stdout)
            return extract_json(raw)

        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        detail = stderr or stdout or f"exit code {proc.returncode}"
        last_error = TailorError(detail)

        if any(token in detail.lower() for token in ("429", "rate limit", "overloaded")) and attempt < 2:
            wait_s = 8 * (attempt + 1)
            print(f"  Claude rate limited; retrying in {wait_s}s...")
            import time

            time.sleep(wait_s)
            continue

        break

    raise TailorError(f"Claude CLI error: {last_error}")


ANALYSIS_SYSTEM = """You analyze job descriptions to guide resume tailoring.
You do NOT rewrite resumes.
Return ONLY valid JSON (no markdown) with this shape:
{
  "priorities": ["..."],
  "important_keywords": ["..."],
  "responsibilities": ["..."]
}
Keep each list concise (about 5-10 items). Focus on what matters most for matching.
Only include technologies/skills that appear in the job description."""

TAILOR_SYSTEM = """You tailor resume bullet wording for a specific job.
You do NOT generate a new resume and you do NOT output LaTeX.

GOAL: Rewrite as many bullets as legitimately can be improved so the resume reads like
it was written for this job, not just lightly polished. Restructure clauses, reorder
what comes first, swap in the job description's terminology for equivalent concepts,
and shift emphasis toward what the role cares about. Aggressive rewriting is
encouraged as long as every fact stays true.

HARD RULES (never violate):
1. Never invent technologies, metrics, employers, or responsibilities the original
   bullet does not support.
2. Never claim a new capability, tool, or scope beyond what the original bullet shows.
3. Only draw on information already present in the resume bullets provided.
4. Preserve existing metrics/numbers whenever they appear.
5. Do not add, remove, combine, or split bullets.
6. Target approximately the same length: word count ±15%, character count ±20%.
7. Do not change implied seniority, or turn an accomplishment into a mere responsibility.
8. Return plain bullet text only — no \\section, \\begin, \\end, \\documentclass, or other structural LaTeX.
9. Existing LaTeX fragments in bullets (e.g. \\textbf{...}, \\%, \\&) may be preserved if still needed.

ENCOURAGED (use freely within the hard rules above):
- Reorder clauses within a bullet to lead with the part most relevant to this job.
- Replace generic phrasing with the job description's terminology when it describes
  the same underlying fact (e.g. "REST APIs" and "RESTful APIs" are the same claim;
  "built" and "engineered" are the same claim at the same seniority).
- Re-emphasize which part of the accomplishment is highlighted, as long as the
  underlying fact doesn't change.
- Rewrite most or all bullets if the job description gives you real material to work
  with — don't hold back just because a bullet's current wording is passable.
- Avoid generic corporate/AI buzzword stuffing — tailoring should read as more
  specific and relevant, not vaguer.

Only omit a bullet from your response if you genuinely have nothing useful to change
about it for this job.

Return ONLY valid JSON (no markdown):
{
  "replacements": [
    {"id": "bullet_1", "text": "..."}
  ]
}"""

CORRECTION_SYSTEM = """You fix resume bullet replacements that violate length constraints.
Do not invent facts. Keep meaning identical to the proposed text, but adjust length
to match the original word count (±15%) and character count (±20%).
Return ONLY valid JSON:
{
  "replacements": [
    {"id": "bullet_1", "text": "..."}
  ]
}"""

FACTUALITY_SYSTEM = """You check whether a proposed resume bullet introduces factual
claims that cannot be supported by the original bullet.

Fail ONLY if the proposed text adds something genuinely new: a technology, tool,
employer, metric/number, project, responsibility, or achievement that is not present
or clearly implied by the original bullet.

Pass everything else, including:
- Synonyms or closely related terminology for the same underlying fact
  (e.g. "REST APIs" -> "RESTful APIs", "built" -> "engineered", "used" -> "leveraged").
- Reordering, re-emphasis, or restructuring of the same facts.
- Minor wording changes that don't add new claims.
- Standard categorization of a named tool/technology that is already in the original
  bullet, even if the category word itself doesn't appear verbatim (e.g. a bullet
  naming "CodeDeploy canaries" may be described as "CI/CD", a bullet naming
  "Elasticsearch" may be described as "search infrastructure", a bullet naming
  "Lambda"/"API Gateway" may be described as "serverless"). This is allowed only when
  the named tool stays the original tool — do not allow this reasoning to justify
  swapping in a different tool, adding a new one, or adding a metric.

Be permissive: tailoring wording to match a job description's language is expected
and desired. Only fail a bullet if a reasonable person would say it now claims a new
tool, employer, metric, or responsibility the original didn't support.

Return ONLY valid JSON:
{
  "results": [
    {"id": "bullet_1", "passed": true, "reason": "..."}
  ]
}"""


def analyze_job(model: str, job: str, resume: str) -> dict:
    user = (
        "Job description:\n"
        f"{job}\n\n"
        "Full resume LaTeX (for context only — do not rewrite):\n"
        f"{resume}\n\n"
        "Return the analysis JSON."
    )
    data = model_json(model, ANALYSIS_SYSTEM, user, max_tokens=2048)
    if not isinstance(data, dict):
        raise TailorError("Job analysis response was not a JSON object.")
    for key in ("priorities", "important_keywords", "responsibilities"):
        if key not in data or not isinstance(data[key], list):
            data[key] = []
    return data


def tailor_bullets(
    model: str,
    job: str,
    analysis: dict,
    bullets: list[Bullet],
) -> dict[str, str]:
    bullet_payload = [
        {
            "id": b.id,
            "text": b.text,
            "word_count": b.word_count,
            "char_count": b.char_count,
        }
        for b in bullets
    ]
    user = (
        "Job description:\n"
        f"{job}\n\n"
        "Extracted priorities / keywords / responsibilities:\n"
        f"{json.dumps(analysis, indent=2)}\n\n"
        "Resume bullets to consider (preserve IDs):\n"
        f"{json.dumps(bullet_payload, indent=2)}\n\n"
        "Return JSON with only bullets that should change."
    )
    data = model_json(model, TAILOR_SYSTEM, user, max_tokens=8192)
    return _parse_replacements_payload(data)


def correct_lengths(
    model: str,
    bullets: list[Bullet],
    failing: dict[str, str],
) -> dict[str, str]:
    by_id = {b.id: b for b in bullets}
    payload = []
    for bid, text in failing.items():
        orig = by_id[bid]
        payload.append(
            {
                "id": bid,
                "original_text": orig.text,
                "original_word_count": orig.word_count,
                "original_char_count": orig.char_count,
                "proposed_text": text,
                "proposed_word_count": word_count(text),
                "proposed_char_count": len(text),
            }
        )
    user = (
        "These replacements failed length checks. Fix length only; keep facts:\n"
        f"{json.dumps(payload, indent=2)}\n\n"
        "Return corrected replacements JSON."
    )
    data = model_json(model, CORRECTION_SYSTEM, user, max_tokens=4096)
    return _parse_replacements_payload(data)


def check_factuality(
    model: str,
    bullets: list[Bullet],
    replacements: dict[str, str],
) -> dict[str, str]:
    """Return only replacements that pass factuality."""
    if not replacements:
        return {}
    by_id = {b.id: b for b in bullets}
    payload = [
        {"id": bid, "original": by_id[bid].text, "proposed": text}
        for bid, text in replacements.items()
    ]
    user = (
        "Check each pair for unsupported factual claims:\n"
        f"{json.dumps(payload, indent=2)}\n\n"
        "Return factuality results JSON."
    )
    data = model_json(model, FACTUALITY_SYSTEM, user, max_tokens=4096)
    if not isinstance(data, dict) or "results" not in data:
        raise TailorError("Factuality response missing 'results'.")

    reported_ids = {
        r.get("id") for r in data["results"] if isinstance(r, dict)
    }
    passed: dict[str, str] = {}
    for item in data["results"]:
        if not isinstance(item, dict):
            continue
        bid = item.get("id")
        if bid not in replacements:
            continue
        if item.get("passed") is True:
            passed[bid] = replacements[bid]
        else:
            reason = item.get("reason", "failed factuality check")
            print(f"  Keeping original {bid}: {reason}")

    for bid in replacements:
        if bid not in reported_ids:
            print(f"  Keeping original {bid}: missing factuality result")
    return passed


def _parse_replacements_payload(data: dict | list) -> dict[str, str]:
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("replacements", [])
    else:
        raise TailorError("Unexpected replacements JSON shape.")
    if not isinstance(items, list):
        raise TailorError("'replacements' must be a list.")
    out: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        bid = item.get("id")
        text = item.get("text")
        if isinstance(bid, str) and isinstance(text, str):
            out[bid] = text
    return out


# ---------------------------------------------------------------------------
# Output / PDF
# ---------------------------------------------------------------------------


def backup_existing_output(output_dir: Path) -> None:
    tex = output_dir / f"{OUTPUT_BASENAME}.tex"
    pdf = output_dir / f"{OUTPUT_BASENAME}.pdf"
    if not tex.exists() and not pdf.exists():
        return
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = output_dir / f"backup_{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    if tex.exists():
        shutil.move(str(tex), str(backup_dir / f"{OUTPUT_BASENAME}.tex"))
    if pdf.exists():
        shutil.move(str(pdf), str(backup_dir / f"{OUTPUT_BASENAME}.pdf"))
    print(f"Backed up previous output to {backup_dir}")


def compile_pdf(tex_path: Path, pdf_dest: Path) -> None:
    """Compile with pdflatex in a temp dir; copy PDF to pdf_dest."""
    if shutil.which("pdflatex") is None:
        raise TailorError(
            "pdflatex not found on PATH. Install MiKTeX or TeX Live and retry.\n"
            f"The tailored .tex was saved at: {tex_path}"
        )

    with tempfile.TemporaryDirectory(prefix="resume_tailor_") as tmp:
        tmp_dir = Path(tmp)
        tmp_tex = tmp_dir / f"{OUTPUT_BASENAME}.tex"
        shutil.copy2(tex_path, tmp_tex)

        cmd = [
            "pdflatex",
            "-interaction=nonstopmode",
            "-halt-on-error",
            f"{OUTPUT_BASENAME}.tex",
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=tmp_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            raise TailorError(
                f"Failed to run pdflatex: {exc}\n"
                f"The tailored .tex was saved at: {tex_path}"
            ) from exc

        tmp_pdf = tmp_dir / f"{OUTPUT_BASENAME}.pdf"
        if proc.returncode != 0 or not tmp_pdf.exists():
            log_path = tmp_dir / f"{OUTPUT_BASENAME}.log"
            if log_path.exists():
                log_text = log_path.read_text(encoding="utf-8", errors="replace")
                log_tail = "\n".join(log_text.splitlines()[-40:])
            else:
                log_tail = (proc.stdout or "")[-2000:] + "\n" + (proc.stderr or "")[-2000:]
            raise TailorError(
                "PDF compilation failed. The .tex was generated but PDF compilation failed.\n"
                f"TeX file: {tex_path}\n"
                f"Compiler output (tail):\n{log_tail}"
            )

        shutil.copy2(tmp_pdf, pdf_dest)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def read_text(path: Path, label: str) -> str:
    if not path.exists():
        raise TailorError(f"Missing {label}: {path}")
    return path.read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Tailor LaTeX resume bullets to a job description using local Claude Code CLI."
    )
    parser.add_argument("--resume", default=DEFAULT_RESUME, help="Path to master .tex resume")
    parser.add_argument("--job", default=DEFAULT_JOB, help="Path to job description text file")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Output directory")
    args = parser.parse_args(argv)

    resume_path = Path(args.resume)
    job_path = Path(args.job)
    output_dir = Path(args.output)
    model = os.getenv("CLAUDE_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL

    try:
        print("Reading resume...")
        resume_source = read_text(resume_path, "resume")
        bullets = parse_resume_items(resume_source)
        if not bullets:
            raise TailorError(
                f"No \\resumeItem{{...}} bullets found in {resume_path}."
            )
        print(f"Found {len(bullets)} resume bullets.\n")

        print("Reading job description...")
        job_text = read_text(job_path, "job description").strip()
        if not job_text:
            raise TailorError(f"Job description is empty: {job_path}")
        print()

        print("Analyzing job description with Claude...")
        analysis = analyze_job(model, job_text, resume_source)
        priorities = analysis.get("priorities") or []
        print(f"Identified {len(priorities)} key priorities.\n")

        print("Tailoring resume bullets...")
        raw_replacements = tailor_bullets(model, job_text, analysis, bullets)

        print("Validating changes...")
        accepted, length_failures, hard_errors = validate_replacements(
            bullets, raw_replacements
        )
        if hard_errors:
            raise TailorError("Validation failed:\n  - " + "\n  - ".join(hard_errors))

        if length_failures:
            print(
                f"  {len(length_failures)} bullet(s) outside length tolerance; "
                "requesting one correction pass..."
            )
            try:
                corrected = correct_lengths(model, bullets, length_failures)
                accepted2, still_failing, hard2 = validate_replacements(
                    bullets, corrected
                )
                if hard2:
                    print(
                        "  Correction pass returned invalid replacements; "
                        "keeping originals for those bullets."
                    )
                    for bid in length_failures:
                        print(f"  Keeping original {bid}: correction rejected")
                else:
                    accepted.update(accepted2)
                    for bid in still_failing:
                        print(
                            f"  Keeping original {bid}: still outside length tolerance"
                        )
                    for bid in length_failures:
                        if bid not in accepted2 and bid not in still_failing:
                            print(f"  Keeping original {bid}: omitted from correction")
            except TailorError as exc:
                print(f"  Correction pass failed ({exc}); keeping original bullets.")
                for bid in length_failures:
                    print(f"  Keeping original {bid}: correction unavailable")

        print("Running factuality checks...")
        try:
            accepted = check_factuality(model, bullets, accepted)
        except TailorError as exc:
            print(
                f"  Factuality check skipped due to Claude error ({exc}). "
                "Proceeding with length-validated replacements."
            )

        final: dict[str, str] = {}
        by_id = {b.id: b for b in bullets}
        for bid, text in accepted.items():
            if text.strip() != by_id[bid].text.strip():
                final[bid] = text

        updated = len(final)
        unchanged = len(bullets) - updated
        print(f"{updated} bullets updated.")
        print(f"{unchanged} bullets unchanged.\n")
        print("Validation passed.\n")

        output_dir.mkdir(parents=True, exist_ok=True)
        backup_existing_output(output_dir)

        tailored_source = apply_replacements(resume_source, bullets, final)
        after_bullets = parse_resume_items(tailored_source)
        if len(after_bullets) != len(bullets):
            raise TailorError(
                f"Bullet count changed after apply ({len(bullets)} -> {len(after_bullets)}). Aborting write."
            )

        tex_out = output_dir / f"{OUTPUT_BASENAME}.tex"
        pdf_out = output_dir / f"{OUTPUT_BASENAME}.pdf"

        print("Writing:")
        print(f"  {tex_out}")
        tex_out.write_text(tailored_source, encoding="utf-8")

        if resume_path.read_text(encoding="utf-8") != resume_source:
            raise TailorError("Refusing to continue: master resume changed unexpectedly.")

        print("\nCompiling PDF...")
        try:
            compile_pdf(tex_out, pdf_out)
        except TailorError as exc:
            print(f"Error: {exc}")
            return 1

        print("PDF compilation successful.\n")
        print("Created:")
        print(f"  {pdf_out}")
        return 0

    except TailorError as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
