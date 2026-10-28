"""Every request body the Host's app reads is read under a cap.

THE CLASS THIS CLOSES. A route that reads its body with `await request.json()`,
`.body()` or `.form()`, or takes a pydantic or Body() parameter (which FastAPI
reads whole before the route runs), holds the whole body before anything looks at
it. A body sent chunked has no Content-Length to check first, so any caller that
could reach such a route could make the Host hold as much as it cared to send.
The routes in server/routes/network.py were bounded one read at a time; about
110 reads elsewhere had no bound. This middleware bounds all of them, and every
route added later, in one place.

HOW. Pure ASGI, outside every middleware and route that reads a body:
  * a declared Content-Length over the route's cap is answered 413 before a byte
    is read (`receive` is never called, so no 100 Continue is sent either);
  * a body without one (chunked) is counted as it arrives, and the read that
    takes it past the cap raises BodyTooLarge, a 413 HTTPException, out of
    `receive`: the route's read stops there, so at most one chunk past the cap is
    ever held. FastAPI answers it as any HTTPException (its body-parameter reader
    passes HTTPException through). A route that swallowed it and answered
    something else has its answer replaced by the 413, so a refused body is never
    reported as served.
Every 413 says `Connection: close`, so the server stops reading the rest of the
body instead of discarding it on the open connection.

THE CAPS. Each is the most the route's real senders send, never a guess and never
below what the route itself accepts: a route that reads its body under its own
cap (network.py `_json_in`, harness_input.read_request) gets at least that cap
here, and an upload route at least its own upload limit. DEFAULT_CAP covers every
route not in ROUTE_CAPS; ROUTE_CAPS names each route that legitimately takes
more, with where its number comes from. tests/test_request_body_cap.py holds each
entry to a real route and each route's own limit to its entry.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Callable, Iterable, NamedTuple, Optional, Union

from starlette.exceptions import HTTPException
from starlette.routing import compile_path

log = logging.getLogger("cseries.body_cap")

KIB = 1 << 10
MIB = 1 << 20

# A control message: ids, names, a code, a setting, a short list. The cap every
# network route that carries one reads under (network.py _BODY_MAX).
DEFAULT_CAP = 1 * MIB


class RouteCap(NamedTuple):
    methods: frozenset
    path: str       # the route's path template, exactly as it is declared
    # None: the route never holds its body (see STREAMED). A callable: a cap the
    # Host's own memory sets, read per request (memory_room_cap).
    cap: Union[int, Callable[[], int], None]
    why: str


def _cap(methods: str, path: str, cap, why: str) -> RouteCap:
    return RouteCap(frozenset(methods.split()), path, cap, why)


# A body the Host holds whole while it is read: the chunks, the joined body, and
# the strings parsing it makes (Starlette's body() joins the chunks; json.loads
# copies every string out of it).
_HELD_COPIES = 3


def _memory_room() -> Optional[int]:
    """The bytes of memory the Host has room for now: its cgroup limit less its
    working set (mastercode.runtime.host_memory, the same reading its turns are
    fitted with), or the machine's free memory where it runs with no limit (a
    local Host); None when neither can be read."""
    try:
        from mastercode.runtime import host_memory
        lim, ws = host_memory.limit_bytes(), host_memory.working_set_bytes()
        if lim is not None and ws is not None:
            return max(0, lim - ws)
    except Exception:  # noqa: BLE001 — an unreadable reading falls back below
        pass
    try:
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return None


def memory_room_cap(floor: int) -> Callable[[], int]:
    """A cap for a body that is the person's own, with no size of its own: what
    the Host's memory has room to hold whole now, never below `floor`. The memory
    is the Host's to manage (host_memory), so a bigger Host takes a bigger message
    and no platform number cuts it short."""
    def cap() -> int:
        room = _memory_room()
        return floor if room is None else max(floor, room // _HELD_COPIES)
    cap.floor = floor
    return cap


# The source constants, copied as numbers so this module imports nothing from the
# routes (it is built before them in some tests). tests/test_request_body_cap.py
# reads each from its module in the real app and fails if one moves without this.
_MAX_FILE_UPLOAD = 25 * MIB          # routes/context.py MAX_FILE_UPLOAD_BYTES
_BUNDLE_ZIP_MAX = 200 * MIB + 50_000 * KIB   # routes/applications.py _BUNDLE_ZIP_MAX
_MAX_WRITE = 50 * MIB                # capabilities/file_access.py MAX_WRITE_BYTES
# The largest single request a model provider the Host routes a turn to accepts:
# Gemini's inline data, 100 MB per request (ai.google.dev/gemini-api/docs/
# file-input-methods; Anthropic's Messages API takes 32 MB). It is
# the FLOOR of a chat message's cap, never its ceiling: the message carries its
# files inline as base64, any number of them (circles-web/src/lib/attachments.ts
# takes any count of files up to 25 MiB each), every one is kept on the Host, and
# the provider never sees the message as sent: pictures are fitted to its limits
# and documents read to text first (brain/mastercode/attachment_parts.py). So the
# cap above the floor is the Host's memory (memory_room_cap).
_PROVIDER_REQUEST_MAX = 100 * 1000 * 1000
# The floor of a clipboard write's cap: the largest file the Host takes in one go
# (_MAX_FILE_UPLOAD), so the text of one fits. Above it the cap is
# the Host's memory, as for a chat message.
_CLIPBOARD_FLOOR = 25 * MIB

# A route whose cap is None is left alone here because it never holds its body,
# and its data has no real maximum: a person's upload to their own app, streamed on
# to the app (applications/proxy.py _request_body), and a Training Memory corpus,
# which Starlette's form parser spools to disk past 1 MiB a file, and a backup the
# owner uploads to restore from, which host_restore.receive_upload writes to disk as
# it arrives. The test holds this set to what it is.
STREAMED = frozenset({"/system-apps/{app_id}/{path:path}", "/api/training-memory/jobs",
                      "/api/backup/files/upload"})

ROUTE_CAPS: tuple[RouteCap, ...] = (
    # ── server/routes/network.py: the caps its routes read under, never lowered ─
    _cap("POST", "/api/network/harness/run", 32 * MIB,
         "the most the bastion reads of a harness spec (client-go/lane_bounds.go "
         "harnessSpecMaxBytes); network.py _HARNESS_BODY_MAX"),
    _cap("POST", "/api/network/mgmt/run", 12 * MIB,
         "one management frame to a bastion (client-go/mgmt.go mgmtFrameMaxBytes); "
         "network.py _MGMT_BODY_MAX"),
    _cap("POST", "/api/network/bastions/heartbeat", 8 * MIB,
         "a heartbeat report; network.py _LARGE_BODY_MAX"),
    _cap("POST", "/api/network/nac/dcn/plan/propose", 8 * MIB,
         "a move plan of up to 5000 moves; network.py _LARGE_BODY_MAX"),

    # ── routes that read under a cap of their own: never below it ────────────
    _cap("POST", "/mcp", 16 * MIB,
         "a JSON-RPC message; routes/mcp.py _MCP_MAX_BODY_BYTES (harness pods: 1 MiB)"),
    _cap("POST", "/api/connector/platform", 12 * MIB * 4 // 3 + 64 * KIB,
         "a connector's files as base64; routes/connector.py _PLATFORM_BODY_MAX"),
    _cap("POST", "/api/applications/{app_id}/runtime-log", 4 * MIB,
         "an app runtime's log batch (sidecars/app-runtime-python/run.py: 400 lines of "
         "4000 characters); routes/applications.py runtime_log"),
    _cap("PUT", "/api/applications/{app_id}/config/{key}", 16 * MIB,
         "one stored app value; routes/applications.py CONFIG_BODY_MAX"),
    _cap("POST", "/api/brain/context/upload", _MAX_FILE_UPLOAD,
         "one file; routes/context.py MAX_FILE_UPLOAD_BYTES"),

    # ── uploads, sized to their real maximum ─────────────────────────────────
    _cap("POST", "/api/appearance/upload", 20 * MIB,
         "a wallpaper image; routes/appearance.py MAX_SIZE_BYTES"),
    _cap("POST", "/api/applications/install", _BUNDLE_ZIP_MAX,
         "a whole .cseries bundle (cli/cseries.py posts the zip); the largest bundle "
         "the store takes, routes/applications.py _BUNDLE_ZIP_MAX"),
    _cap("POST", "/api/applications/publisher/publish", _BUNDLE_ZIP_MAX + 1 * MIB,
         "the bundle as a form upload, plus the form's short text fields and boundaries"),
    _cap("POST", "/api/designs", 3 * 4 * MIB + 64 * KIB,
         "a design's html, at most routes/designs.py MAX_DESIGN_BYTES (4 MiB) of UTF-8; "
         "a JSON sender that escapes non-ASCII writes up to 3 bytes for each"),
    _cap("POST", "/api/files/write", _MAX_WRITE * 4 // 3 + 64 * KIB,
         "one file as base64, at most the 50 MiB the Host writes in one file "
         "(capabilities/file_access.py MAX_WRITE_BYTES)"),
    _cap("POST", "/api/codespace/file", _MAX_WRITE * 4 // 3 + 64 * KIB,
         "one saved file (no cap of its own): the Host's one-file write bound, "
         "with room for its JSON escaping"),
    _cap("POST", "/api/brain/context/import", _MAX_FILE_UPLOAD,
         "an export's items: the bound the same export gets uploaded as a file"),

    # ── messages that carry files ────────────────────────────────────────────
    _cap("POST", "/api/circles/messages", memory_room_cap(_PROVIDER_REQUEST_MAX),
         "a chat message with any number of files inline: what the Host's memory holds, "
         "never below the largest request a routed provider takes"),
    _cap("POST", "/api/brain/messages", memory_room_cap(_PROVIDER_REQUEST_MAX),
         "the same message shape as /api/circles/messages"),
    _cap("POST", "/api/gitea/propose", memory_room_cap(_MAX_WRITE * 4 // 3 + 64 * KIB),
         "a proposed change's whole files, any number (routes/gitea_propose.py: no count or "
         "size cap; at 1 MiB a file it refused a lockfile): what the Host's memory "
         "holds, never below one file of the Host's one-file write bound as base64"),

    # ── free-form content with no size of its own ────────────────────────────
    _cap("POST", "/api/clipboard/write", memory_room_cap(_CLIPBOARD_FLOOR),
         "a person's text for the clipboard (routes/clipboard.py: `content: str`, no size "
         "of its own; no sender in the repo): what the Host's memory holds, never below the "
         "text of the largest file the Host takes in one go"),
    _cap("POST", "/api/integrations/connections/{connection_id}/call",
         memory_room_cap(_PROVIDER_REQUEST_MAX),
         "an app's call to a provider on a connection the Host granted it (routes/"
         "integrations.py: `body: dict`, sent on as it is; an app may send a mail with "
         "attachments through it; no sender in the repo): what the Host's memory holds. The integrations layer sets "
         "no attachment size (server/integrations, the route and the broker's request side "
         "have none; authz.py _MAX_BODY bounds the response), so the floor is the chat "
         "message's"),

    # ── bodies that grow with the person's own data ──────────────────────────
    _cap("POST", "/api/world/sql", 4 * MIB,
         "a statement of up to 200,000 characters (routes/world.py) at up to 12 bytes "
         "each as escaped JSON, plus its parameters: the route's own limit answers first"),
    _cap("POST", "/api/applications/sites/customers/import", 64 * MIB,
         "a pasted customer list with no limit of its own; it goes to the platform as "
         "one bridge line, read with a 64 MiB limit (bridge_server.py STREAM_LIMIT)"),
    _cap("POST", "/api/artifacts", 64 * MIB,
         "an artifact's files; the largest page the Host's browser check takes "
         "(sidecars/browser/control/document_verify.py MAX_PAGE_CHARS)"),
    _cap("POST", "/api/artifacts/{artifact_id}/files", 64 * MIB, "as /api/artifacts"),
    _cap("POST", "/api/training-memory/jobs/{job_id}/review/bulk", 16 * MIB,
         "every pair id on screen (the review list shows the whole job), about 76 bytes "
         "each: some 200,000 pairs"),

    # ── never held: streamed on, or spooled to disk (STREAMED) ───────────────
    _cap("GET POST PUT DELETE PATCH OPTIONS", "/system-apps/{app_id}/{path:path}", None,
         "a person's request to their own app; a large body is streamed to the app"),
    _cap("POST", "/api/training-memory/jobs", None,
         "a Training Memory corpus; the form parser spools each file to disk"),
    _cap("PUT", "/api/backup/files/upload", None,
         "a backup file the owner uploads to restore from, in pieces of 32 MiB "
         "(system-apps/backups/restore-file.js CHUNK_BYTES); host_restore.receive_upload "
         "writes each piece to disk as it arrives"),
)


def size_words(n: int) -> str:
    """A cap as a person reads it: 32 MiB, 64 KiB, 25.5 MiB."""
    if n >= MIB:
        return f"{n // MIB} MiB" if n % MIB == 0 else f"{n / MIB:.1f} MiB"
    if n >= KIB:
        return f"{n // KIB} KiB" if n % KIB == 0 else f"{n / KIB:.1f} KiB"
    return f"{n} bytes"


def refusal_words(cap: int) -> str:
    return (f"This request is larger than the {size_words(cap)} this route accepts, "
            "so it was refused. Send a smaller one.")


class BodyTooLarge(HTTPException):
    """Raised out of `receive` by the read that takes a body past its cap."""

    def __init__(self, cap: int):
        super().__init__(413, refusal_words(cap))
        self.cap = cap


def _route_path(scope) -> str:
    """The path routing matches (Starlette's get_route_path): scope["path"] without
    the root_path a server may put in front of it."""
    path = scope.get("path") or ""
    root = scope.get("root_path") or ""
    if root and path.startswith(root) and (len(path) == len(root) or path[len(root)] == "/"):
        return path[len(root):]
    return path


def _declared_length(headers) -> tuple[Optional[int], bool]:
    """(the Content-Length the request declares or None, whether it is well formed)."""
    values = {v.strip() for k, v in headers if k == b"content-length"}
    if not values:
        return None, True
    if len(values) != 1:
        return None, False
    v = values.pop()
    if not v.isdigit():
        return None, False
    return int(v), True


def _is_refusal(exc: BaseException) -> bool:
    if isinstance(exc, BodyTooLarge):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_refusal(e) for e in exc.exceptions)
    return False


class RequestBodyCap:
    """The ASGI middleware. `default` and `routes` are the module's own unless a
    test passes others."""

    def __init__(self, app, *, default: int = DEFAULT_CAP,
                 routes: Iterable[RouteCap] = ROUTE_CAPS):
        self.app = app
        self.default = default
        self.routes = [(rc, compile_path(rc.path)[0]) for rc in routes]

    def cap_for(self, method: str, path: str) -> Optional[int]:
        for rc, regex in self.routes:
            if method in rc.methods and regex.match(path):
                return rc.cap() if callable(rc.cap) else rc.cap
        return self.default

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        cap = self.cap_for(scope.get("method") or "GET", _route_path(scope))
        if cap is None:          # a STREAMED route: it never holds the body
            await self.app(scope, receive, send)
            return
        declared, well_formed = _declared_length(scope.get("headers") or ())
        if not well_formed:
            await _answer(send, 400, "This request's Content-Length is not a single number.")
            return
        if declared is not None and declared > cap:
            log.info("refused a %d-byte body on %s %s (cap %d)", declared,
                     scope.get("method"), scope.get("path"), cap)
            await _answer(send, 413, refusal_words(cap))
            return

        taken = 0
        refused = False
        started = False
        replaced = False

        async def capped_receive():
            nonlocal taken, refused
            if refused:
                raise BodyTooLarge(cap)
            message = await receive()
            if message.get("type") == "http.request":
                taken += len(message.get("body") or b"")
                if taken > cap:
                    refused = True
                    log.info("stopped a body past %d bytes on %s %s", cap,
                             scope.get("method"), scope.get("path"))
                    raise BodyTooLarge(cap)
            return message

        async def guarded_send(message):
            nonlocal started, replaced
            if replaced:
                return
            if message.get("type") == "http.response.start" and not started:
                started = True
                if refused:
                    if message.get("status") != 413:
                        # The route swallowed the refusal and answered something else.
                        replaced = True
                        await _answer(send, 413, refusal_words(cap))
                        return
                    message = dict(message, headers=[
                        *[(k, v) for k, v in message.get("headers") or () if k.lower() != b"connection"],
                        (b"connection", b"close")])
            await send(message)

        try:
            await self.app(scope, capped_receive, guarded_send)
        except BaseException as exc:
            # A route that caught the refusal and raised something else of its own
            # is answered the 413 too, never the 500 that error would become.
            # Cancellation is never taken for one.
            if not (refused and (_is_refusal(exc) or isinstance(exc, Exception))):
                raise
            if not _is_refusal(exc):
                log.info("a route raised %s after its body was refused on %s %s",
                         type(exc).__name__, scope.get("method"), scope.get("path"))
            if not started:
                await _answer(send, 413, refusal_words(cap))
            return
        if refused and not started:
            await _answer(send, 413, refusal_words(cap))


async def _answer(send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                            (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": body})
