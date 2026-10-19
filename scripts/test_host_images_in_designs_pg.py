#!/usr/bin/env python3
"""Gate: the Host's own images reach its wallpaper and its designs
(richer UI and wallpaper, with no capability constrained by today's limits).

The Host's images are rows of generated_images, named /api/images/<id>: what
image_generate made and what an app such as Blender Engine rendered.

  W  artifact(op='set_wallpaper', image_url='/api/images/<id>') through the
     tool's own path wears the image as the custom wallpaper (the Settings
     upload's writer, entity_appearance), desktop and phone; another Host's
     image, a name that is no image, and a type a wallpaper cannot be are
     refused, and nothing is written.
  R  an artifact that names one of the Host's images renders with the image
     itself in it as a data: URL (artifacts.render, the lane every desktop and
     design path uses); a name that is not one of its images is left as
     written; the stored file keeps the short name.
  F  a desktop document using a render as its backdrop passes the desktop
     floor once rendered (no external reference).
  B  in headless Chromium, under the shell's own frame policy
     (dashboard/base/js/sandbox-csp.js), the rendered document paints the
     image: the <img> decodes at the image's size.

Real kernel schema (kernel_schema.py); a throwaway projects folder.

  python3 scripts/test_host_images_in_designs_pg.py
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
import re
import struct
import subprocess
import sys
import tempfile
import zlib

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT), str(ROOT / "brain"), str(ROOT / "scripts")]

EID = 1
FAILED = 0


def check(label, cond, detail=""):
    global FAILED
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"\n      {str(detail)[:600]}"))
    if not cond:
        FAILED += 1


def _png(w: int, h: int) -> bytes:
    raw = b"".join(b"\x00" + b"\x30\x80\xd0" * w for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


PNG = _png(40, 30)
GIF = b"GIF89a" + b"\x01\x00\x01\x00\x80\x00\x00" + b"\x00" * 30


async def _image(pool, eid: int, data: bytes, ctype: str) -> str:
    import secrets
    iid = "gimg_" + secrets.token_urlsafe(16)
    await pool.execute("INSERT INTO generated_images (id, entity_id, content_type, bytes, prompt, model) "
                       "VALUES ($1, $2, $3, $4, 'test', 'app:blender-engine')", iid, eid, ctype, data)
    return iid


async def checks(dsn: str, tmp: pathlib.Path):
    import asyncpg

    from mastercode.runtime import direct_ops
    from server import brain_service
    from server.applications import artifacts, desktop_contract
    from server.routes import applications as app_routes

    app_routes.PROJECTS_ROOT = tmp / "projects"
    (tmp / "projects").mkdir()
    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=6)
    # The artifact tool reads the kernel's own pool and entity, as it does in a
    # real Host (server/brain_service.py _pool, _entity_id).
    brain_service._pool, brain_service._entity_id = pool, EID
    try:
        await pool.execute("INSERT INTO domain_registry (id, name, domain_group) VALUES "
                           "('core.entity', 'core.entity', 'core') ON CONFLICT DO NOTHING")
        await pool.execute(
            "INSERT INTO entity_domain_access (entity_id, domain_id, access_level) VALUES "
            "($1, 'core.entity', 'admin') ON CONFLICT (entity_id, domain_id) DO UPDATE "
            "SET access_level = 'admin'", EID)
        other = await pool.fetchval("SELECT id FROM entity_registry WHERE id <> $1 ORDER BY id LIMIT 1", EID)
        if other is None:
            other = await pool.fetchval(
                "INSERT INTO entity_registry (display_name) VALUES ('Other') RETURNING id")
        mine = await _image(pool, EID, PNG, "image/png")
        theirs = await _image(pool, other, PNG, "image/png")
        gif = await _image(pool, EID, GIF, "image/gif")

        print("W. the wallpaper")

        async def wear(**args):
            return await direct_ops.run_direct_op("artifact", {"op": "set_wallpaper", **args},
                                                  entity_id=EID, pool=pool)
        out = await wear(image_url=f"/api/images/{mine}")
        row = await pool.fetchrow("SELECT wallpaper, image_bytes, image_content_type FROM entity_appearance "
                                  "WHERE entity_id = $1", EID)
        check("W1 set_wallpaper through the tool wears one of the Host's images as the custom wallpaper",
              out.get("status") == "ok" and row is not None and row["wallpaper"] == "custom"
              and bytes(row["image_bytes"]) == PNG and row["image_content_type"] == "image/png", (out, row))
        out = await wear(image_url=f"/api/images/{mine}", variant="mobile")
        row = await pool.fetchrow("SELECT wallpaper_mobile, image_bytes_mobile FROM entity_appearance "
                                  "WHERE entity_id = $1", EID)
        check("W2 ...and the phone wallpaper with variant='mobile'",
              out.get("status") == "ok" and row["wallpaper_mobile"] == "custom"
              and bytes(row["image_bytes_mobile"]) == PNG, out)
        await pool.execute("UPDATE entity_appearance SET wallpaper = 'ink', image_bytes = NULL "
                           "WHERE entity_id = $1", EID)
        for args, why in ((dict(image_url=f"/api/images/{theirs}"), "not one of your images"),
                          (dict(image_url="/api/images/gimg_" + "B" * 22), "not one of your images"),
                          (dict(image_url="https://example.com/x.png"), "/api/images/"),
                          (dict(image_url=f"/api/images/{gif}"), "image/gif"),
                          (dict(image_url=f"/api/images/{mine}", variant="tv"), "variant")):
            out = await wear(**args)
            check(f"W3 refused, and nothing written: {why}",
                  out.get("status") != "ok" and why in str(out.get("result"))
                  and await pool.fetchval("SELECT wallpaper FROM entity_appearance WHERE entity_id = $1",
                                          EID) == "ink", out)

        print("R. an artifact names the Host's images")
        doc = desktop_contract.template_html()
        backdrop = (f'<img id="art" alt="" src="/api/images/{mine}" '
                    'style="position:fixed;inset:0;width:100%;height:100%;object-fit:cover;z-index:-1">'
                    f'<i id="foreign" data-x="/api/images/{theirs}"></i>')
        doc = doc.replace("</body>", backdrop + "</body>", 1)
        created = await artifacts.create_artifact(pool, EID, name="render backdrop",
                                                  files=[{"path": "index.html", "content": doc}])
        aid = created["artifact_id"]
        html = await artifacts.render(pool, EID, aid)
        data_url = "data:image/png;base64,"
        check("R1 the render carries the image itself as a data: URL",
              f'src="{data_url}' in html and f"/api/images/{mine}" not in html, html[:200])
        check("R2 another Host's image is not put in (its name stays as written)",
              f"/api/images/{theirs}" in html, "missing")
        stored = (tmp / "projects" / str(aid) / "index.html").read_text()
        check("R3 the stored file keeps the short name", f"/api/images/{mine}" in stored
              and data_url not in stored)

        out = await direct_ops.run_direct_op("artifact", {
            "op": "create", "name": "two images",
            "files": [{"path": "index.html", "content":
                       f'<img src="/api/images/{mine}"><img src="/api/images/gimg_{"C" * 22}">'}]},
            entity_id=EID, pool=pool)
        miss = ((out.get("result") or {}).get("images_missing") or {}).get("names")
        check("R4 writing a name that is not one of the Host's images says so in the result, "
              "and only that name", miss == [f"/api/images/gimg_{'C' * 22}"], out)

        print("F. the desktop floor")
        report = desktop_contract.check_document(html)
        check("F1 a desktop whose backdrop is a render passes the floor, rendered",
              report["ok"] and not any("external" in e for e in report["errors"]), report["errors"])

        print("P. the pin a worn desktop shows, and a publish of it sends")
        # A worn desktop with no pin yet keeps the revision before an edit on
        # screen until the edit renders (desktop_pin.after_write, its
        # baseline). That pin is what the shell shows and what publishing the
        # worn desktop sends to the shelf (designs.publish_artifact ->
        # desktop_pin.worn_html), so it must carry the image itself: the frame
        # has no network, and another person's copy has no way to the
        # publisher's gallery. The browser is a stub that fails the edit, so
        # the baseline is what stays pinned.
        from server.applications import desktop_pin, desktop_render
        desktop_render.state_dir = lambda: tmp / ".kernel-state"

        async def _fail_check(_pool, html, **_kw):
            return {"checked": True, "ok": False, "why": "an uncaught error at line 1",
                    "failures": ["an uncaught error at line 1"]}

        async def _quiet():
            return None
        desktop_render._render_check = _fail_check
        desktop_pin._notify_shell = _quiet
        worn = await artifacts.create_artifact(pool, EID, name="worn backdrop",
                                               files=[{"path": "index.html", "content": doc}])
        wid = worn["artifact_id"]
        await pool.execute(
            "INSERT INTO entity_appearance (entity_id, wallpaper, desktop_artifact_id, desktop_entry) "
            "VALUES ($1, 'html', $2, 'index.html') ON CONFLICT (entity_id) DO UPDATE SET "
            "wallpaper = 'html', desktop_artifact_id = $2, desktop_entry = 'index.html'", EID, wid)
        await artifacts.write_files(pool, EID, wid, [{"path": "index.html",
                                                      "content": doc.replace("</body>", "<p>v2</p></body>")}],
                                    desktop_wait_s=5)
        shown = await desktop_pin.worn_html(pool, EID, wid, "index.html")
        check("P1 the pre-edit revision a worn desktop keeps on screen carries the image itself",
              f'src="{data_url}' in shown and "<p>v2</p>" not in shown, shown[:300])
        check("P2 ...and never the publisher's gallery name (what a publish of it would send)",
              f"/api/images/{mine}" not in shown, "the raw /api/images name is in the pin")

        print("B. the browser, under the shell's frame policy")
        csp = re.search(r'var CSP = ((?:"[^"]*"\s*\+?\s*)+);',
                        (ROOT / "dashboard/base/js/sandbox-csp.js").read_text())
        policy = "".join(re.findall(r'"([^"]*)"', csp.group(1))) if csp else ""
        check("B0 the shell's frame policy was read, and it allows data: images",
              "img-src data:" in policy, policy)
        sealed = re.sub(r"(<head[^>]*>)", r'\1<meta http-equiv="Content-Security-Policy" content="'
                        + policy + '">', html, count=1, flags=re.I)
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(sealed, wait_until="load")
                size = await page.evaluate("() => { const i = document.getElementById('art'); "
                                           "return i && i.complete ? [i.naturalWidth, i.naturalHeight] : null; }")
            finally:
                await browser.close()
        check("B1 the image paints in the sealed desktop frame at its own size", size == [40, 30], size)
    finally:
        await pool.close()


async def main(dsn: str) -> int:
    with tempfile.TemporaryDirectory(prefix="host-images-") as td:
        await checks(dsn, pathlib.Path(td))
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    import signal

    from kernel_schema import kernel_schema_db, throwaway_server
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
    if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("SKIP-LOUD: no test server and docker unavailable — this gate needs a real database.")
        sys.exit(2)
    with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
            kernel_schema_db(_srv, entity_id=EID) as _db:
        rc = asyncio.run(main(_db))
    print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
    sys.exit(rc)
