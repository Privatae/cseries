"""
Code Approvals — the plain-English face of a project's pull request.

Nobody has to know what a pull request is. Every one the Host opens has one
fixed shape, written here from records and never by a model:

  Title              what it does, under 70 characters: the plan's own words
                     for the one save point in it, else the person's request
                     (the project's description), else the project's name.
  What this changes  one line per save point, in the plan's words.
  Why                the person's request, and a link back to the project.
  What was checked   the build contract and each check, in plain words.
  Who wrote it       per save point, the coding tool and the model its run
                     recorded (harness_run, found through the commit's
                     Host-Run trailer), or the Host itself, or whoever
                     committed from the editor.
  Details            files changed and where it lands, folded.

Commits never name a model: the no-attribution rule covers commits and source
(project_git.commit_message, scripts/test_commit_run_link.py). The Code
Approval is where a reviewer reads which models wrote the code.

Approving is the person's act, never the Host's: a merge button in a UI merges
as the person clicking it. On the platform forge the
Approve button opens a window on the platform's own site, which reads the
change from the forge and, on the person's click there, merges it with the
person's own forge account. On a git server the person connected in Settings
(server/git_server.py) it opens the pull request's own page on that server,
where they merge signed in as themselves. This pod holds no credential that
can merge as the person, so nothing here merges. The Host merges only under Auto-Approve
(project_git.merge_project_pr), as itself, and every merge — the person's or
the Host's — gets one line in the project's chat, written from the forge's own
merge record (announce_merge).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import types
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote, urlencode

logger = logging.getLogger("privatae.pc.code_approvals")

# The format's version, first in every body the Host writes. A body without it
# was written by someone else (a person, another tool) and is shown as it is.
MARKER = "<!-- code-approval v1 -->"
# Hidden, so the words the person reads can change without changing what
# auto-merge refuses on (project_git._merge_rolling_pr).
VIOLATIONS_MARKER = "<!-- build-contract: violations -->"
# The phrase the format before this one carried; a pull request written by
# an older Host still says it until its next save point rewrites the body.
LEGACY_VIOLATIONS_PHRASE = "violations (not ready to accept)"
_MODELS_RE = re.compile(r"<!-- models: ([^>]*) -->")
# One hidden line per part of the project the build contract refused — ""
# is the root app, else a module's name. A save point clears only the part it
# checked (project_git._ensure_pr), so a refusal survives both a save point
# that ran no contract and a check of some other part.
_SCOPE_MARK = "<!-- build-contract: violations in {} -->"
_SCOPE_RE = re.compile(r"<!-- build-contract: violations in (.*?) -->")
_ROOT_SCOPE = "the app"


def violation_scopes(body: Optional[str]) -> set:
    """The parts of the project a Code Approval's body stands refused on."""
    b = body or ""
    scopes = {("" if m == _ROOT_SCOPE else m) for m in _SCOPE_RE.findall(b)}
    if not scopes and has_violations(b):
        scopes = {""}          # an older body: one refusal, for the whole project
    return scopes


def unchecked_violation(scopes) -> str:
    """The line a refusal carried from an earlier save point reads as."""
    names = sorted(("the app" if s == "" else s) for s in scopes)
    return (f"an earlier save point broke the build contract in {', '.join(names)}, and no "
            "build has checked it again since")

TITLE_MAX = 70
_ITEMS_MAX = 12

# The platform's own site: where the approve window lives. Never this Host's
# address — the whole point is a page the Host's code cannot script.
PLATFORM_SITE = os.environ.get("PLATFORM_SITE_URL", "https://privatae.ai").rstrip("/")


def has_violations(body: Optional[str]) -> bool:
    """Did the save point this body describes break the build contract?"""
    b = body or ""
    return VIOLATIONS_MARKER in b or LEGACY_VIOLATIONS_PHRASE in b


