"""
Support tickets — the platform-help half of what was the Tickets pane (2026-08-29; the Support card in Circles since its slice 11).

The Circles "Reviews" tab became "Tickets": one queue, two kinds.

  * decisions — server/routes/tickets.py, unchanged. The host hands the USER a
    decision (sitrep + options).
  * support   — this module. The host (on the user's behalf) asks the PLATFORM
    for help: a bug, a question, a feature request, billing, access. Every
    ticket is categorized and identified by project.

The privacy shape is the bastion agent one. Nothing leaves the pod on `open`:
the host writes its four-part card (problem / context / recommendation /
pushback — the same shape the operator reads on their side) and this module
ASSEMBLES a context blob next to it — the C-Series version, installed apps, the
recent error lane, recent inference failures, and redacted excerpts of any
context files the host named. The user sees exactly that list in the Tickets
pane, toggles items off, and only then does `send` push the approved copy
over the bridge. Secret shapes are cut at assembly
(brain/mastercode/secret_shapes.redact), so a pasted key never reaches the
approval screen, let alone the platform.

Where `send` puts it depends on the category (2026-09-23):
  * bug  -> a PULL REQUEST on the bug repo, via `proposal.file` (the same
            door a capability proposal uses). A bug is never a row in an
            operator's database. The read-only validator passes it to main
            or closes it; the operator Host reads validated reports from main.
  * every other category -> platform.ticket op='open' (the operator's queue).

After sending, `refresh` mirrors the far side (status / resolution / PR link /
replies) into the local row on every list or get, so the pane and the host's
`tickets` tool both show `received -> reproducing -> pr_open -> approved ->
fixed`, or `needs_info` (a question the user answers with `reply`) or
`rejected`. The platform never reads this table; it only ever sees what
`send` chose to hand it.

Routes and the `tickets` host tool share the service functions below — one
path, one entity-scoping invariant (every read/write is `entity_id = eid`).
Contracts this leans on: brain_service.get_pool() -> Optional[Pool]
(brain_service.py:149), brain_service._entity_id: int (=1 on a personal
computer, brain_service.py:27), server.main.get_bridge_client() -> BridgeClient
| None (main.py:110) whose request_full(op, params) returns the {ok, data,
error, code} envelope (server/bridge/bridge_client.py:652),
ContextStore(pool, entity_id).get(path) -> Optional[str]
(brain/mastercode/context_store.py:42,70), update._read_local_tag() -> str
(routes/update.py:358), secret_shapes.redact(text) -> (text, findings)
(brain/mastercode/secret_shapes.py:148). Table: migrations/v122_support_tickets.sql.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from server import brain_service
from server.applications.middleware import requires_permission

log = logging.getLogger("privatae.pc.support")

router = APIRouter(prefix="/api/support", tags=["support"])

CATEGORIES = ("bug", "question", "feature", "billing", "access")
SEVERITIES = ("low", "normal", "high")
LOCAL_STATUSES = ("draft", "sent", "discarded", "closed")
# Far-side statuses that end the conversation; the local row closes on them.
TERMINAL_PLATFORM = ("fixed", "rejected")

# Caps on what one ticket may carry across the bridge. They are the BRIDGE's
# (privatae bridge/adapter.py op_platform_ticket: every card field and every
# shared item's content is cut at 8,000 characters, at most 12 shared items,
# a title at 300, a reply body at 8,000) — the platform lane's own bound on
# what any one Host sends into the operator's queue, so they stay. What
# changed: nothing is cut SILENTLY any more.
# A cut field carries a marker inside the cap saying how much was left out,
# the draft's answer names every cut, a file past the item cap is listed as
# not included (with why) instead of vanishing, and a long reply is sent in
# parts rather than cut.
EXCERPT_CAP = 8_000        # per named context file
TEXT_CAP = 8_000           # per prose field
TITLE_CAP = 300            # the bridge's title cap
SHARED_MAX = 12            # the bridge's shared_context item cap
MAX_FILES = 8
# A ticket reads further back than the Host's own default diagnose (24 h):
# a person files about what went wrong this week, not only today.
SUPPORT_FAILURE_WINDOW_H = 72
# The configuration areas a ticket carries. `failures` is not among them: it
# is already in the `errors` / `inference` items, whole.
DIAGNOSE_FAULT_AREAS = ("routing", "embedding", "protocols", "ledger", "memory")
# Harness runs a ticket names (support_open harness_runs / harness_session, or
# a compare session id written in the card): each is offered as one item, its
# outcome plus the END of its transcript, where a failure shows.
MAX_RUNS = 8
# How much more of a run's transcript is read than is shown: the read is cut
# from the front BEFORE redaction, so its first line is dropped (it may hold
# the back half of a secret whose recognisable front was cut off) and this
# much spare keeps the shown end whole after that.
TAIL_SLACK = 4_096
# The card fields a draft's support_update may rewrite.
UPDATE_FIELDS = ("title", "problem", "context", "recommendation", "pushback",
                 "repro_steps", "expected", "actual", "category", "severity")
_SESSION_RE = re.compile(r"\b[0-9a-f]{32}\b")


def _fit(text: str, cap: int, cuts: list | None = None, what: str = "") -> str:
    """`text` whole when it fits `cap`; else cut so that the cut text PLUS a
    marker saying how much was left out still fits `cap` (so the bridge's own
    slice never removes the marker). A cut is recorded in `cuts`."""
    text = text or ""
    if len(text) <= cap:
        return text
    def _marker(n: int) -> str:
        return (f"\n…[{n:,} more characters not sent — a support ticket carries at "
                f"most {cap:,} per field]")
    keep = max(0, cap - len(_marker(len(text))))
    out = text[:keep] + _marker(len(text) - keep)
    if cuts is not None:
        cuts.append({"field": what, "chars": len(text), "sent": keep})
    return out


def _reply_parts(body: str, cap: int) -> list[str]:
    """A reply longer than the bridge's per-reply cap, as numbered parts that
    each fit it — the whole reply arrives, in order, never cut."""
    if len(body) <= cap:
        return [body]
    head = "(part {i} of {n})\n"
    room = cap - len(head.format(i=999, n=999))
    chunks = [body[i:i + room] for i in range(0, len(body), room)]
    return [head.format(i=k + 1, n=len(chunks)) + c for k, c in enumerate(chunks)]


class SupportError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict:
    out = dict(row)
    for k in ("shared_context", "thread", "metadata"):
        v = out.get(k)
        if isinstance(v, str):
            try:
                out[k] = json.loads(v)
            except Exception:
                pass
    for k in ("opened_at", "sent_at", "updated_at", "closed_at"):
        v = out.get(k)
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
    if out.get("ticket_uuid") is not None:
        out["ticket_uuid"] = str(out["ticket_uuid"])
    return out


def _redact(text: str) -> str:
    """Cut hard secret shapes — the same rules the composer and send_message
    apply. If the redactor cannot load, REFUSE rather than pass text through:
    an unredacted blob on the approval screen is the failure this exists to
    prevent."""
    if not text:
        return ""
    try:
        from brain.mastercode import secret_shapes
    except Exception:
        try:
            from mastercode import secret_shapes  # type: ignore
        except Exception as e:
            raise SupportError(503, f"redaction unavailable ({e}); refusing to assemble shared context")
    cleaned, _findings = secret_shapes.redact(text)
    return cleaned


# Provider ACCOUNT and REQUEST identifiers. Not secrets, so secret_shapes
# leaves them, but they are not the person's to hand over either: a model
# provider's error body names the account it was billed to and the request
# (measured 2026-10-07 on a real ticket: an OpenRouter `user_id` and an
# Anthropic `request_id` rode in the model-call failures item). The platform
# needs the error text, never who the provider thinks the caller is.
_PROVIDER_ID_KEY_RE = re.compile(
    r"(?i)([\"']?\b(?:x-request-id|request[-_]?id|user[-_]?id|org(?:anization)?[-_]?id|"
    r"organization|account[-_]?id|generation[-_]?id)\b[\"']?\s*[:=]\s*[\"']?)"
    # An id carries a digit; a code name (`user_id = get_user`) does not.
    r"((?=[A-Za-z_\-.:]*\d)[A-Za-z0-9][A-Za-z0-9_\-.:]{5,})")
_PROVIDER_ID_TOKEN_RE = re.compile(
    r"\b(?:req_[A-Za-z0-9]{12,}|user_[A-Za-z0-9]{16,}|org-[A-Za-z0-9]{12,}|gen-[A-Za-z0-9-]{12,})\b")
PROVIDER_ID_MARK = "[provider id removed]"


def _scrub_provider_ids(text: str) -> str:
    if not text:
        return ""
    text = _PROVIDER_ID_KEY_RE.sub(lambda m: m.group(1) + PROVIDER_ID_MARK, text)
    return _PROVIDER_ID_TOKEN_RE.sub(PROVIDER_ID_MARK, text)


def _redact_shared(text: str) -> str:
    """What the diagnostic items (errors, model-call failures, container
    signal, harness runs) go through: secret shapes, then provider
    account/request identifiers. A named file or the project text is the
    Host's own choice of words and takes secret shapes only."""
    return _scrub_provider_ids(_redact(text))


