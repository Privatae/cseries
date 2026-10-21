"""
Coding protocols — the Orchestrator's "Multi-Agent Coding" section.

A coding protocol is a JSON file of INSTRUCTIONS the host reads: which engines
to run — one after another, or several at once — with which model and effort,
a time budget if any, and what to do on pass (the user says pass → end) and on
fail (the user says fail, or it obviously fails → the next instruction). The
host drives it with the doors it already has (build — action='run' with
variants for several engines at once — / ladder.run / the merge path). There is deliberately NO runner here: "it's just
instruction — the host looks at it and understands what to do". This module
stores, validates, preflights (are the engines granted before anything runs?)
and measures (runs the host records against the protocol) — nothing else.

Storage is FILES, on purpose: one `<slug>.json` per protocol under
PROTOCOLS_ROOT, plus `<slug>.runs.jsonl` for the recorded runs. A protocol is
meant to be handed to someone else as-is — export IS the file, import IS the
file — with a name and an author for credit, and its stats riding along when
shared. The file never carries a credential: `auth` on an engine says what
KIND of grant it needs (vendor login / API key); the receiving host's preflight
answers whether that grant is present HERE.

Consumers: the Orchestrator's Multi-Agent Coding list (dashboard/base/js/
model-picker.js), the `coding_protocol` host tool (brain/mastercode/runtime/
tools.py) and coding_bench's summary. The old "Parallel Builders" tick list and
the Orchestrator's ladder editor are gone; both migrate into protocol files
once (ensure_migrated) so nothing the user had configured is lost.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from server import brain_service
from server.applications.middleware import requires_permission
from server.routes import review

log = logging.getLogger("privatae.pc.coding_protocols")

router = APIRouter(prefix="/api/coding-protocols", tags=["coding-protocols"])

# PVC-backed, beside the review sessions / ladder runs (review.SESSIONS_ROOT,
# ladder.LADDER_ROOT) — survives the pod, never inside the image. The state
# root is overridable so the gate can point it at a temp dir.
PROTOCOLS_ROOT = Path(os.environ.get("CSERIES_STATE_ROOT", "/var/lib/cseries")) / "coding-protocols"

FORMAT = "privatae.coding-protocol/v1"
_MIGRATED_MARKER = ".migrated"

# Bounds — sanity caps on a hand-editable file, never a throttle on the user.
_NAME_MAX = 80
_AUTHOR_MAX = 80
# description / notes / step notes are the person's own prose and are kept
# WHOLE (they were once cut at 500 / 2,000 / 1,000 characters without a
# word). The counts below stay: they are shape guards on
# a hand-editable file, and each refuses in words.
_STEPS_MAX = 32
_ENGINES_PER_STEP_MAX = 16
_RUNS_TAIL = 200            # runs kept readable per protocol (older lines stay on disk)
_RUN_SESSIONS_MAX = 64      # session ids one recorded run may carry

# Keys whose string values must never appear in a shareable instruction file,
# plus credential-shaped strings. A protocol names the KIND of grant an engine
# needs; the grant itself lives in Keys / the vendor login, never here.
_SECRET_KEYS = frozenset({
    "api_key", "apikey", "api-key", "token", "access_token", "refresh_token",
    "secret", "password", "authorization", "bearer", "credential", "credentials",
})
# Searched ANYWHERE in a string (a key pasted into `notes` leaks just the same
# as one in a field), bounded so an ordinary word never trips it.
_SECRET_SHAPES = re.compile(
    r"(?<![A-Za-z0-9])(sk-[A-Za-z0-9_-]{16,}|sk_[A-Za-z0-9_-]{16,}|xai-[A-Za-z0-9_-]{16,}|"
    r"gh[pous]_[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,})"
)


# ── helpers ──────────────────────────────────────────────────────────

def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return (s[:60].rstrip("-") or "protocol")


def _path(slug: str) -> Path:
    return PROTOCOLS_ROOT / f"{slug}.json"


def _runs_path(slug: str) -> Path:
    return PROTOCOLS_ROOT / f"{slug}.runs.jsonl"


def _valid_slug(slug: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,59}", slug or ""))


def _engine_ids() -> tuple:
    return tuple(review.HARNESS_APP_IDS)


def _find_secrets(node: Any, path: str = "") -> Optional[str]:
    """Path of the first credential-looking value in the document, or None."""
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else str(k)
            if str(k).strip().lower() in _SECRET_KEYS and isinstance(v, str) and v.strip():
                return p
            hit = _find_secrets(v, p)
            if hit:
                return hit
    elif isinstance(node, list):
        for i, v in enumerate(node):
            hit = _find_secrets(v, f"{path}[{i}]")
            if hit:
                return hit
    elif isinstance(node, str):
        if _SECRET_SHAPES.search(node):
            return path or "<root>"
    return None


def auth_kind_for(engine: str) -> Optional[str]:
    """'oauth' (the vendor's own login) or 'api_key' (a key pasted in Keys),
    derived from the harness catalog — never typed by hand. None when the
    catalog cannot say (import trouble); the file then simply omits it."""
    try:
        from server.routes.harnesses import _harness_app_to_provider
        from server.network.harness_keys import _fallback_providers
        entry = _harness_app_to_provider().get(engine)
        if not entry:
            return None
        return "api_key" if _fallback_providers(entry["id"]) else "oauth"
    except Exception:
        return None


# ── validation (pure) ────────────────────────────────────────────────

def validate_protocol(doc: Any, *, model_opts: Optional[dict] = None,
                      auth_fill=None) -> tuple[Optional[dict], Optional[str]]:
    """Shape-check a protocol document. Returns (clean_doc, None) or (None,
    error). Pure apart from the optional model/effort validators it borrows
    from the coding matrix (inference.validate_harness_model /
    validate_harness_effort — the SAME lists the Engines table renders, so a
    protocol can never name a model its engine's key does not serve).

    `model_opts` = {engine: [model ids]} for the model check (None = the
    cached-catalog fallback inside validate_harness_model, which fails open).
    `auth_fill(engine) -> 'oauth'|'api_key'|None` fills a missing `auth` on
    each engine entry (None = leave absent)."""
    if not isinstance(doc, dict):
        return None, "a protocol is a JSON object"
    fmt = str(doc.get("format") or FORMAT).strip()
    if fmt != FORMAT:
        return None, f"unknown format {fmt!r} — this host reads {FORMAT}"
    name = str(doc.get("name") or "").strip()
    if not name:
        return None, "name required"
    if len(name) > _NAME_MAX:
        return None, f"name must be at most {_NAME_MAX} characters"
    author = str(doc.get("author") or "").strip()
    if len(author) > _AUTHOR_MAX:
        return None, f"author must be at most {_AUTHOR_MAX} characters"
    description = str(doc.get("description") or "").strip()
    notes = str(doc.get("notes") or "").strip()

    hit = _find_secrets(doc)
    if hit:
        return None, (f"a protocol must not carry a credential (found one at {hit}) — "
                      "it names the KIND of grant an engine needs; the grant lives in "
                      "Keys or the vendor's own login")

    steps_in = doc.get("steps")
    if not isinstance(steps_in, list) or not steps_in:
        return None, "steps required — at least one instruction"
    if len(steps_in) > _STEPS_MAX:
        return None, f"at most {_STEPS_MAX} steps"

    engines_known = set(_engine_ids())
    try:
        from server.routes.inference import (validate_harness_model,
                                             validate_harness_effort,
                                             HARNESS_EFFORTS)
    except Exception:  # pragma: no cover - import trouble → shape-only check
        validate_harness_model = None   # type: ignore[assignment]
        validate_harness_effort = None  # type: ignore[assignment]
        HARNESS_EFFORTS = {}            # type: ignore[assignment]
    try:
        from server.routes.ladder import rung_wait_cap
        _CAP = rung_wait_cap()
    except Exception:  # pragma: no cover
        _CAP = 240

    steps: list[dict] = []
    ids: list[str] = []
    for i, s in enumerate(steps_in):
        if not isinstance(s, dict):
            return None, f"step {i}: must be an object"
        sid = str(s.get("id") or f"step-{i + 1}").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}", sid):
            return None, f"step {i}: id must be letters/digits/-/_ (max 40)"
        if sid in ids:
            return None, f"step {i}: duplicate id {sid!r}"
        ids.append(sid)

        # engines: a list of {engine, model?, effort?, auth?}; the single-engine
        # spelling {engine: ..., model: ...} on the step itself is accepted and
        # normalized into a one-entry list.
        raw_engines = s.get("engines")
        if raw_engines is None and s.get("engine"):
            raw_engines = [{"engine": s.get("engine"), "model": s.get("model"),
                            "effort": s.get("effort"), "auth": s.get("auth")}]
        if not isinstance(raw_engines, list) or not raw_engines:
            return None, f"step {sid}: engines required — one engine, or several to run at once"
        if len(raw_engines) > _ENGINES_PER_STEP_MAX:
            return None, f"step {sid}: at most {_ENGINES_PER_STEP_MAX} engines"
        engines: list[dict] = []
        seen_eng: set = set()
        for j, e in enumerate(raw_engines):
            if isinstance(e, str):
                e = {"engine": e}
            if not isinstance(e, dict):
                return None, f"step {sid}: engines[{j}] must be an object or an engine id"
            eng = str(e.get("engine") or e.get("id") or e.get("harness") or "").strip()
            if eng not in engines_known:
                return None, (f"step {sid}: unknown engine {eng!r} — known: "
                              f"{', '.join(sorted(engines_known))}")
            if eng in seen_eng:
                return None, f"step {sid}: engine {eng!r} listed twice (use samples for repeats)"
            seen_eng.add(eng)
            entry: dict = {"engine": eng}
            model = e.get("model")
            if model is not None and not isinstance(model, str):
                return None, f"step {sid}: {eng} model must be a string"
            model = (model or "").strip() or None
            if model:
                if validate_harness_model is not None:
                    err = validate_harness_model(eng, model, model_opts)
                    if err:
                        return None, f"step {sid}: {err}"
                entry["model"] = model
            effort = e.get("effort")
            if effort is not None and not isinstance(effort, str):
                return None, f"step {sid}: {eng} effort must be a string"
            effort = (effort or "").strip().lower() or None
            if effort:
                if validate_harness_effort is not None:
                    err = validate_harness_effort(eng, effort, model=model)
                    if err:
                        return None, f"step {sid}: {err}"
                elif HARNESS_EFFORTS and eng not in HARNESS_EFFORTS:
                    return None, f"step {sid}: {eng} has no reasoning-effort control"
                entry["effort"] = effort
            auth = e.get("auth")
            if auth is not None:
                auth = str(auth).strip().lower()
                if auth not in ("oauth", "api_key"):
                    return None, (f"step {sid}: {eng} auth must be 'oauth' or 'api_key' "
                                  "(the KIND of grant — never the grant itself)")
            elif auth_fill is not None:
                try:
                    auth = auth_fill(eng)
                except Exception:
                    auth = None
            if auth:
                entry["auth"] = auth
            engines.append(entry)

        step: dict = {"id": sid, "engines": engines}
        samples = s.get("samples")
        if samples is not None:
            if not isinstance(samples, dict):
                return None, f"step {sid}: samples must be {{engine: N}}"
            clean_s: dict = {}
            for k, n in samples.items():
                if str(k) not in seen_eng:
                    return None, f"step {sid}: samples names {k!r}, not an engine of this step"
                if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 16:
                    return None, f"step {sid}: samples must be integers in 1..16"
                if n > 1:
                    clean_s[str(k)] = n
            if clean_s:
                step["samples"] = clean_s
        tb = s.get("time_budget_minutes")
        if tb is not None:
            if (isinstance(tb, bool) or not isinstance(tb, int) or tb < 1
                    or (_CAP is not None and tb > _CAP)):
                return None, (f"step {sid}: time_budget_minutes, when given, must be a whole "
                              "number of minutes from 1"
                              + (f" to {_CAP} (the person's limit build.model_wait_max_minutes)"
                                 if _CAP is not None else "")
                              + ": the longest a model call may stay silent")
            step["time_budget_minutes"] = tb
        for key in ("on_pass", "on_fail"):
            v = s.get(key)
            if v is None:
                continue
            v = str(v).strip()
            if v:
                step[key] = v          # "end" or a step id — checked below
        snotes = str(s.get("notes") or "").strip()
        if snotes:
            step["notes"] = snotes
        steps.append(step)

    # Defaults + targets: on_pass → end; on_fail → the next step, else end.
    for i, step in enumerate(steps):
        if "on_pass" not in step:
            step["on_pass"] = "end"
        if "on_fail" not in step:
            step["on_fail"] = steps[i + 1]["id"] if i + 1 < len(steps) else "end"
        for key in ("on_pass", "on_fail"):
            t = step[key]
            if t != "end" and t not in ids:
                return None, f"step {step['id']}: {key} names unknown step {t!r}"
            if t == step["id"]:
                return None, f"step {step['id']}: {key} may not point at itself"

    clean: dict = {"format": FORMAT, "name": name}
    if author:
        clean["author"] = author
    if description:
        clean["description"] = description
    clean["steps"] = steps
    if notes:
        clean["notes"] = notes
    return clean, None


def summarize(doc: dict) -> dict:
    """The list row: what the file says, in one line's worth of fields."""
    steps = doc.get("steps") or []
    engines: list[str] = []
    at_once = False
    parts: list[str] = []
    for s in steps:
        es = [e.get("engine") for e in (s.get("engines") or []) if e.get("engine")]
        if len(es) > 1:
            at_once = True
            parts.append("[" + " + ".join(es) + "]")
        else:
            parts.append(es[0] if es else "?")
        for e in es:
            if e not in engines:
                engines.append(e)
    return {
        "slug": doc.get("slug"),
        "name": doc.get("name"),
        "author": doc.get("author"),
        "description": doc.get("description"),
        "steps": len(steps),
        "engines": engines,
        "at_once": at_once,
        "shape": " → ".join(parts),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }


# ── storage ──────────────────────────────────────────────────────────

def _read(slug: str) -> dict:
    p = _path(slug)
    if not _valid_slug(slug) or not p.exists():
        raise HTTPException(404, f"no coding protocol {slug!r}")
    try:
        doc = json.loads(p.read_text())
    except Exception as e:
        raise HTTPException(500, f"protocol file {slug}.json is unreadable: {e}")
    if not isinstance(doc, dict):
        raise HTTPException(500, f"protocol file {slug}.json is not an object")
    doc["slug"] = slug
    return doc


def _write(slug: str, doc: dict) -> None:
    PROTOCOLS_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = _path(slug).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, _path(slug))


def _list_slugs() -> list[str]:
    if not PROTOCOLS_ROOT.exists():
        return []
    return sorted(p.stem for p in PROTOCOLS_ROOT.glob("*.json")
                  if _valid_slug(p.stem))


def _read_runs(slug: str, limit: int = _RUNS_TAIL) -> list[dict]:
    p = _runs_path(slug)
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        lines = p.read_text().splitlines()
    except Exception:
        return []
    for ln in lines[-max(1, limit):]:
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


async def _cost_for_sessions(session_ids: list[str]) -> dict:
    """Billed truth for the recorded runs — summed off harness_run (the row
    every pool run writes), never estimated. {} when there is no pool or no
    session ids; a query failure reads as {} too (best-effort measure)."""
    ids = [s for s in session_ids if isinstance(s, str) and s.strip()][:500]
    if not ids:
        return {}
    pool = brain_service.get_pool()
    if pool is None:
        return {}
    try:
        row = await pool.fetchrow(
            """SELECT COUNT(*) AS harness_runs,
                      COUNT(*) FILTER (WHERE ok) AS harness_ok,
                      COALESCE(SUM(compute_s), 0) AS compute_s,
                      COALESCE(SUM(elapsed_seconds), 0) AS elapsed_s
                 FROM harness_run WHERE session_id = ANY($1::text[])""", ids)
        if not row:
            return {}
        return {"harness_runs": int(row["harness_runs"] or 0),
                "harness_ok": int(row["harness_ok"] or 0),
                "compute_s": round(float(row["compute_s"] or 0), 1),
                "elapsed_s": round(float(row["elapsed_s"] or 0), 1)}
    except Exception as e:
        log.info("coding_protocols: cost read failed: %s", e)
        return {}


async def stats_for(slug: str) -> dict:
    """runs / passes / fails / last result+time from the recorded runs, plus
    the pool's own numbers for the sessions those runs named."""
    runs = _read_runs(slug)
    out = {"runs": len(runs),
           "pass": sum(1 for r in runs if r.get("result") == "pass"),
           "fail": sum(1 for r in runs if r.get("result") == "fail"),
           "abandoned": sum(1 for r in runs if r.get("result") == "abandoned"),
           "last_result": runs[-1].get("result") if runs else None,
           "last_run_at": runs[-1].get("recorded_at") if runs else None}
    sids: list[str] = []
    for r in runs:
        for s in (r.get("session_ids") or []):
            if isinstance(s, str) and s not in sids:
                sids.append(s)
    out.update(await _cost_for_sessions(sids))
    return out


# ── migration from the older settings ─────────────────────────────────

def _star_rung(bp: dict) -> bool:
    """True when the legacy ladder carries the '*' (whole bench) rung — the ONE
    part of the migration whose output depends on what is connected right now,
    so the one part that can be silently lossy if it runs too early."""
    return any(isinstance(r, dict) and str(r.get("harness") or "") == "*"
               for r in (bp.get("coding_ladder") or []))


def migrate_legacy(bp: dict, connected: list[str]) -> list[dict]:
    """Pure: the protocol documents the OLD Orchestrator settings amount to.
    `coding_ladder` (the rung editor) → one chained protocol; a
    `coding_harnesses` list with 2+ entries (the Parallel Builders ticks) →
    one at-once protocol. Rung '*' (the full bench) becomes an at-once step
    across the engines connected at migration time — the file has to name
    engines, it cannot say "whatever is connected". Returns [] when there was
    nothing worth keeping."""
    known = set(_engine_ids())
    docs: list[dict] = []
    rungs = [r for r in (bp.get("coding_ladder") or []) if isinstance(r, dict)]
    plain = [r for r in rungs if str(r.get("harness") or "") in known]
    star = any(str(r.get("harness") or "") == "*" for r in rungs)
    # The '*' rung → one at-once step across the bench, MINUS the engines the
    # earlier rungs already climbed: a rung that failed does not get a second
    # shot inside the last resort (seen live: a migrated
    # ladder was [kimi] → [all seven, kimi again]). Below two engines there
    # is no bench to speak of (a one-engine "bench" is just a build), so it is
    # dropped. A ladder that is ONLY a '*' rung still produces a protocol —
    # keeping this outside `if plain:` is the fix for that shape.
    climbed = {str(r.get("harness")) for r in plain}
    bench = [h for h in connected if h in known and h not in climbed]
    # One engine left after the exclusion is still a real last rung (the
    # one builder that has not tried yet); one engine with NOTHING climbed
    # before it is just a build, not a ladder.
    bench_step = ({"id": "bench", "engines": [{"engine": h} for h in bench],
                   "notes": "last resort — the rest of the bench at once, one shot; "
                            "the user picks the best result"}
                  if star and bench and (len(bench) > 1 or climbed) else None)
    if plain or bench_step:
        steps: list[dict] = []
        for i, r in enumerate(plain):
            e: dict = {"engine": str(r["harness"])}
            if r.get("model"):
                e["model"] = str(r["model"])
            if r.get("effort"):
                e["effort"] = str(r["effort"])
            st: dict = {"id": f"rung-{i + 1}", "engines": [e]}
            n = r.get("samples")
            if isinstance(n, int) and 1 < n <= 16:
                st["samples"] = {e["engine"]: n}
            mm = r.get("max_minutes")
            if isinstance(mm, int) and mm > 0:
                st["time_budget_minutes"] = mm
            steps.append(st)
        if bench_step:
            steps.append(bench_step)
        docs.append({"format": FORMAT, "name": "Ladder (from your previous settings)",
                     "author": "migrated",
                     "description": ("Your old ladder: one engine at a time, in order — "
                                     "a fail goes to the next step"
                                     + ("; the last step runs the rest of the bench at once."
                                        if bench_step else ".")),
                     "steps": steps})
    sel = [h for h in (bp.get("coding_harnesses") or []) if isinstance(h, dict)
           and str(h.get("id") or "") in known]
    if len(sel) >= 2:
        engines = []
        for h in sel:
            e = {"engine": str(h["id"])}
            if h.get("model"):
                e["model"] = str(h["model"])
            if h.get("effort"):
                e["effort"] = str(h["effort"])
            engines.append(e)
        docs.append({"format": FORMAT, "name": "Parallel (from your previous settings)",
                     "author": "migrated",
                     "description": "Your old parallel builders: the same task on these "
                                    "engines at once — you keep the best result.",
                     "steps": [{"id": "all-at-once", "engines": engines}]})
    return docs


def legacy_default_engine(bp: dict) -> Optional[str]:
    """Pure: the engine the OLD settings made an unnamed build run on — the
    ladder's first real rung (the old resolver's step 2), else the first
    entry of the old tick list (its step 3). None when neither names a known
    engine. This is what the Default tick inherits so an upgrade never turns
    a working unnamed build into a refusal."""
    known = set(_engine_ids())
    for r in (bp.get("coding_ladder") or []):
        if isinstance(r, dict) and str(r.get("harness") or "") in known:
            return str(r["harness"])
    for h in (bp.get("coding_harnesses") or []):
        hid = (h.get("id") if isinstance(h, dict) else h) or ""
        if str(hid).strip() in known:
            return str(hid).strip()
    return None


def repair_migrated_bench(doc: dict) -> tuple[dict, bool]:
    """Pure: a MIGRATED ladder whose last-resort `bench` step repeats an
    engine an earlier rung already climbed gets that engine dropped from the
    bench step. Returns (doc, changed). Only files the migration wrote
    (author 'migrated') are touched; a hand-written protocol is the user's."""
    if not isinstance(doc, dict) or doc.get("author") != "migrated":
        return doc, False
    steps = doc.get("steps") or []
    climbed: set[str] = set()
    changed = False
    for st in steps:
        if not isinstance(st, dict):
            continue
        engines = [e for e in (st.get("engines") or []) if isinstance(e, dict)]
        if st.get("id") == "bench" and climbed:
            keep = [e for e in engines if str(e.get("engine")) not in climbed]
            if len(keep) != len(engines):
                st["engines"] = keep
                st["notes"] = ("last resort — the rest of the bench at once, one shot; "
                               "the user picks the best result")
                changed = True
        for e in engines:
            if e.get("engine"):
                climbed.add(str(e["engine"]))
    if changed:
        desc = str(doc.get("description") or "")
        if "the rest of the bench" not in desc:
            doc["description"] = ("Your old ladder: one engine at a time, in order — a fail "
                                  "goes to the next step; the last step runs the rest of the "
                                  "bench at once.")
    return doc, changed


def repair_migrated_files() -> list[str]:
    """Apply repair_migrated_bench to every protocol file on disk. Idempotent;
    runs at startup after the migration so an entity migrated under the old
    rule (e.g. a ladder [kimi] → [seven engines incl. kimi]) is corrected
    once, without re-running the migration. Returns the slugs rewritten."""
    fixed: list[str] = []
    for slug in _list_slugs():
        try:
            doc = json.loads(_path(slug).read_text())
        except Exception:
            continue
        doc, changed = repair_migrated_bench(doc)
        if changed:
            doc["updated_at"] = time.time()
            _write(slug, doc)
            fixed.append(slug)
    if fixed:
        log.info("coding_protocols: repaired migrated bench step in %s", fixed)
    return fixed


async def ensure_migrated(*, patient: bool = True) -> Optional[list[str]]:
    """One-time: if the protocols dir has never been populated, turn the old
    ladder / parallel-builders settings into protocol files, and — when no
    default engine is ticked yet — tick the engine the old settings made an
    unnamed build run on (legacy_default_engine). Runs from the kernel's
    startup (migrate_at_startup), never from a read path.

    Returns the slugs written ([] = done, nothing to write) once the
    migration has RUN TO COMPLETION and the marker is down; returns None when
    it could not run yet (no pool, a DB/catalog read failed, or `patient` and
    the ladder's '*' rung would expand against an empty bench) — nothing is
    written then, so the caller retries. The marker keeps it from re-running
    after the user deletes every file."""
    PROTOCOLS_ROOT.mkdir(parents=True, exist_ok=True)
    marker = PROTOCOLS_ROOT / _MIGRATED_MARKER
    if marker.exists():
        return []
    pool = brain_service.get_pool()
    if pool is None:
        return None
    written: list[str] = []
    try:
        row = await pool.fetchrow(
            "SELECT behavioral_params FROM entity_registry WHERE id = $1",
            brain_service._entity_id)
        raw = (row and row["behavioral_params"]) or {}
        if isinstance(raw, str):
            raw = json.loads(raw or "{}")
        bp: dict = raw if isinstance(raw, dict) else {}
        connected = await review._installed_harnesses()
        # A '*' rung expands to the engines connected AT MIGRATION TIME, and the
        # kernel restores harness credentials AFTER this task starts (main.py,
        # materialize_all_harness_creds). Seen on a rollout: the
        # marker landed 20-65ms BEFORE every credential file, so a Host's
        # bench rung was expanded against an empty bench and silently dropped.
        # Write nothing and let the caller retry; the last attempt gives up
        # patience, so an entity with genuinely no engines still migrates.
        if _star_rung(bp) and not connected:
            if patient:
                log.info("coding_protocols: the old ladder has a '*' rung and no engine "
                         "reads as connected yet — deferring the migration one cycle")
                return None
            # Out of patience: migrate rather than never migrate, but SAY that
            # the bench rung is being dropped. That rollout's loss was invisible
            # precisely because nothing logged it.
            log.warning("coding_protocols: migrating with NO engine connected — the old "
                        "ladder's '*' (whole bench) rung is being dropped. Re-add it in "
                        "Orchestrator → Multi-Agent Coding once an engine is connected.")
        existing = set(_list_slugs())
        for doc in migrate_legacy(bp, connected):
            # model_opts={} → the model check fails open: a migrated rung keeps
            # the model the user had saved even if the catalog is cold now.
            clean, err = validate_protocol(doc, model_opts={}, auth_fill=auth_kind_for)
            if err:
                log.info("coding_protocols: migration skipped %r: %s", doc.get("name"), err)
                continue
            slug = _slugify(clean["name"])
            if slug in existing:
                continue        # a file the user already has wins
            now = time.time()
            clean.update({"slug": slug, "created_at": now, "updated_at": now})
            _write(slug, clean)
            written.append(slug)
        # The Default tick inherits what the old settings made an unnamed
        # build run on — only when no tick is set yet.
        from server.network import harness_state
        if await harness_state.default_coding_harness() is None:
            first = legacy_default_engine(bp)
            if first and not await harness_state.set_default_coding_harness(first):
                raise RuntimeError("default-engine write failed")
    except Exception as e:
        log.warning("coding_protocols: migration did not complete (will retry): %s", e)
        return None
    try:
        marker.write_text(json.dumps({"at": time.time(), "written": written}))
    except Exception as e:  # pragma: no cover
        log.warning("coding_protocols: migration marker write failed: %s", e)
        return None
    return written


async def migrate_at_startup(attempts: int = 40, interval_s: float = 15.0) -> None:
    """Kernel-startup task: run the one-time migration once the pool is up.
    Retries (pool not ready / a DB blip / harness credentials not restored yet,
    which a '*' rung needs) for ~10 minutes, then runs one impatient attempt so
    an entity that genuinely has no engines still migrates. Never writes the
    marker on a failed attempt, so a later boot picks the work up. Never raises.
    Dispatched from main.py AFTER the credential materialize — see the comment
    there; the patience below is the backstop, not the primary fix."""
    total = max(1, attempts)
    for i in range(total):
        # Patient on every attempt but the last: a '*' rung waits for the
        # credential restore rather than migrating against an empty bench.
        try:
            res = await ensure_migrated(patient=i < total - 1)
        except Exception as e:  # pragma: no cover - ensure_migrated swallows
            log.warning("coding_protocols: startup migration error: %s", e)
            res = None
        if res is not None:
            if res:
                log.info("coding_protocols: migrated the old coding settings into %s", res)
            try:
                repair_migrated_files()
            except Exception as e:  # pragma: no cover
                log.warning("coding_protocols: migrated-file repair failed: %s", e)
            return
        try:
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            return


# ── service layer (the host tool calls these; the routes wrap them) ──

async def list_protocols() -> dict:
    rows = []
    for slug in _list_slugs():
        try:
            doc = _read(slug)
        except HTTPException:
            continue
        row = summarize(doc)
        row["stats"] = await stats_for(slug)
        rows.append(row)
    return {"protocols": rows, "format": FORMAT, "root": str(PROTOCOLS_ROOT)}


async def get_protocol(slug: str, *, with_stats: bool = False, runs: int = 0) -> dict:
    doc = _read(slug)
    if with_stats:
        doc["stats"] = await stats_for(slug)
    if runs:
        doc["runs"] = _read_runs(slug, limit=runs)
    return doc


async def _model_opts() -> dict:
    try:
        from server.routes.inference import harness_model_options
        return await harness_model_options()
    except Exception:
        return {}


async def save_protocol(doc: Any, *, slug: Optional[str] = None,
                        create: bool = False) -> dict:
    """Create (create=True: slug from the name, 409 if taken) or replace
    (slug given). Validation runs against the coding matrix's model lists."""
    clean, err = validate_protocol(doc, model_opts=await _model_opts(),
                                   auth_fill=auth_kind_for)
    if err:
        raise HTTPException(400, err)
    now = time.time()
    if create:
        wanted = str((doc or {}).get("slug") or "").strip() if isinstance(doc, dict) else ""
        slug = wanted if _valid_slug(wanted) else _slugify(clean["name"])
        if _path(slug).exists():
            raise HTTPException(409, f"a protocol named {slug!r} already exists — "
                                     "rename it, or save over it with PUT")
        clean.update({"slug": slug, "created_at": now, "updated_at": now})
    else:
        if not slug or not _valid_slug(slug):
            raise HTTPException(400, "slug required")
        prev = _read(slug) if _path(slug).exists() else {}
        clean.update({"slug": slug, "created_at": prev.get("created_at") or now,
                      "updated_at": now})
    _write(slug, clean)
    return clean


async def delete_protocol(slug: str) -> dict:
    if not _valid_slug(slug) or not _path(slug).exists():
        raise HTTPException(404, f"no coding protocol {slug!r}")
    _path(slug).unlink(missing_ok=True)
    _runs_path(slug).unlink(missing_ok=True)
    return {"ok": True, "deleted": slug}


async def preflight(slug: str) -> dict:
    """Are the engines this protocol names GRANTED on this host, right now?
    Reads the coding matrix (the one resolution the Engines table and the
    host tool share) — key state + the one action that fixes a row. Nothing
    wakes, nothing is charged."""
    doc = _read(slug)
    from server.routes.inference import _build_coding_matrix
    matrix = await _build_coding_matrix()
    rows = {r.get("id"): r for r in matrix.get("harnesses", [])}
    wanted: list[str] = []
    for s in doc.get("steps") or []:
        for e in s.get("engines") or []:
            if e.get("engine") and e["engine"] not in wanted:
                wanted.append(e["engine"])
    engines = []
    for h in wanted:
        r = rows.get(h) or {}
        state = ((r.get("key") or {}).get("state")) or "unknown"
        engines.append({
            "engine": h,
            "label": r.get("label") or h,
            "state": state,
            "auth": auth_kind_for(h),
            "provider": r.get("provider"),
            "ready": state == "connected",
            "action": r.get("action"),
        })
    missing = [e["engine"] for e in engines if not e["ready"]]
    return {"slug": slug, "ready": not missing, "engines": engines, "missing": missing}


async def record_run(slug: str, run: Any) -> dict:
    """Append one run record the host reports: result pass|fail|abandoned
    (the USER's say — pass = done, fail = the file's next instruction was
    taken), the steps it took, and the session/task ids of the harness runs
    (so stats can pull billed truth off harness_run)."""
    if not _valid_slug(slug) or not _path(slug).exists():
        raise HTTPException(404, f"no coding protocol {slug!r}")
    if not isinstance(run, dict):
        raise HTTPException(400, "run must be an object")
    result = str(run.get("result") or "").strip().lower()
    if result not in ("pass", "fail", "abandoned"):
        raise HTTPException(400, "result must be pass, fail or abandoned")
    rec: dict = {"recorded_at": time.time(), "result": result}
    steps = run.get("steps")
    if isinstance(steps, list):
        clean_steps = []
        for s in steps[:_STEPS_MAX]:
            if isinstance(s, dict):
                clean_steps.append({
                    str(k)[:40]: (v if isinstance(v, (int, float, bool, type(None)))
                                  else str(v)[:200])
                    for k, v in list(s.items())[:12]})
            else:
                clean_steps.append(str(s)[:120])
        rec["steps"] = clean_steps
    sids = run.get("session_ids")
    if isinstance(sids, list):
        rec["session_ids"] = [str(s)[:80] for s in sids if str(s or "").strip()][:_RUN_SESSIONS_MAX]
    tids = run.get("task_ids")
    if isinstance(tids, list):
        rec["task_ids"] = [int(t) for t in tids if isinstance(t, int)][:_RUN_SESSIONS_MAX]
    if run.get("project_id") is not None:
        try:
            rec["project_id"] = int(run["project_id"])
        except Exception:
            pass
    note = str(run.get("note") or "").strip()[:500]
    if note:
        rec["note"] = note
    PROTOCOLS_ROOT.mkdir(parents=True, exist_ok=True)
    with _runs_path(slug).open("a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"ok": True, "run": rec, "stats": await stats_for(slug)}


# ── routes ───────────────────────────────────────────────────────────

@router.get("", dependencies=[Depends(requires_permission("read:inference_catalog"))])
async def http_list() -> dict:
    return await list_protocols()


@router.post("", dependencies=[Depends(requires_permission("write:inference_routing"))])
async def http_create(doc: dict = Body(...)) -> dict:
    """Create from a document (the Import box pastes a whole file here)."""
    return {"ok": True, "protocol": await save_protocol(doc, create=True)}


@router.get("/{slug}", dependencies=[Depends(requires_permission("read:inference_catalog"))])
async def http_get(slug: str, with_stats: int = 0, runs: int = 0) -> dict:
    """The file. with_stats=1 attaches the measured numbers (that is the
    Export — credit + stats ride along); runs=N attaches the last N runs."""
    return await get_protocol(slug, with_stats=bool(with_stats),
                              runs=max(0, min(runs, _RUNS_TAIL)))


@router.put("/{slug}", dependencies=[Depends(requires_permission("write:inference_routing"))])
async def http_put(slug: str, doc: dict = Body(...)) -> dict:
    return {"ok": True, "protocol": await save_protocol(doc, slug=slug)}


@router.delete("/{slug}", dependencies=[Depends(requires_permission("write:inference_routing"))])
async def http_delete(slug: str) -> dict:
    return await delete_protocol(slug)


@router.get("/{slug}/preflight", dependencies=[Depends(requires_permission("read:inference_catalog"))])
async def http_preflight(slug: str) -> dict:
    return await preflight(slug)


class RunRecord(BaseModel):
    result: str
    steps: Optional[list] = None
    session_ids: Optional[list[str]] = None
    task_ids: Optional[list[int]] = None
    project_id: Optional[int] = None
    note: Optional[str] = None


@router.post("/{slug}/runs", dependencies=[Depends(requires_permission("write:inference_routing"))])
async def http_record(slug: str, body: RunRecord) -> dict:
    return await record_run(slug, body.model_dump())


@router.get("/{slug}/runs", dependencies=[Depends(requires_permission("read:inference_catalog"))])
async def http_runs(slug: str, limit: int = 20) -> dict:
    if not _valid_slug(slug) or not _path(slug).exists():
        raise HTTPException(404, f"no coding protocol {slug!r}")
    return {"slug": slug, "runs": _read_runs(slug, limit=max(1, min(limit, _RUNS_TAIL))),
            "stats": await stats_for(slug)}
