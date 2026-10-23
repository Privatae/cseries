"""
Training Memory — distills uploaded Claude Code sessions into behavioral_corrections.

Pipeline per job:
  1. POST /jobs                       — accept upload, persist raw files under jobs/<id>/raw/
  2. GET  /jobs/{id}/estimate         — run extract_dpo_pairs; return counts + cost projection
  3. POST /jobs/{id}/start            — kick off classify phase (background worker)
  4. GET  /jobs/{id}/events           — SSE stream of phase + progress events
  5. GET  /jobs/{id}/review           — full classifier output + current review state
  6. PATCH /jobs/{id}/review/{pid}    — update one pair's status/rule/role/severity
  7. POST /jobs/{id}/review/bulk      — bulk action on the listed pair_ids (approve_visible / reject_below_conf / set_role)
  8. POST /jobs/{id}/commit           — embed + insert approved/edited rules (second worker phase)
  9. POST /jobs/{id}/undo             — soft-undo recent commit (within 10 min)
 10. GET  /jobs/{id}/result           — final summary (counts + sample rules)
 11. GET  /existing-roles             — distinct roles already populated for this entity
"""

import asyncio
import json
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

from server import brain_service, secret_cut
from brain.training_memory.worker import (
    CLASSIFY_SUMMARY,
    extract_pairs_for_job,
    estimate_cost,
    run_classify_phase,
    run_commit_phase,
    sample_rules_for_role,
    undo_commit,
)

logger = logging.getLogger("privatae.training_memory")
from server.applications.middleware import requires_permission

# Scoped 2026-10-01. These routes ingest the Host's own SDK transcripts, spend
# inference on classifying them and COMMIT rules into behavioral_corrections —
# rules the Host then follows. Unscoped, any app could run that whole pipeline
# and write the Host's corrections. Reads take read:brain_corrections and every
# step that creates, spends or writes takes write:brain_corrections — the two
# scopes the Training Memory app declares. /jobs/{id}/events stays token-less
# (an EventSource cannot send the header; it carries phase/progress/counters
# only) and is listed in server/applications/route_policy.py.
router = APIRouter(prefix="/api/training-memory", tags=["training-memory"])

# Created where a job's files are first written, never at import.
JOBS_ROOT = Path(os.environ.get("TM_JOBS_ROOT", "/app/training_memory_jobs"))

# In-process registry of active job event streams.
_event_queues: dict[str, asyncio.Queue] = {}


def _job_dir(job_id: str) -> Path:
    p = JOBS_ROOT / job_id
    if not p.exists():
        raise HTTPException(status_code=404, detail="job not found")
    return p


@router.get("/existing-roles", dependencies=[Depends(requires_permission("read:brain_corrections"))])
async def existing_roles(entity_id: int = 0):
    if entity_id == 0:
        entity_id = brain_service._entity_id
    pool = brain_service.get_pool()
    rows = await pool.fetch(
        "SELECT DISTINCT role FROM behavioral_corrections "
        "WHERE entity_id=$1 AND role IS NOT NULL ORDER BY role",
        entity_id,
    )
    return {"roles": [r["role"] for r in rows]}


@router.post("/jobs", dependencies=[Depends(requires_permission("write:brain_corrections"))])
async def create_job(
    files: list[UploadFile] = File(...),
    role: str = Form(...),
    billing: str = Form(...),
    byo_key: Optional[str] = Form(None),
):
    role = role.strip()
    if not role or len(role) > 100:
        raise HTTPException(400, "role must be 1-100 chars")
    if billing not in ("platform", "byo"):
        raise HTTPException(400, "billing must be platform|byo")
    # An empty byo_key is fine: the worker's _resolve_byo_key falls back to the
    # container's ANTHROPIC_API_KEY and only raises if that's also missing.

    job_id = uuid.uuid4().hex[:12]
    raw_dir = JOBS_ROOT / job_id / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    saved = 0
    for f in files:
        # Only accept .jsonl (and .zip, expanded later in worker)
        name = (f.filename or "").lower()
        if not (name.endswith(".jsonl") or name.endswith(".zip")):
            continue
        target = raw_dir / Path(f.filename).name
        with target.open("wb") as out:
            shutil.copyfileobj(f.file, out)
        saved += 1
    if saved == 0:
        raise HTTPException(400, "no .jsonl or .zip files in upload")

    meta = {
        "job_id": job_id,
        "role": role,
        "billing": billing,
        "byo_key_set": bool(byo_key),
        "byo_key": byo_key if billing == "byo" else None,
        "status": "uploaded",
        "files_received": saved,
        "entity_id": brain_service._entity_id,
    }
    (JOBS_ROOT / job_id / "meta.json").write_text(json.dumps(meta, indent=2))
    return {"id": job_id, "files_received": saved}


