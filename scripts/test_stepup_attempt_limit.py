#!/usr/bin/env python3
"""The second factor's attempt limit (server/network/nac_stepup.py verify_stepup).

A six-digit code is right about 3 times in a million, so whatever can submit
codes must not be able to submit many: after STEPUP_FREE_FAILURES wrong codes
in a row each further try waits (doubling to STEPUP_LOCK_MAX_S), a try inside
the wait is not looked at and not counted, and a right code clears the count.

And the limit must not become a way to lock the owner out:
  - a missing or malformed code is not an attempt and is never counted;
  - codes tried by the Host (its tool calls, its kernel-op token) count on the
    Host's own record, never the owner's, so the Host can hold up only itself;
  - codes sent with any other app's token count on that app's own record and
    on one record all such apps share, except the owner's own windows as the
    platform signed them, so no app token can hold up the owner, and holding
    many app tokens gives no more guesses than holding one (a page on the
    shell's own origin can still act as the owner: nac_stepup.py says how);
  - one app alone cannot keep the other apps waiting, and the apps' records
    start again after a quiet day, without more guesses in any day;
  - a last failure dated in the future holds a record for one wait, once;
  - a record that cannot be read waits once and is repaired, never for ever;
  - every route that asks for a code answers the same 429 with the wait.

The first section is a repro: the Host's agent calling approve_nac_draft with
no code each time the lock lapsed refused the owner's right code 143 times out
of 144 in a day. It fails on the code before the limit.

The database is a stand-in for kernel_secrets; the real table and locks are
exercised by scripts/check_nac_user_gate.py inside a Host.

    python3 scripts/test_stepup_attempt_limit.py
"""
import ast
import asyncio
import contextlib
import inspect
import os
import sys

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "brain"))

from server.network import nac_stepup as S  # noqa: E402

FAILED = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  [{detail}]"))
    if not cond:
        FAILED.append(label)


class Conn:
    def __init__(self, store):
        self.s = store

    def transaction(self):
        class T:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *a):
                return False
        return T()

    async def execute(self, sql, *a):
        if sql.startswith("INSERT INTO kernel_secrets"):
            if "DO UPDATE" in sql or a[0] not in self.s:
                self.s[a[0]] = a[1]
        elif sql.startswith("DELETE FROM kernel_secrets"):
            self.s.pop(a[0], None)
        elif sql.startswith("DELETE FROM nac_bastion_suspend") or sql.startswith("INSERT INTO nac_mgmt_audit"):
            pass
        return "OK"                 # the schema block and the advisory locks

    async def fetchrow(self, sql, *a):
        if "FROM kernel_secrets WHERE id=$1" in sql:
            v = self.s.get(a[0])
            return None if v is None else ({"secret": v} if "secret" in sql.split("FROM")[0] else {"?": 1})
        if "FROM installed_applications WHERE id=$1" in sql:
            p = INSTALLED.get(a[0])
            return None if p is None else {"provenance": p}
        raise AssertionError(sql)


# installed_applications: app id -> provenance, as the installer records it.
INSTALLED = {"settings": "native", "network": "standard", "evil-app": "community",
             "keys": "custom"}      # a Host-built app that took an owner window's id


class Pool:
    def __init__(self):
        self.s = {}

    def acquire(self):
        conn = Conn(self.s)

        class A:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return A()

    async def execute(self, sql, *a):
        return await Conn(self.s).execute(sql, *a)


def run(c):
    return asyncio.run(c)


# Run on the code before the fix too, so it FAILS there rather than crashing.
HOST = getattr(S, "host_attempts", contextlib.nullcontext)
APP = getattr(S, "app_attempts", lambda app_id: contextlib.nullcontext())
LOCKED = getattr(S, "StepupLocked", ())
OWNER_ID = S._TOTP_FAILURES_ID
HOST_ID = getattr(S, "_TOTP_HOST_FAILURES_ID", S._TOTP_FAILURES_ID)
APP_PREFIX = getattr(S, "_TOTP_APP_FAILURES_PREFIX", S._TOTP_FAILURES_ID)


def _as(host=False, app=None):
    return HOST() if host else (APP(app) if app else contextlib.nullcontext())


def verify(pool, c, t, host=False, app=None):
    """True / False, or "locked" for the 429."""
    with _as(host, app):
        try:
            return run(S.verify_stepup(pool, c, at=t))
        except Exception as e:  # noqa: BLE001
            if LOCKED and isinstance(e, LOCKED):
                return "locked"
            raise


def wait(pool, t, host=False, app=None):
    with _as(host, app):
        return run(S.stepup_wait_seconds(pool, at=t))


def fresh():
    p = Pool()
    p.s.update(BASE.s)
    return p


BASE = Pool()
secret = run(S.ensure_stepup_secret(BASE))
run(S.mark_enrolled(BASE))
T0 = 1_800_000_000.0             # a whole number of 30 s windows, so codes are stable
DAY = 86400


def code(t):
    return S.current_code(secret, at=t)


def wrong(t):
    """A code no window the check could accept at t: the verifier takes the
    windows within S._TOTP_DRIFT of now, and a route reading the wall clock
    may land one window later. This used to avoid only the code of t's own
    window, so "000000" was the RIGHT code whenever a neighbouring window's
    code happened to be 000000 — about one chance in a million per window,
    over ~26,000 windows a run: measured 3 runs in 48 with a "wrong" code
    accepted, the one-in-twenty flake of this gate."""
    near = {code(t + k * S._TOTP_PERIOD)
            for k in range(-S._TOTP_DRIFT - 1, S._TOTP_DRIFT + 2)}
    return next(c for c in ("000000", "111111", "222222", "333333", "444444",
                            "555555", "666666") if c not in near)


