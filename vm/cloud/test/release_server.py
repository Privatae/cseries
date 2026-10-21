#!/usr/bin/env python3
"""The VM test's stand-in for the release location and the heartbeat
receiver, on 127.0.0.1 inside the test VM, over TLS with a test CA the VM
trusts.

  release_server.py <dir> <cert.pem> <key.pem> <heartbeat pubkey b64> <port>

GET /release/<file> serves <dir>/<file>. POST /api/machines/heartbeat checks
X-Machine-Signature against the box's heartbeat key, as the platform's
receiver does, answers 204 when it verifies and 401 when it does not, and
appends one line per post to <dir>/heartbeats.jsonl.
"""
import base64
import hashlib
import http.server
import json
import os
import ssl
import sys

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ROOT, CERT, KEY, PUB, PORT = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5])
PUBKEY = Ed25519PublicKey.from_public_bytes(base64.b64decode(PUB))


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("release_server: " + fmt % args + "\n")

    def do_GET(self):
        if not self.path.startswith("/release/") or ".." in self.path:
            self.send_error(404)
            return
        p = os.path.join(ROOT, self.path[len("/release/"):])
        if not os.path.isfile(p):
            self.send_error(404)
            return
        with open(p, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != "/api/machines/heartbeat":
            self.send_error(404)
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        rec = {"verified": False}
        try:
            hb = json.loads(body)
            msg = (f"privatae-machine-heartbeat-v1\n{hb['machine_id']}\n{hb['ts']}\n"
                   f"{hashlib.sha256(body).hexdigest()}\n").encode()
            PUBKEY.verify(base64.b64decode(self.headers.get("X-Machine-Signature", "")), msg)
            rec = {"verified": True, "body": hb}
        except (ValueError, KeyError, InvalidSignature) as e:
            rec["error"] = type(e).__name__
        with open(os.path.join(ROOT, "heartbeats.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        self.send_response(204 if rec["verified"] else 401)
        self.end_headers()


ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(CERT, KEY)
srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
srv.serve_forever()
