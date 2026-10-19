"""
Skills — the platform's skills and the Host's own, in one app.

A skill is the steps a Host
follows to do one kind of job with what it already has; it is plain prose the
Host reads and acts on, never a program (a program is an App Store app).

  - The PLATFORM's skills are .md files in the seeded skills folder
    (skill_loader.get_location, /var/lib/cseries/skills), copied from the
    image's default-skills/. Platform code: read here, never written here.
  - The HOST's own skills are versions in its database (host_skills,
    migrations/v207, runtime/host_skills.py). The Host writes them with
    `context(action='write', path='skills/<name>')`; here the person reads
    each one's history (author and diff), edits it (validated against the
    same standard, author 'person'), retires it and restores any version (a
    new version carrying the old text, validated again).

Reads take read:files and writes write:files, the two scopes the Skills app
declares (system-apps/skills/app.yaml); write:files is not open to community
apps (build_contract.COMMUNITY_ALLOWED_SCOPES).

The folder picker (GET/POST /api/skills/location) is gone: it took any path
with no check, the seeders never read it, and a Host's own skills now live in
its database, so a folder choice has nothing left to choose.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server import brain_service
from server.applications.middleware import requires_permission

logger = logging.getLogger("privatae.skills")
router = APIRouter(prefix="/api/skills", tags=["skills"])


def _get_location() -> Path:
    """The platform's seeded skills folder — the loader's own reader, so the
    app and the Host see the same files."""
    from mastercode.runtime.skill_loader import get_location
    return get_location()


def _safe_skill_path(name: str) -> Path:
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(status_code=400, detail="invalid skill name")
    if not name.endswith(".md"):
        name = name + ".md"
    root = _get_location()
    candidate = (root / name).resolve()
    try:
        candidate.relative_to(root.resolve())
    except (ValueError, FileNotFoundError):
        raise HTTPException(status_code=400, detail="skill path escapes root")
    return candidate


def _parse_frontmatter(text: str) -> dict:
    """The loader's own frontmatter reader (skill_loader._parse_frontmatter)."""
    from mastercode.runtime.skill_loader import _parse_frontmatter as _pf
    return _pf(text)[0]


def _pool_and_entity():
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="the Host's database is not ready")
    return pool, int(brain_service._entity_id)


def _host_name(name: str) -> str:
    """A Host skill's name is a key in its own table, never a path on disk, so
    a `/` in it is only a character. A skill the standard accepts has none
    (skill_standard.NAME_RE), but v207 moved every `skills/` file in as a
    retired version under its path's name, so `skills/nested/thing.md` is
    `nested/thing` — and the person can open it. The routes take the name as a
    `{name:path}` parameter for that (the repo's form for a value that may hold
    `/`: inference.py `/favorites/{model_id:path}`, context_manager.py
    `/route/{prefix:path}`); the versions route is declared before the plain
    read, so `<name>/versions/<n>` is a version of `<name>`."""
    if not name or len(name) > 1024 or "\x00" in name:
        raise HTTPException(status_code=400, detail="invalid skill name")
    return name


class SkillText(BaseModel):
    text: str


class RestoreRequest(BaseModel):
    version: int


@router.get("/list", dependencies=[Depends(requires_permission("read:files"))])
async def list_skills():
    """Every skill, the Host's own first: {skills: [{name, description,
    author ('host' | 'person' | 'platform'), kind ('host' | 'platform'), …}]}.
    A Host skill carries its version, how many it has, and its status."""
    out = []
    pool = brain_service.get_pool()
    if pool is not None:
        from mastercode.runtime import host_skills
        from server.mcp_connections import stale_skills
        # An MCP server's skill written before the server's tool list last
        # changed is marked stale, so the Host (and the person) revise it.
        stale = await stale_skills(pool, int(brain_service._entity_id))
        for d in await host_skills.list_skills(pool, int(brain_service._entity_id)):
            row = {"name": d["name"], "description": d["description"], "kind": "host",
                   "author": d["author"], "version": d["version"],
                   "versions": int(d.get("versions") or 1), "status": d["status"],
                   "chars": d["size"], "updated": d["created_at"]}
            if d["status"] == "active" and d["name"] in stale:
                row["stale"] = "its MCP server's tool list changed after this version"
            # Written in a turn that read untrusted content (host_skills,
            # migrations/v232): the person reviews it, and saving it clears it.
            if d.get("untrusted"):
                row["untrusted"] = d["untrusted"]
            out.append(row)
    loc = _get_location()
    if loc.is_dir():
        for f in sorted(loc.iterdir()):
            if not (f.is_file() and f.suffix == ".md"):
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            meta = _parse_frontmatter(text)
            out.append({"name": f.stem, "description": meta.get("description", ""),
                        "kind": "platform", "author": "platform",
                        "size_bytes": f.stat().st_size})
    return {"skills": out, "location": str(loc)}


