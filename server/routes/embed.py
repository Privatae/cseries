"""
Embed Jobs — durable, resumable background embedding for both memory banks.

Replaces the previous fire-and-forget asyncio tasks in:
  - server/routes/brain.py   (entity_memory re-embedding)
  - server/routes/context.py (user_context embedding)

A job row in the `embed_jobs` table represents work to do; the JobRunner
claims one pending row at a time, processes it in batches, and persists
progress (cursor + counters) after every batch. Container restarts no
longer drop in-flight work — orphaned `running` jobs are reset to
`pending` on boot and the runner picks them back up.

Live progress is broadcast on the dashboard WebSocket as
  {type: "embed_job", module: "embed", data: {job_id, status, total,
                                               embedded, failed, ...}}

Endpoints:
  GET    /api/brain/embed/jobs                    — list jobs
  GET    /api/brain/embed/jobs/{job_id}           — single job detail
  POST   /api/brain/embed/jobs                    — enqueue a FILL job (dashboard)
  POST   /api/brain/embed/jobs/{job_id}/cancel    — cancel
  POST   /api/brain/embed/jobs/{job_id}/retry     — retry failed/cancelled
  GET    /api/brain/embed/coverage                — both-bank coverage stats
  GET    /api/brain/embed/coverage/by-model       — per-model readiness (column-first)
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from fastapi import APIRouter, HTTPException, Query, Depends

from server import brain_service
from server.applications.middleware import requires_permission

logger = logging.getLogger("privatae.brain.embed")

router = APIRouter(prefix="/api/brain/embed", tags=["brain", "embed"])

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
# NO default model. Jobs carry the model they were enqueued with; when absent
# it resolves from the entity's config (behavioral_params.embedding_model) at
# run time — never a literal. An unconfigured entity fails the job loudly.


async def _active_model(pool, entity_id) -> str:
    """The entity's configured embedding model, or '' when unconfigured."""
    from mastercode.memory_engine import _entity_embedding_config
    _, model = await _entity_embedding_config(pool, entity_id)
    return model


async def _active_media_model(pool, entity_id) -> str:
    """The entity's configured MEDIA embedding model, or '' when media is not
    set up (media_config returns {} then). Deliberately never falls back to the
    text model: the two live in different vector spaces, so borrowing one for
    the other writes vectors nothing can search."""
    from mastercode.media_engine import media_config
    cfg = await media_config(pool, entity_id)
    return (cfg.get("model") or "") if cfg else ""

# Batch size per round-trip. Keeps a long job interruptible — each batch
# updates the cursor + counters so worst-case loss on crash is one batch.
BATCH_SIZE = 20
# The bridge's embeddings.embed_batch refuses more inputs than this in one call
# (privatae bridge/adapter.py op_embeddings_embed_batch MAX_BATCH = 256). A row
# longer than one embed window is several inputs (mastercode.embed_windows), so
# a batch of BATCH_SIZE rows can pass it; the windows are sent in slices of it.
BRIDGE_EMBED_BATCH_MAX = 256

# Ollama retry policy on a single row before marking it failed and moving on.
ROW_RETRY_ATTEMPTS = 3
ROW_RETRY_BASE_DELAY = 1.0  # seconds, exponential

# Job-level pause if Ollama is unreachable for this many consecutive batches —
# the runner sets status='paused' and re-tries the job after JOB_RESUME_DELAY.
JOB_PAUSE_AFTER_BATCH_FAILURES = 3
JOB_RESUME_DELAY = 60.0


def _lane_endpoint_name(target: str) -> str:
    """What to CALL the endpoint in a paused job's reason string.

    The reason is host-facing and gets repeated to the user, so it has to name
    the thing that actually failed. "Ollama unreachable" was hardcoded here, and
    it is wrong for every lane that is not Ollama — a media job pointed at a
    provider, most obviously. Naming the lane is honest without pretending to
    know which endpoint the user chose.
    """
    return "the media embedding endpoint" if target == "user_context_media" \
        else "the embedding endpoint"


class TransientLaneError(Exception):
    """The lane cannot fetch right now for a reason expected to clear on its
    own — a failed config read, say.

    It exists because the runner otherwise has only two outcomes and neither is
    correct for this case: an EMPTY fetch means the work is finished, so the job
    reports done with every row still unembedded; and an unexpected exception
    escapes to _loop, which logs and moves on, leaving the job 'running' for
    ever (nothing re-queues it but a restart, and while it sits there
    _ingest_binary_file hands every new upload that dead job's id). Raising this
    pauses and re-tries instead, which is what a transient fault deserves.
    """

# target='gitea_repo' jobs name a repo on the host's own gitea as "owner/name"
# (the same charset the Gitea app's routes accept for a path segment). The
# charset alone admits "." and ".." as segments — "../projects" matched, and
# the runner's pre-clone cleanup would have rmtree'd the projects root. A
# segment is a name, never a dot-name.
import re as _re
_REPO_RE = _re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def valid_repo_name(repo: str) -> bool:
    """'owner/name' on the host's gitea: two segments, charset-clean, and
    neither segment is '.' or '..' (or all dots)."""
    repo = (repo or "").strip()
    if not _REPO_RE.match(repo):
        return False
    return all(seg.strip(".") for seg in repo.split("/"))


# ── Schema ──────────────────────────────────────────────────────────
#
# embed_jobs is declared at init-db.sql:1136 and created at boot, not from here.
# The block that used to live here was a defensive mirror of that
# declaration, with one real difference: ix_embed_jobs_entity and
# ix_embed_jobs_active were declared ONLY here, so they moved to
# migrations/v142_band_b_appearance_and_embed.sql rather than being lost.


# ── WebSocket broadcast helper ─────────────────────────────────────

async def _emit(payload: dict) -> None:
    """Push an embed_job event to the dashboard WS. Best-effort."""
    try:
        from server.main import broadcast
        await broadcast({
            "type": "embed_job",
            "module": "embed",
            "data": payload,
        })
    except Exception:
        pass


# ── Embedding (model-agnostic per row) ────────────────────────────

async def _embed_batch_via_bridge(model: str, texts: list[str]) -> list[Optional[str]]:
    """Embed many texts in ONE bridge embeddings.embed_batch call — GPU-batched on
    RunPod for the baked model, else the platform's Ollama; embed_log per item
    (same path as the single op; not billed by the platform). Returns pgvector literals
    aligned to `texts` (None for any failed slot). On a bridge/transport failure
    returns all-None so the caller's batch-failure + pause logic still fires.
    """
    n = len(texts)
    if n == 0:
        return []
    try:
        from server import main as _server_main
        client = _server_main.get_bridge_client()
    except Exception:
        return [None] * n
    if client is None:
        return [None] * n
    # Every character of every text: a text past one embed
    # window goes as several windows in the same batch call and its vectors
    # are combined into one (mastercode.embed_windows) — never `t[:8000]`.
    # A text inside one window is sent unchanged, as before.
    from mastercode.embed_windows import combine, to_literal, windows
    parts = [windows(t or "") for t in texts]
    flat = [w for p in parts for w in p]
    last_err: Optional[Exception] = None
    for attempt in range(ROW_RETRY_ATTEMPTS):
        try:
            # The windows go in slices of the bridge's own per-call limit
            # (BRIDGE_EMBED_BATCH_MAX); one failed slice retries the batch.
            vecs: list = []
            envelope = None
            for s in range(0, len(flat), BRIDGE_EMBED_BATCH_MAX):
                envelope = await client.request_full(
                    "embeddings.embed_batch",
                    {"model": model, "inputs": flat[s:s + BRIDGE_EMBED_BATCH_MAX]},
                )
                if not (envelope and envelope.get("ok")):
                    break
                got_slice = (envelope.get("data") or {}).get("vectors") or []
                want = len(flat[s:s + BRIDGE_EMBED_BATCH_MAX])
                vecs.extend(list(got_slice[:want]) + [None] * (want - len(got_slice[:want])))
            if envelope and envelope.get("ok"):
                out: list[Optional[str]] = []
                k = 0
                for p in parts:
                    got = [vecs[k + j] if k + j < len(vecs) else None
                           for j in range(len(p))]
                    k += len(p)
                    if not all(isinstance(v, list) and v for v in got):
                        out.append(None)
                    elif len(p) == 1:
                        out.append(to_literal(got[0]))
                    else:
                        out.append(to_literal(combine(got, [len(w) for w in p])))
                return out
            last_err = RuntimeError((envelope or {}).get("error") or "bridge batch call failed")
        except Exception as e:
            last_err = e
        if attempt + 1 < ROW_RETRY_ATTEMPTS:
            await asyncio.sleep(ROW_RETRY_BASE_DELAY * (2 ** attempt))
    logger.warning("Bridge embed_batch failed after %d attempts: %s", ROW_RETRY_ATTEMPTS, last_err)
    return [None] * n


