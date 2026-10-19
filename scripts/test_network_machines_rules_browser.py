#!/usr/bin/env python3
"""Gate: the Network app's machines' Hosts and per-peer rules, in a real browser
over a mock kernel.

What Circles' Bastions and Domain policy panes had moves to Network before the
cards replace Circles' rail: the other Hosts on each machine with their card,
Request connection, Message and the statuses, and the per-peer rules list.

The kernel is a mock: FastAPI on a loopback port answering the routes the app
calls with fixed rows. What is NOT mocked is who may call them. Every mocked
route is guarded by the scope its REAL route takes, read from the real routers
(server/routes/network.py, domain_policy.py, contacts.py, host_cards.py) through
the tag requires_permission() or requires_any_permission() puts on its dependency, and the window token
carries exactly the permissions the Network manifest declares. So a call the
manifest does not cover is refused here as the kernel would refuse it.

The app runs framed in a stand-in desktop page that records what the app posts
to it (Message opens Circles that way). Every scenario runs at 1440 x 900 and at
390 x 844 with touch:

  B. a machine's other Hosts, each with its status: Message for a Host you can
     message, Request connection, Request sent, Blocked, No longer announced,
     Not identified yet, a Host waiting for the anchor and one removed (no
     button); a machine too old to say, one that serves only you, one never
     heard from; a Host's name opens its card; a click in the list is not a
     click on the machine; a name from a peer is text, never markup.
  R. Request connection: one POST naming that Host; the row then reads
     "Request sent", and still does in a new window (from the Host's card).
     Each new window is a new browser context with empty storage; a "sent"
     flag planted in the first window's storage while the card is not pending
     does not reach it.
  E. refusals in words: the route's own answer, and the 403 a window without
     request:peer gets, with the button offered again.
  G. Message posts cseries:open-app for Circles with view card:entity:<id> to the
     desktop, and calls nothing.
  D. the rules: listed under the posture; Delete asks first, Cancel sends
     nothing, OK deletes that rule; a rule gone already is said in words; Add
     rule sends the peer and allow or deny.
  M. the manifest covers every route these use (the real routes' scopes).
  P. the phone: every Host row and every rule inside the screen.

Needs Playwright with Chromium, fastapi and uvicorn. Screenshots in a mkdtemp,
removed on a pass (SHOTS_DIR keeps them). ONLY=scenario_request,... runs only
those.

  python3 scripts/test_network_machines_rules_browser.py
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
for p in (str(ROOT / "brain"), str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import uvicorn  # noqa: E402
from gate_loopback import IDLE_KEEP_ALIVE_S  # noqa: E402
import yaml  # noqa: E402
from fastapi import Depends, FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

APP_DIR = ROOT / "system-apps" / "network"
CSS_DIR = ROOT / "dashboard" / "base" / "css"
JS_DIR = ROOT / "dashboard" / "base" / "js"
MANIFEST_PERMS = list(yaml.safe_load((APP_DIR / "app.yaml").read_text()).get("permissions") or [])
_OWN_SHOTS = not os.environ.get("SHOTS_DIR")
SHOTS = Path(os.environ["SHOTS_DIR"]) if not _OWN_SHOTS else Path(tempfile.mkdtemp(prefix="network-machines-"))
SHOTS.mkdir(parents=True, exist_ok=True)
ONLY = {x.strip() for x in os.environ.get("ONLY", "").split(",") if x.strip()}

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {str(detail)[:400]}" if detail and not ok else ""))
    if not ok:
        fails += 1


# ── the scopes the real kernel takes ─────────────────────────────────────────
def real_scopes() -> dict:
    """(method, path template) → the scopes its real route takes, any one of
    them enough (one, for most; request-peer takes request:peer or
    write:address_book)."""
    from server.applications.api_contract import _route_scopes
    from server.routes import contacts, domain_policy, host_cards, network
    out = {}
    for mod in (network, domain_policy, contacts, host_cards):
        for r in mod.router.routes:
            for m in getattr(r, "methods", None) or ():
                out[(m, r.path)] = tuple(_route_scopes(r))
    return out


SCOPES = real_scopes()

# The routes this gate answers, as the real kernel names them. Each one is
# guarded below by the real route's scope.
MOCKED = [
    ("GET", "/api/network/overview"),
    ("GET", "/api/network/bastions"),
    ("GET", "/api/network/bastions/directory"),
    ("GET", "/api/network/bastion-latest"),
    ("GET", "/api/network/bastions/auto-update"),
    ("GET", "/api/network/nac/mgmt/grants"),
    ("GET", "/api/network/nac/egress"),
    ("GET", "/api/network/peers"),
    ("POST", "/api/hosts/cards"),
    ("POST", "/api/contacts/request-peer"),
    ("GET", "/api/domain-policy"),
    ("PUT", "/api/domain-policy/posture"),
    ("POST", "/api/domain-policy/rules"),
    ("DELETE", "/api/domain-policy/rules/{rule_id}"),
]
# The ones this slice added to Network, which the manifest must cover.
NEW_ROUTES = [
    ("GET", "/api/network/bastions/directory"),
    ("POST", "/api/hosts/cards"),
    ("POST", "/api/contacts/request-peer"),
    ("GET", "/api/domain-policy"),
    ("POST", "/api/domain-policy/rules"),
    ("DELETE", "/api/domain-policy/rules/{rule_id}"),
]

# ── the rows ─────────────────────────────────────────────────────────────────
NOW = "2026-10-10T12:00:00+00:00"
EVIL = '<img src=x onerror="window.__owned=1">'

BASTIONS = [
    {"id": 7, "hostname": "studio-mac", "name": "studio-mac", "online": True, "last_seen_at": NOW,
     "platform": "darwin", "bastion_version": "0.40.0", "mode": "networked", "capabilities": [], "models": [],
     "anchor": {"anchor_host": "juno.zone.example.test", "anchor_handle": "juno", "verified": True}},
    {"id": 9, "hostname": "old-box", "online": True, "last_seen_at": NOW, "platform": "linux",
     "bastion_version": "0.12.0", "capabilities": [], "models": []},
    {"id": 11, "hostname": "laptop", "online": True, "last_seen_at": NOW, "platform": "linux",
     "bastion_version": "0.40.0", "mode": "local", "capabilities": [], "models": []},
    {"id": 13, "hostname": "fresh-box", "online": False, "last_seen_at": None, "capabilities": [], "models": []},
]


def host(name, host_id, *, state="active", current=True, anchor=False):
    return {"host_name": f"{name}.zone.example.test", "handle": name, "state": state,
            "since": "2026-09-01T10:00:00+00:00", "host_id": host_id, "current": current, "is_anchor": anchor}


DIRECTORY = [
    {"token_id": 7, "machine": "studio-mac", "online": True, "heard": True, "last_seen_at": NOW,
     "mode": "networked", "anchored": True, "anchor": None,
     "hosts": [host("juno", 201, anchor=True), host("wren", 202), host("kite", 203, current=False),
               host("mist", None), host("pell", 204), host("bram", 205), host("newb", 206, state="pending"),
               host("gone", 207, state="revoked"), host("evil", 208), host("flint", 209)]},
    {"token_id": 9, "machine": "old-box", "online": True, "heard": True, "last_seen_at": NOW,
     "mode": None, "anchored": None, "anchor": None, "hosts": None},
    {"token_id": 11, "machine": "laptop", "online": True, "heard": True, "last_seen_at": NOW,
     "mode": "local", "anchored": False, "anchor": None, "hosts": []},
    {"token_id": 13, "machine": "fresh-box", "online": False, "heard": False, "last_seen_at": None,
     "mode": None, "anchored": None, "anchor": None, "hosts": None},
]


def card(host_id, name, rel, *, can_message=False, user=None):
    labels = {"contact": "Contact", "pending": "Connection pending", "blocked": "Blocked",
              "shares_machine": "Shares a machine with you", "known": "Not connected"}
    return {"host_id": host_id, "number": f"Host #{host_id}", "classification": "Host", "display_name": name,
            "address": None, "relationship": rel, "relationship_label": labels[rel], "mine": False,
            "connection_label": labels[rel], "shares_machine": True, "can_message": can_message,
            "contact_number": None, "user": user, "sources": []}


CARDS = {
    201: card(201, "Juno", "contact", can_message=True, user="Ana Ruiz"),
    202: card(202, "Wren", "shares_machine"),
    203: card(203, "Kite", "shares_machine"),
    204: card(204, "Pell", "pending"),
    205: card(205, "Bram", "blocked"),
    206: card(206, "Newb", "shares_machine"),
    207: card(207, "Gone", "known"),
    208: card(208, EVIL, "shares_machine"),
    209: card(209, "Flint", "shares_machine"),
}

RULES = [
    {"id": 31, "peer": "atlas", "allow_inbox": True, "allow_dm": True, "allow_circle_invite": True,
     "note": None, "created_at": NOW},
    {"id": 32, "peer": "spam@user.example.test", "allow_inbox": False, "allow_dm": False,
     "allow_circle_invite": False, "note": "blocked from Contacts", "created_at": NOW},
    {"id": 33, "peer": "<b>bold</b>", "allow_inbox": False, "allow_dm": False, "allow_circle_invite": False,
     "note": None, "created_at": NOW},
]

STATE: dict = {}


def reset():
    STATE.clear()
    STATE.update({"calls": [], "perms": list(MANIFEST_PERMS), "cards": copy.deepcopy(CARDS),
                  "rules": copy.deepcopy(RULES), "next_rule": 100, "gone_rules": set()})


# ── the mock kernel ──────────────────────────────────────────────────────────
DESKTOP = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>html,body{margin:0;height:100%;background:#000}iframe{border:0;width:100%;height:100%;display:block}</style>
</head><body><script>
window.__posted = [];
window.addEventListener('message', (e) => {
  if (e.origin !== window.location.origin) return;
  window.__posted.push(e.data);
});
</script><iframe id="app" src="/system-apps/network/index.html?w=__W__"></iframe></body></html>"""