_PEM_END_RE = re.compile(r"-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")


def _drop_cut_front(text: str) -> str:
    """`text` is the END of something longer, cut at an arbitrary character.
    Secret shapes are recognised by their front (`sk-ant-…`, `Bearer …`, a
    PEM's BEGIN line), so the back half of a secret the cut split would pass
    the redactor untouched. Drop what the cut may have split: the rest of the
    first line (in a line longer than TAIL_SLACK, the first two words, which
    covers `Bearer <token>`), and a private key's body whose BEGIN line was
    cut off."""
    if not text:
        return ""
    nl = text.find("\n", 0, TAIL_SLACK)
    if nl >= 0:
        text = text[nl + 1:]
    else:
        m = re.match(r"\S*\s+\S*\s+", text[:TAIL_SLACK])
        text = text[m.end():] if m else text[TAIL_SLACK:]
    end = _PEM_END_RE.search(text)
    if end and "-----BEGIN" not in text[:end.start()]:
        text = text[end.end():]
    return text


async def _run_items(pool, eid: int, *, run_ids: list[int], sessions: list[str],
                     cuts: list | None) -> list[dict]:
    """One item per harness run the ticket names: who ran, how it ended, and
    the end of its transcript. Read only with `right()`, so a 7 MB transcript
    is never pulled whole. A run of another entity's project never matches."""
    ids = sorted({int(r) for r in run_ids or []})
    sess = sorted({s for s in sessions or [] if s})
    if not ids and not sess:
        return []
    rows = await pool.fetch(
        """SELECT hr.id, hr.session_id, hr.project_id, hr.harness, hr.model, hr.ok,
                  hr.exit_code, hr.error, hr.elapsed_seconds, hr.diff_bytes, hr.status,
                  hr.metadata->>'stderr_tail' AS stderr_tail, hr.created_at,
                  length(hr.transcript) AS transcript_chars,
                  right(hr.transcript, $3) AS tail
             FROM harness_run hr
            WHERE (hr.id = ANY($1::bigint[]) OR hr.session_id = ANY($2::text[]))
              AND (hr.project_id IS NULL OR hr.project_id IN
                   (SELECT id FROM projects WHERE entity_id = $4))
            ORDER BY hr.id""",
        ids, sess, EXCERPT_CAP + TAIL_SLACK, eid)
    items: list[dict] = []
    found = set()
    for r in rows:
        found.add(int(r["id"]))
        outcome = "ok" if r["ok"] else "failed"
        head = (f"run {r['id']} · {r['harness']}/{r['model'] or '?'} · {outcome}"
                f" · exit {r['exit_code']} · {float(r['elapsed_seconds'] or 0):.0f} s"
                f" · {int(r['diff_bytes'] or 0):,} bytes of changes"
                f" · session {r['session_id'] or '-'} · project {r['project_id'] or '-'}"
                f" · {r['created_at'].isoformat() if r['created_at'] else ''}")
        # Every cut below comes AFTER redaction, so it never splits a secret
        # into a back half the redactor no longer recognises.
        head = _redact_shared(head)
        if r["error"]:
            head += f"\nerror: {_redact_shared(str(r['error']))[:1000]}"
        if r["stderr_tail"]:
            # Stored already cut from the front at 2,000 (compute.py).
            stderr = str(r["stderr_tail"])
            stderr = _drop_cut_front(stderr) if len(stderr) >= 2000 else stderr
            head += f"\nstderr (end): {_redact_shared(stderr)[-1000:]}"
        total = int(r["transcript_chars"] or 0)
        if not total:
            body = ("\n(no transcript kept — this run was archived)" if r["status"] == "archived"
                    else "\n(no transcript)")
        else:
            # Redact first, then cut from the FRONT: the end of a run is where
            # it failed, and the bridge's own cap would otherwise take the end.
            # The read itself was cut from the front, so its split first line
            # goes before redaction.
            raw = r["tail"] or ""
            tail = _redact_shared(_drop_cut_front(raw) if len(raw) < total else raw)
            label = f"\n--- transcript: the last {{n:,}} of {total:,} characters ---\n"
            room = max(0, EXCERPT_CAP - len(head) - len(label.format(n=EXCERPT_CAP)))
            kept = tail[-room:] if room else ""
            body = label.format(n=len(kept)) + kept
            if len(kept) < total and cuts is not None:
                cuts.append({"field": f"run:{r['id']}", "chars": total, "sent": len(kept)})
        items.append({
            "key": f"run:{r['id']}", "label": f"Harness run {r['id']} ({r['harness']}, {outcome})",
            "kind": "run", "content": head + body, "included": True,
        })
    for rid in ids:
        if rid not in found:
            items.append({"key": f"run:{rid}", "label": f"Harness run {rid} (not found)",
                          "kind": "run", "content": "", "included": False})
    return items


def _named_sessions(*texts: str) -> list[str]:
    """Compare-session ids written in the card (32 lowercase hex)."""
    out: list[str] = []
    for t in texts:
        for s in _SESSION_RE.findall(t or ""):
            if s not in out:
                out.append(s)
    return out


# ── Context assembly ───────────────────────────────────────────────────────

