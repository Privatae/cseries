"""The Designs shelf (server/routes/designs.py, 2026-09-28) — against a stubbed
pool, a stubbed bridge and a temp projects root. No network, no pod.

What this pins:
  - "Yours" lists every desktop made on this host from the artifact's own
    static head (a `cseries-desktop` meta), and ONLY those — an ordinary
    artifact is not a design;
  - an unreachable library never blanks the screen: the host's own desktops
    still list, and the error is named;
  - going back restores the ordinary wallpaper swatch the user had, and a
    wallpaper key that is not a real swatch is refused rather than written
    (`previous` round-trips through the client);
  - trying a second design drops the FIRST trial copy, never the one being
    worn;
  - publishing checks the floor before anything reaches the bridge (and
    before the browser render check), and the request carries no shelf —
    the platform decides it.

Run: pytest tests/test_designs_shelf.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from server import brain_service  # noqa: E402
from server.applications import artifacts as artifact_service  # noqa: E402
from server.applications.artifacts import ArtifactError  # noqa: E402
from server.applications.desktop_contract import TEMPLATE_HTML  # noqa: E402
from server.routes import designs as D  # noqa: E402

EID = 9003


class Pool:
    def __init__(self, artifacts=(), appearance=None):
        self.artifacts = list(artifacts)   # dicts: id, name, description, updated_at
        self.appearance = appearance       # dict: wallpaper, desktop_artifact_id, desktop_entry
        self.executed = []

    async def fetch(self, sql, *a):
        assert "kind='artifact'" in sql
        return list(self.artifacts)

    async def fetchval(self, sql, *a):
        return "Sage"

    async def execute(self, sql, *a):
        self.executed.append((sql, a))
        return "UPDATE 1"


@pytest.fixture(autouse=True)
def entity(monkeypatch):
    monkeypatch.setattr(brain_service, "_entity_id", EID)


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(artifact_service, "_projects_root", lambda: tmp_path)
    from server.applications import desktop_render
    monkeypatch.setattr(desktop_render, "state_dir", lambda: tmp_path / ".kernel-state")
    return tmp_path


def _art(root, aid, html, name):
    (root / str(aid)).mkdir()
    (root / str(aid) / "index.html").write_text(html)
    return {"id": aid, "name": name, "description": "", "updated_at": None}


def test_local_desktops_are_the_artifacts_that_declare_a_desktop(root):
    pool = Pool([
        _art(root, 249, TEMPLATE_HTML, "Slate Mist"),
        _art(root, 250, "<html><head><title>a chart</title></head></html>", "Sales chart"),
        _art(root, 251, '<!DOCTYPE html><head><meta name="cseries-desktop" content="wallpaper"></head>', "Dusk"),
    ])
    got = asyncio.run(D.local_desktops(pool))
    assert [g["artifact_id"] for g in got] == [249, 251]
    assert got[0]["mode"] == "desktop" and got[0]["tokens"]["--cseries-ink"]
    assert got[1]["mode"] == "wallpaper"


def test_an_unreachable_library_still_lists_what_the_host_made(root, monkeypatch):
    pool = Pool([_art(root, 249, TEMPLATE_HTML, "Slate Mist")])
    monkeypatch.setattr(D, "_pool", lambda: pool)

    async def down(op, params):
        raise HTTPException(503, "the design library is not reachable yet")

    async def wearing(_pool):
        return {"wallpaper": "default", "artifact_id": None, "entry": None}

    monkeypatch.setattr(D, "_bridge", down)
    monkeypatch.setattr(D, "_wearing", wearing)
    out = asyncio.run(D.list_designs())
    # Only the house designs built into the image are left on the shelf.
    assert [d["id"] for d in out["designs"]] == [d["id"] for d in D.builtin_designs()]
    assert all(d.get("builtin") for d in out["designs"]) and out["library_error"]
    assert [d["artifact_id"] for d in out["local"]] == [249]
    assert out["host"] == "Sage"


def test_go_back_restores_the_swatch_and_refuses_an_unknown_one(monkeypatch):
    pool = Pool()
    cleared = []

    async def set_desktop(p, eid, aid, entry="index.html"):
        cleared.append(aid)
        return {"wallpaper": "default", "desktop_artifact_id": None}

    import server.routes.appearance as A
    monkeypatch.setattr(A, "set_desktop_artifact", set_desktop)
    out = asyncio.run(D._restore(pool, {"wallpaper": "aurora"}))
    assert cleared == [None] and out["wallpaper"] == "aurora"
    assert pool.executed and pool.executed[-1][1] == (EID, "aurora")

    pool2 = Pool()
    with pytest.raises(ArtifactError) as e:
        asyncio.run(D._restore(pool2, {"wallpaper": "url(evil)"}))
    assert e.value.status == 400 and not pool2.executed


def test_go_back_to_an_html_desktop_points_at_it_again(monkeypatch):
    seen = []

    async def set_desktop(p, eid, aid, entry="index.html", *, render_check=True):
        seen.append((aid, entry, render_check))
        return {"wallpaper": "html", "desktop_artifact_id": aid}

    import server.routes.appearance as A
    monkeypatch.setattr(A, "set_desktop_artifact", set_desktop)
    asyncio.run(D._restore(Pool(), {"wallpaper": "html", "artifact_id": 236, "entry": "index.html"}))
    # Going back never waits on the browser render check: the desktop was on
    # screen a moment ago, and "Go back" must work with no browser awake.
    assert seen == [(236, "index.html", False)]


def _phone_writes(pool):
    return [a for sql, a in pool.executed if "SET wallpaper_mobile" in sql]


def test_go_back_puts_the_phones_own_wallpaper_back(monkeypatch):
    # Trying a design moves the phone onto it (appearance.set_desktop_artifact
    # sets wallpaper_mobile NULL); Go back must give the phone back what it had.
    async def set_desktop(p, eid, aid, entry="index.html", *, render_check=True):
        return {"wallpaper": "html" if aid else "default", "desktop_artifact_id": aid}

    import server.routes.appearance as A
    monkeypatch.setattr(A, "set_desktop_artifact", set_desktop)

    pool = Pool()
    out = asyncio.run(D._restore(pool, {"wallpaper": "html", "artifact_id": 236, "entry": "index.html",
                                        "wallpaper_mobile": "custom"}))
    assert _phone_writes(pool) == [(EID, "custom")] and out["wallpaper_mobile"] == "custom"

    pool = Pool()
    asyncio.run(D._restore(pool, {"wallpaper": "aurora", "wallpaper_mobile": "ice"}))
    assert _phone_writes(pool) == [(EID, "ice")]

    # "Same as desktop" is a choice too: NULL is written back as NULL.
    pool = Pool()
    asyncio.run(D._restore(pool, {"wallpaper": "html", "artifact_id": 236, "entry": "index.html",
                                  "wallpaper_mobile": None}))
    assert _phone_writes(pool) == [(EID, None)]


def test_what_is_worn_includes_the_phones_own_wallpaper(monkeypatch):
    row = {"wallpaper": "html", "wallpaper_mobile": "custom",
           "desktop_artifact_id": 249, "desktop_entry": "index.html"}

    class Conn:
        async def fetchrow(self, sql, *a):
            return row

    class Acquire:
        async def __aenter__(self):
            return Conn()

        async def __aexit__(self, *a):
            return False

    class APool:
        def acquire(self):
            return Acquire()

    async def pointer(conn, eid, r):
        return {"artifact_id": r["desktop_artifact_id"], "entry": r["desktop_entry"]}

    import server.routes.appearance as A
    monkeypatch.setattr(A, "_desktop_pointer", pointer)
    got = asyncio.run(D._wearing(APool()))
    assert got == {"wallpaper": "html", "artifact_id": 249, "entry": "index.html",
                   "wallpaper_mobile": "custom"}
    row.update(wallpaper="ice", wallpaper_mobile=None)
    got = asyncio.run(D._wearing(APool()))
    assert got["wallpaper"] == "ice" and "wallpaper_mobile" in got and got["wallpaper_mobile"] is None


class PhonePool(Pool):
    """Holds the phone's own wallpaper the way entity_appearance does."""
    def __init__(self, mobile):
        super().__init__()
        self.mobile = mobile

    async def fetchval(self, sql, *a):
        assert "wallpaper_mobile" in sql
        return self.mobile

    async def execute(self, sql, *a):
        if "SET wallpaper_mobile" in sql:
            self.mobile = a[1]
        return await super().execute(sql, *a)


