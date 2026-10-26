"""Remote access pages: adding a phone, the phone's join page, and what the
remote address shows when the Host cannot be reached.

On the desktop's own address (signed in): /remote turns remote access on and
off (server/network/remote_access.py) and shows how it stands: the remote
address, each relay's link, and the certificate's expiry or its job's error.
Once it is on, "Add a phone" with its QR code (server/remote_join.py), and the
passkeys that work on the remote address, each with a Remove button.
/api/owner/remote/status answers the same as JSON, and
/api/owner/remote/enable and /api/owner/remote/disable are the two buttons.

On the remote address: /remote/join is the page the QR code opens; its two
posts spend the code and keep the phone's new passkey, which starts a session
there. /remote-sw.js is a small service worker that answers a page the Host
cannot serve, because the machine is asleep, off or offline, with
/remote/offline instead of the browser's own error page.
"""

from __future__ import annotations

import html
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from server import home_origin, owner_login, owner_passkeys, remote_join
from server.home_origin import HOME
from server.middleware.owner_session import live_session, request_host
from server.network import remote_access
from server.routes.owner import (_client, _day, _form, _no_store, _page, _refusal, _text,
                                 _with_cookie)

router = APIRouter()

_NOT_HERE = {"error": "not on this address"}


def _on_remote(request: Request) -> bool:
    r = home_origin.REMOTE
    return r is not None and request_host(request.scope) == r.host


