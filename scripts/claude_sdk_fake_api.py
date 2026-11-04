"""A stand-in for Anthropic's Messages API, and a recording CONNECT proxy, for
the gates that run the REAL bundled `claude` CLI offline
(scripts/test_claude_sdk_walls.py; brain/mastercode/claude_sdk_wire.py is what
they test).

Standard library only, so it runs wherever the gate does. Nothing here reaches
a network: both servers listen on 127.0.0.1, the CLI is pointed at the first
with ANTHROPIC_BASE_URL, and every other host it tries goes through the proxy,
which writes the host down and refuses it.

FakeMessagesAPI records every request (path, headers, JSON body) and answers
POST /v1/messages from a script: a queue of replies, each a list of content
blocks plus a stop_reason and usage. An empty queue answers a short text reply.
A request whose cache marks the real API would refuse (more than four, or a 1h
mark after a 5m one) gets the real API's 400 instead. Every POST /v1/messages
is also kept in `history`, which only reset() empties, so a gate can check
every request of a run after clearing `requests` between calls.
Replies stream as Server-Sent Events when the request asks for a stream, in the
event order the Messages API documents (message_start, content_block_start /
_delta / _stop per block, message_delta, message_stop)."""
from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def text_reply(text: str, *, stop_reason: str = "end_turn", usage: dict | None = None) -> dict:
    return {"blocks": [{"type": "text", "text": text}], "stop_reason": stop_reason,
            "usage": usage or {}}


def tool_reply(calls: list[tuple[str, str, dict]], *, text: str = "",
               usage: dict | None = None) -> dict:
    """calls: (id, name, input) per tool_use block, after an optional text."""
    blocks = [{"type": "text", "text": text}] if text else []
    blocks += [{"type": "tool_use", "id": i, "name": n, "input": inp} for i, n, inp in calls]
    return {"blocks": blocks, "stop_reason": "tool_use", "usage": usage or {}}


def refusal_reply(*, text: str = "", category: str | None = "cyber") -> dict:
    blocks = [{"type": "text", "text": text}] if text else []
    return {"blocks": blocks, "stop_reason": "refusal",
            "stop_details": {"type": "refusal", "category": category,
                             "explanation": "invented explanation"}}


def cache_mark_ttls(body) -> list[str]:
    """Every cache mark's TTL in the order the Messages API reads them (tools,
    system, messages); a mark with no ttl is the API's default, '5m'."""
    if not isinstance(body, dict):
        return []
    blocks = list(body.get("tools") or [])
    if isinstance(body.get("system"), list):
        blocks += body["system"]
    for m in body.get("messages") or []:
        if isinstance(m, dict) and isinstance(m.get("content"), list):
            blocks += m["content"]
    return [(b["cache_control"].get("ttl") or "5m") for b in blocks
            if isinstance(b, dict) and isinstance(b.get("cache_control"), dict)]


def cache_mark_error(body) -> str | None:
    """The two cache-mark rules the real API enforces with a 400, worded as it
    words them: at most four marks, and no 1h mark after a 5m one."""
    ttls = cache_mark_ttls(body)
    if len(ttls) > 4:
        return (f"A maximum of 4 blocks with cache_control may be provided. "
                f"Found {len(ttls)}.")
    if "5m" in ttls and "1h" in ttls[ttls.index("5m"):]:
        return ("a ttl='1h' cache_control block must not come after a ttl='5m' "
                "cache_control block. Note that blocks are processed in the "
                "following order: `tools`, `system`, `messages`.")
    return None


