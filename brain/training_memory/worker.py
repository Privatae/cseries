"""
Training Memory worker — wraps the session-segmentation scripts as a
per-job pipeline.

  1. extract_pairs_for_job(job_dir)        — preprocess + extract DPO triples
  2. estimate_cost(counts, billing)        — token & dollar projection
  3. run_full_pipeline(job_dir, meta)      — async generator yielding SSE events
  4. sample_rules_for_role(role, n)        — last N inserted rules for the report view

Each call reads inputs from the job's directory tree and writes intermediate
files (pairs.jsonl, classified.jsonl, insert_log.jsonl) for resumability.
"""
import asyncio
import json
import logging
import math
import os
import shutil
import zipfile
from pathlib import Path
from typing import AsyncIterator

import aiohttp
import asyncpg

from server import brain_service

# Co-located extractor, kept in this package so the cseries container is
# self-contained.
from .extract_dpo_pairs import extract_pairs as _extract_pairs_for_file

logger = logging.getLogger("privatae.training_memory.worker")

ANTHROPIC_API = "https://api.anthropic.com/v1"
# Anthropic-locked (BYOK batch API) — model centralized in inference_config,
# not pinned here.
from mastercode.inference_config import LOCKED_MODEL_DEFAULTS as _LOCKED_MODELS
HAIKU_MODEL = _LOCKED_MODELS["training"]

# Written beside review_state.json by the classify phase; read by the review
# route (server/routes/training_memory.py).
CLASSIFY_SUMMARY = "classify_summary.json"
# NO pinned embed model/URL: rule embeddings go through the canonical
# config-driven path (the entity's CONFIGURED model). The target column is
# routed by dim (correction_embeddings, multi-dim); a dim with no
# trigger column stores NULL instead of embedding with a model the entity
# doesn't serve.

# Same prompt as the prod classifier. Source of truth: prod behavioral_corrections.py.
CLASSIFIER_SYSTEM = """You are a behavioral feedback classifier for a synaptive entity platform.

Given an exchange between a user and an entity, determine:
1. Is the user providing negative feedback, correction, or expressing frustration about the entity's behavior or response?
2. If yes, extract a clear, actionable behavioral rule the entity should follow going forward.
3. Identify which personality dimensions should shift and in what direction.

Personality dimensions (0.0-2.0 scale, 1.0 is baseline/neutral):
- warmth, directness, humor, formality, curiosity, assertiveness, patience, vulnerability, energy, depth

For codebook_signals: use small deltas (-0.15 to +0.15). Negative = decrease, positive = increase. Only include dimensions clearly implicated by the feedback.

Respond with ONLY valid JSON, no markdown fences, no explanation:
{"is_feedback":true/false,"type":"rejection"|"correction"|"frustration"|null,"confidence":0.0-1.0,"rule":"behavioral rule string"|null,"codebook_signals":{}}"""


# ---------------------------------------------------------------
# Stage 1 — extract pairs from uploaded sources
# ---------------------------------------------------------------

async def extract_pairs_for_job(job_dir: Path) -> dict:
    """Run extractor over the job's raw/ tree, write pairs.jsonl, return counts."""
    raw = job_dir / "raw"
    expanded = job_dir / "expanded"
    expanded.mkdir(exist_ok=True)

    # Expand any zips. Folders we walk directly.
    for f in raw.iterdir():
        if f.suffix.lower() == ".zip":
            with zipfile.ZipFile(f) as z:
                z.extractall(expanded)
        elif f.suffix.lower() == ".jsonl":
            shutil.copy2(f, expanded / f.name)

    pairs_path = job_dir / "pairs.jsonl"
    n_sessions, n_pairs, n_turns = 0, 0, 0
    with pairs_path.open("w") as out:
        for p in expanded.rglob("*.jsonl"):
            n_sessions += 1
            n_turns += sum(1 for _ in p.open())
            for pair in _extract_pairs_for_file(p):
                out.write(json.dumps(pair) + "\n")
                n_pairs += 1
    return {"sessions": n_sessions, "turns": n_turns, "pairs": n_pairs}


