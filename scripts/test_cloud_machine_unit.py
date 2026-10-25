#!/usr/bin/env python3
"""A cloud Host and the machine under it (server/cloud_machine.py, and what
owner_login and routes/owner.py do with it).

  1. setup_blocked: a Host with no machine (no CSERIES_MACHINE_DIR) and a
     machine with no vault never block; a machine whose vault is not yet
     enrolled blocks /setup with its unlock page's address; one whose state
     file is missing blocks too (a box mid-restart), and an enrolled vault
     does not.
  2. While it blocks, create_owner refuses with that address and makes no
     owner; once the vault is enrolled the same request makes the owner.
  3. /setup shows the disk step and its link instead of the owner form while
     it blocks.
  4. The access log and the unlock record are read from the machine's files,
     a line that is not a JSON object skipped; /machine shows them newest
     first, says so plainly when the log no longer matches, offers Restart
     only when a restart is needed, and asks a signed-out visitor to sign in.
  5. restart asks the door helper for machine.restart over its socket and
     reports a refusal; a Host with no machine cannot ask.

No database or container: the pool and the helper are stand-ins.

    python3 scripts/test_cloud_machine_unit.py   (repo root, image's deps)
"""
import asyncio
import json
import os
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from server import cloud_machine as M  # noqa: E402
from server import owner_login as L  # noqa: E402

failed = 0


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


class Pool:
    def __init__(self):
        self.inserted = []

    async def fetchrow(self, sql, *args):
        return None

    async def fetchval(self, sql, *args):
        if "INSERT INTO owner_account" in sql:
            self.inserted.append(args[0])
            return args[0]
        return None

    async def execute(self, sql, *args):
        return "DELETE 0"


def write_state(d, vault, mismatch=False, reboot=False):
    with open(os.path.join(d, M.STATE), "w") as f:
        json.dump({"vault": vault, "unlock_host": "unlock-orchard.k7x2m9.cloud.privatae.ai",
                   "access_log": {"count": 2, "head": "a" * 64, "mismatch": mismatch},
                   "patches": {"reboot_required": reboot}}, f)


