#!/usr/bin/env python3
"""What the host-authored map frame can and cannot send, measured in Chromium.

WHY THIS EXISTS. scripts/test_network_map_routes.py asserts the CSP by grepping
app.js for its directives. Every one of those greps was green while the frame
could still exfiltrate the whole graph by navigating itself — a string being
present is not a browser enforcing it, and that gap is exactly where the hole
sat. This runs the lanes.

It also pins the honest boundary rather than a comfortable one:

  BLOCKED (the CSP does this)      fetch, XHR, Image.src, sendBeacon, WebSocket,
                                   CSS url(), <iframe src>, <link> prefetch,
                                   window.open, form POST, nested srcdoc
  NOT BLOCKED (nothing can)        the document navigating ITSELF —
                                   location.href, <meta http-equiv=refresh>,
                                   a clicked <a href> with no target

The sandbox flag only forbids navigating OTHER browsing contexts; a frame may
always navigate itself, and CSP has no shipped directive for navigations
(navigate-to was dropped). So the design's answer is to narrow what the frame is
given and to make a departure visible — asserted here too.

Run:  python3 scripts/e2e_hosted_map_egress.py
"""
from __future__ import annotations

import http.server
import os
import re
import socketserver
import sys
import threading

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(__file__), "..")

PORT = int(os.environ.get("EGRESS_PROBE_PORT", "8798"))
hits: list[str] = []
failures: list[str] = []
notes: list[str] = []


def check(cond, msg):
    if cond:
        notes.append("  PASS  " + msg)
    else:
        failures.append(msg)


class _H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        # The PARENT page must be real HTML — a 204 aborts the navigation and
        # the whole probe runs from about:blank, which silently changes the
        # answer. Everything else answers 204 so a leak is recorded without
        # rendering anything.
        if self.path.startswith("/parent-origin"):
            body = b"<!DOCTYPE html><html><body>parent</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        hits.append(self.path)
        self.send_response(204)
        self.end_headers()

    do_POST = do_GET

    def log_message(self, *a):
        pass