@router.post("/jobs/from-sdk-sessions", dependencies=[Depends(requires_permission("write:brain_corrections"))])
async def create_job_from_sdk_sessions(
    role: str = Form(...),
    billing: str = Form(...),
    byo_key: Optional[str] = Form(None),
    since: Optional[str] = Form(None),
):
    """Build a Training Memory job from the SDK transcripts the
    claude-code sidecar writes (Ship 2). Globs the per-entity
    /var/lib/cseries/cc-sessions tree, filters by mtime since `since`
    (ISO-8601; default: 24h ago), copies matching .jsonl files into
    the job's raw/ dir, then the existing pipeline takes over
    (/jobs/{id}/estimate, /start, /events, /review, /commit).

    Doesn't delete or move the source transcripts. The pipeline's
    embedding-similarity dedup (_dedup_peek, _embed_and_insert_reviewed)
    handles overlap if the same files are re-ingested.

    Returns 400 if no transcripts match.
    """
    from datetime import datetime, timezone, timedelta

    role = role.strip()
    if not role or len(role) > 100:
        raise HTTPException(400, "role must be 1-100 chars")
    if billing not in ("platform", "byo"):
        raise HTTPException(400, "billing must be platform|byo")
    # An empty byo_key is fine: the worker's _resolve_byo_key falls back to the
    # container's ANTHROPIC_API_KEY and only raises if that's also missing.

    # Default to last 24h. Accept either YYYY-MM-DD or full ISO.
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(400, f"since must be ISO-8601 (got {since!r})")
    else:
        since_dt = datetime.now(timezone.utc) - timedelta(hours=24)

    entity_id = brain_service._entity_id
    src_root = Path(
        os.environ.get("CC_TRANSCRIPT_ROOT", "/var/lib/cseries/cc-sessions")
    ) / str(entity_id)
    if not src_root.is_dir():
        raise HTTPException(400, "no SDK transcripts on disk for this host")

    since_ts = since_dt.timestamp()
    candidates = sorted(
        p for p in src_root.glob("*.jsonl")
        if p.is_file() and p.stat().st_mtime >= since_ts
    )
    if not candidates:
        raise HTTPException(
            400,
            f"no transcripts modified since {since_dt.isoformat()} "
            f"under {src_root}",
        )

    job_id = uuid.uuid4().hex[:12]
    raw_dir = JOBS_ROOT / job_id / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    for src in candidates:
        target = raw_dir / src.name
        try:
            shutil.copy2(src, target)
            saved += 1
        except Exception as e:
            logger.warning("copy %s → %s failed: %s", src, target, e)

    if saved == 0:
        # Cleanup empty job dir + surface failure
        try:
            shutil.rmtree(JOBS_ROOT / job_id, ignore_errors=True)
        except Exception:
            pass
        raise HTTPException(500, "transcripts found but all copies failed")

    meta = {
        "job_id": job_id,
        "role": role,
        "billing": billing,
        "byo_key_set": bool(byo_key),
        "byo_key": byo_key if billing == "byo" else None,
        "status": "uploaded",
        "files_received": saved,
        "entity_id": entity_id,
        # Provenance: this job was built from SDK transcripts, not user upload.
        "source": "sdk_sessions",
        "since": since_dt.isoformat(),
    }
    (JOBS_ROOT / job_id / "meta.json").write_text(json.dumps(meta, indent=2))
    return {
        "id": job_id,
        "files_received": saved,
        "since": since_dt.isoformat(),
        "source": "sdk_sessions",
    }


