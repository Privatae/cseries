#!/usr/bin/env python3
"""Gate for the `appearance` prompt block.

A host built desktop artifact 233, never called set_desktop, and then told
the user "233 was already your live desktop". It was not: the pointer in
entity_appearance still named something else, and nothing in the per-turn
prompt said so. PROMPT_SCHEMA's PER_TURN half carried app_state and nothing
about appearance at all, so the only answer the host had was an inference from
what its own turn had just done — the app_state failure again, one surface
along.

The same turn also produced artifact 234, whose token line declared the
palette backwards. That one mounted. So the block has to carry two facts, not
one: WHICH artifact is the desktop, and whether that document still passes the
floor RIGHT NOW.

What this pins:
  1. a live pointer renders the id, the name, the mode and the age of the
     DOCUMENT (projects.updated_at, not entity_appearance.updated_at — that
     row is bumped by an icon drag or a wallpaper swatch, so quoting it would
     invite "you set this a minute ago" about a turn that moved an icon;
     review, 2026-09-19);
  2. a pointed document that fails the floor says so, with the first error;
  3. no pointer renders as the stock wallpaper, and a pointer at a deleted
     artifact reads as NO pointer (the same rule the shell applies), never as
     a desktop that is not there;
  4. it fails soft — a broken pool, a missing row and an unavailable floor
     check each cost the turn nothing;
  5. it is actually WIRED, PER_TURN and conversational-only;
  6. it stays short enough to sit in every turn's prompt.

Fake pool, real block function; no DB, no container, no network.

Run: APP_ROOT=<repo> python3 scripts/test_appearance_block.py
"""
import asyncio
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "brain"))

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"[appearance] {'ok  ' if cond else 'FAIL'} {label}"
          f"{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


# The block imports server.routes.appearance lazily for the floor check. That
# module pulls in FastAPI and the whole route stack, which this lane does not
# have — so stand a stub in its place and drive the verdict from here. The
# floor's own maths is pinned next door in scripts/test_desktop_contract.py;
# what is under test here is what the block DOES with a verdict.
_verdict: dict | None = None


def _install_fake_appearance_module(raiser: bool = False) -> None:
    for name in ("server", "server.routes"):
        if name not in sys.modules:
            mod = types.ModuleType(name)
            mod.__path__ = []  # a package, so the submodule import resolves
            sys.modules[name] = mod

    mod = types.ModuleType("server.routes.appearance")

    async def desktop_contract_report(_pool, _eid, _aid, _entry="index.html"):
        if raiser:
            raise RuntimeError("floor check unavailable")
        return _verdict

    mod.desktop_contract_report = desktop_contract_report
    sys.modules["server.routes.appearance"] = mod
    sys.modules["server.routes"].appearance = mod


_install_fake_appearance_module()

from mastercode.runtime import prompt_schema as PS  # noqa: E402


class FakePool:
    """One row, whatever the query. The block issues exactly one fetchrow."""

    def __init__(self, row):
        self.row = row
        self.calls = 0

    async def fetchrow(self, _sql, *_a):
        self.calls += 1
        return self.row


class BoomPool:
    async def fetchrow(self, *_a):
        raise RuntimeError("pool down")


def render(row, verdict=None):
    global _verdict
    _verdict = verdict
    rt = types.SimpleNamespace(pool=FakePool(row))
    sess = types.SimpleNamespace(entity_id=9003)
    return asyncio.run(PS._b_appearance(rt, sess, None))


NOW = datetime.now(timezone.utc)
PASSES = {"ok": True, "mode": "desktop", "errors": [], "warnings": [], "tokens": {"--cseries-ink": "#000"}}
# The real first error off one Host's artifact 234, as the floor now words it.
FAILS = {"ok": False, "mode": "desktop", "warnings": [], "tokens": {}, "errors": [
    "--cseries-paper on --cseries-night measures 1.08:1 (body text on a window) — the floor "
    "is 4.5:1. Declared: --cseries-paper:#14141c, --cseries-night:#0a0a0f. The shell "
    "composites these two, so this is unreadable on screen, not a matter of taste"]}

