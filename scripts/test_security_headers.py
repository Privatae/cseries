#!/usr/bin/env python3
"""The headers server/main.py's security_headers middleware puts on every
response.

Kept: X-Content-Type-Options nosniff, Referrer-Policy no-referrer, and the
framing policy (an app origin's CSP frame-ancestors, else X-Frame-Options
SAMEORIGIN). Gone: X-XSS-Protection. Browsers dropped the filter it switched
on (Chrome, Edge and Safari removed it; Firefox never had it), and where it
lingered its block mode could itself be used to blank parts of a page; OWASP
advises leaving the header out.

The desktop shell's own page also carries its Content-Security-Policy
(server/shell_csp.py), on each of the Host's desktop names and on any address
that is not an app's origin; nothing else of the desktop's does. The names are
checked as a browser reaches them: the default local address
(http://home.cseries.localhost:8800); in a second run with CSERIES_URL and
CSERIES_REMOTE_URL set the way a cloud machine with remote access sets them,
the machine's https name and the remote address, each arriving through its
front door with the browser's name in Host and X-Forwarded-Host; and in a
third, a member's own machine on a port of its own with remote access on, where
the two names differ in scheme and port, so each name's policy must carry its
own. A request's own port, a name outside the Host's zones and a handle the
request made up add no origin the Host's addresses do not name. The remote and
cloud addresses are invented (.example).

Reads the middleware's own source (importing server.main needs the whole
runtime), and runs that function, compiled on its own, against requests for
the shell, an API path and an app origin. Run: python3 scripts/test_security_headers.py
"""
import ast
import os
import subprocess
import sys

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILED = []


# The second run: a cloud machine's own https name and its remote address.
CLOUD_URL = "https://home.b7c4d2.cloud.example"
REMOTE_URL = "https://home.m3n8p5.remote.example"
# The third: a member's own machine, published on a port of its own
# (CSERIES_PORT), with remote access on.
MEMBER_URL = "http://home.cseries.localhost:9000"
CHILD = os.environ.get("SHELL_CSP_ADDRESSES") or ""


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond or not detail else f"  [{str(detail)[:300]}]"))
    if not cond:
        FAILED.append(label)


src = open(os.path.join(ROOT, "server", "main.py"), encoding="utf-8").read()
fn = next((n for n in ast.walk(ast.parse(src))
           if isinstance(n, ast.AsyncFunctionDef) and n.name == "security_headers"), None)
check("server/main.py defines the security_headers middleware", fn is not None)
set_headers = {}
if fn is not None:
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                        and isinstance(t.slice.value, str) and ast.unparse(t.value) == "response.headers"):
                    set_headers[t.slice.value.lower()] = ast.unparse(node.value)
check("every response says nosniff", set_headers.get("x-content-type-options") == "'nosniff'")
check("every response says Referrer-Policy no-referrer", set_headers.get("referrer-policy") == "'no-referrer'")
check("a response that is not an app's is SAMEORIGIN-framed", set_headers.get("x-frame-options") == "'SAMEORIGIN'")
check("an app origin's response gets its CSP (frame-ancestors)", "content-security-policy" in set_headers)
check("no response carries the retired X-XSS-Protection header", "x-xss-protection" not in set_headers)

# The middleware itself, run on its own: its source compiled with the real
# app_origin and shell_csp, a request, and a response from call_next.
sys.path.insert(0, ROOT)
import asyncio  # noqa: E402
import types  # noqa: E402
from urllib.parse import urlsplit  # noqa: E402
from server import home_origin, shell_csp  # noqa: E402
from server.applications import app_origin  # noqa: E402
from starlette.datastructures import Headers  # noqa: E402
from starlette.responses import Response  # noqa: E402
from starlette.staticfiles import NotModifiedResponse  # noqa: E402

fn_src = ast.get_source_segment(src, fn) if fn is not None else ""
ns = {"app_origin": app_origin, "shell_csp": shell_csp}
exec(compile(ast.Module(body=[ast.parse(fn_src).body[0]], type_ignores=[]) if fn_src else
             ast.parse(""), "security_headers", "exec"), ns)
# the decorator line is not part of the segment; the function is plain async
middleware = ns.get("security_headers")


def respond(path, host, ctype="text/html; charset=utf-8", xfh=None, status=200):
    headers = {"host": host}
    if xfh is not None:
        headers["x-forwarded-host"] = xfh
    req = types.SimpleNamespace(headers=headers, scope={"path": path})

    async def call_next(_req):
        if status == 304:
            # What StaticFiles answers a revalidation with: cache headers only.
            return NotModifiedResponse(Headers({"etag": '"e1"', "content-type": ctype}))
        return Response(b"x", status_code=status, media_type=ctype.split(";")[0] if ctype else None)
    return asyncio.run(middleware(req, call_next))