# Declared before GET /host/{name:path}: the first route that matches a path
# answers it, and the plain read would otherwise take `x/versions/2` as a name.
@router.get("/host/{name:path}/versions/{version:int}",
            dependencies=[Depends(requires_permission("read:files"))])
async def read_host_skill_version(name: str, version: int):
    """One version's text and its diff against the version before it."""
    from mastercode.runtime import host_skills
    pool, eid = _pool_and_entity()
    v = await host_skills.get_version(pool, eid, _host_name(name), version)
    if v is None:
        raise HTTPException(status_code=404, detail="version not found")
    return v


@router.get("/host/{name:path}", dependencies=[Depends(requires_permission("read:files"))])
async def read_host_skill(name: str):
    """One Host skill: its active version (or newest retired one), with the
    whole text, and every version's history (author, status, times)."""
    from mastercode.runtime import host_skills
    pool, eid = _pool_and_entity()
    name = _host_name(name)
    latest = await host_skills.get_latest(pool, eid, name)
    if latest is None:
        raise HTTPException(status_code=404, detail="skill not found")
    return {"skill": latest, "history": await host_skills.history(pool, eid, name)}


@router.put("/host/{name:path}", dependencies=[Depends(requires_permission("write:files"))])
async def write_host_skill(name: str, req: SkillText):
    """The person's edit (or a new skill): validated against the skill
    standard; stored as a new version with author 'person'. A refusal is 422
    with every rule the text broke. The new version carries no `untrusted`
    mark: the person read what they saved, so saving a marked skill, changed
    or not, is how they clear it (host_skills.write)."""
    from mastercode.runtime import host_skills
    pool, eid = _pool_and_entity()
    r = await host_skills.write(pool, eid, _host_name(name), req.text, author="person")
    if r["status"] == "refused":
        raise HTTPException(status_code=422, detail={"message": r["message"],
                                                     "failures": r["failures"]})
    return r


@router.post("/host/{name:path}/retire", dependencies=[Depends(requires_permission("write:files"))])
async def retire_host_skill(name: str):
    """409 while an active turn type names the skill: the message names the
    types, and the type's skill changes first (with the person's approval)."""
    from mastercode.runtime import host_skills
    pool, eid = _pool_and_entity()
    try:
        r = await host_skills.retire(pool, eid, _host_name(name))
    except host_skills.HostSkillError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if r.get("status") == "refused":
        raise HTTPException(status_code=409, detail=r["message"])
    return r


@router.post("/host/{name:path}/restore", dependencies=[Depends(requires_permission("write:files"))])
async def restore_host_skill(name: str, req: RestoreRequest):
    """A new version carrying an old version's text, validated again."""
    from mastercode.runtime import host_skills
    pool, eid = _pool_and_entity()
    try:
        r = await host_skills.restore(pool, eid, _host_name(name), req.version, author="person")
    except host_skills.HostSkillError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if r["status"] == "refused":
        raise HTTPException(status_code=422, detail={"message": r["message"],
                                                     "failures": r["failures"]})
    return r


@router.get("/{name}", dependencies=[Depends(requires_permission("read:files"))])
async def read_skill(name: str):
    """A platform skill file."""
    path = _safe_skill_path(name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="skill not found")
    text = path.read_text(encoding="utf-8", errors="replace")
    meta = _parse_frontmatter(text)
    return {
        "name": path.stem,
        "description": meta.get("description", ""),
        "frontmatter": meta,
        "body": text,
    }