def build_app() -> FastAPI:
    from server.applications.middleware import requires_any_permission
    from server.applications.tokens import AppToken

    app = FastAPI()

    @app.middleware("http")
    async def window_token(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            body = None
            if request.method in ("POST", "PUT"):
                raw = await request.body()
                try:
                    body = json.loads(raw or b"null")
                except ValueError:
                    body = raw.decode("utf-8", "replace")
            STATE["calls"].append({"method": request.method, "path": request.url.path, "body": body})
            # What the kernel's token middleware leaves on the request for a
            # Network window: the manifest's own permissions, nothing more.
            request.state.app_token = AppToken(app_id="network", permissions=list(STATE["perms"]),
                                               window_id="gate", iat=0)
        return await call_next(request)

    def guard(method, path):
        return [Depends(requires_any_permission(*SCOPES[(method, path)]))]

    def route(method, path):
        return app.api_route(path, methods=[method], dependencies=guard(method, path))

    @route("GET", "/api/network/overview")
    def overview():
        return {"identity": {"handle": "ivy", "entity_id": 1}, "bridge": {"connected": True},
                "counts": {"bastions": 4, "bastions_online": 3, "unrequested_agents": 0},
                "bastions": [], "peers": [], "contacts": []}

    @route("GET", "/api/network/bastions")
    def bastions():
        return {"bastions": copy.deepcopy(BASTIONS)}

    @route("GET", "/api/network/bastions/directory")
    def directory():
        return {"machines": copy.deepcopy(DIRECTORY)}

    @route("GET", "/api/network/bastion-latest")
    def latest():
        return {"version": "0.40.0"}

    @route("GET", "/api/network/bastions/auto-update")
    def auto_update():
        return {"enabled": False, "latest": None}

    @route("GET", "/api/network/nac/mgmt/grants")
    def grants():
        return {"grants": [], "stepup_enrolled": False}

    @route("GET", "/api/network/nac/egress")
    def egress():
        return {"rules": []}

    @route("GET", "/api/network/peers")
    def peers():
        return {"contacts": [], "secured_channels": []}

    @route("POST", "/api/hosts/cards")
    async def cards(request: Request):
        ids = (await request.json()).get("ids") or []
        return {"cards": {str(i): STATE["cards"][i] for i in ids if i in STATE["cards"]}}

    @route("POST", "/api/contacts/request-peer")
    async def request_peer(request: Request):
        pid = (await request.json()).get("peer_entity_id")
        if pid == 209:
            return JSONResponse({"detail": "That Host is no longer announced on any of your machines."},
                                status_code=409)
        # The real route writes a pending contact; the Host's card then reads pending.
        if pid in STATE["cards"]:
            STATE["cards"][pid]["relationship"] = "pending"
            STATE["cards"][pid]["connection_label"] = "Connection pending"
        return {"ok": True, "sent_to": "wren", "machine": "studio-mac"}

    @route("GET", "/api/domain-policy")
    def policy():
        return {"ok": True, "posture": "contacts", "rules": copy.deepcopy(STATE["rules"])}

    @route("PUT", "/api/domain-policy/posture")
    async def posture(request: Request):
        return {"ok": True, "posture": (await request.json()).get("posture")}

    @route("POST", "/api/domain-policy/rules")
    async def add_rule(request: Request):
        b = await request.json()
        STATE["next_rule"] += 1
        row = {"id": STATE["next_rule"], "peer": b["peer"], "allow_inbox": b["allow_inbox"],
               "allow_dm": b["allow_dm"], "allow_circle_invite": b["allow_circle_invite"],
               "note": b.get("note"), "created_at": NOW}
        STATE["rules"] = [r for r in STATE["rules"] if r["peer"] != row["peer"]] + [row]
        return {"ok": True, "rule": row}

    @route("DELETE", "/api/domain-policy/rules/{rule_id}")
    def delete_rule(rule_id: int):
        if rule_id in STATE["gone_rules"] or not any(r["id"] == rule_id for r in STATE["rules"]):
            return JSONResponse({"detail": "rule not found"}, status_code=404)
        STATE["rules"] = [r for r in STATE["rules"] if r["id"] != rule_id]
        return {"ok": True, "deleted": rule_id}

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE"])
    def unmocked(rest: str):
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    @app.get("/desktop.html")
    def desktop(w: str = "1"):
        return HTMLResponse(DESKTOP.replace("__W__", w))

    @app.get("/css/{name}")
    def css(name: str):
        target = (CSS_DIR / name).resolve()
        if target.parent != CSS_DIR.resolve() or not target.is_file():
            return Response(status_code=404)
        return FileResponse(str(target), media_type="text/css")

    @app.get("/js/{name}")
    def js(name: str):
        target = (JS_DIR / name).resolve()
        if target.parent != JS_DIR.resolve() or not target.is_file():
            return Response(status_code=404)
        return FileResponse(str(target), media_type="text/javascript")

    @app.get("/system-apps/network/{rest:path}")
    def static(rest: str):
        target = (APP_DIR / (rest or "index.html")).resolve()
        if APP_DIR.resolve() not in target.parents:
            return Response(status_code=403)
        return FileResponse(str(target)) if target.is_file() else Response(status_code=404)

    return app


def serve(app) -> int:
    # Loopback on purpose: the only client is the headless Chromium this same
    # process launches on this same box.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error",
                                           timeout_keep_alive=IDLE_KEEP_ALIVE_S))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    return port


