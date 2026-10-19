#!/usr/bin/env python3
"""Gate: the Settings restore module in a real browser (headless Chromium).

scripts/test_settings_restore_browser.py runs the module under node. This runs
the same files, served over HTTP exactly as the Host serves them
(/system-apps/settings/restore.js importing ./vendor/age-encryption.js), in
Chromium, and watches the browser itself:

  * the owner types their recovery key into a field; the page fetches the
    backup's sealed key file from the Host, reseals it and POSTs;
  * every request the browser sends is recorded by Playwright, and no URL,
    header or body carries the recovery key or the backup's own key;
  * nothing the page logs carries either;
  * the Host's key (the age binary, as on the Host) opens what was sent, and
    it is the same key file;
  * where the browser has no BarcodeDetector (headless Chromium on Linux),
    the scan path says so and hands back nothing: the page falls back to paste.

Needs the playwright Python package and its Chromium. Where they are missing
it says so and exits 3, which pre_merge_gates reports as a failure: a gate
that cannot run is not a pass.

    python3 scripts/test_settings_restore_chromium.py
"""
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FAILED = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  [{str(detail)[:400]}]"))
    if not cond:
        FAILED.append(label)


try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("cannot run: the playwright Python package is not installed")
    sys.exit(3)

from server import age_keys  # noqa: E402
from gate_loopback import ClientClosesFirst  # noqa: E402

AGE = os.environ.get("AGE_BIN") if os.environ.get("AGE_BIN") and Path(os.environ["AGE_BIN"]).is_file() \
    else shutil.which("age")
if not AGE:
    print("cannot run: no age binary")
    sys.exit(3)


def age_open(identity, data):
    rfd, wfd = os.pipe()
    os.write(wfd, (identity + "\n").encode())
    os.close(wfd)
    try:
        return subprocess.run([AGE, "-d", "-i", f"/dev/fd/{rfd}"], input=data, capture_output=True,
                              pass_fds=(rfd,), timeout=60)
    finally:
        os.close(rfd)


rec_ident, rec_rec = age_keys.new_pair()        # the owner's Host recovery key
host_ident, host_rec = age_keys.new_pair()      # the restoring Host's vault key
per_backup, _ = age_keys.new_pair()
STAMP = "20260927T020700Z"
BODY = json.dumps({"v": 1, "kind": "privatae-backup-key", "identity": per_backup,
                   "object": f"b/{STAMP}.tar.zst.age", "entity_id": 42, "stamp": STAMP,
                   "made_at": "2026-09-27T02:07:00+00:00"})
SIDECAR = subprocess.run([AGE, "-a", "-r", rec_rec], input=BODY.encode(), capture_output=True).stdout.decode()

PAGE = """<!doctype html><meta charset="utf-8"><title>restore</title>
<input id="reckey" type="password"><button id="go">Get the key</button><div id="out"></div>
<script type="module">
import * as R from '/system-apps/settings/restore.js';
async function fetchJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}
window.run = async () => {
  const key = document.getElementById('reckey').value;
  document.getElementById('reckey').value = '';
  try {
    const r = await R.restoreKeyInBrowser({ fetchJSON, stamp: '%s', identity: key, stepupCode: '123456' });
    console.log('restore answered', JSON.stringify(r));
    return 'ok';
  } catch (e) {
    console.error('restore failed:', e && e.message);
    return 'error: ' + (e && e.message);
  }
};
window.scan = async () => {
  const can = window.SettingsRestore.canScan();
  const got = await window.SettingsRestore.scanQr(document.createElement('video'));
  return { can, got, detector: typeof BarcodeDetector };
};
window.ready = true;
</script>""" % STAMP

posted = []


class Host(ClientClosesFirst, http.server.BaseHTTPRequestHandler):
    # ClientClosesFirst: closing first left a TIME_WAIT on this port per
    # request, and CI's firewall refuses a later request that lands on one
    # (scripts/gate_loopback.py).
    """The Host, as far as the page can see it."""

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/page":
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        if path == "/api/backup/restore/sidecar":
            return self._send(200, json.dumps({"stamp": STAMP, "entity_id": 42, "host_recipient": host_rec,
                                               "sidecar": SIDECAR}).encode(), "application/json")
        if path.startswith("/system-apps/settings/"):
            f = (ROOT / path.lstrip("/")).resolve()
            if str(f).startswith(str(ROOT / "system-apps/settings")) and f.is_file():
                return self._send(200, f.read_bytes(), "text/javascript")
        self._send(404, b"", "text/plain")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        posted.append(body)
        self._send(200, json.dumps({"key_ready": True, "stamp": STAMP}).encode(), "application/json")


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Host)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"

requests, console = [], []


