"""Install-time skill seeding.

Ship 12 (2026-05-26): each harness app can declare a default skill `.md`
file in /app/default-skills/ gated on `present_if_app_installed:
<app_id>`. The application installer calls `seed_for_app(app_id)` after
a successful install so the matching skill file lands in
/var/lib/cseries/skills/.

Mechanism is declarative file-copy only (no
scripted execution). The skill files live in /app/default-skills/
(baked into the image at build time); the install hook simply checks
frontmatter + copies matching files to the user-visible skills
directory.

Boot-time path (entrypoint.sh) still seeds ALWAYS-present skills
(no `present_if_app_installed` field or `: always`). This file
covers the gated case.
"""
from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path

log = logging.getLogger("privatae.applications.skill_seeding")

DEFAULT_SKILLS_DIR = Path("/app/default-skills")
SKILLS_DEST_DIR = Path("/var/lib/cseries/skills")


def _parse_gating_app(md_path: Path) -> str | None:
    """Read the frontmatter `present_if_app_installed:` field.

    Returns the gating app_id, or "always" if explicitly always-present,
    or None if no frontmatter field is present (also treated as always).
    """
    try:
        text = md_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    block = text[3:end]
    for line in block.splitlines():
        line = line.strip()
        if line.startswith("present_if_app_installed:"):
            value = line.split(":", 1)[1].strip().strip('"').strip("'")
            return value or None
    return None


def seed_for_app(app_id: str) -> list[str]:
    """Walk /app/default-skills/*.md and copy any whose frontmatter says
    `present_if_app_installed: <app_id>` to /var/lib/cseries/skills/.

    Does NOT overwrite a destination file that already exists — user
    edits take precedence. Returns the list of skill basenames seeded
    in this call (empty list on no-op).

    Safe to call multiple times; idempotent at the file level.
    """
    if not DEFAULT_SKILLS_DIR.is_dir():
        return []
    SKILLS_DEST_DIR.mkdir(parents=True, exist_ok=True)
    seeded: list[str] = []
    for md_path in sorted(DEFAULT_SKILLS_DIR.iterdir()):
        if not (md_path.is_file() and md_path.suffix == ".md"):
            continue
        gate = _parse_gating_app(md_path)
        if gate != app_id:
            continue
        dest = SKILLS_DEST_DIR / md_path.name
        if dest.exists():
            continue  # user edit / prior seed
        try:
            shutil.copy(md_path, dest)
            try:
                dest.chmod(0o644)
            except OSError:
                pass
            # Stamp the sha256 marker exactly as entrypoint.sh does for its
            # boot-time seeds (2026-08-23). The boot refresh now covers gated
            # skills that are present, and it tells "unedited default" from
            # "user edit" by comparing the file to this marker — without it,
            # a user edit made before the next restart would be refreshed
            # (backed up as .pre-refresh-<date>, but moved aside).
            try:
                marker_dir = SKILLS_DEST_DIR / ".seeded"
                marker_dir.mkdir(parents=True, exist_ok=True)
                (marker_dir / f"{md_path.name}.sha256").write_text(
                    hashlib.sha256(md_path.read_bytes()).hexdigest() + "\n")
            except OSError as exc:
                log.warning("could not stamp seed marker for %s: %s", md_path.name, exc)
            seeded.append(md_path.name)
            log.info("seeded skill %s (gated on app %s)", md_path.name, app_id)
        except OSError as exc:
            log.warning("could not seed %s: %s", md_path.name, exc)
    return seeded
