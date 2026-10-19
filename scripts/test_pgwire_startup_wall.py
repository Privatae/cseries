#!/usr/bin/env python3
"""Invariant: the memory-mirror lane can only ever carry replication.

server/network/pgwire_relay.py splices an authenticated bastion WebSocket onto
the entity's PostgreSQL unix socket, which has `trust` auth and no network
listener. The ONLY thing standing between a bastion and unauthenticated
superuser SQL on the user's memory is that the relay discards the client's
StartupMessage and writes its own with replication=true.

So that discard is an invariant, not an implementation detail. This gate
asserts it directly: whatever a client claims about who it is, what database
it wants, or what options it wants set, none of those bytes reach the backend.

Run: python3 scripts/test_pgwire_startup_wall.py
"""
import importlib.util
import os
import struct
import sys

ROOT = os.environ.get("APP_ROOT", os.path.join(os.path.dirname(__file__), ".."))
_spec = importlib.util.spec_from_file_location(
    "pgwire_relay", os.path.join(ROOT, "server", "network", "pgwire_relay.py"))
pg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pg)

FAILS = []


def check(cond, label):
    if not cond:
        FAILS.append(label)


def startup(version, params):
    body = struct.pack("!I", version)
    for k, v in params:
        body += k.encode() + b"\x00" + v.encode() + b"\x00"
    body += b"\x00"
    return struct.pack("!I", len(body) + 4) + body


def drive(chunks, dbname="hostmem", app="mirror:BOX"):
    """Feed chunks through a fresh rewriter; return (to_client, to_backend)."""
    r = pg.StartupRewriter(dbname, app)
    out_c, out_b = b"", b""
    for c in chunks:
        a, b = r.feed(c)
        out_c += a
        out_b += b
    return out_c, out_b


# 1. The kernel's own startup is well-formed and selects PHYSICAL replication.
msg = pg.build_startup("hostmem", "mirror:DESKTOP-X")
(length,) = struct.unpack("!I", msg[:4])
check(length == len(msg), "build_startup: length prefix must cover the whole message")
check(struct.unpack("!I", msg[4:8])[0] == 196608, "build_startup: must be protocol 3.0")
check(msg.endswith(b"\x00\x00"), "build_startup: must be NUL-terminated then terminated")
fields = dict(zip(msg[8:-1].split(b"\x00")[0::2], msg[8:-1].split(b"\x00")[1::2]))
check(fields.get(b"replication") == b"true",
      "build_startup: replication MUST be 'true' (physical walsender). "
      "'database' would be LOGICAL replication — that puts a full SQL parser "
      "behind the lane and removes the entire wall.")
check(fields.get(b"user") == b"entity", "build_startup: user must be the pod's own role")
check(fields.get(b"database") == b"hostmem", "build_startup: database must be the pod's own")
check(fields.get(b"application_name") == b"mirror:DESKTOP-X",
      "build_startup: application_name is how pg_stat_replication attributes the "
      "stream — it must be kernel-set, never client-set")

# 2. THE WALL. A hostile client startup is discarded in full.
hostile = startup(196608, [
    ("user", "postgres"),
    ("database", "template1"),
    ("options", "-c log_statement=none"),
    ("application_name", "definitely-not-a-mirror"),
    ("replication", "false"),
])
_, to_backend = drive([hostile])
for probe in (b"postgres", b"template1", b"log_statement", b"definitely-not-a-mirror"):
    check(probe not in to_backend,
          f"WALL BREACH: client-supplied {probe!r} reached the backend")
check(b"replication\x00true\x00" in to_backend,
      "WALL BREACH: a client asking for replication=false still got a non-replication "
      "session — the rewrite must force physical replication regardless")
check(to_backend == pg.build_startup("hostmem", "mirror:BOX"),
      "the bytes sent to the backend must be EXACTLY the kernel's own startup")

# 3. A perfectly ordinary SQL startup gets the same treatment — there is no
#    path by which a client obtains a query-capable session on this lane.
_, to_backend = drive([startup(196608, [("user", "entity"), ("database", "hostmem")])])
check(b"replication\x00true\x00" in to_backend,
      "an ordinary (non-replication) startup must still be forced to replication")

# 4. SSLRequest is answered 'N' and the following startup is still rewritten.
ssl_req = struct.pack("!II", 8, 80877103)
to_client, to_backend = drive([ssl_req + hostile])
check(to_client == b"N", "SSLRequest must be answered with a single 'N'")
check(b"postgres" not in to_backend and b"replication\x00true\x00" in to_backend,
      "the startup following an SSLRequest must still be rewritten")

# 5. Frame boundaries do not align with message boundaries. Byte-at-a-time must
#    behave identically — assuming one frame is one message is how relays desync.
whole = drive([ssl_req + hostile])
split = drive([bytes([b]) for b in (ssl_req + hostile)])
check(whole == split, "byte-at-a-time framing must produce identical output")

# 6. Bytes pipelined behind the startup are preserved, in order, after it.
trailer = b"Q\x00\x00\x00\x0eIDENTIFY_SYS"
_, to_backend = drive([hostile + trailer])
check(to_backend == pg.build_startup("hostmem", "mirror:BOX") + trailer,
      "pipelined bytes after the startup must be forwarded verbatim, after the rewrite")

# 7. Refusals. Each of these must raise rather than be forwarded.
for label, payload in (
    ("oversized startup length", struct.pack("!II", 999999, 196608)),
    ("cancel request", struct.pack("!IIII", 16, 80877102, 1, 2)),
    ("unsupported protocol version", startup(131072, [("user", "entity")])),
):
    try:
        drive([payload])
    except pg.PgWireError:
        pass
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{label}: raised {type(e).__name__}, expected PgWireError")
    else:
        FAILS.append(f"{label}: was ACCEPTED — must be refused")

# 8. A client cannot loop negotiations to make us buffer forever.
try:
    drive([ssl_req * 5])
except pg.PgWireError:
    pass
else:
    FAILS.append("repeated negotiations: must be refused past the cap")

# 9. The concurrency cap exists and is small — the DB container runs
#    max_connections=16 and the kernel's own pool needs its share.
check(1 <= pg.MAX_CONCURRENT_STREAMS <= 4,
      f"MAX_CONCURRENT_STREAMS={pg.MAX_CONCURRENT_STREAMS} is outside a sane range")

if FAILS:
    for f in FAILS:
        print(f"FAIL: {f}")
    print(f"PGWIRE_WALL_FAIL {len(FAILS)} check(s) failed")
    sys.exit(1)
print("PGWIRE_WALL_OK startup rewrite holds — client identity discarded, "
      "replication forced, framing stable, refusals enforced")