def hosted_csp() -> str:
    """The CSP exactly as it ships — read from the ONE definition
    (dashboard/base/js/sandbox-csp.js), which is where the map now gets it too.

    Reading it out of app.js stopped working the moment the four host-authored
    surfaces were consolidated onto the shared module, and a test that silently
    probed an EMPTY policy would have reported every lane wide open. Hence the
    plausibility check below: refuse to "measure" nothing.
    """
    src = open(os.path.join(_ROOT, "dashboard/base/js/sandbox-csp.js"), encoding="utf-8").read()
    m = re.search(r"var CSP = (.*?);\n", src, re.S)
    if not m:
        raise SystemExit("could not find CSP in dashboard/base/js/sandbox-csp.js")
    parts = re.findall(r"'([^']*)'|\"([^\"]*)\"", m.group(1))
    csp = "".join(a or b for a, b in parts)
    if "default-src" not in csp:
        raise SystemExit("parsed an implausible CSP, refusing to test nothing: " + repr(csp))
    return '<meta http-equiv="Content-Security-Policy" content="' + csp + '">'


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"SKIP — playwright unavailable: {e}")
        return 2

    csp = hosted_csp()
    check("default-src 'none'" in csp and "connect-src 'none'" in csp,
          f"the shared CSP denies by default: {csp[:90]}…")
    app_src = open(os.path.join(_ROOT, "system-apps/network/app.js"), encoding="utf-8").read()
    check("CSeriesSandbox" in app_src,
          "the map mounts through the shared definition rather than its own copy")

    base = f"http://127.0.0.1:{PORT}"
    srv = socketserver.TCPServer(("127.0.0.1", PORT), _H)
    srv.allow_reuse_address = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # Every lane a document might try, each tagged so the server says which got out.
    doc = (
        "<!DOCTYPE html><html><head>" + csp + "</head><body>"
        "<div id='ctl'>script-did-not-run</div>"
        "<style>#x{background:url('" + base + "/css')}</style>"
        "<link rel='prefetch' href='" + base + "/prefetch'>"
        "<img src='" + base + "/imgtag'>"
        "<iframe src='" + base + "/iframesrc'></iframe>"
        "<form id='f' method='POST' action='" + base + "/form'></form>"
        "<script>"
        "document.getElementById('ctl').textContent='SCRIPT RAN';"
        "try{fetch('" + base + "/fetch')}catch(e){}"
        "try{var x=new XMLHttpRequest();x.open('GET','" + base + "/xhr');x.send()}catch(e){}"
        "try{new Image().src='" + base + "/img'}catch(e){}"
        "try{navigator.sendBeacon('" + base + "/beacon','x')}catch(e){}"
        "try{new WebSocket('ws://127.0.0.1:" + str(PORT) + "/ws')}catch(e){}"
        "try{window.open('" + base + "/open')}catch(e){}"
        "try{document.getElementById('f').submit()}catch(e){}"
        "</script></body></html>"
    )

    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        # A REAL http parent origin. An about:blank parent silently changes the
        # result — measured: the same navigation that leaks from an http page
        # sends nothing from about:blank, which is how a first probe of this can
        # report "no leak" and be wrong.
        pg.goto(f"{base}/parent-origin", wait_until="domcontentloaded")
        pg.evaluate(
            """(d) => { const f = document.createElement('iframe');
                 f.id='probe'; f.setAttribute('sandbox','allow-scripts'); f.srcdoc=d;
                 document.body.appendChild(f); }""",
            doc,
        )
        pg.wait_for_timeout(1800)

        ran = pg.frame_locator("#probe").locator("#ctl").inner_text()
        check(ran == "SCRIPT RAN",
              f"POSITIVE CONTROL — the document's script actually executed (got {ran!r}); "
              "without this, 'nothing leaked' cannot be told apart from 'nothing ran'")

        leaked = {h.split("?")[0].lstrip("/") for h in hits if not h.startswith("/parent-origin")}
        for lane in ("fetch", "xhr", "img", "imgtag", "beacon", "css",
                     "prefetch", "iframesrc", "open", "form"):
            check(lane not in leaked, f"the CSP blocks {lane}")

        # The honest part. This lane is NOT closed, and the test says so —
        # asserting it blocked would make this gate a comfortable lie.
        hits.clear()
        pg.evaluate(
            """(d) => { const f = document.createElement('iframe');
                 f.id='nav'; f.setAttribute('sandbox','allow-scripts'); f.srcdoc=d;
                 document.body.appendChild(f); }""",
            "<!DOCTYPE html><html><head>" + csp + "</head><body><script>"
            "location.href='" + base + "/selfnav?d=' + encodeURIComponent('desktop-01');"
            "</script></body></html>",
        )
        pg.wait_for_timeout(1500)
        navved = any("selfnav" in h for h in hits)
        check(navved,
              "self-navigation IS still a lane — pinned as a KNOWN residual, so that "
              "if a future browser or directive closes it this test fails and someone "
              "re-reads the design instead of trusting a stale comment")
        b.close()

    srv.shutdown()

    # The two mitigations that follow from that residual.
    app_js = open(os.path.join(_ROOT, "system-apps/network/app.js"), encoding="utf-8").read()
    payload = app_js[app_js.find("function hostedPush"):app_js.find("window.addEventListener('message'")]
    # Strip // comments first. Without this the check matches the PROSE that
    # explains why those fields were removed, and passes or fails on the wrong
    # text entirely — the same mistake as grepping a CSP string and calling it
    # enforcement.
    payload = "\n".join(re.sub(r"//.*$", "", ln) for ln in payload.splitlines())
    for field in ("endpoint", "tunnel_ip", "entity_id"):
        check(field not in payload,
              f"the outbound payload does not carry {field} — a lane that cannot be "
              f"closed is answered by not putting the reach details through it")
    check("cseries:network:leaving" in app_js and "hostedLeft" in app_js,
          "a departure is reported and the map is not re-mounted, so the leak is "
          "visible rather than silent")

    for line in notes:
        print(line)
    if failures:
        print(f"\nFAIL — {len(failures)} problem(s):")
        for f in failures:
            print("  · " + f)
        return 1
    print(f"\nok — {len(notes)} egress checks measured in a real browser")
    return 0


if __name__ == "__main__":
    sys.exit(main())
