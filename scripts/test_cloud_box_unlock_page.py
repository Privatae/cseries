#!/usr/bin/env python3
"""The cloud machine's unlock page (vm/cloud/overlay/usr/share/cseries-cloud/
unlock/), the parts that run in a browser.

  1. derive.js, under node's WebCrypto, gives a passkey's passphrase exactly
     as the machine computes it (cseries_vault.passkey_passphrase): the same
     64 hex for the same PRF output, machine and credential.
  2. The page needs nothing its Content-Security-Policy refuses: no inline
     script, no inline style, no event-handler attribute, every script and
     stylesheet its own file (the policy is in the Caddyfile).
  3. Every element unlock.js looks up exists in index.html, and every view
     it switches between is a section there.
  4. The page calls only the agent's own routes (cseries-unlock-agent's
     ROUTES and /api/state), and what it sends for a passkey is the derived
     passphrase: the PRF output goes to derive.js and nowhere else.

Needs node for 1 (CSERIES_REQUIRE_NODE=1 makes its absence a failure).

Container-free: python3 scripts/test_cloud_box_unlock_page.py
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from importlib.machinery import SourceFileLoader

ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PAGE = os.path.join(ROOT, "vm/cloud/overlay/usr/share/cseries-cloud/unlock")
LIB = os.path.join(ROOT, "vm/cloud/overlay/usr/local/lib/cseries-cloud")
sys.path.insert(0, LIB)
import cseries_vault as cv  # noqa: E402

failed = 0


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


def read(name):
    with open(os.path.join(PAGE, name), encoding="utf-8") as f:
        return f.read()


html, js, derive = read("index.html"), read("unlock.js"), read("derive.js")
MID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"           # invented
CRED = "Q3JlZGVudGlhbC1pbnZlbnRlZC0wMQ"                 # invented
PRF = bytes(range(32))
want = cv.passkey_passphrase(PRF, MID, CRED)

# 1. derive.js against the machine
node = shutil.which("node")
if not node:
    if os.environ.get("CSERIES_REQUIRE_NODE") == "1":
        check(False, "node is needed for derive.js")
    else:
        print("SKIP  derive.js under node: node is not installed")
else:
    script = r"""
const fs = require('fs'), vm = require('vm');
const ctx = {crypto: globalThis.crypto, TextEncoder, atob, btoa, Uint8Array, Array, String};
ctx.globalThis = ctx;
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const [machine, cred] = [process.argv[2], process.argv[3]];
const prf = Uint8Array.from({length: 32}, (_, i) => i);
ctx.cseriesDerive.passphrase(prf, machine, cred).then((p) => {
  const b = ctx.cseriesDerive.b64u;
  const round = b.enc(b.dec(cred)) === cred;
  console.log(JSON.stringify({passphrase: p, round}));
});
"""
    r = subprocess.run([node, "-e", script, os.path.join(PAGE, "derive.js"), MID, CRED],
                       capture_output=True, text=True, timeout=60, check=False)
    try:
        got = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        got = {}
    check(got.get("passphrase") == want,
          "derive.js gives the passphrase the machine computes for the same PRF output", (got, want, r.stderr[-300:]))
    check(got.get("round") is True, "its base64url reads back what it writes")

# 2. the page and its policy
check(not re.search(r"<script(?![^>]*\ssrc=)[^>]*>", html), "no inline <script> in the page")
check("<style" not in html and not re.search(r"\sstyle=", html), "no inline style in the page")
check(not re.search(r"\son[a-z]+=", html, re.I), "no event-handler attribute in the page")
srcs = re.findall(r'<script src="([^"]+)"', html) + re.findall(r'<link rel="stylesheet" href="([^"]+)"', html)
check(sorted(srcs) == sorted(["/qrcode.js", "/derive.js", "/unlock.js", "/unlock.css"]),
      "its scripts and stylesheet are its own files", srcs)
check(all(os.path.exists(os.path.join(PAGE, s.lstrip("/"))) for s in srcs if s != "/qrcode.js"),
      "each one ships with the page (qrcode.js comes from the dashboard's copy, build-overlay.sh)")
with open(os.path.join(ROOT, "vm/cloud/build-overlay.sh"), encoding="utf-8") as f:
    check('root/usr/share/cseries-cloud/unlock/qrcode.js", os.path.join(repo, "dashboard/base/js/qrcode.js")'
          in f.read(), "build-overlay.sh puts the vendored QR library beside the page")

# 3. the elements
ids = set(re.findall(r'\sid="([^"]+)"', html))
used = set(re.findall(r"\$\('([^']+)'\)", js)) | set(re.findall(r"say\('([^']+)'", js))
check(used <= ids, "every element unlock.js looks up is in index.html", sorted(used - ids))
views = re.search(r"const VIEWS = \[([^\]]+)\]", js).group(1)
views = re.findall(r"'([^']+)'", views)
check(all(re.search(rf'<section id="{v}"', html) for v in views), "every view is a section", views)

# 4. what the page calls and sends
loader = SourceFileLoader("unlock_agent", os.path.join(ROOT, "vm/cloud/overlay/usr/local/sbin/cseries-unlock-agent"))
spec = importlib.util.spec_from_loader("unlock_agent", loader)
agent = importlib.util.module_from_spec(spec)
loader.exec_module(agent)
routes = {p for (_m, p) in agent.Agent.ROUTES} | {"/api/state"}
called = {"/api" + p for p in re.findall(r"api\('([^']+)'", js)}
check(called and called <= routes, "the page calls only the agent's routes", sorted(called - routes))
uses = [ln.strip() for ln in js.splitlines() if "results.first" in ln]
reads = [ln for ln in uses if not ln.startswith("if (")]
check(len(uses) == 4 and reads and all("D.passphrase(new Uint8Array(" in ln for ln in reads),
      "the PRF output is tested for, and otherwise goes only to derive.js's passphrase", uses)
check("rpId: S.rp_id" in js and "id: S.rp_id" in js and "userVerification: 'required'" in js,
      "every passkey ceremony names the unlock page's own relying party and asks for the person")

sys.exit(failed)
