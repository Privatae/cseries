"""Recorder — "Create agent": watch the user do a task once in the live
viewport, and write it down as steps a machine can replay.

The person goes to a site and does the sequence once; the browser then
automates it, and the host can call that deterministic agent on a schedule or
when needed. No language model runs the replay.

What a recording IS. A list of steps addressed the way the replay engine
(sessions.py) addresses a page: by what a person sees — the role and the
visible name of the thing clicked, the label of the field typed into. Never
a pixel position, never a CSS path. The replay engine re-finds each element
from a fresh snapshot, so a restyle survives and a removed button fails
loudly at that step.

What a recording is NOT: a keylogger. The rule formcapture.py set on
2026-08-30 stands — nothing this container stores comes from the keystroke
stream. The in-page script below sends the IDENTITY of a field the user
typed into (its label, its key, whether it is a password box) and never
its contents. Field VALUES are read the way "save these entries" reads
them: from the page, once, at a commit point (the click of a button or
link, Enter in a field, a form submit) — the same moment a person would
have pressed save — and held here under the site until the recording is
stopped. Then they go to the kernel through formcapture's single-use
hand-off and the user approves each one before it is sealed. A password
is sealed as a password (typed later only by the vault, only into a real
password box); a plain entry the user may seal or keep in the agent file.

Recording is an explicit MODE with a visible indicator: the user starts it
by name and stops it. While it is off, the page script asks
`__cseries_rec_active()` before reading anything and sends nothing. There is
one recording at a time, it lives in this process only, and it expires if
never stopped.

Navigations, downloads and popups are observed from the Python side (page
events), so a click that led to a page load is written as
`wait_for_load`, an address typed in the bar as `go`, and a file the site
handed over as `expect_download`.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from control import formcapture

log = logging.getLogger("cseries-browser.recorder")

router = APIRouter(prefix="/v1/record", tags=["record"])

# A recording that is never stopped holds plaintext field values (read at
# commit points) in this process. Bound that: a person recording a task
# finishes it in minutes, not hours.
RECORD_TTL_S = int(os.environ.get("BROWSER_RECORD_TTL_S", "1800"))
MAX_STEPS = int(os.environ.get("BROWSER_RECORD_MAX_STEPS", "400"))
# A click followed by a page load within this window is "the click loaded a
# page" (wait_for_load); a load with no recent action is the user going
# somewhere (go). Measured on real sites a login POST + redirect lands well
# inside 5s; a user reading a page and then typing an address takes longer.
NAV_ATTRIBUTION_MS = 5000

_page_provider: Optional[Callable[[], Any]] = None


def set_page_provider(page: Callable[[], Any]) -> None:
    global _page_provider
    _page_provider = page


# ── the in-page script ────────────────────────────────────────────────
#
# Installed into the SHARED context as an init script (every frame, every
# page). It is inert until `__cseries_rec_active()` answers true. The label /
# role resolution is the snapshot's (sessions._SNAPSHOT_JS): nameOf never
# falls through to a field's value. keyOf is formcapture's: what the SITE
# calls the field, so the key on a `type` step is the same key the sealed
# entry is saved under.
_RECORDER_JS = r"""
(() => {
  if (window.__cseries_rec_installed) return;
  window.__cseries_rec_installed = true;

  const SEL = [
    'a[href]', 'button', 'input', 'select', 'textarea', 'summary',
    '[role="button"]', '[role="link"]', '[role="checkbox"]', '[role="radio"]',
    '[role="tab"]', '[role="menuitem"]', '[role="option"]', '[role="switch"]',
    '[role="textbox"]', '[role="combobox"]', '[contenteditable="true"]'
  ].join(',');

  const clip = (s, n) => {
    if (!s) return '';
    s = String(s).replace(/\s+/g, ' ').trim();
    return s.length > n ? s.slice(0, n) : s;
  };
  const tidy = (s) => clip(s, 120);

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

  // NEVER el.value for a field, never innerText from a field — the
  // snapshot's rule, for the same reason: the name of a password box must
  // not be the password.
  const nameOf = (el) => tidy(
    el.getAttribute('aria-label') ||
    labelText(el) ||
    el.getAttribute('placeholder') ||
    el.getAttribute('alt') ||
    el.getAttribute('title') ||
    (el.tagName === 'INPUT' && el.type === 'submit' ? el.value : '') ||
    (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' ||
     el.isContentEditable ? '' : el.innerText) ||
    ''
  );

  const roleOf = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const t = el.tagName.toLowerCase();
    if (t === 'a') return 'link';
    if (t === 'button' || t === 'summary') return 'button';
    if (t === 'select') return 'combobox';
    if (t === 'textarea') return 'textbox';
    if (t === 'input') {
      const it = (el.type || 'text').toLowerCase();
      if (it === 'checkbox') return 'checkbox';
      if (it === 'radio') return 'radio';
      if (it === 'submit' || it === 'button') return 'button';
      return 'textbox';
    }
    return 'generic';
  };

  const keyOf = (el) => {
    const ac = tidy(el.getAttribute('autocomplete'));
    if (ac && ac !== 'off' && ac !== 'on') return ac.toLowerCase();
    const n = tidy(el.getAttribute('name'));
    if (n) return n.toLowerCase();
    const i = tidy(el.getAttribute('id'));
    if (i) return i.toLowerCase();
    const l = nameOf(el);
    if (l) return l.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    return '';
  };

  const isSecret = (el) =>
    el.tagName === 'INPUT' && String(el.type || '').toLowerCase() === 'password';

  const isField = (el) => {
    const t = el.tagName;
    if (t === 'TEXTAREA' || t === 'SELECT') return true;
    if (el.isContentEditable) return true;
    if (t !== 'INPUT') return false;
    const it = String(el.type || 'text').toLowerCase();
    return !['submit', 'button', 'image', 'reset', 'hidden', 'file',
             'checkbox', 'radio'].includes(it);
  };

  // The host the fields belong to is the TOP page's, even from inside a
  // login iframe: the sealed entry is filed under the site the user is
  // signing into, and the replay's expect_host check reads the session's
  // top-level url. ancestorOrigins is Chromium's, which is the engine here.
  const topHost = () => {
    try {
      const ao = location.ancestorOrigins;
      if (ao && ao.length) return new URL(ao[ao.length - 1]).hostname;
    } catch (e) { /* fall through */ }
    return location.hostname;
  };

  const describe = (raw) => {
    let el = raw;
    try { if (el && el.closest) el = el.closest(SEL) || el; } catch (e) {}
    if (!el || !el.tagName) return null;
    const d = {role: roleOf(el), name: nameOf(el), tag: el.tagName.toLowerCase()};
    if (el.type) d.type = String(el.type).toLowerCase();
    if (isField(el)) { d.key = keyOf(el); d.secret = isSecret(el); }
    return d;
  };

  const SKIP_TYPES = new Set(['hidden', 'submit', 'button', 'image', 'reset', 'file']);
  const readFields = () => {
    const out = [];
    let els = [];
    try { els = Array.from(document.querySelectorAll('input, select, textarea')); }
    catch (e) { return out; }
    for (const el of els) {
      if (out.length >= 60) break;
      const tag = el.tagName;
      const t = String(el.type || '').toLowerCase();
      if (tag === 'INPUT' && SKIP_TYPES.has(t)) continue;
      if (tag === 'INPUT' && (t === 'checkbox' || t === 'radio')) {
        if (!el.checked) continue;
        const k = keyOf(el);
        if (!k) continue;
        out.push({key: k, label: nameOf(el), type: t, secret: false, checked: true, value: ''});
        continue;
      }
      let v = '';
      try { v = el.value === null || el.value === undefined ? '' : String(el.value); }
      catch (e) { continue; }
      if (!v) continue;
      const k = keyOf(el);
      if (!k) continue;
      out.push({key: k, label: nameOf(el),
                type: tag === 'SELECT' ? 'select' : (tag === 'TEXTAREA' ? 'textarea' : t || 'text'),
                secret: isSecret(el), checked: false, value: clip(v, 4096)});
    }
    return out;
  };

  const active = async () => {
    try { return !!(window.__cseries_rec_active && await window.__cseries_rec_active()); }
    catch (e) { return false; }
  };
  const send = (p) => {
    try { if (window.__cseries_rec) window.__cseries_rec(JSON.stringify(p)); } catch (e) {}
  };
  const base = (t) => ({t: t, host: topHost(), frame_host: location.hostname,
                        url: location.href, ms: Date.now()});

  // A commit point: the moment a person would have pressed "save these
  // entries". Field VALUES are read here and only here.
  const isCommit = (d) => d && (d.role === 'button' || d.role === 'link' ||
                                d.type === 'submit');

  document.addEventListener('click', async (e) => {
    if (!(await active())) return;
    const d = describe(e.target);
    if (!d) return;
    const p = Object.assign(base('click'), {target: d});
    if (isCommit(d)) p.fields = readFields();
    send(p);
  }, true);

  // `change` fires when a field loses focus after its value changed — one
  // event per field the user filled, carrying the field's identity only.
  document.addEventListener('change', async (e) => {
    if (!(await active())) return;
    const el = e.target;
    if (!el || !el.tagName) return;
    const d = describe(el);
    if (!d) return;
    if (el.tagName === 'SELECT') {
      let label = '';
      try { const o = el.options[el.selectedIndex]; label = tidy(o ? (o.text || o.value) : ''); }
      catch (err) {}
      send(Object.assign(base('select'), {target: d, value: label}));
      return;
    }
    if (d.role === 'checkbox' || d.role === 'radio') {
      send(Object.assign(base('click'), {target: d}));
      return;
    }
    if (isField(el)) send(Object.assign(base('type'), {target: d}));
  }, true);

  document.addEventListener('keydown', async (e) => {
    if (e.key !== 'Enter') return;
    if (!(await active())) return;
    const el = e.target;
    if (!el || !el.tagName || !isField(el)) return;
    if (el.tagName === 'TEXTAREA') return;
    const d = describe(el);
    send(Object.assign(base('press'), {key: 'Enter', target: d, fields: readFields()}));
  }, true);

  document.addEventListener('submit', async () => {
    if (!(await active())) return;
    send(Object.assign(base('submit'), {fields: readFields()}));
  }, true);
})();
"""


# ── the recording ─────────────────────────────────────────────────────

class Recording:
    def __init__(self, name: str) -> None:
        self.name = name
        self.started = time.monotonic()
        self.started_ms = int(time.time() * 1000)
        self.steps: list[dict] = []
        # host -> key -> field (with value). Values live HERE, never in steps.
        self.fields: dict[str, dict[str, dict]] = {}
        self.hosts: list[str] = []          # every host seen, first = the site
        self.last_action_ms = 0
        self.last_url = ""
        self.bar_nav_pending = False        # a /navigate from the address bar
        self.event_count = 0
        self.dropped = 0
        # Set when the TTL ended the recording. The steps are kept for the
        # person to collect; only the plaintext field values are dropped.
        self.auto_stopped = False

    def note_host(self, host: str) -> None:
        h = (host or "").strip().lower()
        if h and h not in self.hosts:
            self.hosts.append(h)

    def add(self, step: dict) -> None:
        if len(self.steps) >= MAX_STEPS:
            self.dropped += 1
            return
        self.steps.append(step)

    def merge_fields(self, host: str, fields: list) -> None:
        h = (host or "").strip().lower()
        if not h or not isinstance(fields, list) or not fields:
            return
        bucket = self.fields.setdefault(h, {})
        for f in fields:
            if not isinstance(f, dict):
                continue
            key = str(f.get("key") or "").strip().lower()
            if not key:
                continue
            if len(bucket) >= formcapture.MAX_FIELDS and key not in bucket:
                continue
            bucket[key] = {
                "key": key,
                "label": str(f.get("label") or "")[:120],
                "type": str(f.get("type") or "text")[:20],
                "secret": bool(f.get("secret")),
                "checked": bool(f.get("checked")),
                "value": str(f.get("value") or "")[:formcapture.MAX_VALUE_CHARS],
            }

    def site(self) -> str:
        return self.hosts[0] if self.hosts else ""

    def preview(self) -> dict:
        """Everything the UI needs, with secret VALUES withheld."""
        return {
            "name": self.name,
            "started_ms": self.started_ms,
            "site": self.site(),
            "hosts": list(self.hosts),
            "steps": list(self.steps),
            "step_count": len(self.steps),
            "dropped_steps": self.dropped,
            **({"auto_stopped": True,
                "values_dropped": True,
                "auto_stop_note": (
                    f"recording stopped by itself after {RECORD_TTL_S}s without "
                    f"a stop. The steps are kept; the values typed into fields "
                    f"were dropped from memory, so those fields must be "
                    f"entered again before the agent can be saved")}
               if self.auto_stopped else {}),
            "fields": [
                {"host": h, "fields": [formcapture.mask(f) for f in
                                       sorted(b.values(), key=lambda x: x["key"])]}
                for h, b in sorted(self.fields.items())
            ],
        }


_rec: Optional[Recording] = None


def is_active() -> bool:
    if _rec is None:
        return False
    if _rec.auto_stopped:
        return False
    if time.monotonic() - _rec.started > RECORD_TTL_S:
        # The TTL exists because field values sit in memory as plaintext.
        # So drop exactly those, keep the steps for stop/status to hand
        # back, and stop capturing.
        for bucket in _rec.fields.values():
            for f in bucket.values():
                f["value"] = ""
        _rec.auto_stopped = True
        log.warning("recording %s auto-stopped after %ds without stop; "
                    "%d steps kept, field values dropped",
                    _rec.name, RECORD_TTL_S, len(_rec.steps))
        return False
    return True


def current() -> Optional[Recording]:
    return _rec if is_active() else None


def _locator(target: dict) -> dict:
    """A step's address: role + visible name. `key` rides along for fields so
    the replay can pick the matching sealed entry."""
    loc = {"role": str(target.get("role") or "generic")[:24],
           "name": str(target.get("name") or "")[:120]}
    return loc


def _same_field(a: dict, b: dict) -> bool:
    return (a.get("field") == b.get("field") and a.get("key") == b.get("key"))


async def on_event(_source: Any, payload: str) -> bool:
    """The page binding. Returns True when the event was recorded."""
    rec = current()
    if rec is None:
        return False
    try:
        ev = json.loads(payload) if isinstance(payload, str) else dict(payload or {})
    except Exception:
        return False
    if not isinstance(ev, dict):
        return False
    t = str(ev.get("t") or "")
    host = str(ev.get("host") or "").lower()
    frame_host = str(ev.get("frame_host") or "").lower()
    rec.note_host(host)
    if frame_host and frame_host != host:
        rec.note_host(frame_host)
    rec.event_count += 1
    target = ev.get("target") if isinstance(ev.get("target"), dict) else {}
    now = int(ev.get("ms") or time.time() * 1000)

    if t == "click":
        rec.merge_fields(host, ev.get("fields") or [])
        rec.add({"click": _locator(target)})
        rec.last_action_ms = now
    elif t == "type":
        # `host` is the page the entry will be SEALED under (the top host at
        # the commit point is the same value) — the replay types it under
        # that host, which need not be the agent's site.
        step = {"type": {"field": str(target.get("name") or "")[:120],
                         "key": str(target.get("key") or "")[:64],
                         "secret": bool(target.get("secret")),
                         "host": host}}
        if step["type"]["secret"]:
            step = {"secret": {"field": step["type"]["field"],
                               "key": step["type"]["key"], "host": host}}
        last = rec.steps[-1] if rec.steps else {}
        lk = "secret" if "secret" in step else "type"
        if lk in last and _same_field(last[lk], step[lk]):
            return True                      # the same field, edited twice
        rec.add(step)
        rec.last_action_ms = now
    elif t == "select":
        rec.add({"select": {"field": str(target.get("name") or "")[:120],
                            "value": str(ev.get("value") or "")[:120]}})
        rec.last_action_ms = now
    elif t == "press":
        rec.merge_fields(host, ev.get("fields") or [])
        rec.add({"press": str(ev.get("key") or "Enter")[:20]})
        rec.last_action_ms = now
    elif t == "submit":
        rec.merge_fields(host, ev.get("fields") or [])
    else:
        return False
    return True


# ── page-side observations (app.py wires these on the shared context) ──

def note_bar_navigate(url: str) -> None:
    """The user typed an address: a `go`. The framenavigated that follows is
    the same event and must not become a second step."""
    rec = current()
    if rec is None:
        return
    rec.add({"go": str(url)[:2000]})
    try:
        rec.note_host(urlparse(url).hostname or "")
    except Exception:
        pass
    rec.bar_nav_pending = True
    rec.last_url = url


def on_frame_navigated(frame: Any) -> None:
    rec = current()
    if rec is None:
        return
    try:
        if frame is not None and frame.parent_frame is not None:
            return                           # main frame only
        url = str(frame.url or "")
    except Exception:
        return
    if not url or url == "about:blank":
        return
    if rec.bar_nav_pending:
        rec.bar_nav_pending = False
        rec.last_url = url
        return
    if url == rec.last_url:
        return                               # same-document / hash change
    rec.last_url = url
    try:
        rec.note_host(urlparse(url).hostname or "")
    except Exception:
        pass
    now = int(time.time() * 1000)
    if rec.last_action_ms and now - rec.last_action_ms <= NAV_ATTRIBUTION_MS:
        if not (rec.steps and "wait_for_load" in rec.steps[-1]):
            rec.add({"wait_for_load": True})
    else:
        rec.add({"go": url[:2000]})


def on_download(download: Any) -> None:
    rec = current()
    if rec is None:
        return
    try:
        name = str(download.suggested_filename or "")
    except Exception:
        name = ""
    # A dated statement filename becomes a pattern: the date will differ next
    # time, the extension will not.
    ext = os.path.splitext(name)[1].lower() if name else ""
    rec.add({"expect_download": {"match": f"*{ext}" if ext else "*",
                                 "example": name[:120]}})


def on_popup(index: int) -> None:
    rec = current()
    if rec is None:
        return
    rec.add({"switch_page": {"index": int(index)}})


# ── API ───────────────────────────────────────────────────────────────

class StartBody(BaseModel):
    name: str = Field(description="the agent's name, 2-40 chars of lowercase "
                                  "letters, digits and hyphens")


@router.post("/start")
async def start(body: StartBody) -> dict:
    global _rec
    n = (body.name or "").strip().lower()
    import re
    if not re.match(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$", n):
        raise HTTPException(400, "name must be 2-40 chars of lowercase letters, "
                                 "digits and hyphens")
    if current() is not None:
        raise HTTPException(409, f"already recording {_rec.name!r} — stop it first")
    if _rec is not None and _rec.auto_stopped:
        raise HTTPException(
            409, f"recording {_rec.name!r} stopped by itself and its steps are "
                 f"still waiting — stop it (or stop with discard) to collect them")
    _rec = Recording(n)
    page = _page_provider() if _page_provider else None
    if page is not None:
        try:
            url = str(page.url or "")
            if url and url != "about:blank":
                _rec.last_url = url
                _rec.note_host(urlparse(url).hostname or "")
                # The page already open is where the agent starts.
                _rec.add({"go": url[:2000]})
        except Exception:
            pass
    log.info("recording %s started", n)
    return {"recording": True, "name": n, "started_ms": _rec.started_ms}


@router.get("")
async def status() -> dict:
    rec = current()
    if rec is None:
        if _rec is not None and _rec.auto_stopped:
            out = _rec.preview()
            out["recording"] = False
            out["events"] = _rec.event_count
            return out
        return {"recording": False}
    out = rec.preview()
    out["recording"] = True
    out["events"] = rec.event_count
    return out


class StopBody(BaseModel):
    discard: bool = Field(default=False, description="throw the recording away")


@router.post("/stop")
async def stop(body: StopBody) -> dict:
    """End the recording. Returns the steps and a masked preview of the field
    values read at commit points, each host's values registered with
    formcapture as a single-use capture so the kernel seals them through
    the existing "save these entries" route — same preview, same approval,
    same envelope names (form:<host>:<key>), same per-field cred_type."""
    global _rec
    rec = current()
    if rec is None and _rec is not None and _rec.auto_stopped:
        rec = _rec
    if rec is None:
        raise HTTPException(409, "nothing is being recorded")
    _rec = None
    if body.discard:
        log.info("recording %s discarded (%d steps)", rec.name, len(rec.steps))
        return {"recording": False, "discarded": True, "name": rec.name}
    captures: dict[str, str] = {}
    for host, bucket in rec.fields.items():
        # An auto-stopped recording has no values left to seal.
        if not bucket or rec.auto_stopped:
            continue
        cid = formcapture.register(host=host, url=f"https://{host}/",
                                   fields=list(bucket.values()))
        captures[host] = cid
    out = rec.preview()
    out["recording"] = False
    out["captures"] = captures
    out["events"] = rec.event_count
    log.info("recording %s stopped: %d steps, %d host(s) with fields",
             rec.name, len(rec.steps), len(captures))
    return out


def _reset_for_tests() -> None:
    global _rec
    _rec = None