@router.get("/jobs/{job_id}/estimate", dependencies=[Depends(requires_permission("read:brain_corrections"))])
async def estimate(job_id: str):
    jd = _job_dir(job_id)
    meta = json.loads((jd / "meta.json").read_text())
    counts = await extract_pairs_for_job(jd)
    est = estimate_cost(counts, meta["billing"])
    return {**counts, **est}


@router.post("/jobs/{job_id}/start", dependencies=[Depends(requires_permission("write:brain_corrections"))])
async def start(job_id: str):
    """Kick off the classify phase. Ends at `ready_for_review`."""
    jd = _job_dir(job_id)
    meta = json.loads((jd / "meta.json").read_text())
    if meta["status"] in ("classifying", "committing", "done"):
        raise HTTPException(409, f"job already {meta['status']}")
    queue: asyncio.Queue = asyncio.Queue()
    _event_queues[job_id] = queue
    asyncio.create_task(_run_with_events(job_id, jd, meta, queue, run_classify_phase))
    meta["status"] = "classifying"
    (jd / "meta.json").write_text(json.dumps(meta, indent=2))
    return {"started": True}


async def _run_with_events(job_id, jd, meta, queue, runner):
    try:
        async for evt in runner(jd, meta):
            await queue.put(evt)
    except Exception as e:
        logger.exception("training-memory job %s phase failed", job_id)
        await queue.put({"phase": "error", "error": str(e)[:300], "done": True})
    finally:
        await queue.put(None)  # sentinel


@router.get("/jobs/{job_id}/events")
async def events(job_id: str, request: Request):
    if job_id not in _event_queues:
        raise HTTPException(404, "no event stream for this job")
    queue = _event_queues[job_id]

    async def gen():
        while True:
            if await request.is_disconnected():
                break
            evt = await queue.get()
            if evt is None:
                break
            yield f"data: {json.dumps(evt)}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/jobs/{job_id}/result", dependencies=[Depends(requires_permission("read:brain_corrections"))])
async def result(job_id: str):
    jd = _job_dir(job_id)
    result_path = jd / "result.json"
    if not result_path.exists():
        raise HTTPException(425, "result not ready")
    data = json.loads(result_path.read_text())
    # Attach a few sample rules from this role to make the success state concrete.
    data["sample_rules"] = await sample_rules_for_role(data["role"], n=5)
    return data


# ------------------------------------------------------------------
# Review surface — between classify and commit, the user curates.
# ------------------------------------------------------------------

def _read_review_state(jd: Path) -> dict:
    rs = jd / "review_state.json"
    if not rs.exists():
        raise HTTPException(425, "review state not ready — classify phase has not completed")
    return json.loads(rs.read_text())


def _write_review_state(jd: Path, state: dict):
    (jd / "review_state.json").write_text(json.dumps(state, indent=2))


def _read_classified(jd: Path) -> dict:
    """Load classified.jsonl keyed by pair_id for context in the review view."""
    by_id = {}
    cp = jd / "classified.jsonl"
    if not cp.exists():
        return by_id
    for line in cp.open():
        if not line.strip():
            continue
        rec = json.loads(line)
        by_id[rec["pair_id"]] = rec
    return by_id


def _read_pairs(jd: Path) -> dict:
    """Load pairs.jsonl keyed by pair_id so the review view can show context turns."""
    by_id = {}
    pp = jd / "pairs.jsonl"
    if not pp.exists():
        return by_id
    for line in pp.open():
        if not line.strip():
            continue
        p = json.loads(line)
        by_id[p["pair_id"]] = p
    return by_id


