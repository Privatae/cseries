#!/usr/bin/env python3
"""Release step: refuse a tree that contains a listed term.

Run on the release machine before a push; it is not a gate in the public
lanes. The list lives outside the repository as keyed digests (HMAC-SHA256
with the maintainers' key), so neither the terms nor anything that can be
tested against guesses is published with the source.

    release_term_check.py --digests <file> [--key <file>] [--root <dir>]

--digests  a file holding the digests, 32 hex characters each, in any layout
--key      the HMAC key, as hex (default $NO_PERSONAL_DATA_KEY_FILE, else
           /etc/privatae/keys/no-personal-data.hmac)
--root     the tree to scan (default: this checkout)

Text is normalised the way the digests are made: lowercase runs of letters
and digits, and every run of one to three consecutive tokens on a line is
checked, file names included. Every tracked text file is scanned, and every
member of each .cseries bundle. Each hit prints as `path:line: listed term`;
the term and the line are never printed.

A line is read three ways, and a term found in any of them counts:
  - as written;
  - with each backslash escape (\\n, \\t, \\b, \\x41, \\u00e9 ...) read as a
    space, so a term written straight after "\\n" in a string literal is not
    glued to the escape's letter. The first reading still holds, so a term
    after a Windows path separator is found too;
  - through every base64 or base64url run of 24 or more characters in it that
    decodes to text (UTF-8, at least 90% printable): the decoded text is read
    the same three ways, two levels deep. Signed payloads, data URIs and
    encoded fixtures carry text no other reading can see.

Exit 0 = clean; 1 = a listed term was found; 2 = the key or digests are missing.
"""

import argparse
import base64
import binascii
import hmac
import os
import re
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_KEY = "/etc/privatae/keys/no-personal-data.hmac"
MAX_TOKENS = 3
# Other projects' text, kept verbatim; their authors' names are theirs.
NOT_SCANNED_PREFIXES = ("third_party/",)
NOT_SCANNED_FILES = {"LICENSE", "NOTICE", "THIRD-PARTY-NOTICES.md"}
NOT_SCANNED_MEMBERS = ("THIRD-PARTY-LICENSES.txt",)
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2",
              ".ttf", ".otf", ".eot", ".pdf", ".gz", ".tgz", ".zip", ".wasm",
              ".mp3", ".mp4", ".webm", ".ogg", ".wav", ".bin", ".der", ".raw"}

# A backslash escape as a string literal or a pattern spells it: \xHH, \uHHHH,
# \UHHHHHHHH, or a backslash and one letter or digit.
ESCAPE = re.compile(r"\\(?:x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|[A-Za-z0-9])")
# A run of the base64 alphabet, standard or url-safe, long enough to hold a
# word: 24 characters decode to 18 bytes.
BASE64_RUN = re.compile(r"[A-Za-z0-9+/_-]{24,}={0,2}")
_URLSAFE = str.maketrans("-_", "+/")
BASE64_DEPTH = 2


def base64_texts(line: str):
    """The decoded text of each base64 run in the line that decodes to text."""
    for m in BASE64_RUN.finditer(line):
        run = m.group(0).rstrip("=").translate(_URLSAFE)
        if len(run) % 4 == 1:
            run = run[:-1]
        try:
            text = base64.b64decode(run + "=" * (-len(run) % 4)).decode("utf-8")
        except (binascii.Error, ValueError):
            continue
        if text and sum(c.isprintable() or c in "\t\r\n" for c in text) >= 0.9 * len(text):
            yield text


def readings(line: str, depth: int = 0):
    """The line as written, with its escapes read as spaces, and the text
    base64 in it decodes to, read the same way."""
    yield line
    if "\\" in line:
        yield ESCAPE.sub(" ", line)
    if depth < BASE64_DEPTH:
        for text in base64_texts(line):
            for sub in text.splitlines():
                yield from readings(sub, depth + 1)


class Matcher:
    def __init__(self, key: bytes, digests: set[str]):
        self.key = key
        self.digests = digests
        self._seen: dict[str, bool] = {}

    def _listed(self, gram: str) -> bool:
        hit = self._seen.get(gram)
        if hit is None:
            hit = hmac.new(self.key, gram.encode(), "sha256").hexdigest()[:32] in self.digests
            self._seen[gram] = hit
        return hit

    def _tokens_have_term(self, text: str) -> bool:
        toks = re.findall(r"[a-z0-9]+", text.lower())
        for n in range(1, MAX_TOKENS + 1):
            for i in range(len(toks) - n + 1):
                if self._listed(" ".join(toks[i:i + n])):
                    return True
        return False

    def line_has_term(self, line: str) -> bool:
        return any(self._tokens_have_term(r) for r in readings(line))


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
        files += [os.path.relpath(os.path.join(d, n), root) for n in names]
    return files


def texts(root: str, rel: str):
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        return
    ext = os.path.splitext(rel)[1].lower()
    if ext == ".cseries":
        try:
            with zipfile.ZipFile(path) as z:
                for name in z.namelist():
                    if os.path.basename(name) in NOT_SCANNED_MEMBERS:
                        continue
                    if os.path.splitext(name)[1].lower() in BINARY_EXT:
                        continue
                    data = z.read(name)
                    if b"\0" not in data[:8192]:
                        yield f"{rel}!{name}", data.decode("utf-8", "replace")
        except zipfile.BadZipFile:
            pass
        return
    if ext in BINARY_EXT:
        return
    with open(path, "rb") as f:
        data = f.read()
    if b"\0" not in data[:8192]:
        yield rel, data.decode("utf-8", "replace")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--digests", required=True)
    ap.add_argument("--key", default=os.environ.get("NO_PERSONAL_DATA_KEY_FILE", DEFAULT_KEY))
    ap.add_argument("--root", default=ROOT)
    a = ap.parse_args(argv)
    try:
        key = bytes.fromhex(open(a.key).read().strip())
        digests = set(re.findall(r"\b[0-9a-f]{32}\b", open(a.digests).read()))
    except (OSError, ValueError) as e:
        print(f"release term check: cannot read the key or the digests: {e}", file=sys.stderr)
        return 2
    if not key or not digests:
        print("release term check: the key or the digest list is empty", file=sys.stderr)
        return 2
    m = Matcher(key, digests)
    hits = 0
    n_texts = 0
    for rel in sorted(tracked_files(a.root)):
        if rel in NOT_SCANNED_FILES or rel.startswith(NOT_SCANNED_PREFIXES):
            continue
        if m.line_has_term(rel):
            print(f"{rel}:0: listed term (in the file name)")
            hits += 1
        for label, text in texts(a.root, rel):
            n_texts += 1
            for i, line in enumerate(text.splitlines(), 1):
                if m.line_has_term(line):
                    print(f"{label}:{i}: listed term")
                    hits += 1
    if hits:
        print(f"release term check: {hits} line(s) carry a listed term ({n_texts} texts scanned)")
        return 1
    print(f"release term check: clean ({n_texts} texts, {len(digests)} listed terms)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