def _wear_moves_the_phone(monkeypatch):
    # The real wear (appearance.set_desktop_artifact) sets wallpaper_mobile
    # NULL; clearing the pointer leaves it alone.
    async def set_desktop(p, eid, aid, entry="index.html", *, render_check=True):
        if aid is not None:
            p.mobile = None
        return {"wallpaper": "html" if aid else "default", "desktop_artifact_id": aid}

    import server.routes.appearance as A
    monkeypatch.setattr(A, "set_desktop_artifact", set_desktop)


def test_go_back_from_a_try_recorded_before_the_phone_leaves_the_phone_alone(monkeypatch):
    # A try recorded before this release has no wallpaper_mobile, and that try
    # never moved the phone. Going back to an HTML desktop wears it again —
    # which now moves the phone — so the phone must come back as it was.
    _wear_moves_the_phone(monkeypatch)
    pool = PhonePool("custom")
    asyncio.run(D._restore(pool, {"wallpaper": "html", "artifact_id": 236, "entry": "index.html"}))
    assert pool.mobile == "custom"
    # The swatch path never touched the phone, and still does not.
    pool = PhonePool("ice")
    asyncio.run(D._restore(pool, {"wallpaper": "aurora"}))
    assert pool.mobile == "ice" and _phone_writes(pool) == []


