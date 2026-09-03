#!/usr/bin/env python3
"""Validate a tailoring plan and apply it to the document tree.

Validation here is structural rather than semantic. Anything the plan adds must
cite a bank `source_id` that really exists, which is what guarantees new content
traces back to something the user actually wrote. Rewrites of existing bullets
are restricted: they must keep the original metrics and most of the original
wording, and they must actually insert an in-scope, posting-relevant fact.
Cosmetic paraphrases are discarded and the original bullet is kept.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bank import JMU_COURSEWORK_AWARDS, ContentBank, Fact
from resume_doc import (
    Bullet,
    Document,
    Entry,
    TailorError,
    has_structural_latex,
    remove_resume_items_by_prefix,
)

SKILL_LINE_RE = re.compile(r"(\\textbf\s*\{)([^{}]*)(\}\s*\{:\s*)([^{}]*)(\})")

# A rewrite must keep this fraction of the original content tokens. Inserting
# "against Snowflake" into an existing sentence easily clears it; rebuilding
# the bullet from scratch does not.
MIN_REWRITE_OVERLAP = 0.7
_NUMBER_RE = re.compile(
    r"\d[\d,]*(?:\.\d+)?(?:\+|k|m|b|x|%)?",
    re.IGNORECASE,
)
TECH_TAG_WORDS = frozenset({
    "python", "typescript", "javascript", "splunk", "snowflake", "unix", "linux",
    "bash", "perl", "docker", "kubernetes", "go", "sql", "git", "aws", "react",
    "django", "elasticsearch", "claude", "cursor",
})
_STOPWORDS = {
    "a",
    "an",
    "the",
    "to",
    "for",
    "of",
    "and",
    "in",
    "on",
    "with",
    "by",
    "that",
    "this",
    "was",
    "were",
    "using",
    "from",
    "into",
    "across",
    "over",
    "via",
    "as",
    "at",
    "or",
    "its",
    "it",
}


def _normalize_number(raw: str) -> str:
    return raw.lower().replace(",", "").replace(" ", "")


def _numbers(text: str) -> list[str]:
    return [_normalize_number(match.group(0)) for match in _NUMBER_RE.finditer(text)]


def _content_tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+(?:[+#]+)?", text.lower())
    return [word for word in words if word not in _STOPWORDS and len(word) > 1]


def _overlap_ratio(original: str, new: str) -> float:
    original_tokens = _content_tokens(original)
    if not original_tokens:
        return 1.0
    new_tokens = set(_content_tokens(new))
    kept = sum(1 for token in original_tokens if token in new_tokens)
    return kept / len(original_tokens)


def _term_in_text(term: str, text: str) -> bool:
    term = term.lower().strip()
    if not term:
        return False
    haystack = text.lower()
    if len(term) <= 3:
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", haystack) is not None
    return term in haystack


def _fact_usable_in_entry(fact: Fact, entry_id: str) -> bool:
    if fact.scope == "skills":
        return False
    if fact.scope == "global":
        return True
    return fact.scope == entry_id


def _fact_matches_posting(fact: Fact, keywords: list[str], job_text: str) -> bool:
    haystack = f"{job_text} {' '.join(keywords)}".lower()
    if not haystack.strip():
        return True
    for tag in fact.tags:
        tag_l = tag.lower().strip()
        if len(tag_l) >= 3 and tag_l in haystack:
            return True
    return False


def _fact_insert_terms(fact: Fact) -> list[str]:
    """Distinctive terms a rewrite must add to count as using this fact."""
    tag_parts: set[str] = set()
    for tag in fact.tags:
        cleaned = tag.lower().strip()
        tag_parts.add(cleaned)
        tag_parts.update(part for part in re.split(r"[-_/]", cleaned) if len(part) >= 3)

    terms: list[str] = []
    seen: set[str] = set()
    for word in re.findall(r"[A-Z][A-Za-z0-9+#/.]*", fact.text):
        cleaned = word.lower().strip()
        if len(cleaned) < 3 or cleaned in _STOPWORDS or cleaned not in tag_parts:
            continue
        if cleaned not in seen:
            seen.add(cleaned)
            terms.append(cleaned)
    for tag in fact.tags:
        cleaned = tag.lower().strip()
        if cleaned in TECH_TAG_WORDS and cleaned not in seen:
            seen.add(cleaned)
            terms.append(cleaned)
    return terms


def _fact_inserted(original: str, new: str, fact: Fact) -> bool:
    """True when a distinctive term from the fact appears in `new` but not `original`."""
    return any(
        _term_in_text(term, new) and not _term_in_text(term, original)
        for term in _fact_insert_terms(fact)
    )


def _posting_relevant_facts(
    bank: ContentBank, entry_id: str, keywords: list[str], job_text: str
) -> list[Fact]:
    return [
        fact
        for fact in bank.facts.values()
        if _fact_usable_in_entry(fact, entry_id)
        and _fact_matches_posting(fact, keywords, job_text)
    ]


def justified_enrichment(
    original: str,
    new: str,
    entry_id: str,
    bank: ContentBank,
    keywords: list[str],
    job_text: str,
) -> tuple[bool, str]:
    """Return (ok, reason) for whether a rewrite is a surgical fact insert."""
    if new.strip() == original.strip():
        return False, "identical to original"

    original_numbers = _numbers(original)
    new_numbers = _numbers(new)
    missing = [number for number in original_numbers if number not in new_numbers]
    if missing:
        return False, f"dropped or changed metric(s) {', '.join(missing)}"

    if _overlap_ratio(original, new) < MIN_REWRITE_OVERLAP:
        return False, "rewrote too much of the original wording"

    relevant_facts = _posting_relevant_facts(bank, entry_id, keywords, job_text)
    inserted_facts = [
        fact for fact in relevant_facts if _fact_inserted(original, new, fact)
    ]
    if not inserted_facts:
        return False, "no in-scope posting-relevant fact was added"

    allowed_numbers = set(original_numbers)
    for fact in inserted_facts:
        allowed_numbers.update(_numbers(fact.text))
    invented = [number for number in new_numbers if number not in allowed_numbers]
    if invented:
        return False, f"invented metric(s) {', '.join(invented)}"
    return True, ""


def _fact_already_present(original: str, fact: Fact) -> bool:
    terms = _fact_insert_terms(fact)
    if not terms:
        return False
    return all(_term_in_text(term, original) for term in terms)


def propose_fact_insert(original: str, fact: Fact) -> str | None:
    """Insert a fact into a bullet with minimal wording change. None if redundant."""
    if _fact_already_present(original, fact):
        return None
    orig_l = original.lower()

    if fact.id == "co_splunk":
        if "splunk" in orig_l:
            return None
        updated = re.sub(
            r"(CodeDeploy canaries)(,\s*)",
            r"\1 and Splunk alerts\2",
            original,
            count=1,
            flags=re.I,
        )
        return updated if updated != original else None

    if fact.id == "co_languages":
        has_py = "python" in orig_l
        has_ts = "typescript" in orig_l
        if has_py and has_ts:
            return None
        if has_ts and not has_py:
            return original.replace("TypeScript", "Python/TypeScript")
        updated = re.sub(
            r"(AWS Lambda service)",
            r"\1 (Python/TypeScript)",
            original,
            count=1,
            flags=re.I,
        )
        if updated != original:
            return updated
        updated = re.sub(
            r"(\d+ lambdas)",
            r"\1 (Python)",
            original,
            count=1,
            flags=re.I,
        )
        return updated if updated != original else None

    if fact.id == "cross_screen_snowflake":
        if "snowflake" in orig_l:
            return None
        updated = re.sub(
            r"(Python/SQL data pipeline)(,\s*)",
            r"\1 against Snowflake\2",
            original,
            count=1,
            flags=re.I,
        )
        return updated if updated != original else None

    terms = [t for t in _fact_insert_terms(fact) if not _term_in_text(t, original)]
    if not terms:
        return None
    primary = terms[0]
    label = {"splunk": "Splunk", "python": "Python", "typescript": "TypeScript"}.get(
        primary, primary.title()
    )
    if "," in original:
        cut = original.find(",")
        return f"{original[:cut]} using {label}{original[cut:]}"
    return f"{original} using {label}"


def suggest_enrichments(
    doc: Document,
    bank: ContentBank,
    keywords: list[str],
    job_text: str = "",
) -> list[dict[str, str]]:
    """Posting-relevant fact inserts the planner should strongly consider."""
    suggestions: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for section in doc.mutable_sections:
        for entry in section.entries:
            for bullet in entry.bullets:
                for fact in _posting_relevant_facts(bank, entry.id, keywords, job_text):
                    if not any(str(t).lower() in TECH_TAG_WORDS for t in fact.tags):
                        continue
                    if _fact_already_present(bullet.text, fact):
                        continue
                    if fact.bullet_hint:
                        hint_tokens = [
                            w
                            for w in re.findall(r"[a-z0-9]+", fact.bullet_hint.lower())
                            if len(w) > 4 and w not in _STOPWORDS
                        ]
                        bullet_l = bullet.text.lower()
                        if hint_tokens and not any(t in bullet_l for t in hint_tokens):
                            continue
                    key = (bullet.id, fact.id)
                    if key in seen:
                        continue
                    seen.add(key)
                    suggestions.append({"id": bullet.id, "fact": fact.id})
    return suggestions


@dataclass
class ChangeLog:
    rewritten: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    dropped_bullets: list[str] = field(default_factory=list)
    added_bullets: list[str] = field(default_factory=list)
    added_entries: list[str] = field(default_factory=list)
    dropped_entries: list[str] = field(default_factory=list)
    reordered: list[str] = field(default_factory=list)
    skills_updated: bool = False
    facts_used: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _check_text(text: object, where: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise TailorError(f"{where}: missing or empty text.")
    cleaned = text.strip()
    if has_structural_latex(cleaned):
        raise TailorError(f"{where}: structural LaTeX is not allowed in bullet text.")
    return cleaned


def normalize_label(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", label.lower())


def split_skill_tokens(value: str) -> list[str]:
    """Split a skills line on commas, ignoring commas inside parentheses."""
    tokens: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            tokens.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    tokens.append("".join(current).strip())
    return [t for t in tokens if t]


def unsupported_skill_tokens(skills: dict, corpus: str) -> list[str]:
    """Skill tokens that appear nowhere in the resume, bank, or facts.

    The skills lines are the one place the model writes freely, so this is where
    job-description keywords can leak in as claims. A token counts as supported
    if it, or any parenthesized part of it, shows up in the corpus.
    """
    haystack = re.sub(r"[^a-z0-9+#/]+", " ", corpus.lower())
    unsupported: list[str] = []
    for value in skills.values():
        for token in split_skill_tokens(str(value)):
            parts = [token, *re.findall(r"[^(),]+", token)]
            if not any(
                re.sub(r"[^a-z0-9+#/]+", " ", p.lower()).strip() in haystack
                for p in parts
                if p.strip()
            ):
                unsupported.append(token)
    return unsupported


def apply_skills(doc: Document, skills: dict) -> bool:
    section = doc.section("SKILLS")
    if section is None or not isinstance(skills, dict) or not skills:
        return False

    wanted = {normalize_label(str(k)): str(v).strip() for k, v in skills.items() if str(v).strip()}
    raw = doc.source[section.start : section.end]
    matched: list[str] = []

    def substitute(match: re.Match) -> str:
        key = normalize_label(match.group(2))
        if key in wanted:
            matched.append(key)
            return f"{match.group(1)}{match.group(2)}{match.group(3)}{wanted[key]}{match.group(5)}"
        return match.group(0)

    updated = SKILL_LINE_RE.sub(substitute, raw)
    if not matched or updated == raw:
        return False
    section.override = updated
    return True


def _keep_or_enrich(
    original: str,
    proposed: str,
    entry_id: str,
    bank: ContentBank,
    keywords: list[str],
    job_text: str,
    where: str,
    log: ChangeLog,
) -> str:
    """Use `proposed` only if it is a surgical fact insert; otherwise keep original."""
    if proposed.strip() == original.strip():
        return original
    ok, reason = justified_enrichment(
        original, proposed, entry_id, bank, keywords, job_text
    )
    if ok:
        return proposed
    log.warnings.append(f"Rejected rewrite of {where} ({reason}); kept original wording.")
    return original


def _plan_source_id(item: dict, *keys: str) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _collect_drop_entries(plan: dict) -> set[str]:
    doomed: set[str] = set()
    for item in plan.get("drop_entries") or plan.get("drops") or []:
        if isinstance(item, str) and item.strip():
            doomed.add(item.strip())
        elif isinstance(item, dict):
            entry_id = _plan_source_id(item, "id", "entry_id", "entry")
            if entry_id:
                doomed.add(entry_id)
    return doomed


def apply_plan(
    doc: Document,
    bank: ContentBank,
    plan: dict,
    allow_drop_entries: bool = True,
    keywords: list[str] | None = None,
    job_text: str = "",
) -> ChangeLog:
    log = ChangeLog()
    keywords = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    job_text = job_text or ""

    all_bullet_ids = {b.id for e in doc.all_entries() for b in e.bullets}
    bullet_text = {
        b.id: b.text
        for e in doc.all_entries()
        for b in e.bullets
    }
    rewrites: dict[str, str] = {}
    to_drop: set[str] = set()
    touched: set[str] = set()

    # --- sparse: enrich -------------------------------------------------------
    for item in plan.get("enrich") or []:
        if not isinstance(item, dict):
            continue
        bid = _plan_source_id(item, "id")
        if not bid or bid not in all_bullet_ids:
            log.warnings.append(f"enrich referenced unknown bullet id '{bid}'; ignored.")
            continue
        fact_id = _plan_source_id(item, "fact", "fact_id")
        original = bullet_text[bid]
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            proposed = text.strip()
        elif fact_id and fact_id in bank.facts:
            proposed = propose_fact_insert(original, bank.facts[fact_id])
            if not proposed:
                continue
        else:
            log.warnings.append(f"enrich on {bid} missing fact or text; kept original.")
            continue
        rewrites[bid] = _check_text(proposed, f"enrich of {bid}")
        touched.add(bid)
        if fact_id:
            if fact_id in bank.facts:
                if fact_id not in log.facts_used:
                    log.facts_used.append(fact_id)
            else:
                log.warnings.append(
                    f"enrich on {bid} cited unknown fact '{fact_id}' - verify wording."
                )

    # --- sparse: drop_bullets -------------------------------------------------
    for bid in plan.get("drop_bullets") or []:
        if isinstance(bid, str) and bid in all_bullet_ids:
            to_drop.add(bid)
            touched.add(bid)
        elif isinstance(bid, str):
            log.warnings.append(f"drop_bullets referenced unknown id '{bid}'; ignored.")

    # --- legacy: bullet_decisions ---------------------------------------------
    for item in plan.get("bullet_decisions") or []:
        if not isinstance(item, dict):
            continue
        bid = item.get("id")
        if not isinstance(bid, str) or bid not in all_bullet_ids:
            log.warnings.append(f"Plan referenced unknown bullet id '{bid}'; ignored.")
            continue
        touched.add(bid)
        action = str(item.get("action") or "keep").lower()
        if action in {"rewrite", "enrich"}:
            rewrites[bid] = _check_text(item.get("text"), f"rewrite of {bid}")
        elif action == "drop":
            to_drop.add(bid)
        else:
            log.kept.append(bid)

    for bid in sorted(all_bullet_ids - touched):
        log.kept.append(bid)

    doomed_entries = _collect_drop_entries(plan) if allow_drop_entries else set()

    # --- 1. rewrites (anchors for `after` are still intact) -----------------
    for section in doc.mutable_sections:
        for entry in section.entries:
            for bullet in entry.bullets:
                new_text = rewrites.get(bullet.id)
                if not new_text:
                    continue
                applied = _keep_or_enrich(
                    bullet.text,
                    new_text,
                    entry.id,
                    bank,
                    keywords,
                    job_text,
                    bullet.id,
                    log,
                )
                if applied != bullet.text:
                    bullet.text = applied
                    log.rewritten.append(bullet.id)
                else:
                    log.kept.append(bullet.id)

    # --- 2. add bullets to existing entries ---------------------------------
    for item in plan.get("add_bullets") or []:
        if not isinstance(item, dict):
            continue
        source_id = _plan_source_id(item, "id", "source_id")
        if not source_id or source_id not in bank.bullets:
            raise TailorError(
                f"add_bullets cited unknown bank id '{source_id}'. "
                "Added content must come from the content bank."
            )
        entry_id = item.get("entry") or bank.bullets[source_id].entry
        found = doc.find_entry(str(entry_id))
        if found is None:
            log.warnings.append(
                f"add_bullets targeted unknown entry '{entry_id}'; skipped {source_id}."
            )
            continue
        _, entry = found
        bank_text = bank.bullets[source_id].text
        proposed = _check_text(
            item.get("text") or bank_text, f"added bullet {source_id}"
        )
        text = _keep_or_enrich(
            bank_text,
            proposed,
            entry.id,
            bank,
            keywords,
            job_text,
            source_id,
            log,
        )
        new_bullet = Bullet(
            id=f"{entry.id}.add_{source_id}", text=text, source_id=source_id
        )
        anchor = item.get("after")
        index = len(entry.bullets)
        if isinstance(anchor, str):
            for position, existing in enumerate(entry.bullets):
                if existing.id == anchor:
                    index = position + 1
                    break
        entry.bullets.insert(index, new_bullet)
        log.added_bullets.append(f"{entry.id} <- {source_id}")

    # --- 3. drop bullets ----------------------------------------------------
    for section in doc.mutable_sections:
        for entry in section.entries:
            survivors = [b for b in entry.bullets if b.id not in to_drop]
            if not survivors and entry.bullets and entry.id not in doomed_entries:
                log.warnings.append(
                    f"Refused to drop every bullet from '{entry.id}'; kept the first."
                )
                survivors = entry.bullets[:1]
            dropped = [b.id for b in entry.bullets if b not in survivors]
            log.dropped_bullets.extend(dropped)
            entry.bullets = survivors

    # --- 4. drop entries ----------------------------------------------------
    if allow_drop_entries:
        for section in doc.mutable_sections:
            survivors = [e for e in section.entries if e.id not in doomed_entries]
            if not survivors and section.entries:
                log.warnings.append(
                    f"Refused to empty section '{section.name}'; kept the first entry."
                )
                survivors = section.entries[:1]
            log.dropped_entries.extend(
                e.id for e in section.entries if e not in survivors
            )
            section.entries = survivors

    # --- 5. add entries from the bank ---------------------------------------
    for item in plan.get("add_entries") or []:
        if not isinstance(item, dict):
            continue
        source_id = _plan_source_id(item, "id", "source_id")
        if not source_id or source_id not in bank.entries:
            raise TailorError(
                f"add_entries cited unknown bank source_id '{source_id}'. "
                "Added experiences must come from the content bank."
            )
        bank_entry = bank.entries[source_id]
        section_name = str(
            item.get("section")
            or ("WORK EXPERIENCE" if bank_entry.kind == "subheading" else "PROJECTS")
        )
        section = doc.section(section_name)
        if section is None or not section.mutable:
            log.warnings.append(
                f"add_entries targeted unknown section '{section_name}'; skipped {source_id}."
            )
            continue
        if doc.find_entry(source_id) is not None:
            log.warnings.append(f"Entry '{source_id}' already present; skipped.")
            continue

        allowed = {b.id: b for b in bank_entry.bullets}
        chosen: list[Bullet] = []
        for raw_bullet in item.get("bullets") or []:
            if isinstance(raw_bullet, str):
                raw_bullet = {"id": raw_bullet}
            if not isinstance(raw_bullet, dict):
                continue
            bullet_source = _plan_source_id(raw_bullet, "id", "source_id")
            if not bullet_source or bullet_source not in allowed:
                raise TailorError(
                    f"add_entries['{source_id}'] cited bullet '{bullet_source}' "
                    f"which does not belong to that bank entry."
                )
            bank_text = allowed[bullet_source].text
            proposed = _check_text(
                raw_bullet.get("text") or bank_text,
                f"added bullet {bullet_source}",
            )
            text = _keep_or_enrich(
                bank_text,
                proposed,
                source_id,
                bank,
                keywords,
                job_text,
                bullet_source,
                log,
            )
            chosen.append(
                Bullet(
                    id=f"{source_id}.add_{bullet_source}",
                    text=text,
                    source_id=bullet_source,
                )
            )
        if not chosen:
            chosen = [
                Bullet(id=f"{source_id}.add_{b.id}", text=b.text, source_id=b.id)
                for b in bank_entry.bullets[:3]
            ]

        new_entry = Entry(
            id=source_id,
            kind=bank_entry.kind,
            fields=list(bank_entry.fields),
            bullets=chosen,
            source_id=source_id,
        )
        position = item.get("position")
        index = position if isinstance(position, int) and 0 <= position <= len(section.entries) else len(section.entries)
        section.entries.insert(index, new_entry)
        log.added_entries.append(f"{section.name} <- {source_id} ({len(chosen)} bullets)")

    # --- 6. reorder ---------------------------------------------------------
    for item in plan.get("reorder") or []:
        if not isinstance(item, dict):
            continue
        section = doc.section(str(item.get("section") or ""))
        if section is None or not section.mutable:
            continue
        order = item.get("order")
        if not isinstance(order, list):
            continue
        by_id = {e.id: e for e in section.entries}
        ordered = [by_id[eid] for eid in order if isinstance(eid, str) and eid in by_id]
        # Anything the model forgot keeps its relative position at the end.
        ordered.extend(e for e in section.entries if e not in ordered)
        if [e.id for e in ordered] != [e.id for e in section.entries]:
            section.entries = ordered
            log.reordered.append(section.name)

    # --- 7. skills ----------------------------------------------------------
    skills = plan.get("skills") or {}
    if isinstance(skills, dict) and skills:
        corpus = "\n".join(
            [doc.source]
            + [b.text for b in bank.bullets.values()]
            + [f.text for f in bank.facts.values()]
            + [" ".join(e.fields) for e in bank.entries.values()]
        )
        for token in unsupported_skill_tokens(skills, corpus):
            log.warnings.append(
                f"Skills claims '{token}', which appears nowhere in your resume, "
                "bank, or facts - verify before sending."
            )
    log.skills_updated = apply_skills(doc, skills)

    # Report any legacy facts_used field not already captured from enrich.
    for fact_id in plan.get("facts_used") or []:
        if not isinstance(fact_id, str):
            continue
        if fact_id in bank.facts:
            if fact_id not in log.facts_used:
                log.facts_used.append(fact_id)
        else:
            log.warnings.append(
                f"Plan claimed fact '{fact_id}' which is not in facts.yaml - "
                "check the affected bullets for invented detail."
            )

    # --- 8. renumber bullet ids so later passes have stable handles ---------
    for section in doc.mutable_sections:
        for entry in section.entries:
            for position, bullet in enumerate(entry.bullets, start=1):
                bullet.id = f"{entry.id}.b{position}"

    return log


def _drop_target_present(doc: Document, target: str) -> bool:
    if target == JMU_COURSEWORK_AWARDS:
        education = doc.section("EDUCATION")
        if education is None:
            return False
        raw = education.override if education.override is not None else education.raw
        return "\\textbf{Relevant Coursework}" in raw or "\\textbf{Awards}" in raw
    return doc.find_entry(target) is not None


def _remove_drop_target(doc: Document, target: str, log: ChangeLog, why: str) -> bool:
    if target == JMU_COURSEWORK_AWARDS:
        education = doc.section("EDUCATION")
        if education is None:
            return False
        raw = education.override if education.override is not None else education.raw
        new_raw, removed = remove_resume_items_by_prefix(
            raw, ["\\textbf{Relevant Coursework}", "\\textbf{Awards}"]
        )
        if not removed:
            return False
        education.override = new_raw
        log.dropped_entries.append(f"EDUCATION <- Relevant Coursework + Awards ({why})")
        return True

    found = doc.find_entry(target)
    if found is None:
        return False
    section, _ = found
    section.entries = [e for e in section.entries if e.id != target]
    log.dropped_entries.append(f"{section.name} <- {target} ({why})")
    return True


def enforce_entry_requirements(
    doc: Document, bank: ContentBank, log: ChangeLog, allow_drop_entries: bool = True
) -> None:
    """Force the tradeoffs declared by bank entries' `requires_drop`.

    This is a counting rule, not a per-entry one: each triggering bank entry on
    the final resume demands exactly one removal from a shared, preference-
    ordered target pool. What counts is how many pool targets are missing in
    total, regardless of why - if the model already dropped Fantasy Stock
    League for its own unrelated reasons, that satisfies one entry's
    requirement outright, and a second triggering entry only then reaches for
    the next preference. This has to be counted rather than checked "was this
    exact target removed by this exact rule", or a coincidental drop the model
    made for other reasons would look like the requirement was unmet and cause
    an extra, unwanted cut.
    """
    triggered = [
        bank.entries[entry.source_id]
        for section in doc.mutable_sections
        for entry in section.entries
        if entry.source_id in bank.entries and bank.entries[entry.source_id].requires_drop
    ]
    if not triggered:
        return

    ordered_targets: list[str] = []
    for bank_entry in triggered:
        for target in bank_entry.requires_drop:
            if target not in ordered_targets:
                ordered_targets.append(target)

    required = len(triggered)
    already_gone = sum(1 for t in ordered_targets if not _drop_target_present(doc, t))
    need = required - already_gone
    if need <= 0:
        return

    if not allow_drop_entries:
        log.warnings.append(
            f"{required} added bank entr{'y' if required == 1 else 'ies'} normally "
            f"require dropping {need} more item(s) from {ordered_targets}, but "
            "--no-drop-entries is set, so nothing more was removed. Resume is "
            "likely over-full."
        )
        return

    why = f"required by {', '.join(e.id for e in triggered)}"
    for target in ordered_targets:
        if need <= 0:
            break
        if _remove_drop_target(doc, target, log, why):
            need -= 1

    if need > 0:
        log.warnings.append(
            f"Could not fully satisfy the trade-off for {', '.join(e.id for e in triggered)}: "
            f"ran out of drop targets in {ordered_targets}. Resume may be over-full."
        )


def apply_trim(doc: Document, trim: dict) -> tuple[int, int]:
    """Apply a trim pass. Returns (shortened, dropped)."""
    shorten = {
        item["id"]: item["text"]
        for item in (trim.get("shorten") or [])
        if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("text"), str)
    }
    drop: set[str] = set()
    for item in trim.get("drop") or []:
        if isinstance(item, str):
            drop.add(item)
        elif isinstance(item, dict) and isinstance(item.get("id"), str):
            drop.add(item["id"])

    shortened = 0
    dropped = 0
    for section in doc.mutable_sections:
        for entry in section.entries:
            for bullet in entry.bullets:
                new_text = shorten.get(bullet.id)
                if new_text and new_text.strip() and not has_structural_latex(new_text):
                    if len(new_text.strip()) < len(bullet.text):
                        bullet.text = new_text.strip()
                        shortened += 1
            survivors = [b for b in entry.bullets if b.id not in drop]
            if not survivors:
                survivors = entry.bullets[:1]
            dropped += len(entry.bullets) - len(survivors)
            entry.bullets = survivors

    for section in doc.mutable_sections:
        for entry in section.entries:
            for position, bullet in enumerate(entry.bullets, start=1):
                bullet.id = f"{entry.id}.b{position}"

    return shortened, dropped