async def _embed_batch_config_driven(model: str, texts: list[str], *,
                                     entity_id, pool) -> list[Optional[str]]:
    """Config-driven batch embed for the durable re-embed/backfill jobs, which
    embed ENTITY data (entity_memory / user_context) inside the entity pod and so
    must respect the entity's CONFIGURED embedding backend — 100% config-driven,
    NO cross-lane fallback. Resolves the backend ONCE:

      - BYO/bastion backend configured & reachable → embed each row THROUGH that
        lane (the shared resolver), NEVER the bridge.
      - BYO/bastion configured but currently DOWN → return all-None so the job's
        batch-failure/pause logic fires and the rows re-fill when it returns.
      - No BYO/bastion configured → the platform bridge endpoint IS the backend;
        one GPU-batched embeddings.embed_batch call (the fast path).

    Returns pgvector literals aligned to `texts` (None for failed/paused slots).
    """
    from mastercode.memory_engine import _byo_embedding_config, _embed_text_config_driven
    byo = await _byo_embedding_config(pool, entity_id)
    if byo:
        if byo.get("backend_down"):
            logger.warning("embed batch: BYO/bastion backend down (eid=%s) — "
                           "pausing batch, no bridge fallback", entity_id)
            return [None] * len(texts)
        # BYO/bastion is single-text (peer-forward CONNECT proxy); loop the batch
        # through the shared resolver with the already-resolved config.
        # The whole text, window by window — never `[:8000]`.
        from mastercode.embed_windows import embed_whole, to_literal
        out: list[Optional[str]] = []
        for t in texts:
            async def _one(part: str):
                return await _embed_text_config_driven(
                    part, model, entity_id=entity_id, pool=pool, byo=byo)
            out.append(to_literal(await embed_whole(t or "", _one)))
        return out
    # No BYO/bastion → the platform bridge endpoint is the chosen backend.
    return await _embed_batch_via_bridge(model, texts)


async def _embed_batch_media(items: list[tuple], *, entity_id,
                             pool) -> list[Optional[str]]:
    """Batch-embed PICTURES through the host's media endpoint.

    `items` is [(row_id, mime, base64_content)]. Returns pgvector literals
    aligned to `items`, None for any slot that did not embed.

    No bridge branch and no fallback of any kind, unlike the text batch above:
    there is no platform media embedder to fall back TO. An unconfigured host
    returns all-None, which the caller's batch-failure logic treats the same way
    it treats a down endpoint — the pictures stay unembedded and coverage says so.

    Sent one at a time because every wire shape in media_engine takes a single
    image per call; none of the vendor bodies checked batches images.
    """
    from mastercode.media_engine import (
        MEDIA_MAX_EMBED_BYTES, data_uri, decode_len, embed_media, media_config)
    cfg = await media_config(pool, entity_id)
    if not cfg:
        logger.info("media batch: no media model configured (eid=%s) — nothing "
                    "embedded, and that is a setup state not a fault", entity_id)
        return [None] * len(items)
    want = int(cfg.get("dim") or 0)
    out: list[Optional[str]] = []
    for _rid, mime, b64 in items:
        # Bound what is posted. An endpoint that refuses an oversized body
        # refuses it on every pass, so an unbounded picture is not a slow row —
        # it is a row that can never finish, retried for ever. Skipping it lets
        # the rest of the batch succeed. decode_len reads the true byte count
        # from the base64 length without decoding it.
        size = decode_len(b64)
        if MEDIA_MAX_EMBED_BYTES and size > MEDIA_MAX_EMBED_BYTES:
            logger.info("media batch: row %s is %s bytes, past the %s an endpoint "
                        "will take in one call — skipped rather than retried for "
                        "ever", _rid, size, MEDIA_MAX_EMBED_BYTES)
            out.append(None)
            continue
        vec = await embed_media(cfg, image=data_uri(mime, b64),
                                entity_id=entity_id, pool=pool)
        # A vector of the wrong width reaches _write_embedding's
        # $1::halfvec(dim) cast and raises out of _run_job uncaught. The job
        # then sits 'running' for ever — only a restart re-queues it, into the
        # same row — and because _ingest_binary_file treats a running job as
        # live, EVERY later picture upload is handed that job's id and never
        # embedded. One comparison closes a silent permanent wedge.
        if vec and want and len(vec) != want:
            logger.warning("media batch: row %s came back %dd but the media role "
                           "is set to %dd — dropped rather than wedging the job",
                           _rid, len(vec), want)
            vec = None
        out.append("[" + ",".join(str(x) for x in vec) + "]" if vec else None)
    return out


# ── JobRunner: single in-process worker ───────────────────────────