def test_a_try_on_top_of_a_try_recorded_before_the_phone_keeps_the_phone(root):
    # The first try was recorded before the phone was (no key) and left the
    # phone alone; the second try is recorded now, with the phone it replaces.
    D._record_try({"wallpaper": "ice", "artifact_id": None, "entry": None}, 300)
    D._record_try({"wallpaper": "html", "artifact_id": 300, "entry": "index.html",
                   "wallpaper_mobile": "custom"}, 301)
    rec = D._try_load()
    assert rec["trying"] == 301
    assert rec["previous"] == {"wallpaper": "ice", "artifact_id": None, "entry": None,
                               "wallpaper_mobile": "custom"}
    # Recorded with the phone: the FIRST try's phone wins, as before.
    D._try_clear()
    D._record_try({"wallpaper": "ice", "artifact_id": None, "entry": None,
                   "wallpaper_mobile": "aurora"}, 300)
    D._record_try({"wallpaper": "html", "artifact_id": 300, "entry": "index.html",
                   "wallpaper_mobile": None}, 301)
    assert D._try_load()["previous"]["wallpaper_mobile"] == "aurora"


def test_go_back_never_writes_a_phone_value_it_cannot_show(monkeypatch):
    async def set_desktop(p, eid, aid, entry="index.html", *, render_check=True):
        return {"wallpaper": "default", "desktop_artifact_id": aid}

    import server.routes.appearance as A
    monkeypatch.setattr(A, "set_desktop_artifact", set_desktop)
    # A record made before the phone was recorded: the phone is left alone.
    pool = Pool()
    asyncio.run(D._restore(pool, {"wallpaper": "aurora"}))
    assert _phone_writes(pool) == []
    # Not a real swatch key.
    pool = Pool()
    asyncio.run(D._restore(pool, {"wallpaper": "aurora", "wallpaper_mobile": "url(evil)"}))
    assert _phone_writes(pool) == []
    # The Host design on the phone, but going back clears the HTML desktop:
    # there would be nothing for the phone to show.
    pool = Pool()
    asyncio.run(D._restore(pool, {"wallpaper": "aurora", "wallpaper_mobile": "html"}))
    assert _phone_writes(pool) == []


