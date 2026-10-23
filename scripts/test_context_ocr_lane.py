#!/usr/bin/env python3
"""Gates for OCR — the host half.

THE ONE OUTCOME THIS LANE MUST NEVER PRODUCE: a user must
never get a silent empty result for a document that visibly has text on it. A
scanned PDF is images wrapped in a PDF, so pdftotext SUCCEEDS and returns
nothing for one — a job that exits 0 and tells someone their contract has no
words in it.

So these checks are about what the host DECIDES and what it SAYS, and the three
answers it has to keep apart:

    the engine read every page and they are blank     -> nothing to fix
    the engine is installed and has no language data  -> install a language pack
    no machine of theirs has an engine at all         -> install tesseract + a pack

The OCR itself is proven where it runs, against the real binary through a real
management frame: the gateway's client-go/contextocr_test.go. What is
proven HERE is that a real scanned document reaches the right job and that a
real empty result is explained rather than reported.

When the extraction tools are on this machine, the fixtures below are BUILT with
them rather than embedded — a two-page image-only PDF whose text layer is
measured to be empty, which is the property that makes it a scan. Without them
the source-shape checks still run and the live ones skip, saying so.

Run: APP_ROOT=<repo> python3 scripts/test_context_ocr_lane.py
"""
import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
from tools_source import tools_source  # tools.py and the modules split out of it

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "brain"))

failures = []
skipped = []
checks = 0


def check(label, cond, extra=""):
    global checks
    checks += 1
    print(("  ok    " if cond else "  FAIL  ") + label +
          (("\n        " + str(extra)) if extra and not cond else ""))
    if not cond:
        failures.append(label)


def skip(label, why):
    skipped.append(label)
    print(f"  skip  {label}\n        {why}")


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def flat(s):
    return re.sub(r"\s+", " ", s)


extract_py = read("server/routes/context_extract.py")
topology_py = read("server/network/topology.py")
tools_py = tools_source()

from server.routes import context_extract as ce  # noqa: E402

print("\n── 1. an image routes to OCR, and a PDF routes to the job that reads a scan ──")

check("every image format the engine links against maps to a job",
      all(ce.tool_for_mime(m) == "ocr" for m in
          ("image/png", "image/jpeg", "image/tiff", "image/webp", "image/bmp", "image/gif")),
      {m: ce.tool_for_mime(m) for m in ("image/png", "image/jpeg", "image/tiff")})
check("a charset parameter on the mime does not defeat the lookup",
      ce.tool_for_mime("image/png; charset=binary") == "ocr")
check("HEIC is NOT mapped — nothing in the leptonica stack decodes it, and "
      "mapping it would route an iPhone photo to a job that refuses it",
      ce.tool_for_mime("image/heic") is None and ce.tool_for_mime("image/heif") is None)
check("a PDF still routes to the text-layer job by default, so a machine with "
      "poppler and no engine is not cut off",
      ce.tool_for_mime("application/pdf") == "pdf-text")
check("and the host asks for the job that ALSO reads scanned pages when a "
      "machine advertises it",
      ce.TOOL_UPGRADE.get("pdf-text") == "pdf-ocr")
check("the upgrade is per-machine and never narrows the answer — the upgraded "
      "list is used only when it is non-empty, and (2026-09-24) only when one of "
      "those machines holds the user's signed grant for the job — or nothing at all "
      "does the asked job, so the refusal names the machine and the missing grant",
      "if upgraded:" in extract_py and "if _any_granted(upgraded, up_gate) or not machines:" in extract_py
      and "tool, machines, gate = better, upgraded, up_gate" in extract_py)

print("\n── 2. the three answers stay three different sentences ──")


class _FakeMachines:
    """Stands in for machines_for so the note logic can be driven against every
    shape of fleet a user can actually have."""

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


ENGINE_ONLY = [{"server": "laptop", "tools": ["pdf-text"],
                "scan": {"supported": True, "probed": 3, "found": 1,
                         "blocked": [{"name": "ocr", "bin": "tesseract",
                                      "reason": "tesseract is installed but has no "
                                                "language data"}]}}]
NO_ENGINE = [{"server": "laptop", "tools": ["pdf-text"],
              "scan": {"supported": True, "probed": 3, "found": 1, "blocked": []}}]

blank = note({"pages": 12, "pages_ocr": 12}, "pdf-ocr", NO_ENGINE)
nolang = note({}, "pdf-text", ENGINE_ONLY)
noengine = note({}, "pdf-text", NO_ENGINE)

check("a BLANK scan reports the pages it read — the one answer only the box can "
      "give, and the one that must not read as a missing capability",
      "12" in blank and "blank" in blank.lower() and "install" not in blank.lower(),
      blank)
check("an engine with NO LANGUAGE DATA says so and does not tell the user to "
      "install what is already on their disk",
      "language data" in nolang.lower() and "IS installed" in nolang,
      nolang)
