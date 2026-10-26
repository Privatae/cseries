"""Setup and sign-in pages for the owner of a standalone Host.

The pages are plain HTML with a small inline script, so they work before any
app or dashboard bundle has loaded. server/owner_login.py holds the rules;
these routes only carry the form and set or clear the cookie.
"""

from __future__ import annotations

import html
import logging

from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from server import cloud_machine, home_origin, owner_login, owner_passkeys, trusted_proxies
from server.home_origin import HOME
from server.middleware.localhost_only import DOOR_PATH, _unmap_v4
from server.middleware.owner_session import (_cookie, door_decision, join_url, live_session,
                                              own_app_origin, request_host)

router = APIRouter()
logger = logging.getLogger("cseries.owner_login")

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · C-Series</title>
<style>
 body{{font-family:system-ui,sans-serif;max-width:24rem;margin:12vh auto;padding:0 1rem;color:#1b1b1b}}
 h1{{font-size:1.4rem}} label{{display:block;margin:.8rem 0 .2rem}}
 input{{width:100%;padding:.55rem;font-size:1rem;box-sizing:border-box}}
 button{{margin-top:1.2rem;padding:.6rem 1rem;font-size:1rem;width:100%}}
 p.note{{color:#555;font-size:.9rem}} p#err{{color:#b00020}} p#done{{color:#1b5e20}}
 details{{margin-top:1.4rem}} summary{{cursor:pointer;color:#333}}
 ol.codes{{font-family:ui-monospace,monospace;font-size:1.05rem;line-height:1.8;background:#f4f4f4;
          padding:.6rem 1rem .6rem 2.4rem}}
 ul.keys{{list-style:none;padding:0}} ul.keys li{{border-top:1px solid #ddd;padding:.6rem 0}}
 ul.keys button{{margin-top:.4rem;width:auto}}
</style></head><body>
<h1>{title}</h1>
{intro}
{body}
<p id="done" role="status"></p>
<p id="err" role="alert"></p>
<script>{script}</script></body></html>"""

# The pages' script, passed to _PAGE as a value, so its braces are its own.
# Every form with data-action posts its fields as JSON and, when the answer
# is ok, goes where the answer's `next` says. The passkey half turns the
# server's options (the webauthn library's JSON, base64url fields) into what
# navigator.credentials takes, and the browser's answer back.
_SCRIPT = r"""
const $ = (id) => document.getElementById(id);
const say = (id, msg) => { const el = $(id); if (el) el.textContent = msg || ''; };
const fail = (msg) => { say('done', ''); say('err', msg || 'Something went wrong.'); };
async function post(url, body) {
  const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                              body: JSON.stringify(body || {}), credentials: 'same-origin'});
  const j = await r.json().catch(() => ({}));
  return {ok: r.ok, j: j};
}
document.querySelectorAll('form[data-action]').forEach((form) => {
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    say('err', '');
    const res = await post(form.dataset.action, Object.fromEntries(new FormData(form)));
    if (res.ok) { location.href = res.j.next || '/'; } else { fail(res.j.error); }
  });
});
const b64u = {
  dec(s) {
    s = s.replace(/-/g, '+').replace(/_/g, '/');
    s += '==='.slice((s.length + 3) % 4);
    return Uint8Array.from(atob(s), (c) => c.charCodeAt(0)).buffer;
  },
  enc(buf) {
    let s = '';
    new Uint8Array(buf).forEach((x) => { s += String.fromCharCode(x); });
    return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  },
};
const ids = (list) => (list || []).map((c) => Object.assign({}, c, {id: b64u.dec(c.id)}));
function creationOptions(o) {
  if (window.PublicKeyCredential && PublicKeyCredential.parseCreationOptionsFromJSON) {
    return PublicKeyCredential.parseCreationOptionsFromJSON(o);
  }
  return Object.assign({}, o, {challenge: b64u.dec(o.challenge),
    user: Object.assign({}, o.user, {id: b64u.dec(o.user.id)}),
    excludeCredentials: ids(o.excludeCredentials)});
}
function requestOptions(o) {
  if (window.PublicKeyCredential && PublicKeyCredential.parseRequestOptionsFromJSON) {
    return PublicKeyCredential.parseRequestOptionsFromJSON(o);
  }
  return Object.assign({}, o, {challenge: b64u.dec(o.challenge), allowCredentials: ids(o.allowCredentials)});
}
function credentialJSON(c) {
  if (typeof c.toJSON === 'function') { try { return c.toJSON(); } catch (e) { /* below */ } }
  const r = c.response;
  const out = {id: c.id, rawId: b64u.enc(c.rawId), type: c.type,
               clientExtensionResults: c.getClientExtensionResults ? c.getClientExtensionResults() : {},
               response: {clientDataJSON: b64u.enc(r.clientDataJSON)}};
  if (c.authenticatorAttachment) out.authenticatorAttachment = c.authenticatorAttachment;
  if (r.attestationObject) {
    out.response.attestationObject = b64u.enc(r.attestationObject);
    out.response.transports = r.getTransports ? r.getTransports() : [];
  }
  if (r.authenticatorData) {
    out.response.authenticatorData = b64u.enc(r.authenticatorData);
    out.response.signature = b64u.enc(r.signature);
    if (r.userHandle) out.response.userHandle = b64u.enc(r.userHandle);
  }
  return out;
}
async function ceremony(name, create) {
  if (!window.PublicKeyCredential) {
    return {ok: false, j: {error: 'This browser has no passkey support.'}};
  }
  const a = await post('/api/owner/passkey/' + name + '/options', {});
  if (!a.ok) return a;
  let cred;
  try {
    cred = create ? await navigator.credentials.create({publicKey: creationOptions(a.j.options)})
                  : await navigator.credentials.get({publicKey: requestOptions(a.j.options)});
  } catch (e) {
    return {ok: false, j: {error: 'The passkey was not used.'}};
  }
  return post('/api/owner/passkey/' + name + '/verify', {ceremony: a.j.ceremony, credential: credentialJSON(cred)});
}
async function withCheck(run) {
  let res = await run();
  if (!res.ok && res.j.step_up) {
    const check = await ceremony('stepup', false);
    if (!check.ok) return check;
    res = await run();
  }
  return res;
}
function showCodes(codes) {
  if (!codes || !codes.length) return;
  const list = $('code-list');
  list.textContent = '';
  codes.forEach((code) => { const li = document.createElement('li'); li.textContent = code; list.appendChild(li); });
  $('codes').hidden = false;
}
function on(id, handler) { const el = $(id); if (el) el.addEventListener('click', handler); }
on('passkey', async () => {
  say('err', '');
  const res = await ceremony('login', false);
  if (res.ok) { location.href = res.j.next || '/'; } else { fail(res.j.error); }
});
const ended = (n) => n ? ' ' + n + ' other session' + (n === 1 ? ' was' : 's were') + ' signed out.' : '';
on('add', async () => {
  say('err', '');
  const res = await withCheck(() => ceremony('register', true));
  if (!res.ok) { fail(res.j.error); return; }
  say('done', (res.j.recovery_codes ? 'Passkey added. Your password no longer signs in to this Host.'
                                    : 'Passkey added.') + ended(res.j.sessions_ended));
  showCodes(res.j.recovery_codes);
});
document.querySelectorAll('button[data-remove]').forEach((btn) => {
  btn.addEventListener('click', async () => {
    say('err', '');
    const res = await withCheck(() => post('/api/owner/passkey/remove', {id: btn.dataset.remove}));
    if (!res.ok) { fail(res.j.error); return; }
    const li = btn.closest('li');
    if (li) li.remove();
    say('done', 'Passkey removed.' + ended(res.j.sessions_ended));
  });
});
on('end-others', async () => {
  say('err', '');
  const res = await withCheck(() => post('/api/owner/sessions/end-others', {}));
  if (!res.ok) { fail(res.j.error); return; }
  say('done', res.j.sessions_ended ? ended(res.j.sessions_ended).trim()
                                   : 'No other session was signed in.');
});
on('confirm', async () => {
  say('err', '');
  const res = await ceremony('stepup', false);
  if (res.ok) { say('done', 'Done. For the next five minutes this Host can open its pairing port for you.'); }
  else { fail(res.j.error); }
});
on('restart', async () => {
  say('err', '');
  const res = await post('/api/owner/machine/restart', {});
  if (!res.ok) { fail(res.j.error); return; }
  say('done', 'The machine restarts in a few seconds. It comes back locked: unlock it, then sign in again.');
});
on('add-phone', async () => {
  say('err', '');
  const res = await withCheck(() => post('/api/owner/remote/phone', {}));
  if (!res.ok) { fail(res.j.error); return; }
  const box = $('qr');
  box.textContent = '';
  if (window.qrcode) {
    const q = qrcode(0, 'M');
    q.addData(res.j.url);
    q.make();
    box.innerHTML = q.createSvgTag({cellSize: 5, margin: 4, scalable: true, alt: 'QR code for adding a phone'});
  }
  say('phone-link', 'Or open this on the phone: ' + res.j.url);
  $('phone').hidden = false;
  say('done', 'The code works for ten minutes.');
});
if ($('remote-sw') && 'serviceWorker' in navigator) {
  navigator.serviceWorker.register('/remote-sw.js').catch(() => {});
}
on('new-codes', async () => {
  say('err', '');
  const res = await withCheck(() => post('/api/owner/recovery/new', {}));
  if (!res.ok) { fail(res.j.error); return; }
  say('done', 'New recovery codes made. The earlier ones no longer work.');
  showCodes(res.j.recovery_codes);
});
"""


def _form_html(action: str, fields: str, button: str) -> str:
    return f'<form data-action="{action}">{fields}<button type="submit">{button}</button></form>'


def _page(title: str, intro: str, body: str, extra_script: str = "") -> HTMLResponse:
    return HTMLResponse(_PAGE.format(title=title, intro=intro, body=body,
                                     script=_SCRIPT + extra_script),
                        headers={"Cache-Control": "no-store"})


_EMAIL = '<label for="email">Email</label><input id="email" name="email" type="email" autocomplete="username" required>'


def _client(request: Request) -> str:
    """Who is signing in, for the wrong-password backoff only. Behind a proxy
    every visitor arrives from the proxy, so a backoff keyed on the peer would
    let one attacker lock the owner out. When the peer is a trusted proxy, the
    nearest X-Forwarded-For address that is not itself a trusted proxy is used
    instead. Never used to decide access: the gates read the socket peer."""
    return trusted_proxies.visitor(request.client.host if request.client else None,
                                   request.headers.get("x-forwarded-for"))


def _browser_used_https(request: Request) -> bool:
    """Whether the session cookie is marked Secure. Always on an https Host
    (CSERIES_URL): its cookie is __Host- named, which a browser keeps only
    with Secure. Otherwise when this hop is HTTPS, or a trusted proxy
    (CSERIES_TRUSTED_PROXIES) says the browser's was: behind a proxy that
    terminates TLS this hop is plain HTTP and only the proxy's header says
    otherwise (server/launch.py turns uvicorn's own reading of it off).
    Anyone else's X-Forwarded-Proto or Forwarded is a header the visitor
    wrote, and is not read."""
    if owner_login.secure_cookie(request_host(request.scope)) or request.url.scheme == "https":
        return True
    if not trusted_proxies.peer_is_trusted(_unmap_v4(request.client.host if request.client else None)):
        return False
    xfp = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if xfp == "https":
        return True
    first = (request.headers.get("forwarded") or "").split(",")[0]
    return any(p.strip().lower() == "proto=https" for p in first.split(";"))


def _with_cookie(resp, token: str, request: Request):
    resp.set_cookie(owner_login.cookie_name(request_host(request.scope)), token,
                    max_age=int(owner_login.SESSION_MAX.total_seconds()),
                    httponly=True, samesite="strict",
                    secure=_browser_used_https(request), path="/")
    return resp


async def _form(request: Request) -> dict:
    """The JSON object the page posted, or {} for anything else, so a
    malformed body is a refusal rather than a server error."""
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _text(body: dict, key: str) -> str:
    v = body.get(key)
    return v if isinstance(v, str) else ""


def _unlock_link(st: dict | None, text: str) -> str:
    url = cloud_machine.unlock_url(st)
    return f'<p><a href="{html.escape(url, quote=True)}">{text}</a></p>' if url else ""


def _remote_refusal() -> HTMLResponse:
    """/setup and the password on the remote address: never there."""
    resp = _page("Not on this address", '<p>Set up your Host, and sign in with your password, on '
                 'the computer it runs on. On this address your Host signs in with a passkey or a '
                 'recovery code only.</p>', '<p><a href="/login">Sign in</a></p>')
    resp.status_code = 403
    return _no_store(resp)


def _is_remote(request: Request | None) -> bool:
    return request is not None and home_origin.is_remote_host(request_host(request.scope))


@router.get("/setup", include_in_schema=False)
async def setup_page(request: Request = None):
    if _is_remote(request):
        return _remote_refusal()
    if not owner_login.needs_setup():
        return RedirectResponse("/login", status_code=303)
    st = cloud_machine.state()
    if cloud_machine.setup_blocked():
        # A cloud machine: its disk's keys first (server/cloud_machine.py),
        # on its unlock page, which then makes the owner and signs them in.
        intro = ('<p>Before anything of yours is kept on this machine, set up the keys that '
                 'unlock its disk: a passkey and a recovery key that only you hold. On '
                 'privatae.ai, press Set up your machine on this machine&#39;s card: it opens '
                 'this machine&#39;s unlock page, and you come back here signed in.</p>')
        return _page("Set up C-Series", intro, _unlock_link(st, "Set up the disk&#39;s keys"))
    if cloud_machine.keeps_vault(st):
        # The owner is made from the unlock page (owner_login.claim_owner).
        intro = ('<p>Your owner account is made on this machine&#39;s unlock page: confirm with '
                 'your passkey there, and you come back here signed in.</p>')
        return _page("Set up C-Series", intro, _unlock_link(st, "Go to this machine&#39;s unlock page"))
    code_field = ('<label for="code">Setup code</label><input id="code" name="code" '
                  'autocomplete="one-time-code" required>')
    if st is not None:
        # A cloud machine built before machines had a locked disk, and so an
        # unlock page: its code came with it.
        intro = ('<p class="note">Create the owner account with the setup code that came with '
                 "this machine.</p>")
    else:
        # The Host cannot tell how it was installed, so the page names each
        # way: npx first (the cseries command reads the code from the Host's
        # log and prints it: packages/cseries/src/setup.js showSetup), then
        # Docker Compose, then a Host handed over with its code.
        intro = ('<p class="note">Create the owner account. To get the setup code, run '
                 "<code>npx cseries open</code> on the machine this Host runs on if C-Series "
                 "was installed with npx cseries; with Docker Compose it is in this Host's "
                 "log (<code>docker compose logs host</code>). A Host someone set up for you "
                 "came with its code.</p>")
    return _page(
        "Set up C-Series",
        intro,
        _form_html("/api/owner/setup",
                   code_field + _EMAIL +
                   f'<label for="password">Password (at least {owner_login.MIN_PASSWORD_LEN} '
                   'characters)</label><input id="password" name="password" type="password" '
                   'autocomplete="new-password" required>',
                   "Create owner"))


async def _passkeys_and_codes() -> tuple[int, int]:
    pool = owner_login._pool()
    if pool is None:
        return 0, 0
    return await owner_passkeys.passkey_count(pool), await owner_passkeys.unused_recovery_codes(pool)


_RECOVERY_FORM = ('<details><summary>Sign in with a recovery code instead</summary>'
                  + _form_html("/api/owner/recovery/login",
                               '<label for="code">Recovery code</label><input id="code" name="code" '
                               'autocomplete="one-time-code" required>',
                               "Sign in with the code")
                  + '<p class="note">Each recovery code works once.</p></details>')


def _remote_login_page() -> HTMLResponse:
    """The remote address's sign-in: a passkey made on this address, or a
    recovery code. Never the password."""
    if owner_login.needs_setup():
        return _page("Sign in to C-Series", '<p>This Host is not set up yet. Set it up on the '
                     'computer it runs on.</p>', "")
    body = ('<button id="passkey" type="button">Sign in with a passkey</button>' + _RECOVERY_FORM
            + '<p class="note">No passkey on this device yet? On the computer your Host runs on, '
            'open Settings, Remote access, and choose Add a phone.</p>'
            '<span id="remote-sw" hidden></span>')
    return _page("Sign in to C-Series", "", body)


@router.get("/login", include_in_schema=False)
async def login_page(request: Request = None):
    if _is_remote(request):
        return _remote_login_page()
    if owner_login.needs_setup():
        return RedirectResponse("/setup", status_code=303)
    passkeys, _codes = await _passkeys_and_codes()
    st = cloud_machine.state()
    if cloud_machine.keeps_vault(st):
        # A cloud machine: its unlock page signs the owner in with one of the
        # disk's keys (owner_login.box_sign_in); the Host's own passkey, or a
        # password from before, as well when there is one.
        body = _unlock_link(st, "Sign in with this machine&#39;s passkey")
        if passkeys:
            body += ('<details><summary>Sign in with this Host&#39;s own passkey</summary>'
                     '<button id="passkey" type="button">Sign in with a passkey</button>'
                     + _form_html("/api/owner/recovery/login",
                                  '<label for="code">Recovery code</label><input id="code" name="code" '
                                  'autocomplete="one-time-code" required>',
                                  "Sign in with the code") + '</details>')
        elif await owner_login.has_password():
            body += ('<details><summary>Sign in with your password</summary>'
                     + _form_html("/api/owner/login",
                                  _EMAIL + '<label for="password">Password</label><input id="password" '
                                  'name="password" type="password" autocomplete="current-password" required>',
                                  "Sign in") + '</details>')
        return _page("Sign in to C-Series", '<p class="note">On this machine&#39;s unlock page, confirm '
                     'with your passkey or your recovery key, and you come back here signed in.</p>', body)
    if passkeys:
        body = ('<button id="passkey" type="button">Sign in with a passkey</button>'
                '<details><summary>Sign in with a recovery code instead</summary>'
                + _form_html("/api/owner/recovery/login",
                             '<label for="code">Recovery code</label><input id="code" name="code" '
                             'autocomplete="one-time-code" required>',
                             "Sign in with the code")
                + '<p class="note">Each recovery code works once.</p></details>')
    else:
        body = _form_html("/api/owner/login",
                          _EMAIL + '<label for="password">Password</label><input id="password" '
                          'name="password" type="password" autocomplete="current-password" required>',
                          "Sign in")
    return _page("Sign in to C-Series", "", body)


@router.get("/passkey", include_in_schema=False)
async def passkey_page(request: Request):
    """The owner's passkeys: add one (walked to after setup or a password
    sign-in on an https Host), confirm with one before pairing a machine, and
    make new recovery codes. Signed-in owners only, on the desktop's name."""
    if request_host(request.scope) != HOME.host:
        return _no_store(JSONResponse({"error": "not the desktop"}, status_code=404))
    email, _token = await live_session(request.scope)
    if not email:
        return _no_store(RedirectResponse("/setup" if owner_login.needs_setup() else "/login",
                                          status_code=303))
    passkeys, codes = await _passkeys_and_codes()
    codes_html = ('<div id="codes" hidden><p>Your recovery codes. Each one signs you in once, '
                  'if you no longer have a passkey. Keep them somewhere safe: this Host shows them '
                  'only now.</p><ol id="code-list" class="codes"></ol></div>')
    end_others = ('<button id="end-others" type="button">Sign out every other session</button>'
                  '<p class="note">Every browser signed in to this Host but this one is signed '
                  'out.</p>')
    if not passkeys:
        intro = ('<p>A passkey signs you in with this device&#39;s own lock: a fingerprint, your '
                 'face or a PIN. Once you add one, your password no longer signs in, so a password '
                 'someone learns is not enough to get in, and every other session is signed '
                 'out.</p>')
        body = '<button id="add" type="button">Add a passkey</button>' + codes_html + end_others
    else:
        intro = (f'<p class="note">This Host has {passkeys} passkey{"" if passkeys == 1 else "s"} '
                 f'and {codes} unused recovery code{"" if codes == 1 else "s"}.</p>')
        body = ('<button id="confirm" type="button">Confirm with your passkey</button>'
                '<p class="note">Pairing a machine from outside needs this in the last five '
                'minutes.</p>'
                '<button id="add" type="button">Add another passkey</button>'
                '<button id="new-codes" type="button">Make new recovery codes</button>' + codes_html
                + await _passkey_list(passkeys) + end_others)
    if cloud_machine.state() is not None:
        body += '<p><a href="/machine">This machine: its disk, its access log, restarts</a></p>'
    if home_origin.REMOTE is not None:
        body += '<p><a href="/remote">Remote access: add a phone</a></p>'
    body += '<p><a href="/">Continue to C-Series</a></p>'
    return _page("Passkeys", intro, body)


@router.get("/machine", include_in_schema=False)
async def machine_page(request: Request):
    """The machine this Host runs on (server/cloud_machine.py): its disk's
    state, every action Privatae or its provider took on it, its unlock
    record, and Restart when security updates need one. Signed-in owners
    only, on the desktop's name."""
    if request_host(request.scope) != HOME.host:
        return _no_store(JSONResponse({"error": "not the desktop"}, status_code=404))
    email, _token = await live_session(request.scope)
    if not email:
        return _no_store(RedirectResponse("/setup" if owner_login.needs_setup() else "/login",
                                          status_code=303))
    st = cloud_machine.state()
    if st is None:
        return _page("This machine", '<p class="note">This Host does not run on a machine of '
                     'C-Series Cloud.</p>', '<p><a href="/">Continue to C-Series</a></p>')
    vault = st.get("vault") or {}
    log = st.get("access_log") or {}
    body = []
    if vault.get("state") == "absent":
        body.append('<p class="note">This machine keeps no locked disk: it was built before '
                    'machines had one.</p>')
    else:
        url = cloud_machine.unlock_url(st)
        body.append('<p>Its disk is locked to keys only you hold. After every restart it waits '
                    'for you to unlock it' + (f' at <a href="{html.escape(url, quote=True)}">'
                                              f'{html.escape(url)}</a>' if url else '') + '.</p>')
    if (st.get("patches") or {}).get("reboot_required"):
        body.append('<p>Security updates need a restart to finish.</p>'
                    '<button id="restart" type="button">Restart now</button>'
                    '<p class="note">The machine comes back locked: unlock it when it does.</p>')
    if log.get("mismatch"):
        body.append('<p id="mismatch" role="alert"><strong>The access log Privatae sent '
                    'no longer matches the copy on this machine.</strong> A row was removed or '
                    'changed. The copy below is the one this machine kept.</p>')
    rows = list(reversed(cloud_machine.access_log()))
    items = []
    for r in rows:
        who = cloud_machine.actor_label(r)
        items.append(f'<li>{html.escape(str(r.get("at", ""))[:19].replace("T", " "))} UTC · '
                     f'{html.escape(who)}<br>{html.escape(str(r.get("summary", "")))}</li>')
    body.append('<h2>Access log</h2>' + ('<ul class="keys">' + "".join(items) + '</ul>' if items else
                '<p class="note">Nothing yet: Privatae has taken no action on this machine.</p>'))
    rec = list(reversed(cloud_machine.unlock_record()))[:20]
    if rec:
        lines = [f'<li>{html.escape(str(r.get("at", "")).replace("T", " "))} · '
                 f'{"opened" if r.get("opened") else "refused"} · {html.escape(str(r.get("with", "")))}'
                 f' · from {html.escape(str(r.get("from", "")))}</li>' for r in rec]
        body.append('<h2>Unlocks</h2><ul class="keys">' + "".join(lines) + '</ul>')
    body.append('<p><a href="/">Continue to C-Series</a></p>')
    return _page("This machine", "", "".join(body))


@router.post("/api/owner/machine/restart", include_in_schema=False)
async def machine_restart(request: Request):
    if request_host(request.scope) != HOME.host:
        return JSONResponse({"error": "not the desktop"}, status_code=404)
    email, _token = await live_session(request.scope)
    if not email:
        return JSONResponse({"error": "Sign in first."}, status_code=401)
    try:
        secs = await cloud_machine.restart()
    except cloud_machine.RestartError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    logger.warning("owner asked the machine to restart")
    return {"ok": True, "in_seconds": secs}


def _day(when) -> str:
    return when.strftime("%Y-%m-%d") if hasattr(when, "strftime") else "unknown"


async def _passkey_list(count: int) -> str:
    """The passkeys, each with a Remove button while there is more than one."""
    rows = await owner_passkeys.list_passkeys(owner_login._pool())
    items = []
    for r in rows:
        used = f'last used {_day(r["last_used_at"])}' if r["last_used_at"] else "not used yet"
        button = (f'<button type="button" data-remove="{html.escape(r["id"], quote=True)}">Remove'
                  '</button>' if count > 1 else "")
        items.append(f'<li>{html.escape(r["label"] or "passkey")} · added {_day(r["created_at"])} · '
                     f'{used}<br>{button}</li>')
    note = ('<p class="note">Removing a passkey also signs out every other session. The only '
            'passkey stays until you add another.</p>')
    return '<h2>Your passkeys</h2><ul class="keys">' + "".join(items) + "</ul>" + note


def _after_password() -> str:
    """Where a password sign-in (or setup) goes next: on an https Host, to
    add a passkey, since a password alone is what a Host the internet
    reaches should not rely on; else the desktop."""
    return "/passkey" if owner_login.secure_cookie() else "/"


@router.get("/api/owner/status", include_in_schema=False)
async def owner_status(request: Request):
    signed_in = bool((await live_session(request.scope))[0])
    return {"setup_needed": owner_login.needs_setup(), "signed_in": signed_in}


@router.post("/api/owner/setup", include_in_schema=False)
async def owner_setup(request: Request):
    if _is_remote(request):
        return JSONResponse({"error": "Set up your Host on the computer it runs on."}, status_code=403)
    body = await _form(request)
    try:
        await owner_login.create_owner(_text(body, "code"), _text(body, "email"),
                                       _text(body, "password"), client=_client(request))
        token = await owner_login.sign_in(_text(body, "email"), _text(body, "password"),
                                          _client(request), request_host(request.scope))
    except owner_login.LoginError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    owner_passkeys.note_password_sign_in(token)
    return _with_cookie(JSONResponse({"ok": True, "next": _after_password()}), token, request)


@router.post("/api/owner/login", include_in_schema=False)
async def owner_sign_in(request: Request):
    if _is_remote(request):
        # owner_login.sign_in refuses it too.
        return JSONResponse({"error": "On this address the Host signs in with a passkey or a "
                                      "recovery code only."}, status_code=403)
    body = await _form(request)
    try:
        token = await owner_login.sign_in(_text(body, "email"), _text(body, "password"),
                                          _client(request), request_host(request.scope))
    except owner_login.LoginError as exc:
        return JSONResponse({"error": str(exc)}, status_code=401)
    # Opens adding the first passkey for a few minutes (owner_passkeys._may_add).
    owner_passkeys.note_password_sign_in(token)
    return _with_cookie(JSONResponse({"ok": True, "next": _after_password()}), token, request)


# ── the owner from the machine's unlock page (owner_login.claim_owner) ────

_TICKET_FORM_MAX = 4096


def _box_refusal(msg: str, status: int):
    st = cloud_machine.state()
    resp = _page("Sign in to C-Series", f"<p>{html.escape(msg)}</p>",
                 _unlock_link(st, "Back to this machine&#39;s unlock page"))
    resp.status_code = status
    return _no_store(resp)


async def _from_unlock_page(request: Request, take):
    """A ticket the unlock page posted, as a form, from its own origin, and
    only to the Host's own name: `take` checks it and makes the session,
    which goes back with the desktop. A refusal is a page (this is a
    navigation) with the way back to the unlock page."""
    st = cloud_machine.state()
    unlock = cloud_machine.unlock_url(st)
    if not cloud_machine.keeps_vault(st) or not unlock or request_host(request.scope) != HOME.host:
        return _box_refusal("This Host is signed in to here only from its machine's unlock page.", 404)
    if request.headers.get("origin") != unlock.rstrip("/"):
        return _box_refusal("That sign-in did not come from this machine's unlock page.", 403)
    raw = await request.body()
    form = parse_qs(raw.decode("utf-8", "replace"), max_num_fields=4) if len(raw) <= _TICKET_FORM_MAX else {}
    ticket = (form.get("ticket") or [""])[0]
    try:
        token = await take(ticket, _client(request), request_host(request.scope))
    except owner_login.LoginError as exc:
        return _box_refusal(str(exc), 403)
    owner_passkeys.note_box_sign_in(token)
    return _no_store(_with_cookie(RedirectResponse("/", status_code=303), token, request))


@router.post("/api/owner/claim", include_in_schema=False)
async def owner_claim(request: Request):
    return await _from_unlock_page(request, owner_login.claim_owner)


@router.post("/api/owner/box-sign-in", include_in_schema=False)
async def owner_box_sign_in(request: Request):
    return await _from_unlock_page(request, owner_login.box_sign_in)


# ── passkeys and recovery codes (server/owner_passkeys.py) ─────────────────

def _refusal(exc: owner_login.LoginError, status: int) -> JSONResponse:
    body = {"error": str(exc)}
    if isinstance(exc, owner_passkeys.StepUpNeeded):
        body["step_up"] = True
    return _no_store(JSONResponse(body, status_code=status))


async def _signed_in(request: Request) -> str | None:
    """The live session's token on this request's host, or None."""
    email, token = await live_session(request.scope)
    return token if email else None


_SIGN_IN_FIRST = {"error": "Sign in first."}


@router.post("/api/owner/passkey/login/options", include_in_schema=False)
async def passkey_login_options(request: Request):
    try:
        return _no_store(JSONResponse(await owner_passkeys.login_options(
            _client(request), request_host(request.scope))))
    except owner_login.LoginError as exc:
        return _refusal(exc, 400)


@router.post("/api/owner/passkey/login/verify", include_in_schema=False)
async def passkey_login_verify(request: Request):
    body = await _form(request)
    try:
        token = await owner_passkeys.passkey_sign_in(body.get("ceremony"), body.get("credential"),
                                                     _client(request), request_host(request.scope))
    except owner_login.LoginError as exc:
        return _refusal(exc, 401)
    return _with_cookie(JSONResponse({"ok": True, "next": "/"}), token, request)


@router.post("/api/owner/recovery/login", include_in_schema=False)
async def recovery_login(request: Request):
    body = await _form(request)
    try:
        token = await owner_passkeys.recovery_sign_in(_text(body, "code"), _client(request),
                                                      request_host(request.scope))
    except owner_login.LoginError as exc:
        return _refusal(exc, 401)
    # Someone signing in with a code has usually lost their passkey. The
    # passkeys page is the desktop's; on the remote address, the Host.
    nxt = "/" if _is_remote(request) else "/passkey"
    return _with_cookie(JSONResponse({"ok": True, "next": nxt}), token, request)


@router.post("/api/owner/passkey/register/options", include_in_schema=False)
async def passkey_register_options(request: Request):
    token = await _signed_in(request)
    if not token:
        return _no_store(JSONResponse(_SIGN_IN_FIRST, status_code=401))
    try:
        return _no_store(JSONResponse(await owner_passkeys.registration_options(
            token, request_host(request.scope))))
    except owner_login.LoginError as exc:
        return _refusal(exc, 403)


@router.post("/api/owner/passkey/register/verify", include_in_schema=False)
async def passkey_register_verify(request: Request):
    token = await _signed_in(request)
    if not token:
        return _no_store(JSONResponse(_SIGN_IN_FIRST, status_code=401))
    body = await _form(request)
    try:
        got = await owner_passkeys.register(token, request_host(request.scope), body.get("ceremony"),
                                            body.get("credential"), body.get("label"))
    except owner_login.LoginError as exc:
        return _refusal(exc, 400)
    return _no_store(JSONResponse({"ok": True, "recovery_codes": got["recovery_codes"],
                                   "sessions_ended": got["sessions_ended"]}))


@router.post("/api/owner/passkey/stepup/options", include_in_schema=False)
async def passkey_step_up_options(request: Request):
    token = await _signed_in(request)
    if not token:
        return _no_store(JSONResponse(_SIGN_IN_FIRST, status_code=401))
    try:
        return _no_store(JSONResponse(await owner_passkeys.step_up_options(
            token, request_host(request.scope))))
    except owner_login.LoginError as exc:
        return _refusal(exc, 400)


@router.post("/api/owner/passkey/stepup/verify", include_in_schema=False)
async def passkey_step_up_verify(request: Request):
    token = await _signed_in(request)
    if not token:
        return _no_store(JSONResponse(_SIGN_IN_FIRST, status_code=401))
    body = await _form(request)
    try:
        await owner_passkeys.step_up(token, body.get("ceremony"), body.get("credential"),
                                     _client(request), request_host(request.scope))
    except owner_login.LoginError as exc:
        return _refusal(exc, 401)
    return _no_store(JSONResponse({"ok": True, "fresh_for_s": int(owner_passkeys.STEP_UP_S)}))


@router.post("/api/owner/passkey/remove", include_in_schema=False)
async def passkey_remove(request: Request):
    token = await _signed_in(request)
    if not token:
        return _no_store(JSONResponse(_SIGN_IN_FIRST, status_code=401))
    body = await _form(request)
    try:
        ended = await owner_passkeys.remove_passkey(token, request_host(request.scope),
                                                    body.get("id"))
    except owner_login.LoginError as exc:
        return _refusal(exc, 403)
    return _no_store(JSONResponse({"ok": True, "sessions_ended": ended}))


@router.post("/api/owner/sessions/end-others", include_in_schema=False)
async def sessions_end_others(request: Request):
    token = await _signed_in(request)
    if not token:
        return _no_store(JSONResponse(_SIGN_IN_FIRST, status_code=401))
    try:
        ended = await owner_passkeys.end_other_sessions(token, request_host(request.scope))
    except owner_login.LoginError as exc:
        return _refusal(exc, 403)
    return _no_store(JSONResponse({"ok": True, "sessions_ended": ended}))


@router.post("/api/owner/recovery/new", include_in_schema=False)
async def recovery_new(request: Request):
    token = await _signed_in(request)
    if not token:
        return _no_store(JSONResponse(_SIGN_IN_FIRST, status_code=401))
    try:
        codes = await owner_passkeys.replace_recovery_codes(token, request_host(request.scope))
    except owner_login.LoginError as exc:
        return _refusal(exc, 403)
    return _no_store(JSONResponse({"ok": True, "recovery_codes": codes}))


@router.post("/api/owner/logout", include_in_schema=False)
async def owner_sign_out(request: Request):
    await owner_login.sign_out(_cookie(request.scope))
    resp = JSONResponse({"ok": True})
    # The same attributes it was set with: a browser ignores a clearing
    # Set-Cookie for a __Host- name that lacks Secure or path /.
    resp.delete_cookie(owner_login.cookie_name(request_host(request.scope)), path="/",
                       secure=_browser_used_https(request),
                       httponly=True, samesite="strict")
    return resp


# ── app-origin sessions (server/owner_login.py start_join / finish_join) ────
#
# An origin-isolated app is framed from its own host, which the desktop's
# host-only cookie never reaches. Its first page load comes to the session
# gate without a session and is sent to the desktop's /__host/join, which
# (with the desktop's session) makes a one-time code for that app host and
# sends the frame on to the app host's /__host/session, which redeems it for
# a session cookie of the app host's own and returns the frame to its page.
# Every hop is the same site, so the SameSite=Strict cookie rides each one.

def _safe_rd(rd: str | None) -> str:
    """A path on the same origin to return to, or "/"."""
    rd = rd or "/"
    if not rd.startswith("/") or rd.startswith("//") or "\\" in rd or any(ord(c) < 0x20 for c in rd):
        return "/"
    return rd


def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@router.get("/__host/renew", include_in_schema=False)
async def host_renew(request: Request):
    """204 while this origin's session is live, else 401 (app-frame-init.js)."""
    email, _token = await live_session(request.scope)
    return _no_store(Response(status_code=204 if email else 401))


@router.get(DOOR_PATH, include_in_schema=False)
async def host_door(request: Request):
    """The front door's question about a request it holds: the session
    gate's own answer (owner_session.door_decision)."""
    status, location = await door_decision(request.scope)
    resp = Response(status_code=status)
    if location:
        resp.headers["Location"] = location
    return _no_store(resp)


@router.websocket(DOOR_PATH)
async def host_door_ws(websocket: WebSocket):
    """The same question for a WebSocket handshake. The front door asks it
    with a handshake of its own (measured on Caddy 2.6.2), which the server
    routes here; it is answered with a plain HTTP status and no socket is
    ever opened."""
    status, _location = await door_decision(websocket.scope)
    try:
        await websocket.send_denial_response(_no_store(Response(status_code=status)))
    except RuntimeError:
        # A server without the denial-response extension: a refusal either way.
        await websocket.close(code=1008)


@router.get("/__host/join", include_in_schema=False)
async def host_join(request: Request, to: str = "", rd: str = "/"):
    """On the desktop, with its session: send the frame to app origin `to`
    with a one-time code for a session there."""
    home = home_origin.desktop_origin(request_host(request.scope))
    if home is None:
        return _no_store(JSONResponse({"error": "not the desktop"}, status_code=404))
    _email, token = await live_session(request.scope)
    if not token:
        return _no_store(JSONResponse({"error": "sign in first"}, status_code=401))
    target = urlsplit(to)
    try:
        port = target.port
    except ValueError:
        port = -1
    host = (target.hostname or "").lower()
    # An app origin of THIS address: the remote desktop's session never makes
    # one for the desktop's own address, nor the other way round.
    if target.scheme != home.scheme or port != home.port or not own_app_origin(host) \
            or not host.endswith(home.zone) or home.origin_of(host) != to.rstrip("/"):
        return _no_store(JSONResponse({"error": "not an app origin of this Host"}, status_code=400))
    code = owner_login.start_join(token, host)
    return _no_store(RedirectResponse(
        home.origin_of(host) + "/__host/session?" + urlencode({"code": code, "rd": _safe_rd(rd)}),
        status_code=303))


@router.get("/__host/session", include_in_schema=False)
async def host_session(request: Request, code: str = "", rd: str = "/"):
    """On an app origin: redeem a join code for this origin's own session,
    or, without a usable one, go and get one from the desktop. Anywhere else
    (the desktop: session-keeper.js's Continue after a session ends): back to
    `rd` while signed in, else to the sign-in page."""
    host = request_host(request.scope)
    rd = _safe_rd(rd)
    if not own_app_origin(host):
        email, _token = await live_session(request.scope)
        target = rd if email else ("/setup" if owner_login.needs_setup() and not _is_remote(request)
                                   else "/login")
        return _no_store(RedirectResponse(target, status_code=303))
    token = await owner_login.finish_join(code, host) if code else None
    if not token:
        return _no_store(RedirectResponse(join_url(host, rd), status_code=303))
    return _no_store(_with_cookie(RedirectResponse(rd, status_code=303), token, request))