async def _assemble_context(pool, eid: int, *, project_id: int | None,
                            files: list[str], include_errors: bool,
                            include_inference: bool, cuts: list | None = None,
                            run_ids: list[int] | None = None,
                            sessions: list[str] | None = None) -> list[dict]:
    """Build the list the user approves. Every item: {key, label, kind,
    content, included}. `included` defaults True; the pane flips it. A cut
    is marked in the item's content and recorded in `cuts`. Diagnostic
    items go through _redact_shared (secret shapes + provider ids)."""
    items: list[dict] = []

    # 1. Versions — the platform's first question, always.
    tag = ""
    try:
        from server.routes.update import _read_local_tag
        tag = _read_local_tag()
    except Exception:
        pass
    apps: list[str] = []
    try:
        rows = await pool.fetch(
            "SELECT id, version, provenance, status FROM installed_applications ORDER BY id"
        )
        apps = [
            f"{r['id']} {r['version']} ({r['provenance']}"
            f"{'' if r['status'] == 'active' else ', ' + r['status']})"
            for r in rows
        ]
    except Exception as e:
        log.debug("installed_applications read failed: %s", e)
    items.append({
        "key": "versions", "label": "Privatae Host version and installed apps",
        "kind": "versions",
        "content": f"cseries {tag or 'unknown'}\n" + "\n".join(apps),
        "included": True,
    })

    # 2. The project, when the ticket names one.
    if project_id is not None:
        try:
            p = await pool.fetchrow(
                "SELECT id, name, description, status FROM projects WHERE id = $1 AND entity_id = $2",
                int(project_id), eid,
            )
        except Exception:
            p = None
        if p:
            items.append({
                "key": "project", "label": f"Project: {p['name']}", "kind": "project",
                "content": _fit(_redact(f"#{p['id']} {p['name']} [{p['status']}]\n{p['description'] or ''}"),
                                TEXT_CAP, cuts, "project"),
                "included": True,
            })

    # 3–4. What the Host's `diagnose` shows (diagnose is where a support
    #    ticket's evidence comes from). The SAME reader the
    #    tool uses (diagnostics.collect_failures): failures grouped by what
    #    they say, counted, each in the provider's or tool's own words, already
    #    screened — so the operator reads what the Host read, not a second,
    #    differently cut copy. `errors` = failed tool calls and tasks,
    #    `inference` = model calls a provider refused (the keys the approval
    #    pane and include_errors / include_inference have always used), and
    #    `diagnose` = the measured configuration faults.
    if include_errors or include_inference:
        failures: dict = {}
        try:
            from mastercode.runtime.diagnostics import collect_failures
            failures = await collect_failures(pool, eid, since_hours=SUPPORT_FAILURE_WINDOW_H)
        except Exception as e:
            log.debug("diagnose failures read failed: %s", e)
        try:
            from mastercode.runtime.diagnostics import render_failures_text
        except Exception as e:  # pragma: no cover - same module as above
            log.debug("diagnostics renderer unavailable: %s", e)
            render_failures_text = None
        if render_failures_text is not None and failures:
            for key, sources, label, on in (
                    ("errors", ("tool", "task"), "Failed tool calls and tasks (from diagnose)", include_errors),
                    ("inference", ("provider",), "Model calls a provider refused (from diagnose)", include_inference)):
                text = render_failures_text(failures, sources) if on else ""
                if text:
                    n = sum(len(failures.get(s) or []) for s in sources)
                    items.append({
                        "key": key, "label": f"{label} — {n} kind{'s' if n != 1 else ''}",
                        "kind": key, "content": _fit(_redact_shared(text), TEXT_CAP, cuts, key),
                        "included": True,
                    })
        try:
            from mastercode.runtime.diagnostics import collect_faults
            faults = await collect_faults(pool, eid, areas=[a for a in DIAGNOSE_FAULT_AREAS])
        except Exception as e:
            log.debug("diagnose faults read failed: %s", e)
            faults = []
        if faults:
            text = "\n".join(f"- [{f['severity']}] {f['area']}: {f['title']}"
                             + (f" — {f['detail']}" if f.get("detail") else "") for f in faults)
            items.append({
                "key": "diagnose", "label": f"Machine check from diagnose — {len(faults)} fault{'s' if len(faults) != 1 else ''}",
                "kind": "diagnose", "content": _fit(_redact_shared(text), TEXT_CAP, cuts, "diagnose"),
                "included": True,
            })

    # 5. Raw container signal — the ONE item on this list that reads no
    #    database. Every other item here is a pool query, so a host whose
    #    database is the thing that is broken could previously only file a
    #    ticket describing the symptom in prose. This attaches what is actually
    #    running: each process, what it is, what it costs (private/PSS, not the
    #    RSS that counts shared pages once per process and over-reports), how
    #    long it has been idle, and any rule hits. No file contents, no memory,
    #    no user text — process names and numbers.
    #
    #    REDACTED like every other item. The report embeds process command
    #    lines, and argv is a classic credential lane — `psql
    #    postgresql://u:p@h/db`, a curl with a bearer header, a `bash -c`
    #    one-liner all land in /proc verbatim. Assembly is the ONLY redaction
    #    point (send_service ships stored content as-is), so an item that
    #    skipped it would put a live secret on the approval screen.
    report = ""
    try:
        from server.process_awareness import support_report
        report = support_report()
    except Exception as e:
        log.debug("container signal unavailable: %s", e)
    # _redact deliberately OUTSIDE that try: it raises SupportError(503) when
    # the redactor cannot load, and that must fail the whole assembly exactly
    # as it does for every other item. Swallowing it here would make this the
    # one item that silently disappears instead of refusing.
    if report:
        items.append({
            "key": "container", "label": "Container signal (no database read)",
            "kind": "container", "content": _fit(_scrub_provider_ids(_redact(report)), TEXT_CAP, cuts, "container"),
            "included": True,
        })

    # 6. The harness runs the ticket is about — before the files, since a
    #    ticket that names a run is about what that run did. Runs past the
    #    item cap are LISTED, not included, with why.
    runs = await _run_items(pool, eid, run_ids=list(run_ids or []),
                            sessions=list(sessions or []), cuts=cuts)
    for n, it in enumerate(runs):
        full = sum(1 for i in items if i.get("included")) >= SHARED_MAX
        if it["included"] and (n >= MAX_RUNS or full):
            # Offered but not ticked: the content stays, so the person can
            # untick something else and tick this instead.
            why = (f"at most {MAX_RUNS} runs are offered ticked" if n >= MAX_RUNS else
                   f"a support ticket carries at most {SHARED_MAX} shared items")
            it = {**it, "included": False,
                  "label": it["label"] + f" (not ticked — {why}; untick another to make room)"}
            if cuts is not None:
                cuts.append({"field": it["key"], "not_included": True})
        items.append(it)

    # 7. Named context files — excerpts, through ContextStore (backend-aware).
    #    The bridge carries at most SHARED_MAX items in all, so the files get
    #    the slots the diagnostics above left (never more than MAX_FILES). A
    #    file past that is LISTED, not included, with why — never dropped.
    named = [str(p).strip() for p in list(files or []) if str(p).strip()]
    slots = max(0, min(MAX_FILES, SHARED_MAX - sum(1 for i in items if i.get("included"))))
    for path in named[slots:]:
        items.append({"key": f"file:{path}",
                      "label": (f"File: {path} (not included — a support ticket carries at "
                                f"most {SHARED_MAX} shared items; untick another to make room, "
                                f"or quote what matters in the problem)"),
                      "kind": "file", "content": "", "included": False})
        if cuts is not None:
            cuts.append({"field": f"file:{path}", "not_included": True})
    for path in named[:slots]:
        content = None
        try:
            from brain.mastercode.context_store import ContextStore
            content = await ContextStore(pool, eid).get(path)
        except Exception as e:
            log.debug("context read %s failed: %s", path, e)
        if content is None:
            items.append({"key": f"file:{path}", "label": f"File: {path} (not found)",
                          "kind": "file", "content": "", "included": False})
            continue
        items.append({
            "key": f"file:{path}", "label": f"File: {path}",
            "kind": "file", "content": _fit(_redact(content), EXCERPT_CAP, cuts, f"file:{path}"),
            "included": True,
        })
    return items


# ── Service ────────────────────────────────────────────────────────────────

