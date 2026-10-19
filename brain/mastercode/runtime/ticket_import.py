"""Import a ticket bundle into this Host.

One ticket system, two shapes: what
a PERSON must do is an operations ticket in Circles; what a BUILD must do is a
phase in a Workbench project. A bundle carries both, plus a readable archive
of what is already closed, from another ticket system into this Host. The
operator's own ledger is the first such system; nothing here is operator
only — any Host can bring its own queue in the same way.

Nothing is pushed into the pod. The user uploads the bundle (Workbench or the
Context app; a non-text extension keeps its bytes exact in the file lane,
server/routes/context.py _ingest_binary_file) and the Host runs
`tickets op='import' file=<name>`. The op:

  reads      the newest live upload of that filename, gzip or plain JSON,
             and checks its format, version and sha256 before anything is
             written — a bundle that fails any check writes nothing.
  tickets    each becomes an OPEN operations ticket (source 'manual',
             source_ref 'import:<bundle>:<key>'). Every one is user_only: the
             person brought their own queue in, the Host did not raise it, so
             the Host's model may not answer or close it. A credential ask on
             an option goes through the same refuse_bad_asks every writer uses.
  projects   each becomes a Workbench project, its items phases in status
             'planning' with no dependencies: nothing is buildable until the
             person readies it (the scheduler starts only ready/failed
             phases, runtime/tools.py _schedule_phases).
  archive    each section is filed in the context corpus as one document the
             Host reads with context_ingest op='read'. The import queues no
             embed job; a later embed fill over the corpus may still embed it.

Re-running is safe: tickets are keyed by source_ref, projects and phases by
the bundle's keys recorded in projects.config.import. A ticket, project or
phase already present is skipped, never rewritten, so a newer export adds
only the new ones. The archive is different: it is a record, so each section
is made to match the export being imported — one already identical (by
position and text, piece_sha256) is skipped; one worded differently is
written again whole. A piece the user promoted into memory is never removed:
it stays in the archive when it is still the section's text, and otherwise
moves to its own file (promoted_name). One import runs at a time per Host (a
session advisory lock).
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import logging
import re
import zlib
from typing import Any
from mastercode.runtime.tool_call_ledger import refused as _refused, refused_if_caller as _refused_if_caller

log = logging.getLogger("privatae.ticket_import")

FORMAT = "privatae.tickets-bundle"
VERSION = 1
ARCHIVE_SOURCE = "import"          # user_context.source for archive rows
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_TICKETS = 2000
MAX_PROJECTS = 50
MAX_PHASES = 500                   # per project
MAX_SECTIONS = 20000
MAX_OPTIONS = 6
PIECE_CHARS = 8000                 # one archive row; a longer section is split on lines
_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
# No ':' in a bundle id, so source_ref 'import:<bundle>:<key>' splits one way.
_BUNDLE_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_OPTION_ID_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
_LOCK_NS = 0x7469636B              # 'tick' — pg_try_advisory_lock(ns, entity_id)
_OPTION_KEYS = ("id", "label", "consequence", "recommend", "credential_request")
IMPORT_BITS = 1 | 2 | 4 | 8        # read/write context and memory: tickets, uploads, projects


class BundleError(ValueError):
    """The bundle cannot be imported as given. Nothing was written."""


def digest(body: dict) -> str:
    """sha256 over the bundle minus its `sha256` field, in one canonical
    encoding (sorted keys, no whitespace, UTF-8). The exporter computes the
    same; a bundle cut short or edited in transit does not match."""
    b = {k: v for k, v in body.items() if k != "sha256"}
    raw = json.dumps(b, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def decode(raw: bytes) -> dict:
    """Bytes of an uploaded bundle -> the checked bundle. Raises BundleError."""
    if raw[:2] == b"\x1f\x8b":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
                raw = gz.read(MAX_BUNDLE_BYTES + 1)
        except (OSError, EOFError, zlib.error) as e:
            # A cut-short upload ends before the end-of-stream marker (EOFError).
            raise BundleError(f"not a readable gzip file ({e})") from None
    if len(raw) > MAX_BUNDLE_BYTES:
        raise BundleError(f"bundle is larger than {MAX_BUNDLE_BYTES // (1024 * 1024)} MiB")
    try:
        b = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise BundleError(f"not JSON ({e})") from None
    if not isinstance(b, dict) or b.get("format") != FORMAT:
        raise BundleError(f"not a ticket bundle (format must be {FORMAT!r})")
    if b.get("version") != VERSION:
        raise BundleError(f"bundle version {b.get('version')!r} — this Host reads version {VERSION}")
    if b.get("sha256") != digest(b):
        raise BundleError("the bundle's sha256 does not match its contents — it was cut short "
                          "or changed after export; export it again")
    why = check(b)
    if why:
        raise BundleError(why)
    return b


def _str(v: Any, cap: int, what: str, *, required: bool = True) -> str:
    if v is None and not required:
        return ""
    if not isinstance(v, str) or (required and not v.strip()):
        raise BundleError(f"{what} must be a non-empty string")
    if len(v) > cap:
        raise BundleError(f"{what} is longer than {cap} characters")
    return v


def _key(v: Any, what: str) -> str:
    if not isinstance(v, str) or not _KEY_RE.match(v):
        raise BundleError(f"{what} must match {_KEY_RE.pattern}")
    return v


def _no_nul(v: Any, where: str) -> None:
    """Postgres text and jsonb cannot hold U+0000, and JSON can: one such
    character would fail its row after everything before it was written.
    Refused here, so a bundle carrying one writes nothing."""
    if isinstance(v, str):
        if "\x00" in v:
            raise BundleError(f"{where} contains a NUL character (U+0000), which the Host "
                              "cannot store — remove it and export again")
    elif isinstance(v, dict):
        for k, x in v.items():
            _no_nul(k, f"{where} key")
            _no_nul(x, f"{where}.{k}")
    elif isinstance(v, list):
        for i, x in enumerate(v):
            _no_nul(x, f"{where}[{i}]")


def check(b: dict) -> str:
    """'' when the bundle's shape is importable, else the first reason it is
    not. Every limit is checked here, before anything is written."""
    try:
        if not isinstance(b.get("bundle_id"), str) or not _BUNDLE_RE.match(b["bundle_id"]):
            raise BundleError(f"bundle_id must match {_BUNDLE_RE.pattern}")
        _no_nul(b, "bundle")
        _str(b.get("source"), 200, "source")
        tickets = b.get("tickets") or []
        projects = b.get("projects") or []
        archive = b.get("archive")
        if not isinstance(tickets, list) or len(tickets) > MAX_TICKETS:
            raise BundleError(f"tickets must be a list of at most {MAX_TICKETS}")
        if not isinstance(projects, list) or len(projects) > MAX_PROJECTS:
            raise BundleError(f"projects must be a list of at most {MAX_PROJECTS}")
        seen: set = set()
        for i, t in enumerate(tickets):
            w = f"tickets[{i}]"
            if not isinstance(t, dict):
                raise BundleError(f"{w} must be an object")
            k = _key(t.get("key"), f"{w}.key")
            if ("t", k) in seen:
                raise BundleError(f"{w}.key {k!r} appears twice")
            seen.add(("t", k))
            _str(t.get("sitrep"), 12000, f"{w}.sitrep")
            _str(t.get("project"), 200, f"{w}.project", required=False)
            p = t.get("priority", 50)
            if isinstance(p, bool) or not isinstance(p, int) or not 0 <= p <= 100:
                raise BundleError(f"{w}.priority must be an integer 0..100")
            opts = t.get("options")
            if not isinstance(opts, list) or not 1 <= len(opts) <= MAX_OPTIONS:
                raise BundleError(f"{w}.options must list 1..{MAX_OPTIONS} options")
            ids: set = set()
            for j, o in enumerate(opts):
                ow = f"{w}.options[{j}]"
                if not isinstance(o, dict):
                    raise BundleError(f"{ow} must be an object")
                extra = set(o) - set(_OPTION_KEYS)
                if extra:
                    raise BundleError(f"{ow} carries unknown field(s) {sorted(extra)}")
                oid = o.get("id")
                if not isinstance(oid, str) or not _OPTION_ID_RE.match(oid) or oid in ids:
                    raise BundleError(f"{ow}.id must be a unique id matching {_OPTION_ID_RE.pattern}")
                ids.add(oid)
                _str(o.get("label"), 200, f"{ow}.label")
                _str(o.get("consequence"), 2000, f"{ow}.consequence", required=False)
                if "recommend" in o and not isinstance(o["recommend"], bool):
                    raise BundleError(f"{ow}.recommend must be true or false")
        for i, p in enumerate(projects):
            w = f"projects[{i}]"
            if not isinstance(p, dict):
                raise BundleError(f"{w} must be an object")
            k = _key(p.get("key"), f"{w}.key")
            if ("p", k) in seen:
                raise BundleError(f"{w}.key {k!r} appears twice")
            seen.add(("p", k))
            _str(p.get("name"), 200, f"{w}.name")
            _str(p.get("description"), 4000, f"{w}.description", required=False)
            phases = p.get("phases") or []
            if not isinstance(phases, list) or len(phases) > MAX_PHASES:
                raise BundleError(f"{w}.phases must be a list of at most {MAX_PHASES}")
            pk: set = set()
            for j, ph in enumerate(phases):
                pw = f"{w}.phases[{j}]"
                if not isinstance(ph, dict):
                    raise BundleError(f"{pw} must be an object")
                kk = _key(ph.get("key"), f"{pw}.key")
                if kk in pk:
                    raise BundleError(f"{pw}.key {kk!r} appears twice in this project")
                pk.add(kk)
                _str(ph.get("title"), 200, f"{pw}.title")
                _str(ph.get("spec"), 16000, f"{pw}.spec")
        if archive is not None:
            if not isinstance(archive, dict):
                raise BundleError("archive must be an object")
            fn = _str(archive.get("filename"), 120, "archive.filename")
            if "/" in fn or "\\" in fn or fn.startswith("."):
                raise BundleError("archive.filename must be a plain file name")
            secs = archive.get("sections") or []
            if not isinstance(secs, list) or len(secs) > MAX_SECTIONS:
                raise BundleError(f"archive.sections must be a list of at most {MAX_SECTIONS}")
            sk: set = set()
            for i, s in enumerate(secs):
                sw = f"archive.sections[{i}]"
                if not isinstance(s, dict):
                    raise BundleError(f"{sw} must be an object")
                k = _key(s.get("key"), f"{sw}.key")
                if k in sk:
                    raise BundleError(f"{sw}.key {k!r} appears twice")
                sk.add(k)
                _str(s.get("title"), 300, f"{sw}.title")
                _str(s.get("text"), 2_000_000, f"{sw}.text")
    except BundleError as e:
        return str(e)
    return ""


def promoted_name(filename: str) -> str:
    """Where a promoted archive piece goes when a newer export replaces it."""
    stem = filename[:-3] if filename.endswith(".md") else filename
    return f"{stem} (pieces you promoted from an earlier import).md"


def _sha(piece: str) -> str:
    """A piece's identity: sha256 of its text as stored (the stage strips
    every row), so a stored row and this export's piece compare equal only
    when the text is the same."""
    return hashlib.sha256(piece.strip().encode("utf-8")).hexdigest()


def pieces(text: str, cap: int = PIECE_CHARS) -> list[str]:
    """Split a section into rows of at most `cap` characters, on line breaks
    where there are any. Nothing is dropped or repeated: the pieces joined
    back are the text."""
    if len(text) <= cap:
        return [text]
    out: list[str] = []
    cur = ""
    for line in text.splitlines(keepends=True):
        while len(line) > cap:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:cap])
            line = line[cap:]
        if len(cur) + len(line) > cap:
            out.append(cur)
            cur = ""
        cur += line
    if cur:
        out.append(cur)
    return out


async def read_upload(pool, eid: int, filename: str) -> bytes:
    """The newest live upload of `filename` in the file lane, as bytes.
    Raises BundleError naming what IS uploaded when the name does not match."""
    row = await pool.fetchrow(
        """SELECT id, content, storage_backend FROM user_context
            WHERE entity_id = $1 AND status = 'raw' AND source = 'file_upload'
              AND metadata->>'kind' = 'file' AND metadata->>'from_file' = $2
            ORDER BY id DESC LIMIT 1""",
        eid, filename)
    if row is None:
        names = await pool.fetch(
            """SELECT DISTINCT metadata->>'from_file' AS f FROM user_context
                WHERE entity_id = $1 AND status = 'raw' AND source = 'file_upload'
                  AND metadata->>'kind' = 'file'
                ORDER BY 1 LIMIT 20""", eid)
        have = ", ".join(r["f"] for r in names if r["f"]) or "none"
        raise BundleError(f"no uploaded file named {filename!r} (uploaded files: {have}). "
                          "Upload the bundle as it came from the export — a .json or .txt "
                          "name would be split into text and cannot be imported.")
    content = row["content"]
    if (row["storage_backend"] or "pg") != "pg":
        from brain.mastercode import context_backends
        content = await context_backends.retrieve_user_context_content(
            pool, eid, row["id"], row["storage_backend"])
        if not content:
            raise BundleError("the bundle's bytes are held on a machine that is not checked in "
                              "right now — try again when it is")
    try:
        return base64.b64decode(content, validate=True)
    except (ValueError, TypeError) as e:
        raise BundleError(f"the stored upload is not readable ({e})") from None


async def retire_bundle_text(pool, eid: int, filename: str) -> int:
    """Dismiss the text the host's own reader filed from this bundle upload.

    An upload is read the moment it lands (server/readers — a .gz too), so
    the bundle's JSON sits in the context
    store as searchable text next to the file. Once the bundle has been
    IMPORTED, that text is a second copy of what the import just created as
    tickets, phases and archive pieces — a search would return the raw dump
    beside the real rows. The file row itself stays (a re-import reads it);
    a piece the user promoted stays (promoted text is theirs, never removed);
    only the other extracted text rows hanging off it go. Returns the count."""
    tag = await pool.execute(
        """UPDATE user_context SET status = 'dismissed', updated_at = now()
            WHERE entity_id = $1 AND source = 'extracted'
              AND status NOT IN ('dismissed', 'promoted')
              AND media_parent_id IN (
                  SELECT id FROM user_context
                   WHERE entity_id = $1 AND source = 'file_upload'
                     AND metadata->>'kind' = 'file' AND metadata->>'from_file' = $2)""",
        eid, filename)
    try:
        return int(str(tag).rsplit(" ", 1)[-1])
    except (ValueError, TypeError):
        return 0


async def _retire_after_import(pool, eid: int, filename: str, res: dict) -> None:
    """Retire the bundle's auto-read text once the import landed clean. The
    import is already committed by now, so a failure HERE is noted on the
    result and never turned into a failed import."""
    if res.get("errors"):
        return
    try:
        res["bundle_text_retired"] = await retire_bundle_text(pool, eid, filename)
    except Exception as e:  # noqa: BLE001 — the import stands; this is housekeeping
        log.info("ticket import: could not retire the bundle's text for %r (%s)", filename, e)
        res["bundle_text_retired"] = 0
        res["note"] = ("the bundle's own text is still in the context store beside the "
                       "imported rows (retiring it failed: %s)" % (str(e)[:120]))


def _options(opts: list) -> list:
    """The bundle's options, carrying only the fields a ticket option has."""
    out = []
    for o in opts:
        keep = {k: o[k] for k in _OPTION_KEYS if k in o and o[k] not in (None, "")}
        out.append(keep)
    return out


