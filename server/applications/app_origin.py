"""One web origin per isolated app.

Every isolated app (proxy.is_isolated: kernel.SDK_MANAGED_APPS plus every
`community` app) is framed by the desktop from its OWN origin,

    apps-<key>-<handle><zone>

so the browser's same-origin policy walls each app off from every other one.
Before this, every isolated app shared `apps-<handle><zone>`, and a co-open
community app could read the Settings frame's recovery-key input and its window
token.

THE KEY. 20 characters of lowercase base32 of SHA-256 over a versioned prefix
and the app id: 100 bits. A publisher chooses its app id, so the key must be
too long to aim at a chosen app's key (Settings') — a second preimage on 100
bits. An 8-hex key (32 bits) could be aimed in minutes on one CPU core. The
recipe is versioned and pinned by test vectors (scripts/test_app_origin.py):
changing it moves every app's origin and empties its browser storage.

THE LABEL GRAMMAR, identical to the direct-router's handle_for_label. After
lowercasing, dropping the port and removing a zone suffix, the label is exactly one of:

    <handle>               [a-z0-9]{1,63}                      the desktop
    apps-<key>-<handle>    apps-[a-z2-7]{20}-[a-z0-9]{1,37}    one app's origin
    apps-<handle>          apps-[a-z0-9]{1,58}                 the old shared
                                                               origin (refused here)

Handles are 16 hex characters (routes/synaptive.py _mint_handle) and never
contain "-", so the three shapes cannot overlap.

WHAT AN APP ORIGIN MAY SERVE (path_allowed). Distinct origins only isolate if
each origin runs only its own app's code, and the key is public. Without this
list the pod would serve any app's files (/system-apps/<id>/ has no host
check), the Host's project files (/user-apps) and the entity-writable custom/
overlay on any origin, and an app could load its OWN page on Settings' origin.
The middleware is server/middleware/app_origin_guard.py.

WHICH TOKEN AN ORIGIN ACCEPTS (token_fits_origin). An isolated app's window
token is minted ON its own origin (routes/applications.py frame_token) and
carries that origin's key; it is refused anywhere else. A token without a key
(the desktop's, a same-origin app's, the Host's kernel-op token) is refused on
every app origin. So a token minted for app B is useless on app A's origin, and
the desktop never holds an isolated app's token at all.
"""
from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from server.home_origin import ORIGINS
from server.host_names import APP_ZONES

KEY_RECIPE = b"app-origin/v1\x00"
KEY_LEN = 20
# Label budget: 63 - len("apps-") - KEY_LEN - len("-")
MAX_APP_HANDLE_LEN = 63 - 5 - KEY_LEN - 1

_DESKTOP_LABEL_RE = re.compile(r"[a-z0-9]{1,63}")
_APP_LABEL_RE = re.compile(r"apps-([a-z2-7]{%d})-([a-z0-9]{1,%d})" % (KEY_LEN, MAX_APP_HANDLE_LEN))
_LEGACY_LABEL_RE = re.compile(r"apps-([a-z0-9]{1,58})")
_KEY_RE = re.compile(r"[a-z2-7]{%d}" % KEY_LEN)

# kind values
DESKTOP = "desktop"
APP = "app"
LEGACY_APP = "legacy_app"     # apps-<handle>: the retired shared origin
BAD_APP = "bad_app"           # starts "apps-" in a zone but is no valid shape
OTHER = "other"               # not a zone name: in-pod callers, IPs, local dev


def origin_key(app_id: str) -> str:
    """The 20-character key in app `app_id`'s origin label."""
    digest = hashlib.sha256(KEY_RECIPE + app_id.encode("utf-8")).digest()
    return base64.b32encode(digest).decode("ascii").lower()[:KEY_LEN]


def is_key(value: object) -> bool:
    return isinstance(value, str) and bool(_KEY_RE.fullmatch(value))


@dataclass(frozen=True)
class Origin:
    kind: str
    handle: str = ""
    key: str = ""
    zone: str = ""


def host_of(value: str) -> str:
    """The bare host in a Host / X-Forwarded-Host value: first hop, no port,
    lowercased, an IPv6 literal understood. A malformed value is no host."""
    first = (value or "").split(",")[0].strip()
    if not first:
        return ""
    from urllib.parse import urlsplit
    try:
        return (urlsplit("//" + first).hostname or "").lower()
    except ValueError:
        return ""


def browser_host(headers: Mapping[str, str] | Iterable[tuple[bytes, bytes]]) -> str:
    """The name the BROWSER addressed. The direct-router rewrites Host to the
    pod's SAN and puts the browser's name in X-Forwarded-Host (it drops any
    the client sent), so XFH wins; Host is the no-router fallback (local dev,
    in-pod callers). Accepts a Starlette Headers mapping or raw ASGI headers."""
    xfh = host = ""
    if hasattr(headers, "get"):
        xfh = headers.get("x-forwarded-host") or ""
        host = headers.get("host") or ""
    else:
        for k, v in headers:
            name = k.decode("latin-1").lower()
            if name == "x-forwarded-host" and not xfh:
                xfh = v.decode("latin-1")
            elif name == "host" and not host:
                host = v.decode("latin-1")
    return host_of(xfh) if xfh.strip() else host_of(host)


