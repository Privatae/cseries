#!/usr/bin/env python3
"""Gate: the host can LOOK at a broken page itself — 2026-09-05.

Origin, entity 9128. A user reported an app's UI as broken and repeated it four
times. The host could not look. Its only web-facing tools were `browser_agent`
(replays a task the USER recorded — no observation) and `web_search`, and the
Browser app's own card said it outright: "you have no tool that reads the
viewport". So the host opened a browser it could not read and fell back to
"hard-refresh / clear your cache / describe it again" for a day and a half,
while the platform owned the exact capability the whole time
(sidecars/browser/control/verify.py) reachable only from the delivery proof.

`check_page` is the troubleshooting door onto that same check, held by the
host. This gate pins the four things that make it useful rather than
decorative:

  1. it is REACHABLE — a whole-tool read lane. The moment looking requires
     dispatching an agent, the host stops looking and asks the user again.
  2. it is OFFERED — schema + meta-name + caps entry, so it is advertised and
     not default-denied at the call.
  3. `checked: false` is NEVER a pass. The proof already learned this the
     hard way; a look that did not happen must not read as a working page.
  4. the Browser app's card no longer tells the host it cannot look, and
     names this tool instead. That sentence is the block itself.

Run: APP_ROOT=<repo> python3 scripts/test_check_page_tool.py
"""
import asyncio
import os
import sys
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[check-page] ok   {label}")
    else:
        FAILED.append(label)
        print(f"[check-page] FAIL {label}" + (f" — {detail}" if detail else ""))


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def main() -> int:
    from mastercode.public_tool_registry import (PUBLIC_TOOL_SCHEMAS,
                                                 PUBLIC_META_TOOL_NAMES)
    from mastercode.runtime.tool_lanes import lane_for
    from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS

    # ── 1. reachable, and reachable WITHOUT an agent ──────────────────────
    check("check_page is a whole-tool READ (the host looks for itself)",
          lane_for("check_page", PUBLIC_TOOL_SCHEMAS.get("check_page")) == "read",
          lane_for("check_page"))

    # ── 2. offered and not default-denied ─────────────────────────────────
    sch = PUBLIC_TOOL_SCHEMAS.get("check_page")
    check("check_page carries a schema", isinstance(sch, dict))
    check("check_page dispatches inline", "check_page" in PUBLIC_META_TOOL_NAMES)
    check("check_page has a META_TOOL_CAPS entry (else default-deny at call)",
          "check_page" in META_TOOL_CAPS)
    fn = (sch or {}).get("function", {})
    params = fn.get("parameters", {}).get("properties", {})
    check("takes app_id, required", "app_id" in params
          and "app_id" in fn.get("parameters", {}).get("required", []))
    check("takes an optional path, so a DEEP route can be looked at "
          "(an entry page often renders while /trade is empty)",
          "path" in params
          and "path" not in fn.get("parameters", {}).get("required", []))
    desc = fn.get("description", "")
    check("the description tells the host to look BEFORE asking for a "
          "hard-refresh — the entity-9128 behaviour this replaces",
          "hard-refresh" in desc.lower() and "before" in desc.lower(), desc[:120])

    # ── 3. a look that did not happen is never a pass ─────────────────────
    from mastercode.runtime import tools as T

    async def _no_browser(app_id, entrypoint, *, require_html=True):
        return {"checked": False, "reason": "browser re-wake refused or failed"}

    import mastercode.runtime.app_verify as AV
    real_bv, real_bs = AV._browser_verify, None
    AV._browser_verify = _no_browser

    class _App:
        id = "trading-bot"
        name = "F"
        source_dir = None
        tier = "custom"

    class _Reg:
        @staticmethod
        async def list_active(_pool):
            return [_App()]

    import server.applications.registry as _R
    import server.brain_service as _BS
    real_list, real_pool = _R.list_active, _BS.get_pool
    _R.list_active = _Reg.list_active
    _BS.get_pool = lambda: object()
    try:
        out = run(T._exec_check_page({"app_id": "trading-bot"}))
        res = out.get("result", {})
        check("a browser that could not be asked reports checked:false",
              res.get("checked") is False, str(res)[:120])
        check("...carries the reason", bool(res.get("reason")))
        check("...and says plainly it is not a pass",
              "not" in (res.get("note") or "").lower()
              and "working" in (res.get("note") or "").lower(),
              res.get("note"))
        check("...and never claims ok",
              "ok" not in res or res.get("ok") is not True)

        # An app that is not installed is an honest answer, not a crash.
        _R.list_active = lambda _p: _none()

        async def _none():
            return []
        out = run(T._exec_check_page({"app_id": "nope"}))
        check("an app that is not installed answers installed:false",
              out.get("result", {}).get("installed") is False, str(out)[:120])
    finally:
        AV._browser_verify = real_bv
        _R.list_active, _BS.get_pool = real_list, real_pool

    # ── 3b. a DEEP ROUTE must actually reach the browser ──────────────────
    # Review-2 found this by reading, because the check above stubs
    # _browser_verify and so cannot see it: _browser_verify refuses anything
    # not ending .html/.htm, and the schema's own examples ('trade',
    # 'ui/dashboard') have no extension — so every deep-route look
    # returned "the browser could not be asked". Drive the REAL prelude and
    # assert it gets past the extension guard to the next honest failure
    # (no browser lane / no routable kernel in this harness).
    real_av = AV._browser_verify
    for route in ("trade", "ui/dashboard", "index.html"):
        got = run(real_av("trading-bot", route,
                          require_html=(route.endswith(".html"))))
        check(f"a deep route gets PAST the html guard: {route!r}",
              got.get("reason") != "no HTML entrypoint to load", str(got)[:110])
    # ...and the delivery proof's own contract is unchanged: a manifest
    # entrypoint that is not HTML is still refused.
    got = run(real_av("trading-bot", "run.sh"))
    check("the delivery proof still refuses a non-HTML entrypoint",
          got.get("checked") is False
          and got.get("reason") == "no HTML entrypoint to load", str(got)[:110])
    got = run(real_av("trading-bot", ""))
    check("an empty page is refused, with a reason",
          got.get("checked") is False and got.get("reason") == "no page to load")

    # ── 4. the card that WAS the block ────────────────────────────────────
    card = (_ROOT / "system-apps/browser/app.yaml").read_text()
    check("the Browser card no longer says the host has no tool to read a page",
          "no tool that reads the viewport" not in card)
    check("...and points at check_page instead", "check_page" in card)
    check("...telling it to look before asking for a hard-refresh",
          "hard-refresh" in card.lower())

    print()
    if FAILED:
        print(f"[check-page] {len(FAILED)} FAILED: {FAILED}")
        return 1
    print("[check-page] all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
