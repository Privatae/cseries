"""The host's own readers: a stored file's bytes to its text, with no AI and
no machine — a plain function the host calls the moment a file lands.

Files a user uploads to a Host belong on that Host and are processed there,
by a *read* function. A file
dropped on a Host is read BY the Host: PDF, Office (docx/xlsx/pptx, odt/ods/
odp), RTF, email, HTML and plain text. What needs an engine the Host does not
carry (OCR of a picture, speech, video frames) still goes to one of the user's
machines through context_extract.

These readers are the bastion agent's (bastion-agent-dev bastion_agent/
extract.py and pdftext.py), vendored so one code path reads a file whether it
sits on the user's machine or was uploaded to their Host. Keep them in step:
a fix in one is a fix in both. Standard library only.
"""

import codecs
import csv
import email
import email.policy
import html
import html.parser
import io
import mimetypes
import re
import tarfile
import threading
import time
import unicodedata
import zipfile
import zlib
import xml.etree.ElementTree as ET

from . import pdftext as _pdftext

ZIP_MEMBER_CAP = 64 * 1024 * 1024   # decompressed bytes we will read from one member
PDF_TEXT_MIN_RATIO = 0.9             # printable share below which builtin PDF text is refused

TEXT_MIMES_PREFIX = ("text/",)
TEXT_MIMES = {
    "application/json", "application/x-ndjson", "application/yaml", "application/toml",
    "application/xml", "application/sql", "image/svg+xml", "application/x-pem-file",
}
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
ODT, ODS, ODP = ("application/vnd.oasis.opendocument.text",
                 "application/vnd.oasis.opendocument.spreadsheet",
                 "application/vnd.oasis.opendocument.presentation")
IMAGE_OCR_MIMES = ("image/png", "image/jpeg", "image/tiff", "image/bmp", "image/gif", "image/webp")


# ── text normalisation ──────────────────────────────────────────────────────

