"""Gates for the host's OWN readers — a file uploaded to a Host is read BY the
Host, as a plain function, the moment it lands (2026-09-22).

A file a user uploads directly to a Host belongs there and is processed there,
by a *read* function. Before this, a PDF
upload stopped at "stored", the only extractor was a job on one of the user's
machines, and context_ingest op='extract' was routed to a DISPATCHED AGENT —
so opening a PDF cost a model call and still failed when no machine had
poppler.

WHAT THIS FILE PINS, by running the real code on real bytes:

  1. A PDF / Office file is read in the host: rows written, NO machine job sent.
  2. The rows carry the SOURCE row's tags — an upload into a project is tagged
     project:<id>, and so is every piece of its text.
  3. A file the host reads with no text (a scan) is handed to a machine that
     can OCR it, and the answer says so; with no such machine the reason names
     both halves.
  4. The upload itself reads the file, from the bytes it already holds.
  5. A hostile file is an error sentence, never an exception.
  6. Neither the upload nor op='read' starts a machine job; only op='extract'.
  7. A re-upload replaces the prior copy of the same name, file and text, and
     text rows carry their upload's batch so one read never mixes versions.

Run: APP_ROOT=<repo> python3 scripts/test_host_read_lane.py
"""
import asyncio
import base64
import io
import json as _json
import os
import sys
import types
import zipfile
import zlib

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "brain"))

failures = []
checks = 0


def check(label, cond, extra=""):
    global checks
    checks += 1
    print(("  ok    " if cond else "  FAIL  ") + label +
          (("\n        " + str(extra)) if extra and not cond else ""))
    if not cond:
        failures.append(label)


# ── fixtures: real files, built here ────────────────────────────────────────

def _pdf(content: bytes) -> bytes:
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Count 1 /Kids [4 0 R] >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>"]
    raw = zlib.compress(content)
    out = b"%PDF-1.4\n"
    for i, o in enumerate(objs, 1):
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    out += (b"5 0 obj\n<< /Filter /FlateDecode /Length %d >>\nstream\n" % len(raw)
            + raw + b"\nendstream\nendobj\ntrailer << /Root 1 0 R >>\n%%EOF\n")
    return out


PDF_TEXT = _pdf(b"BT /F1 12 Tf 72 700 Td (Quiz Results) Tj 0 -14 Td (Score: 9 of 10 caf\\351) Tj ET")
PDF_SCAN = _pdf(b"q 100 0 0 100 0 0 cm /Im1 Do Q")      # pictures only, no text