async def _plan(pool, eid: int, b: dict) -> dict:
    """What is already here, by key. Read-only."""
    bid = b["bundle_id"]
    refs = {r["source_ref"] for r in await pool.fetch(
        "SELECT source_ref FROM entity_tickets WHERE entity_id = $1 AND source_ref LIKE $2",
        eid, f"import:{bid}:%")}
    projects: dict[str, dict] = {}
    for r in await pool.fetch(
            """SELECT id, config FROM projects
                WHERE entity_id = $1 AND config->'import'->>'bundle_id' = $2""", eid, bid):
        cfg = r["config"]
        if isinstance(cfg, str):
            cfg = json.loads(cfg)
        imp = (cfg or {}).get("import") or {}
        if imp.get("key"):
            projects[imp["key"]] = {"id": r["id"], "phases": dict(imp.get("phases") or {})}
    live_phases: set = set()
    ids = [pid for p in projects.values() for pid in p["phases"].values() if isinstance(pid, int)]
    if ids:
        live_phases = {r["id"] for r in await pool.fetch(
            "SELECT id FROM project_phases WHERE id = ANY($1::int[])", ids)}
    # What of each archive section the document holds: (id, status, chunk,
    # chunk_count, piece sha256) per live row in the document's batch. The
    # import compares these with the pieces THIS export splits the section
    # into — by position, count and content — never with what an older
    # export wrote.
    sections: dict[str, list] = {}
    for r in await pool.fetch(
            """SELECT id, metadata->>'import_key' AS k, status,
                      (metadata->>'chunk')::int AS n, (metadata->>'chunk_count')::int AS of,
                      metadata->>'piece_sha256' AS sha
                 FROM user_context
                WHERE entity_id = $1 AND status <> 'dismissed'
                  AND metadata->>'import_bundle' = $2 AND metadata->>'upload_batch' = $3""",
            eid, bid, f"import-{bid}"):
        sections.setdefault(r["k"], []).append((r["id"], r["status"], r["n"], r["of"], r["sha"]))
    return {"refs": refs, "projects": projects, "live_phases": live_phases, "sections": sections}