def _normalise(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = unicodedata.normalize("NFC", text)
    return text.strip("\n") + ("\n" if text.strip() else "")


def decode_bytes(data):
    """Best-effort decode with the encoding recorded. BOMs first, strict
    UTF-8 next, UTF-16 by NUL pattern, then cp1252 with replacement."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace"), "utf-8-sig"
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16", "replace"), "utf-16"
    # BOM-less UTF-16 is byte-valid UTF-8 (NUL is a code point), so the
    # NUL-density test has to come before the strict UTF-8 attempt.
    sample = data[:4096]
    if len(sample) >= 4 and sample.count(b"\x00") > len(sample) // 4:
        enc = "utf-16-le" if sample[1:2] == b"\x00" else "utf-16-be"
        return data.decode(enc, "replace"), enc
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    return data.decode("cp1252", "replace"), "cp1252"


# ── html ────────────────────────────────────────────────────────────────────

_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
               "section", "article", "header", "footer", "table", "ul", "ol",
               "blockquote", "pre", "hr", "td", "th", "dd", "dt", "title"}


class _HtmlText(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "template"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self):
        out = "".join(self.parts)
        out = re.sub(r"[ \t\f\v]+", " ", out)
        out = re.sub(r" *\n *", "\n", out)
        return re.sub(r"\n{3,}", "\n\n", out)


def html_to_text(markup):
    p = _HtmlText()
    p.feed(markup)
    p.close()
    return p.text()


# ── office (zip + xml) ──────────────────────────────────────────────────────
#
# STREAMED, ON A BUDGET. A 200 KB spreadsheet can hold a gigabyte of XML, and
# parsing a member whole (ElementTree.fromstring) built the full tree in
# memory — two of them at once across sheets (measured 2.2 GB peak for a
# 1.3 MB xlsx). Members are fed to a pull parser in chunks; each wanted element
# (a paragraph, a row) is read and then dropped from its parent, so memory is
# one element, not one document. A document's decompressed XML, its text and
# its wall time are capped; reaching a cap stops the read with the text so far
# and says so on the last line. A DTD is refused outright: Office XML never
# carries one, and one is how entity expansion is smuggled in.

OFFICE_DOC_CAP = 256 * 1024 * 1024   # decompressed XML bytes, one document
OFFICE_TEXT_CAP = 16 * 1024 * 1024   # characters of text, one document
OFFICE_SECONDS = 60.0                # wall time, one document
# Parts one paragraph / row may hold while it is being read. A real paragraph
# has tens of runs and an Excel row at most 16,384 cells; a crafted one with
# millions is refused rather than held in memory.
OFFICE_PARTS_PER_ITEM = 200_000
_OFFICE_CHUNK = 64 * 1024
_DTD = re.compile(rb"<!(?:DOCTYPE|ENTITY)", re.I)
_DROPPED = "\x00dropped"
_XML_ENCODING = re.compile(r"""(<\?xml[^>]*?encoding\s*=\s*["'])[^"']*(["'])""")


class _OfficeStop(Exception):
    """A per-document cap was reached; the text so far stands."""


# An OUTER deadline a reader inherits: set by the archive reader for the whole
# archive, so a member's own budget (Office, PDF) can never outrun it. Thread-
# local because readers run in worker threads (context_extract runs each read
# on asyncio.to_thread); an archive sets it for its thread and clears it after.
_outer = threading.local()


def _outer_deadline():
    return getattr(_outer, "deadline", None)


def _bounded_deadline(seconds):
    """Now + seconds, or the outer deadline if that comes first."""
    own = time.monotonic() + seconds
    outer = _outer_deadline()
    return min(own, outer) if outer is not None else own


class _OfficeBudget:
    def __init__(self):
        self.bytes = self.chars = 0
        self.deadline = _bounded_deadline(OFFICE_SECONDS)
        self.cut_items = 0     # paragraphs/rows cut at OFFICE_PARTS_PER_ITEM

    def take_bytes(self, n):
        self.bytes += n
        if self.bytes > OFFICE_DOC_CAP:
            raise _OfficeStop("the document expands past %d MB of XML" % (OFFICE_DOC_CAP >> 20))
        if time.monotonic() > self.deadline:
            raise _OfficeStop("the document took longer than %ds to read" % OFFICE_SECONDS)

    def take_text(self, out, line):
        self.chars += len(line) + 1
        if self.chars > OFFICE_TEXT_CAP:
            raise _OfficeStop("the document holds more than %d MB of text" % (OFFICE_TEXT_CAP >> 20))
        out.append(line)


def _zip_elements(zf, name, budget, wanted, skip_within=()):
    """Yield each `wanted` element of one member as it closes, then drop it.
    A wanted tag inside a `skip_within` ancestor is left in place for that
    ancestor (an ODF table row reads its own cells' paragraphs). Every other
    finished element that no open wanted element needs is dropped as it
    closes too, so siblings never pile up — memory is one item, and removal
    from a parent stays O(1)."""
    info = zf.getinfo(name)
    if info.file_size > ZIP_MEMBER_CAP:
        raise ValueError("zip member %s too large (%d bytes)" % (name, info.file_size))
    keep = set(wanted) | set(skip_within)
    parser = ET.XMLPullParser(("start", "end"))
    stack = []
    state = {"skipping": 0, "keeping": 0, "parts": 0}

    # Finished elements are cleared at once (their subtree is freed) and
    # detached from their parents in one batch per fed chunk. The pull parser
    # reads the whole chunk ahead, so a closing element is NOT its parent's
    # last child when its event is handled, and remove() scans every kept
    # sibling from the start: per element that was quadratic.
    detach = {}

    def drop(el):
        el.clear()
        # Until flush() detaches it, a dropped element is still a child of
        # its parent; renamed so no reader's tag match sees it (a cut row
        # would otherwise end in empty cells).
        el.tag = _DROPPED
        if stack:
            parent = stack[-1]
            detach.setdefault(id(parent), (parent, set()))[1].add(id(el))

    def flush():
        for parent, gone in detach.values():
            parent[:] = [c for c in parent if id(c) not in gone]
        detach.clear()

    def drain():
        for ev, el in parser.read_events():
            if ev == "start":
                stack.append(el)
                if el.tag in skip_within:
                    state["skipping"] += 1
                if el.tag in keep:
                    state["keeping"] += 1
                elif state["keeping"]:
                    state["parts"] += 1
                    if state["parts"] == OFFICE_PARTS_PER_ITEM + 1:
                        budget.cut_items += 1
                continue
            stack.pop()
            if el.tag in skip_within:
                state["skipping"] -= 1
            if el.tag in keep:
                state["keeping"] -= 1
                if not state["keeping"]:
                    state["parts"] = 0
            if el.tag in wanted and not (state["skipping"] and el.tag not in skip_within):
                # How many wanted items are still open around this one: a
                # docx text box is a paragraph inside a paragraph.
                yield el, state["keeping"]
                drop(el)
            elif not state["keeping"] or state["parts"] > OFFICE_PARTS_PER_ITEM:
                # Past the cap, an item's further parts are dropped as they
                # close: that one paragraph or row is cut, the rest of the
                # document still reads.
                drop(el)

    seen, tail, utf16 = 0, b"", None
    with zf.open(name) as fh:
        while True:
            chunk = fh.read(_OFFICE_CHUNK)
            if not chunk:
                break
            if not seen:
                # OOXML allows UTF-8 or UTF-16. UTF-16 is decoded to UTF-8
                # here, BEFORE the DTD check, so a DTD cannot ride past it in
                # a wide encoding; the declaration is rewritten to match.
                # Anything else wide (UTF-32, no BOM) is refused.
                if chunk[:2] in (b"\xff\xfe", b"\xfe\xff"):
                    utf16 = codecs.getincrementaldecoder("utf-16")("strict")
                elif b"\x00" in chunk[:4]:
                    raise ValueError("zip member %s is not UTF-8 or UTF-16 XML" % name)
            if utf16 is not None:
                text = utf16.decode(chunk)
                if not seen:
                    text = _XML_ENCODING.sub(r"\1UTF-8\2", text, count=1)
                raw = chunk
                chunk = text.encode("utf-8")
                seen += len(raw)
            else:
                seen += len(chunk)
            if seen > ZIP_MEMBER_CAP:
                raise ValueError("zip member %s too large" % name)
            if _DTD.search(tail + chunk):
                raise ValueError("zip member %s declares a DTD; refused" % name)
            tail = chunk[-16:]
            budget.take_bytes(len(chunk))
            parser.feed(chunk)
            for item in drain():
                yield item
            flush()
    parser.close()
    for item in drain():
        yield item
    flush()


def _office(read):
    """Run one reader with a fresh budget; a cap reached keeps the text so
    far and names the cap on the last line."""
    budget, out = _OfficeBudget(), []
    try:
        read(budget, out)
    except _OfficeStop as e:
        out.append("[stopped reading here: %s]" % e)
    if budget.cut_items:
        out.append("[%d paragraph(s) or row(s) held more than %d parts; each was cut there]"
                   % (budget.cut_items, OFFICE_PARTS_PER_ITEM))
    return "\n".join(out)


def _local(tag):
    return tag.rsplit("}", 1)[-1]


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
TEXT_NS = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
TABLE_NS = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"


def _docx_paragraph(p):
    parts = []
    for node in p.iter():
        t = _local(node.tag)
        if t == "t" and node.text:
            parts.append(node.text)
        elif t == "tab":
            parts.append("\t")
        elif t in ("br", "cr"):
            parts.append("\n")
    return "".join(parts)


def extract_docx(data):
    def read(budget, out):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            order = ["word/document.xml"] + sorted(
                n for n in names if re.match(r"word/(header|footer)\d*\.xml$", n))
            for name in order:
                if name not in names:
                    continue
                boxed = []      # text-box paragraphs, read after the paragraph holding them
                for p, depth in _zip_elements(zf, name, budget, {W + "p"}):
                    if depth:
                        boxed.append(_docx_paragraph(p))
                        continue
                    budget.take_text(out, _docx_paragraph(p))
                    for line in boxed:
                        budget.take_text(out, line)
                    boxed = []
                for line in boxed:
                    budget.take_text(out, line)
                out.append("")
    return _office(read)


def extract_xlsx(data):
    def read(budget, out):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            shared = []
            if "xl/sharedStrings.xml" in names:
                for si, _ in _zip_elements(zf, "xl/sharedStrings.xml", budget, {S + "si"}):
                    text = "".join(t.text or "" for t in si.iter(S + "t"))
                    budget.take_text([], text)       # shared strings count against the cap too
                    shared.append(text)
            sheets = sorted(n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
            for name in sheets:
                out.append("# " + name.rsplit("/", 1)[-1][:-4])
                for row, _ in _zip_elements(zf, name, budget, {S + "row"}):
                    cells = []
                    for c in row.iter(S + "c"):
                        v = c.find(S + "v")
                        is_node = c.find(S + "is")
                        val = ""
                        if c.get("t") == "s" and v is not None and v.text is not None:
                            try:
                                val = shared[int(v.text)]
                            except (ValueError, IndexError):
                                val = ""
                        elif c.get("t") == "inlineStr" and is_node is not None:
                            val = "".join(t.text or "" for t in is_node.iter(S + "t"))
                        elif v is not None and v.text is not None:
                            val = v.text
                        cells.append(val)
                    if any(cells):
                        budget.take_text(out, "\t".join(cells))
                out.append("")
    return _office(read)


def extract_pptx(data):
    def read(budget, out):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            slides = sorted((n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                            key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
            for name in slides:
                out.append("# " + name.rsplit("/", 1)[-1][:-4])
                for p, _ in _zip_elements(zf, name, budget, {A + "p"}):
                    line = "".join(t.text or "" for t in p.iter(A + "t"))
                    if line:
                        budget.take_text(out, line)
                out.append("")
    return _office(read)


def extract_odf(data):
    row_tag, cell_tag = TABLE_NS + "table-row", TABLE_NS + "table-cell"

    def read(budget, out):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            wanted = {TEXT_NS + "p", TEXT_NS + "h", row_tag}
            for el, _ in _zip_elements(zf, "content.xml", budget, wanted, skip_within={row_tag}):
                if el.tag == row_tag:
                    # A row's cells hold paragraphs; the row is one line.
                    cells = ["".join(c.itertext()) for c in el if c.tag == cell_tag]
                    if any(cells):
                        budget.take_text(out, "\t".join(cells))
                else:
                    budget.take_text(out, "".join(el.itertext()))
    return _office(read)


# ── email, rtf ──────────────────────────────────────────────────────────────

def extract_eml(data):
    msg = email.message_from_bytes(data, policy=email.policy.default)
    head = []
    for h in ("From", "To", "Cc", "Date", "Subject"):
        if msg[h]:
            head.append("%s: %s" % (h, msg[h]))
    body = msg.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        content = body.get_content()
        text = html_to_text(content) if body.get_content_type() == "text/html" else content
    return "\n".join(head) + "\n\n" + text


_RTF_SKIP_GROUPS = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "object",
                    "header", "footer", "xmlnstbl", "themedata", "datastore", "generator"}
_RTF_TOKEN = re.compile(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z])|([{}])|([^\\{}]+)")


def extract_rtf(data):
    text = data.decode("latin-1", "replace")
    out = []
    stack = []
    skip_depth = None
    uc_skip = 0
    for m in _RTF_TOKEN.finditer(text):
        word, arg, hexch, esc, brace, plain = m.groups()
        if brace == "{":
            stack.append(len(out))
            continue
        if brace == "}":
            if stack:
                stack.pop()
            # the skipped group closes when the stack drops below the depth
            # it was opened at
            if skip_depth is not None and len(stack) < skip_depth:
                skip_depth = None
            continue
        if skip_depth is not None:
            continue
        if word:
            if word in _RTF_SKIP_GROUPS:
                skip_depth = len(stack)
            elif word in ("par", "line", "sect", "page"):
                out.append("\n")
            elif word == "tab":
                out.append("\t")
            elif word == "u" and arg is not None:
                cp = int(arg)
                out.append(chr(cp + 65536 if cp < 0 else cp))
                uc_skip = 1
            continue
        if hexch:
            if uc_skip:
                uc_skip -= 1
                continue
            out.append(bytes([int(hexch, 16)]).decode("cp1252", "replace"))
            continue
        if esc:
            out.append({"~": "\u00a0", "-": "", "_": "-", "\n": "\n", "\r": ""}.get(esc, esc))
            continue
        if plain:
            if uc_skip:
                plain = plain[uc_skip:]
                uc_skip = 0
            out.append(plain.replace("\r", "").replace("\n", ""))
    return "".join(out)


# ── pdf (built-in best effort) ──────────────────────────────────────────────

def extract_pdf_builtin(data):
    """Text from the document's pages with the standard library (pdftext):
    fonts' /ToUnicode maps, compressed streams and object streams, so a
    browser-printed PDF reads the same as an old literal-string one. Returns
    None when the result does not look like text (encrypted files, pages that
    are only pictures). pdftext never raises and reads on a per-document
    budget, so a hostile file costs bounded time and is at worst no text."""
    # The document's own budget, cut to the outer deadline when an archive
    # is reading it as one member of many.
    outer = _outer_deadline()
    budget = None
    if outer is not None:
        budget = _pdftext.Budget()
        budget.deadline = min(budget.deadline, outer)
    text = _pdftext.extract_text(data, budget)
    if not text or not text.strip():
        return None
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\n\t\f")
    if printable / max(1, len(text)) < PDF_TEXT_MIN_RATIO:
        return None
    return text



# ── host-only below this line (not vendored from the agent) ─────────────────

# Readers with no per-document budget of their own (PDF and Office have one):
# bounded by the size of what they are given.
_UNBUDGETED = frozenset({"text", "html", "csv", "tsv", "rtf", "eml"})
UNBUDGETED_MAX_BYTES = 64 * 1024 * 1024


class Unreadable(Exception):
    """This file type has no host reader, or the file could not be read."""


# mime -> (reader name, function(bytes) -> text or None)
def _text(data):
    return decode_bytes(data)[0]


def _html(data):
    return html_to_text(decode_bytes(data)[0])


def _csv(data, delimiter=","):
    rows = csv.reader(io.StringIO(decode_bytes(data)[0]), delimiter=delimiter)
    return "\n".join("\t".join(r) for r in rows)


# ── archives (host-only) ────────────────────────────────────────────────────
# A gzip / zip / tar upload is unpacked HERE, as a read. Before this, an
# archive was the one common upload type with no reader at all, and the read
# error sent the host looking for a machine to unpack it on — no machine could
# (a .tickets.gz).
# Bounded on every axis a hostile archive can grow along: decompressed bytes
# per member and in total, member count, and characters of text. Depth is
# one: an archive inside an archive is named, not opened.

ARCHIVE_MEMBER_CAP = ZIP_MEMBER_CAP          # decompressed bytes, one member
ARCHIVE_TOTAL_CAP = 64 * 1024 * 1024         # decompressed bytes, the whole archive
ARCHIVE_MEMBERS_MAX = 500                    # members read from one archive
ARCHIVE_TEXT_CAP = OFFICE_TEXT_CAP           # characters of text, the whole archive
ARCHIVE_UNBUDGETED_MEMBER_CAP = 16 * 1024 * 1024   # bytes one text/html/csv/rtf/eml member may be
# Wall time for the WHOLE archive — the same as one Office document, because
# an archive holds one read slot the way one document does. Every member's own
# budget is cut to this deadline (_bounded_deadline), so a zip of no-text
# Office files cannot cost 60 s per member (measured: 5 of them,
# 97 s) — the archive stops at the deadline with what it read.
ARCHIVE_SECONDS = OFFICE_SECONDS

# A tarball inside gzip carries the gzip mime under most browsers and
# application/x-tar (encoding gzip) under mimetypes.guess_type, so both
# readers sniff: _read_gzip finds a tar, _read_tar finds a gzip.
GZIP_MIMES = ("application/gzip", "application/x-gzip",
              "application/x-compressed-tar", "application/x-tgz")
ZIP_MIMES = ("application/zip", "application/x-zip-compressed")
TAR_MIMES = ("application/x-tar", "application/x-gtar")
ARCHIVE_MIMES = GZIP_MIMES + ZIP_MIMES + TAR_MIMES

# Extensions mimetypes does not know, or knows differently from the readers.
_MEMBER_EXT_MIME = {
    ".log": "text/plain", ".txt": "text/plain", ".md": "text/markdown",
    ".markdown": "text/markdown", ".jsonl": "application/x-ndjson",
    ".ndjson": "application/x-ndjson", ".yaml": "application/yaml", ".yml": "application/yaml",
    ".toml": "application/toml", ".sql": "application/sql", ".eml": "message/rfc822",
    ".rtf": "application/rtf", ".tsv": "text/tab-separated-values",
    ".docx": DOCX, ".xlsx": XLSX, ".pptx": PPTX, ".odt": ODT, ".ods": ODS, ".odp": ODP,
    ".gz": "application/gzip", ".tgz": "application/gzip", ".zip": "application/zip",
    ".tar": "application/x-tar",
}


def _member_mime(name, data):
    """The mime a member is read as: its extension first, then a sniff. None
    means binary (not read); "archive" means nested (named, not opened)."""
    lower = (name or "").lower()
    mime = None
    for ext, m in _MEMBER_EXT_MIME.items():
        if lower.endswith(ext):
            mime = m
            break
    if mime is None:
        mime = mimetypes.guess_type(lower, strict=False)[0]
    if mime in ARCHIVE_MIMES:
        return "archive"
    if mime and reader_for(mime):
        return mime
    if data[:5] == b"%PDF-":
        return "application/pdf"
    if data[257:262] == b"ustar" or data[:2] == b"\x1f\x8b" or data[:4] == b"PK\x03\x04":
        return "archive"
    head = data[:65536]
    if b"\x00" not in head:
        try:
            head.decode("utf-8")
            return "text/plain"
        except UnicodeDecodeError:
            pass
    return None


def _gunzip(data):
    """(decompressed bytes, original name or "") — RFC 1952 header read for
    FNAME, inflate bounded at ARCHIVE_TOTAL_CAP so a bomb costs the cap and
    no more."""
    if data[:2] != b"\x1f\x8b" or len(data) < 18:
        raise ValueError("not a gzip file")
    name = ""
    flg = data[3]
    pos = 10
    if flg & 4:                                   # FEXTRA
        pos += 2 + int.from_bytes(data[pos:pos + 2], "little")
    if flg & 8:                                   # FNAME
        end = data.find(b"\x00", pos)
        if end != -1:
            name = data[pos:end].decode("latin-1", "replace")
    d = zlib.decompressobj(wbits=31)
    out = bytearray(d.decompress(data, ARCHIVE_TOTAL_CAP + 1))
    while d.unconsumed_tail and len(out) <= ARCHIVE_TOTAL_CAP:
        out += d.decompress(d.unconsumed_tail, ARCHIVE_TOTAL_CAP + 1 - len(out))
    if len(out) > ARCHIVE_TOTAL_CAP:
        raise ValueError("the archive unpacks to more than %d MB" % (ARCHIVE_TOTAL_CAP >> 20))
    if not d.eof:
        raise ValueError("the gzip stream is truncated")
    return bytes(out), name


class _ArchiveText:
    """Accumulates member texts under the archive's caps. Used as a context
    manager: entering sets the outer deadline every member reader inherits,
    leaving clears it."""

    def __init__(self):
        self.parts = []
        self.chars = 0
        self.members = 0
        self.bytes = 0
        self.cut = ""
        self.deadline = time.monotonic() + ARCHIVE_SECONDS

    def __enter__(self):
        _outer.deadline = self.deadline
        return self

    def __exit__(self, *exc):
        _outer.deadline = None
        return False

    def member(self, name, size):
        """True when this member may be read; sets `cut` and returns False
        once a cap is reached (the text so far is kept). EVERY member goes
        through here, read or skipped — an entry that is only named still
        counts, or a central directory of a million entries is a million
        lines (measured: 400,000 entries, 30 M characters)."""
        if self.cut:
            return False
        if time.monotonic() > self.deadline:
            self.cut = "took longer than %ds to read — the rest were not read" % ARCHIVE_SECONDS
            return False
        self.members += 1
        if self.members > ARCHIVE_MEMBERS_MAX:
            self.cut = "more than %d members — the rest were not read" % ARCHIVE_MEMBERS_MAX
            return False
        self.bytes += size
        if self.bytes > ARCHIVE_TOTAL_CAP:
            self.cut = ("more than %d MB unpacked — the rest were not read"
                        % (ARCHIVE_TOTAL_CAP >> 20))
            return False
        return True

    def _line(self, line):
        """Append under the text cap; the cap is the same for a member's text
        and for a line that only names one."""
        room = ARCHIVE_TEXT_CAP - self.chars
        if len(line) > room:
            self.parts.append(line[:max(0, room)])
            self.chars = ARCHIVE_TEXT_CAP
            self.cut = "more than %d MB of text — cut here" % (ARCHIVE_TEXT_CAP >> 20)
            return
        self.parts.append(line)
        self.chars += len(line)

    def skip(self, name, size, why):
        """Name a member that is not read. Counts as a member (not as bytes —
        nothing was unpacked). False once a cap is reached."""
        if not self.member(name, 0):
            return False
        self._line("=== %s (%d bytes) — %s ===\n" % (name, size, why))
        return True

    def add(self, name, data):
        mime = _member_mime(name, data)
        if mime == "archive":
            line = ("=== %s (%d bytes) — an archive inside this one; upload it on its own "
                    "to read it ===\n" % (name, len(data)))
        elif mime is None:
            line = "=== %s (%d bytes) — binary, not read ===\n" % (name, len(data))
        else:
            try:
                _, text = read(data, mime)
            except Unreadable as exc:
                line = "=== %s (%d bytes) — not read: %s ===\n" % (name, len(data), exc)
            else:
                line = "=== %s ===\n%s\n\n" % (name, (text or "").rstrip())
        self._line(line)

    def text(self):
        out = "".join(self.parts)
        if self.cut:
            out += "\n[%s]\n" % self.cut
        return out


_TOO_LARGE = "larger than one member may be, not read"


def _read_tar(data):
    if data[:2] == b"\x1f\x8b":
        # A .tar.gz named by extension arrives as application/x-tar.
        data, _ = _gunzip(data)
    with _ArchiveText() as acc, tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
        for m in tf:
            if not m.isreg():
                continue
            if m.size > ARCHIVE_MEMBER_CAP:
                if not acc.skip(m.name, m.size, _TOO_LARGE):
                    break
                continue
            if not acc.member(m.name, m.size):
                break
            fh = tf.extractfile(m)
            acc.add(m.name, fh.read(m.size) if fh is not None else b"")
    return acc.text()


def _read_zip(data):
    with _ArchiveText() as acc, zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            if info.file_size > ARCHIVE_MEMBER_CAP:
                if not acc.skip(info.filename, info.file_size, _TOO_LARGE):
                    break
                continue
            if not acc.member(info.filename, info.file_size):
                break
            with zf.open(info) as fh:
                body = fh.read(info.file_size + 1)
            if len(body) > info.file_size:
                # The header lied about the size: the one shape a zip bomb
                # takes that the per-member cap above cannot see.
                raise ValueError("zip member %s is larger than its header says" % info.filename)
            acc.add(info.filename, body)
    return acc.text()


def _read_gzip(data):
    """One gzip: a tarball reads as its members; a single file reads as that
    file (its text, no header — the archive IS the document)."""
    inner, name = _gunzip(data)
    if inner[257:262] == b"ustar" or name.lower().endswith(".tar"):
        return _read_tar(inner)
    mime = _member_mime(name, inner)
    if mime == "archive":
        raise ValueError("the gzip holds another archive (%s); upload it on its own" % (name or "unnamed"))
    if mime is None:
        raise ValueError("the gzip holds a binary file (%s), not text" % (name or "unnamed"))
    # Through the same accounting as a member: the text cap applies to the
    # one file a gzip holds exactly as to a tarball's members (measured:
    # a 96 KB gzip of text read to 66 M characters past the 16 M cap).
    with _ArchiveText() as acc:
        acc._line(read(inner, mime)[1])
    return acc.text()


# The PDF reader's budget stops (pdftext.Budget._stop) by name, as a person
# reads them.
_PDF_STOP_WORDS = {
    "time": "it took longer than %ds to read" % int(_pdftext.MAX_SECONDS),
    "decoded bytes": "it decodes to more than %d MB" % (_pdftext.MAX_DECODED >> 20),
    "operators": "it draws more than %d operations" % _pdftext.MAX_OPS,
    "tokens": "it holds more than %d tokens" % _pdftext.MAX_TOKENS,
    "bytes lexed": "it needs more than %d MB of reading" % (_pdftext.MAX_LEXED >> 20),
}


def _pdf_host(data):
    """extract_pdf_builtin, plus one thing the vendored reader keeps to itself:
    when the per-document budget stops a read, the text says so on its last
    line, the way an Office document's does (a
    PDF past its budget came back as its first pages with nothing saying the
    rest was not read). The budget itself stays: it is what keeps a crafted
    file from costing the Host unbounded time and memory."""
    budget = _pdftext.Budget()
    outer = _outer_deadline()
    if outer is not None:
        budget.deadline = min(budget.deadline, outer)
    text = _pdftext.extract_text(data, budget)
    if not text or not text.strip():
        return None
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\n\t\f")
    if printable / max(1, len(text)) < PDF_TEXT_MIN_RATIO:
        return None
    if budget.reason:
        why = _PDF_STOP_WORDS.get(budget.reason, f"its {budget.reason} budget ran out")
        text = (text.rstrip("\n")
                + f"\n[stopped reading here: {why} — the pages after this were not read]\n")
    return text


READERS = {
    "application/pdf": ("pdf", _pdf_host),
    DOCX: ("docx", extract_docx),
    XLSX: ("xlsx", extract_xlsx),
    PPTX: ("pptx", extract_pptx),
    ODT: ("odf", extract_odf), ODS: ("odf", extract_odf), ODP: ("odf", extract_odf),
    "application/rtf": ("rtf", extract_rtf), "text/rtf": ("rtf", extract_rtf),
    "message/rfc822": ("eml", extract_eml),
    "text/html": ("html", _html),
    "text/csv": ("csv", _csv),
    "text/tab-separated-values": ("tsv", lambda d: _csv(d, "\t")),
    **{m: ("archive", _read_gzip) for m in GZIP_MIMES},
    **{m: ("archive", _read_zip) for m in ZIP_MIMES},
    **{m: ("archive", _read_tar) for m in TAR_MIMES},
}


def reader_for(mime: str):
    """The reader name for a mime, or None when the host has none."""
    mime = (mime or "").split(";")[0].strip().lower()
    if mime in READERS:
        return READERS[mime][0]
    if mime.startswith(TEXT_MIMES_PREFIX) or mime in TEXT_MIMES:
        return "text"
    return None


def read(data: bytes, mime: str):
    """(reader, text) for one file. Raises Unreadable with the reason when the
    host has no reader for the type or the file did not yield text."""
    mime = (mime or "").split(";")[0].strip().lower()
    name = reader_for(mime)
    if name is None:
        raise Unreadable(f"the host has no reader for {mime or 'this file type'}")
    if name in _UNBUDGETED:
        # These readers are linear in their input and have no budget of their
        # own; the input size is their bound. Inside an archive the bound is
        # a quarter of that, because the archive's deadline is the only clock
        # these readers never look at and 64 MB of HTML is ten seconds.
        cap = ARCHIVE_UNBUDGETED_MEMBER_CAP if _outer_deadline() is not None else UNBUDGETED_MAX_BYTES
        if len(data) > cap:
            raise Unreadable(f"{name}: {len(data)} bytes is more than the "
                             f"{cap >> 20} MB this reader takes in one go"
                             + (" inside an archive" if cap != UNBUDGETED_MAX_BYTES else ""))
    fn = READERS[mime][1] if mime in READERS else _text
    try:
        text = fn(data)
    except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError, zlib.error,
            tarfile.TarError, EOFError,
            RuntimeError, UnicodeError, KeyError, csv.Error) as exc:
        # An archive's own reasons are sentences a person can act on ("the
        # gzip holds a binary file"); a parser's are not, and are named by type.
        why = str(exc) if name == "archive" and isinstance(exc, ValueError) and str(exc) else None
        # An Office member over the per-member budget (the vendored reader's
        # "zip member … too large" / "declares a DTD") is a sentence too, and
        # the person is told it — not "could not be read (ValueError)".
        if why is None and isinstance(exc, ValueError) and str(exc).startswith("zip member "):
            why = (f"{exc} — over the {ZIP_MEMBER_CAP >> 20} MB a member may unpack to, "
                   f"the limit that keeps a crafted file from filling the Host"
                   if "too large" in str(exc) else str(exc))
        raise Unreadable(f"{name}: {why or 'the file could not be read (' + type(exc).__name__ + ')'}")
    if text is None:
        why = ("no text layer — the pages are pictures (a scan) or the file is encrypted"
               if name == "pdf" else "no text in it")
        raise Unreadable(f"{name}: {why}")
    return name, text