def classify(host: str) -> Origin:
    """Which of the label shapes `host` (already host_of'd) is."""
    for zone in APP_ZONES:
        if host.endswith(zone):
            label = host[: -len(zone)]
            break
    else:
        return Origin(OTHER)
    m = _APP_LABEL_RE.fullmatch(label)
    if m:
        return Origin(APP, handle=m.group(2), key=m.group(1), zone=zone)
    m = _LEGACY_LABEL_RE.fullmatch(label)
    if m:
        return Origin(LEGACY_APP, handle=m.group(1), zone=zone)
    if label.startswith("apps-"):
        return Origin(BAD_APP, zone=zone)
    if _DESKTOP_LABEL_RE.fullmatch(label):
        return Origin(DESKTOP, handle=label, zone=zone)
    return Origin(BAD_APP, zone=zone)


def app_host(app_id: str, desktop_host: str) -> str | None:
    """The origin host app `app_id` is framed from by the desktop at
    `desktop_host` (<handle><zone>), or None when the handle is too long to
    leave room for the key in one DNS label."""
    origin = classify(host_of(desktop_host))
    if origin.kind != DESKTOP or len(origin.handle) > MAX_APP_HANDLE_LEN:
        return None
    return f"apps-{origin_key(app_id)}-{origin.handle}{origin.zone}"


def frame_ancestors_for(handle: str) -> str:
    """`frame-ancestors` for an app origin of `handle`: only that Host's own
    desktop, on every zone it answers to, with the desktop's scheme and port
    (a source without a port matches only the default one). frame-ancestors
    is checked against EVERY ancestor, so no app's origin can frame another
    app's origin."""
    return " ".join(o.origin_of(f"{handle}{o.zone}") for o in ORIGINS)


_FA_RE = re.compile(r"(?i)(^|;)\s*frame-ancestors[^;]*")


def pin_frame_ancestors(csp: str, handle: str) -> str:
    """`csp` with its frame-ancestors directive replaced (or added) so that only
    `handle`'s desktop may frame the response."""
    directive = f"frame-ancestors {frame_ancestors_for(handle)}"
    if _FA_RE.search(csp):
        return _FA_RE.sub(lambda m: f"{m.group(1)}{' ' if m.group(1) else ''}{directive}", csp, count=1)
    csp = csp.strip().rstrip(";").strip()
    return f"{csp}; {directive}" if csp else directive


_DATA_CSP = "sandbox allow-downloads; default-src 'none'"


def response_csp(origin: Origin, path: str, existing: str) -> str:
    """The Content-Security-Policy for a response on app origin `origin`
    (server/main.py security_headers). Every directive of an existing policy
    (a community app's exfil wall, proxy._app_csp) is kept; frame-ancestors is
    pinned to this Host's desktop. Outside this app's own /system-apps/<id>/ —
    /api, the shared files, another app's image — the response is data, never
    a page of this app, so it is also sandboxed: whatever a browser might
    render from it runs no script with this app's origin."""
    csp = existing or ""
    if not _own_app_path(origin, path):
        csp = _DATA_CSP + (f"; {csp}" if csp else "")
    return pin_frame_ancestors(csp, origin.handle)


# An app origin's own session: redeeming the desktop's one-time join code,
# and asking whether the session is live (server/routes/owner.py); and the
# front door's question about a request for this origin, which the door must
# be able to ask on every name it serves (owner_session.door_decision).
APP_SESSION_PATHS = frozenset({"/__host/session", "/__host/renew", "/__host/door"})

# The shared files the first-party isolated apps load from their own origin
# (grepped from their HTML/JS/CSS): the design stylesheets, the
# fonts tokens.css names, the model picker the Orchestrator uses, the SDK and
# the frame bootstrap. On an app origin these come from the image's base/ only,
# never the entity-writable custom/ overlay (main.py OverlayStaticFiles).
SHARED_EXACT = frozenset({
    "/js/app-sdk.js",
    "/js/app-frame-init.js",
    "/js/model-picker.js",
})
_SHARED_FILE_RE = re.compile(r"/(css/[A-Za-z0-9._-]+\.css|fonts/[A-Za-z0-9._-]+\.woff2?)")
_SYSTEM_APP_RE = re.compile(r"/system-apps/([^/]+)(/.*)?")
# Another app's RASTER images, and nothing else of it: the App Store shows an
# image-baked app's on-disk icon (/system-apps/<id>/icon.png, app-store/app.js
# iconUrl) from its own origin. A raster image runs no script whatever a browser
# does with it, and response_csp sandboxes it anyway. SVG is not on the list —
# opened as a document it runs script.
_OTHER_APP_IMAGE_RE = re.compile(r"/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+\.(?:png|jpe?g|webp|gif|ico)")


