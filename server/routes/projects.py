"""
Projects V2 — Governed Project Execution Engine.

Hierarchy: project → phases → steps
Each step: spec_model → execute_model → check (self/model/human)
Every action produces a provenance node in a DAG.
Model routing from codebook competence + user preference.

Routes:
  Projects:   CRUD + list
  Phases:     CRUD within project, reorder
  Steps:      CRUD within phase, model assignment
  Provenance: query chain per step or full project graph
  Routing:    get/set model preferences per project domain
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import mimetypes
import os
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Annotated, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Depends
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from server.applications.middleware import requires_permission
from pydantic import BaseModel, Field, field_validator

from server import brain_service, secret_cut

logger = logging.getLogger("privatae.pc.projects")

router = APIRouter(prefix="/api/projects", tags=["projects"])


# ── Request models ───────���────────────────────────────

class CreateProject(BaseModel):
    name: str
    description: str = ""
    # Adopt a chat thread into the new project at creation (the Workbench
    # "make it a project" flow): the general-thread conversation MOVES into
    # the project — retagged, never deleted. {kind, ref[, since, until]}.
    adopt_thread: Optional[dict] = None
    # The Code Repository the project mounts (owner/name): an existing one the
    # Host can write to. Omitted, the Host makes a new one, as it always has.
    repo: Optional[str] = None
    # Copy `repo` into a new repository for this project instead — the answer
    # when it already has a live project, or the Host can only read it.
    fork: bool = False


class AdoptThread(BaseModel):
    # source_context kind of the source thread: 'app' | 'thread' | 'project'
    # | 'peer'. 'thread' is a Circles thread, ref = its uuid.
    kind: str
    ref: str           # source_context ref (usually 'workbench')
    since: Optional[str] = None   # ISO bounds — adopt one episode instead of all
    until: Optional[str] = None

class UpdateProject(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    # The bucket this project sits in (v130 folders, called buckets).
    # An explicit null UNFILES the project; the field
    # simply absent leaves it alone — the route tells the two apart through
    # model_fields_set, since None is also the "not provided" default.
    bucket_id: Optional[int] = None
    # owner/name: mount that Code Repository onto a ticket's project
    # (mount_ticket_repo_service). Other projects are mounted when made.
    repo: Optional[str] = None


class PlaceProject(BaseModel):
    """One drag in the Workbench rail: the bucket the project lands in (null =
    unfiled) and the project it lands in front of (null = the end)."""
    bucket_id: Optional[int] = None
    before_id: Optional[int] = None


class CreateBucket(BaseModel):
    name: str
    color: Optional[str] = None


class UpdateBucket(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None
    sort_order: Optional[int] = None

class CreatePhase(BaseModel):
    title: str
    description: str = ""
    spec: Optional[str] = None
    # v124: the phase's proof + gate (see migrations/v124). Same shapes the
    # host's plan tool takes; validated by the one normalizer in tools.py.
    acceptance: Optional[list] = None
    gate: Optional[str] = None
    # How the phase is proven once it can build: checks|proof|review. Implied
    # by acceptance (checks); required when a brief makes it ready without any.
    validation: Optional[str] = None

class UpdatePhase(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    spec: Optional[str] = None
    status: Optional[str] = None
    due_at: Optional[str] = None
    acceptance: Optional[list] = None
    gate: Optional[str] = None
    validation: Optional[str] = None

class CreateStep(BaseModel):
    title: str
    description: str = ""
    domain_tag: Optional[str] = None
    spec_model: Optional[str] = None
    execute_model: Optional[str] = None
    check_mode: str = "self"
    check_model: Optional[str] = None
    model_override: Optional[str] = None
    context: Optional[dict] = None

class UpdateStep(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    domain_tag: Optional[str] = None
    spec_model: Optional[str] = None
    execute_model: Optional[str] = None
    check_mode: Optional[str] = None
    check_model: Optional[str] = None
    model_override: Optional[str] = None
    context: Optional[dict] = None

class RecordProvenance(BaseModel):
    actor: str
    action: str
    parent_node_id: Optional[int] = None
    artifact_path: Optional[str] = None
    artifact_summary: Optional[str] = None
    input_ref: Optional[str] = None
    tokens_used: int = 0
    cost_usd: float = 0.0
    model_used: Optional[str] = None
    elapsed_ms: int = 0

class SetRouting(BaseModel):
    domain: str
    preferred_model: str
    reason: Optional[str] = None


def _pool():
    p = brain_service.get_pool()
    if not p:
        raise HTTPException(503, "Brain not initialized")
    return p

def _eid():
    return brain_service._entity_id


# ══════════════════════════════════════════════════════
# PROJECTS
# ════════════════���═══════════════════��═════════════════

PROJECT_FILES_ROOT = Path("/var/lib/cseries/projects")

# The projects.status enum, mirroring init-db.sql and migrations/v111. Held in
# code because the DB constraint is not a reliable guard on its own: it had
# drifted off long-lived entities entirely, so the same write succeeded on one
# entity and raised on another. Validate here, and the
# constraint stays the backstop rather than the only line.
PROJECT_STATUSES = frozenset(
    {"planning", "active", "review", "complete", "archived"})

# The four GROUPS the Workbench rail and viewer show, mapped onto the status
# column above — no status migration, the column already carries every value.
# 'review' folds into Active because it is work in flight, not a resting
# state; if that reads wrong in use it is a
# one-line change here.
PROJECT_GROUP_OF_STATUS = {
    "active": "active", "review": "active",
    "planning": "pending",
    "complete": "completed",
    "archived": "archived",
}
PROJECT_GROUPS = ("active", "pending", "completed", "archived")


def project_group(status) -> str:
    return PROJECT_GROUP_OF_STATUS.get(str(status or "").strip(), "pending")


# The project a task was run for, as its result records it. `t` is the
# entity_tasks alias wherever it is read: the last-activity stamp below and
# the tabs' landed read (_PAGE_MARKS_CTES).
_TASK_PROJECT_SQL = "t.result->>'project_id'"

# Most-recent-activity stamp: the most recent projects sit at the top, and
# the list shifts as older projects are worked on. updated_at only moves
# on row edits, so a chat turn in the project's thread or a build/fan-out
# against it counts as activity too. ONE expression, shared by list_projects
# and the project search (the search tie-breaks on it) — `p` is the projects
# alias in both.
#
# Each of its two reads is one index entry per project
# (migrations/v216_project_list_reads.sql): idx_entity_memory_project_turns
# for the newest turn row, idx_entity_tasks_project for the newest task, keyed
# on _TASK_PROJECT_SQL and the COALESCE below. An edit to either expression has
# to change the index with it, or every project reads all of the entity's rows
# again (scripts/test_project_list_speed_pg.py checks the plan).
_LAST_ACTIVITY_SQL = f"""GREATEST(
                   p.updated_at,
                   p.created_at,
                   (SELECT MAX(em.created_at) FROM entity_memory em
                     WHERE em.entity_id = p.entity_id AND em.memory_type = 'turn'
                       AND em.project_id = p.id),
                   (SELECT MAX(COALESCE(t.completed_at, t.started_at, t.created_at))
                      FROM entity_tasks t
                     WHERE t.entity_id = p.entity_id
                       AND ({_TASK_PROJECT_SQL}) = p.id::text)
               )"""


# What the Workbench's tabs read for each project, in the list's one query however many
# projects it holds: a read per project of the Host's replies (`lt` in
# _PAGE_MARKS_JOINS) and two grouped reads over the entity's rows joined to
# the projects by id. `p` is the projects alias; $1 is the entity id.
#   last_landed_at: the newest moment the Host produced something in the
#     project. Its turn rows with role 'assistant' (a reply, an agent's
#     delivered result, a note the platform wrote there), at the time each
#     was last written (_LANDED_AT_SQL), and its tasks that finished, completed
#     or failed (a cancel is most often the person's own Kill,
#     agent_dispatch.cancel). The person's own messages never count.
#     Shaped like _LAST_ACTIVITY_SQL, which counts every turn and task.
#   ask_cards_open: the project's open tickets, the list the Workbench raises
#     as ask cards (GET /api/tickets?status=open&asked_in=project:<id>, whose
#     room is tickets._room_filter: asked_in.project_id, and no thread).
#   approvals_waiting / approvals_checked_at: the copy of the forge's count
#     that every forge read keeps (code_approvals.record_waiting). The list
#     never calls the forge.
#   waiting_on_you: the two counts added; an approvals count never read is 0.
#   unseen_landed_at: what lights the page's tab. The newest of
#     what landed after seen_at that the person did not watch arrive: the
#     Host's replies, less those whose turn token is in projects.seen_turns
#     (v217; the reply's row is written after its stream ends, however late,
#     so its time cannot say the person saw it), and the tasks that finished.
#     NULL while nothing has, and while seen_at is NULL. The read starts at
#     seen_at in idx_entity_memory_project_replies, so it reads only what
#     landed since the mark.
#
# When a turn row landed: entity_memory.landed_at (v216), which the turn
# writers set at every write and rewrite of the row and never earlier than
# created_at (memory_engine._write_turn_row_landed), else created_at for a row
# from before the column or laid down by another writer. created_at alone is
# when a reply BEGAN: a reply that ran on for minutes, or a turn that held on a
# build and wrote its final reply over its row much later, would read as landed
# before the person's last look, and the tab would stay dark.
# idx_entity_memory_project_replies is built on exactly this expression, over
# exactly the rows `lt` reads, so a project's newest is that index's last
# entry for the project and one row read: the list's cost grows with its
# projects, never with their replies. Edit the expression or `lt`'s WHERE and
# the index with them (scripts/test_project_list_speed_pg.py checks the plan).
_LANDED_AT_SQL = "COALESCE(em.landed_at, em.created_at)"
_PAGE_MARKS_CTES = f"""
    landed_tasks AS (
        SELECT {_TASK_PROJECT_SQL} AS pid, MAX(t.completed_at) AS at
          FROM entity_tasks t
         WHERE t.entity_id = $1 AND t.status IN ('completed', 'failed')
           AND {_TASK_PROJECT_SQL} IS NOT NULL
         GROUP BY {_TASK_PROJECT_SQL}),
    open_asks AS (
        SELECT tk.metadata->'asked_in'->>'project_id' AS pid, count(*) AS n
          FROM entity_tickets tk
         WHERE tk.entity_id = $1 AND tk.status = 'open'
           AND tk.metadata->'asked_in'->>'project_id' IS NOT NULL
           AND COALESCE(tk.metadata->'asked_in'->>'thread_id', '') = ''
         GROUP BY tk.metadata->'asked_in'->>'project_id')"""
_PAGE_MARKS_JOINS = f"""
    LEFT JOIN LATERAL (
        SELECT MAX({_LANDED_AT_SQL}) AS at
          FROM entity_memory em
         WHERE em.entity_id = $1 AND em.memory_type = 'turn'
           AND em.project_id = p.id
           AND em.metadata->>'role' = 'assistant') lt ON true
    LEFT JOIN LATERAL (
        SELECT MAX({_LANDED_AT_SQL}) AS at
          FROM entity_memory em
         WHERE em.entity_id = $1 AND em.memory_type = 'turn'
           AND em.project_id = p.id
           AND em.metadata->>'role' = 'assistant'
           AND {_LANDED_AT_SQL} > p.seen_at
           AND COALESCE(em.metadata->>'turn_token', '')
               <> ALL(COALESCE(p.seen_turns, '{{}}'::text[]))) us ON true
    LEFT JOIN landed_tasks lk ON lk.pid = p.id::text
    LEFT JOIN open_asks oa ON oa.pid = p.id::text"""
_PAGE_MARKS_COLS = """
    p.seen_at,
    GREATEST(lt.at, lk.at) AS last_landed_at,
    GREATEST(us.at, CASE WHEN lk.at > p.seen_at THEN lk.at END) AS unseen_landed_at,
    COALESCE(oa.n, 0) + COALESCE(p.approvals_waiting, 0) AS waiting_on_you,
    COALESCE(oa.n, 0) AS ask_cards_open,
    p.approvals_waiting,
    p.approvals_checked_at"""


# Artifacts share the projects table (kind='artifact', v075) but are not
# projects: they live in the Artifacts window and, for desktops, the Designs
# app. Every Workbench-facing list filters them out with this one predicate.
# Before it, each desktop the host designed landed in the Workbench rail as an
# empty project, and every design opened another one; on one Host all 17
# artifacts had been archived by hand to clear them. `p` is the projects alias.
_PROJECTS_ONLY_SQL = "p.kind = 'project'"


def ensure_project_dir(project_id: int) -> Path:
    """A project's directory exists from the moment the project does.

    It used to be created by the single-harness build lane and nowhere else
    (review.run_single_harness_build), so a project that had been planned but
    not yet built had no directory at all — and three separate things read
    that absence as their own failure:

      - the multi-harness door (then review.fan_out) fell back to a session-named dir when
        PROJECTS_ROOT/<id> is not a dir, so a bake-off on a fresh project ran
        detached from the project it named;
      - review.adopt refused with "no project <id>", which is what a user hits
        when they pick a run from that bake-off;
      - project_git._sync_blocking returns "project workspace is empty", so
        nothing the project produced ever reached its gitea repo.

    Observed on one project: repo created at creation,
    three phases planned, four fan-out runs with real output, and an empty
    repo with an unadoptable set of runs because this directory was missing.

    Returns the path. Idempotent; never raises on an existing directory."""
    d = PROJECT_FILES_ROOT / str(int(project_id))
    d.mkdir(parents=True, exist_ok=True)
    return d


async def ensure_project_repo(pool, project_id: int, name: str) -> dict:
    """Repo-per-project (v081): create a private gitea repo for the project
    and record its full_name in projects.repo. Fail-SOFT — no gitea
    credential (or a gitea error) never blocks project creation; the repo
    column stays NULL and the next create attempt can backfill. Idempotent:
    an existing repo value is returned as-is."""
    try:
        row = await pool.fetchrow(
            "SELECT repo, config FROM projects WHERE id = $1", project_id)
        existing = row["repo"] if row else None
        if existing:
            return {"repo": existing, "created": False}
        if row is not None and ticket_label(row["config"]):
            # A ticket's fix belongs in the repository the ticket is about; a
            # new empty one would hold a build that fixes nothing.
            return {"repo": None, "created": False, "reason": TICKET_NEEDS_REPO}
        # A project whose code lives on the user's machine gets no platform
        # repo: its repo is made there, on its first save point (project_git).
        from server.routes import project_forge as _pf
        forge = await _pf.project_forge(pool, project_id)
        if forge.on_machine:
            return {"repo": None, "created": False,
                    "reason": f"this project's code lives on {forge.machine}"}
        from server.routes.gitea_client import gitea_creds, gitea_request
        cred = gitea_creds()
        if not cred:
            return {"repo": None, "created": False,
                    "reason": "gitea not connected"}
        user = cred[0]
        # Slug: gitea repo-name charset; project ids keep collisions impossible.
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-").lower() or "project"
        slug = f"{slug[:40]}-p{project_id}"
        data = await gitea_request(
            "POST", "/api/v1/user/repos",
            json={"name": slug, "private": True, "auto_init": True,
                  "description": f"Workbench project {project_id}: {name}"[:255]},
        )
        full = (data or {}).get("full_name") or f"{user}/{slug}"
        await pool.execute(
            "UPDATE projects SET repo = $1 WHERE id = $2", full, project_id)
        # The repo is the HOST's; give the USER write access so they can clone it
        # and push to it as themselves.
        collab = await ensure_user_collaborator(full)
        return {"repo": full, "created": True, "collaborator": collab}
    except Exception as e:
        return {"repo": None, "created": False, "reason": str(e)[:200]}


async def ensure_user_collaborator(repo_full: str) -> dict:
    """Give the signed-in USER write access to a project repo the HOST owns.
    Why: the person's own git clients list their repos through gitea
    `/user/repos` (owned OR collaborative) and push as themselves — so without this
    they could never see or push to the build PR, and the next build starts from
    what they push (project_git.prepare_workspace).

    Idempotent (a PUT of the same permission is a no-op on gitea), fail-soft (never
    raises — a missing sign-in just returns {ok:false, reason}), and it only ever
    WIDENS the user's access to the host's clean-room repo, never the reverse.

    On a git server connected in Settings the Host holds no sign-in of the
    person's: it adds the username the person gave there, with its own token,
    as the account that owns the repo."""
    try:
        if not repo_full or "/" not in repo_full:
            return {"ok": False, "reason": "no repo"}
        from urllib.parse import quote as _quote
        from server.git_server import connected as _git_server
        from server.routes.gitea import _user_bearer_fresh
        from server.routes.gitea_client import gitea_creds, gitea_request, split_repo
        own = _git_server()
        if own is not None:
            if not own.person:
                return {"ok": False, "reason": f"no username of yours on {own.netloc} was given "
                                               "in Settings, Git server"}
            owner, name = split_repo(repo_full)
            await gitea_request(
                "PUT",
                f"/api/v1/repos/{owner}/{name}/collaborators/{_quote(own.person, safe='')}",
                json={"permission": "write"}, raw_text=True)
            return {"ok": True, "login": own.person, "permission": "write"}
        bearer = await _user_bearer_fresh()
        if not bearer:
            return {"ok": False, "reason": "user not signed in to gitea"}
        me = await gitea_request("GET", "/api/v1/user", bearer=bearer)
        login = (me or {}).get("login") if isinstance(me, dict) else None
        if not login:
            return {"ok": False, "reason": "could not read the user's gitea login"}
        cred = gitea_creds()
        if cred and login == cred[0]:
            return {"ok": True, "login": login, "note": "user is the host account"}
        owner, name = split_repo(repo_full)
        await gitea_request(
            "PUT",
            f"/api/v1/repos/{owner}/{name}/collaborators/{_quote(login, safe='')}",
            json={"permission": "write"}, raw_text=True)
        return {"ok": True, "login": login, "permission": "write"}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


# ── Code Repositories ──
# A project mounts ONE Code Repository: a new one the Host makes
# (ensure_project_repo, still the default), an existing one the Host can write
# to, or a fork — a copy of one, under the Host's own account. Two live
# projects never share a repository: each keeps its own host/build branch and
# Code Approval there (migrations/v191 is the database's refusal behind the
# check here). Inside a project the repository is the context, so there is no
# picker; the forge permission on it decides what can happen.

_REPO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_REPO_PAGES = 10          # 500 repositories: the list a person picks from


def clean_repo_name(repo) -> str:
    r = str(repo or "").strip().strip("/")
    if r.lower().endswith(".git"):
        r = r[:-4]
    if not _REPO_NAME.match(r) or ".." in r:
        raise ProjectsError(400, "a Code Repository is named owner/name")
    return r


# A TICKET project — one ticket each:
# config.ticket (an imported ticket, {ref: 'ABC-123', ...}) or
# config.support_ticket (runtime/support_collect._ticket_project). Many
# tickets concern one repository, so a ticket project works IN that
# repository on a branch of its own with its own Code Approval, beside any
# other project there (shared repo, own branch). The
# one-live-project-per-repository rule binds the other projects among
# themselves, as before (v199 is the database's half).
_NOT_TICKET_SQL = ("NOT (COALESCE(config, '{}'::jsonb) ? 'ticket' "
                   "OR COALESCE(config, '{}'::jsonb) ? 'support_ticket')")


def _cfg_dict(cfg) -> dict:
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except ValueError:
            return {}
    return cfg if isinstance(cfg, dict) else {}


def ticket_label(cfg) -> Optional[str]:
    """The branch label of a ticket project ('ticket/ABC-123',
    'ticket/support-12'), or None when the project is not a ticket's."""
    c = _cfg_dict(cfg)
    t = c.get("ticket")
    if isinstance(t, dict) and str(t.get("ref") or "").strip():
        return f"ticket/{str(t['ref']).strip()}"
    if "ticket" in c:
        return "ticket/ticket"
    if c.get("support_ticket") is not None:
        return f"ticket/support-{c['support_ticket']}"
    return None


