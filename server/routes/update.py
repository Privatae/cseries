"""
Update routes — Settings app's view into update state.

Uses the tunnel (not REST to the platform) for actual communication. This
route just surfaces container-local state. The Settings app calls:

  GET  /api/update/check       — ask the relay to query pc_version_query;
                                 return the latest-version info it saw.
  POST /api/update/request     — record a user-initiated update; writes
                                 /app/ceigas/update-request.json so the
                                 running container's pc-updater notices,
                                 and emits a pc_update_signal through
                                 the tunnel for the platform audit log.
  GET  /api/update/manifest    — read /app/ceigas/update-request.json
                                 (what the user asked for, still pending).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import JSONResponse
from server.applications.middleware import requires_permission
from pydantic import BaseModel

logger = logging.getLogger("privatae.pc.routes.update")

router = APIRouter(prefix="/api/update", tags=["update"])

# Strong refs to in-flight background roll dispatches so the event loop
# doesn't GC them after apply_update returns (the roll outlives the HTTP
# request by design — see apply_update).
_ROLL_TASKS: set = set()

UPDATE_DIR = Path("/app/ceigas")
UPDATE_REQUEST_PATH = UPDATE_DIR / "update-request.json"
VERSION_CACHE_PATH = UPDATE_DIR / "version-cache.json"
VERSION_HISTORY_PATH = UPDATE_DIR / "version-history.jsonl"
BUNDLE_DIR = UPDATE_DIR / "update-bundle"


class UpdateRequest(BaseModel):
    action: str  # "update" | "rollback"
    to_version: str
    from_version: Optional[str] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_dir() -> None:
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)


def _append_history(entry: dict) -> None:
    _ensure_dir()
    with VERSION_HISTORY_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _reconcile_applied_request() -> None:
    """Close the loop on a roll that succeeded.

    apply_update writes update-request.json {status: applying} and dispatches
    the roll on a background task; the roll kills THIS pod, so the task's
    success branch (`update_applied` history row) never runs — measured
    on a live Host: the pod came back on the new version with the request
    file still saying "applying" and History showing only the earlier
    failures. Nobody else reconciles that file. So on the first read of
    History/manifest after boot: if the request's to_version is what we are
    now running, record it as completed and clear the request. A request
    whose target we are NOT running is left alone — either it is genuinely
    in flight or a failure row already says why.
    """
    if not UPDATE_REQUEST_PATH.exists():
        return
    try:
        req = json.loads(UPDATE_REQUEST_PATH.read_text())
    except Exception:
        return
    local_tag = _read_local_tag()
    if not local_tag or (req.get("to_version") or "") != local_tag:
        return
    if req.get("status") not in ("applying", "pending_user_action"):
        return
    _append_history({
        "action": req.get("action") or "update",
        "from_version": req.get("from_version"),
        "to_version": local_tag,
        "requested_at": req.get("requested_at"),
        "applied_at": _now(),
        "status": "completed",
    })
    try:
        UPDATE_REQUEST_PATH.unlink()
    except OSError:
        pass


@router.get("/check", dependencies=[Depends(requires_permission("read:update_state"))])
async def check_updates():
    """Ask the governance bridge what version is currently published for
    this container's platform. The legacy WSS relay path is retired —
    we use the same bridge_client the rest of the container uses.

    Falls back to the on-disk cache if the bridge isn't reachable so the
    Settings app still shows the last-known state.
    """
    try:
        from server.main import get_bridge_client
        bridge = get_bridge_client()
        if bridge is None or not getattr(bridge, "connected", False):
            raise RuntimeError("bridge not connected")

        platform = os.environ.get("PRIVATAE_PC_PLATFORM", "linux")
        # raise_on_infra: a transport failure surfaces as BridgeUnavailable
        # ("bridge unavailable on pc.version_query: timeout") instead of the
        # ambiguous "returned no data", so the cache-fallback response's
        # `error` field tells the truth about WHY it's stale.
        data = await bridge.request("pc.version_query", {"platform": platform},
                                    raise_on_infra=True)
        if data is None:
            raise RuntimeError("bridge returned no data")

        info = dict(data)
        info["checked_at"] = _now()
        _ensure_dir()
        VERSION_CACHE_PATH.write_text(json.dumps(info))
        return info
    except HTTPException:
        raise
    except Exception as e:
        logger.error("update check failed: %s", e)
        if VERSION_CACHE_PATH.exists():
            cached = json.loads(VERSION_CACHE_PATH.read_text())
            cached["stale"] = True
            cached["error"] = str(e)
            return cached
        raise HTTPException(status_code=500, detail=str(e))


def _emit_local(signal_type: str, metadata: dict, content: str = "") -> None:
    try:
        from server.engine import rules as rules_engine
        rules_engine.emit_local_signal(signal_type, metadata, content=content)
    except Exception as e:
        logger.debug("emit failed: %s", e)


@router.post("/request", dependencies=[Depends(requires_permission("write:update_request"))])
async def request_update(req: UpdateRequest):
    """Record a user-initiated update or rollback.

    Side effects:
    - Writes /app/ceigas/update-request.json (what user asked for)
    - Appends version-history.jsonl (audit trail)
    - Emits pc_update_signal through the tunnel (platform audit)

    The actual image swap happens on the host when the user runs
    update.ps1/rollback.ps1. Container can't restart itself.
    """
    if req.action not in ("update", "rollback"):
        raise HTTPException(status_code=400, detail="invalid action")

    _ensure_dir()
    entry = {
        "id": str(uuid.uuid4()),
        "action": req.action,
        "from_version": req.from_version,
        "to_version": req.to_version,
        "requested_at": _now(),
        "status": "pending_user_action",
    }
    UPDATE_REQUEST_PATH.write_text(json.dumps(entry, indent=2))
    _append_history(entry)
    _emit_local("pc_update_requested", {
        "action": req.action,
        "from_version": req.from_version,
        "to_version": req.to_version,
    }, content=f"{req.action} to {req.to_version}")

    return entry


@router.get("/manifest", dependencies=[Depends(requires_permission("read:update_state"))])
async def get_manifest():
    """What's the currently pending update request, if any."""
    _reconcile_applied_request()
    if not UPDATE_REQUEST_PATH.exists():
        return {"pending": False}
    try:
        data = json.loads(UPDATE_REQUEST_PATH.read_text())
        data["pending"] = True
        return data
    except Exception:
        return {"pending": False}


