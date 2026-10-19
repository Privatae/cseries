#!/usr/bin/env python3
"""A turn sent before the boot identity seed lands gets its tools, or waits
for them, or is told plainly why not. It never runs toolless.

A Host's tools grant (permission_overrides.bot_capabilities) is written by its
boot identity seed. On the platform that seed imported the identity over the
bridge and a turn could arrive a minute before it (seen on a live test run:
the turn at 01:08:03 ran with tools=None and the Host answered "I can't create
files ... Would you like instructions?"). A standalone Host writes its own
first-run identity (server/local_identity.py) in ONE local attempt, with no
bridge and no retry loop, so a turn can only meet the seed while that local
write is in flight, or after it failed for good. Both still hold or refuse the
turn; neither may run it toolless.

Container-free: APP_ROOT=<repo> python3 scripts/test_tools_before_first_turn.py.
The seed is the real identity_seed module; only the guarded seeder it calls
(routes.genesis._seed_codebook) is replaced, so a test can make the local
write slow, hang, or not take. The pool is a stub; no pod, no DB, no network,
no model.

  1. the seed: the first-run identity and its tools grant are written at once,
     in one attempt
  2. the seed never touches the bridge; the health check reads the standalone
     Host's NoPlatform and reports it as not connected, never ok
  3. the hold: a turn that arrives while the seed is in flight waits, then
     runs on the row that HAS the grant (the session is built after the seed)
  4. a local write that did not take: the turn is refused at once (nothing
     retries it), the reply says the Host stopped trying
  5. the write never lands: the turn does not reach the model; the person gets
     a fixed reply that says nothing was done, and it asks them nothing
  6. steady state costs nothing; an owner's explicit chat-only grant is kept
     by the seed and its turn runs (not refused)
  7. (folded into 2)
  8. a seed that crashed before it READ the row is "unknown", not "owed a
     grant": it retries the read and never refuses a Host (a chat-only Host
     with a transient DB error on the boot read answers normally)
  9. a refused chat request is kept (user row + a system note, surviving a
     reload) and runs by itself, once, when the tools arrive; the note never
     asks the person to send it again
 10. the refusal's wording follows how long the Host has been up
 11. non-chat callers get a not-run, never a result: a scheduled wake-up is
     put back and retried (no success, no run row, no report). (Circles'
     replay and A/B, the other non-chat callers, are gone.)
 12. concurrent first turns never start a second write: the seed makes one
     attempt, and every turn is held or told so
 13. a saved request passes the sender's app permissions through (the audit
     record; the tool gate does not narrow on them) and the thread's chosen
     model, is handed to a newer message instead of dropped, never runs twice
     (a wake LIVE in this process is waited for; a 'running' row nothing runs
     is taken over like a scheduled one; the takeover is repeated after the
     wait, the skip path never claims a carry, and a turn that fails puts the
     held request back), and its words are never copied
     into the calendar in PII mode; the saved wake is released when the grant
     lands; the woken turn is worded by the real
     state; deferrals back off and log once; the note claims only what was
     written, and the writes are bounded
 14. a message typed while a turn is held is sent on as the next turn when the
     turn is refused, never dropped (the composers of Circles and the
     Workbench refuse a plain send while a turn streams; the registry's
     nudge path is for another tab or client)
Exit 0 = all pass; exit 1 + [tools-first-turn] FAIL lines otherwise.
"""
import asyncio
import inspect
import json
import pathlib
import os
import sys
import time

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))

FAILS: list[str] = []
Path_ROOT = pathlib.Path(APP_ROOT)
ROOT_P = Path_ROOT


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"[tools-first-turn] FAIL {name} {extra}")


# server.main needs the pod's /app/dashboard to import. The health route and
# the circles routes only read its bridge-client accessor, so a module with the
# SAME accessor shape stands in, holding what lifespan boot puts there on a
# standalone Host: the real NoPlatform (server/main.py lifespan).
import types  # noqa: E402
STUB_MAIN = types.ModuleType("server.main")
STUB_MAIN._bridge_client_ref = None
STUB_MAIN.get_bridge_client = lambda: STUB_MAIN._bridge_client_ref
sys.modules.setdefault("server.main", STUB_MAIN)
import server.routes.genesis as genesis  # noqa: E402
from server import brain_service  # noqa: E402
from server.bridge import identity_seed as S  # noqa: E402
from server.bridge.no_platform import NoPlatform  # noqa: E402
from server.local_identity import first_run_bundle  # noqa: E402
from mastercode.runtime.tool_access import host_orchestration_enabled  # noqa: E402

try:
    from mastercode.runtime import identity_hold as H  # noqa: E402
except Exception as _imp_err:  # absent before the fix — reported, not crashed
    H = None
    print(f"  (identity_hold not importable: {_imp_err})")
_real_post_line = getattr(H, "post_line", None)

EID = 163
GRANT = {"bot_capabilities": 47}


# ── fixtures ────────────────────────────────────────────────────────────────
def placeholder_row():
    return {
        "id": EID,
        "display_name": "Host",
        "seed_weights": json.dumps(S.PLACEHOLDER_WEIGHTS),
        "behavioral_params": json.dumps({}),
        "permission_overrides": None,
    }


class FakePool:
    """entity_registry for one Host: the two statements the seed issues."""

    def __init__(self, row):
        self.row = row

    async def fetchrow(self, sql, *args):
        if "FROM entity_registry" in sql:
            return dict(self.row) if self.row is not None else None
        raise AssertionError(f"unexpected fetchrow: {sql}")

    async def execute(self, sql, *args):
        if sql.startswith("UPDATE calendar_events"):
            return "UPDATE 0"  # no saved wakes to cancel in this fixture
        if sql.startswith("UPDATE entity_registry SET permission_overrides"):
            if self.row.get("permission_overrides") is None:
                self.row["permission_overrides"] = args[1]
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute: {sql}")


class FakeBook:
    def __init__(self, pool):
        self.pool = pool
        self.reads = 0

    async def get_entity(self, entity_id):
        self.reads += 1
        r = self.pool.row
        return dict(r) if r is not None and entity_id == EID else None


# How the seed's next local write behaves: it lands `delay` seconds after it
# starts (None: it never does, a write hung on the DB). `lands` False is a
# write the guarded seeder refused (routes.genesis._seed_codebook swallows its
# own failures, so the row simply stays the placeholder); "raise" is one that
# raised (a DB error). `calls` counts writes.
SEED_WRITE = {"delay": 0.0, "lands": True, "calls": 0}


def local_write(*, delay=0.0, lands=True):
    SEED_WRITE.update({"delay": delay, "lands": lands, "calls": 0})


async def fake_seed_codebook(entity_id, display_name, codebook):
    SEED_WRITE["calls"] += 1
    delay = SEED_WRITE["delay"]
    # Always yield, as a real DB write does.
    await asyncio.sleep(3600 if delay is None else (delay or 0.001))
    if SEED_WRITE["lands"] == "raise":
        raise ConnectionError("the identity write failed")
    if not SEED_WRITE["lands"]:
        return
    row = _POOL_REF[0].row
    row["display_name"] = display_name
    row["seed_weights"] = json.dumps(codebook.get("seed_weights"))
    row["current_weights"] = json.dumps(codebook.get("current_weights"))
    row["behavioral_params"] = json.dumps(codebook.get("behavioral_params"))


_POOL_REF = [None]


def start_seed(pool, **write):
    """The boot seed as lifespan dispatches it on a standalone Host (with its
    NoPlatform), its local write behaving as `write` says (local_write)."""
    reset_seed(pool)
    local_write(**write)
    return asyncio.create_task(S.run_identity_seed(EID))


def reset_seed(pool):
    S.SEED_STATE.clear()
    S.SEED_STATE.update({"status": "unknown", "detail": "", "ts": None,
                         "entity_id": None, "retrying": False,
                         "started": 0, "done": 0, "placeholder_seen": False,
                         "read_failed": False, "began": None})
    if hasattr(S, "_KICK"):
        S._KICK = None
    _POOL_REF[0] = pool
    brain_service.get_pool = lambda: pool
    genesis._seed_codebook = fake_seed_codebook
    local_write()


async def wait_until(pred, limit):
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit:
        if pred():
            return time.monotonic() - t0
        await asyncio.sleep(0.02)
    return None


async def _stop(task):
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


# ── 1. the first-run identity is written at once, in one attempt ────────────
async def case_seed_timing():
    pool = FakePool(placeholder_row())
    task = start_seed(pool)
    took = await wait_until(lambda: S.SEED_STATE.get("status") == "ok", 3.0)
    await asyncio.sleep(0.2)   # room for a second attempt, were there one
    await _stop(task)
    check("seed: the first-run identity lands at boot, with no wait on anything",
          took is not None and took < 0.5, f"took={took} state={S.SEED_STATE}")
    check("seed: one attempt, one write",
          (S.SEED_STATE.get("started"), S.SEED_STATE.get("done"), SEED_WRITE["calls"]) == (1, 1, 1),
          f"state={S.SEED_STATE} writes={SEED_WRITE['calls']}")
    bundle = first_run_bundle()
    check("seed: the row holds the first-run identity (server/local_identity.py)",
          pool.row["display_name"] == bundle["display_name"]
          and json.loads(pool.row["behavioral_params"]).get("response_style")
          == bundle["codebook"]["behavioral_params"]["response_style"], repr(pool.row))
    check("seed: the tools grant is on the row",
          host_orchestration_enabled({"permission_overrides": pool.row["permission_overrides"]}),
          repr(pool.row.get("permission_overrides")))


# ── 2. the seed takes no bridge; health reads the NoPlatform ───────────────
async def case_no_bridge():
    import inspect
    pool = FakePool(placeholder_row())
    reset_seed(pool)
    check("no bridge: the seed is given only the entity id",
          list(inspect.signature(S.run_identity_seed).parameters) == ["entity_id"],
          str(inspect.signature(S.run_identity_seed)))
    task = asyncio.create_task(S.run_identity_seed(EID))
    took = await wait_until(lambda: S.SEED_STATE.get("status") == "ok", 2.0)
    await _stop(task)
    check("no bridge: the seed writes the identity and opens the tools gate",
          took is not None and host_orchestration_enabled(pool.row), f"state={S.SEED_STATE}")
    src = open(os.path.join(APP_ROOT, "server/bridge/identity_seed.py")).read()
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    check("no bridge: identity_seed.py imports nothing from the bridge client",
          "bridge_client" not in code, "")


# ── 3. a turn during the seed waits, then runs WITH tools ──────────────────
async def case_turn_waits_for_tools():
    from mastercode.runtime.session import EntityRuntime
    pool = FakePool(placeholder_row())
    tasks = [start_seed(pool, delay=0.6)]   # the local write is slow
    await wait_until(lambda: S.SEED_STATE.get("status") == "placeholder", 1.0)
    check("turn: (setup) the turn arrives while the row grants no tools",
          not host_orchestration_enabled(pool.row), repr(pool.row))

    rt = object.__new__(EntityRuntime)
    rt.book = FakeBook(pool)
    seen = []
    frames = []

    async def build(entity_id, **_kw):
        ent = await rt.book.get_entity(entity_id)
        seen.append(host_orchestration_enabled(ent))
        return None  # stop the turn here: what matters is the row it was built from

    async def cb(ev):
        frames.append(ev)

    rt._build_chat_context_transient = build
    out = await rt.send_message(entity_id=EID, user_message="Put a file called x.txt in your workspace.",
                                progress_cb=cb)
    for t in tasks:
        await _stop(t)
    check("turn: the session is built only after the grant lands — with tools",
          seen == [True], f"seen={seen} out={out} state={S.SEED_STATE}")
    check("turn: the person sees why it is waiting (a live status line)",
          any(f.get("event") == "reasoning_delta" and "Loading my tools" in (f.get("text") or "")
              for f in frames), repr(frames))


async def case_durable_path_waits():
    """The voice / demo path loads a stored session row instead of building
    one; the hold applies there too, and the session is re-loaded after it."""
    from mastercode.runtime.session import EntityRuntime
    pool = FakePool(placeholder_row())
    tasks = [start_seed(pool, delay=0.6)]   # the local write is slow
    await wait_until(lambda: S.SEED_STATE.get("status") == "placeholder", 1.0)
    rt = object.__new__(EntityRuntime)
    rt.book = FakeBook(pool)
    loads = []

    class _Sess:
        def __init__(self, ent):
            self.entity_id = EID
            self.entity = ent

    async def load(session_id):
        ent = await rt.book.get_entity(EID)
        loads.append(host_orchestration_enabled(ent))
        # The second load is the one the turn would run on; stop there.
        return _Sess(ent) if len(loads) == 1 else None

    rt._load_session_from_db = load
    await rt.send_message(session_id="voice-1", user_message="Put a file called x.txt in your workspace.")
    for t in tasks:
        await _stop(t)
    check("durable: the stored session is re-loaded after the hold, with tools",
          loads == [False, True], f"loads={loads}")


# ── 4. a local write that did not take: refused at once, nothing retries ───
async def case_write_did_not_take():
    if H is None:
        check("failed write: identity_hold exists", False)
        return
    pool = FakePool(placeholder_row())
    task = start_seed(pool, lands="raise")
    await wait_until(lambda: S.SEED_STATE.get("status") == "failed", 2.0)
    check("failed write: (setup) the seed tried once, failed, and is not retrying",
          S.SEED_STATE.get("status") == "failed" and S.SEED_STATE.get("retrying") is False
          and S.SEED_STATE.get("placeholder_seen") is True and SEED_WRITE["calls"] == 1,
          f"{S.SEED_STATE} writes={SEED_WRITE['calls']}")
    t0 = time.monotonic()
    _bk = FakeBook(pool)
    held, refusal = await H.hold_for_identity(EID, load_entity=lambda: _bk.get_entity(EID))
    took = time.monotonic() - t0
    await asyncio.sleep(0.3)
    await _stop(task)
    text = (refusal or {}).get("response", "")
    check("failed write: the turn is refused at once, not after the wait cap",
          held and refusal is not None and took < 0.5, f"took={took:.2f} refusal={refusal}")
    check("failed write: the reply says nothing was done and that the Host stopped trying",
          "Nothing has been done" in text and "stopped retrying" in text
          and "keeps retrying" not in text, text)
    check("failed write: a turn's arrival never starts a second write",
          SEED_WRITE["calls"] == 1, f"writes={SEED_WRITE['calls']}")

    # The guarded seeder refused the identity (it swallows that) but the seed
    # still wrote the tools grant onto the NULL column: the state is failed,
    # yet the row grants tools, so the turn runs with them, unheld.
    pool = FakePool(placeholder_row())
    task = start_seed(pool, lands=False)
    await wait_until(lambda: S.SEED_STATE.get("status") == "failed", 2.0)
    _bk = FakeBook(pool)
    held, refusal = await H.hold_for_identity(EID, load_entity=lambda: _bk.get_entity(EID))
    await _stop(task)
    check("failed write: an identity the guard refused still leaves the tools grant, and the turn runs",
          S.SEED_STATE.get("status") == "failed" and S.SEED_STATE.get("retrying") is False
          and host_orchestration_enabled(pool.row) and (held, refusal) == (False, None),
          f"state={S.SEED_STATE} row={pool.row.get('permission_overrides')} held={held} refusal={refusal}")


# ── 5. the write never lands: no toolless model call, a plain reply ─────────
async def case_write_never_lands():
    from mastercode.runtime.session import EntityRuntime
    pool = FakePool(placeholder_row())
    _orig_wait = getattr(S, "TURN_WAIT_S", None)
    S.TURN_WAIT_S = 0.6  # the bound under test is TURN_WAIT_S itself; shortened here
    tasks = [start_seed(pool, delay=None)]   # the local write hangs
    await wait_until(lambda: S.SEED_STATE.get("status") == "placeholder", 1.0)
    rt = object.__new__(EntityRuntime)
    rt.book = FakeBook(pool)
    rt.pool = None
    built = []

    async def build(entity_id, **_kw):
        built.append(entity_id)
        return None

    rt._build_chat_context_transient = build
    t0 = time.monotonic()
    out = await rt.send_message(entity_id=EID, user_message="Put a file called x.txt in your workspace.")
    took = time.monotonic() - t0
    for t in tasks:
        await _stop(t)
    if _orig_wait is not None:
        S.TURN_WAIT_S = _orig_wait
    text = (out or {}).get("response") or ""
    check("never: the turn does not reach the model without its tools",
          built == [], f"built={built} out={out}")
    check("never: the hold is bounded", took < 2.0, f"took={took:.2f}")
    check("never: the reply says nothing was done, and why",
          "Nothing has been done" in text and "tools have not loaded" in text, text)
    check("never: the reply asks the person nothing (no go-ahead question)",
          "?" not in text, text)
    check("never: the result is marked for the stream and the logs",
          (out or {}).get("identity_pending") is True, repr(out))


# ── 6. steady state costs nothing; an owner's chat-only grant runs ──────────
async def case_steady_and_chat_only():
    if H is None:
        check("steady: identity_hold exists", False)
        return
    pool = FakePool({**placeholder_row(), "display_name": "Sage",
                     "permission_overrides": {"bot_capabilities": 255}})
    reset_seed(pool)
    S.SEED_STATE.update({"status": "ok", "entity_id": EID})
    book = FakeBook(pool)
    held, refusal = await H.hold_for_identity(EID, load_entity=lambda: book.get_entity(EID))
    check("steady: a seeded Host is not held and its row is not even re-read",
          (held, refusal) == (False, None) and book.reads == 0, f"{held} {refusal} reads={book.reads}")

    reset_seed(pool)  # status 'unknown': no seed ran in this process (offline)
    held, refusal = await H.hold_for_identity(EID, load_entity=lambda: book.get_entity(EID))
    check("steady: no seed in this process → no hold", (held, refusal) == (False, None))

    # The row already holds an explicit chat-only grant (the owner set one
    # before the seed ran): the seed writes its grant only onto a NULL column,
    # so the turn runs as that tier, the real answer, not a pending one.
    chat_only = {"bot_capabilities": 0}
    pool = FakePool({**placeholder_row(), "permission_overrides": dict(chat_only)})
    tasks = [start_seed(pool, delay=0.3)]
    await wait_until(lambda: S.SEED_STATE.get("status") == "placeholder", 1.0)
    _bk = FakeBook(pool)
    held, refusal = await H.hold_for_identity(EID, load_entity=lambda: _bk.get_entity(EID))
    for t in tasks:
        await _stop(t)
    check("chat-only: the first-run grant never overwrites a grant already on the row",
          pool.row["permission_overrides"] == chat_only, repr(pool.row["permission_overrides"]))
    check("chat-only: a seeded Host whose real grant is chat-only runs (not refused)",
          held and refusal is None and S.SEED_STATE.get("status") == "ok",
          f"held={held} refusal={refusal} state={S.SEED_STATE}")


# ── 7. the health check: the REAL route, with the standalone Host's client ──
class _Conn:
    async def fetchval(self, *_a, **_k):
        return 1


class _Acquire:
    async def __aenter__(self):
        return _Conn()

    async def __aexit__(self, *_a):
        return False


class _HealthPool:
    def acquire(self):
        return _Acquire()