async def live_project_on_repo(pool, entity_id: int, repo: str,
                               exclude_id: Optional[int] = None) -> Optional[dict]:
    """The live (not archived) project already holding `repo`, or None. Ticket
    projects work there on branches of their own and hold nothing."""
    row = await pool.fetchrow(
        "SELECT id, name FROM projects WHERE entity_id = $1 AND lower(repo) = lower($2) "
        "AND status <> 'archived' AND ($3::int IS NULL OR id <> $3) "
        f"AND {_NOT_TICKET_SQL} ORDER BY id DESC LIMIT 1",
        entity_id, repo, exclude_id)
    return {"id": row["id"], "name": row["name"]} if row else None


def _host_login() -> Optional[str]:
    from server.routes.gitea_client import gitea_creds
    cred = gitea_creds()
    return cred[0] if cred else None


def _repo_facts(r: dict, host_login: Optional[str]) -> dict:
    """A forge repository as the start dialog and the delete prompt need it,
    read as the Host (the forge's `permissions` are the calling account's)."""
    owner = ((r.get("owner") or {}).get("login")) or ""
    perms = r.get("permissions") or {}
    return {
        "full_name": r.get("full_name"), "owner": owner, "name": r.get("name"),
        "owned_by_host": bool(host_login) and owner.lower() == (host_login or "").lower(),
        # A pull mirror refuses every push, whatever the permission says.
        "can_write": bool(perms.get("push")) and not r.get("mirror"),
        "private": bool(r.get("private")), "empty": bool(r.get("empty")),
        "mirror": bool(r.get("mirror")),
        "default_branch": r.get("default_branch") or "main",
        "description": r.get("description") or "", "updated_at": r.get("updated_at"),
    }


async def host_repo_access(repo: str) -> dict:
    """What the Host can do on `repo` (owner/name), from the forge."""
    from server.routes.gitea_client import gitea_request, split_repo
    if not _host_login():
        raise ProjectsError(409, "the Host has no forge login yet")
    owner, name = split_repo(repo)
    try:
        r = await gitea_request("GET", f"/api/v1/repos/{owner}/{name}")
    except HTTPException as e:
        if e.status_code == 404:
            raise ProjectsError(404, f"The Host can't see the Code Repository {repo}.")
        raise ProjectsError(502, f"the forge did not answer about {repo}: {e.detail}")
    return _repo_facts(r or {}, _host_login())


async def list_code_repositories_service(pool, entity_id: int) -> dict:
    """Every repository the Host can reach — its own, and those shared with
    it — each with the live project already on it, if any. What "start a
    project" picks from; a repository the Host cannot reach cannot be worked
    in, so it is not offered."""
    from server.routes.gitea_client import gitea_request
    host = _host_login()
    if not host:
        return {"connected": False, "repositories": []}
    seen: dict = {}
    for page in range(1, _REPO_PAGES + 1):
        data = await gitea_request("GET", "/api/v1/user/repos",
                                   params={"limit": 50, "page": page})
        if not isinstance(data, list) or not data:
            break
        for r in data:
            if isinstance(r, dict) and r.get("full_name"):
                seen.setdefault(r["full_name"], r)
        if len(data) < 50:
            break
    live = {r["repo"].lower(): {"id": r["id"], "name": r["name"]} for r in await pool.fetch(
        "SELECT id, name, repo FROM projects WHERE entity_id = $1 AND repo IS NOT NULL "
        f"AND status <> 'archived' AND {_NOT_TICKET_SQL} ORDER BY id", entity_id)}
    from server.routes.code_approvals import host_origin, repo_link
    origin = await host_origin(entity_id)
    rows = []
    for full, r in seen.items():
        facts = _repo_facts(r, host)
        facts["project"] = live.get(full.lower())
        facts["url"] = repo_link(origin, full)
        rows.append(facts)
    rows.sort(key=lambda f: str(f["updated_at"] or ""), reverse=True)
    return {"connected": True, "host": host, "repositories": rows}


async def plan_repo_mount(pool, entity_id: int, repo: str, *, fork: bool = False) -> dict:
    """Refuse, before any row is written, a mount that cannot work: a
    repository the Host cannot see, one it can only read, or one that already
    has a live project. The last two are answered with the fork offer."""
    facts = await host_repo_access(clean_repo_name(repo))
    full = facts["full_name"] or repo
    if fork:
        return {"repo": full, "fork": True, "facts": facts}
    if not facts["can_write"]:
        raise ProjectsError(409, f"The Host can read {full} but can't change it. "
                                 f"Fork it into a new project instead.")
    live = await live_project_on_repo(pool, entity_id, full)
    if live:
        raise ProjectsError(409, f"{full} already has a project (“{live['name']}”). "
                                 f"Fork it into a new project instead.")
    return {"repo": full, "fork": False, "facts": facts}


def _seed_json(repo: str, default_branch: str, source: Optional[str] = None) -> str:
    return json.dumps({"repo": repo, "default_branch": default_branch or "main",
                       "source": source, "done": False})


async def insert_mounted_project(pool, entity_id: int, name: str, description: str,
                                 mount: dict):
    """A mounted project's row, its own work branch and its pending read-in,
    in ONE transaction: a project that exists on a repository without its
    pending read-in would let a build in its empty directory propose deleting
    the repository's files. Raises on the unique index (v191) like any insert."""
    from server.routes.project_git import mounted_work_branch
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("""
                INSERT INTO projects (entity_id, name, description, repo)
                VALUES ($1, $2, $3, $4) RETURNING id, status, created_at
            """, entity_id, name, description, mount["repo"])
            await conn.execute(
                "UPDATE projects SET config = COALESCE(config,'{}'::jsonb) || "
                "jsonb_build_object('work_branch', $2::text, 'repo_seed', $3::jsonb) WHERE id = $1",
                row["id"], mounted_work_branch(_host_login(), row["id"]),
                _seed_json(mount["repo"], mount["facts"]["default_branch"]))
    return row


async def mount_project_repo(pool, project_id: int, mount: dict) -> dict:
    """The project's workspace starts from the mounted repository's code, read
    in the background (a large repository takes minutes); builds and save
    points wait for it, and retry it if it failed. The row, its own work
    branch and its pending read-in were written together
    (insert_mounted_project)."""
    from server.routes.project_git import seed_in_background
    facts = mount["facts"]
    seed_in_background(pool, project_id)
    out = {"repo": mount["repo"], "created": False, "mounted": True,
           "reason": f"Reading the code from {mount['repo']} into the project; builds wait for it."}
    if facts["owned_by_host"]:
        # As for a new repo: the person can push to it as themselves.
        out["collaborator"] = await ensure_user_collaborator(mount["repo"])
    return out


TICKET_NEEDS_REPO = ("this is a ticket's project and it has no Code Repository yet — mount the "
                     "repository the ticket is about first (update_project with repo='owner/name'); "
                     "it works there on a branch of its own")


async def _plan_ticket_mount(pool, entity_id: int, project_id: int, repo, *,
                             status: Optional[str] = None) -> dict:
    """Everything a ticket mount checks, and the forge read it needs, with no
    write: {"already": answer} when the project is on that repository now, else
    {"repo", "branch", "seed", "facts"} for _write_ticket_mount. `status` is
    the status the same update sets, when it sets one (a restore mounts; an
    archive does not)."""
    from server.routes.project_git import mounted_work_branch
    row = await pool.fetchrow(
        "SELECT id, name, repo, status, config FROM projects WHERE id = $1 AND entity_id = $2",
        project_id, entity_id)
    if row is None:
        raise ProjectsError(404, "Project not found")
    label = ticket_label(row["config"])
    want = clean_repo_name(repo)
    if row["repo"]:
        if row["repo"].lower() == want.lower():
            return {"already": {"mounted": True, "already": True, "repo": row["repo"],
                                "work_branch": _cfg_dict(row["config"]).get("work_branch")}}
        raise ProjectsError(409, f"This project already works in {row['repo']}.")
    if not label:
        raise ProjectsError(409, "Only a ticket's project takes a Code Repository after it is "
                                 "made. Start a new project on the repository instead.")
    if (status if status is not None else row["status"]) == "archived":
        # Reading a repository in for a project nobody works on would spend the
        # pod's disk on it (a copy each) and leave it a branch to no end.
        raise ProjectsError(409, "This ticket's project is archived. Restore it first.")
    from server.routes import project_forge as _pf
    if (await _pf.project_forge(pool, project_id)).on_machine:
        raise ProjectsError(409, "This project's code lives on the person's machine.")
    facts = await host_repo_access(want)
    full = facts["full_name"] or want
    if not facts["can_write"]:
        raise ProjectsError(409, f"The Host can read {full} but can't change it, so a ticket "
                                 f"can't be fixed there.")
    return {"repo": full, "facts": facts,
            "branch": mounted_work_branch(_host_login(), project_id, label),
            "seed": _seed_json(full, facts["default_branch"])}


async def _write_ticket_mount(conn, entity_id: int, project_id: int, plan: dict) -> Optional[dict]:
    """The mount's one write, on `conn` (a pool, or a transaction's connection
    that also carries the update's other fields). None when it was written;
    else the answer for a project mounted on the same repository meanwhile.
    Raises when the project changed under it (archived, another repository)."""
    done = await conn.fetchval(
        "UPDATE projects SET repo = $3, config = COALESCE(config, '{}'::jsonb) || "
        "jsonb_build_object('work_branch', $4::text, 'repo_seed', $5::jsonb), updated_at = now() "
        "WHERE id = $1 AND entity_id = $2 AND repo IS NULL AND status <> 'archived' RETURNING id",
        project_id, entity_id, plan["repo"], plan["branch"], plan["seed"])
    if done is not None:
        return None
    again = await conn.fetchrow("SELECT repo, status, config FROM projects WHERE id = $1",
                                project_id)
    if again and again["repo"] and again["repo"].lower() == plan["repo"].lower():
        return {"mounted": True, "already": True, "repo": again["repo"],
                "work_branch": _cfg_dict(again["config"]).get("work_branch")}
    if again and again["status"] == "archived":
        raise ProjectsError(409, "This ticket's project was archived while it was being "
                                 "mounted. Restore it first.")
    raise ProjectsError(409, "the project changed while it was being mounted — try again")


async def _start_ticket_mount(pool, project_id: int, plan: dict) -> dict:
    """After the write is committed. A ticket's mount reads NOTHING into the
    pod: the Host understands the code by searching it, and only a build needs
    files. The read-in stays pending (projects.config.repo_seed) and the
    ticket's first build does it (project_git.prepare_workspace), so a ticket
    nobody builds costs no disk at all — 79 tickets on one repository would
    otherwise each hold a copy of it."""
    out = {"repo": plan["repo"], "created": False, "mounted": True,
           "work_branch": plan["branch"],
           "reason": (f"Mounted {plan['repo']} on the branch {plan['branch']}. Nothing is read "
                      f"in until the ticket's first build, which reads it in first.")}
    if plan["facts"]["owned_by_host"]:
        # As for any mount: the person can push to it as themselves.
        out["collaborator"] = await ensure_user_collaborator(plan["repo"])
    return out


async def mount_ticket_repo_service(pool, entity_id: int, project_id: int, repo) -> dict:
    """Mount an existing Code Repository onto a TICKET project that has none.
    The project gets its own work branch named for its ticket
    (mounted_work_branch with ticket_label), made from the default branch
    when its code is read in, and its own Code Approval from that branch — so
    any number of tickets work in one repository at once (v199). Refused: a
    project that is not a ticket's (it is mounted when it is made, under the
    one-project-per-repository rule), one already on another repository, one
    archived, one whose code lives on a machine, a repository the Host cannot
    write. Mounting the repository it is already on answers as done."""
    plan = await _plan_ticket_mount(pool, entity_id, project_id, repo)
    if "already" in plan:
        return plan["already"]
    meanwhile = await _write_ticket_mount(pool, entity_id, project_id, plan)
    if meanwhile is not None:
        return meanwhile
    return await _start_ticket_mount(pool, project_id, plan)


async def fork_repo_for_project(pool, project_id: int, name: str, mount: dict) -> dict:
    """A fork is a copy: a new private repository under the Host's own account
    holding the source's default branch, history included, recorded on the
    project as forked_from {repo, commit}. A copy rather than the forge's own
    fork, because the forge allows one fork per account per repository and a
    person may fork the same code into several projects. Changes go back to
    the source the way the Host already proposes any change to code it does
    not own (default-skills/repos.md, op='propose').

    The new repository is made here; the source's code is copied into it and
    the workspace in the background (seed_project_workspace). Raises
    ProjectsError when the new repository cannot be made — the caller removes
    the project it had just created, so nothing half-made is left."""
    from server.routes.gitea_client import gitea_request
    from server.routes.project_git import seed_in_background
    facts = mount["facts"]
    source = mount["repo"]
    host = _host_login()
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", facts.get("name") or "fork").strip("-").lower() or "fork"
    slug = f"{slug[:40]}-p{project_id}"
    try:
        data = await gitea_request(
            "POST", "/api/v1/user/repos",
            json={"name": slug, "private": True, "auto_init": False,
                  "default_branch": facts["default_branch"],
                  "description": f"Forked from {source} for Workbench project {project_id}: {name}"[:255]})
    except HTTPException as e:
        raise ProjectsError(502, f"The copy of {source} could not be made: {e.detail}")
    full = (data or {}).get("full_name") or f"{host}/{slug}"
    # The copy, where it came from and its pending read-in, in one statement.
    await pool.execute(
        "UPDATE projects SET repo = $1, forked_from = $2::jsonb, config = jsonb_set("
        "COALESCE(config,'{}'::jsonb), '{repo_seed}', $4::jsonb, true) WHERE id = $3",
        full, json.dumps({"repo": source}), project_id,
        _seed_json(full, facts["default_branch"], source))
    seed_in_background(pool, project_id)
    out = {"repo": full, "created": True, "forked_from": source,
           "reason": f"Copying the code from {source} into {full}; builds wait for it."}
    out["collaborator"] = await ensure_user_collaborator(full)
    return out


async def _host_can_merge(repo: str, default_branch: str) -> bool:
    """Can the Host merge into `repo`'s default branch? The forge answers for
    the calling account, branch protection included — never on the platform
    repositories, where only the platform owner merges."""
    from urllib.parse import quote as _quote
    from server.routes.gitea_client import gitea_request, split_repo
    owner, name = split_repo(repo)
    try:
        b = await gitea_request(
            "GET", f"/api/v1/repos/{owner}/{name}/branches/{_quote(default_branch, safe='')}")
    except HTTPException:
        return False
    return bool((b or {}).get("user_can_merge"))


async def project_repository_service(pool, entity_id: int, project_id: int) -> dict:
    """The project's Code Repository as the Workbench shows it: where it is,
    its link, whether deleting the project may offer to delete it too, and
    whether the Host can merge there (so Auto-Approve is offered at all)."""
    row = await pool.fetchrow(
        "SELECT id, name, repo, COALESCE((config->>'auto_merge')::boolean, false) AS auto_merge "
        "FROM projects WHERE id = $1 AND entity_id = $2", project_id, entity_id)
    if row is None:
        raise ProjectsError(404, "Project not found")
    from server.routes import project_forge as _pf
    from server.routes.code_approvals import host_origin, repo_link
    out = {"project_id": project_id, "repo": row["repo"], "on_machine": False, "machine": None,
           "url": "", "owner": None, "owned_by_host": False, "default_branch": None,
           "can_delete": False, "delete_refusal": None, "host_can_merge": False,
           "auto_approve": bool(row["auto_merge"])}
    try:
        forge = await _pf.project_forge(pool, project_id)
    except _pf.ForgeError as e:
        out["delete_refusal"] = e.detail
        return out
    if forge.on_machine:
        # The code server on the person's machine: the Host made the repository
        # there under its own account, so it merges there; the platform never
        # deletes anything on a person's machine.
        out.update({"repo": forge.repo, "on_machine": True, "machine": forge.machine,
                    "host_can_merge": True,
                    "delete_refusal": f"This code lives on {forge.machine}, and it stays there."})
        return out
    if not row["repo"]:
        out["delete_refusal"] = "This project has no Code Repository yet."
        return out
    try:
        facts = await host_repo_access(row["repo"])
    except ProjectsError as e:
        out["delete_refusal"] = e.detail
        return out
    others = await pool.fetch(
        "SELECT id, name, status FROM projects WHERE entity_id = $1 AND lower(repo) = lower($2) "
        "AND id <> $3 ORDER BY id DESC", entity_id, row["repo"], project_id)
    out.update({"repo": facts["full_name"] or row["repo"], "owner": facts["owner"],
                "owned_by_host": facts["owned_by_host"],
                "default_branch": facts["default_branch"],
                "url": repo_link(await host_origin(entity_id), facts["full_name"] or row["repo"]),
                "host_can_merge": await _host_can_merge(row["repo"], facts["default_branch"])})
    if not facts["owned_by_host"]:
        out["delete_refusal"] = (f"{out['repo']} belongs to {facts['owner']}; "
                                 f"only its owner can delete it.")
    elif others:
        o = others[0]
        out["delete_refusal"] = (f"Another project (“{o['name']}”"
                                 + (", archived" if o["status"] == "archived" else "")
                                 + f") also uses {out['repo']}.")
    else:
        out["can_delete"] = True
    return out


async def project_archive_service(pool, entity_id: int, project_id: int, *,
                                  delete_repo: bool = False) -> dict:
    """Archive a project — never destroyed: rows, docs and memory tags stay.
    With `delete_repo`, the person also chose (and confirmed) to delete its
    Code Repository; that is offered only where the Host owns the repository
    and no other project uses it, and it is refused before anything changes
    when it is not allowed. A deletion the forge then refuses leaves the
    project archived and the repository kept, and says so."""
    info = None
    if delete_repo:
        info = await project_repository_service(pool, entity_id, project_id)
        if not info["can_delete"]:
            raise ProjectsError(409, info["delete_refusal"]
                                or "this Code Repository can't be deleted from here")
    result = await pool.execute(
        "UPDATE projects SET status = 'archived', updated_at = now() WHERE id = $1 AND entity_id = $2",
        project_id, entity_id)
    if "UPDATE 0" in result:
        raise ProjectsError(404, "Project not found")
    out = {"archived": True}
    if info is not None:
        from server.routes.gitea_client import gitea_request, split_repo
        owner, name = split_repo(info["repo"])
        try:
            await gitea_request("DELETE", f"/api/v1/repos/{owner}/{name}", raw_text=True)
        except HTTPException as e:
            out.update({"repo_deleted": False,
                        "repo_error": f"The project is archived; {info['repo']} was kept: {e.detail}"})
            return out
        # Restoring the project later starts a new repository (ensure_project_repo
        # backfills a NULL), never a dangling name.
        await pool.execute("UPDATE projects SET repo = NULL, updated_at = now() "
                           "WHERE id = $1 AND entity_id = $2", project_id, entity_id)
        logger.warning("project %d archived and its Code Repository %s deleted",
                       project_id, info["repo"])
        out.update({"repo_deleted": True, "repo": info["repo"]})
    return out


