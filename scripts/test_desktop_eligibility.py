#!/usr/bin/env python3
""""Use as desktop" is offered only where it would actually work.

The "use as desktop" button belongs only on artifacts that can actually be set
as the desktop safely, not on every artifact.

Before this, the button was on every artifact and the only way to find out was
to click it and read a 422 — a refusal arriving after the user had decided.

THE PROPERTY THIS PINS is not "a check exists". It is that the check and the
act are the SAME code: GET /api/appearance/desktop/check and PUT
/api/appearance/desktop both go through desktop_gates() (appearance.py:283), so
the button cannot promise something the kernel then refuses. A second
implementation of the floor — in the route or in the client — would drift the
first time either moved, and a button that lies about what will happen is worse
than one that is always on.

Container-free: the artifact render and the installed-app lookup are stubbed,
because what is under test is the AGREEMENT between check and set, not artifact
storage.

Run:  python3 scripts/test_desktop_eligibility.py
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import os
import sys
import types

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(__file__), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain"), os.path.join(_ROOT, "system-apps")]

failures: list[str] = []
notes: list[str] = []


def check(cond, msg):
    if cond:
        notes.append("  PASS  " + msg)
    else:
        failures.append(msg)


PASSES = (
    '<!DOCTYPE html><html><head>'
    '<meta name="cseries-desktop" content="wallpaper">'
    '<meta name="cseries-tokens" content="'
    '--cseries-ink:#0b0f0e; --cseries-night:#111614; --cseries-twilight:#161d1a;'
    '--cseries-silk:#1d2622; --cseries-veil:#243029; --cseries-mist:#37414b;'
    '--cseries-paper:#e8efe9; --cseries-linen:#cfd6d3; --cseries-ash:#8b968f;'
    '--cseries-ember:#4a7c59; --cseries-dusk:#6b7f8f">'
    '</head><body><div id="bg"></div></body></html>'
)
FAILS = '<!DOCTYPE html><html><head></head><body>just a poster</body></html>'


def _calls(fn) -> set[str]:
    """Every function name called in fn's body, from the AST.

    Deliberately not a substring search: a comment naming desktop_gates would
    satisfy a grep while the code did something else, and that is exactly the
    failure this file exists to prevent.
    """
    tree = ast.parse(inspect.getsource(fn).lstrip())
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = getattr(f, "id", None) or getattr(f, "attr", None)
            if name:
                out.add(name)
    return out


async def main() -> int:
    _stub_main = types.ModuleType("server.main")
    _stub_main.app = None
    sys.modules.setdefault("server.main", _stub_main)

    import server.routes.appearance as appearance
    from server.applications.desktop_contract import check_document

    # ── 1. the check and the set are literally the same gates ───────────────
    setter_calls = _calls(appearance.set_desktop_artifact)
    checker_calls = _calls(appearance.check_desktop_candidate)
    check("desktop_gates" in setter_calls, "the SETTER runs desktop_gates()")
    check("desktop_gates" in checker_calls,
          "the CHECKER runs desktop_gates() — same code, so they cannot disagree")
    check("check_document" not in checker_calls,
          "the checker does not re-implement the floor beside the setter's copy")

    gate_src = inspect.getsource(appearance.desktop_gates)
    for needle, what in (("artifact_service.render", "ownership + a renderable workspace"),
                         ("is_file()", "the NAMED entry must exist"),
                         ("check_document", "the desktop floor")):
        check(needle in gate_src, f"desktop_gates still applies: {what}")

    # ── 2. it answers, and it answers the same as the floor ─────────────────
    rendered: dict[str, str] = {"index.html": PASSES}

    async def fake_render(pool, eid, aid, entry):
        return rendered["index.html"]

    async def fake_ids(pool):
        return frozenset({"artifacts", "network"})

    class FakeSvc:
        # brain_service._entity_id is a module-level int (brain_service.py:38),
        # never None — mirrored here so the stub matches the real contract.
        _entity_id = 42

        @staticmethod
        def get_pool():
            return object()

    appearance.artifact_service = types.SimpleNamespace(render=fake_render)
    appearance._installed_app_ids = fake_ids
    appearance.brain_service = FakeSvc()

    # The entry-exists gate touches disk; the agreement under test is not about
    # disk, so make that ONE call true. A wholesale Path replacement breaks
    # _clean_entry, which legitimately uses Path(entry).parts to reject
    # traversal — subclass so every other Path behaviour is the real one.
    real_path = appearance.Path

    class _AlwaysFile(real_path):
        def is_file(self):
            return True

    appearance.Path = _AlwaysFile

    try:
        good = await appearance.check_desktop_candidate(artifact_id=1)
        check(good["ok"] is True, f"a document that meets the floor is offered: {good['errors']}")
        check(good["mode"] == "wallpaper", f"the mode is reported (got {good['mode']!r})")

        rendered["index.html"] = FAILS
        bad = await appearance.check_desktop_candidate(artifact_id=1)
        check(bad["ok"] is False, "a document that does not meet the floor is NOT offered")
        check(bool(bad["errors"]),
              "the refusal says what is missing, so the user can hand it to the host")
        check(bool(bad["report"]), "the report is the kernel's own text, not a paraphrase")

        for doc, label in ((PASSES, "compliant"), (FAILS, "non-compliant")):
            rendered["index.html"] = doc
            got = await appearance.check_desktop_candidate(artifact_id=1)
            direct = check_document(doc, installed_ids=await fake_ids(None))
            check(got["ok"] == direct["ok"],
                  f"the endpoint's verdict matches check_document on the {label} document")

        # ── 3. and the SETTER refuses exactly what the check declined ───────
        # Offered-but-refused is the bug; this is the pair that proves it gone.
        rendered["index.html"] = FAILS
        refused = False
        try:
            await appearance.set_desktop_artifact(object(), 42, 1)
        except Exception as e:  # noqa: BLE001 - the type IS the assertion
            refused = (e.__class__.__name__ == "ArtifactError"
                       and getattr(e, "status", None) == 422)
        check(refused,
              "the setter refuses (422) the same document the check declined — the "
              "button is never offered for something the kernel would reject")
    finally:
        appearance.Path = real_path

    # ── 4. the client starts disabled and asks ──────────────────────────────
    html = open(os.path.join(_ROOT, "system-apps/artifacts/index.html"), encoding="utf-8").read()
    i = html.find('id="btn-desktop"')
    btn = html[max(0, i - 200):i + 200]
    check("disabled" in btn,
          "the button ships DISABLED, so it is never clickable before the answer is in")

    app_js = open(os.path.join(_ROOT, "system-apps/artifacts/app.js"), encoding="utf-8").read()
    check("/api/appearance/desktop/check" in app_js,
          "the window asks the kernel rather than judging the document itself")
    code = "\n".join(ln.split("//")[0] for ln in app_js.splitlines())
    check("cseries-tokens" not in code and "REQUIRED_TOKENS" not in code,
          "the window does NOT re-implement the floor — a second copy would drift")

    for line in notes:
        print(line)
    if failures:
        print(f"\nFAIL — {len(failures)} problem(s):")
        for f in failures:
            print("  · " + f)
        return 1
    print(f"\nok — {len(notes)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
