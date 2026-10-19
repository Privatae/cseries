"""routes/forge_status.py — the gitea-actions check provider.

A check that runs on the
forge's CI can only answer about a commit the forge has, so it runs after the
save point is pushed: the build waits for the commit statuses the CI posts on
that commit (GET /repos/{owner}/{repo}/commits/{sha}/status — the latest
status per context) and turns them into a verdict.

  every counted status success         -> ok=True
  any counted status failed            -> ok=False, at once (nothing can undo it)
  still pending / not posted at budget -> ran=False, "CI did not answer"

A state other than pending or success (failure, error, warning, anything a
later forge adds) is not a pass: the same rule as a harness check that ran
without a clean True. An API error while polling is waited through, and named
in the reason if the budget runs out on it.

A CI posts one status per job; their names are the CI's own (Gitea Actions
builds one from the workflow and job names, and a job may post its own), so
nothing here assumes them. A row's `contexts` names the ones that must all
pass, as the project's CI spells them — required, because a CI's jobs post
one after another: "every status so far passed" can be read before the last
job has posted anything. A named status not yet posted is still waited for.

Only the CI's word counts: a status posted by this Host's own forge account
is ignored, so a Host can never pass its own build.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

PROVIDER = "gitea-actions"
WHERE = "forge_ci"
POLL_S = 15.0
# A long wait polls less often: the next read comes after a tenth of the time
# already waited, never sooner than POLL_S and never later than this. A
# day-long budget then costs a few hundred status reads instead of thousands,
# and an answer is seen at most a tenth of the wait (or two minutes) late.
MAX_POLL_S = 120.0
_PASS = "success"
_PENDING = "pending"


def _wanted(row: dict) -> list[str]:
    return [n.strip() for n in str(row.get("contexts") or "").split(",") if n.strip()]


def _creator(s: dict) -> str:
    c = s.get("creator")
    return str((c or {}).get("login") or "") if isinstance(c, dict) else ""


def _latest(combined, ignore_creators=None) -> dict:
    """{context: status} — the newest status per context. The combined
    endpoint already returns one per context; kept defensive against a forge
    that returns history (newest first), so a stale 'pending' never wins.
    A status whose poster is in `ignore_creators` is not read at all."""
    out: dict = {}
    skip = {c for c in (ignore_creators or ()) if c}
    for s in (combined or {}).get("statuses") or []:
        if not isinstance(s, dict) or not s.get("context"):
            continue
        if skip and _creator(s) in skip:
            continue
        prev = out.get(s["context"])
        if prev is None or str(s.get("updated_at") or s.get("created_at") or "") > \
                str(prev.get("updated_at") or prev.get("created_at") or ""):
            out[s["context"]] = s
    return out


def _state(s: dict) -> str:
    return str(s.get("status") or s.get("state") or "").lower()


def _result(ctx: str, s: Optional[dict]) -> dict:
    s = s or {}
    return {"name": ctx, "ok": _state(s) == _PASS, "state": _state(s) or "not posted",
            "output_tail": s.get("description") or None, "url": s.get("target_url") or None}


def read(combined, row: dict, sha: str, ignore_creators=None) -> Optional[dict]:
    """The verdict the statuses on `sha` give NOW, or None while they are
    still coming (keep waiting). Pure. A row that names no contexts never
    passes (the validator requires them; this is the backstop)."""
    latest = _latest(combined, ignore_creators)
    wanted = _wanted(row)
    results = [_result(c, latest.get(c)) for c in wanted]
    base = {"provider": PROVIDER, "where": WHERE, "sha": sha, "results": results}
    if any(r["state"] not in (_PASS, _PENDING, "not posted") for r in results):
        return {**base, "ran": True, "ok": False}
    if wanted and all(r["ok"] for r in results):
        return {**base, "ran": True, "ok": True}
    return None


def not_answered(combined, row: dict, sha: str, budget_s: int,
                 last_error: Optional[str] = None, ignore_creators=None) -> dict:
    """The verdict at the end of the budget: the row did not run."""
    latest = _latest(combined, ignore_creators)
    wanted = _wanted(row)
    waiting = [c for c in wanted if _state(latest.get(c) or {}) != _PASS]
    what = (f"still waiting on {', '.join(waiting)}" if waiting
            else "the row names no statuses to wait for")
    mins = budget_s // 60
    span = (f"{mins} min" if mins < 120 else
            f"{mins // 60} h" + (f" {mins % 60} min" if mins % 60 else ""))
    reason = f"CI did not answer for {sha[:7]} within {span} ({what})"
    mine = _posted_by(combined, ignore_creators, waiting)
    if mine:
        # Said, so a CI that posts with this Host's own account is visible
        # as the cause rather than looking like a CI that never ran.
        reason += (f"; {', '.join(mine)} was posted by this Host's own forge account "
                   "and does not count as the CI's answer")
    if last_error:
        reason += f"; the forge last answered: {last_error}"
    return {"provider": PROVIDER, "where": WHERE, "sha": sha, "ran": False, "ok": None,
            "reason": reason, "results": [_result(c, latest.get(c)) for c in wanted]}


def _posted_by(combined, creators, contexts) -> list[str]:
    """The named contexts that have a status only from an ignored poster."""
    skip = {c for c in (creators or ()) if c}
    if not skip:
        return []
    return sorted({s["context"] for s in (combined or {}).get("statuses") or []
                   if isinstance(s, dict) and s.get("context") in contexts
                   and _creator(s) in skip})


def failure_text(v: dict) -> str:
    """The failed statuses of a verdict, one line each with where its log is
    — what a seeded retry of the phase is told."""
    lines = [f"- {r['name']}: {r['state']}" + (f" — {r['output_tail']}" if r.get("output_tail") else "")
             + (f" ({r['url']})" if r.get("url") else "")
             for r in v.get("results") or [] if not r.get("ok")]
    return ("The forge's CI did not pass commit " + str(v.get("sha") or "")[:7] + ":\n"
            + "\n".join(lines)) if lines else ""


def skipped(reason: str, sha: Optional[str] = None) -> dict:
    """A row the build never waited on, with why."""
    return {"provider": PROVIDER, "where": WHERE, "sha": sha, "ran": False, "ok": None,
            "reason": reason, "results": []}


_PAGE = 50
_MAX_PAGES = 20


async def _read_all(request, owner: str, name: str, sha: str) -> dict:
    """Every status on the commit, page by page (the endpoint pages its
    statuses), so a named status past the first page is still read."""
    statuses: list = []
    first = None
    for page in range(1, _MAX_PAGES + 1):
        got = await request("GET", f"/api/v1/repos/{owner}/{name}/commits/{sha}/status",
                            params={"limit": _PAGE, "page": page}) or {}
        first = first or got
        batch = got.get("statuses") or []
        statuses += batch
        if len(batch) < _PAGE:
            break
    return {**(first or {}), "statuses": statuses}


async def wait(repo_full: str, sha: str, row: dict, *,
               request: Optional[Callable[..., Awaitable]] = None,
               sleep: Callable[[float], Awaitable] = asyncio.sleep,
               clock: Callable[[], float] = time.monotonic,
               poll_s: float = POLL_S, ignore_creators=None) -> dict:
    """Poll the commit's statuses until they answer or the row's budget runs
    out. `request` is the gitea_request contract (method, path, params=...);
    omitted, the platform forge as this Host."""
    if request is None:
        from server.routes.gitea_client import gitea_request as request
    from server.routes.gitea_client import split_repo
    owner, name = split_repo(repo_full)
    budget = int(row.get("wait_budget_s") or 900)
    started = clock()
    deadline = started + budget
    combined, last_error = None, None
    while True:
        try:
            combined = await _read_all(request, owner, name, sha)
            last_error = None
        except Exception as e:  # noqa: BLE001 — a blip is waited through, and named at the end
            last_error = str(getattr(e, "detail", None) or e)[:160]
            logger.info("forge status read for %s@%s failed: %s", repo_full, sha[:7], last_error)
        v = read(combined, row, sha, ignore_creators) if combined is not None else None
        if v is not None:
            return v
        now = clock()
        left = deadline - now
        if left <= 0:
            return not_answered(combined, row, sha, budget, last_error, ignore_creators)
        step = min(max(poll_s, (now - started) * 0.1), max(poll_s, MAX_POLL_S))
        await sleep(min(step, left))
