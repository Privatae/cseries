#!/usr/bin/env python3
"""Two C-Series Hosts never collide on one bastion: a standalone Host's name
carries its install's own id.

Every C-Series Host's handle is CSERIES_URL's first label, so every default
install is "home", and a bastion files each pairing under the Host's name and
refuses a second Host of the same name. The name a standalone Host hands a
bastion is now `<handle><id><zone>` (server/network/local_bastion.py
pairing_host_name), the id being the first 8 hex digits of its Host CA's
SHA-256, which is made once per install.

Pinned here:
  1. two installs, two names; one install, one name every time
  2. the name is a desktop label in the kernel's grammar (app_origin.classify),
     so the lane's requests that carry it as their Host are not refused
  3. an EXISTING pairing keeps working: the bare `<handle><zone>` it was filed
     under stays on the lane certificate and among the names the anchor check
     accepts; the anchor check accepts the new name too, and refuses another
     install's
  4. the id unreadable: the bare name alone answers (refuses, never widens)
  5. a Host with a platform is untouched

Container-free: the CA comes from host_ca's own generator, the pool is a stub.
APP_ROOT=<repo> python3 scripts/test_host_names_unique.py
"""
from __future__ import annotations

import asyncio
import os
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]
os.environ["CSERIES_URL"] = "http://home.cseries.localhost:8800"

from server.applications import app_origin  # noqa: E402
from server.home_origin import HOME  # noqa: E402
from server.network import bastion_enroll, host_ca, local_bastion  # noqa: E402
from server.network import pairing_grants as pg  # noqa: E402

LEGACY = f"{HOME.handle}{HOME.zone}"


class CAPool:
    """kernel_secrets as one install holds it (host_ca._stored)."""

    def __init__(self):
        self.secrets: dict = {}

    def acquire(self):
        pool = self

        class _Cx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def execute(self, q, *a):
                if q.lstrip().startswith("INSERT INTO kernel_secrets"):
                    pool.secrets.setdefault(a[0], a[1])

            async def fetchrow(self, q, *a):
                v = pool.secrets.get(a[0])
                return None if v is None else {"secret": v}
        return _Cx()


def _run(coro):
    return asyncio.run(coro)


async def _name(pool):
    return await local_bastion.host_name(1, pool)


def standalone_host(fn):
    """Run `fn` as a Host with no platform (its handle is CSERIES_URL's)."""
    def run():
        real = local_bastion.standalone
        local_bastion.standalone = lambda: True
        try:
            fn()
        finally:
            local_bastion.standalone = real
    run.__name__ = fn.__name__
    return run


@standalone_host
def test_two_installs_two_names_one_install_one_name():
    a, b = CAPool(), CAPool()
    na, nb = _run(_name(a)), _run(_name(b))
    assert na != nb, "two default Hosts must not share a name"
    assert na == _run(_name(a)) == _run(_name(a)), "the name is stable for one install"
    for n, pool in ((na, a), (nb, b)):
        hid = _run(local_bastion.host_id(pool))
        assert len(hid) == local_bastion.HOST_ID_LEN and int(hid, 16) >= 0
        assert n == f"{HOME.handle}{hid}{HOME.zone}" and n != LEGACY
        # the id IS the CA a bastion pins (host_ca.ca_sha256), its first digits
        assert host_ca.ca_sha256(_run(host_ca.ca_cert_pem(pool))).startswith(hid)


@standalone_host
def test_the_name_is_a_desktop_label_so_the_lane_is_not_refused():
    n = _run(_name(CAPool()))
    o = app_origin.classify(n)
    assert o.kind == app_origin.DESKTOP, (n, o)
    # what a separator would have done: the guard refuses it as an app address
    assert app_origin.classify(f"{HOME.handle}-abcd1234{HOME.zone}").kind == app_origin.BAD_APP


@standalone_host
def test_the_lane_keeps_the_bare_name_for_an_existing_pairing():
    pool = CAPool()
    names = _run(local_bastion.lane_names(1, pool))
    assert names[0] == _run(_name(pool)), "the new name is the one a new pairing checks"
    assert LEGACY in names[1:], "an existing pairing's ServerName stays on the certificate"


class _M:
    def __init__(self):
        self.undo = []

    def set(self, obj, name, value):
        self.undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore(self):
        for obj, name, old in reversed(self.undo):
            setattr(obj, name, old)


def _standalone(m, pool, *, on=True, id_ok=True):
    m.set(local_bastion, "standalone", lambda: on)
    m.set(local_bastion, "_control_pool", lambda: pool if id_ok else None)


def test_the_anchor_check_accepts_both_names_and_refuses_another_install():
    m = _M()
    try:
        mine, other = CAPool(), CAPool()
        _standalone(m, mine)
        own = _run(pg._own_host_names(1))
        new = _run(_name(mine))
        assert own[0] == new and LEGACY in own, own
        assert _run(_name(other)) not in own, "another install's name is not this Host"
    finally:
        m.restore()


def test_the_id_unreadable_answers_with_the_bare_name_only():
    m = _M()
    try:
        _standalone(m, CAPool(), id_ok=False)
        assert _run(pg._own_host_names(1)) == (LEGACY,)
    finally:
        m.restore()


def test_a_host_with_a_platform_is_untouched():
    m = _M()
    try:
        _standalone(m, CAPool(), on=False)

        async def _handle(eid):
            return "a1b2c3d4e5f60718"
        m.set(bastion_enroll, "resolve_self_handle", _handle)
        assert _run(pg._own_host_names(1)) == bastion_enroll._host_names("a1b2c3d4e5f60718")
    finally:
        m.restore()


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("OK   " + name)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name} -- {type(e).__name__}: {e}")
    print(f"\nFAILURES: {failed} / {len(tests)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
