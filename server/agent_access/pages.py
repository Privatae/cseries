"""The two pages a person sees: connecting an agent, and answering its request.

Every sentence on them is this Host's, written from its own records: the op
(a checked op name), the machine (one of the person's, tools.call checks it)
and the arguments (each quoted). What an agent wrote itself, its registered
name and its note on a request, never appears inside one of those sentences:
it stands in its own box, labelled as the agent's own words, escaped. Each
page is a form that posts back to itself. Where the owner has a passkey for
this address the page carries one script (PASSKEY_SCRIPT, allowed by its
nonce alone): on a yes it makes the owner's passkey check
(/api/owner/passkey/stepup, the check every owner page uses), then posts the
answer, which person.confirm accepts for owner_passkeys.STEP_UP_S. The code
from the authenticator stays behind "Use a code instead" and needs no script.
"""
from __future__ import annotations

import html
import secrets

_STYLE = ("body{font-family:system-ui,sans-serif;margin:2.5rem auto;max-width:34rem;padding:0 1rem;"
          "color:#1d1d1f;line-height:1.45}h1{font-size:1.35rem}q,.quote{background:#f3f3f5;"
          "border-radius:6px;padding:.1rem .35rem}.box{border:1px solid #d8d8dc;border-radius:8px;"
          "padding:1rem;margin:1rem 0}label{display:block;margin:.5rem 0}input[type=text]{font-size:1.1rem;"
          "padding:.4rem;width:9rem;letter-spacing:.15rem}button{font-size:1rem;padding:.5rem 1rem;"
          "margin:.4rem .4rem 0 0;border-radius:6px;border:1px solid #888;background:#fff}"
          "button.yes{background:#1d1d1f;color:#fff}.err{color:#a40000}.muted{color:#666;font-size:.9rem}")


# The pages' referrer policy: same-origin, never no-referrer. Under
# no-referrer a browser sends a form's post with `Origin: null` (Fetch: a
# request that is not cors-mode; measured, Chromium 151 and Firefox 153), and
# the localhost gate refuses a null Origin (localhost_only.origin_allowed), so
# no answer on these pages would reach its route through the Host's doors.
# same-origin sends the page's own Origin on its own post, and nothing (no
# Referer, no Origin) to any other site, the agent's redirect address included.
REFERRER_POLICY = "same-origin"


def _page(title: str, body: str, nonce: str = "") -> str:
    script = f"<script nonce=\"{e(nonce)}\">{PASSKEY_SCRIPT}</script>" if nonce else ""
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<meta name=\"referrer\" content=\"{REFERRER_POLICY}\">"
            f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
            f"<body>{body}{script}</body></html>")


def e(text) -> str:
    return html.escape("" if text is None else str(text), quote=True)


class _CodeOnly:
    code, passkey = True, False


def passkey_nonce(factors) -> str:
    """The nonce for a page's one script: only where a passkey is offered.
    The route sends the same one in the page's Content-Security-Policy."""
    return secrets.token_urlsafe(18) if factors is not None and factors.passkey else ""


_CODE_INPUT = ("<label>Code from your authenticator "
               "<input type=\"text\" name=\"code\" inputmode=\"numeric\" autocomplete=\"one-time-code\" "
               "maxlength=\"8\"></label>")


def _factor(code_hint: str, factors=None) -> str:
    """The person's factor on a yes: their passkey first, the code behind
    "Use a code instead" (person.Factors says which this Host has). The code
    is never `required`: a passkey check made on this Host in the last few
    minutes confirms the person without one (person.confirm)."""
    factors = factors or _CodeOnly
    hint = f"<p class=\"muted\">{e(code_hint)}</p>"
    if factors.passkey:
        typed = (f"<details><summary>Use a code instead</summary>{_CODE_INPUT}</details>"
                 if factors.code else "")
        return ("<p>When you choose, your device asks for your passkey: your face, fingerprint or "
                "screen lock.</p>"
                f"{typed}{hint}<p class=\"err\" data-passkey-said role=\"alert\"></p>")
    if factors.code:
        return (_CODE_INPUT + f"<p class=\"muted\">{e(code_hint)} If you confirmed with your passkey "
                "on this Host in the last few minutes, you can leave it empty.</p>")
    from server.agent_access.person import no_factor_words
    return f"<p class=\"err\">{e(no_factor_words(factors))}</p>"


def _form_open(factors) -> str:
    marked = " data-passkey" if factors is not None and factors.passkey else ""
    return f"<form method=\"post\" class=\"box\"{marked}>"