def _parse_iso_ts(v: Optional[str]):
    """ISO string → aware datetime, or None. asyncpg needs datetime objects
    against timestamptz params (raw strings raise DataError → 500)."""
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


async def adopt_thread_core(
    pool, entity_id: int, project_id: int,
    kind: str, ref: str,
    since=None, until=None,
) -> dict:
    """Move a chat thread's turns — and the memories derived from them —
    into a project, as an act of its own on request: the adopt-thread route,
    a create that names `adopt_thread` (the Workbench's general→project
    flow), and the Host's project_adopt_thread tool. Creating a project does
    NOT adopt anything by itself: the Host's create_project
    leaves the conversation whole where it was typed and opens the project's
    thread with a marker linking back (tools.py _exec_create_project; the
    carry-forward it replaced moved the request and left the reply behind).
    This is a retag, never a delete, in one transaction: the turns get
    project_id + source_context {project, N}; what was learned from them
    follows by the one rule every move takes (circles_threads.carry_learned:
    said in the project, the records of what they did, slot scopes; no
    project tag, as a row written in the project has none); every scoped
    view (general thread, project thread, recall, threads/episodes) follows
    the tags. A marker turn stays behind in the source thread so the move
    never reads as a disappearance — that exact trust break is what this
    closes.

    `kind` is the source scope's descriptor kind: 'app' (the original
    Circles conversation, 'circles'), 'thread' (a Circles thread the user
    started, ref = its uuid), 'project' (another project's thread), or
    'peer'.

    Raises ValueError('project not found') for a bad/foreign project id.
    """
    proj = await pool.fetchrow(
        "SELECT id, name FROM projects WHERE id = $1 AND entity_id = $2",
        project_id, entity_id)
    if not proj:
        raise ValueError("project not found")

    new_sctx = json.dumps({"kind": "project", "ref": str(project_id)})
    adopted_from = json.dumps({"kind": kind, "ref": ref})

    # kind='project': the source is ANOTHER project's thread —
    # "fold 228 into 229", or one episode of it. Project turns are scoped by
    # the project_id column (memory_engine._turn_row_where), not by
    # source_context, so the filter follows the column. Same retag, same
    # marker; the marker lands in the source project so it stays visible
    # there. Before this the only sources were untagged threads, which is why
    # the 228→229 split had no repair path at all.
    source_project: int | None = None
    if kind == "project":
        try:
            source_project = int(ref)
        except (TypeError, ValueError):
            raise ValueError("project source ref must be a project id")
        if source_project == project_id:
            raise ValueError("source and target are the same project")
        src_owned = await pool.fetchval(
            "SELECT 1 FROM projects WHERE id = $1 AND entity_id = $2",
            source_project, entity_id)
        if not src_owned:
            raise ValueError("source project not found")
        where = ["entity_id = $1", "memory_type = 'turn'", "status = 'active'",
                 "project_id = $2::int",
                 # Never carry a marker across: it describes a move that
                 # happened in the source, and it would read as one in the
                 # target.
                 "COALESCE(metadata->>'source_kind', '') <> 'thread_marker'"]
        params: list = [entity_id, source_project]
    elif kind == "thread":
        # kind='thread': the source is a Circles thread the user
        # started — "this conversation is actually a project". The scope
        # clause comes from memory_engine._turn_row_where, the one place the
        # thread branch lives, so an adopt can never select a different set
        # of rows than recall and the consolidator see in that thread
        # ("two scope truths"). session_id is
        # empty because a descriptor is always supplied here, so that
        # function's session fallback is unreachable.
        # What the thread SHOWS is what becomes the project: the transcript
        # set (superseded rows included, a regenerate's rows left behind) —
        # the same rows circles_threads.split_thread moves, so adopting and
        # splitting a thread never disagree about its contents.
        from mastercode import circles_threads as _ct
        if _ct.is_legacy(str(ref)):
            # shown_where maps the legacy sentinels ("original", "self", "")
            # to the ORIGINAL conversation; adopting that by accident would
            # retag the user's entire Circles history into one project.
            # That scope adopts as kind='app', ref='circles', on purpose.
            raise ValueError(
                "kind='thread' needs a thread id; the original conversation "
                "adopts as kind='app', ref='circles'")
        params = [entity_id]
        where = _ct.shown_where(params, str(ref))
        # Same reason as the project branch: a marker describes a move that
        # happened in the source thread and would read as one in the target.
        where.append("COALESCE(metadata->>'source_kind', '') <> 'thread_marker'")
    elif kind == "app" and str(ref).strip().lower() == "circles":
        # The ORIGINAL Circles conversation adopts the rows it renders — the
        # same set Split moves off it (circles_threads.shown_where(None):
        # the historic app_id-or-untagged clause, superseded rows included,
        # a regenerate's rows and the hidden lane left out). A bare
        # descriptor match here missed pre-descriptor rows and swept in
        # replay/A-B variants the conversation never shows.
        from mastercode import circles_threads as _ct
        params = [entity_id]
        where = _ct.shown_where(params, None)
        where.append("COALESCE(metadata->>'source_kind', '') <> 'thread_marker'")
    else:
        where = ["entity_id = $1", "memory_type = 'turn'", "status = 'active'",
                 "project_id IS NULL",
                 "metadata->'source_context'->>'kind' = $2",
                 "metadata->'source_context'->>'ref'  = $3"]
        params = [entity_id, kind, ref]
    # The source's own clause, before the window narrows it: what is left in
    # the source after the move (the connector map's retag reads it below).
    scope_where, scope_params = list(where), list(params)
    _since, _until = _parse_iso_ts(since), _parse_iso_ts(until)
    if _since is not None:
        params.append(_since)
        where.append(f"created_at >= ${len(params)}")
    if _until is not None:
        params.append(_until)
        where.append(f"created_at <= ${len(params)}")

    params.append(new_sctx)
    sctx_idx = len(params)
    params.append(adopted_from)
    from_idx = len(params)
    params.append(project_id)
    pid_idx = len(params)

    # One transaction: the turns, what was learned from them, the connector
    # map and the marker move together or not at all, so a failure halfway
    # never leaves a turn in the project with what it taught still in the
    # conversation it left (or the reverse).
    from mastercode import circles_threads as _ct
    moved_memories = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            moved = await conn.fetch(
                f"""
                UPDATE entity_memory
                   SET project_id = ${pid_idx},
                       metadata = metadata
                           || jsonb_build_object(
                                  'source_context', ${sctx_idx}::jsonb,
                                  'adopted_from',   ${from_idx}::jsonb,
                                  'adopted_at',     to_jsonb(now()::timestamptz))
                 WHERE {' AND '.join(where)}
                RETURNING id, source_session, created_at
                """, *params)

            if moved and kind in ("thread", "project", "app"):
                # A messaging connector's map rows follow the turns they belong
                # to, so a reply in the messaging app to a moved message lands
                # in the project (messaging connectors design 4.3).
                from server.connectors.message_map import retag_moved

                async def _staying() -> list:
                    return [r["created_at"] for r in await conn.fetch(
                        f"SELECT created_at FROM entity_memory WHERE {' AND '.join(scope_where)}",
                        *scope_params)]
                src_ref = (str(ref).strip().lower() if kind == "app"
                           else _ct.canonical_id(str(ref)) if kind == "thread" else str(ref))
                await retag_moved(conn, entity_id, src=(kind, src_ref),
                                  dst=("project", str(project_id)), moved_rows=moved,
                                  staying_times=_staying)

            if moved:
                # What was learned from the moved messages follows them by the
                # one rule every move takes (circles_threads.carry_learned): a
                # row whose from_turns are all moved messages (a rule taught in
                # one, what close_turn or memory_curate learned in its turn),
                # and — the whole conversation moving, no time window — every
                # row still said in it; the records of what those messages did
                # (retired, closed, said again, taught); slot scopes. Never a
                # project tag: a row said in the project by a fresh write has
                # none, so a row moved there has none either.
                whole = _since is None and _until is None
                if kind == "thread":
                    src_scope = _ct.descriptor_for(ref)
                elif kind == "project":
                    src_scope = {"kind": "project", "ref": str(source_project)}
                elif kind == "app" and str(ref).strip().lower() == "circles":
                    src_scope = {"kind": "app", "ref": "circles"}
                else:
                    src_scope = {"kind": kind, "ref": str(ref)}
                moved_memories = await _ct.carry_learned(
                    conn, entity_id, [int(r["id"]) for r in moved], src_scope,
                    {"kind": "project", "ref": str(project_id)}, whole=whole, resync=False)
                sessions = sorted({r["source_session"] for r in moved if r["source_session"]})
                if sessions and kind == "app" and str(ref).strip().lower() == "circles":
                    # A memory derived from a pre-descriptor session carries no
                    # source_context at all (memory_engine stamps one only when
                    # a turn in that session has one) and names no messages:
                    # the original Circles conversation's arm moves those
                    # sessions' turns, so what was derived there follows them.
                    res = await conn.execute(
                        """
                        UPDATE entity_memory
                           SET metadata = metadata
                                   || jsonb_build_object(
                                          'source_context', $3::jsonb,
                                          'adopted_from',   $4::jsonb)
                         WHERE entity_id = $1
                           AND memory_type <> 'turn'
                           AND status = 'active'
                           AND source_session = ANY($2::text[])
                           AND metadata->'source_context' IS NULL
                           AND metadata->'from_turns' IS NULL
                           AND metadata->'from_turn_tokens' IS NULL
                        """,
                        entity_id, sessions, new_sctx, adopted_from)
                    moved_memories += _ct._count(res)
                from mastercode.memory_slots import resync_scopes
                await resync_scopes(conn, entity_id)

                # The marker: one synthetic assistant turn left in the SOURCE
                # thread. It deliberately carries no embedding — it's
                # furniture, not knowledge.
                marker_md = {
                    "role": "assistant",
                    "source_kind": "thread_marker",
                    "source_context": {"kind": kind, "ref": ref},
                    "marker": {"project_id": project_id, "project_name": proj["name"],
                               "moved_turns": len(moved)},
                }
                if kind == "app":
                    marker_md["app_id"] = ref
                if source_project is not None:
                    # The source is a project thread, which is read by
                    # project_id — the marker must carry it or it renders
                    # nowhere.
                    marker_md["app_id"] = "workbench"
                await conn.execute(
                    """
                    INSERT INTO entity_memory
                      (entity_id, content, memory_type, importance, status,
                       source_session, project_id, metadata)
                    VALUES ($1, $2, 'turn', 0.1, 'active', $3, $4, $5::jsonb)
                    """,
                    entity_id,
                    f"→ Moved {len(moved)} messages into the project “{proj['name']}” "
                    f"— nothing was deleted; the conversation continues in that project's thread.",
                    f"adopt:{project_id}",
                    source_project,
                    json.dumps(marker_md))

    logger.info("adopt_thread entity=%d project=%d src=%s:%s turns=%d memories=%d",
                entity_id, project_id, kind, ref, len(moved), moved_memories)
    return {"project_id": project_id, "project_name": proj["name"],
            "moved_turns": len(moved), "moved_memories": moved_memories}


# A new project's row, as POST /api/projects writes one with no repository
# named: every other column takes its default. The Workbench Index's "new
# page" writes it with the same statement (routes/workbench_index.py).
# $1 entity, $2 name, $3 description.
NEW_PROJECT_SQL = """
    INSERT INTO projects (entity_id, name, description)
    VALUES ($1, $2, $3) RETURNING id, status, created_at
"""


@router.post("", dependencies=[Depends(requires_permission("write:projects"))])
async def create_project(body: CreateProject):
    pool = _pool()
    # The Start dialog makes what the person typed the new project's
    # description, and suggests its title from it: both are cut of their
    # secret shapes before anything keeps them (server/secret_cut.py), since
    # the project search's refresh embeds name and description on the Host's
    # embedding endpoint, which can be a keyed outside provider.
    name = secret_cut.redact_words(body.name, door="new project", log=logger)
    description = secret_cut.redact_words(body.description, door="new project", log=logger)
    # A chosen repository is checked before any row exists, so a refusal (not
    # visible, read-only, already in use) leaves nothing behind.
    mount = None
    if body.repo:
        try:
            mount = await plan_repo_mount(pool, _eid(), body.repo, fork=body.fork)
        except ProjectsError as e:
            raise HTTPException(e.status, e.detail)
    try:
        if mount and not mount["fork"]:
            row = await insert_mounted_project(pool, _eid(), name, description, mount)
        else:
            row = await pool.fetchrow(NEW_PROJECT_SQL, _eid(), name, description)
    except Exception as e:
        # Two starts racing for one repository: the index (v191) takes one.
        if mount and _is_unique_violation(e):
            raise HTTPException(409, f"{mount['repo']} already has a project. "
                                     f"Fork it into a new project instead.")
        raise
    # Directory first, then repo: both are "the project now exists" facts, and
    # the directory is the one every later lane reads (see ensure_project_dir).
    try:
        ensure_project_dir(row["id"])
    except OSError as e:
        logger.warning("create_project dir failed (non-fatal) for %s: %s", row["id"], e)
    if mount and mount["fork"]:
        try:
            repo = await fork_repo_for_project(pool, row["id"], name, mount)
        except ProjectsError as e:
            # Nothing half-made: the project was created a moment ago for this
            # fork and has no conversation, plan or files yet.
            await pool.execute("DELETE FROM projects WHERE id = $1 AND entity_id = $2",
                               row["id"], _eid())
            raise HTTPException(e.status, e.detail)
    elif mount:
        repo = await mount_project_repo(pool, row["id"], mount)
    else:
        repo = await ensure_project_repo(pool, row["id"], name)
    out = {"id": row["id"], "name": name, "status": row["status"],
           "repo": repo.get("repo"), "repo_note": repo.get("reason")}
    if repo.get("forked_from"):
        out["forked_from"] = repo["forked_from"]
    # Creation-time adoption (the Workbench general→project flow). Fail-soft:
    # the project must exist even if the move trips — the UI shows the count
    # (or its absence) either way.
    at = body.adopt_thread or {}
    if at.get("kind") and at.get("ref"):
        try:
            adopted = await adopt_thread_core(
                pool, _eid(), row["id"], str(at["kind"]), str(at["ref"]),
                since=at.get("since"), until=at.get("until"))
            out["adopted"] = adopted
        except Exception as e:
            logger.warning("create_project adopt_thread failed (non-fatal): %s", e)
            out["adopted"] = {"error": str(e)[:200]}
    return out


@router.post("/{project_id}/adopt-thread",
             dependencies=[Depends(requires_permission("write:projects"))])
async def adopt_thread(project_id: int, body: AdoptThread):
    """Move an existing chat thread (or one episode of it, via since/until)
    into this project. Retag-only — see adopt_thread_core."""
    pool = _pool()
    try:
        return await adopt_thread_core(
            pool, _eid(), project_id, body.kind, body.ref,
            since=body.since, until=body.until)
    except ValueError as e:
        # 'project not found', or a refused source ref — the message says which.
        raise HTTPException(404, str(e) or "project not found")


@router.get("", dependencies=[Depends(requires_permission("read:projects"))])
async def list_projects(status: Optional[str] = None, include_archived: bool = False,
                        limit: Annotated[int, Query(le=1000)] = 50,
                        offset: Annotated[int, Query(ge=0)] = 0):
    """The entity's projects, most recent activity first. `offset` pages past
    the first `limit`; next_offset is null on
    the last page.

    The defaults are written in the Annotated form so a direct Python caller
    (the pipeline and bucket gates call this route) gets a real 0 and 50, not
    the Query() marker: a left-out `offset` reached the OFFSET argument as
    Query(0) and failed a smoke test. FastAPI's HTTP validation is the
    same either way. scripts/test_route_param_markers_direct_calls.py holds
    every route to this.

    `include_archived=true` is the Workbench UI's parameter: its
    rail shows Archived as an ordinary group and its viewer shows every
    project, so it asks for all four groups in ONE call. Every other caller
    keeps the default below. The cap is 1000 rather than 200 for the same
    reason — the viewer is built for a user with several hundred projects.

    Each row also carries what the Workbench's tabs read (_PAGE_MARKS_COLS):
    seen_at, last_landed_at, unseen_landed_at, waiting_on_you and its parts.
    Still one query.
    """
    pool = _pool()
    params = [_eid()]
    where = f"WHERE p.entity_id = $1 AND {_PROJECTS_ONLY_SQL}"
    if status:
        where += " AND p.status = $2"
        params.append(status)
    elif not include_archived:
        # Archived projects are DELETED as far as the rails/pickers are
        # concerned (testing piles up demo projects and confuses the host). Ask for
        # status='archived' explicitly, or pass
        # include_archived=true (the Workbench's grouped rail), to see them.
        where += " AND p.status != 'archived'"

    rows = await pool.fetch(f"""
        WITH {_PAGE_MARKS_CTES}
        SELECT p.id, p.name, p.description, p.status, p.created_at, p.updated_at,
               -- The name the Workbench's PR pane asks for: the project's
               -- platform repo, else (its code on the user's machine and no
               -- platform repo ever made) its repo there — project_forge
               -- resolves either one to where the code lives.
               COALESCE(p.repo, CASE WHEN p.config->'pipeline'->'source'->>'provider' = 'machine-forge'
                                     THEN p.config->'pipeline'->'source'->>'repo' END) AS repo,
               (p.config->'pipeline'->'source'->>'provider' = 'machine-forge') IS TRUE AS code_on_machine,
               p.folder_id AS bucket_id, p.sort_order,
               (SELECT COUNT(*) FROM project_phases WHERE project_id = p.id) AS phase_count,
               (SELECT COUNT(*) FROM phase_steps s
                JOIN project_phases ph ON ph.id = s.phase_id
                WHERE ph.project_id = p.id) AS step_count,
               (SELECT COUNT(*) FROM phase_steps s
                JOIN project_phases ph ON ph.id = s.phase_id
                WHERE ph.project_id = p.id AND s.status = 'done') AS steps_done,
               (SELECT COUNT(*) FROM phase_steps s
                JOIN project_phases ph ON ph.id = s.phase_id
                WHERE ph.project_id = p.id AND s.status = 'failed') AS steps_failed,
               (SELECT s.title FROM phase_steps s
                JOIN project_phases ph ON ph.id = s.phase_id
                -- 'implementing' only: 'running' is not a member of the
                -- phase_steps CHECK, so that arm could never match. Same
                -- dead value fixed in mcp.py.
                WHERE ph.project_id = p.id AND s.status = 'implementing'
                ORDER BY ph.phase_number, s.step_number
                LIMIT 1) AS step_in_flight_title,
               {_LAST_ACTIVITY_SQL} AS last_activity_at,
               {_PAGE_MARKS_COLS}
        FROM projects p
        {_PAGE_MARKS_JOINS}
        {where}
        ORDER BY last_activity_at DESC NULLS LAST, p.id DESC
        LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
    """, *params, limit, offset)

    out = []
    for r in rows:
        d = dict(r)
        d["group"] = project_group(d.get("status"))
        out.append(d)
    return {"projects": out, "offset": offset,
            "next_offset": (offset + len(out)) if len(out) == limit else None}


