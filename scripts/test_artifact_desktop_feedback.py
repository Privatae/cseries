#!/usr/bin/env python3
"""Gate for the `artifact` tool's desktop feedback + template seeding.

2026-09-18. The host wrote desktop artifact 233 with `op='create'`,
got nothing back about it, never called set_desktop, and told the user it was
live. Two holes made that possible and both are closed here:

  1. op='create' never called _attach_desktop_contract at all — only write,
     promote and import did. The op that writes the FIRST version of a desktop
     was the one op with no verdict.
  2. _attach_desktop_contract returned early unless the document declared a
     mode meta or some tokens. A document that ignores the floor completely
     declares neither, so the one design that most needed to hear from the
     floor was the one guaranteed not to. The trigger is now: HTML entry, AND
     (the document declares something OR the caller said kind='desktop' OR the
     artifact's own name/description says desktop/wallpaper) — and
     contract.trigger says WHICH of the three fired, because they do not mean
     the same thing. A name is not a claim about the bytes, so the name-only
     case never says REFUSED: nothing was refused, and calling it that trains
     the host to read a real refusal as noise (review, 2026-09-19).

And the seed: `op='create'` with kind='desktop' and no files copies the
platform's template server-side (switching shells is an op, not a model
rewrite), so the host starts from a document that already
passes the floor instead of retyping 20 KB and losing the meta on the way.

Fake pool and fake artifact service, REAL floor and real tool handler; no DB,
no container, no network.

Run: APP_ROOT=<repo> python3 scripts/test_artifact_desktop_feedback.py
"""
import asyncio
import os
import sys
import types
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "brain"))

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"[artifact-floor] {'ok  ' if cond else 'FAIL'} {label}"
          f"{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


# ── stand-ins for the parts that need a pod, with the REAL floor kept ───────
# server.* is faked as a package whose __path__ still points at the real tree,
# so server.applications.desktop_contract (the floor, and the template the seed
# copies) imports for real while the pod-bound modules beside it do not.
def _pkg(name: str, path: Path):
    m = types.ModuleType(name)
    m.__path__ = [str(path)]
    sys.modules[name] = m
    return m


_pkg("server", _ROOT / "server")
_pkg("server.applications", _ROOT / "server" / "applications")
_pkg("server.routes", _ROOT / "server" / "routes")

from server.applications.desktop_contract import (  # noqa: E402
    TEMPLATE_HTML, check_document)

_brain = types.ModuleType("server.brain_service")
_brain._entity_id = 9003
_brain.get_pool = lambda: POOL
sys.modules["server.brain_service"] = _brain
sys.modules["server"].brain_service = _brain


class ArtifactError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status, self.detail = status, detail


# The artifact workspace, in a dict: {artifact_id: {path: content}}.
WORKSPACE: dict[int, dict[str, str]] = {}
LAST_CREATE: dict = {}


async def _create_artifact(_pool, _eid, *, name, files=None, entry="index.html",
                           description=""):
    aid = 501 + len(WORKSPACE)
    WORKSPACE[aid] = {f["path"]: f["content"] for f in (files or [])}
    LAST_CREATE.clear()
    LAST_CREATE.update({"name": name, "files": files, "entry": entry,
                        "description": description, "artifact_id": aid})
    return {"artifact_id": aid, "name": name, "entry": entry,
            "files_written": len(files or []), "preview_url": f"/a/{aid}"}


async def _write_files(_pool, _eid, artifact_id, files, *, desktop_wait_s=None):
    # desktop_wait_s: how long a Host write waits for the worn desktop's
    # browser render check (server/applications/desktop_pin.py) — not this
    # gate's concern; the stand-in only has to accept it like the real one.
    WORKSPACE.setdefault(int(artifact_id), {}).update(
        {f["path"]: f["content"] for f in (files or [])})
    return {"artifact_id": int(artifact_id), "files_written": len(files or [])}