# ---------------------------------------------------------------
# Stage 2 — cost estimate
# ---------------------------------------------------------------

def estimate_cost(counts: dict, billing: str) -> dict:
    """Project the inference cost of the classify run (the user's own
    Anthropic key at Batches list price — the retired platform-billed lane
    is gone; there is nothing platform-side to reconcile against).
    """
    pairs = counts["pairs"]
    # Conservative ceiling — matches bridge op's _EST_TOKENS_PER_REQUEST = 1500.
    tokens = pairs * 1500
    in_tokens = int(tokens * 0.85)
    out_tokens = int(tokens * 0.15)
    raw_cost = (in_tokens / 1_000_000) * 0.50 + (out_tokens / 1_000_000) * 2.50
    platform_markup = 1.00  # no markup — matches the live inference billing path
                            # (passthrough at provider list price; no _PLATFORM_MARKUP
                            # constant exists in the adapter).
    return {
        "tokens": tokens,
        # Inference cost to the user, either path. App-purchase fee ($1.99
        # one-time) lives in the app store, not here.
        "byo_cost_estimate_usd": round(raw_cost, 2),
        "platform_cost_estimate_usd": round(raw_cost * platform_markup, 2),
    }


# ---------------------------------------------------------------
# Stage 3a — classify phase (extract → classify → seed review state).
# Emits SSE events. Ends when review_state.json is ready for human review.
# ---------------------------------------------------------------

async def run_classify_phase(job_dir: Path, meta: dict) -> AsyncIterator[dict]:
    pairs_path = job_dir / "pairs.jsonl"
    pairs = [json.loads(l) for l in pairs_path.open()]
    n = len(pairs)
    classified_path = job_dir / "classified.jsonl"
    billing = meta.get("billing", "byo")
    yield {"phase": "classifying", "progress": 0.0,
           "counters": {"pairs": n, "billing": billing}}

    # Build the Anthropic batch requests once — same shape regardless of path.
    batch_requests = [
        {
            "custom_id": f"p{i:05d}",
            "params": {
                "model": HAIKU_MODEL, "max_tokens": 1024, "temperature": 0.1,
                "system": CLASSIFIER_SYSTEM,
                "messages": [{"role": "user", "content": _build_user_msg(p)}],
            },
        }
        for i, p in enumerate(pairs)
    ]

    if billing == "byo":
        api_key = await _resolve_byo_key(meta)
        async with aiohttp.ClientSession() as http:
            batch_id = await _submit_batch_byok(http, api_key, batch_requests)
            yield {"phase": "classifying", "progress": 0.05,
                   "counters": {"pairs": n, "batch_id": batch_id, "billing": billing}}
            async for status in _poll_batch_byok(http, api_key, batch_id):
                counts = status.get("request_counts", {})
                done = counts.get("succeeded", 0) + counts.get("errored", 0)
                yield {"phase": "classifying",
                       "progress": min(0.85, 0.05 + 0.80 * done / max(n, 1)),
                       "counters": counts}
                if status.get("processing_status") == "ended":
                    break
            results_text = await _fetch_results_byok(http, api_key, status["results_url"])
        await _log_batch_spend(results_text, meta.get("entity_id"))
        all_recs, not_classified = _persist_results(classified_path, pairs, results_text)
    else:
        # The platform-billed batch lane is RETIRED (compute-only billing — the bridge's inference.batches.classify op
        # and its +30% markup are deleted). Classification runs on the
        # user's own Anthropic key (billing=byo, the default).
        yield {"phase": "error", "done": True,
               "error": "platform-billed classification is retired — run this "
                        "job with your own Anthropic key (the app's default).",
               "counters": {"pairs": n, "billing": billing}}
        return

    # Seed review state: one entry per classifier-yes pair, status=pending.
    yield {"phase": "seeding_review", "progress": 0.9,
           "counters": {"yes_pairs": sum(
               1 for r in all_recs if (r.get("classifier") or {}).get("is_feedback") is True)}}

    pool = brain_service.get_pool()
    entity_id = meta["entity_id"]
    default_role = meta["role"]
    review_state = await _seed_review_state(
        all_recs, default_role=default_role, pool=pool, entity_id=entity_id,
    )
    (job_dir / "review_state.json").write_text(json.dumps(review_state, indent=2))
    # Pairs the batch did not classify have no review entry. The review
    # screen reads this file to say how many there were and why.
    (job_dir / CLASSIFY_SUMMARY).write_text(json.dumps(
        {"pairs": n, "classified": len(all_recs),
         "not_classified": sum(not_classified.values()),
         "not_classified_by_type": not_classified}, indent=2))

    yield {"phase": "ready_for_review", "progress": 1.0, "done": True,
           "counters": {
               "total_classified": len(all_recs),
               "not_classified": sum(not_classified.values()),
               **{f"not_classified_{t}": c for t, c in sorted(not_classified.items())},
               "yes_pending_review": sum(1 for v in review_state.values()
                                         if v["status"] == "pending"),
               # By status, as the review screen's "no" tally counts it, so an
               # unparseable reply (is_feedback None) is counted in both.
               "no_pairs": sum(1 for v in review_state.values()
                               if v["status"] == "skipped_no_feedback"),
           }}