def _docx(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml",
                   '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>")
    return buf.getvalue()


# ── fakes: the store and the machine lane ───────────────────────────────────

from server.routes import context_extract as ce  # noqa: E402

ENTITY, CTX_ID = 7, 500
OCR_MACHINE = {"server": "laptop.example", "token_id": 11, "tools": ["pdf-text", "pdf-ocr"],
               "prep_tools": [], "pull": False, "max_bytes": ce.MAX_SOURCE_BYTES}


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

    async def fetchval(self, sql, *args):      # the upload's INSERT
        return CTX_ID

    async def fetch(self, sql, *args):         # prior copies' tags
        self.sink.setdefault("fetched", []).append((sql, args))
        return [{"t": "collection:old"}]

    async def execute(self, sql, *args):
        self.sink.setdefault("executed", []).append((sql, args))
        return "UPDATE 3"

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

        async def dispatch(self, tid, op, args, wait, approval=""):  # (+ approval since 0.33.0)
            sink["dispatched"] = _json.loads(args[0])
            return (_json.dumps({"text": "OCR WORDS", "pages": 1, "pages_ocr": 1,
                                 "lang": "eng"}), "", 0)
    sys.modules.setdefault("server.main", types.ModuleType("server.main"))
    nm = types.ModuleType("server.network.nac_mgmt")
    nm.get_mgmt_relay = lambda: _Relay()
    # The signed-grant gate (rung 3, 2026-09-24): these fixtures grant every
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
        sink["embed_ids"] = (k.get("metadata") or {}).get("ids")
        return {"id": 42}
    emb.enqueue_job = _enqueue
    sys.modules["server.routes.embed"] = emb

    async def _machines(p, eid, t=None):
        return [dict(m) for m in machines if t is None or t in m["tools"]]
    return _machines


async def _drive(data, mime, *, tags=("project:12",), machines=(), tool=None, stored=True,
                 fname="Quiz Results - Print Friendly.pdf"):
    sink = {"rows": [], "dispatched": None}
    row = {"id": CTX_ID, "title": fname,
           "content": base64.b64encode(data).decode() if stored else "",
           "storage_backend": "pg", "tags": list(tags),
           "metadata": _json.dumps({"kind": "file", "mime": mime, "from_file": fname,
                                    "size_bytes": len(data), "upload_batch": "b-new"})}
    real = ce.machines_for
    ce.machines_for = _stub_lanes(sink, machines)
    try:
        res = await ce.extract_context_file(_Pool(sink, row), ENTITY, CTX_ID, tool=tool,
                                            data=None if stored else data)
    finally:
        ce.machines_for = real
    return res, sink


print("host reads a PDF itself")
res, sink = asyncio.run(_drive(PDF_TEXT, "application/pdf"))
check("the PDF is read and filed", res.get("ok") and res.get("chunks") == 1, res)
check("by the host, with no machine job sent",
      res.get("server") == "host" and res.get("tool") == ce.HOST_TOOL
      and sink["dispatched"] is None, (res.get("server"), sink["dispatched"]))
text = sink["rows"][0][1] if sink["rows"] else ""
check("the text is the document's words (WinAnsi decoded)",
      "Quiz Results" in text and "Score: 9 of 10 café" in text, text)
check("every row carries the upload's project tag",
      all(r[5] == ["project:12"] for r in sink["rows"]), [r[5] for r in sink["rows"]])
meta = _json.loads(sink["rows"][0][6]) if sink["rows"] else {}
check("and records what read it", meta.get("extracted_by") == ce.HOST_TOOL
      and meta.get("extracted_on") == "host", meta)
check("and which upload it belongs to — a read of one upload never mixes two",
      meta.get("upload_batch") == "b-new", meta)
check("an embed job is queued for exactly those rows",
      sink.get("embed_ids") == res.get("row_ids") and res.get("embed_job_id") == 42, sink)

print("\nthe upload's own bytes are read, nothing re-fetched")
res, sink = asyncio.run(_drive(PDF_TEXT, "application/pdf", stored=False))
check("read from `data` with an empty stored row", res.get("ok") and res.get("chunks") == 1, res)

print("\nOffice")
res, sink = asyncio.run(_drive(_docx("Quarterly plan"),
                               "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                               fname="plan.docx"))
check("a .docx is read in the host", res.get("ok") and res.get("reader") == "docx"
      and "Quarterly plan" in (sink["rows"][0][1] if sink["rows"] else ""), res)

print("\na scan: the host has no text, a machine with OCR is asked")
res, sink = asyncio.run(_drive(PDF_SCAN, "application/pdf", machines=[OCR_MACHINE]))
check("the machine's OCR text is filed", res.get("ok") and res.get("server") == "laptop.example"
      and sink["dispatched"] and sink["dispatched"]["tool"] == "pdf-ocr", (res, sink["dispatched"]))
check("and the answer says why a machine was asked",
      "so one of your machines was asked" in (res.get("note") or ""), res.get("note"))
check("OCR rows keep the project tag too", all(r[5] == ["project:12"] for r in sink["rows"]))
res, sink = asyncio.run(_drive(PDF_SCAN, "application/pdf", machines=[]))
check("with no machine, the reason names the scan AND the missing machine",
      "no text layer" in (res.get("error") or "")
      and "machine" in (res.get("error") or "").split("asked.", 1)[-1], res.get("error"))

check("a scan named for the host reader only is NOT sent to a machine",
      asyncio.run(_drive(PDF_SCAN, "application/pdf", machines=[OCR_MACHINE],
                         tool=ce.HOST_TOOL))[1]["dispatched"] is None)
res, sink = asyncio.run(_drive(b"PK\x03\x04 not really a zip",
                               "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                               machines=[dict(OCR_MACHINE, tools=["office-text"])], fname="bad.docx"))
check("a broken Office file is not re-tried on a machine (it would refuse it the same way)",
      res.get("error") and sink["dispatched"] is None, (res, sink["dispatched"]))

print("\na file that reads but holds no text replaces old text, it does not leave it")
res, sink = asyncio.run(_drive(_docx(""),
                               "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                               fname="blank.docx"))
check("an empty read still runs the replace and queues nothing",
      res.get("ok") and res.get("chunks") == 0 and "embed_ids" not in sink
      and "no text" in (res.get("note") or ""), res)
# A PDF with no text layer is different: it is Unreadable, and text an earlier
# OCR run filed for this same file row must survive (it is the only text).

print("\nnaming the host's reader")
res, sink = asyncio.run(_drive(b"\x89PNG....", "image/png", tool=ce.HOST_TOOL, fname="p.png"))
check("a picture is not something the host reads — said, not attempted",
      res.get("error") and sink["rows"] == [], res)

print("\nhostile bytes")
for junk in (b"%PDF-1.4\n" + b"[" * 50000, b"%PDF-1.4\n1 0 obj << /Kids 5 >> endobj", os.urandom(4096)):
    try:
        res, sink = asyncio.run(_drive(junk, "application/pdf"))
        check("a malformed PDF is an error sentence, never an exception",
              isinstance(res.get("error"), str) and sink["rows"] == [], res)
    except Exception as e:  # noqa: BLE001
        check("a malformed PDF is an error sentence, never an exception", False, repr(e))

print("\nthe upload reads what it can, with its tags")
from server.routes import context as ctx  # noqa: E402
sink = {"rows": [], "dispatched": None}
row = {"id": CTX_ID, "title": "q.pdf", "content": "", "storage_backend": "pg",
       "tags": ["project:12"], "metadata": _json.dumps({"kind": "file", "mime": "application/pdf",
                                                          "from_file": "q.pdf", "upload_batch": "b1"})}
cb = types.ModuleType("brain.mastercode.context_backends")


async def _store(*a, **k):
    return "pg"
cb.store_user_context_content = _store
sys.modules["brain.mastercode.context_backends"] = cb
me = types.ModuleType("mastercode.media_engine")
me.media_kind = lambda m: "document"


async def _mc(*a):
    return None
me.media_config = _mc
sys.modules["mastercode.media_engine"] = me
real_mf = ce.machines_for
ce.machines_for = _stub_lanes(sink, [OCR_MACHINE])      # a machine IS online
up = asyncio.run(ctx._ingest_binary_file(_Pool(sink, row), ENTITY, PDF_TEXT, "q.pdf",
                                         "application/pdf", ["project:12"], "b1"))
check("the upload answer carries the read", (up.get("read") or {}).get("ok")
      and (up.get("read") or {}).get("chunks") == 1, up.get("read"))
check("and the file itself is still stored as a file", up.get("kind") == "file" and up.get("id") == CTX_ID)
scan = asyncio.run(ctx._ingest_binary_file(_Pool(sink, dict(row, metadata=row["metadata"])), ENTITY,
                                           PDF_SCAN, "q.pdf", "application/pdf", ["project:12"], "b1"))
check("an upload never starts a machine job, even with an OCR machine online",
      sink["dispatched"] is None and (scan.get("read") or {}).get("error"), scan.get("read"))
ce.machines_for = real_mf
sup = [e for e in sink.get("executed", []) if "status = 'dismissed'" in e[0]]
check("a re-upload supersedes prior copies of the same name — file AND its text",
      sup and sup[0][1][1] == "q.pdf" and sup[0][1][2] == "b1"
      and up.get("superseded") == 6, (sup, up.get("superseded")))
legacy = [e for e in sink.get("executed", []) if "media_parent_id IN (SELECT f.id" in e[0]]
check("unstamped (legacy) text is judged by its FILE's scope and goes with that file",
      legacy and "AND NOT (source = 'extracted' AND metadata->>'upload_batch' IS NULL)" in sup[0][0],
      legacy)
check("only inside the upload's own project (another project's spec.pdf is untouched)",
      sup and sup[0][1][3] == ["project:12"] and "tags && $4::text[]" in sup[0][0], sup)
carried = [e for e in sink.get("executed", []) if "unnest(tags || $1" in e[0]]
check("and hands their collections to the new file and its text rows",
      carried and CTX_ID in carried[0][1][1] and len(carried[0][1][1]) >= 2, carried)

print()
if failures:
    print(f"FAIL: {len(failures)} of {checks} check(s)")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print(f"PASS: {checks} checks — the host reads what it can itself")
