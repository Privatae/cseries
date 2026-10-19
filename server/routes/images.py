"""GET /api/images/{id} — serve an image this Host generated and kept inline.

Only images a provider returned as `b64_json` land here (image_engine
_store_inline_image, migrations/v169_generated_images.sql); an image that came
back as a provider link keeps its link.

NO API TOKEN, deliberately and for the same reason as the wallpaper
(appearance.serve_custom_image): the consumer is an <img> tag or a clicked link
in the user's own dashboard, and neither can attach one. What stands in front of
it is the same thing that stands in front of every Host page — the zone's
Authentik proxy at the edge — plus an id that is 128 random bits.

WHAT IT WILL SERVE is decided by the bytes when the image was stored (raster
formats only, sniffed, never the provider's claim), and re-checked here against
the same allowlist, so a row written by anything else still cannot make this
route hand a browser a document. `nosniff` is already set on every response
(main.py security_headers); the CSP sandbox is added here so that even a browser
that ignored it would get an inert page.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from server import brain_service

router = APIRouter(prefix="/api/images", tags=["images"])

# "gimg_" + secrets.token_urlsafe(16): 22 chars of the url-safe alphabet.
# Refused before the lookup so nothing but a well-formed id reaches the query.
_ID_RE = re.compile(r"^gimg_[A-Za-z0-9_-]{22}$")
_SERVABLE = {"image/png", "image/jpeg", "image/gif", "image/webp"}


@router.get("/{image_id}")
async def serve_generated_image(image_id: str) -> Response:
    if not _ID_RE.match(image_id or ""):
        raise HTTPException(404, "no such image")
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "host storage unavailable")
    row = await pool.fetchrow(
        "SELECT content_type, bytes FROM generated_images "
        "WHERE id = $1 AND entity_id = $2",
        image_id, brain_service._entity_id)
    if row is None or row["content_type"] not in _SERVABLE:
        raise HTTPException(404, "no such image")
    return Response(
        content=bytes(row["bytes"]),
        media_type=row["content_type"],
        headers={
            # Immutable: an id is never reused and its bytes never change.
            "Cache-Control": "private, max-age=31536000, immutable",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )
