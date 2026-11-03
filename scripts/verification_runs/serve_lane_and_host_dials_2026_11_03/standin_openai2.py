"""A tiny OpenAI-compatible server for the e2e: GET /v1/models, POST /v1/chat/completions.
Plain prompts get a fixed sentence. A person's message asking to connect to a server
("connect ... server <address> ... code <code>") gets the calls a model would make, in the
Host's XML tool format: find_tools(name=manage_bastion), then manage_bastion(add_server),
then a sentence. Logs every request. Usage: python3 -I standin_openai2.py 127.0.0.1 8080"""
import json, re, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
MODEL = "standin-llama-1b"
def text_of(m):
    c = m.get("content")
    if isinstance(c, list):
        return " ".join(str(p.get("text", "")) for p in c if isinstance(p, dict))
    return str(c or "")
def reply_for(msgs):
    users = [text_of(m) for m in msgs if m.get("role") == "user"]
    ask = next((u for u in users if re.search(r"connect.*server", u, re.I | re.S) and "code" in u.lower()), None)
    if not ask:
        return "Hello from the stand-in model server on the bastion's machine."
    convo = "\n".join(text_of(m) for m in msgs[1:])
    done = convo.count("function_results") + sum(1 for m in msgs if m.get("role") == "tool")
    addr = re.search(r"server (?:at )?([0-9a-zA-Z.\-\[\]:]+)", ask).group(1).rstrip(".,")
    code = re.search(r"code (?:is )?([0-9A-Za-z\-]{20,29})", ask).group(1)
    if done == 0:
        return ('<function_calls>\n<invoke name="find_tools">\n<parameter name="name">manage_bastion</parameter>\n'
                '</invoke>\n</function_calls>')
    if done == 1:
        args = json.dumps({"op": "add_server", "address": addr, "code": code})
        return ('<function_calls>\n<invoke name="operate">\n<parameter name="tool">manage_bastion</parameter>\n'
                f'<parameter name="args">{args}</parameter>\n'
                '<parameter name="intent">connect the Host to the server the person named, with its code</parameter>\n'
                '</invoke>\n</function_calls>')
    return "I've asked you on a card to connect to that server."
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
        msgs = body.get("messages") or []
        text = reply_for(msgs)
        last = text_of(msgs[-1]) if msgs else ""
        print(f"{time.strftime('%H:%M:%S')} standin POST {self.path} from {self.client_address[0]} "
              f"model={body.get('model')!r} stream={body.get('stream')} last_message={last[:80]!r} "
              f"authorization={'present' if self.headers.get('Authorization') else 'none'} answer={text[:60]!r}", flush=True)
        if self.path != "/v1/chat/completions":
            return self._send(404, {"error": "not found"})
        if not body.get("stream"):
            return self._send(200, {"id": "c1", "object": "chat.completion", "model": MODEL,
                                    "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                                 "finish_reason": "stop"}]})
        self.send_response(200); self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
        def chunk(b):
            self.wfile.write(b"%x\r\n" % len(b) + b + b"\r\n"); self.wfile.flush()
        for i in range(0, len(text), 24):
            chunk(b"data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": text[i:i + 24]}, "finish_reason": None}]}).encode() + b"\n\n")
        chunk(b"data: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}).encode() + b"\n\n")
        chunk(b"data: [DONE]\n\n"); self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()
ThreadingHTTPServer((sys.argv[1], int(sys.argv[2])), H).serve_forever()