@router.delete("/request", dependencies=[Depends(requires_permission("write:update_request"))])
async def clear_update_request():
    """User cancels a pending update (hasn't run the script yet)."""
    if UPDATE_REQUEST_PATH.exists():
        UPDATE_REQUEST_PATH.unlink()
    return {"cleared": True}


@router.get("/history", dependencies=[Depends(requires_permission("read:update_state"))])
async def get_history(limit: int = 50):
    """Version history for this container. Newest first."""
    _reconcile_applied_request()
    if not VERSION_HISTORY_PATH.exists():
        return {"history": [], "count": 0}
    rows = []
    with VERSION_HISTORY_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    rows = rows[-max(1, min(limit, 500)):][::-1]
    return {"history": rows, "count": len(rows)}


# ════════════════════════════════════════════════════════════════════
# Bundle refresh — tunnel-mTLS fetch of a fresh compose bundle.
#
# No FDA. The user's relay cert already proves who they are; the tunnel
# already has mTLS. pc_bundle_refresh asks the platform bridge to stream a
# fresh compose.yml + .env + update/rollback scripts + README as
# pc_bundle_chunk frames (see container_network/ceigas_bridge.py
# _stream_pc_bundle + _build_pc_bundle). We write files to
# /app/ceigas/update-bundle/. Settings app shows user the path + the
# update.ps1/.sh command to run.
# ════════════════════════════════════════════════════════════════════