def calls(path_part: str, method: str):
    return [c for c in STATE["calls"] if path_part in c["path"] and c["method"] == method]


# ── the page ─────────────────────────────────────────────────────────────────
_OPENS = iter(range(1, 1_000_000))


def open_network(page, base):
    """A Network window inside the stand-in desktop; returns the app's frame."""
    page.goto(f"{base}/desktop.html?w={next(_OPENS)}", wait_until="domcontentloaded", timeout=15_000)
    app = page.frame_locator("#app")
    expect(app.locator(".net-rail-btn").first).to_be_visible(timeout=10_000)
    return app


def lens(app, name):
    app.locator(f'.net-rail-btn[data-lens="{name}"]').click()


# The bastions lens renders its rows, then starts these two reads without
# awaiting them (system-apps/network/app.js loadBastions: loadEgressStatus,
# loadAutoUpdate). On a busy box they reached the server after a check had
# taken its count of calls, and were counted as the click's (seen on CI, check
# G: "...without opening the machine or calling the Host").
LENS_TAIL_READS = ("/api/network/nac/egress", "/api/network/bastions/auto-update")


def open_machines(page, base):
    start = len(STATE["calls"])
    app = open_network(page, base)
    lens(app, "bastions")
    expect(app.locator('[data-machine-hosts="7"] .net-host-row')).to_have_count(10, timeout=10_000)
    deadline = time.time() + 10
    while time.time() < deadline and not all(
            any(c["method"] == "GET" and c["path"] == p for c in STATE["calls"][start:])
            for p in LENS_TAIL_READS):
        time.sleep(0.05)
    return app


