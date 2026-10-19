"""
runtime/skill_loader.py — kernel-side reader for skill .md files.

The platform's skills are markdown files in /var/lib/cseries/skills/, seeded
from the image's default-skills/. A Host's OWN skills are not files: they
live in its database (runtime/host_skills.py, migrations/v207), and every
caller that resolves a skill by name looks there first.

agentskills.io frontmatter shape:
  ---
  name: research
  description: How this entity researches the web.
  allowed-tools: WebSearch, WebFetch
  ---
  <markdown body of rules>

Architecture:
  - Skills feed the SDK AGENT (harness), not the entity prompt.
  - Entity directs ("research X"); harness reads applicable skill files,
    executes per the rules, returns synthesis.
  - Skills are RULES, not executable code, not model-call bodies.

Public API:
  load_skill(name)            -> Skill | None
  list_skills()               -> list[Skill]
  find_matching_skills(intent) -> list[Skill]   # keyword-match for now
  get_location()              -> Path

Selection (find_matching_skills) is keyword-based today. Embedding match
is a later optimization (and lives outside the entity-DB-backed
function_registry pattern, which was rejected).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

log = logging.getLogger("mastercode.skill_loader")

DEFAULT_LOCATION = "/var/lib/cseries/skills"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    frontmatter: dict
    path: Path

    @property
    def allowed_tools(self) -> list[str]:
        raw = self.frontmatter.get("allowed-tools", "")
        if not raw:
            return []
        return [t.strip() for t in raw.split(",") if t.strip()]

    @property
    def intent_match(self) -> bool:
        """False when the skill opts out of chat intent-matching via
        frontmatter `intent-match: false`. Explicitly-loaded surfaces
        (the harness pods, which read the skills dir themselves)
        are unaffected — this only gates find_matching_skills. A skill
        whose rules need tools the chat lane hard-disallows (git.md:
        Bash) opts out so it never loads into a harness call that gives
        it zero usable tools."""
        raw = str(self.frontmatter.get("intent-match", "")).strip().lower()
        return raw not in ("false", "no", "0", "off")


def get_location() -> Path:
    """The folder the platform's skills are seeded into, and the only one.

    A `/var/lib/cseries/skills.location` file could once point this anywhere; the Skills app's folder picker (POST /api/skills/location) wrote
    it. Both seeders write only here (entrypoint.sh's seed loop, and
    server/applications/skill_seeding.py SKILLS_DEST_DIR), so a location
    pointing elsewhere hid every platform skill from the loader. The picker is
    gone (a Host's own skills live in its database, host_skills), so nothing
    could ever point it back; a location
    file left on a Host is no longer read."""
    return Path(DEFAULT_LOCATION)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body_text). Empty frontmatter if absent."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    block = text[3:end].strip()
    body = text[end + 4 :].lstrip("\n")
    meta: dict = {}
    for line in block.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip().lower()] = v.strip().strip('"').strip("'")
    return meta, body


def load_skill(name: str) -> Skill | None:
    root = get_location()
    if not root.is_dir():
        return None
    if not name.endswith(".md"):
        name = name + ".md"
    path = root / name
    try:
        path = path.resolve()
        path.relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    meta, body = _parse_frontmatter(text)
    return Skill(
        name=path.stem,
        description=meta.get("description", ""),
        body=body,
        frontmatter=meta,
        path=path,
    )


def list_skills() -> list[Skill]:
    root = get_location()
    if not root.is_dir():
        return []
    out: list[Skill] = []
    for f in sorted(root.iterdir()):
        if not (f.is_file() and f.suffix == ".md"):
            continue
        s = load_skill(f.stem)
        if s is not None:
            out.append(s)
    return out


_WORD_RE = re.compile(r"[a-z][a-z0-9_]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def find_matching_skills(intent: str, limit: int = 3) -> list[Skill]:
    """Keyword match between intent and skill name/description/body.

    Score = unique-token overlap; skills with at least one overlap are
    candidates. Returns top `limit` by score (stable on ties).
    Returns [] when nothing matches — caller falls back to generic
    harness call without skill context.
    """
    skills = list_skills()
    if not skills:
        return []
    intent_tokens = _tokens(intent)
    if not intent_tokens:
        return []
    scored: list[tuple[int, int, Skill]] = []
    for idx, s in enumerate(skills):
        if not s.intent_match:
            continue
        haystack = f"{s.name} {s.description} {s.body}"
        skill_tokens = _tokens(haystack)
        overlap = len(intent_tokens & skill_tokens)
        if overlap > 0:
            scored.append((-overlap, idx, s))  # neg for descending sort
    scored.sort()
    return [s for _, _, s in scored[:limit]]
