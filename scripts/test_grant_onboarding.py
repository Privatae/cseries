#!/usr/bin/env python3
"""
A Host drafts only grants that can authorize something.

A Host asked its user to approve grants for 'service restart', 'sentinel apply',
'bastion self-update' and an observe grant 'with no expiry'. None of those is
a token any gate enforces, and propose_mgmt_grant drafted whatever it was given.

Container-free: APP_ROOT=<repo> python3 scripts/<this>. No pod, no DB; the
draft runs against a stub pool holding a roster, and every harness verdict is
checked against the REAL gate (nac_mgmt.harness_image_allowed).

  1. every purpose names only tokens a Host may grant, and the skill lists
     every purpose
  2. a purpose drafts exactly its ops; purposes and ops merge without repeats
  3. a token a Host may not grant is refused, nothing is written, and the
     refusal lists what can be granted: guesses, the op lane's own ops, a shell
  4. harness tokens form a set the gate can match: builds and service need
     the image the person names, a lone half is refused, image:* is refused
     both ways, coding harness images only in a lab grant on one machine;
     every accepted builds grant really lets the gate run that image
  5. an allow for a machine that never checked in is refused with the roster;
     a deny for it is not
  6. an op a machine does not answer yet is a caution; an ops grant on a
     machine that cannot apply is a caution
  7. what still asks the person follows the machine's write gate
  8. the lifetime: the result says 30 days, and approval refuses longer
  9. an unreadable roster checks nothing, and says so in the log

Exit 0 = all pass; exit 1 + [grant-onboarding] FAIL lines otherwise.
"""

import asyncio
import json
import logging
import os
os.environ.setdefault("CSERIES_OPERATOR_ENTITY_ID", "9003")  # the operator Host in these cases
import sys

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "brain"))

from server.network import grant_vocab, nac_commit, nac_dcn, nac_mgmt, nac_propose  # noqa: E402

fail = 0


def check(name, cond, detail=""):
    global fail
    print(("OK   " if cond else "FAIL ") + "[grant-onboarding] " + name
          + ("" if cond else f"  — {detail}"))
    if not cond:
        fail = 1


class Pool:
    def __init__(self, roster, broken=False):
        self.roster, self.broken, self.inserts = roster, broken, []

    async def fetch(self, sql, *a):
        if self.broken:
            raise RuntimeError("relation does not exist")
        assert "network_bastion_heartbeat" in sql and "write_gate" in sql and "second_factor" in sql
        assert "'stepup'" in sql
        j = lambda v: json.dumps(v) if v is not None else None  # noqa: E731 — jsonb arrives as text
        return [{"hostname": h, "ops": j(o), "write_gate": j(g), "second_factor": j(f),
                 "stepup": j(c)} for h, (o, g, f, c) in self.roster.items()]

    async def fetchval(self, sql, *a):
        assert sql.startswith("INSERT INTO nac_mgmt_grants")
        self.inserts.append(a)
        return len(self.inserts)


ALL = sorted(nac_dcn.KNOWN_BOX_OPS)
OLD = [o for o in ALL if o != "observe:logs"]
# hostname -> (ops it answers, write_gate, second_factor, stepup: a factor at its console)
ROSTER = {"build-server": (ALL, True, True, True), "edge-old": (OLD, True, True, True),
          "silent-box": (None, None, None, None), "home-laptop": (ALL, False, True, True),
          "lab-box": (ALL, True, True, True), "no-factor-box": (ALL, True, False, False),
          "unanchored-no-factor": (ALL, False, False, False),
          # a device pinned, no console factor: taps work, a pull's console ask does not
          "device-only": (ALL, True, True, False),
          # a bastion older than 0.33.0: no write gate or second_factor, a console factor
          "old-with-console": (ALL, None, None, True)}
USER_IMAGE = "image:docker.io/library/python:3.12"
HARNESS_IMAGE = "image:registry.example/cseries-harness-claude-code:0.2.15"
# The same image pinned by digest from a mirror, and a person's own image
# that happens to share the bare name.
HARNESS_IMAGE_NEW = "image:mirror.example/team/cseries-harness-claude-code@sha256:" + "d" * 64
USER_HARNESS_NAMED = "image:ghcr.io/someone/harness-claude-code:1"