def row(app, handle):
    return app.locator(f'[data-machine-hosts="7"] [data-host-row="{handle}.zone.example.test"]')


def action_text(app, handle):
    return (row(app, handle).locator(".net-host-action").text_content() or "").strip()


def posted(page):
    return page.evaluate("window.__posted")


# ── scenarios ────────────────────────────────────────────────────────────────
def scenario_machines(page, base, tag):
    app = open_machines(page, base)
    want = {"juno": "Message", "wren": "Request connection", "kite": "No longer announced",
            "mist": "Not identified yet", "pell": "Request sent", "bram": "Blocked", "newb": "",
            "gone": "", "flint": "Request connection"}
    got = {h: action_text(app, h) for h in want}
    check(f"B[{tag}] each Host on the machine ends with its status or its one button", got == want, got)
    check(f"B[{tag}] Message and Request connection are buttons; the statuses are words",
          row(app, "juno").locator("button[data-host-message]").count() == 1
          and row(app, "wren").locator("button[data-host-request]").count() == 1
          and row(app, "pell").locator("button").count() == 1   # only its name, which opens the card
          and row(app, "mist").locator("button").count() == 0)
    check(f"B[{tag}] names come from the Host's card, else the handle",
          (row(app, "juno").locator(".net-host-name").text_content() or "") == "Juno"
          and (row(app, "mist").locator(".net-host-name").text_content() or "") == "@mist")
    check(f"B[{tag}] the anchor, a Host waiting for the anchor and one removed are labelled",
          row(app, "juno").get_by_text("Anchor", exact=True).count() == 1
          and row(app, "newb").get_by_text("Waiting for the anchor", exact=True).count() == 1
          and row(app, "gone").get_by_text("Removed", exact=True).count() == 1)
    sub = row(app, "juno").locator(".net-host-sub").text_content() or ""
    check(f"B[{tag}] a row says whose Host it is and its address on the machine",
          sub.startswith("Ana Ruiz · juno.zone.example.test · on this machine since "), sub)
    check(f"B[{tag}] a name from a peer is text, never markup",
          app.locator('[data-machine-hosts="7"] img').count() == 0
          and (row(app, "evil").locator(".net-host-name").text_content() or "") == EVIL
          and page.frames[1].evaluate("window.__owned === undefined"))
    check(f"B[{tag}] a machine too old to say, one with only your Host, one never heard from",
          "too old to report which other Hosts" in (app.locator('[data-machine-hosts="9"]').text_content() or "")
          and "Only your Host is on this machine." in (app.locator('[data-machine-hosts="11"]').text_content() or "")
          and app.locator('[data-machine-hosts="13"]').count() == 0)
    # A click in the list that is not on a button is not a click on the machine.
    row(app, "mist").locator(".net-host-sub").click()
    check(f"B[{tag}] a click in the Hosts list does not open the machine", app.locator("#inspector").is_hidden())
    # The name opens the Host's card.
    row(app, "juno").locator("button.net-host-name").click()
    expect(app.locator("#inspector")).to_be_visible(timeout=5_000)
    body = app.locator("#inspector-body").text_content() or ""
    check(f"B[{tag}] the name opens the Host's card",
          (app.locator("#inspector-title").text_content() or "") == "Juno"
          and "Host #201" in body and "Ana Ruiz" in body and "Contact" in body, body[:200])
    page.screenshot(path=str(SHOTS / f"machines-{tag}.png"), full_page=True)


