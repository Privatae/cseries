#!/usr/bin/env python3
"""Gate: the restore that runs in the owner's browser (system-apps/settings/restore.js).

THE RULE: the platform holds a backup OR a key, never both.
To restore, the owner's browser opens a backup's sealed key file with their
Host recovery key and seals it again to the Host; only that resealed file is
sent. This gate:

  * checks the vendored age library is the pinned one (vendor/VERSIONS sha256);
  * runs the module under node with a recording fetch and a captured console
    (scripts/test_settings_restore_browser.mjs): the resealed file opens with
    the Host's key and is the same key file; no request and no log line carries
    the recovery key; every refusal says why without repeating the key;
  * proves the library and the `age` binary open each other's files, so what
    the browser seals is what the Host (age binary) opens.

    python3 scripts/test_settings_restore_browser.py
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAILED = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  [{str(detail)[:400]}]"))
    if not cond:
        FAILED.append(label)


vendor = ROOT / "system-apps/settings/vendor"
pinned = re.search(r"sha256 \(age-encryption.js\):\s*\n\s*([0-9a-f]{64})", (vendor / "VERSIONS").read_text())
got = hashlib.sha256((vendor / "age-encryption.js").read_bytes()).hexdigest()
check("the vendored age library is the pinned build", pinned and pinned.group(1) == got, got)
check("its licences ship with it",
      all((vendor / f).is_file() for f in ("LICENSE-age-encryption", "LICENSE-noble-hashes",
                                           "LICENSE-noble-curves", "LICENSE-noble-ciphers",
                                           "LICENSE-noble-post-quantum", "LICENSE-scure-base")))
bundled = set(re.findall(r"node_modules/((?:@[a-z0-9-]+/)?[a-z0-9-]+)/(?!node_modules)",
                         (vendor / "age-encryption.js").read_text()))
nested = set(re.findall(r"node_modules/[^ ]*?/node_modules/((?:@[a-z0-9-]+/)?[a-z0-9-]+)/",
                        (vendor / "age-encryption.js").read_text()))
listed = (vendor / "VERSIONS").read_text()
check("VERSIONS names every package the bundle holds, nested ones included",
      bundled and all(p in listed for p in bundled | nested)
      and listed.count("@noble/curves") >= 2 and listed.count("@noble/hashes") >= 2, sorted(bundled | nested))
page = (ROOT / "system-apps/settings/index.html").read_text()
appjs = (ROOT / "system-apps/settings/app.js").read_text()
ack = re.search(r"var RESTORE_ACK = ((?:'[^']*'\s*\+?\s*)+);", appjs)
ack_text = "".join(re.findall(r"'([^']*)'", ack.group(1))) if ack else ""
check("before a restore the owner is told plainly what comes back and what does not",
      ack and "credentials" in ack_text and "including any credential I deleted since" in ack_text
      and "apps I uninstalled since come back" in ack_text
      and "Access stays as it is now" in ack_text and "second factor" in ack_text
      and "bk-restore-ack-text').textContent = RESTORE_ACK" in appjs, ack_text)
check("the Settings page loads the module", 'src="/system-apps/settings/restore.js"' in page
      and 'type="module"' in page)

node = shutil.which("node")
check("node is available to run the module", node is not None)
interop = None
if node:
    proc = subprocess.run([node, str(ROOT / "scripts/test_settings_restore_browser.mjs")],
                          capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    for line in proc.stdout.splitlines():
        if line.startswith("INTEROP "):
            interop = json.loads(line[len("INTEROP "):])
        else:
            print(line)
    check("the browser module's own checks pass", proc.returncode == 0, proc.stderr[-400:])

age = os.environ.get("AGE_BIN") if os.environ.get("AGE_BIN") and Path(os.environ["AGE_BIN"]).is_file() \
    else shutil.which("age")
check("the age binary is available for the interop check", age is not None)
if age and interop:
    rfd, wfd = os.pipe()
    os.write(wfd, (interop["host"] + "\n").encode())
    os.close(wfd)
    try:
        p = subprocess.run([age, "-d", "-i", f"/dev/fd/{rfd}"], input=interop["sidecarForHost"].encode(),
                           capture_output=True, pass_fds=(rfd,), timeout=60)
    finally:
        os.close(rfd)
    check("the age binary (the Host's) opens what the browser sealed, and it is the same key file",
          p.returncode == 0 and p.stdout.decode() == interop["body"], p.stderr[-200:])

print()
# Settings shows the links an unverified restore left out, in the restore card
# and in the run list (app.js leftOut -> restore.js linksLeftOutSentence).
app_js = (ROOT / "system-apps/settings/app.js").read_text()
check("the Backup tab says which links a restore left out, in the restore card and in the run list",
      "S.linksLeftOutSentence(links, count)" in app_js
      and "leftOut(rs.links_left_out, rs.links_left_out_count)" in app_js
      and "leftOut(p.links_left_out, p.links_left_out_count)" in app_js)
# A restore keeps this Host's own sign-in; when that differs from what the
# owner expects (another email in the backup, or no owner yet), the restore's
# owner note (host_restore.owner_note, kept as parts.restored.owner and the
# progress's owner) is said in both places too.
check("the Backup tab says the restore's owner note, in the restore card and in the run list",
      "(rs.owner ? ' ' + rs.owner : '')" in app_js
      and "if (p.owner) lines.push(p.owner);" in app_js)

# A name in a crafted backup could hold a line break, and
# the run list is shown with white-space: pre-line, so it added lines of its
# own. Each status line is made one line before the lines are joined.
check("every backup status is joined through says(), which makes each line one line",
      "lines.join(" not in app_js and app_js.count("says(lines, ") >= 6
      and "box.textContent = says(lines, '\\n');" in app_js, app_js.count("says(lines, "))
_a, _b = app_js.find("    function oneLine("), app_js.find("    function sizeOf(")
_probe = subprocess.run(
    ["node", "-e", "const f = new Function(require('fs').readFileSync(0, 'utf8') + '; return says;')();"
     "const c = String.fromCharCode;"
     "process.stdout.write(JSON.stringify(f(['a' + c(10) + 'Restored.', 'b' + c(13) + c(0x2028) + 'x'"
     " + c(0x202e) + c(0x85) + 'y', 'caf' + c(0xe9)], c(10))));"],
    input=app_js[_a:_b] if _a > 0 and _b > _a else "function says(){return ''}",
    capture_output=True, text=True, timeout=60)
check("  a name with a line break, a line separator or a bidi control shows on the one line",
      _probe.returncode == 0 and json.loads(_probe.stdout or '""') == "a Restored.\nb  x  y\ncaf" + chr(0xe9),
      (_probe.stdout, _probe.stderr[-300:]))
# The bidi marks (U+200E, U+200F, U+061C) reorder text
# too and were missing. Settings and the backup writer must agree on the set.
sys.path.insert(0, str(ROOT))
from server import host_backup as _hb  # noqa: E402
_a1, _b1 = app_js.find("    function oneLine("), app_js.find("    function says(")
_probe = subprocess.run(
    ["node", "-e", "const f = new Function(require('fs').readFileSync(0, 'utf8') + '; return oneLine;')();"
     "const out = []; for (let c = 0; c < 0x3000; c++) { if (f(String.fromCharCode(c)) !== String.fromCharCode(c)) out.push(c); }"
     "process.stdout.write(JSON.stringify(out));"],
    input=app_js[_a1:_b1] if _a1 > 0 and _b1 > _a1 else "function oneLine(s){return s}",
    capture_output=True, text=True, timeout=60)
_js = set(json.loads(_probe.stdout or "[]"))
_py = {c for c in range(0x3000) if _hb.unshowable(chr(c))}
check("Settings replaces exactly the characters a backup leaves out, the bidi marks included",
      _probe.returncode == 0 and _js == _py and {0x200e, 0x200f, 0x61c} <= _py,
      (sorted(_js ^ _py)[:10], _probe.stderr[-200:]))

if FAILED:
    print(f"FAILED: {len(FAILED)}")
    sys.exit(1)
print("OK: the browser restore keeps the recovery key in the page")
