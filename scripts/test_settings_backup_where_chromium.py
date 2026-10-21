#!/usr/bin/env python3
"""Gate: Settings, Backup, "Where backups go", in a real browser (headless Chromium).

The page is the Settings app as the Host serves it (system-apps/settings
index.html and app.js), in front of a stand-in for the Host's backup routes
that records every request. It holds:

  * what the owner is offered follows what the Host is: a standalone Host
    cannot choose the network's storage (there is none), and starts on a
    folder on this machine; a network member can;
  * choosing their own bucket: the access key and secret go to the vault
    route alone (POST /api/backup/storage-key), and the setting saved after it
    names the key and carries neither; moving the backups asks for the owner's
    code and sends it only on the retry the Host asked for; the key fields are
    emptied once saved;
  * choosing the folder on this machine: how many to keep is saved, and no
    second copy is;
  * "Download the latest backup" asks the Host for a one-time link to the
    newest backup in the folder and the browser downloads that link itself
    (a navigation, never a fetch whose whole body the page would hold):
    exactly those bytes, under the file's own name;
  * the tab opens with one verdict line: whether backups are working, and
    when the last one finished; a Host with no recovery key (restored onto a
    new machine) reads "Backups are paused: add your recovery key".

Needs the playwright Python package and its Chromium. Where they are missing
it says so and exits 3, which pre_merge_gates reports as a failure: a gate
that cannot run is not a pass.

    python3 scripts/test_settings_backup_where_chromium.py
"""
import http.server
import json
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
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

from gate_loopback import ClientClosesFirst  # noqa: E402

# Invented values only.
ACCESS_KEY = "AKEXAMPLEEXAMPLE7Q2"
SECRET_KEY = "example-secret-3f9c1e7b2d"
STAMP_NEW, STAMP_OLD = "20261018T021500Z", "20261017T021500Z"
DOWNLOAD = b"README.txt and two encrypted files, as a tar would hold them " * 64

state = {"mode": "standalone", "status": {
    "state": "working", "headline": "Backups are working", "detail": "",
    "last_finished_at": "2026-10-18T02:16:40+00:00", "running": False}}
requests = []        # (method, path, query, body)
fetch_modes = {}     # path -> the Sec-Fetch-Mode the browser sent it with
TICKET = "dl_" + "Q7x" * 10 + "ab"


def settings_json() -> dict:
    s = state.get("saved") or {}
    where = s.get("where") or ("local" if state["mode"] == "standalone" else "network")
    return {"frequency": "daily", "hour": 2, "minute": 30, "weekday": 0, "timezone": "UTC",
            "is_default": False, "platform": True, "target": s.get("target"), "target_problem": None,
            "mode": state["mode"], "where": where, "where_chosen": bool(s.get("where")),
            "local_keep": s.get("local_keep", 7),
            "key": {"registered": True, "recovery": {"fingerprint": "0a1b2c3d4e5f6a7b", "label": "x"}}}