async def case_health_check():
    import server.routes.system as system
    import server.mtls_frontdoor as frontdoor
    server_main = STUB_MAIN
    _fd_state = frontdoor.STATE
    frontdoor.STATE = "listening"
    saved = (brain_service.get_pool, brain_service.get_runtime,
             getattr(server_main, "_bridge_client_ref", None))
    brain_service.get_pool = lambda: _HealthPool()

    class _RT:
        _loaded = True
    brain_service.get_runtime = lambda: _RT()
    try:
        server_main._bridge_client_ref = NoPlatform()
        frontdoor.STATE = "waiting-cert"
        body = await system.health_deep()
        down, fd_standalone = body["checks"]["bridge"], body["checks"]["mtls_frontdoor"]
        server_main._bridge_client_ref = None
        body = await system.health_deep()
        none_, fd_none = body["checks"]["bridge"], body["checks"]["mtls_frontdoor"]
    finally:
        brain_service.get_pool, brain_service.get_runtime = saved[0], saved[1]
        frontdoor.STATE = _fd_state
    check("health: a standalone Host's NoPlatform reads as not used (never ok, never a warning)",
          down == "not used (standalone)", down)
    check("health: on a standalone Host the front door waiting for a certificate is not used",
          fd_standalone == "not used (standalone)", fd_standalone)
    check("health: no client at all reads a warning", none_.startswith("warn"), none_)
    check("health: without a NoPlatform a front door waiting for its certificate still warns",
          fd_none.startswith("warn") and "waiting-cert" in fd_none, fd_none)


# ── 8. a crash before the row was read is "unknown", not "owed a grant" ─────
class FlakyReadPool(FakePool):
    """entity_registry reads raise `fail_reads` times (a transient DB error on
    the boot read) and then answer."""

    def __init__(self, row, fail_reads):
        super().__init__(row)
        self.fail_reads = fail_reads
        self.seed_reads = 0

    async def fetchrow(self, sql, *args):
        if "FROM entity_registry" in sql and "display_name, seed_weights" in sql:
            self.seed_reads += 1
            if self.seed_reads <= self.fail_reads:
                raise ConnectionError("transient DB error on the boot read")
        return await super().fetchrow(sql, *args)


def chat_only_row():
    return {**placeholder_row(), "display_name": "Sage",
            "seed_weights": json.dumps({**S.PLACEHOLDER_WEIGHTS, "warmth": 0.4}),
            "behavioral_params": json.dumps({"response_style": "conversational"}),
            "permission_overrides": None}


async def _turn_on(pool, msg="hello"):
    from mastercode.runtime.session import EntityRuntime
    rt = object.__new__(EntityRuntime)
    rt.book = FakeBook(pool)
    rt.pool = pool
    built = []

    async def build(entity_id, **_kw):
        built.append(entity_id)
        return None
    rt._build_chat_context_transient = build
    t0 = time.monotonic()
    out = await rt.send_message(entity_id=EID, user_message=msg)
    return out, built, time.monotonic() - t0


async def case_boot_read_crash():
    _base = S._READ_RETRY_BASE_S
    S._READ_RETRY_BASE_S = 0.2
    try:
        # (a) the read fails once, then succeeds: a chat-only Host.
        pool = FlakyReadPool(chat_only_row(), fail_reads=1)
        reset_seed(pool)
        task = asyncio.create_task(S.run_identity_seed(EID))
        await wait_until(lambda: S.SEED_STATE.get("read_failed"), 1.0)
        check("crash: (setup) the boot read failed and is being retried",
              S.SEED_STATE.get("status") == "checking" and S.SEED_STATE.get("read_failed")
              and S.SEED_STATE.get("placeholder_seen") is False, repr(S.SEED_STATE))
        check("crash: while the read is retried the hold WAITS (a grant may still be owed)",
              S.identity_unresolved(EID) is True)
        out, built, took = await _turn_on(pool)
        check("crash: a chat-only Host's turn waits for the retried read, then RUNS (never refused)",
              built == [EID] and not (out or {}).get("identity_pending")
              and 0.1 < took < 1.5 and S.SEED_STATE.get("status") == "ok",
              f"built={built} out={out} took={took:.2f} state={S.SEED_STATE}")
        check("crash: the read is retried and the seed settles ok (no write needed)",
              S.SEED_STATE.get("status") == "ok" and SEED_WRITE["calls"] == 0, repr(S.SEED_STATE))
        await _stop(task)

        # (b) the read never recovers: the turn waits out its cap, then runs —
        # an unseen placeholder is never a refusal.
        pool = FlakyReadPool(chat_only_row(), fail_reads=10 ** 6)
        reset_seed(pool)
        _w0 = S.TURN_WAIT_S
        S.TURN_WAIT_S = 0.5
        task = asyncio.create_task(S.run_identity_seed(EID))
        await wait_until(lambda: S.SEED_STATE.get("read_failed"), 1.0)
        out, built, took = await _turn_on(pool)
        S.TURN_WAIT_S = _w0
        await _stop(task)
        check("crash: a read that keeps failing holds the turn to its cap, then runs it, never refuses",
              built == [EID] and not (out or {}).get("identity_pending") and 0.4 < took < 1.5,
              f"built={built} out={out} took={took:.2f}")

        # (c) a row that already holds a real grant is never refused, whatever
        # the seed's state says.
        pool = FakePool({**chat_only_row(), "permission_overrides": GRANT})
        reset_seed(pool)
        S.SEED_STATE.update({"status": "failed", "entity_id": EID, "retrying": False,
                             "placeholder_seen": False, "detail": "identity seed loop crashed: x"})
        out, built, took = await _turn_on(pool)
        check("crash: a Host whose row holds a grant runs after a pre-read crash",
              built == [EID] and not (out or {}).get("identity_pending"), repr(out))
        S.SEED_STATE.update({"placeholder_seen": True})
        out, built, took = await _turn_on(pool)
        check("crash: ...and after a seen placeholder too (the grant is on the row)",
              built == [EID] and not (out or {}).get("identity_pending"), repr(out))

        # (d) a chat-only row in a pre-read 'failed' state (no placeholder seen)
        # runs: a failed state is only a refusal once the placeholder was seen.
        pool = FakePool(chat_only_row())
        reset_seed(pool)
        S.SEED_STATE.update({"status": "failed", "entity_id": EID, "retrying": True,
                             "placeholder_seen": False, "detail": "x"})
        out, built, took = await _turn_on(pool)
        check("crash: a 'failed' seed that never saw the placeholder does not refuse",
              built == [EID] and not (out or {}).get("identity_pending") and took < 1.0,
              f"built={built} out={out} took={took:.2f}")

        # (e) the boot read is still IN FLIGHT (hung, not failed) when a turn
        # holds for it: the hold times out with the placeholder never seen, and
        # an unseen placeholder is not a refusal either.
        class HungReadPool(FakePool):
            async def fetchrow(self, sql, *args):
                if "display_name, seed_weights" in sql:
                    await asyncio.sleep(30)
                return await super().fetchrow(sql, *args)
        pool = HungReadPool(chat_only_row())
        reset_seed(pool)
        _w = S.TURN_WAIT_S
        S.TURN_WAIT_S = 0.3
        task = asyncio.create_task(S.run_identity_seed(EID))
        await asyncio.sleep(0.05)
        out, built, took = await _turn_on(pool)
        S.TURN_WAIT_S = _w
        await _stop(task)
        check("crash: a turn that outwaits a hung boot read runs (an unseen placeholder never refuses)",
              built == [EID] and not (out or {}).get("identity_pending") and 0.25 < took < 1.5,
              f"built={built} out={out} took={took:.2f}")
    finally:
        S._READ_RETRY_BASE_S = _base


# ── 9. a refused request is kept and runs by itself, once ───────────────────
class StorePool(FakePool):
    """FakePool plus the writes a refused request makes: turn rows (fetchval
    INSERT INTO entity_memory), the wake (fetchrow INSERT INTO
    calendar_events) and the runner's own bookkeeping (execute). It also
    answers the few reads the runner and the hold make about those rows, so a
    wake can be cancelled, found superseded, or read back by its status."""

    def __init__(self, row):
        super().__init__(row)
        self.memory = []
        self.events = []          # event i has id i + 1
        self.executed = []
        self.hang_memory = False
        self.fail_memory = False
        self.fail_events = False
        self.fail_cancel = False  # the takeover UPDATE raises
        self.queries = []         # every calendar_events statement the hold / runner issued
        self.released = []        # events made due now by the seed

    async def fetchval(self, sql, *args):
        if "INSERT INTO entity_memory" in sql:
            if self.hang_memory:
                await asyncio.sleep(60)
            if self.fail_memory:
                raise ConnectionError("memory insert failed")
            self.memory.append({"entity_id": args[0], "content": args[1],
                                "source_session": args[2], "metadata": json.loads(args[3]),
                                "created_at": args[-1]})
            return len(self.memory)
        if "FROM calendar_event_runs" in sql:
            return 0
        if "SELECT content FROM entity_memory" in sql:
            for m in self.memory:
                if (m["source_session"] == args[1]
                        and m["metadata"].get("role") == "user"):
                    return m["content"]
            return None
        if sql.startswith("SELECT 1 FROM calendar_events"):
            self.queries.append(" ".join(sql.split()))
            return 1 if any(e["payload"].get("saved_request")
                            and (e["status"] in ("scheduled", "running")
                                 or (e["status"] == "cancelled" and e.get("reason") == "held_for_person"))
                            for e in self.events) else None
        if "jsonb_set(payload" in sql:
            # fold into the newest OTHER pending saved wake of the thread
            self.queries.append("FOLD")
            skip, thread, ic, ev_id = args[3], args[1], args[2], args[3]
            for i in range(len(self.events) - 1, -1, -1):
                ev = self.events[i]
                o = ev["payload"].get("origin") or {}
                if (i + 1 != ev_id and ev["status"] == "scheduled" and ev["payload"].get("saved_request")
                        and (o.get("thread_id") or "") == thread
                        and (o.get("interaction_context") or "") == ic):
                    ev["payload"].setdefault("earlier", []).extend(json.loads(args[4]))
                    return i + 1
            return None
        if "SELECT status FROM calendar_events" in sql:
            return self.events[args[0] - 1]["status"]
        if "FROM entity_memory" in sql and "created_at >" in sql:
            _e, saved_at, kind, ref = args
            for m in self.memory:
                md = m["metadata"]
                sc = md.get("source_context") or {}
                if (md.get("role") == "user" and m["created_at"] > saved_at
                        and sc.get("kind") == kind and str(sc.get("ref")) == ref
                        and md.get("app_id") not in ("circles-replay", "circles-ab",
                                                     "scheduled_task")):
                    return 1
            return None
        return None

    def _by_event_id(self, event_id):
        for ev in self.events:
            if str(ev.get("event_id")) == str(event_id):
                return ev
        return None

    async def fetchrow(self, sql, *args):
        flat_ = " ".join(sql.split())
        if "SET status='running'" in flat_ and "RETURNING id, title" in flat_:    # the runner's claim
            ev = self.events[args[0] - 1]
            if ev["status"] != "scheduled":
                return None
            ev["status"] = "running"
            return {"id": args[0], "title": ev["summary"], "recurrence": None,
                    "start_time": None, "payload": ev["payload"]}
        if flat_.startswith("SELECT status FROM calendar_events WHERE event_id="):
            ev = self._by_event_id(args[0])
            return {"status": ev["status"]} if ev else None
        if flat_.startswith("SELECT payload FROM calendar_events WHERE event_id="):
            ev = self._by_event_id(args[0])
            return {"payload": json.dumps(ev["payload"])} if ev else None
        if "INSERT INTO calendar_events" in sql:
            if self.fail_events:
                raise ConnectionError("calendar insert failed")
            from datetime import datetime, timezone
            self.events.append({"event_type": args[1], "summary": args[3],
                                "payload": json.loads(args[4]), "status": args[6]})
            return {"event_id": "00000000-0000-0000-0000-000000000001",
                    "event_at": datetime.now(timezone.utc), "status": args[6]}
        return await super().fetchrow(sql, *args)

    def _matching(self, status, args):
        out = []
        for i, ev in enumerate(self.events):
            pl, o = ev["payload"], ev["payload"].get("origin") or {}
            if (ev["status"] == status and pl.get("saved_request")
                    and (o.get("thread_id") or "") == args[1]
                    and (o.get("interaction_context") or "") == args[2]):
                out.append(i)
        return out

    async def fetch(self, sql, *args):
        flat = " ".join(sql.split())
        if flat.startswith("SELECT id FROM calendar_events") and "start_time <= NOW()" in flat:
            return [{"id": i + 1} for i, e in enumerate(self.events) if e["status"] == "scheduled"]
        if self.fail_cancel and flat.startswith("UPDATE calendar_events"):
            raise ConnectionError("takeover update failed")
        if flat.startswith("SELECT id FROM calendar_events") and "status='running'" in flat:
            self.queries.append(flat)
            return [{"id": i + 1} for i in self._matching("running", args)]
        if (flat.startswith("UPDATE calendar_events") and "RETURNING id, payload" in flat
                and "AND status='cancelled' AND cancelled_reason=$5" in flat and args[4] == "held_for_person"):
            # the person's next message takes back what a blocked turn kept for it
            self.queries.append(flat)
            rows = []
            for i, ev in enumerate(self.events):
                o = ev["payload"].get("origin") or {}
                if (ev["status"] == "cancelled" and ev.get("reason") == "held_for_person"
                        and (o.get("thread_id") or "") == args[1]
                        and (o.get("interaction_context") or "") == args[2]):
                    ev["reason"] = "taken"
                    ev["age_s"] = 0
                    if "taken_boot" in flat:             # the statement records which process took it
                        ev["payload"]["taken_boot"] = args[3]
                    rows.append({"id": i + 1, "payload": json.dumps(ev["payload"])})
            return rows
        if flat.startswith("UPDATE calendar_events") and "RETURNING id, payload" in flat:
            self.queries.append(flat)
            rows = []
            if "status='running' AND id = ANY" in flat:
                for i in args[1]:
                    ev = self.events[i - 1]
                    if ev["status"] == "running":
                        ev["status"], ev["reason"], ev["age_s"] = "cancelled", "taken", 0
                        if "'{taken_boot}'" in flat:
                            ev["payload"]["taken_boot"] = args[2]
                        if "'{crash_unknown}'" in flat:       # the statement records "may have acted"
                            ev["payload"]["crash_unknown"] = True
                        rows.append({"id": i, "payload": json.dumps(ev["payload"])})
                return rows
            for i in self._matching("scheduled", args):
                self.events[i]["status"] = "cancelled"
                self.events[i]["reason"] = "taken"
                self.events[i]["age_s"] = 0
                if "taken_boot" in flat:
                    self.events[i]["payload"]["taken_boot"] = args[3]
                rows.append({"id": i + 1, "payload": json.dumps(self.events[i]["payload"])})
            return rows
        return []

    async def execute(self, sql, *args):
        flat = " ".join(sql.split())
        if flat.startswith("UPDATE calendar_events") or "calendar_event_runs" in flat:
            self.executed.append((flat, args))
            # ── the orphan sweep (identity_hold.sweep_stuck_running) ──────────
            if "AND NOT (id = ANY($3::bigint[]))" in flat and "status='running'" in flat:
                saved_stmt = "SET status='cancelled', cancelled_reason=$4" in flat
                n = 0
                for i, ev in enumerate(self.events):
                    is_saved = bool(ev["payload"].get("saved_request"))
                    if ev["status"] != "running" or ev.get("age_s", 0) <= args[1] or (i + 1) in args[2]:
                        continue
                    if saved_stmt and is_saved:
                        ev["status"], ev["reason"] = "cancelled", args[3]
                        ev["payload"]["crash_unknown"] = True
                        n += 1
                    elif (not saved_stmt) and not (is_saved and "<> 'true'" in flat):
                        ev["status"] = "scheduled"
                        n += 1
                return f"UPDATE {n}"
            # ── the runner's own re-queues (nothing ran) ──────────────────────
            if "SET status='scheduled', updated_at=NOW() WHERE id=$1 AND entity_id=$2 AND status='running'" in flat:
                if self.events[args[0] - 1]["status"] == "running":
                    self.events[args[0] - 1]["status"] = "scheduled"
                return "UPDATE 1"
            if "plan_waits" in flat and "SET status='scheduled'" in flat:
                if self.events[args[0] - 1]["status"] == "running":
                    self.events[args[0] - 1]["status"] = "scheduled"
                return "UPDATE 1"
            if "SET updated_at=NOW() WHERE id=$1 AND entity_id=$2 AND status='running'" in flat:
                return "UPDATE 1"
            if "payload - 'plan_event_id'" in flat:
                self.events[args[0] - 1]["payload"].pop("plan_event_id", None)
                return "UPDATE 1"
            if "status = 'cancelled'" in flat and "WHERE id = $1 AND entity_id = $2" in flat:
                self.events[args[0] - 1]["status"] = "cancelled"
                self.events[args[0] - 1]["reason"] = "the thread it was set in was deleted"
                return "UPDATE 1"
            # ── cancels / completes by event_id (person, Host, Agents, browser) ──
            if "SET status='cancelled', cancelled_reason=$1" in flat:
                ev = self._by_event_id(args[1])
                if ev:
                    ev["status"], ev["reason"] = "cancelled", args[0]
                return "UPDATE 1"
            if "SET status='cancelled'" in flat and "WHERE event_id=$1::uuid" in flat:
                ev = self._by_event_id(args[0])
                allowed = ("scheduled", "running") if "status IN ('scheduled','running')" in flat else ("scheduled",)
                if ev and ev["status"] in allowed:
                    ev["status"] = "cancelled"
                    return "UPDATE 1"
                return "UPDATE 0"
            if "SET status='completed', payload=$1::jsonb" in flat:
                ev = self._by_event_id(args[1])
                if ev:
                    ev["status"] = "completed"
                return "UPDATE 1"
            # identity_hold.reschedule, flagged rows: a request whose turn died with its
            # process goes back to held, never to scheduled
            if ("payload->>'crash_unknown'='true'" in flat and "AND cancelled_reason=$4" in flat
                    and "SET cancelled_reason=$3" in flat):
                for i in args[1]:
                    ev = self.events[i - 1]
                    if (ev["status"] == "cancelled" and ev.get("reason") == "taken"
                            and ev["payload"].get("crash_unknown")):
                        ev["reason"] = args[2]
                return "UPDATE 1"
            if "SET status='cancelled', cancelled_reason=$2" in flat:     # a flagged row found scheduled
                for ev in (self.events if "payload->>'crash_unknown'='true'" in flat else []):
                    if (ev["status"] == "scheduled" and ev["payload"].get("saved_request")
                            and ev["payload"].get("crash_unknown")):
                        ev["status"], ev["reason"] = "cancelled", args[1]
                return "UPDATE 1"
            if "cancelled_reason=NULL" in flat:      # a turn that did not act puts its takeover back
                guard = "<> 'true'" in flat          # the statement itself excludes flagged rows
                for i in args[1]:
                    ev = self.events[i - 1]
                    if ev["status"] == "cancelled" and not (guard and ev["payload"].get("crash_unknown")):
                        ev["status"] = "scheduled"
                        ev["deferred"] = True            # later, with the normal backoff
                        ev["payload"]["identity_defers"] = int(ev["payload"].get("identity_defers") or 0) + 1
                return "UPDATE 1"
            if "{crash_unknown}" in flat:            # boot sweep of takeovers whose process died
                n = 0
                for ev in self.events:
                    if (ev["status"] == "cancelled" and ev.get("reason") == "taken"
                            and ((ev["payload"].get("taken_boot") or "") != args[4]
                                 or ev.get("age_s", 0) > args[3])):
                        ev["reason"] = args[2]
                        ev["payload"]["crash_unknown"] = True
                        n += 1
                return f"UPDATE {n}"
            if "SET cancelled_reason=$4" in flat:    # ...or keeps them for the person's next message
                for i in args[1]:
                    ev = self.events[i - 1]
                    if ev["status"] == "cancelled" and ev.get("reason", "taken") == "taken":
                        ev["reason"] = args[3]
                return "UPDATE 1"
            if "- 'identity_defers'" in flat and "start_time=$2" in flat:   # a re-arm
                self.events[args[0] - 1]["payload"].pop("identity_defers", None)
                return "UPDATE 1"
            if "start_time=NOW()" in flat and "saved_request" in flat:
                guard = "<> 'true'" in flat
                for ev in self.events:
                    if (ev["payload"].get("saved_request") and ev["status"] == "scheduled"
                            and not (guard and ev["payload"].get("crash_unknown"))):
                        ev["released"] = True
                        self.released.append(time.monotonic())
                return "UPDATE 1"
            if "SET status=$2" in flat:
                ev = self.events[args[0] - 1]
                # the real statement says `AND status <> 'cancelled'`: a
                # cancelled row is never finalized over
                if not (ev["status"] == "cancelled" and "status <> 'cancelled'" in flat):
                    ev["status"] = args[1]
            elif "identity_defers" in flat:
                self.events[args[0] - 1]["status"] = "scheduled"
                self.events[args[0] - 1]["payload"]["identity_defers"] = args[3]
            return "UPDATE 1"
        return await super().execute(sql, *args)

    def reload_thread(self, thread_ref):
        """What a reader of the thread sees after a page reload: the saved
        rows of that thread, oldest first."""
        rows = [m for m in self.memory
                if (m["metadata"].get("source_context") or {}).get("ref") == thread_ref]
        return sorted(rows, key=lambda m: m["created_at"])