class FakeMessagesAPI:
    def __init__(self):
        self.requests: list[dict] = []
        # every POST /v1/messages of the run; clearing `requests` keeps these
        self.history: list[dict] = []
        self.script: list[dict] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.first_request_at: float | None = None

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def messages(self) -> list[dict]:
        """The recorded POST /v1/messages bodies, in arrival order."""
        return [r["body"] for r in self.requests
                if r["method"] == "POST" and r["path"].split("?")[0] == "/v1/messages"]

    def queue(self, *replies: dict) -> None:
        with self._lock:
            self.script.extend(replies)

    def reset(self) -> None:
        with self._lock:
            self.requests.clear()
            self.history.clear()
            self.script.clear()
            self.first_request_at = None

    def _next(self) -> dict:
        with self._lock:
            return self.script.pop(0) if self.script else text_reply("ok")

    def start(self) -> "FakeMessagesAPI":
        api = self

        class _H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):   # quiet
                return

            def _record(self, body):
                with api._lock:
                    if api.first_request_at is None:
                        api.first_request_at = time.monotonic()
                    rec = {"method": self.command, "path": self.path,
                           "headers": {k.lower(): v for k, v in self.headers.items()},
                           "body": body}
                    api.requests.append(rec)
                    if self.command == "POST" and self.path.split("?")[0] == "/v1/messages":
                        api.history.append(rec)

            def _read_body(self):
                n = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    return json.loads(raw or b"null")
                except Exception:
                    return raw.decode("utf-8", "replace")

            def _send_json(self, status, obj):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.send_header("request-id", "req_fake")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._record(None)
                self._send_json(404, {"type": "error", "error": {
                    "type": "not_found_error", "message": "not served by the stand-in"}})

            def do_HEAD(self):
                self._record(None)
                self.send_response(404)
                self.send_header("content-length", "0")
                self.end_headers()

            def do_POST(self):
                body = self._read_body()
                self._record(body)
                if self.path.split("?")[0] != "/v1/messages":
                    if self.path.split("?")[0] == "/v1/messages/count_tokens":
                        self._send_json(200, {"input_tokens": 10})
                        return
                    self._send_json(404, {"type": "error", "error": {
                        "type": "not_found_error", "message": "not served by the stand-in"}})
                    return
                _cache_err = cache_mark_error(body)
                if _cache_err:
                    self._send_json(400, {"type": "error", "error": {
                        "type": "invalid_request_error", "message": _cache_err}})
                    return
                reply = api._next()
                if reply.get("http_status"):
                    self._send_json(reply["http_status"], reply.get("error_body") or {
                        "type": "error", "error": {"type": "rate_limit_error", "message": "Error"}})
                    return
                model = (body or {}).get("model") if isinstance(body, dict) else None
                usage = {"input_tokens": 100, "output_tokens": 7,
                         "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                         **(reply.get("usage") or {})}
                if isinstance(body, dict) and body.get("stream"):
                    self._stream(model or "claude-invented", reply, usage)
                else:
                    self._send_json(200, {
                        "id": "msg_fake", "type": "message", "role": "assistant",
                        "model": model or "claude-invented", "content": reply["blocks"],
                        "stop_reason": reply["stop_reason"], "stop_sequence": None,
                        **({"stop_details": reply["stop_details"]}
                           if reply.get("stop_details") else {}),
                        "usage": usage})

            def _stream(self, model, reply, usage):
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("cache-control", "no-cache")
                self.send_header("request-id", "req_fake")
                self.send_header("connection", "close")
                self.end_headers()

                def ev(obj):
                    self.wfile.write(f"event: {obj['type']}\ndata: {json.dumps(obj)}\n\n".encode())
                    self.wfile.flush()

                start_usage = {k: v for k, v in usage.items() if k != "output_tokens"}
                ev({"type": "message_start", "message": {
                    "id": "msg_fake", "type": "message", "role": "assistant", "model": model,
                    "content": [], "stop_reason": None, "stop_sequence": None,
                    "usage": {**start_usage, "output_tokens": 1}}})
                for i, b in enumerate(reply["blocks"]):
                    if b["type"] == "text":
                        ev({"type": "content_block_start", "index": i,
                            "content_block": {"type": "text", "text": ""}})
                        txt = b["text"]
                        for j in range(0, len(txt), 16):
                            ev({"type": "content_block_delta", "index": i,
                                "delta": {"type": "text_delta", "text": txt[j:j + 16]}})
                            if reply.get("stall_s"):
                                # A reply that stops mid-stream (the cancel test).
                                time.sleep(float(reply["stall_s"]))
                                return
                    elif b["type"] == "tool_use":
                        ev({"type": "content_block_start", "index": i,
                            "content_block": {"type": "tool_use", "id": b["id"],
                                              "name": b["name"], "input": {}}})
                        raw = json.dumps(b["input"])
                        ev({"type": "content_block_delta", "index": i,
                            "delta": {"type": "input_json_delta", "partial_json": raw}})
                    elif b["type"] == "thinking":
                        ev({"type": "content_block_start", "index": i,
                            "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
                        ev({"type": "content_block_delta", "index": i,
                            "delta": {"type": "thinking_delta", "thinking": b["thinking"]}})
                        ev({"type": "content_block_delta", "index": i,
                            "delta": {"type": "signature_delta", "signature": "c2ln"}})
                    ev({"type": "content_block_stop", "index": i})
                delta = {"stop_reason": reply["stop_reason"], "stop_sequence": None}
                if reply.get("stop_details"):
                    delta["stop_details"] = reply["stop_details"]
                ev({"type": "message_delta", "delta": delta,
                    "usage": {"output_tokens": usage.get("output_tokens", 7)}})
                ev({"type": "message_stop"})
                self.close_connection = True

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()


class ConnectRecorder:
    """An HTTP proxy that records which host each request asked for and
    refuses all of them (CONNECT for https, absolute-URI requests for http)."""

    def __init__(self):
        self.hosts: list[str] = []
        self._sock: socket.socket | None = None
        self._stop = threading.Event()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._sock.getsockname()[1]}"

    def start(self) -> "ConnectRecorder":
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(0.2)
        threading.Thread(target=self._serve, daemon=True).start()
        return self

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn):
        try:
            conn.settimeout(5)
            data = b""
            while b"\r\n" not in data and len(data) < 8192:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                data += chunk
            line = data.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            parts = line.split(" ")
            if len(parts) >= 2:
                target = parts[1]
                if "://" in target:
                    target = target.split("://", 1)[1].split("/", 1)[0]
                self.hosts.append(target)
            conn.sendall(b"HTTP/1.1 403 Forbidden\r\ncontent-length: 0\r\nconnection: close\r\n\r\n")
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def stop(self):
        self._stop.set()
        if self._sock:
            self._sock.close()
