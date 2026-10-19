"""server/routes/mcp_oauth.py — the person's sign-in to an MCP server.

The flow is server/mcp_oauth.py; these are its three doors.

  POST /api/mcp/oauth/{name}/start     the Sign in button on the server's card
                                       in the Network app: the authorization
                                       URL its window opens. 409 with
                                       needs_client when the authorization
                                       server lets no client register itself,
                                       so the card asks for one.
  POST /api/mcp/oauth/{name}/sign-out  forget the server's tokens.
  GET  /api/mcp/oauth/callback         the authorization server's redirect. A
                                       top-level browser navigation with no app
                                       token and, on the loopback redirect, no
                                       session (route_policy 'oauth-callback';
                                       localhost_only.OAUTH_CALLBACK_PATHS):
                                       it acts only on a pending sign-in this
                                       Host made, found by its state.

Starting and ending a sign-in are the person's decisions, the scope the
Network app approves egress rules with (resolve:decision).
"""
from __future__ import annotations

import html
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from server import brain_service
from server.applications.middleware import requires_permission

log = logging.getLogger("privatae.routes.mcp_oauth")

router = APIRouter(prefix="/api/mcp/oauth", tags=["mcp"])
_DECIDE = Depends(requires_permission("resolve:decision"))


def _pool():
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "the Host's database is not ready yet")
    return pool


async def _connection(name: str) -> dict:
    from server import mcp_connections as MC
    try:
        n = MC._check_name(name)
    except MC.McpConnectionError as e:
        raise HTTPException(400, str(e)) from None
    row = await MC.live_row(_pool(), brain_service._entity_id, n)
    if row is None:
        raise HTTPException(404, f"no MCP server named {n!r} is connected")
    return row


@router.post("/{name}/start", dependencies=[_DECIDE])
async def start(name: str, request: Request):
    from server import mcp_oauth
    body: dict = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            got = await request.json()
            body = got if isinstance(got, dict) else {}
        except ValueError:
            raise HTTPException(400, "the body is not JSON") from None
    row = await _connection(name)
    try:
        return await mcp_oauth.begin(_pool(), brain_service._entity_id, row,
                                     client_id=body.get("client_id"),
                                     client_secret=body.get("client_secret"))
    except mcp_oauth.NeedsClient as e:
        from urllib.parse import urlsplit
        return JSONResponse(status_code=409, content={
            "needs_client": True, "detail": str(e), "redirect_uri": e.redirect_uri,
            "authorization_server": urlsplit(e.issuer).hostname})
    except mcp_oauth.OAuthError as e:
        raise HTTPException(400, str(e)) from None


@router.post("/{name}/sign-out", dependencies=[_DECIDE])
async def sign_out(name: str):
    from server import mcp_oauth
    row = await _connection(name)
    if await mcp_oauth.load(_pool(), row) is None:
        raise HTTPException(404, f"{row['name']} has no sign-in")
    await mcp_oauth.sign_out(_pool(), brain_service._entity_id, row)
    return {"server": row["name"], "signed_out": True}


@router.get("/callback")
async def callback(request: Request):
    from server import mcp_oauth
    params = {k: v for k, v in request.query_params.items()
              if k in ("code", "state", "iss", "error", "error_description")}
    try:
        result = await mcp_oauth.finish(_pool(), brain_service._entity_id, params)
    except HTTPException:
        result = {"ok": False, "error": "not_ready", "detail": "the Host is still starting; try again"}
    except Exception:  # noqa: BLE001 - the page says it failed; the log has why
        log.exception("MCP sign-in callback failed")
        result = {"ok": False, "error": "callback_failed", "detail": "the sign-in could not be finished"}
    return HTMLResponse(_page(result), status_code=200 if result.get("ok") else 400, headers={
        "Cache-Control": "no-store",
        # The URL carried the code; nothing on this page may send it on.
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": ("default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
                                    "form-action 'none'; frame-ancestors 'none'"),
    })


def _page(result: dict) -> str:
    """The page the sign-in tab lands on: what happened and a link back to
    C-Series. No script: the Network app opened this tab with rel=noopener (so
    the authorization server's page could not navigate the app), and it learns
    the outcome by reading the server's card again."""
    from server.home_origin import HOME
    ok = bool(result.get("ok"))
    server = result.get("server")
    if ok:
        title = f"Signed in to {server}"
        hint = "Your Host can use it now. You can close this tab."
    else:
        title = f"The sign-in to {server} did not finish" if server else "The sign-in did not finish"
        hint = str(result.get("detail") or "")
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>C-Series · sign-in</title>
<style>
 body {{font:14px ui-sans-serif,system-ui; background:#0d0d12; color:#e9e5d9; display:flex;
        align-items:center; justify-content:center; height:100vh; margin:0;}}
 .box {{padding:20px 28px; border:1px solid #1f1f25; border-radius:6px; background:#131319;
        max-width:420px; text-align:center;}}
 .sys {{font-size:11px; letter-spacing:0.12em; text-transform:uppercase; color:#9a9587; margin-bottom:10px;}}
 .msg {{font-size:15px;}}
 .hint {{font-size:12px; color:#7a766a; margin-top:12px;}}
 a {{color:#c9b98a;}}
</style></head>
<body><div class="box">
 <div class="sys">MCP server</div>
 <div class="msg">{html.escape(title)}</div>
 <div class="hint">{html.escape(hint)}</div>
 <div class="hint"><a href="{html.escape(HOME.origin)}/">Back to C-Series</a></div>
</div>
</body></html>"""