THREAD = "0b8f2c1e-4d3a-4a55-9a77-0c1d2e3f4a5b"
MSG = "Put a file called x.txt in your workspace."


async def _refused_turn(pool, **kw):
    """One chat turn refused by the hold (the seed's local write hangs)."""
    from mastercode.runtime.session import EntityRuntime
    import mastercode.memory_engine as me
    S.TURN_WAIT_S = 0.4
    task = start_seed(pool, delay=None)   # the local write hangs
    await wait_until(lambda: S.SEED_STATE.get("status") == "placeholder", 1.0)

    async def no_embed(_text):
        raise RuntimeError("no embedder in this test")
    me._get_memory_embedding = no_embed
    brain_service._entity_id = EID
    rt = object.__new__(EntityRuntime)
    rt.book = FakeBook(pool)
    rt.pool = pool
    built = []

    async def build(entity_id, **_kw):
        built.append(entity_id)
        return None
    rt._build_chat_context_transient = build
    out = await rt.send_message(entity_id=EID, user_message=MSG, **kw)
    return rt, out, built, task


async def case_refusal_saved_and_runs_once():
    import mastercode.memory_engine as me
    from mastercode.brain import offline_task_runner as otr
    _orig_embed = me._get_memory_embedding
    _orig_wait = S.TURN_WAIT_S
    _orig_eid = brain_service._entity_id
    _orig_rt = brain_service.get_runtime
    try:
        pool = StorePool(placeholder_row())
        rt, out, built, task = await _refused_turn(
            pool, app_id="circles", thread_id=THREAD)
        text = (out or {}).get("response") or ""
        check("saved: the turn is refused and never reaches the model",
              (out or {}).get("identity_pending") is True and built == [], repr(out))
        rows = pool.reload_thread(THREAD)
        check("saved: after a reload the thread holds the person's message, then the note",
              len(rows) == 2 and (rows[0]["metadata"].get("role"), rows[0]["content"]) == ("user", MSG)
              and rows[1]["metadata"].get("role") == "assistant",
              repr([(r["metadata"].get("role"), r["content"][:40]) for r in rows]))
        note = rows[1]["metadata"] if len(rows) == 2 else {}
        check("saved: the note is the system-note shape (origin='cancelled')",
              note.get("origin") == "cancelled" and note.get("stopped_reason") == "identity_pending",
              repr(note))
        check("saved: the note's text is the reply the stream showed",
              len(rows) == 2 and rows[1]["content"] == text, text)
        low = text.lower()
        check("saved: the note says the request WILL run, and never asks for a resend",
              "will run by itself" in low and "as soon as my tools arrive" in low
              and "again" not in low and "?" not in low, text)
        check("saved: exactly one wake is scheduled; it points at the saved row (no copy of the words)",
              len(pool.events) == 1 and "prompt" not in pool.events[0]["payload"]
              and pool.events[0]["payload"].get("user_row", {}).get("session_id")
              == rows[0]["source_session"]
              and pool.events[0]["payload"].get("saved_request") is True
              and pool.events[0]["payload"]["origin"].get("thread_id") == THREAD,
              repr(pool.events))

        # The wake fires. While the tools have not arrived it is put back — not
        # run, not reported, not recorded as a run.
        ran, posts = [], []

        async def fake_post(_pool, _eid, origin, text_, **_kw):
            posts.append(text_)
            return True
        _orig_post = otr.post_to_origin_thread
        otr.post_to_origin_thread = fake_post

        async def stream(**kw):
            res = await rt.send_message(
                entity_id=kw["entity_id"], user_message=kw["message"], app_id=kw["app_id"],
                system_turn=kw.get("system_turn", False), thread_id=kw.get("thread_id"),
                history_context=kw.get("history_context"))
            if isinstance(res, dict) and res.get("identity_pending"):
                yield "2:" + json.dumps([{"type": "identity_pending"}]) + "\n"
                yield "0:" + json.dumps(res["response"]) + "\n"
                return
            ran.append(kw["message"])
            yield '0:"Done: x.txt is in the workspace."\n'
        brain_service.get_runtime = lambda: rt
        _orig_send = brain_service.send_message
        brain_service.send_message = stream
        claimed = {"id": 5, "title": pool.events[0]["summary"], "recurrence": None,
                   "start_time": None, "payload": pool.events[0]["payload"]}
        try:
            await otr._run_one(pool, EID, claimed)
            deferred = [e for e in pool.executed if "identity_defers" in e[0]]
            check("saved: before the tools arrive the wake is NOT run",
                  ran == [] and len(deferred) == 1 and deferred[0][1][3] == 1
                  and deferred[0][1][2] == 60.0, repr(pool.executed))
            check("saved: ...nor reported, nor recorded as a run, nor completed",
                  posts == [] and not any("calendar_event_runs" in e[0] for e in pool.executed)
                  and not any("completed" in map(str, e[1]) for e in pool.executed),
                  repr((posts, pool.executed)))

            # The grant lands (the seed finishes).
            pool.row["permission_overrides"] = GRANT
            S.SEED_STATE.update({"status": "ok", "retrying": False})
            claimed["payload"] = {**claimed["payload"], "identity_defers": 1}
            pool.executed.clear()
            await otr._run_one(pool, EID, claimed)
            check("saved: once the tools arrive the saved request runs, once, with the saved message",
                  len(ran) == 1 and MSG in ran[0], repr(ran))
            check("saved: its result is reported into the thread it was sent from",
                  len(posts) == 1 and posts[0].startswith("Done"), repr(posts))
            check("saved: the run is recorded",
                  any("calendar_event_runs" in e[0] for e in pool.executed), repr(pool.executed))
        finally:
            otr.post_to_origin_thread = _orig_post
            brain_service.send_message = _orig_send
        await _stop(task)

        # A replay / A-B / scheduled turn is not a person's request: nothing is
        # saved for it.
        pool2 = StorePool(placeholder_row())
        rt2, out2, built2, task2 = await _refused_turn(pool2, app_id="circles-replay",
                                                       history_context="chat")
        check("saved: a replay turn that is refused saves no rows and schedules no wake",
              (out2 or {}).get("identity_pending") is True and pool2.memory == [] and pool2.events == [],
              repr((pool2.memory, pool2.events)))
        await _stop(task2)
        pool3 = StorePool(placeholder_row())
        rt3, out3, built3, task3 = await _refused_turn(pool3, app_id="scheduled_task",
                                                       system_turn=True)
        check("saved: a platform wake-up that is refused saves nothing either",
              (out3 or {}).get("identity_pending") is True and pool3.memory == [] and pool3.events == [])
        await _stop(task3)
    finally:
        me._get_memory_embedding = _orig_embed
        S.TURN_WAIT_S = _orig_wait
        brain_service._entity_id = _orig_eid
        brain_service.get_runtime = _orig_rt


async def case_stream_frame():
    """The real stream wrapper marks a refused turn with a typed frame, and
    still sends the refusal text as the reply."""
    class _RT:
        async def send_message(self, **_kw):
            return {"response": "Nothing has been done with this yet.",
                    "identity_pending": True, "identity": {"status": "placeholder"}}
    _orig = brain_service._runtime
    brain_service._runtime = _RT()
    try:
        frames = [f async for f in brain_service._send_message_inner(
            entity_id=EID, message="hi")]
    finally:
        brain_service._runtime = _orig
    marked = [f for f in frames if f.startswith("2:") and "identity_pending" in f]
    check("stream: a refused turn carries a typed identity_pending frame", len(marked) == 1, repr(frames))
    check("stream: ...and the refusal text is still the reply",
          any(f.startswith("0:") and "Nothing has been done" in f for f in frames), repr(frames))


# ── 10. wording follows how long the Host has been up ───────────────────────
def case_wording():
    if H is None:
        check("wording: identity_hold exists", False)
        return
    young = {"status": "placeholder", "detail": "", "retrying": True, "began": time.time() - 30}
    old = {**young, "began": time.time() - 3600}
    t_young = H.refusal_text(young, 45)
    t_old = H.refusal_text(old, 45)
    check("wording: a Host up for seconds is 'just started'", "just started" in t_young, t_young)
    check("wording: a Host up for an hour is never 'just started'",
          "just started" not in t_old and "60 minutes" in t_old, t_old)
    # A standalone seed makes one local attempt: 'failed' is always final.
    stopped = {**old, "status": "failed", "retrying": False,
               "detail": "first-run identity failed: guard denied"}
    ts = H.refusal_text(stopped, 45)
    check("wording: a seed that stopped retrying says so and never says 'just started'",
          "stopped retrying" in ts and "just started" not in ts and "keeps retrying" not in ts, ts)
    check("wording: the saved note, retrying: runs as soon as the tools arrive",
          "as soon as my tools arrive" in H.refusal_text(old, 45, saved=True))
    sv = H.refusal_text(stopped, 45, saved=True)
    check("wording: the saved note, stopped: runs only if the tools arrive; no resend asked",
          "if my tools arrive" in sv and "again" not in sv.lower()
          and "send it" not in sv.lower() and "send this" not in sv.lower(), sv)
    for nm, ln in (("held again", H.HELD_AGAIN_LINE), ("time limit, held again", H.TIMEOUT_HELD_AGAIN_LINE)):
        check(f"wording: '{nm}' says exactly what happens: one more run after a wait, then failed if it persists",
              "once more after a short wait" in ln and "reported as failed and not tried again" in ln
              and "once it can" not in ln, ln)
    check("wording: a crisis-gate block after acting is worded as a block, not as a failure",
          "blocked" in H.BLOCKED_PARTIAL_LINE and "failed" not in H.BLOCKED_PARTIAL_LINE
          and "may be partly done" in H.BLOCKED_PARTIAL_LINE, H.BLOCKED_PARTIAL_LINE)
    check("wording: the live status line follows the uptime too",
          "just started" in H.hold_status(young) and "just started" not in H.hold_status(old))
    check("wording: unknown uptime claims nothing about it",
          "just started" not in H.refusal_text({"status": "placeholder", "retrying": True}, 45))


# ── 11. non-chat callers get a not-run ──────────────────────────────────────
async def case_not_run_callers():
    from mastercode.brain import offline_task_runner as otr

    refusal = {"response": "Nothing has been done with this yet. My tools have not loaded.",
               "identity_pending": True}

    class _RT:
        async def send_message(self, **_kw):
            return dict(refusal)
    _orig_rt = brain_service.get_runtime
    _orig_inner_rt = brain_service._runtime
    brain_service.get_runtime = lambda: _RT()
    brain_service._runtime = _RT()
    try:
        # scheduled turn, through the REAL brain_service frame
        _orig_inner = brain_service.send_message

        async def stream(**kw):
            async for f in brain_service._send_message_inner(
                    **{k: v for k, v in kw.items() if k != "thread_key"}):
                yield f
        brain_service.send_message = stream
        pool = StorePool(placeholder_row())
        posts = []

        async def fake_post(_p, _e, _o, text_, **_k):
            posts.append(text_)
            return True
        _orig_post = otr.post_to_origin_thread
        otr.post_to_origin_thread = fake_post
        try:
            claimed = {"id": 9, "title": "t", "recurrence": None, "start_time": None,
                       "payload": {"prompt": "do the thing"}}
            await otr._run_one(pool, EID, claimed)
            check("not-run: a scheduled turn that was refused is put back for later (counted)",
                  len([e for e in pool.executed if "identity_defers" in e[0]]) == 1, repr(pool.executed))
            check("not-run: ...never recorded as a successful run",
                  not any("calendar_event_runs" in e[0] for e in pool.executed)
                  and not any("completed" in map(str, e[1]) for e in pool.executed), repr(pool.executed))
            check("not-run: ...and nothing the refusal said is reported as its result",
                  posts == [], repr(posts))
            pool.executed.clear()
            claimed["payload"] = {"prompt": "do the thing", "identity_defers": otr.IDENTITY_DEFER_MAX}
            await otr._run_one(pool, EID, claimed)
            check("not-run: after the cap it stops retrying and says plainly nothing ran",
                  any("failed" in map(str, e[1]) for e in pool.executed)
                  and len(posts) == 1 and "Nothing ran" in posts[0], repr((pool.executed, posts)))
        finally:
            otr.post_to_origin_thread = _orig_post
            brain_service.send_message = _orig_inner
    finally:
        brain_service.get_runtime = _orig_rt
        brain_service._runtime = _orig_inner_rt


# ── 12. concurrent first turns never start a second write ───────────────────
async def case_concurrent_turns():
    from mastercode.runtime.session import EntityRuntime
    import mastercode.memory_engine as me
    _orig_wait = S.TURN_WAIT_S
    _orig_embed = me._get_memory_embedding

    async def no_embed(_t):
        raise RuntimeError("no embedder in this test")
    me._get_memory_embedding = no_embed

    def runtime(pool, built):
        rt = object.__new__(EntityRuntime)
        rt.book = FakeBook(pool)
        rt.pool = pool

        async def build(entity_id, **_kw):
            built.append(host_orchestration_enabled(await rt.book.get_entity(entity_id)))
            return None
        rt._build_chat_context_transient = build
        return rt
    tasks = []
    try:
        # (a) the write lands while six first turns wait: each runs, with tools.
        pool = StorePool(placeholder_row())
        S.TURN_WAIT_S = 3.0
        tasks.append(start_seed(pool, delay=0.5))
        await wait_until(lambda: S.SEED_STATE.get("status") == "placeholder", 1.0)
        built = []
        outs = await asyncio.gather(*[
            runtime(pool, built).send_message(entity_id=EID, user_message=f"hello {i}", app_id="circles")
            for i in range(6)])
        check("concurrent: six turns held through a slow write all run, each with tools",
              built == [True] * 6 and not any((o or {}).get("identity_pending") for o in outs),
              f"built={built} outs={repr(outs)[:200]}")
        check("concurrent: ...and the seed wrote once", SEED_WRITE["calls"] == 1,
              f"writes={SEED_WRITE['calls']}")
        await _stop(tasks.pop())

        # (b) the write hangs: a platform wake-up and six person turns are all
        # told so, none runs toolless, and none starts another write.
        pool = StorePool(placeholder_row())
        S.TURN_WAIT_S = 0.4
        tasks.append(start_seed(pool, delay=None))
        await wait_until(lambda: S.SEED_STATE.get("status") == "placeholder", 1.0)
        built = []
        sys_out = await runtime(pool, built).send_message(
            entity_id=EID, user_message="hello", system_turn=True, app_id="scheduled_task")
        outs = await asyncio.gather(*[
            runtime(pool, built).send_message(entity_id=EID, user_message=f"hello {i}", app_id="circles")
            for i in range(6)])
        await asyncio.sleep(0.2)
    finally:
        S.TURN_WAIT_S = _orig_wait
        me._get_memory_embedding = _orig_embed
        for t in tasks:
            await _stop(t)
    check("concurrent: a scheduled wake-up during a hung write is told so (not run)",
          (sys_out or {}).get("identity_pending") is True, repr(sys_out)[:300])
    check("concurrent: every one of the six is told so (none ran toolless)",
          built == [] and all((o or {}).get("identity_pending") for o in outs), repr(outs)[:300])
    check("concurrent: seven held turns never start a second write",
          SEED_WRITE["calls"] == 1 and S.SEED_STATE.get("started") == 1,
          f"writes={SEED_WRITE['calls']} state={S.SEED_STATE}")


# ── 13. what a saved request keeps, what supersedes it ──────────────────────
class _Env:
    """The saved-request world: a refused turn's runtime, a runner stream that
    goes through the REAL stream wrapper into a recording runtime, and the
    runner's posts."""

    def __init__(self, rt, pool):
        self.rt, self.pool = rt, pool
        self.runtime_calls = []   # what the runtime's send_message was called with
        self.posts = []
        self.messages = []        # what the runner sent as the turn's message
        self.gate = None          # an Event the recording runtime waits on, if set
        self.reply = {"content": "ok"}   # what the recording runtime answers

    async def start(self):
        from mastercode.brain import offline_task_runner as otr
        self.otr = otr
        env = self

        class _RecRT:
            async def send_message(self, **kw):
                env.runtime_calls.append(kw)
                if env.gate is not None:
                    await env.gate.wait()
                return dict(env.reply)
        self._orig = (brain_service._runtime, brain_service.get_runtime,
                      brain_service.send_message, otr.post_to_origin_thread)
        brain_service._runtime = _RecRT()
        brain_service.get_runtime = lambda: self.rt

        async def stream(**kw):
            env.messages.append(kw["message"])
            async for f in brain_service._send_message_inner(
                    **{k: v for k, v in kw.items() if k != "thread_key"}):
                yield f
        brain_service.send_message = stream

        async def fake_post(_p, _e, origin, text_, **_k):
            env.posts.append(text_)
            if hasattr(env.pool, "memory"):
                # What the real writer does: the report becomes a row of the thread.
                env.pool.memory.append({
                    "entity_id": EID, "content": text_, "source_session": "report",
                    "metadata": {"role": "assistant"},
                    "created_at": __import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc)})
            return True
        otr.post_to_origin_thread = fake_post

    def stop(self):
        (brain_service._runtime, brain_service.get_runtime, brain_service.send_message,
         self.otr.post_to_origin_thread) = self._orig

    async def fire(self, idx):
        ev = self.pool.events[idx]
        await self.otr._run_one(self.pool, EID, {
            "id": idx + 1, "title": ev["summary"], "recurrence": None,
            "start_time": None, "payload": ev["payload"]})

    def grant(self):
        self.pool.row["permission_overrides"] = GRANT
        S.SEED_STATE.update({"status": "ok", "retrying": False})


TUTOR = ["tutor.read", "tutor.chat"]