def directive(csp, name):
    """The sources of one directive, or None when the policy has none."""
    for part in csp.split(";"):
        words = part.split()
        if words and words[0] == name:
            return words[1:]
    return None


def name_checks(label, name, frame_src, ws_src):
    """The shell's page on one desktop name: its policy, and the same page at
    every path the static mount answers with index.html."""
    desk = respond("/", name, xfh=name)
    csp = desk.headers.get("content-security-policy") or ""
    want = shell_csp.shell_policy(app_origin.classify(app_origin.host_of(name)))
    check(f"the shell's page on {label} carries the shell policy", csp == want, csp)
    check(f"... it frames its own origin and that name's isolated apps ({frame_src}) only",
          directive(csp, "frame-src") == ["'self'", frame_src], csp)
    check(f"... it connects to its own origin and that name's WebSocket ({ws_src}) only",
          directive(csp, "connect-src") == ["'self'", ws_src], csp)
    check(f"... and on {label} it is still SAMEORIGIN-framed for a browser without CSP",
          desk.headers.get("x-frame-options") == "SAMEORIGIN")
    # An isolated app's frame, as the shell builds its URL (app-runner.js
    # frameOrigin: the page's own scheme, then "apps-<key>-" and the page's
    # host:port), is one the frame-src source matches: same scheme, a host in
    # the source's wildcard zone, the same port.
    app_name = app_origin.app_host("a-test-app", app_origin.host_of(name))
    frame_url = urlsplit(frame_src.split("://")[0] + "://apps-" + app_origin.origin_key("a-test-app")
                         + "-" + name)
    src_scheme, src_rest = frame_src.split("://", 1)
    src_zone, _, src_port = src_rest[1:].partition(":")
    check(f"... the app frame the shell opens there ({frame_url.netloc}) is under that source",
          bool(app_name) and frame_url.hostname == app_name and frame_url.scheme == src_scheme
          and frame_url.hostname.endswith(src_zone) and str(frame_url.port or "") == src_port,
          (frame_url.geturl(), frame_src))
    for alias in ("//", "/./", "/index.html/", "//index.html", "/js/../index.html"):
        got = respond(alias, name, xfh=name).headers.get("content-security-policy")
        check(f"the shell's page at {alias!r} on {label} carries the shell policy too", got == want, got)
    app = respond("/", name, xfh=app_name).headers.get("content-security-policy") or ""
    check(f"an app's origin on {label} keeps its own policy, not the shell's",
          "frame-ancestors" in app and "default-src 'self'" not in app, app)


if middleware is None:
    check("the middleware compiles on its own", False)
elif not CHILD:
    home = home_origin.HOME
    check("this run is the default local address (CSERIES_URL unset)",
          home.origin == "http://home.cseries.localhost:8800", home.origin)
    name_checks("the local address", "home.cseries.localhost:8800",
                "http://*.cseries.localhost:8800", "ws://home.cseries.localhost:8800")
    want = shell_csp.shell_policy(app_origin.classify("home.cseries.localhost"))
    local = respond("/index.html", "localhost:8800").headers.get("content-security-policy") or ""
    check("the shell's page on an address that is no desktop name (localhost) carries the "
          "policy for its own origin only",
          local == shell_csp.shell_policy(app_origin.classify("localhost"))
          and directive(local, "frame-src") == ["'self'"]
          and directive(local, "connect-src") == ["'self'"], local)
    nm = respond("/", "home.cseries.localhost:8800", status=304)
    check("a 304 for the shell's page carries the policy (the browser keeps the 304's headers)",
          nm.status_code == 304 and "content-type" not in nm.headers
          and nm.headers.get("content-security-policy") == want, dict(nm.headers))
    check("a 304 for an asset gets no shell policy",
          "content-security-policy" not in respond("/js/ws-client.js", "home.cseries.localhost:8800",
                                                   status=304).headers)
    check("an asset gets no shell policy",
          "content-security-policy" not in respond("/js/ws-client.js", "home.cseries.localhost:8800",
                                                   "text/javascript").headers)
    check("a page in a subfolder gets no shell policy",
          "content-security-policy" not in respond("/js/", "home.cseries.localhost:8800").headers)
    check("an API response gets no shell policy",
          "content-security-policy" not in respond("/api/applications", "home.cseries.localhost:8800",
                                                   "application/json").headers)
    check("a page other than the shell's gets no shell policy",
          "content-security-policy" not in respond("/system-apps/settings/index.html",
                                                   "home.cseries.localhost:8800").headers)
    check("the owner's sign-in page gets no shell policy",
          "content-security-policy" not in respond("/login", "home.cseries.localhost:8800").headers)
    # A host header that tries to write the policy is no desktop name: the
    # policy names no host from it.
    forged = respond("/", "x.cseries.localhost; script-src *").headers.get("content-security-policy") or ""
    check("a forged Host header cannot write into the policy",
          "script-src *" not in forged and "ws://" not in forged and "wss://" not in forged, forged)
    for run, url, title in (("cloud", CLOUD_URL, "a cloud machine with remote access"),
                            ("member", MEMBER_URL, "a member's own machine on its own port, "
                                                   "with remote access")):
        env = dict(os.environ, SHELL_CSP_ADDRESSES=run, CSERIES_URL=url,
                   CSERIES_REMOTE_URL=REMOTE_URL,
                   CSERIES_REMOTE_STATE=os.path.join(ROOT, "no-such-remote-state.json"))
        print(f"  -- {title} --", flush=True)
        child = subprocess.run([sys.executable, os.path.abspath(__file__)], env=env, timeout=120)
        check(f"the run for {title} passed", child.returncode == 0, f"exit {child.returncode}")
