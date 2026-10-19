#!/usr/bin/env python3
"""Gates for office documents — the host half.

docx, xlsx and pptx are the last of the big filename-only formats. What this
lane decides on the host is small — which mime goes to which job, and what a
user is told when the answer is empty or the job is unavailable — so these
checks are about the SENTENCES, which is where this lane can lie.

THE EXTRACTION ITSELF IS PROVEN WHERE IT RUNS: the gateway's
client-go/contextoffice_test.go drives real LibreOffice-produced docx, xlsx and
pptx through a real ctx_extract management frame, including six overlapping
extractions, and asserts the words come back.

WHY THERE IS NO LIBREOFFICE ANYWHERE IN THIS, and why the gate that forbids it
is not being worked around: soffice's headless conversion was installed and
measured before the implementation was chosen, and it loses one of
two SIMULTANEOUS conversions against one user profile in 5 of 5 trials — exit 1,
no output file, and nothing on stdout or stderr. It also has no text export
filter for Calc or Impress at all, and costs the user a 332 MB install. The box
reads these formats with Go's own zip and xml packages instead, so the user
installs NOTHING. contextoffice.go's header carries every measurement.

Run: APP_ROOT=<repo> python3 scripts/test_context_office_lane.py
"""
import asyncio
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
    print(("  ok    " if cond else "  FAIL  ") + label +
          (("\n        " + str(extra)) if extra and not cond else ""))
    if not cond:
        failures.append(label)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


extract_py = read("server/routes/context_extract.py")

from server.routes import context_extract as ce  # noqa: E402

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

print("\n── 1. the three OOXML formats route to the job that reads them ──")

for name, mime in (("docx", DOCX), ("xlsx", XLSX), ("pptx", PPTX)):
    check(f"a {name} routes to office-text",
          ce.tool_for_mime(mime) == "office-text", ce.tool_for_mime(mime))

check("a charset parameter on the mime does not defeat the lookup",
      ce.tool_for_mime(DOCX + "; charset=binary") == "office-text")

# THE LEGACY BINARY FORMATS ARE NOT MAPPED, and that is a decision rather than an
# oversight. A .doc is an OLE2 compound file, not a ZIP; the box reads ZIPs.
# Mapping them here would route the file to a job that would refuse it, which is
# the same mistake mapping HEIC to the OCR engine would have been.
for name, mime in (("doc", "application/msword"),
                   ("xls", "application/vnd.ms-excel"),
                   ("ppt", "application/vnd.ms-powerpoint")):
    check(f"the LEGACY binary .{name} is NOT mapped — it is an OLE2 file, not a "
          f"ZIP, and nothing on the box reads one",
          ce.tool_for_mime(mime) is None, ce.tool_for_mime(mime))

check("an unmapped type is still told so plainly rather than guessed at",
      ce.tool_for_mime("application/zip") is None)

print("\n── 2. an empty office document is not described as a scan ──")


class _FakeMachines:
    def __init__(self, machines):
        self.machines = machines

    async def __call__(self, pool, entity_id, tool=None):
        if tool:
            return [m for m in self.machines if tool in (m.get("tools") or [])]
        return self.machines


def note(payload, tool, machines, server="laptop"):
    real = ce.machines_for
    ce.machines_for = _FakeMachines(machines)
    try:
        return asyncio.run(ce._no_text_note(None, 1, tool, payload, {"server": server}))
    finally:
        ce.machines_for = real


BOX = [{"server": "laptop", "tools": ["office-text", "pdf-text"],
        "scan": {"supported": True, "probed": 4, "found": 2, "blocked": []}}]

office_note = note({"chars": 0}, "office-text", BOX)
print("        " + office_note)

# THE FALL-THROUGH WAS REAL. Before this branch existed, every tool that was not
# the OCR engine landed on the scanned-PDF explanation, so an empty spreadsheet
# came back as "it is a scan, so the words on it are a picture" — sending a user
# to install an OCR engine that would find exactly the same nothing.
check("an empty office document is NOT called a scan",
      "scan" not in office_note.lower(), office_note)
check("and is not sent to install an OCR engine for it",
      "tesseract" not in office_note.lower(), office_note)
check("it says the document was opened and there was nothing in it",
      "no text in it" in office_note, office_note)
check("and names the machine that looked, so 'nothing found' is attributable "
      "to a box that actually read it",
      "laptop" in office_note, office_note)

# The OCR path must be UNCHANGED by all of this.
scan_note = note({"pages": 0}, "pdf-text", BOX)
check("a text-layer read with no text is STILL explained as a scan",
      "scan" in scan_note.lower(), scan_note)

print("\n── 3. a job that needs no install is not described as one ──")

check("office-text is declared a builtin on this host",
      "office-text" in ce.BUILTIN_TOOLS)
check("the 'nobody can do this' message branches on that",
      "BUILTIN_TOOLS" in extract_py and "update the bastion" in extract_py)
# "install the tool and it appears on the next check-in" is true of poppler and
# tesseract and FALSE of a job built into the bastion. A user who went looking
# for the package would not find one.
m = re.search(r"how = \((.{0,400}?)\)\n", extract_py, re.S)
check("and the builtin branch tells the user to UPDATE rather than install",
      bool(m) and "update the bastion" in m.group(1) and "installed" in m.group(1),
      m.group(1) if m else "the branch was not found")

print("\n── 4. nothing here extracts anything platform-side ──")

# The premise of the whole ticket. This file names three office mime types and
# must never name a library that opens one.
BANNED = ("python-docx", "openpyxl", "python-pptx", "libreoffice", "soffice",
          "unoconv", "olefile", "xlrd")
found = [b for b in BANNED if b in extract_py.lower()]
check("the host route names no office library — it routes, the box reads",
      not found, found)
check("and the standing gate that enforces that across every image still passes",
      os.system(f"cd {ROOT} && python3 scripts/check_no_platform_extractors.py "
                f">/dev/null 2>&1") == 0)

print()
if failures:
    print(f"FAIL: {len(failures)} of {checks} check(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"PASS: {checks} checks — docx/xlsx/pptx reach the job that reads them, "
      f"an empty one is not called a scan, and a builtin is not called an install")