@pytest.fixture(autouse=True)
def notified(monkeypatch):
    """Every appearance_changed push, in order (the real one broadcasts to
    open desktops through server.main)."""
    import server.routes.appearance as A
    out = []

    async def notify():
        out.append("notify")

    monkeypatch.setattr(A, "_notify_shell", notify)
    return out


class PointerPool(Pool):
    """Pool + acquire(): the artifacts that exist, and every write in order."""
    def __init__(self, existing=(249,)):
        super().__init__()
        self.existing = set(existing)
        self.order = []

    def acquire(self):
        pool = self

        class Conn:
            async def fetchval(self, sql, *a):
                assert "FROM projects" in sql and "kind='artifact'" in sql
                return 1 if a[0] in pool.existing else None

            async def execute(self, sql, *a):
                pool.order.append(("pointer", a))
                pool.executed.append((sql, a))

        class Acquire:
            async def __aenter__(self):
                return Conn()

            async def __aexit__(self, *a):
                return False

        return Acquire()

    async def execute(self, sql, *a):
        self.order.append(("execute", a))
        return await super().execute(sql, *a)


def test_what_is_worn_records_the_design_the_phone_wears_under_a_desktop_swatch(monkeypatch):
    row = {"wallpaper": "aurora", "wallpaper_mobile": "html",
           "desktop_artifact_id": 249, "desktop_entry": "index.html"}

    class Conn:
        async def fetchrow(self, sql, *a):
            return row

    class Acquire:
        async def __aenter__(self):
            return Conn()

        async def __aexit__(self, *a):
            return False

    class APool:
        def acquire(self):
            return Acquire()

    async def pointer(conn, eid, r):
        return {"artifact_id": r["desktop_artifact_id"], "entry": r["desktop_entry"]}

    import server.routes.appearance as A
    monkeypatch.setattr(A, "_desktop_pointer", pointer)
    got = asyncio.run(D._wearing(APool()))
    # Not "worn on the desktop" (artifact_id stays None: the try-on-a-try and
    # discard rules read it), but kept for the phone.
    assert got == {"wallpaper": "aurora", "artifact_id": None, "entry": None,
                   "wallpaper_mobile": "html", "phone_artifact_id": 249,
                   "phone_entry": "index.html"}


def test_go_back_gives_the_phone_its_host_design_back_under_a_desktop_swatch(monkeypatch, notified):
    # Desktop on "aurora", phone on the Host design (artifact 249). A try
    # replaced both; going back clears the trial's pointer, and before this the
    # phone was left with nothing to show.
    _wear_moves_the_phone(monkeypatch)
    pool = PointerPool(existing={249})
    pool.mobile = None
    out = asyncio.run(D._restore(pool, {"wallpaper": "aurora", "artifact_id": None, "entry": None,
                                        "wallpaper_mobile": "html", "phone_artifact_id": 249,
                                        "phone_entry": "index.html"}))
    pointer = [a for kind, a in pool.order if kind == "pointer"]
    assert pointer == [(EID, 249, "index.html")]
    assert _phone_writes(pool) == [(EID, "html")] and out["wallpaper_mobile"] == "html"
    assert out["wallpaper"] == "aurora"


