#!/usr/bin/env python3
"""Gates for the context-extraction lane.

What each check guards is named in its own message. Two of them exist because
the defect was found the hard way in this lane:

  * the token-id KEY. list_bastions returns the token as "id"; the mgmt relay is
    keyed by that value. Reading "token_id" off that dict does not raise — it
    dispatches to None and comes back "no worker connected", which is
    indistinguishable from an offline machine.
  * the OP NAME. The "ctx_" prefix is what puts ctx_extract in the dcn caps
    family on the bastion. opFamily returns "" for a name it does not recognise
    and capsAllow then ALLOWS it, so a rename on either side of the wire
    silently ungates an extractor on every member pairing.

Run: APP_ROOT=<repo> python3 scripts/test_context_extract_lane.py
"""
import os
import re
import sys

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "brain"))

failures = []
checks = 0


def check(label, cond, extra=""):
    global checks
    checks += 1
    print(("  ok    " if cond else "  FAIL  ") + label + (("\n        " + str(extra)) if extra and not cond else ""))
    if not cond:
        failures.append(label)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def flat(s):
    """Whitespace-normalised: source formatting is not the invariant."""
    return re.sub(r"\s+", " ", s)


extract_py = read("server/routes/context_extract.py")
topology_py = read("server/network/topology.py")
main_py = read("server/main.py")
migration = read("migrations/v137_bastion_prep_tools.sql")

# ── 1. the op name, which is the caps gate ──────────────────────────────────
from server.routes.context_extract import (  # noqa: F401
    FRAME_TOOLS,  # noqa: E402
    EXTRACT_OP, MAX_SOURCE_BYTES, MIME_TO_TOOL, PREP_CAPABILITY, tool_for_mime,
    tools_for_mime,
)

check("the extraction op keeps the ctx_ prefix (its caps family depends on it)",
      EXTRACT_OP.startswith("ctx_"), f"EXTRACT_OP={EXTRACT_OP!r}")
check("the capability token matches what the bastion advertises",
      PREP_CAPABILITY == "context-prep", PREP_CAPABILITY)

# ── 2. the token-id key (the bug this lane actually hit) ────────────────────
check("the machine's token id is read as 'id', not 'token_id'",
      'b.get("id")' in extract_py and '"token_id": b.get("id")' in extract_py)
check("and the wrong key is not used anywhere in the lookup",
      'b.get("token_id")' not in extract_py)

# ── 3. extracted rows must be EMBEDDABLE ────────────────────────────────────
# NOT_FILE_KIND_SQL excludes metadata.kind == 'file' from every embed surface.
# Extracted text that inherited 'file' would be stored and never embedded — the
# whole point is that it joins the lane that already works.
from server.routes.context import NOT_FILE_KIND_SQL  # noqa: E402

check("the embeddable predicate is the one that excludes file rows",
      "'file'" in NOT_FILE_KIND_SQL and "NOT IN" in NOT_FILE_KIND_SQL,
      NOT_FILE_KIND_SQL)
# And video frame rows, which hold no bytes at all. A frame with an
# empty content column inside the text lane is permanently "unembedded text".
check("...and video frame rows too",
      "'frame'" in NOT_FILE_KIND_SQL, NOT_FILE_KIND_SQL)
check("extracted rows are written as kind 'text', never 'file'",
      '"kind": "text"' in extract_py and '"kind": "file"' not in extract_py)
check("extracted rows link back to the document they came from",
      "media_parent_id" in extract_py and '"extracted_from": ctx_id' in extract_py)
check("extracted text is chunked with the SAME chunker uploaded text uses",
      "_chunk_text" in extract_py)

# ── 4. re-running must not double the document ──────────────────────────────
check("a re-run REPLACES the previous extraction instead of appending",
      "DELETE FROM user_context" in extract_py and "media_parent_id = $2" in flat(extract_py))

# ── 5. the host refuses what the box would refuse, and says why ─────────────
check("the host mirrors the box's size cap (8 MiB) rather than pushing bytes "
      "at a socket that will refuse them",
      MAX_SOURCE_BYTES == 8 * 1024 * 1024, MAX_SOURCE_BYTES)
check("an oversize document is refused WITH its size and the cap",
      "size_bytes" in extract_py and "not attempted" in flat(extract_py))
check("a machine is only offered when it advertises the capability AND the tool",
      "PREP_CAPABILITY not in" in extract_py and "tool not in names" in flat(extract_py))