# ── Buckets (v130 folders) + the project index (v131) ───────────────────────
# Buckets ARE the v130 project_folders rows, renamed in the UI and this API;
# the status groups no longer sit under them.
# The table keeps its name (no migration for a rename).
# Registered BEFORE the /{project_id} routes: FastAPI matches in registration
# order, and "/buckets" or "/search" would otherwise bind to project_id and
# fail int validation with a 422.

@router.get("/buckets", dependencies=[Depends(requires_permission("read:projects"))])
async def list_buckets():
    return await list_buckets_service(_pool(), _eid())


@router.post("/buckets", dependencies=[Depends(requires_permission("write:projects"))])
async def create_bucket(body: CreateBucket):
    try:
        return await create_bucket_service(_pool(), _eid(), body.name, color=body.color)
    except ProjectsError as e:
        raise HTTPException(e.status, e.detail)


@router.patch("/buckets/{bucket_id}", dependencies=[Depends(requires_permission("write:projects"))])
async def update_bucket(bucket_id: int, body: UpdateBucket):
    try:
        return await update_bucket_service(
            _pool(), _eid(), bucket_id,
            name=body.name, color=body.color, sort_order=body.sort_order)
    except ProjectsError as e:
        raise HTTPException(e.status, e.detail)


@router.delete("/buckets/{bucket_id}", dependencies=[Depends(requires_permission("write:projects"))])
async def delete_bucket(bucket_id: int):
    """Delete the BUCKET only. Its projects fall back to unfiled (the FK is
    ON DELETE SET NULL) — a bucket delete never cascades into projects."""
    try:
        return await delete_bucket_service(_pool(), _eid(), bucket_id)
    except ProjectsError as e:
        raise HTTPException(e.status, e.detail)


# The two searches the Workbench runs as the person types take their words in
# a JSON body, never in a URL. What is typed into these boxes is often a
# message, and a password or a key can be in it a character at a time; a URL
# is kept by whatever sits in front of the pod (a proxy's access log), a body
# is not (CWE-598). The pod's own server writes no access log
# (server/launch.py), so the URL was only ever exposed upstream of it.
#
# JSON can carry half of a surrogate pair ("\ud800"). A Python str holds it,
# but the UTF-8 every outgoing call needs refuses it, and a query string never
# carried one (the server read it as UTF-8 and replaced what was not). It
# becomes U+FFFD before anything reads the words, so such a search is answered
# like any other.
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _well_formed(q):
    """`q` with each lone surrogate replaced; anything that is not a str is
    left for the model to refuse. It runs before the model's own check, which
    refuses a lone surrogate (a 422) rather than reading past it."""
    return _LONE_SURROGATE.sub("\ufffd", q) if isinstance(q, str) else q


class SearchProjects(BaseModel):
    """POST /api/projects/search: the Start dialog's box."""
    q: str = Field("", max_length=500)
    limit: int = Field(8, ge=1, le=50)

    @field_validator("q", mode="before")
    @classmethod
    def _well_formed_q(cls, q):
        return _well_formed(q)


@router.post("/search", dependencies=[Depends(requires_permission("read:projects"))])
async def search_projects(body: SearchProjects):
    """Rank the entity's projects against free text — the "What would you
    like to work on?" box. Lexical always; semantic only when the host has
    an embedding model configured (the response says which, `semantic`).
    The words are the body's `q` (see SearchProjects)."""
    return await search_projects_service(_pool(), _eid(), body.q, limit=body.limit)


# The Workbench Index's search (routes/workbench_index.py): how many pages one
# answer lists, by default and at most.
INDEX_LIMIT_DEFAULT = 20
INDEX_LIMIT_MAX = 100


class SearchIndex(BaseModel):
    """POST /api/projects/index: the Index's box."""
    q: str = Field("", max_length=2000)
    limit: int = Field(INDEX_LIMIT_DEFAULT, ge=1, le=INDEX_LIMIT_MAX)

    @field_validator("q", mode="before")
    @classmethod
    def _well_formed_q(cls, q):
        return _well_formed(q)


# read:brain_memory as well as read:projects: the excerpts are the
# conversation's own words, which /api/brain/chat serves under that scope, and
# read:projects alone is a scope a store app may hold. Beside /search, and
# registered before /{project_id} for the same reason.
@router.post("/index", dependencies=[Depends(requires_permission("read:projects")),
                                     Depends(requires_permission("read:brain_memory"))])
async def index_projects(body: SearchIndex):
    """The Workbench Index while the person types: every page, open and
    archived, in the order it fits the body's `q`, with what matched in its
    name, its description, its phase titles or its conversation, and no score
    (workbench_index.index_search_service)."""
    from server.routes.workbench_index import index_search_service
    return await index_search_service(_pool(), _eid(), body.q, limit=body.limit)


# The project the Workbench reopens on (server/ui_last_open.py). Declared
# before /{project_id} for the same reason as /code-repositories below.
@router.get("/last-open", dependencies=[Depends(requires_permission("read:projects"))])
async def projects_last_open():
    """{ref}: the project id the Workbench last had open, or null when nothing
    is stored or that project is gone. Any status: an archived project is
    still the one the person left it on."""
    from server import ui_last_open
    try:
        return await ui_last_open.read(_pool(), _eid(), "workbench")
    except ui_last_open.LastOpenError as e:
        raise HTTPException(e.status, e.detail) from None


@router.put("/last-open", dependencies=[Depends(requires_permission("write:projects"))])
async def projects_set_last_open(request: Request):
    """Record the project now open in the Workbench. Body: {ref: project id}."""
    from server import ui_last_open
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "body must be JSON") from None
    try:
        return await ui_last_open.write(_pool(), "workbench", (body or {}).get("ref"))
    except ui_last_open.LastOpenError as e:
        raise HTTPException(e.status, e.detail) from None


# Declared before /{project_id}: a literal path after it would be read as a
# project id and refused.
@router.get("/code-repositories", dependencies=[Depends(requires_permission("read:projects"))])
async def list_code_repositories():
    """What "start a project" picks from: every Code Repository the Host can
    reach, each with the live project already on it (the fork offer's cue)."""
    try:
        return await list_code_repositories_service(_pool(), _eid())
    except HTTPException as e:
        raise HTTPException(502 if e.status_code >= 500 else e.status_code,
                            f"the forge did not list repositories: {e.detail}")


@router.get("/{project_id}/repository", dependencies=[Depends(requires_permission("read:projects"))])
async def get_project_repository(project_id: int):
    """The project's Code Repository: its link, whether the Host can merge
    there (Auto-Approve is offered only then), and whether deleting the
    project may offer to delete it too."""
    try:
        return await project_repository_service(_pool(), _eid(), project_id)
    except ProjectsError as e:
        raise HTTPException(e.status, e.detail)


@router.get("/{project_id}/activity", dependencies=[Depends(requires_permission("read:projects"))])
async def project_activity(
    project_id: int,
    since: Optional[str] = None,
    limit: int = Query(80, le=200),
    before: Optional[str] = None,
):
    """Live-view activity feed for a project: tool calls + research findings.

    Returns a chronological merged stream so the Workbench's live view can show
    what the Host is doing in real time. Sources: meta_tool:* dispatches in
    tool_call_log (project-scoped + a 2h recent window) and
    deep_research findings in entity_memory. `since` is an ISO timestamp;
    events strictly newer are returned. Default limit 80, caps at 200.

    Thin adapter: the merge/dedup logic lives in get_project_activity_service
    so the get_project_activity host tool shares it. Response shape
    unchanged.
    """
    try:
        return await get_project_activity_service(
            _pool(), _eid(), project_id, since=since, limit=limit, before=before,
        )
    except ProjectsError as e:
        raise HTTPException(e.status, e.detail)


class SeenMark(BaseModel):
    """The body of PUT /seen, optional. `turn_token`: the page's turn the
    person watched end (the token of its 2:[{type:"session"}] frame)."""
    turn_token: Optional[str] = None


@router.put("/{project_id}/seen", dependencies=[Depends(requires_permission("write:projects"))])
async def mark_project_seen(project_id: int, body: Optional[SeenMark] = None):
    """The person has this project's page open. Returns {project_id, seen_at},
    the mark as stored: it never moves back (mark_project_seen_service). With
    a body {turn_token}, the reply of that turn is also marked as watched, so
    its row never lights the page's tab whenever it is written."""
    try:
        return await mark_project_seen_service(
            _pool(), _eid(), project_id, turn_token=body.turn_token if body else None)
    except ProjectsError as e:
        raise HTTPException(e.status, e.detail)


# Two scopes, both held by the Workbench (system-apps/workbench/app.yaml), and
# not read:projects, which a store app may hold (build_contract.
# COMMUNITY_ALLOWED_SCOPES): the page carries what the conversation says the
# work is (the goal and the handoff, served under read:brain_memory by
# /api/brain/thread-context) and what it cost (the turns' and the runs' usage,
# served under read:inference_log by /api/compute).
@router.get("/{project_id}/page",
            dependencies=[Depends(requires_permission("read:brain_memory")),
                          Depends(requires_permission("read:inference_log"))])
async def get_project_page(project_id: int):
    """What the Workbench shows at the top of a project's page: where the work
    stands, the conversation's running totals, the coding-engine runs launched
    from it, the seen mark and what is waiting on the person
    (project_page_service)."""
    try:
        return await project_page_service(_pool(), _eid(), project_id)
    except ProjectsError as e:
        raise HTTPException(e.status, e.detail)


@router.get("/{project_id}", dependencies=[Depends(requires_permission("read:projects"))])
async def get_project(project_id: int):
    # Thin adapter: drill-into-one-project logic lives in get_project_service so the
    # get_project host tool shares it. Response shape unchanged.
    try:
        return await get_project_service(_pool(), _eid(), project_id)
    except ProjectsError as e:
        raise HTTPException(e.status, e.detail)


# ── Project PREVIEW — the landed build, viewable ─────────────────────────────
#
# Origin: the finance build LANDED — index.html + app.yaml
# at the project root — and the user still had nothing to look at. Render
# lanes existed for artifacts (/api/artifacts/{id}/render) and for bake-off
# worktrees (/api/review/sessions/.../preview/...), never for the project's
# canonical landed state, so "completed" arrived with no face and the host
# was left to infer where the output went. Same mechanics, landed tree.

def project_fs_root(project_id: int) -> Path:
    """The project's on-disk workspace (landed state + review machinery)."""
    from server.routes.applications import PROJECTS_ROOT
    return Path(PROJECTS_ROOT) / str(project_id)


def project_entry_choice(root: Path):
    """The LANDED tree's renderable page(s) — entry_pages.choose_entry with
    app.yaml's entrypoint first, then root index.html, then EVERY index.html
    at the shallowest depth OUTSIDE review machinery and dot-directories
    (harness worktrees — kimi/, codex/, … — are runs' output, not the
    project; previewing them would show work that never landed). Several
    pages at the same depth is a list for the person, never a silent pick
    (one project had five themes, and the preview opened whichever the
    filesystem listed first). No run asks here, so no changed files: a module
    VERSION is the whole module at that point, not one run's diff.

    Each step takes only a real file (entry_pages.page_in_tree): a regular
    file inside the tree, reached without a symlink, so an entrypoint or
    index.html linked out of the tree is not an entry. app.yaml itself is
    read by entry_pages.read_in_tree, which never follows a link either. The
    entrypoint may be any real file (a process app's main.py)."""
    from server.applications.entry_pages import choose_entry
    try:
        from server.routes.review import _is_review_dir
    except Exception:  # pragma: no cover — review router always ships
        def _is_review_dir(name: str) -> bool:
            return False
    return choose_entry(root, manifest=True,
                        skip_dir=lambda d: _is_review_dir(d) or d.startswith("."))


async def _own_project_row(project_id: int):
    row = await _pool().fetchrow(
        "SELECT id, name, kind FROM projects WHERE id=$1 AND entity_id=$2",
        project_id, _eid())
    if not row:
        raise HTTPException(404, f"project {project_id} not found")
    return row


def _preview_root(project_id: int, module: Optional[str], version: Optional[int]) -> Path:
    """What a preview renders: the project's landed root (no module — the
    single-app layout), a module's directory as it is now, or one VERSION's
    snapshot (every version stays openable, adopted or not). Names are
    validated by project_modules; a bad one is a 400."""
    if not module:
        if version is not None:
            raise HTTPException(400, "version needs a module")
        return project_fs_root(project_id)
    from server.routes.project_modules import ModuleError, module_dir, version_dir
    try:
        if version is not None:
            return version_dir(project_id, module, int(version))
        return module_dir(project_id, module)
    except ModuleError as e:
        raise HTTPException(400, str(e)) from None


@router.get("/{project_id}/preview", dependencies=[Depends(requires_permission("read:projects"))])
async def preview_project(project_id: int, module: Optional[str] = None,
                          version: Optional[int] = None, entry: Optional[str] = None):
    """Self-contained render of the project's LANDED entry doc — the iframe
    srcdoc source for the Workbench's post-build "open" affordance. Same
    asset-inlining + jail as the artifact render (render_workspace).
    `module` (+ `version`) renders one module, now or at a version.

    More than one candidate page is answered with 300 and the list
    ({detail, entries, basis}); `entry` then names the page to open
    (entry_pages.entry_param: 400 for an unsafe name, 404 for no such page,
    review machinery excluded)."""
    from server.applications.entry_pages import (
        EntryParamError, choice_detail, choice_headers, entry_param)
    from server.routes.review import _is_review_dir
    await _own_project_row(project_id)
    root = _preview_root(project_id, module, version)
    where = (f"version {version} of module {module}" if version is not None
             else f"module {module}" if module else "the project root")
    headers: dict = {}
    if entry:
        try:
            entry = entry_param(root, entry, skip_dir=_is_review_dir)
        except EntryParamError as e:
            raise HTTPException(e.status, f"{e.detail} ({where})") from None
    else:
        choice = project_entry_choice(root)
        if not choice.entries:
            raise HTTPException(404, (
                f"no renderable entry in {where} — nothing openable has been built there yet"))
        if choice.default is None:
            return JSONResponse(status_code=300, content={
                "detail": choice_detail(choice, where),
                "entries": list(choice.entries), "basis": choice.basis})
        entry = choice.default
        headers = choice_headers(choice, entry)
    from server.applications.artifacts import ArtifactError, render_workspace
    try:
        html = render_workspace(root, entry)
    except ArtifactError as e:
        raise HTTPException(e.status, e.detail) from None
    return HTMLResponse(html, headers=headers)


@router.get("/{project_id}/preview/{path:path}",
            dependencies=[Depends(requires_permission("read:projects"))])
async def preview_project_file(project_id: int, path: str = "",
                               module: Optional[str] = None,
                               version: Optional[int] = None):
    """Serve one file from the project's landed tree (multi-file apps
    navigated directly). Jail mirrors review.preview_harness_file: reject
    absolute / traversal / .git / symlink and any real target escaping the
    root. `module` (+ `version`) scopes the root the same way preview does."""
    await _own_project_row(project_id)
    root = _preview_root(project_id, module, version).resolve()
    rel = path or "index.html"
    parts = Path(rel).parts
    if os.path.isabs(rel) or ".." in parts or ".git" in parts or not parts:
        raise HTTPException(400, "rejected_unsafe_path")
    target = root / rel
    try:
        target_real = target.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(404, "file not found")
    escapes = not (target_real == root or root in target_real.parents)
    if target.is_symlink() or escapes or not target_real.is_file():
        raise HTTPException(403, "rejected_path_escape")
    # Served from the descriptor the no-follow walk opened, never by name: a
    # link swapped in after the checks above is not followed, and one swapped
    # in after the open cannot change what is sent.
    from server.applications.entry_pages import fd_file_response, open_in_tree
    fd = open_in_tree(root, Path(rel).as_posix())
    if fd is None:
        raise HTTPException(403, "rejected_path_escape")
    ctype, _enc = mimetypes.guess_type(str(target_real))
    return fd_file_response(fd, media_type=ctype or "application/octet-stream")


# ── Modules and versions ───────────────────────────────────────────────────
# Modules are declared by the host's plan (build action='plan', modules=[…]);
# these routes are what the Workbench reads and the pointer moves it makes.

@router.get("/{project_id}/modules", dependencies=[Depends(requires_permission("read:projects"))])
async def list_project_modules(project_id: int):
    """Modules with their current version, version candidates and (for an
    installable target) the observed app state, plus the root app's state —
    the Workbench's MODULES section (project_modules.modules_view)."""
    await _own_project_row(project_id)
    from server.routes.project_modules import modules_view
    return {"project_id": project_id, **await modules_view(_pool(), project_id)}


@router.get("/{project_id}/modules/{module}/versions",
            dependencies=[Depends(requires_permission("read:projects"))])
async def list_project_module_versions(project_id: int, module: str):
    await _own_project_row(project_id)
    from server.routes.project_modules import ModuleError, list_versions
    try:
        return {"project_id": project_id, "module": module,
                "versions": await list_versions(_pool(), project_id, module)}
    except ModuleError as e:
        raise HTTPException(404, str(e)) from None


class ModuleVersionPick(BaseModel):
    number: int


async def _commit_pointer_move(project_id: int, module: str, label: str) -> dict:
    """The module directory changed to another version's snapshot: that is a
    save point like any other, scoped to the module's directory."""
    try:
        from server.routes.project_git import sync_project_repo
        return await sync_project_repo(_pool(), project_id, phase_label=label,
                                       paths=[module])
    except Exception as e:  # noqa: BLE001 — the pointer moved; the commit is best-effort
        return {"synced": False, "error": str(e)[:200]}


@router.post("/{project_id}/modules/{module}/current",
             dependencies=[Depends(requires_permission("write:projects"))])
async def set_project_module_version(project_id: int, module: str, body: ModuleVersionPick):
    """Make version N the module's current one: the pointer moves and the
    module directory becomes that version's snapshot. Nothing is deleted."""
    await _own_project_row(project_id)
    from server.routes.project_modules import ModuleError, set_current_version
    try:
        out = await set_current_version(_pool(), project_id, module, body.number)
    except ModuleError as e:
        raise HTTPException(404, str(e)) from None
    out["repo_sync"] = await _commit_pointer_move(
        project_id, module, f"{module}: version {out['number']} is current")
    return out


@router.post("/{project_id}/modules/{module}/undo",
             dependencies=[Depends(requires_permission("write:projects"))])
async def undo_project_module_version(project_id: int, module: str):
    """The module's pointer goes back one version (pointer-only; the version
    it leaves stays and can be made current again)."""
    await _own_project_row(project_id)
    from server.routes.project_modules import ModuleError, undo_version
    try:
        out = await undo_version(_pool(), project_id, module)
    except ModuleError as e:
        raise HTTPException(409, str(e)) from None
    out["repo_sync"] = await _commit_pointer_move(
        project_id, module, f"{module}: back to version {out['number']}")
    return out