async def case_saved_request_semantics():
    import mastercode.memory_engine as me
    _orig_embed, _orig_wait, _orig_eid = me._get_memory_embedding, S.TURN_WAIT_S, brain_service._entity_id
    try:
        # ── the sender's app permissions travel with the wake (for the audit record) ──
        pool = StorePool(placeholder_row())
        rt, out, built, task = await _refused_turn(
            pool, app_id="circles", thread_id=THREAD, app_permissions=TUTOR)
        env = _Env(rt, pool)
        await env.start()
        try:
            check("audit: the saved wake carries the sender's app permissions",
                  pool.events and pool.events[0]["payload"].get("app_permissions") == TUTOR,
                  repr(pool.events))
            env.grant()
            await env.fire(0)
            got = [c.get("app_permissions") for c in env.runtime_calls]
            check("audit: the woken turn is called with the sender's app permissions (recorded for audit; the gate does not narrow on them)",
                  got == [TUTOR], repr(got))
        finally:
            env.stop()
        await _stop(task)
        pool = StorePool(placeholder_row())
        rt, out, built, task = await _refused_turn(pool, app_id="circles", thread_id=THREAD)
        env = _Env(rt, pool)
        await env.start()
        try:
            check("audit: a request sent with no app permissions stores none",
                  "app_permissions" not in pool.events[0]["payload"], repr(pool.events))
            env.grant()
            await env.fire(0)
            check("audit: ...and the woken turn passes None, as it was sent",
                  [c.get("app_permissions") for c in env.runtime_calls] == [None])
        finally:
            env.stop()
        await _stop(task)

        # ── a newer message supersedes it: never twice, never after "never mind"
        for label, newer in (("resend", MSG), ("never mind", "never mind, do Y instead")):
            pool = StorePool(placeholder_row())
            rt, out, built, task = await _refused_turn(
                pool, app_id="circles", thread_id=THREAD)
            env = _Env(rt, pool)
            await env.start()
            try:
                env.grant()
                built.clear()
                # The person's next message passes the hold (tools are here).
                _s, built, o2 = await _person_turn(rt, pool, newer)
                check(f"supersede[{label}]: the newer message runs (passes the hold)",
                      built == [EID] and o2 is None, repr((built, o2)))
                check(f"supersede[{label}]: it cancels the thread's pending saved wake",
                      pool.events[0]["status"] == "cancelled", repr(pool.events))
                await env.fire(0)
                check(f"supersede[{label}]: the saved request never runs (exactly one run in all)",
                      env.runtime_calls == [] and env.messages == [] and len(built) == 1,
                      repr((env.runtime_calls, env.messages, built)))
            finally:
                env.stop()
            await _stop(task)

        # A wake in ANOTHER thread is left alone.
        pool = StorePool(placeholder_row())
        rt, out, built, task = await _refused_turn(pool, app_id="circles", thread_id=THREAD)
        env = _Env(rt, pool)
        await env.start()
        try:
            env.grant()
            await rt.send_message(entity_id=EID, user_message="elsewhere", app_id="circles",
                                  thread_id="11111111-2222-3333-4444-555555555555")
            check("supersede: a message in a different thread leaves this thread's wake alone",
                  pool.events[0]["status"] == "scheduled", repr(pool.events))
        finally:
            env.stop()
        await _stop(task)

        # ── two held requests: the newer one carries the older forward ───────
        pool = StorePool(placeholder_row())
        rt, out, built, task = await _refused_turn(pool, app_id="circles", thread_id=THREAD)
        env = _Env(rt, pool)
        await env.start()
        try:
            await rt.send_message(entity_id=EID, user_message="never mind, do Y instead",
                                  app_id="circles", thread_id=THREAD)
            check("handover: the older wake is cancelled when the newer one is saved, and carried on it",
                  len(pool.events) == 2 and pool.events[0]["status"] == "cancelled"
                  and len(pool.events[1]["payload"].get("earlier") or []) == 1, repr(pool.events))
            env.grant()
            await env.fire(1)
            check("handover: the woken turn gets the newer words AND the earlier request, as data; one run",
                  len(env.runtime_calls) == 1 and "Their message: never mind, do Y" in env.messages[-1]
                  and f"An earlier message from the person, not yet run: \u00ab{MSG}\u00bb" in env.messages[-1]
                  and "will not run on its own now" in env.messages[-1], repr(env.messages))
            await _drain_bg()
            check("handover: the thread shows the line saying the earlier one was carried forward",
                  H.HANDOVER_LINE in env.posts, repr(env.posts))
            # The safety net: an older wake that lost the race and is still
            # scheduled folds into the newest held request and never runs; it
            # never claims a newer message carried it.
            pool.events[0]["status"] = "scheduled"
            pool.events[1]["status"] = "scheduled"      # the newest held request, pending
            before = len(env.runtime_calls)
            env.posts.clear()
            await env.fire(0)
            check("supersede: a wake that finds a newer message folds into the newest held request, with its own line",
                  len(env.runtime_calls) == before and env.posts == [H.FOLD_LINE], repr((env.runtime_calls, env.posts)))
        finally:
            env.stop()
        await _stop(task)

        # ── the woken turn's words follow the real state ─────────────────────
        pool = StorePool(placeholder_row())
        rt, out, built, task = await _refused_turn(pool, app_id="circles", thread_id=THREAD)
        env = _Env(rt, pool)
        await env.start()
        try:
            await env.fire(0)  # seed still on the placeholder, no grant
            check("state: no grant and the seed unresolved → put back, nothing sent to the model",
                  env.messages == [] and pool.events[0]["status"] == "scheduled"
                  and any("identity_defers" in e[0] for e in pool.executed), repr(env.messages))
            S.SEED_STATE.update({"status": "ok", "retrying": False})  # resolved chat-only
            await env.fire(0)
            check("state: resolved chat-only → runs, and says there are no tools (not 'loaded now')",
                  len(env.messages) == 1 and "no tools" in env.messages[0]
                  and "loaded now" not in env.messages[0], repr(env.messages))
            pool.row["permission_overrides"] = GRANT
            pool.events[0]["status"] = "scheduled"
            await env.fire(0)
            check("state: grant present → says the tools have loaded",
                  len(env.messages) == 2 and "loaded now" in env.messages[1], repr(env.messages))
        finally:
            env.stop()
        await _stop(task)

        # ── deferral backs off, and logs once at INFO ────────────────────────
        otr = env.otr
        check("defer: the wait doubles from 60 s to a 300 s cap",
              [otr.identity_defer_delay(n) for n in (1, 2, 3, 4, 5, 9)]
              == [60.0, 120.0, 240.0, 300.0, 300.0, 300.0])
        import logging
        recs = []

        class _H(logging.Handler):
            def emit(self, r):
                if not r.name.startswith("privatae.pc.identity_seed"):
                    recs.append(r)
        h = _H(level=logging.DEBUG)
        logging.getLogger().addHandler(h)
        _lvl = logging.getLogger().level
        logging.getLogger().setLevel(logging.DEBUG)
        try:
            pool = StorePool(placeholder_row())
            rt, out, built, task = await _refused_turn(pool, app_id="circles", thread_id=THREAD)
            env = _Env(rt, pool)
            await env.start()
            try:
                recs.clear()
                pool.events[0]["payload"]["identity_defers"] = 2
                await env.fire(0)
                third = [e for e in pool.executed if "identity_defers" in e[0]]
                lines = [r for r in recs if r.levelno >= logging.INFO
                         and r.name.startswith("cseries.brain.offline_tasks")]
                check("defer: the third deferral waits 240 s",
                      third and third[-1][1][2] == 240.0, repr(third))
                check("defer: one INFO line per deferral, no ERROR or WARNING at all",
                      len(lines) == 1 and lines[0].levelno == logging.INFO
                      and not [r for r in recs if r.levelno >= logging.WARNING],
                      repr([(r.name, r.levelname, r.getMessage()[:60]) for r in recs if r.levelno >= logging.INFO]))
            finally:
                env.stop()
            await _stop(task)
        finally:
            logging.getLogger().removeHandler(h)
            logging.getLogger().setLevel(_lvl)

    finally:
        me._get_memory_embedding = _orig_embed
        S.TURN_WAIT_S = _orig_wait
        brain_service._entity_id = _orig_eid


async def case_note_is_honest_and_bounded():
    import mastercode.memory_engine as me
    _orig_embed, _orig_wait, _orig_eid = me._get_memory_embedding, S.TURN_WAIT_S, brain_service._entity_id
    _bound = H.ROW_WRITE_BOUND_S
    try:
        pool = StorePool(placeholder_row())
        pool.fail_memory = True
        rt, out, built, task = await _refused_turn(pool, app_id="circles", thread_id=THREAD)
        t = out["response"]
        check("honest: the person's row failed to write → the note does not say it is saved",
              "is saved" not in t and "could not be written into this thread" in t
              and "will still run" in t, t)
        await _stop(task)

        pool = StorePool(placeholder_row())
        pool.fail_events = True
        rt, out, built, task = await _refused_turn(pool, app_id="circles", thread_id=THREAD)
        t = out["response"]
        check("honest: the wake could not be queued → the note says it will not run on its own",
              "could not be queued" in t and "will run by itself" not in t
              and len(pool.reload_thread(THREAD)) == 2, t)
        await _stop(task)

        pool = StorePool(placeholder_row())
        pool.fail_memory = True
        pool.fail_events = True
        rt, out, built, task = await _refused_turn(pool, app_id="circles", thread_id=THREAD)
        t = out["response"]
        check("honest: nothing could be saved → it says so, and promises nothing",
              "could not be saved" in t and "will run" not in t.split("Nothing has been done")[-1].replace(
                  "will not run on its own", ""), t)
        await _stop(task)

        # A database that hangs cannot hold the answer for more than the bound.
        H.ROW_WRITE_BOUND_S = 0.3
        pool = StorePool(placeholder_row())
        pool.hang_memory = True
        t0 = time.monotonic()
        rt, out, built, task = await _refused_turn(pool, app_id="circles", thread_id=THREAD)
        took = time.monotonic() - t0
        await _stop(task)
        check("bounded: rows that hang add at most about 2 x the bound to the answer",
              took < 0.4 + 0.3 * 2 + 0.8 and (out or {}).get("identity_pending")
              and "could not be written into this thread" in out["response"],
              f"took={took:.2f} {out}")
    finally:
        H.ROW_WRITE_BOUND_S = _bound
        me._get_memory_embedding = _orig_embed
        S.TURN_WAIT_S = _orig_wait
        brain_service._entity_id = _orig_eid



async def _drain_bg():
    if H._BG:
        await asyncio.gather(*list(H._BG), return_exceptions=True)


class _Sess:
    """What _build_chat_context_transient returns in the person-turn tests: the
    turn sets what it hands the model on it, then trips on the rest of the stub."""


async def _person_turn(rt, pool, text, *, thread=THREAD, mode="ok", progress=None):
    """A person's turn that passes the hold.

    mode "ok":    core stand-in that takes the held requests over exactly as the
                  real core does (rt._take_over_held), then succeeds;
    mode "error"/"raise"/"stop"/"timeout": the same, then a provider error result, a crash,
                  or a Stop BEFORE any model call; "..._text": after a model call
                  that answered in text only; "..._acted": after a tool call
                  executed in the turn; "unconfigured" /
                  "blocked": a result no model acted on;
    mode "real":  the REAL core up to a stub session that cannot finish a turn.
    Returns (session the held words were put on, built list, error raised)."""
    sess = _Sess()
    built = []

    async def cb(ev):
        if progress is not None:
            progress.append(ev)

    if mode == "real":
        async def build(entity_id, **_kw):
            built.append(entity_id)
            return sess
        rt._build_chat_context_transient = build
    else:
        async def core(entity_id=None, user_message=None, app_id=None, thread_id=None,
                       progress_cb=None, **_kw):
            built.append(entity_id)
            sess.held_requests = await rt._take_over_held(
                entity_id, project_id=None, app_id=app_id, thread_id=thread_id,
                history_context=None, progress_cb=progress_cb)
            from mastercode.runtime.session import _TAKEOVER
            tk = _TAKEOVER.get()
            if tk is not None and (mode == "ok" or mode.endswith(("_after", "_text", "_acted"))):
                tk.model_saw = True        # a model call carried the earlier words and answered
            if tk is not None and mode.endswith("_acted"):
                tk.acted = True            # and a tool call executed in this turn
            if tk is not None and mode.endswith("_search"):
                from mastercode.runtime.session import tool_acts
                tk.model_saw = True
                tk.acted = tool_acts("web_search")       # a search changes nothing
            if mode.startswith("error"):
                return {"error": "provider error: 529 overloaded"}
            if mode.startswith("raise"):
                raise RuntimeError("the turn crashed")
            if mode.startswith("stop"):
                raise asyncio.CancelledError()
            if mode.startswith("unconfigured"):
                return {"response": "I don't have a chat model configured yet.",
                        "model_unconfigured": True}
            if mode.startswith("blocked"):
                return {"response": "I'm unable to respond right now.", "blocked": True,
                        "block_reason": "governance_error"}
            return {"content": "ok"}
        rt._send_message_core = core
    err = None
    try:
        await rt.send_message(entity_id=EID, user_message=text, app_id="circles",
                              thread_id=thread, progress_cb=cb)
    except BaseException as exc:  # noqa: BLE001 — the stub session cannot run a turn; Stop is a cancel
        err = exc
    return sess, built, err


async def case_handover():
    import mastercode.memory_engine as me
    import mastercode.runtime.prompt_schema as ps
    _orig_embed, _orig_wait, _orig_eid = me._get_memory_embedding, S.TURN_WAIT_S, brain_service._entity_id
    try:
        for label, newer in (("thanks", "thanks, let me know when it's done"),
                             ("never mind", "never mind, do Y instead")):
            pool = StorePool(placeholder_row())
            rt, out, built, task = await _refused_turn(pool, app_id="circles", thread_id=THREAD)
            env = _Env(rt, pool)
            await env.start()
            try:
                env.grant()
                sess, built2, err = await _person_turn(rt, pool, newer)
                held = getattr(sess, "held_requests", None)
                check(f"handover[{label}]: the earlier request is in THIS turn's context, not lost",
                      held == [MSG], repr((held, err)))
                check(f"handover[{label}]: the wake was cancelled and never runs on its own; one turn in all",
                      pool.events[0]["status"] == "cancelled" and built2 == [EID]
                      and env.runtime_calls == [], repr((pool.events, built2, env.runtime_calls)))
                await env.fire(0)
                check(f"handover[{label}]: ...even if the cancelled wake is fired",
                      env.runtime_calls == [], repr(env.runtime_calls))
                await _drain_bg()
                check(f"handover[{label}]: once the turn succeeded, the thread shows the carried-forward line",
                      env.posts.count(H.HANDOVER_LINE) == 1, repr(env.posts))

                async def facts(*_a, **_k):
                    return {}
                import mastercode.runtime.frame as frame_mod
                _of = frame_mod.frame_facts
                frame_mod.frame_facts = facts
                try:
                    frame_text = await ps._b_frame(None, sess, newer)
                finally:
                    frame_mod.frame_facts = _of
                check(f"handover[{label}]: the model's turn block quotes it as data beside the newer words",
                      f"An earlier message from the person, not yet run: \u00ab{MSG}\u00bb. "
                      "It will not run on its own now." in frame_text, frame_text)
            finally:
                env.stop()
            await _stop(task)

        # ── the turn that took it over ends before the model saw it: put back ─
        for mode, label in (("error", "provider error"), ("raise", "crash"),
                            ("real", "turn cannot finish"),
                            ("error_after", "provider error on the first model call"),
                            ("raise_text", "crash after a text-only reply"),
                            ("error_text", "error result after a text-only reply"),
                            ("error_search", "failure after only a web search"),
                            ("unconfigured", "no chat model")):
            pool = StorePool(placeholder_row())
            rt, out, built, task = await _refused_turn(pool, app_id="circles", thread_id=THREAD)
            env = _Env(rt, pool)
            await env.start()
            try:
                env.grant()
                sess, built2, err = await _person_turn(rt, pool, "email bob", mode=mode)
                await _drain_bg()
                check(f"before-model[{label}]: the held words still reached that turn's context",
                      getattr(sess, "held_requests", None) == [MSG], repr((getattr(sess, "held_requests", None), err)))
                check(f"before-model[{label}]: the wake is put back (scheduled, due now), not left cancelled",
                      pool.events[0]["status"] == "scheduled", repr(pool.events))
                check(f"before-model[{label}]: put back with the NORMAL deferral (counted, backing off), not due now",
                      pool.events[0]["payload"].get("identity_defers") == 1
                      and any("start_time = NOW() + make_interval(secs => LEAST(60 * power(2" in e[0]
                              for e in pool.executed)
                      and not any("start_time = NOW()," in e[0] for e in pool.executed),
                      repr(pool.executed))
                check(f"before-model[{label}]: no 'carried forward' line; an honest 'held again' line instead",
                      H.HANDOVER_LINE not in env.posts and len(env.posts) <= 1
                      and all(("held again" in p or "has not run yet" in p) for p in env.posts),
                      repr(env.posts))
                if mode in ("unconfigured", "blocked"):
                    check(f"before-model[{label}]: the line says why it has not run yet",
                          len(env.posts) == 1 and "has not run yet" in env.posts[0], repr(env.posts))
                await env.fire(0)
                check(f"before-model[{label}]: the held request then runs, exactly once",
                      len(env.runtime_calls) == 1 and MSG in env.messages[-1], repr(env.messages))
            finally:
                env.stop()
            await _stop(task)

        # ── the turn ACTED (a tool call ran), or was Stopped: never re-run ───
        for mode, label, line in (("stop", "Stop before the model", None),
                                  ("stop_after", "Stop after a text-only reply", H.STOPPED_LINE),
                                  ("stop_acted", "Stop after a tool call", H.STOPPED_PARTIAL_LINE),
                                  ("raise_acted", "crash after a tool call", H.PARTIAL_LINE),
                                  ("error_acted", "error result after a tool call", H.PARTIAL_LINE),
                                  ("blocked_acted", "mid-loop crisis gate after a tool call", H.BLOCKED_PARTIAL_LINE),
                                  ("unconfigured_acted", "no-model result after a tool call", H.PARTIAL_LINE)):
            pool = StorePool(placeholder_row())
            rt, out, built, task = await _refused_turn(pool, app_id="circles", thread_id=THREAD)
            env = _Env(rt, pool)
            await env.start()
            try:
                env.grant()
                sess, built2, err = await _person_turn(rt, pool, "Email Bob the quarterly report", mode=mode)
                await _drain_bg()
                if mode == "stop":
                    line = H.STOPPED_LINE
                check(f"saw[{label}]: the held request is NOT put back (stays cancelled, not deferred)",
                      pool.events[0]["status"] == "cancelled"
                      and not any("cancelled_reason=NULL" in e[0] for e in pool.executed),
                      repr((pool.events[0]["status"], [e[0][:60] for e in pool.executed])))
                check(f"saw[{label}]: an honest line is posted",
                      env.posts == [line], repr(env.posts))
                await env.fire(0)
                await _drain_bg()
                check(f"saw[{label}]: it never runs on its own afterwards",
                      env.runtime_calls == [], repr(env.runtime_calls))
                if mode.startswith("stop"):
                    check(f"saw[{label}]: the cancel itself still propagates",
                          isinstance(err, asyncio.CancelledError), repr(err))
            finally:
                env.stop()
            await _stop(task)

        # Two held requests are both handed over, oldest first.
        pool = StorePool(placeholder_row())
        rt, out, built, task = await _refused_turn(pool, app_id="circles", thread_id=THREAD)
        await rt.send_message(entity_id=EID, user_message="and also this", app_id="circles",
                              thread_id=THREAD)
        pool.row["permission_overrides"] = GRANT
        S.SEED_STATE.update({"status": "ok", "retrying": False})
        sess, built2, err = await _person_turn(rt, pool, "ok go")
        check("handover: two held requests both reach the turn, oldest first",
              getattr(sess, "held_requests", None) == [MSG, "and also this"],
              repr(getattr(sess, "held_requests", None)))
        await _stop(task)

        # The refusal note says plainly that a newer message carries it forward.
        pool = StorePool(placeholder_row())
        rt, out, built, task = await _refused_turn(pool, app_id="circles", thread_id=THREAD)
        check("handover: the refusal note says a newer message carries the request forward",
              "newer in this thread first, that message carries this one forward" in out["response"],
              out["response"])
        await _stop(task)
    finally:
        me._get_memory_embedding = _orig_embed
        S.TURN_WAIT_S = _orig_wait
        brain_service._entity_id = _orig_eid


