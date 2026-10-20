#!/usr/bin/env python3
"""The published tree carries no private tracking references, internal names
or contact details.

Container-free: APP_ROOT=<repo> python3 scripts/test_public_source_clean.py [path-prefix ...]
Reads files, runs nothing. Optional path prefixes narrow the scan (for working
on one area); the gate in the lanes always scans the whole tree.

A built image's /app, copied out:
    APP_ROOT=<copy of /app> python3 scripts/test_public_source_clean.py --source=<repo>
The image holds files the tree does not (what the build generates and bakes),
so they are scanned as well. A document the image names may be one the source
repository carries, which a reader can open, so document names resolve
against <repo> too. ALLOW entries need not all match: the image holds part of
the tree, and the tree's own run checks that every entry is still needed.

This source is read by people who have none of the trackers, plans or Hosts
it was built alongside. A comment that points at a ticket, a plan document or
a Host they cannot open explains nothing to them, and some of those pointers
identify people. So every comment states its reason in place. This fails on:

  ticket    ticket ids: T-, R- or BT- followed by digits
  pr        pull request numbers: "PR", "review of" and the like before a
            number sign and digits, a pair of them, or one in parentheses
  doc-ref   a PLAN-... or DESIGN-... document, or any other capitalised .md
            file name, that is not in this tree
  entity-id an entity id from RESERVED_ENTITY_IDS in entity-N, entity_id=N or
            entity N form; examples and tests use ids of 9000 and above
  tool      the name of an internal tool, service or repository (TOOL_NAMES)
  old-name  a name the project used before it had this one (OLD_NAMES)
  mailbox   an address at a personal mail provider; examples use example.com
  phone     a North American phone number, unless its area code or exchange
            is 555 (the range set aside for fiction)
  provenance a claim about where text or data came from: a Host, a user, a
            transcript or a run. Examples are written for the purpose; a test
            that needs a real shape says what the shape is, not whose it was.

TOOL_NAMES and OLD_NAMES are written in pieces so this file never spells the
words it refuses. Words on the maintainers' own list are checked separately,
at release, by scripts/release_term_check.py against digests kept outside the
repository.

Every tracked text file is scanned, file names included, and every member of
each .cseries bundle, since a bundle is what installs. LICENSE, NOTICE,
third_party/ and every THIRD-PARTY-* file or bundle member hold other
projects' text as it was published and are not scanned.

A legitimate hit goes in ALLOW with its reason. An ALLOW entry that matches
nothing fails too, so the list cannot outlive the line it was written for.

Exit 0 = clean; exit 1 + FAIL lines otherwise.
"""

import os
import re
import subprocess
import sys
import zipfile

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── what is refused ──────────────────────────────────────────────────────────

# Ticket numbers are four digits or zero-padded to three, so arithmetic on a
# variable named T ("T-240") is not one.
TICKET = re.compile(r"\b(?:T-(?:0\d{2}|\d{4,5})|R-\d{2,4}|BT-\d+)\b")
PR = re.compile(r"\bPRs?\s*#\d+|\(#\d{3,4}(?:\)|[,;]\s)|#\d{3,4}\s*(?:/|and|,\s)\s*#\d{3,4}\b"
                r"|\b(?:of|renovate|cseries|findings?|reviewing|merged)\s+#\d{3,4}\b", re.I)
DOC_REF = re.compile(r"\b((?:PLAN|DESIGN)-[A-Za-z0-9][A-Za-z0-9_.-]*)")
# Any other hyphenated document named in capitals (AUDIT-…, PROPOSAL-…): refused
# when no file of that name is in the tree. One-word names (README.md,
# LICENSE.md, a skill's DESIGN.md) are conventions, not references.
DOC_FILE = re.compile(r"\b([A-Z][A-Z0-9]+-[A-Za-z0-9][A-Za-z0-9_.-]*\.md)\b")