_art = types.ModuleType("server.applications.artifacts")
_art.ArtifactError = ArtifactError
_art.create_artifact = _create_artifact
_art.write_files = _write_files
# Stand-in for the real name rule (server/applications/artifacts.py _NAME_RE),
# which the tool handler consults when it names an artifact itself. The real
# module needs the pod; the class is copied here for the fake only.
import re as _re  # noqa: E402
_art.is_valid_artifact_name = lambda n: _re.match(
    r"^[\w .,()\-–—'’\"“”:;&!?+]{1,120}$", (n or "").strip()) is not None
sys.modules["server.applications.artifacts"] = _art
sys.modules["server.applications"].artifacts = _art

_app = types.ModuleType("server.routes.appearance")
RAISE_REPORT = {"on": False}


async def _desktop_contract_report(_pool, _eid, artifact_id, entry="index.html"):
    """The real floor, run over whatever the fake workspace holds."""
    if RAISE_REPORT["on"]:
        raise RuntimeError("render unavailable")
    html = WORKSPACE.get(int(artifact_id), {}).get(entry)
    if html is None:
        return None
    return check_document(html)


_app.desktop_contract_report = _desktop_contract_report
sys.modules["server.routes.appearance"] = _app
sys.modules["server.routes"].appearance = _app


class FakePool:
    """Only _artifact_words queries this — the artifact's own name row."""

    def __init__(self):
        self.row = None
        self.fetchrow_calls = 0

    async def fetchrow(self, _sql, *_a):
        self.fetchrow_calls += 1
        return self.row


POOL = FakePool()

from mastercode.runtime.tools import _exec_artifact  # noqa: E402


def call(**kw):
    return asyncio.run(_exec_artifact(kw))


PLAIN = ("<!DOCTYPE html><html><head><title>Invoice</title></head>"
         "<body><h1>Invoice</h1></body></html>")
BACKWARDS = (
    '<!DOCTYPE html><html><head><meta name="cseries-desktop" content="desktop">'
    '<meta name="cseries-tokens" content="--cseries-ink:#e8e6f0; --cseries-night:#0a0a0f; '
    '--cseries-twilight:#1a1440; --cseries-silk:#f2f0fa; --cseries-veil:rgba(255,255,255,0.04); '
    '--cseries-mist:#6b6880; --cseries-paper:#14141c; --cseries-linen:#1c1c26; '
    '--cseries-ash:#3a3848; --cseries-ember:#e8a04c; --cseries-dusk:#8b7cf6">'
    "</head><body><div data-cseries-apps></div></body></html>")

# ── 1. the seed is an OPERATION ─────────────────────────────────────────────
r = call(op="create", name="Threshold", kind="desktop")
check("create kind='desktop' with no files succeeds", r["status"] == "ok", repr(r))
seeded = WORKSPACE[r["result"]["artifact_id"]].get("index.html")
check("...and the kernel seeded index.html from the template, byte for byte",
      seeded == TEMPLATE_HTML)
check("...with the model supplying no file content at all",
      LAST_CREATE["files"] is not None and len(LAST_CREATE["files"]) == 1
      and LAST_CREATE["files"][0]["path"] == "index.html")
check("a seeded desktop passes the floor as created",
      r["result"].get("contract", {}).get("ok") is True, repr(r["result"].get("contract")))

r = call(op="create", name="Threshold", kind="desktop", entry="home.html")
check("the seed honours a non-default entry",
      WORKSPACE[r["result"]["artifact_id"]].get("home.html") == TEMPLATE_HTML)

r = call(op="create", name="Mine", kind="desktop",
         files=[{"path": "index.html", "content": BACKWARDS}])
check("kind='desktop' WITH files never overwrites what the host wrote",
      WORKSPACE[r["result"]["artifact_id"]]["index.html"] == BACKWARDS)

r = call(op="create", name="Invoice tool",
         files=[{"path": "index.html", "content": PLAIN}])
check("an ordinary create is not given a desktop it did not ask for",
      WORKSPACE[r["result"]["artifact_id"]]["index.html"] == PLAIN)