async def case_stale_and_missed_takeovers():
    """A 'running' row nothing runs; and the ways a takeover can miss a wake."""
    import mastercode.memory_engine as me
    from mastercode.brain import offline_task_runner as otr
    _orig_embed, _orig_wait, _orig_eid = me._get_memory_embedding, S.TURN_WAIT_S, brain_service._entity_id
    _ww, _wp = H.WAKE_WAIT_S, H.WAKE_POLL_S
    try:
        # ── blocking: a stale running row stalls nothing ─────────────────────
        pool = StorePool(placeholder_row())
        rt, out, built, task = await _refused_turn(pool, app_id="circles", thread_id=THREAD)
        env = _Env(rt, pool)
        await env.start()
        try:
            env.grant()
            pool.events[0]["status"] = "running"     # the process that ran it died
            H.WAKE_WAIT_S = 5.0
            progress = []
            t0 = time.monotonic()
            sess, built2, err = await _person_turn(rt, pool, "thanks", progress=progress)
            took = time.monotonic() - t0
            await _drain_bg()
            check("stale: a 'running' wake nothing runs causes NO wait",
                  took < 1.0, f"took={took:.2f}")
            check("stale: it is taken over like a scheduled one (cancelled, words in the turn's context)",
                  pool.events[0]["status"] == "cancelled"
                  and getattr(sess, "held_requests", None) == [{"text": MSG, "interrupted": True}],
                  repr((pool.events[0]["status"], getattr(sess, "held_requests", None))))
            check("stale: it is flagged as possibly partly done (the process that ran it died)",
                  pool.events[0]["payload"].get("crash_unknown") is True, repr(pool.events[0]["payload"]))
            check("stale: no false 'Finishing your earlier request' status line",
                  not any("Finishing your earlier request" in (e.get("text") or "") for e in progress),
                  repr(progress))
            check("stale: and the carried-forward line is posted once",
                  env.posts.count(H.HANDOVER_LINE) == 1, repr(env.posts))
        finally:
            env.stop()
        await _stop(task)

        # ── a wake put back while we waited is taken in the repeat takeover ──
        H.WAKE_POLL_S = 0.03
        H.WAKE_WAIT_S = 5.0
        pool = StorePool(placeholder_row())
        rt, out, built, task = await _refused_turn(pool, app_id="circles", thread_id=THREAD)
        env = _Env(rt, pool)
        await env.start()
        try:
            env.grant()
            pool.events[0]["status"] = "running"
            gate = asyncio.Event()

            async def _live():
                await gate.wait()
            fake = asyncio.create_task(_live())       # stands for the live wake task
            fake._event_id = 1
            otr._RUNNING.add(fake)
            turn = asyncio.create_task(_person_turn(rt, pool, "thanks"))
            await asyncio.sleep(0.3)
            check("miss: (setup) the turn is waiting on the live wake", not turn.done())
            # the wake's pre-check puts it back (PII row not landed / ceiling / unknown seed)
            pool.events[0]["status"] = "scheduled"
            gate.set()
            sess, built2, err = await asyncio.wait_for(turn, 3.0)
            await _drain_bg()
            check("miss: a wake put back during the wait is taken by the repeat takeover, never left to run",
                  pool.events[0]["status"] == "cancelled" and getattr(sess, "held_requests", None) == [MSG],
                  repr((pool.events[0]["status"], getattr(sess, "held_requests", None))))
        finally:
            otr._RUNNING.discard(fake)
            env.stop()
        await _stop(task)

        # ── the flag-clear race: a save landing during the final SELECT ──────
        H.reset_saved_state()
        pool = StorePool(placeholder_row())

        class RacePool(StorePool):
            async def fetchval(self, sql, *args):
                if sql.startswith("SELECT 1 FROM calendar_events") and "IN ('scheduled','running')" in sql \
                        and getattr(self, "race", False):
                    self.race = False
                    H._SAVES += 1                    # a request is saved while we look
                    H._MAYBE = True
                    return None
                return await super().fetchval(sql, *args)
        rp = RacePool(placeholder_row())
        H._MAYBE, H._BOOT_CHECKED = True, True
        rp.race = True
        await H.hand_over_saved_requests(rp, EID, app_id="circles", thread_id=THREAD)
        check("race: a save that lands during the final look keeps the flag on",
              H._MAYBE is True, repr(H._MAYBE))
        H._MAYBE = True
        await H.hand_over_saved_requests(rp, EID, app_id="circles", thread_id=THREAD)
        check("race: ...and with no save meanwhile and nothing held, the flag clears",
              H._MAYBE is False, repr(H._MAYBE))

        # ── the takeover UPDATE raises: the wake later skips, never lies ─────
        for variant in ("alone", "newer-held"):
            H.reset_saved_state()
            pool = StorePool(placeholder_row())
            rt, out, built, task = await _refused_turn(pool, app_id="circles", thread_id=THREAD)
            env = _Env(rt, pool)
            await env.start()
            try:
                env.grant()
                if variant == "newer-held":
                    # a newer message saved after the grant (e.g. refused again)
                    S.SEED_STATE.update({"status": "placeholder", "retrying": True})
                    pool.row["permission_overrides"] = None
                    pool.fail_cancel = True
                    await rt.send_message(entity_id=EID, user_message="actually do Y", app_id="circles",
                                          thread_id=THREAD)
                    pool.fail_cancel = False
                    env.grant()
                    check("miss: (setup) the takeover failed, so both wakes are still scheduled",
                          [e["status"] for e in pool.events] == ["scheduled", "scheduled"],
                          repr([e["status"] for e in pool.events]))
                else:
                    pool.fail_cancel = True
                    sess, built2, err = await _person_turn(rt, pool, "thanks, never mind")
                    pool.fail_cancel = False
                    await _drain_bg()
                    check("miss: (setup) the takeover UPDATE raised: nothing cancelled, nothing claimed carried",
                          pool.events[0]["status"] == "scheduled" and env.posts == []
                          and not getattr(sess, "held_requests", None), repr((pool.events, env.posts)))
                    # the newer message is a person turn row in the thread, as a real turn writes it
                    pool.memory.append({"entity_id": EID, "content": "thanks, never mind",
                                        "source_session": "x", "metadata": {
                                            "role": "user", "app_id": "circles",
                                            "source_context": pool.events[0]["payload"]["source_context"]},
                                        "created_at": __import__("datetime").datetime.now(
                                            __import__("datetime").timezone.utc)})
                env.posts.clear()
                await env.fire(0)
                if variant == "newer-held":
                    check("miss[newer-held]: the older wake folds into the newest held request, and does not run",
                          env.runtime_calls == [] and len(pool.events[1]["payload"].get("earlier") or []) >= 1
                          and env.posts == [H.FOLD_LINE], repr((env.runtime_calls, env.posts, pool.events[1]["payload"])))
                    check("miss[newer-held]: the line never claims it was carried by the newer message",
                          "carries it forward" not in " ".join(env.posts), repr(env.posts))
                else:
                    check("miss[alone]: with nothing newer held it runs, with the newer messages put in front of the model",
                          len(env.runtime_calls) == 1 and "newer messages" in env.messages[-1]
                          and MSG in env.messages[-1], repr(env.messages))
                    check("miss[alone]: and it never claims the request was carried forward",
                          "carries it forward" not in " ".join(env.posts), repr(env.posts))
            finally:
                env.stop()
            await _stop(task)
    finally:
        H.WAKE_WAIT_S, H.WAKE_POLL_S = _ww, _wp
        me._get_memory_embedding = _orig_embed
        S.TURN_WAIT_S = _orig_wait
        brain_service._entity_id = _orig_eid


async def case_running_wake():
    """A resend while the saved wake is RUNNING must not run it twice: the
    person's turn waits for it and answers with its result in the history."""
    import mastercode.memory_engine as me
    _orig_embed, _orig_wait, _orig_eid = me._get_memory_embedding, S.TURN_WAIT_S, brain_service._entity_id
    _poll = H.WAKE_POLL_S
    H.WAKE_POLL_S = 0.03
    try:
        pool = StorePool(placeholder_row())
        rt, out, built, task = await _refused_turn(pool, app_id="circles", thread_id=THREAD)
        env = _Env(rt, pool)
        await env.start()
        try:
            env.grant()
            env.gate = asyncio.Event()
            pool.events[0]["status"] = "running"      # the runner has claimed it
            wake = asyncio.create_task(env.fire(0))
            wake._event_id = 1                      # the runner's own task, as _RUNNING holds it
            env.otr._RUNNING.add(wake)
            await wait_until(lambda: len(env.runtime_calls) == 1, 2.0)
            order = []

            async def build(entity_id, **_kw):
                order.append(("build", any(m["content"] == "ok" and m["metadata"].get("role") == "assistant"
                                           for m in pool.memory)))
                return None
            rt._build_chat_context_transient = build
            person = asyncio.create_task(rt.send_message(
                entity_id=EID, user_message=MSG, app_id="circles", thread_id=THREAD))
            await asyncio.sleep(0.4)
            check("running: a resend during the wake WAITS for it (the turn is not built yet)",
                  order == [] and not person.done(), repr(order))
            env.gate.set()
            await wake
            env.otr._RUNNING.discard(wake)
            await asyncio.wait_for(person, 3.0)
            check("running: the resend is answered after the wake finished, with its result in the history",
                  order == [("build", True)], repr(order))
            check("running: the request ran exactly once (the wake), never again",
                  len(env.runtime_calls) == 1 and pool.events[0]["status"] == "completed",
                  repr((len(env.runtime_calls), pool.events[0]["status"])))
        finally:
            env.stop()
        await _stop(task)
    finally:
        H.WAKE_POLL_S = _poll
        me._get_memory_embedding = _orig_embed
        S.TURN_WAIT_S = _orig_wait
        brain_service._entity_id = _orig_eid


async def case_gating_and_extras():
    import mastercode.memory_engine as me
    from mastercode import pii_queue
    from mastercode.brain import offline_task_runner as otr
    _orig_embed, _orig_wait, _orig_eid = me._get_memory_embedding, S.TURN_WAIT_S, brain_service._entity_id
    try:
        # ── a seeded Host pays only an in-memory check per person turn ───────
        H.reset_saved_state()
        pool = StorePool({**placeholder_row(), "permission_overrides": GRANT})
        reset_seed(pool)
        S.SEED_STATE.update({"status": "ok", "entity_id": EID})
        from mastercode.runtime.session import EntityRuntime
        rt = object.__new__(EntityRuntime)
        rt.book, rt.pool = FakeBook(pool), pool

        async def turn():
            sess, built, err = await _person_turn(rt, pool, "hello")
            return built
        await turn()
        first = list(pool.queries)
        await turn()
        await turn()
        check("gate: the first person turn does ONE boot look for saved requests, no UPDATE",
              len(first) == 1 and first[0].startswith("SELECT 1") , repr(first))
        check("gate: later turns on a seeded Host issue no calendar query at all",
              pool.queries == first, repr(pool.queries))
        me._get_memory_embedding = None
        async def no_embed(_t):
            raise RuntimeError("no embedder in this test")
        me._get_memory_embedding = no_embed
        # a save in this process turns the check on, a clean pass turns it off again
        rt2, out, built, task = await _refused_turn(pool2 := StorePool(placeholder_row()),
                                                    app_id="circles", thread_id=THREAD)
        pool2.row["permission_overrides"] = GRANT
        S.SEED_STATE.update({"status": "ok", "retrying": False})
        pool2.queries.clear()
        await _person_turn(rt2, pool2, "hello")
        did = [q for q in pool2.queries if q.startswith("UPDATE")]
        pool2.queries.clear()
        await _person_turn(rt2, pool2, "hello again")
        check("gate: after this process saved a request, the next person turn takes it over (UPDATE issued)",
              len(did) >= 1, repr(did))
        check("gate: ...and once nothing is held the flag clears (no query on the turn after)",
              pool2.queries == [], repr(pool2.queries))
        await _stop(task)

        # ── PII mode: queued counts as saved; no raw words in the calendar ───
        H.reset_saved_state()
        queued = []
        _enabled, _submit = pii_queue.enabled, pii_queue.submit
        pii_queue.enabled = lambda: True

        async def fake_submit(text, scope, writer, verify=False, **_k):
            queued.append((text, writer))
            return None
        pii_queue.submit = fake_submit
        try:
            pool = StorePool(placeholder_row())
            SECRET = "my card is 4111 1111 1111 1111, buy the thing"
            S.TURN_WAIT_S = 0.4
            task = start_seed(pool, delay=None)   # the local write hangs
            await wait_until(lambda: S.SEED_STATE.get("status") == "placeholder", 1.0)
            brain_service._entity_id = EID
            rt = object.__new__(EntityRuntime)
            rt.book, rt.pool = FakeBook(pool), pool

            async def build(entity_id, **_kw):
                return None
            rt._build_chat_context_transient = build
            out = await rt.send_message(entity_id=EID, user_message=SECRET,
                                        app_id="circles", thread_id=THREAD)
            t = out["response"]
            check("pii: a queued row counts as saved, and the note says it is being saved",
                  "is being saved in this thread" in t and "could not" not in t, t)
            check("pii: the raw words are not in the calendar payload at all",
                  len(pool.events) == 1 and SECRET not in json.dumps(pool.events[0]["payload"])
                  and "prompt" not in pool.events[0]["payload"], repr(pool.events))
            env = _Env(rt, pool)
            await env.start()
            try:
                S.SEED_STATE.update({"status": "ok", "retrying": False})
                pool.row["permission_overrides"] = GRANT
                await env.fire(0)
                check("pii: until the tokenized row has landed the wake waits (nothing sent to the model)",
                      env.messages == [] and any("identity_defers" in e[0] for e in pool.executed),
                      repr(env.messages))
                await queued[0][1]("TOKENIZED(" + queued[0][0][:7] + ")")   # the drain worker lands it
                pool.events[0]["status"] = "scheduled"
                await env.fire(0)
                check("pii: once it has landed the wake runs on the stored (tokenized) words, not the raw ones",
                      len(env.messages) == 1 and "TOKENIZED(" in env.messages[0]
                      and SECRET not in env.messages[0], repr(env.messages))
            finally:
                env.stop()
            await _stop(task)
        finally:
            pii_queue.enabled, pii_queue.submit = _enabled, _submit

        # ── the thread's chosen model travels with the wake ──────────────────
        H.reset_saved_state()
        pool = StorePool(placeholder_row())
        override = {"provider": "local", "model_id": "private-model", "base_url": "http://127.0.0.1:1"}
        rt, out, built, task = await _refused_turn(
            pool, app_id="circles", thread_id=THREAD, thread_override=override)
        env = _Env(rt, pool)
        await env.start()
        try:
            check("model: the wake carries the thread's chosen model",
                  pool.events[0]["payload"].get("thread_override") == override, repr(pool.events))
            env.grant()
            await env.fire(0)
            check("model: the woken turn runs on it (a privacy-chosen local model is respected)",
                  [c.get("thread_override") for c in env.runtime_calls] == [override],
                  repr([c.get("thread_override") for c in env.runtime_calls]))
        finally:
            env.stop()
        await _stop(task)

        # ── the grant landing releases the wake at once ──────────────────────
        pool = StorePool(placeholder_row())
        pool.events.append({"event_type": "scheduled_task", "summary": "held", "status": "scheduled",
                            "payload": {"saved_request": True, "origin": {}}})
        tasks = [start_seed(pool)]
        got = await wait_until(lambda: bool(pool.released), 3.0)
        for t_ in tasks:
            await _stop(t_)
        check("release: the moment the seed resolves, held requests are made due now (not at the next poll)",
              got is not None and got < 2.0 and pool.events[0].get("released") is True
              and S.SEED_STATE.get("status") == "ok", f"{got} {S.SEED_STATE}")

        # ...and the runner's poll wakes at once instead of sleeping its interval.
        class _LoopPool:
            def __init__(self):
                self.fetches = 0

            async def execute(self, *_a, **_k):
                return "UPDATE 0"

            async def fetch(self, *_a, **_k):
                self.fetches += 1
                return []
        lp = _LoopPool()
        _pi = otr.POLL_INTERVAL_S
        otr.POLL_INTERVAL_S = 30
        loop_task = asyncio.create_task(otr.run_offline_tasks_loop(lp, EID))
        await asyncio.sleep(0.2)
        idle = lp.fetches
        otr.kick_poll()
        woke = await wait_until(lambda: lp.fetches > idle, 1.0)
        await _stop(loop_task)
        otr.POLL_INTERVAL_S = _pi
        check("release: the runner polls the moment it is kicked (its interval is 30 s)",
              idle == 0 and woke is not None and woke < 0.9, f"idle={idle} woke={woke}")
    finally:
        me._get_memory_embedding = _orig_embed
        S.TURN_WAIT_S = _orig_wait
        brain_service._entity_id = _orig_eid


