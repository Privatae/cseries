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
  5. The claim and the sign-in: no code in any address (no fragment, no
     /setup link, no /api/setup-code); every call carries the handoff's
     cookie (same-origin); the enroll view asks for no code; the recovery
     key is downloaded and confirmed with "I saved it", never retyped (the
     page sends back the key it showed; one another tab replaced is refused
     and taken off the page); a code
     that came with the machine is typed once, into a form posted to
     /api/setup; the page's policy lets forms go to its agent and the Host
     only, and sends its origin. Under node: the browser that set the keys
     up waits for the Host and posts the claim ticket to /api/owner/claim
     with nothing pressed; Sign in to your Host takes a key and posts a
     sign-in ticket to /api/owner/box-sign-in; a ticket for another address
     is not posted; a Host without an owner and no email shows the form.

Needs node for 1 and 5 (CSERIES_REQUIRE_NODE=1 makes its absence a failure).

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

# 5. The claim and the sign-in: nothing is ever typed but on the page's own
# form, the code is never in an address, and the ticket goes to the Host's
# two routes as a form.
check("#code=" not in js and "setupUrl" not in js and "'/setup-code'" not in js and "location.hash" not in js,
      "no code in any address: no fragment, no /setup link, no /api/setup-code")
check("credentials: 'same-origin'" in js and "credentials: 'omit'" not in js,
      "every call sends the handoff's cookie (same-origin credentials)")
enroll = html[html.index('<section id="enroll"'):]
enroll = enroll[:enroll.index("</section>")]
check("<input" not in enroll, "the enroll view asks for no code: the handoff's cookie stands for it")
rshow = html[html.index('<section id="recovery-show"'):]
rshow = rshow[:rshow.index("</section>")]
confirm = rshow[rshow.index('<form id="confirm-form"'):]
confirm = confirm[:confirm.index("</form>")]
check('id="download-recovery"' in rshow and "<input" not in confirm and "I saved it" in confirm,
      "the recovery key: Download, then I saved it, nothing retyped")
hform = html[html.index('<form id="handoff-form"'):]
hform = hform[:hform.index("</form>")]
check('method="post"' in hform and 'action="/api/setup"' in hform and 'name="setup_code"' in hform and
      'name="email"' in hform, "a code that came with the machine is typed once, into a form posted to /api/setup")
check('<meta name="referrer" content="strict-origin">' in html, "the page's referrer policy is strict-origin")
with open(os.path.join(ROOT, "vm/cloud/overlay/etc/caddy/Caddyfile.in"), encoding="utf-8") as f:
    caddy = f.read()
usite = caddy[caddy.index("https://@UNLOCK_HOST@ {"):caddy.index("https://*.@ZONE_HOST@ {")]
check("form-action 'self' https://@HOST_NAME@;" in usite and "Referrer-Policy strict-origin" in usite,
      "its policy lets forms go to its own agent and the Host only, and sends its origin (an Origin to check)",
      usite)
if not node:
    if os.environ.get("CSERIES_REQUIRE_NODE") == "1":
        check(False, "node is needed for the page's claim and sign-in")
    else:
        print("SKIP  the claim and sign-in under node: node is not installed")