# ── what a save point did, in the plan's words ──────────────────────────────
# The save-point labels are plumbing (tools._save_point, _exec_workspace_file,
# review.adopt_run_service / undo_adopt, projects._commit_pointer_move). Each
# shape maps to a sentence; anything else is shown as written.
_PHASE = re.compile(r"^(?:(?P<mod>[^:]+?): )?phase \d+: (?P<t>.+)$", re.S)
_BUILD = re.compile(r"^(?:(?P<mod>[^:]+?): )?build: (?P<t>.+)$", re.S)
_HOST = re.compile(r"^host: (?P<op>write|delete) (?P<p>.+)$", re.S)
_ADOPT = re.compile(r"^adopt run \S+: (?P<h>\S+)(?: into (?P<into>.+))?$", re.S)
_CURRENT = re.compile(r"^(?P<mod>.+): version (?P<n>\d+) is current$", re.S)
_BACK = re.compile(r"^(?P<mod>.+): back to version (?P<n>\d+)$", re.S)
_PLAN_END = re.compile(r"^plan end: ", re.S)


def _sentence(s: str) -> str:
    s = " ".join(str(s or "").split())
    return (s[:1].upper() + s[1:]) if s else s


def harness_label(harness: Optional[str]) -> str:
    """The coding tool's own name (Claude Code, Codex, …)."""
    h = str(harness or "").strip()
    try:
        from server.routes.inference import HARNESS_LABELS
        return HARNESS_LABELS.get(h) or h or "a coding tool"
    except Exception:  # noqa: BLE001 — a label is display; the id still reads
        return h or "a coding tool"


def plain_item(subject: str) -> Optional[str]:
    """One save point's commit subject as a sentence, or None for bookkeeping
    a person has no use for (the plan-end sweep)."""
    s = str(subject or "").strip()
    if not s or _PLAN_END.match(s):
        return None
    m = _PHASE.match(s) or _BUILD.match(s)
    if m:
        t = _sentence(m.group("t"))
        return f"{t} (in {m.group('mod')})" if m.group("mod") else t
    m = _HOST.match(s)
    if m:
        verb = "changed" if m.group("op") == "write" else "removed"
        return f"The Host {verb} {m.group('p').strip()}"
    m = _ADOPT.match(s)
    if m:
        into = f" for {m.group('into').strip()}" if m.group("into") else ""
        return f"Kept the version {harness_label(m.group('h'))} wrote{into}"
    m = _CURRENT.match(s)
    if m:
        return f"Switched {m.group('mod')} to version {m.group('n')}"
    m = _BACK.match(s)
    if m:
        return f"Put {m.group('mod')} back to version {m.group('n')}"
    return _sentence(s)