# ── 14. a message typed while the turn is held ──────────────────────────────
async def case_nudge_during_hold():
    """A different message sent while a turn runs becomes a nudge, waiting for
    the turn's session frame. A turn the hold refuses never gets one, so the
    registry must send those messages on as the next turn, never drop them."""
    from server.turn_registry import TurnRegistry
    reg = TurnRegistry()
    ran = []

    def refused_turn(release):
        async def inner():
            await release.wait()          # the 45 s hold
            yield "2:" + json.dumps([{"type": "identity_pending"}]) + "\n"
            yield "0:" + json.dumps("Nothing has been done with this yet.") + "\n"
        return inner

    def followup(text):
        async def inner():
            ran.append(text)
            yield "2:" + json.dumps([{"type": "session", "session_id": "s2", "turn_token": "t2"}]) + "\n"
            yield "0:" + json.dumps("ok") + "\n"
        return inner()

    async def idle(key):
        for _ in range(100):
            if reg.active_for(key) is None:
                return
            await asyncio.sleep(0.02)

    release = asyncio.Event()
    rec, mode = reg.start_or_attach(thread_key="k1", entity_id=EID, app_id="circles", message="first",
                                    make_inner=refused_turn(release), make_followup=followup)
    r2, m2 = reg.start_or_attach(thread_key="k1", entity_id=EID, app_id="circles", message="second, typed during the hold",
                                 make_inner=lambda: None, make_followup=followup)
    r3, m3 = reg.start_or_attach(thread_key="k1", entity_id=EID, app_id="circles", message="and a third",
                                 make_inner=lambda: None, make_followup=followup)
    check("nudge: (setup) both messages arrived as nudges into the held turn",
          (mode, m2, m3) == ("started", "nudged", "nudged") and len(rec.pending_nudges) == 2, repr((mode, m2, m3)))
    release.set()
    await asyncio.wait_for(rec.done.wait(), 3.0)
    await idle("k1")
    await asyncio.sleep(0.1)
    check("nudge: the messages sent during the hold are sent on as the NEXT turn, once, joined",
          ran == ["second, typed during the hold\n\nand a third"], repr(ran))
    check("nudge: the held turn's stream says so (a typed frame, with the new turn's id)",
          any(f.startswith("2:") and "nudge_requeued" in f for f in rec.frames), repr(rec.frames))
    check("nudge: nothing is left pending on the ended turn", rec.pending_nudges == [])

    # A turn that is STOPPED keeps the stop: nothing restarts it.
    ran.clear()
    release = asyncio.Event()
    rec, _ = reg.start_or_attach(thread_key="k2", entity_id=EID, app_id="circles", message="first",
                                 make_inner=refused_turn(release), make_followup=followup)
    reg.start_or_attach(thread_key="k2", entity_id=EID, app_id="circles", message="typed",
                        make_inner=lambda: None, make_followup=followup)
    rec.task.cancel()
    try:
        await rec.task
    except BaseException:
        pass
    await asyncio.sleep(0.1)
    check("nudge: a turn the person stopped does not restart their typed message", ran == [], repr(ran))

    # A turn that reached its session delivers nudges the normal way (no requeue).
    ran.clear()

    def with_session():
        async def inner():
            yield "2:" + json.dumps([{"type": "session", "session_id": "live", "turn_token": "t"}]) + "\n"
            await asyncio.sleep(0.2)
            yield "0:" + json.dumps("done") + "\n"
        return inner
    rec, _ = reg.start_or_attach(thread_key="k3", entity_id=EID, app_id="circles", message="first",
                                 make_inner=with_session(), make_followup=followup)
    await asyncio.sleep(0.05)
    reg.start_or_attach(thread_key="k3", entity_id=EID, app_id="circles", message="mid-turn",
                        make_inner=lambda: None, make_followup=followup)
    await asyncio.wait_for(rec.done.wait(), 3.0)
    await asyncio.sleep(0.1)
    check("nudge: a turn that had a session pushes its nudge to the live loop, no requeue",
          ran == [] and not any("nudge_requeued" in f for f in rec.frames), repr((ran, rec.frames)))

    # The real stream wrapper builds the follow-up turn from the nudged text and
    # the thread ONLY: tab A's attachments, frame, artifact and brief never ride
    # on tab B's text.
    calls = []
    _orig = brain_service._send_message_inner

    async def rec_inner(*a, **kw):
        calls.append((a, kw))
        yield '0:"x"\n'
    mk = brain_service._followup_maker((), {
        "entity_id": EID, "message": "m1", "app_id": "circles", "thread_id": THREAD,
        "project_id": 7, "thread_override": {"provider": "local", "model_id": "m"},
        "attachments": [{"name": "tabA-secret.pdf"}], "frame": {"view": "tabA"},
        "artifact_id": "art-A", "system_turn": True, "origin_request": "tab A's ask",
        "history_before": "x", "turn_type_id": "ty"})
    mk2 = brain_service._followup_maker((None, "m1"), {"entity_id": EID, "thread_id": THREAD})
    brain_service._send_message_inner = rec_inner
    try:
        async for _ in mk("typed in tab B"):
            pass
        async for _ in mk2("typed later 2"):
            pass
    finally:
        brain_service._send_message_inner = _orig
    kw = calls[0][1] if calls else {}
    check("nudge: the follow-up keeps the thread, its model and the app, and the typed text",
          kw.get("message") == "typed in tab B" and kw.get("thread_id") == THREAD
          and kw.get("project_id") == 7 and kw.get("thread_override") == {"provider": "local", "model_id": "m"},
          repr(calls))
    check("nudge: tab A's attachments, frame, artifact, brief and origin never ride on tab B's text",
          not any(k in kw for k in ("attachments", "frame", "artifact_id", "system_turn",
                                    "origin_request", "history_before", "turn_type_id")), repr(kw))
    check("nudge: a positional message is replaced too",
          len(calls) == 2 and calls[1][1].get("message") == "typed later 2", repr(calls))


def case_model_call_marks_the_takeover():
    """The first model call of a turn is what makes a later failure "after the
    model saw it". The flag must be set where that call is made, before it."""
    from mastercode.runtime.session import EntityRuntime
    src = inspect.getsource(EntityRuntime._send_message_core)
    call = src.index("result = await _call_with_overflow_retry(\n            grok_messages,")
    mark = src.find("_tk.model_saw = True")
    first_call = src.index("await _call_with_overflow_retry(")
    check("takeover: the model-saw flag is set immediately before the turn's first model call",
          0 <= mark < call and first_call == call + len("result = ") and call - mark < 400,
          f"mark={mark} call={call} first={first_call}")
    acted = src.find("_tk_acted.acted = True")
    pub = src.find("publish_turn_trace(\n                    _turn_token, tool_actions=tool_actions")
    check("takeover: the acted flag is set where a tool call completes, right after its trace is published",
          0 <= pub < acted and acted - pub < 1400, f"pub={pub} acted={acted}")




async def case_no_model_blocked_and_stop_in_wait():
    import mastercode.memory_engine as me
    from mastercode.brain import offline_task_runner as otr
    _orig_embed, _orig_wait, _orig_eid = me._get_memory_embedding, S.TURN_WAIT_S, brain_service._entity_id
    _ww, _wp = H.WAKE_WAIT_S, H.WAKE_POLL_S
    try:
        # ── 1. no chat model: the request stays held and runs once one is set up ──
        pool = StorePool(placeholder_row())
        rt, out, built, task = await _refused_turn(pool, app_id="circles", thread_id=THREAD)
        env = _Env(rt, pool)
        await env.start()
        try:
            env.grant()
            env.reply = {"response": "I don't have a chat model configured yet.",
                         "model_unconfigured": True}
            await env.fire(0)
            check("no-model: the wake is held (put back, backing off), not completed",
                  pool.events[0]["status"] == "scheduled"
                  and any("identity_defers" in e[0] for e in pool.executed)
                  and not any("SET status=$2" in e[0] for e in pool.executed), repr(pool.executed))
            check("no-model: the no-model text is NOT posted as the request's result, and no run is recorded",
                  env.posts == [] and not any("calendar_event_runs" in e[0] for e in pool.executed),
                  repr((env.posts, pool.executed)))
            await env.fire(0)
            check("no-model: still no model → still held (never spent)",
                  env.posts == [] and pool.events[0]["status"] == "scheduled", repr(env.posts))
            # a chat model gets configured: the route save releases the held request
            env.reply = {"content": "Done: x.txt is in the workspace."}
            pool.executed.clear()
            await otr.release_saved_requests(pool, EID)
            check("no-model: saving a route makes the held request due now",
                  pool.events[0].get("released") is True, repr(pool.events[0]))
            await env.fire(0)
            check("no-model: it then runs once and its result is posted once",
                  env.posts == ["Done: x.txt is in the workspace."]
                  and pool.events[0]["status"] == "completed", repr((env.posts, pool.events[0]["status"])))
            from server.routes import inference as inf_routes
            src = inspect.getsource(inf_routes)
            check("no-model: the real route that saves a chat route releases held requests",
                  "release_saved_requests" in src.split('@router.put("/routing"')[1].split('@router.delete("/routing/{role}"')[0],
                  "inference.py: PUT /routing does not call release_saved_requests")
        finally:
            env.stop()
        await _stop(task)

        # ── 2. a crisis-floor / blocked turn: held, NOT due, until the next ordinary turn ──
        pool = StorePool(placeholder_row())
        rt, out, built, task = await _refused_turn(pool, app_id="circles", thread_id=THREAD)
        env = _Env(rt, pool)
        await env.start()
        try:
            env.grant()
            sess, built2, err = await _person_turn(rt, pool, "i want to die", mode="blocked")
            await _drain_bg()
            held = [e for e in pool.executed if "SET cancelled_reason=$4" in e[0]]
            check("blocked: the request is kept for the person as a CANCELLED row with its own reason (no date sentinel)",
                  len(held) == 1 and "interval" not in held[0][0] and held[0][1][3] == "held_for_person"
                  and pool.events[0]["status"] == "cancelled"
                  and pool.events[0].get("reason") == "held_for_person"
                  and not any("years" in e[0] for e in pool.executed), repr(pool.executed))
            check("blocked: nothing makes it run on its own (status cancelled; no due-now or deferral update)",
                  not any("cancelled_reason=NULL" in e[0] for e in pool.executed), repr(pool.executed))
            check("blocked: the line says it stays held until the next message",
                  len(env.posts) == 1 and "until your next message" in env.posts[0], repr(env.posts))
            await otr.release_saved_requests(pool, EID)
            check("blocked: a release (grant landing / model saved) does not touch it",
                  pool.events[0].get("released") is not True and pool.events[0]["status"] == "cancelled",
                  repr(pool.events[0]))
            check("blocked: the in-memory flag stays on for it, so the person's next turn looks",
                  H._MAYBE is True, repr(H._MAYBE))
            H.reset_saved_state()
            sess2, built3, err3 = await _person_turn(rt, pool, "sorry, that was a bad moment. please do it")
            check("blocked: the person's next ordinary turn takes it over as usual (even after a restart's boot look)",
                  getattr(sess2, "held_requests", None) == [MSG] and pool.events[0]["status"] == "cancelled"
                  and pool.events[0].get("reason") == H.CARRIED_REASON,
                  repr((getattr(sess2, "held_requests", None), pool.events[0])))
            # every reader that lists scheduled / running work ignores a cancelled row; the delete clears it
            from mastercode import circles_threads as CT
            import mastercode.runtime.timeline as tl_mod
            import server.routes.agents as agents_mod
            check("blocked: the reason is one constant, and thread delete clears it",
                  CT.HELD_FOR_PERSON_REASON == H._HELD_REASON == "held_for_person"
                  and "HELD_FOR_PERSON_REASON" in inspect.getsource(CT.delete_thread), "")
            check("blocked: the Agents list, the timeline and the task list only read scheduled/running rows",
                  "e.status IN ('scheduled', 'running')" in inspect.getsource(agents_mod)
                  and "status IN ('scheduled', 'running')" in inspect.getsource(tl_mod), "")
        finally:
            env.stop()
        await _stop(task)

        # ── 4. a Stop while the turn waits for a running wake keeps what it already took ──
        H.WAKE_POLL_S, H.WAKE_WAIT_S = 0.03, 5.0
        pool = StorePool(placeholder_row())
        rt, out, built, task = await _refused_turn(pool, app_id="circles", thread_id=THREAD)
        import copy
        pool.events.append(copy.deepcopy(pool.events[0]))     # a second held request
        env = _Env(rt, pool)
        await env.start()
        fake = None
        try:
            env.grant()
            pool.events[0]["status"] = "running"
            gate = asyncio.Event()

            async def _live():
                await gate.wait()
            fake = asyncio.create_task(_live())
            fake._event_id = 1
            otr._RUNNING.add(fake)
            turn = asyncio.create_task(_person_turn(rt, pool, "thanks", mode="ok"))
            await asyncio.sleep(0.3)
            check("stop-wait: (setup) the turn took the scheduled wake and now waits on the live one",
                  not turn.done() and pool.events[1]["status"] == "cancelled", repr(pool.events))
            turn.cancel()
            try:
                await turn
            except BaseException:
                pass
            await _drain_bg()
            check("stop-wait: the wake already taken is settled by the Stop (stays cancelled, Stop line posted)",
                  pool.events[1]["status"] == "cancelled" and env.posts == [H.STOPPED_LINE]
                  and not any("cancelled_reason=NULL" in e[0] for e in pool.executed),
                  repr((pool.events[1]["status"], env.posts)))
            check("stop-wait: the live wake it was waiting for is untouched",
                  pool.events[0]["status"] == "running", repr(pool.events[0]["status"]))
        finally:
            if fake is not None:
                otr._RUNNING.discard(fake)
            env.stop()
        await _stop(task)
    finally:
        H.WAKE_WAIT_S, H.WAKE_POLL_S = _ww, _wp
        me._get_memory_embedding = _orig_embed
        S.TURN_WAIT_S = _orig_wait
        brain_service._entity_id = _orig_eid




async def case_recurring_outage_and_read_only_tools():
    from mastercode.brain import offline_task_runner as otr
    from mastercode.runtime.session import tool_acts
    import datetime as _dt
    pool = StorePool(placeholder_row())
    reset_seed(pool)
    env = _Env(object(), pool)
    await env.start()
    try:
        pool.events.append({"event_type": "scheduled_task", "summary": "daily report", "status": "running",
                            "payload": {"prompt": "make the report", "origin": {}}})

        async def fire():
            pool.events[0]["status"] = "running"
            await otr._run_one(pool, EID, {
                "id": 1, "title": "daily report", "recurrence": "every:3600",
                "start_time": _dt.datetime.now(_dt.timezone.utc),
                "payload": pool.events[0]["payload"]})

        def notices():
            return [p for p in env.posts if "no chat model" in p]
        # ── outage #1: noticed once, deferred each firing ────────────────────
        env.reply = {"response": "I don't have a chat model configured yet.", "model_unconfigured": True}
        await fire()
        await fire()
        check("outage: the person is told ONCE, on the first deferral (not once per retry)",
              len(notices()) == 1 and "has not run" in notices()[0], repr(env.posts))
        check("outage: the no-model reply is never posted as the task's result",
              not any("don't have a chat model" in p for p in env.posts), repr(env.posts))
        check("outage: the deferral is counted", pool.events[0]["payload"].get("identity_defers") == 2,
              repr(pool.events[0]["payload"]))
        # ── the outage outlasts the cap: reported, and the counter is cleared on re-arm ──
        pool.events[0]["payload"]["identity_defers"] = otr.IDENTITY_DEFER_MAX
        await fire()
        check("outage: past the cap the task is reported as not run (once)",
              any("Nothing ran" in p and "no chat model" in p for p in env.posts), repr(env.posts))
        check("outage: re-arming the recurring task clears the deferral counter",
              "identity_defers" not in pool.events[0]["payload"], repr(pool.events[0]["payload"]))
        # ── recovery: the next firing runs normally, not at once as "60 tries" ──
        env.reply = {"content": "The report is ready."}
        before = len(env.runtime_calls)
        await fire()
        check("recovery: after the outage the next firing RUNS (no false 'tries' failure)",
              len(env.runtime_calls) == before + 1 and env.posts[-1] == "The report is ready."
              and "identity_defers" not in pool.events[0]["payload"], repr((env.posts[-3:], pool.events[0]["payload"])))
        # ── a new outage is noticed once again ───────────────────────────────
        env.reply = {"response": "I don't have a chat model configured yet.", "model_unconfigured": True}
        n_before = len(notices())
        await fire()
        await fire()
        check("outage: a NEW outage gets its own single notice",
              len(notices()) == n_before + 1, repr(env.posts))
    finally:
        env.stop()

    # ── read-only tools do not count as having acted ─────────────────────────
    check("acted: a web search and a read are not actions; a write or a build is",
          tool_acts("web_search") is False and tool_acts("code_read") is False
          and tool_acts("workspace_file") is True and tool_acts("build") is True
          and tool_acts("some_tool_nobody_classified") is True, "")
    from mastercode.runtime.session import EntityRuntime
    src = inspect.getsource(EntityRuntime._send_message_core)
    # Judged on the call's target since the search-mode doors: in groups
    # mode `_eff_name` IS func_name
    # (tool_doors.effective_call), so this is the same test it always was.
    check("acted: the tool loop sets it through tool_acts, never for the close_turn signal",
          "if func_name != _ct.TOOL_NAME and tool_acts(_eff_name) and not (" in src, "")




async def case_real_registry_time_limit():
    """The time limit, driven through the REAL turn registry and stream wrapper
    (nothing stubs _turn_timed_out): the registry's wait_for cancels the turn
    before it records the status, so the runtime must still tell a time limit
    from the person's Stop."""
    import mastercode.memory_engine as me
    from mastercode.brain import offline_task_runner as otr
    import server.turn_registry as TR
    from mastercode.runtime.session import EntityRuntime
    _orig_embed, _orig_wait, _orig_eid = me._get_memory_embedding, S.TURN_WAIT_S, brain_service._entity_id
    _lim, _rt, _post = TR.TURN_TIMEOUT_S, brain_service._runtime, otr.post_to_origin_thread
    try:
        for label, acted, how, want_line, put_back in (
                ("limit, nothing acted", False, "limit", H.TIMEOUT_HELD_AGAIN_LINE, True),
                ("limit, after a tool call", True, "limit", H.TIMEOUT_PARTIAL_LINE, False),
                ("Stop, nothing acted", False, "stop", H.STOPPED_LINE, False),
                ("Stop in the limit's last half second", False, "stop_edge", H.STOPPED_LINE, False)):
            pool = StorePool(placeholder_row())
            rt, out, built, task = await _refused_turn(pool, app_id="circles")
            posts = []

            async def fake_post(_p, _e, _o, text_, **_k):
                posts.append(text_)
                return True
            otr.post_to_origin_thread = fake_post
            S.SEED_STATE.update({"status": "ok", "retrying": False})
            pool.row["permission_overrides"] = GRANT

            async def core(entity_id=None, user_message=None, app_id=None, thread_id=None,
                           progress_cb=None, **_kw):
                from mastercode.runtime.session import _TAKEOVER
                await rt._take_over_held(entity_id, project_id=None, app_id=app_id,
                                         thread_id=thread_id, history_context=None,
                                         progress_cb=progress_cb)
                tk = _TAKEOVER.get()
                if tk is not None:
                    tk.model_saw = True
                    tk.acted = acted
                await asyncio.sleep(60)          # a long turn
                return {"content": "never reached"}
            rt._send_message_core = core
            brain_service._runtime = rt
            reg = TR.get_turn_registry()
            TR.TURN_TIMEOUT_S = (1 if how == "limit" else 2 if how == "stop_edge" else 600)
            frames = []

            async def drain():
                async for f in brain_service.send_message(
                        entity_id=EID, message=f"a long job ({label})", app_id="circles"):
                    frames.append(f)
            drainer = asyncio.create_task(drain())
            if how in ("stop", "stop_edge"):
                await wait_until(lambda: any(r.thread_key for r in reg._active.values())
                                 and pool.events[0]["status"] == "cancelled", 5.0)
                rec = next(iter(reg._active.values()))
                if how == "stop_edge":
                    # inside the half-second allowance before the 2 s limit: the
                    # clock alone would call this the limit.
                    await wait_until(lambda: time.time() >= rec.started_at + 1.6, 5.0)
                await reg.cancel(rec.turn_id)
            await asyncio.wait_for(drainer, 15)
            await asyncio.sleep(0.2)
            await _drain_bg()
            check(f"real registry[{label}]: settled with the right wording, through the real cancel",
                  posts == [want_line], repr((posts, frames[-2:])))
            check(f"real registry[{label}]: {'put back with the normal deferral' if put_back else 'not put back'}",
                  (pool.events[0]["status"] == "scheduled" and pool.events[0]["payload"].get("identity_defers") == 1)
                  if put_back else pool.events[0]["status"] == "cancelled",
                  repr((pool.events[0]["status"], pool.events[0]["payload"])))
            await _stop(task)
    finally:
        TR.TURN_TIMEOUT_S, brain_service._runtime, otr.post_to_origin_thread = _lim, _rt, _post
        me._get_memory_embedding = _orig_embed
        S.TURN_WAIT_S = _orig_wait
        brain_service._entity_id = _orig_eid




