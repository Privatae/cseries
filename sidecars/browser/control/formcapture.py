"""Form capture — "save these entries", the user-driven half of autofill.

The problem this closes. The live viewport (viewport.py) hands the user a
real browser they type into themselves, and the vault broker
(server/vault/authz.py::_do_browser_type) can type a sealed credential
back into a field later without any model holding it. Those are the two
ends of "sign me up now, do it automatically next time" — and nothing
joined them, because NOTHING recorded what the user typed. viewport.py
dispatches every keystroke straight into Chromium and stores none of it.

Why capture is a button and not a recorder. The obvious implementation is
to watch the keystrokes already flowing through the viewport socket. That
turns this container into a keylogger — in a module whose whole posture is
that the frames go to the USER and never to a model — and it records
whether or not anyone wanted it to. So capture is an explicit act instead:
the user fills the form, and BEFORE submitting clicks "save these
entries". Only then is the page read, and only the page's current field
values are read. Nothing is recorded at any other moment. This shape was
chosen over the recorder.

What comes out is held HERE, in this process, under a one-shot id with a
short TTL — never written to disk, never returned twice. The kernel
(server/routes/browser_forms.py) previews it to the user with secrets
masked, and on confirmation calls /emit once to seal the values into the
identity-scoped vault. The values therefore travel sidecar -> kernel ->
vault and are never handed to a model, exactly like type_secret in the
other direction.

This reads the SHARED context — the page the Browser app shows and the
user drives. Named sessions (sessions.py) are the automation lane and
have their own contexts; they are not what a person types into.
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

log = logging.getLogger("cseries-browser.formcapture")

router = APIRouter(prefix="/v1/form-capture")

# The shared context's active page, provided by app.py at startup — the
# same provider viewport.py streams from, so what is captured is exactly
# what the user was looking at.
_page_provider: Optional[Callable[[], Any]] = None

# A capture is a short-lived, single-use handoff to the kernel, not
# storage. It exists only for the round trip between "show me what you
# found" and "yes, seal those" — a user who walks away leaves nothing
# behind. Deliberately short: the confirm click is seconds away, and the
# alternative is plaintext form values sitting in a process for minutes.
CAPTURE_TTL_S = int(os.environ.get("BROWSER_CAPTURE_TTL_S", "180"))

# Bound the capture so a hostile page cannot make this allocate freely.
MAX_FIELDS = int(os.environ.get("BROWSER_CAPTURE_MAX_FIELDS", "60"))
MAX_VALUE_CHARS = int(os.environ.get("BROWSER_CAPTURE_MAX_VALUE", "4096"))

_captures: dict[str, dict[str, Any]] = {}


def set_page_provider(page: Callable[[], Any]) -> None:
    global _page_provider
    _page_provider = page


def _reap() -> None:
    """Drop expired captures. Called on every entry point rather than on a
    timer: the values are plaintext, so the sweep must not depend on a task
    that could have died."""
    now = time.monotonic()
    for cid in [c for c, v in _captures.items() if now - v["created"] > CAPTURE_TTL_S]:
        _captures.pop(cid, None)


# ── the in-page read ──────────────────────────────────────────────────
#
# Field VALUES are what this returns, which is the one thing _SNAPSHOT_JS
# in sessions.py is built never to do (a snapshot goes to a model; this
# goes to the vault). The two must not be confused, so this is a separate
# script in a separate module rather than a flag on that one.
#
# Skipped on purpose:
#   - input[type=hidden]  — CSRF and session tokens. Per-visit by design,
#     so replaying one is useless, and capturing one stores a live bearer
#     for no benefit.
#   - submit/button/image/reset — labels, not data.
#   - file inputs — the value is a fake path, never the file.
#   - empty values — nothing to save, and they would bury the real ones.
_CAPTURE_JS = r"""
(limits) => {
  const {maxFields, maxValue} = limits;

  const clip = (s, n) => {
    if (s === null || s === undefined) return '';
    s = String(s);
    return s.length > n ? s.slice(0, n) : s;
  };
  const tidy = (s) => clip(String(s || '').replace(/\s+/g, ' ').trim(), 120);

  // Same label resolution as the snapshot: <label for=x> is how real
  // signup forms name their fields, and without it half of them come
  // back unlabelled and the user cannot tell what they are approving.
  const labelText = (el) => {
    try {
      if (el.labels && el.labels.length) return el.labels[0].innerText;
      const id = el.getAttribute('id');
      if (id) {
        const root = el.getRootNode();
        const l = root.querySelector('label[for="' + CSS.escape(id) + '"]');
        if (l) return l.innerText;
      }
      const anc = el.closest && el.closest('label');
      if (anc) return anc.innerText;
    } catch (e) { /* CSS.escape or closest unavailable */ }
    return '';
  };

  const labelOf = (el) => tidy(
    labelText(el) ||
    el.getAttribute('aria-label') ||
    el.getAttribute('placeholder') ||
    el.getAttribute('title') ||
    el.getAttribute('name') ||
    el.getAttribute('id') ||
    ''
  );

  // The replay key. It must survive a page redesign well enough to be
  // worth storing, so it prefers what the SITE calls the field over
  // anything we derive: autocomplete is standardised ("email",
  // "new-password", "given-name"), name/id are the form's own contract.
  const keyOf = (el) => {
    const ac = tidy(el.getAttribute('autocomplete'));
    if (ac && ac !== 'off' && ac !== 'on') return ac.toLowerCase();
    const n = tidy(el.getAttribute('name'));
    if (n) return n.toLowerCase();
    const i = tidy(el.getAttribute('id'));
    if (i) return i.toLowerCase();
    const l = labelOf(el);
    if (l) return l.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    return '';
  };

  // A REAL password box, judged by the DOM and not by an attribute the
  // page chose — the same test type_secret uses (sessions.py). A
  // <div contenteditable type="password"> answers "password" to
  // getAttribute and is not a password input; treating it as one here
  // would mask a value in the preview that the site never obscured.
  const isSecret = (el) =>
    el.tagName === 'INPUT' && String(el.type || '').toLowerCase() === 'password';

  const SKIP_TYPES = new Set([
    'hidden', 'submit', 'button', 'image', 'reset', 'file',
  ]);

  const collect = (root, out, depth) => {
    if (depth > 8) return;
    let els = [];
    try {
      els = Array.from(root.querySelectorAll('input, select, textarea'));
    } catch (e) { return; }
    for (const el of els) out.push(el);
    let all = [];
    try { all = Array.from(root.querySelectorAll('*')); } catch (e) { return; }
    for (const el of all) {
      if (el.shadowRoot) collect(el.shadowRoot, out, depth + 1);
    }
  };

  const found = [];
  collect(document, found, 0);

  const fields = [];
  let truncated = false;
  for (const el of found) {
    if (fields.length >= maxFields) { truncated = true; break; }
    const tag = el.tagName;
    const t = String(el.type || '').toLowerCase();
    if (tag === 'INPUT' && SKIP_TYPES.has(t)) continue;

    // Checkbox/radio carry state, not text — a signup form's consent
    // ticks. Record whether it was set, never a "value" to type back.
    if (tag === 'INPUT' && (t === 'checkbox' || t === 'radio')) {
      if (!el.checked) continue;
      const k = keyOf(el);
      if (!k) continue;
      fields.push({key: k, label: labelOf(el), type: t,
                   secret: false, checked: true, value: ''});
      continue;
    }

    let v = '';
    try { v = el.value === null || el.value === undefined ? '' : String(el.value); }
    catch (e) { continue; }
    if (!v) continue;

    const k = keyOf(el);
    if (!k) continue;

    fields.push({
      key: k,
      label: labelOf(el),
      type: tag === 'SELECT' ? 'select' : (tag === 'TEXTAREA' ? 'textarea' : t || 'text'),
      secret: isSecret(el),
      checked: false,
      value: clip(v, maxValue),
    });
  }

  return {
    url: location.href,
    title: document.title,
    fields: fields,
    truncated: truncated,
    considered: found.length,
  };
}
"""


def register(*, host: str, url: str, fields: list[dict]) -> str:
    """Hold fields read by another surface (the recorder, at its commit
    points) under a single-use id, exactly as /capture holds a page read.
    Same TTL, same one-shot /emit, same masked preview — the recorder's
    values reach the vault through the path this module already guards
    rather than a second one."""
    _reap()
    cid = secrets.token_urlsafe(18)
    _captures[cid] = {"created": time.monotonic(), "fields": list(fields),
                      "url": url, "host": (host or "").lower()}
    log.info("capture %s registered by recorder: %d field(s) on %s",
             cid, len(fields), host)
    return cid


def mask(field: dict) -> dict:
    """The preview shape: everything the user needs to decide, with secret
    VALUES withheld.

    Non-secret values go back in clear on purpose — the user typed them
    into a form that is on screen in front of them right now, so hiding
    them would only stop them noticing that the page put something else
    in the box. A password is different: the site rendered it as dots and
    we do not undo that just because we are about to seal it.
    """
    out = {k: field.get(k) for k in ("key", "label", "type", "secret", "checked")}
    value = field.get("value") or ""
    out["value"] = "" if field.get("secret") else value
    out["length"] = len(value)
    return out


@router.post("", response_model=None)
async def capture() -> dict:
    """Read the shared page's filled-in fields and hold them for one emit.

    Returns the PREVIEW (secrets masked). The raw values never leave this
    process until /emit, and then exactly once.
    """
    _reap()
    page = _page_provider() if _page_provider else None
    if page is None:
        raise HTTPException(status_code=503, detail="page not ready")
    try:
        result = await page.evaluate(
            _CAPTURE_JS, {"maxFields": MAX_FIELDS, "maxValue": MAX_VALUE_CHARS})
    except Exception as e:
        raise HTTPException(
            status_code=502, detail=f"could not read the page: {type(e).__name__}")

    result = result or {}
    fields = list(result.get("fields") or [])
    if not fields:
        raise HTTPException(
            status_code=409,
            detail="no filled-in fields on this page — fill the form first, "
                   "then save (nothing is recorded while you type)")

    url = str(result.get("url") or "")
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    if not host:
        raise HTTPException(
            status_code=409,
            detail=f"this page has no host to file the entries under (url={url!r})")

    cid = secrets.token_urlsafe(18)
    _captures[cid] = {"created": time.monotonic(), "fields": fields,
                      "url": url, "host": host}
    log.info("form capture %s: %d field(s) on %s", cid, len(fields), host)
    return {
        "capture_id": cid,
        "url": url,
        "host": host,
        "title": str(result.get("title") or ""),
        "fields": [mask(f) for f in fields],
        "truncated": bool(result.get("truncated")),
    }


class EmitBody(BaseModel):
    keys: list[str] = Field(
        default_factory=list,
        description="the field keys the user approved; empty means none. "
                    "A key not in the capture is ignored rather than an "
                    "error — the page may have changed under the preview.")


@router.post("/{capture_id}/emit", response_model=None)
async def emit(capture_id: str, body: EmitBody) -> dict:
    """Hand the approved values to the kernel, ONCE, then forget them.

    Single-use is the point: the capture is consumed whether or not the
    caller does anything useful with it, so a leaked id cannot be replayed
    and a retry cannot quietly re-read the form.
    """
    _reap()
    cap = _captures.pop(capture_id, None)
    if cap is None:
        raise HTTPException(
            status_code=404,
            detail="no such capture (already used, or it expired) — "
                   "click save again on the form")
    wanted = set(body.keys)
    fields = [f for f in cap["fields"] if f.get("key") in wanted]
    log.info("form capture %s emitted: %d of %d field(s) approved",
             capture_id, len(fields), len(cap["fields"]))
    return {"url": cap["url"], "host": cap["host"], "fields": fields}


def live_count() -> int:
    """Captures currently held. For /health, so an operator can see that
    plaintext is resident without being shown any of it."""
    _reap()
    return len(_captures)


def _reset_for_tests() -> None:
    _captures.clear()
