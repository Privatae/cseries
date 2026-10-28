"""The Content-Security-Policy for the Host's desktop shell, the page at /.

The shell loads everything it runs from its own origin: its scripts, styles,
fonts and images, the API and one WebSocket. It frames the Host's apps, either
under /system-apps/ on its own origin or, for an isolated app, on
apps-<key>-<handle> of the zone the browser addressed, with the same scheme
and port (dashboard/base/js/app-runner.js frameOrigin,
server/applications/app_origin.py app_host). It mounts the Host-made desktop
as a srcdoc frame, and a srcdoc document runs under its parent's policy as
well as its own (dashboard/base/js/sandbox-csp.js). So the shell keeps inline
script and style, and data: and blob: images, fonts and media, which that
document is built from.

What the policy closes, in the shell's own document: script there sending
anything to another host by fetch, XHR, a WebSocket, an image, a font, a frame
or a form; a <base> that re-points the page's relative URLs; plugins; workers;
and the shell being framed by another origin.

What it does not close:
  - inline script itself, which the Host-made desktop needs while it is a
    srcdoc frame;
  - a script navigating the page away (no CSP directive covers navigation);
  - any other document on the shell's origin that carries no policy of its
    own. A policy belongs to one document, not to the origin: script in the
    shell can frame or open a same-origin response without one (an API answer,
    a stylesheet, a same-origin app's page) and call fetch or Image from that
    document's window, which is not under this policy. Measured in Chromium
    and pinned by scripts/test_shell_csp_chromium.py (its check P5). Closing it
    means every document the origin serves carries a policy, not this one
    alone.
"""
from __future__ import annotations

import posixpath

from server import home_origin
from server.applications import app_origin

# The shell's page is / and /index.html, written here as the static mount
# resolves a request path (starlette StaticFiles.get_path: split on "/",
# joined, normpath), because "//", "/./" and "/index.html/" serve the same
# page and must get the same policy.
SHELL_FILES = (".", "index.html")


def _static_file(path: str) -> str:
    return posixpath.normpath(posixpath.join(*(path or "/").split("/")))


def is_shell_document(path: str, content_type: str | None, status: int = 200) -> bool:
    """Whether a response is the shell's own page: a path the static mount
    answers with index.html, as HTML. A 304 for that path carries no
    Content-Type (starlette's NotModifiedResponse keeps only cache headers),
    and a browser updates its stored copy with the 304's headers, so the 304
    gets the policy too: a copy stored without it would otherwise keep
    running without it for as long as its ETag stands."""
    if _static_file(path) not in SHELL_FILES:
        return False
    if status == 304:
        return True
    media = (content_type or "").split(";")[0].strip().lower()
    return media == "text/html"


def shell_policy(origin: app_origin.Origin) -> str:
    """The policy for the shell's page as the browser addressed it. On one of
    the Host's desktop names (the address CSERIES_URL names, or the remote
    address when remote access is on: server/home_origin.py ORIGINS) the shell
    also frames isolated apps on that name's zone and opens its WebSocket by
    name, each with that address's scheme and port, since a source without a
    port matches only the scheme's default one. Anywhere else (an IP address,
    localhost) its apps are on its own origin. The host names in the policy
    come from classify's validated label and a zone the Host answers on, never
    from the raw request header."""
    frame = ["'self'"]
    connect = ["'self'"]
    home = home_origin.origin_of_zone(origin.zone) if origin.kind == app_origin.DESKTOP else None
    if home is not None:
        frame.append(home.origin_of(f"*{home.zone}"))
        ws = "wss" if home.scheme == "https" else "ws"
        connect.append(f"{ws}://{origin.handle}{home.zone}" + (f":{home.port}" if home.port else ""))
    return "; ".join([
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        "media-src 'self' data: blob:",
        "connect-src " + " ".join(connect),
        "frame-src " + " ".join(frame),
        "worker-src 'none'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'self'",
    ])