RESERVED_ENTITY_IDS = (3, 102, 122, 123, 128, 163, 164)
_IDS = "|".join(str(i) for i in RESERVED_ENTITY_IDS)
ENTITY_ID = re.compile(
    r"\bentit(?:y|ies)(?:[-_ ]|_id\s*==?\s*|_id[\"']?\s*:\s*|\s+id\s+)#?(?:%s)\b" % _IDS, re.I)


def _alt(*names: str) -> str:
    return "|".join(re.escape(n) for n in names)


# Each name is split so a search of this file does not find it.
TOOL_NAMES = [
    ("op" "test", "operator test harness"),
    ("para" "llax", "legacy database name"),
    ("privatae-bas" "tion-dev", "private repository"),
    ("owner/" "sentinel", "private repository"),
    ("privatae-" "api", "platform service"),
    ("fable" " review", "review tool"),
]
TOOL = [(re.compile(r"(?<![A-Za-z0-9])" + re.escape(n).replace(r"\ ", r"\s+"), re.I), label)
        for n, label in TOOL_NAMES]
# Matched at the start of a word or of a camelCase part, so compounds are
# caught too.
OLD_NAMES = ("sk" "ema", "li" "men", "den" "zi")
OLD_NAME = re.compile(r"(?:(?<![A-Za-z])|(?<=[a-z])(?=[A-Z]))(?i:%s)" % _alt(*OLD_NAMES))

_PERSONAL_MAIL = ("gmail", "googlemail", "yahoo", "ymail", "outlook", "hotmail", "live", "msn",
                  "icloud", "me", "mac", "aol", "proton", "protonmail", "pm", "gmx", "zoho",
                  "yandex", "fastmail", "hey", "tutanota", "tuta", "comcast", "verizon",
                  "sbcglobal", "att")
MAILBOX = re.compile(r"\b[A-Za-z0-9._%%+-]+@(?:%s)\.(?:com|net|org|me|io|ch|de|ru|co\.uk)\b"
                     % _alt(*_PERSONAL_MAIL), re.I)

# +1 and ten digits, + and ten digits, (NPA) NXX-XXXX, or NPA-NXX-XXXX with one
# separator throughout. Ten bare digits are not read as a number: sizes,
# masks and timestamps look the same.
PHONE = re.compile(r"(?<![\w.+/-])(?:\+1?[ .-]?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}"
                   r"|\(\d{3}\)[ .-]?\d{3}[.-]\d{4}|\d{3}([.-])\d{3}\1\d{4})(?![\w/-])")


def phone_is_real(text: str) -> bool:
    digits = re.sub(r"\D", "", text)[-10:]
    area, exchange = digits[:3], digits[3:6]
    return "555" not in (area, exchange) and area[:1] not in "01"


_SOURCE = (r"(?:a |an |the |one |his |her |their )?(?:live |real |production )?"
           r"(?:Host|user|owner|operator|customer|transcript|chat|conversation|incident|inbox|run|session)s?"
           r"(?![\w./-])")
PROVENANCE = re.compile(
    r"\b(?:verbatim|copied|captured|recorded|quoted|exported|sampled)(?: verbatim| live)? from " + _SOURCE
    + r"|\breal (?:rows?|data|messages?|replies|turns?|transcripts?|emails?|conversations?) (?:from|of) " + _SOURCE
    + r"|\b(?:a|one) real Host's\b|\bcopy of (?:a|one) (?:live|real) Host's\b", re.I)

