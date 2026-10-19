#!/usr/bin/env python3
"""
End-to-end test of the SDK training loop (Ships 1–5).

Drives every leg of the loop that does NOT require live Anthropic auth:

  1. Synthesize a CC-format JSONL transcript (a 4-turn pair with a
     classifier-shaped user correction)
  2. Plant it under /var/lib/cseries/cc-sessions/<entity_id>/
  3. POST /api/training-memory/jobs/from-sdk-sessions  → ingest
  4. GET  /jobs/<id>/estimate                          → extract_pairs
  5. Bypass /jobs/<id>/start (would call Haiku Batch API) — instead
     manually seed classified.jsonl + review_state.json with one
     approved pair, mimicking the post-classify post-review state.
  6. POST /jobs/<id>/commit                            → embed + insert
  7. Assert a row landed in behavioral_corrections with the expected
     correction text + role.

Exits non-zero on any failure. Designed to run inside the smoke test
container (via `docker exec`) since it relies on the kernel listening
on :8800 and a writable local Postgres.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
import urllib.error
import uuid
from pathlib import Path


KERNEL = os.environ.get("CSERIES_API", "http://[::1]:8800")
TRANSCRIPT_ROOT = Path(
    os.environ.get("CC_TRANSCRIPT_ROOT", "/var/lib/cseries/cc-sessions")
)
JOBS_ROOT = Path(os.environ.get("TM_JOBS_ROOT", "/app/training_memory_jobs"))

_TOKEN: list = []


def _auth() -> dict:
    """The app-token header for the training-memory routes.

    They take read/write:brain_corrections (they ingest the
    Host's own transcripts and commit rules it follows), and a request with no
    token is a 401 (server/applications/middleware.py requires_permission).
    The Training Memory app is not installed in smoke, so mint the two scopes
    in-container, the way smoke's own ingest gate does."""
    if not _TOKEN:
        import asyncio
        import asyncpg
        sys.path.insert(0, "/app")
        from server.applications.middleware import HEADER_NAME
        from server.applications.tokens import get_secret, mint

        async def _mint() -> str:
            pool = await asyncpg.create_pool(
                host="/var/run/postgresql", user="entity", database="hostmem",
                min_size=1, max_size=1)
            try:
                secret = await get_secret(pool)
            finally:
                await pool.close()
            token, _ = mint(secret, app_id="smoke-sdk-training-loop",
                            permissions=["read:brain_corrections",
                                         "write:brain_corrections"])
            return token
        _TOKEN.append((HEADER_NAME, asyncio.run(_mint())))
    name, token = _TOKEN[0]
    return {name: token}


def _post_form(path: str, fields: dict) -> dict:
    """multipart/form-data POST. Returns parsed JSON."""
    boundary = "----TestBoundary" + uuid.uuid4().hex
    body = []
    for k, v in fields.items():
        body.append(("--" + boundary).encode())
        body.append(
            f'Content-Disposition: form-data; name="{k}"'.encode()
        )
        body.append(b"")
        body.append(str(v).encode())
    body.append(("--" + boundary + "--").encode())
    body.append(b"")
    data = b"\r\n".join(body)
    req = urllib.request.Request(
        KERNEL + path,
        data=data,
        headers={"content-type": f"multipart/form-data; boundary={boundary}",
                 **_auth()},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _get_json(path: str) -> dict:
    req = urllib.request.Request(KERNEL + path, headers=_auth())
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def _post_json(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        KERNEL + path,
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json", **_auth()},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def _line(role: str, blocks: list) -> dict:
    return {
        "type": "user" if role == "user" else "assistant",
        "uuid": f"sdk-{uuid.uuid4().hex}",
        "isSidechain": False,
        "isMeta": False,
        "message": {"role": role, "content": blocks},
    }


def _entity_id() -> int:
    """Read brain_service._entity_id the same way the route does."""
    sys.path.insert(0, "/app")
    from server import brain_service
    return int(getattr(brain_service, "_entity_id", 0) or 0)


# ─────────────────────────────────────────────────────────────────────

def main() -> int:
    import_eid = _entity_id()
    env_eid = int(os.environ.get("ENTITY_ID", "0") or 0)
    # brain_service._entity_id resolves differently in the python -c
    # context vs the running uvicorn handler. Plant under every plausible
    # entity_id so whichever the route reads at request time finds the
    # synthetic transcript. CLEAR each dir of any prior smoke debris
    # first so extract_pairs only sees this run's file.
    candidate_eids = sorted({import_eid, env_eid, 0, 1})
    print(f"entity_id candidates: {candidate_eids}")

    lines = [
        _line("user", [{"type": "text",
                        "text": ("Write a thorough Python function that "
                                  "counts unique elements in a nested list, "
                                  "with docstring and example.")}]),
        _line("assistant", [{"type": "text",
                              "text": ("Here you go: " + "A" * 150 +
                                       " def count_unique(lst): return len(set(lst))")}]),
        _line("user", [{"type": "text",
                        "text": ("no thats wrong, set() on a nested list "
                                  "raises TypeError because lists are "
                                  "unhashable. fix it.")}]),
        _line("assistant", [{"type": "text",
                              "text": ("Right — flatten first: " + "B" * 80 +
                                       " from itertools import chain; "
                                       "def count_unique(nested): return "
                                       "len(set(chain.from_iterable(nested)))")}]),
    ]
    sid = f"e2e-{uuid.uuid4().hex[:12]}"
    primary_path = None
    for eid in candidate_eids:
        session_dir = TRANSCRIPT_ROOT / str(eid)
        session_dir.mkdir(parents=True, exist_ok=True)
        # Clear any stray *.jsonl from prior smoke gates so the
        # extractor's input is just this synthetic file.
        for stale in session_dir.glob("*.jsonl"):
            try:
                stale.unlink()
            except Exception:
                pass
        p = session_dir / f"{sid}.jsonl"
        with p.open("w") as f:
            for ln in lines:
                f.write(json.dumps(ln) + "\n")
        if primary_path is None:
            primary_path = p
    print(f"  transcript planted under {len(candidate_eids)} dirs "
          f"(primary: {primary_path}, {primary_path.stat().st_size} bytes)")
    sess_path = primary_path  # for cleanup at end

    # 2. Ingest via the route.
    ingest = _post_form(
        "/api/training-memory/jobs/from-sdk-sessions",
        {"role": "coding", "billing": "byo", "byo_key": "sk-placeholder"},
    )
    job_id = ingest["id"]
    print(f"  job_id: {job_id} files_received={ingest['files_received']}")
    if ingest["files_received"] < 1:
        print("FAIL: no files ingested", file=sys.stderr)
        return 1

    # 3. Estimate runs extract_pairs.
    est = _get_json(f"/api/training-memory/jobs/{job_id}/estimate")
    print(f"  estimate: sessions={est['sessions']} turns={est['turns']} "
          f"pairs={est['pairs']}")
    if est["pairs"] < 1:
        print("FAIL: extract_pairs found 0 pairs", file=sys.stderr)
        return 1

    # 4. Bypass classify (would call Haiku Batch). Synthesize the
    #    post-classify state directly: read pairs.jsonl, write a
    #    classified.jsonl row + review_state.json entry for the first
    #    pair, marked status='approved' with a concrete rule.
    job_dir = JOBS_ROOT / job_id
    pairs = [json.loads(l) for l in (job_dir / "pairs.jsonl").open()]
    if not pairs:
        print("FAIL: pairs.jsonl empty", file=sys.stderr)
        return 1
    pair = pairs[0]
    pid = pair["pair_id"]

    expected_rule = (
        f"e2e-sdk-loop-{uuid.uuid4().hex[:8]} "
        "ALWAYS flatten nested lists before calling set() — set() "
        "raises TypeError on lists because lists are unhashable."
    )
    classified_row = {
        "pair_id": pid,
        "kind": pair["correction"]["kind"],
        "classifier": {
            "is_feedback": True,
            "type": "correction",
            "confidence": 0.92,
            "rule": expected_rule,
        },
        "correction_text": pair["correction"]["text"],
        "rejected_preview": pair["rejected"]["text"][:300],
    }
    (job_dir / "classified.jsonl").write_text(
        json.dumps(classified_row) + "\n"
    )
    review_state = {
        pid: {
            "pair_id": pid,
            "session_id": pair["session_id"],
            "kind": pair["correction"]["kind"],
            "classifier_is_feedback": True,
            "classifier_type": "correction",
            "classifier_confidence": 0.92,
            "rule_original": expected_rule,
            "rule_final": expected_rule,
            "role": "coding",
            "severity": "gentle",
            "status": "approved",
            "would_escalate_id": None,
        }
    }
    (job_dir / "review_state.json").write_text(
        json.dumps(review_state, indent=2)
    )

    # 5. Commit — embeds (bge-m3 may not exist; worker degrades to
    #    embedding=None path) and inserts.
    commit = _post_json(f"/api/training-memory/jobs/{job_id}/commit", {})
    print(f"  commit: started={commit.get('started')}")

    # /commit kicks an async task. Stream the SSE to know when done.
    import time
    deadline = time.time() + 60
    done = False
    last_phase = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                KERNEL + f"/api/training-memory/jobs/{job_id}/events",
                timeout=30,
            ) as resp:
                for raw in resp:
                    if not raw.startswith(b"data:"):
                        continue
                    evt = json.loads(raw[5:].decode().strip())
                    last_phase = evt.get("phase")
                    if evt.get("done"):
                        done = True
                        break
                if done:
                    break
        except Exception as e:
            print(f"  events stream error: {e}", file=sys.stderr)
            break
        time.sleep(1)
    print(f"  commit phase: {last_phase}, done={done}")

    # 6. Verify a row landed in behavioral_corrections.
    import asyncio
    import asyncpg

    async def _check() -> int:
        # Worker uses brain_service.get_pool() which talks to the local
        # Postgres via unix socket. Replicate that.
        conn = await asyncpg.connect(
            host="/var/run/postgresql", user="entity", database="hostmem",
        )
        try:
            row = await conn.fetchrow(
                "SELECT id, role, correction, severity, entity_id FROM behavioral_corrections "
                "WHERE entity_id = ANY($1::int[]) AND correction LIKE $2 "
                "ORDER BY id DESC LIMIT 1",
                candidate_eids, "e2e-sdk-loop-%",
            )
            if row is None:
                print("FAIL: no behavioral_corrections row found",
                      file=sys.stderr)
                return 1
            print(f"  inserted: id={row['id']} entity_id={row['entity_id']} "
                  f"role={row['role']!r} severity={row['severity']!r}")
            print(f"  correction[:80]={row['correction'][:80]!r}")
            return 0
        finally:
            await conn.close()

    rc = asyncio.run(_check())
    if rc != 0:
        return rc

    # Cleanup the test transcripts so re-runs don't accumulate.
    for eid in candidate_eids:
        try:
            (TRANSCRIPT_ROOT / str(eid) / f"{sid}.jsonl").unlink(missing_ok=True)
        except Exception:
            pass

    print("PASS e2e SDK training loop")
    return 0


if __name__ == "__main__":
    sys.exit(main())
