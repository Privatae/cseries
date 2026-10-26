"""Remote access pages: adding a phone, the phone's join page, and what the
remote address shows when the Host cannot be reached.

On the desktop's own address (signed in): /remote shows the remote address,
"Add a phone" with its QR code (server/remote_join.py), and the passkeys that
work on the remote address, each with a Remove button.

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
    r = home_origin.REMOTE
    if r is None:
        return _page("Remote access", '<p class="note">Remote access is not set up on this '
                     'Host.</p>', '<p><a href="/">Continue to C-Series</a></p>')
    st = remote_access.status()
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
    body = []
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
    body.append('<p><a href="/passkey">Passkeys on this computer</a></p>'
                '<p><a href="/">Continue to C-Series</a></p>')
    return _page("Remote access", intro, "".join(body))
