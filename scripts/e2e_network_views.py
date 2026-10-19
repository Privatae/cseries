#!/usr/bin/env python3
"""End-to-end exercise of the Network app's map, from where the user stands.

scripts/test_network_views.js proves the renderers are pure functions and that
their output contains "<svg". That is not a drawing. This drives a REAL browser
against the REAL files over REAL HTTP (scripts/serve_network_app_local.js) and
asserts against the LIVE DOM: the SVG actually parsed, the nodes actually
painted with non-zero geometry, the picker actually swaps the rendering, and no
console error fired on the way.

It is the check that would catch what a string assertion cannot — malformed SVG
that a browser silently drops, a node drawn off-canvas, a renderer that throws
only once a real event loop touches it.

Run (server must be up):
    node scripts/serve_network_app_local.js 8791 &
    python3 scripts/e2e_network_views.py
"""
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8791/"
failures: list[str] = []
notes: list[str] = []


def check(cond, msg):
    if cond:
        notes.append("  PASS  " + msg)
    else:
        failures.append(msg)


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(BASE, wait_until="networkidle")
        page.wait_for_selector("#topo-scene svg", timeout=10_000)

        # ── the default (logical) view actually painted ──────────────────
        nodes = page.query_selector_all("#topo-scene [data-node]")
        check(len(nodes) == 8,
              f"logical view paints every node (3 machines + 3 shared Hosts + peer + contact); got {len(nodes)}")

        # The Hosts that share a machine with you must be ON the map — this is
        # the whole point of discovery, and it lived only in the database until
        # the map drew it.
        body_text = page.eval_on_selector("#topo-scene svg", "el => el.textContent")
        for name in ("beta", "gamma"):
            check(name in body_text, f"the Host {name!r} sharing your machine is drawn on the map")

        # Nothing may be clipped by the scene box. The first cut of the shared
        # ring pushed the top peers off the canvas entirely.
        scene_box = page.locator("#topo-scene").bounding_box()
        clipped = []
        for n in nodes:
            b = n.bounding_box()
            if not b:
                continue
            if (b["y"] < scene_box["y"] or b["x"] < scene_box["x"]
                    or b["y"] + b["height"] > scene_box["y"] + scene_box["height"]
                    or b["x"] + b["width"] > scene_box["x"] + scene_box["width"]):
                clipped.append(n.get_attribute("data-node"))
        check(not clipped, f"no node is drawn outside the frame; clipped: {clipped}")

        # Geometry is real: every node group has a non-zero box inside the frame.
        offscreen = []
        for n in nodes:
            box = n.bounding_box()
            if not box or box["width"] <= 0 or box["height"] <= 0:
                offscreen.append(n.get_attribute("data-node") or "?")
            elif box["x"] < 0 or box["y"] < 0 or box["x"] > 1280:
                offscreen.append(n.get_attribute("data-node") or "?")
        check(not offscreen, f"every painted node has real on-canvas geometry; off-canvas/zero: {offscreen}")

        # The machine that has NEVER reported must be on the map, not hidden.
        check(page.query_selector('#topo-scene [data-node="8"]') is not None,
              "the never-reported machine (id 8) is drawn in the logical view")

        # ── the picker exists and is built from the registry ─────────────
        btns = page.query_selector_all("#topo-viewpick [data-view]")
        labels = [b.inner_text().strip() for b in btns]
        # Three here because this fixture serves a host-authored map. "Host
        # design" is offered ONLY when one exists — the picker is built from
        # what can actually draw, not from every registered name.
        check(labels == ["Logical", "Physical", "Host design"],
              f"the picker offers each view that can draw; got {labels}")

        # ── swap to physical and prove the DOM changed ───────────────────
        before = page.inner_html("#topo-scene")
        page.click('#topo-viewpick [data-view="physical"]')
        page.wait_for_timeout(300)
        after = page.inner_html("#topo-scene")
        check(before != after, "clicking Physical actually re-renders the scene")

        check(page.query_selector("#topo-scene svg") is not None,
              "the physical view produced a parsed <svg> in the live DOM")

        phys_nodes = page.query_selector_all("#topo-scene [data-node]")
        check(len(phys_nodes) == 5,
              f"physical view draws 3 machines + an 'also serves' line per sharing machine; got {len(phys_nodes)}")
        phys_text = page.eval_on_selector("#topo-scene svg", "el => el.textContent")
        check("also serves" in phys_text,
              "the physical view says which other Hosts a machine serves, on its row")
        check("beta" in phys_text and "gamma" in phys_text,
              f"both shared Hosts are named on the physical path: {phys_text[:160]!r}")
        check(page.query_selector('#topo-scene [data-node="8"]') is not None,
              "the never-reported machine is drawn in the physical view too")

        # The physical path's own facts must be on screen — this is the whole
        # point of the view, so assert the relay endpoint really rendered.
        scene_text = page.inner_text("#topo-scene") if page.query_selector("#topo-scene") else ""
        svg_text = page.eval_on_selector("#topo-scene svg", "el => el.textContent")
        # The endpoint must be painted WHOLE. Truncating it hides the port and
        # half the hostname — the one fact this view exists to show.
        hop_texts = page.eval_on_selector_all(
            "#topo-scene .topo-hop-label, #topo-scene .topo-hop-port",
            "els => els.map(e => e.textContent)")
        joined = "".join(hop_texts)
        check("edge-a.example.test" in joined and ":51820" in joined,
              f"the relay endpoint is painted in full (host and port); hop labels = {hop_texts}")
        check(not any("…" in t for t in hop_texts),
              f"no relay endpoint is elided; hop labels = {hop_texts}")
        check("your machine" in svg_text and "your Host" in svg_text,
              "the physical view's column headers are painted")
        check("peer host" in svg_text,
              "peers are NAMED in the physical view rather than silently dropped")

        # Every path must actually REACH the Host. The first cut of this view
        # ended each segment at a fixed x, leaving a line pointing at nothing —
        # geometry assertions on the node boxes could not see it, only the
        # rendered picture could. Pin it: the rightmost segment of every row
        # terminates on the host circle.
        host_c = page.eval_on_selector(
            "#topo-scene circle.topo-node-ring.hub",
            "el => ({cx: +el.getAttribute('cx'), cy: +el.getAttribute('cy'), r: +el.getAttribute('r')})")
        seg_ends = page.eval_on_selector_all(
            "#topo-scene .topo-path line",
            "els => els.map(e => ({x: +e.getAttribute('x2'), y: +e.getAttribute('y2')}))")
        # the converging segments are the ones ending near the host's x
        converging = [s for s in seg_ends if abs(s["x"] - host_c["cx"]) <= host_c["r"] + 6]
        check(len(converging) == 3,
              f"one segment per machine converges on the Host; got {len(converging)} of {len(seg_ends)}")
        check(all(abs(s["y"] - host_c["cy"]) <= 1 for s in converging),
              f"every converging segment lands on the Host's centre line "
              f"(host cy={host_c['cy']}, segment ends={[s['y'] for s in converging]})")

        # Geometry again, for the view that was never checked in a browser.
        bad = []
        for n in phys_nodes:
            box = n.bounding_box()
            if not box or box["width"] <= 0 or box["height"] <= 0:
                bad.append(n.get_attribute("data-node") or "?")
        check(not bad, f"every physical-view node has real geometry; zero-size: {bad}")

        # ── the choice survives a reload (the remembered preference) ─────
        page.reload(wait_until="networkidle")
        page.wait_for_selector("#topo-scene svg", timeout=10_000)
        active = page.query_selector("#topo-viewpick .net-viewbtn.active")
        check(active is not None and active.inner_text().strip() == "Physical",
              f"the chosen view survives a reload; active = {active.inner_text().strip() if active else None}")

        # ── clicking a node still opens its inspector (the swap didn't break it) ──
        page.click('#topo-scene [data-node="7"]')
        page.wait_for_timeout(400)
        body = page.inner_text("body")
        check("desktop-01" in body, "clicking a machine on the physical map opens its inspector")

        # ── the host's OWN map, in its sandboxed frame ───────────────────
        page.click('#topo-viewpick [data-view="hosted"]')
        page.wait_for_timeout(900)
        frame_el = page.query_selector("#topo-hosted-frame")
        check(frame_el is not None, "the host-authored map renders in a frame")
        if frame_el:
            sb = frame_el.get_attribute("sandbox")
            check(sb == "allow-scripts",
                  f"the frame is sandboxed to allow-scripts ONLY (got {sb!r})")
            fr = page.frame_locator("#topo-hosted-frame")
            # The document cannot fetch anything — everything it shows arrived
            # over the bridge. So what it draws IS the live graph.
            drawn = fr.locator(".cseries-node").count()
            check(drawn == 8,
                  f"the host's document drew every node the app gave it (got {drawn}, want 8)")
            first = fr.locator(".cseries-node").first.inner_text()
            check("desktop-01" in first,
                  f"the nodes carry real labels from the graph, not typed ones: {first!r}")
            head = fr.locator("#t").inner_text()
            check("acme" in head and "8" in head,
                  f"the document read host + count off the bridge: {head!r}")

            # Clicking a node inside the sandbox reaches the app's inspector —
            # the one inbound message, and only for a node the app actually sent.
            fr.locator(".cseries-node").first.click()
            page.wait_for_timeout(500)
            body = page.inner_text("body")
            check("desktop-01" in body,
                  "a click inside the host's map opens that machine's inspector")

        # ── and nothing threw on any of it ───────────────────────────────
        real_errors = [e for e in errors if "favicon" not in e.lower()]
        check(not real_errors, f"no console/page errors during the run: {real_errors[:3]}")

        page.screenshot(path="/tmp/network-physical.png")
        page.click('#topo-viewpick [data-view="logical"]')
        page.wait_for_timeout(300)
        page.screenshot(path="/tmp/network-logical.png")
        browser.close()

    for n in notes:
        print(n)
    if failures:
        print(f"\nFAIL — {len(failures)} problem(s):")
        for f in failures:
            print("  · " + f)
        return 1
    print(f"\nok — {len(notes)} live-DOM checks passed in a real browser")
    return 0


if __name__ == "__main__":
    sys.exit(main())
