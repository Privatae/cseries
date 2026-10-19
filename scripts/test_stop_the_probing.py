#!/usr/bin/env python3
"""test_stop_the_probing.py — a failure is reported, never routed around.

What happened (06:23 UTC): asked to import a .tickets.gz, the host's
read errored, the error told it to unpack the file "on one of the user's
machines" (no machine could — nothing unpacks gzip), the runtime then said
"do NOT give up", and the host probed a machine, then another lane. Three
pieces close it, and this file replays the turn against the real code:

  1. An archive (gzip / zip / tar) is read by the host's OWN reader, as a
     read — so this case never reaches for a machine at all.
  2. The read error for a type the host cannot read names a machine route
     ONLY when the type has a job AND one of the user's machines is online
     for it; otherwise it says there is nothing to try.
  3. The failure nudge: a refusal is reported as it stands and never retried
     by another machine, lane, grant or tool; "do NOT give up" is gone.

Container-free: real readers on real bytes, stub pools only.
Run: APP_ROOT=<repo> python3 scripts/test_stop_the_probing.py
"""
from __future__ import annotations

import asyncio
import base64
import gzip
import io
import json
import os
import sys
import tarfile
import types
import zipfile
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

failures: list[str] = []


def check(name: str, cond: bool, detail="") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ── 3. the nudge ─────────────────────────────────────────────────────────
from mastercode.runtime.tool_failures import note_round, is_refusal, FAILED_HEAD  # noqa: E402

print("the failure nudge")
REFUSED = ("refused: op 'ctx_extract' is not permitted on this host — no signed grant (default-deny)")
r = note_round({}, [{"tool": "context_ingest", "result_status": "error", "error_detail": REFUSED}])
check("a 'refused: …' error is a refusal even with status=error", is_refusal("error", REFUSED))
check("a refusal is reported, not worked around",
      "WAS REFUSED" in r["message"] and "Do not try another machine, lane, grant, tool" in r["message"], r["message"])
check("the refusal carries the tool's own words", REFUSED in r["message"])
for bad in ("do NOT give up", "MUST try a different approach", "Try a different approach", "Recovery strategies"):
    check(f"the nudge never says {bad!r}", bad not in r["message"], r["message"])
d = note_round({}, [{"tool": "mgmt_run", "result_status": "denied", "error_detail": "no grant"}])
check("status=denied is a refusal", "WAS REFUSED" in d["message"])
b = note_round({}, [{"tool": "mgmt_run", "result_status": "blocked", "error_detail": "quarantined"}])
check("status=blocked is a refusal", "WAS REFUSED" in b["message"])
t = note_round({}, [{"tool": "research", "result_status": "error", "error_detail": "timeout after 30s"}])
check("a plain error may be retried only when the error says so",
      "Retry only if the error itself says a retry can succeed" in t["message"] and "WAS REFUSED" not in t["message"])
check("the header asks for the tool's own words, and nothing more",
      t["message"].startswith(FAILED_HEAD) and "give up" not in FAILED_HEAD)
m = note_round({}, [{"tool": "memory", "result_status": "error", "error_detail": "boom"}])
check("tool-specific hints survive for plain errors", any(h.startswith("MEMORY RECOVERY") for h in m["hints"]))
src_tf = (ROOT / "brain/mastercode/runtime/tool_failures.py").read_text()
check("tool_failures.py: 'do NOT give up' exists only in the record of its removal",
      src_tf.count("do NOT give up") == 1 and "used to open with" in src_tf)

# ── 1. archives are read by the host ─────────────────────────────────────
from server.readers import docread  # noqa: E402

print("\narchives")
TICKETS = json.dumps({"format": "tickets/1", "tickets": [{"key": "T-1", "title": "the gzip case"}]}).encode()


def _gz(data: bytes, name: str = "") -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", filename=name, mtime=0) as g:
        g.write(data)
    return buf.getvalue()