def draft(pool=None, **kw):
    pool = pool or Pool(ROSTER)
    try:
        return pool, asyncio.run(nac_propose.propose_mgmt_grant(pool, **kw)), None
    except ValueError as e:
        return pool, None, str(e)


# 1
for name, p in grant_vocab.PURPOSES.items():
    check(f"purpose {name} names only grantable tokens", not grant_vocab.unknown_tokens(p["ops"]),
          grant_vocab.unknown_tokens(p["ops"]))
skill = open(os.path.join(ROOT, "default-skills/network.md")).read()
onboard = skill[skill.index("## Onboarding a machine"):skill.index("## propose_mgmt_grant")]
missing = [p for p in grant_vocab.PURPOSES if f"`{p}`" not in onboard]
check("the onboarding steps list every purpose", not missing, missing)
check("the machinery, the shell and the DCN lane are not grantable",
      not (grant_vocab.HOST_ALLOW & (grant_vocab.MACHINERY_OPS | grant_vocab.SESSION_OPS
                                     | grant_vocab.DCN_OPS))
      and {"op_grant_run", "op_sync", "pairing_grant_sync", "session:root"}
      <= grant_vocab.MACHINERY_OPS | grant_vocab.SESSION_OPS
      and {"dcn_read_range", "dcn_plan_apply", "dcn_grant_sync"} <= grant_vocab.DCN_OPS)
# Every signed-grant push (<kind>_grant_sync / <kind>_grant_drop) is sent by the
# pod under the person's own approval, never by a Host's grant. Checked as a
# class so a new kind the bastion adds (coder_grant_*, 0.50.0) cannot slip into
# what a Host may ask for.
grant_pushes = {o for o in nac_dcn.KNOWN_BOX_OPS if o.endswith(("_grant_sync", "_grant_drop"))}
check("no signed-grant push is grantable (coder_grant_* included)",
      grant_pushes and {"coder_grant_sync", "coder_grant_drop"} <= grant_pushes
      and not (grant_vocab.HOST_ALLOW & grant_pushes),
      sorted(grant_vocab.HOST_ALLOW & grant_pushes))
check("the op door, the harness tokens and the model-pull ask are",
      {"op_dry_run", "op_apply", "op_grant", "contain", "harness:run", "mode:service",
       "stepup_request", "stepup_status"} <= grant_vocab.HOST_ALLOW)
check("models carries the ask a pull needs (network skill: stepup_request, then stepup_status)",
      {"stepup_request", "stepup_status", "model:pull"} <= set(grant_vocab.PURPOSES["models"]["ops"]))

# 2
pool, r, err = draft(server="build-server", purposes=["observe"])
check("observe drafts its ops", r and r["ops"] == grant_vocab.PURPOSES["observe"]["ops"], err or r)
check("... one row written", len(pool.inserts) == 1)
check("... and names a proof call", r and r["purposes"]["observe"].get("prove_it_with") == "mgmt_run op='uptime'", r)
_, r, err = draft(server="build-server", purposes=["builds", "ops"], ops=[USER_IMAGE, "uptime", "harness:run"])
check("purposes + ops merge in order, no repeats",
      r and r["ops"] == ["harness:run", "op_dry_run", "op_apply", "op_grant", USER_IMAGE, "uptime"], err or r)

# 3
for bad in (["service:restart"], ["sentinel apply"], ["self-update"], ["observe"], ["op_grant_run"],
            ["pairing_grant_sync"], ["session:open"], ["df"]):
    pool, r, err = draft(server="build-server", ops=bad)
    check(f"allow {bad[0]!r} refused, nothing drafted", r is None and not pool.inserts and bad[0] in (err or ""), err)
check("the refusal lists what can be granted, and not the shell",
      err and "purpose (observe" in err and "uptime" in err and "no restart" in err
      and "session:open," not in err and "op_grant_run" not in err, err)
