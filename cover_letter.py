#!/usr/bin/env python3
"""Render the cover letter LaTeX template with per-job content.

Only the opening paragraph and the closing company mention are model-written;
everything else (contact header, body paragraphs about actual experience,
sign-off) is fixed text from base_cover_letter.tex.
"""

from __future__ import annotations

from datetime import datetime

from resume_doc import TailorError, escape_latex_specials

REQUIRED_TOKENS = ("%%DATE%%", "%%GREETING%%", "%%OPENING%%", "%%CLOSING_MENTION%%")


def format_date(when: datetime | None = None) -> str:
    when = when or datetime.now()
    return f"{when:%B} {when.day}, {when:%Y}"


def format_greeting(company: str) -> str:
    company = company.strip()
    if not company:
        return "Dear Hiring Team,"
    return f"Dear {escape_latex_specials(company)} Hiring Team,"


def render_cover_letter(
    template: str,
    company: str,
    opening_paragraph: str,
    closing_mention: str,
    when: datetime | None = None,
) -> str:
    for token in REQUIRED_TOKENS:
        if token not in template:
            raise TailorError(f"Cover letter template missing placeholder {token}.")

    out = template
    out = out.replace("%%DATE%%", format_date(when))
    out = out.replace("%%GREETING%%", format_greeting(company))
    out = out.replace("%%OPENING%%", escape_latex_specials(opening_paragraph.strip()))
    out = out.replace("%%CLOSING_MENTION%%", escape_latex_specials(closing_mention.strip()))
    return out