def _tar(members: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tf:
        for name, body in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tf.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def _zip(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, body in members.items():
            z.writestr(name, body)
    return buf.getvalue()


def _docx(text: str) -> bytes:
    return _zip({"[Content_Types].xml": b"<Types/>",
                 "word/document.xml": ('<w:document xmlns:w="http://schemas.openxmlformats.org/'
                                       f'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}'
                                       '</w:t></w:r></w:p></w:body></w:document>').encode()})


for mime in ("application/x-gzip", "application/gzip", "application/zip", "application/x-tar"):
    check(f"{mime} has a host reader", docread.reader_for(mime) == "archive")
name, text = docread.read(_gz(TICKETS, "box-a-ledger.tickets"), "application/x-gzip")
check("a gzip'd ticket bundle reads as its JSON (the 06:23 file)", name == "archive" and json.loads(text)["format"] == "tickets/1", text[:80])
name, text = docread.read(_gz(TICKETS), "application/gzip")
check("a gzip with no recorded name still reads: the bytes are sniffed as text", json.loads(text)["format"] == "tickets/1")
name, text = docread.read(_gz(_tar({"notes/a.md": b"# alpha\n", "bin/x": b"\x00\x01\x02"})), "application/gzip")
check("a .tar.gz reads as its members, each under its name",
      "=== notes/a.md ===" in text and "# alpha" in text and "bin/x" in text and "binary, not read" in text, text)
name, text = docread.read(_tar({"a.txt": b"one\n", "b.json": b'{"k": 1}'}), "application/x-tar")
check("a plain tar reads both members", "one" in text and '"k": 1' in text, text)
name, text = docread.read(_zip({"plan.docx": _docx("Quarterly plan"), "inner.zip": _zip({"x": b"y"}), "r.csv": b"a,b\n1,2\n"}),
                          "application/zip")
check("a zip's Office member is read with the Office reader", "Quarterly plan" in text, text)
check("a zip's csv member is read", "a\tb" in text, text)
check("an archive inside an archive is named, not opened", "an archive inside this one" in text and "=== x ===" not in text, text)
for junk, why in ((b"\x1f\x8b" + os.urandom(64), "random bytes with a gzip header"),
                  (_gz(TICKETS)[:-8], "a truncated gzip"),
                  (os.urandom(256), "random bytes"),
                  (_gz(os.urandom(4096)), "a gzip holding a binary file")):
    try:
        docread.read(junk, "application/gzip")
        check(f"{why} is an error sentence", False, "read returned")
    except docread.Unreadable as e:
        check(f"{why} is an error sentence", str(e).startswith("archive:"), str(e))
    except Exception as e:  # noqa: BLE001
        check(f"{why} is an error sentence, never an exception", False, repr(e))
bomb = _gz(b"\x00" * (docread.ARCHIVE_TOTAL_CAP + 1024))
try:
    docread.read(bomb, "application/gzip")
    check("a gzip bomb is refused at the cap", False, "read returned")
except docread.Unreadable as e:
    check("a gzip bomb is refused at the cap, and says so", "unpacks to more than" in str(e), str(e))
many = _tar({f"f{i}.txt": b"x" for i in range(docread.ARCHIVE_MEMBERS_MAX + 5)})
_, text = docread.read(many, "application/x-tar")
check("too many members: what was read is kept, the cut is said",
      "=== f0.txt ===" in text and "more than %d members" % docread.ARCHIVE_MEMBERS_MAX in text, text[-120:])

# A zip whose central directory claims huge members (nothing
# behind them) used to bypass the member count AND the text cap — 400,000
# entries, 30 M characters, in 0.7 s. Every named member counts now.
def _zip_claiming(n: int, size: int) -> bytes:
    raw = bytearray(_zip({f"e{i}": b"" for i in range(n)}))
    at = 0
    while True:
        at = raw.find(b"PK\x01\x02", at)
        if at == -1:
            break
        raw[at + 24:at + 28] = size.to_bytes(4, "little")   # uncompressed size
        at += 4
    return bytes(raw)


_, text = docread.read(_zip_claiming(docread.ARCHIVE_MEMBERS_MAX + 50, 0xFFFFFFF0), "application/zip")
check("oversized zip entries are named, counted, and cut at the member cap",
      text.count("larger than one member may be") == docread.ARCHIVE_MEMBERS_MAX
      and "more than %d members" % docread.ARCHIVE_MEMBERS_MAX in text, (text.count("larger"), text[-100:]))
check("the archive's text is under its cap", len(text) <= docread.ARCHIVE_TEXT_CAP + 200)

# No time bound covered the whole archive — every Office or
# PDF member got its own fresh 60 s (measured: five no-text docx, 97 s). The
# archive now sets one deadline every member reader inherits.
import time as _time  # noqa: E402
_real_secs, _real_readers = docread.ARCHIVE_SECONDS, dict(docread.READERS)


def _slow(data):
    _time.sleep(0.08)
    return "slow text"


docread.ARCHIVE_SECONDS = 0.1
docread.READERS["text/plain"] = ("text", _slow)
try:
    _, text = docread.read(_tar({f"s{i}.txt": b"x" for i in range(10)}), "application/x-tar")
finally:
    docread.ARCHIVE_SECONDS = _real_secs
    docread.READERS.clear()
    docread.READERS.update(_real_readers)
check("an archive stops at ITS deadline with what it read, and says so",
      "=== s0.txt ===" in text and "took longer than" in text and text.count("slow text") < 10, text[-160:])
_seen_budget = []
_real_extract = docread._pdftext.extract_text


def _capture(data, budget=None):
    _seen_budget.append(budget)
    return None


docread._pdftext.extract_text = _capture
try:
    with docread._ArchiveText() as _acc:
        _ob = docread._OfficeBudget()
        check("an Office member's budget is cut to the archive's deadline", _ob.deadline <= _acc.deadline)
        docread.extract_pdf_builtin(b"%PDF-1.4 whatever")
        check("a PDF member is read under a budget cut to the archive's deadline (the real call)",
              _seen_budget and _seen_budget[-1] is not None and _seen_budget[-1].deadline <= _acc.deadline)
    docread.extract_pdf_builtin(b"%PDF-1.4 whatever")
    check("outside an archive a PDF keeps its own budget", _seen_budget[-1] is None)
finally:
    docread._pdftext.extract_text = _real_extract
check("the outer deadline is cleared after the archive", docread._outer_deadline() is None)
try:
    with docread._ArchiveText():
        raise RuntimeError("boom")
except RuntimeError:
    pass
check("… and after an exception inside it", docread._outer_deadline() is None)

# The cut INSIDE one document, not only between members: a docx of many
# paragraphs, read as a member under a tiny archive deadline, stops partway
# with the Office reader's own sentence.
_big_docx = _zip({"[Content_Types].xml": b"<Types/>",
                  "word/document.xml": (b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                                        + b"<w:p><w:r><w:t>para</w:t></w:r></w:p>" * 200_000
                                        + b"</w:body></w:document>")})
docread.ARCHIVE_SECONDS = 0.02
try:
    _, text = docread.read(_zip({"big.docx": _big_docx}), "application/zip")
finally:
    docread.ARCHIVE_SECONDS = _real_secs
check("a member document's OWN read is cut at the archive's deadline",
      "stopped reading here: the document took longer than" in text, text[-200:])

# A single-file gzip bypassed the text cap (96 KB → 66 M
# characters). It goes through the same accounting now, and inside an
# archive the linear readers (text/html/csv/rtf/eml) take at most
# ARCHIVE_UNBUDGETED_MEMBER_CAP.
_real_text_cap = docread.ARCHIVE_TEXT_CAP
docread.ARCHIVE_TEXT_CAP = 100
try:
    _, text = docread.read(_gz(b"x" * 10_000, "big.txt"), "application/gzip")
finally:
    docread.ARCHIVE_TEXT_CAP = _real_text_cap
check("a single-file gzip's text goes through the archive text cap", len(text) < 300 and "cut here" in text, len(text))
big_text = b"y" * (docread.ARCHIVE_UNBUDGETED_MEMBER_CAP + 1)
try:
    docread.read(_gz(big_text, "big.txt"), "application/gzip")
    check("a text member over the in-archive cap is refused, saying so", False, "read returned")
except docread.Unreadable as e:
    check("a text member over the in-archive cap is refused, saying so", "inside an archive" in str(e), str(e))
_, text = docread.read(_zip({"big.txt": big_text}), "application/zip")
check("… and inside a zip it is named, not read, and the zip still reads", "inside an archive" in text and "big.txt" in text, text[:200])
check("outside an archive the same text reads (the 64 MB bound is unchanged)",
      docread.read(big_text, "text/plain")[1].startswith("y"))
check("the outer deadline is cleared after a refused member", docread._outer_deadline() is None)
_ob2 = docread._OfficeBudget()
check("outside an archive an Office document keeps its own 60 s", _ob2.deadline > _time.monotonic() + docread.OFFICE_SECONDS - 1)

# A .tar.gz with no browser type is guessed application/x-tar
# by the upload; application/x-compressed-tar had no reader at all.
_, text = docread.read(_gz(_tar({"a.txt": b"tgz member"})), "application/x-tar")
check("a .tar.gz recorded as application/x-tar reads (the tar reader sniffs gzip)", "tgz member" in text, text)
_, text = docread.read(_gz(_tar({"a.txt": b"ctar member"})), "application/x-compressed-tar")
check("application/x-compressed-tar has a reader", "ctar member" in text, text)

print("\nrefusals that repeat, and refusals wrapped as results")
led: dict = {}
den = {"tool": "context_ingest", "result_status": "denied", "error_detail": "no signed grant (default-deny)"}
note_round(led, [den])
r2 = note_round(led, [den])
check("a refusal that repeats is 'refused again', never 'take another route'",
      "WAS REFUSED AGAIN" in r2["message"] and "take another route, or tell" not in r2["message"]
      and "do not take another route" in r2["message"], r2["message"])
from mastercode.runtime.tool_failures import DRAFT_GRANT_TAIL  # noqa: E402
ng = note_round({}, [{"tool": "mgmt_run", "result_status": "denied",
                      "error_detail": "op 'df' on 'web-1' not permitted — no signed grant (default-deny)"}])
check("a 'no signed grant' refusal may become a DRAFT for the user to approve — said as such",
      DRAFT_GRANT_TAIL in ng["message"] and "does nothing until they approve it" in ng["message"])
ud = note_round({}, [{"tool": "mgmt_run", "result_status": "denied",
                      "error_detail": "op 'df' on 'web-1' denied by your grant — you said: \"never df\""}])
check("a refusal naming the user's own deny gets no such offer", DRAFT_GRANT_TAIL not in ud["message"])
skill = (ROOT / "default-skills/network.md").read_text()
reg = (ROOT / "brain/mastercode/public_tool_registry.py").read_text()
check("network.md no longer says 'draft one first' after a refusal",
      "draft one first with" not in skill and "A refusal is reported to the user as it is" in skill)
check("the mgmt_run description says the same thing as the nudge",
      "only 'no signed grant' may become a draft" in reg and "draft one with propose_mgmt_grant. Returns" not in reg
      and "status 'denied' + the box's words" in reg)
src_sess = (ROOT / "brain/mastercode/runtime/session.py").read_text()
check("session: a lane refusal is not shown as a capability-wall denial (ceigas_denied needs violations)",
      '"violations" in tool_result or "capabilities_required" in tool_result' in src_sess)
from mastercode.runtime.tools import _refused_or_ok  # noqa: E402
w = _refused_or_ok({"server": "web-1", "op": "df", "exit": 126, "stdout": "",
                    "stderr": "op 'df' on 'web-1' not permitted — no signed grant (default-deny)"})
check("an exit-126 lane result is a refusal (status=denied) with the lane's own words",
      w["status"] == "denied" and "not permitted" in w["result"] and w["detail"]["exit"] == 126, w)
check("and the ledger reads it as one", "WAS REFUSED" in note_round({}, [{"tool": "mgmt_run", "result_status": w["status"], "error_detail": w["result"]}])["message"])
check("an ordinary lane result stays ok", _refused_or_ok({"exit": 0, "stdout": "x"})["status"] == "ok")
# The host's `session` tool is gone (test_gate_the_ungated.py),
# and with it the lane result that carried its own `refused` word; the mgmt
# lane's exit 126 is the one refusal shape left.
check("a refusal with no words still says what was refused",
      "refused: uptime on web-1" in _refused_or_ok({"server": "web-1", "op": "uptime", "exit": 126})["result"])
src_tools = (ROOT / "brain/mastercode/runtime/tools.py").read_text()
check("mgmt_run routes its result through the refusal check (the session tool is gone)",
      src_tools.count("return _refused_or_ok(res)") == 1)

print("\nthe imported bundle's text is retired")
from mastercode.runtime import ticket_import as _ti  # noqa: E402


class _RetirePool:
    def __init__(self):
        self.sql = []

    async def execute(self, sql, *args):
        self.sql.append((sql, args))
        return "UPDATE 3"


_rp = _RetirePool()
n = asyncio.run(_ti.retire_bundle_text(_rp, 3, "box-a-ledger.tickets.gz"))
check("retire_bundle_text dismisses the extracted rows hanging off the bundle file, by name, and counts them",
      n == 3 and "source = 'extracted'" in _rp.sql[0][0] and "media_parent_id IN" in _rp.sql[0][0]
      and _rp.sql[0][1] == (3, "box-a-ledger.tickets.gz"), _rp.sql)
check("the file row itself is never touched, and a piece the user promoted stays",
      "SET status = 'dismissed'" in _rp.sql[0][0] and "source = 'extracted'" in _rp.sql[0][0]
      and "status NOT IN ('dismissed', 'promoted')" in _rp.sql[0][0])


class _RaisingPool:
    async def execute(self, sql, *args):
        raise RuntimeError("pg went away")


res_ok = {"errors": []}
asyncio.run(_ti._retire_after_import(_RaisingPool(), 3, "b.gz", res_ok))
check("a failure to retire never fails the import: noted on the result, count 0",
      res_ok.get("bundle_text_retired") == 0 and "retiring it failed" in res_ok.get("note", ""), res_ok)
res_err = {"errors": [{"kind": "ticket"}]}
asyncio.run(_ti._retire_after_import(_rp, 3, "b.gz", res_err))
check("an import with errors keeps the bundle's text", "bundle_text_retired" not in res_err and len(_rp.sql) == 1)
src_ti = (ROOT / "brain/mastercode/runtime/ticket_import.py").read_text()
check("run() goes through _retire_after_import", "await _retire_after_import(pool, int(entity_id), fname, res)" in src_ti)

# ── the replay: the 06:23 read, through extract_context_file ─────────────
from server.routes import context_extract as ce  # noqa: E402

ENTITY, CTX_ID = 3, 480


class _Conn:
    def __init__(self, sink):
        self.sink = sink

    def transaction(self):
        conn = self

        class _T:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return _T()

    async def execute(self, sql, *args):
        return "DELETE 0"

    async def fetchval(self, sql, *args):
        if "count(*)" in sql:
            return 0
        self.sink["rows"].append(args)
        return 900 + len(self.sink["rows"])


class _Pool:
    def __init__(self, sink, row):
        self.sink, self.row = sink, row

    async def fetchrow(self, sql, *args):
        return self.row if "FROM user_context" in sql else None

    async def fetch(self, sql, *args):
        return []

    async def execute(self, sql, *args):
        return "UPDATE 0"

    def acquire(self):
        conn = _Conn(self.sink)

        class _A:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return _A()


def _stub_lanes(sink, machines):
    class _Relay:
        def worker_count(self, tid):
            return 1

        async def dispatch(self, tid, op, args, wait):
            sink["dispatched"] = json.loads(args[0])
            return (json.dumps({"text": "MACHINE TEXT", "pages": 1}), "", 0)
    sys.modules.setdefault("server.main", types.ModuleType("server.main"))
    nm = types.ModuleType("server.network.nac_mgmt")
    nm.get_mgmt_relay = lambda: _Relay()
    # The signed-grant gate: these fixtures grant every
    # machine; test_gate_the_ungated.py is where a missing grant refuses.
    async def _gate_servers(p, eid, op, servers):
        return {str(sv): (True, "", "ran") for sv in servers if sv}
    nm.gate_servers = _gate_servers
    async def _audit(*a, **k):
        sink.setdefault("audit", []).append((a[2:], k))
    nm.audit_mgmt = _audit
    sys.modules["server.network.nac_mgmt"] = nm
    emb = types.ModuleType("server.routes.embed")

    async def _enqueue(*a, **k):
        return {"id": 1}
    emb.enqueue_job = _enqueue
    sys.modules["server.routes.embed"] = emb

    async def _machines(p, eid, t=None):
        if machines == "raise":
            raise RuntimeError("db down")
        return [dict(m) for m in machines if t is None or t in m["tools"]]
    return _machines


async def _read(data, mime, fname, machines=()):
    sink = {"rows": [], "dispatched": None}
    row = {"id": CTX_ID, "title": fname, "content": base64.b64encode(data).decode(),
           "storage_backend": "pg", "tags": [],
           "metadata": json.dumps({"kind": "file", "mime": mime, "from_file": fname,
                                   "size_bytes": len(data), "upload_batch": "b1"})}
    real = ce.machines_for
    ce.machines_for = _stub_lanes(sink, machines)
    try:
        res = await ce.extract_context_file(_Pool(sink, row), ENTITY, CTX_ID, tool=ce.HOST_TOOL)
    finally:
        ce.machines_for = real
    return res, sink


print("\nthe replay")
res, sink = asyncio.run(_read(_gz(TICKETS, "box-a-ledger.tickets"), "application/x-gzip", "box-a-ledger.tickets.gz"))
check("the .tickets.gz is read by the host, rows filed, no machine touched",
      res.get("ok") and res.get("reader") == "archive" and res.get("chunks") and sink["dispatched"] is None, res)
check("its text is the bundle's JSON", any("tickets/1" in (r[1] or "") for r in sink["rows"]))

# ── 2. the read error for a type the host cannot read ────────────────────
print("\nthe read error")
OCR = {"server": "laptop.example", "token_id": 11, "tools": ["ocr", "pdf-text"], "prep_tools": [], "pull": False}


async def _msg(mime, machines):
    real = ce.machines_for
    ce.machines_for = _stub_lanes({"rows": [], "dispatched": None}, machines)
    try:
        return await ce.no_reader_message(object(), ENTITY, "scan.png", mime, 77)
    finally:
        ce.machines_for = real


m0 = asyncio.run(_msg("application/octet-stream", [OCR]))
check("a type nothing turns into text: says so, names no machine and no op='extract'",
      "Nothing on this platform turns" in m0 and "op='extract'" not in m0 and "laptop" not in m0, m0)
m1 = asyncio.run(_msg("image/png", []))
check("a type a machine could read, with no machine online: says that, no op='extract'",
      "no online machine offers one" in m1 and "op='extract'" not in m1, m1)
m2 = asyncio.run(_msg("image/png", [OCR]))
check("a type a machine could read, with one online: names it and the op",
      "laptop.example" in m2 and "op='extract' (context_id=77)" in m2 and "ocr" in m2, m2)
FRAMES = {"server": "desk.example", "token_id": 12, "tools": ["video-frames"], "prep_tools": [], "pull": False}
m4 = asyncio.run(_msg("video/mp4", [FRAMES]))
check("a type with two jobs: a machine offering only the second is still named, with that job",
      "desk.example" in m4 and "video-frames" in m4 and "op='extract'" in m4, m4)
m5 = asyncio.run(_msg("video/mp4", []))
check("with no machine for either job, both jobs are named", "av-transcribe or video-frames" in m5, m5)
m3 = asyncio.run(_msg("image/png", "raise"))
check("a failed machine check is said as that, never as 'no machine'",
      "could not check" in m3 and "no online machine" not in m3 and "op='extract'" not in m3, m3)
for m in (m0, m1, m2, m3):
    check("every wording says what the file is and that the host has no reader",
          "'scan.png' is a stored" in m and "no reader for that type" in m, m)
src_tools = (ROOT / "brain/mastercode/runtime/tools.py").read_text()
check("tools.py: the read path asks context_extract for the wording", "no_reader_message as _no_reader" in src_tools)
check("tools.py: the blanket 'op=extract_machines says which machines can' is gone",
      "op='extract_machines' says which machines can" not in src_tools)

print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
sys.exit(1 if failures else 0)