check("NO ENGINE AT ALL names both packages, because the language pack is a "
      "separate install on most systems and the engine alone reads nothing",
      "tesseract" in noengine and "separate packages" in noengine
      and "tesseract-ocr-eng" in noengine,
      noengine)
check("and the three are genuinely different text, not one sentence with a "
      "variable in it",
      len({blank, nolang, noengine}) == 3)
check("none of the three is the old 'not wired up yet' placeholder",
      "not wired up yet" not in extract_py)
check("a single-image OCR that finds nothing says the PICTURE has no words, "
      "not that the document is unreadable",
      "no text on it" in note({}, "ocr", NO_ENGINE).lower())

print("\n── 3. a partial read is never silent ──")

more = ce._partial_note({"pages": 30, "pages_more": True})
late = ce._partial_note({"pages": 7, "deadline": True})
whole = ce._partial_note({"pages": 3})

check("a document longer than one job reads says the rest is NOT in the corpus "
      "— silently filing 30 pages of a 44-page contract is the same failure as "
      "filing none of it",
      "30" in more and "NOT in your corpus" in more, more)
check("a run that hit the clock says how much it finished",
      "7" in late and "ran out of time" in late, late)
unread = ce._partial_note({"pages": 9, "pages_unread": 2})
check("a page the engine could not finish is UNREAD, never reported as blank — "
      "they are two different facts and only one of them means the document is "
      "complete",
      "2" in unread and "not blank" in unread, unread)
check("and a complete read adds nothing at all",
      whole == "", repr(whole))
check("the partial sentence comes FIRST, before the reassuring half",
      '_partial_note(payload) + (' in extract_py)
# And every caveat ahead of it: a language the machine could not be told about
# is the same kind of sentence as a document that was only half read, so it goes
# in the same place — ahead of "filed and findable", never after it.
check("a caveat that is not about pages goes ahead of the partial sentence too",
      '"note": _lang_note(stt_lang, payload) + _partial_note(payload)' in extract_py)

print("\n── 4. the clocks: the box gives up first, for a job this host cannot know ──")

MiB = 1024 * 1024
sizes = [0, 1 * MiB, 8 * MiB, 25 * MiB, ce.PULL_MAX_BYTES]

# THE NUMBER COMES OFF THE BOX. A table of tool ceilings here could only default
# an UNKNOWN job to the largest value THIS host knows, which protects an old host
# and leaves a new box unprotected — and the bastion publishes separately, so the
# box being ahead is the normal state. The concrete case, and the one this block
# exists for: a bastion ships transcription bounded at ten minutes, this host has
# never heard of it, and a mirrored table would make the host wait ~170 s while
# the machine works for 600 s. The user is then told their machine did not answer
# by the side that quit first, and the transcript is discarded on arrival.
FUTURE = {"server": "laptop", "tools": ["transcribe"],
          "prep_tools": [{"name": "transcribe", "bin": "whisper", "ceiling_s": 600.0}]}
OLD_BOX = {"server": "desktop", "tools": ["pdf-text"],
           "prep_tools": [{"name": "pdf-text", "bin": "pdftotext"}]}

# 600 s is the BOX'S REAL COST, stated here as a literal and NOT read back
# through the host's own lookup. That distinction is the whole check: an earlier
# draft of this block compared the host's wait against whatever declared_ceiling
# returned, which is true by construction and passes on the broken design — with
# a mirrored table it asserted "the host waits longer than 150 s" while the
# machine worked for 600. The assertion has to be against the number the MACHINE
# published, because that is the number the machine will actually spend.
BOX_REAL_COST_S = 600.0

future_ceiling = ce.declared_ceiling(FUTURE, "transcribe")
check("a job THIS HOST HAS NEVER HEARD OF is bounded by the number the machine "
      "that runs it declared, not by anything on this side",
      future_ceiling == BOX_REAL_COST_S, future_ceiling)
check("so the host OUTWAITS what that machine will actually spend, at every "
      "source size — the inversion a mirrored table produces and cannot be "
      "fixed to avoid",
      all(ce._pull_timeout(s, future_ceiling) > BOX_REAL_COST_S for s in sizes),
      [(s, ce._pull_timeout(s, future_ceiling)) for s in sizes])
check("and it outwaits it by a full round trip on top of the transfer grant, so "
      "the margin is the wire's and not a coincidence of the numbers",
      all(ce._pull_timeout(s, future_ceiling) - ce._pull_budget(s) - BOX_REAL_COST_S
          == ce.REPLY_ROUND_TRIP for s in sizes),
      [(s, ce._pull_timeout(s, future_ceiling) - ce._pull_budget(s)) for s in sizes])
check("there is NO table of per-tool ceilings on this host — that shape is what "
      "leaves a newer box unprotected",
      "TOOL_EXTRACT_CEILING" not in extract_py)
