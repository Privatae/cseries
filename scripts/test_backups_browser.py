#!/usr/bin/env python3
"""Gate: the Backups app in the owner's browser (system-apps/backups).

THE RULE: the platform holds a backup OR a key, never both. The recovery key
is made in the owner's browser on their own Host and only its public half is
sent; a backup kept as a file is restored by opening its key file in the
browser and sealing it to the Host, never by sending the recovery key. This
gate:

  * checks the vendored libraries are the pinned ones (the same files the
    rest of the tree carries) with their notices;
  * runs both modules under node with a recording fetch and a captured
    console (scripts/test_backups_browser.mjs), against a real backup file
    and key file made by the Host's own code (server/backup_stores.py,
    server/host_backup.py make_sidecar, real `age`);
  * proves a key made in the browser is a key the rest of the world agrees
    on: Python's age_keys (the Host's port of the encoding) reads the same
    public half from the private half and the same fingerprint as
    backup_key.fingerprint; `age-keygen -y` reads the saved key file to the
    same public half; and the `age` binary encrypts to the public half and
    decrypts with the saved file. The account page makes its keys with the
    same WebCrypto call and the same bech32 encoding, checked against the
    same binaries (its tests/export-key.test.mjs), so a key made either way
    opens backups either way.

    python3 scripts/test_backups_browser.py
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FAILED = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  [{str(detail)[:400]}]"))
    if not cond:
        FAILED.append(label)


if not Path(os.environ.get("AGE_BIN", "/usr/local/bin/age")).is_file():
    os.environ["AGE_BIN"] = shutil.which("age") or "/usr/local/bin/age"

from server import age_keys, backup_key, backup_stores  # noqa: E402
from server import host_backup as hb  # noqa: E402

app = ROOT / "system-apps/backups"
vendor = app / "vendor"
pinned = re.search(r"sha256 \(age-encryption.js\):\s*\n\s*([0-9a-f]{64})", (vendor / "VERSIONS").read_text())
got = hashlib.sha256((vendor / "age-encryption.js").read_bytes()).hexdigest()
check("the vendored age library is the pinned build, the same file Settings carries",
      pinned and pinned.group(1) == got
      == hashlib.sha256((ROOT / "system-apps/settings/vendor/age-encryption.js").read_bytes()).hexdigest(), got)
check("its licences ship with it", all((vendor / f).is_file() for f in (
    "LICENSE-age-encryption", "LICENSE-noble-hashes", "LICENSE-noble-curves", "LICENSE-noble-ciphers",
    "LICENSE-noble-post-quantum", "LICENSE-scure-base")))
qr = (vendor / "qrcode.js").read_bytes()
check("the QR library is the tree's own copy, with its notice",
      qr == (ROOT / "dashboard/base/js/qrcode.js").read_bytes() and b"MIT" in qr[:400]
      and hashlib.sha256(qr).hexdigest() in (vendor / "VERSIONS").read_text())
page = (app / "index.html").read_text()
check("the page loads both modules and the QR library from its own folder",
      'src="/system-apps/backups/recovery-key.js"' in page and 'src="/system-apps/backups/restore-file.js"' in page
      and 'src="/system-apps/backups/vendor/qrcode.js"' in page and page.count('type="module"') >= 2)

# ── fixtures: a real backup file and its key file, as the Host makes them ────
tmp = Path(tempfile.mkdtemp(prefix="bb-test-"))
rec_ident, rec_rec = age_keys.new_pair()
stamp = "20261019T020000Z"
store = backup_stores.LocalStore(tmp / "store")
up = store.uploader(stamp)
payload = os.urandom(10000)
up.part(payload)
obj = up.complete()
per_ident, _ = age_keys.new_pair()
sealed = hb.make_sidecar([rec_rec], eid=1, stamp=stamp, object_name=f"{stamp}.tar.zst.age",
                         identity=per_ident, data=obj)
store.put_sidecar(stamp, sealed)
fix = tmp / "fix"
fix.mkdir()
shutil.copy(tmp / "store" / f"{stamp}.tar.zst.age", fix)
shutil.copy(tmp / "store" / f"{stamp}.key.age", fix)
with open(fix / backup_stores.bundle_name(stamp), "wb") as f:
    for chunk in backup_stores.bundle_stream(store.bundle(stamp)):
        f.write(chunk)
with open(fix / "big-header.tar", "wb") as f:
    side_hdr = backup_stores.tar_header(f"{stamp}.key.age", len(sealed))
    f.write(side_hdr + sealed + b"\0" * ((-len(sealed)) % 512))
    f.write(backup_stores.tar_header(f"{stamp}.tar.zst.age", 9 << 30) + b"x" * 4096)

node = shutil.which("node")
check("node is available to run the modules", node is not None)
interop = None
if node:
    env = dict(os.environ, FIXTURES=str(fix), STAMP=stamp, REC_IDENTITY=rec_ident)
    proc = subprocess.run([node, str(ROOT / "scripts/test_backups_browser.mjs")], capture_output=True,
                          text=True, timeout=180, cwd=str(ROOT), env=env)
    for line in proc.stdout.splitlines():
        if line.startswith("INTEROP "):
            interop = json.loads(line[len("INTEROP "):])
        else:
            print(line)
    check("the browser modules' own checks pass", proc.returncode == 0, proc.stderr[-600:])

print("== a key made in the browser, as the rest of the world reads it")
if interop:
    ident, recip, fp = interop["identity"], interop["recipient"], interop["fingerprint"]
    check("the Host's Python port reads the same public half from the private half",
          age_keys.recipient_of_identity(ident) == recip)
    check("and names it with the same fingerprint the Host shows",
          backup_key.fingerprint(age_keys.recipient_bytes(recip)) == fp)
    keyfile = tmp / "saved-key.txt"
    keyfile.write_text(interop["keyfile"])
    keygen = shutil.which("age-keygen")
    if keygen:
        y = subprocess.run([keygen, "-y", str(keyfile)], capture_output=True, text=True)
        check("age-keygen reads the saved key file to the same public half",
              y.returncode == 0 and y.stdout.strip() == recip, y.stderr[-200:])
    else:
        check("age-keygen is available for the check", False)
    age = os.environ["AGE_BIN"]
    enc = subprocess.run([age, "-r", recip], input=b"a backup", capture_output=True)
    dec = subprocess.run([age, "-d", "-i", str(keyfile)], input=enc.stdout, capture_output=True)
    check("the age binary encrypts to the public half and the saved file opens it",
          enc.returncode == 0 and dec.returncode == 0 and dec.stdout == b"a backup", dec.stderr[-200:])

shutil.rmtree(tmp, ignore_errors=True)
print()
if FAILED:
    print(f"FAILED: {len(FAILED)}")
    sys.exit(1)
print("OK: the recovery key is made, used and restored with in the page, and never leaves it")