# ---------------------------------------------------------------
# Stage 3b — commit phase (embed + insert reviewed-approved rules).
# Emits SSE events. Only acts on entries in review_state.json whose
# `status` is `approved` or `edited`.
# ---------------------------------------------------------------

async def run_commit_phase(job_dir: Path, meta: dict) -> AsyncIterator[dict]:
    rs_path = job_dir / "review_state.json"
    if not rs_path.exists():
        yield {"phase": "error", "error": "no_review_state", "done": True}
        return
    review_state = json.loads(rs_path.read_text())

    classified_path = job_dir / "classified.jsonl"
    by_id = {}
    for line in classified_path.open():
        if not line.strip():
            continue
        rec = json.loads(line)
        by_id[rec["pair_id"]] = rec

    to_commit = [
        (pid, st) for pid, st in review_state.items()
        if st["status"] in ("approved", "edited")
    ]
    yield {"phase": "embedding", "progress": 0.0,
           "counters": {"to_commit": len(to_commit)}}

    pool = brain_service.get_pool()
    entity_id = meta["entity_id"]
    inserted, escalated, skipped = 0, 0, 0
    committed_ids: list[int] = []   # for undo
    async with aiohttp.ClientSession() as http:
        for i, (pid, st) in enumerate(to_commit):
            rec = by_id.get(pid)
            if not rec:
                skipped += 1
                continue
            try:
                action, row_id = await _embed_and_insert_reviewed(
                    http, pool, entity_id, st, rec,
                )
            except Exception as e:
                logger.warning("commit failed for %s: %s", pid, e)
                action, row_id = "error", None
            if action == "inserted":
                inserted += 1
                if row_id is not None:
                    committed_ids.append(row_id)
            elif action == "escalated":
                escalated += 1
            else:
                skipped += 1
            if (i + 1) % 25 == 0 or i == len(to_commit) - 1:
                yield {"phase": "writing",
                       "progress": (i + 1) / max(len(to_commit), 1),
                       "counters": {"inserted": inserted,
                                    "escalated": escalated,
                                    "skipped": skipped}}

    role_counts = {}
    for pid, st in to_commit:
        role_counts[st.get("role", meta["role"])] = role_counts.get(
            st.get("role", meta["role"]), 0) + 1

    result = {
        "role": meta["role"],
        "role_counts": role_counts,
        "entity_id": entity_id,
        "inserted": inserted,
        "escalated": escalated,
        "skipped": skipped,
        "to_commit": len(to_commit),
        "committed_ids": committed_ids,
        "committed_at": _utc_iso_now(),
    }
    (job_dir / "result.json").write_text(json.dumps(result, indent=2))
    yield {"phase": "done", "progress": 1.0, "done": True, "counters": result}


def _utc_iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------
# Review state helpers — used by both worker and routes.
# ---------------------------------------------------------------