async def open_service(pool, eid: int, *, category: str, title: str,
                       problem: str, context: str = "", recommendation: str = "",
                       pushback: str = "", repro_steps: str = "", expected: str = "",
                       actual: str = "", severity: str = "normal",
                       project_id: int | None = None, files: list[str] | None = None,
                       include_errors: bool = True, include_inference: bool = True,
                       metadata: dict | None = None, harness_runs: list[int] | None = None,
                       harness_session: str = "") -> dict:
    """Create a DRAFT. Nothing leaves the pod here.

    `harness_runs` / `harness_session` (or a compare-session id written in
    the card) offer those runs' outcomes and transcript ends as evidence. The
    assembly inputs are kept in metadata.assembly so support_update can
    assemble the same context again."""
    if pool is None:
        raise SupportError(503, "brain pool down")
    if category not in CATEGORIES:
        raise SupportError(400, f"category must be one of {list(CATEGORIES)}")
    title = (title or "").strip()
    problem = (problem or "").strip()
    if not title or not problem:
        raise SupportError(400, "title and problem are required")
    if severity not in SEVERITIES:
        severity = "normal"

    project_name = ""
    if project_id is not None:
        try:
            project_name = await pool.fetchval(
                "SELECT name FROM projects WHERE id = $1 AND entity_id = $2", int(project_id), eid) or ""
        except Exception:
            project_name = ""
        if not project_name:
            raise SupportError(404, f"project {project_id} not found")

    assembly = {"files": [str(f) for f in (files or [])],
                "harness_runs": [int(r) for r in (harness_runs or [])],
                "harness_session": (harness_session or "").strip(),
                "include_errors": bool(include_errors),
                "include_inference": bool(include_inference)}
    cuts: list[dict] = []
    shared = await _assemble_context(
        pool, eid, project_id=project_id, files=assembly["files"],
        include_errors=include_errors, include_inference=include_inference, cuts=cuts,
        run_ids=assembly["harness_runs"],
        sessions=_assembly_sessions(assembly, title, problem, context, repro_steps, actual),
    )
    # `unticked` starts empty: an item the assembly holds off (over the cap,
    # not found) is not one the person turned off.
    metadata = {**(metadata or {}), "assembly": assembly, "unticked": []}
    card = {k: _fit(_redact(v), TEXT_CAP, cuts, k) for k, v in (
        ("problem", problem), ("context", context), ("recommendation", recommendation),
        ("pushback", pushback), ("repro_steps", repro_steps), ("expected", expected),
        ("actual", actual))}
    title_sent = _fit(_redact(title), TITLE_CAP, cuts, "title")
    row = await pool.fetchrow(
        """INSERT INTO support_tickets
              (entity_id, category, project_id, project_name, title, severity,
               problem, context, recommendation, pushback, repro_steps, expected, actual,
               shared_context, thread, metadata)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                   $14::jsonb, '[]'::jsonb, $15::jsonb)
           RETURNING id, ticket_uuid, opened_at""",
        eid, category, project_id, project_name, title_sent, severity,
        card["problem"], card["context"], card["recommendation"], card["pushback"],
        card["repro_steps"], card["expected"], card["actual"],
        json.dumps(shared), json.dumps(metadata),
    )
    log.info("support ticket drafted id=%d category=%s project=%s", row["id"], category, project_id)
    out = {
        "ticket_id": row["id"], "ticket_uuid": str(row["ticket_uuid"]), "status": "draft",
        "shared_items": len(shared), "view": draft_view(row["id"]),
        "note": ("draft — the user approves what is shared on the Support card in Circles before "
                 "anything leaves this pod; nothing has been sent. "
                 f"open_app(app_id='circles', view='{draft_view(row['id'])}') shows them this "
                 "draft. To change it, op='support_update' with this ticket_id — never open "
                 "a second ticket for the same problem"),
    }
    if cuts:
        # Said, never silent: the platform takes at most TEXT_CAP characters
        # per field and SHARED_MAX items, and each cut is marked in place.
        out["cut"] = cuts
        out["note"] += (f". {len(cuts)} part(s) are over what a support ticket carries "
                        f"(at most {TEXT_CAP:,} characters a field, {SHARED_MAX} shared "
                        "items) and are marked where they were cut — tell the user, "
                        "and quote what matters most in the problem")
    return out


def draft_view(ticket_id: int) -> str:
    """The Circles view that opens the Support card on this support ticket
    (circles-web/src/lib/cards.ts cardsTarget)."""
    return f"support:{int(ticket_id)}"


def _person_unticked(md: dict, items: list) -> set[str]:
    """The item keys the person unticked in the pane (metadata.unticked,
    written by set_shared_service). Kept while an item is off the list, so an
    item an update drops and a later update brings back returns unticked
    rather than ticked. A draft from before the record has only its item list:
    the items it holds off with content are its unticks."""
    if isinstance(md.get("unticked"), list):
        return {str(k) for k in md["unticked"]}
    return {str(i.get("key")) for i in (items or [])
            if not i.get("included") and i.get("content")}


def _assembly_sessions(assembly: dict, *card_text: str) -> list[str]:
    out = [assembly["harness_session"]] if assembly.get("harness_session") else []
    if assembly.get("harness_session_set"):
        # support_update named the session outright: that beats whatever the
        # card text names, and an empty one means no session's runs at all.
        return out
    return out + [s for s in _named_sessions(*card_text) if s not in out]


async def update_service(pool, eid: int, ticket_id: int, *, title: str | None = None,
                         problem: str | None = None, context: str | None = None,
                         recommendation: str | None = None, pushback: str | None = None,
                         repro_steps: str | None = None, expected: str | None = None,
                         actual: str | None = None, category: str | None = None,
                         severity: str | None = None, files: list[str] | None = None,
                         harness_runs: list[int] | None = None,
                         harness_session: str | None = None) -> dict:
    """Rewrite a DRAFT in place: the card fields passed replace the old ones
    (the rest stay), and the shared context is assembled again from the same
    inputs (or the new files / runs). An item the person had unticked stays
    unticked. Anything but a draft is refused: once sent, the platform holds
    its copy and the way to add to it is support_reply.

    Origin (2026-10-07): a draft could not be amended, so one report
    took three drafts, each opened beside the last."""
    if pool is None:
        raise SupportError(503, "brain pool down")
    row = await pool.fetchrow(
        "SELECT * FROM support_tickets WHERE id = $1 AND entity_id = $2", ticket_id, eid)
    if not row:
        raise SupportError(404, "ticket not found")
    if row["status"] != "draft":
        raise SupportError(409, f"ticket is {row['status']!r}, not a draft — only a draft can be "
                                "changed; a sent ticket takes support_reply")
    given = {k: v for k, v in (
        ("title", title), ("problem", problem), ("context", context),
        ("recommendation", recommendation), ("pushback", pushback),
        ("repro_steps", repro_steps), ("expected", expected), ("actual", actual),
        ("category", category), ("severity", severity)) if v is not None}
    if not given and files is None and harness_runs is None and harness_session is None:
        raise SupportError(400, "nothing to change: pass the card fields, files or runs to rewrite")
    if "category" in given and given["category"] not in CATEGORIES:
        raise SupportError(400, f"category must be one of {list(CATEGORIES)}")
    if "severity" in given and given["severity"] not in SEVERITIES:
        raise SupportError(400, f"severity must be one of {list(SEVERITIES)}")
    for req in ("title", "problem"):
        if req in given and not given[req].strip():
            raise SupportError(400, f"{req} cannot be empty")

    md = row["metadata"]
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:
            md = {}
    md = dict(md or {})
    old_items = row["shared_context"]
    if isinstance(old_items, str):
        old_items = json.loads(old_items)
    old_items = old_items or []
    # A draft from before metadata.assembly existed: its files are its
    # file:<path> items; errors and model-call failures were offered.
    assembly = dict(md.get("assembly") or {
        "files": [i["key"][5:] for i in old_items if str(i.get("key", "")).startswith("file:")],
        "harness_runs": [], "harness_session": "",
        "include_errors": True, "include_inference": True})
    if files is not None:
        assembly["files"] = [str(f) for f in files]
    if harness_runs is not None:
        assembly["harness_runs"] = [int(r) for r in harness_runs]
    if harness_session is not None:
        assembly["harness_session"] = harness_session.strip()
        # Kept, so a later update that passes only card text does not bring
        # back a session this one dropped.
        assembly["harness_session_set"] = True

    vals = {k: (given[k].strip() if k in ("title", "problem") else given[k]) if k in given
            else row[k] for k in UPDATE_FIELDS}
    cuts: list[dict] = []
    shared = await _assemble_context(
        pool, eid, project_id=row["project_id"], files=assembly.get("files") or [],
        include_errors=bool(assembly.get("include_errors", True)),
        include_inference=bool(assembly.get("include_inference", True)), cuts=cuts,
        run_ids=assembly.get("harness_runs") or [],
        sessions=_assembly_sessions(assembly, vals["title"], vals["problem"], vals["context"],
                                    vals["repro_steps"], vals["actual"]),
    )
    # Only what was passed is re-redacted and re-fitted; the stored fields
    # already were when they were written.
    for k in ("problem", "context", "recommendation", "pushback", "repro_steps", "expected", "actual"):
        if k in given:
            vals[k] = _fit(_redact(given[k]), TEXT_CAP, cuts, k)
    if "title" in given:
        vals["title"] = _fit(_redact(vals["title"]), TITLE_CAP, cuts, "title")
    # The person may untick an item in the pane while the context above was
    # being assembled (it reads files and the database). Their unticks are
    # read again under a row lock at the moment of writing, so one made in
    # that window is kept rather than overwritten with the older list.
    async with pool.acquire() as con:
        async with con.transaction():
            cur = await con.fetchrow(
                "SELECT status, shared_context, metadata FROM support_tickets "
                "WHERE id = $1 AND entity_id = $2 FOR UPDATE", ticket_id, eid)
            if not cur or cur["status"] != "draft":
                # Sent or discarded between the read and the write.
                raise SupportError(409, "the ticket stopped being a draft while it was being changed")
            now_items = cur["shared_context"]
            if isinstance(now_items, str):
                now_items = json.loads(now_items)
            now_md = cur["metadata"]
            if isinstance(now_md, str):
                now_md = json.loads(now_md)
            # The person's own unticks carry over, including for an item this
            # update brings back after an earlier one dropped it: an item they
            # turned off stays off, and nothing here ticks one.
            was_off = _person_unticked(dict(now_md or {}), now_items)
            for it in shared:
                if it.get("key") in was_off:
                    it["included"] = False
            md = {**md, **dict(now_md or {}), "assembly": assembly,
                  "unticked": sorted(was_off)}
            # Send carries the revision the person was shown (send_service):
            # a draft the Host changed after that is not sent unseen.
            md["revision"] = int(md.get("revision") or 0) + 1
            await con.execute(
                """UPDATE support_tickets
                      SET category = $1, title = $2, severity = $3, problem = $4, context = $5,
                          recommendation = $6, pushback = $7, repro_steps = $8, expected = $9,
                          actual = $10, shared_context = $11::jsonb, metadata = $12::jsonb,
                          updated_at = NOW()
                    WHERE id = $13 AND entity_id = $14""",
                vals["category"], vals["title"], vals["severity"], vals["problem"], vals["context"],
                vals["recommendation"], vals["pushback"], vals["repro_steps"], vals["expected"],
                vals["actual"], json.dumps(shared), json.dumps(md), ticket_id, eid)
    log.info("support draft %d updated (%s)", ticket_id, ", ".join(sorted(given)) or "context")
    out = {"ticket_id": ticket_id, "status": "draft", "changed": sorted(given),
           "revision": md["revision"],
           "shared_items": len(shared), "view": draft_view(ticket_id),
           "note": ("draft updated in place; the person still approves what is shared and "
                    "presses Send — nothing has been sent")}
    if cuts:
        out["cut"] = cuts
    return out