@router.post("/apply", dependencies=[Depends(requires_permission("write:update_request"))])
async def apply_update():
    """Apply the currently published update by asking the bridge to
    recreate this container at the new tag. Returns after the recreate
    completes and the new container is healthy — the WS will already
    have flapped by then, so the dashboard must paint the boot screen
    BEFORE calling this endpoint so users don't see a blank disconnect.

    Idempotent in spirit: if the published tag matches the running tag,
    the underlying `docker compose up -d` is a no-op (no recreate).
    """
    try:
        from server.main import get_bridge_client
        bridge = get_bridge_client()
        if bridge is None or not getattr(bridge, "connected", False):
            raise HTTPException(status_code=503, detail="bridge not connected")

        platform = os.environ.get("PRIVATAE_PC_PLATFORM", "linux")
        version = await bridge.request("pc.version_query", {"platform": platform})
        if not version or not version.get("available"):
            raise HTTPException(409, "no published image available for this platform")

        target_tag = version.get("tag")
        entity_id = int(os.environ.get("ENTITY_ID", "0"))
        local_tag = _read_local_tag()

        # Already on the target — no roll, no boot screen. The Settings app
        # keys off note == 'spec already at target' to show "Already on
        # latest" without reloading.
        if target_tag and local_tag and target_tag == local_tag:
            return {"status": "noop", "note": "spec already at target", "tag": target_tag}

        _ensure_dir()
        UPDATE_REQUEST_PATH.write_text(json.dumps({
            "action": "update",
            "from_version": local_tag,
            "to_version": target_tag,
            "requested_at": _now(),
            "status": "applying",
        }, indent=2))

        # pc.update_apply patches THIS pod's Entity CR and rolls it — i.e.
        # it kills the very container serving this request. If we awaited the
        # full rollout here, our own connection would die mid-response and the
        # proxy would return 502 (even though the roll succeeds — the "502 but
        # it updated anyway" symptom). Instead dispatch the roll on a detached
        # background task and return 202 immediately, while the pod is still
        # alive to answer. The dashboard keeps the boot screen up and reloads
        # onto the new version once the host is back; the operator completes
        # the roll independently of this request.
        async def _dispatch_roll() -> None:
            try:
                envelope = await bridge.request_full("pc.update_apply", {
                    "entity_id": entity_id,
                    "target_tag": target_tag,
                })
                if not envelope.get("ok"):
                    _append_history({
                        "action": "update_failed",
                        "to_version": target_tag,
                        "failed_at": _now(),
                        "error": envelope.get("error") or "bridge returned no data",
                    })
                else:
                    _append_history({
                        "action": "update_applied",
                        "to_version": target_tag,
                        "applied_at": _now(),
                        "result": envelope.get("data") or {},
                    })
            except Exception as exc:  # the pod usually dies before this returns
                logger.info("background roll dispatch ended: %s", exc)

        task = asyncio.create_task(_dispatch_roll())
        _ROLL_TASKS.add(task)
        task.add_done_callback(_ROLL_TASKS.discard)
        # Let the task put the request on the wire before we return (and well
        # before the roll can take this pod down — kopf reconcile is seconds).
        await asyncio.sleep(0.3)

        return JSONResponse(status_code=202, content={
            "status": "applying",
            "tag": target_tag,
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error("apply update failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


def _read_local_tag() -> str:
    try:
        info = json.loads(Path("/app/system/build-info.json").read_text())
        return info.get("tag", "")
    except Exception:
        return ""


@router.post("/fetch-bundle", dependencies=[Depends(requires_permission("write:update_request"))])
async def fetch_bundle():
    """Request that the host operator apply the published update.

    For server-hosted C-Series containers (the default deployment today),
    there's nothing for the container to download — applying an update
    means the host runs `docker pull` and recreates the container at
    the new tag, preserving volumes and env. The container can't
    restart itself.

    This endpoint records the user's intent via the governance bridge
    so the operator (and the provisioner watcher, eventually) sees the
    request. Returns a manifest the Settings app renders as "Update
    requested — your host will apply v$tag shortly."
    """
    # Pull current published version from the bridge so we don't trust
    # the cache for the apply step.
    try:
        from server.main import get_bridge_client
        bridge = get_bridge_client()
        if bridge is None or not getattr(bridge, "connected", False):
            raise HTTPException(status_code=503, detail="bridge not connected")

        platform = os.environ.get("PRIVATAE_PC_PLATFORM", "linux")
        version = await bridge.request("pc.version_query", {"platform": platform})
        if not version or not version.get("available"):
            raise HTTPException(
                status_code=409,
                detail="no published image available for this platform",
            )

        # Record the user's request both locally (for audit) and on the
        # bridge (so the provisioner watcher can pick it up).
        entity_id = int(os.environ.get("ENTITY_ID", "0"))
        request_payload = {
            "entity_id": entity_id,
            "platform": platform,
            "target_tag": version.get("tag"),
            "target_image": version.get("image"),
            "target_digest": version.get("digest"),
            "requested_at": _now(),
        }

        _ensure_dir()
        UPDATE_REQUEST_PATH.write_text(json.dumps(request_payload, indent=2))
        _append_history({"action": "update_requested", **request_payload})

        # Best-effort: tell the bridge so the operator sees it. The op
        # doesn't need to exist yet on the bridge side — we'll wire the
        # provisioner watcher next. Until then, the local record + the
        # operator-facing inbox is the source of truth.
        try:
            await bridge.request("pc.update_request", request_payload)
        except Exception as e:
            logger.debug("bridge pc.update_request best-effort failed: %s", e)

        _emit_local("pc_update_requested", request_payload, content=f"requested {version.get('tag')}")
        logger.info(
            "update requested: entity=%d target=%s",
            entity_id, version.get("tag"),
        )

        return {
            "kind": "container_deployment",
            "status": "requested",
            "requested_at": request_payload["requested_at"],
            "target_tag": request_payload["target_tag"],
            "target_image": request_payload["target_image"],
            "target_digest": request_payload["target_digest"],
            "platform": platform,
            "message": (
                f"Update to {request_payload['target_tag']} requested. Your host "
                "operator will recreate the container with the new image. "
                "Volumes, certs, and environment are preserved. No download is "
                "needed on your side."
            ),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("update request failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/bundle-manifest", dependencies=[Depends(requires_permission("read:update_state"))])
async def bundle_manifest():
    """Return what's currently staged in /app/ceigas/update-bundle/."""
    if not BUNDLE_DIR.exists():
        return {"staged": False}
    m = BUNDLE_DIR / "manifest.json"
    if not m.exists():
        return {"staged": False}
    try:
        data = json.loads(m.read_text())
        data["staged"] = True
        return data
    except Exception:
        return {"staged": False}


@router.get("/bundle-file/{filename}", dependencies=[Depends(requires_permission("read:update_state"))])
async def bundle_file(filename: str):
    """Download a single file from the staged bundle."""
    from starlette.responses import FileResponse

    if "/" in filename or ".." in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="invalid filename")
    path = BUNDLE_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path=str(path), filename=filename, media_type="application/octet-stream")


@router.get("/bundle-zip", dependencies=[Depends(requires_permission("read:update_state"))])
async def bundle_zip():
    """Stream the whole staged bundle as a zip for one-click download."""
    import io
    import zipfile
    from starlette.responses import StreamingResponse

    if not BUNDLE_DIR.exists():
        raise HTTPException(status_code=404, detail="no bundle staged")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(BUNDLE_DIR.iterdir()):
            if p.is_file():
                zf.write(p, arcname=p.name)
    buf.seek(0)

    tag = "update"
    try:
        manifest = json.loads((BUNDLE_DIR / "manifest.json").read_text())
        tag = manifest.get("tag") or "update"
    except Exception:
        pass
    filename = f"cseries-{tag}.zip"

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/bundle", dependencies=[Depends(requires_permission("write:update_request"))])
async def clear_bundle():
    """Remove all staged bundle files."""
    if BUNDLE_DIR.exists():
        for p in BUNDLE_DIR.iterdir():
            try:
                p.unlink()
            except Exception:
                pass
    return {"cleared": True}