# The offline page: nothing of the owner's, so it can be kept by the service
# worker and shown with no Host behind it.
OFFLINE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>Host not reachable · C-Series</title>
<style>body{font-family:system-ui,sans-serif;max-width:26rem;margin:12vh auto;padding:0 1rem;color:#1b1b1b}
h1{font-size:1.4rem} p{line-height:1.5}</style></head><body>
<h1>Your Host isn't reachable right now</h1>
<p>The computer your Host runs on may be asleep, turned off, or not connected to the internet.</p>
<p>When it is back, this page opens your Host by itself. It tries again every 30 seconds.</p>
</body></html>"""

SERVICE_WORKER_JS = """// C-Series remote address: when a page cannot be loaded because the Host
// is not reachable, show the offline page instead of the browser's error.
// Only page loads are answered here; everything else goes to the network.
const CACHE = 'cseries-remote-v1';
const OFFLINE = '/remote/offline';
self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.add(OFFLINE)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});
self.addEventListener('fetch', (event) => {
  if (event.request.mode !== 'navigate') return;
  event.respondWith(fetch(event.request).catch(() => caches.match(OFFLINE)));
});
"""

# Registers the service worker; only remote-address pages carry it.
SW_MARK = '<span id="remote-sw" hidden></span>'

# The join page's own script, on top of the pages' shared one (routes/
# owner._SCRIPT, whose ceremony helpers it uses). It is the only page that
# reads a code from its address: the code is in the fragment, which the
# browser never sends, and is taken out of the address as soon as it is read.
JOIN_SCRIPT = r"""
on('join', async () => {
  say('err', '');
  const code = new URLSearchParams(location.hash.slice(1)).get('c') || '';
  if (!code) { fail('This page needs the code from the QR code on your computer.'); return; }
  if (!window.PublicKeyCredential) { fail('This browser has no passkey support.'); return; }
  const a = await post('/api/owner/remote/join/options', {code: code});
  if (!a.ok) { fail(a.j.error); return; }
  history.replaceState(null, '', location.pathname);
  let cred;
  try { cred = await navigator.credentials.create({publicKey: creationOptions(a.j.options)}); }
  catch (e) { fail('The passkey was not made. Make a new code on your computer and try again.'); return; }
  const res = await post('/api/owner/remote/join/verify',
                         {code: code, ceremony: a.j.ceremony, credential: credentialJSON(cred)});
  if (res.ok) { location.href = res.j.next || '/'; } else { fail(res.j.error); }
});
"""


@router.get("/remote-sw.js", include_in_schema=False)
async def remote_service_worker(request: Request):
    if not _on_remote(request):
        return JSONResponse(_NOT_HERE, status_code=404)
    return Response(SERVICE_WORKER_JS, media_type="text/javascript",
                    headers={"Cache-Control": "no-cache"})


@router.get("/remote/offline", include_in_schema=False)
async def remote_offline(request: Request):
    if not _on_remote(request):
        return JSONResponse(_NOT_HERE, status_code=404)
    return Response(OFFLINE_HTML, media_type="text/html; charset=utf-8",
                    headers={"Cache-Control": "no-cache"})


@router.get("/remote/join", include_in_schema=False)
async def remote_join_page(request: Request):
    if not _on_remote(request):
        return _no_store(JSONResponse(_NOT_HERE, status_code=404))
    intro = ('<p>This adds this phone to your Host. The phone makes a passkey of its own: '
             'you confirm with its lock, a fingerprint, your face or a PIN. After that, '
             'this phone signs in to your Host with that passkey.</p>')
    body = ('<button id="join" type="button">Make a passkey on this phone</button>'
            '<p class="note">The code in this link works once, for ten minutes. If it has run '
            'out, make a new one on the computer your Host runs on: Settings, Remote access, '
            'Add a phone.</p>' + SW_MARK)
    return _page("Add this phone", intro, body, JOIN_SCRIPT)


@router.post("/api/owner/remote/join/options", include_in_schema=False)
async def remote_join_options(request: Request):
    if not _on_remote(request):
        return _no_store(JSONResponse(_NOT_HERE, status_code=404))
    body = await _form(request)
    try:
        return _no_store(JSONResponse(await remote_join.start(
            body.get("code"), _client(request), request_host(request.scope))))
    except owner_login.LoginError as exc:
        return _refusal(exc, 403)


@router.post("/api/owner/remote/join/verify", include_in_schema=False)
async def remote_join_verify(request: Request):
    if not _on_remote(request):
        return _no_store(JSONResponse(_NOT_HERE, status_code=404))
    body = await _form(request)
    try:
        token = await remote_join.finish(body.get("ceremony"), body.get("code"),
                                         body.get("credential"), request_host(request.scope),
                                         _text(body, "label") or "phone")
    except owner_login.LoginError as exc:
        return _refusal(exc, 400)
    return _with_cookie(_no_store(JSONResponse({"ok": True, "next": "/"})), token, request)


@router.post("/api/owner/remote/phone", include_in_schema=False)
async def remote_new_phone(request: Request):
    """A join code for a phone, from the desktop's own address."""
    if request_host(request.scope) != HOME.host:
        return _no_store(JSONResponse(_NOT_HERE, status_code=404))
    email, token = await live_session(request.scope)
    if not email:
        return _no_store(JSONResponse({"error": "Sign in first."}, status_code=401))
    try:
        got = await remote_join.new_code(token, request_host(request.scope))
    except owner_login.LoginError as exc:
        return _refusal(exc, 403)
    got["expires_in_s"] = max(0, got["expires_at"] - int(time.time()))
    return _no_store(JSONResponse({"ok": True, **got}))


# The page's own script: the two buttons, and the status line kept fresh.
CONTROL_SCRIPT = r"""
async function remoteSwitch(path, wait) {
  say('err', ''); say('done', wait);
  // Turning it on asks for a fresh passkey check first, as adding a phone does.
  const res = await withCheck(() => post(path, {}));
  if (!res.ok) { fail(res.j.error); return; }
  location.reload();
}
on('remote-on', () => remoteSwitch('/api/owner/remote/enable', 'Turning remote access on...'));
on('remote-off', () => remoteSwitch('/api/owner/remote/disable', 'Turning remote access off...'));
if ($('remote-status')) {
  setInterval(async () => {
    const r = await fetch('/api/owner/remote/status', {credentials: 'same-origin'});
    if (!r.ok) return;
    const j = await r.json().catch(() => null);
    if (j && typeof j.summary === 'string') say('remote-status', j.summary);
  }, 10000);
}
"""