async def _seed_review_state(all_recs: list, default_role: str, pool, entity_id: int) -> dict:
    """Build per-pair review entries from classified records. Includes a
    dedup peek (would_escalate_id) for each yes-pair so the UI can warn
    that committing this rule will escalate an existing one rather than
    insert a new row.
    """
    # Cache existing rule words for this entity+role for fast dedup peek.
    rule_words_cache = await _existing_rule_words(pool, entity_id, default_role)
    state = {}
    for rec in all_recs:
        pid = rec["pair_id"]
        cls = rec.get("classifier") or {}
        is_fb = cls.get("is_feedback")
        rule = (cls.get("rule") or "").strip()
        entry = {
            "pair_id": pid,
            "session_id": rec.get("session_id"),
            "kind": rec.get("kind"),
            "classifier_is_feedback": is_fb,
            "classifier_type": cls.get("type"),
            "classifier_confidence": cls.get("confidence"),
            "rule_original": rule,
            "rule_final": rule,
            "role": default_role,
            "severity": "gentle",
            "status": "pending" if is_fb is True else "skipped_no_feedback",
            "would_escalate_id": None,
        }
        if is_fb is True and rule:
            entry["would_escalate_id"] = _dedup_peek(rule, rule_words_cache)
        state[pid] = entry
    return state


async def _existing_rule_words(pool, entity_id: int, role: str) -> list:
    rows = await pool.fetch(
        "SELECT id, correction FROM behavioral_corrections "
        "WHERE entity_id=$1 AND role=$2 AND active=true",
        entity_id, role,
    )
    return [(r["id"], _clean_words(r["correction"])) for r in rows]


def _dedup_peek(rule: str, cache: list):
    new_words = _clean_words(rule)
    if not new_words:
        return None
    for row_id, ew in cache:
        if not ew:
            continue
        overlap = len(new_words & ew) / max(len(new_words), len(ew))
        if overlap > 0.6:
            return row_id
    return None


async def _embed_and_insert_reviewed(http, pool, entity_id: int, st: dict, rec: dict):
    """Insert a reviewer-approved/edited rule. Returns (action, new_id).

    Mirrors _embed_and_insert but takes review-state fields (rule_final,
    role, severity) rather than the raw classifier output.
    """
    rule = (st.get("rule_final") or "").strip()
    if not rule:
        return ("skipped", None)
    role = st.get("role") or "coding"
    severity = st.get("severity") or "gentle"
    feedback_type = st.get("classifier_type") or "correction"
    confidence = float(st.get("classifier_confidence") or 0.5)
    trigger_context = (rec.get("rejected_preview") or "")[:500]
    source_message = (rec.get("correction_text") or "")[:1000]

    # Dedup at commit time too (defense in depth — peek result may be stale).
    rows = await pool.fetch(
        "SELECT id, correction, severity FROM behavioral_corrections "
        "WHERE entity_id=$1 AND role=$2 AND active=true",
        entity_id, role,
    )
    new_words = _clean_words(rule)
    for row in rows:
        ew = _clean_words(row["correction"])
        if not ew or not new_words:
            continue
        if len(new_words & ew) / max(len(new_words), len(ew)) > 0.6:
            await pool.execute(
                "UPDATE behavioral_corrections SET severity=$1, "
                "times_violated=times_violated+1, updated_at=NOW() WHERE id=$2",
                _escalate(row["severity"] or "gentle"), row["id"])
            return ("escalated", row["id"])

    embedding = await _embed(http, f"{rule} {trigger_context}")
    # Invariant 19 chokepoint. Single wrapper writes
    # the row + computes row_digest atomically so codebook_engine.load()
    # can validate at read time.
    import sys as _sys
    _sys.path.insert(0, "/app/brain")
    from mastercode.runtime.behavioral_correction_digest import apply_behavioral_correction
    result = await apply_behavioral_correction(
        pool,
        entity_id=entity_id,
        role=role,
        correction=rule[:500],
        trigger_context=trigger_context,
        source_message=source_message,
        feedback_type=feedback_type,
        confidence=round(confidence, 3),
        severity=severity,
        trigger_embedding=embedding,
        active=True,
    )
    return ("inserted", result["id"])