@router.put("/{project_id}", dependencies=[Depends(requires_permission("write:projects"))])
async def update_project(project_id: int, body: UpdateProject):
    # Thin adapter: rename/re-status/archive logic lives in update_project_service so the
    # update_project host tool shares it. Response shape unchanged.
    # A name or description the person typed is cut of its secret shapes before
    # anything keeps it, as POST /api/projects cuts a new project's: the project
    # search's refresh embeds both on the Host's embedding endpoint
    # (server/secret_cut.py). A field not sent stays None and is left alone.
    name, description = (None if v is None else secret_cut.redact_words(v, door="project edit", log=logger)
                         for v in (body.name, body.description))
    try:
        return await update_project_service(
            _pool(), _eid(), project_id,
            name=name, description=description, status=body.status,
            # Sent at all (including an explicit null = unfile) vs. absent.
            bucket_id=body.bucket_id if "bucket_id" in body.model_fields_set else _UNSET,
            repo=body.repo,
        )
    except ProjectsError as e:
        raise HTTPException(e.status, e.detail)


@router.patch("/{project_id}/place", dependencies=[Depends(requires_permission("write:projects"))])
async def place_project(project_id: int, body: PlaceProject):
    """One drop in the Workbench rail: file the project in a bucket and put it
    in front of another project there (or at the end). One request per drop;
    the server renumbers the bucket (place_project_service)."""
    try:
        return await place_project_service(
            _pool(), _eid(), project_id, bucket_id=body.bucket_id, before_id=body.before_id)
    except ProjectsError as e:
        raise HTTPException(e.status, e.detail)


@router.put("/{project_id}/manifest", dependencies=[Depends(requires_permission("write:projects"))])
async def put_project_manifest(project_id: int, request: Request):
    """Write the project's app.yaml so the Workbench can ship it.

    Body: {"yaml": "<full app.yaml text>"}
    Stored at /var/lib/cseries/projects/<id>/app.yaml. Validated by loading it
    through the manifest parser; rejected if the manifest doesn't pass.

    Thin adapter: the write+validate+rollback logic lives in
    set_project_manifest_service so the set_project_manifest host tool shares it
    without faking an HTTP Request. Response shape unchanged.
    """
    pool = _pool()
    eid = _eid()
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "body must be JSON") from None
    try:
        return await set_project_manifest_service(pool, eid, project_id, body.get("yaml"))
    except ProjectsError as e:
        raise HTTPException(e.status, e.detail)


@router.delete("/{project_id}", dependencies=[Depends(requires_permission("write:projects"))])
async def archive_project(project_id: int, delete_repo: bool = False):
    """Soft-delete: status -> 'archived'. The project vanishes from lists,
    rails, and the host's list_projects (and so stops confusing it), but rows,
    docs and memory tags survive. Its Code Repository survives too, unless the
    person asked, with a confirm, to delete it as well (`delete_repo`: deleting
    a project prompts whether to delete the repo too) — see project_archive_service
    for when that is
    allowed.

    This path was once declared TWICE with the same method and path.
    FastAPI matches in registration order, so this one always served and the
    second was unreachable — two implementations returning different shapes,
    one of them dead. The duplicate is gone; this response shape is the one
    that has always been live, so no caller changes.
    """
    try:
        return await project_archive_service(_pool(), _eid(), project_id, delete_repo=delete_repo)
    except ProjectsError as e:
        raise HTTPException(e.status, e.detail)


# ═════════════════���═══════════════════════════════════���
# PHASES
# ══════════���═══════════════════════════════════════════



@router.post("/{project_id}/phases", dependencies=[Depends(requires_permission("write:projects"))])
async def create_phase(project_id: int, body: CreatePhase):
    pool = _pool()
    await _verify_project(pool, project_id)

    next_num = await pool.fetchval(
        "SELECT COALESCE(MAX(phase_number), 0) + 1 FROM project_phases WHERE project_id = $1",
        project_id,
    )
    # A phase with a BRIEF is ready to build; one without stays 'planning'.
    # Same rule as _persist_plan and project_phase(op='add_phase') — this route
    # wrote the spec but left status at its 'planning' default, so a phase
    # created here was structurally unbuildable (both build doors refuse a
    # non-ready phase) despite carrying a perfectly good brief.
    _status = "ready" if (body.spec or "").strip() else "planning"
    _acc, _gate = _phase_proof_fields(body.acceptance, body.gate)
    # A phase that can build says how it is proven — the rule every other
    # writer of this table applies.
    _val = _phase_validation_or_400(_acc, body.validation, None) if _status == "ready" else None
    row = await pool.fetchrow("""
        INSERT INTO project_phases
            (project_id, phase_number, title, description, spec, status, acceptance, gate,
             validation)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
        RETURNING id, phase_number, status, gate, validation
    """, project_id, next_num, body.title, body.description, body.spec, _status,
        _acc, _gate or "none", _val)

    return dict(row)


def _phase_validation_or_400(acc_json, own, module_validation):
    """The proof rule for one buildable phase (tools._phase_validation_for),
    as a 400 naming what is missing."""
    from mastercode.runtime.tools import _phase_validation_for
    acc = json.loads(acc_json) if isinstance(acc_json, str) else acc_json
    val, why = _phase_validation_for(acc, own, module_validation)
    if why:
        raise HTTPException(400, f"phase refused — {why}")
    return val


def _phase_proof_fields(acceptance, gate):
    """(acceptance_json|None, gate|None) validated by the SAME normalizers the
    host's plan tool uses — one definition of a runnable check. 400 on a
    shape that cannot run. Function-local import: tools.py imports from this
    module, so a top-level import would cycle."""
    from mastercode.runtime.tools import _normalize_acceptance, _normalize_gate
    try:
        acc = _normalize_acceptance(acceptance) if acceptance is not None else None
        g = _normalize_gate(gate) if gate is not None else None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return (json.dumps(acc) if acc is not None else None), g


@router.get("/{project_id}/phases", dependencies=[Depends(requires_permission("read:projects"))])
async def list_phases(project_id: int):
    pool = _pool()
    await _verify_project(pool, project_id)

    rows = await pool.fetch("""
        SELECT ph.id, ph.phase_number, ph.title, ph.description, ph.spec, ph.status, ph.due_at,
               ph.gate, ph.approved_at,
               COALESCE(jsonb_array_length(ph.acceptance), 0) AS acceptance_checks,
               (SELECT COUNT(*) FROM phase_steps WHERE phase_id = ph.id) AS step_count,
               (SELECT COUNT(*) FROM phase_steps WHERE phase_id = ph.id AND status = 'done') AS steps_done,
               -- The Workbench plan pane groups phases under their module and
               -- shows how each is proven. The phase's own
               -- validation wins; the module's is what it inherits.
               m.name AS module,
               COALESCE(ph.validation, m.validation) AS validation,
               -- v175: the split and each phase's engine.
               ph.module_id, ph.depends_on, ph.harness, ph.model, ph.integration_error
        FROM project_phases ph
        LEFT JOIN project_modules m ON m.id = ph.module_id
        WHERE ph.project_id = $1
        ORDER BY ph.phase_number
    """, project_id)

    phases = [dict(r) for r in rows]
    # What each phase waits for, as the phase NUMBERS the pane shows — by the
    # scheduler's own rule (an unmarked phase waits for the one before it in
    # its module), so the pane and the build chain can never disagree.
    try:
        from mastercode.runtime.tools import _phase_deps
        num = {p["id"]: p["phase_number"] for p in phases}
        for p in phases:
            p["waits_for"] = [num[d] for d in _phase_deps(p, phases) if d in num]
    except Exception as e:  # noqa: BLE001 — the list still renders without it
        logger.warning("phase dependencies unavailable for project %s: %s", project_id, e)
    return {"phases": phases}


@router.put("/{project_id}/phases/{phase_id}", dependencies=[Depends(requires_permission("write:projects"))])
async def update_phase(project_id: int, phase_id: int, body: UpdatePhase):
    pool = _pool()
    await _verify_project(pool, project_id)

    sets, params = [], [phase_id, project_id]
    if body.title is not None:
        params.append(body.title)
        sets.append(f"title = ${len(params)}")
    if body.description is not None:
        params.append(body.description)
        sets.append(f"description = ${len(params)}")
    if body.spec is not None:
        params.append(body.spec)
        sets.append(f"spec = ${len(params)}")
    if body.status is not None:
        params.append(body.status)
        sets.append(f"status = ${len(params)}")
    if body.due_at is not None:
        params.append(datetime.fromisoformat(body.due_at))
        sets.append(f"due_at = ${len(params)}")
    _acc = None
    if body.acceptance is not None or body.gate is not None:
        _acc, _gate = _phase_proof_fields(body.acceptance, body.gate)
        if body.acceptance is not None:
            params.append(_acc)
            sets.append(f"acceptance = ${len(params)}::jsonb")
        if _gate is not None:
            params.append(_gate)
            sets.append(f"gate = ${len(params)}")
    # The phase as it will be after this update: if it is ready to build it
    # must say how it is proven — so neither making it ready nor clearing its
    # acceptance can leave a buildable phase with no proof.
    cur = await pool.fetchrow(
        "SELECT p.spec, p.status, p.acceptance, p.validation, "
        "m.validation AS module_validation FROM project_phases p "
        "LEFT JOIN project_modules m ON m.id = p.module_id "
        "WHERE p.id=$1 AND p.project_id=$2", phase_id, project_id)
    if cur is None:
        raise HTTPException(404, "Phase not found")
    status_after = body.status if body.status is not None else cur["status"]
    # Only an update that touches readiness or proof is held to the rule; a
    # title edit on a phase written before validation existed is not.
    touches_proof = (body.status is not None or body.spec is not None
                     or body.acceptance is not None or body.validation is not None)
    # Buildable = 'ready' or a 'failed' retry (the build door takes both), so
    # a failed phase is held to the same rule as a ready one.
    if touches_proof and (status_after in ("ready", "failed") or body.validation is not None):
        acc_after = (json.loads(_acc) if (body.acceptance is not None and _acc) else
                     (None if body.acceptance is not None else cur["acceptance"]))
        spec_after = body.spec if body.spec is not None else cur["spec"]
        if status_after in ("ready", "failed") and not (spec_after or "").strip():
            raise HTTPException(400, "phase refused — a ready phase needs a brief (spec)")
        _val = _phase_validation_or_400(
            acc_after, body.validation or cur["validation"], cur["module_validation"])
        params.append(_val)
        sets.append(f"validation = ${len(params)}")
    if not sets:
        raise HTTPException(400, "Nothing to update")
    sets.append("updated_at = now()")

    result = await pool.execute(f"""
        UPDATE project_phases SET {', '.join(sets)}
        WHERE id = $1 AND project_id = $2
    """, *params)
    if "UPDATE 0" in result:
        raise HTTPException(404, "Phase not found")
    return {"updated": True}


@router.post("/{project_id}/phases/{phase_id}/approve",
             dependencies=[Depends(requires_permission("write:projects"))])
async def approve_phase(project_id: int, phase_id: int):
    """The USER's signature on a phase (v124). approved_at has exactly two
    writers, both the user's own act: this route, and the user's 'approve' on
    the phase-gate ticket in Circles (server/routes/tickets.py
    _apply_phase_gate_decision). No host tool writes it.

    Two things it means, by the phase's state:
      * gate='user', not yet built: the approval that makes it buildable. The
        build door and the auto-advance chain both refuse a gated phase until
        this row carries approved_at; no host tool can write it.
      * status='review': a phase that landed under the older rules (v197 turned
        those complete; nothing writes 'review' now). Kept so a row the
        migration has not reached yet still completes.
    Anything else is a no-op that says so. Entity-scoped like every route here.
    """
    pool = _pool()
    await _verify_project(pool, project_id)
    row = await pool.fetchrow(
        "SELECT id, status, gate, approved_at FROM project_phases "
        "WHERE id = $1 AND project_id = $2", phase_id, project_id)
    if row is None:
        raise HTTPException(404, "Phase not found")
    if row["status"] == "review":
        await pool.execute(
            "UPDATE project_phases SET status = 'complete', "
            "approved_at = COALESCE(approved_at, now()), updated_at = now() "
            "WHERE id = $1", phase_id)
        # The project completes here if this was its last open phase — the
        # same rule the build path applies (tools._run_phase_build).
        remaining = await pool.fetchval(
            "SELECT COUNT(*) FROM project_phases WHERE project_id = $1 "
            "AND status NOT IN ('complete','archived')", project_id)
        if not remaining:
            await pool.execute(
                "UPDATE projects SET status = 'complete', updated_at = now() "
                "WHERE id = $1", project_id)
        # The operations ticket joined to this phase hears the
        # same completion the build path reports. Fail-soft in the service.
        from server.routes.tickets import on_phase_outcome
        await on_phase_outcome(pool, _eid(), phase_id=phase_id, status="complete",
                               note="confirmed by the user in the Workbench")
        # The click goes on with the plan: what this phase held
        # back starts now, as when a phase completes on its own — the chain
        # stop used to ask for a typed word to continue.
        went_on = {"started": False}
        if remaining:
            from mastercode.runtime.tools import continue_plan
            went_on = await continue_plan(pool, _eid(), project_id)
        return {"phase_id": phase_id, "status": "complete", "approved": True,
                "project_complete": not remaining, "build": went_on}
    if row["gate"] == "user" and not row["approved_at"]:
        await pool.execute(
            "UPDATE project_phases SET approved_at = now(), updated_at = now() "
            "WHERE id = $1", phase_id)
        # The click is the go-ahead: the phase starts now if its
        # turn has come, else when it does — nothing to type (the note used to
        # ask for a typed word to start it). Started BEFORE the gate ticket is
        # decided below, so that decide finds the phase building and starts
        # nothing twice.
        from mastercode.runtime.tools import start_approved_phase
        started = await start_approved_phase(pool, _eid(), project_id, phase_id)
        # Approved here, so the gate ticket in Circles (if one was raised) is
        # decided the same way rather than left dangling. Fail-soft.
        from server.routes.tickets import resolve_phase_gate_tickets
        await resolve_phase_gate_tickets(pool, _eid(), phase_id=phase_id)
        return {"phase_id": phase_id, "status": row["status"], "approved": True,
                "build": started,
                "note": ("approved — it is building" if started.get("started")
                         else "approved — " + (started.get("why")
                                               or "the plan starts it when its turn comes"))}
    return {"phase_id": phase_id, "status": row["status"],
            "approved": bool(row["approved_at"]),
            "note": "nothing to approve on this phase"}


# ══════════════��═════════════════════════════���═════════
# STEPS
# ══════════════════════���═══════════════════════════════

@router.post("/{project_id}/phases/{phase_id}/steps", dependencies=[Depends(requires_permission("write:projects"))])
async def create_step(project_id: int, phase_id: int, body: CreateStep):
    pool = _pool()
    await _verify_project(pool, project_id)
    await _verify_phase(pool, phase_id, project_id)

    next_num = await pool.fetchval(
        "SELECT COALESCE(MAX(step_number), 0) + 1 FROM phase_steps WHERE phase_id = $1",
        phase_id,
    )

    # Auto-classify domain if not provided
    domain_tag = body.domain_tag
    if not domain_tag:
        try:
            from mastercode.domain_classifier import classify_domain
            classified = classify_domain(f"{body.title} {body.description}")
            if classified.get("domain"):
                domain_tag = classified["domain"]
        except Exception:
            pass

    # Auto-fill model assignments from the project's routing
    execute_model = body.execute_model or body.model_override
    spec_model = body.spec_model
    check_model = body.check_model

    if domain_tag and (not execute_model or not spec_model):
        routing = await _resolve_model_for_domain(pool, project_id, domain_tag)
        if routing:
            if not execute_model:
                execute_model = routing
            if not spec_model:
                spec_model = routing

    row = await pool.fetchrow("""
        INSERT INTO phase_steps
            (phase_id, step_number, title, description, domain_tag,
             spec_model, execute_model, check_mode, check_model,
             model_override, context)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        RETURNING id, step_number, status, domain_tag, execute_model, spec_model, check_model
    """,
        phase_id, next_num, body.title, body.description, domain_tag,
        spec_model, execute_model, body.check_mode, check_model,
        body.model_override, json.dumps(body.context or {}),
    )

    return dict(row)


@router.get("/{project_id}/phases/{phase_id}/steps", dependencies=[Depends(requires_permission("read:projects"))])
async def list_steps(project_id: int, phase_id: int):
    pool = _pool()
    await _verify_project(pool, project_id)

    rows = await pool.fetch("""
        SELECT s.id, s.step_number, s.title, s.description, s.domain_tag,
               s.spec_model, s.execute_model, s.check_mode, s.check_model,
               s.model_override, s.status, s.context
        FROM phase_steps s
        WHERE s.phase_id = $1
        ORDER BY s.step_number
    """, phase_id)

    return {"steps": [dict(r) for r in rows]}


@router.put("/{project_id}/steps/{step_id}", dependencies=[Depends(requires_permission("write:projects"))])
async def update_step(project_id: int, step_id: int, body: UpdateStep):
    pool = _pool()
    await _verify_project(pool, project_id)

    sets, params = [], [step_id]
    for field in ("title", "description", "status", "domain_tag", "spec_model",
                  "execute_model", "check_mode", "check_model", "model_override"):
        val = getattr(body, field, None)
        if val is not None:
            params.append(val)
            sets.append(f"{field} = ${len(params)}")
    if body.context is not None:
        params.append(json.dumps(body.context))
        sets.append(f"context = ${len(params)}::jsonb")
    if not sets:
        raise HTTPException(400, "Nothing to update")
    sets.append("updated_at = now()")

    result = await pool.execute(f"""
        UPDATE phase_steps SET {', '.join(sets)} WHERE id = $1
    """, *params)
    if "UPDATE 0" in result:
        raise HTTPException(404, "Step not found")
    return {"updated": True}


# ════════���═══════════════════════════════��═════════════
# PROVENANCE
# ═════════════════���════════════════════════════════════

@router.post("/{project_id}/steps/{step_id}/provenance", dependencies=[Depends(requires_permission("write:projects"))])
async def record_provenance(project_id: int, step_id: int, body: RecordProvenance):
    pool = _pool()

    row = await pool.fetchrow("""
        INSERT INTO step_provenance
            (step_id, parent_node_id, actor, action, artifact_path,
             artifact_summary, input_ref, tokens_used, cost_usd,
             model_used, elapsed_ms)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        RETURNING id, created_at
    """,
        step_id, body.parent_node_id, body.actor, body.action,
        body.artifact_path, body.artifact_summary, body.input_ref,
        body.tokens_used, body.cost_usd, body.model_used, body.elapsed_ms,
    )

    return {"id": row["id"], "created_at": row["created_at"].isoformat()}


@router.get("/{project_id}/steps/{step_id}/provenance", dependencies=[Depends(requires_permission("read:projects"))])
async def get_step_provenance(project_id: int, step_id: int):
    pool = _pool()

    rows = await pool.fetch("""
        SELECT id, parent_node_id, actor, action, artifact_path,
               artifact_summary, input_ref, tokens_used, cost_usd,
               model_used, elapsed_ms, created_at
        FROM step_provenance
        WHERE step_id = $1
        ORDER BY created_at
    """, step_id)

    return {"provenance": [dict(r) for r in rows]}