class JobRunner:
    """Continuously processes pending embed_jobs.

    One instance per server process. Started in lifespan(); stopped on shutdown.
    Crash-safe: progress is persisted after every batch.
    """

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()  # signal new job arrived

    def wake(self) -> None:
        """Tell the runner to check for new work immediately."""
        self._wake.set()

    async def start(self) -> None:
        if self._task is not None:
            return
        await self._reset_orphans()
        self._task = asyncio.create_task(self._loop(), name="embed-job-runner")
        logger.info("Embed JobRunner started")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        self._task = None

    async def _reset_orphans(self) -> None:
        """Mark any 'running' jobs from a prior process as 'pending' so the
        runner picks them up. Boot-time resume."""
        pool = brain_service.get_pool()
        if not pool:
            return
        try:
            # 'paused' is included: a paused job's 60s resume is an in-memory
            # asyncio task that dies with the process, and the claim loop only
            # ever takes 'pending' — so a restart during the pause window
            # orphaned the job FOREVER (coverage never completed, search over
            # its chunks silently empty). At boot, paused == interrupted:
            # requeue it like a running one.
            n = await pool.fetchval(
                "SELECT COUNT(*) FROM embed_jobs WHERE status IN ('running','paused')"
            )
            if n:
                await pool.execute(
                    "UPDATE embed_jobs SET status = 'pending', updated_at = now() "
                    "WHERE status IN ('running','paused')"
                )
                logger.info("Reset %d orphaned running/paused job(s) to pending", n)
        except Exception as e:
            logger.warning("Could not reset orphaned jobs: %s", e)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                claimed = await self._claim_next()
                if claimed is None:
                    # Idle: wait for a wake signal or a periodic poll
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        pass
                    self._wake.clear()
                    continue
                await self._run_job(claimed)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("JobRunner loop error: %s", e)
                await asyncio.sleep(2.0)

    async def _claim_next(self) -> Optional[dict]:
        """Atomically claim the oldest pending job."""
        pool = brain_service.get_pool()
        if not pool:
            return None
        row = await pool.fetchrow("""
            UPDATE embed_jobs
               SET status = 'running',
                   started_at = COALESCE(started_at, now()),
                   updated_at = now()
             WHERE id = (
                SELECT id FROM embed_jobs
                 WHERE status = 'pending'
                 ORDER BY created_at
                 FOR UPDATE SKIP LOCKED
                 LIMIT 1
             )
            RETURNING id, entity_id, job_type, target, total, embedded, failed,
                      last_id, model, metadata
        """)
        if not row:
            return None
        return dict(row)

    async def _run_job(self, job: dict) -> None:
        pool = brain_service.get_pool()
        if not pool:
            return

        job_id = job["id"]
        target = job["target"]
        if target == "gitea_repo":
            # A repo is not a bank of rows: clone/refresh it, then hand the
            # tree to the codesearch indexer (which embeds through the
            # entity's configured lane itself). Same row, same status
            # machine, same dashboard frames — a different body.
            await self._run_repo_job(job)
            return
        # Config-driven: a job with no recorded model resolves the entity's
        # configured model NOW; still none → fail the job loudly rather than
        # embed with a substitute the endpoint may not serve.
        if target == "user_context_media":
            model = job["model"] or await _active_media_model(pool, job["entity_id"])
            _unset_hint = "set one via /api/media-embedding/switch"
        else:
            model = job["model"] or await _active_model(pool, job["entity_id"])
            _unset_hint = "set one via /api/embedding/switch"
        if not model:
            logger.error("embed job %d: entity %s has no configured embedding "
                         "model — failing job (%s)",
                         job_id, job["entity_id"], _unset_hint)
            await pool.execute("""
                UPDATE embed_jobs
                   SET status = 'error',
                       error = 'no embedding model configured',
                       updated_at = now()
                 WHERE id = $1
            """, job_id)
            await _emit({"job_id": job_id, "status": "error",
                         "error": "no embedding model configured"})
            return
        last_id = job["last_id"] or 0
        embedded = job["embedded"] or 0
        failed = job["failed"] or 0
        consecutive_batch_failures = 0

        await _emit({
            "job_id": job_id,
            "status": "running",
            "target": target,
            "total": job["total"],
            "embedded": embedded,
            "failed": failed,
        })

        while True:
            # Cancellation check (status may have been flipped externally)
            current_status = await pool.fetchval(
                "SELECT status FROM embed_jobs WHERE id = $1", job_id
            )
            if current_status != "running":
                logger.info("Job %d status changed to %s — stopping", job_id, current_status)
                await _emit({
                    "job_id": job_id, "status": current_status,
                    "embedded": embedded, "failed": failed,
                    "total": job["total"],
                })
                return

            job_metadata = job.get("metadata") or {}
            if isinstance(job_metadata, str):
                try:
                    job_metadata = json.loads(job_metadata)
                except Exception:
                    job_metadata = {}
            try:
                rows = await self._fetch_batch(target, job["entity_id"],
                                               last_id, job_metadata)
            except TransientLaneError as e:
                # NOT done and NOT wedged: pause with the real reason and let
                # the existing resume timer try again.
                await pool.execute("""
                    UPDATE embed_jobs
                       SET status = 'paused', error = $2,
                           embedded = $3, failed = $4, last_id = $5,
                           updated_at = now()
                     WHERE id = $1
                """, job_id, str(e), embedded, failed, last_id)
                await _emit({"job_id": job_id, "status": "paused",
                             "embedded": embedded, "failed": failed,
                             "total": job["total"], "error": str(e)})
                logger.warning("Job %d paused (%s) — re-queuing in %ds",
                               job_id, e, JOB_RESUME_DELAY)
                asyncio.create_task(self._resume_after(job_id, JOB_RESUME_DELAY))
                return
            if not rows:
                # Done.
                await pool.execute("""
                    UPDATE embed_jobs
                       SET status = 'done',
                           embedded = $2,
                           failed = $3,
                           last_id = $4,
                           completed_at = now(),
                           updated_at = now()
                     WHERE id = $1
                """, job_id, embedded, failed, last_id)
                await _emit({
                    "job_id": job_id, "status": "done",
                    "embedded": embedded, "failed": failed,
                    "total": job["total"],
                })
                logger.info("Job %d done: %d embedded, %d failed", job_id, embedded, failed)
                return

            batch_had_success = False
            # Re-embed jobs honour metadata.target_model; first-fill uses the
            # job's `model`. Both billed at 90% market via the bridge.
            if target == "entity_memory_reembed":
                embed_model = (job_metadata or {}).get("target_model") or model
            else:
                embed_model = model

            # Config-driven batch: BYO/bastion when the entity configured one
            # (looped single embeds through its lane), else ONE GPU-batched
            # embeddings.embed_batch bridge call. NO cross-lane fallback. Empty
            # rows can't be embedded — count them failed up front.
            valid = [(row["id"], (row["content"] or "").strip()) for row in rows]
            embeddable = [(rid, c) for rid, c in valid if c]
            failed += len(valid) - len(embeddable)
            if target == "user_context_media":
                # A media row's `content` is base64 bytes, not language. Turn
                # each into the data URI the proven wire shape expects and send
                # it down the MEDIA lane — _embed_batch_config_driven would post
                # base64 as if it were a sentence.
                mime_by_id = {r["id"]: (r.get("mime") or "") for r in rows}
                vecs = await _embed_batch_media(
                    [(rid, mime_by_id.get(rid, ""), c) for rid, c in embeddable],
                    entity_id=job["entity_id"], pool=pool)
            else:
                vecs = await _embed_batch_config_driven(
                    embed_model, [c for _, c in embeddable],
                    entity_id=job["entity_id"], pool=pool)
            for (rid, _c), vec in zip(embeddable, vecs):
                if vec:
                    await self._write_embedding(target, rid, vec, job_metadata,
                                                model=embed_model)
                    embedded += 1
                    batch_had_success = True
                else:
                    failed += 1
            # Advance the cursor past the whole fetched batch (rows ordered by id),
            # so empty/failed rows aren't re-fetched this run.
            if rows:
                last_id = rows[-1]["id"]

            # Persist progress after the batch.
            await pool.execute("""
                UPDATE embed_jobs
                   SET embedded = $2, failed = $3, last_id = $4, updated_at = now()
                 WHERE id = $1
            """, job_id, embedded, failed, last_id)

            await _emit({
                "job_id": job_id, "status": "running",
                "embedded": embedded, "failed": failed,
                "total": job["total"], "last_id": last_id,
            })

            if batch_had_success:
                consecutive_batch_failures = 0
            else:
                consecutive_batch_failures += 1
                if consecutive_batch_failures >= JOB_PAUSE_AFTER_BATCH_FAILURES:
                    await pool.execute("""
                        UPDATE embed_jobs
                           SET status = 'paused',
                               error = $2,
                               updated_at = now()
                         WHERE id = $1
                    """, job_id, f"{_lane_endpoint_name(target)} unreachable after "
                                 f"{consecutive_batch_failures} batches")
                    await _emit({
                        "job_id": job_id, "status": "paused",
                        "embedded": embedded, "failed": failed,
                        "total": job["total"],
                        "error": f"{_lane_endpoint_name(target)} unreachable",
                    })
                    logger.warning("Job %d paused — re-queuing in %ds", job_id, JOB_RESUME_DELAY)
                    # Re-queue after delay
                    asyncio.create_task(self._resume_after(job_id, JOB_RESUME_DELAY))
                    return

    async def _run_repo_job(self, job: dict) -> None:
        """embed_jobs target='gitea_repo': index one repo NOW. Every repo the
        Host can read is indexed without being asked (codesearch.repo_sync, a pass every few
        minutes); this job is the same work for one repo, at once. metadata.repo
        is "owner/name" on the host's own gitea.

        The repo must be on the forge's own list of what this Host can read
        (repo_sync.readable_repos — gitea op='list'): a foreign repo (another
        user's that was not granted, a GitHub one) is never indexed in the pod.
        The body is repo_sync.sync_repo: clone or fetch the default branch as
        the HOST (its own credential, no token in a URL), then the incremental
        indexer, which embeds through the entity's configured lane.

        Progress on the row: total = files that changed, embedded = chunks
        written. failed = 1 with `error` set on any step that did not finish.
        Crash-safe by construction: a re-run of the same job is a fetch plus
        an incremental index (unchanged files are skipped by hash)."""
        pool = brain_service.get_pool()
        if not pool:
            return
        job_id = job["id"]
        meta = job.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        repo = str(meta.get("repo") or "").strip().strip("/")

        async def _fail(msg: str) -> None:
            await pool.execute("""
                UPDATE embed_jobs
                   SET status = 'error', failed = 1, error = $2, updated_at = now()
                 WHERE id = $1
            """, job_id, msg[:500])
            await _emit({"job_id": job_id, "status": "error", "target": "gitea_repo",
                         "repo": repo, "error": msg[:500]})
            logger.warning("embed job %d (gitea_repo %s) failed: %s", job_id, repo, msg)

        if not valid_repo_name(repo):
            await _fail("metadata.repo must be 'owner/name' on the forge")
            return
        if str(meta.get("ref") or "").strip():
            await _fail("the code index follows each repo's default branch; read another branch "
                        "with gitea op='read' ref=...")
            return
        from codesearch import repo_sync
        readable = await repo_sync.readable_repos(also=[repo])
        if readable is None:
            await _fail("could not ask the forge which repos this host can read "
                        "(no gitea identity in the pod, or the forge did not answer)")
            return
        row = next((r for r in readable if r["repo"].lower() == repo.lower()), None)
        if row is None:
            await _fail(f"{repo} is not a repo this host owns or was granted on the forge; "
                        "a foreign repo is never indexed in the pod (read it with the gitea tool, "
                        "or dispatch an agent)")
            return
        repo = row["repo"]

        async def _still_running() -> bool:
            # Cancellation between the clone and the index — the status may
            # have been flipped externally while git ran.
            if await pool.fetchval("SELECT status FROM embed_jobs WHERE id = $1", job_id) != "running":
                return False
            await _emit({"job_id": job_id, "status": "running", "target": "gitea_repo",
                         "repo": repo, "stage": "index"})
            return True

        await _emit({"job_id": job_id, "status": "running", "target": "gitea_repo",
                     "repo": repo, "stage": "clone"})
        try:
            from codesearch import store as _cs_store
            db = _cs_store.connect()   # the pod's Postgres pool
            res = await repo_sync.sync_repo(db, row, force=True, before_index=_still_running)
        except Exception as e:  # noqa: BLE001
            await _fail(f"index failed for {repo}: {e}")
            return
        if res.get("reason") == "stopped before indexing":
            return
        if res.get("state") != "indexed":
            await _fail(str(res.get("reason") or f"{repo} was not indexed"))
            return
        total = int(res.get("files_changed") or 0)
        written = int(res.get("chunks_written") or 0)
        await pool.execute("""
            UPDATE embed_jobs
               SET status = 'done', total = $2, embedded = $3, failed = 0,
                   completed_at = now(), updated_at = now()
             WHERE id = $1
        """, job_id, total, written)
        await _emit({"job_id": job_id, "status": "done", "target": "gitea_repo", "repo": repo,
                     "total": total, "embedded": written, "failed": 0,
                     "files_deleted": res.get("files_deleted", 0)})
        logger.info("embed job %d done: repo %s files_changed=%d chunks=%d",
                    job_id, repo, total, written)

    async def _fetch_batch(self, target: str, entity_id: int, last_id: int,
                           metadata: Optional[dict] = None) -> list:
        # (user_context honours metadata.ids — see _job_ids — and rehydrates
        # externalized rows so the marker is never what gets embedded.)
        pool = brain_service.get_pool()
        if not pool:
            return []
        from mastercode.memory_engine import active_embedding_column
        if target == "entity_memory":
            # Multi-dim: first-fill = rows missing the ACTIVE column's vector
            # (not just the legacy 1024 `embedding`). Whitelisted name.
            content_col, _ = await active_embedding_column(pool, entity_id)
            return await pool.fetch(f"""
                SELECT id, content FROM entity_memory
                 WHERE entity_id = $1
                   AND {content_col} IS NULL
                   AND status = 'active'
                   AND id > $2
                 ORDER BY id
                 LIMIT $3
            """, entity_id, last_id, BATCH_SIZE)
        elif target == "user_context":
            content_col, _ = await active_embedding_column(pool, entity_id)
            # Binary file-lane rows (metadata.kind='file') hold base64, not
            # language — embedding them would poison search. The shared
            # fragment keeps every selector/denominator in lockstep.
            from server.routes.context import NOT_FILE_KIND_SQL
            ids = _job_ids(metadata)
            if ids:
                # Per-ids job (the Explorer's per-file "Re-embed"): embed the
                # named rows regardless of current vector state — a refresh,
                # not a first-fill.
                rows = await pool.fetch(f"""
                    SELECT id, content, storage_backend FROM user_context
                     WHERE entity_id = $1
                       AND id = ANY($2::int[])
                       AND status != 'dismissed'
                       AND {NOT_FILE_KIND_SQL}
                       AND id > $3
                     ORDER BY id
                     LIMIT $4
                """, entity_id, ids, last_id, BATCH_SIZE)
            else:
                rows = await pool.fetch(f"""
                    SELECT id, content, storage_backend FROM user_context
                     WHERE entity_id = $1
                       AND {content_col} IS NULL
                       AND status != 'dismissed'
                       AND {NOT_FILE_KIND_SQL}
                       AND id > $2
                     ORDER BY id
                     LIMIT $3
                """, entity_id, last_id, BATCH_SIZE)
            # Externalized rows hold only the pointer marker in `content` — embedding
            # that would silently poison search for the s3/bastion lane. Rehydrate
            # the real bytes per non-pg row; an offline/unfetchable holder yields
            # content=None, which the runner already counts as failed (and the
            # cursor advances, so the job never wedges on it).
            out = []
            for r in rows:
                d = dict(r)
                if (d.get("storage_backend") or "pg") != "pg":
                    try:
                        from brain.mastercode import context_backends
                        d["content"] = await context_backends.retrieve_user_context_content(
                            pool, entity_id, d["id"], d["storage_backend"])
                    except Exception as e:
                        logger.info("embed fetch: user_context %s rehydrate failed "
                                    "(holder offline?) — counted failed: %s", d["id"], e)
                        d["content"] = None
                out.append(d)
            return out
        elif target == "user_context_media":
            # The MEDIA lane: exactly the rows the text lane excludes. Uploaded
            # PICTURES, and the FRAMES a clip was decoded into on the user's own
            # machine — one predicate for both, media_embeddable_sql,
            # which carries the reason a row with no bytes is not selected.
            #
            # THIS IS THE RETRY LANE, and for frames it is the ONLY one. The
            # inline pass at extraction time is an optimisation; if it fails
            # part way — a provider timeout, a 502 on frame 180 of 240, the
            # request aborted — the rows are already written with their staged
            # bytes and this is what finishes them. Without that a frame whose
            # embedding failed was unsearchable for ever, repairable only by
            # re-pulling the whole clip across the user's link.
            from mastercode.media_engine import (
                MEDIA_COLUMN_BY_DIM, MEDIA_MAX_EMBED_BYTES, media_config_state,
                media_embeddable_sql)
            # An unreadable config is not an unconfigured host. Without this the
            # job below sees no column, returns no rows, and reports DONE with
            # every picture still unembedded — over a DB blip.
            _cfg, unreadable = await media_config_state(pool, entity_id)
            if unreadable:
                raise TransientLaneError(
                    "the media embedding configuration could not be read just "
                    "now (a transient error, not a setup problem) — retrying")
            # Resolved from the config just read, NOT a second read: between two
            # reads a blip would return [] here and finish the job as done, which
            # is the failure the check above exists to prevent.
            media_col = MEDIA_COLUMN_BY_DIM.get(_cfg.get("dim")) if _cfg else None
            media_model = (_cfg or {}).get("model")
            if not media_col:
                # No media model configured. Not an error and not an empty
                # corpus — there is nowhere to put a vector, so the job finishes
                # rather than embedding into a column belonging to another model.
                logger.info("media fetch: no media model configured for entity %s", entity_id)
                return []
            # content is fetched only UNDER THE CAP. The cap in _embed_batch_media
            # runs after this, so without the CASE a 24MB picture was still pulled
            # out of Postgres in full on every pass, just to be skipped — and
            # skipped rows stay stale, so it happened on every future fill too.
            # NULL content reaches the skip below and is reported honestly.
            rows = await pool.fetch(f"""
                SELECT id,
                       CASE WHEN length(content) <= $5 THEN content ELSE NULL END
                           AS content,
                       length(content) AS b64_len,
                       storage_backend, metadata->>'mime' AS mime
                  FROM user_context
                 WHERE entity_id = $1
                   AND status != 'dismissed'
                   AND {media_embeddable_sql(media_col, '$4')}
                   AND id > $2
                 ORDER BY id
                 LIMIT $3
            """, entity_id, last_id, BATCH_SIZE, media_model,
                 (MEDIA_MAX_EMBED_BYTES * 4) // 3 + 4)
            out = []
            for r in rows:
                d = dict(r)
                # retrieve_user_context_content returns None for a pg row AND
                # for an unreachable holder (context_backends.py:1308-1330), so
                # only call it for non-pg rows — otherwise a perfectly good
                # inline picture would be blanked.
                if (d.get("storage_backend") or "pg") != "pg":
                    try:
                        from brain.mastercode import context_backends
                        d["content"] = await context_backends.retrieve_user_context_content(
                            pool, entity_id, d["id"], d["storage_backend"])
                    except Exception as e:
                        logger.info("media fetch: user_context %s rehydrate failed "
                                    "(holder offline?) — counted failed: %s", d["id"], e)
                        d["content"] = None
                elif d.get("content") is None and (d.get("b64_len") or 0) > 0:
                    # Blanked by the cap above, not missing. Say which, once,
                    # rather than letting it read as a rehydrate failure.
                    logger.info("media fetch: user_context %s is ~%s bytes, past the "
                                "%s one embed call will carry — left unembedded",
                                d["id"], (d["b64_len"] * 3) // 4, MEDIA_MAX_EMBED_BYTES)
                out.append(d)
            return out
        elif target == "entity_memory_reembed":
            target_model = (metadata or {}).get("target_model")
            return await pool.fetch("""
                SELECT id, content FROM entity_memory
                 WHERE entity_id = $1
                   AND status = 'active'
                   AND (embedding_model IS DISTINCT FROM $2 OR embedding_model IS NULL)
                   AND id > $3
                 ORDER BY id
                 LIMIT $4
            """, entity_id, target_model, last_id, BATCH_SIZE)
        else:
            raise ValueError(f"Unknown target: {target}")

    async def _write_embedding(self, target: str, row_id: int, vec: str,
                               metadata: Optional[dict] = None,
                               model: Optional[str] = None) -> None:
        pool = brain_service.get_pool()
        if not pool:
            return
        from mastercode.memory_engine import (
            active_embedding_column, EMBEDDING_COLUMN_BY_DIM, embedding_cast_type,
        )
        if target == "entity_memory":
            # Multi-dim: first-fill writes into the column matching the
            # entity's active model's dim. Whitelisted name (injection-safe).
            # Stamp embedding_model like the reembed path does:
            # unstamped fills left by-model coverage blind to filled columns,
            # so 'Embed missing (N)' never converged.
            eid = brain_service._entity_id
            content_col, dim = await active_embedding_column(pool, eid)
            cast = embedding_cast_type(dim)
            await pool.execute(
                f"""UPDATE entity_memory
                       SET {content_col} = $1::{cast},
                           embedding_model = COALESCE($3, embedding_model),
                           updated_at = now()
                     WHERE id = $2""",
                vec, row_id, model,
            )
        elif target == "user_context":
            # Multi-dim: user_context mirrors entity_memory's dim columns.
            eid = brain_service._entity_id
            content_col, dim = await active_embedding_column(pool, eid)
            cast = embedding_cast_type(dim)
            await pool.execute(
                f"UPDATE user_context SET {content_col} = $1::{cast}, updated_at = now() WHERE id = $2",
                vec, row_id,
            )
        elif target == "user_context_media":
            # Media vectors go ONLY into the media column family, never the text
            # columns — a media model's space is not the text model's even at
            # the same width, and sharing a column would split-brain search
            # (migrations/v135 carries the rationale). Stamp the model too, so
            # media coverage can say which model wrote a row.
            from mastercode.media_engine import (
                MEDIA_DROP_STAGED_CONTENT_SQL, active_media_target, media_cast_type)
            eid = brain_service._entity_id
            # The stamp is the model that PRODUCED this vector, read from the
            # same live config _embed_batch_media embedded with — never the
            # job's `model`, which is whatever was configured when the job was
            # enqueued. Switch models mid-job and the job's name would stamp
            # rows the NEW model embedded; switch back later and they would read
            # as fresh for a model that never saw them.
            media_col, dim, live_model = await active_media_target(pool, eid)
            if not media_col:
                logger.warning("media write: no media column for entity %s — "
                               "dropping the vector rather than guessing a column", eid)
                return
            cast = media_cast_type(dim)
            # AND THE STAGED BYTES GO, in the same statement that writes the
            # vector. A frame's picture is staging, not storage (keeping it
            # measured at 12x); dropping it in a second statement would leave
            # a window where a crash keeps 5 MB per clip that nothing reads.
            # A picture is untouched — its content is the user's stored file.
            await pool.execute(
                f"""UPDATE user_context
                       SET {media_col} = $1::{cast},
                           media_embed_model = COALESCE($3, media_embed_model),
                           {MEDIA_DROP_STAGED_CONTENT_SQL},
                           updated_at = now()
                     WHERE id = $2""",
                vec, row_id, live_model or model,
            )
        elif target == "entity_memory_reembed":
            # Keep-warm: write ONLY the target dim column + the model tag.
            # Do NOT clear the other dim columns — keeping prior embeddings
            # means switching back to a previously-used model needs no
            # re-embed. The active-column router reads only the column for
            # the current model, so a stale other-dim vector is never seen.
            col = (metadata or {}).get("target_column") or "embedding"
            # Whitelist guard: the interpolated column must be a known dim
            # column. Falls back to the 1024d default on any unexpected value.
            if col not in EMBEDDING_COLUMN_BY_DIM.values():
                col = "embedding"
            # Derive the dim from the column so the literal is cast to the
            # column's pgvector type (halfvec for >2000d). Default 1024.
            dim = next((d for d, c in EMBEDDING_COLUMN_BY_DIM.items() if c == col), 1024)
            cast = embedding_cast_type(dim)
            model = (metadata or {}).get("target_model")
            await pool.execute(
                f"""UPDATE entity_memory
                       SET {col} = $1::{cast},
                           embedding_model = $2,
                           updated_at = now()
                     WHERE id = $3""",
                vec, model, row_id,
            )

    async def _resume_after(self, job_id: int, delay: float) -> None:
        await asyncio.sleep(delay)
        pool = brain_service.get_pool()
        if not pool:
            return
        # Only resume if still paused (user may have cancelled in the meantime)
        await pool.execute("""
            UPDATE embed_jobs
               SET status = 'pending', error = NULL, updated_at = now()
             WHERE id = $1 AND status = 'paused'
        """, job_id)
        self.wake()


# Single global runner — set by main.py during lifespan.
_runner: Optional[JobRunner] = None


def get_runner() -> Optional[JobRunner]:
    return _runner


def set_runner(runner: Optional[JobRunner]) -> None:
    global _runner
    _runner = runner


def _job_ids(metadata: Optional[dict]) -> list[int]:
    """metadata.ids for a per-ids user_context job (the Explorer's per-file
    re-embed), validated to a bounded list of ints. Empty list = corpus job."""
    raw = (metadata or {}).get("ids")
    if not isinstance(raw, list) or not raw:
        return []
    out = []
    for v in raw[:1000]:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


# ── Public job creation (used by other routes) ────────────────────

async def enqueue_job(
    entity_id: int,
    job_type: str,
    target: str,
    metadata: Optional[dict] = None,
    model: Optional[str] = None,
) -> Optional[dict]:
    """Insert a new pending job. Returns the inserted row as a dict, or None
    if the brain isn't ready. Sizes the `total` field by counting eligible
    rows up front so the UI can show a real progress denominator.

    `model` omitted → the entity's CONFIGURED model (never a literal); an
    unconfigured entity raises so the caller surfaces a real error instead of
    silently embedding with a model its endpoint may not serve."""
    pool = brain_service.get_pool()
    if not pool:
        return None
    # The media lane has its OWN model. Resolving it through _active_model would
    # stamp rows with the TEXT model's name and, worse, read as "configured"
    # when media is not set up at all.
    if target == "user_context_media":
        model = (model or "").strip() or await _active_media_model(pool, entity_id)
        if not model:
            raise ValueError("no media embedding model configured for this host — "
                             "set one via /api/media-embedding/switch")
    else:
        model = (model or "").strip() or await _active_model(pool, entity_id)
        if not model:
            raise ValueError("no embedding model configured for this host — "
                             "set one via /api/embedding/switch")

    from mastercode.memory_engine import active_embedding_column
    if target == "entity_memory":
        # Multi-dim: denominator = rows missing the ACTIVE column's vector,
        # matching what _fetch_batch selects. Whitelisted name.
        content_col, _ = await active_embedding_column(pool, entity_id)
        total = await pool.fetchval(f"""
            SELECT COUNT(*) FROM entity_memory
             WHERE entity_id = $1 AND {content_col} IS NULL AND status = 'active'
        """, entity_id)
    elif target == "user_context":
        content_col, _ = await active_embedding_column(pool, entity_id)
        # Denominators mirror _fetch_batch: file-lane rows never embed, so
        # they never count (a total that includes them would wedge progress
        # at total > embedded forever).
        from server.routes.context import NOT_FILE_KIND_SQL
        ids = _job_ids(metadata)
        if ids:
            # Per-ids job: denominator = the named live rows (refresh semantics —
            # matches what _fetch_batch selects for an ids job).
            total = await pool.fetchval(f"""
                SELECT COUNT(*) FROM user_context
                 WHERE entity_id = $1 AND id = ANY($2::int[]) AND status != 'dismissed'
                   AND {NOT_FILE_KIND_SQL}
            """, entity_id, ids)
        else:
            total = await pool.fetchval(f"""
                SELECT COUNT(*) FROM user_context
                 WHERE entity_id = $1 AND {content_col} IS NULL AND status != 'dismissed'
                   AND {NOT_FILE_KIND_SQL}
            """, entity_id)
    elif target == "user_context_media":
        # Denominator mirrors _fetch_batch exactly: stored pictures with no
        # vector in the media column. An unconfigured host has no media column,
        # so the total is 0 and the job completes immediately — "not set up
        # yet" must read as nothing to do, never as a failure.
        from mastercode.media_engine import (
            active_media_target, media_embeddable_sql)
        media_col, _, media_model = await active_media_target(pool, entity_id)
        if not media_col:
            total = 0
        else:
            # Same predicate the fetch uses, from the same helper — a
            # denominator that disagreed with the fetch would report done with
            # work outstanding, or never reach its own total.
            total = await pool.fetchval(f"""
                SELECT COUNT(*) FROM user_context
                 WHERE entity_id = $1
                   AND status != 'dismissed'
                   AND {media_embeddable_sql(media_col, '$2')}
            """, entity_id, media_model)
    elif target == "entity_memory_reembed":
        # Re-embed everything whose embedding_model doesn't match the
        # one this switch is moving to. metadata.target_model is set.
        target_model = (metadata or {}).get("target_model")
        total = await pool.fetchval("""
            SELECT COUNT(*) FROM entity_memory
             WHERE entity_id = $1 AND status = 'active'
               AND (embedding_model IS DISTINCT FROM $2 OR embedding_model IS NULL)
        """, entity_id, target_model)
    elif target == "gitea_repo":
        # A repo on the host's gitea, cloned + indexed by _run_repo_job. The
        # denominator is unknown until the tree is on disk; the runner sets
        # total to the files it actually indexed.
        repo = str((metadata or {}).get("repo") or "").strip().strip("/")
        if not valid_repo_name(repo):
            raise ValueError("gitea_repo needs metadata.repo as 'owner/name'")
        # One live job per repo: a second enqueue while one is pending or
        # running would just clone the same tree twice.
        live = await pool.fetchval("""
            SELECT id FROM embed_jobs
             WHERE entity_id = $1 AND target = 'gitea_repo'
               AND status IN ('pending', 'running', 'paused')
               AND metadata->>'repo' = $2
             LIMIT 1
        """, entity_id, repo)
        if live:
            raise ValueError(f"a job for {repo} is already queued or running (job {live})")
        total = 0
    else:
        raise ValueError(f"Unknown target: {target}")

    row = await pool.fetchrow("""
        INSERT INTO embed_jobs (entity_id, job_type, target, total, model, metadata)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        RETURNING id, entity_id, job_type, target, status, total, embedded,
                  failed, last_id, model, error, metadata, created_at,
                  started_at, completed_at, updated_at
    """,
        entity_id, job_type, target, int(total or 0),
        model,
        json.dumps(metadata or {}),
    )

    if _runner is not None:
        _runner.wake()

    return _row_to_dict(row)


# ── Routes ─────────────────────────────────────────────────────────

def _row_to_dict(r) -> dict:
    if r is None:
        return None
    out = dict(r)
    for k in ("created_at", "started_at", "completed_at", "updated_at"):
        if out.get(k):
            out[k] = out[k].isoformat()
    if isinstance(out.get("metadata"), str):
        try:
            out["metadata"] = json.loads(out["metadata"])
        except Exception:
            pass
    return out


@router.get("/jobs", dependencies=[Depends(requires_permission("read:brain_memory"))])
async def list_jobs(
    status: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
):
    """List embed jobs for this entity, newest first."""
    pool = brain_service.get_pool()
    if not pool:
        return {"error": "Brain not initialized"}
    eid = brain_service._entity_id

    where = "WHERE entity_id = $1"
    params: list = [eid]
    if status:
        where += " AND status = $2"
        params.append(status)
    params.append(limit)

    rows = await pool.fetch(f"""
        SELECT id, entity_id, job_type, target, status, total, embedded,
               failed, last_id, model, error, metadata, created_at,
               started_at, completed_at, updated_at
          FROM embed_jobs {where}
         ORDER BY created_at DESC
         LIMIT ${len(params)}
    """, *params)

    return {"jobs": [_row_to_dict(r) for r in rows]}


@router.get("/jobs/{job_id}", dependencies=[Depends(requires_permission("read:brain_memory"))])
async def get_job(job_id: int):
    pool = brain_service.get_pool()
    if not pool:
        return {"error": "Brain not initialized"}
    eid = brain_service._entity_id
    row = await pool.fetchrow("""
        SELECT * FROM embed_jobs WHERE id = $1 AND entity_id = $2
    """, job_id, eid)
    if not row:
        raise HTTPException(404, "Job not found")
    return _row_to_dict(row)


@router.post("/jobs/{job_id}/cancel", dependencies=[Depends(requires_permission("write:brain_memory"))])
async def cancel_job(job_id: int):
    pool = brain_service.get_pool()
    if not pool:
        return {"error": "Brain not initialized"}
    eid = brain_service._entity_id
    row = await pool.fetchrow("""
        UPDATE embed_jobs
           SET status = 'cancelled',
               completed_at = now(),
               updated_at = now()
         WHERE id = $1 AND entity_id = $2
           AND status IN ('pending','running','paused')
        RETURNING *
    """, job_id, eid)
    if not row:
        raise HTTPException(404, "Job not found or already finalized")
    return _row_to_dict(row)


@router.post("/jobs/{job_id}/retry", dependencies=[Depends(requires_permission("write:brain_memory"))])
async def retry_job(job_id: int):
    pool = brain_service.get_pool()
    if not pool:
        return {"error": "Brain not initialized"}
    eid = brain_service._entity_id
    row = await pool.fetchrow("""
        UPDATE embed_jobs
           SET status = 'pending',
               error = NULL,
               completed_at = NULL,
               updated_at = now()
         WHERE id = $1 AND entity_id = $2
           AND status IN ('failed','cancelled','done','paused')
        RETURNING *
    """, job_id, eid)
    if not row:
        raise HTTPException(404, "Job not found or already running")
    if _runner is not None:
        _runner.wake()
    return _row_to_dict(row)


@router.post("/jobs", dependencies=[Depends(requires_permission("write:brain_memory"))])
async def enqueue_fill_job(body: dict):
    """Queue a FILL job from the dashboard: embed only the rows MISSING a
    vector on the entity's active model/column. Fill-only by design — the UI
    never re-embeds; models live side by side in per-dim columns and switching
    adds coverage, it never migrates or discards vectors (the Health-tab
    'embed missing' action). Re-embed stays a host-tool /
    operator concern (embed_jobs target=entity_memory_reembed)."""
    target = str((body or {}).get("target") or "entity_memory").strip()
    if target not in ("entity_memory", "user_context", "user_context_media"):
        raise HTTPException(
            400, "target must be entity_memory, user_context or user_context_media")
    pool = brain_service.get_pool()
    if not pool:
        return {"error": "Brain not initialized"}
    eid = brain_service._entity_id
    # The media lane needs the same table guard as the text one, and it is the
    # ONLY way to act on what /api/media-embedding/switch reports: after a
    # model switch that returns rows_missing_media > 0, this is the job that
    # clears it. Without the target here the host tool could queue that work and
    # an app could not, while the number telling the user it was needed came
    # from a route they could reach.
    if target in ("user_context", "user_context_media"):
        # Same to_regclass guard coverage() carries: entities that never
        # ingested context have no user_context table, and enqueue_job would
        # 500 on UndefinedTableError. 409 = clean no-op.
        if not await pool.fetchval("SELECT to_regclass('public.user_context') IS NOT NULL"):
            raise HTTPException(409, "no user context ingested yet — nothing to embed")
    try:
        row = await enqueue_job(entity_id=eid, job_type="fill", target=target)
    except ValueError as e:
        raise HTTPException(409, str(e))
    if row is None:
        return {"error": "Brain not initialized"}
    return row


@router.get("/coverage", dependencies=[Depends(requires_permission("read:brain_memory"))])
async def coverage():
    """Embedding coverage across both memory banks for the dashboard."""
    pool = brain_service.get_pool()
    if not pool:
        return {"error": "Brain not initialized"}
    eid = brain_service._entity_id

    # Multi-dim: coverage is measured against the ACTIVE column (the one the
    # entity's current model writes/reads), not the legacy 1024 `embedding`.
    # Whitelisted name (injection-safe).
    from mastercode.memory_engine import active_embedding_column
    content_col, _ = await active_embedding_column(pool, eid)

    em = await pool.fetchrow(f"""
        SELECT COUNT(*) AS total,
               COUNT({content_col}) AS embedded
          FROM entity_memory
         WHERE entity_id = $1 AND status = 'active'
    """, eid)

    # user_context is created lazily by context.py on first external-context
    # intake — it's ABSENT on entities that never ingested any,
    # where querying it raised UndefinedTableError → 500 on this route (and broke
    # the Memory app's Health lens). Guard with the same to_regclass check the
    # v049/v071 migrations use; report zeros when the bank doesn't exist yet.
    if await pool.fetchval("SELECT to_regclass('public.user_context') IS NOT NULL"):
        from server.routes.context import NOT_FILE_KIND_SQL
        # NOT_FILE_KIND_SQL: enqueue/_fetch_batch already exclude binary
        # file-lane rows (they can never embed) — coverage counting them made
        # `missing` permanently positive and the "Embed missing" button a
        # dead total=0 job (phantom-missing).
        uc = await pool.fetchrow(f"""
            SELECT COUNT(*) AS total,
                   COUNT({content_col}) AS embedded
              FROM user_context
             WHERE entity_id = $1 AND status != 'dismissed'
               AND {NOT_FILE_KIND_SQL}
        """, eid)
    else:
        uc = {"total": 0, "embedded": 0}

    active_jobs = await pool.fetchval("""
        SELECT COUNT(*) FROM embed_jobs
         WHERE entity_id = $1 AND status IN ('pending','running','paused')
    """, eid)

    return {
        "entity_memory": {
            "total": em["total"] or 0,
            "embedded": em["embedded"] or 0,
            "missing": (em["total"] or 0) - (em["embedded"] or 0),
        },
        "user_context": {
            "total": uc["total"] or 0,
            "embedded": uc["embedded"] or 0,
            "missing": (uc["total"] or 0) - (uc["embedded"] or 0),
        },
        "active_jobs": int(active_jobs or 0),
    }


@router.get("/coverage/by-model", dependencies=[Depends(requires_permission("read:brain_memory"))])
async def coverage_by_model():
    """Per-model embedding readiness for the Memory app.

    For each embedding model, how many active entity_memory rows have a vector
    the model can actually search — COLUMN-FIRST: a model is
    covered on a row when its own dim column holds a vector, because models
    live side by side in per-dim columns and a fill only ever ADDS to a
    model's own column. The scalar embedding_model stamp records the LAST
    writer only, so it cannot represent multi-model coverage — it is used
    solely to dis-credit a row from model M when a same-dim RIVAL explicitly
    stamped it (same dim ≠ same embedding space; a rival's vector is not
    searchable by M). Rows with a filled column and a NULL/different-dim stamp
    credit M (best the schema can attribute; converges as stamped fills land).
    Counts come straight from the DB (no bridge needed); dim + rate + the
    user-facing provider are enriched from the platform catalog when the
    bridge is up, and the list still renders (counts only) when it is not.
    """
    pool = brain_service.get_pool()
    if not pool:
        return {"error": "Brain not initialized"}
    eid = brain_service._entity_id

    total = await pool.fetchval(
        "SELECT COUNT(*) FROM entity_memory WHERE entity_id=$1 AND status='active'", eid,
    ) or 0

    # Rows actually embedded by each model — the stamp the engine writes alongside
    # the dim-column vector (synaptive_handler routes to the active column AND
    # stamps embedding_model atomically, so the stamp implies the vector exists).
    rows = await pool.fetch("""
        SELECT embedding_model AS model, COUNT(*) AS n
          FROM entity_memory
         WHERE entity_id=$1 AND status='active' AND embedding_model IS NOT NULL
         GROUP BY embedding_model
    """, eid)
    by_model = {r["model"]: int(r["n"]) for r in rows}

    from server.routes import embedding as embedding_route
    cur = await embedding_route.current()
    current_model = cur.get("model")

    catalog_by_id: dict = {}
    catalog_available = False
    try:
        cat = await embedding_route.catalog()
        catalog_by_id = {m["id"]: m for m in (cat.get("embedding_models") or []) if m.get("id")}
        catalog_available = True
    except Exception as exc:  # bridge down — degrade to counts-only
        logger.warning("by-model coverage: catalog unavailable: %s", exc)

    from mastercode.memory_engine import model_dim, EMBEDDING_COLUMN_BY_DIM
    # Union of catalog models and any model already present on rows (legacy models
    # the catalog no longer lists still show their readiness).
    ids = list(dict.fromkeys(list(catalog_by_id.keys()) + list(by_model.keys())))
    dims = {mid: (catalog_by_id.get(mid, {}).get("output_dim") or model_dim(mid))
            for mid in ids}
    # Fillable = rows a FILL job can actually reach (column NULL). Same-dim
    # models share ONE physical column, so a gap made of same-dim-rival rows
    # is NOT fillable — only a re-embed (which overwrites the rival's vectors
    # in the shared column) converts it, and re-embed is a consented host
    # action, never a dashboard button (never-migrate-vectors doctrine). The
    # UI branches on this so it never renders a dead 0-row fill button.
    fillable_by_col: dict = {}
    for col in {EMBEDDING_COLUMN_BY_DIM.get(d) for d in dims.values()} - {None}:
        fillable_by_col[col] = int(await pool.fetchval(f"""
            SELECT COUNT(*) FROM entity_memory
             WHERE entity_id = $1 AND status = 'active' AND {col} IS NULL
        """, eid) or 0)
    models = []
    for mid in ids:
        meta = catalog_by_id.get(mid, {})
        dim = dims.get(mid)
        col = EMBEDDING_COLUMN_BY_DIM.get(dim)
        if col:
            # Column-first (see docstring): vector present in M's own column,
            # minus rows a same-dim rival explicitly stamped. Column name is
            # whitelisted by construction (EMBEDDING_COLUMN_BY_DIM values).
            rivals = [o for o in ids if o != mid and dims.get(o) == dim]
            embedded = int(await pool.fetchval(f"""
                SELECT COUNT(*) FROM entity_memory
                 WHERE entity_id = $1 AND status = 'active'
                   AND {col} IS NOT NULL
                   AND (embedding_model IS NULL
                        OR embedding_model = $2
                        OR NOT (embedding_model = ANY($3::text[])))
            """, eid, mid, rivals) or 0)
        else:
            # No storage column for this dim (model can't be assigned anyway) —
            # fall back to the stamp count so legacy rows still render.
            embedded = by_model.get(mid, 0)
        is_current = mid == current_model
        in_catalog = mid in catalog_by_id
        # WHO serves this model, and where. The platform catalog is the list
        # the bridge serves (provider 'privatae'); the CURRENT model is what
        # the engine embeds with, and for a bring-your-own or bastion lane it
        # is the user's own endpoint — never 'privatae' (qwen3-embedding:4b on
        # a Host's own endpoint once read "provider privatae, in_catalog false,
        # ready false" while it was mid-fill).
        own_endpoint = bool(cur.get("embedding_base_url") or cur.get("bastion"))
        if is_current and cur.get("configured") and own_endpoint:
            provider = cur.get("provider") or "your endpoint"
            source = "your endpoint"
        elif in_catalog:
            provider = meta.get("provider") or "privatae"
            source = "platform catalog"
        elif not catalog_available:
            # The bridge did not answer: a platform-served model cannot be
            # told from a delisted one, so say that instead of guessing.
            provider = cur.get("provider") if is_current else "privatae"
            source = "catalog unavailable"
        else:
            provider = "unknown"
            source = "legacy rows only"
        models.append({
            "model": mid,
            "provider": provider,
            "source": source,
            "endpoint": cur.get("embedding_base_url") if is_current else None,
            "dim": meta.get("output_dim") or model_dim(mid),
            "charge_microdollars_per_M": meta.get("charge_microdollars_per_M"),
            "embedded": embedded,
            "total": total,
            "fillable": fillable_by_col.get(col, 0) if col else 0,
            # `ready` = COVERAGE COMPLETE (every active row has a vector in
            # this model's column). It has never meant "the endpoint answers".
            "ready": total > 0 and embedded >= total,
            "coverage_complete": total > 0 and embedded >= total,
            "is_current": is_current,
            "in_catalog": in_catalog,
            "catalog_available": catalog_available,
        })
    # Current first, then ready, then by descending coverage.
    models.sort(key=lambda m: (not m["is_current"], not m["ready"], -m["embedded"]))
    return {"total": int(total), "current_model": current_model,
            "current_provider": cur.get("provider"),
            "current_endpoint": cur.get("embedding_base_url"),
            "catalog_available": catalog_available,
            "meaning": {
                "ready": "coverage complete: every active memory row has a vector in this model's column",
                "in_catalog": "listed in the platform's embedding catalog; a bring-your-own or bastion model is not, by design",
            },
            "models": models}