# The one script, on a page that offers a passkey. On a yes (a button marked
# data-yes) with no code typed: the owner's passkey check, as every owner page
# makes it (routes/owner.py: POST /api/owner/passkey/stepup/options, the
# device signs, POST .../stepup/verify), then the form posts the answer in
# `decision`; person.confirm finds the check fresh. Conversions are those of
# routes/owner.py's _SCRIPT (requestOptions, credentialJSON).
PASSKEY_SCRIPT = r"""
(function () {
  'use strict';
  var form = document.querySelector('form[data-passkey]');
  if (!form) return;
  var said = form.querySelector('[data-passkey-said]');
  var typed = form.querySelector('input[name=code]');
  var more = form.querySelector('details');
  if (!(window.PublicKeyCredential && navigator.credentials && window.fetch)) {
    if (more) more.open = true;
    said.textContent = more ? 'This browser cannot use a passkey. Use a code instead.'
      : 'This browser cannot use a passkey. Open this page in a browser that can.';
    return;
  }
  function b64uToBytes(s) {
    s = String(s || '');
    var bin = atob(s.replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((s.length + 3) % 4));
    var out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }
  function bytesToB64u(buf) {
    var b = new Uint8Array(buf), bin = '';
    for (var i = 0; i < b.length; i++) bin += String.fromCharCode(b[i]);
    return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }
  function optionsFromJSON(o) {
    var r = Object.assign({}, o, { challenge: b64uToBytes(o.challenge) });
    if (Array.isArray(o.allowCredentials)) {
      r.allowCredentials = o.allowCredentials.map(function (d) {
        return Object.assign({}, d, { id: b64uToBytes(d.id) });
      });
    }
    return r;
  }
  function assertionToJSON(c) {
    var r = c.response;
    var out = { id: c.id, rawId: bytesToB64u(c.rawId), type: c.type, clientExtensionResults: {},
      response: { clientDataJSON: bytesToB64u(r.clientDataJSON),
        authenticatorData: bytesToB64u(r.authenticatorData),
        signature: bytesToB64u(r.signature) } };
    if (r.userHandle) out.response.userHandle = bytesToB64u(r.userHandle);
    if (c.authenticatorAttachment) out.authenticatorAttachment = c.authenticatorAttachment;
    return out;
  }
  function words(err) {
    var alt = more ? ', or use a code instead.' : '.';
    if (err && err.name === 'NotAllowedError') {
      return 'The passkey was not used (the prompt was closed or ran out). Try again' + alt;
    }
    if (err && err.name === 'SecurityError') {
      return 'Your passkey cannot be used on this address' + (more ? '. Use a code instead.' : '.');
    }
    return (err && err.message) || ('The passkey did not work. Try again' + alt);
  }
  async function post(url, body) {
    var r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}), credentials: 'same-origin', cache: 'no-store' });
    var j = await r.json().catch(function () { return {}; });
    if (!r.ok) throw new Error(j.error || 'Your Host could not check your passkey. Try again.');
    return j;
  }
  async function approve(decision) {
    var a = await post('/api/owner/passkey/stepup/options', {});
    var cred = await navigator.credentials.get({ publicKey: optionsFromJSON(a.options) });
    await post('/api/owner/passkey/stepup/verify', { ceremony: a.ceremony, credential: assertionToJSON(cred) });
    var chosen = document.createElement('input');
    chosen.type = 'hidden'; chosen.name = 'decision'; chosen.value = decision;
    chosen.setAttribute('data-chosen', '');
    form.appendChild(chosen);
    if (typed) typed.value = '';
    form.submit();
  }
  var busy = false;
  form.addEventListener('submit', function (ev) {
    // A page still here after its post (a navigation stopped, or the page
    // brought back from the back-forward cache) still holds the answer that
    // post carried: gone first, so a post names one decision only
    // (oauth._form refuses a name given twice).
    var left = form.querySelectorAll('input[data-chosen]');
    for (var i = 0; i < left.length; i++) left[i].remove();
    var b = ev.submitter;
    if (!b || !b.hasAttribute('data-yes') || (typed && typed.value.trim())) return;
    ev.preventDefault();
    if (busy) return;
    busy = true;
    said.textContent = '';
    approve(b.value).catch(function (err) { said.textContent = words(err); })
      .then(function () { busy = false; });
  });
})();
"""


def message(title: str, text: str, *, error: bool = False) -> str:
    cls = " class=\"err\"" if error else ""
    return _page(title, f"<h1>{e(title)}</h1><p{cls}>{e(text)}</p>")


def agent_words(rows: list[tuple[str, str]]) -> str:
    """What the agent wrote itself, apart from the Host's sentences: one box,
    labelled as the agent's own words, each value quoted and escaped."""
    items = "".join(f"<dt>{e(label)}</dt><dd><q>{e(text)}</q></dd>" for label, text in rows if text)
    if not items:
        return ""
    return ("<div class=\"box agent-words\"><p class=\"muted\">In the agent's own words. Your Host "
            "has not checked them, and they are not your Host's.</p>"
            f"<dl>{items}</dl></div>")


# What the 'read' choice lets an agent do without asking, said exactly
# (tools.READ_GRANT_OPS and READ_GRANT_PREFIXES): nothing more is in it.
READ_WORDS = ("Without asking you, let it list your machines and run these on them: uptime; the "
              "health checks (disks, network, services, kernel, open ports, firewall, "
              "Kubernetes, alerts, metrics and system logs); what is installed, the folder "
              "names and the open windows' titles; and the lists of models, backups and your "
              "Host's ops with their status.")
