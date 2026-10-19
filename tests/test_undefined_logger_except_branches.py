"""Three except branches that named a logger their module does not define.

server/main.py defines `logger`; its boot-time desktop snapshot branch called
`log`. server/routes/applications.py defines `log`; its two circle-publication
bookkeeping branches called `logger`. Each branch exists to log a non-fatal
failure and carry on. Instead it raised NameError: boot lost the rest of its
startup, and a publish the store had already accepted answered the publisher
with a 500.

Each test drives the real branch with the failure it exists for, and asserts
it logs and returns. Stand-ins replace only what the branch's function calls
out to: the store, the licence gate, the forge, the publication record.
"""
from __future__ import annotations

import asyncio
import logging
import pathlib
import sys
import types

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]

from server.routes import applications as A  # noqa: E402
from server.applications import appstore_circle  # noqa: E402
from server.applications import build_contract  # noqa: E402
from server.applications import licensing  # noqa: E402
from server.routes import project_forge  # noqa: E402

APP_ID = "app.test.circle"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _record_fails(*a, **k):
    raise RuntimeError("publication table unreachable")


@pytest.fixture
def circle_publish(monkeypatch):
    """Everything up to the store accepting a circle publish succeeds; the
    local publication record after it fails."""
    async def _no_errors(*a, **k):
        return []

    async def _publishable(*a, **k):
        return None

    monkeypatch.setattr(build_contract, "validate_publish", lambda raw: [])
    monkeypatch.setattr(licensing, "publish_gate", _no_errors)
    monkeypatch.setattr(A.bundles, "_read_zip",
                        lambda raw: types.SimpleNamespace(payload={}, binaries={}))
    monkeypatch.setattr(A, "_declared_app_id_in_zip", lambda raw: APP_ID)
    monkeypatch.setattr(appstore_circle, "check_publishable", _publishable)
    monkeypatch.setattr(appstore_circle, "record_publication", _record_fails)


def _assert_logged(caplog, name):
    hits = [r for r in caplog.records
            if r.name == name and "circle publication record failed" in r.getMessage()]
    assert hits, [(r.name, r.getMessage()) for r in caplog.records]
    assert APP_ID in hits[0].getMessage() and "unreachable" in hits[0].getMessage()


def test_the_publisher_upload_logs_a_failed_record_and_returns_the_store_answer(
        monkeypatch, caplog, circle_publish):
    accepted = {"request_id": "req-1", "status": "pending_review"}

    async def _store(op, params):
        assert op == "appstore.publish_bundle" and params["group_ref"] == "g-1"
        return accepted

    monkeypatch.setattr(A, "_publisher_call", _store)
    monkeypatch.setattr(A.brain_service, "get_pool", lambda: object())

    class _Upload:
        async def read(self):
            return b"PK-not-really-a-zip"

    class _Request:
        async def form(self):
            return {"file": _Upload(), "audience": "circle", "group_ref": "g-1"}

    caplog.set_level(logging.WARNING)
    out = _run(A.publisher_publish(_Request()))
    assert out == accepted
    _assert_logged(caplog, A.log.name)


def test_publishing_a_project_logs_a_failed_record_and_returns_the_store_answer(
        monkeypatch, caplog, circle_publish):
    accepted = {"request_id": "req-2", "status": "pending_review"}

    async def _pack(pool, project_id, module, kinds, verb):
        return {"signed": b"PK-signed", "open_phases": 2, "entity_id": 1, "manifest": None}

    async def _forge(pool, project_id):
        return types.SimpleNamespace(repo=None, on_machine=False)

    async def _store(op, params):
        assert op == "appstore.publish_bundle" and params["group_ref"] == "g-2"
        return dict(accepted)

    class _Pool:
        async def fetchrow(self, *a, **k):
            return None

    monkeypatch.setattr(A, "_pack_and_sign_project", _pack)
    monkeypatch.setattr(project_forge, "project_forge", _forge)
    monkeypatch.setattr(A, "publisher_op_service", _store)

    caplog.set_level(logging.WARNING)
    out = _run(A.publish_project_service(_Pool(), 7, audience="circle", group_ref="g-2"))
    assert out == {**accepted, "open_phases": 2}
    _assert_logged(caplog, A.log.name)


def _main(monkeypatch):
    # server.main mounts StaticFiles over the container's directories at
    # import; the same switch scripts/test_api_route_guards.py uses.
    import starlette.staticfiles as _sf
    orig = _sf.StaticFiles.__init__

    def _init(self, *a, **k):
        k["check_dir"] = False
        return orig(self, *a, **k)
    monkeypatch.setattr(_sf.StaticFiles, "__init__", _init)
    from server import main as M
    return M


def test_boot_logs_a_desktop_snapshot_it_could_not_schedule_and_carries_on(monkeypatch, caplog):
    M = _main(monkeypatch)
    from server.routes import appearance

    def _fails():
        raise RuntimeError("appearance store not ready")

    monkeypatch.setattr(appearance, "publish_desktop_snapshot_at_boot", _fails)
    caplog.set_level(logging.WARNING)

    async def _boot():
        return M._schedule_desktop_snapshot()

    assert _run(_boot()) is None
    hits = [r for r in caplog.records
            if r.name == M.logger.name and "desktop snapshot boot task not scheduled" in r.getMessage()]
    assert hits and "not ready" in hits[0].getMessage(), \
        [(r.name, r.getMessage()) for r in caplog.records]


def test_boot_schedules_the_desktop_snapshot_when_it_can(monkeypatch):
    M = _main(monkeypatch)
    from server.routes import appearance
    ran = []

    async def _publish():
        ran.append(1)

    monkeypatch.setattr(appearance, "publish_desktop_snapshot_at_boot", _publish)

    async def _boot():
        task = M._schedule_desktop_snapshot()
        await task
        return task

    task = _run(_boot())
    assert task is not None and task.done() and ran == [1]