FAKE_SENT_KEY = "network.hostAsks"


def other_window(page, tag):
    """A new window in a NEW browser context: its own storage, nothing carried
    over from `page`. Only what the Host answers can make it say anything."""
    ctx = page.context.browser.new_context(**{"locale": "en-US", **dict(VIEWPORTS)[tag]})
    p = ctx.new_page()
    errors = []
    p.on("pageerror", lambda e: errors.append(str(e)))
    return ctx, p, errors


def scenario_request(page, base, tag):
    app = open_machines(page, base)
    # The proof that the new-window check below reads the Host and not this
    # browser: a "sent" flag planted in this window's storage, while the
    # Host's card says the request was never sent, must not reach a new
    # window. If the new window shared storage, or the app read a flag like
    # this one, it would say Request sent here.
    check(f"R[{tag}] the card is not pending before the request",
          STATE["cards"][202]["relationship"] != "pending")
    page.evaluate("([k, v]) => localStorage.setItem(k, v)",
                  [FAKE_SENT_KEY, json.dumps({"202": {"state": "sent"}})])
    check(f"R[{tag}] the planted flag is in this window's storage",
          page.evaluate("(k) => localStorage.getItem(k)", FAKE_SENT_KEY) is not None)
    ctx2, page2, errors2 = other_window(page, tag)
    try:
        app2 = open_machines(page2, base)
        check(f"R[{tag}] a new window starts with empty storage",
              page2.evaluate("(k) => localStorage.getItem(k)", FAKE_SENT_KEY) is None)
        check(f"R[{tag}] ...and with the card not pending it offers Request connection, not Request sent",
              action_text(app2, "wren") == "Request connection", action_text(app2, "wren"))
        check(f"R[{tag}] ...with no page errors", not errors2, "; ".join(errors2)[:300])
    finally:
        ctx2.close()
    page.evaluate("(k) => localStorage.removeItem(k)", FAKE_SENT_KEY)
    before = len(STATE["calls"])
    row(app, "wren").locator("button[data-host-request]").click()
    expect(row(app, "wren").locator(".net-host-action")).to_have_text("Request sent", timeout=10_000)
    sent = calls("/api/contacts/request-peer", "POST")
    check(f"R[{tag}] one request, naming that Host", [c["body"] for c in sent] == [{"peer_entity_id": 202}],
          sent)
    after = STATE["calls"][before:]
    i = next((k for k, c in enumerate(after) if c["path"] == "/api/contacts/request-peer"), -1)
    check(f"R[{tag}] the Host's card is read again after the request",
          any(c["path"] == "/api/hosts/cards" and 202 in ((c["body"] or {}).get("ids") or [])
              for c in after[i + 1:]))
    check(f"R[{tag}] the row reads Request sent, with no button left to press",
          row(app, "wren").locator("button[data-host-request]").count() == 0)
    check(f"R[{tag}] the other rows are unchanged", action_text(app, "flint") == "Request connection")
    # A new window, in a new browser context so it shares no storage with this
    # one, says the same: only the Host's card can tell it.
    ctx2, page2, errors2 = other_window(page, tag)
    try:
        app2 = open_machines(page2, base)
        check(f"R[{tag}] a new window (fresh storage) still reads Request sent",
              action_text(app2, "wren") == "Request sent", action_text(app2, "wren"))
        check(f"R[{tag}] ...with no page errors", not errors2, "; ".join(errors2)[:300])
    finally:
        ctx2.close()
    check(f"R[{tag}] ...and sends nothing more", len(calls("/api/contacts/request-peer", "POST")) == 1)