async def import_bundle(pool, eid: int, b: dict, *, dry_run: bool = False) -> dict:
    """Land a checked bundle (from `decode`). Returns counts per kind plus the
    items that failed and why; one item failing never stops the rest."""
    from server.routes.tickets import TicketError, create_ticket_service
    bid, src = b["bundle_id"], b["source"]
    have = await _plan(pool, eid, b)
    out: dict = {"bundle_id": bid, "source": src, "dry_run": bool(dry_run),
                 "projects": {"created": 0, "present": 0},
                 "phases": {"created": 0, "present": 0},
                 "tickets": {"created": 0, "present": 0},
                 "archive": {"rows": 0, "present": 0},
                 "errors": []}

    for p in b.get("projects") or []:
        known = have["projects"].get(p["key"])
        if known is not None:
            out["projects"]["present"] += 1
            project_id = known["id"]
        else:
            out["projects"]["created"] += 1
            project_id = None
            if not dry_run:
                try:
                    project_id = await _create_project(pool, eid, p, bid, src)
                except Exception as e:  # noqa: BLE001
                    out["projects"]["created"] -= 1
                    out["errors"].append({"kind": "project", "key": p["key"], "error": str(e)[:300]})
                    continue
            known = {"id": project_id, "phases": {}}
        for ph in p.get("phases") or []:
            pid = known["phases"].get(ph["key"])
            if isinstance(pid, int) and pid in have["live_phases"]:
                out["phases"]["present"] += 1
                continue
            out["phases"]["created"] += 1
            if dry_run:
                continue
            try:
                await _insert_phase(pool, project_id, ph, bid)
            except Exception as e:  # noqa: BLE001
                out["phases"]["created"] -= 1
                out["errors"].append({"kind": "phase", "key": ph["key"], "error": str(e)[:300]})

    for t in b.get("tickets") or []:
        ref = f"import:{bid}:{t['key']}"
        if ref in have["refs"]:
            out["tickets"]["present"] += 1
            continue
        if dry_run:
            out["tickets"]["created"] += 1
            continue
        context = {"imported": {"bundle_id": bid, "key": t["key"], "source": src}}
        if t.get("project"):
            context["project"] = t["project"]
        try:
            await create_ticket_service(
                pool, eid, source="manual", sitrep=t["sitrep"], options=_options(t["options"]),
                source_ref=ref,
                requester={"kind": "import", "display_name": src, "handle": t["key"]},
                context=context, priority=int(t.get("priority", 50)),
                # The person's own queue, brought in by them: the Host did not
                # raise these and may not answer or close them.
                metadata={"imported": {"bundle_id": bid, "key": t["key"]}, "user_only": True},
            )
            out["tickets"]["created"] += 1
        except TicketError as e:
            out["errors"].append({"kind": "ticket", "key": t["key"], "error": e.detail})
        except Exception as e:  # noqa: BLE001 — one item never stops the rest
            out["errors"].append({"kind": "ticket", "key": t["key"], "error": str(e)[:300]})

    archive = b.get("archive")
    if archive:
        from server.routes.context import stage_context_service
        out["archive"]["filename"] = archive["filename"]
        for s in archive.get("sections") or []:
            # stage_context_service strips every row and skips an empty one, so
            # a whitespace-only piece (a line break after a full-width line)
            # is never stored — it must not count toward chunk_count either.
            parts = [x for x in pieces(s["text"]) if x.strip()]
            want = sorted((n, len(parts), _sha(x)) for n, x in enumerate(parts))
            stored = have["sections"].get(s["key"], [])
            # Present: every piece of THIS export is stored at its position,
            # with this split's count and this text (raw, or promoted by the
            # user) — and nothing else is in the document for this section.
            if sorted((n, of, sha) for _i, _st, n, of, sha in stored) == want:
                out["archive"]["present"] += 1
                continue
            # Otherwise the section is written again whole: a failure or an
            # interruption cut it short, or a newer export splits it or words
            # it differently. Raw pieces are dismissed. A piece the user
            # promoted into memory keeps its status and its memory link; it
            # stays in the document only when it IS this export's piece at
            # its position, and is otherwise moved out of the document
            # (its batch changes) so a read never mixes two exports.
            keep = {n for _i, st, n, of, sha in stored
                    if st == "promoted" and (n, of, sha) in want}
            detach = [i for i, st, n, of, sha in stored
                      if st == "promoted" and (n, of, sha) not in want]
            items = [{
                "content": text,
                "title": f"{archive['filename']} — {s['title']}"
                         + (f" ({n + 1}/{len(parts)})" if len(parts) > 1 else ""),
                "source_id": f"import:{bid}:{s['key']}:{n}",
                "tags": [],
                "metadata": {"from_file": archive["filename"], "import_bundle": bid,
                             "import_key": s["key"], "chunk": n, "chunk_count": len(parts),
                             "piece_sha256": _sha(text), "upload_batch": f"import-{bid}"},
            } for n, text in enumerate(parts) if n not in keep]
            if dry_run:
                out["archive"]["rows"] += len(items)
                continue
            try:
                if any(st == "raw" for _i, st, _n, _of, _sha_ in stored):
                    await _drop_section(pool, eid, bid, s["key"])
                if detach:
                    # Its own file, named for what it is: a read of the archive
                    # never reaches it whatever the timestamps, and the file
                    # list says plainly what it holds.
                    await pool.execute(
                        """UPDATE user_context
                              SET metadata = metadata || jsonb_build_object(
                                      'upload_batch', $3::text, 'from_file', $4::text,
                                      'import_detached', true),
                                  updated_at = now()
                            WHERE entity_id = $1 AND id = ANY($2::int[]) AND status = 'promoted'""",
                        eid, detach, f"import-{bid}-promoted", promoted_name(archive["filename"]))
                res = (await stage_context_service(pool, eid, source=ARCHIVE_SOURCE, items=items,
                                                   embed_now=False)) if items else {"imported": 0}
            except Exception as e:  # noqa: BLE001
                out["errors"].append({"kind": "archive", "key": s["key"], "error": str(e)[:300]})
                continue
            out["archive"]["rows"] += int(res.get("imported") or 0)
            if res.get("errors") or int(res.get("imported") or 0) != len(items):
                # Some pieces did not land: the section is incomplete and the
                # next import writes it again whole.
                why = "; ".join(str(e.get("error"))[:200] for e in (res.get("errors") or [])[:2])
                out["errors"].append({"kind": "archive", "key": s["key"],
                                      "error": why or "not every piece was stored"})
    return out