# video/mp4 was the unextractable example here and is extractable now
# (its audio track becomes a transcript). Replaced rather than deleted, with a
# type nothing in this lane opens — a .doc is an OLE2 compound file, not a ZIP,
# and the office job reads ZIPs. The assertion is what it always was: a mime with
# no entry is NAMED as unextractable rather than routed to a job that would
# refuse it.
check("an unextractable mime is named, never attempted with the wrong tool",
      tool_for_mime("application/pdf") == "pdf-text"
      and tool_for_mime("application/zip") is None
      and tool_for_mime("application/msword") is None
      and tool_for_mime("APPLICATION/PDF; charset=x") == "pdf-text")
# A CLIP SUPPORTS TWO JOBS, and its DEFAULT is the transcript
# rather than the frames — the value order: "a clip's audio is more
# searchable than its frames, because someone hunting 'the demo where we talked
# about pricing' wants the transcript, not a keyframe." The frame job is still
# there, still the one whose answer is pictures rather than text, and still
# reachable by name; this pins both halves so neither can quietly disappear.
check("a clip supports both its words and its pictures, words first",
      tools_for_mime("video/mp4") == ("av-transcribe", "video-frames")
      and tools_for_mime("VIDEO/QUICKTIME; x=1")[0] == "av-transcribe")
check("and the frame job is not a text tool",
      "video-frames" in FRAME_TOOLS
      and "video-frames" in tools_for_mime("video/mp4"))
check("a scanned PDF with no text says so instead of reporting success emptily",
      "needs OCR" in extract_py)

# ── 5b. the transport, and the cap that belongs to it ──────────────────────
# The 8 MiB inline cap is a WebSocket FRAME limit, not a product decision. A box
# that can fetch the source is not subject to it, and the host must not hand a
# ticket to a box that cannot — an older bastion would refuse the job.
# The pull route's own authority is gated by scripts/test_bastion_context_pull.py,
# which drives it over real HTTP; this pins the host's side of the choice.
from server.routes.context_extract import machine_pulls, PULL_MAX_BYTES  # noqa: E402

check("a machine is only handed a ticket when it advertises the pull path",
      machine_pulls({}) is False
      and machine_pulls({"scan": {"supported": True, "found": 1}}) is False
      and machine_pulls({"scan": {"pull": True}}) is True)
check("the pull ceiling is well clear of what the platform lets a user upload "
      "(25 MiB), so the cap on a stored document stays the upload cap",
      PULL_MAX_BYTES == 64 * 1024 * 1024, PULL_MAX_BYTES)
check("an inline-only machine is told that updating it raises the ceiling, not "
      "merely that its document is too big",
      "bastion build that takes the" in flat(extract_py))
check("nothing is base64'd into a frame on the pull path",
      '"ticket": ticket' in extract_py and "context_tickets.mint" in extract_py)

# ── 6. the heartbeat actually STORES what the bastion advertises ────────────
# The heartbeat writer drops fields it has no column for, so the capability
# would arrive and the tool list would vanish.
# This used to check BOTH that topology.ensure_table created the columns and
# that the migration mirrored it. The runtime half is gone — schema is
# declared in migrations/ and nowhere else, so the kernel's database role can
# stop needing DDL rights — which makes the migration the single declaration
# rather than the mirror. The invariant is unchanged and is still checked here:
# the columns must exist, or the heartbeat writer silently drops what the box
# advertised. What is gone is the second copy, not the guarantee.
for piece, why in (
    ("ADD COLUMN IF NOT EXISTS prep_tools", "prep_tools column is declared"),
    ("ADD COLUMN IF NOT EXISTS prep_scan", "prep_scan column is declared"),
):
    check(f"the migration declares it ({why})", piece in migration)
check("prep_tools is parsed from the heartbeat body and bounded",
      "prep_tools_json" in topology_py and "prep_in[:32]" in topology_py)
check("prep_tools is written on insert AND kept on a beat that omits it",
      "COALESCE(EXCLUDED.prep_tools" in topology_py)
check("prep_tools/prep_scan are read back out to callers",
      '"prep_tools": _json_or' in topology_py and '"prep_scan": _json_or' in topology_py)
check("prep columns default to NULL, not '[]' (absence stays distinguishable)",
      "prep_tools JSONB;" in flat(migration).replace("  ", " ")
      or "ADD COLUMN IF NOT EXISTS prep_tools JSONB" in migration)
check("the migration does not DEFAULT the prep columns to an empty list",
      "prep_tools JSONB NOT NULL DEFAULT" not in migration)