def scenario_refused(page, base, tag):
    app = open_machines(page, base)
    row(app, "flint").locator("button[data-host-request]").click()
    err = row(app, "flint").locator(".net-host-error")
    expect(err).to_be_visible(timeout=10_000)
    check(f"E[{tag}] the route's refusal is said in its words",
          err.text_content() == "That Host is no longer announced on any of your machines.", err.text_content())
    check(f"E[{tag}] ...and Request connection is offered again", action_text(app, "flint") == "Request connection")
    # A window whose token lacks request:peer is refused by the kernel.
    STATE["perms"] = [p for p in MANIFEST_PERMS if p != "request:peer"]
    app = open_machines(page, base)
    row(app, "wren").locator("button[data-host-request]").click()
    err = row(app, "wren").locator(".net-host-error")
    expect(err).to_be_visible(timeout=10_000)
    check(f"E[{tag}] without request:peer the request is refused, in words",
          "request:peer" in (err.text_content() or ""), err.text_content())
    check(f"E[{tag}] ...and nothing changed on the Host",
          STATE["cards"][202]["relationship"] == "shares_machine" and action_text(app, "wren") == "Request connection")


def scenario_message(page, base, tag):
    app = open_machines(page, base)
    before = len(STATE["calls"])
    row(app, "juno").locator("button[data-host-message]").click()
    page.wait_for_function("window.__posted.length > 0", timeout=5_000)
    msgs = posted(page)
    check(f"G[{tag}] Message asks the desktop to open Circles on that Host's card",
          msgs == [{"type": "cseries:open-app", "app_id": "circles", "view": "card:entity:201"}], msgs)
    check(f"G[{tag}] ...without opening the machine or calling the Host",
          app.locator("#inspector").is_hidden() and len(STATE["calls"]) == before,
          STATE["calls"][before:])