# The helper itself: a "wrong" code must not be the right code of any window
# the check accepts. Forced here by making the NEXT window's code 000000.
_real_hotp = S._hotp
_next_window = int(T0 // S._TOTP_PERIOD) + 1
S._hotp = lambda raw, counter: "000000" if counter == _next_window else _real_hotp(raw, counter)
try:
    check("the gate's wrong code is wrong in every window the check accepts "
          "(not just its own)", S.matched_counter(secret, wrong(T0), at=T0) is None,
          wrong(T0))
finally:
    S._hotp = _real_hotp


def owner_day(pool, host_code, *, app=None, owner_app=None):
    """For a day the Host (or, with `app`, a request carrying that app's token)
    tries `host_code(t)` every time its record lets it (every 30 s), and the
    owner types the right code every 10 minutes (with `owner_app`, in that
    window). Returns (tries, owner ok, owner refused)."""
    t, end, nxt = T0 + 30, T0 + 30 + DAY, T0 + 30
    host_tries = ok = refused = 0
    host = app is None
    while t < end:
        if wait(pool, t, host=host, app=app) == 0:
            verify(pool, host_code(t), t, host=host, app=app)
            host_tries += 1
        if t >= nxt:
            if verify(pool, code(t), t, app=owner_app) is True:
                ok += 1
            else:
                refused += 1
            nxt = t + 600
        t += 30
    return host_tries, ok, refused


print("== the repro: the Host sends no code each time it may")
p = fresh()
tries, ok, refused = owner_day(p, lambda t: "")
check(f"the owner's right code works all day (ok={ok} refused={refused}; the Host tried {tries} times)",
      refused == 0 and ok == 144, (ok, refused))
check("  and a missing code was never counted, on either record",
      OWNER_ID not in p.s and HOST_ID not in p.s, sorted(k for k in p.s if "fail" in k))

print("== the Host sends wrong codes each time it may: it holds up only itself")
p = fresh()
tries, ok, refused = owner_day(p, wrong)
check(f"the owner's right code works all day (ok={ok} refused={refused})",
      refused == 0 and ok == 144, (ok, refused))
check(f"  the Host got {tries} tries in the day, not more than a guesser should (<= 40)",
      tries <= 40, tries)
check("  its wrong codes are on its own record, none on the owner's",
      HOST_ID != OWNER_ID and HOST_ID in p.s and OWNER_ID not in p.s)

# Any caller in the pod that holds an app
# token other than the Host's counted as the owner, so it could keep the
# owner's record waiting all day with wrong six-digit codes (143 of 144 of the
# owner's right codes refused).
print("== an app in the pod sends wrong codes each time it may: it holds up only itself")
for owner_app, where in ((None, "with no app token"), ("settings", "in Settings"),
                         ("network", "in Network")):
    p = fresh()
    tries, ok, refused = owner_day(p, wrong, app="evil-app", owner_app=owner_app)
    check(f"the owner's right code, typed {where}, works all day (ok={ok} refused={refused})",
          refused == 0 and ok == 144, (ok, refused))
    check("  the app's wrong codes are on its own record, none on the owner's",
          APP_PREFIX + "evil-app" in p.s and OWNER_ID not in p.s and HOST_ID not in p.s, sorted(p.s))
    check(f"  and it got {tries} tries in the day (<= 40)", tries <= 40, tries)
p = fresh()
t = T0 + 30
for _ in range(S.STEPUP_FREE_FAILURES):
    verify(p, wrong(t), t, app="settings")
check("the owner's own window counts on the owner's record (its wrong codes make the owner wait)",
      OWNER_ID in p.s and wait(p, t) == S.STEPUP_LOCK_BASE_S
      and wait(p, t, app="network") == S.STEPUP_LOCK_BASE_S, sorted(p.s))
check("  and they do not hold up another app", wait(p, t, app="evil-app") == 0)
p = fresh()
for _ in range(S.STEPUP_FREE_FAILURES):
    verify(p, wrong(t), t, app="keys")
check("an app that took an owner window's id but is not the platform's (custom) counts on its own",
      APP_PREFIX + "keys" in p.s and OWNER_ID not in p.s and wait(p, t) == 0, sorted(p.s))
p = fresh()
for _ in range(S.STEPUP_FREE_FAILURES):
    verify(p, wrong(t), t, app="context-manager")
check("an owner window id that is not installed counts on its own record too",
      APP_PREFIX + "context-manager" in p.s and OWNER_ID not in p.s, sorted(p.s))
p = fresh()
d = ""
with APP("evil-app"):
    for _ in range(S.STEPUP_FREE_FAILURES):
        try:
            run(S.verify_stepup(p, wrong(t), at=t))
        except Exception as e:  # noqa: BLE001
            d = str(getattr(e, "detail", e))
            break
check("  its 429 names the app and says the owner is not held up",
      "app 'evil-app'" in d and "not held up" in d, d)

APPS_ID = getattr(S, "_TOTP_APPS_FAILURES_ID", None)
print("== every app other than the owner's windows draws on one shared record")
p = fresh()
t = T0 + 30
for _ in range(S.STEPUP_FREE_FAILURES):
    verify(p, wrong(t), t, app="evil-app")
check("five wrong codes through one app make every other such app wait too",
      wait(p, t, app="other-app") == S.STEPUP_LOCK_BASE_S and wait(p, t, app="keys") == S.STEPUP_LOCK_BASE_S,
      (wait(p, t, app="other-app"), sorted(p.s)))
check("  but not the owner, the owner's windows, or the Host",
      wait(p, t) == 0 and wait(p, t, app="settings") == 0 and wait(p, t, host=True) == 0)
check("  and they are counted on the app's own record and the shared one, nothing else",
      APPS_ID is not None and sorted(k for k in p.s if "fail" in k) == sorted([APP_PREFIX + "evil-app", APPS_ID]),
      sorted(p.s))
d = verify(p, wrong(t), t, app="other-app")
check("  the other app is refused without its code being looked at or counted",
      d == "locked" and APP_PREFIX + "other-app" not in p.s, (d, sorted(p.s)))
try:
    with APP("other-app"):
        run(S.verify_stepup(p, wrong(t), at=t))
    d = ""
except Exception as e:  # noqa: BLE001
    d = str(getattr(e, "detail", e))
check("  and its 429 says the wait is the apps', and that the owner is not held up",
      "apps on this Host" in d and "not held up" in d, d)
t2 = t + S.STEPUP_LOCK_BASE_S
check("a right code through an app clears its record and the shared one, not another app's",
      verify(p, code(t2), t2, app="other-app") is True and APPS_ID not in p.s
      and APP_PREFIX + "other-app" not in p.s and APP_PREFIX + "evil-app" in p.s, sorted(p.s))
p = fresh()
t, end, tries = T0 + 10 * DAY, T0 + 11 * DAY, 0
apps_many = [f"app-{i}" for i in range(12)]
while t < end:
    free = [a for a in apps_many if wait(p, t, app=a) == 0]
    if not free:
        t += min(wait(p, t, app=a) for a in apps_many)
        continue
    for a in free:
        # every code tried while nothing waited was looked at, the one that
        # starts a wait (a 429) included
        tries += 1
        if verify(p, "999999" if code(t) != "999999" else "888888", t, app=a) is not False:
            break
    t += 1
check(f"guessing as fast as allowed for a day through 12 app tokens gets {tries} tries in all (<= 40)",
      tries <= 40, tries)

# A count only went down on a right code, so once the
# shared record reached the ceiling one app's wrong code each hour started
# each hour's wait as the last one ended, and no other app could try at all.
print("== one app cannot keep the other apps waiting")
p = fresh()
t, end = T0 + 10 * DAY, T0 + 12 * DAY
tries_day, free, steps, longest, held_since = [0, 0], 0, 0, 0, None
while t < end:
    if wait(p, t, app="evil-app") == 0:
        verify(p, wrong(t), t, app="evil-app")
        tries_day[int((t - T0 - 10 * DAY) // DAY)] += 1
    if t >= T0 + 10 * DAY + 3 * 3600:      # past its first two hours
        steps += 1
        if wait(p, t, app="other-app") == 0:
            free += 1
            held_since = None
        else:
            held_since = t if held_since is None else held_since
            longest = max(longest, t - held_since + 30)
    t += 30
check(f"one app guessing as fast as it may leaves the other apps free {100 * free // steps}% of the time "
      f"(>= 45%; it was 0%)", free * 100 >= 45 * steps, (free, steps))
check(f"  and holds them at most {S.STEPUP_LOCK_MAX_S} s at a time ({longest:.0f} s)",
      longest <= S.STEPUP_LOCK_MAX_S, longest)
check(f"  it gets {tries_day} tries a day on its own record (<= 22 each)", max(tries_day) <= 22, tries_day)
_ceil = getattr(S, "_ceiling", None)
check("an app's own record waits up to twice the others' ceiling",
      _ceil is not None and S._lock_seconds(10 ** 6, _ceil(APP_PREFIX + "evil-app")) == 2 * S.STEPUP_LOCK_MAX_S
      and _ceil(APPS_ID) == _ceil(OWNER_ID) == _ceil(HOST_ID) == S.STEPUP_LOCK_MAX_S)

# The attacker above sends a code only when the
# wait reads 0, so it never leans on verify_stepup's own refusal. This one
# ignores the wait and sends a wrong code every 30 s for three days. Only the
# codes actually looked at count (matched_counter is called on each of them,
# and on nothing refused unlooked).
_looked = []
_real_matched = S.matched_counter


def _counting_matched(*a, **k):
    _looked.append(k.get("at"))
    return _real_matched(*a, **k)


def most_in_a_day(times):
    """The most of `times` inside any 24 h, sliding."""
    times, best, j = sorted(times), 0, 0
    for i in range(len(times)):
        while times[i] - times[j] >= DAY:
            j += 1
        best = max(best, i - j + 1)
    return best


S.matched_counter = _counting_matched
try:
    _looked.clear()
    p = fresh()
    t, end = T0 + 40 * DAY, T0 + 43 * DAY
    free, steps, longest, held_since = 0, 0, 0, None
    while t < end:
        verify(p, wrong(t), t, app="evil-app")
        if t >= T0 + 40 * DAY + 3 * 3600:      # past its first two hours
            steps += 1
            if wait(p, t, app="other-app") == 0:
                free += 1
                held_since = None
            else:
                held_since = t if held_since is None else held_since
                longest = max(longest, t - held_since + 30)
        t += 30
finally:
    S.matched_counter = _real_matched
check(f"one app sending a wrong code every 30 s whatever the wait, for three days, has at most "
      f"{most_in_a_day(_looked)} looked at in any 24 h (<= 22)", most_in_a_day(_looked) <= 22,
      most_in_a_day(_looked))
check(f"  and leaves the other apps free {100 * free // steps}% of the time (>= 45%)",
      free * 100 >= 45 * steps, (free, steps))
check(f"  and holds them at most 3600 s at a time ({longest:.0f} s)", longest <= 3600, longest)
p = fresh()
t, end, tries, day_tries = T0 + 10 * DAY, T0 + 17 * DAY, 0, {}
while t < end:
    free_apps = [a for a in apps_many if wait(p, t, app=a) == 0]
    if not free_apps:
        t += min(wait(p, t, app=a) for a in apps_many)
        continue
    for a in free_apps:
        d = int((t - T0) // DAY)
        day_tries[d] = day_tries.get(d, 0) + 1
        if verify(p, "999999" if code(t) != "999999" else "888888", t, app=a) is not False:
            break
    # guess for a day, then keep quiet for a day, so the records start again
    t += 1 if int((t - T0) // DAY) % 2 == 0 else DAY
check(f"guessing a day, quiet a day, for a week through 12 app tokens: at most {max(day_tries.values())} "
      f"tries in any day (<= 40)", max(day_tries.values()) <= 40, day_tries)

print("== the apps' records start again after a quiet day; the owner's do not")


def at_ceiling(p, fid, last):
    p.s[fid] = b'{"n": 11, "at": %f}' % last


t = T0 + 30 * DAY
# A whole day, written here rather than read from the module: that ageing gives
# a guesser nothing rests on the quiet period being at least a day.
for quiet, starts_again in ((DAY, True), (DAY - 3600, False)):
    p = fresh()
    at_ceiling(p, APP_PREFIX + "evil-app", t - quiet)
    at_ceiling(p, APPS_ID, t - quiet)
    got = verify(p, wrong(t), t, app="evil-app")
    counts = [S._parse_failures(p.s[f])[0] for f in (APP_PREFIX + "evil-app", APPS_ID)]
    if starts_again:
        check(f"no wrong code for {quiet // 3600} h: the app's record and the shared one count from zero "
              f"(the next wrong code is free, counted as one on each)",
              got is False and counts == [1, 1] and wait(p, t, app="other-app") == 0, (got, counts))
    else:
        check(f"a wrong code {quiet // 3600} h after the last: the count stands, and it waits again",
              got == "locked" and counts == [12, 12], (got, counts))
for who, fid, host in (("owner", OWNER_ID, False), ("Host", HOST_ID, True)):
    p = fresh()
    at_ceiling(p, fid, t - 2 * DAY)
    got = verify(p, wrong(t), t, host=host)
    check(f"the {who}'s record does not age: a wrong code two days on still waits {S.STEPUP_LOCK_MAX_S} s",
          got == "locked" and wait(p, t, host=host) == S.STEPUP_LOCK_MAX_S
          and S._parse_failures(p.s[fid])[0] == 12, (got, p.s[fid]))
# The 429 a wrong code answers with says the wait it started, so a caller that
# honours Retry-After is not sent back an hour early by an app's longer wait.
p = fresh()
_app_ceiling = getattr(S, "STEPUP_APP_LOCK_MAX_S", S.STEPUP_LOCK_MAX_S)
at_ceiling(p, APP_PREFIX + "evil-app", t - _app_ceiling)
with _as(app="evil-app"):
    try:
        run(S.verify_stepup(p, wrong(t), at=t))
        _told = None
    except Exception as e:  # noqa: BLE001
        if not (LOCKED and isinstance(e, LOCKED)):
            raise
        _told = e.wait_s
check(f"a wrong code that takes an app's own record to its ceiling answers a 429 saying "
      f"{2 * S.STEPUP_LOCK_MAX_S} s, the wait it then reads ({_told})",
      _told == 2 * S.STEPUP_LOCK_MAX_S == wait(p, t, app="evil-app"), (_told, wait(p, t, app="evil-app")))
# An app that guesses until its wait reaches an
# hour, keeps quiet for a few hours, and starts again. If its records started
# again after a few quiet hours, each burst would bring a fresh budget; with a
# whole quiet day needed, no 24 h holds more than an app's own record's day.
worst = {}
S.matched_counter = _counting_matched
try:
    for quiet_h in (3, 6, 12):
        _looked.clear()
        p = fresh()
        t, end = T0 + 50 * DAY, T0 + 56 * DAY
        while t < end:
            before = len(_looked)
            verify(p, wrong(t), t, app="evil-app")
            if len(_looked) > before:
                t += 30
                continue
            w = wait(p, t, app="evil-app")
            t += quiet_h * 3600 if w >= 3600 else max(w, 30)
        worst[quiet_h] = most_in_a_day(_looked)
finally:
    S.matched_counter = _real_matched
check(f"guessing until the wait is an hour, then quiet for 3, 6 or 12 h, for six days: at most "
      f"{max(worst.values())} looked at in any 24 h (<= 22; by hours quiet: {worst})",
      max(worst.values()) <= 22, worst)


print("== a page that mints many app tokens, through the real middleware and routes")
# A Host-built app is framed on the shell's
# origin, so it can POST /api/applications/<id>/launch with no token of its
# own and hold the token of browser (write:vault), security and terminal
# (write:network), and its own. Each had a record of its own, so each gave it
# about 30 guesses a day: five apps, five times the budget, and a right guess
# on browser registers a key that opens the vault. Here the real launch, the
# real middleware and the real code-taking routes run against a stand-in
# database, over a whole day of guessing as fast as each answer allows.
import tempfile  # noqa: E402
import types  # noqa: E402
import yaml  # noqa: E402
from datetime import datetime as _dtm  # noqa: E402
from fastapi import FastAPI as _FastAPI  # noqa: E402
from fastapi.testclient import TestClient as _TestClient  # noqa: E402
from server import brain_service as _bs  # noqa: E402
from server.applications import middleware as _MW, registry as _REG  # noqa: E402
import server.routes.applications as _RA  # noqa: E402
import server.routes.vault as _RV  # noqa: E402
import server.routes.backup as _RB  # noqa: E402
import server.routes.network as _RN  # noqa: E402


class _AppConn(Conn):
    async def fetchval(self, sql, *a):
        return None

    async def fetch(self, sql, *a):
        return []


class _AppPool(Pool):
    def acquire(self):
        conn = _AppConn(self.s)

        class A:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return A()

    async def fetchrow(self, sql, *a):
        return await _AppConn(self.s).fetchrow(sql, *a)

    async def fetchval(self, sql, *a):
        return None

    async def fetch(self, sql, *a):
        return []


_hostapp_dir = tempfile.mkdtemp(prefix="stepup-hostapp-")
_m = yaml.safe_load(open(os.path.join(ROOT, "system-apps", "settings", "app.yaml")))
_m.update(id="hostapp", name="Host-built", provenance="custom")
with open(os.path.join(_hostapp_dir, "app.yaml"), "w") as _f:
    yaml.safe_dump(_m, _f)
# Circles: an owner window (OWNER_UI_APP_IDS) still served on the desktop's
# origin, holding write:vault from its own manifest.
_APPS = {"settings": ("native", None), "browser": (None, None), "security": (None, None),
         "terminal": (None, None), "hostapp": ("custom", _hostapp_dir), "circles": ("native", None)}
_installed = {}
for _a, (_prov, _dir) in _APPS.items():
    _src = _dir or os.path.join(ROOT, "system-apps", _a)
    _man = yaml.safe_load(open(os.path.join(_src, "app.yaml")))
    _installed[_a] = (_prov or _man.get("provenance") or "native",
                      list(_man.get("permissions") or []) + (["write:vault"] if _a == "hostapp" else []), _src)
INSTALLED.update({a: v[0] for a, v in _installed.items()})


async def _reg_get(pool, app_id):
    v = _installed.get(app_id)
    if v is None:
        return None
    return _REG.InstalledApp(id=app_id, name=app_id, version="1", provenance=v[0], entrypoint="index.html",
                             permissions=v[1], source_dir=v[2], source_signature=None,
                             installed_at=_dtm.now(), upgraded_at=None, status="active", app_license={},
                             authored_here=None)


_apool = _AppPool()
_apool.s.update(BASE.s)
_saved = (_bs.get_pool, _RV._pool, _RA.registry.get, S.time)
_bs.get_pool = lambda: _apool
_RV._pool = lambda: _apool
_RA.registry.get = _reg_get
_saved_eid = os.environ.get("ENTITY_ID")
os.environ["ENTITY_ID"] = "7"             # the vault routes need a Host number
_clock_now = [T0 + 20 * DAY]
S.time = types.SimpleNamespace(time=lambda: _clock_now[0])
try:
    _app = _FastAPI()
    _app.state.kernel_secret = os.urandom(32)
    _app.add_middleware(_MW.ApplicationTokenMiddleware)
    for _r in (_RA.router, _RV.router, _RB.router, _RN.router):
        _app.include_router(_r)
    _c = _TestClient(_app)
    _H = {"host": "h.example", "x-forwarded-host": "h.example", "origin": "https://h.example",
          "sec-fetch-site": "same-origin"}
    _toks = {}
    _delivery = {}
    for _a in _APPS:
        _lr = _c.post(f"/api/applications/{_a}/launch", headers=_H)
        _toks[_a] = _lr.json().get("token") if _lr.status_code == 200 else None
        _delivery[_a] = _lr.json().get("token_delivery") if _lr.status_code == 200 else None
    # An ISOLATED app's token is minted only on that app's own
    # origin and never handed to a browser by /launch, so this page gets no
    # Settings token at all (routes/applications.py launch_app, frame_token).
    check("the page gets NO Settings token from /launch (minted only on Settings' own origin)",
          _toks["settings"] is None and _delivery["settings"] == "app-origin", _delivery)
    _toks.pop("settings")
    check("the page holds a token for each same-origin app, minted with no token of its own",
          all(_toks.values()), {a: bool(v) for a, v in _toks.items()})
    _targets = {
        "browser": ("/api/vault/user-keys", lambda t: {"user_id": "u9", "pubkey_b64": "AA==", "stepup_code": wrong(t)}),
        "security": ("/api/network/nac/mgmt/resume", lambda t: {"server": "box1", "stepup_code": wrong(t)}),
        "terminal": ("/api/network/nac/mgmt/resume", lambda t: {"server": "box1", "stepup_code": wrong(t)}),
        "hostapp": ("/api/vault/user-keys", lambda t: {"user_id": "u9", "pubkey_b64": "AA==", "stepup_code": wrong(t)}),
        "circles": ("/api/vault/user-keys", lambda t: {"user_id": "u9", "pubkey_b64": "AA==", "stepup_code": wrong(t)}),
    }
    other = set()
    start = _clock_now[0]
    while _clock_now[0] < start + DAY and all(_toks.values()) and not other:
        waits = []
        for _a, (_path, _body) in _targets.items():
            _r = _c.post(_path, headers={"X-Application-Token": _toks[_a]}, json=_body(_clock_now[0]))
            if _r.status_code == 429:
                waits.append(int(_r.headers.get("retry-after") or 1))
                if "try again" not in (_r.json().get("detail") or ""):
                    other.add((_a, 429, _r.text[:80]))
            elif _r.status_code != 403:
                other.add((_a, _r.status_code, _r.text[:80]))
        if len(waits) == len(_targets):
            _clock_now[0] += min(waits)
        else:
            _clock_now[0] += 1
    # What was counted, read off the records themselves: nothing here ever
    # sends a right code, so no record is cleared and each count is every
    # wrong code it took (the one that started each wait included).
    def _n(fid):
        got = S._parse_failures(_apool.s[fid]) if fid in _apool.s else None
        return got[0] if got else 0
    counted = {a: _n(APP_PREFIX + a) for a in _targets if a != "circles"}
    apps_total = sum(counted.values())
    check(f"in a day, every app other than the owner's windows together got {apps_total} wrong "
          f"codes counted (<= 40), not about 30 each; by app: {counted}",
          0 < apps_total <= 40 and not other, (counted, sorted(other)[:3]))
    check(f"  and the owner's record, which this page reaches only through an owner window still "
          f"served on the desktop's origin (Circles here; it gets no Settings token; see "
          f"nac_stepup.py 'What that does NOT cover'), got {_n(OWNER_ID)} (<= 40)",
          0 < _n(OWNER_ID) <= 40, _n(OWNER_ID))
    check("  and Circles' wrong codes counted on the owner's record, not on an app record",
          _n(APP_PREFIX + "circles") == 0, _n(APP_PREFIX + "circles"))
    # A request with no app token does not count as the owner's. It is
    # refused before any code is looked at.
    _before = dict(_apool.s)
    _clock_now[0] += DAY
    _st = _c.get("/api/applications/shell-token", headers=_H)
    _shell = _st.json().get("token") if _st.status_code == 200 else None
    _code_routes = {**_targets, "backup": ("/api/backup/restore/fetch",
                    lambda t: {"stepup_code": wrong(t), "stamp": "20260929T000000Z", "token_id": 1})}
    _asked = {}
    for _a, (_path, _body) in _code_routes.items():
        _asked[_a] = (_c.post(_path, headers=_H, json=_body(_clock_now[0])).status_code,
                      _c.post(_path, headers={**_H, "X-Application-Token": _shell or "x"},
                              json=_body(_clock_now[0])).status_code)
    check(f"on the three code-taking routes driven here (vault user-keys, network mgmt resume, backup "
          f"restore fetch), a page sending no app token gets a 401 and the shell's own token a 403, "
          f"before any code is counted (the routes a call-graph scan connects: the next section): {_asked}",
          _shell and all(v == (401, 403) for v in _asked.values())
          and {k: v for k, v in _apool.s.items() if "fail" in k} == {k: v for k, v in _before.items() if "fail" in k},
          (_asked, bool(_shell)))
finally:
    _bs.get_pool, _RV._pool, _RA.registry.get, S.time = _saved
    if _saved_eid is None:
        os.environ.pop("ENTITY_ID", None)
    else:
        os.environ["ENTITY_ID"] = _saved_eid
    for _a in _APPS:
        INSTALLED.pop(_a, None)
    INSTALLED.update({"settings": "native", "keys": "custom"})
    shutil_rm = __import__("shutil").rmtree
    shutil_rm(_hostapp_dir, ignore_errors=True)

print("== the routes a call-graph scan connects to a code check need an app token first")
# The check above drives three routes. This one
# reads more of them off the code, and proves exactly this much: every route
# that a static call graph connects to nac_stepup.verify_stepup requires an
# app token. The graph covers server/ and brain/, and follows a call or a
# reference only when its module's own imports resolve it (a bare name,
# module.name, self.name). It starts from every route a router declares
# (@<router>.<verb>, or <router>.add_api_route with a named endpoint). Each
# route it connects must require a permission (requires_permission, read off
# the dependencies FastAPI resolved for the route) outside the shell's own
# read-only scopes, so a request with no app token gets a 401, and the shell's
# token a 403, before any code is looked at. (requires_permission also lets the
# pod's own INTERNAL_API_SECRET through on _INTERNAL_LOOPBACK_PATHS; only the
# pod holds it, and the entity's own workload is outside what the attempt limit
# can stop, as nac_stepup.py says.)
#
# What it does not prove: that no other route reaches a code check. A call
# through an object, a factory, a lookup table or getattr is not followed. The
# routes reached that way are named below (_DYNAMIC_ROUTES), each with
# what every path from it to a code check passes through, and why a code tried
# there cannot count on the owner's record; both are checked. Two rules keep a
# code check from dropping out of the graph unseen: every reference to
# verify_stepup must be one the scan resolves (no getattr or string dispatch),
# and every function that calls it must be reached from a route the scan
# connects or from one of the named paths.
import importlib  # noqa: E402
from fastapi import APIRouter as _APIRouter  # noqa: E402

_VERIFY = "server.network.nac_stepup:verify_stepup"
_VERBS = {"get", "post", "put", "patch", "delete", "api_route", "websocket"}


def _module_files(root):
    """{module name: path under root} for server/ and brain/ (brain/ is imported bare)."""
    out = {}
    for top in ("server", "brain"):
        for base, dirs, names in os.walk(os.path.join(root, top)):
            dirs[:] = [d for d in dirs if d not in ("node_modules", "__pycache__", "tests")]
            for fn in names:
                if fn.endswith(".py"):
                    rel = os.path.relpath(os.path.join(base, fn), root)
                    parts = rel[:-3].split(os.sep)
                    parts = parts[1:] if parts[0] == "brain" else parts
                    out[".".join(parts[:-1] if parts[-1] == "__init__" else parts)] = rel
    return out


def _call_graph(root):
    """({function: what it calls or names}, [(route node, module, path or None)],
    [where verify_stepup is named in a way the scan cannot resolve])."""
    files = _module_files(root)
    edges, routes, unresolved = {}, [], []
    for mod, rel in files.items():
        try:
            tree = ast.parse(open(os.path.join(root, rel)).read())
        except SyntaxError:
            continue
        names = {}          # local name -> "module", or "module:symbol" for a from-import
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    names[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom):
                src = node.module or ""
                if node.level:
                    pkg = mod.split(".") if rel.endswith("__init__.py") else mod.split(".")[:-1]
                    pkg = pkg[:len(pkg) - node.level + 1]
                    src = ".".join(pkg + ([node.module] if node.module else []))
                for a in node.names:
                    full = f"{src}.{a.name}"
                    names[a.asname or a.name] = full if full in files else f"{src}:{a.name}"
        top = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}

        def resolve(sub, cls=None):
            """The function a Name or Attribute names, or None when its module's
            imports do not say."""
            chain, e = [], sub
            while isinstance(e, ast.Attribute):
                chain.append(e.attr)
                e = e.value
            if not isinstance(e, ast.Name):
                return None
            chain = [e.id] + chain[::-1]
            if chain[0] in ("self", "cls") and cls and len(chain) == 2:
                return f"{mod}:{cls}.{chain[1]}"
            if len(chain) == 1:
                if chain[0] in top:
                    return f"{mod}:{chain[0]}"
                return names[chain[0]] if ":" in names.get(chain[0], "") else None
            if chain[0] in top:
                return f"{mod}:{chain[0]}.{chain[1]}"
            if chain[0] in names and ":" not in names[chain[0]]:
                dotted = ".".join([names[chain[0]]] + chain[1:-1])
                if dotted in files:
                    return f"{dotted}:{chain[-1]}"
            return None

        def refs(node, cls=None):
            got = set()
            for sub in ast.walk(node):
                if isinstance(getattr(sub, "ctx", None), ast.Load):
                    q = resolve(sub, cls)
                    if q:
                        got.add(q)
            return got

        for sub in ast.walk(tree):
            named = (sub.value if isinstance(sub, ast.Constant) else sub.attr if isinstance(sub, ast.Attribute)
                     else sub.id if isinstance(sub, ast.Name) else None)
            if named != "verify_stepup":
                continue
            if isinstance(sub, ast.Constant) or (isinstance(getattr(sub, "ctx", None), ast.Load)
                                                 and resolve(sub) != _VERIFY):
                unresolved.append(f"{rel}:{sub.lineno}")

        def visit(body, cls=None):
            for n in body:
                if isinstance(n, ast.ClassDef):
                    visit(n.body, n.name)
                elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    q = f"{mod}:{cls + '.' if cls else ''}{n.name}"
                    edges.setdefault(q, set()).update(refs(n, cls))
                    if any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                           and d.func.attr in _VERBS for d in n.decorator_list):
                        routes.append((q, mod, None))
        visit(tree.body)
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "add_api_route":
                kw = {k.arg: k.value for k in n.keywords}
                path = n.args[0] if n.args else kw.get("path")
                endpoint = n.args[1] if len(n.args) > 1 else kw.get("endpoint", n)
                path = path.value if isinstance(path, ast.Constant) else None
                q = f"{mod}:<add_api_route {path}>"
                edges.setdefault(q, set()).update(refs(endpoint))
                routes.append((q, mod, path))
    return edges, routes, unresolved


def _reaching(edges):
    back = {}
    for q, rs in edges.items():
        for r in rs:
            back.setdefault(r, set()).add(q)
    seen, stack = {_VERIFY}, [_VERIFY]
    while stack:
        for q in back.get(stack.pop(), ()):
            if q not in seen:
                seen.add(q)
                stack.append(q)
    return seen


def _from(edges, starts, cut=()):
    seen, stack = set(starts), list(starts)
    while stack:
        for r in edges.get(stack.pop(), ()):
            if r not in seen and r not in cut:
                seen.add(r)
                stack.append(r)
    return seen


_DEP = f"{_MW.requires_permission.__qualname__}.<locals>._dep"


def _scopes(route):
    """Every permission requires_permission demands of a route, from FastAPI's own dependency tree."""
    out, stack = [], [route.dependant]
    while stack:
        d = stack.pop()
        if getattr(d.call, "__qualname__", "") == _DEP:
            out.append(inspect.getclosurevars(d.call).nonlocals.get("permission"))
        stack.extend(d.dependencies)
    return out


def _built(node, mod, path):
    """The routes FastAPI built for a route node of the scan."""
    m = importlib.import_module(mod)
    built = [r for v in vars(m).values() if isinstance(v, _APIRouter) for r in v.routes]
    if path is not None:
        return [r for r in built if getattr(r, "path", "").endswith(path)]
    fn = node.split(":", 1)[1]
    return [r for r in built if getattr(r, "endpoint", None) is not None
            and getattr(inspect.unwrap(r.endpoint), "__qualname__", None) == fn
            and getattr(inspect.unwrap(r.endpoint), "__module__", None) == mod]


def _fn_tree(node):
    """A scan node's function, parsed from its file (an empty module when it is gone)."""
    m_name, q = node.split(":")
    rel = _module_files(ROOT).get(m_name)
    body = ast.parse(open(os.path.join(ROOT, rel)).read()).body if rel else []
    for part in q.split("."):
        found = [n for n in body if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == part]
        if not found:
            return ast.Module(body=[], type_ignores=[])
        body = found[0].body if part != q.split(".")[-1] else found
    return ast.Module(body=body, type_ignores=[])


def _passes_no_code(node):
    """No call inside this function passes a stepup_code."""
    return [k.arg for c in ast.walk(_fn_tree(node)) if isinstance(c, ast.Call)
            for k in c.keywords if k.arg == "stepup_code"]


_HOST_TOOLS = "mastercode.runtime.tools:_execute_meta_tool"
_ADD_CHANNEL = "server.reachout.service:add_channel"

# The one route the scan connects that is not an app's: a bastion's heartbeat.
# It is refused with a 401 unless it carries the bastion's own bearer, and it
# reaches a code check only through the box completing requests the owner
# approved on the machine, which passes no code, or through the Host's own
# tools, whose codes count on the Host's record.
_BEARER_ROUTES = {"server.routes.network:bastion_heartbeat": (
    "server.network.nac_commit:complete_box_requests",
    "server.network.service_reach:complete_box_reach_requests",
    _HOST_TOOLS)}

# Routes that reach a code check through calls the scan does not follow, as
# traced by hand: route -> (the name its handler calls
# them by, the functions it reaches that way, what every path from those to a
# code check passes through).
_TURN = ("send_message", ("mastercode.runtime.session:EntityRuntime.send_message",), _HOST_TOOLS)
_DYNAMIC_ROUTES = {
    # the Host's turn, through the session runtime
    "server.routes.circles:send_message": _TURN,
    "server.routes.brain:send_message_session_free": _TURN,
    # an OAuth consent, through the provider table: the channel it builds
    "server.routes.integrations:provider_callback": ("handle_callback", None, _ADD_CHANNEL),
}

_edges, _routes, _unresolved = _call_graph(ROOT)
_reach = _reaching(_edges)
_route_nodes = {q for q, _, _ in _routes}
_code_routes = [(q, mod, path) for q, mod, path in _routes if q in _reach]
_found = {q for q, _, _ in _code_routes}
_callbacks = tuple(sorted(q for q in _edges if q.startswith("server.integrations.providers.")
                          and q.endswith(":handle_callback")))
_dynamic = {q: (name, targets if targets is not None else _callbacks, through)
            for q, (name, targets, through) in _DYNAMIC_ROUTES.items()}
check(f"the scan connects {len(_code_routes)} of {len(_routes)} routes to a code check, the known ones "
      f"among them", {"server.routes.vault:reveal_secret", "server.routes.vault:register_user_key",
                      "server.routes.backup:restore_fetch", "server.routes.network:nac_mgmt_resume",
                      "server.routes.tickets:decide_ticket"} <= _found, sorted(_found))
check("  every reference to verify_stepup in server/ and brain/ is one the scan resolves "
      "(a getattr, a string or an object naming it fails here)", not _unresolved, _unresolved)
_callers = {q for q, rs in _edges.items() if _VERIFY in rs}
_unreached = _callers - _from(_edges, list(_route_nodes) + [t for _, ts, _ in _dynamic.values() for t in ts])
check(f"  and every one of the {len(_callers)} functions in server/ and brain/ that calls or names it is reached "
      f"from a route the scan connects or from a named path", len(_callers) >= 15 and not _unreached,
      sorted(_unreached))
_open, _shell_scoped = [], []
for q, mod, path in _code_routes:
    if q in _BEARER_ROUTES:
        continue
    built = _built(q, mod, path)
    scopes = [s for r in built for s in _scopes(r)]
    if not built or not all(_scopes(r) for r in built):
        _open.append(q)
    elif any(s in _RA.SHELL_SCOPES for s in scopes):
        _shell_scoped.append(q)
check("each route the scan connects, other than the bastion heartbeat, requires an app token with a "
      "permission (so no app token gets a 401)", not _open, _open)
check("  and none of those permissions is one of the shell's own (so its token gets a 403)",
      not _shell_scoped, _shell_scoped)

print("== the bastion heartbeat: its own bearer, and no code of its own")
for q, through in _BEARER_ROUTES.items():
    check(f"{q.split(':')[1]}: it reaches a code check only through "
          f"{', '.join(t.split(':')[1] for t in through)}",
          q in _found and _VERIFY not in _from(_edges, [q], cut=set(through)), q in _found)
    for t in through[:2]:
        check(f"  {t.split(':')[1]} passes no code to what it approves", not _passes_no_code(t),
              _passes_no_code(t))
from fastapi import HTTPException as _HTTPException  # noqa: E402


class _BearerPool:
    def __init__(self, row):
        self.row, self.asked = row, []

    async def fetchrow(self, sql, *a):
        self.asked.append(sql)
        return self.row


class _BeatReq:
    headers = {"content-type": "application/json"}

    async def json(self):
        return {"hostname": "box1"}


_beats = []


async def _beat(pool, eid, body):
    _beats.append(body)
    return None


def _heartbeat(authorization, row):
    """(status, beats recorded, SQL asked) for one heartbeat."""
    pool = _BearerPool(row)
    before = len(_beats)
    _bs.get_control_pool = lambda: pool
    try:
        run(_RN.bastion_heartbeat(_BeatReq(), authorization=authorization))
        got = None
    except _HTTPException as e:
        got = e.status_code
    except Exception as e:  # noqa: BLE001
        got = type(e).__name__
    return got, len(_beats) - before, pool.asked


_saved_beat = (_bs.get_control_pool, _RN.topology.upsert_heartbeat)
_RN.topology.upsert_heartbeat = _beat
try:
    _none = _heartbeat(None, {"id": 1, "entity_id": 7})
    _stranger = _heartbeat("Bearer not-a-bastion", None)
    _known = _heartbeat("Bearer a-bastion", {"id": 1, "entity_id": 7})
finally:
    _bs.get_control_pool, _RN.topology.upsert_heartbeat = _saved_beat
check(f"  no bearer: a 401, and nothing recorded ({_none[:2]})", _none[:2] == (401, 0), _none)
check(f"  a bearer that is no live bastion's: the lookup's own 401, and nothing recorded ({_stranger[:2]})",
      _stranger[:2] == (401, 0) and any("operator_kind = 'bastion'" in s and "revoked_at IS NULL" in s
                                       for s in _stranger[2]), _stranger)
check(f"  a live bastion's bearer: the beat is recorded (so the two refusals above are the route's own)",
      _known[1] == 1, _known)

print("== routes that reach a code check through calls the scan does not follow")
def _handler_trees(q):
    """The route's function, and any function of its own module it hands its
    work to by name (a door that hands its work to the router's 30 s job
    helper, as Circles' replay did before it left with the rail). One hop,
    same module only."""
    mod, fn = q.split(":")
    tree = _fn_tree(q)
    out = [tree]
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and n.id != fn:
            t = _fn_tree(f"{mod}:{n.id}")
            if t.body:
                out.append(t)
    return out


for q, (name, targets, through) in _dynamic.items():
    route_fn = q.split(":")[1]
    calls_it = q in _route_nodes and any(
        isinstance(n, ast.Attribute) and n.attr == name for t in _handler_trees(q) for n in ast.walk(t))
    reached = [t for t in targets if t in _reach]
    only_through = all(_VERIFY not in _from(_edges, [t], cut={through}) for t in targets)
    via = ", ".join(sorted({t.split(":")[1] for t in targets})) + (f" ({len(targets)} of them)"
                                                                   if len(targets) > 1 else "")
    check(f"{route_fn}: a route whose handler calls .{name}(), reaching a code check through "
          f"{via} only by way of {through.split(':')[1]}",
          calls_it and targets and reached and only_through, (calls_it, targets, reached, only_through))

# Why a code tried by way of the Host's tools counts on the Host's record:
_tools_tree = ast.parse(open(os.path.join(ROOT, "brain", "mastercode", "runtime", "tools.py")).read())
_wrapped = [n for n in _tools_tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == _HOST_TOOLS.split(":")[1]
            and any(isinstance(d, ast.Name) and d.id == "_codes_count_as_host" for d in n.decorator_list)]
from mastercode.runtime import tools as _T  # noqa: E402


@_T._codes_count_as_host
async def _whose():
    return S.attempts_by()

with APP("evil-app"):
    _counted_as = run(_whose())
check(f"_execute_meta_tool is wrapped by _codes_count_as_host, under which a code counts as the Host's, "
      f"even inside an app's request ({_counted_as})", bool(_wrapped) and _counted_as == S.BY_HOST,
      (bool(_wrapped), _counted_as))

# Why the OAuth callback checks no code: the channel it builds needs none.
from server.reachout import service as _RO  # noqa: E402
_guarded = [c for n in ast.walk(_fn_tree(_ADD_CHANNEL)) if isinstance(n, ast.If)
            and isinstance(n.test, ast.Call) and getattr(n.test.func, "id", None) == "action_needs_stepup"
            for c in ast.walk(n) if isinstance(c, ast.Call) and getattr(c.func, "attr", None) == "verify_stepup"]
_all_checks = [c for c in ast.walk(_fn_tree(_ADD_CHANNEL))
               if isinstance(c, ast.Call) and getattr(c.func, "attr", None) == "verify_stepup"]
check("add_channel checks a code only for an action that needs one (action_needs_stepup)",
      _all_checks and len(_guarded) == len(_all_checks), (len(_guarded), len(_all_checks)))
_default = [n for n in ast.walk(_fn_tree("server.reachout.defaults:ensure_gmail_channel"))
            if isinstance(n, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == "type" for k in n.keys)]
_kinds = [v.value for d in _default for k, v in zip(d.keys, d.values)
          if isinstance(k, ast.Constant) and k.value == "type" and isinstance(v, ast.Constant)]
check(f"  the channel a Gmail consent builds is {_kinds}, which needs none, and nothing on that path "
      f"passes a code", _kinds == ["gmail_send"] and not _RO.action_needs_stepup({"type": "gmail_send"})
      and not _passes_no_code("server.reachout.defaults:_build")
      and not _passes_no_code("server.reachout.defaults:ensure_gmail_channel"), (_kinds,))

print("== a missing or malformed code is not an attempt")
p = fresh()
t = T0 + 30
for bad in ("", None, "   ", "12345", "1234567", "abcdef", "12 34 5"):
    for host in (False, True):
        check(f"{bad!r} ({'Host' if host else 'owner'}) is refused and not counted",
              verify(p, bad, t, host=host) is False and OWNER_ID not in p.s and HOST_ID not in p.s)
check("a code with spaces in it is still a code", verify(p, " ".join(code(t)), t) is True)

print("== wrong codes, then the wait (the owner's record)")
p = fresh()
check("a right code verifies", verify(p, code(T0), T0) is True)
t = T0 + 30
got = [verify(p, wrong(t), t) for _ in range(S.STEPUP_FREE_FAILURES)]
check(f"wrong codes 1..{S.STEPUP_FREE_FAILURES - 1} are refused as wrong",
      got[:-1] == [False] * (S.STEPUP_FREE_FAILURES - 1), got)
check(f"wrong code {S.STEPUP_FREE_FAILURES} starts the wait, and says so (429)", got[-1] == "locked", got[-1])
check(f"  the next try waits {S.STEPUP_LOCK_BASE_S} s", wait(p, t) == S.STEPUP_LOCK_BASE_S, wait(p, t))
t2 = t + 30
check("inside the wait even the right code is not accepted (429)", verify(p, code(t2), t2) == "locked")
check("  and a try inside the wait is not counted", wait(p, t2) == S.STEPUP_LOCK_BASE_S - 30, wait(p, t2))
check("the owner's wait does not hold the Host's record", wait(p, t2, host=True) == 0)
t3 = t + S.STEPUP_LOCK_BASE_S
check("once the wait is over the right code verifies (it was not burned by the refused try)",
      verify(p, code(t3), t3) is True)
check("  and the count is cleared", wait(p, t3) == 0 and OWNER_ID not in p.s)

print("== the wait doubles, to a ceiling")
t = t3 + 30
for _ in range(S.STEPUP_FREE_FAILURES):
    verify(p, wrong(t), t)
waits = []
for _ in range(10):
    w = wait(p, t)
    waits.append(w)
    t += w
    verify(p, wrong(t), t)
check("each wrong code after the free ones doubles the wait, up to an hour",
      waits[:7] == [60, 120, 240, 480, 960, 1920, 3600] and max(waits) == S.STEPUP_LOCK_MAX_S, waits)
t += wait(p, t)
check("the owner's right code after the wait still works", verify(p, code(t), t) is True)

print("== what a guesser gets, per record")
for host in (False, True):
    p2 = fresh()
    t, tries, end = T0 + 10 * DAY, 0, T0 + 11 * DAY
    while t < end:
        w = wait(p2, t, host=host)
        if w:
            t += w
            continue
        verify(p2, "999999" if code(t) != "999999" else "888888", t, host=host)
        tries += 1
        t += 1
    check(f"guessing as fast as allowed for a day as the {'Host' if host else 'owner'} gets {tries} tries",
          tries <= 40, tries)

print("== the answer: a 429 that says how long")
p = fresh()
t = T0 + 30
for host in (False, True):
    with (HOST() if host else contextlib.nullcontext()):
        for _ in range(S.STEPUP_FREE_FAILURES - 1):
            run(S.verify_stepup(p, wrong(t), at=t))
        try:
            run(S.verify_stepup(p, wrong(t), at=t))
            check(f"{'Host' if host else 'owner'}: the wrong code that starts the wait raises", False)
        except Exception as e:  # noqa: BLE001
            d = str(getattr(e, "detail", e))
            check(f"{'Host' if host else 'owner'}: 429, Retry-After, 'try again in 1 minute(s)'",
                  getattr(e, "status_code", None) == 429 and "try again in 1 minute(s)" in d
                  and (getattr(e, "headers", None) or {}).get("Retry-After") == "60", (type(e), d))
            check(f"  the Host's answer says it is the Host's record" if host
                  else "  the owner's answer is the plain one",
                  ("Host's own tools" in d) == host, d)

print("== a record that cannot be read")
import time as _clock  # noqa: E402
_wall = _clock.time


def at_wall(t, fn):
    """fn() with the wall clock reading t: the module reads it when no `at` is
    given, which is how the routes call it."""
    _clock.time = lambda: t
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — a crash is a failure to report, not to stop on
        return f"raised {type(e).__name__}: {e}"
    finally:
        _clock.time = _wall


for raw, why in ((b"not json", "not JSON"), (b"[1, 2]", "not an object"),
                 (b'{"n": "x", "at": 1}', "a count that is not a number"),
                 (b'{"n": 6, "at": NaN}', "a time that is not a number")):
    p = fresh()
    p.s[OWNER_ID] = raw
    t = T0 + 30
    w0 = at_wall(t, lambda: run(S.stepup_wait_seconds(p)))
    check(f"{why}: the record waits once, from now ({w0} s)", w0 == S.STEPUP_LOCK_BASE_S, w0)
    check("  and the row is repaired, so it reads as a record again",
          S._parse_failures(p.s[OWNER_ID]) is not None if hasattr(S, "_parse_failures") else False,
          p.s[OWNER_ID])
    t2 = t + S.STEPUP_LOCK_BASE_S
    w2 = at_wall(t2, lambda: run(S.stepup_wait_seconds(p)))
    check(f"  and once that wait is over, it is over ({w2} s; it used to start again at every read)",
          w2 == 0, w2)
    ok2 = at_wall(t2, lambda: verify(p, code(t2), None))
    check("  and the owner's right code then works", ok2 is True, ok2)
# Each read capped the wait at one lock
# from that read's own 'now', so the cap moved with every read, and a row
# dated two days ahead said 'wait 120 s' 1,430 times and freed the record only
# at +48 h. Read it as a caller would, across time: wait, come back, wait.
for ahead, n in ((10 * DAY, 6), (2 * DAY, 6), (90, 5), (3 * DAY, 1_000_000)):
    p = fresh()
    p.s[OWNER_ID] = b'{"n": %d, "at": %f}' % (n, T0 + ahead)
    t, reads = T0 + 30, 0
    while t < T0 + 4 * DAY:
        w = wait(p, t)
        if not w:
            break
        reads += 1
        t += w + 1
    lock = S._lock_seconds(n)
    check(f"a last failure {ahead / 3600:g} h in the future (n={n}) holds the record for one wait "
          f"from the first read, then it is free ({reads} read(s) waited; free {t - T0 - 30:.0f} s on)",
          reads == 1 and t - (T0 + 30) <= lock + 1, (reads, t - T0))
    t_ok = T0 + 30 + lock + 30
    check("  and the owner's right code then works", verify(p, code(t_ok), t_ok) is True)
p = fresh()
p.s[OWNER_ID] = b'{"n": 6, "at": %f}' % (T0 + 2 * DAY)
wait(p, T0 + 30)
check("  the row is written back as of the read that saw it",
      S._parse_failures(p.s[OWNER_ID]) == (6, T0 + 30), p.s[OWNER_ID])
p = fresh()
p.s[OWNER_ID] = b'{"n": 1000000, "at": %f}' % T0
_s = _clock.monotonic()
w = wait(p, T0 + 1)
_took = _clock.monotonic() - _s
check("an absurd count waits the ceiling, and answers at once",
      w == S.STEPUP_LOCK_MAX_S - 1 and _took < 1, (w, _took))

print("== every route asks the same way")
from fastapi import HTTPException  # noqa: E402
import time as _time  # noqa: E402
now = _time.time()


def locked_pool():
    p = fresh()
    p.s[OWNER_ID] = b'{"n": %d, "at": %f}' % (S.STEPUP_FREE_FAILURES, now)
    return p


def status_of(coro):
    try:
        run(coro)
        return None
    except HTTPException as e:
        return e.status_code, str(e.detail)


from server.routes import backup as RB  # noqa: E402
got = status_of(RB._require_stepup(locked_pool(), code(now)))
check("backup: locked -> 429 with the wait", got and got[0] == 429 and "minute" in got[1], got)
got = status_of(RB._require_stepup(fresh(), wrong(now)))
check("backup: a wrong code under the limit is still 403", got and got[0] == 403, got)

from server.routes import vault as RV  # noqa: E402
_lp = locked_pool()
RV._pool = lambda: _lp
got = status_of(RV.reveal_secret("k", RV.RevealReq(stepup_code=code(now)), None))
check("vault reveal: locked -> the same 429", got and got[0] == 429 and "minute" in got[1], got)

from server import brain_service  # noqa: E402
from server.routes import network as RN  # noqa: E402


class _Req:
    headers = {"content-type": "application/json"}

    def __init__(self, body):
        self._b = body

    async def json(self):
        return self._b


_lp2 = locked_pool()
brain_service.get_pool = lambda: _lp2
got = status_of(RN.nac_mgmt_resume(_Req({"server": "b1", "stepup_code": code(now)}), None))
check("network resume (through nac_commit's CommitError handling): locked -> the same 429",
      got and got[0] == 429 and "minute" in got[1], got)

# Structural: no caller wraps verify_stepup in a try that could turn the 429
# into something else. Every call site in server/ must propagate it.
def _calls_in_try(tree):
    """(line, inside a try body in its own function?) for every verify_stepup call."""
    out = []

    def walk(node, in_try):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                walk(ch, False)
                continue
            inner = in_try or (isinstance(node, ast.Try) and any(ch is b for b in node.body))
            if isinstance(ch, ast.Call):
                f = ch.func
                if (getattr(f, "attr", None) or getattr(f, "id", None)) in ("verify_stepup", "_verify_stepup"):
                    out.append((ch.lineno, inner))
            walk(ch, inner)
    walk(tree, False)
    return out


bad, sites = [], 0
for base, _, files in os.walk(os.path.join(ROOT, "server")):
    for fn in files:
        if fn.endswith(".py"):
            path = os.path.join(base, fn)
            for line, inside in _calls_in_try(ast.parse(open(path).read())):
                sites += 1
                if inside:
                    bad.append(f"{os.path.relpath(path, ROOT)}:{line}")
check(f"none of the {sites} verify_stepup call sites in server/ sits inside a try", sites >= 15 and not bad, bad)

print("== who counts as the Host")
from mastercode.runtime import tools as T  # noqa: E402
src = inspect.getsource(T._execute_meta_tool)
check("every Host tool call runs under _codes_count_as_host",
      hasattr(T, "_codes_count_as_host") and "@_codes_count_as_host" in src.split("async def", 1)[0])
seen = []


async def _probe():
    seen.append(S.attempts_by() if hasattr(S, "attempts_by") else "owner")


if hasattr(T, "_codes_count_as_host"):
    run(T._codes_count_as_host(_probe)())
    run(_probe())
check("  inside one it is the Host; outside, the owner", seen == ["host", "owner"], seen)

# The kernel-op token (app id "host"), through the real middleware.
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from server.applications import tokens as TK  # noqa: E402
from server.applications.middleware import ApplicationTokenMiddleware, HEADER_NAME  # noqa: E402

app = FastAPI()
app.add_middleware(ApplicationTokenMiddleware)
key = os.urandom(32)
app.state.kernel_secret = key
http_pool = fresh()


@app.post("/probe")
async def _probe_route(body: dict):
    by = S.attempts_by() if hasattr(S, "attempts_by") else "owner"
    ok = await S.verify_stepup(http_pool, body.get("code"), at=float(body["t"]))
    return {"by": by, "ok": ok}


client = TestClient(app)
host_tok, _ = TK.mint(key, app_id="host", permissions=[])
app_tok, _ = TK.mint(key, app_id="settings", permissions=[])
other_tok, _ = TK.mint(key, app_id="evil-app", permissions=[])
r = client.post("/probe", json={"code": "", "t": T0}, headers={HEADER_NAME: host_tok})
check("a request with the kernel-op token counts as the Host", r.status_code == 200 and r.json()["by"] == "host",
      r.text)
r = client.post("/probe", json={"code": "", "t": T0}, headers={HEADER_NAME: app_tok})
_rec = (run(S._records_of(http_pool, r.json()["by"]))[0]
        if r.status_code == 200 and hasattr(S, "_records_of") else (OWNER_ID,))
check("a request with the Settings app's token counts on the owner's record",
      r.status_code == 200 and _rec == (OWNER_ID,), (r.text, _rec))
r = client.post("/probe", json={"code": "", "t": T0}, headers={HEADER_NAME: other_tok})
check("a request with any other app's token counts as that app",
      r.status_code == 200 and r.json()["by"] == "app:evil-app", r.text)
last = None
for _ in range(S.STEPUP_FREE_FAILURES):
    last = client.post("/probe", json={"code": wrong(T0), "t": T0}, headers={HEADER_NAME: other_tok})
check("that app's wrong codes: a 429 with the wait, on its own record only",
      last.status_code == 429 and "app 'evil-app'" in last.json().get("detail", "")
      and APP_PREFIX + "evil-app" in http_pool.s and OWNER_ID not in http_pool.s,
      (last.status_code, last.text, sorted(http_pool.s)))
last = None
for _ in range(S.STEPUP_FREE_FAILURES):
    last = client.post("/probe", json={"code": wrong(T0), "t": T0}, headers={HEADER_NAME: host_tok})
check("the kernel-op token's wrong codes: a 429 with the wait, on the Host's record only",
      last.status_code == 429 and "minute" in last.json().get("detail", "")
      and last.headers.get("retry-after") == "60" and HOST_ID in http_pool.s and OWNER_ID not in http_pool.s,
      (last.status_code, last.text, sorted(http_pool.s)))
r = client.post("/probe", json={"code": code(T0 + 30), "t": T0 + 30}, headers={HEADER_NAME: app_tok})
check("  and the owner's right code goes straight through", r.status_code == 200 and r.json()["ok"] is True,
      r.text)

print()
if FAILED:
    print(f"FAILED: {len(FAILED)}")
    sys.exit(1)
print("OK: the second factor's attempt limit holds, and only ever holds up whoever sent the codes")