async def main():
    os.environ.pop(M.DIR_ENV, None)
    check(M.setup_blocked() is None and M.state() is None and M.access_log() == [],
          "a Host with no machine: nothing blocks, nothing to read")
    with tempfile.TemporaryDirectory() as d:
        os.environ[M.DIR_ENV] = d
        check(M.setup_blocked() == "the machine's unlock page",
              "a machine that has not written its state yet blocks setup")
        write_state(d, {"state": "absent", "since": None, "enrolled": None})
        check(M.setup_blocked() is None, "a machine without a vault never blocks")
        write_state(d, {"state": "open", "since": "2026-10-23T10:00:00Z", "enrolled": False})
        check(M.setup_blocked() == "https://unlock-orchard.k7x2m9.cloud.privatae.ai/",
              "a vault not yet enrolled blocks setup with its unlock page's address")

        # 2. create_owner
        pool = Pool()
        L._pool = lambda: pool
        L._owner_email, L._setup_code = None, "abc123-def456-789abc"
        L._failures.clear()
        try:
            await L.create_owner("abc123-def456-789abc", "owner@example.com", "x" * 20, client="198.51.100.9")
            check(False, "create_owner refuses while the disk's keys are not set up")
        except L.LoginError as e:
            check("https://unlock-orchard.k7x2m9.cloud.privatae.ai/" in str(e) and not pool.inserted,
                  "create_owner refuses while the disk's keys are not set up, naming where", str(e))

        # 3. /setup
        from server.routes import owner as R
        page = (await R.setup_page()).body.decode()
        check("Set up the disk&#39;s keys" in page and "unlock-orchard.k7x2m9.cloud.privatae.ai" in page and
              "/api/owner/setup" not in page, "/setup shows the disk step, not the owner form")

        write_state(d, {"state": "open", "since": "2026-10-23T10:00:00Z", "enrolled": True})
        check(M.setup_blocked() is None, "an enrolled vault does not block")
        await L.create_owner("abc123-def456-789abc", "owner@example.com", "x" * 20, client="198.51.100.9")
        check(pool.inserted == ["owner@example.com"], "once enrolled the same request makes the owner")

        # 4. the log, the record and /machine
        rows = [{"seq": 1, "at": "2026-10-23T10:12:01.123456Z", "actor": "provisioner", "action": "stop",
                 "summary": "Invented: the first action."},
                {"seq": 2, "at": "2026-10-23T11:00:00.000000Z", "actor": "breakglass", "action": "vps-console",
                 "summary": "Invented: <the second> action."}]
        with open(os.path.join(d, M.LOG), "w") as f:
            f.write(json.dumps(rows[0]) + "\nnot json\n[1]\n" + json.dumps(rows[1]) + "\n")
        with open(os.path.join(d, M.RECORD), "w") as f:
            f.write(json.dumps({"at": "2026-10-23T09:59:00Z", "from": "203.0.113.7", "with": "a passkey",
                                "opened": True}) + "\n")
        check(M.access_log() == rows and len(M.unlock_record()) == 1,
              "the log and the record are read, lines that are not objects skipped")

        # owner_session.request_host(scope) -> str and live_session(scope) ->
        # (email, token), as routes/owner.py imports them.
        R.request_host = lambda scope: R.HOME.host
        signed_in = {"email": "owner@example.com"}

        async def live(scope):
            return signed_in["email"], "t"
        R.live_session = live

        class Req:
            scope = {}
        page = (await R.machine_page(Req())).body.decode()
        check(page.index("the second") < page.index("the first"), "/machine lists the newest action first")
        check("&lt;the second&gt;" in page and "<the second>" not in page, "a summary is escaped")
        check("Privatae&#x27;s provisioner (automated, no AI)" in page and "by hand" not in page,
              "a row without detail.by is labelled by its actor, and none claims a person")
        by = lambda kind, name: {"actor": "breakglass", "detail": json.dumps({"by": {"kind": kind, "name": name}})}
        check(M.actor_label(by("ai", "Claude Code")) == "An AI agent working for Privatae (Claude Code)",
              "an AI agent's row says so")
        check(M.actor_label(by("automated", "invented.service")) == "A Privatae program, no AI (invented.service)",
              "a program's row says so")
        check(M.actor_label(by("person", "a person at Privatae")) == "A person at Privatae"
              and M.actor_label(by("person", "Invented Operator")) == "A person at Privatae (Invented Operator)",
              "a person's row says so")
        check(M.actor_label(by("robot", "x")) == "Privatae, through its emergency tool"
              and M.actor_label({"actor": "breakglass", "detail": "not json"}) == "Privatae, through its emergency tool"
              and M.actor_label({"actor": "server-login", "detail": json.dumps({"by": "person"})})
              == "A sign-in to the server this machine runs on",
              "a by it cannot read falls back to the actor")
        check(M.actor_label(by("person", "Invented \u202eOperator")) == "Privatae, through its emergency tool"
              and M.actor_label(by("person", "<b>Invented</b>")) == "Privatae, through its emergency tool"
              and M.actor_label(by("ai", 'Invented "approved by you"')) == "Privatae, through its emergency tool"
              and M.actor_label(by("person", "x" * 81)) == "Privatae, through its emergency tool"
              and M.actor_label(by("person", "Zoë Example (laptop)")) == "A person at Privatae (Zoë Example (laptop))",
              "a name the platform's access log would refuse is not shown; a name it takes is")
        check('id="mismatch"' not in page and 'id="restart"' not in page,
              "no warning while the log matches; no Restart while none is needed")
        write_state(d, {"state": "open", "since": None, "enrolled": True}, mismatch=True, reboot=True)
        page = (await R.machine_page(Req())).body.decode()
        check('id="mismatch"' in page and "no longer matches" in page, "a log that no longer matches is said plainly")
        check('id="restart"' in page, "Restart is offered when security updates need it")
        signed_in["email"] = None
        resp = await R.machine_page(Req())
        check(resp.status_code == 303 and resp.headers["location"] == "/login",
              "a signed-out visitor is sent to sign in")

        # 5. restart over the helper's socket
        sock = os.path.join(d, "helper.sock")
        seen = []

        async def serve(reader, writer):
            line = await reader.readline()
            seen.append(json.loads(line))
            answer = {"ok": True, "in_seconds": 5} if len(seen) == 1 else {"ok": False, "error": "too soon"}
            writer.write((json.dumps(answer) + "\n").encode())
            await writer.drain()
            writer.close()
        server = await asyncio.start_unix_server(serve, sock)
        check(await M.restart(sock) == 5 and seen == [{"op": "machine.restart"}],
              "restart asks the helper for machine.restart")
        try:
            await M.restart(sock)
            check(False, "a refusal is reported")
        except M.RestartError as e:
            check("too soon" in str(e), "a refusal is reported with the helper's reason", str(e))
        server.close()
        await server.wait_closed()
    os.environ.pop(M.DIR_ENV, None)
    try:
        await M.restart(sock)
        check(False, "a Host with no machine cannot ask for a restart")
    except M.RestartError:
        check(True, "a Host with no machine cannot ask for a restart")


asyncio.run(main())
sys.exit(failed)