def _when(ts) -> str:
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return str(ts or "")
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


def summary(st: dict) -> str:
    """One plain paragraph of how remote access stands, for the owner."""
    parts = []
    if not st.get("on"):
        return "Remote access is off."
    if st.get("address"):
        parts.append(f"Address: {st['address']}.")
    dns = st.get("dns")
    if dns and dns != "ready":
        parts.append("The network is still making this Host's DNS records." if dns == "wanted" else
                     f"DNS: {dns}" + (f" ({st['dns_error']})" if st.get("dns_error") else "") + ".")
    links = st.get("links") or []
    if links:
        up = sum(1 for link in links if link.get("state") == "up")
        each = "; ".join(f"{link.get('relay')}: {link.get('state')}"
                         + (f" ({link['last_error']})" if link.get("last_error") and link.get("state") != "up"
                            else "") for link in links)
        parts.append(f"Links to the network: {up} of {len(links)} up ({each}).")
    elif st.get("on"):
        parts.append("The connection to the network is not running yet.")
    cert = st.get("certificate") or {}
    if cert.get("not_after"):
        parts.append(f"Certificate valid until {cert['not_after']}.")
    elif st.get("on"):
        parts.append("No certificate yet.")
    if cert.get("error"):
        parts.append(f"Certificate problem (checked {_when(cert.get('checked_at'))}): {cert['error']}")
    if st.get("problem"):
        parts.append(f"Problem: {st['problem']}")
    return " ".join(parts)


def _remote():
    from server.bridge import membership
    return membership.current().remote


async def _owner_on_desktop(request: Request):
    """None when the request is the signed-in owner on the desktop's own
    address, else the answer to give."""
    if request_host(request.scope) != HOME.host:
        return _no_store(JSONResponse(_NOT_HERE, status_code=404))
    email, _token = await live_session(request.scope)
    if not email:
        return _no_store(JSONResponse({"error": "Sign in first."}, status_code=401))
    return None


@router.get("/api/owner/remote/status", include_in_schema=False)
async def remote_status(request: Request):
    refused = await _owner_on_desktop(request)
    if refused is not None:
        return refused
    st = _remote().status()
    st["summary"] = summary(st)
    return _no_store(JSONResponse(st))


@router.post("/api/owner/remote/enable", include_in_schema=False)
async def remote_enable(request: Request):
    """Turn remote access on: the Host's ACME account, then remote.enable.
    It puts this Host on an address the whole internet can reach, so it asks
    what adding a phone asks (remote_join.require_fresh_check): a passkey on
    this computer and a fresh check with it. Turning it off asks only for the
    session."""
    refused = await _owner_on_desktop(request)
    if refused is not None:
        return refused
    _email, token = await live_session(request.scope)
    try:
        await remote_join.require_fresh_check(
            token, "Turning on remote access",
            "Add a passkey on this computer first: on the remote address only a passkey "
            "signs in.")
    except owner_login.LoginError as exc:
        return _refusal(exc, 403)
    try:
        st = await _remote().turn_on()
    except remote_access.RemoteError as exc:
        return _no_store(JSONResponse({"error": exc.message}, status_code=exc.status))
    st["summary"] = summary(st)
    return _no_store(JSONResponse({"ok": True, **st}))


@router.post("/api/owner/remote/disable", include_in_schema=False)
async def remote_disable(request: Request):
    refused = await _owner_on_desktop(request)
    if refused is not None:
        return refused
    st = await _remote().turn_off()
    st["summary"] = summary(st)
    return _no_store(JSONResponse({"ok": True, **st}))