async def _drop_section(pool, eid: int, bid: str, key: str) -> None:
    """Remove the raw pieces of an archive section before it is written again
    whole. Only this bundle's rows for this key, and never a piece the user
    promoted (the caller keeps or leaves those)."""
    await pool.execute(
        """UPDATE user_context SET status = 'dismissed', updated_at = now()
            WHERE entity_id = $1 AND metadata->>'import_bundle' = $2
              AND metadata->>'import_key' = $3 AND status = 'raw'
              AND metadata->>'upload_batch' = $4""",
        eid, bid, key, f"import-{bid}")


async def _create_project(pool, eid: int, p: dict, bid: str, src: str) -> int:
    """The project row, then the same setup every other creation path does
    (tools.create_project, POST /api/projects): its directory — which compare,
    adopt and the repo sync read to decide the project exists on disk — and
    its repo. The setup is fail-soft like theirs; the row is the import."""
    cfg = {"import": {"bundle_id": bid, "key": p["key"], "source": src, "phases": {}}}
    project_id = await pool.fetchval(
        """INSERT INTO projects (entity_id, name, description, status, config)
           VALUES ($1, $2, $3, 'active', $4::jsonb) RETURNING id""",
        eid, p["name"], p.get("description") or "", json.dumps(cfg))
    try:
        from server.routes.projects import ensure_project_dir, ensure_project_repo
        ensure_project_dir(int(project_id))
        await ensure_project_repo(pool, int(project_id), p["name"])
    except Exception as e:  # noqa: BLE001
        log.info("ticket import: project %s setup incomplete (%s) — the repo backfills on sync",
                 project_id, e)
    return int(project_id)