def test_go_back_republishes_the_tile_once_the_phone_design_is_pointed_at_again(monkeypatch):
    # Clearing the trial's pointer publishes the Host Manager tile with no
    # design (set_desktop_artifact(None)); the tile draws the pointer whatever
    # the wallpaper, so once the phone's design is pointed at again the tile
    # must be published again — after that write, not before.
    _wear_moves_the_phone(monkeypatch)
    import server.routes.appearance as A
    pool = PointerPool(existing={249})
    pool.mobile = None

    async def publish(p, *, reason="change"):
        pool.order.append(("publish", reason))
        return True

    monkeypatch.setattr(A, "publish_desktop_snapshot", publish)
    asyncio.run(D._restore(pool, {"wallpaper": "aurora", "artifact_id": None, "entry": None,
                                  "wallpaper_mobile": "html", "phone_artifact_id": 249,
                                  "phone_entry": "index.html"}))
    kinds = [k for k, _a in pool.order]
    assert "publish" in kinds and kinds.index("publish") > kinds.index("pointer"), pool.order
    # No design to point at: nothing new to publish.
    pool = PointerPool(existing=set())
    pool.mobile = None
    asyncio.run(D._restore(pool, {"wallpaper": "aurora", "wallpaper_mobile": "html",
                                  "phone_artifact_id": 249, "phone_entry": "index.html"}))
    assert all(k != "publish" for k, _a in pool.order), pool.order


def test_go_back_does_not_point_the_phone_at_a_design_that_is_gone(monkeypatch):
    _wear_moves_the_phone(monkeypatch)
    pool = PointerPool(existing=set())
    pool.mobile = None
    asyncio.run(D._restore(pool, {"wallpaper": "aurora", "wallpaper_mobile": "html",
                                  "phone_artifact_id": 249, "phone_entry": "index.html"}))
    assert [a for kind, a in pool.order if kind == "pointer"] == []
    assert _phone_writes(pool) == []


def test_go_back_tells_the_desktop_after_every_write_has_landed(monkeypatch, notified):
    # set_desktop_artifact pushes appearance_changed after its own write; the
    # swatch and the phone are written after it, so a desktop that re-read on
    # that push kept the trial's wallpaper. One more push, after the last write.
    seen = []

    async def set_desktop(p, eid, aid, entry="index.html", *, render_check=True):
        seen.append("set")
        return {"wallpaper": "default", "desktop_artifact_id": aid}

    import server.routes.appearance as A
    monkeypatch.setattr(A, "set_desktop_artifact", set_desktop)

    class OrderPool(Pool):
        async def execute(self, sql, *a):
            notified.append("write")
            return await super().execute(sql, *a)

    asyncio.run(D._restore(OrderPool(), {"wallpaper": "aurora", "wallpaper_mobile": "ice"}))
    assert notified[-1] == "notify" and notified.count("write") == 2, notified
    notified.clear()
    asyncio.run(D._restore(OrderPool(), {"wallpaper": "html", "artifact_id": 236,
                                         "entry": "index.html", "wallpaper_mobile": "custom"}))
    assert notified == ["write", "notify"], notified


class Req:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


def _trial(root, aid, revisions=1, marked=True):
    ws = root / str(aid)
    (ws / ".history").mkdir(parents=True, exist_ok=True)
    (ws / "index.html").write_text(TEMPLATE_HTML)
    (ws / ".history" / "log.jsonl").write_text(
        "".join('{"rev": "%04d"}\n' % (i + 1) for i in range(revisions)))
    if marked:
        D._mark_trial(aid, 7)


@pytest.fixture
def deleted(monkeypatch):
    out = []

    async def delete(pool, eid, aid):
        out.append(aid)
        return {"deleted": True}

    monkeypatch.setattr(artifact_service, "delete_artifact", delete)
    return out


def test_a_second_try_drops_the_first_trial_copy_never_the_worn_one(root, deleted):
    _trial(root, 301)
    _trial(root, 302)
    # 301 was on screen; trying 302 replaces it.
    assert asyncio.run(D._discard_trial(Pool(), {"discard_artifact_id": 301}, 302, 301)) is True
    assert asyncio.run(D._discard_trial(Pool(), {"discard_artifact_id": 302}, 302, 302)) is False
    assert asyncio.run(D._discard_trial(Pool(), {}, 302, 301)) is False
    assert deleted == [301]