pool, r, err = draft(server="build-server", deny=["reboot"])
check("an unknown deny token is refused", r is None and "reboot" in (err or ""), err)
_, r, err = draft(server="build-server", deny=["session:open"], do_not_want="no terminal on this box")
check("a deny of the shell is accepted (host_gate enforces it)", r is not None and r["requires_stepup"] is False, err)
_, r, err = draft(server="build-server", ops=["session:open"])
check("an allowed shell says it needs no grant on 0.39.0+ and how to update an older box",
      r is None and "0.39.0" in (err or "") and "manage_bastion op='update'" in (err or ""), err)
for side in ("ops", "deny"):
    _, r, err = draft(server="build-server", **{side: ["dcn_read_range"]})
    check(f"a dcn_* {side} is refused, pointing at the store's own grant and how to stop reads",
          r is None and "belongs to the DCN lane" in (err or "")
          and "revoking or narrowing that store" in (err or ""), err)
_, r, err = draft(server="build-server", deny=["*"])
check("'*' is a valid deny", r is not None, err)
_, r, err = draft(server="build-server", ops=["*"])
check("'*' is not a valid allow", r is None, r)
_, r, err = draft(server="build-server", purposes=["fleet"])
check("an unknown purpose is refused with the list", r is None and "observe (" in (err or ""), err)

# 4
_, r, err = draft(server="build-server", purposes=["builds"])
check("builds without the person's image is refused", r is None and "image:<ref>" in (err or ""), err)
_, r, err = draft(server="build-server", purposes=["builds"], ops=[USER_IMAGE])
check("builds with their image drafts", r is not None, err)
g = [{"server": "build-server", "ops": r["ops"] if r else []}]
check("... and the real gate runs that image in git mode",
      nac_mgmt.harness_image_allowed(g, 9003, "build-server", USER_IMAGE[6:], mode="git"))
check("... and no other image",
      not nac_mgmt.harness_image_allowed(g, 9003, "build-server", "docker.io/library/node:22", mode="git"))
_, r, err = draft(server="build-server", purposes=["builds"], ops=[HARNESS_IMAGE])
check("a coding harness image is refused outside the lab", r is None and "coding harness" in (err or ""), err)
_, r, err = draft(server="lab-box", ops=["harness:run", "mode:lab", HARNESS_IMAGE])
check("a lab grant may name one, on the lab machine", r is not None, err)
_, r, err = draft(server="*", ops=["harness:run", "mode:lab", HARNESS_IMAGE])
check("... never on '*'", r is None and "lab machine" in (err or ""), err)
_, r, err = draft(server="build-server", purposes=["builds"], ops=[HARNESS_IMAGE_NEW])
check("a coding harness image pinned from a mirror is refused outside the lab",
      r is None and "coding harness" in (err or ""), err)
_, r, err = draft(server="lab-box", ops=["harness:run", "mode:lab", HARNESS_IMAGE_NEW])
check("... and a lab grant may name it, on the lab machine", r is not None, err)
_, r, err = draft(server="build-server", purposes=["builds"], ops=[USER_HARNESS_NAMED])
check("a person's own image that is merely called harness-<id> drafts", r is not None, err)
_, r, err = draft(server="build-server", ops=["harness:run", "mode:lab"])
check("mode:lab without its image is refused", r is None and "mode:lab" in (err or ""), err)
_, r, err = draft(server="build-server", ops=[USER_IMAGE])
check("a lone image token is refused", r is None and "harness:run" in (err or ""), err)
_, r, err = draft(server="build-server", ops=["harness:run"])
check("a lone harness:run is refused", r is None and "image" in (err or ""), err)
_, r, err = draft(server="build-server", ops=["harness:run", "mode:service"])
check("harness:run + mode:service with no image drafts (stop and status)", r is not None, err)
_, r, err = draft(server="build-server", purposes=["service"])
check("the service purpose needs the image to start one", r is None and "image:<ref>" in (err or ""), err)
_, r, err = draft(server="build-server", ops=["harness:run", "image:*"])
check("an allowed image:* is refused, and says why", r is None and "coding harness" in (err or ""), err)
_, r, err = draft(server="build-server", deny=["image:*"], do_not_want="never any container here")
check("a denied image:* is refused, pointing at harness:run",
      r is None and "never enforced" in (err or "") and "'harness:run'" in (err or ""), err)
