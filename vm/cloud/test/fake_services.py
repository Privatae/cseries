"""Stand-ins on the test machine's side of the tap, for the local runtime's
VM test:

  python3 fake_services.py <tap address> <telegram cert> <telegram key> <static dir> <log>

  * https://<tap>:443 answers as Telegram's Bot API would for a bot that has
    had one message: the first getUpdates without an offset gets it (so the
    connector moves its offset on and saves it in its data volume), every
    later one an empty list after a short wait, anything else ok; each request
    is logged with its method, path and offset, so the test can see the Host
    reached it with the bot's token in the path, and from which offset;
  * http://<tap>:8098 serves <static dir> (the signed App Store catalog and
    its bundles, the engines' sources) and /site/, a page the browser and the
    coding engine load from "the internet"; http://<tap>:80 serves the same,
    for the engines' plain proxy, which passes only ports 80 and 443.

Every value here is invented for the test.
"""

import http.server
import json
import os
import ssl
import sys
import threading
import time
import urllib.parse

ADDR, CERT, KEY, STATIC, LOG = sys.argv[1:6]
_lock = threading.Lock()


def note(line):
    with _lock, open(LOG, "a") as f:
        f.write(line + "\n")


class Telegram(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    served = False

    def _answer(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        path = urllib.parse.urlsplit(self.path).path
        try:
            ask = json.loads(body or b"{}")
        except ValueError:
            ask = {}
        if not isinstance(ask, dict):
            ask = {}
        note(f"telegram {self.command} {path} offset={ask.get('offset')}")
        if path.endswith("/getUpdates"):
            if not ask.get("offset") and not Telegram.served:
                Telegram.served = True
                data = {"ok": True, "result": [{"update_id": 4242, "message": {
                    "message_id": 1, "date": int(time.time()), "text": "hello",
                    "from": {"id": 900002, "is_bot": False, "first_name": "Someone"},
                    "chat": {"id": 900002, "type": "private"}}}]}
            else:
                try:
                    wait = min(2.0, float(ask.get("timeout") or 0))
                except (TypeError, ValueError):
                    wait = 0
                time.sleep(wait)
                data = {"ok": True, "result": []}
        elif path.endswith("/getMe"):
            data = {"ok": True, "result": {"id": 900001, "is_bot": True, "first_name": "Test",
                                           "username": "cseries_test_bot"}}
        else:
            data = {"ok": True, "result": True}
        raw = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = _answer

    def log_message(self, *a):
        pass


class Static(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC, **kw)

    def do_GET(self):
        # A proxy may pass the request line on in absolute form
        # ("GET http://host/path"); a server must accept it (RFC 9112 3.2.2),
        # and http.server keeps it whole in self.path.
        if self.path.startswith(("http://", "https://")):
            u = urllib.parse.urlsplit(self.path)
            self.path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        if self.path.startswith("/site/"):
            note(f"site GET {self.path} from {self.client_address[0]}")
            raw = b"<!doctype html><title>Fake site</title><p>a page from the internet</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        note(f"static GET {self.path}")
        return super().do_GET()

    def log_message(self, *a):
        pass


def serve(srv):
    srv.serve_forever()


tg = http.server.ThreadingHTTPServer((ADDR, 443), Telegram)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(CERT, KEY)
tg.socket = ctx.wrap_socket(tg.socket, server_side=True)
st = http.server.ThreadingHTTPServer((ADDR, 8098), Static)
# The same on port 80: the machine's plain proxy for the coding engines
# passes only ports 80 and 443 (vm/runtime/squid.conf).
web = http.server.ThreadingHTTPServer((ADDR, 80), Static)
for s in (tg, st, web):
    threading.Thread(target=serve, args=(s,), daemon=True).start()
note(f"ready pid {os.getpid()}")
while True:
    time.sleep(3600)