elif CHILD == "member":
    check("this run has the local address on its own port and the remote address",
          home_origin.HOME.origin == MEMBER_URL and home_origin.REMOTE is not None
          and home_origin.REMOTE.origin == REMOTE_URL,
          (home_origin.HOME, home_origin.REMOTE))
    # Two names that differ in scheme and port: each name's policy is built
    # from that name's own address, never the desktop's for both.
    name_checks("the local address on its own port", "home.cseries.localhost:9000",
                "http://*.cseries.localhost:9000", "ws://home.cseries.localhost:9000")
    name_checks("the remote address of a machine at home", "home.m3n8p5.remote.example",
                "https://*.m3n8p5.remote.example", "wss://home.m3n8p5.remote.example")
    # What a request can choose. The scheme, the port and the zone are the
    # Host's own; only the desktop label comes from the request, and only one
    # in a zone the Host answers on.
    asked = respond("/", "home.cseries.localhost:1").headers.get("content-security-policy") or ""
    check("a request's own port never reaches the policy (the address's port does)",
          directive(asked, "frame-src") == ["'self'", "http://*.cseries.localhost:9000"]
          and directive(asked, "connect-src") == ["'self'", "ws://home.cseries.localhost:9000"], asked)
    for foreign in ("home.cseries.example", "home.cseries.localhost.example", "home.m3n8p5.remote.example.org"):
        csp = respond("/", foreign).headers.get("content-security-policy") or ""
        check(f"a name outside the Host's zones ({foreign}) adds no origin to the policy",
              directive(csp, "frame-src") == ["'self'"] and directive(csp, "connect-src") == ["'self'"], csp)
    made_up = respond("/", "other.cseries.localhost:9000").headers.get("content-security-policy") or ""
    check("a handle the request made up names only itself, on the Host's zone, scheme and port",
          directive(made_up, "frame-src") == ["'self'", "http://*.cseries.localhost:9000"]
          and directive(made_up, "connect-src") == ["'self'", "ws://other.cseries.localhost:9000"], made_up)
else:
    check("this run has the cloud name as its desktop address and the remote address",
          home_origin.HOME.origin == CLOUD_URL and home_origin.REMOTE is not None
          and home_origin.REMOTE.origin == REMOTE_URL,
          (home_origin.HOME, home_origin.REMOTE))
    name_checks("the cloud name", "home.b7c4d2.cloud.example",
                "https://*.b7c4d2.cloud.example", "wss://home.b7c4d2.cloud.example")
    name_checks("the remote address", "home.m3n8p5.remote.example",
                "https://*.m3n8p5.remote.example", "wss://home.m3n8p5.remote.example")
    # Through the door the Host may see its own upstream name in Host; the
    # browser's name is the one in X-Forwarded-Host, which the policy follows.
    door = respond("/", "127.0.0.1:8800", xfh="home.m3n8p5.remote.example").headers.get(
        "content-security-policy") or ""
    check("a request whose browser name is only in X-Forwarded-Host gets that name's policy",
          door == shell_csp.shell_policy(app_origin.classify("home.m3n8p5.remote.example")), door)
    for label, name, other in (("the cloud name", "home.b7c4d2.cloud.example", "m3n8p5.remote"),
                               ("the remote address", "home.m3n8p5.remote.example", "b7c4d2.cloud")):
        csp = respond("/", name, xfh=name).headers.get("content-security-policy") or ""
        check(f"the policy on {label} names nothing of the other address", other not in csp, csp)

if FAILED:
    print(f"test_security_headers: {len(FAILED)} FAILED")
    sys.exit(1)
print("test_security_headers: all checks passed")