def open_rules(page, base):
    app = open_network(page, base)
    lens(app, "peers")
    expect(app.locator("#peers-rules .net-rule")).to_have_count(len(STATE["rules"]), timeout=10_000)
    return app


def rule_peers(app):
    return app.locator("#peers-rules .net-rule-peer").all_text_contents()


def scenario_rules(page, base, tag):
    app = open_rules(page, base)
    check(f"D[{tag}] the rules are listed under the posture",
          rule_peers(app) == ["atlas", "spam@user.example.test", "<b>bold</b>"], rule_peers(app))
    check(f"D[{tag}] each says allow or deny, with its note",
          app.locator('[data-rule="31"] .cseries-pill').text_content() == "allow"
          and app.locator('[data-rule="32"] .cseries-pill').text_content() == "deny"
          and "blocked from Contacts" in (app.locator('[data-rule="32"]').text_content() or ""))
    check(f"D[{tag}] a peer is text, never markup", app.locator("#peers-rules b").count() == 0)
    asked = []

    def answer(accept):
        def on(d):
            asked.append(d.message)
            d.accept() if accept else d.dismiss()
        return on

    handler = answer(False)
    page.on("dialog", handler)
    app.locator('[data-rule-delete="32"]').click()
    page.wait_for_timeout(300)
    page.remove_listener("dialog", handler)
    check(f"D[{tag}] Delete asks first, naming the rule",
          len(asked) == 1 and asked[0].startswith("Delete the rule for spam@user.example.test?"), asked)
    check(f"D[{tag}] Cancel sends nothing", not calls("/api/domain-policy/rules", "DELETE")
          and len(rule_peers(app)) == 3)
    handler = answer(True)
    page.on("dialog", handler)
    app.locator('[data-rule-delete="32"]').click()
    expect(app.locator("#peers-rules .net-rule")).to_have_count(2, timeout=10_000)
    page.remove_listener("dialog", handler)
    dels = calls("/api/domain-policy/rules", "DELETE")
    check(f"D[{tag}] OK deletes that rule, once", [c["path"] for c in dels] == ["/api/domain-policy/rules/32"], dels)
    check(f"D[{tag}] ...and the list is read again without it", rule_peers(app) == ["atlas", "<b>bold</b>"])
    check(f"D[{tag}] ...and says so", app.locator("#peers-rules-status").text_content()
          == "Deleted the rule for spam@user.example.test.")
    # A rule another window already deleted.
    STATE["gone_rules"].add(31)
    page.on("dialog", lambda d: d.accept())
    app.locator('[data-rule-delete="31"]').click()
    expect(app.locator("#peers-rules-status")).to_have_text("Couldn't delete the rule: rule not found",
                                                           timeout=10_000)
    check(f"D[{tag}] a rule gone already is said in words", True)
    # Add rule.
    app.locator("#peers-rule-peer").fill("orbit")
    app.locator("#peers-rule-allow").select_option("deny")
    app.locator("#peers-rule-submit").click()
    expect(app.locator('#peers-rules .net-rule-peer:text-is("orbit")')).to_have_count(1, timeout=10_000)
    adds = calls("/api/domain-policy/rules", "POST")
    check(f"D[{tag}] Add rule sends the peer and deny",
          [c["body"] for c in adds] == [{"peer": "orbit", "allow_inbox": False, "allow_dm": False,
                                         "allow_circle_invite": False}], adds)
    check(f"D[{tag}] ...lists it as deny and clears the box",
          app.locator('#peers-rules .net-rule:has-text("orbit") .cseries-pill').text_content() == "deny"
          and app.locator("#peers-rule-peer").input_value() == "")
    page.screenshot(path=str(SHOTS / f"rules-{tag}.png"), full_page=True)