@router.get("/remote", include_in_schema=False)
async def remote_page(request: Request):
    """Remote access, on the desktop's own address: the remote address,
    Add a phone, and the phones that can sign in there."""
    if request_host(request.scope) != HOME.host:
        return _no_store(JSONResponse(_NOT_HERE, status_code=404))
    email, _token = await live_session(request.scope)
    if not email:
        return _no_store(RedirectResponse("/setup" if owner_login.needs_setup() else "/login",
                                          status_code=303))
    st = _remote().status()
    status_line = f'<p id="remote-status" role="status">{html.escape(summary(st))}</p>'
    err = '<p id="err" role="alert"></p><p id="done" role="status"></p>'
    r = home_origin.REMOTE
    if not st.get("on") or r is None:
        intro = ('<p>Remote access lets you open this Host from a phone or another computer '
                 'anywhere. Nothing on this computer is opened to the internet: your Host keeps '
                 'one connection out to the network, and your passkey decides who gets in. It is '
                 'part of the network membership.</p>')
        if not st.get("member"):
            body = ('<p class="note">Join the network first, in Settings, Network.</p>'
                    '<p><a href="/">Continue to C-Series</a></p>')
        elif not st.get("available"):
            body = (f'<p class="note">{html.escape(_remote().available() or "")}</p>'
                    '<p><a href="/">Continue to C-Series</a></p>')
        elif st.get("on"):
            body = (status_line + err + '<button id="remote-off" type="button">Turn off remote access'
                    '</button><p><a href="/">Continue to C-Series</a></p>')
        else:
            body = ((status_line if st.get("problem") or st.get("refused") else "") + err
                    + '<button id="remote-on" type="button">Turn on remote access</button>'
                    '<p><a href="/">Continue to C-Series</a></p>')
        return _page("Remote access", intro, body, CONTROL_SCRIPT)
    pool = owner_login._pool()
    rows = await owner_passkeys.list_passkeys(pool) if pool is not None else []
    phones = [p for p in rows if p["rp_id"] == r.host]
    desktop_keys = len(rows) - len(phones)
    addr = html.escape(r.origin)
    intro = (f'<p>Your Host can also be opened at <strong>{addr}</strong>, from a phone or '
             'another computer anywhere. Nothing on this computer is open to the internet: '
             'your Host keeps one connection out to the network, and your passkey decides who '
             'gets in.</p>')
    if st["problem"]:
        intro += f'<p id="problem" role="alert">{html.escape(st["problem"])}</p>'
    elif not st["running"]:
        intro += '<p class="note">The connection to the network is not running yet.</p>'
    body = [status_line, err]
    if desktop_keys:
        body.append('<button id="add-phone" type="button">Add a phone</button>'
                    '<div id="phone" hidden><p>Scan this with the phone&#39;s camera. The code '
                    'works once, for ten minutes.</p><div id="qr"></div>'
                    '<p class="note" id="phone-link"></p></div>')
    else:
        body.append('<p>Add a passkey on this computer first, so you can still sign in here '
                    'without the phone: <a href="/passkey">Passkeys</a>.</p>')
    items = []
    for p in phones:
        used = f'last used {_day(p["last_used_at"])}' if p["last_used_at"] else "not used yet"
        items.append(f'<li>{html.escape(p["label"] or "phone")} · added {_day(p["created_at"])} · '
                     f'{used}<br><button type="button" data-remove="{html.escape(p["id"], quote=True)}">'
                     'Remove</button></li>')
    body.append('<h2>Phones</h2>' + ('<ul class="keys">' + "".join(items) + '</ul>' if items else
                                     '<p class="note">No phone yet.</p>'))
    body.append('<script src="/js/qrcode.js"></script>')
    body.append('<p><button id="remote-off" type="button">Turn off remote access</button></p>')
    body.append('<p><a href="/passkey">Passkeys on this computer</a></p>'
                '<p><a href="/">Continue to C-Series</a></p>')
    return _page("Remote access", intro, "".join(body), CONTROL_SCRIPT)