def _session_label(pair: dict) -> str:
    """Per (c) — first user message preview. Pair already carries the first
    4 prior turns as context; take the earliest user turn we have."""
    ctx = pair.get("context") or []
    for turn in ctx:
        if turn.get("role") == "user":
            t = (turn.get("text") or "").strip()
            if t:
                return t[:60].replace("\n", " ")
    # Fall back to the correction text.
    t = (pair.get("correction", {}).get("text") or "").strip()
    return t[:60].replace("\n", " ") or "(no preview)"


@router.get("/jobs/{job_id}/review", dependencies=[Depends(requires_permission("read:brain_corrections"))])
async def review(job_id: str, show_all: bool = False):
    """Full classifier output + current review state.

    Default: only classifier-yes pairs (to be curated). show_all=true also
    returns classifier-no pairs so the user can manually promote any the
    classifier missed.
    """
    jd = _job_dir(job_id)
    state = _read_review_state(jd)
    classified = _read_classified(jd)
    pairs = _read_pairs(jd)

    rows = []
    sessions = {}    # session_id -> {label, count}
    for pid, entry in state.items():
        is_yes = entry.get("classifier_is_feedback") is True
        if not is_yes and not show_all:
            continue
        rec = classified.get(pid, {})
        pair = pairs.get(pid, {})
        sid = entry.get("session_id") or pair.get("session_id")
        if sid not in sessions:
            sessions[sid] = {"id": sid, "label": _session_label(pair), "count": 0}
        sessions[sid]["count"] += 1
        rows.append({
            **entry,
            "rejected_preview": rec.get("rejected_preview"),
            "correction_text": rec.get("correction_text"),
            "context": pair.get("context"),
            "session_label": sessions[sid]["label"],
        })

    counts = {
        "total": len(state),
        "pending": sum(1 for v in state.values() if v["status"] == "pending"),
        "approved": sum(1 for v in state.values() if v["status"] == "approved"),
        "edited": sum(1 for v in state.values() if v["status"] == "edited"),
        "rejected": sum(1 for v in state.values() if v["status"] == "rejected"),
        "skipped_no_feedback": sum(1 for v in state.values()
                                   if v["status"] == "skipped_no_feedback"),
        # Pairs the batch did not classify (errored, expired, ...) have no
        # entry in the review state; the screen says how many and why.
        "not_classified": 0,
        "not_classified_by_type": {},
    }
    summary_path = jd / CLASSIFY_SUMMARY
    if summary_path.exists():
        summary = json.loads(summary_path.read_text())
        counts["not_classified"] = int(summary.get("not_classified") or 0)
        counts["not_classified_by_type"] = summary.get("not_classified_by_type") or {}
    return {"rows": rows, "sessions": list(sessions.values()), "counts": counts}


class ReviewPatch(BaseModel):
    status: Optional[str] = None       # approved | rejected | edited | pending
    rule_final: Optional[str] = None
    role: Optional[str] = None
    severity: Optional[str] = None     # gentle | firm | hard


@router.patch("/jobs/{job_id}/review/{pair_id}", dependencies=[Depends(requires_permission("write:brain_corrections"))])
async def patch_review(job_id: str, pair_id: str, body: ReviewPatch):
    jd = _job_dir(job_id)
    state = _read_review_state(jd)
    if pair_id not in state:
        raise HTTPException(404, "pair not found in review state")
    entry = state[pair_id]
    if body.status is not None:
        if body.status not in ("approved", "rejected", "edited", "pending"):
            raise HTTPException(400, "invalid status")
        entry["status"] = body.status
    if body.rule_final is not None:
        # The person's own words for the rule are cut of their secret shapes
        # before they are kept, as the turn cuts a message (server/secret_cut.py):
        # the commit embeds the rule, and the turn's system prompt reads the
        # learned rules (mastercode/constitution/context.py). Cut, then
        # trimmed, so the length cap can never leave half a key behind. The
        # classifier's own wording, drawn from the uploaded sessions, is left
        # as it came, as uploaded files and documents are.
        entry["rule_final"] = secret_cut.redact_words(
            body.rule_final, door="training memory review", log=logger).strip()[:2000]
        # Editing the text → flip status to edited unless explicitly set otherwise.
        if body.status is None and entry["status"] != "rejected":
            entry["status"] = "edited"
    if body.role is not None:
        entry["role"] = body.role.strip()[:100]
    if body.severity is not None:
        if body.severity not in ("gentle", "firm", "hard"):
            raise HTTPException(400, "invalid severity")
        entry["severity"] = body.severity
    state[pair_id] = entry
    _write_review_state(jd, state)
    return entry