def scenario_phone(page, base, tag):
    app = open_machines(page, base)
    width = page.viewport_size["width"]
    boxes = [row(app, h).bounding_box() for h in ("juno", "wren", "kite", "mist", "evil")]
    check(f"P[{tag}] every Host row is inside the screen",
          all(b and b["x"] >= 0 and b["x"] + b["width"] <= width for b in boxes), boxes)
    btn = row(app, "wren").locator("button[data-host-request]").bounding_box()
    check(f"P[{tag}] Request connection is inside the screen and big enough to tap",
          btn and btn["x"] + btn["width"] <= width and btn["height"] >= 28, btn)
    row(app, "wren").locator("button[data-host-request]").tap()
    expect(row(app, "wren").locator(".net-host-action")).to_have_text("Request sent", timeout=10_000)
    check(f"P[{tag}] a tap sends the request", len(calls("/api/contacts/request-peer", "POST")) == 1)
    page.screenshot(path=str(SHOTS / f"phone-machines-{tag}.png"), full_page=True)
    app = open_rules(page, base)
    boxes = [app.locator(f'[data-rule="{i}"]').bounding_box() for i in (31, 32, 33)]
    boxes.append(app.locator("#peers-rule-add").bounding_box())
    check(f"P[{tag}] every rule and the Add rule form are inside the screen",
          all(b and b["x"] >= 0 and b["x"] + b["width"] <= width for b in boxes), boxes)
    page.screenshot(path=str(SHOTS / f"phone-rules-{tag}.png"), full_page=True)


def scopes_check():
    missing_routes = [r for r in MOCKED if r not in SCOPES]
    check("M every route this gate answers is a real kernel route", not missing_routes, missing_routes)
    unscoped = [r for r in MOCKED if r in SCOPES and not SCOPES[r]]
    check("M ...and each takes a scope", not unscoped, unscoped)
    uncovered = {r: SCOPES.get(r) for r in NEW_ROUTES if not any(sc in MANIFEST_PERMS for sc in SCOPES.get(r, ()))}
    check("M the Network manifest declares a scope of every route this slice calls", not uncovered, uncovered)
    check("M Request connection rides request:peer: Network holds neither write:address_book "
          "(contact delete, block, accept) nor read:circles",
          "request:peer" in MANIFEST_PERMS and "write:address_book" not in MANIFEST_PERMS
          and "read:circles" not in MANIFEST_PERMS
          and SCOPES.get(("POST", "/api/contacts/request-peer")) == ("request:peer", "write:address_book"),
          (MANIFEST_PERMS, SCOPES.get(("POST", "/api/contacts/request-peer"))))
    src = (APP_DIR / "app.js").read_text()
    for method, path in NEW_ROUTES:
        stem = path.split("{", 1)[0]
        check(f"M app.js calls {method} {stem}", f"'{stem}" in src)


DESKTOP_VIEW = {"viewport": {"width": 1440, "height": 900}}
PHONE = {"viewport": {"width": 390, "height": 844}, "has_touch": True, "is_mobile": True, "device_scale_factor": 2}
VIEWPORTS = (("1440x900", DESKTOP_VIEW), ("390x844", PHONE))
SCENARIOS = (scenario_machines, scenario_request, scenario_refused, scenario_message, scenario_rules)


def main() -> int:
    if not (APP_DIR / "index.html").is_file():
        print(f"FAIL  no Network app at {APP_DIR}")
        return 1
    scopes_check()
    port = serve(build_app())
    base = f"http://127.0.0.1:{port}"
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        runs = [(fn, tag, opts) for tag, opts in VIEWPORTS for fn in SCENARIOS]
        runs.append((scenario_phone, "390x844", PHONE))
        for fn, tag, opts in runs:
            if ONLY and fn.__name__ not in ONLY:
                continue
            reset()
            ctx = browser.new_context(**{"locale": "en-US", **opts})
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
            try:
                fn(page, base, tag)
            except Exception as e:  # a scenario that throws is a failed gate, not a crash
                check(f"{fn.__name__}[{tag}]: completed", False, f"{type(e).__name__}: {e}")
                page.screenshot(path=str(SHOTS / f"{fn.__name__}-{tag}-error.png"))
            check(f"{fn.__name__}[{tag}]: no uncaught page errors", not errors, "; ".join(errors)[:300])
            ctx.close()
        browser.close()
    print("\nPASS network_machines_rules_browser" if not fails
          else f"\nFAIL network_machines_rules_browser: {fails} check(s)")
    if _OWN_SHOTS and not fails:
        shutil.rmtree(SHOTS, ignore_errors=True)
    else:
        print(f"screenshots: {SHOTS}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
