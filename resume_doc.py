#!/usr/bin/env python3
"""Structured view over the LaTeX resume: sections -> entries -> bullets.

The old pipeline could only splice replacement text into fixed \\resumeItem spans,
which made adding or removing content impossible. This module parses the resume
into a mutable tree and renders the content sections back out, so the planner can
add, drop, and reorder entries as well as rewrite bullet text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Sections the planner is never allowed to restructure. Everything outside the
# mutable sections is copied through byte-for-byte.
PROTECTED_SECTIONS = {"EDUCATION"}
MUTABLE_SECTIONS = ("WORK EXPERIENCE", "PROJECTS")
SKILLS_SECTION = "SKILLS"

SPECIAL_CHARS = {"&", "%", "$", "#", "_"}

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

# Calibrated against this document's margins at 11pt \small by padding the resume
# until pdflatex reported a second page. Deliberately slightly pessimistic so the
# planner under-fills rather than overflows; the real ceiling is still enforced by
# compiling and reading the page count.
CHARS_PER_LINE = 120
# A heading block costs more vertical space than its two text lines because of the
# tabular and \vspace around it.
ENTRY_OVERHEAD_LINES = 3


class TailorError(Exception):
    """User-facing fatal error."""


def slugify(text: str) -> str:
    cleaned = re.sub(r"\\[a-zA-Z]+", " ", text)
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", cleaned).strip("_").lower()
    return cleaned or "entry"


@dataclass
class Bullet:
    id: str
    text: str
    source_id: str | None = None  # bank item this bullet came from, if any

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    @property
    def est_lines(self) -> int:
        return max(1, -(-len(self.text) // CHARS_PER_LINE))


@dataclass
class Entry:
    id: str
    kind: str  # "subheading" | "project"
    fields: list[str]  # exactly 4 macro arguments
    bullets: list[Bullet] = field(default_factory=list)
    source_id: str | None = None

    @property
    def title(self) -> str:
        return self.fields[0] if self.fields else self.id

    @property
    def est_lines(self) -> int:
        return ENTRY_OVERHEAD_LINES + sum(b.est_lines for b in self.bullets)


@dataclass
class Section:
    name: str
    start: int
    end: int
    raw: str
    entries: list[Entry] = field(default_factory=list)
    # Verbatim replacement for the whole section block. Used for SKILLS, which is
    # rewritten as text rather than restructured as entries.
    override: str | None = None

    @property
    def mutable(self) -> bool:
        return self.name in MUTABLE_SECTIONS

    @property
    def est_lines(self) -> int:
        return 2 + sum(e.est_lines for e in self.entries)


@dataclass
class Document:
    source: str
    sections: list[Section]

    def section(self, name: str) -> Section | None:
        for sec in self.sections:
            if sec.name == name:
                return sec
        return None

    @property
    def mutable_sections(self) -> list[Section]:
        return [s for s in self.sections if s.mutable]

    def all_entries(self) -> list[Entry]:
        return [e for s in self.mutable_sections for e in s.entries]

    def find_entry(self, entry_id: str) -> tuple[Section, Entry] | None:
        for sec in self.mutable_sections:
            for entry in sec.entries:
                if entry.id == entry_id:
                    return sec, entry
        return None

    def find_bullet(self, bullet_id: str) -> tuple[Section, Entry, Bullet] | None:
        for sec in self.mutable_sections:
            for entry in sec.entries:
                for bullet in entry.bullets:
                    if bullet.id == bullet_id:
                        return sec, entry, bullet
        return None

    @property
    def skills_est_lines(self) -> int:
        """Skills is not restructured, but retuning it can change how it wraps,
        so its cost has to be counted or the budget silently drifts."""
        section = self.section(SKILLS_SECTION)
        if section is None:
            return 0
        raw = section.override if section.override is not None else self.source[section.start : section.end]
        total = 0
        for match in re.finditer(r"\\textbf\s*\{([^{}]*)\}\s*\{:\s*([^{}]*)\}", raw):
            width = len(match.group(1)) + len(match.group(2)) + 2
            total += max(1, -(-width // CHARS_PER_LINE))
        return total

    @property
    def est_lines(self) -> int:
        """Estimated rendered lines across everything the tailor can change."""
        return sum(s.est_lines for s in self.mutable_sections) + self.skills_est_lines


# ---------------------------------------------------------------------------
# Brace-aware parsing
# ---------------------------------------------------------------------------


def find_balanced_brace_content(source: str, open_brace_index: int) -> tuple[str, int]:
    """Given index of '{', return (inner content, index after closing '}')."""
    if open_brace_index >= len(source) or source[open_brace_index] != "{":
        raise TailorError("Expected '{' while parsing LaTeX macro argument.")

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
    raise TailorError("Unbalanced braces while parsing LaTeX macro argument.")


def read_macro_args(source: str, after_macro: int, count: int) -> tuple[list[str], int]:
    """Read `count` consecutive brace groups, skipping whitespace between them."""
    args: list[str] = []
    i = after_macro
    for _ in range(count):
        while i < len(source) and source[i].isspace():
            i += 1
        if i >= len(source) or source[i] != "{":
            raise TailorError(
                f"Expected {count} arguments for resume macro; found {len(args)}."
            )
        content, i = find_balanced_brace_content(source, i)
        args.append(content.strip())
    return args, i


def parse_entries(section_raw: str, section_name: str) -> list[Entry]:
    """Parse \\resumeSubheading / \\resumeProjectHeading blocks and their bullets."""
    entries: list[Entry] = []
    heading = re.compile(r"\\(resumeSubheading|resumeProjectHeading)\b")
    matches = list(heading.finditer(section_raw))

    for index, match in enumerate(matches):
        kind = "subheading" if match.group(1) == "resumeSubheading" else "project"
        fields, cursor = read_macro_args(section_raw, match.end(), 4)

        # Bullets belonging to this heading end where the next heading begins.
        block_end = matches[index + 1].start() if index + 1 < len(matches) else len(section_raw)
        block = section_raw[cursor:block_end]

        base_id = slugify(fields[0])
        entry_id = base_id
        suffix = 2
        while any(e.id == entry_id for e in entries):
            entry_id = f"{base_id}_{suffix}"
            suffix += 1

        bullets: list[Bullet] = []
        for item in re.finditer(r"\\resumeItem\s*\{", block):
            content, _ = find_balanced_brace_content(block, item.end() - 1)
            bullets.append(
                Bullet(id=f"{entry_id}.b{len(bullets) + 1}", text=content.strip())
            )

        entries.append(Entry(id=entry_id, kind=kind, fields=fields, bullets=bullets))

    if not entries and section_name in MUTABLE_SECTIONS:
        raise TailorError(f"No entries found in section {section_name}.")
    return entries


def parse_resume(source: str) -> Document:
    begin = re.search(r"\\begin\{document\}", source)
    end = re.search(r"\\end\{document\}", source)
    body_start = begin.end() if begin else 0
    body_end = end.start() if end else len(source)

    section_starts = [
        m for m in re.finditer(r"\\section\s*\{", source) if body_start <= m.start() < body_end
    ]
    if not section_starts:
        raise TailorError("No \\section{...} blocks found in the resume.")

    sections: list[Section] = []
    for index, match in enumerate(section_starts):
        name, after_name = find_balanced_brace_content(source, match.end() - 1)
        stop = (
            section_starts[index + 1].start()
            if index + 1 < len(section_starts)
            else body_end
        )
        raw = source[after_name:stop]
        section = Section(
            name=name.strip(), start=match.start(), end=stop, raw=raw
        )
        if section.name in MUTABLE_SECTIONS:
            section.entries = parse_entries(raw, section.name)
        sections.append(section)

    missing = [n for n in MUTABLE_SECTIONS if not any(s.name == n for s in sections)]
    if missing:
        raise TailorError(
            "Resume is missing expected section(s): " + ", ".join(missing)
        )
    return Document(source=source, sections=sections)


# ---------------------------------------------------------------------------
# LaTeX escaping
# ---------------------------------------------------------------------------


def escape_latex_specials(text: str) -> str:
    """Escape raw specials without double-escaping ones already escaped."""
    result: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            nxt = text[i + 1]
            if nxt.isalpha():
                # Control word (e.g. \textbf): copy the command name verbatim, then
                # pass its balanced {...} argument through at the brace level while
                # still escaping specials inside the argument.
                j = i + 1
                while j < n and text[j].isalpha():
                    j += 1
                result.append(text[i:j])
                i = j
                while i < n and text[i] == "{":
                    inner, after = find_balanced_brace_content(text, i)
                    result.append("{")
                    result.append(escape_latex_specials(inner))
                    result.append("}")
                    i = after
                continue
            # Control symbol (e.g. \%, \&, \_): already escaped, keep as-is.
            result.append(text[i : i + 2])
            i += 2
            continue
        if ch in SPECIAL_CHARS:
            result.append("\\" + ch)
        elif ch == "{" or ch == "}":
            # Bare braces with no owning command are literal text.
            result.append("\\" + ch)
        else:
            result.append(ch)
        i += 1
    return "".join(result)


def has_structural_latex(text: str) -> bool:
    return any(re.search(pat, text) for pat in STRUCTURAL_LATEX_PATTERNS)


def remove_resume_items_by_prefix(raw: str, prefixes: list[str]) -> tuple[str, int]:
    """Delete whole \\resumeItem{...} occurrences whose content starts with one
    of the given prefixes. Used for narrow, deterministic removals from a
    section the model is never allowed to edit (e.g. EDUCATION) - this is a
    hard-coded structural rule, not a model decision, so it works directly on
    the raw LaTeX rather than the parsed entry tree.

    If removing the matched items leaves a \\resumeItemListStart/End wrapper
    with nothing inside, the empty wrapper is removed too (LaTeX's itemize
    requires at least one \\item).
    """
    removed = 0
    pieces: list[str] = []
    cursor = 0
    for match in re.finditer(r"\\resumeItem\s*\{", raw):
        open_brace = match.end() - 1
        content, after = find_balanced_brace_content(raw, open_brace)
        if any(content.strip().startswith(p) for p in prefixes):
            pieces.append(raw[cursor : match.start()])
            cursor = after
            removed += 1
    pieces.append(raw[cursor:])
    result = "".join(pieces)

    if removed:
        result = re.sub(
            r"\\resumeItemListStart\s*\\resumeItemListEnd", "", result
        )
        result = re.sub(r"[ \t]*\n[ \t]*\n+", "\n", result)
    return result, removed


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_entry(entry: Entry) -> str:
    macro = "resumeSubheading" if entry.kind == "subheading" else "resumeProjectHeading"
    lines = [f"    \\{macro}"]
    lines.append(f"      {{{entry.fields[0]}}}{{{entry.fields[1]}}}")
    lines.append(f"      {{{entry.fields[2]}}}{{{entry.fields[3]}}}")
    if entry.bullets:
        lines.append("      \\resumeItemListStart")
        for bullet in entry.bullets:
            lines.append(f"        \\resumeItem{{{escape_latex_specials(bullet.text)}}}")
        lines.append("      \\resumeItemListEnd")
    return "\n".join(lines)


def render_section(section: Section) -> str:
    parts = [f"\\section{{{section.name}}}", "  \\resumeSubHeadingListStart"]
    parts.extend(render_entry(entry) for entry in section.entries)
    parts.append("  \\resumeSubHeadingListEnd")
    return "\n".join(parts) + "\n\n"


def render_document(doc: Document) -> str:
    """Rebuild the source, replacing only mutable sections."""
    pieces: list[str] = []
    cursor = 0
    for section in doc.sections:
        pieces.append(doc.source[cursor : section.start])
        if section.override is not None:
            pieces.append(section.override)
        elif section.mutable:
            pieces.append(render_section(section))
        else:
            pieces.append(doc.source[section.start : section.end])
        cursor = section.end
    pieces.append(doc.source[cursor:])
    return "".join(pieces)
