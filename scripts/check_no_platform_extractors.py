#!/usr/bin/env python3
"""Invariant: NO native content-extractor package is installed into a platform
image without a reviewed decision.

WHAT CHANGED. This gate used to state a principle —
"anything that turns a user's bytes into their content runs on their
hardware" — that was never decided: it was generalised from
a remark that a VIDEO DECODER is not a platform service. The decision is
the opposite for documents: a file uploaded to a Host is read BY the Host,
as a plain read function. It does that with its own standard-library readers
(server/readers — PDF, Office, RTF, email, HTML, text), vendored from the
bastion agent so one code path reads a file wherever it lives.

WHY THE GATE STILL EXISTS. Those readers are pure Python on a per-document
budget. A native extractor (poppler, tesseract, libreoffice, ffmpeg, a
whisper, or a pip parser such as pypdf) is a large parser surface running on
arbitrary user bytes inside every host pod, plus image weight. Installing one
may be the right call some day — for OCR in the Host, say — but it is a
decision to make in a reviewed PR, not a one-line fix to get a file indexed.
If a file does not read today: fix server/readers (and the bastion agent's
copy), or route the engine job to one of the user's machines.

WHAT IS CHECKED. Every Dockerfile in the repo (Dockerfile.linux, the postgres
image, and every sidecars/*/Dockerfile — all of them run on PLATFORM hardware)
plus the root requirements.txt, which is what Dockerfile.linux pip-installs.
Comment lines are ignored, so a note that merely mentions ffmpeg is fine; an
install line is not.

IF THE PACKAGE WRITES RATHER THAN READS. Some stems here are libraries that
also generate files — openpyxl, python-docx, python-pptx, pandoc. Adding one to
EXPORT platform data or render our own markdown is not what this gate is about,
and the answer there is not a bastion release. Remove that stem from EXTRACTORS
in a PR that states which of the two the package is for and why, so a human
reviews the decision. Deleting this gate, or quietly dropping a stem without
saying so, is the failure mode that line exists to prevent.

SCOPE HONESTY. This reads the two places a platform image's contents are
declared in this repo. It cannot see a binary fetched by a URL that does not
name its payload, and it does not read the sidecars' node/npm manifests. It is
a ratchet against the plausible accident, not a proof of absence.

Pure: reads files, runs nothing. Container-free; runs in pre-merge and smoke.
Run from the repo root:  APP_ROOT=<repo> python3 scripts/check_no_platform_extractors.py
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])

# ── the banned surface ────────────────────────────────────────────────────
# Keyed by the stem an installer would actually TYPE, because that is what has
# to be caught: nobody types "poppler" — they
# type poppler-utils, or pdftotext, or pypdf, or mupdf-tools when poppler is
# blocked. Each stem is matched as a case-insensitive SUBSTRING, so
# tesseract-ocr-eng, imageio-ffmpeg and python3-pypdf2 all land on their stem.
# Short or ambiguous names (sox, gs, av, fitz) are NOT here: a substring rule
# on them fires on unrelated packages, and a gate that cries wolf gets bypassed.
EXTRACTORS: dict[str, tuple[str, ...]] = {
    "PDF text extraction — hands back the literal text of the user's document": (
        "poppler", "pdftotext", "pdftoppm", "pdftocairo", "pdfimages",
        "mupdf", "mutool", "ghostscript", "xpdf",
        "pypdf", "pdfminer", "pdfplumber", "pymupdf", "pdfium",
    ),
    "OCR — hands back the literal text printed on the user's page or screenshot": (
        "tesseract", "ocrmypdf", "easyocr", "paddleocr", "gocr", "cuneiform",
    ),
    "audio/video decode and transcription — hands back the literal words the user said": (
        "ffmpeg", "ffprobe", "libav-tools", "libavcodec", "libavformat",
        "libavfilter", "libswscale", "gstreamer",
        "whisper", "vosk", "moviepy", "pydub",
    ),
    "office-document extraction — hands back the literal contents of the user's file": (
        "libreoffice", "soffice", "unoconv", "antiword", "catdoc", "pandoc",
        "python-docx", "docx2txt", "python-pptx", "openpyxl", "xlrd", "odfpy",
    ),
    "general-purpose extraction frameworks — every format above, in one package": (
        "textract", "unstructured", "apache-tika", "tika-server",
    ),
}

_STEM_REASON = {stem: reason for reason, stems in EXTRACTORS.items() for stem in stems}


def scan_text(text: str) -> list[tuple[int, str, str, str]]:
    """Return (lineno, line, stem, reason) for every non-comment hit.

    A line whose first non-space character is '#' is a comment in both the
    Dockerfile parser and in requirements.txt, so prose about ffmpeg is free.
    An install line is not.
    """
    hits: list[tuple[int, str, str, str]] = []
    for n, raw in enumerate(text.splitlines(), start=1):
        if raw.lstrip().startswith("#"):
            continue
        low = raw.lower()
        for stem, reason in _STEM_REASON.items():
            if stem in low:
                hits.append((n, raw.strip(), stem, reason))
    return hits


def targets(root: Path) -> list[Path]:
    out = [p for p in root.rglob("Dockerfile*")
           if p.is_file() and "node_modules" not in p.parts and ".git" not in p.parts]
    req = root / "requirements.txt"
    if req.is_file():
        out.append(req)
    return sorted(out)


def main() -> int:
    # ── self-test FIRST: a gate that cannot fire is not a gate ────────────
    # Measured, not assumed: run the real matcher over the exact line a future
    # session would write. If the matcher ever stops matching, this fails here
    # rather than passing green over a violated tree.
    probe = "RUN apt-get install -y tesseract-ocr poppler-utils ffmpeg\n"
    probe_hits = {h[2] for h in scan_text(probe)}
    if not {"tesseract", "poppler", "ffmpeg"} <= probe_hits:
        print("FAIL: the matcher itself did not flag an apt line installing "
              f"tesseract-ocr, poppler-utils and ffmpeg (matched {sorted(probe_hits)}).")
        print("      Fix the matcher. Do NOT ship the green run this would otherwise give.")
        return 2
    if scan_text("# ffmpeg was deliberately NOT installed here — see the decision\n"):
        print("FAIL: the matcher flagged a COMMENT. It would fire on prose, which "
              "trains people to bypass it. Fix the comment handling.")
        return 2

    files = targets(_ROOT)
    dockerfiles = [p for p in files if p.name.startswith("Dockerfile")]
    if not dockerfiles:
        print(f"FAIL: found 0 Dockerfiles under {_ROOT} — this gate checked nothing "
              "and would have exited 0. That is a dead gate, not a clean tree.")
        return 2

    violations: list[tuple[Path, int, str, str, str]] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"FAIL: cannot read {path}: {exc}")
            return 2
        for lineno, line, stem, reason in scan_text(text):
            violations.append((path, lineno, line, stem, reason))

    if violations:
        print("FAIL: a content EXTRACTOR is being installed into a platform image.")
        print()
        for path, lineno, line, stem, reason in violations:
            rel = path.relative_to(_ROOT) if path.is_relative_to(_ROOT) else path
            print(f"  {rel}:{lineno}  matches '{stem}'")
            print(f"      {line}")
            print(f"      {reason}")
        print()
        print("  WHY THIS IS REFUSED. The Host reads documents with its own standard-")
        print("  library readers (server/readers). A native extractor is a large parser")
        print("  surface on arbitrary user bytes inside every host pod, and image weight;")
        print("  adding one is a decision for a reviewed PR, not a fix to get a file indexed.")
        print()
        print("  If you got here trying to make a PDF, an office document or a text file")
        print("  readable: the Host reads those itself with server/readers — fix the reader")
        print("  (and the bastion agent's copy). A scan, audio or video goes to one of the")
        print("  user's machines. Adding a native extractor to every host pod is a decision")
        print("  for a reviewed PR, not a bug fix.")
        print()
        print("  IF THE PACKAGE WRITES RATHER THAN READS. openpyxl, python-docx,")
        print("  python-pptx and pandoc also GENERATE files. Adding one to export platform")
        print("  data or render our own markdown is not what this gate is about, and a")
        print("  bastion release is not the answer. Remove that stem from EXTRACTORS in")
        print("  scripts/check_no_platform_extractors.py, in a PR that states which of the")
        print("  two the package is for and why, so a human reviews the call. Deleting this")
        print("  gate, or quietly dropping a stem without saying so, is the failure mode")
        print("  this line exists to prevent.")
        return 1

    print(f"OK: no content extractor in any platform image — {len(dockerfiles)} Dockerfiles "
          f"+ requirements.txt checked against {len(_STEM_REASON)} banned package stems "
          f"across {len(EXTRACTORS)} extractor families (PDF, OCR, audio/video, office, "
          "general). The Host reads documents with its own standard-library readers "
          "(server/readers).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