check("... and that is what the gate does",
      nac_mgmt.harness_image_allowed([{"server": "b", "ops": ["harness:run", "image:*"]},
                                      {"server": "b", "ops": [], "deny": ["image:*"]}], 3, "b", "x", mode="git"))

# 5
pool, r, err = draft(server="new-box", purposes=["observe"])
check("an allow for a machine that never checked in is refused, with the roster and the enroll step",
      r is None and not pool.inserts and "build-server" in err and "manage_bastion op='enroll'" in err, err)
_, r, err = draft(server="new-box", deny=["harness:run"], do_not_want="no coding agents on my new laptop")
check("a deny for it drafts", r is not None, err)
_, r, err = draft(server="*", purposes=["observe"])
check("'*' drafts", r is not None, err)

# 6
_, r, err = draft(server="edge-old", purposes=["observe"])
check("an op the machine does not answer is a caution naming it",
      r is not None and any("observe:logs" in c for c in r["cautions"]), err or r)
_, r, err = draft(server="*", purposes=["observe"])
check("'*' names the machines that fall short", r and any("edge-old" in c for c in r["cautions"]), r)
_, r, err = draft(server="silent-box", purposes=["observe"])
check("a machine that never listed its ops gets no caution", r is not None and r["cautions"] == [], err or r)
_, r, err = draft(server="home-laptop", purposes=["ops"])
check("an ops grant on a machine that cannot apply is a caution",
      r is not None and any("cannot be applied there" in c for c in r["cautions"]), err or r)
check("... and its asks say so, with no promise of dry runs",
      r and "refused there" in r["purposes"]["ops"]["asks_the_user"]
      and "dry run" not in r["purposes"]["ops"]["asks_the_user"], r)
_, r, err = draft(server="no-factor-box", purposes=["contain", "observe"])
check("an anchored machine with no way to take an approval: a caution with the fix",
      r is not None and any("pin-device" in c and "stepup-enroll" in c for c in r["cautions"]), err or r)
check("... its asks say what asks is refused; a read is untouched",
      r and "refused until" in r["purposes"]["contain"]["asks_the_user"]
      and r["purposes"]["observe"]["asks_the_user"].startswith("never"), r)
_, r, err = draft(server="unanchored-no-factor", purposes=["models"])
check("a model pull on an unanchored machine with no console factor is refused too",
      r and "pulls are refused until they enrol one there" in r["purposes"]["models"]["asks_the_user"], r)
_, r, err = draft(server="device-only", purposes=["models"])
check("a device alone does not approve a pull (its ask is filed at the console): said, with a caution",
      r and "pulls are refused" in r["purposes"]["models"]["asks_the_user"]
      and "console" in r["purposes"]["models"]["asks_the_user"]
      and "or their device" not in r["purposes"]["models"]["asks_the_user"]
      and any("stepup-enroll" in c for c in r["cautions"]), r)
_, r, err = draft(server="old-with-console", purposes=["models"])
check("an old bastion with a console factor can take a pull: no refusal, no caution",
      r and "refused" not in r["purposes"]["models"]["asks_the_user"] and r["cautions"] == [], r)
_, r, err = draft(server="old-with-console", purposes=["ops"])
check("an ops grant on a machine that does not gate writes names the old-bastion case too",
      r and any("older than 0.33.0" in c for c in r["cautions"]), r)
_, r, err = draft(server="build-server", deny=["stepup_request"])
check("a deny of the step-up ask is refused, pointing at deny model:pull",
      r is None and "deny 'model:pull'" in (err or ""), err)

# 7
_, r, _ = draft(server="build-server", purposes=["contain"])
check("an anchored machine asks for every stop", r and r["purposes"]["contain"]["asks_the_user"] == "every stop", r)
_, r, _ = draft(server="home-laptop", purposes=["contain"])
check("a machine that does not gate writes asks nothing",
      r and "pairing is the authorization" in r["purposes"]["contain"]["asks_the_user"], r)