async def list_service(pool, eid: int, *, status_filter: str | None = None,
                       category: str | None = None, project_id: int | None = None,
                       limit: int = 50, refresh: bool = True) -> dict:
    if pool is None:
        raise SupportError(503, "brain pool down")
    if status_filter and status_filter != "active" and status_filter not in LOCAL_STATUSES:
        raise SupportError(400, f"status must be one of {list(LOCAL_STATUSES)} or 'active'")
    if refresh:
        try:
            await refresh_service(pool, eid)
        except SupportError as e:
            log.debug("refresh skipped: %s", e.detail)  # bridge down: serve the last mirror
    sql = ["SELECT id, ticket_uuid, category, project_id, project_name, title, severity,",
           "       problem, status, platform_id, platform_status, platform_ref, resolution,",
           "       pr_url, opened_at, sent_at, updated_at, closed_at,",
           "       jsonb_array_length(shared_context) AS shared_items,",
           "       jsonb_array_length(thread) AS thread_len",
           "FROM support_tickets WHERE entity_id = $1"]
    params: list[Any] = [eid]
    if status_filter == "active":
        sql.append("AND status IN ('draft', 'sent')")
    elif status_filter:
        params.append(status_filter)
        sql.append(f"AND status = ${len(params)}")
    if category:
        if category not in CATEGORIES:
            raise SupportError(400, f"category must be one of {list(CATEGORIES)}")
        params.append(category)
        sql.append(f"AND category = ${len(params)}")
    if project_id is not None:
        params.append(int(project_id))
        sql.append(f"AND project_id = ${len(params)}")
    try:
        lim = max(1, min(int(limit or 50), 200))
    except (TypeError, ValueError):
        lim = 50
    sql.append(f"ORDER BY updated_at DESC LIMIT {lim}")
    rows = await pool.fetch(" ".join(sql), *params)
    return {"tickets": [_row_to_dict(r) for r in rows]}


async def get_service(pool, eid: int, ticket_id: int, *, refresh: bool = True) -> dict:
    if pool is None:
        raise SupportError(503, "brain pool down")
    if refresh:
        try:
            await refresh_service(pool, eid, ticket_id=ticket_id)
        except SupportError as e:
            log.debug("refresh skipped: %s", e.detail)
    row = await pool.fetchrow(
        "SELECT * FROM support_tickets WHERE id = $1 AND entity_id = $2", ticket_id, eid)
    if not row:
        raise SupportError(404, "ticket not found")
    out = _row_to_dict(row)
    # Which version of the draft this is; the pane hands it back on Send.
    out["revision"] = _revision(out.get("metadata"))
    return out


