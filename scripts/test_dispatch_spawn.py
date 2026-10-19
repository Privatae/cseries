#!/usr/bin/env python3
"""Gate: a governed dispatch actually spawns.

A change once passed `grant=` into AgentDispatch._run_and_record
and used it in the body, but never added it to the signature — every async
dispatch on every entity died at spawn with a TypeError that
surfaced only as chat text. A week of invisibility because nothing exercised
a dispatch spawn. This gate pins both layers:

  1. STRUCTURAL — _run_and_record accepts every kwarg dispatch() passes.
  2. FUNCTIONAL — dispatch() with stubbed DB/engine completes the background
     task and records a terminal state (not a TypeError corpse).

stdlib-only, check() shape per test_host_sdk_wall.py.
"""
import asyncio
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.environ.get("APP_ROOT", "/app"), "brain"))

FAILS = []


def check(name, ok, detail=""):
    print(f"[dispatch-spawn] {'ok  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def main():
    from mastercode.runtime import agent_dispatch as ad

    # 1. structural: every kwarg dispatch() passes must be in the signature
    sig = inspect.signature(ad.AgentDispatch._run_and_record)
    for kw in ("task_id", "entity_id", "task_prompt", "tool_scope", "grant",
               "model", "provider", "session_id", "system_preamble",
               "max_rounds", "origin"):
        check(f"signature accepts {kw!r}", kw in sig.parameters)

    # 2. functional: a stubbed dispatch completes without TypeError
    class FakePool:
        async def execute(self, *a, **k):
            return None

    async def fake_create(entity_id, title, task_prompt):
        return (4242, "agent:4242")

    captured = {}

    async def fake_run(pool, **kwargs):
        # record, don't assert here — _run_and_record never raises, so an
        # in-coroutine assert would be swallowed; assert AFTER await instead.
        captured.update(kwargs)
        return {"status": "done", "output": "stub"}

    async def go():
        d = ad.AgentDispatch(FakePool())
        d._create_agent_task = fake_create
        ad_run = ad.run_scoped_agent
        ad.run_scoped_agent = fake_run
        try:
            handle = await d.dispatch(entity_id=7, title="t",
                                      task_prompt="p", tool_scope=[],
                                      grant=["memory_curate"])
            task = d._running[handle["task_id"]]
            await task
            return task
        finally:
            ad.run_scoped_agent = ad_run

    try:
        t = asyncio.run(go())
        check("dispatch() spawns and completes (no TypeError)", t.done() and t.exception() is None)
        check("grant reaches the engine intact",
              captured.get("grant") == ["memory_curate"], f"got {captured.get('grant')!r}")
    except TypeError as e:
        check("dispatch() spawns and completes (no TypeError)", False, str(e))

    # ── the unpriced claim, checked on BOTH sides ───────────────────────
    # _est_cost returns None when the catalog has no published rate, replacing
    # a hardcoded table that invented one. That is only an improvement if the
    # surfaces SAY so: an empty cost cell reads as $0 to a person, and $0 and
    # "we hold no rate card" are opposite facts. The docstrings in
    # agent_dispatch.py name the three render sites, which makes the claim
    # checkable — so check it, or it rots the first time a card is restyled.
    # Inside the image there is no React SOURCE (Dockerfile.linux bakes the
    # built app under system-apps/<app>/assets, never <app>-web/src), and
    # smoke runs this gate by `docker exec` — so the source read failed on the
    # first ship after this check landed. What ships is the built asset, so when
    # the source is absent that is what is checked: the word is a rendered
    # string, not an identifier, so it survives minification.
    import os as _os
    _root = _os.environ.get("APP_ROOT", "/app")
    _sites = (("workbench-web/src/components/workbench/ops-pane.tsx", "workbench"),
              ("workbench-web/src/components/chat/tool-card.tsx", "workbench"),
              ("circles-web/src/components/chat/tool-card.tsx", "circles"))
    # In the bundle the render sites are one concatenated file, so a site that
    # lost its label would hide behind a sibling that kept it. The bundle check
    # therefore requires as many occurrences as the app has sites (2 for the
    # workbench, 1 for circles) — measured today: workbench 2, circles 1.
    _sites_per_app = {a: sum(1 for _, b in _sites if b == a) for _, a in _sites}
    for _rel, _app in _sites:
        _p = _os.path.join(_root, _rel)
        _need = 1
        try:
            with open(_p, encoding="utf-8") as _fh:
                _src = _fh.read()
            _what = _rel
        except OSError:
            # The image bakes the built app only as its signed bundle,
            # /app/native-bundles/<id>.cseries (a zip; the JS sits under
            # payload/). The tree carries the same file, so one reader serves
            # both. install_image_bundles is what an entity actually unpacks,
            # so the bundle is the truer surface than the source anyway.
            import zipfile as _zipfile
            _bundle = _os.path.join(_root, "native-bundles", f"{_app}.cseries")
            try:
                with _zipfile.ZipFile(_bundle) as _z:
                    _src = "".join(
                        _z.read(_n).decode("utf-8", errors="replace")
                        for _n in _z.namelist() if _n.endswith(".js"))
            except (OSError, _zipfile.BadZipFile) as _e:
                check(f"{_rel} (or the built {_app} bundle) is readable", False,
                      f"neither the source nor native-bundles/{_app}.cseries under {_root}: {_e}")
                continue
            _what = f"built {_app} bundle (source absent in the image) for {_rel.rsplit('/', 1)[-1]}"
            _need = _sites_per_app[_app]
        _n = _src.count("unpriced")
        check(f"{_what} renders 'unpriced' for a null cost ({_n} occurrence(s), need {_need})",
              _n >= _need)
    # And the estimator it is claiming about must still be able to return None.
    from mastercode.runtime import agent_dispatch as _ad
    check("_est_cost returns None when nothing can price the model",
          _ad._est_cost("model-that-is-in-no-catalog", 100, 100) is None)
    check("the hardcoded rate table is gone (one pricing source)",
          not hasattr(_ad, "_RATE_PER_M") and not hasattr(_ad, "_DEFAULT_RATE"))
    check("a None cost formats for a log line instead of raising",
          _ad._cost_str(None) == "unpriced" and _ad._cost_str(0.5) == "$0.50000")

    if FAILS:
        print(f"[dispatch-spawn] FAIL ({len(FAILS)} failing)")
        return 1
    print("[dispatch-spawn] PASS (0 failing)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
