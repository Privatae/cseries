#!/usr/bin/env python3
"""The Ships app, its routes, the Host's reach to them, and the ships card.

Runs as pytest AND as a plain script in the container-free gate lane
(APP_ROOT=<repo> python3 scripts/test_ships_app.py). No DB, no network.

WHAT THIS FILE DEFENDS.

1. THE APP'S CONTRACT. It declares exactly read:ships and write:ships, calls
   only routes that exist under a scope it declares, writes nothing through
   innerHTML (hostnames, step names, errors and receipts come from machines
   and pipeline files), is image-baked with a current signed bundle, and has
   a dock glyph.
2. THE HOST'S REACH. kernel_op exposes /api/ships: the Host reads its runs and
   may start one (which only files the plan on the person's device). Clearing
   a machine's pinned key is walled, with a reason that sends the Host to the
   person. Both scopes carry entity bits; neither is open to community apps.
3. THE CARD. `ships` is a platform card (its tables are platform
   migrations), appended after machines and findings, and it does not take
   the servers question those two answer.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import zipfile

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

from server.routes import ships as routes                    # noqa: E402

APP_DIR = os.path.join(_ROOT, "system-apps", "ships")


def _read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_manifest_declares_exactly_the_ships_scopes():
    from pathlib import Path
    from server.applications import manifest, scopes
    m = manifest.load(Path(APP_DIR) / "app.yaml")
    assert m.id == "ships"
    assert sorted(m.permissions) == ["read:ships", "write:ships"], m.permissions
    for p in m.permissions:
        assert p in scopes.PERMISSIONS, p
    assert not m.target_entities, "every Host can ship, so every Host gets the window"
    assert m.prompt_behavior.user_path


def test_community_apps_cannot_ask_for_ships():
    from server.applications import build_contract
    assert not {"read:ships", "write:ships"} & set(build_contract.COMMUNITY_ALLOWED_SCOPES)


def test_every_route_the_app_calls_exists_under_a_scope_it_declares():
    # Each call is reduced to a template: a literal, then `{x}` where it
    # splices encodeURIComponent(...), then any literal suffix.
    js = _read("system-apps/ships/app.js")
    rx = re.compile(r"'(/api/ships/[a-z/]*?)/?\??'"
                    r"(\s*\+\s*encodeURIComponent\([^)]*\)(?:\s*\+\s*'(/[a-z]+)')?)?")
    called = set()
    for lit, spliced, suffix in rx.findall(js):
        called.add(lit + ("/{x}" + (suffix or "") if spliced else ""))
    assert called == {"/api/ships/runs", "/api/ships/runs/{x}/stop", "/api/ships/pipelines",
                      "/api/ships/machines", "/api/ships/machines/{x}/pin", "/api/ships/keys"}, called
    paths = {}
    for r in routes.router.routes:
        scope = [getattr(d.call, "_app_scope", None) for d in r.dependant.dependencies]
        s = [x for x in scope if x]
        paths.setdefault(re.sub(r"\{[^}]+\}", "{x}", r.path), set()).update(s)
    declared = {"read:ships", "write:ships"}
    for c in called:
        assert c in paths, f"the app calls {c}, which no route serves"
        assert paths[c] <= declared and paths[c], f"{c} needs {paths[c]}, which the app does not declare"


def test_every_route_takes_a_ships_scope():
    for r in routes.router.routes:
        scope = [getattr(d.call, "_app_scope", None) for d in r.dependant.dependencies]
        assert [s for s in scope if s] and all(s in ("read:ships", "write:ships") for s in scope if s), r.path
        want = "read:ships" if "GET" in r.methods else "write:ships"
        assert want in scope, f"{sorted(r.methods)} {r.path} should need {want}"


def test_nothing_a_machine_names_reaches_innerhtml():
    js = _read("system-apps/ships/app.js")
    for rx in (r"\.innerHTML\s*[+]?=", r"\.outerHTML\s*[+]?=", r"insertAdjacentHTML\s*\(",
               r"document\.write\s*\(", r"\.srcdoc\s*="):
        assert not re.search(rx, js), f"app.js writes HTML ({rx})"


def test_the_app_is_image_baked_with_a_current_signed_bundle():
    baked = [ln.strip() for ln in _read("scripts/image_baked_apps.txt").splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    assert "ships" in baked
    assert baked == sorted(baked), "image_baked_apps.txt is kept sorted"
    members = {"manifest.yaml": "app.yaml"}
    members.update({f"payload/{n}": n for n in ("index.html", "app.js", "style.css", "icon.svg")})
    from server.applications import bundles
    with open(os.path.join(_ROOT, "etc", "cseries-apps.pub.raw"), "rb") as f:
        pub = f.read()
    for d in ("native-bundles", "factory-bundles"):
        path = os.path.join(_ROOT, d, "ships.cseries")
        assert os.path.exists(path), f"{d}/ships.cseries is missing"
        with zipfile.ZipFile(path) as z:
            for member, src in members.items():
                with open(os.path.join(APP_DIR, src), "rb") as f:
                    assert z.read(member) == f.read(), f"{d}/ships.cseries carries a stale {src} — re-pack and re-sign it"
        with open(path, "rb") as f:
            bundles.verify(f.read(), public_key_raw=pub)


def test_the_dock_has_a_glyph_for_it():
    assert re.search(r"^\s+ships: `", _read("dashboard/base/js/icons.js"), re.M)


def test_the_host_reads_and_starts_runs_but_never_clears_a_pin():
    from mastercode.runtime import kernel_ops as ko
    from mastercode.runtime.meta_tool_caps import ENTITY_SCOPE_TO_BITS
    assert "/api/ships" in ko._EXPOSED_PREFIXES
    assert ENTITY_SCOPE_TO_BITS.get("read:ships") == 1 and ENTITY_SCOPE_TO_BITS.get("write:ships") == 4
    wall = ko._walled_path_for("/api/ships/machines/12/pin")
    assert wall == "/api/ships/machines/{token_id}/pin", wall
    assert "Clear pin" in ko._WALL_REASONS[wall]
    # The walled template is the router's own path, so the wall names a real route.
    assert any(r.path == wall for r in routes.router.routes)


def test_the_ships_card_is_a_platform_card_that_leaves_the_servers_question_alone():
    from mastercode import live_context as lc
    ids = [c.id for c in lc.PLATFORM_CARDS]
    assert ids[:2] == ["machines", "findings"] and ids[-1] == "ships", ids
    card = lc.PLATFORM_CARDS[-1]
    assert "servers" not in card.keywords and "looking" not in card.keywords and "build" not in card.keywords
    scored = {c.id for _s, c in lc.score_cards("how are the servers looking?", lc.PLATFORM_CARDS)}
    assert scored == {"machines", "findings"}, scored
    shipq = {c.id for _s, c in lc.score_cards("did the bridge ship go out, and is the deploy done?",
                                               lc.PLATFORM_CARDS)}
    assert "ships" in shipq, shipq


def test_the_card_names_what_waits_and_what_needs_the_host():
    from datetime import datetime, timezone
    from mastercode import live_context as lc
    now = datetime.now(timezone.utc)

    class Pool:
        async def fetch(self, sql, *a):
            assert "FROM ship_runs" in sql
            return [
                {"id": 9, "name": "bridge", "state": "awaiting_tap", "step": 0, "commit_sha": "a" * 40,
                 "error": None, "needs_host": False, "last_seen": now, "step_name": None, "machine": None,
                 "step_state": None},
                {"id": 8, "name": "cseries", "state": "failed", "step": 2, "commit_sha": "b" * 40,
                 "error": "step 2 (roll) failed on box-d: rollout did not finish", "needs_host": True,
                 "last_seen": now, "step_name": "roll", "machine": "box-d", "step_state": "failed"},
            ]
    lines, as_of = asyncio.run(lc._fetch_ships(Pool(), 3))
    text = "\n".join(lines)
    assert "1 pipeline run(s) under way." in text and "pipeline bridge run 9" in text \
        and "waiting for the tap" in text, text
    assert "NEEDS YOU: pipeline cseries run 8" in text and "rollout did not finish" in text, text
    assert as_of == now


def _main() -> int:
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"OK    {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL  {name}: {e}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{'FAILED' if fails else 'PASSED'} — {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_main())