LIVE = {"desktop_artifact_id": 233, "desktop_entry": "index.html", "wallpaper": "html",
        "artifact_name": "Nocturne",
        "artifact_updated_at": NOW - timedelta(minutes=12)}

# ── 1. a live pointer is a fact the host can check itself against ───────────
out = render(LIVE, PASSES)
check("the pointer's artifact id reaches the host", bool(out) and "artifact 233" in out, repr(out))
check("...with the artifact's own name", bool(out) and "Nocturne" in out)
check("...and the mode it declares", bool(out) and "desktop" in out)
check("...and when the DOCUMENT itself was last edited",
      bool(out) and "document last edited 12m ago" in out, repr(out))
check("...never the appearance row's stamp, which any layout save bumps",
      bool(out) and "appearance saved" not in out)
check("a passing desktop is not accused of failing",
      bool(out) and "FAILS" not in out, repr(out))

# ── 2. a desktop that fails the floor TODAY says so ─────────────────────────
out2 = render(LIVE, FAILS)
check("a pointed document that fails the floor is called out",
      bool(out2) and "currently FAILS the floor" in out2, repr(out2))
check("...naming the measured failure, not just 'invalid'",
      bool(out2) and "--cseries-paper on --cseries-night" in out2 and "1.08:1" in out2)
check("...and the pointer itself is still reported alongside it",
      bool(out2) and "artifact 233" in out2)

LONG = {"ok": False, "mode": "desktop", "warnings": [], "tokens": {},
        "errors": ["x" * 400]}
out2b = render(LIVE, LONG)
check("a very long first error is truncated, never dumped into every turn",
      bool(out2b) and len(out2b) < 260 and "…" in out2b, str(len(out2b or "")))

# ── 2b. the phone: what a phone shows when it is NOT the design ─────────────
# A desktop put on is worn on the phone too (appearance.set_desktop_artifact);
# a phone wallpaper the user picks in Settings afterwards wins, and then the
# design is not on their phone.
check("a phone that follows the desktop (no phone choice) adds nothing",
      bool(out) and "On a phone" not in out, repr(out))
out2c = render({**LIVE, "wallpaper_mobile": "custom"}, PASSES)
check("a phone wallpaper the user chose is named: the design is not on their phone",
      bool(out2c) and "On a phone: the 'custom' wallpaper" in out2c
      and "not this design" in out2c and "artifact 233" in out2c, repr(out2c))
out2d = render({**LIVE, "wallpaper_mobile": "html"}, PASSES)
check("...but the Host design chosen for the phone is the design: nothing added",
      bool(out2d) and "On a phone" not in out2d, repr(out2d))
# A swatch picked for the desktop leaves the pointer set: the desktop shows
# the swatch, and the design is only pointed at.
out2e = render({**LIVE, "wallpaper": "aurora"}, PASSES)
check("a desktop on a swatch with a design pointed at says the SWATCH is on the desktop",
      bool(out2e) and out2e.startswith("Desktop: stock wallpaper 'aurora', not the Host design")
      and "artifact 233 'Nocturne'" in out2e
      and "On a phone" not in out2e, repr(out2e))
out2f = render({**LIVE, "wallpaper": "aurora", "wallpaper_mobile": "html"}, PASSES)
check("...and when the PHONE wears the design under that swatch, it says so",
      bool(out2f) and "not the Host design" in out2f
      and out2f.splitlines()[1] == "On a phone: this Host design.", repr(out2f))
# An uploaded picture on the desktop is the user's own, never a stock one.
out2g = render({**LIVE, "wallpaper": "custom"}, PASSES)
check("...a picture the user uploaded for the desktop is called theirs, not stock",
      bool(out2g) and out2g.startswith("Desktop: the user's own uploaded picture, not the Host design")
      and "stock" not in out2g.splitlines()[0], repr(out2g))