_, r, _ = draft(server="home-laptop", purposes=["models"])
check("... except a model pull", r and "each pull" in r["purposes"]["models"]["asks_the_user"], r)
_, r, _ = draft(server="*", purposes=["contain"])
check("'*' says both", r and "anchored to this Host" in r["purposes"]["contain"]["asks_the_user"], r)
_, r, _ = draft(server="build-server", purposes=["context", "backups"])
check("the context store's own writes and backups never ask, even when anchored",
      r and r["purposes"]["context"]["asks_the_user"].startswith("the store's own writes never ask")
      and "mgmt_run asks each time" in r["purposes"]["context"]["asks_the_user"]
      and r["purposes"]["backups"]["asks_the_user"].startswith("never"), r)
_, r, _ = draft(server="build-server", purposes=["ops"])
check("an anchored ops grant names the standing approval that stops the asking",
      r and "from now on" in r["purposes"]["ops"]["asks_the_user"], r)

# 7b: through mgmt_run a Host files only a model_pull ask and reads only its own
import base64  # noqa: E402

_pull = "model_pull|qwen3-embedding:4b|embed|3000000000|"
_details = base64.b64encode(json.dumps({"kind": "model_pull"}).encode()).decode()
_other = base64.b64encode(json.dumps({"kind": "dcn_store_grant"}).encode()).decode()
check("a model_pull ask is allowed",
      nac_mgmt.host_stepup_refusal(3, 11, "stepup_request", [_pull, _details]) is None)
check("any other kind is refused, naming the model_pull shape",
      all(w and "model_pull|<model>" in w for w in (
          nac_mgmt.host_stepup_refusal(3, 11, "stepup_request", ["dcn_store_grant|x", _other]),
          nac_mgmt.host_stepup_refusal(3, 11, "stepup_request", [_pull, _other]),
          nac_mgmt.host_stepup_refusal(3, 11, "stepup_request", ["critical_action|x", _details]))))
check("a malformed model_pull ask goes to the machine, whose words say what is wrong",
      nac_mgmt.host_stepup_refusal(3, 11, "stepup_request", [_pull]) is None
      and nac_mgmt.host_stepup_refusal(3, 11, "stepup_request", [_pull, "not base64!"]) is None)
skill_pull = skill[skill.index("## Putting a model on someone's machine"):]
check("the skill names the ask's exact arguments and the restart answer",
      "model_pull|<model>|<role>|<max_bytes>|<replaces>" in skill_pull
      and "not_filed_here" in skill_pull)


class _MRelay:
    def __init__(self):
        self.sent = []

    def worker_count(self, token_id):
        return 1

    async def dispatch_detailed(self, token_id, op, args, timeout, approval=""):
        self.sent.append((op, list(args)))
        if op == "stepup_request":
            return (json.dumps({"state": "pending", "request": "req-host-7"}).encode(), b"", 0), ""
        return (json.dumps({"state": "pending", "request": args[0]}).encode(), b"", 0), ""


class _GatePool:
    async def fetchval(self, sql, *a):
        return 1   # the token really is this server's


_audits = []


async def _audit(pool, eid, server, op, *, exit_code, decision):
    _audits.append((op, exit_code, decision))


async def _state(pool, eid):
    return [], set(), set()


_saved = (nac_mgmt._gate_state, nac_mgmt._decide, nac_mgmt.audit_mgmt, nac_mgmt.get_mgmt_relay)
_relay = _MRelay()
nac_mgmt._gate_state = _state
nac_mgmt._decide = lambda g, s, q, e, server, op: (True, "", "allowed")
nac_mgmt.audit_mgmt = _audit
nac_mgmt.get_mgmt_relay = lambda: _relay
try:
    def _run(op, args, origin="tool"):
        return asyncio.run(nac_mgmt.run_mgmt_op(_GatePool(), 3, "box-9", op, args,
                                                token_id=21, origin=origin))
    r1 = _run("stepup_request", [_pull, _details])
    check("run_mgmt_op: the Host's model_pull ask reaches the machine",
          r1["exit"] == 0 and _relay.sent[-1][0] == "stepup_request", r1)
    r2 = _run("stepup_status", ["req-host-7"])
    check("... and its own answer is read from the machine",
          r2["exit"] == 0 and _relay.sent[-1] == ("stepup_status", ["req-host-7"]), r2)
    n = len(_relay.sent)
    r3 = _run("stepup_status", ["req-platform-1"])
    check("run_mgmt_op: an ask the Host did not file is ANSWERED (not refused) and never sent",
          r3["exit"] == 0 and json.loads(r3["stdout"])["state"] == "not_filed_here"
          and len(_relay.sent) == n and ("stepup_status", 0, "not-filed-here") in _audits, r3)
    r4 = _run("stepup_request", ["mgmt_op|contain|box-9|x", _details], origin="route")
    check("run_mgmt_op: the route is held to the same rule, refused before anything is sent",
          r4["exit"] == 126 and "model_pull" in r4["stderr"] and len(_relay.sent) == n, r4)
    r5 = _run("stepup_status", ["req-platform-1"], origin="service")
    check("run_mgmt_op: the pod's own callers are not guarded",
          r5["exit"] == 0 and _relay.sent[-1] == ("stepup_status", ["req-platform-1"]), r5)