# On the DESKTOP's origin an isolated app keeps only its images, SVG included:
# the dock and the launcher draw every app's icon from /system-apps/<id>/
# (Sites and Publisher ship icon.svg). Served with a sandbox CSP and nosniff
# (IMAGE_HEADERS), so an SVG opened as a document runs no script.
_DESKTOP_IMAGE_RE = re.compile(
    r"/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+\.(?:png|jpe?g|webp|gif|ico|svg)")

SERVE = "serve"     # the app's files, as its own page
IMAGE = "image"     # an image only, sandboxed (IMAGE_HEADERS)
REFUSE = "refuse"   # nothing

IMAGE_HEADERS = {
    "Content-Security-Policy": "sandbox; default-src 'none'",
    "X-Content-Type-Options": "nosniff",
}


def app_files_here(app_id: str, isolated: bool, origin: Origin, rest: str) -> str:
    """THE one decision on where an app's files may be served: how
    /system-apps/<app_id>/<rest> may be answered on `origin`. Used by the
    origin guard (middleware/app_origin_guard.py, via path_allowed) and by
    both of the proxy's routes (proxy.serve_app, proxy.serve_app_ws).

      - its own origin, apps-<key of app_id>-<handle>: SERVE;
      - another app's origin: IMAGE for a raster image, else REFUSE;
      - the DESKTOP's origin: SERVE for a same-origin app (that is where it is
        meant to run); for an ISOLATED app IMAGE for an image (the dock's
        icons), else REFUSE — its pages would otherwise run same-origin with
        the live desktop, the shell and every same-origin app, which is the
        wall per-app origins exist to put up;
      - no zone name at all (in-pod callers: the page checks dial
        entity-<id>-0...svc.cluster.local:8800, scripts, the CLI): SERVE;
      - the retired shared origin and malformed labels: REFUSE (the guard
        answers them before any route)."""
    path = "/" + rest.lstrip("/")
    if origin.kind == OTHER:
        return SERVE
    if origin.kind == APP:
        if origin_key(app_id) == origin.key:
            return SERVE
        return IMAGE if _OTHER_APP_IMAGE_RE.fullmatch(path) else REFUSE
    if origin.kind == DESKTOP:
        if not isolated:
            return SERVE
        return IMAGE if _DESKTOP_IMAGE_RE.fullmatch(path) else REFUSE
    return REFUSE


def _own_app_path(origin: Origin, path: str) -> bool:
    m = _SYSTEM_APP_RE.fullmatch(path)
    return bool(m) and origin_key(m.group(1)) == origin.key


def path_allowed(origin: Origin, path: str) -> bool:
    """May a request for `path` (scope["path"], decoded) be served on app
    origin `origin`? Only the app whose key the origin carries, its /api, the
    shared files above, and another app's raster images."""
    if origin.kind != APP:
        return False
    m = _SYSTEM_APP_RE.fullmatch(path)
    if m:
        # On an app origin the answer does not depend on whether the app is
        # isolated (its own key, or another app's raster image), so the guard,
        # which has no registry, can ask the one decision directly.
        own = origin_key(m.group(1)) == origin.key
        if not own and not m.group(2):
            return False
        return app_files_here(m.group(1), True, origin, m.group(2) or "") != REFUSE
    if path.startswith("/api/"):
        return True
    if path in APP_SESSION_PATHS:
        return True
    if path in SHARED_EXACT:
        return True
    return bool(_SHARED_FILE_RE.fullmatch(path))


def overlay_allowed(scope: Mapping) -> bool:
    """May the static mount serve the entity-writable custom/ overlay for this
    request? Not on an app origin (the guard marks the scope): there the shared
    files come from the image only, so nothing the entity writes runs with an
    app's origin."""
    return not scope.get("app_origin")


def token_fits_origin(token_origin_key: str, origin: Origin) -> bool:
    """A window token is accepted only on the origin it belongs to: a token
    carrying an app origin's key only on that origin, a token carrying none
    only off every app origin."""
    if origin.kind == APP:
        return bool(token_origin_key) and token_origin_key == origin.key
    return not token_origin_key


def keys_for(apps: Iterable) -> dict[str, str | None]:
    """Origin key per installed app, with collisions refused.

    `apps` are registry.InstalledApp rows (id, provenance, installed_at). Two
    different ids sharing a key would share an origin, so only one keeps it:
    an image-baked app (native/standard) before any other, then the earlier
    install, then the lower id. The rest get None, and the desktop refuses to
    frame an isolated app without a key. With 100-bit keys this is not
    expected to fire; at install, installer._refuse_origin_key_collision
    refuses a community app whose key is already taken."""
    from server.applications.manifest import IMAGE_BAKED_PROVENANCES
    ranked = sorted(
        apps,
        key=lambda a: (a.provenance not in IMAGE_BAKED_PROVENANCES,
                       getattr(a, "installed_at", None) is None,
                       getattr(a, "installed_at", None) or 0, a.id))
    taken: dict[str, str] = {}
    out: dict[str, str | None] = {}
    for a in ranked:
        k = origin_key(a.id)
        if k in taken and taken[k] != a.id:
            out[a.id] = None
        else:
            taken[k] = a.id
            out[a.id] = k
    return out