async def undo_commit(job_dir: Path) -> dict:
    """Soft-undo a recent commit by setting active=false on every row this
    job inserted. Allowed within UNDO_WINDOW_SECONDS of the commit timestamp.
    """
    UNDO_WINDOW_SECONDS = 600  # 10 min
    result_path = job_dir / "result.json"
    if not result_path.exists():
        return {"ok": False, "error": "no_result"}
    result = json.loads(result_path.read_text())
    committed_at = result.get("committed_at")
    committed_ids = result.get("committed_ids") or []
    if not committed_at:
        return {"ok": False, "error": "no_commit_timestamp"}
    from datetime import datetime, timezone
    try:
        t = datetime.fromisoformat(committed_at)
    except ValueError:
        return {"ok": False, "error": "bad_commit_timestamp"}
    elapsed = (datetime.now(timezone.utc) - t).total_seconds()
    if elapsed > UNDO_WINDOW_SECONDS:
        return {"ok": False, "error": "undo_window_expired",
                "elapsed_seconds": int(elapsed),
                "window_seconds": UNDO_WINDOW_SECONDS}
    if not committed_ids:
        return {"ok": True, "deactivated": 0}
    pool = brain_service.get_pool()
    n = await pool.fetchval(
        "WITH deactivated AS ("
        "  UPDATE behavioral_corrections SET active=false, updated_at=NOW() "
        "  WHERE id = ANY($1::int[]) AND active=true "
        "  RETURNING id) "
        "SELECT COUNT(*) FROM deactivated",
        committed_ids,
    )
    return {"ok": True, "deactivated": int(n or 0),
            "elapsed_seconds": int(elapsed)}


async def _resolve_byo_key(meta: dict) -> str:
    """BYOK path only. Order:
       1. byo_key explicitly passed in the job (user typed it into the app form)
       2. the user's canonical 'anthropic' Keys entry — the typed-credentials
          model: one Anthropic API key in the
          Keys app serves every consumer, this worker included
       3. ANTHROPIC_API_KEY in container env (provisioned BYOK containers carry it)
    Raises if none is present — billing=byo with no key is a misconfiguration.
    """
    key = (meta.get("byo_key") or "").strip()
    if key:
        return key
    try:
        from server import brain_service
        from server.vault.providers import resolve_credential
        pool = brain_service.get_pool()
        eid_s = os.environ.get("ENTITY_ID", "")
        if pool is not None and eid_s.isdigit():
            key = await resolve_credential(
                pool, int(eid_s), "anthropic", "api_key",
                actor="training-byok", env_fallback=False)
            if key:
                return key
    except Exception as exc:  # vault miss degrades to env, never breaks the job
        logger.warning("training byok vault read failed (env fallback): %s", exc)
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if key:
        return key
    raise RuntimeError(
        "BYOK selected but no Anthropic key found. Add your key to the Keys "
        "app (name it 'anthropic'), provide one in the app form, or set "
        "ANTHROPIC_API_KEY in the container env."
    )


def _build_user_msg(pair):
    return (f"Entity response (truncated):\n{pair['rejected']['text'][:400]}\n\n"
            f"User message:\n{pair['correction']['text'][:500]}")


async def _submit_batch_byok(http, api_key, batch_requests):
    """BYOK path: hit Anthropic Batches API directly with the user's key."""
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    async with http.post(f"{ANTHROPIC_API}/messages/batches",
                         headers=headers, json={"requests": batch_requests}) as r:
        d = await r.json()
        if r.status != 200:
            raise RuntimeError(f"batch submit failed {r.status}: {d}")
        return d["id"]


async def _poll_batch_byok(http, api_key, batch_id):
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    while True:
        async with http.get(f"{ANTHROPIC_API}/messages/batches/{batch_id}",
                            headers=headers) as r:
            d = await r.json()
        yield d
        if d.get("processing_status") == "ended":
            break
        await asyncio.sleep(15)