# Legitimate hits: (path regex, class, needle, why). The needle must appear on
# the line, or be the finding's own detail (for a tool name, its label), so an
# entry never has to spell out a refused word.
ALLOW: list[tuple[str, str, str, str]] = [
    # The Host's database had another name before it was renamed. The boot
    # scripts still probe for it, so a Host created before the rename finds its
    # memory; the restore, recall and harness paths and their tests name it for
    # the same reason.
    (r"^entrypoint(-postgres)?\.sh$", "tool", "legacy database name", "legacy database name, probed at boot"),
    (r"^server/(brain_service|code_harness|host_restore)\.py$", "tool", "legacy database name", "legacy database name"),
    (r"^scripts/(kernel_schema|test_no_hand_kernel_tables)\.py$", "tool", "legacy database name", "legacy database name"),
    (r"^tests/test_bastion_control_pool_reserved\.py$", "tool", "legacy database name", "legacy database name"),
    # The wiki leak scanner's own test: page sentences it must reject.
    (r"^scripts/test_wiki_pages\.py$", "pr", "Fixed in PR", "a page sentence the wiki scanner must refuse"),
    (r"^scripts/test_wiki_pages\.py$", "doc-ref", "Per DESIGN-", "a page sentence the wiki scanner must refuse"),
]

NOT_SCANNED_PREFIXES = ("third_party/",)
NOT_SCANNED_FILES = {"LICENSE", "NOTICE", "THIRD-PARTY-NOTICES.md"}
NOT_SCANNED_MEMBER_PREFIX = "THIRD-PARTY"
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2",
              ".ttf", ".otf", ".eot", ".pdf", ".gz", ".tgz", ".zip", ".wasm",
              ".mp3", ".mp4", ".webm", ".ogg", ".wav", ".bin", ".der", ".raw"}
BUNDLE_EXT = ".cseries"


def doc_stems(paths: list[str]) -> set[str]:
    """Stems of the PLAN-/DESIGN- documents in the tree, plus every file's
    base name (for DOC_FILE), kept apart by a leading slash."""
    stems = set()
    for p in paths:
        base = os.path.basename(p)
        stems.add("/" + base)
        if base.startswith(("PLAN-", "DESIGN-")):
            stems.add(re.sub(r"\.md$", "", base))
    return stems


def doc_ref_missing(ref: str, stems: set[str]) -> bool:
    ref = re.sub(r"(\.md)?[.\-_]*$", "", ref)
    return not any(s == ref or s.startswith(ref + "-") for s in stems)


