#!/usr/bin/env python3
"""Loader for the YAML content bank: off-resume experiences, projects, and bullets.

Every bank item carries a stable id. The planner may only add content by citing
one of those ids, which is what keeps added material traceable to something you
actually wrote now that the factuality pass is gone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from resume_doc import TailorError

DEFAULT_BANK_DIR = "content"
EXPERIENCE_FILE = "experience_bank.yaml"
EXTRA_BULLETS_FILE = "extra_bullets.yaml"
FACTS_FILE = "facts.yaml"

GLOBAL_SCOPES = {"global", "skills"}

# Sentinel `requires_drop` target: JMU's "Relevant Coursework" and "Awards"
# bullets, removed together as one unit. EDUCATION is otherwise off-limits to
# the model entirely - this is the one deterministic, code-enforced exception.
JMU_COURSEWORK_AWARDS = "jmu_coursework_and_awards"
KNOWN_DROP_SENTINELS = {JMU_COURSEWORK_AWARDS}


@dataclass
class BankBullet:
    id: str
    text: str
    entry: str | None = None  # existing resume entry id this belongs to
    tags: list[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class BankEntry:
    id: str
    kind: str  # "subheading" | "project"
    fields: list[str]
    bullets: list[BankBullet]
    tags: list[str] = field(default_factory=list)
    notes: str = ""
    # Ordered preference list of what must be removed from the resume if this
    # entry is added. Consumed one target per triggering entry, in order,
    # skipping any target already gone - see apply_plan.enforce_entry_requirements.
    requires_drop: list[str] = field(default_factory=list)


@dataclass
class Fact:
    """A true detail too small to be its own bullet, woven into one instead."""

    id: str
    text: str
    scope: str  # entry id, "global", or "skills"
    tags: list[str] = field(default_factory=list)
    bullet_hint: str = ""


@dataclass
class ContentBank:
    entries: dict[str, BankEntry] = field(default_factory=dict)
    bullets: dict[str, BankBullet] = field(default_factory=dict)
    facts: dict[str, Fact] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.entries and not self.bullets and not self.facts

    def bullets_for(self, entry_id: str) -> list[BankBullet]:
        return [b for b in self.bullets.values() if b.entry == entry_id]

    def unattached_bullets(self) -> list[BankBullet]:
        return [b for b in self.bullets.values() if not b.entry]


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TailorError(f"Could not parse {path}:\n{exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise TailorError(f"{path} must contain a YAML mapping at the top level.")
    return data


def _require(item: dict, key: str, where: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TailorError(f"{where} is missing required field '{key}'.")
    return value.strip()


def _parse_requires_drop(raw: object, where: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise TailorError(f"{where}: 'requires_drop' must be a list.")
    return [str(item).strip() for item in raw if str(item).strip()]


def _parse_bullets(
    raw: object, where: str, default_entry: str | None, seen: set[str]
) -> list[BankBullet]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise TailorError(f"{where}: 'bullets' must be a list.")
    out: list[BankBullet] = []
    for index, item in enumerate(raw):
        # Allow a bare string as shorthand; synthesize an id from position.
        if isinstance(item, str):
            item = {"id": f"{default_entry or 'bullet'}_{index + 1}", "text": item}
        if not isinstance(item, dict):
            raise TailorError(f"{where}: bullet #{index + 1} must be a mapping or string.")
        bullet_id = _require(item, "id", f"{where} bullet #{index + 1}")
        if bullet_id in seen:
            raise TailorError(f"Duplicate bank bullet id '{bullet_id}'.")
        seen.add(bullet_id)
        tags = item.get("tags") or []
        if not isinstance(tags, list):
            raise TailorError(f"{where} bullet '{bullet_id}': 'tags' must be a list.")
        out.append(
            BankBullet(
                id=bullet_id,
                text=_require(item, "text", f"{where} bullet '{bullet_id}'"),
                entry=item.get("entry", default_entry),
                tags=[str(t) for t in tags],
                notes=str(item.get("notes") or ""),
            )
        )
    return out


def load_bank(bank_dir: Path) -> ContentBank:
    bank = ContentBank()
    seen_bullets: set[str] = set()

    exp_data = _load_yaml(bank_dir / EXPERIENCE_FILE)

    for raw in exp_data.get("experiences") or []:
        if not isinstance(raw, dict):
            raise TailorError("Each item under 'experiences' must be a mapping.")
        entry_id = _require(raw, "id", "experience")
        if entry_id in bank.entries:
            raise TailorError(f"Duplicate bank entry id '{entry_id}'.")
        where = f"experience '{entry_id}'"
        bank.entries[entry_id] = BankEntry(
            id=entry_id,
            kind="subheading",
            fields=[
                _require(raw, "company", where),
                _require(raw, "dates", where),
                _require(raw, "title", where),
                _require(raw, "location", where),
            ],
            bullets=_parse_bullets(raw.get("bullets"), where, entry_id, seen_bullets),
            tags=[str(t) for t in (raw.get("tags") or [])],
            notes=str(raw.get("notes") or ""),
            requires_drop=_parse_requires_drop(raw.get("requires_drop"), where),
        )

    for raw in exp_data.get("projects") or []:
        if not isinstance(raw, dict):
            raise TailorError("Each item under 'projects' must be a mapping.")
        entry_id = _require(raw, "id", "project")
        if entry_id in bank.entries:
            raise TailorError(f"Duplicate bank entry id '{entry_id}'.")
        where = f"project '{entry_id}'"
        link = str(raw.get("link") or "").strip()
        bank.entries[entry_id] = BankEntry(
            id=entry_id,
            kind="project",
            fields=[
                _require(raw, "name", where),
                _require(raw, "dates", where),
                _require(raw, "tech", where),
                f"\\href{{{link}}}{{Source Code}}" if link else "",
            ],
            bullets=_parse_bullets(raw.get("bullets"), where, entry_id, seen_bullets),
            tags=[str(t) for t in (raw.get("tags") or [])],
            notes=str(raw.get("notes") or ""),
            requires_drop=_parse_requires_drop(raw.get("requires_drop"), where),
        )

    # Bullets nested under a bank entry are reachable through that entry; the
    # flat file holds bullets that attach to entries already on the resume.
    for entry in bank.entries.values():
        for bullet in entry.bullets:
            bank.bullets[bullet.id] = bullet

    extra_data = _load_yaml(bank_dir / EXTRA_BULLETS_FILE)
    for bullet in _parse_bullets(
        extra_data.get("bullets"), EXTRA_BULLETS_FILE, None, seen_bullets
    ):
        bank.bullets[bullet.id] = bullet

    facts_data = _load_yaml(bank_dir / FACTS_FILE)
    for index, raw in enumerate(facts_data.get("facts") or []):
        if not isinstance(raw, dict):
            raise TailorError(f"{FACTS_FILE}: fact #{index + 1} must be a mapping.")
        fact_id = _require(raw, "id", f"{FACTS_FILE} fact #{index + 1}")
        if fact_id in seen_bullets or fact_id in bank.facts:
            raise TailorError(f"Duplicate id '{fact_id}' in {FACTS_FILE}.")
        seen_bullets.add(fact_id)
        tags = raw.get("tags") or []
        if not isinstance(tags, list):
            raise TailorError(f"{FACTS_FILE} fact '{fact_id}': 'tags' must be a list.")
        bank.facts[fact_id] = Fact(
            id=fact_id,
            text=_require(raw, "text", f"{FACTS_FILE} fact '{fact_id}'"),
            scope=_require(raw, "scope", f"{FACTS_FILE} fact '{fact_id}'"),
            tags=[str(t) for t in tags],
            bullet_hint=str(raw.get("bullet_hint") or ""),
        )

    return bank


def bank_payload(bank: ContentBank, resume_entry_ids: list[str]) -> dict:
    """Serialize the bank for the planner prompt (compact keys)."""
    unknown = [
        b.entry
        for b in bank.bullets.values()
        if b.entry and b.entry not in resume_entry_ids and b.entry not in bank.entries
    ]
    if unknown:
        raise TailorError(
            "extra_bullets.yaml references unknown entry id(s): "
            + ", ".join(sorted(set(unknown)))
            + ".\nValid resume entry ids: "
            + ", ".join(resume_entry_ids)
        )

    valid_drop_targets = set(resume_entry_ids) | KNOWN_DROP_SENTINELS
    bad_drops = {
        (entry.id, target)
        for entry in bank.entries.values()
        for target in entry.requires_drop
        if target not in valid_drop_targets
    }
    if bad_drops:
        detail = ", ".join(f"{eid} -> '{target}'" for eid, target in sorted(bad_drops))
        raise TailorError(
            f"experience_bank.yaml has unknown requires_drop target(s): {detail}.\n"
            "Valid targets: " + ", ".join(sorted(valid_drop_targets))
        )

    valid_scopes = set(resume_entry_ids) | set(bank.entries) | GLOBAL_SCOPES
    bad_scopes = sorted(
        {f.scope for f in bank.facts.values() if f.scope not in valid_scopes}
    )
    if bad_scopes:
        raise TailorError(
            "facts.yaml has unknown scope(s): "
            + ", ".join(bad_scopes)
            + ".\nValid scopes: global, skills, "
            + ", ".join(sorted(set(resume_entry_ids) | set(bank.entries)))
        )

    return {
        "entries": [
            {
                "id": entry.id,
                "hdr": " | ".join(f for f in entry.fields if f),
                "tags": entry.tags,
                **({"drop": entry.requires_drop} if entry.requires_drop else {}),
                **({"n": entry.notes.strip()[:160]} if entry.notes.strip() else {}),
                "b": [
                    {"id": b.id, "t": b.text, "tags": b.tags}
                    for b in entry.bullets
                ],
            }
            for entry in bank.entries.values()
        ],
        "extra": [
            {
                "id": b.id,
                "e": b.entry,
                "t": b.text,
                "tags": b.tags,
            }
            for b in bank.bullets.values()
            if b.entry and b.entry in resume_entry_ids
        ],
        "facts": [
            {
                "id": f.id,
                "in": f.scope,
                "t": f.text,
                "tags": f.tags,
                **({"hint": f.bullet_hint} if f.bullet_hint else {}),
            }
            for f in bank.facts.values()
        ],
    }