async def _log_batch_spend(results_text: str, entity_id) -> None:
    """The classifier batch's model calls, in the one cost ledger
    (inference_cost_log, source 'training_memory'), so the owner's activity
    feed and the Compute app show what the run spent: the usage every result
    line reports, summed into one row per batch. Priced at the model's list
    rate (the gateway's estimator); the Message Batches API bills less, which
    the row's metadata says. Never raises: a run is never failed by its
    record."""
    tin = tout = n = 0
    for line in (results_text or "").splitlines():
        try:
            rec = json.loads(line)
        except (TypeError, ValueError):
            continue
        usage = ((rec.get("result") or {}).get("message") or {}).get("usage") or {}
        if not usage:
            continue
        n += 1
        tin += (int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0)
                + int(usage.get("cache_creation_input_tokens") or 0))
        tout += int(usage.get("output_tokens") or 0)
    if not n:
        return
    try:
        from mastercode.provider_gateway import _log_inference
        await _log_inference("training_memory", HAIKU_MODEL, "anthropic", tin + tout,
                             prompt_tokens=tin, completion_tokens=tout,
                             entity_id=int(entity_id) if entity_id is not None else None,
                             metadata={"requests": n, "batch": True,
                                       "note": "Message Batches API; priced here at the list rate"})
    except Exception as e:  # noqa: BLE001
        logger.warning("training memory: batch spend not logged: %s", e)


async def _fetch_results_byok(http, api_key, url):
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    async with http.get(url, headers=headers) as r:
        return await r.text()


def _persist_results(path: Path, pairs: list, body: str) -> tuple[list, dict]:
    """Write the classifier rows for a finished batch to `path`.

    Returns (rows, not_classified). _seed_review_state marks the not-feedback
    rows skipped_no_feedback; the review screen counts those and lists them
    under show-all. not_classified counts the pairs that got no classifier
    row, by the batch's result type (errored, expired, canceled), with
    "missing" for a pair the results text never mentions. `body` is the
    batch's newline-delimited results text."""
    by_id = {f"p{i:05d}": p for i, p in enumerate(pairs)}
    out = []
    not_classified: dict = {}
    seen = set()
    with path.open("w") as fout:
        for line in body.splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            cid = rec.get("custom_id")
            pair = by_id.get(cid)
            if not pair or cid in seen:
                continue
            seen.add(cid)
            result = rec.get("result") or {}
            rtype = result.get("type")
            if rtype != "succeeded":
                key = rtype if isinstance(rtype, str) and rtype else "unknown"
                not_classified[key] = not_classified.get(key, 0) + 1
                continue
            blocks = result.get("message", {}).get("content") or []
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            parsed = _verdict_shape(_extract_json(text))
            row = {"pair_id": pair["pair_id"],
                   "kind": pair["correction"]["kind"],
                   "classifier": parsed,
                   "correction_text": pair["correction"]["text"],
                   "rejected_preview": pair["rejected"]["text"][:300]}
            fout.write(json.dumps(row) + "\n")
            out.append(row)
    missing = len(by_id) - len(seen)
    if missing > 0:
        not_classified["missing"] = missing
    return out, not_classified


def _verdict_shape(parsed):
    """Give a classifier reply the field types its readers assume.

    A reply that is valid JSON but not an object (`true`, `null`, a list)
    carries no verdict, so it is kept as a parse error. Inside an object, a
    rule or type that is not a string, or a confidence that is not a finite
    number, becomes None: seeding calls .strip() on the rule and the review
    screen calls toFixed() on the confidence, so either one would otherwise
    lose the paid-for batch or break the review list.
    """
    if not isinstance(parsed, dict):
        return {"_parse_error": "not_object"}
    for key in ("rule", "type"):
        if key in parsed and not isinstance(parsed[key], str):
            parsed[key] = None
    conf = parsed.get("confidence")
    if conf is not None and (isinstance(conf, bool)
                             or not isinstance(conf, (int, float))
                             or not math.isfinite(conf)):
        parsed["confidence"] = None
    return parsed