class Host(ClientClosesFirst, http.server.BaseHTTPRequestHandler):
    """The Host's routes, as far as this tab uses them."""

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path.startswith("/api/"):
            requests.append(("GET", path, query, ""))
            fetch_modes[path] = self.headers.get("Sec-Fetch-Mode")
        if path.startswith("/css/"):
            # The shell's shared styles, as the Host serves them (their
            # [hidden] rules decide what the owner can click).
            f = (ROOT / "dashboard/base" / path.lstrip("/")).resolve()
            if str(f).startswith(str(ROOT / "dashboard/base/css")) and f.is_file():
                return self._send(200, f.read_bytes(), "text/css")
        if path.startswith("/system-apps/settings/"):
            f = (ROOT / path.lstrip("/")).resolve()
            if str(f).startswith(str(ROOT / "system-apps/settings")) and f.is_file():
                ctype = "text/html; charset=utf-8" if f.suffix == ".html" else (
                    "text/css" if f.suffix == ".css" else "text/javascript")
                return self._send(200, f.read_bytes(), ctype)
        if path == "/api/backup/settings":
            return self._send(200, settings_json())
        if path == "/api/backup/files":
            return self._send(200, {"backups": [
                {"stamp": STAMP_NEW, "bytes": len(DOWNLOAD), "taken_at": "2026-10-18T02:15:00+00:00",
                 "folder": ""},
                {"stamp": STAMP_OLD, "bytes": 9, "taken_at": "2026-10-17T02:15:00+00:00", "folder": ""}],
                "exports": [], "uploads": [], "where": settings_json()["where"], "mode": state["mode"]})
        if path == f"/api/backup/files/{STAMP_NEW}/download":
            return self._send(200, DOWNLOAD, "application/x-tar",
                              {"Content-Disposition": f'attachment; filename="cseries-backup-{STAMP_NEW}.tar"'})
        if path == f"/api/backup/files/download/{TICKET}" and not state.get("link_used"):
            state["link_used"] = True        # one download per link, as the Host's are
            return self._send(200, DOWNLOAD, "application/x-tar",
                              {"Content-Disposition": f'attachment; filename="cseries-backup-{STAMP_NEW}.tar"'})
        if path == "/api/backup/runs":
            return self._send(200, {"runs": state.get("runs", []), "problem": None, "running": False,
                                    "status": state["status"]})
        if path == "/api/backup/restore":
            return self._send(200, {"backups": [], "problems": [], "progress": state.get("progress") or {}})
        if path.startswith("/api/backup/"):
            return self._send(200, {})
        self._send(404, {"detail": "not here"})

    def do_POST(self):
        path, _, query = self.path.partition("?")
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        requests.append(("POST", path, query, body))
        if path == "/api/backup/storage-key":
            got = json.loads(body)
            return self._send(200, {"ok": True, "cred_name": got.get("name") or "backup-storage"})
        if path == f"/api/backup/files/{STAMP_NEW}/link":
            return self._send(200, {"url": f"/api/backup/files/download/{TICKET}",
                                    "name": f"cseries-backup-{STAMP_NEW}.tar", "expires_in": 60})
        self._send(404, {"detail": "not here"})

    def do_PUT(self):
        path, _, query = self.path.partition("?")
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        requests.append(("PUT", path, query, body))
        if path == "/api/backup/settings":
            got = json.loads(body)
            if got.get("where") != (state.get("saved") or {}).get("where") and not got.get("stepup_code"):
                return self._send(428, {"detail": "enter your six-digit code to change where your backups go"})
            state["saved"] = got
            return self._send(200, settings_json())
        self._send(404, {"detail": "not here"})


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Host)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"


def install_browser() -> bool:
    """As scripts/test_settings_restore_chromium.py: playwright's own installer."""
    print("installing chromium-headless-shell with playwright's own installer")
    try:
        r = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium-headless-shell"],
                           capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0


def calls(method, path):
    return [r for r in requests if r[0] == method and r[1] == path]


pw = sync_playwright().start()
try:
    browser = pw.chromium.launch()
except Exception as e:
    if "Executable doesn't exist" not in str(e) or not install_browser():
        print(f"cannot run: no browser here ({str(e)[:300]})")
        sys.exit(3)
    browser = pw.chromium.launch()