# ── 3. no pointer, and a pointer at something that is gone ──────────────────
out3 = render({"desktop_artifact_id": None, "desktop_entry": None,
               "wallpaper": "aurora", "artifact_name": None,
               "artifact_updated_at": None}, PASSES)
check("no pointer renders as the stock wallpaper, by name",
      bool(out3) and "stock wallpaper" in out3 and "aurora" in out3, repr(out3))
out3b = render({"desktop_artifact_id": None, "desktop_entry": None,
                "wallpaper": "custom", "artifact_name": None,
                "artifact_updated_at": None}, PASSES)
check("...and an uploaded picture with no design set is the user's own, never 'stock'",
      out3b == "Desktop: the user's own uploaded picture — no host-designed desktop is set.",
      repr(out3b))
check("...and says plainly that no host design is up",
      bool(out3) and "no host-designed desktop is set" in out3)

# A deleted or converted artifact leaves wallpaper='html' with a dangling id.
# server/routes/appearance.py::_desktop_pointer reads that as no pointer; the
# prompt must not claim a desktop the shell will not show.
out4 = render({"desktop_artifact_id": 999, "desktop_entry": "index.html",
               "wallpaper": "html", "artifact_name": None,
               "artifact_updated_at": None}, PASSES)
check("a pointer at a deleted artifact reads as NO pointer, like the shell's",
      bool(out4) and "artifact 999" not in out4 and "stock wallpaper" in out4, repr(out4))
check("...and does not offer 'html' as if it were a wallpaper name",
      bool(out4) and "'html'" not in out4)

# ── 4. cost and safety ──────────────────────────────────────────────────────
check("an entity with no appearance row renders nothing", render(None, PASSES) is None)
check("a broken pool never takes the turn down",
      asyncio.run(PS._b_appearance(types.SimpleNamespace(pool=BoomPool()),
                                   types.SimpleNamespace(entity_id=9003), None)) is None)
check("the block issues exactly ONE appearance query",
      (lambda p: (asyncio.run(PS._b_appearance(types.SimpleNamespace(pool=p),
                                               types.SimpleNamespace(entity_id=9003), None)),
                  p.calls == 1)[1])(FakePool(LIVE)))

# An unavailable floor check must still leave the POINTER in the prompt: which
# artifact is the desktop is the fact the incident was about.
_install_fake_appearance_module(raiser=True)
out5 = render(LIVE, PASSES)
check("the pointer survives a floor check that raises",
      bool(out5) and "artifact 233" in out5 and "FAILS" not in out5, repr(out5))
_install_fake_appearance_module()

# ── 5. wired, per-turn, conversational ──────────────────────────────────────
_blocks = {b.name: b for b in PS.PROMPT_SCHEMA}
check("appearance is in PROMPT_SCHEMA", "appearance" in _blocks)
check("...as PER_TURN — the pointer changes inside the conversation that sets it",
      "appearance" in _blocks
      and _blocks["appearance"].freshness is PS.Freshness.PER_TURN)
check("...and only on conversational surfaces",
      "appearance" in _blocks
      and _blocks["appearance"].gate is PS.conversational_only)

# ── 6. it has to fit in EVERY turn ──────────────────────────────────────────
for label, o in (("live + passing", out), ("live + failing", out2),
                 ("stock wallpaper", out3),
                 # The longest the phone lines make it: a failing floor AND a
                 # phone line, on a swatch or on the design itself.
                 ("swatch + failing + phone wears the design",
                  render({**LIVE, "wallpaper": "aurora", "wallpaper_mobile": "html"}, FAILS)),
                 ("failing + a phone wallpaper the user chose",
                  render({**LIVE, "wallpaper_mobile": "custom"}, FAILS))):
    check(f"the {label} line stays inside the per-turn budget (~200 chars)",
          bool(o) and len(o) <= 300, f"{len(o or '')} chars: {o!r}")

print()
if _FAILURES:
    print(f"{len(_FAILURES)} FAILED: {_FAILURES}")
    sys.exit(1)
print("[appearance] ALL OK")