def line_findings(line: str, stems: set[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for m in TICKET.finditer(line):
        out.append(("ticket", m.group(0)))
    for m in PR.finditer(line):
        out.append(("pr", m.group(0)))
    for m in DOC_REF.finditer(line):
        if doc_ref_missing(m.group(1), stems):
            out.append(("doc-ref", m.group(1)))
    for m in DOC_FILE.finditer(line):
        name = m.group(1)
        if not name.startswith(("PLAN-", "DESIGN-")) and "/" + name not in stems:
            out.append(("doc-ref", name))
    for m in ENTITY_ID.finditer(line):
        out.append(("entity-id", m.group(0)))
    for rx, label in TOOL:
        if rx.search(line):
            out.append(("tool", label))
    if OLD_NAME.search(line):
        out.append(("old-name", "a former project name"))
    for m in MAILBOX.finditer(line):
        out.append(("mailbox", m.group(0)))
    for m in PHONE.finditer(line):
        if phone_is_real(m.group(0)):
            out.append(("phone", m.group(0)))
    for m in PROVENANCE.finditer(line):
        out.append(("provenance", m.group(0)))
    return out


# ── reading the tree ─────────────────────────────────────────────────────────

def tracked_files(root: str) -> list[str]:
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True,
                             check=True, timeout=60).stdout
        files = [p for p in out.decode("utf-8", "replace").split("\0") if p]
        if files:
            return files
    except (OSError, subprocess.SubprocessError):
        pass
    files = []
    for d, dirs, names in os.walk(root):
        dirs[:] = [x for x in dirs if x not in (".git", "node_modules")]
        for n in names:
            files.append(os.path.relpath(os.path.join(d, n), root))
    return files


def is_text(data: bytes) -> bool:
    return b"\0" not in data[:8192]


def texts(root: str, rel: str):
    """(label, text) for one tracked path: the file, or each bundle member."""
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        return
    ext = os.path.splitext(rel)[1].lower()
    if ext == BUNDLE_EXT:
        try:
            with zipfile.ZipFile(path) as z:
                for name in z.namelist():
                    if os.path.basename(name).startswith(NOT_SCANNED_MEMBER_PREFIX):
                        continue
                    data = z.read(name)
                    if os.path.splitext(name)[1].lower() in BINARY_EXT or not is_text(data):
                        continue
                    yield f"{rel}!{name}", data.decode("utf-8", "replace")
        except zipfile.BadZipFile:
            yield rel, "unreadable bundle"
        return
    if ext in BINARY_EXT:
        return
    with open(path, "rb") as f:
        data = f.read()
    if is_text(data):
        yield rel, data.decode("utf-8", "replace")


def scanned(rel: str) -> bool:
    return (rel not in NOT_SCANNED_FILES and not rel.startswith(NOT_SCANNED_PREFIXES)
            and not os.path.basename(rel).startswith(NOT_SCANNED_MEMBER_PREFIX))


def allowed(label: str, cls: str, line: str, what: str, used: set[int]) -> bool:
    for i, (path_re, a_cls, needle, _why) in enumerate(ALLOW):
        if a_cls == cls and (needle in line or needle == what) and re.search(path_re, label):
            used.add(i)
            return True
    return False


# ── self-test: the detector is not vacuous ───────────────────────────────────

def self_test() -> list[str]:
    """Each class fires on a planted line and stays quiet on its look-alikes.
    The planted lines are written with a "|" that is removed before they are
    read, so this file does not match its own patterns when it is scanned."""
    def live(s: str) -> str:
        return s.replace("|", "")

    fails: list[str] = []
    stems = {live("DESIGN|-in-tree-2026-01-01"), "/README.md", live("/NOTES|-HERE.md")}

    def classes(line):
        return {c for c, _ in line_findings(line, stems)}

    ids = RESERVED_ENTITY_IDS
    positives = [(c, live(s)) for c, s in [
        ("ticket", "# fixed in T|-1234 last week"), ("ticket", "(T|-091)"), ("ticket", "see R|-12 in the model"),
        ("ticket", "the BT|-7 cap"), ("pr", "landed in PR |#431"), ("pr", "(|#812) moved it"),
        ("pr", "found in review of |#770"), ("pr", "two of them, |#836 and |#837"), ("pr", "(renovate |#300)"),
        ("doc-ref", "see DESIGN|-not-here §2"),
        ("doc-ref", "docs/PLAN|-gone-2026-01-01.md"), ("doc-ref", "see AUDIT|-2026-01-01-gone.md"), ("entity-id", "entity_id=%d," % ids[0]),
        ("entity-id", '"entity_id": %d' % ids[1]), ("entity-id", "Entity %d's app" % ids[-1]),
        ("entity-id", "repo entity-%d/p1" % ids[2]),
        ("tool", "caught by op|test's lane"), ("tool", "OP|TEST_MARKER"), ("tool", "the para|llax db"),
        ("tool", "in privatae-bas|tion-dev"), ("tool", "see owner/|sentinel"), ("tool", "the privatae-|api pod"),
        ("tool", "(fable |review B1)"), ("tool", "Fable  |Review"),
        ("old-name", "the sk|ema kernel"), ("old-name", "OldSk|emaPath"), ("old-name", "li|men_root"),
        ("old-name", "den|zibot image"), ("old-name", "acme/sk|ema"),
        ("mailbox", "mail al.ex+x@gm|ail.com now"), ("mailbox", "<a@proton|mail.com>"), ("mailbox", "x@i|cloud.com"),
        ("phone", "call +1|3014127788 now"), ("phone", "+|3014127788"), ("phone", "(301) 4|12-7788"),
        ("phone", "301-4|12-7788"), ("phone", "301.4|12.7788"), ("phone", "+1 301 4|12 7788"),
        ("provenance", "real rows from a |Host's db"), ("provenance", "verbatim from the trans|cript"),
        ("provenance", "Captured live from one H|ost"), ("provenance", "none of a real H|ost's turns"),
        ("provenance", "a copy of a live H|ost's data"), ("provenance", "cop|ied from the user's inbox"),
    ]]
    negatives = [live(s) for s in [
        "GPT-4 and SHA-256 and UTF-8", "color: #333;", "bg: \"#123\", fg: \"#000\"", "linear-gradient(#123,#456)", "Host #9003 and Host #9128", "entity_id=9003", "entity_id=30",
        "entity 1023", "see DESIGN|-in-tree §2", "docs/DESIGN|-in-tree-2026-01-01.md",
        "see README.md, LICENSE.md and NOTES|-HERE.md", "T-12 minus", "if(T>240){ x=(T|-240)/2400 }",
        "a schema, a lemon, a dozen", "sub" + "li" + "men", "the parallel lane", "an optimistic test", "privatae-bastion",
        "owner/widget", "privatae-apps", "a fable", "reviewed",
        "a@example.com", "x@mail.example.test", "me@acme.com", "user@livestock.org",
        "+15555550100", "+1 555 010 0003", "(555) 010-0100", "301-555-0142", "4294967295", "584 555 0142",
        "2026-10-18", "1.2.3.4", "+44 20 7946 0958", "127-0-0-1",
        "the real rows in Postgres", "copied verbatim from the SQL", "a card taken from a Host we accepted",
        "moved verbatim from session.py", "a project copied from owner/widget", "a live Host's pod",
        "real data of the test",
    ]]
    for cls, line in positives:
        if cls not in classes(line):
            fails.append(f"self-test: {cls} not found in {line!r}")
    for line in negatives:
        if classes(line):
            fails.append(f"self-test: false hit {classes(line)} in {line!r}")
    return fails


# ── main ─────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    only = [a.rstrip("/") for a in argv if not a.startswith("-")]
    source = next((a.split("=", 1)[1] for a in argv if a.startswith("--source=")), "")
    fails = self_test()
    for f in fails:
        print(f"  FAIL  {f}")
    files = tracked_files(APP_ROOT)
    if source:
        source_files = tracked_files(source)
        if not source_files:
            print(f"  FAIL  --source={source} holds no files")
            return 1
        stems = doc_stems(files + source_files)
    else:
        stems = doc_stems(files)
    used: set[int] = set()
    findings: list[str] = []
    counts: dict[str, int] = {}
    n_texts = 0
    for rel in sorted(files):
        if not scanned(rel):
            continue
        if only and not any(rel == p or rel.startswith(p + "/") for p in only):
            continue
        for cls, what in line_findings(rel, stems):
            if not allowed(rel, cls, rel, what, used):
                findings.append(f"{rel}  [{cls}: {what}]  (file name)")
                counts[cls] = counts.get(cls, 0) + 1
        for label, text in texts(APP_ROOT, rel):
            n_texts += 1
            for i, line in enumerate(text.splitlines(), 1):
                for cls, what in line_findings(line, stems):
                    if allowed(label, cls, line, what, used):
                        continue
                    findings.append(f"{label}:{i}  [{cls}: {what}]  {line.strip()[:140]}")
                    counts[cls] = counts.get(cls, 0) + 1
    for f in findings:
        print(f"  FAIL  {f}")
    if not only and not source:
        for i, entry in enumerate(ALLOW):
            if i not in used:
                fails.append(f"ALLOW entry matches nothing, remove it: {entry[:3]}")
                print(f"  FAIL  ALLOW entry matches nothing, remove it: {entry[:3]}")
    if findings or fails:
        summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        print(f"FAIL  {len(findings)} finding(s) in {n_texts} texts ({summary}); "
              f"{len(fails)} other failure(s)")
        return 1
    print(f"ok    {n_texts} texts clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