# ── 1b. an omitted name is named by the kernel, never refused ───────────────
# The schema can only mark `op` required, so the model learned at execution
# that create wanted a name and retried (2026-09-19).
r = call(op="create", description="A quiet nebula desktop — deep blues & slow drift!",
         files=[{"path": "index.html", "content": PLAIN}])
check("create with no name succeeds", r["status"] == "ok", repr(r))
check("...named from the description, inside the name rule's character class",
      LAST_CREATE["name"] == "A quiet nebula desktop deep blues slow drift" and r["result"].get("name_defaulted") is True,
      repr(LAST_CREATE["name"]))
r = call(op="create", kind="desktop")
check("create with no name and no description is named from the kind and the date",
      r["status"] == "ok" and LAST_CREATE["name"].startswith("Desktop 20") and r["result"].get("name_defaulted") is True,
      repr(LAST_CREATE["name"]))
r = call(op="create", description="Alex's dashboard — v2", files=[{"path": "index.html", "content": PLAIN}])
check("the derived name keeps an apostrophe (the name rule allows it) and drops the dash",
      LAST_CREATE["name"] == "Alex's dashboard v2", repr(LAST_CREATE["name"]))
r = call(op="create", name="Given", files=[{"path": "index.html", "content": PLAIN}])
check("a given name is kept and not marked defaulted",
      LAST_CREATE["name"] == "Given" and "name_defaulted" not in r["result"], repr(r["result"]))

# ── 2. create hears the floor — the hole that let 233 ship ──────────────────
r = call(op="create", name="Nocturne", kind="desktop",
         files=[{"path": "index.html", "content": BACKWARDS}])
c = r["result"].get("contract")
check("create attaches the floor's verdict (it attached none at all before)",
      c is not None, repr(r["result"]))
check("...and an inverted palette is REFUSED at create time",
      c and c["ok"] is False and any("--cseries-paper on --cseries-night" in e
                                     for e in c["errors"]), repr(c))
check("...with a readable report the host can act on in the same turn",
      "REFUSED" in (r["result"].get("contract_report") or ""))

# The exact 233 shape: a document that declares NOTHING, so the old
# declares-something test stayed silent on the one design that needed it.
POOL.row = None
r = call(op="create", name="Nocturne dark desktop",
         files=[{"path": "index.html", "content": PLAIN}])
c = r["result"].get("contract")
check("a document that ignores the floor still HEARS the floor, via the name",
      c is not None and c["ok"] is False, repr(r["result"]))
check("...and is told what it is missing, not just that it failed",
      any("no mode declared" in e for e in (c or {}).get("errors", []))
      and any("no design tokens" in e for e in (c or {}).get("errors", [])))

# Review item 3 (2026-09-19). A name is not a claim about the bytes. Nothing was
# refused here, so the report must not say REFUSED, and the host has to be able
# to tell WHY it is being told at all.
check("the name-only trigger says so in contract.trigger", c.get("trigger") == "name")
_rep = r["result"].get("contract_report") or ""
check("...and its report never claims a refusal",
      "REFUSED" not in _rep and _rep.startswith("Not a desktop document"), repr(_rep))
check("...while still saying what it would need, and the one-step way to get it",
      "no mode declared" in _rep and "kind='desktop'" in _rep)

r = call(op="create", name="Quiet", kind="desktop",
         files=[{"path": "index.html", "content": PLAIN}])
c = r["result"].get("contract")
check("kind='desktop' alone is enough to trigger the verdict", c.get("ok") is False)
check("...and that one IS a refusal — the host asked for a desktop",
      c.get("trigger") == "kind"
      and (r["result"].get("contract_report") or "").startswith("REFUSED"))

r = call(op="create", name="Aurora wallpaper",
         files=[{"path": "index.html", "content": PLAIN}])
check("'wallpaper' in the name triggers it too", r["result"].get("contract") is not None)
check("...as a name trigger, not a refusal",
      r["result"]["contract"].get("trigger") == "name"
      and "REFUSED" not in (r["result"].get("contract_report") or ""))

r = call(op="create", name="Nocturne", kind="desktop",
         files=[{"path": "index.html", "content": BACKWARDS}])
