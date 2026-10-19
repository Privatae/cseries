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

Exit 0 = clean; 1 = a listed term was found; 2 = the key or digests are missing.
"""

import argparse
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

    def line_has_term(self, line: str) -> bool:
        toks = re.findall(r"[a-z0-9]+", line.lower())
        for n in range(1, MAX_TOKENS + 1):
            for i in range(len(toks) - n + 1):
                if self._listed(" ".join(toks[i:i + n])):
                    return True
        return False


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