async def case_held_request_reads_as_held():
    """A request kept for the person's next message is stored 'cancelled' with a
    reason; the Host's read, the calendar list and the pane must say it is held."""
    import uuid as _uuid
    import datetime as _dt
    import server.bridge.memory_handlers as MH
    ev = _uuid.uuid4()
    now = _dt.datetime.now(_dt.timezone.utc)

    def row(status, reason):
        return {"event_id": ev, "entity_id": EID, "event_type": "scheduled_task", "event_at": now,
                "summary": "Your request, held until my tools arrived", "status": status,
                "source": "platform", "recurrence": None, "payload": json.dumps({"saved_request": True}),
                "cost_usd": None, "tokens_in": None, "tokens_out": None, "model_id": None,
                "related_message_id": None, "related_artifact_uri": None, "related_project_id": None,
                "cancelled_reason": reason}

    class P:
        def __init__(self, r):
            self.r, self.sql = r, []

        async def fetchrow(self, sql, *a):
            return self.r

        async def fetch(self, sql, *a):
            self.sql.append(sql)
            return [self.r]
    _gp, _eid = brain_service.get_pool, brain_service._entity_id
    brain_service._entity_id = EID
    try:
        brain_service.get_pool = lambda: P(row("cancelled", "held_for_person"))
        held = await MH.handle_calendar_read({"event_id": str(ev)})
        brain_service.get_pool = lambda: P(row("cancelled", "a newer message in this thread took it over"))
        taken = await MH.handle_calendar_read({"event_id": str(ev)})
        brain_service.get_pool = lambda: P(row("scheduled", None))
        sched = await MH.handle_calendar_read({"event_id": str(ev)})
        check("held-read: the Host's read returns the cancelled reason and says plainly the request is held",
              held.get("cancelled_reason") == "held_for_person" and held.get("held") is True
              and "next message" in (held.get("state") or ""), repr(held))
        check("held-read: any other cancellation, and a live event, are not reported as held",
              "held" not in taken and taken.get("cancelled_reason") and "held" not in sched
              and sched.get("cancelled_reason") is None, repr((taken, sched)))
        pl = P(row("cancelled", "held_for_person"))
        brain_service.get_pool = lambda: pl
        lst = await MH.handle_calendar_list({})
        check("held-read: the calendar LIST rows carry the reason and the held flag too (the pane reads them)",
              "cancelled_reason" in pl.sql[0] and lst["events"][0].get("held") is True
              and lst["events"][0].get("cancelled_reason") == "held_for_person", repr((pl.sql, lst)))
    finally:
        brain_service.get_pool, brain_service._entity_id = _gp, _eid
    # The calendar is the Calendar app's (Circles' calendar pane left with
    # the rail): a held
    # task reads as held, its own tone and note, never as a failure.
    cal = (Path_ROOT / "system-apps/calendar/app.js").read_text()
    check("held-pane: the Calendar app reads a held task as held, with its tone and note (no failure)",
          "function isHeld(e) { return e.status === 'cancelled' && (e.cancelled_reason === HELD_FOR_PERSON || e.held === true); }" in cal
          and "function statusWord(e) { return isHeld(e) ? 'held'" in cal
          and "function statusTone(e) { return isHeld(e) ? 'warn'" in cal
          and "'Runs with your next message.'" in cal, "")




async def case_takeover_final_reasons_and_crash_sweep():
    import mastercode.memory_engine as me
    from mastercode.brain import offline_task_runner as otr
    import server.turn_registry as TR
    _orig_embed, _orig_wait, _orig_eid = me._get_memory_embedding, S.TURN_WAIT_S, brain_service._entity_id
    try:
        # ── every settled takeover leaves a final reason, never 'taken over' ──
        for mode, want, label in (("ok", H.CARRIED_REASON, "carried"),
                                  ("stop", H.STOPPED_REASON, "Stop"),
                                  ("stop_acted", H.STOPPED_REASON, "Stop after acting"),
                                  ("error_acted", H.PARTIAL_REASON, "failure after acting"),
                                  ("blocked", "held_for_person", "blocked")):
            pool = StorePool(placeholder_row())
            rt, out, built, task = await _refused_turn(pool, app_id="circles", thread_id=THREAD)
            env = _Env(rt, pool)
            await env.start()
            try:
                env.grant()
                await _person_turn(rt, pool, "thanks", mode=mode)
                await _drain_bg()
                check(f"final-reason[{label}]: the row no longer says a turn is still running",
                      pool.events[0].get("reason") == want and pool.events[0]["status"] == "cancelled",
                      repr((pool.events[0].get("reason"), pool.events[0]["status"])))
            finally:
                env.stop()
            await _stop(task)
        # a put-back leaves no cancelled reason at all (the row is scheduled again)
        pool = StorePool(placeholder_row())
        rt, out, built, task = await _refused_turn(pool, app_id="circles", thread_id=THREAD)
        env = _Env(rt, pool)
        await env.start()
        try:
            env.grant()
            await _person_turn(rt, pool, "thanks", mode="error")
            check("final-reason[put back]: the row is scheduled again", pool.events[0]["status"] == "scheduled", "")
        finally:
            env.stop()
        await _stop(task)

        # ── the pod dies mid-turn: nothing settles the row; the sweep keeps it, flagged ──
        pool = StorePool(placeholder_row())
        rt, out, built, task = await _refused_turn(pool, app_id="circles", thread_id=THREAD)
        await rt.send_message(entity_id=EID, user_message="and a second one", app_id="circles",
                              thread_id=THREAD)            # folds the first into the second
        pool.row["permission_overrides"] = GRANT
        S.SEED_STATE.update({"status": "ok", "retrying": False})
        env = _Env(rt, pool)
        await env.start()
        try:
            # a turn takes the request over, then the process dies: no settle ever runs
            handle = await rt._take_over_held(EID, project_id=None, app_id="circles",
                                              thread_id=THREAD, history_context=None)
            check("crash: (setup) the takeover is in flight: row cancelled, 'taken over', nothing settled",
                  pool.events[1]["status"] == "cancelled" and pool.events[1].get("reason") == "taken", repr(pool.events[1]))
            young = pool.events[1].get("age_s", 0)
            await otr._sweep_orphans(pool, EID)
            check("crash: a takeover younger than the turn limit is left alone (its turn may be running)",
                  pool.events[1].get("reason") == "taken", repr(pool.events[1]))
            pool.events[1]["age_s"] = TR.TURN_TIMEOUT_S + 600   # the process died long ago
            await otr._sweep_orphans(pool, EID)
            check("crash: the boot sweep keeps it for the person's next message, flagged, never due",
                  pool.events[1].get("reason") == "held_for_person"
                  and pool.events[1]["payload"].get("crash_unknown") is True
                  and pool.events[1]["status"] == "cancelled", repr(pool.events[1]))
            await env.fire(1)
            check("crash: it is never auto-run (a cancelled row is not claimed or run)",
                  env.runtime_calls == [], repr(env.runtime_calls))
            sess, built2, err = await _person_turn(rt, pool, "hello again", mode="ok")
            held = getattr(sess, "held_requests", None)
            check("crash: the next person turn's takeover carries it with the interrupted flag",
                  held and isinstance(held[-1], dict) and held[-1].get("interrupted") is True
                  and held[-1]["text"] == "and a second one"
                  or (held and any(isinstance(h, dict) and h.get("interrupted") for h in held)), repr(held))

            async def facts(*_a, **_k):
                return {}
            import mastercode.runtime.frame as frame_mod
            import mastercode.runtime.prompt_schema as ps
            _of = frame_mod.frame_facts
            frame_mod.frame_facts = facts
            try:
                frame_text = await ps._b_frame(None, sess, "hello again")
            finally:
                frame_mod.frame_facts = _of
            check("crash: that turn is told it may have been partly done before a restart, so to check before redoing it",
                  "interrupted by a restart" in frame_text and "partly done" in frame_text
                  and "check before redoing it" in frame_text and "not yet run" not in frame_text.split("restart")[0].split("\n")[-1],
                  frame_text)
        finally:
            env.stop()
        await _stop(task)
        check("crash: an untouched held request still reads as 'not yet run' (no restart warning)",
              "not yet run" in H.held_line("do X") and "restart" not in H.held_line("do X"), H.held_line("do X"))
    finally:
        me._get_memory_embedding = _orig_embed
        S.TURN_WAIT_S = _orig_wait
        brain_service._entity_id = _orig_eid




def _flag_as_crashed(ev):
    """What the boot sweep leaves: kept for the person's next message, flagged."""
    ev["status"], ev["reason"], ev["age_s"] = "cancelled", "held_for_person", 0
    ev["payload"]["crash_unknown"] = True


async def case_crash_flagged_never_schedules():
    """A request whose turn died with its process may be partly done. Whatever
    path would turn a saved request back into a scheduled wake, it comes back to
    held_for_person instead, flag kept; the runner also words such a payload
    as interrupted."""
    import mastercode.memory_engine as me
    from mastercode.brain import offline_task_runner as otr
    import datetime as _dt
    import server.turn_registry as _TR
    TR_LIMIT = _TR.TURN_TIMEOUT_S
    _orig_embed, _orig_wait, _orig_eid = me._get_memory_embedding, S.TURN_WAIT_S, brain_service._entity_id

    def stays_held(ev):
        return (ev["status"] == "cancelled" and ev.get("reason") == "held_for_person"
                and ev["payload"].get("crash_unknown") is True)
    try:
        # ── every settle outcome that would put a request back ──────────────
        for outcome, why in (("failed", ""), ("timeout", ""), ("not_run", "model_unconfigured"),
                             ("ok", ""), ("unqueued", "")):
            pool = StorePool(placeholder_row())
            pool.events.append({"event_type": "scheduled_task", "summary": "held", "status": "cancelled",
                                "reason": "taken", "payload": {"saved_request": True, "origin": {},
                                                               "crash_unknown": True}})
            posts = []
            handle = H.Takeover(pool, EID)
            handle.add(1, pool.events[0]["payload"])
            handle.ids = [1]
            handle.model_saw = False
            H.post_line = lambda _p, _e, _o, text=None, _posts=posts: _posts.append(text)
            try:
                await handle.settle(outcome, why)
            finally:
                H.post_line = _real_post_line
            if outcome == "ok":
                # not a put-back: nothing to schedule (carried or not-run folds into put-back)
                check(f"crash-flag[settle {outcome}]: never becomes a scheduled wake",
                      pool.events[0]["status"] == "cancelled", repr(pool.events[0]))
            else:
                check(f"crash-flag[settle {outcome}]: goes back to held_for_person, flag kept, never scheduled",
                      stays_held(pool.events[0]), repr(pool.events[0]))
            if outcome in ("failed", "not_run"):
                check(f"crash-flag[settle {outcome}]: the line says it stays held until the next message",
                      len(posts) == 1 and "stays held until your next message" in posts[0]
                      and "interrupted by a restart" in posts[0], repr(posts))

        # ── the grant landing / a model route saved (release) ───────────────
        pool = StorePool(placeholder_row())
        for flagged in (False, True):
            pool.events.append({"event_type": "scheduled_task", "summary": "held", "status": "scheduled",
                                "payload": {"saved_request": True, "origin": {},
                                            **({"crash_unknown": True} if flagged else {})}})
        pool.events.append({"event_type": "scheduled_task", "summary": "held", "status": "cancelled",
                            "reason": "held_for_person",
                            "payload": {"saved_request": True, "origin": {}, "crash_unknown": True}})
        await otr.release_saved_requests(pool, EID)
        check("crash-flag[release]: an ordinary held request is made due now",
              pool.events[0].get("released") is True and pool.events[0]["status"] == "scheduled", repr(pool.events[0]))
        check("crash-flag[release]: a flagged row found scheduled is put back to held, never released",
              stays_held(pool.events[1]) and pool.events[1].get("released") is not True, repr(pool.events[1]))
        check("crash-flag[release]: a flagged row already held is untouched",
              stays_held(pool.events[2]) and pool.events[2].get("released") is not True, repr(pool.events[2]))

        # ── the person's turn fails after taking a flagged request over ─────
        for mode in ("error", "raise", "error_after", "unconfigured"):
            pool = StorePool(placeholder_row())
            rt, out, built, task = await _refused_turn(pool, app_id="circles", thread_id=THREAD)
            _flag_as_crashed(pool.events[0])
            env = _Env(rt, pool)
            await env.start()
            try:
                env.grant()
                sess, built2, err = await _person_turn(rt, pool, "thanks", mode=mode)
                await _drain_bg()
                check(f"crash-flag[turn {mode}]: the failed turn leaves it held, not scheduled",
                      stays_held(pool.events[0]), repr(pool.events[0]))
                check(f"crash-flag[turn {mode}]: the words still reached that turn, marked interrupted",
                      (getattr(sess, "held_requests", None) or [{}])[0] == {"text": MSG, "interrupted": True}
                      if mode != "unconfigured" else True, repr(getattr(sess, "held_requests", None)))
                await otr.release_saved_requests(pool, EID)
                await env.fire(0)
                check(f"crash-flag[turn {mode}]: and neither a release nor a fire runs it",
                      stays_held(pool.events[0]) and env.runtime_calls == [], repr((pool.events[0], env.runtime_calls)))
            finally:
                env.stop()
            await _stop(task)

        # ── the fold path: a newer refused message takes a flagged request ───
        for wake_ok in (True, False):
            pool = StorePool(placeholder_row())
            rt, out, built, task = await _refused_turn(pool, app_id="circles", thread_id=THREAD)
            _flag_as_crashed(pool.events[0])
            env = _Env(rt, pool)
            await env.start()
            try:
                pool.fail_events = not wake_ok
                await rt.send_message(entity_id=EID, user_message="actually do this instead",
                                      app_id="circles", thread_id=THREAD)
                pool.fail_events = False
                await _drain_bg()
                if wake_ok:
                    new = pool.events[1]
                    check("crash-flag[fold]: the newer wake carries the flagged request as interrupted",
                          any(isinstance(l, dict) and l.get("interrupted") for l in new["payload"].get("earlier", [])),
                          repr(new["payload"]))
                    env.grant()
                    await env.fire(1)
                    check("crash-flag[fold]: its woken turn is told the earlier one was interrupted by a restart",
                          len(env.messages) == 1 and "interrupted by a restart" in env.messages[0]
                          and "check before redoing it" in env.messages[0], repr(env.messages))
                else:
                    check("crash-flag[fold, wake not scheduled]: the flagged request goes back to held, not scheduled",
                          stays_held(pool.events[0]) and len(pool.events) == 1, repr(pool.events))
            finally:
                env.stop()
            await _stop(task)

        # ── the runner, as a second line of defence ─────────────────────────
        pool = StorePool(placeholder_row())
        rt, out, built, task = await _refused_turn(pool, app_id="circles", thread_id=THREAD)
        env = _Env(rt, pool)
        await env.start()
        try:
            env.grant()
            pool.events[0]["payload"]["crash_unknown"] = True       # a flagged row that reached the runner
            await env.fire(0)
            check("crash-flag[runner]: a flagged payload that reaches a turn is worded as interrupted, never 'do it now'",
                  len(env.messages) == 1 and "interrupted by a restart" in env.messages[0]
                  and "Do what it asks, now" not in env.messages[0], repr(env.messages))
        finally:
            env.stop()
        await _stop(task)

        # ── after a restart: rows taken over by a process that is gone ──────
        pool = StorePool(placeholder_row())
        for boot, age in (("a-process-that-is-gone", 5), (H.PROCESS_BOOT_ID, 5),
                          (H.PROCESS_BOOT_ID, TR_LIMIT + 600)):
            pool.events.append({"event_type": "scheduled_task", "summary": "held", "status": "cancelled",
                                "reason": "taken", "age_s": age,
                                "payload": {"saved_request": True, "origin": {}, "taken_boot": boot}})
        await otr._sweep_orphans(pool, EID)
        check("boot: a row taken by another process is kept at once, however young (exact boot id, no clocks)",
              stays_held(pool.events[0]), repr(pool.events[0]))
        check("boot: ...a young row taken by THIS process is left alone (its turn may be running)",
              pool.events[1].get("reason") == "taken", repr(pool.events[1]))
        check("boot: ...and one of this process's that outlived any turn is still caught (backstop)",
              stays_held(pool.events[2]), repr(pool.events[2]))

        class _LoopPool(StorePool):
            async def fetch(self, *_a, **_k):
                return []
        lp = _LoopPool(placeholder_row())
        _pi = otr.POLL_INTERVAL_S
        otr.POLL_INTERVAL_S = 30
        loop_task = asyncio.create_task(otr.run_offline_tasks_loop(lp, EID))
        await asyncio.sleep(0.3)
        await _stop(loop_task)
        otr.POLL_INTERVAL_S = _pi
        sweeps = [e for e in lp.executed if "{crash_unknown}" in e[0] and "taken_boot" in e[0]]
        check("boot: the runner's FIRST sweep matches rows on this process's boot id",
              sweeps and sweeps[0][1][4] == H.PROCESS_BOOT_ID, repr(lp.executed))
    finally:
        me._get_memory_embedding = _orig_embed
        S.TURN_WAIT_S = _orig_wait
        brain_service._entity_id = _orig_eid




# ── 15. the saved request's state machine, driven from its map ──────────────
# docs/DESIGN-saved-request-state-machine-2026-10-01.md lists every statement in
# server/ and brain/ that writes calendar_events, one row (T01...) each, and
# scripts/test_saved_request_state_map.py pins that table to the code and to the
# ids below. Each scenario starts a saved request in the state the row leaves,
# makes the event happen through the REAL function, and checks the invariant:
# a saved request that leaves running / taken-over without a recorded "nothing
# acted" outcome is held_for_person + crash_unknown, never scheduled.
SM_IDS = tuple(f"T{i:02d}" for i in range(1, 26))


def _sm_pool(state, *, flagged=False, boot=None, age=0, thread=THREAD, n=1):
    import uuid as _uuid
    pool = StorePool(placeholder_row())
    for _ in range(n):
        ev = {"event_type": "scheduled_task", "summary": "a saved request", "age_s": age,
              "event_id": str(_uuid.uuid4()),
              "payload": {"saved_request": True, "prompt": MSG,
                          "origin": {"interaction_context": "chat", "thread_id": thread}}}
        if state in ("scheduled", "running"):
            ev["status"] = state
        elif state == "taken":
            ev["status"], ev["reason"] = "cancelled", "taken"
            ev["payload"]["taken_boot"] = boot or H.PROCESS_BOOT_ID
        elif state == "held":
            ev["status"], ev["reason"] = "cancelled", "held_for_person"
        if flagged:
            ev["payload"]["crash_unknown"] = True
        pool.events.append(ev)
    return pool


def _sm_held_flagged(ev):
    return (ev["status"] == "cancelled" and ev.get("reason") == "held_for_person"
            and ev["payload"].get("crash_unknown") is True)