check("a document that DECLARES a mode reports trigger='declared', and is refused",
      r["result"]["contract"].get("trigger") == "declared"
      and (r["result"].get("contract_report") or "").startswith("REFUSED"))

# ── 3. and it stays OFF for ordinary artifacts ──────────────────────────────
POOL.row = {"name": "Invoice tool", "description": "a pricing table"}
POOL.fetchrow_calls = 0
r = call(op="create", name="Invoice tool", description="a pricing table",
         files=[{"path": "index.html", "content": PLAIN}])
check("an ordinary artifact gets no desktop verdict bolted onto it",
      "contract" not in r["result"], repr(r["result"]))
# Review item 6 (2026-09-19): on create the name and description are the call's
# own arguments, so reading the projects row back is pure waste.
check("...and create never reads back a name it was just handed",
      POOL.fetchrow_calls == 0, f"{POOL.fetchrow_calls} lookups")

r = call(op="create", name="Notes", files=[{"path": "app.js", "content": "var a=1;"}],
         entry="app.js")
check("a non-HTML entry is never run through the desktop floor",
      "contract" not in r["result"])

# ── 4. write: the name row is the last resort, and only the last resort ─────
aid = r["result"]["artifact_id"]
WORKSPACE[aid]["index.html"] = PLAIN
POOL.row = {"name": "Nocturne desktop", "description": ""}
POOL.fetchrow_calls = 0
w = call(op="write", artifact_id=aid, files=[{"path": "index.html", "content": PLAIN}])
check("a write on an artifact NAMED a desktop gets the verdict",
      w["result"].get("contract") is not None, repr(w["result"]))
check("...read from the artifact's own row", POOL.fetchrow_calls == 1)

POOL.fetchrow_calls = 0
WORKSPACE[aid]["index.html"] = BACKWARDS
w = call(op="write", artifact_id=aid, files=[{"path": "index.html", "content": BACKWARDS}])
check("a document that declares tokens needs no name lookup at all",
      POOL.fetchrow_calls == 0, f"{POOL.fetchrow_calls} lookups")
check("...and its inverted palette is reported on every write, not just the first",
      (w["result"].get("contract") or {}).get("ok") is False)

# ── 5. the verdict is a bonus; the write is the job ────────────────────────
RAISE_REPORT["on"] = True
w = call(op="write", artifact_id=aid, files=[{"path": "index.html", "content": BACKWARDS}])
check("a floor check that explodes never fails the write",
      w["status"] == "ok" and "contract" not in w["result"], repr(w))
r = call(op="create", name="Threshold", kind="desktop")
check("...nor the create, and the seed still lands",
      r["status"] == "ok"
      and WORKSPACE[r["result"]["artifact_id"]]["index.html"] == TEMPLATE_HTML)
RAISE_REPORT["on"] = False

# ── 6. the schema advertises what the handler implements ───────────────────
from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS  # noqa: E402

_props = PUBLIC_TOOL_SCHEMAS["artifact"]["function"]["parameters"]["properties"]
check("the artifact schema carries `kind`", "kind" in _props)
check("...enumerated artifact|desktop, defaulting to artifact",
      _props.get("kind", {}).get("enum") == ["artifact", "desktop"]
      and "Default 'artifact'" in _props.get("kind", {}).get("description", ""))
check("...and it stays inside the registry's per-parameter cap",
      len(_props["kind"]["description"]) <= 160,
      str(len(_props["kind"]["description"])))
_skill = (_ROOT / "default-skills" / "artifacts.md").read_text(encoding="utf-8")
check("the artifacts skill documents `kind` in its parameter notes",
      "**artifact** `kind`" in _skill)
_desk = (_ROOT / "default-skills" / "desktop.md").read_text(encoding="utf-8")
check("the desktop skill tells the host to seed the template as an op",
      "kind='desktop'" in _desk and "NO `files`" in _desk)

print()
if _FAILURES:
    print(f"{len(_FAILURES)} FAILED: {_FAILURES}")
    sys.exit(1)
print("[artifact-floor] ALL OK")