@router.get("/{project_id}/provenance", dependencies=[Depends(requires_permission("read:projects"))])
async def get_project_provenance(project_id: int, limit: int = Query(200, le=1000)):
    pool = _pool()
    await _verify_project(pool, project_id)

    rows = await pool.fetch("""
        SELECT sp.id, sp.step_id, sp.parent_node_id, sp.actor, sp.action,
               sp.artifact_path, sp.artifact_summary, sp.tokens_used,
               sp.cost_usd, sp.model_used, sp.elapsed_ms, sp.created_at,
               ps.title AS step_title, ps.step_number,
               pp.title AS phase_title, pp.phase_number
        FROM step_provenance sp
        JOIN phase_steps ps ON ps.id = sp.step_id
        JOIN project_phases pp ON pp.id = ps.phase_id
        WHERE pp.project_id = $1
        ORDER BY sp.created_at DESC
        LIMIT $2
    """, project_id, limit)

    total_tokens = sum(r["tokens_used"] or 0 for r in rows)
    total_cost = sum(float(r["cost_usd"] or 0) for r in rows)
    actors = list(set(r["actor"] for r in rows))

    return {
        "provenance": [dict(r) for r in rows],
        "summary": {
            "total_nodes": len(rows),
            "total_tokens": total_tokens,
            "total_cost_usd": round(total_cost, 6),
            "actors": actors,
        },
    }


# ══════════════════════════════════════════════════════
# AUTO-MERGE (the build chain's landing policy, per project)
# ══════════════════════════════════════════════════════
#
# With auto-merge selected, the host proceeds to the next phase on its own
# after the PR is written, reviewed and merged.
#
# Shape: ONE rolling PR per project (host/build → default
# branch, kept current by project_git._ensure_pr) with each phase landing as its
# own commit inside it. So auto-merge is an END-OF-PLAN policy, not a per-phase one:
# phases chain as they already do, and the PR is merged once the last phase of
# the approved plan lands clean.
#
# OFF by default, and it stays off unless the user turns it on. The Host never
# merges for the person — they approve, and the platform merges as them
# (routes/code_approvals.py) — so this is the person's explicit, per-project,
# revocable permission for the Host to approve and merge its own changes.
# It is called AUTO-APPROVE and its switch sits beside the chat input, in
# place of an auto-merge button: it gives the host the user's permission to
# merge code. It is not tied to a model. Lives in projects.config (the settings
# bag, init-db.sql:621) alongside build_chain_halt — no migration, and the key
# keeps its old name, `auto_merge`, so every reader stays as it is.

@router.get("/{project_id}/auto-merge",
            dependencies=[Depends(requires_permission("read:projects"))])
async def get_auto_merge(project_id: int):
    pool = _pool()
    await _verify_project(pool, project_id)
    row = await pool.fetchrow(
        "SELECT COALESCE((config->>'auto_merge')::boolean, false) AS auto_merge, "
        "       repo "
        "FROM projects WHERE id = $1", project_id)
    return {"project_id": project_id,
            "auto_merge": bool(row["auto_merge"]) if row else False,
            "repo": (row["repo"] if row else None)}


class SetAutoMerge(BaseModel):
    enabled: bool


@router.put("/{project_id}/auto-merge",
            dependencies=[Depends(requires_permission("write:projects"))])
async def set_auto_merge(project_id: int, body: SetAutoMerge):
    pool = _pool()
    await _verify_project(pool, project_id)
    if body.enabled:
        # Only where the Host can merge — never on the platform repositories,
        # where only the platform owner merges. Turning it OFF is always allowed.
        repo = await project_repository_service(pool, _eid(), project_id)
        if not repo["host_can_merge"]:
            raise HTTPException(409, (
                f"The Host can't merge into {repo['repo'] or 'this project'}'s Code Repository, "
                "so Auto-Approve can't be turned on here."))
    # jsonb_set with create_missing=true, exactly as the halt flag is written
    # (tools.py, action == "stop") — never a whole-config overwrite, which would
    # drop build_chain_halt and anything else the bag is carrying.
    await pool.execute(
        "UPDATE projects SET config = jsonb_set("
        "  COALESCE(config, '{}'::jsonb), '{auto_merge}', $2::jsonb, true"
        "), updated_at = now() WHERE id = $1",
        project_id, "true" if body.enabled else "false")
    logger.info("Auto-Approve (auto_merge) %s for project %d",
                "ENABLED" if body.enabled else "disabled", project_id)
    return {"project_id": project_id, "auto_merge": body.enabled}


# ── Pipeline ────────────────────────────────────────────────────────────────
# The project's build pipeline: one provider per stage, several for check,
# stored as projects.config.pipeline beside auto_merge. Absent = today's
# pipeline. READ-ONLY for now: the setting is written only through the host's
# proposal and the user's approval (build step 3), so no write route exists
# before that approval does.

@router.get("/{project_id}/pipeline",
            dependencies=[Depends(requires_permission("read:projects"))])
async def get_pipeline(project_id: int):
    from mastercode.runtime import pipeline as _pipeline
    pool = _pool()
    await _verify_project(pool, project_id)
    cfg = await pool.fetchval("SELECT config FROM projects WHERE id = $1", project_id)
    if isinstance(cfg, str):
        cfg = json.loads(cfg)
    stored = cfg.get("pipeline") if isinstance(cfg, dict) else None
    effective = _pipeline.resolve(cfg, approved=await _pipeline.load_approved(
        pool, _eid(), project_id, cfg))
    return {"project_id": project_id,
            # What is stored (None = never configured → defaults). A setting
            # that does not validate is not echoed — it may carry the very
            # value the validator refused; effective.error names what is wrong.
            "setting": stored if "error" not in effective else None,
            "configured": stored is not None,
            "effective": effective,
            "providers": [{"key": p.key, "stage": p.stage, "where": p.where,
                           "proves": p.proves, "settings": sorted(p.config_schema)}
                          for p in _pipeline.PROVIDERS.values()]}


# ═════════════════════���════════════════════════════���═══
# ROUTING (model preferences per domain)
# ═══════���════════════════���═════════════════════════════

@router.get("/{project_id}/routing", dependencies=[Depends(requires_permission("read:projects"))])
async def get_routing(project_id: int):
    pool = _pool()
    await _verify_project(pool, project_id)

    rows = await pool.fetch(
        "SELECT domain, preferred_model, reason, set_by, created_at FROM project_routing WHERE project_id = $1 ORDER BY domain",
        project_id,
    )
    return {"routing": [dict(r) for r in rows]}


@router.put("/{project_id}/routing", dependencies=[Depends(requires_permission("write:projects"))])
async def set_routing(project_id: int, body: SetRouting):
    # Thin adapter: set/clear preferred-model logic lives in set_project_routing_service so the
    # set_project_routing host tool shares it. Response shape unchanged.
    try:
        return await set_project_routing_service(
            _pool(), _eid(), project_id,
            body.domain, preferred_model=body.preferred_model, reason=body.reason,
        )
    except ProjectsError as e:
        raise HTTPException(e.status, e.detail)


@router.delete("/{project_id}/routing/{domain}", dependencies=[Depends(requires_permission("write:projects"))])
async def delete_routing(project_id: int, domain: str):
    # Thin adapter: the clear-side of set_project_routing_service (clear=True). Now also
    # entity-scoped via _verify_project inside the service — a tightening of the old route,
    # which deleted by (project_id, domain) with no ownership check. Response shape
    # unchanged on success.
    try:
        return await set_project_routing_service(
            _pool(), _eid(), project_id, domain, clear=True,
        )
    except ProjectsError as e:
        raise HTTPException(e.status, e.detail)


# ═══���═════════════��════════════════════════��═══════════
# HELPERS
# ═══════���══════════════════════════════════════════════

async def _verify_project(pool, project_id: int):
    exists = await pool.fetchval(
        "SELECT 1 FROM projects WHERE id = $1 AND entity_id = $2",
        project_id, _eid(),
    )
    if not exists:
        raise HTTPException(404, "Project not found")


async def _verify_phase(pool, phase_id: int, project_id: int):
    exists = await pool.fetchval(
        "SELECT 1 FROM project_phases WHERE id = $1 AND project_id = $2",
        phase_id, project_id,
    )
    if not exists:
        raise HTTPException(404, "Phase not found")


async def _resolve_model_for_domain(pool, project_id: int, domain: str) -> str | None:
    """The preferred model for a domain, from the project's routing."""
    # Check project-level routing preference
    row = await pool.fetchrow(
        "SELECT preferred_model FROM project_routing WHERE project_id = $1 AND domain = $2",
        project_id, domain,
    )
    if row:
        return row["preferred_model"]

    return None


# ======================================================================
# SERVICE LAYER — shared by the HTTP routes AND the host chat tools.
# Each function holds the EXACT route logic + every invariant (entity
# scoping, manifest validation, 404s). It reads NOTHING from a Request;
# it raises ProjectsError(status, detail) for HTTP-ish failures and
# returns the normal response dict otherwise. The thin routes map
# ProjectsError -> HTTPException; the _exec_* host tools call these
# directly (no _ShimRequest), sharing one audited code path.
# ======================================================================

class ProjectsError(Exception):
    """A projects-domain failure the caller surfaces. `status` is the HTTP code the
    route maps it to; the host tool reports `detail`. Mirrors nac_mgmt.MgmtError so
    the route↔service↔tool split is identical across the codebase."""
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


async def set_project_manifest_service(pool, entity_id: int, project_id: int, yaml_text) -> dict:
    """Write + validate the project's app.yaml so the Workbench can ship it — the SINGLE source of
    truth for PUT /api/projects/{id}/manifest AND the set_project_manifest host tool. Stored at
    /var/lib/cseries/projects/<id>/app.yaml; validated by loading it through the manifest parser and
    ROLLED BACK if it doesn't pass. Entity-scoped: a project the caller doesn't own is 404, never
    written. Raises ProjectsError(status, detail); returns the saved-manifest summary otherwise."""
    from pathlib import Path
    from server.applications.manifest import load as load_manifest, ManifestError

    row = await pool.fetchrow(
        "SELECT id FROM projects WHERE id = $1 AND entity_id = $2",
        project_id, entity_id,
    )
    if row is None:
        raise ProjectsError(404, "Project not found")

    if not yaml_text or not isinstance(yaml_text, str):
        raise ProjectsError(400, "yaml string required")

    proj_dir = Path("/var/lib/cseries/projects") / str(project_id)
    proj_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = proj_dir / "app.yaml"
    manifest_path.write_text(yaml_text)

    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as e:
        # Roll back the write — invalid manifests don't get persisted.
        manifest_path.unlink(missing_ok=True)
        raise ProjectsError(400, f"manifest invalid: {e}") from None

    return {
        "saved": True,
        "manifest_path": str(manifest_path),
        "id": manifest.id,
        "provenance": manifest.provenance,
        "permissions": list(manifest.permissions),
    }


async def get_project_service(pool, entity_id: int, project_id: int) -> dict:
    """Drill into one project — its row, phases (with per-phase step rollups), and routing — the
    SINGLE source of truth for GET /api/projects/{id} AND the get_project host tool. Entity-scoped:
    a project the caller doesn't own is 404. Raises ProjectsError(404) on miss; returns the project
    detail dict otherwise. Response shape is unchanged from the original route."""
    project = await pool.fetchrow(
        "SELECT * FROM projects WHERE id = $1 AND entity_id = $2",
        project_id, entity_id,
    )
    if not project:
        raise ProjectsError(404, "Project not found")

    phases = await pool.fetch("""
        SELECT ph.id, ph.phase_number, ph.title, ph.description, ph.status, ph.due_at,
               (SELECT COUNT(*) FROM phase_steps WHERE phase_id = ph.id) AS step_count,
               (SELECT COUNT(*) FROM phase_steps WHERE phase_id = ph.id AND status = 'done') AS steps_done,
               (SELECT COUNT(*) FROM phase_steps WHERE phase_id = ph.id AND status = 'failed') AS steps_failed
        FROM project_phases ph
        WHERE ph.project_id = $1
        ORDER BY ph.phase_number
    """, project_id)

    routing = await pool.fetch(
        "SELECT domain, preferred_model, reason, set_by FROM project_routing WHERE project_id = $1",
        project_id,
    )

    return {
        "project": dict(project),
        "phases": [dict(p) for p in phases],
        "routing": [dict(r) for r in routing],
    }


# A turn token (session.py: uuid4().hex), as PUT /seen takes one.
_TURN_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
# How many watched replies a page keeps (v217's CHECK holds the same number).
SEEN_TURNS_KEEP = 32


async def mark_project_seen_service(pool, entity_id: int, project_id: int,
                                    turn_token: str | None = None) -> dict:
    """Record that the person has the project's page open now. The mark only
    moves forward (GREATEST keeps a later one already stored, from another
    device or a clock stepped back), so a repeated or late call is harmless.
    updated_at is left alone: opening a page is not activity, and the list
    orders by activity. Any status: an archived page can be read too.

    `turn_token`: the page's turn the person watched end. Its reply's row is
    written after the stream, so it lands after this mark; the token goes on
    projects.seen_turns (newest last, the last SEEN_TURNS_KEEP kept), and the
    list's unseen_landed_at leaves that reply out whenever it lands
    (_PAGE_MARKS_JOINS). A mark without one keeps the tokens already there.
    Raises ProjectsError(400) for a token that is not one, (404) for a project
    the entity does not own."""
    token = turn_token or None
    if token is not None and not _TURN_TOKEN_RE.fullmatch(token):
        raise ProjectsError(400, "turn_token is not a turn token")
    seen = await pool.fetchval(
        "UPDATE projects SET seen_at = GREATEST(seen_at, now()), "
        "seen_turns = CASE WHEN $3::text IS NULL THEN seen_turns "
        "  ELSE (SELECT array_agg(t ORDER BY i) FROM ("
        "          SELECT t, i FROM unnest(array_remove(COALESCE(seen_turns, '{}'::text[]), $3::text)"
        "                                  || $3::text) WITH ORDINALITY AS u(t, i)"
        "           ORDER BY i DESC LIMIT $4) kept) END "
        "WHERE id = $1 AND entity_id = $2 RETURNING seen_at",
        project_id, entity_id, token, SEEN_TURNS_KEEP)
    if seen is None:
        raise ProjectsError(404, "Project not found")
    return {"project_id": project_id, "seen_at": seen}


async def project_page_service(pool, entity_id: int, project_id: int) -> dict:
    """A Workbench page's own data, in one call, read-only and without any
    network call:

      where_this_stands  close_turn.thread_state_parts on the conversation's
                         scope: the text a turn starts from ("Where this
                         thread stands"), always, not only while the
                         consolidator is off, and the same rows as fields.
      totals             context_budget.running_spend on that scope: the
                         turns' own token totals summed, and the dollars as
                         the estimate each reply prints (est_cost_usd), summed
                         and labelled an estimate.
      engines            compute.project_run_usage: the project's coding-engine
                         runs as the Compute app counts and prices them.
      seen_at, last_landed_at, unseen_landed_at, waiting_on_you,
      ask_cards_open, approvals_waiting, approvals_checked_at
                         the list row's marks (_PAGE_MARKS_COLS), read by the
                         same SQL.

    The scope is the one a Workbench project thread is read under
    (routes/brain._thread_src_ctx, which /api/brain/thread-context uses). Each
    of the three reads is best-effort, as the thread-context panel's sections
    are: one that fails is logged and reads null, and the page still opens.
    Raises ProjectsError(404) for a project the entity does not own."""
    marks = await pool.fetchrow(f"""
        WITH {_PAGE_MARKS_CTES}
        SELECT p.id, {_PAGE_MARKS_COLS}
          FROM projects p
          {_PAGE_MARKS_JOINS}
         WHERE p.entity_id = $1 AND p.id = $2
    """, entity_id, project_id)
    if marks is None:
        raise ProjectsError(404, "Project not found")
    from server.routes.brain import _thread_src_ctx
    scope = _thread_src_ctx("workbench", project_id)
    out: dict = {"project_id": project_id, "scope": scope}

    async def _section(name: str, read):
        try:
            out[name] = await read()
        except Exception as e:  # noqa: BLE001 — one section never closes the page
            logger.warning("project page %s: %s unavailable: %s", project_id, name, e)
            out[name] = None

    from mastercode.context_budget import running_spend
    from mastercode.runtime.close_turn import thread_state_parts
    from server.routes.compute import project_run_usage
    await _section("where_this_stands", lambda: thread_state_parts(pool, entity_id, scope))
    await _section("totals", lambda: running_spend(pool, entity_id, scope))
    await _section("engines", lambda: project_run_usage(pool, project_id))
    out.update({k: marks[k] for k in (
        "seen_at", "last_landed_at", "unseen_landed_at", "waiting_on_you", "ask_cards_open",
        "approvals_waiting", "approvals_checked_at")})
    return out


# "Not provided" for update_project_service's bucket_id, where None is itself
# a meaningful value (unfile). A module-level sentinel, not a default of
# None.
_UNSET = object()


