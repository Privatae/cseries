#!/usr/bin/env python3
"""Gate: the Network app's Ingress lane shows messaging connectors, run on the
REAL system-apps/network/app.js under node.

  - a pairing shows its one-time code, fetched from the person-only code route,
    with a countdown; a code the route will not give shows the reason instead;
  - a matched pairing asks the person to let the account in, in the kernel's
    words, with the account as the connector reported it (escaped), and that
    button asks for the authenticator code (data-widen=1); "Don't pair" is free;
  - a live grant shows the account and a free Revoke; a revoked one is not drawn;
  - waiting connectors open the Ingress lane and count on the NAC badge;
  - the buttons post to the connector routes: approve with the code, decline
    and revoke without one, revoke only after the person confirms.

Container-free: node and the app's own source. Run: python3 scripts/test_network_connectors.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
failures: list[str] = []


def check(label: str, ok: bool, detail=None) -> None:
    print(("  OK   " if ok else "  FAIL ") + label + (f"\n         {detail!r}"[:600] if detail is not None and not ok else ""))
    if not ok:
        failures.append(label)


_JS = open(os.path.join(ROOT, "system-apps/network/app.js"), encoding="utf-8").read()


def _slice(start, end):
    i = _JS.find(start)
    j = _JS.find(end, i + 1)
    if i < 0 or j < 0:
        raise SystemExit(f"FAIL — app.js marker missing: {start!r} .. {end!r}")
    return _JS[i:j]


_nd = _JS.find("function nacDispatch(")
_APP = "\n".join([
    _slice("const $ = (id)", "// ── lens switching"),
    _slice("async function loadNAC()", "// Second-factor chip"),
    _slice("// ── INGRESS —", "// ── v125: ADD RULE"),
    _slice("const LANES = [", "function toggleLane("),
    _JS[_JS.find("function declineGrant(id)"):_JS.find("\n    }\n", _nd) + 7],
])

_HARNESS = r"""
const els = {};
function mkEl(id) {
  if (!els[id]) {
    const s = new Set();
    els[id] = { id, innerHTML: '', textContent: '', hidden: false,
      classList: { toggle(c, on) { if (on === undefined) on = !s.has(c); on ? s.add(c) : s.delete(c); },
                   contains(c) { return s.has(c); } },
      setAttribute() {}, removeAttribute() {}, querySelector() { return null; }, style: {} };
  }
  return els[id];
}
const document = { getElementById: mkEl };
const state = { nac: { laneOpen: {} }, loaded: {} };
const confirms = [], posts = [];
let confirmAnswer = true;
function confirm(m) { confirms.push(m); return confirmAnswer; }
function alert(m) { confirms.push('ALERT ' + m); }
function withStepup(fn, widening) { posts.push({ widening: !!widening }); fn(widening ? '123456' : null); }
function nacReload() {}
function setInterval() { return 1; } function clearInterval() {}
function renderNacStepupChip() {} function renderNacPending() {} function renderNacPosture() {}
function renderNacEgress() {} function renderNacGrants() {} function renderNacAudit() {}
function updateNacBadge() {}
"""


def _node(inputs, body):
    js = (_HARNESS + _APP + "\nfetchJSON = async (url, opts) => {"
          " if (opts && opts.method === 'POST') { posts.push({ url, body: opts.body }); return {}; }"
          " const r = inputs.reads[url.replace('/api/network', '')];"
          " if (r && r._throw) { const e = new Error(r._throw); e.status = r.status; throw e; }"
          " return r === undefined ? {} : r; };\n"
          + "const inputs = " + json.dumps(inputs) + ";\n"
          + "(async () => {\n" + body + "\n})().catch(e => { console.error(e && e.stack || e); process.exit(3); });\n")
    p = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        return None, p.stderr.strip()[-800:]
    try:
        return json.loads(p.stdout.strip().splitlines()[-1]), ""
    except Exception as e:
        return None, f"{e}: {p.stdout[-400:]}"


if not shutil.which("node"):
    print("FAIL — node is not installed; the Network app checks cannot run")
    sys.exit(1)

CONNECTORS = {"connectors": [
    {"id": 7, "app_id": "tg-conn", "platform": "telegram", "platform_label": "Telegram",
     "status": "pairing", "capability": "live"},
    {"id": 6, "app_id": "tg-two", "platform": "telegram", "platform_label": "Telegram",
     "status": "awaiting_approval", "account_id": "123456789",
     "display_name": "Alex <img src=x onerror=alert(1)>", "capability": "live",
     "token_name": "telegram-bot <b>x</b>"},
    {"id": 5, "app_id": "slack-conn", "platform": "slack", "platform_label": "Slack",
     "status": "active", "account_id": "T1:U2", "display_name": "Alex",
     "approved_at": "2026-10-04T08:00:00+00:00", "stepup_at": "2026-10-04T08:00:00+00:00"},
    {"id": 4, "app_id": "old", "platform": "discord", "platform_label": "Discord",
     "status": "revoked", "account_id": "999999", "display_name": "Old"},
    {"id": 8, "app_id": "dc-conn", "platform": "discord", "platform_label": "Discord",
     "status": "pairing", "capability": "pending"},
]}
READS = {"/nac/connector": CONNECTORS,
         "/nac/connector/7/code": {"code": "BCDF-GHJK", "seconds_left": 581},
         "/nac/connector/8/code": {"_throw": "this pairing's code was lost when your Host restarted", "status": 410}}

print("1. the Ingress lane draws connectors from /nac/connector")
view, err = _node({"reads": READS}, """
    await loadNAC();
    // One clock reading for both sides: connectorLeft floors to the second,
    // so a millisecond between two Date.now() calls read 61 s as "1:00 left".
    const T0 = Date.now();
    console.log(JSON.stringify({ html: els['nac-connector-list'].innerHTML,
        open: els['nac-lane-ingress'].classList.contains('open'), pending: state.nac.pending,
        left: connectorLeft(T0 + 61000, T0), gone: connectorLeft(T0, T0) }));""")
check("the lane renders under node", view is not None, err)
if view:
    h = view["html"]
    check("a pairing shows its code, fetched from the code route", "BCDF-GHJK" in h, h[:400])
    check("…with a countdown", "9:41 left" in h or "9:40 left" in h, h[:600])
    check("…and a free Decline", 'data-nac-decline="connector:7"' in h)
    check("a code the route will not give shows why, not a code",
          "lost when your Host restarted" in h and h.count("nac-pair-code") == 1, h)
    check("a pairing whose connector has no live key yet says the connector is restarting",
          "restarting to receive its key" in h)
    check("a matched pairing asks the person to let the account in, in the kernel's words",
          "Let a Telegram account in" in h and "cannot approve anything" in h
          and "full chat permissions" in h)
    check("…naming the Keys entry the connector sends to Telegram, escaped",
          "Key it sends to Telegram, from your Keys: “telegram-bot &lt;b&gt;x&lt;/b&gt;”" in h
          and "<b>x</b>" not in h, h)
    check("…showing the account as reported, escaped",
          "id 123456789" in h and "<img" not in h and "&lt;img" in h, h)
    check("…'Let this account in' asks for the authenticator code, 'Don't pair' is free",
          'data-nac-approve="connector:6" data-widen="1"' in h and 'data-nac-decline="connector:6"' in h)
    check("a live grant shows the account and a free Revoke",
          "Slack account “Alex” (id T1:U2)" in h and 'data-nac-revoke="connector:5" data-widen="0"' in h, h)
    check("a revoked connector is not drawn", 'connector:4"' not in h and "Old" not in h)
    check("waiting connectors open the Ingress lane and count as pending",
          view["open"] is True and view["pending"] == 3, view)
    check("the countdown reads m:ss and stops at expired", view["left"] == "1:01 left" and view["gone"] == "expired",
          view)

print("2. the buttons post to the connector routes")
acts, err = _node({"reads": READS}, """
    await loadNAC();
    nacDispatch('connector', 'approve', '6', true);
    nacDispatch('connector', 'decline', '7', false);
    confirmAnswer = false; nacDispatch('connector', 'revoke', '5', false);
    confirmAnswer = true; nacDispatch('connector', 'revoke', '5', false);
    await new Promise(r => setImmediate(r));
    console.log(JSON.stringify({ posts, confirms }));""")
check("the actions run under node", acts is not None, err)
if acts:
    urls = [p.get("url") for p in acts["posts"] if p.get("url")]
    check("'Let this account in' goes through the code prompt and posts the code",
          {"widening": True} in acts["posts"]
          and any(p.get("url") == "/api/network/nac/connector/6/approve"
                  and json.loads(p["body"]).get("stepup_code") == "123456" for p in acts["posts"]), acts)
    check("'Don't pair' / Decline posts without a code", "/api/network/nac/connector/7/decline" in urls, urls)
    check("Revoke asks first, and a 'no' posts nothing",
          urls.count("/api/network/nac/connector/5/revoke") == 1 and len(acts["confirms"]) == 2, acts)
    check("the confirm says the app keeps running",
          any("keeps running" in c for c in acts["confirms"]), acts["confirms"])

print(f"\n{'FAILED' if failures else 'PASSED'}: Network connectors ({len(failures)} failure(s))")
sys.exit(1 if failures else 0)