async def case_state_machine():
    import copy
    import mastercode.runtime.session as sess_mod
    import server.bridge.memory_handlers as MH
    from mastercode.brain import offline_task_runner as otr
    from mastercode import circles_threads as CT
    _gp, _eid_bs = brain_service.get_pool, brain_service._entity_id
    brain_service._entity_id = EID
    ok = {}

    def sm(id_, name, cond, extra=""):
        ok.setdefault(id_, 0)
        ok[id_] += 1
        check(f"{id_} {name}", cond, extra)

    def bind(pool):
        brain_service.get_pool = lambda: pool
        return pool
    try:
        # T01 ── (new) → scheduled
        pool = bind(StorePool(placeholder_row()))
        env_ = await MH.handle_calendar_append({
            "event_id": "11111111-1111-1111-1111-111111111111", "event_type": "scheduled_task",
            "summary": "held", "event_at": "2026-10-01T00:00:30Z", "status": "scheduled",
            "source": "platform", "payload": {"saved_request": True, "prompt": "x"}})
        sm("T01", "a new saved request is created scheduled, nothing has run",
           env_.get("ok") and pool.events[0]["status"] == "scheduled", repr(pool.events))

        # T02 ── scheduled → running (the runner's claim)
        pool = bind(_sm_pool("scheduled"))
        ran = []

        async def noop_run(_p, _e, claimed):
            ran.append(claimed["id"])
        _gr, _pi = otr._guarded_run, otr.POLL_INTERVAL_S
        otr._guarded_run, otr.POLL_INTERVAL_S = noop_run, 0.02
        t = asyncio.create_task(otr.run_offline_tasks_loop(pool, EID))
        await wait_until(lambda: bool(ran), 3.0)
        await _stop(t)
        otr._guarded_run, otr.POLL_INTERVAL_S = _gr, _pi
        sm("T02", "the runner's poll claims a due saved request: it is running",
           pool.events[0]["status"] == "running" and ran == [1], repr((pool.events[0]["status"], ran)))

        # T03 ── running, alive
        pool = bind(_sm_pool("running"))
        await otr._touch(pool, EID, 1)
        sm("T03", "a touch keeps a running request running", pool.events[0]["status"] == "running", "")

        # T04 ── running → scheduled: no free turn slot, nothing ran
        pool = bind(_sm_pool("running"))
        await otr._requeue(pool, EID, 1)
        sm("T04", "no free turn slot: put back scheduled (the turn never started)",
           pool.events[0]["status"] == "scheduled", "")

        # T05 ── running → scheduled, delayed: refused before the model
        pool = bind(_sm_pool("running"))
        await otr._defer_for_identity(pool, EID, 1, 1)
        sm("T05", "refused before the model: put back scheduled, delayed and counted",
           pool.events[0]["status"] == "scheduled" and pool.events[0]["payload"].get("identity_defers") == 1, "")

        # T06 ── running → scheduled: PII plan not landed
        pool = bind(_sm_pool("running"))
        await otr._wait_for_plan(pool, EID, 1, 2)
        sm("T06", "PII plan not landed: put back scheduled (nothing ran)",
           pool.events[0]["status"] == "scheduled", "")

        # T07 ── payload only
        pool = bind(_sm_pool("scheduled"))
        pool.events[0]["payload"]["plan_event_id"] = "x"
        await otr._plan_confirmed(pool, EID, 1)
        sm("T07", "a confirmed plan changes the payload, never the status",
           pool.events[0]["status"] == "scheduled" and "plan_event_id" not in pool.events[0]["payload"], "")

        # T08 ── payload only (fold)
        pool = bind(_sm_pool("scheduled", n=2))
        got = await otr._fold_into_newest(pool, EID, pool.events[0]["payload"], 1)
        sm("T08", "a wake folding into the newest held request changes its payload only",
           got and pool.events[1]["status"] == "scheduled" and pool.events[1]["payload"].get("earlier"), repr(pool.events))

        # T09 ── finalize
        for okf, want in ((True, "completed"), (False, "failed")):
            pool = bind(_sm_pool("running"))
            await otr._finalize(pool, 1, EID, None, None, okf)
            sm("T09", f"a finished one-shot run ends {want}, never scheduled",
               pool.events[0]["status"] == want, repr(pool.events[0]["status"]))

        # T10 ── running → cancelled (thread deleted)
        pool = bind(_sm_pool("running"))
        pool.row["permission_overrides"] = GRANT
        S.SEED_STATE.update({"status": "ok", "entity_id": EID, "retrying": False})
        from mastercode.circles_threads import DELETED_THREAD_REFUSAL
        _bs_send, _bs_rt, _post = brain_service.send_message, brain_service.get_runtime, otr.post_to_origin_thread

        async def deleted_stream(**_kw):
            yield "3:" + json.dumps(DELETED_THREAD_REFUSAL) + "\n"

        async def quiet_post(*_a, **_k):
            return True
        brain_service.send_message, brain_service.get_runtime = deleted_stream, (lambda: object())
        otr.post_to_origin_thread = quiet_post
        try:
            await otr._run_one(pool, EID, {"id": 1, "title": "t", "recurrence": None, "start_time": None,
                                           "payload": pool.events[0]["payload"]})
        finally:
            brain_service.send_message, brain_service.get_runtime = _bs_send, _bs_rt
            otr.post_to_origin_thread = _post
        sm("T10", "the thread was deleted while it ran: cancelled",
           pool.events[0]["status"] == "cancelled", repr(pool.events[0]["status"]))

        # T11 ── stuck running → held + crash_unknown (saved) / scheduled (ordinary)
        pool = bind(_sm_pool("running", age=otr.ORPHAN_AFTER_S + 60, n=2))
        pool.events[1]["payload"].pop("saved_request")           # an ordinary scheduled task
        await otr._sweep_orphans(pool, EID)
        sm("T11", "a SAVED request stuck running (its process died) is held and flagged, never scheduled",
           _sm_held_flagged(pool.events[0]), repr(pool.events[0]))
        sm("T11", "an ordinary task stuck running is reset to scheduled as before",
           pool.events[1]["status"] == "scheduled", repr(pool.events[1]))
        pool = bind(_sm_pool("running", age=otr.ORPHAN_AFTER_S + 60))
        _live = otr._RUNNING
        t_live = asyncio.create_task(asyncio.sleep(5))
        t_live._event_id = 1
        otr._RUNNING.add(t_live)
        await otr._sweep_orphans(pool, EID)
        otr._RUNNING.discard(t_live)
        t_live.cancel()
        sm("T11", "a request THIS process is running is left alone", pool.events[0]["status"] == "running", "")

        # T12 ── scheduled / held → taken over (boot id recorded, flag kept)
        H.reset_saved_state()
        pool = bind(_sm_pool("scheduled", n=2))
        _flag_as_crashed(pool.events[1])
        handle = await H.hand_over_saved_requests(pool, EID, app_id="circles", thread_id=THREAD,
                                                  wait_running=False)
        sm("T12", "the person's next turn takes a scheduled and a held request over, recording this process's boot id",
           all(e["status"] == "cancelled" and e.get("reason") == "taken"
               and e["payload"].get("taken_boot") == H.PROCESS_BOOT_ID for e in pool.events), repr(pool.events))
        sm("T12", "...and a held request keeps its crash flag through the takeover",
           pool.events[1]["payload"].get("crash_unknown") is True and 2 in handle.flagged, repr(handle.flagged))

        # T13 ── stale running → taken + crash_unknown; a failed turn then leaves it held
        H.reset_saved_state()
        pool = bind(_sm_pool("running"))
        handle = await H.hand_over_saved_requests(pool, EID, app_id="circles", thread_id=THREAD,
                                                  wait_running=True)
        sm("T13", "a stale running request is taken over flagged as possibly partly done",
           pool.events[0]["status"] == "cancelled" and pool.events[0]["payload"].get("crash_unknown") is True
           and handle.flagged == [1], repr(pool.events[0]))
        H.post_line = lambda *_a, **_k: None
        try:
            await handle.settle("failed")
        finally:
            H.post_line = _real_post_line
        sm("T13", "...and when that turn fails before acting it is held, not scheduled",
           _sm_held_flagged(pool.events[0]), repr(pool.events[0]))

        # T14 ── taken over → settled / held
        for outcome, why, acted, want in (("ok", "", False, H.CARRIED_REASON),
                                          ("stopped", "", False, H.STOPPED_REASON),
                                          ("failed", "", True, H.PARTIAL_REASON),
                                          ("not_run", "blocked", False, "held_for_person")):
            pool = bind(_sm_pool("taken"))
            h = H.Takeover(pool, EID)
            h.add(1, pool.events[0]["payload"])
            h.model_saw, h.acted = (outcome == "ok"), acted
            H.post_line = lambda *_a, **_k: None
            try:
                await h.settle(outcome, why)
            finally:
                H.post_line = _real_post_line
            sm("T14", f"a takeover settled '{outcome}'{' after acting' if acted else ''} ends '{want}', never scheduled",
               pool.events[0]["status"] == "cancelled" and pool.events[0].get("reason") == want,
               repr(pool.events[0]))

        # T15 ── reschedule: back off / release
        for flagged in (False, True):
            pool = bind(_sm_pool("taken", flagged=flagged))
            await H.reschedule(pool, EID, ids=[1], how="backoff")
            if flagged:
                sm("T15", "a flagged request is never put back to scheduled (backoff)",
                   _sm_held_flagged(pool.events[0]), repr(pool.events[0]))
            else:
                sm("T15", "an unflagged request whose turn did not act goes back scheduled, delayed and counted",
                   pool.events[0]["status"] == "scheduled" and pool.events[0]["payload"].get("identity_defers") == 1,
                   repr(pool.events[0]))
            pool = bind(_sm_pool("scheduled", flagged=flagged))
            await H.reschedule(pool, EID, how="release")
            if flagged:
                sm("T15", "a release never makes a flagged request due: it is put back to held",
                   _sm_held_flagged(pool.events[0]) and pool.events[0].get("released") is not True,
                   repr(pool.events[0]))
            else:
                sm("T15", "a release makes an unflagged request due now", pool.events[0].get("released") is True, "")

        # T16 ── taken over by a process that is gone → held + flag
        pool = bind(_sm_pool("taken", boot="another-process"))
        await H.sweep_taken_over(pool, EID, 1800)
        sm("T16", "a request taken over by a process that is gone is held and flagged at once, never scheduled",
           _sm_held_flagged(pool.events[0]), repr(pool.events[0]))
        pool = bind(_sm_pool("taken"))
        await H.sweep_taken_over(pool, EID, 1800)
        sm("T16", "...but one taken over by THIS process, young, is left alone",
           pool.events[0].get("reason") == "taken", "")

        # T17 ── thread deleted (the transaction needs a real connection: its statements are read)
        src = inspect.getsource(CT.delete_thread)
        sm("T17", "deleting a thread cancels scheduled and running wakes, and clears a held request's reason",
           "status IN ('scheduled', 'running')" in CT._WAKEUP_IN_THREAD
           and "HELD_FOR_PERSON_REASON" in src and "status = 'cancelled'" in src, "")
        sm("T17", "...and none of its statements schedules anything",
           "status = 'scheduled'" not in src and "status='scheduled'" not in src, "")

        # T18 ── cancel by event id (the person / the Host / the calendar pane)
        for st in ("scheduled", "running"):
            pool = bind(_sm_pool(st))
            r = await MH.handle_calendar_cancel({"event_id": pool.events[0]["event_id"], "reason": "no"})
            sm("T18", f"a cancel of a {st} saved request ends cancelled",
               r.get("ok") and pool.events[0]["status"] == "cancelled", repr((r, pool.events[0])))

        # T19 ── the Host's schedule_task cancel
        from mastercode.runtime import tools as T
        pool = bind(_sm_pool("scheduled"))
        r = await T._exec_schedule_task({"op": "cancel", "event_id": pool.events[0]["event_id"]})
        sm("T19", "schedule_task cancel ends a scheduled saved request cancelled",
           r.get("status") == "ok" and pool.events[0]["status"] == "cancelled", repr(r))

        # T20 ── the Agents app
        try:
            import server.routes.agents as agents_mod
        except Exception as exc:  # noqa: BLE001
            agents_mod = None
            sm("T20", f"(agents route not importable here: {type(exc).__name__}; statement read instead)",
               "status='cancelled'" in (ROOT_P / "server/routes/agents.py").read_text(), "")
        if agents_mod is not None:
            pool = bind(_sm_pool("running"))
            _ae = getattr(agents_mod, "_entity_id", None)
            agents_mod._entity_id = lambda: EID
            try:
                r = await agents_mod.cancel_scheduled(pool.events[0]["event_id"])
            finally:
                if _ae is not None:
                    agents_mod._entity_id = _ae
            sm("T20", "the Agents cancel ends a running saved request cancelled",
               pool.events[0]["status"] == "cancelled", repr((r, pool.events[0])))

        # T21 ── a browser agent run's cancel
        try:
            import server.browser_agents as ba
            pool = bind(_sm_pool("scheduled"))
            await ba._cancel_event(pool, EID, pool.events[0]["event_id"])
            sm("T21", "a browser agent's cancel ends a scheduled saved request cancelled",
               pool.events[0]["status"] == "cancelled", repr(pool.events[0]))
        except ImportError as exc:
            sm("T21", f"(browser_agents not importable here: {exc}; statement read instead)",
               "status='cancelled'" in (ROOT_P / "server/browser_agents.py").read_text(), "")

        # T22 ── marked completed
        pool = bind(_sm_pool("running"))
        r = await MH.handle_calendar_complete({"event_id": pool.events[0]["event_id"], "result": {"ok": 1}})
        sm("T22", "marking a saved request complete ends it completed, never scheduled",
           r.get("ok") and pool.events[0]["status"] == "completed", repr(r))

        # T23 ── a restore's renumber: not a state transition (run for real in
        # scripts/test_saved_request_sql_real_db.py)
        import server.host_restore as hr
        rsrc = inspect.getsource(hr.renumber)
        sm("T23", "a restore's renumber updates one column per table, entity_id: its statement sets "
                  "and matches that column only, so status, reason and payload are untouched",
           'SET {_qi(col)} = $1 WHERE {_qi(col)} = $2' in rsrc, "")
        sm("T23", "...and the columns it renumbers are those that hold this Host's number, never a state column",
           all("status" not in c and "payload" not in c and "reason" not in c for _, c in hr.SELF_REFS), repr(hr.SELF_REFS))

        # T24 ── a platform event wakes a schedule_task wake_on row (run for
        # real in scripts/test_schedule_wake_on_event.py): a saved request
        # carries no wake_on, so it is never moved, and nothing is scheduled.
        import server.host_events as HE
        for st in ("scheduled", "running", "held"):
            pool = bind(_sm_pool(st))
            before_ev = copy.deepcopy(pool.events[0])
            for ev_name in HE.EVENT_NAMES:
                await HE.emit(pool, EID, ev_name, {"name": "x", "grant_id": 1, "ticket_id": 1})
            sm("T24", f"every host event leaves a {st} saved request exactly as it was",
               pool.events[0] == before_ev, repr((before_ev, pool.events[0])))
        esrc = inspect.getsource(HE.emit)
        sm("T24", "...and its statement matches only rows waiting on wake_on, still scheduled, and "
                  "sets no status",
           "payload->'wake_on'->>'event' = $2" in esrc and "status = 'scheduled'" in esrc
           and "status" not in esrc.split("UPDATE calendar_events SET", 1)[-1].split("WHERE", 1)[0],
           "")
        # T25 ── a Circles task that names no thread gets its topic recorded
        # in its origin (offline_task_runner.ensure_origin_thread): payload
        # only, never a state, and never for a task that names a thread.
        import mastercode.brain.offline_task_runner as OTR
        import mastercode.circles_threads as CTH
        _home = CTH.home_thread

        async def _fake_home(_p, _e, _t, *, title):
            return "11111111-2222-4333-8444-555555555555"

        class _Rec:
            def __init__(self):
                self.sql = []

            async def execute(self, q, *a):
                self.sql.append((q, a))
                return "UPDATE 1"
        CTH.home_thread = _fake_home
        try:
            rec = _Rec()
            o = await OTR.ensure_origin_thread(rec, EID, {"interaction_context": "chat"}, title="t", event_id=7)
            rec2 = _Rec()
            o2 = await OTR.ensure_origin_thread(rec2, EID, {"interaction_context": "chat",
                                                            "thread_id": "22222222-2222-4333-8444-555555555555"},
                                                title="t", event_id=8)
        finally:
            CTH.home_thread = _home
        upd = [q for q, _a in rec.sql if "UPDATE calendar_events" in q]
        sm("T25", "a task that names no thread gets its topic recorded in payload.origin, one statement, "
                  "which sets no status",
           o.get("thread_id") == "11111111-2222-4333-8444-555555555555" and len(upd) == 1
           and "'{origin}'" in upd[0] and "status" not in upd[0].split("SET", 1)[-1].split("WHERE", 1)[0],
           repr(rec.sql))
        sm("T25", "...and a task that names a thread is never written",
           rec2.sql == [] and o2.get("thread_id") == "22222222-2222-4333-8444-555555555555", repr(rec2.sql))
    finally:
        brain_service.get_pool, brain_service._entity_id = _gp, _eid_bs
    missing = [i for i in SM_IDS if not ok.get(i)]
    check("state machine: every transition in the map has a scenario that ran", not missing, repr(missing))




def _run(name, coro_fn):
    try:
        asyncio.run(asyncio.wait_for(coro_fn(), 20))
    except Exception as exc:  # noqa: BLE001
        check(f"{name}: ran to completion", False, f"{type(exc).__name__}: {exc}")


_orig_get_pool = brain_service.get_pool
_orig_seed_codebook = genesis._seed_codebook
try:
    _run("seed", case_seed_timing)
    _run("no-bridge", case_no_bridge)
    _run("turn", case_turn_waits_for_tools)
    _run("durable", case_durable_path_waits)
    _run("failed-write", case_write_did_not_take)
    _run("never", case_write_never_lands)
    _run("steady", case_steady_and_chat_only)
    _run("health", case_health_check)
    _run("crash", case_boot_read_crash)
    _run("saved", case_refusal_saved_and_runs_once)
    _run("stream", case_stream_frame)
    case_wording()
    case_model_call_marks_the_takeover()
    _run("not-run", case_not_run_callers)
    _run("concurrent", case_concurrent_turns)
    _run("saved-semantics", case_saved_request_semantics)
    _run("honest", case_note_is_honest_and_bounded)
    _run("handover", case_handover)
    _run("stale", case_stale_and_missed_takeovers)
    _run("no-model", case_no_model_blocked_and_stop_in_wait)
    _run("outage", case_recurring_outage_and_read_only_tools)
    _run("registry-limit", case_real_registry_time_limit)
    _run("held-read", case_held_request_reads_as_held)
    _run("final-reasons", case_takeover_final_reasons_and_crash_sweep)
    _run("crash-flag", case_crash_flagged_never_schedules)
    _run("state-machine", case_state_machine)
    _run("running", case_running_wake)
    _run("nudge", case_nudge_during_hold)
    _run("gating", case_gating_and_extras)
finally:
    brain_service.get_pool = _orig_get_pool
    genesis._seed_codebook = _orig_seed_codebook

# A saved request is the person's own words. The offline runner
# sends it as a system turn (its own lane), and the turn must not count as
# woken (tools' phase-repair rule refuses a woken "build it again").
import inspect as _insp  # noqa: E402
_run_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                             "brain", "mastercode", "brain", "offline_task_runner.py")).read()
_bs_params = _insp.signature(brain_service._send_message_inner).parameters
from mastercode.runtime import session as _sess_mod  # noqa: E402
_core_params = _insp.signature(_sess_mod.EntityRuntime._send_message_core).parameters
_core_src = _insp.getsource(_sess_mod.EntityRuntime._send_message_core)
check("a saved request reaches the turn as the person's words, never a woken turn",
      'saved_request=bool(payload.get("saved_request"))' in _run_src
      and "saved_request" in _bs_params and "saved_request" in _core_params
      and '"woken": bool(system_turn) and not saved_request' in _core_src)

if FAILS:
    print(f"\n[tools-first-turn] {len(FAILS)} FAILED")
    sys.exit(1)
print("\ntools before the first turn: all passed")