async def update_project_service(pool, entity_id: int, project_id: int,
                                 name=None, description=None, status=None,
                                 bucket_id=_UNSET, repo=None) -> dict:
    """Rename / re-status / archive / re-file a project — the SINGLE source of truth
    for PUT /api/projects/{id} AND the update_project host tool. Builds the partial UPDATE over
    only the provided fields, always entity-scoped in the WHERE. Raises ProjectsError(400) when
    nothing is provided and ProjectsError(404) when no owned row matched; returns
    {\"updated\": True} otherwise. (To archive, pass status='archived' — same as the DELETE
    route's effect. bucket_id (v130 folders) files the project: an int must be one of the
    entity's own buckets, None unfiles it, and leaving it at the _UNSET default touches
    nothing.) There is no per-project style — artifacts wear the desktop.
    `repo` mounts a Code Repository onto a ticket's project (mount_ticket_repo_service). With
    other fields in the same call, every field and the mount are checked first (the forge read
    included) and then written in ONE transaction, so a refusal anywhere leaves the project as
    it was; the code is read in only after that commits. A status in the same call is the one
    the mount is judged by: a restore mounts, an archive does not."""
    want_mount = repo is not None and bool(str(repo).strip())
    if want_mount and name is None and description is None and status is None \
            and bucket_id is _UNSET:
        return {"updated": True,
                "repository": await mount_ticket_repo_service(pool, entity_id, project_id, repo)}
    sets, params = [], [project_id, entity_id]
    lock_bucket = None
    if bucket_id is not _UNSET:
        # A project filed somewhere ELSE by this door (the viewer's bulk move)
        # has no place in its new bucket yet: it sorts with the never-placed
        # ones instead of carrying its old bucket's position into this one. A
        # "move" to the bucket it is already in keeps its place. (UPDATE reads
        # the old row, so folder_id here is the bucket it is leaving.)
        if bucket_id is None:
            sets.append("sort_order = CASE WHEN folder_id IS NULL THEN sort_order END")
            sets.append("folder_id = NULL")
        else:
            try:
                fid = int(bucket_id)
            except (TypeError, ValueError):
                raise ProjectsError(400, "bucket_id must be an integer or null")
            owned = await pool.fetchrow(
                "SELECT id FROM project_folders WHERE id = $1 AND entity_id = $2",
                fid, entity_id)
            if not owned:
                raise ProjectsError(404, f"bucket {fid} not found")
            lock_bucket = fid
            params.append(fid)
            sets.append(f"sort_order = CASE WHEN folder_id = ${len(params)} THEN sort_order END")
            sets.append(f"folder_id = ${len(params)}")
    if name is not None:
        params.append(name)
        sets.append(f"name = ${len(params)}")
    if description is not None:
        params.append(description)
        sets.append(f"description = ${len(params)}")
    if status is not None:
        # Refuse an unknown status BY NAME rather than letting it reach the
        # column. This wrote `status` verbatim with no allowlist, and it is the
        # single writer behind BOTH PUT /api/projects/{id} and the
        # update_project host tool — so the only guard was the CHECK
        # constraint, which had drifted off long-lived entities entirely
        # (v111). The result: a value like 'inactive' — which list_projects'
        # own parameter description advertised as a real status — landed
        # silently on an old entity and raised a constraint violation on a new
        # one. Validating here means both behave the same, and the caller is
        # told what it did wrong instead of getting a 500 from the driver.
        status = str(status).strip()
        if status not in PROJECT_STATUSES:
            raise ProjectsError(400, (
                f"unknown project status {status!r} — must be one of: "
                + ", ".join(sorted(PROJECT_STATUSES))))
        params.append(status)
        sets.append(f"status = ${len(params)}")
    if not sets:
        raise ProjectsError(400, "Nothing to update")
    sets.append("updated_at = now()")

    upd = f"""
        UPDATE projects SET {', '.join(sets)}
        WHERE id = $1 AND entity_id = $2
    """
    plan, then = None, None
    if want_mount:
        # Checked (and the forge read) before anything is written; the mount's
        # write then joins the update's transaction.
        plan = await _plan_ticket_mount(pool, entity_id, project_id, repo, status=status)
        if "already" not in plan:
            async def _mount_write(conn):
                return await _write_ticket_mount(conn, entity_id, project_id, plan)
            then = _mount_write
    try:
        out = await _run_project_update(pool, entity_id, upd, params, lock_bucket, then)
    except Exception as e:
        # Restoring an archived project onto a Code Repository a newer live
        # project now holds: one live project per repository (v191).
        if _is_unique_violation(e):
            if "repo_branch" in str(getattr(e, "constraint_name", "") or e):
                # v199: two live projects never work on one branch of a repository.
                raise ProjectsError(409, (
                    "Another live project works on this project's branch of its Code "
                    "Repository. Archive that one first to restore this one.")) from None
            if "support_ticket" in str(getattr(e, "constraint_name", "") or e):
                # v198: one live project per support ticket.
                raise ProjectsError(409, (
                    "This support ticket has another live project now. Archive that one "
                    "first to restore this one.")) from None
            raise ProjectsError(409, (
                "This project's Code Repository has another live project now. Archive that "
                "one first, or start a new project that forks the repository.")) from None
        raise
    meanwhile = out.pop("then", None)
    if plan is not None:
        if "already" in plan:
            out["repository"] = plan["already"]
        elif meanwhile is not None:
            out["repository"] = meanwhile          # mounted on it by another call
        else:
            # Committed: only now is the code read in.
            out["repository"] = await _start_ticket_mount(pool, project_id, plan)
    return out


async def _run_project_update(pool, entity_id: int, upd: str, params: list,
                              lock_bucket, then=None) -> dict:
    """The update, and `then(conn)` — another write that must land with it or not
    at all (a ticket's mount) — in one transaction. Returns {"updated": True},
    plus "then": whatever `then` returned."""
    if lock_bucket is None and then is None:
        result = await pool.execute(upd, *params)
        extra = None
    else:
        async with pool.acquire() as conn:
            async with conn.transaction():
                if lock_bucket is not None:
                    # Filing into a bucket: hold that bucket's drop lock (the one
                    # place_project_service and delete_bucket_service take) and
                    # check it still exists under it. Checked only before, a PUT
                    # racing the bucket's delete wrote folder_id to a deleted
                    # row — a ForeignKeyViolation, a 500 (the route had it
                    # since v130). This takes one advisory lock and
                    # one row, so it cannot close a cycle with a drop, which
                    # takes all its advisory locks first.
                    await conn.execute("SELECT pg_advisory_xact_lock($1::int, $2::int)",
                                       int(entity_id), int(lock_bucket))
                    if not await conn.fetchrow(
                            "SELECT id FROM project_folders WHERE id = $1 AND entity_id = $2",
                            lock_bucket, entity_id):
                        raise ProjectsError(404, f"bucket {lock_bucket} not found")
                result = await conn.execute(upd, *params)
                if "UPDATE 0" in result:
                    raise ProjectsError(404, "Project not found")
                extra = await then(conn) if then is not None else None
    if "UPDATE 0" in result:
        raise ProjectsError(404, "Project not found")
    out = {"updated": True}
    if then is not None:
        out["then"] = extra
    return out


# The rail's manual order inside a bucket. Placed projects first (sort_order),
# then the never-placed ones newest activity first — the same order the client
# sorts by, so "in front of project N" means what the user saw when dropping.
_PLACE_ORDER_SQL = f"p.sort_order ASC NULLS LAST, {_LAST_ACTIVITY_SQL} DESC NULLS LAST, p.id DESC"


async def place_project_service(pool, entity_id: int, project_id: int, *,
                                bucket_id=None, before_id=None) -> dict:
    """Move a project into `bucket_id` (None = unfiled) in front of
    `before_id` (None = last), and renumber that bucket 1..n in one
    transaction. The whole bucket is renumbered — archived projects included,
    since a filter may hide them — so the positions never need a midpoint and
    one drop is one request. updated_at is left alone: reordering a list is
    not work on the project, and it must not reshuffle the recent-activity
    sort."""
    fid = None
    if bucket_id is not None:
        try:
            fid = int(bucket_id)
        except (TypeError, ValueError):
            raise ProjectsError(400, "bucket_id must be an integer or null")
    bid = None
    if before_id is not None:
        try:
            bid = int(before_id)
        except (TypeError, ValueError):
            raise ProjectsError(400, "before_id must be an integer or null")
        if bid == int(project_id):
            raise ProjectsError(400, "a project cannot be placed in front of itself")
    for _attempt in range(3):
        # Where the project is now, read without a lock: the drop has to hold
        # the lock of the bucket it leaves as well as the one it enters.
        src_row = await pool.fetchrow(
            "SELECT folder_id FROM projects WHERE id = $1 AND entity_id = $2",
            int(project_id), entity_id)
        if not src_row:
            raise ProjectsError(404, "Project not found")
        src = src_row["folder_id"]
        try:
            order = await _place_locked(pool, entity_id, int(project_id), src, fid, bid)
        except _MovedMeanwhile:
            continue
        return {"placed": True, "project_id": int(project_id), "bucket_id": fid, "order": order}
    raise ProjectsError(409, "the project kept moving while it was being placed — drop it again")


class _MovedMeanwhile(Exception):
    """The project changed bucket between the unlocked read and the row lock
    (a PUT move takes no bucket lock): roll back and read again."""


async def _place_locked(pool, entity_id: int, project_id: int, src, fid, bid) -> list[int]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            # One drop at a time per bucket, taken BEFORE any row lock, on BOTH
            # buckets the drop touches — the one it leaves and the one it
            # enters — in sorted order. Keyed on the target alone, two drops
            # into one bucket deadlocked on each other's moved row (200 of 200
            # overlapping pairs), and two drops SWAPPING
            # projects between two buckets still did (37 of 40);
            # sorted keys make every pair of drops queue in the same order. It
            # also keeps a drop from renumbering from an order it read before
            # waiting (READ COMMITTED re-checks rows, not the sort). Keyed
            # (entity, bucket; 0 = unfiled — bucket ids start at 1).
            for key in sorted({src or 0, fid or 0}):
                await conn.execute("SELECT pg_advisory_xact_lock($1::int, $2::int)",
                                   int(entity_id), key)
            owned = await conn.fetchrow(
                "SELECT folder_id FROM projects WHERE id = $1 AND entity_id = $2 FOR UPDATE",
                project_id, entity_id)
            if not owned:
                raise ProjectsError(404, "Project not found")
            if owned["folder_id"] != src:
                raise _MovedMeanwhile()
            if fid is not None and not await conn.fetchrow(
                    "SELECT id FROM project_folders WHERE id = $1 AND entity_id = $2",
                    fid, entity_id):
                raise ProjectsError(404, f"bucket {fid} not found")
            rows = await conn.fetch(f"""
                SELECT p.id FROM projects p
                 WHERE p.entity_id = $1 AND p.folder_id IS NOT DISTINCT FROM $2::int
                   AND p.id <> $3
                 ORDER BY {_PLACE_ORDER_SQL}
                 FOR UPDATE OF p
            """, entity_id, fid, project_id)
            order = [int(r["id"]) for r in rows]
            if bid is None:
                order.append(project_id)
            elif bid in order:
                order.insert(order.index(bid), project_id)
            else:
                raise ProjectsError(
                    409, f"project {bid} is not in that bucket any more — reload and drop again")
            await conn.execute("""
                UPDATE projects p
                   SET sort_order = v.ord::int,
                       folder_id = CASE WHEN p.id = $3 THEN $2::int ELSE p.folder_id END
                  FROM unnest($1::int[]) WITH ORDINALITY AS v(id, ord)
                 WHERE p.id = v.id AND p.entity_id = $4
            """, order, fid, project_id, entity_id)
    return order


# ── Buckets (v130 project_folders) ──────────────────────────────────────────

_BUCKET_COUNTS_SQL = """
    COUNT(p.id) FILTER (WHERE p.status IN ('active','review'))::int AS active,
    COUNT(p.id) FILTER (WHERE p.status = 'planning')::int           AS pending,
    COUNT(p.id) FILTER (WHERE p.status = 'complete')::int           AS completed,
    COUNT(p.id) FILTER (WHERE p.status = 'archived')::int           AS archived,
    COUNT(p.id)::int                                                AS total"""

_COLOR_RE = re.compile(r"^(#[0-9a-fA-F]{6}|[a-z][a-z0-9-]{0,23})$")


def _clean_bucket_name(name) -> str:
    n = " ".join(str(name or "").split())
    if not n:
        raise ProjectsError(400, "bucket name required")
    if len(n) > 80:
        raise ProjectsError(400, "bucket name is too long (80 characters max)")
    return n


def _clean_bucket_color(color):
    """A swatch is a #rrggbb or a short token the UI maps to its palette; None
    clears it. Anything else is refused rather than stored for the rail to
    style itself with."""
    if color is None:
        return None
    c = str(color).strip()
    if not c:
        return None
    if not _COLOR_RE.match(c):
        raise ProjectsError(400, "color must be #rrggbb or a short palette token")
    return c


def _is_unique_violation(e: Exception) -> bool:
    return getattr(e, "sqlstate", None) == "23505"


def _counts_of(row) -> dict:
    return {k: int(row[k] or 0) for k in ("active", "pending", "completed", "archived", "total")}


async def list_buckets_service(pool, entity_id: int) -> dict:
    """Every bucket with its per-status project counts, plus the counts of the
    unfiled projects. Counts include archived rows on purpose — a bucket holds
    archived and active projects together."""
    rows = await pool.fetch(f"""
        SELECT f.id, f.name, f.color, f.sort_order, f.created_at, f.updated_at,
               {_BUCKET_COUNTS_SQL}
        FROM project_folders f
        LEFT JOIN projects p ON p.folder_id = f.id AND p.entity_id = f.entity_id
                            AND {_PROJECTS_ONLY_SQL}
        WHERE f.entity_id = $1
        GROUP BY f.id
        ORDER BY f.sort_order, lower(f.name), f.id
    """, entity_id)
    unfiled = await pool.fetchrow(f"""
        SELECT {_BUCKET_COUNTS_SQL}
        FROM projects p WHERE p.entity_id = $1 AND p.folder_id IS NULL
                          AND {_PROJECTS_ONLY_SQL}
    """, entity_id)
    buckets = []
    for r in rows:
        buckets.append({
            "id": r["id"], "name": r["name"], "color": r["color"],
            "sort_order": r["sort_order"],
            "created_at": r["created_at"], "updated_at": r["updated_at"],
            "counts": _counts_of(r),
        })
    return {"buckets": buckets,
            "unfiled": {"counts": _counts_of(unfiled) if unfiled else
                        {k: 0 for k in ("active", "pending", "completed", "archived", "total")}}}


async def create_bucket_service(pool, entity_id: int, name, color=None) -> dict:
    n = _clean_bucket_name(name)
    c = _clean_bucket_color(color)
    # New buckets go last: one past the current highest sort_order.
    try:
        row = await pool.fetchrow("""
            INSERT INTO project_folders (entity_id, name, color, sort_order)
            VALUES ($1, $2, $3,
                    COALESCE((SELECT MAX(sort_order) + 1 FROM project_folders WHERE entity_id = $1), 0))
            RETURNING id, name, color, sort_order, created_at
        """, entity_id, n, c)
    except Exception as e:
        if _is_unique_violation(e):
            raise ProjectsError(409, f"a bucket named {n!r} already exists")
        raise
    return {"id": row["id"], "name": row["name"], "color": row["color"],
            "sort_order": row["sort_order"], "created_at": row["created_at"],
            "counts": {k: 0 for k in ("active", "pending", "completed", "archived", "total")}}


async def update_bucket_service(pool, entity_id: int, bucket_id: int,
                                name=None, color=None, sort_order=None) -> dict:
    """Rename / recolor / reorder. Partial, entity-scoped, same shape as
    update_project_service. color='' clears the swatch."""
    sets, params = [], [bucket_id, entity_id]
    if name is not None:
        params.append(_clean_bucket_name(name))
        sets.append(f"name = ${len(params)}")
    if color is not None:
        c = _clean_bucket_color(color)
        if c is None:
            sets.append("color = NULL")
        else:
            params.append(c)
            sets.append(f"color = ${len(params)}")
    if sort_order is not None:
        try:
            params.append(int(sort_order))
        except (TypeError, ValueError):
            raise ProjectsError(400, "sort_order must be an integer")
        sets.append(f"sort_order = ${len(params)}")
    if not sets:
        raise ProjectsError(400, "Nothing to update")
    sets.append("updated_at = now()")
    try:
        result = await pool.execute(
            f"UPDATE project_folders SET {', '.join(sets)} WHERE id = $1 AND entity_id = $2",
            *params)
    except Exception as e:
        if _is_unique_violation(e):
            raise ProjectsError(409, "a bucket with that name already exists")
        raise
    if "UPDATE 0" in result:
        raise ProjectsError(404, "bucket not found")
    return {"updated": True}


async def delete_bucket_service(pool, entity_id: int, bucket_id: int) -> dict:
    """Delete the bucket row and nothing else. The FK (ON DELETE SET NULL)
    unfiles its projects; the count returned is how many that was, so the UI
    can say "12 projects are now unfiled" rather than implying a loss."""
    owned = await pool.fetchrow(
        "SELECT id FROM project_folders WHERE id = $1 AND entity_id = $2",
        bucket_id, entity_id)
    if not owned:
        raise ProjectsError(404, "bucket not found")
    async with pool.acquire() as conn:
        async with conn.transaction():
            # The bucket's drop lock (place_project_service), so a delete and a
            # drop landing in this bucket queue instead of deadlocking on its
            # rows; unfiled (0) too, since its projects land there.
            for key in (0, int(bucket_id)):
                await conn.execute("SELECT pg_advisory_xact_lock($1::int, $2::int)",
                                   int(entity_id), key)
            n = await conn.fetchval(
                "SELECT COUNT(*) FROM projects WHERE folder_id = $1 AND entity_id = $2",
                bucket_id, entity_id)
            # Its projects come back unfiled with no position: the numbers they
            # had ranked them in the bucket being deleted, not among the unfiled.
            await conn.execute(
                "UPDATE projects SET sort_order = NULL WHERE folder_id = $1 AND entity_id = $2",
                bucket_id, entity_id)
            await conn.execute(
                "DELETE FROM project_folders WHERE id = $1 AND entity_id = $2",
                bucket_id, entity_id)
    return {"deleted": True, "unfiled": int(n or 0)}


# ── The project index (v131): POST /api/projects/search ──────────────────────
#
# Two arms, fused, the lexical one as the floor:
#   lexical   core Postgres FTS over name (A) / description (B) / phase titles
#             (C) — every typed word as a prefix term, OR-joined, so partial
#             typing matches and ts_rank_cd rewards rows that match more of
#             them — plus a name ILIKE boost. No extension: pg_trgm is not
#             installed (init-db.sql creates vector/pgcrypto/age only).
#   semantic  ONLY when the host has an embedding model configured: the query
#             is embedded on the host's own endpoint and compared (cosine)
#             against the stored per-project embedding. Unconfigured = the arm
#             is absent and the response says semantic:false — never "search
#             is down" (default-skills/embedding.md: no platform embedder).
# The whole entity's project set is fetched (hundreds of rows at most) and
# fused in Python, which is what lets the semantic arm use a REAL[] column
# at whatever dim the entity's model produces.
#
# Ranking defaults, chosen here rather than asked: relative floor 0.15 × the
# top score, ties broken by last_activity_at DESC, and the popup asks for 5.

_SEARCH_NAME_BOOST = 0.35     # added when the name itself contains the text
_SEARCH_SEMANTIC_WEIGHT = 0.6  # cosine similarity's share when the arm is present
_SEARCH_RELATIVE_FLOOR = 0.15  # drop rows scoring under this × the top row
_SEARCH_SEMANTIC_MIN = 0.25    # a cosine below this is noise, not a match
_EMBED_REFRESH_BATCH = 40      # stale rows re-embedded per background pass
_EMBED_REFRESH_INFLIGHT: set = set()   # entity ids with a refresh running

_SEARCH_ROWS_SQL = f"""
    WITH docs AS (
        SELECT p.id, p.name, p.description, p.status, p.folder_id, p.repo,
               p.created_at, p.updated_at,
               p.search_embedding, p.search_embedding_model, p.search_embedding_hash,
               (SELECT COUNT(*) FROM project_phases WHERE project_id = p.id)::int AS phase_count,
               COALESCE((SELECT string_agg(title, ' ' ORDER BY phase_number)
                           FROM project_phases WHERE project_id = p.id), '') AS phase_titles,
               {_LAST_ACTIVITY_SQL} AS last_activity_at
        FROM projects p
        WHERE p.entity_id = $1 AND {_PROJECTS_ONLY_SQL}
    )
    SELECT d.*,
           CASE WHEN $2 = '' THEN 0::real ELSE ts_rank_cd(
               setweight(to_tsvector('english', COALESCE(d.name, '')), 'A')
               || setweight(to_tsvector('english', COALESCE(d.description, '')), 'B')
               || setweight(to_tsvector('english', d.phase_titles), 'C'),
               to_tsquery('english', $2)) END AS lex_rank,
           ($3 <> '' AND d.name ILIKE $3) AS name_hit
    FROM docs d
    -- BOUND THE CANDIDATE SET. Every row carries search_embedding, a REAL[] of
    -- one vector per project, and this runs once per keystroke past the 200 ms
    -- debounce — against the entity pod's postgres, which runs
    -- max_connections=16 and is shared with every app in that Workbench. An
    -- unbounded fetch makes a host with a few hundred projects pay hundreds of
    -- vectors per keypress.
    --
    -- The ordering is what makes the cap safe rather than lossy: whatever the
    -- lexical arm matched comes first, then a name hit, then most-recent
    -- activity. The rows that could win a score are the rows that survive, and
    -- the tail that gets dropped is the tail that scored nothing and has not
    -- been touched. $4 is the ceiling.
    ORDER BY lex_rank DESC NULLS LAST,
             ($3 <> '' AND d.name ILIKE $3) DESC,
             d.last_activity_at DESC NULLS LAST,
             d.id DESC
    LIMIT $4
"""