async def _insert_phase(pool, project_id: int, ph: dict, bid: str) -> int:
    """One 'planning' phase, and its key recorded on the project, in one
    transaction: a phase is never inserted without the record that makes a
    re-run skip it."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT 1 FROM projects WHERE id = $1 FOR UPDATE", project_id)
            new_id = await conn.fetchval(
                """INSERT INTO project_phases
                       (project_id, phase_number, title, description, spec, status, gate,
                        validation, depends_on)
                   VALUES ($1,
                           (SELECT COALESCE(MAX(phase_number), 0) + 1 FROM project_phases
                             WHERE project_id = $1),
                           $2, '', $3, 'planning', 'none', 'review', '{}')
                   RETURNING id""",
                project_id, ph["title"], ph["spec"])
            await conn.execute(
                """UPDATE projects
                      SET config = jsonb_set(config, '{import,phases}',
                                             COALESCE(config->'import'->'phases', '{}'::jsonb)
                                             || jsonb_build_object($2::text, $3::int)),
                          updated_at = now()
                    WHERE id = $1 AND config->'import'->>'bundle_id' = $4""",
                project_id, ph["key"], new_id, bid)
    return int(new_id)


async def run(args: dict, *, pool, entity_id: int, agent_grant: int | None = None) -> dict:
    """The tool envelope for tickets op='import'. The Host itself runs it on
    the person's word; a dispatched agent may not (it would write projects
    under a grant that only named tickets)."""
    a = args or {}
    if agent_grant is not None:
        return _refused("import is the Host's own act on the person's request — "
                                             "a dispatched agent may not run it")
    fname = str(a.get("file") or "").strip()
    if not fname:
        return _refused("file is required for op='import' — the uploaded "
                                             "bundle's name (context_ingest op='files')")
    if pool is None:
        return {"status": "error", "result": "pool unavailable"}
    # The tool's own caps name tickets (1|8); the import also reads the
    # upload (read_memory, 2) and writes projects and phases (write_context,
    # 4 — what create_project and project_phase require). The Host must hold
    # all of them, so the import never writes what the Host could not.
    from mastercode.runtime.meta_tool_caps import _resolve_entity_scopes, scopes_to_bits
    try:
        bits = scopes_to_bits(await _resolve_entity_scopes(pool, int(entity_id), strict=True))
    except Exception as e:  # noqa: BLE001
        return _refused_if_caller(e, f"could not read this Host's capabilities this call "
                                             f"({type(e).__name__}) — nothing was written; try again")
    if bits & IMPORT_BITS != IMPORT_BITS:
        return _refused("import needs this Host to read its uploads and "
                                             "write projects as well as tickets, and it cannot "
                                             "(capability bits) — nothing was written")
    async with pool.acquire() as lock_conn:
        got = await lock_conn.fetchval("SELECT pg_try_advisory_lock($1, $2)", _LOCK_NS, int(entity_id))
        if not got:
            return _refused("an import is already running on this Host — "
                                                 "wait for it to finish, then run it again")
        try:
            try:
                b = decode(await read_upload(pool, int(entity_id), fname))
                res = await import_bundle(pool, int(entity_id), b)
            except BundleError as e:
                return _refused(f"import refused, nothing written: {e}")
            await _retire_after_import(pool, int(entity_id), fname, res)
        finally:
            await lock_conn.fetchval("SELECT pg_advisory_unlock($1, $2)", _LOCK_NS, int(entity_id))
    log.info("ticket import %s from %r: %s", res["bundle_id"], fname,
             {k: res[k] for k in ("projects", "phases", "tickets", "archive")})
    return {"status": "ok", "result": res}