def _revision(md) -> int:
    """How many times the Host rewrote this draft (support_update). 0 for a
    draft never rewritten, or one from before the count existed."""
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:
            md = {}
    try:
        return int((md or {}).get("revision") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


async def badge_service(pool, eid: int) -> dict:
    """What needs the USER: drafts awaiting approval + platform questions."""
    if pool is None:
        return {"drafts": 0, "needs_info": 0, "total": 0}
    r = await pool.fetchrow(
        """SELECT count(*) FILTER (WHERE status = 'draft') AS drafts,
                  count(*) FILTER (WHERE status = 'sent' AND platform_status = 'needs_info') AS needs_info
             FROM support_tickets WHERE entity_id = $1""", eid)
    d, n = int(r["drafts"] or 0), int(r["needs_info"] or 0)
    return {"drafts": d, "needs_info": n, "total": d + n}


async def set_shared_service(pool, eid: int, ticket_id: int, included: dict[str, bool]) -> dict:
    """The user's trim: flip `included` per item key. Draft only."""
    if pool is None:
        raise SupportError(503, "brain pool down")
    # One locked read-modify-write, under the same row lock update_service
    # writes with: a support_update landing between a plain read and write
    # here would have its new item list replaced by the older one.
    async with pool.acquire() as con:
        async with con.transaction():
            row = await con.fetchrow(
                "SELECT shared_context, status, metadata FROM support_tickets "
                "WHERE id = $1 AND entity_id = $2 FOR UPDATE",
                ticket_id, eid)
            if not row:
                raise SupportError(404, "ticket not found")
            if row["status"] != "draft":
                raise SupportError(409, "shared context can only change on a draft")
            items = row["shared_context"]
            if isinstance(items, str):
                items = json.loads(items)
            md = row["metadata"]
            if isinstance(md, str):
                md = json.loads(md)
            # Recorded by key in metadata.unticked as well as on the item, so
            # the untick outlives the item leaving the list (update_service).
            off = _person_unticked(dict(md or {}), items)
            for it in items:
                if it.get("key") in included:
                    it["included"] = bool(included[it["key"]])
                    if it["included"]:
                        off.discard(str(it["key"]))
                    else:
                        off.add(str(it["key"]))
            await con.execute(
                "UPDATE support_tickets SET shared_context = $1::jsonb, "
                "metadata = metadata || jsonb_build_object('unticked', $4::jsonb), "
                "updated_at = NOW() WHERE id = $2 AND entity_id = $3",
                json.dumps(items), ticket_id, eid, json.dumps(sorted(off)))
    return {"ticket_id": ticket_id, "shared_context": items}


async def _bridge(op: str, params: dict) -> dict:
    try:
        from server.main import get_bridge_client
    except Exception as exc:
        raise SupportError(503, f"bridge unavailable: {exc}")
    bc = get_bridge_client()
    if bc is None or not getattr(bc, "_connected", False):
        raise SupportError(503, "bridge not connected")
    try:
        envelope = await bc.request_full(op, params)
    except Exception as exc:
        raise SupportError(502, f"bridge call failed: {exc}")
    if not envelope.get("ok"):
        raise SupportError(int(envelope.get("code") or 502), envelope.get("error") or f"{op} failed")
    return envelope.get("data") or {}


async def send_service(pool, eid: int, ticket_id: int, *, revision: int | None = None) -> dict:
    """The USER's approval. Sends the card + the INCLUDED context items.

    `revision` is the draft version the person was shown (get_service). The
    Host can rewrite a draft (support_update) while it is open in the pane;
    a Send naming an older version is refused, so nothing the person did not
    see leaves the pod."""
    if pool is None:
        raise SupportError(503, "brain pool down")
    row = await pool.fetchrow(
        "SELECT * FROM support_tickets WHERE id = $1 AND entity_id = $2", ticket_id, eid)
    if not row:
        raise SupportError(404, "ticket not found")
    if row["status"] != "draft":
        raise SupportError(409, f"ticket is {row['status']!r}, not a draft")
    if revision is not None and int(revision) != _revision(row["metadata"]):
        raise SupportError(409, "Your Host changed this draft after you opened it. Nothing was "
                                "sent; look over the new version, then press Send again.")
    items = row["shared_context"]
    if isinstance(items, str):
        items = json.loads(items)
    approved = [{"key": i["key"], "label": i["label"], "kind": i["kind"], "content": i["content"]}
                for i in items if i.get("included")]
    if len(approved) > SHARED_MAX:
        # The bridge keeps the first SHARED_MAX and drops the rest without a
        # word; refuse here instead so nothing ticked is lost silently.
        raise SupportError(400, f"{len(approved)} items are ticked to share; a support ticket "
                                f"carries at most {SHARED_MAX} — untick {len(approved) - SHARED_MAX}")

    if row["category"] == "bug":
        return await _send_bug_as_pr(pool, row, approved)

    data = await _bridge("platform.ticket", {
        "op": "open",
        "category": row["category"],
        "project": row["project_name"] or "",
        "title": row["title"],
        "severity": row["severity"],
        "card": {
            "problem": row["problem"], "context": row["context"],
            "recommendation": row["recommendation"], "pushback": row["pushback"],
            "repro_steps": row["repro_steps"], "expected": row["expected"], "actual": row["actual"],
        },
        "shared_context": approved,
        "local_ticket_uuid": str(row["ticket_uuid"]),
    })
    pid = int(data.get("ticket_id") or 0)
    if not pid:
        # An ok envelope with no id would strand the row as 'sent' with nothing
        # to refresh or reply to. Treat it as a failed send; the draft survives.
        raise SupportError(502, "platform accepted the ticket but returned no id — not sent, try again")
    await pool.execute(
        """UPDATE support_tickets
              SET status = 'sent', platform_id = $1, platform_status = $2,
                  sent_at = NOW(), updated_at = NOW()
            WHERE id = $3""",
        pid or None, str(data.get("status") or "received"), ticket_id)
    log.info("support ticket %d sent → platform %s", ticket_id, pid)
    return {"ticket_id": ticket_id, "platform_id": pid, "status": "sent",
            "platform_status": data.get("status") or "received", "shared_items": len(approved)}


async def _send_bug_as_pr(pool, row, approved: list[dict]) -> dict:
    """A BUG goes to the bug repo as a pull request, never into an operator's
    database: a platform bug landing straight in an operator's local database
    is a bad design.

    Same consent gate as every other category — this runs only after the user
    pressed Send, with exactly the items they left ticked. The file is authored
    in the set format and handed to the bridge op `proposal.file`; the operator
    signs the commit as this Host and opens the PR on the bug repo. The
    read-only validator then either merges it to main (validated) or closes it
    (rejected); the operator Host reads validated reports from main only.

    Local bookkeeping: `platform_id` stays NULL (there is no platform_tickets
    row to refresh), `platform_ref` is the PR ("Community/bugs#N"), the bug
    PR url goes in metadata so `pr_url` keeps meaning what the skill says it
    means — the FIX. refresh_service mirrors these rows through
    `proposal.status` instead of `platform.ticket`.
    """
    try:
        from brain.mastercode.runtime.capability_propose import (
            propose_bug_report, ProposalConflict, ProposalCredentialUnavailable)
    except Exception:
        try:
            from mastercode.runtime.capability_propose import (  # type: ignore
                propose_bug_report, ProposalConflict, ProposalCredentialUnavailable)
        except Exception as exc:
            raise SupportError(503, f"bug reporting unavailable ({exc})")
    import os
    # The name only labels the PR body; the bridge stamps the real identity
    # from the certificate. GITEA_USERNAME is optional in the pod (entrypoint),
    # and every Host's forge account is `entity-<id>` by construction, so a
    # missing variable must not make a Send fail (review NB7).
    gitea_user = os.environ.get("GITEA_USERNAME") or f"entity-{int(row['entity_id'])}"
    ticket = {k: row[k] for k in ("title", "severity", "problem", "context", "recommendation",
                                 "pushback", "repro_steps", "expected", "actual", "project_name")}
    ticket["ticket_uuid"] = str(row["ticket_uuid"])
    try:
        res = await propose_bug_report(gitea_user=gitea_user, ticket=ticket,
                                       shared_context=approved)
    except ProposalCredentialUnavailable as e:
        raise SupportError(503, f"the platform cannot file bug reports right now: {e}")
    except ProposalConflict:
        # The branch slug carries a hash of the content, so this can only be a
        # collision on the same ticket with the same bytes but a branch the
        # platform could not read back. Plain words, and a way out (review N7).
        raise SupportError(409, "the platform already holds a different copy of this report; "
                                "change the title or the shared items and send again")
    except ValueError as e:
        raise SupportError(400, f"bug report incomplete: {e}")
    except RuntimeError as e:
        raise SupportError(502, f"bug report not filed: {e}")
    except Exception as e:  # noqa: BLE001 — a transport failure is a 502, never a 500
        raise SupportError(502, f"bug report not filed (bridge): {type(e).__name__}: {e}")
    pr = int(res.get("pr") or 0)
    if not pr:
        raise SupportError(502, "platform accepted the report but returned no PR number — not sent, try again")
    ref = f"{res.get('repo') or 'bugs'}#{pr}"
    md = row["metadata"]
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:
            md = {}
    md = dict(md or {})
    md.update({"lane": "bug_pr", "bug_pr": pr, "bug_pr_url": res.get("url") or "",
               "bug_repo": res.get("repo") or "", "bug_path": res.get("path") or ""})
    await pool.execute(
        """UPDATE support_tickets
              SET status = 'sent', platform_id = NULL, platform_status = 'received',
                  platform_ref = $1, metadata = $2::jsonb, sent_at = NOW(), updated_at = NOW()
            WHERE id = $3""",
        ref, json.dumps(md), row["id"])
    log.info("bug ticket %d filed as %s", row["id"], ref)
    return {"ticket_id": row["id"], "platform_id": None, "status": "sent",
            "platform_status": "received", "platform_ref": ref,
            "bug_pr_url": res.get("url") or "", "shared_items": len(approved)}


# What the validator / operator side of the bug repo can say about a report,
# mapped by the operator from the
# PR's state + labels + marker comments. Mirrored into platform_status as-is.
BUG_PR_STATUSES = ("received", "validated", "needs_info", "pr_open", "fixed", "rejected")
BUG_STATUS_BATCH = 50   # the bridge's per-call cap on proposal.status numbers
BUG_REPLY_CAP = 4000    # the bridge's and operator's cap on a proposal.reply body


async def _refresh_bug_prs(pool, rows) -> int:
    """Mirror bug-PR rows (metadata.lane = 'bug_pr') through `proposal.status`.
    One bridge call for all of them; a row moves only when something changed."""
    numbers = {}
    for r in rows:
        md = r["metadata"]
        if isinstance(md, str):
            try:
                md = json.loads(md)
            except Exception:
                md = {}
        n = int((md or {}).get("bug_pr") or 0)
        if n:
            numbers[n] = r
    if not numbers:
        return 0
    # The bridge and the operator cap one status call at 50 numbers, and a bug
    # row only leaves this set on fixed/rejected — so the 51st open report
    # would otherwise turn every refresh into a 400 (review B2). Batches.
    reports: list[dict] = []
    ordered = sorted(numbers)
    for i in range(0, len(ordered), BUG_STATUS_BATCH):
        data = await _bridge("proposal.status",
                             {"kind": "bug", "numbers": ordered[i:i + BUG_STATUS_BATCH]})
        reports.extend(data.get("reports") or [])
    n_moved = 0
    for rep in reports:
        try:
            r = numbers[int(rep.get("number"))]
        except (KeyError, TypeError, ValueError):
            continue
        pstatus = str(rep.get("status") or "")
        if pstatus not in BUG_PR_STATUSES:
            continue
        thread = r["thread"]
        if isinstance(thread, str):
            thread = json.loads(thread)
        have = {(m.get("author"), m.get("body"), m.get("at")) for m in thread}
        grew = False
        for ev in rep.get("events") or []:
            key = ("platform", ev.get("body"), ev.get("at"))
            if ev.get("body") and key not in have:
                thread.append({"author": "platform", "body": ev.get("body") or "",
                               "at": ev.get("at") or _now()})
                have.add(key)
                grew = True
        new_vals = (pstatus, str(rep.get("resolution") or ""), str(rep.get("pr_url") or ""))
        old_vals = (r["platform_status"], r["resolution"], r["pr_url"])
        if new_vals == old_vals and not grew:
            continue
        closes = pstatus in TERMINAL_PLATFORM
        await pool.execute(
            """UPDATE support_tickets
                  SET platform_status = $1, resolution = $2, pr_url = $3,
                      thread = $4::jsonb, updated_at = NOW(),
                      status = CASE WHEN $5 THEN 'closed' ELSE status END,
                      closed_at = CASE WHEN $5 THEN COALESCE(closed_at, NOW()) ELSE closed_at END
                WHERE id = $6""",
            pstatus, new_vals[1], new_vals[2], json.dumps(thread), closes, r["id"])
        n_moved += 1
    return n_moved


async def discard_service(pool, eid: int, ticket_id: int) -> dict:
    if pool is None:
        raise SupportError(503, "brain pool down")
    res = await pool.execute(
        """UPDATE support_tickets SET status = 'discarded', closed_at = NOW(), updated_at = NOW()
            WHERE id = $1 AND entity_id = $2 AND status = 'draft'""", ticket_id, eid)
    if not res.endswith(" 1"):
        raise SupportError(404, "draft not found")
    return {"ticket_id": ticket_id, "status": "discarded"}


async def close_service(pool, eid: int, ticket_id: int) -> dict:
    """The user closes a ticket on their side; a sent one tells the platform."""
    if pool is None:
        raise SupportError(503, "brain pool down")
    row = await pool.fetchrow(
        "SELECT platform_id, status FROM support_tickets WHERE id = $1 AND entity_id = $2",
        ticket_id, eid)
    if not row:
        raise SupportError(404, "ticket not found")
    if row["status"] == "sent" and row["platform_id"]:
        try:
            await _bridge("platform.ticket", {"op": "close", "ticket_id": int(row["platform_id"])})
        except SupportError as e:
            log.info("platform close for %s failed (%s) — closing locally anyway", ticket_id, e.detail)
    await pool.execute(
        """UPDATE support_tickets SET status = 'closed', closed_at = NOW(), updated_at = NOW()
            WHERE id = $1 AND entity_id = $2 AND status IN ('draft', 'sent')""", ticket_id, eid)
    return {"ticket_id": ticket_id, "status": "closed"}


async def reply_service(pool, eid: int, ticket_id: int, *, body: str,
                        author: str = "user") -> dict:
    """A message from the user or the host to the platform (answers needs_info)."""
    if pool is None:
        raise SupportError(503, "brain pool down")
    body = _redact((body or "").strip())
    if not body:
        raise SupportError(400, "body required")
    if author not in ("user", "host"):
        author = "user"
    row = await pool.fetchrow(
        "SELECT platform_id, status, thread, metadata FROM support_tickets "
        "WHERE id = $1 AND entity_id = $2",
        ticket_id, eid)
    if not row:
        raise SupportError(404, "ticket not found")
    if row["status"] != "sent":
        raise SupportError(409, "only a sent ticket can be replied to")
    md = row["metadata"]
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:
            md = {}
    md = md or {}
    if row["platform_id"]:
        # The bridge takes at most TEXT_CAP characters a reply: a longer one
        # goes as numbered parts, whole and in order (RT-B22), never cut.
        for part in _reply_parts(body, TEXT_CAP):
            await _bridge("platform.ticket", {"op": "reply", "ticket_id": int(row["platform_id"]),
                                              "body": part, "author": author})
    elif md.get("lane") == "bug_pr" and md.get("bug_pr"):
        # A bug lives on its report PR: the answer to a needs_info note lands
        # there as a comment the validator reads (proposal.reply, review NB1).
        # The bridge and the operator cap a reply at 4,000 chars; say so here
        # instead of letting a longer one bounce off the bridge (review N3).
        if len(body) > BUG_REPLY_CAP:
            raise SupportError(400, f"a reply on a bug report is at most {BUG_REPLY_CAP} characters "
                                    f"(this one is {len(body)})")
        await _bridge("proposal.reply", {"kind": "bug", "number": int(md["bug_pr"]), "body": body})
    else:
        raise SupportError(409, "this ticket has nowhere to reply to (no platform id, no report PR)")
    thread = row["thread"]
    if isinstance(thread, str):
        thread = json.loads(thread)
    thread.append({"author": author, "body": body, "at": _now()})
    await pool.execute(
        "UPDATE support_tickets SET thread = $1::jsonb, updated_at = NOW() WHERE id = $2",
        json.dumps(thread), ticket_id)
    return {"ticket_id": ticket_id, "thread_len": len(thread)}


async def refresh_service(pool, eid: int, ticket_id: int | None = None) -> dict:
    """Mirror the platform side into the local rows: status, resolution, PR,
    and any replies the operator left. One bridge call for all sent tickets."""
    if pool is None:
        raise SupportError(503, "brain pool down")
    sql = ("SELECT id, platform_id, thread, platform_status, platform_ref, resolution, pr_url, "
           "metadata FROM support_tickets "
           "WHERE entity_id = $1 AND status = 'sent' "
           "AND (platform_id IS NOT NULL OR metadata->>'lane' = 'bug_pr')")
    params: list[Any] = [eid]
    if ticket_id is not None:
        params.append(ticket_id)
        sql += f" AND id = ${len(params)}"
    all_rows = await pool.fetch(sql, *params)
    if not all_rows:
        return {"refreshed": 0}
    bug_rows = [r for r in all_rows if r["platform_id"] is None]
    rows = [r for r in all_rows if r["platform_id"] is not None]
    # The two lanes are independent: a failure reading the bug repo (a 503
    # while the operator has no proposals credential, a 400 on a bad batch)
    # must not stop the platform-ticket mirror for the same Host (review B2).
    n_bugs = 0
    if bug_rows:
        try:
            n_bugs = await _refresh_bug_prs(pool, bug_rows)
        except SupportError as e:
            log.info("bug-PR refresh skipped (%s); platform tickets still refresh", e.detail)
    if not rows:
        return {"refreshed": n_bugs}
    data = await _bridge("platform.ticket", {"op": "list", "ids": [int(r["platform_id"]) for r in rows]})
    remote = {int(t["id"]): t for t in (data.get("tickets") or [])}
    n = 0
    for r in rows:
        t = remote.get(int(r["platform_id"]))
        if not t:
            continue
        thread = r["thread"]
        if isinstance(thread, str):
            thread = json.loads(thread)
        have = {(m.get("author"), m.get("body"), m.get("at")) for m in thread}
        grew = False
        for ev in t.get("events") or []:
            if ev.get("author") != "platform":
                continue
            key = ("platform", ev.get("body"), ev.get("at"))
            if key not in have:
                thread.append({"author": "platform", "body": ev.get("body") or "",
                               "at": ev.get("at") or _now()})
                have.add(key)
                grew = True
        pstatus = str(t.get("status") or "")
        new_vals = (pstatus, str(t.get("ledger_ref") or ""), str(t.get("resolution") or ""),
                    str(t.get("pr_url") or ""))
        old_vals = (r["platform_status"], r["platform_ref"], r["resolution"], r["pr_url"])
        if new_vals == old_vals and not grew:
            continue   # nothing moved — don't touch updated_at
        closes = pstatus in TERMINAL_PLATFORM
        await pool.execute(
            """UPDATE support_tickets
                  SET platform_status = $1, platform_ref = $2, resolution = $3, pr_url = $4,
                      thread = $5::jsonb, updated_at = NOW(),
                      status = CASE WHEN $6 THEN 'closed' ELSE status END,
                      closed_at = CASE WHEN $6 THEN COALESCE(closed_at, NOW()) ELSE closed_at END
                WHERE id = $7""",
            pstatus, str(t.get("ledger_ref") or ""), str(t.get("resolution") or ""),
            str(t.get("pr_url") or ""), json.dumps(thread), closes, r["id"])
        n += 1
    return {"refreshed": n + n_bugs}


# ── Routes ─────────────────────────────────────────────────────────────────

def _pool_eid():
    return brain_service.get_pool(), brain_service._entity_id


@router.get("", dependencies=[Depends(requires_permission("read:tickets"))])
async def list_tickets(status_filter: str | None = Query(None, alias="status"),
                       category: str | None = None, project_id: int | None = None,
                       limit: int = Query(50, le=200), refresh: bool = True) -> dict:
    pool, eid = _pool_eid()
    try:
        return await list_service(pool, eid, status_filter=status_filter, category=category,
                                  project_id=project_id, limit=limit, refresh=refresh)
    except SupportError as e:
        raise HTTPException(e.status, e.detail)


@router.get("/badge-counts", dependencies=[Depends(requires_permission("read:tickets"))])
async def badge_counts() -> dict:
    pool, eid = _pool_eid()
    return await badge_service(pool, eid)


@router.get("/{ticket_id}", dependencies=[Depends(requires_permission("read:tickets"))])
async def get_ticket(ticket_id: int, refresh: bool = True) -> dict:
    pool, eid = _pool_eid()
    try:
        return await get_service(pool, eid, ticket_id, refresh=refresh)
    except SupportError as e:
        raise HTTPException(e.status, e.detail)


@router.post("", dependencies=[Depends(requires_permission("write:tickets"))])
async def open_ticket(request: Request) -> dict:
    """The user filing directly from the pane (the host normally does this
    through the `tickets` tool). Same service, same draft-first rule."""
    pool, eid = _pool_eid()
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "body must be JSON") from None
    try:
        return await open_service(
            pool, eid,
            category=body.get("category") or "question", title=body.get("title") or "",
            problem=body.get("problem") or "", context=body.get("context") or "",
            recommendation=body.get("recommendation") or "", pushback=body.get("pushback") or "",
            repro_steps=body.get("repro_steps") or "", expected=body.get("expected") or "",
            actual=body.get("actual") or "", severity=body.get("severity") or "normal",
            project_id=body.get("project_id"), files=body.get("files") or [],
            include_errors=bool(body.get("include_errors", True)),
            include_inference=bool(body.get("include_inference", True)),
        )
    except SupportError as e:
        raise HTTPException(e.status, e.detail)