# ── 6b. prep_scan is whitelisted, not stored as whatever arrived ───────────
# It travels: heartbeat body -> JSONB -> list_bastions -> machines_for emits it
# verbatim as "scan" -> the /machines route AND the extract_machines tool result,
# i.e. the model's context. The comment beside it claimed "bounded and
# shape-checked" while it stored any dict of any size.
check("prep_scan is whitelisted field by field, like scheduler_scan — including "
      "the pull flag, which decides whether a machine is handed a ticket",
      '"supported": bool(scan_in.get("supported"))' in topology_py
      and '"probed"' in topology_py and '"found"' in topology_py
      and '"pull": bool(scan_in.get("pull"))' in topology_py)
check("prep_scan does not store the raw dict",
      "json.dumps(prep_scan_in)" not in topology_py)

# ── 6c. extraction QUEUES the embedding, it does not merely claim it ───────
# The rows qualify for the text lane the moment they are written, but nothing
# embeds them until a job runs — so "the embedding job picks it up" was true
# only for a user who happened to run one.
check("a fill is queued for the rows extraction just wrote",
      "enqueue_job" in extract_py and '"ids": written' in extract_py)
check("and the result says which job, or carries the REAL reason none was queued",
      "embed_job_id" in extract_py and "embed_not_queued" in extract_py
      and "is not embedded yet because" in extract_py)

# ── 6d. a DB error is not reported as "you have no machines" ───────────────
check("machines_for re-raises instead of swallowing every error as []",
      "raise" in extract_py and "could not check which of your machines" in extract_py)

# ── 6e. replacing an extraction is ATOMIC, and says what it displaced ──────
# DELETE + N INSERTs as separate statements left the previous extraction gone
# and the new one half-written when anything failed mid-loop — worse than either
# end state. A promoted chunk is counted, not silently dropped: the memory it
# was promoted into survives but its link back here does not.
check("the replace runs in one transaction",
      "async with conn.transaction():" in extract_py)
check("promoted chunks are counted and reported, not silently removed",
      "status = 'promoted'" in extract_py and "replaced_promoted" in extract_py)

# ── 6f. reading a stored document returns TEXT, never its base64 ───────────
# The batch resolution in the read op only sees rows carrying an upload_batch;
# extracted rows carry none, so it resolved to the original file row — and
# list_context_service selects content raw with no file-kind guard. The host
# asked to read a document and got the document's encoding.
tools_py = read("brain/mastercode/runtime/tools.py")
check("read prefers extracted text over the stored bytes",
      "EXTRACTED TEXT WINS OVER THE STORED BYTES" in tools_py
      and 'source="extracted" if extracted_note else None' in tools_py)
# The wording moved to context_extract.no_reader_message, which
# names a machine route only when one exists (test_stop_the_probing.py).
check("and a document with no extraction is refused, not dumped as base64",
      "no_reader_message as _no_reader" in tools_py
      and "the host has no reader for that type" in flat(extract_py))
# A type the HOST can read (PDF, Office, text) is read right there
# by the host's own reader and the read continues on its text — one call, no
# agent. Only a type it cannot read is sent back as a next step.
check("a stored document the host can read is read in the same call",
      "_reader_for(mime)" in tools_py and "extract_context_file as _read_file" in tools_py
      and "was just read by the host" in flat(tools_py))
check("extraction reads in the host first, and asks a machine only for what it cannot read",
      "_read_in_host(" in extract_py and "HOST_TOOL" in extract_py
      and "so one of your machines was asked" in flat(extract_py))
check("an upload the host can read is read the moment it lands, with the upload's tags",
      "extract_context_file(pool, eid, row_id, data=raw," in read("server/routes/context.py")
      and "tags=list(row[\"tags\"] or [])" in extract_py
      and "ctitle, tags, json.dumps(cmeta)" in extract_py)

check("text filed before batch stamps (a machine's OCR, a transcript) stays readable, "
      "scoped to its own upload through the file row it hangs off",
      "legacy_parent" in tools_py and "parent_batch=legacy_parent" in tools_py
      and "media_parent_id IN (SELECT f.id FROM user_context f" in read("server/routes/context.py"))

# ── 7. the route is actually mounted ────────────────────────────────────────
check("the extraction router is registered in main",
      "context_extract_routes.router" in main_py)

print()
if failures:
    print(f"FAIL: {len(failures)} of {checks} check(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"PASS: {checks} checks — the extraction lane's contracts hold")
