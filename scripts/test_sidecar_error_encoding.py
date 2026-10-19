#!/usr/bin/env python3
"""
Sidecar error-encoding gate (smoke, 2026-09-03 — entity-9128 factory-build-9).

WHAT BROKE. cli/cseries_sidecar.py's dispatcher reported handler failures with
`self.send_error(code, str(e))`. BaseHTTPRequestHandler.send_error puts that
string in the HTTP REASON PHRASE, and http.server encodes the status line
latin-1 STRICT (send_response_only). So a handler error containing any
non-ASCII character raised UnicodeEncodeError *while writing the status line*:
nothing was sent, the connection closed with zero bytes, and the caller saw a
bare connection drop instead of the error.

Measured on one Host's build factory-build-9. The harness raised the entirely
correct "kimi has no model configured [em-dash] pick one in the Orchestrator
Coding section"; the em-dash sat at position 42 of "HTTP/1.0 500 <message>";
send_error blew up; and the kernel recorded
ServerDisconnectedError('Server disconnected'). The build had a real, stated,
fixable cause and the platform reported a phantom network fault instead. House
style puts em-dashes in error text throughout, so this masked EVERY non-ASCII
handler error on every harness sidecar.

WHY THIS TEST DRIVES A REAL SOCKET. The defect lives in the status-line write,
not in any function's return value. A unit test on the error helper would pass
against the broken code. So this binds the real Sidecar, serves real requests,
and reads real responses.

Gates (exit 0 = all pass; exit 1 + [sidecar-err] FAIL lines):
  1. a handler raising a NON-ASCII message answers 500 (not a dropped
     connection) — the regression itself
  2. that response carries the original message, em-dash intact, in its body
  3. a handler raising a plain-ASCII message still answers 500 with its text
  4. an unknown route answers 404 with a JSON body. NOTE, honestly: this one
     does NOT pin the encoding bug. A URL path arrives percent-encoded, so it
     is ASCII by the time it reaches the handler, and the two cap-denial
     strings are ASCII literals — neither the 404 nor the 403 site was
     actually reachable by the fault. They were converted with the 500 for
     consistency and because nothing stops a future message from carrying an
     em-dash. This gate only holds their envelope shape.
  5. a successful handler is untouched
  6. the reason phrase stays the stdlib ASCII default, so the status line can
     always be encoded. NOTE: this one only means anything while gate 1 passes
     — on a dropped connection `reason` is the client exception's repr, which
     is ASCII, so it would pass vacuously on the broken code
  7. a handler returning a non-serializable payload answers 500. Same class:
     json.dumps raised in _write BEFORE a byte was written, so it produced the
     identical drop
  8. a malformed Content-Length answers 400. Same class again: the int() was
     outside the try, so it raised before any byte was written
  9. a NEGATIVE Content-Length answers 400 and does not hang. int("-5") parses,
     and rfile.read(-5) reads to EOF, so this one wedged a worker thread
     instead of dropping the connection

Runs against the image (/app) or a source tree (APP_ROOT=<repo>).
"""

import http.client
import json
import os
import signal
import socket
import sys
import threading
import time
from urllib.parse import quote

FAILS = []

EM = "—"                      # the exact character that broke production
NON_ASCII_MSG = f"kimi has no model configured {EM} pick one in the Orchestrator"
ASCII_MSG = "plain ascii failure"