console = []
try:
    page = browser.new_page(accept_downloads=True)
    page.on("console", lambda m: console.append(m.text))
    page.on("pageerror", lambda e: console.append("pageerror: " + str(e)))
    prompts = []

    def on_dialog(d):
        prompts.append(d.message)
        d.accept("123456")
    page.on("dialog", on_dialog)

    print("== a standalone Host")
    page.goto(BASE + "/system-apps/settings/index.html#view=backup")
    page.wait_for_function("document.getElementById('bk-local-keep').value === '7'", timeout=15000)
    print("   in Chromium", browser.version)
    page.wait_for_function("document.getElementById('bk-verdict').textContent.indexOf('Backups are') === 0",
                           timeout=15000)
    verdict = page.text_content("#bk-verdict")
    last_line = page.text_content("#bk-verdict-when")
    check("the tab opens with its verdict: backups are working", verdict == "Backups are working.", verdict)
    check("  and when the last one finished, in this browser's time",
          last_line.startswith("The last backup finished ") and "2026" in last_line, last_line)
    tops = page.evaluate("""() => [...document.querySelectorAll('#view-backup > .cseries-card')]
                                     .map(c => c.id)""")
    check("  it is the first thing on the tab", tops[:1] == ["bk-verdict-card"], tops)
    net_disabled = page.eval_on_selector('#bk-where option[value="network"]', "o => o.disabled")
    check("the network's storage cannot be chosen by a Host that is not on the network", net_disabled)
    check("it starts on a folder on this machine, keeping a week of backups",
          page.input_value("#bk-where") == "local" and page.input_value("#bk-local-keep") == "7")
    check("the folder's controls show; the bucket's and the second copy's do not",
          page.is_visible("#bk-local-keep") and page.is_visible("#bk-local-download")
          and not page.is_visible("#bk-target-address") and not page.is_visible("#bk-target-kind"))

    print("== the owner chooses their own bucket")
    page.select_option("#bk-where", "s3")
    check("the bucket's fields show", page.is_visible("#bk-target-address") and page.is_visible("#bk-target-secret"))
    page.fill("#bk-target-address", "my-host-backups")
    page.fill("#bk-target-endpoint", "https://s3.example.test")
    page.fill("#bk-target-prefix", "host-7")
    page.fill("#bk-target-keyid", ACCESS_KEY)
    page.fill("#bk-target-secret", SECRET_KEY)
    page.click("#bk-save")
    page.wait_for_function("document.getElementById('bk-status').textContent.indexOf('bucket') >= 0",
                           timeout=10000)
    keys = calls("POST", "/api/backup/storage-key")
    check("the key goes to the vault route, once, with both halves",
          len(keys) == 1 and json.loads(keys[0][3]) == {"access_key_id": ACCESS_KEY,
                                                       "secret_access_key": SECRET_KEY,
                                                       "name": "backup-storage"}, keys)
    puts = calls("PUT", "/api/backup/settings")
    check("moving the backups asked for the owner's code, and only the retry carries it",
          len(puts) == 2 and "stepup_code" not in json.loads(puts[0][3])
          and json.loads(puts[1][3]).get("stepup_code") == "123456" and len(prompts) == 1
          and "where your backups go" in prompts[0], (puts, prompts))
    saved = json.loads(puts[-1][3]) if puts else {}
    check("the setting saved names the bucket and the key, and holds no secret",
          saved.get("where") == "s3" and saved.get("target") == {
              "kind": "s3", "address": "my-host-backups", "endpoint": "https://s3.example.test",
              "region": None, "prefix": "host-7", "cred_name": "backup-storage"}, saved)
    wire = json.dumps([r for r in requests if r[1] != "/api/backup/storage-key"])
    check("THE RULE: no other request carries the bucket's secret or access key",
          SECRET_KEY not in wire and ACCESS_KEY not in wire)
    page.wait_for_function("document.getElementById('bk-target-secret').value === ''", timeout=5000)
    check("the key fields are emptied once saved",
          page.input_value("#bk-target-secret") == "" and page.input_value("#bk-target-keyid") == "")

    print("== the owner moves them to a folder on this machine")
    page.wait_for_function("document.getElementById('bk-target-cred-state').textContent.indexOf('vault') >= 0",
                           timeout=5000)
    before = len(calls("PUT", "/api/backup/settings"))
    page.select_option("#bk-where", "local")
    page.fill("#bk-local-keep", "14")
    page.click("#bk-save")
    page.wait_for_function("document.getElementById('bk-status').textContent.indexOf('folder') >= 0",
                           timeout=10000)
    saved = json.loads(calls("PUT", "/api/backup/settings")[-1][3])
    check("saved: the folder, keeping 14, with no second copy",
          saved.get("where") == "local" and saved.get("local_keep") == 14 and saved.get("target") is None
          and len(calls("PUT", "/api/backup/settings")) == before + 2, saved)
    check("and the bucket's key was not sent again", len(calls("POST", "/api/backup/storage-key")) == 1)

    print("== download the latest backup")
    with page.expect_download(timeout=10000) as dl:
        page.click("#bk-local-download")
    d = dl.value
    got = Path(d.path()).read_bytes()
    check("the browser saves the newest backup, under its own name, byte for byte",
          d.suggested_filename == f"cseries-backup-{STAMP_NEW}.tar" and got == DOWNLOAD,
          (d.suggested_filename, len(got)))
    check("it read the list, asked for a link to that one backup, and the browser downloaded the link",
          [r[1] for r in requests if r[1].startswith("/api/backup/files")][-3:]
          == ["/api/backup/files", f"/api/backup/files/{STAMP_NEW}/link", f"/api/backup/files/download/{TICKET}"],
          [r[1] for r in requests if r[1].startswith("/api/backup/files")])
    check("  as a navigation the browser writes to disk, not a fetch whose whole body the page holds",
          fetch_modes.get(f"/api/backup/files/download/{TICKET}") == "navigate"
          and f"/api/backup/files/{STAMP_NEW}/download" not in fetch_modes, fetch_modes)
    check("  and the page says the browser has it", "downloading" in page.text_content("#bk-local-download-state"))

    print("== a Host restored onto a new machine, with no recovery key")
    state["status"] = {"state": "paused", "headline": "Backups are paused: add your recovery key",
                       "detail": "This Host has no recovery key, so it has nobody to encrypt a backup to.",
                       "last_finished_at": "2026-10-18T02:16:40+00:00", "running": False}
    page.goto(BASE + "/system-apps/settings/index.html#view=backup")
    page.wait_for_function("document.getElementById('bk-verdict').textContent.indexOf('paused') >= 0",
                           timeout=15000)
    check("the verdict says backups are paused and what to do",
          page.text_content("#bk-verdict") == "Backups are paused: add your recovery key."
          and "no recovery key" in page.text_content("#bk-verdict-detail")
          and page.get_attribute("#bk-verdict-card", "data-state") == "paused")
    state["status"] = {"state": "working", "headline": "Backups are working", "detail": "",
                       "last_finished_at": "2026-10-18T02:16:40+00:00", "running": False}

    print("== what a backup held, and what a restore did not put back")
    # Invented runs: a backup with two apps' data on this machine, and a
    # restore that could not put one app's data back.
    note = ("The data of 1 app(s) was not put back, and is as it was on this machine: "
            "notes: putting back the data of notes takes 3.0 GiB and would leave this machine 1.1 GiB free.")
    # And what the restore said of the backup's database (host_restore db_note).
    db_note = ("This backup's rows of 1 table(s) this version no longer has were left out: "
               "garden_beds (12 rows).")
    # And which apps an unverified restore installed again (host_restore apps_note).
    apps_note = ("This Host could not confirm it made this backup, so no app's installed code came back "
                 "from it, only each app's data. Installed again: notes (from the App Store). 1 app(s) "
                 "need reinstalling: board: reinstall it from project Board (app build rejected: no index.html).")
    state["runs"] = [
        {"started_at": "2026-10-18T03:00:00+00:00", "reason": "restore", "state": "ok",
         "parts": {"restored": {"taken_at": "2026-10-18T02:15:00+00:00", "app_data_note": note,
                                "database_note": db_note, "apps_note": apps_note}}},
        {"started_at": "2026-10-18T02:15:00+00:00", "reason": "schedule", "state": "ok", "stored": "local",
         "recipients": [], "parts": {"db": {"ok": True, "bytes": 2 << 20},
                                     "apps": {"ok": True, "bytes": 5 << 20, "files": 9},
                                     "app_data": {"ok": True, "bytes": 3 << 20, "apps": [
                                         {"app_id": "notes", "ok": True}, {"app_id": "photos", "ok": True}]},
                                     "gitea": {"ok": True, "repos": 0}}}]
    state["progress"] = {"state": "ok", "taken_at": "2026-10-18T02:15:00+00:00", "app_data_note": note,
                         "database_note": db_note, "apps_note": apps_note}
    page.goto("about:blank")          # the same address again would not load the page again
    page.goto(BASE + "/system-apps/settings/index.html#view=backup")
    page.wait_for_function("document.getElementById('bk-runs').textContent.indexOf('Backed up') >= 0",
                           timeout=15000)
    runs_text = page.text_content("#bk-runs")
    check("a backup's line names the apps' own data it holds",
          "2 apps' own data" in runs_text, runs_text)
    check("  and the restore's line names the app whose data was not put back, and why",
          "notes: putting back the data of notes" in runs_text, runs_text)
    page.wait_for_function("document.getElementById('bk-restore-state').textContent.indexOf('Restored') >= 0",
                           timeout=15000)
    check("the restore card says it too", "was not put back" in page.text_content("#bk-restore-state"),
          page.text_content("#bk-restore-state"))
    check("the restore's line says what the restore said of the backup's database",
          "garden_beds (12 rows)" in runs_text, runs_text)
    check("  and the restore card says it too",
          "garden_beds (12 rows)" in page.text_content("#bk-restore-state"),
          page.text_content("#bk-restore-state"))
    check("the restore's line says which apps were installed again and which need reinstalling",
          "Installed again: notes (from the App Store)" in runs_text
          and "board: reinstall it from project Board" in runs_text, runs_text)
    check("  and the restore card says it too",
          "Installed again: notes (from the App Store)" in page.text_content("#bk-restore-state")
          and "board: reinstall it from project Board" in page.text_content("#bk-restore-state"),
          page.text_content("#bk-restore-state"))
    state.pop("runs")
    state.pop("progress")
    page.goto("about:blank")

    print("== a network member")
    state.update(mode="member", saved=None)
    page.goto(BASE + "/system-apps/settings/index.html#view=backup")
    page.wait_for_function("document.getElementById('bk-where').value === 'network'", timeout=15000)
    check("a member may keep its backups in the network's storage, and starts there",
          not page.eval_on_selector('#bk-where option[value="network"]', "o => o.disabled")
          and page.is_visible("#bk-platform") and not page.is_visible("#bk-local-keep"))
    check("no page errors", not any(c.startswith("pageerror") for c in console),
          [c for c in console if c.startswith("pageerror")])
finally:
    browser.close()
    pw.stop()
    srv.shutdown()

print()
if FAILED:
    print(f"FAILED: {len(FAILED)}")
    sys.exit(1)
print("OK: Settings, Backup chooses where backups go, keeps the bucket's key out of the setting, "
      "and downloads the latest backup")
