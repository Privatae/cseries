#!/usr/bin/env python3
"""scripts/release_term_check.py finds a listed term however the tree spells it.

Container-free: APP_ROOT=<repo> python3 scripts/test_release_term_check.py
Builds throwaway trees in a temporary directory, with a random key and
digests of invented terms, and runs the release check on each. Nothing here
is a real term, and the key is made fresh each run.

Pins:
  1. a term as plain text, in a file name and in a .cseries bundle member is
     found, and a clean tree passes;
  2. a term written straight after a backslash escape in a literal (\\n, \\t,
     a pattern's \\b, \\x20) is found, and so is one after a Windows path
     separator;
  3. a term inside base64 is found: standard and url-safe alphabets, with and
     without padding, base64 inside JSON inside base64, and base64 in a
     bundle member;
  4. base64 that decodes to bytes rather than text, hex digests and long
     paths raise nothing;
  5. a hit prints the path and line only; the term is not printed unless it
     is in the path itself.
"""
from __future__ import annotations

import base64
import contextlib
import hmac
import importlib.util
import io
import json
import os
import sys
import tempfile
import zipfile

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("release_term_check",
                                              os.path.join(APP_ROOT, "scripts", "release_term_check.py"))
rtc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rtc)

TERMS = ("zorblaxquent", "quilmar vesk")
KEY = os.urandom(32)
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  - {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def b64(s: str, urlsafe: bool = False, pad: bool = True) -> str:
    out = (base64.urlsafe_b64encode if urlsafe else base64.b64encode)(s.encode()).decode()
    return out if pad else out.rstrip("=")


def run(files: dict[str, str | bytes]) -> tuple[int, str]:
    """Exit code and output of the release check over a tree of these files."""
    with tempfile.TemporaryDirectory() as d:
        tree = os.path.join(d, "tree")
        for rel, body in files.items():
            path = os.path.join(tree, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(body if isinstance(body, bytes) else body.encode())
        os.makedirs(tree, exist_ok=True)
        key_file, dig_file = os.path.join(d, "key"), os.path.join(d, "digests")
        with open(key_file, "w") as f:
            f.write(KEY.hex())
        with open(dig_file, "w") as f:
            f.write("\n".join(hmac.new(KEY, t.encode(), "sha256").hexdigest()[:32] for t in TERMS))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = rtc.main(["--digests", dig_file, "--key", key_file, "--root", tree])
        return code, out.getvalue()


def bundle(members: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, body in members.items():
            z.writestr(name, body)
    return buf.getvalue()


def found(name: str, files: dict[str, str | bytes], where: str, quiet: bool = True) -> None:
    """The check exits 1 naming `where`; unless the hit is in a path (which is
    printed as it is), the output does not hold the term."""
    code, out = run(files)
    check(name, code == 1 and where in out, f"exit {code}: {out.strip()[:200]}")
    if quiet:
        check(f"{name}: the term is not printed", not any(t.split()[0] in out.lower() for t in TERMS), out[:200])


CLEAN = "def f():\n    return 'nothing listed here'\n"

print("1. plain text, file names, bundles")
code, out = run({"a.py": CLEAN})
check("a clean tree passes", code == 0, out.strip())
found("a plain term", {"a.py": "x = 'the Zorblaxquent ledger'\n"}, "a.py:1")
found("a two-word term across punctuation", {"a.py": "QUILMAR-vesk\n"}, "a.py:1")
found("a term in a file name", {"docs/zorblaxquent-notes.md": "clean\n"}, "(in the file name)", quiet=False)
found("a term in a bundle member", {"app.cseries": bundle({"app.yaml": "name: zorblaxquent\n"})}, "app.cseries!app.yaml:1")

print("2. backslash escapes")
found("after \\n in a literal", {"t.py": 'reply = "done.\\n\\nZorblaxquent landed."\n'}, "t.py:1")
found("after \\t", {"t.py": 'row = "id\\tquilmar vesk"\n'}, "t.py:1")
found("inside a pattern's \\b", {"t.py": 're.compile(r"\\bzorblaxquent\\b")\n'}, "t.py:1")
found("after a \\x20 escape", {"t.py": 's = "x\\x20zorblaxquent"\n'}, "t.py:1")
found("after a Windows path separator", {"t.ps1": "C:\\Users\\zorblaxquent\\Desktop\n"}, "t.ps1:1")

print("3. base64")
src = '{"image": "registry.example/owner/app", "source": "https://forge.example/zorblaxquent/repo"}'
found("standard base64 of JSON", {"fx.json": json.dumps({"payload": b64(src)}) + "\n"}, "fx.json:1")
found("url-safe base64 without padding", {"fx.txt": "token=" + b64("owner: quilmar vesk, ~~~?>>", True, False) + "\n"},
      "fx.txt:1")
inner = json.dumps({"payloadType": "x", "payload": b64("line one\nby zorblaxquent\n")})
found("base64 inside JSON inside base64", {"fx.json": '{"envelope": "' + b64(inner) + '"}\n'}, "fx.json:1")
found("a term after an escape inside decoded base64", {"fx.json": '"' + b64('{"m": "done\\nzorblaxquent"}') + '"\n'},
      "fx.json:1")
found("base64 in a bundle member", {"app.cseries": bundle({"seed.json": '{"p": "' + b64(src) + '"}'})},
      "app.cseries!seed.json:1")

print("4. what is not text raises nothing")
binary = base64.b64encode(bytes(range(256)) * 2).decode()
code, out = run({"a.json": json.dumps({"png": binary, "clean": b64("a sentence with nothing listed in it")}) + "\n",
                 "b.txt": "sha256:" + "ab" * 32 + "\n" + "/srv/example/some/long/path/to_a_file_name.py\n"})
check("binary base64, digests and paths pass", code == 0, out.strip())

if failures:
    print(f"FAIL: {len(failures)} check(s)")
    sys.exit(1)
print("OK: all checks passed")