def _clip(s: str, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def make_title(items: list, description: str, project_name: str) -> str:
    """Under 70 characters, in the person's words — never `Build: <project>`."""
    distinct = list(dict.fromkeys(i for i in items if i))
    if len(distinct) == 1:
        return _clip(distinct[0], TITLE_MAX)
    first = (description or "").strip().splitlines()[0] if (description or "").strip() else ""
    first = re.split(r"(?<=[.!?])\s", first, maxsplit=1)[0] if first else ""
    return _clip(_sentence(first) or _sentence(project_name) or "Changes to this project", TITLE_MAX)


def _run_ids(message: str) -> list:
    from server.routes.project_git import RUN_TRAILER
    out = []
    for line in (message or "").splitlines():
        m = re.match(rf"^{re.escape(RUN_TRAILER)}:\s*(\d+)\s*$", line.strip())
        if m:
            out.append(int(m.group(1)))
    return out


def _model_words(model: Optional[str]) -> str:
    m = str(model or "").strip()
    return "its default model" if m in ("", "default") else m


def _commit_time(c: dict) -> datetime:
    info = c.get("commit") or {}
    for who in ("committer", "author"):
        raw = (info.get(who) or {}).get("date") or c.get("created")
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
    return datetime.min.replace(tzinfo=timezone.utc)


async def writers_of(pool, commits: list, host_login: Optional[str]) -> list:
    """Per commit, oldest first: {item, writer, models}. `commits` is the
    forge's commit list for the pull request, in whatever order it gives.
    The run a commit carries is the record of who wrote it; with no run, the
    commit's author is (the Host itself, or a person in the editor)."""
    ids = sorted({i for c in commits for i in _run_ids((c.get("commit") or {}).get("message"))})
    runs = {}
    if ids and pool is not None:
        try:
            rows = await pool.fetch(
                "SELECT id, harness, model FROM harness_run WHERE id = ANY($1::bigint[])", ids)
            runs = {int(r["id"]): r for r in rows}
        except Exception as e:  # noqa: BLE001 — the body still says what changed
            logger.warning("code approval: run lookup failed: %s", e)
    out = []
    for c in sorted(commits or [], key=_commit_time):
        msg = (c.get("commit") or {}).get("message") or ""
        item = plain_item(msg.splitlines()[0] if msg else "")
        if item is None:
            continue
        rs = [runs[i] for i in _run_ids(msg) if i in runs]
        if rs:
            parts = list(dict.fromkeys(
                f"{harness_label(r['harness'])} on {_model_words(r['model'])}" for r in rs))
            writer = " and ".join(parts)
            models = [str(r["model"]) for r in rs if str(r["model"] or "") not in ("", "default")]
        else:
            login = ((c.get("author") or {}).get("login") or "").strip()
            name = ((c.get("commit") or {}).get("author") or {}).get("name") or login
            if host_login and login.lower() == host_login.lower():
                writer = "the Host itself"
            elif name and host_login and name.lower() == host_login.lower():
                writer = "the Host itself"
            else:
                writer = f"{name or 'someone'} in the editor"
            models = []
        out.append({"item": item, "writer": writer, "models": models})
    return out


def build_body(*, items_writers: list, description: str, project_name: str,
               project_link: str, gate_violations: list, check_lines: list,
               phase_label: str, default_branch: str, repo_full: str,
               changed_files: Optional[int] = None,
               additions: Optional[int] = None, deletions: Optional[int] = None,
               violation_scopes: Optional[set] = None) -> str:
    """The body in the one fixed format. Pure: every input is a record.
    `violation_scopes` — the parts that stand refused; omitted, a non-empty
    `gate_violations` refuses the root app."""
    lines = [MARKER, "**What this changes**"]
    shown = items_writers[-_ITEMS_MAX:]
    if shown:
        lines += [f"- {w['item']}" for w in shown]
        if len(items_writers) > len(shown):
            lines.append(f"- …and {len(items_writers) - len(shown)} earlier changes")
    else:
        lines.append(f"- {_sentence(plain_item(phase_label) or phase_label or 'Changes to this project')}")
    lines += ["", "**Why**"]
    req = " ".join((description or "").split())
    lines.append(f"You asked: “{_clip(req, 400)}”" if req
                 else f"Part of the project “{project_name}”.")
    if project_link:
        lines.append(f"[Open the project]({project_link})")
    lines += ["", "**What was checked**"]
    if gate_violations:
        lines.append("- The build contract found problems, so this is not ready to approve:")
        lines += [f"  - {' '.join(str(v).split())}" for v in gate_violations[:10]]
    else:
        lines.append("- The build contract passed.")
    if check_lines:
        lines += list(check_lines)
    else:
        lines.append("- No other checks ran on the latest save point.")
    lines += ["", "**Who wrote it**"]
    if shown:
        lines += [f"- {w['item']}: {w['writer']}." for w in shown]
    else:
        lines.append("- Not recorded for this save point.")
    stats = []
    if changed_files is not None:
        stats.append(f"{changed_files} file{'s' if changed_files != 1 else ''} changed"
                     + (f" (+{additions or 0} −{deletions or 0})" if additions is not None else ""))
    lines += ["", "<details>", "<summary>Details</summary>", ""]
    if stats:
        lines.append(f"- {stats[0]}")
    lines += [f"- Latest save point: {phase_label}",
              f"- Approving this puts it into `{default_branch}` of the Code Repository `{repo_full}`.",
              "", "</details>"]
    models = sorted({m for w in items_writers for m in w.get("models") or []})
    if models:
        lines.append(f"<!-- models: {', '.join(models)} -->")
    scopes = set(violation_scopes if violation_scopes is not None
                 else ({""} if gate_violations else set()))
    if scopes:
        lines.append(VIOLATIONS_MARKER)
        lines += [_SCOPE_MARK.format(_ROOT_SCOPE if sc == "" else sc) for sc in sorted(scopes)]
    return "\n".join(lines)


def summary_of(body: Optional[str], title: str) -> str:
    """The list row's one line: the first thing it changes, else the title."""
    b = body or ""
    if MARKER in b:
        seg = b.split("**What this changes**", 1)[-1]
        for line in seg.splitlines()[1:]:
            line = line.strip()
            if line.startswith("- "):
                return line[2:]
            if line.startswith("**"):
                break
    return title


def models_of(body: Optional[str]) -> list:
    m = _MODELS_RE.search(body or "")
    return [x.strip() for x in m.group(1).split(",") if x.strip()] if m else []


# ── the project's forge, and the approvals on it ────────────────────────────

@contextlib.asynccontextmanager
async def _forge(pool, entity_id: int, project_id: int):
    """(forge, request, repo) for the project's Code Repository — the platform
    forge as the Host, or the code server on the user's machine."""
    from server.routes import project_forge as _pf
    from server.routes.gitea_client import gitea_request
    forge = await _pf.project_forge(pool, project_id)
    if not forge.repo:
        raise _pf.ForgeError(409, "this project has no Code Repository yet")
    if not forge.on_machine:
        yield forge, gitea_request, forge.repo, None
        return
    async with _pf.open_forge(pool, entity_id, forge) as sess:
        yield forge, sess.request, forge.repo, sess


async def _project(pool, entity_id: int, project_id: int):
    row = await pool.fetchrow(
        "SELECT id, name, description, repo, config FROM projects WHERE id=$1 AND entity_id=$2",
        project_id, entity_id)
    if row is None:
        from server.routes.project_forge import ForgeError
        raise ForgeError(404, "project not found")
    cfg = row["config"]
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except ValueError:
            cfg = {}
    return row, (cfg if isinstance(cfg, dict) else {})


def _state(pr: dict, changes: dict) -> tuple[str, str]:
    if pr.get("merged"):
        return "approved", "Approved"
    if (pr.get("state") or "") != "open":
        return "closed", "Closed"
    asked = changes.get(str(pr.get("number")))
    head = ((pr.get("head") or {}).get("sha") or "")
    if isinstance(asked, dict) and asked.get("head") and asked.get("head") == head:
        return "changes_asked", "Changes asked for"
    return "waiting", "Waiting for you"


async def host_origin(entity_id: int) -> str:
    try:
        from server.reachout.service import resolve_host_origin
        return await resolve_host_origin(int(entity_id))
    except Exception:  # noqa: BLE001 — a link without an origin is dropped, never guessed
        return ""


def app_url(origin: str, app_id: str, view: str) -> str:
    """An absolute link to an app on this Host (server/deep_link.app_link),
    or "" when the Host's address is unknown."""
    from server.deep_link import app_link
    return (origin.rstrip("/") + app_link(app_id, view)) if origin else ""


def repo_link(origin: str, repo_full: str) -> str:
    """Where a Code Repository mention links: this Host's own viewer of it.
    The platform forge answers only the platform's own machines, so a link
    there is dead for a member's browser."""
    return app_url(origin, "gitea", f"repo:{repo_full}")


def approve_url(repo_full: str, number: int, handle: str = "") -> str:
    q = {"repo": repo_full, "pr": str(int(number))}
    if handle:
        q["host"] = handle
    return f"{PLATFORM_SITE}/approve?" + urlencode(q, quote_via=quote, safe="/")


async def _handle(entity_id: int) -> str:
    try:
        from server.network.bastion_enroll import resolve_self_handle
        return (await resolve_self_handle(int(entity_id))) or ""
    except Exception:  # noqa: BLE001
        return ""


def _row(pr: dict, changes: dict, announced: set, *, branch: str | None = None,
         since: Optional[str] = None, owner: str = "", name: str = "") -> dict:
    key, words = _state(pr, changes)
    body = pr.get("body") or ""
    title = pr.get("title") or ""
    return {
        "number": pr.get("number"),
        "title": title,
        "summary": summary_of(body, title),
        "state": key,
        "state_words": words,
        "ready": not has_violations(body),
        "models": models_of(body),
        "plain": MARKER in body,
        "author": ((pr.get("user") or {}).get("login")),
        "merged_by": ((pr.get("merged_by") or {}).get("login")) if pr.get("merged") else None,
        "merged_at": pr.get("merged_at"),
        "updated_at": pr.get("updated_at"),
        "head_sha": ((pr.get("head") or {}).get("sha")),
        "base": ((pr.get("base") or {}).get("ref")),
        "announced": int(pr.get("number") or 0) in announced,
        # The project's own Code Approval (its work branch, in this repository);
        # a mounted repository also lists everyone else's.
        "own": bool(branch) and ((pr.get("head") or {}).get("ref")) == branch
               and _same_repo(pr, owner, name),
        # Merged since this project first listed its approvals and not yet in
        # the chat: the Workbench asks for the line (announce_merge). Merges
        # from before that are history, never announced.
        "announce": bool(pr.get("merged")) and int(pr.get("number") or 0) not in announced
                    and _after(pr.get("merged_at"), since),
    }


def _same_repo(pr: dict, owner: str, name: str) -> bool:
    from server.routes.project_git import same_repo_pr
    return same_repo_pr(pr, owner, name)


def _after(iso: Optional[str], since: Optional[str]) -> bool:
    try:
        a = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
        return a >= b
    except (TypeError, ValueError):
        return False


def _announced(cfg: dict) -> set:
    raw = cfg.get("code_approvals_announced")
    return {int(x) for x in raw if str(x).isdigit()} if isinstance(raw, list) else set()


def _changes(cfg: dict) -> dict:
    return cfg.get("code_approval_changes") if isinstance(cfg.get("code_approval_changes"), dict) else {}


async def _open_pulls(request, owner: str, name: str) -> list:
    """Every open pull request on the repository: a mounted one can hold many
    pages of them. An empty page ends the list. An answer that is not a list
    at all is a read that failed (the forge clients hand back {} for a 2xx
    with an empty body), so it raises instead of reading as "none open": the
    stored waiting count must never drop to 0 on a failed read."""
    open_prs: list = []
    for page in range(1, 21):
        got = await request("GET", f"/api/v1/repos/{owner}/{name}/pulls",
                            params={"state": "open", "limit": 50, "page": page})
        if not isinstance(got, list):
            from server.routes.project_forge import ForgeError
            raise ForgeError(502, "the forge's open Code Approvals did not come back as a list")
        if not got:
            break
        open_prs.extend(got)
        if len(got) < 50:
            break
    return open_prs


# ── the waiting count the projects list reads ───────────────────────────────
# The forge is the record, and list_approvals reads it every time. The
# projects list cannot (a forge call per row), so each project keeps how many
# of its Code Approvals were waiting for the person at the last forge read,
# and when that read was (projects.approvals_waiting / approvals_checked_at,
# migrations/v215). Every read of the project's approvals stores it
# (list_approvals), and so does every change the platform itself makes to
# them: a save point opening the project's Code Approval or moving it to a new
# version, which turns "Changes asked for" back into "Waiting for you"
# (project_git._ensure_pr), and the Host merging it under Auto-Approve
# (project_git.merge_project_pr). What the person does to one (approving in
# the platform's window, asking for changes) is followed by the Workbench
# listing them again. A change made on the forge by anyone else is seen at the
# next read; the checked time says how old the count is.

def waiting_count(rows) -> int:
    """How many Code Approval rows wait for the person: the "N waiting" the
    Workbench's bench shows (ops-pane.tsx CodeApprovalsSection), every row in
    the state "waiting" (_state)."""
    return sum(1 for r in rows if isinstance(r, dict) and r.get("state") == "waiting")


async def record_waiting(pool, project_id: int, count: int,
                         read_at: Optional[datetime] = None) -> None:
    """Store the count, stamped with when the read it comes from began
    (`read_at`; now when not given). A read that began before the one stored
    never replaces it: the bench's listing can still be reading the forge
    when a save point's refresh stores a newer count, and its older view must
    not land on top. Never raises: the forge read it comes from has already
    answered."""
    at = read_at or datetime.now(timezone.utc)
    try:
        await pool.execute(
            "UPDATE projects SET approvals_waiting = $2, approvals_checked_at = $3 "
            "WHERE id = $1 AND (approvals_checked_at IS NULL OR approvals_checked_at <= $3)",
            int(project_id), max(0, int(count)), at)
    except Exception as e:  # noqa: BLE001 — the count is a copy; the forge keeps the record
        logger.warning("code approvals: waiting count for project %s not stored: %s", project_id, e)


async def refresh_waiting(pool, project_id: int, request, repo_full: Optional[str]) -> Optional[int]:
    """Read the project's open Code Approvals again through `request` (the
    caller's forge, already open) and store how many wait for the person.
    Returns the count, or None when it could not be read (the stored one is
    then left as it was). Never raises."""
    if pool is None or request is None or not repo_full:
        return None
    read_at = datetime.now(timezone.utc)
    try:
        from server.routes.gitea_client import split_repo
        cfg = await pool.fetchval("SELECT config FROM projects WHERE id = $1", int(project_id))
        if isinstance(cfg, str):
            cfg = json.loads(cfg)
        changes = _changes(cfg if isinstance(cfg, dict) else {})
        owner, name = split_repo(repo_full)
        prs = await _open_pulls(request, owner, name)
        n = sum(1 for p in prs if isinstance(p, dict) and _state(p, changes)[0] == "waiting")
        await record_waiting(pool, project_id, n, read_at)
        return n
    except Exception as e:  # noqa: BLE001 — never fails the save point or the merge it follows
        logger.warning("code approvals: waiting count for project %s not refreshed: %s",
                       project_id, e)
        return None


async def list_approvals(pool, entity_id: int, project_id: int, *, limit: int = 20) -> dict:
    """The project's Code Approvals: every open one, then the most recently
    closed. Reads the forge each time — the forge is the record — and stores
    how many wait for the person for the projects list (record_waiting)."""
    # Before anything is read: the "changes asked" record below is part of
    # what the count is made from.
    read_at = datetime.now(timezone.utc)
    row, cfg = await _project(pool, entity_id, project_id)
    changes = _changes(cfg)
    announced = _announced(cfg)
    since = cfg.get("code_approvals_since")
    if not since:
        # The first listing sets where announcing starts, so merges from
        # before Code Approvals existed are never announced.
        since = await pool.fetchval(
            "UPDATE projects SET config = jsonb_set(COALESCE(config,'{}'::jsonb), "
            "'{code_approvals_since}', to_jsonb(to_char(now() AT TIME ZONE 'UTC', "
            "'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')), true) WHERE id = $1 "
            "RETURNING config->>'code_approvals_since'", project_id)
    from server.routes.gitea_client import split_repo
    from server.routes.project_git import work_branch_of
    branch = work_branch_of(cfg)
    async with _forge(pool, entity_id, project_id) as (forge, request, repo, sess):
        owner, name = split_repo(repo)
        # Every open one, then the most recently updated of the closed ones.
        open_prs = await _open_pulls(request, owner, name)
        closed = await request("GET", f"/api/v1/repos/{owner}/{name}/pulls",
                               params={"state": "closed", "limit": limit,
                                       "sort": "recentupdate"}) or []
    origin = await host_origin(entity_id)
    rows = [_row(p, changes, announced, branch=branch, since=since, owner=owner, name=name)
            for p in [*open_prs, *closed] if isinstance(p, dict)]
    await record_waiting(pool, project_id, waiting_count(rows), read_at)
    from server.routes.projects import ticket_label
    if ticket_label(cfg):
        # A ticket's project shares its repository with other tickets' projects
        # (v199): its chat says when ITS change merged, never another ticket's.
        for r in rows:
            r["announce"] = r["announce"] and r["own"]
    # The project's own approval first, then everyone else's, each in the forge's order.
    rows.sort(key=lambda r: (not r["own"],))
    return {"project_id": project_id, "repo": repo, "on_machine": forge.on_machine,
            "machine": forge.machine if forge.on_machine else None,
            "repo_url": "" if forge.on_machine else repo_link(origin, repo),
            "approvals": rows}


async def get_approval(pool, entity_id: int, project_id: int, number: int) -> dict:
    """One Code Approval in full, and how the person approves it."""
    row, cfg = await _project(pool, entity_id, project_id)
    changes = cfg.get("code_approval_changes") if isinstance(cfg.get("code_approval_changes"), dict) else {}
    from server.routes.gitea_client import split_repo
    async with _forge(pool, entity_id, project_id) as (forge, request, repo, sess):
        owner, name = split_repo(repo)
        pr = await request("GET", f"/api/v1/repos/{owner}/{name}/pulls/{int(number)}")
        link = sess.pr_url(pr) if sess is not None else None
    out = _row(pr or {}, changes, _announced(cfg))
    out.update({
        "repo": repo,
        "body": (pr or {}).get("body") or "",
        "mergeable": (pr or {}).get("mergeable"),
        "changed_files": (pr or {}).get("changed_files"),
        "additions": (pr or {}).get("additions"),
        "deletions": (pr or {}).get("deletions"),
        "on_machine": forge.on_machine,
    })
    own = None
    if not forge.on_machine:
        from server.git_server import connected as _git_server
        own = _git_server()
    out["approve_on"] = None
    if forge.on_machine:
        # The platform cannot reach the code server on the person's machine,
        # so the approval happens there; this names where.
        out["approve_url"] = None
        out["approve_where"] = (f"This code lives on {forge.machine}. Approve it in the code "
                                f"server there.")
        out["forge_url"] = link
    elif own is not None:
        # The person's own git server: they merge on the pull request's own
        # page there, signed in as themselves. The Host notices the merge on
        # its next read of the forge (the card's watch, the bench's listing),
        # where merged_by says whether it was them or the Host.
        from server.git_server import pr_page
        out["approve_url"] = pr_page(repo, number, (pr or {}).get("html_url"))
        out["approve_where"] = None
        out["approve_on"] = own.netloc
        out["repo_url"] = repo_link(await host_origin(entity_id), repo)
    else:
        out["approve_url"] = approve_url(repo, number, await _handle(entity_id))
        out["approve_where"] = None
        out["repo_url"] = repo_link(await host_origin(entity_id), repo)
    return out


async def ask_for_changes(pool, entity_id: int, project_id: int, number: int, note: str) -> dict:
    """The person wants changes before approving. Recorded against the
    change's current version, so the row reads "Changes asked for" until the
    Host saves a new version; the note itself goes to the Host in the chat."""
    appr = await get_approval(pool, entity_id, project_id, number)
    if appr["state"] not in ("waiting", "changes_asked"):
        from server.routes.project_forge import ForgeError
        raise ForgeError(409, f"that Code Approval is {appr['state_words'].lower()}")
    await pool.execute(
        "UPDATE projects SET config = jsonb_set(COALESCE(config,'{}'::jsonb), "
        "'{code_approval_changes}', COALESCE(config->'code_approval_changes','{}'::jsonb) "
        "|| jsonb_build_object($2::text, jsonb_build_object('head', $3::text, 'at', now())), true) "
        "WHERE id = $1", project_id, str(int(number)), appr.get("head_sha") or "")
    note = " ".join(str(note or "").split())
    text = (f"Code Approval #{int(number)} “{appr['title']}”: please change this before I approve it."
            + (f" {note}" if note else ""))
    return {"recorded": True, "message": text}


def _when(iso: Optional[str]) -> str:
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(timezone.utc)
        return dt.strftime("%H:%M UTC")
    except (TypeError, ValueError):
        return "just now"


async def announce_merge(pool, entity_id: int, project_id: int, number: int) -> dict:
    """One line in the project's chat for a merged Code Approval, written from
    the forge's own merge record (who merged it, when, the merge commit) — never
    from a click, so it says what happened, not what was asked for. Idempotent:
    a change is announced once (projects.config.code_approvals_announced)."""
    row, cfg = await _project(pool, entity_id, project_id)
    if int(number) in _announced(cfg):
        return {"announced": False, "already": True}
    from server.routes.gitea_client import gitea_creds, split_repo
    async with _forge(pool, entity_id, project_id) as (forge, request, repo, _sess):
        owner, name = split_repo(repo)
        pr = await request("GET", f"/api/v1/repos/{owner}/{name}/pulls/{int(number)}") or {}
        if _sess is not None:
            _sess = types.SimpleNamespace(user=_sess.user)   # the relay closes with the block
    if not pr.get("merged"):
        return {"announced": False, "merged": False}
    # Claim the number before writing, so two callers racing (the window
    # closing and the list refresh) write one line, not two.
    claimed = await pool.fetchval(
        "UPDATE projects SET config = jsonb_set(COALESCE(config,'{}'::jsonb), "
        "'{code_approvals_announced}', COALESCE(config->'code_approvals_announced','[]'::jsonb) "
        "|| to_jsonb($2::int), true) "
        "WHERE id = $1 AND NOT (COALESCE(config->'code_approvals_announced','[]'::jsonb) @> to_jsonb($2::int)) "
        "RETURNING id", project_id, int(number))
    if claimed is None:
        return {"announced": False, "already": True}
    by = ((pr.get("merged_by") or {}).get("login") or "").strip()
    cred = gitea_creds()
    # The Host's own account on that forge: the platform login, or the one it
    # acts as on the code server on the person's machine.
    host_login = _sess.user if _sess is not None else (cred[0] if cred else None)
    person = None
    from server.git_server import connected as _git_server
    own = None if forge.on_machine else _git_server()
    if own is not None:
        # On the person's own git server: the username they gave in Settings.
        person = own.person or None
    else:
        try:
            from server.routes import gitea as _gitea
            await _gitea._load_user_tok()     # a restarted pod holds it in the vault
            person = _gitea.user_login()
        except Exception:  # noqa: BLE001 — then the line names the login the forge recorded
            person = None
    title = pr.get("title") or f"#{number}"
    base = ((pr.get("base") or {}).get("ref")) or "main"
    commit = (pr.get("merge_commit_sha") or "")[:7]
    at = _when(pr.get("merged_at"))
    link = "" if forge.on_machine else repo_link(await host_origin(entity_id), repo)
    where = f"`{base}`" + (f" at {commit}" if commit else "") + (f" ([Code Repository]({link}))" if link else "")
    if host_login and by.lower() == host_login.lower():
        text = f"The Host approved “{title}” under your Auto-Approve at {at}. It is now in {where}."
    elif person and by.lower() == person.lower():
        text = f"You approved “{title}” at {at}. It is now in {where}."
    else:
        text = f"{by or 'Someone'} approved “{title}” at {at}. It is now in {where}."
    written = False
    try:
        from mastercode.brain.offline_task_runner import post_to_origin_thread
        written = await post_to_origin_thread(
            pool, entity_id, {"project_id": project_id}, text,
            extra_tags={"origin": "code_approval", "code_approval": int(number)},
            fallback_session=f"project:{project_id}")
    except Exception as e:  # noqa: BLE001 — the merge stands; the line is retried
        logger.warning("code approval: chat line not written for %s#%s: %s", repo, number, e)
    if not written:
        # Give the claim back, so the next listing or the next Auto-Approve
        # merge writes the line instead of it being lost for good.
        await pool.execute(
            "UPDATE projects SET config = jsonb_set(config, '{code_approvals_announced}', "
            "COALESCE((SELECT jsonb_agg(x) FROM jsonb_array_elements("
            "config->'code_approvals_announced') x WHERE x <> to_jsonb($2::int)), '[]'::jsonb)) "
            "WHERE id = $1", project_id, int(number))
        return {"announced": False, "retry": True}
    return {"announced": True, "text": text}
