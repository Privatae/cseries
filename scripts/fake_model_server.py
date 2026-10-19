#!/usr/bin/env python3
"""A stand-in model server for tests: answers like Ollama.

  GET  /api/tags              -> one model, "tiny:latest"
  POST /v1/chat/completions   -> a streamed reply, "hello from tiny"

    python3 fake_model_server.py [port]      (default 11434, on 127.0.0.1)
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "tiny:latest"
REPLY = "hello from tiny"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/tags":
            return self._json({"models": [{"name": MODEL}]})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        if self.path != "/v1/chat/completions" or req.get("model") != MODEL:
            return self._json({"error": "unknown"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for piece in (REPLY[:5], REPLY[5:]):
            chunk = {"choices": [{"index": 0, "delta": {"content": piece}}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        done = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        self.wfile.write(f"data: {json.dumps(done)}\n\ndata: [DONE]\n\n".encode())


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 11434
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