NOT_READ_WORDS = ("Anything else asks you first, every time or once for good: reading your files, "
                  "your context store or anything else of yours, and every change.")


def connect_page(*, client_name: str, redirect_host: str, form_token: str,
                 error: str = "", read_checked: bool = True, factors=None, nonce: str = "") -> str:
    err = f"<p class=\"err\">{e(error)}</p>" if error else ""
    checked = " checked" if read_checked else ""
    body = (
        "<h1>Connect an agent to your Host?</h1>"
        "<p>An agent asks to connect to your Host. When you approve, your Host sends its "
        f"sign-in to the address the agent registered, at <b>{e(redirect_host)}</b>.</p>"
        f"{agent_words([('What it calls itself', client_name)])}"
        f"{_form_open(factors)}"
        f"<input type=\"hidden\" name=\"form\" value=\"{e(form_token)}\">"
        f"<label><input type=\"checkbox\" name=\"read\" value=\"1\"{checked}> {e(READ_WORDS)}</label>"
        f"<p class=\"muted\">{e(NOT_READ_WORDS)} You can disconnect it in Settings, under "
        "Connected agents.</p>"
        f"{_factor('Your Host asks for it so that nobody but you can connect an agent.', factors)}"
        f"{err}"
        "<button class=\"yes\" name=\"decision\" value=\"approve\" data-yes>Connect</button>"
        "<button name=\"decision\" value=\"deny\" formnovalidate>Don't connect</button>"
        "</form>")
    return _page("Connect an agent", body, nonce)


def grant_ask_page(*, client_name: str, server: str, op: str, args: list, note: str,
                   form_token: str, error: str = "", reads: bool = False,
                   factors=None, nonce: str = "") -> str:
    """`reads`: the op is one the box classes as a read that the 'read'
    choice does not cover (the person's files, their context store): the page
    says it reads, never that it changes. `factors` (person.Factors): what
    the yes takes; `nonce`: the page's script nonce (passkey_nonce)."""
    err = f"<p class=\"err\">{e(error)}</p>" if error else ""
    args_html = ("<p>With these arguments: " + ", ".join(f"<q>{e(a)}</q>" for a in args) + "</p>"
                 if args else "")
    head = "An agent asks to read from your machine" if reads else "An agent asks to make a change"
    what = ("It would read what this returns from the machine, and the agent would receive it."
            if reads else "")
    body = (
        f"<h1>{e(head)}</h1>"
        f"<p>A connected agent asks your Host to run <b>{e(op)}</b> on <b>{e(server)}</b>.</p>"
        f"{args_html}"
        + (f"<p>{e(what)}</p>" if what else "")
        + f"{agent_words([('Its name', client_name), ('Its reason', note)])}"
        "<p class=\"muted\">Nothing has run. If you allow it, your Host runs it through the same "
        "checks as its own work, and the machine still applies its own rules.</p>"
        f"{_form_open(factors)}"
        f"<input type=\"hidden\" name=\"form\" value=\"{e(form_token)}\">"
        f"{_factor('Needed to allow it, either way.', factors)}"
        f"{err}"
        "<button class=\"yes\" name=\"decision\" value=\"once\" data-yes>Just this once</button>"
        "<button class=\"yes\" name=\"decision\" value=\"always\" data-yes>Always for this agent</button>"
        "<button name=\"decision\" value=\"refused\" formnovalidate>No</button>"
        "</form>"
        f"<p class=\"muted\">\"Always\" lets this agent run {e(op)} on {e(server)} again without "
        "asking. You can take it back in Settings, under Connected agents.</p>")
    return _page("An agent's request", body, nonce)


def machine_ask_page(*, client_name: str, server: str, op: str, where: str,
                     form_token: str = "", card_words: str = "", error: str = "") -> str:
    """A request the machine itself holds for the person's approval: the card
    the Host put up for it (card_words, its own sitrep; the person's click
    sends the ask on to their approval device, as the chat's pop-up does), or
    the device or console the machine named (where)."""
    err = f"<p class=\"err\">{e(error)}</p>" if error else ""
    if card_words:
        form = ("<form method=\"post\" class=\"box\">"
                f"<p>{e(card_words)}</p>"
                f"<input type=\"hidden\" name=\"form\" value=\"{e(form_token)}\">{err}"
                "<button class=\"yes\" name=\"decision\" value=\"once\">Do it once</button>"
                "<button name=\"decision\" value=\"no\">Don't run it</button>"
                "</form>")
    else:
        form = f"<div class=\"box\"><p>{e(where)}</p></div>{err}"
    body = (
        "<h1>A machine is waiting for your approval</h1>"
        f"<p>A connected agent asked your Host to run <b>{e(op)}</b> on <b>{e(server)}</b>. "
        f"{e(server)} runs it only once you approve it.</p>"
        f"{agent_words([('Its name', client_name)])}{form}")
    return _page("A machine's approval", body)