check("a machine that declares NOTHING gets the text-layer number, which is the "
      "only job a bastion predating the field has",
      ce.declared_ceiling(OLD_BOX, "pdf-text") == ce.UNDECLARED_CEILING_S)
check("an absurd declaration is capped, so a buggy or hostile box cannot park a "
      "management worker indefinitely",
      ce.declared_ceiling({"prep_tools": [{"name": "x", "ceiling_s": 10 ** 9}]}, "x")
      == ce.MAX_DECLARED_CEILING_S)
check("a non-numeric declaration falls back rather than raising",
      ce.declared_ceiling({"prep_tools": [{"name": "x", "ceiling_s": "soon"}]}, "x")
      == ce.UNDECLARED_CEILING_S)
check("the ceiling is GRANTED BACK on the wire, so the box clamps to it rather "
      "than to its own idea — the shape pull_budget already proved",
      '"extract_budget": round(ceiling, 3)' in extract_py
      and extract_py.count('"extract_budget"') == 2,
      extract_py.count('"extract_budget"'))
check("both transports grant it: a document pulled and a document handed over "
      "inline reach the extractor the same way",
      "wait = budget + _reply_allowance(ceiling)" in extract_py
      and "wait = _reply_allowance(ceiling) + REPLY_ROUND_TRIP" in extract_py)
check("the pdf-text numbers did not move: the default allowance is still the "
      "40 s the pull gate pins",
      ce._reply_allowance() == ce.PULL_REPLY_ALLOWANCE == 40.0)
check("and the box is still strictly under the host at every size",
      all(ce._pull_budget(s) < ce._pull_timeout(s, c)
          for s in sizes for c in (ce.UNDECLARED_CEILING_S, 150.0, 600.0)))
check("EXTRACT_TIMEOUT is gone — a constant with that name in this file that "
      "bounds nothing is what a future session would read as the answer",
      "EXTRACT_TIMEOUT" not in extract_py)
print("        declared by the box   host waits (25 MiB source)")
for c in (ce.UNDECLARED_CEILING_S, 150.0, 600.0):
    print(f"        {c:17.0f} s   {ce._pull_timeout(25 * MiB, c):9.0f} s")

print("\n── 5. the box's answer survives the heartbeat whitelist ──")

check("the language list is whitelisted and bounded, not stored as it arrived",
      'entry["langs"]' in topology_py and "langs[:32]" in topology_py)
check("the blocked list is whitelisted field by field, like prep_scan's siblings",
      'prep_scan["blocked"] = blocked' in topology_py
      and 'str(e.get("reason") or "").strip()[:400]' in topology_py)
check("a bastion too old to report either sends no key, and absence is not "
      "written as 'looked and found nothing'",
      "isinstance(blocked_in, list)" in topology_py)
check("the blocked reasons reach the model, not only the raw scan blob",
      "ocr_blocked" in extract_py and "ocr_blocked" in tools_py)
check("ocr_blocked reports ONLY machines that have the engine — a machine with "
      "no tesseract says nothing, because there is nothing to tell someone "
      "about software they never installed",
      ce.ocr_blocked_reasons(NO_ENGINE) == []
      and len(ce.ocr_blocked_reasons(ENGINE_ONLY)) == 1)
check("how the words were obtained is recorded on the ROW, so a garbled name "
      "found in search later is visibly an OCR reading",
      '"ocr_pages"' in extract_py and '"ocr_lang"' in extract_py)

print("\n── 6. NO EXTRACTOR IN A PLATFORM IMAGE ──")
# The premise of the whole ticket, and there is already a gate for it
# (scripts/check_no_platform_extractors.py). Asserted here too because THIS is
# the change that would break it: adding tesseract to a Dockerfile is the
# obvious wrong way to make OCR work, and the wrong way must fail in the lane
# that tempts someone into it.
gate = os.path.join(ROOT, "scripts/check_no_platform_extractors.py")
if os.path.exists(gate):
    r = subprocess.run([sys.executable, gate], cwd=ROOT, capture_output=True, text=True)
    check("no platform image gained an extractor for this feature — it runs on "
          "the user's hardware, which is the entire premise of this ticket",
          r.returncode == 0, (r.stdout or "") + (r.stderr or ""))
else:
    skip("the no-platform-extractors gate", f"{gate} is not on this branch")

print("\n── 7. LIVE: a real scanned document, with the real tools ──")

have = {b: shutil.which(b) for b in ("pdftotext", "pdftoppm", "tesseract", "gs")}
missing = [b for b, p in have.items() if not p]
if missing:
    skip("the live scanned-PDF checks", "not installed here: " + ", ".join(missing) +
         " — these tools belong on a BASTION, never in a platform image, so a "
         "CI runner without them is correct and this skip is not a gap")