def install_browser() -> bool:
    """The browser this playwright version drives, installed by playwright's
    own installer into its usual cache (PLAYWRIGHT_BROWSERS_PATH when set).
    The version always matches: it is the installed package that installs it.

    Old versions do not pile up: the installer itself removes every browser in
    the cache that no installed playwright uses ("Removing unused browser at
    ..."; checked with a planted chromium_headless_shell-1100).
    `playwright uninstall` is NOT that: it removes the browsers THIS
    installation uses, i.e. the one just installed."""
    print("installing chromium-headless-shell with playwright's own installer")
    try:
        r = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium-headless-shell"],
                           capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        print("  the install did not finish in 10 minutes")
        return False
    if r.returncode != 0:
        print("  the install failed: " + (r.stderr or r.stdout).strip()[-400:])
    return r.returncode == 0


pw = sync_playwright().start()
try:
    browser = pw.chromium.launch()
except Exception as e:
    if "Executable doesn't exist" not in str(e):
        print(f"FAIL: Chromium did not start ({str(e)[:600]})")
        sys.exit(1)
    if not install_browser():
        print("cannot run: no browser here, and it could not be installed")
        sys.exit(3)
    try:
        browser = pw.chromium.launch()
    except Exception as e2:
        # Installed, and still does not start: most often system libraries
        # the browser needs are missing (playwright install-deps lists them).
        # A failure to report, never to hide.
        print(f"FAIL: Chromium was installed but did not start ({str(e2)[:600]})")
        sys.exit(1)
try:
    page = browser.new_page()
    page.on("request", lambda r: requests.append({"url": r.url, "method": r.method,
                                                   "headers": r.headers, "body": r.post_data or ""}))
    page.on("console", lambda m: console.append(m.text))
    page.on("pageerror", lambda e: console.append("pageerror: " + str(e)))
    page.goto(BASE + "/page")
    page.wait_for_function("window.ready === true && window.SettingsRestore !== undefined", timeout=15000)

    print("== in Chromium", browser.version)
    page.fill("#reckey", "# Host 42 recovery key\n" + rec_ident.lower())
    result = page.evaluate("window.run()")
    check("the page reseals the backup's key and the Host answers", result == "ok", (result, console[-3:]))
    check("the recovery key field is emptied once used", page.input_value("#reckey") == "")
    sent = json.loads(posted[-1]) if posted else {}
    check("one POST, carrying the backup id, the code and an age file",
          len(posted) == 1 and sent.get("via") == "browser" and sent.get("stamp") == STAMP
          and str(sent.get("sidecar", "")).startswith("-----BEGIN AGE ENCRYPTED FILE-----"), sent)
    opened = age_open(host_ident, str(sent.get("sidecar", "")).encode())
    check("the Host's key (the age binary) opens it, and it is the same key file",
          opened.returncode == 0 and opened.stdout.decode() == BODY, opened.stderr[-200:])
    check("the recovery key does not open what was sent (it is sealed to the Host only)",
          age_open(rec_ident, str(sent.get("sidecar", "")).encode()).returncode != 0)
    wire = json.dumps(requests).upper()
    check("THE RULE: no request the browser sent carries the recovery key",
          rec_ident.upper() not in wire and "AGE-SECRET-KEY" not in wire, wire[:300])
    check("THE RULE: nor the backup's own key", per_backup.upper() not in wire)
    check("the page made only the two requests it needs, besides loading itself",
          [r["method"] + " " + r["url"].split("?")[0].replace(BASE, "") for r in requests
           if "/api/" in r["url"]] == ["GET /api/backup/restore/sidecar", "POST /api/backup/restore/key"],
          [r["url"] for r in requests])

    print("== a wrong key, in the browser")
    other, _ = age_keys.new_pair()
    page.fill("#reckey", other)
    result = page.evaluate("window.run()")
    check("another key is refused and nothing is sent", result.startswith("error:") and len(posted) == 1, result)
    page.fill("#reckey", rec_ident[:-1] + ("Q" if rec_ident[-1] != "Q" else "P"))
    result = page.evaluate("window.run()")
    check("a mistyped key is refused without being repeated",
          "typing mistake" in result and "AGE-SECRET-KEY" not in result.upper() and len(posted) == 1, result)

    print("== scanning where the browser cannot")
    got = page.evaluate("window.scan()")
    check("no BarcodeDetector here: the page says it cannot scan and returns nothing, so the "
          "field stays for pasting", got == {"can": False, "got": None, "detector": "undefined"}, got)

    logs = "\n".join(console).upper()
    check("THE RULE: nothing the page logged carries a key",
          "AGE-SECRET-KEY" not in logs and rec_ident.upper() not in logs, console)
    check("no page errors", not any(c.startswith("pageerror") for c in console), console)
finally:
    browser.close()
    pw.stop()
    srv.shutdown()

print()
if FAILED:
    print(f"FAILED: {len(FAILED)}")
    sys.exit(1)
print("OK: in Chromium, the recovery key stays in the page")