@router.post("/{ticket_id}/update", dependencies=[Depends(requires_permission("write:tickets"))])
async def update_ticket(ticket_id: int, request: Request) -> dict:
    """Rewrite a draft's card (the same service as the host's support_update)."""
    pool, eid = _pool_eid()
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "body must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be a JSON object")
    fields = {k: str(body[k]) for k in UPDATE_FIELDS if body.get(k) is not None}
    if body.get("files") is not None:
        fields["files"] = [str(f) for f in (body.get("files") or [])]
    try:
        return await update_service(pool, eid, ticket_id, **fields)
    except SupportError as e:
        raise HTTPException(e.status, e.detail)


@router.post("/{ticket_id}/shared", dependencies=[Depends(requires_permission("write:tickets"))])
async def set_shared(ticket_id: int, request: Request) -> dict:
    pool, eid = _pool_eid()
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "body must be JSON") from None
    inc = body.get("included") or {}
    if not isinstance(inc, dict):
        raise HTTPException(400, "included must be {key: bool}")
    try:
        return await set_shared_service(pool, eid, ticket_id, inc)
    except SupportError as e:
        raise HTTPException(e.status, e.detail)


@router.post("/{ticket_id}/send", dependencies=[Depends(requires_permission("write:tickets"))])
async def send_ticket(ticket_id: int, request: Request) -> dict:
    pool, eid = _pool_eid()
    # {"revision": n}: the draft version the person approved (optional, so an
    # older client still sends; the Circles pane always passes it).
    revision = None
    try:
        body = await request.json()
    except Exception:
        body = None
    if isinstance(body, dict) and body.get("revision") is not None:
        try:
            revision = int(body["revision"])
        except (TypeError, ValueError):
            raise HTTPException(400, "revision must be an integer") from None
    try:
        return await send_service(pool, eid, ticket_id, revision=revision)
    except SupportError as e:
        raise HTTPException(e.status, e.detail)


@router.post("/{ticket_id}/discard", dependencies=[Depends(requires_permission("write:tickets"))])
async def discard_ticket(ticket_id: int) -> dict:
    pool, eid = _pool_eid()
    try:
        return await discard_service(pool, eid, ticket_id)
    except SupportError as e:
        raise HTTPException(e.status, e.detail)


@router.post("/{ticket_id}/close", dependencies=[Depends(requires_permission("write:tickets"))])
async def close_ticket(ticket_id: int) -> dict:
    pool, eid = _pool_eid()
    try:
        return await close_service(pool, eid, ticket_id)
    except SupportError as e:
        raise HTTPException(e.status, e.detail)


@router.post("/{ticket_id}/reply", dependencies=[Depends(requires_permission("write:tickets"))])
async def reply_ticket(ticket_id: int, request: Request) -> dict:
    pool, eid = _pool_eid()
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "body must be JSON") from None
    try:
        return await reply_service(pool, eid, ticket_id, body=body.get("body") or "", author="user")
    except SupportError as e:
        raise HTTPException(e.status, e.detail)