finally:
    nac_mgmt._gate_state, nac_mgmt._decide, nac_mgmt.audit_mgmt, nac_mgmt.get_mgmt_relay = _saved

# 8
_, r, err = draft(server="build-server", purposes=["observe"], ttl_days=3650)
check("the result says 30 days, never longer, and echoes no ttl",
      r and r["lasts"].startswith("30 days") and "never longer" in r["lasts"] and "ttl_days" not in r, r)
for ttl in (31, 3650, -1):
    try:
        asyncio.run(nac_commit.approve_mgmt_grant(object(), 3, 1, stepup_code="000000", ttl_days=ttl))
        ok = False
    except nac_commit.CommitError as e:
        ok = e.status == 400 and "1 to 30 days" in e.detail
    except Exception:  # reached the database: the bound did not hold
        ok = False
    check(f"approval refuses a {ttl}-day grant before touching the database", ok)


class _Reached(Exception):
    pass


class _DbPool:
    def acquire(self):
        raise _Reached()


for ttl in (None, 0, 1, 30):
    try:
        asyncio.run(nac_commit.approve_mgmt_grant(_DbPool(), 3, 1, stepup_code="000000", ttl_days=ttl))
        ok = False
    except _Reached:
        ok = True
    except Exception:
        ok = False
    check(f"approval with ttl_days={ttl!r} is inside the bound and goes on to the grant "
          f"(the Network app sends none)", ok)

# 8b: the harness gate itself, for grants already approved (an 'image:*' one).
from server.network import nac_harness  # noqa: E402


class _HPool:
    async def fetch(self, *a, **k):
        return []

    async def fetchval(self, *a, **k):
        return None

    async def fetchrow(self, *a, **k):
        return None

    async def execute(self, *a, **k):
        return "OK"


def _dispatch(image, mode):
    try:
        return asyncio.run(nac_harness.run_harness_op(
            _HPool(), 9003, "build-server", "h1", image=image, mode=mode, repo="o/r")), None
    except nac_mgmt.MgmtError as e:
        return None, e


for mode in ("echo", "git", "worktree", "container"):
    res, e = _dispatch(HARNESS_IMAGE[len("image:"):], mode)
    check(f"the gate refuses a coding harness image in mode={mode}",
          e is not None and e.status == 403 and "coding harness" in e.detail, (res, e))
    res, e = _dispatch(HARNESS_IMAGE_NEW[len("image:"):], mode)
    check(f"... and the same image pinned from a mirror in mode={mode}",
          e is not None and e.status == 403 and "coding harness" in e.detail, (res, e))
res, e = _dispatch(HARNESS_IMAGE[len("image:"):], "lab")
check("... but not in lab mode (refused there for another reason: the rate)",
      e is None or "coding harness" not in e.detail, e and e.detail)
res, e = _dispatch(USER_IMAGE[len("image:"):], "git")
check("a person's own image passes that rule and meets the grant check",
      e is None and res.get("exit") == 126, (res, e and e.detail))

# 9
seen = []


class _H(logging.Handler):
    def emit(self, rec):
        seen.append(rec.getMessage())


logging.getLogger("cseries.nac_propose").addHandler(_H())
pool, r, err = draft(pool=Pool({}, broken=True), server="anything", purposes=["observe"])
check("an unreadable roster checks nothing", r is not None and len(pool.inserts) == 1, err)
check("... and logs that it did not", any("roster unreadable" in m for m in seen), seen)

sys.exit(fail)