class BulkAction(BaseModel):
    action: str   # approve_visible | reject_visible | reject_below_conf | set_role | set_severity
    # The pairs on the review screen when the button was pressed. The action
    # applies to these and no others: re-deriving the screen's filters here
    # (confidence, type, kind, session, search, show-no) drifted from the
    # app and acted on pairs the user could not see. Required, so a request
    # without it is refused instead of matching every pair.
    pair_ids: list[str]
    role: Optional[str] = None
    severity: Optional[str] = None
    confidence_threshold: Optional[float] = None


@router.post("/jobs/{job_id}/review/bulk", dependencies=[Depends(requires_permission("write:brain_corrections"))])
async def bulk_action(job_id: str, body: BulkAction):
    jd = _job_dir(job_id)
    state = _read_review_state(jd)
    # A pair_id the job no longer has is skipped; duplicates count once.
    matched = [pid for pid in dict.fromkeys(body.pair_ids) if pid in state]
    if body.action == "approve_visible":
        for pid in matched:
            if state[pid]["status"] == "pending":
                state[pid]["status"] = "approved"
    elif body.action == "reject_visible":
        for pid in matched:
            state[pid]["status"] = "rejected"
    elif body.action == "reject_below_conf":
        if body.confidence_threshold is None:
            raise HTTPException(400, "confidence_threshold required")
        for pid in matched:
            c = state[pid].get("classifier_confidence") or 0
            if c < body.confidence_threshold:
                state[pid]["status"] = "rejected"
    elif body.action == "set_role":
        if not body.role:
            raise HTTPException(400, "role required")
        for pid in matched:
            state[pid]["role"] = body.role.strip()[:100]
    elif body.action == "set_severity":
        if body.severity not in ("gentle", "firm", "hard"):
            raise HTTPException(400, "invalid severity")
        for pid in matched:
            state[pid]["severity"] = body.severity
    else:
        raise HTTPException(400, f"unknown action: {body.action}")
    _write_review_state(jd, state)
    return {"matched": len(matched), "action": body.action}


@router.post("/jobs/{job_id}/commit", dependencies=[Depends(requires_permission("write:brain_corrections"))])
async def commit(job_id: str):
    """Kick off the commit phase. Embeds + inserts approved/edited rules.
    Stream progress via the events endpoint."""
    jd = _job_dir(job_id)
    meta = json.loads((jd / "meta.json").read_text())
    if meta.get("status") == "committing":
        raise HTTPException(409, "already committing")
    queue: asyncio.Queue = asyncio.Queue()
    _event_queues[job_id] = queue
    asyncio.create_task(_run_with_events(job_id, jd, meta, queue, run_commit_phase))
    meta["status"] = "committing"
    (jd / "meta.json").write_text(json.dumps(meta, indent=2))
    return {"started": True}


@router.post("/jobs/{job_id}/undo", dependencies=[Depends(requires_permission("write:brain_corrections"))])
async def undo(job_id: str):
    """Soft-undo the commit (active=false on every row this job inserted).
    Allowed within 10 min of the commit timestamp."""
    jd = _job_dir(job_id)
    res = await undo_commit(jd)
    if not res.get("ok"):
        raise HTTPException(409 if res.get("error") == "undo_window_expired" else 400,
                            res.get("error") or "undo_failed")
    return res