else:
    harness = r"""
const fs = require('fs'), vm = require('vm');
const sc = JSON.parse(process.argv[2]);
const VIEWS = ['locked', 'handoff', 'enroll', 'recovery-show', 'claiming', 'signing-in', 'open', 'absent'];
const els = {};
const submitted = [];
const made = [];
function el(id) {
  if (!els[id]) {
    // The page's sections start hidden, as in index.html.
    const e = {id, hidden: VIEWS.includes(id), textContent: '', value: '', href: '', handlers: {}, children: [],
               focused: false, details: {open: false}, attrs: {}};
    e.addEventListener = (t, fn) => { e.handlers[t] = fn; };
    e.appendChild = (c) => { e.children.push(c); };
    e.focus = () => { e.focused = true; };
    e.closest = (sel) => sel === 'details' ? e.details : null;
    e.click = () => { e.clicked = true; };
    e.remove = () => { e.removed = true; };
    e.submit = () => { submitted.push({method: e.method, action: e.action,
                                       fields: e.children.map((c) => [c.type, c.name, c.value])}); };
    els[id] = e;
  }
  return els[id];
}
const calls = [];
const BASE = {state: 'open', name: 'orchard', machine_id: 'm', rp_id: 'unlock-orchard.k7x2m9.cloud.privatae.ai',
              host_url: 'https://orchard.k7x2m9.cloud.privatae.ai/', enrolled: true, claim: null,
              passkeys: [], salt: null, recovery: true, claiming: false, resume: false, dropped: [],
              handoff: false, handoff_claim: false, host: {up: true, owner: true}};
let stateN = 0;
async function fetch(url, opts) {
  calls.push({url, credentials: opts.credentials, body: opts && opts.body ? JSON.parse(opts.body) : null});
  if (url === '/api/state') {
    const s = sc.states[Math.min(stateN++, sc.states.length - 1)];
    return {ok: true, status: 200, json: async () => Object.assign({}, BASE, s)};
  }
  const a = (sc.answers || {})[url] || {status: 200, body: {ok: true}};
  return {ok: a.status < 300, status: a.status, json: async () => a.body};
}
const location = {href: 'https://unlock-orchard.k7x2m9.cloud.privatae.ai/'};
let n = 0;
const document = {getElementById: el, createElement: (t) => { const e = el('made' + (n++)); e.tag = t; made.push(e); return e; },
                  body: el('body')};
class Blob { constructor(parts, o) { this.parts = parts; this.type = o.type; } }
const URL = {createObjectURL: (b) => { URL.last = b; return 'blob:invented'; }, revokeObjectURL: () => {}};
const timers = (fn, ms) => setTimeout(fn, Math.min(ms, 5));
const ctx = {window: {cseriesDerive: {}}, document, fetch, location, crypto: globalThis.crypto, Blob, URL,
             setTimeout: timers, setInterval: (fn) => 0, clearInterval: () => {}, console};
const tick = () => new Promise((r) => setTimeout(r, 40));
(async () => {
  vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
  await tick();
  const out = {views: {}};
  for (const step of sc.steps || []) {
    if (step.click) el(step.click).handlers.click();
    if (step.submit) el(step.submit).handlers.submit ? el(step.submit).handlers.submit({preventDefault() {}})
                                                     : el(step.submit).onsubmit({preventDefault() {}});
    if (step.value) el(step.value[0]).value = step.value[1];
    await tick();
  }
  for (const v of VIEWS) out.views[v] = !el(v).hidden;
  out.calls = calls.filter((c) => c.url !== '/api/state').map((c) => ({url: c.url, body: c.body}));
  out.credentials = [...new Set(calls.map((c) => c.credentials))];
  out.submitted = submitted;
  out.note = el('sign-in-note').textContent;
  out.err = el('err').textContent;
  out.download = made.filter((e) => e.tag === 'a' && e.clicked).map((e) => [e.href, e.download]);
  out.blob = URL.last ? URL.last.parts.join('') : null;
  out.href = location.href;
  out.recoveryText = el('recovery-text').textContent;
  console.log(JSON.stringify(out));
})();
"""

    def page(scenario):
        r = subprocess.run([node, "-e", harness, os.path.join(PAGE, "unlock.js"), json.dumps(scenario)],
                           capture_output=True, text=True, timeout=60, check=False)
        try:
            return json.loads(r.stdout.strip().splitlines()[-1]), r.stderr[-600:]
        except (ValueError, IndexError):
            return {}, r.stderr[-600:]

    HOST = "https://orchard.k7x2m9.cloud.privatae.ai/"
    TICKET = "invented-ticket-0123456789abcdef"
    ticket = lambda kind, route: {"status": 200, "body": {"ok": True, "kind": kind, "ticket": TICKET,
                                                          "action": HOST + route}}

    # The browser that set up the keys: on into the Host as its owner, once
    # the Host answers, with nothing to press.
    # The first state (host not up) is the page's own; the auto sign-in starts once one says up with no owner.
    got, err = page({"states": [{"handoff": True, "handoff_claim": True, "host": {"up": True, "owner": False}},
                                {"handoff": True, "handoff_claim": True, "host": {"up": False, "owner": None}},
                                {"handoff": True, "handoff_claim": True, "host": {"up": True, "owner": False}}],
                     "answers": {"/api/owner-ticket": ticket("claim", "api/owner/claim")}})
    check(got.get("calls") == [{"url": "/api/owner-ticket", "body": {}}] and
          got.get("submitted") == [{"method": "post", "action": HOST + "api/owner/claim",
                                    "fields": [["hidden", "ticket", TICKET]]}] and got.get("views", {}).get("signing-in"),
          "set up by this browser: it waits for the Host, asks for the claim ticket with no key and posts it "
          "to the Host's /api/owner/claim as a form", (got, err))
    check(got.get("credentials") == ["same-origin"], "every call with same-origin credentials", got.get("credentials"))
    check(TICKET not in got.get("href", "") and got.get("href") == "https://unlock-orchard.k7x2m9.cloud.privatae.ai/",
          "the ticket is never put in an address", got.get("href"))

    # Signing in later: the button, a key, a sign-in ticket.
    got, err = page({"states": [{}], "answers": {"/api/owner-ticket": ticket("sign-in", "api/owner/box-sign-in")},
                     "steps": [{"click": "host-sign-in"}, {"value": ["auth-recovery", "AAAA-BBBB"]},
                               {"submit": "auth-recovery-form"}]})
    check(got.get("calls") == [{"url": "/api/owner-ticket", "body": {"auth": {"recovery_key": "AAAA-BBBB"}}}] and
          [s["action"] for s in got.get("submitted", [])] == [HOST + "api/owner/box-sign-in"],
          "Sign in to your Host: one of the disk's keys, then the sign-in ticket posted to /api/owner/box-sign-in",
          (got, err))

    # An answer naming anywhere but the Host's two routes sends nothing.
    got, err = page({"states": [{}], "answers": {"/api/owner-ticket": {"status": 200, "body": {
        "ok": True, "kind": "sign-in", "ticket": TICKET, "action": "https://elsewhere.example/api/owner/claim"}}},
                     "steps": [{"click": "host-sign-in"}, {"value": ["auth-recovery", "AAAA-BBBB"]},
                               {"submit": "auth-recovery-form"}]})
    check(got.get("submitted") == [] and "not your Host" in got.get("err", ""),
          "a ticket for an address that is not the Host is not posted", (got, err))

    # No owner and no handoff (a key, but no email): the page offers the setup form.
    got, err = page({"states": [{"host": {"up": True, "owner": False}}],
                     "answers": {"/api/owner-ticket": {"status": 409, "body": {
                         "error": "Your Host has no owner yet.", "handoff": False}}},
                     "steps": [{"click": "host-sign-in"}, {"value": ["auth-recovery", "AAAA-BBBB"]},
                               {"submit": "auth-recovery-form"}]})
    check(got.get("views", {}).get("handoff") and got.get("submitted") == [],
          "no email for the owner yet: the page shows the setup form", (got, err))

    # Not set up, no handoff in this browser: the setup form, nothing else.
    got, err = page({"states": [{"enrolled": False, "host": None, "recovery": False}]})
    check(got.get("views", {}).get("handoff") and not got.get("views", {}).get("enroll") and got.get("calls") == [],
          "a browser without a handoff is sent to the account site's button, or the code's form", (got, err))

    # With the handoff: one passkey (none here: the recovery key alone),
    # the key downloaded, I saved it, and the disk is set up.
    got, err = page({"states": [{"enrolled": False, "handoff": True, "host": None, "recovery": False}],
                     "answers": {"/api/enroll": {"status": 200, "body": {"ok": True,
                                                                         "recovery_key": "INVENTED-RECOVERY-KEY"}},
                                 "/api/enroll/confirm": {"status": 200, "body": {"ok": True, "started": True}}},
                     "steps": [{"submit": "enroll-form"}, {"click": "download-recovery"},
                               {"submit": "confirm-form"}]})
    check(got.get("calls") == [{"url": "/api/enroll", "body": {"passkey": None}},
                               {"url": "/api/enroll/confirm", "body": {"recovery_key": "INVENTED-RECOVERY-KEY"}}],
          "the handoff's enroll sends no code, and I saved it sends back the key the page showed, "
          "nothing typed", (got, err))
    check(got.get("download") == [["blob:invented", "c-series-recovery-key-orchard.txt"]] and
          "INVENTED-RECOVERY-KEY" in (got.get("blob") or "") and got.get("views", {}).get("claiming"),
          "Download saves the recovery key as a file made in the page; then the disk's progress", (got, err))


    # Another tab or browser enrolled since: the machine refuses this tab's
    # I saved it, and the key it showed, which unlocks nothing, leaves the page.
    got, err = page({"states": [{"enrolled": False, "handoff": True, "host": None, "recovery": False}],
                     "answers": {"/api/enroll": {"status": 200, "body": {"ok": True,
                                                                         "recovery_key": "INVENTED-RECOVERY-KEY"}},
                                 "/api/enroll/confirm": {"status": 403, "body": {
                                     "error": "That is not the recovery key this machine waits for.",
                                     "replaced": True}}},
                     "steps": [{"submit": "enroll-form"}, {"submit": "confirm-form"}]})
    v = got.get("views", {})
    check(v.get("enroll") and not v.get("recovery-show") and not v.get("claiming") and
          got.get("recoveryText") == "" and "not the recovery key" in got.get("err", ""),
          "a key another tab replaced: refused, taken off the page, and the setup can start again", (got, err))

sys.exit(failed)