else:
    langs = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True)
    usable = [l.strip() for l in langs.stdout.splitlines()[1:]
              if l.strip() and l.strip() != "osd"]
    if not usable:
        skip("the live scanned-PDF checks",
             "tesseract is installed with no language data — which is exactly the "
             "state check 2 above is about, and it cannot OCR here")
    else:
        tmp = tempfile.mkdtemp(prefix="ocrgate-")
        try:
            ps = os.path.join(tmp, "d.ps")
            with open(ps, "w") as f:
                f.write("%!PS-Adobe-3.0\n<< /PageSize [420 200] >> setpagedevice\n"
                        "/Helvetica-Bold findfont 20 scalefont setfont\n"
                        "20 140 moveto (GATE FIXTURE alpha bravo) show\n"
                        "20 100 moveto (second line charlie delta) show\nshowpage\n%%EOF\n")
            txt = os.path.join(tmp, "text.pdf")
            scan = os.path.join(tmp, "scan.pdf")
            # Ghostscript reads stdin and writes stdout here, and keeps its own
            # temp files in /tmp. Ubuntu confines gs with an AppArmor profile
            # ("gs": user-tmp, $HOME and /mnt, /media by file extension), so
            # with TMPDIR anywhere else -- a scratch tree, as reviewers run these
            # gates -- gs could not create its temp or output file and this
            # check went red on a working lane (measured: dmesg
            # apparmor="DENIED" operation="mknod" profile="gs"). gs removes
            # its own temp files.
            gs_env = {**os.environ, "TMPDIR": "/tmp", "TEMP": "/tmp", "TMP": "/tmp"}

            def _gs(device: str, src: bytes, *extra: str) -> bytes:
                return subprocess.run(
                    ["gs", "-q", "-dSAFER", "-dNOPAUSE", "-dBATCH", f"-sDEVICE={device}",
                     *extra, "-sOutputFile=-", "-"],
                    input=src, check=True, capture_output=True, env=gs_env).stdout

            with open(ps, "rb") as f:
                pdf = _gs("pdfwrite", f.read())          # what ps2pdf does
            with open(txt, "wb") as f:
                f.write(pdf)
            with open(scan, "wb") as f:
                f.write(_gs("pdfimage8", pdf, "-r150"))

            # THE PREMISE, measured rather than asserted: the text-layer job
            # succeeds on this document and returns nothing.
            layer = subprocess.run(["pdftotext", "-layout", "-q", "--", scan, "-"],
                                   capture_output=True, text=True)
            check("the fixture really is a SCAN: pdftotext exits 0 and returns no "
                  "text for a page covered in words",
                  layer.returncode == 0 and layer.stdout.replace("\f", "").strip() == "",
                  repr(layer.stdout[:120]))

            # And the pipeline the box runs reads it.
            subprocess.run(["pdftoppm", "-png", "-gray", "-r", "300", "-f", "1", "-l", "1",
                            scan, os.path.join(tmp, "p")], check=True, capture_output=True)
            page = [os.path.join(tmp, f) for f in sorted(os.listdir(tmp))
                    if f.startswith("p-") and f.endswith(".png")][0]
            ocr = subprocess.run(["tesseract", page, "-", "-l", usable[0]],
                                 capture_output=True, text=True)
            check("and OCR at the resolution this lane rasterizes at reads the "
                  "words off it",
                  ocr.returncode == 0 and "GATE FIXTURE" in ocr.stdout,
                  repr(ocr.stdout[:200]))
            check("both lines, so it read the page and not just the top of it",
                  "charlie delta" in ocr.stdout, repr(ocr.stdout[:200]))

            # THE MEASUREMENT BEHIND THE PROBE. --version exits 0 with no
            # language data; only a real OCR exits non-zero. A probe on the
            # cheap argv would advertise OCR on a machine that cannot read a
            # word, which is the bug this whole slice is shaped around.
            env = dict(os.environ, TESSDATA_PREFIX=os.path.join(tmp, "empty"))
            os.makedirs(env["TESSDATA_PREFIX"], exist_ok=True)
            v = subprocess.run(["tesseract", "--version"], capture_output=True, env=env)
            real = subprocess.run(["tesseract", page, "-"], capture_output=True, env=env)
            check("`tesseract --version` exits 0 with NO language data — which is "
                  "why the bastion's probe performs a real OCR instead",
                  v.returncode == 0, v.returncode)
            check("and a real OCR with no language data fails, so the probe that "
                  "does one is the only honest capability test",
                  real.returncode != 0, real.returncode)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

print()
if failures:
    print(f"FAIL: {len(failures)} of {checks} check(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
tail = f" ({len(skipped)} skipped)" if skipped else ""
print(f"PASS: {checks} checks{tail} — a scanned document reaches OCR, and an "
      f"empty result is explained rather than reported")
