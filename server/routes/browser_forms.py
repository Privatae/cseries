"""Browser form capture — HTTP API for "save these entries".

The user fills a signup form in the live Browser app, and before
submitting clicks save. This router is what that button talks to. Two
steps, deliberately:

    POST /api/browser/forms/capture   -> read the page, PREVIEW it
    POST /api/browser/forms/save      -> seal the fields the user ticked

The split exists so consent is informed. A one-shot "capture and store"
would seal whatever the page happened to contain — including a field the
site prefilled that the user never looked at — and the user would learn
what they had stored only by going to look afterwards. Here they see the
list first, with password values withheld, and approve it field by field.

Where the values go. Each approved field becomes its OWN sealed envelope
named `form:<host>:<key>`, cred_type `form_entry`. One envelope per field
rather than one bundle per site, because that is what the existing broker
already knows how to replay: authz.use_credential(action="browser_type")
opens ONE credential and types it into ONE field, with its own audit row
and its own expect_host check. A bundle would have needed a new broker
action and a new guard, for no gain.

The plaintext path is sidecar -> this process -> vault, and it is never
returned to the caller, never logged, and never put in front of a model.
The sidecar hands each capture over exactly once (formcapture.emit).

Scope: write:vault, the same gate the Keys app holds — this writes
credentials. read:vault lists what was saved.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from server import brain_service
from server import browser_autofill as autofill
from server.applications.middleware import requires_permission
from server.operator_identity import request_email
from server.vault import api as vault
from server.vault import authz

logger = logging.getLogger("privatae.pc.browser_forms")

router = APIRouter(prefix="/api/browser/forms", tags=["browser-forms"])

# The logical-name prefix these live under in the vault. Everything the
# Keys app shows is named, so this is the string a user reads next to
# their saved entries — keep it obvious.
NAME_PREFIX = autofill.NAME_PREFIX

# Two cred_types, not one, and the split is load-bearing rather than
# cosmetic: REPLAY reads it to decide whether a value may be typed into a
# non-password box (server/browser_autofill.fill). With a single type,
# replay would have had to ask the CALLER whether the target was a
# password field, and a caller that answered wrongly — or was talked into
# it by a hostile page — would get a saved password typed into a visible
# text input. The sidecar's DOM check is the backstop; this is what stops
# us asking the wrong question in the first place.
CRED_TYPE = autofill.CRED_TYPE_PLAIN
CRED_TYPE_SECRET = autofill.CRED_TYPE_SECRET

# A capture id is interpolated into a URL path on the sidecar. Same
# reasoning as _SESSION_NAME_RX in authz.py: unvalidated, a crafted id
# normalises to a different endpoint. token_urlsafe output is exactly this
# alphabet.
_CAPTURE_ID_RX = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

# Vault logical names are used in URL paths by the Keys app
# (/api/vault/secrets/{name}) and read by humans. Keep the derived part
# conservative rather than trusting a key the PAGE chose.
_KEY_RX = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_HOST_RX = re.compile(r"^[a-z0-9][a-z0-9.-]{0,252}$")


def _actor(request: Request, action: str) -> str:
    """Audit actor — who, and from where. Mirrors server/routes/vault.py so
    a form entry and a hand-added key read the same way in vault_audit.
    request_email returns "" when the header is absent, never None."""
    email = request_email(request.headers) or "unknown"
    xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    ip = xff or (request.client.host if request.client else "unknown")
    return f"user:{email} ip:{ip} {action}"


def _entity_id() -> int:
    eid = os.environ.get("ENTITY_ID", "")
    if not eid.isdigit():
        raise HTTPException(503, "ENTITY_ID not set; vault unavailable")
    return int(eid)


def _pool():
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain_not_initialized")
    return pool


def vault_name(host: str, key: str) -> str:
    """`form:<host>:<key>` — the name the replay side asks for and the name
    the user sees in Keys."""
    return f"{NAME_PREFIX}:{host}:{key}"


# The most of one browser control answer read here: the same bound as
# browser_agents._BROWSER_JSON_MAX (a form capture, a snapshot of at most 200
# elements; a screenshot rides as base64 inside the JSON).
_FORMS_JSON_MAX = 16 * 1024 * 1024


async def _sidecar(method: str, path: str, payload: Optional[dict] = None) -> Any:
    """One call to the browser control API.

    trust_env is OFF: this is our own pod / the harness Service on the
    cluster network and must never be handed to the egress proxy — the same
    reasoning as _do_browser_type in authz.py.
    """
    url = f"{authz.browser_base_url()}{path}"
    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as s:
            async with s.request(
                    method, url, json=payload,
                    headers=authz.browser_headers()) as resp:
                status = resp.status
                # The browser runs on the harness pool and renders pages the
                # Host does not control: its answer is read under a cap on
                # decoded bytes and a value budget (server/harness_input.py),
                # never whole.
                from server import harness_input as _hin
                try:
                    buf = await _hin.read_body(resp, _FORMS_JSON_MAX, "browser forms")
                except _hin.HarnessInputRefused as e:
                    raise HTTPException(502, str(e)) from None
                try:
                    body = await _hin.parse_json(buf, "browser forms")
                except _hin.HarnessInputRefused:
                    body = {"detail": bytes(buf[:500]).decode("utf-8", "replace")}
    except HTTPException:
        raise                  # the size refusal above: a sentence, no payload in it
    except Exception as e:
        # Surface the TYPE only. The payload of an emit response is in
        # scope on the failure path and must not reach an error string.
        raise HTTPException(
            502, f"could not reach the browser: {type(e).__name__}")
    if status >= 400:
        detail = body.get("detail") if isinstance(body, dict) else ""
        if status == 503:
            raise HTTPException(
                503, "the browser is not running — open the Browser app first")
        raise HTTPException(status, str(detail) or "browser refused the request")
    return body


@router.post("/capture", dependencies=[Depends(requires_permission("write:vault"))])
async def capture() -> dict:
    """Read the page the user is looking at and return the preview.

    Nothing is stored by this call. The values stay in the sidecar under a
    single-use id until /save is called with the keys the user ticked.
    """
    body = await _sidecar("POST", "/v1/form-capture")
    if not isinstance(body, dict):
        raise HTTPException(502, "unexpected answer from the browser")
    # Echo the preview through unchanged — the sidecar already withheld
    # secret values (formcapture.mask) and this layer must not widen that.
    return {
        "capture_id": body.get("capture_id"),
        "url": body.get("url"),
        "host": body.get("host"),
        "title": body.get("title"),
        "fields": body.get("fields") or [],
        "truncated": bool(body.get("truncated")),
    }


class SaveBody(BaseModel):
    capture_id: str
    keys: list[str] = Field(
        default_factory=list,
        description="the field keys the user ticked in the preview")
    overwrite: bool = Field(
        default=True,
        description="re-saving a site's form replaces the entries under the "
                    "same names. False refuses when any name already exists, "
                    "so a second signup on the same site cannot silently "
                    "overwrite the first.")


@router.post("/save", dependencies=[Depends(requires_permission("write:vault"))])
async def save(body: SaveBody, request: Request) -> dict:
    """Seal the approved fields into the vault, one envelope per field."""
    if not _CAPTURE_ID_RX.match(body.capture_id or ""):
        raise HTTPException(400, "capture_id is not a valid capture id")
    if not body.keys:
        raise HTTPException(400, "no fields were approved — nothing to save")

    pool, eid = _pool(), _entity_id()

    # Ask for exactly what the user ticked. The sidecar drops the capture on
    # this call whether or not the sealing below succeeds, which is correct:
    # a retry must re-read the form and re-ask, not replay a stale id.
    emitted = await _sidecar(
        "POST", f"/v1/form-capture/{body.capture_id}/emit", {"keys": body.keys})
    if not isinstance(emitted, dict):
        raise HTTPException(502, "unexpected answer from the browser")

    host = str(emitted.get("host") or "").lower()
    if not _HOST_RX.match(host):
        raise HTTPException(502, "the browser reported no usable host for this page")
    fields = [f for f in (emitted.get("fields") or []) if isinstance(f, dict)]
    if not fields:
        raise HTTPException(
            409,
            "none of the approved fields were still in the capture — "
            "the page changed; fill the form and save again")

    existing = set()
    if not body.overwrite:
        rows = await vault.list_secrets(pool, entity_id=eid)
        existing = {r["logical_name"] for r in rows}

    saved: list[dict] = []
    skipped: list[dict] = []
    for f in fields:
        key = str(f.get("key") or "").lower()
        if not _KEY_RX.match(key):
            skipped.append({"key": key[:64], "reason": "unusable field name"})
            continue
        # A ticked checkbox has no value to type back — it is a consent
        # state. Record it so the replay knows to tick it, with a marker
        # value rather than an empty envelope.
        value = str(f.get("value") or "")
        if f.get("checked") and not value:
            value = "on"
        if not value:
            skipped.append({"key": key, "reason": "empty"})
            continue
        name = vault_name(host, key)
        if name in existing:
            skipped.append({"key": key, "reason": "already saved"})
            continue
        # The password/plain split is decided HERE, from what the sidecar
        # observed in the DOM, and stored on the envelope. Replay reads it
        # back rather than asking the caller — see browser_autofill.fill.
        ctype = CRED_TYPE_SECRET if f.get("secret") else CRED_TYPE
        try:
            await vault.store_secret(
                pool, entity_id=eid, name=name, cred_type=ctype,
                plaintext=value.encode("utf-8"),
                actor=_actor(request, f"browser-form-save {host}"),
            )
        except Exception as e:
            # Never let the exception text carry the value.
            logger.warning("form save: sealing %r failed: %s", name, type(e).__name__)
            skipped.append({"key": key, "reason": "could not be sealed"})
            continue
        saved.append({"key": key, "name": name, "label": f.get("label") or key,
                      "secret": bool(f.get("secret"))})

    logger.info("form save: %d sealed, %d skipped for %s", len(saved), len(skipped), host)
    return {"ok": bool(saved), "host": host, "saved": saved, "skipped": skipped}


@router.get("", dependencies=[Depends(requires_permission("read:vault"))])
async def list_saved(host: str = "") -> dict:
    """What has been saved, by site. Metadata only — never values.

    This is the surface that answers "what does it already know about me",
    which a user has to be able to ask before they can meaningfully agree
    to it filling anything in.
    """
    rows = await vault.list_secrets(_pool(), entity_id=_entity_id())
    want = (host or "").strip().lower()
    sites: dict[str, list[dict]] = {}
    for r in rows:
        name = r["logical_name"]
        if not name.startswith(f"{NAME_PREFIX}:"):
            continue
        parts = name.split(":", 2)
        if len(parts) != 3:
            continue
        _, site, key = parts
        if want and site != want:
            continue
        sites.setdefault(site, []).append({
            "key": key,
            "name": name,
            "created_at": r["created_at"].isoformat(),
            "deactivated": bool(r.get("deactivated_at")),
        })
    return {"sites": [{"host": h, "fields": sorted(f, key=lambda x: x["key"])}
                      for h, f in sorted(sites.items())]}


# ── the standing per-site grant, and the replay it authorizes ─────────
#
# The standing grant was chosen over per-run approval,
# because per-run approval does not deliver automation — it delivers a
# faster way to type, with the user present every time. The rationale and the
# guards that make unattended use defensible live in
# server/browser_autofill.py; these are just the HTTP edges.


class GrantBody(BaseModel):
    host: str = Field(description="the site, e.g. 'acme.example'")


@router.get("/grants", dependencies=[Depends(requires_permission("read:vault"))])
async def list_grants() -> dict:
    """Which sites may be autofilled unattended, and when each was last
    used. `last_used_at: null` is the signal a user prunes on."""
    return {"grants": await autofill.list_grants(_pool(), entity_id=_entity_id())}


@router.post("/grants", dependencies=[Depends(requires_permission("write:vault"))])
async def add_grant(body: GrantBody, request: Request) -> dict:
    """Allow unattended autofill for one site. Idempotent."""
    try:
        return await autofill.grant(
            _pool(), entity_id=_entity_id(), host=body.host,
            actor=_actor(request, "autofill-grant"))
    except autofill.AutofillError as e:
        raise HTTPException(400, str(e))


@router.delete("/grants/{host}",
               dependencies=[Depends(requires_permission("write:vault"))])
async def remove_grant(host: str, request: Request) -> dict:
    """Withdraw the standing grant. The saved entries are NOT deleted — the
    user can still use them by hand; what stops is unattended use."""
    revoked = await autofill.revoke(
        _pool(), entity_id=_entity_id(), host=host,
        actor=_actor(request, "autofill-revoke"))
    return {"host": host.strip().lower(), "revoked": revoked}


class FillBody(BaseModel):
    host: str = Field(description="the site being filled; checked against the "
                                  "session's LIVE url by the vault broker")
    session: str = Field(description="the browser-sidecar session name to type "
                                     "into (the automation lane, not the "
                                     "shared page the user drives)")
    mapping: list[dict] = Field(
        default_factory=list,
        description="[{ref, key}] — which saved entry goes in which element. "
                    "The caller matches these from a snapshot because only it "
                    "can see the page; the broker still checks the host and "
                    "the sidecar still checks the field type.")


@router.post("/fill", dependencies=[Depends(requires_permission("write:vault"))])
async def fill(body: FillBody, request: Request) -> dict:
    """Type the saved entries into a session. Refused without a live grant.

    Returns which refs were filled and which were skipped with why. No
    value is ever returned — `filled` names the field, never its contents.
    """
    try:
        return await autofill.fill(
            _pool(), entity_id=_entity_id(), host=body.host,
            session=body.session, mapping=body.mapping,
            actor=_actor(request, f"autofill-fill {body.host}"))
    except autofill.AutofillError as e:
        # A missing grant is the expected refusal, not a server fault: 403
        # so a caller can tell "you never allowed this" from "it broke".
        raise HTTPException(403 if "grant" in str(e) else 400, str(e))