def test_an_id_that_is_not_a_trial_copy_is_never_deleted(root, deleted):
    """Review B1: write:appearance is a scope a store app may hold, and the id
    comes from the client — so an ordinary artifact, an EDITED trial copy and
    a KEPT one must all survive a discard request."""
    _art(root, 400, TEMPLATE_HTML, "someone's desktop")      # never a trial
    _trial(root, 401, revisions=2)                            # edited, with a revision
    _trial(root, 402)
    asyncio.run(D.keep(Req({"artifact_id": 402})))            # kept
    _trial(root, 403)
    (root / "403" / "index.html").write_text("<!-- edited through the project file lane, no revision -->")
    for aid in (400, 401, 402, 403, 999):
        # Even named as the desktop that was on screen, none of these may go.
        assert asyncio.run(D._discard_trial(Pool(), {"discard_artifact_id": aid}, 1, aid)) is False
    assert deleted == []


def test_a_trial_that_was_not_on_screen_is_not_discarded(root, deleted):
    """A copy the user kept wearing and then switched away from is no longer a
    trial anyone is undoing — a stale Go back, or any other caller, leaves it."""
    _trial(root, 450)
    assert asyncio.run(D._discard_trial(Pool(), {"discard_artifact_id": 450}, 1, 999)) is False
    assert asyncio.run(D._discard_trial(Pool(), {"discard_artifact_id": 450}, 1, None)) is False
    assert deleted == []


def test_restore_discards_only_a_trial_that_is_not_worn(root, deleted, monkeypatch):
    _trial(root, 501)
    _art(root, 502, TEMPLATE_HTML, "not a trial")

    async def restore(pool, previous):
        return {"wallpaper": "default"}

    async def wearing(pool):
        return {"wallpaper": "html", "artifact_id": on_screen[0], "entry": "index.html"}

    on_screen = [502]
    monkeypatch.setattr(D, "_restore", restore)
    monkeypatch.setattr(D, "_wearing", wearing)
    monkeypatch.setattr(D, "_pool", lambda: Pool())
    D._record_try({"wallpaper": "default", "artifact_id": None, "entry": None}, 502)
    out = asyncio.run(D.restore(Req({"discard_artifact_id": 502})))
    assert out["discarded"] is False and deleted == []
    on_screen[0] = 501
    D._record_try({"wallpaper": "default", "artifact_id": None, "entry": None}, 501)
    out = asyncio.run(D.restore(Req({"discard_artifact_id": 501})))
    assert out["discarded"] is True and deleted == [501]


def test_go_back_never_discards_the_design_the_phone_was_put_back_on(root, deleted, monkeypatch):
    # The phone wore trial copy 501 while the desktop wore a swatch; 501 was
    # then tried on the desktop too. Go back points the phone at 501 again, so
    # deleting it afterwards would leave the phone on a design that is gone.
    _trial(root, 501)

    async def restore(pool, previous):
        return {"wallpaper": "aurora", "wallpaper_mobile": "html"}

    async def wearing(pool):
        return {"wallpaper": "html", "artifact_id": 501, "entry": "index.html"}

    monkeypatch.setattr(D, "_restore", restore)
    monkeypatch.setattr(D, "_wearing", wearing)
    monkeypatch.setattr(D, "_pool", lambda: Pool())
    D._record_try({"wallpaper": "aurora", "artifact_id": None, "entry": None,
                   "wallpaper_mobile": "html", "phone_artifact_id": 501,
                   "phone_entry": "index.html"}, 501)
    out = asyncio.run(D.restore(Req({"discard_artifact_id": 501})))
    assert out["discarded"] is False and deleted == []


