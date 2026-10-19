#!/usr/bin/env python3
"""The media probe must tell an IMAGE tower from a TEXT one.

This gate runs the REAL probe_media_endpoint against stand-in endpoints on
loopback, so it measures BEHAVIOUR rather than asserting that some sentence
still appears in the source. It exists because the first version of the gate
checked only that the image and text towers answered at the same WIDTH, and
width cannot discriminate: a text-embedding model handed a data URI embeds the
STRING, at its usual width, and passed. Every picture then got embedded as
base64 gibberish while coverage reported them searchable.

Verified by disabling the two thresholds: text and constant are both
ACCEPTED without them, REFUSED with them.

  TEXT tower  — embeds whatever STRING it is handed (hashes the input text).
                This is what nomic-embed-text does with a data URI, and it is
                the model that used to pass on width alone.
  IMAGE tower — decodes the PNG and embeds the PIXELS, so two encodings of the
                same picture give the same vector.
  CONSTANT    — ignores its input entirely and returns one fixed vector.

Expected: text REFUSED, constant REFUSED, image ACCEPTED.
"""
import asyncio, base64, hashlib, json, os, struct, sys, threading, zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gate_loopback import ClientClosesFirst

_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "brain"))
sys.path.insert(0, _ROOT)

DIM = 768


def png_pixels(raw):
    """Inflate IDAT + undo row filters -> raw pixel bytes."""
    i, idat, ihdr = 8, b"", None
    while i < len(raw):
        ln = struct.unpack(">I", raw[i:i + 4])[0]
        typ, body = raw[i + 4:i + 8], raw[i + 8:i + 8 + ln]
        if typ == b"IHDR":
            ihdr = body
        elif typ == b"IDAT":
            idat += body
        i += 8 + ln + 4
    w, h, _d, color = struct.unpack(">IIBB", ihdr[:10])
    bpp = {0: 1, 2: 3, 4: 2, 6: 4}[color]
    stride, data = w * bpp, zlib.decompress(idat)
    out, prev, pos = bytearray(), bytearray(stride), 0
    for _ in range(h):
        f = data[pos]; pos += 1
        line = bytearray(data[pos:pos + stride]); pos += stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if f == 1: line[x] = (line[x] + a) & 0xFF
            elif f == 2: line[x] = (line[x] + b) & 0xFF
            elif f == 3: line[x] = (line[x] + (a + b) // 2) & 0xFF
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if (pa <= pb and pa <= pc)
                                      else (b if pb <= pc else c))) & 0xFF
        out += line
        prev = line
    return bytes(out)


def vec_from_seed(seed: bytes):
    """A deterministic unit vector from arbitrary bytes."""
    v, h = [], hashlib.sha256(seed).digest()
    while len(v) < DIM:
        h = hashlib.sha256(h).digest()
        v.extend(b / 255.0 for b in h)
    v = v[:DIM]
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v]


def make_handler(kind):
    # ClientClosesFirst, on a threading server: closing first left a TIME_WAIT
    # on the port per probe request, and CI's firewall refuses a later request
    # that lands on one (scripts/gate_loopback.py).
    class H(ClientClosesFirst, BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = json.loads(raw or b"{}")
            inp = body.get("input")
            if isinstance(inp, list):
                inp = inp[0] if inp else ""
            item = inp if isinstance(inp, str) else json.dumps(inp)

            if kind == "constant":
                vec = vec_from_seed(b"always the same")
            elif kind == "text":
                # Embeds the STRING. A data URI is just a long string to it.
                vec = vec_from_seed(item.encode())
            else:  # image tower
                if item.startswith("data:image"):
                    png = base64.b64decode(item.split(",", 1)[1])
                    vec = vec_from_seed(png_pixels(png))   # PIXELS, not bytes
                else:
                    vec = vec_from_seed(b"TEXT:" + item.encode())
            out = json.dumps({"data": [{"embedding": vec}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
    return H


async def main():
    from mastercode.media_engine import probe_media_endpoint
    results = {}
    # Port 0 = let the OS pick. Fixed ports collide when two gate runs overlap,
    # which on a shared builder reads as a mysterious failure of the feature.
    for kind in ("text", "constant", "image"):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kind))
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        wire, dim, reason = await probe_media_endpoint(
            f"http://127.0.0.1:{port}", f"stand-in-{kind}")
        results[kind] = (wire, dim, reason)
        srv.shutdown()

    ok = True
    for kind, (wire, dim, reason) in results.items():
        verdict = "ACCEPTED" if wire else "REFUSED"
        want = "ACCEPTED" if kind == "image" else "REFUSED"
        mark = "ok  " if verdict == want else "FAIL"
        if verdict != want:
            ok = False
        print(f"  {mark}  {kind:9s} -> {verdict:8s} (want {want})")
        print(f"           {reason}")
    print()
    print("PASS — the probe tells an image tower from a text one" if ok
          else "FAIL — the discriminator does not discriminate")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