def check(name: str, ok: bool, detail: str = ""):
    print(f"[sidecar-err] {'ok  ' if ok else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, app_root)
    sys.path.insert(0, os.path.join(app_root, "cli"))
    # Bind loopback v4 and give the module a real port before import: serve()
    # refuses an ephemeral bind by design, and BIND_HOST/PORT are read at import.
    port = int(os.environ.get("SIDECAR_TEST_PORT", "18999"))
    os.environ["PRIVATAE_SIDECAR_BIND"] = "127.0.0.1"
    os.environ["PORT"] = str(port)
    os.environ["PRIVATAE_APP_ID"] = "errtest"
    os.environ.pop("CSERIES_HARNESS_CAP", None)
    os.environ["CSERIES_HARNESS_CAP_ENFORCE"] = "0"
    # The newer names win when set; clear them so the values above hold.
    os.environ.pop("PRIVATAE_HARNESS_CAP", None)
    os.environ.pop("PRIVATAE_HARNESS_CAP_ENFORCE", None)
    os.environ.pop("HTTP_PROXY", None)
    os.environ.pop("HTTPS_PROXY", None)

    from cseries_sidecar import Sidecar

    sc = Sidecar()

    @sc.handler("POST", "/boom-unicode")
    def _boom_unicode(_request):
        raise ValueError(NON_ASCII_MSG)

    @sc.handler("POST", "/boom-ascii")
    def _boom_ascii(_request):
        raise ValueError(ASCII_MSG)

    @sc.handler("GET", "/fine")
    def _fine():
        return {"ok": True, "hello": "world"}

    @sc.handler("POST", "/unserializable")
    def _unserializable(_request):
        return {"nope": object()}          # json.dumps raises TypeError

    def call(method: str, path: str):
        """Returns (status, reason, body) or ('DROP', repr(exc), '') when the
        connection died with no response — the shape of the original bug."""
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request(method, path, body=b"{}")
            r = c.getresponse()
            raw = r.read().decode("utf-8", "replace")
            c.close()
            return r.status, r.reason, raw
        except Exception as exc:
            return "DROP", repr(exc), ""

    def raw_request(wire: str):
        """Send a handcrafted request. http.client will not emit a malformed
        Content-Length, and that header is parsed before any handler runs, so
        the only way to exercise it is to write the bytes ourselves. Returns
        (status, body) or ('DROP', repr(exc))."""
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=10)
            s.sendall(wire.encode("latin-1"))
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            try:
                buf += s.recv(65536)
            except OSError:
                pass
            s.close()
            if not buf:
                return "DROP", "no bytes"
            head, _, rest = buf.partition(b"\r\n\r\n")
            first = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            return int(first.split()[1]), rest.decode("utf-8", "replace")
        except Exception as exc:
            return "DROP", repr(exc)

    def checks() -> None:
        # Poll for the listener rather than sleeping a fixed amount. The 20ms
        # is load-bearing: without it 200 refused loopback connects burn
        # through in a few milliseconds, which can be shorter than the time
        # serve() takes to bind — and exhausting the loop SIGTERMs before
        # serve() has installed its handler, so the default action kills the
        # process at exit 143 with no gate output at all.
        for _ in range(200):
            st, _reason, _b = call("GET", "/fine")
            if st == 200:
                break
            time.sleep(0.02)
        else:
            check("sidecar came up", False, "listener never answered /fine")
            return
        _gates()

    def _gates() -> None:
        # 1 + 2 + 6. The regression.
        st, reason, body = call("POST", "/boom-unicode")
        check("a non-ASCII handler error answers 500, not a dropped connection",
              st == 500, f"got status={st} reason={reason}")
        ok_body = False
        try:
            ok_body = EM in json.loads(body).get("error", "")
        except Exception:
            ok_body = EM in body
        check("the response carries the original message, em-dash intact",
              ok_body, f"body={body[:200]!r}")
        check("the reason phrase stays ASCII-encodable",
              isinstance(reason, str) and reason.isascii(), f"reason={reason!r}")

        # 3. The ordinary path still reports.
        st, _reason, body = call("POST", "/boom-ascii")
        check("a plain-ASCII handler error still answers 500 with its text",
              st == 500 and ASCII_MSG in body, f"status={st} body={body[:160]!r}")

        # 4. Envelope shape only — see the note in the module docstring: the
        # path arrives percent-encoded, so this cannot exercise the encoder.
        st, _reason, body = call("POST", "/" + quote(f"nope{EM}route"))
        check("an unknown route answers 404 with a JSON body",
              st == 404 and "error" in body, f"status={st} body={body[:160]!r}")

        # 5. Success is untouched.
        st, _reason, body = call("GET", "/fine")
        ok = False
        try:
            ok = st == 200 and json.loads(body).get("hello") == "world"
        except Exception:
            pass
        check("a successful handler is unchanged", ok,
              f"status={st} body={body[:120]!r}")

        # 7 + 8. The same failure class on two other pre-existing paths: both
        # raised BEFORE any byte was written, so both produced the identical
        # unattributable drop rather than a reportable status.
        st, _reason, body = call("POST", "/unserializable")
        check("a non-serializable handler return answers 500, not a drop",
              st == 500 and "non-serializable" in body,
              f"status={st} body={body[:160]!r}")

        # Must target a REGISTERED route+method: the route lookup runs before
        # the Content-Length parse, so an unknown path 404s first and never
        # reaches the code under test.
        st, body = raw_request(
            "POST /boom-ascii HTTP/1.1\r\nHost: x\r\n"
            "Content-Length: banana\r\n\r\n")
        check("a malformed Content-Length answers 400, not a drop",
              st == 400, f"status={st} body={body[:160]!r}")

        # 9. Worse than malformed: int("-5") PARSES, and rfile.read(-5) reads
        # until EOF, so a client holding the connection open hangs the worker
        # rather than dropping it. The socket timeout below is what turns a
        # regression here into a failed gate instead of a stuck suite.
        st, body = raw_request(
            "POST /boom-ascii HTTP/1.1\r\nHost: x\r\n"
            "Content-Length: -5\r\n\r\n")
        check("a negative Content-Length answers 400, and does not hang",
              st == 400, f"status={st} body={body[:160]!r}")

    # serve() installs SIGTERM/SIGINT handlers, so it must own the MAIN thread.
    # The checks therefore run on a worker and signal the server down when done;
    # serve() returns and main resumes to report.
    def _drive() -> None:
        try:
            checks()
        except BaseException as exc:          # noqa: BLE001
            # Without this the suite passes VACUOUSLY: a raise inside the
            # checker goes to threading.excepthook, FAILS stays empty, and main
            # prints "all gates pass" and exits 0 on a test that never ran.
            check("the checker itself completed", False, repr(exc))
        finally:
            os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_drive, daemon=True).start()
    sc.serve(port=port)          # blocks until the SIGTERM above

    print()
    if FAILS:
        print(f"[sidecar-err] {len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("[sidecar-err] all gates pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