def test_go_back_restores_only_the_wear_the_server_recorded(root, monkeypatch):
    """Review: Go back wears WITHOUT a render check, so it must never wear an
    artifact the client names. It restores the desktop the server saw worn
    when the try began, and only while that try is still on screen."""
    put_back: list = []

    async def restore(pool, previous):
        put_back.append(previous)
        return {"wallpaper": previous.get("wallpaper")}

    on_screen = [701]

    async def wearing(pool):
        return {"wallpaper": "html", "artifact_id": on_screen[0], "entry": "index.html"}

    monkeypatch.setattr(D, "_restore", restore)
    monkeypatch.setattr(D, "_wearing", wearing)
    monkeypatch.setattr(D, "_pool", lambda: Pool())
    D._try_clear()
    with pytest.raises(HTTPException) as e:          # nothing was tried
        asyncio.run(D.restore(Req({"previous": {"wallpaper": "html", "artifact_id": 666}})))
    assert e.value.status_code == 409 and put_back == []

    original = {"wallpaper": "html", "artifact_id": 700, "entry": "index.html"}
    D._record_try(original, 701)
    # A second try on top of the first keeps the ORIGINAL desktop to go back to.
    D._record_try({"wallpaper": "html", "artifact_id": 701, "entry": "index.html"}, 702)
    on_screen[0] = 702
    asyncio.run(D.restore(Req({"previous": {"wallpaper": "html", "artifact_id": 666}})))
    assert put_back == [original]          # the client's 666 was never worn
    with pytest.raises(HTTPException) as e:          # one Go back per try
        asyncio.run(D.restore(Req({})))
    assert e.value.status_code == 409

    D._record_try(original, 703)
    on_screen[0] = 704                     # the desktop changed since the try
    with pytest.raises(HTTPException) as e:
        asyncio.run(D.restore(Req({})))
    assert e.value.status_code == 409 and put_back == [original]

    D._record_try(original, 705)
    asyncio.run(D.keep(Req({"artifact_id": 705})))
    on_screen[0] = 705
    with pytest.raises(HTTPException) as e:          # kept: nothing to go back from
        asyncio.run(D.restore(Req({})))
    assert e.value.status_code == 409


def test_the_local_routes_serve_desktops_only(root, monkeypatch):
    """Review B1: /local/{id}/document must not read an arbitrary artifact."""
    _art(root, 600, "<html><head><title>notes</title></head><body>private</body></html>", "Notes")
    monkeypatch.setattr(D, "_pool", lambda: Pool())
    with pytest.raises(HTTPException) as e:
        asyncio.run(D.local_document(600))
    assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        asyncio.run(D.publish_local(600, Req({"name": "x"})))
    assert e.value.status_code == 404


def test_publish_checks_the_floor_first_and_never_names_a_shelf(monkeypatch):
    sent = []

    async def bridge(op, params):
        sent.append((op, params))
        return {"published": True, "design_id": 9, "shelf": "house"}

    async def installed(_pool):
        return frozenset()

    rendered = []

    async def render_ok(pool, html):
        # The browser render check (server/applications/desktop_render.py);
        # its doors are pinned in scripts/test_desktop_render_doors.py.
        rendered.append(html)
        return {"checked": True, "ok": True, "why": "rendered", "failures": []}

    monkeypatch.setattr(D, "_bridge", bridge)
    monkeypatch.setattr(D, "_installed_ids", installed)
    monkeypatch.setattr(D.desktop_render, "render_check", render_ok)
    with pytest.raises(HTTPException) as e:
        asyncio.run(D.publish_document(Pool(), "Stub", "<html><body>no mode, no tokens</body></html>"))
    assert e.value.status_code == 422 and sent == [] and rendered == []

    out = asyncio.run(D.publish_document(Pool(), "Threshold", TEMPLATE_HTML))
    assert out["shelf"] == "house" and out["name"] == "Threshold"
    op, params = sent[-1]
    assert op == "design.publish" and "shelf" not in params
    assert params["author_name"] == "Sage" and params["tokens"]["--cseries-ink"]


def test_a_name_that_cannot_be_installed_is_refused_at_publish(monkeypatch):
    with pytest.raises(HTTPException) as e:
        asyncio.run(D.publish_document(Pool(), "a/b", TEMPLATE_HTML))
    assert e.value.status_code == 400