# How many projects a single search may score. Comfortably above what a person
# accumulates (the popup asks for 5, and the relative floor discards most of
# what it scores), and low enough that a keystroke never drags the pod's DB.
_SEARCH_CANDIDATE_CAP = 300


def _search_tsquery(q: str) -> str:
    """Every word a prefix term, OR-joined: "trad soft" → 'trad':* | 'soft':*.
    Empty when nothing survives (punctuation only) — the SQL then skips the
    tsquery arm instead of parsing an empty string."""
    words = [w for w in re.findall(r"[^\W_]+", q.lower()) if w]
    return " | ".join(f"'{w}':*" for w in words[:12])


def _search_text_of(name, description, phase_titles) -> str:
    parts = [str(name or "").strip(), str(description or "").strip(), str(phase_titles or "").strip()]
    return "\n".join(p for p in parts if p)


def _search_hash(model: str, text: str) -> str:
    return hashlib.sha1(f"{model}\n{text}".encode("utf-8", "replace")).hexdigest()


def _cosine(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / math.sqrt(na * nb)


async def _search_embedding_lane(pool, entity_id: int):
    """(embed, model): the entity's configured embedding model and a callable
    that embeds text on the host's own endpoint — or (None, "") when no model
    is configured. Never raises; a lane that cannot answer is simply absent."""
    try:
        from mastercode.memory_engine import (
            _embed_text_config_driven, _entity_embedding_config,
        )
        _, model = await _entity_embedding_config(pool, entity_id)
        model = (model or "").strip()
        if not model:
            return None, ""

        async def embed(text: str):
            return await _embed_text_config_driven(
                text[:2000], model, entity_id=entity_id, pool=pool)
        return embed, model
    except Exception as e:
        logger.debug("project search: embedding lane unavailable (non-fatal): %s", e)
        return None, ""


async def _refresh_project_embeddings(pool, entity_id: int, embed, model: str, stale: list) -> int:
    """Embed up to _EMBED_REFRESH_BATCH stale projects and store the vector
    with the model + text-hash stamp. Fail-soft per row; returns the count
    written. Runs in the background after a search, one pass per entity at a
    time, so a 300-project entity switching models converges over a few
    searches rather than blocking one of them."""
    if entity_id in _EMBED_REFRESH_INFLIGHT:
        return 0
    _EMBED_REFRESH_INFLIGHT.add(entity_id)
    written = 0
    try:
        for pid, text, h in stale[:_EMBED_REFRESH_BATCH]:
            try:
                vec = await embed(text)
                if not vec:
                    break  # the lane went quiet (down / cooldown) — try next search
                await pool.execute(
                    "UPDATE projects SET search_embedding = $3::real[], "
                    "search_embedding_model = $4, search_embedding_hash = $5 "
                    "WHERE id = $1 AND entity_id = $2",
                    pid, entity_id, [float(v) for v in vec], model, h)
                written += 1
            except Exception as e:
                logger.debug("project search: embed refresh failed for %s (non-fatal): %s", pid, e)
    finally:
        _EMBED_REFRESH_INFLIGHT.discard(entity_id)
    return written


def search_recency(r) -> tuple:
    """A search row's tie-break: most recent activity, then the newer id."""
    la = r["last_activity_at"]
    return (la.timestamp() if la is not None else 0.0, int(r["id"]))


def search_text_scores(rows, *, model: str, qvec, semantic: bool, sim_of=None) -> tuple[list, list]:
    """The project search's own score for each _SEARCH_ROWS_SQL row: the
    lexical rank as a share of the best one, the name boost, and the cosine
    share when the semantic arm ran. Returns (scored, stale): `scored` is
    (score, search_recency, row) for every row scoring above 0, unsorted;
    `stale` is (id, text, hash) for every row whose stored embedding is not
    the current model's over its current text. The Start dialog's search and
    the Workbench Index (routes/workbench_index.py) both rank by it.

    `sim_of(row)`: the row's cosine already computed, for a caller whose read
    computed it in the database instead of returning the stored vector; by
    default it is computed here from row["search_embedding"]."""
    max_lex = max((float(r["lex_rank"] or 0.0) for r in rows), default=0.0)
    scored, stale = [], []
    for r in rows:
        text = _search_text_of(r["name"], r["description"], r["phase_titles"])
        h = _search_hash(model, text) if model else None
        if model and (r["search_embedding_model"] != model or r["search_embedding_hash"] != h):
            stale.append((r["id"], text, h))
        lex = (float(r["lex_rank"] or 0.0) / max_lex) if max_lex > 0 else 0.0
        score = lex + (_SEARCH_NAME_BOOST if r["name_hit"] else 0.0)
        sim = 0.0
        if semantic and r["search_embedding_model"] == model:
            if sim_of is not None:
                sim = sim_of(r)
            elif r["search_embedding"]:
                sim = _cosine(qvec, r["search_embedding"])
            if sim >= _SEARCH_SEMANTIC_MIN:
                score += _SEARCH_SEMANTIC_WEIGHT * sim
        if score > 0:
            scored.append((score, search_recency(r), r))
    return scored, stale


def refresh_stale_embeddings(pool, entity_id: int, embed, model: str, stale: list) -> None:
    """Start the background re-embed of `stale` rows (one pass per entity at
    a time, _refresh_project_embeddings); nothing when there is no running
    loop (a sync test harness): the next search retries."""
    try:
        asyncio.get_running_loop().create_task(
            _refresh_project_embeddings(pool, entity_id, embed, model, stale))
    except RuntimeError:
        pass


async def search_projects_service(pool, entity_id: int, q, limit: int = 8, *,
                                  lane=None, refresh_in_background: bool = True) -> dict:
    """Rank the entity's projects against `q`. Returns
    {projects: [...ranked rows...], semantic: bool, q}. An empty query returns
    the most recently active non-archived projects with score null — the
    popup's "before you type" state. `lane` overrides the embedding lane
    lookup ((embed, model) or (None, "")) so tests can drive both paths.

    The search reads `q` with its secret shapes cut, as the Workbench Index's
    search reads it (secret_cut.search_words): the box is where the person
    types the message they are about to send, and the semantic arm sends the
    query to the Host's embedding endpoint, which can be a keyed outside
    provider. Words with no secret in them read as before."""
    q = " ".join(secret_cut.search_words(q, door="project search", log=logger).split())[:500]
    limit = max(1, min(50, int(limit or 8)))
    tsq = _search_tsquery(q)
    like = f"%{q}%" if q else ""
    rows = await pool.fetch(_SEARCH_ROWS_SQL, entity_id, tsq, like, _SEARCH_CANDIDATE_CAP)

    def base(r) -> dict:
        return {
            "id": r["id"], "name": r["name"], "description": r["description"],
            "status": r["status"], "group": project_group(r["status"]),
            "bucket_id": r["folder_id"], "repo": r["repo"],
            "phase_count": int(r["phase_count"] or 0),
            "last_activity_at": r["last_activity_at"],
        }

    if not q:
        recent = sorted((r for r in rows if r["status"] != "archived"), key=search_recency, reverse=True)
        return {"projects": [dict(base(r), score=None) for r in recent[:limit]],
                "semantic": False, "q": q}

    # Semantic arm — present only when a model is configured AND the query
    # embedded; otherwise absent, silently.
    embed, model = lane if lane is not None else await _search_embedding_lane(pool, entity_id)
    qvec = None
    if embed is not None:
        try:
            qvec = await embed(q)
        except Exception as e:
            logger.debug("project search: query embedding failed (non-fatal): %s", e)
            qvec = None
    semantic = bool(qvec)

    scored, stale = search_text_scores(rows, model=model, qvec=qvec, semantic=semantic)

    if model and stale and refresh_in_background:
        refresh_stale_embeddings(pool, entity_id, embed, model, stale)

    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    top = scored[0][0] if scored else 0.0
    out = []
    for score, _rec, r in scored:
        if score < top * _SEARCH_RELATIVE_FLOOR:
            break
        out.append(dict(base(r), score=round(score, 4)))
        if len(out) >= limit:
            break
    return {"projects": out, "semantic": semantic, "q": q}


async def get_project_activity_service(pool, entity_id: int, project_id: int,
                                       since=None, limit: int = 80, before=None) -> dict:
    """Live-view activity feed for a project — the SINGLE source of truth for GET
    /api/projects/{id}/activity AND the get_project_activity host tool. Merges the Host's tool
    calls (tool_call_log, one row per call: the ones that named this project plus a 2h
    recent-window union; args are the ledger's scrubbed preview) with deep_research findings (entity_memory memory_type='research'), newest first. Entity-scoped:
    a project the caller doesn't own is 404. `since` is an ISO timestamp; events strictly newer are
    returned. `before` pages OLDER (without it the feed stopped at the
    newest 200 and nothing older was reachable): pass the previous answer's `next_before`, a
    compound "<iso>|<source>|<id>" cursor, so events sharing a timestamp are
    never skipped and both sources are cut at the same key; a bare ISO timestamp still means
    strictly older. `next_before` is None only when nothing older exists. Raises
    ProjectsError(404 / 400); returns {\"events\": [...], \"project_id\": id,
    \"next_before\": cursor|None}."""
    proj = await pool.fetchval(
        "SELECT id FROM projects WHERE id = $1 AND entity_id = $2",
        project_id, entity_id,
    )
    if not proj:
        raise ProjectsError(404, "project not found")

    # Clamp the limit here (the route's Query(le=200) only bounds the HTTP path;
    # the host tool reaches the service directly, so the cap lives with the logic).
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 80
    limit = max(1, min(200, limit))

    # asyncpg binds timestamptz params strictly — a raw `since` string raises
    # DataError, so parse it up front and reject a bad value as a 400.
    since_dt = None
    if since:
        from datetime import datetime as _dt
        try:
            since_dt = _dt.fromisoformat(str(since).replace("Z", "+00:00"))
        except ValueError:
            raise ProjectsError(400, "since must be an ISO timestamp")
    # The `before` cursor. A bare ISO timestamp — the
    # first form, still accepted — keeps "strictly older". The form this
    # service answers is the compound key of the last event a page returned,
    # "<iso>|<source>|<id>": every event sorts on (created_at, source, id), so
    # two events in the same microsecond are never skipped, and both sources
    # are cut at the same key.
    cur = _parse_activity_cursor(before)

    # ── Tool calls ── one tool_call_log row per call (the runtime's ledger,
    # runtime/tool_call_ledger.py); project_id is stamped from the call's args.
    tool_fetch = limit + 1
    research_fetch = limit + 1
    tool_params: list = [entity_id, int(project_id)]
    tool_since_clause = ""
    if since_dt:
        tool_params.append(since_dt)
        tool_since_clause = f"AND created_at > ${len(tool_params)}"
    if cur:
        tool_since_clause += " " + _activity_cursor_sql("t", cur, tool_params)

    tool_sql = f"""
        SELECT id, tool, reason, status, args, latency_ms, created_at
          FROM (SELECT id, tool, reason, status, args, latency_ms, project_id,
                       at AS created_at
                  FROM tool_call_log WHERE entity_id = $1) t
         WHERE (project_id = $2 OR created_at > NOW() - INTERVAL '2 hours')
           {tool_since_clause}
         ORDER BY created_at DESC, id DESC
         LIMIT {int(tool_fetch)}
    """
    try:
        tool_rows = await pool.fetch(tool_sql, *tool_params)
    except Exception:
        tool_rows = []

    # ── Research findings ──
    research_params: list = [entity_id, project_id]
    research_since_clause = ""
    if since_dt:
        research_params.append(since_dt)
        research_since_clause = f"AND created_at > ${len(research_params)}"
    if cur:
        research_since_clause += " " + _activity_cursor_sql("r", cur, research_params)

    research_sql = f"""
        SELECT id, content, metadata, created_at
          FROM entity_memory
         WHERE entity_id = $1
           AND project_id = $2
           AND memory_type = 'research'
           AND status = 'active'
           {research_since_clause}
         ORDER BY created_at DESC, id DESC
         LIMIT {int(research_fetch)}
    """
    try:
        research_rows = await pool.fetch(research_sql, *research_params)
    except Exception:
        research_rows = []

    events: list[dict] = []

    for r in tool_rows:
        args = r["args"]
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        if not isinstance(args, dict):
            args = {}
        # The ledger keeps a scrubbed, length-capped JSON preview of the call's
        # args (runtime/tool_call_ledger._preview). Shown as the args when it
        # parses; a preview cut mid-value stays a string under "preview".
        _pv = args.get("preview")
        if isinstance(_pv, str):
            try:
                _parsed = json.loads(_pv)
                args = _parsed if isinstance(_parsed, dict) else {"preview": _pv}
            except Exception:
                args = {"preview": _pv}
        ts = r["created_at"]
        events.append({
            "kind": "tool_call",
            "tool": r["tool"],
            "status": r["status"],
            "error": None if r["reason"] == "ok" else r["reason"],
            "args": args,
            "result": None,
            "elapsed_ms": r["latency_ms"],
            "event_type": "tool_call",
            "at": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "_key": _activity_key(ts, "t", r["id"]),
        })

    for r in research_rows:
        md = r["metadata"]
        if isinstance(md, str):
            try:
                md = json.loads(md)
            except Exception:
                md = {}
        if not isinstance(md, dict):
            md = {}
        events.append({
            "kind": "research",
            "memory_id": r["id"],
            "research_id": md.get("research_id"),
            "source_kind": md.get("source_kind", "research_summary"),
            "content": r["content"] or "",
            "metadata": md,
            "at": r["created_at"].isoformat() if hasattr(r["created_at"], "isoformat") else str(r["created_at"]),
            "_key": _activity_key(r["created_at"], "r", r["id"]),
        })

    # A source that filled its fetch has rows older than its last one that
    # this answer has not seen; no event older than that row may be returned,
    # or the next page's cursor would step over them. The horizon is the
    # newest such row; the page is cut there and at `limit`.
    horizon = None
    for rows_, fetched, src in ((tool_rows, tool_fetch, "t"),
                                (research_rows, research_fetch, "r")):
        if len(rows_) >= fetched:
            last = _activity_key(rows_[-1]["created_at"], src, rows_[-1]["id"])
            horizon = last if horizon is None else max(horizon, last)
    events.sort(key=lambda e: e["_key"], reverse=True)
    if horizon is not None:
        events = [e for e in events if e["_key"] >= horizon]
    page = events[:limit]
    more = horizon is not None or len(events) > limit
    if page:
        last_key = page[-1]["_key"]
    elif horizon is not None:
        last_key = horizon    # everything fetched de-duplicated away: step past it
    else:
        last_key = None
    for e in page:
        e.pop("_key", None)
    return {"events": page, "project_id": project_id,
            "next_before": _format_activity_cursor(last_key) if (more and last_key) else None}


# ── The activity feed's compound cursor ───────────────────────────────────

def _activity_key(ts, src: str, row_id) -> tuple:
    """The total order the feed is drawn in, newest first: (created_at,
    source, id). Two events in the same microsecond still sort apart."""
    try:
        ts_key = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        from datetime import datetime as _dt
        ts_val = _dt.fromisoformat(ts_key.replace("Z", "+00:00"))
    except Exception:
        ts_val = ts
    return (ts_val, src, int(row_id or 0))


def _format_activity_cursor(key: tuple) -> str:
    ts, src, row_id = key
    return f"{ts.isoformat()}|{src}|{row_id}"


def _parse_activity_cursor(before):
    """(ts, source | None, id | None) from `before`, or None when absent. A
    bare ISO timestamp (the first form) is (ts, None, None): strictly older."""
    if not before:
        return None
    from datetime import datetime as _dt
    raw = str(before)
    parts = raw.split("|")
    try:
        ts = _dt.fromisoformat(parts[0].replace("Z", "+00:00"))
    except ValueError:
        raise ProjectsError(400, "before must be an ISO timestamp or a next_before cursor")
    if len(parts) == 1:
        return (ts, None, None)
    if len(parts) != 3 or parts[1] not in ("t", "r"):
        raise ProjectsError(400, "before must be an ISO timestamp or a next_before cursor")
    try:
        return (ts, parts[1], int(parts[2]))
    except ValueError:
        raise ProjectsError(400, "before must be an ISO timestamp or a next_before cursor")


def _activity_cursor_sql(src: str, cur: tuple, params: list) -> str:
    """The SQL for "this source's rows older than the cursor", in the feed's
    (created_at, source, id) order. Appends its parameters to `params`."""
    ts, csrc, cid = cur
    params.append(ts)
    t = f"${len(params)}"
    if csrc is None:
        return f"AND created_at < {t}"
    if src < csrc:      # this source sorts below the cursor's at equal time
        return f"AND created_at <= {t}"
    if src > csrc:
        return f"AND created_at < {t}"
    params.append(cid)
    return f"AND (created_at, id) < ({t}, ${len(params)})"


async def set_project_routing_service(pool, entity_id: int, project_id: int,
                                      domain, preferred_model=None, reason=None,
                                      clear: bool = False) -> dict:
    """Set OR clear the preferred model for a project domain — the SINGLE source of truth for
    PUT /api/projects/{id}/routing (set) + DELETE /api/projects/{id}/routing/{domain} (clear) AND
    the set_project_routing host tool. Entity-scoped via _verify_project (404 if not owned — this
    also tightens the old DELETE route, which lacked the ownership check). When clear=True, deletes
    the (project, domain) routing row (404 if absent, matching the DELETE route). Otherwise upserts
    (project, domain) -> preferred_model with set_by='user'. Raises ProjectsError; returns
    {\"set\": True, ...} on set or {\"cleared\": True, ...} on clear."""
    await _verify_project(pool, project_id)

    domain = (domain or "").strip()
    if not domain:
        raise ProjectsError(400, "domain required")

    if clear:
        result = await pool.execute(
            "DELETE FROM project_routing WHERE project_id = $1 AND domain = $2",
            project_id, domain,
        )
        if "DELETE 0" in result:
            raise ProjectsError(404, "Routing entry not found")
        return {"cleared": True, "domain": domain}

    if not preferred_model or not isinstance(preferred_model, str) or not preferred_model.strip():
        raise ProjectsError(400, "preferred_model required to set routing")

    await pool.execute("""
        INSERT INTO project_routing (project_id, domain, preferred_model, reason, set_by)
        VALUES ($1, $2, $3, $4, 'user')
        ON CONFLICT (project_id, domain)
        DO UPDATE SET preferred_model = $3, reason = $4, set_by = 'user', created_at = now()
    """, project_id, domain, preferred_model, reason)

    return {"set": True, "domain": domain, "model": preferred_model}


# NOTE on placement: insert this whole block ABOVE the routes in projects.py (e.g. right after the
# `_eid()` helper, before the PROJECTS section). The service uses module-level `json`,
# `_verify_project`, and `_resolve_model_for_domain`, which already exist in projects.py — do not
# re-import/redeclare them. `_verify_project` raises HTTPException(404); that's only reachable from
# the set_project_routing path via an owned-project miss, and the host tool wrapper converts any
# raised exception to an error string, so no HTTPException leaks out of the tool surface.