def _extract_json(text):
    if not text:
        return {"_parse_error": "empty"}
    s = text.strip()
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        pass
    start = s.rfind("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(s)):
            if s[i] == "{": depth += 1
            elif s[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start:i + 1])
                    except (json.JSONDecodeError, ValueError):
                        break
    return {"_parse_error": "no_json"}


# ---------------------------------------------------------------
# Embed + insert
# ---------------------------------------------------------------

def _clean_words(text):
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in text.lower())
    return set(cleaned.split())


def _escalate(current):
    return {"gentle": "firm", "firm": "hard", "hard": "hard"}.get(current, "firm")


async def _embed(http, text):
    """Embed with the entity's CONFIGURED model via the canonical path
    (memory_engine: BYO/bastion first, then bridge). Returns the pgvector
    literal, or None when embedding is unavailable OR the configured model's
    dim has no trigger_embedding column (correction_embeddings) — the row
    lands with NULL (degrade, don't mis-embed)."""
    try:
        from mastercode.correction_embeddings import trigger_column_for
        from mastercode.memory_engine import _get_memory_embedding
        vec_str = await _get_memory_embedding(f"search_document: {text[:1000]}")
        if not vec_str:
            return None
        col, dim = trigger_column_for(vec_str)
        if col is None:
            logger.info("training-memory: configured embedding model emits "
                        "%dd — no trigger_embedding column for that dim "
                        "(correction_embeddings) — storing NULL", dim)
            return None
        return vec_str
    except Exception as e:
        logger.info("training-memory embed unavailable (%s) — row will land "
                    "with NULL embedding", e)
        return None


async def _embed_and_insert(http, pool, entity_id, role, rec) -> str:
    cls = rec.get("classifier") or {}
    rule = (cls.get("rule") or "").strip()
    confidence = float(cls.get("confidence") or 0)
    if not rule or confidence < 0.4:
        return "skipped"

    feedback_type = cls.get("type") or "correction"
    trigger_context = (rec.get("rejected_preview") or "")[:500]
    source_message = (rec.get("correction_text") or "")[:1000]

    # Dedup against existing rows for this entity+role.
    rows = await pool.fetch(
        "SELECT id, correction, severity FROM behavioral_corrections "
        "WHERE entity_id=$1 AND role=$2 AND active=true",
        entity_id, role,
    )
    new_words = _clean_words(rule)
    for row in rows:
        ew = _clean_words(row["correction"])
        if not ew or not new_words:
            continue
        if len(new_words & ew) / max(len(new_words), len(ew)) > 0.6:
            await pool.execute(
                "UPDATE behavioral_corrections SET severity=$1, "
                "times_violated=times_violated+1, updated_at=NOW() WHERE id=$2",
                _escalate(row["severity"] or "gentle"), row["id"])
            return "escalated"

    embedding = await _embed(http, f"{rule} {trigger_context}")
    # Same Invariant 19 chokepoint as the other worker INSERT site.
    import sys as _sys
    _sys.path.insert(0, "/app/brain")
    from mastercode.runtime.behavioral_correction_digest import apply_behavioral_correction
    await apply_behavioral_correction(
        pool,
        entity_id=entity_id,
        role=role,
        correction=rule[:500],
        trigger_context=trigger_context,
        source_message=source_message,
        feedback_type=feedback_type,
        confidence=round(confidence, 3),
        severity="gentle",
        trigger_embedding=embedding,
        active=True,
    )
    return "inserted"


# ---------------------------------------------------------------
# Report support
# ---------------------------------------------------------------

async def sample_rules_for_role(role: str, n: int = 5) -> list[str]:
    pool = brain_service.get_pool()
    entity_id = brain_service._entity_id
    rows = await pool.fetch(
        "SELECT correction FROM behavioral_corrections "
        "WHERE entity_id=$1 AND role=$2 AND active=true "
        "ORDER BY created_at DESC LIMIT $3",
        entity_id, role, n)
    return [r["correction"] for r in rows]
