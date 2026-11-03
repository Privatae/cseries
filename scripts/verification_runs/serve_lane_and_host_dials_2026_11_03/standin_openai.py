"""A tiny OpenAI-compatible server: GET /v1/models, POST /v1/chat/completions (streamed or not).
Logs every request it gets to stdout. Usage: python3 -I standin_openai.py 127.0.0.1 8080"""
import json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
MODEL = "standin-llama-1b"
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        print(f"{time.strftime('%H:%M:%S')} standin GET {self.path} from {self.client_address[0]}", flush=True)
        if self.path.rstrip("/") == "/v1/models":
            return self._send(200, {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "standin"}]})
        self._send(404, {"error": "not found"})
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        last = (body.get("messages") or [{}])[-1].get("content")
        print(f"{time.strftime('%H:%M:%S')} standin POST {self.path} from {self.client_address[0]} "
              f"model={body.get('model')!r} stream={body.get('stream')} last_message={str(last)[:80]!r} "
              f"authorization={'present' if self.headers.get('Authorization') else 'none'}", flush=True)
        if self.path != "/v1/chat/completions":
            return self._send(404, {"error": "not found"})
        text = "Hello from the stand-in model server on the bastion's machine."
        if not body.get("stream"):
            return self._send(200, {"id": "c1", "object": "chat.completion", "model": MODEL,
                                    "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                                 "finish_reason": "stop"}]})
        self.send_response(200); self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
        def chunk(b):
            self.wfile.write(b"%x\r\n" % len(b) + b + b"\r\n"); self.wfile.flush()
        for w in text.split(" "):
            chunk(b"data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": w + " "}, "finish_reason": None}]}).encode() + b"\n\n")
        chunk(b"data: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}).encode() + b"\n\n")
        chunk(b"data: [DONE]\n\n"); self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()
ThreadingHTTPServer((sys.argv[1], int(sys.argv[2])), H).serve_forever()
