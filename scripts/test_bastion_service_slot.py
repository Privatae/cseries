#!/usr/bin/env python3
"""Gate: the bastion SERVICE SLOT (bastion 0.34.0 client-go/service.go) is wired so that a
server the host builds on the user's machine is governed the way the box expects.

What this pins, all pure Python (no server, no DB — the dispatch is driven with fakes):

  1. The grant: a service verb needs harness:run AND 'mode:service' in the SAME grant;
     'image:*' does not imply it; `up` also needs the image token; `down`/`status` act on
     a slot and need no image token; a deny of mode:service refuses. Every other mode is
     gated exactly as before.
  2. The platform's CODING HARNESS images never run as a server on a user's machine.
  3. The approval binds what the box binds: service_approval_args in serviceArgs order,
     pinned by the same vector the Go test pins (TestServiceArgsMatchThePodVector).
  4. The dispatch: a service job never carries the agent's credential; `up` and `down`
     ask for the person's approval under op 'service:<verb>' with those args; `status`
     asks for none; a successful `up` returns the endpoint this pod dials.
  5. `services` is a pairing-grant family on this side too.

Run: python3 scripts/test_bastion_service_slot.py
"""
import asyncio
import os
import sys
from tools_source import tools_source  # tools.py and the modules split out of it

ROOT = os.environ.get("APP_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "brain"))

failures = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


from server.network import nac_mgmt, nac_harness, nac_approval, pairing_grants  # noqa: E402

IMG = "gitea/gitea:1.26.2-rootless"
RUN = nac_mgmt.HARNESS_RUN_OP
SVC = nac_mgmt.SERVICE_MODE_TOKEN
allowed = nac_mgmt.harness_image_allowed

# ── 1. the grant ──────────────────────────────────────────────────────────────
job_grant = [{"server": "box", "ops": [RUN, "image:" + IMG]}]
star_grant = [{"server": "box", "ops": [RUN, "image:*"]}]
svc_grant = [{"server": "box", "ops": [RUN, "image:" + IMG, SVC]}]
svc_no_image = [{"server": "box", "ops": [RUN, SVC]}]

check("a job grant does not cover a server", allowed(job_grant, 1, "box", IMG, mode="service") is False)
check("image:* does not imply mode:service", allowed(star_grant, 1, "box", IMG, mode="service") is False)
check("a service grant covers up with its image", allowed(svc_grant, 1, "box", IMG, mode="service") is True)
check("service up with an image the grant does not name is refused",
      allowed(svc_grant, 1, "box", "other/image:1", mode="service") is False)
check("down/status (no image) need only run + mode:service",
      allowed(svc_no_image, 1, "box", None, mode="service") is True)
check("down/status still need mode:service",
      allowed([{"server": "box", "ops": [RUN]}], 1, "box", None, mode="service") is False)
check("the tokens must come from ONE grant",
      allowed([{"server": "box", "ops": [RUN, "image:" + IMG]}, {"server": "box", "ops": [SVC]}],
              1, "box", IMG, mode="service") is False)
check("a deny of mode:service refuses",
      allowed(svc_grant + [{"server": "box", "deny": [SVC]}], 1, "box", IMG, mode="service") is False)
check("ordinary modes are unchanged: a job grant still covers a git run",
      allowed(job_grant, 1, "box", IMG, mode="git") is True)
check("ordinary modes still need an image token when none is named",
      allowed([{"server": "box", "ops": [RUN]}], 1, "box", None, mode="git") is False)
check("_mode_token: service / lab / none",
      nac_mgmt._mode_token("service") == SVC and nac_mgmt._mode_token("lab") == nac_mgmt.LAB_MODE_TOKEN
      and nac_mgmt._mode_token("git") is None)

# ── 2. coding harness images ──────────────────────────────────────────────────
for img in ("registry.example/cseries-harness-codex:v0.2.9", "cseries-harness-kimi",
            "mirror.example/team/cseries-harness-claude-code@sha256:" + "a" * 64,
            "registry.example/cseries-vscode:v0.2.0"):
    check(f"coding harness image refused: {img}", nac_harness.is_coding_harness_image(img))
for img in (IMG, "codeberg.org/forgejo/forgejo:9", "registry.example/cseries-browser:v1",
            "someone/cseries-harnessed:1"):
    check(f"other image allowed: {img}", not nac_harness.is_coding_harness_image(img))

# ── 3. the approval vector (pinned on the box too) ───────────────────────────
spec = {"harness_id": "s1", "service_op": "up", "service_name": "forge", "image": IMG, "socket": "gitea.sock",
        "data_path": "/var/lib/gitea", "data_uid": 1000,
        "envs": {"GITEA__server__DISABLE_SSH": "true", "B": "2"}, "purge": False}
purpose = nac_approval.mgmt_op_purpose("service:up", "box1", nac_harness.service_approval_args(spec))
check("service purpose matches the box's pinned vector",
      purpose == "mgmt_op|service:up|box1|5cdcf7d1a0ff93ec85ed757e96c56aa80cf45f05018a56d52b07b6915d2318a3",
      purpose)
check("purge is bound: a stop is not a purge",
      nac_harness.service_approval_args({**spec, "purge": True})[-1] == "true"
      and nac_harness.service_approval_args(spec)[-1] == "false")
check("harness_approval_args is unchanged for the other modes (16 fields)",
      len(nac_harness.harness_approval_args({"mode": "git"})) == 16)

# ── 4. the dispatch, driven with fakes ────────────────────────────────────────


class FakePool:
    async def fetchval(self, q, *a):
        return 7 if "token_id" in q else None

    async def fetchrow(self, q, *a):
        return {"reported_ip": "2001:db8::a:1"}

    async def fetch(self, q, *a):
        # The approved reach grant for a mirror sync: two sources.
        if "service_reach_grants" in q:
            import json
            return [{"signed_terms": json.dumps({"mirrors": [
                {"url": "https://github.com/a/b.git", "repo": "m/b"},
                {"url": "https://github.com/a/c.git", "repo": "m/c"}]})}]
        return []


class FakeRelay:
    def __init__(self):
        self.sent = []
        self.timeouts = []

    def worker_count(self, token_id):
        return 1

    async def dispatch(self, token_id, spec, timeout=0):
        import json
        self.sent.append(spec)
        self.timeouts.append(timeout)
        if spec.get("service_op") == "up":
            return json.dumps({"ok": True, "tunnel_port": 18100}).encode(), b"", 0
        return json.dumps({"ok": True, "services": []}).encode(), b"", 0


relay = FakeRelay()
asked = []


async def _grants(*a, **k):
    return svc_grant + [{"server": "box", "ops": [RUN, SVC]}]


async def _empty(*a, **k):
    return set()


async def _none(*a, **k):
    return None


async def _creds(*a, **k):
    return "SECRET-AGENT-CREDENTIAL"


async def _obtain(pool, eid, server, op, args, **k):
    asked.append((op, list(args)))
    return {"state": "approved", "approval": "APPROVAL"}


from server.network import nac_stepup  # noqa: E402

nac_stepup.entity_verify_pubkey = _none
nac_mgmt.load_mgmt_grants = _grants
nac_mgmt.load_suspended_servers = _empty
nac_mgmt.load_quarantined = _empty
nac_mgmt.audit_mgmt = _none
nac_harness.resolve_agent_creds = _creds
nac_harness.get_harness_relay = lambda: relay
nac_approval.obtain = _obtain


async def drive():
    pool = FakePool()
    up = await nac_harness.run_harness_op(
        pool, 1, "box", "s1", image=IMG, mode="service", socket="gitea.sock", service_op="up",
        service_name="forge", data_path="/var/lib/gitea", data_uid=1000,
        envs={"GITEA__server__DISABLE_SSH": "true"})
    status = await nac_harness.run_harness_op(pool, 1, "box", "s2", mode="service", service_op="status")
    down = await nac_harness.run_harness_op(pool, 1, "box", "s3", mode="service", service_op="down",
                                            service_name="forge", purge=True)
    refused = None
    try:
        await nac_harness.run_harness_op(pool, 1, "box", "s4", mode="service", service_op="up",
                                         service_name="x", image="registry.example/cseries-harness-codex:v1",
                                         socket="s.sock", data_path="/d", data_uid=1000)
    except nac_mgmt.MgmtError as e:
        refused = e
    return up, status, down, refused


up, status, down, refused = asyncio.run(drive())
check("up ran", up.get("exit") == 0, str(up))
check("no service job carries the agent's credential",
      all("agent_creds" not in s for s in relay.sent) and len(relay.sent) == 3, str(relay.sent))
check("the up spec carries the slot fields, the socket included",
      relay.sent[0].get("service_name") == "forge" and relay.sent[0].get("data_uid") == 1000
      and relay.sent[0].get("socket") == "gitea.sock"
      and relay.sent[0].get("approval") == "APPROVAL", str(relay.sent[0]))
check("up and down asked approval as service:<verb>; status asked none",
      [op for op, _ in asked] == ["service:up", "service:down"], str(asked))
check("the down approval binds purge=true", asked and asked[-1][1][-1] == "true", str(asked))
check("a successful up returns the endpoint this pod dials",
      (up.get("result") or {}).get("endpoint") == "http://[2001:db8::a:1]:18100", str(up.get("result")))
check("a coding harness image is refused before dispatch",
      refused is not None and refused.status == 403 and len(relay.sent) == 3, str(refused))

# ── 4b. review fixes: the box's refusals are checked before the person is asked ──
asked.clear()
sent_before = len(relay.sent)


class NoServicesPool(FakePool):
    async def fetchval(self, q, *a):
        if "capabilities" in q:
            return ["embed"]  # the box advertised no `services` to this Host
        return await super().fetchval(q, *a)


async def drive_refusals():
    out = {}
    try:
        await nac_harness.run_harness_op(FakePool(), 1, "box", "r1", image=IMG, mode="service",
                                         socket="gitea.sock", service_op="up", service_name="forge",
                                         data_path="relative", data_uid=1000)
    except nac_mgmt.MgmtError as e:
        out["bad_path"] = e
    out["no_cap"] = await nac_harness.run_harness_op(
        NoServicesPool(), 1, "box", "r2", image=IMG, mode="service", socket="gitea.sock", service_op="up",
        service_name="forge", data_path="/var/lib/gitea", data_uid=1000)
    return out


r = asyncio.run(drive_refusals())
check("an invalid spec is refused before the person is asked",
      isinstance(r.get("bad_path"), nac_mgmt.MgmtError) and r["bad_path"].status == 400 and not asked,
      str(r.get("bad_path")))
check("a box not offering `services` is refused before the person is asked",
      r["no_cap"].get("exit") == 126 and "services" in r["no_cap"].get("log", "") and not asked,
      str(r["no_cap"]))
check("nothing was dispatched for either refusal", len(relay.sent) == sent_before)
check("spec refusal mirrors the box: names, images, ports, env",
      nac_harness.service_spec_refusal("up", "../x", IMG, "s.sock", "/d", 0, {}) is not None
      and nac_harness.service_spec_refusal("up", "f", "docker-archive:/x", "s.sock", "/d", 0, {}) is not None
      and nac_harness.service_spec_refusal("up", "f", "docker:27", "s.sock", "/d", 1000, {}) is None
      and nac_harness.service_spec_refusal("up", "f", IMG, "", "/d", 0, {}) is not None
      and nac_harness.service_spec_refusal("up", "f", IMG, "s.sock", "/d", 0, {"-e": "x"}) is not None
      and nac_harness.service_spec_refusal("up", "f", IMG, "s.sock", "/d", 0, {"A": "x\ny"}) is not None
      and nac_harness.service_spec_refusal("down", "forge", None, "", "", 0, {}) is None
      and nac_harness.service_spec_refusal("status", "", None, "", "", 0, {}) is None)
check("the socket is one file name in the data dir, as the box requires",
      nac_harness.service_spec_refusal("up", "f", IMG, "../x.sock", "/d", 0, {}) is not None
      and nac_harness.service_spec_refusal("up", "f", IMG, "run/g.sock", "/d", 0, {}) is not None
      and nac_harness.service_spec_refusal("up", "f", IMG, "..", "/d", 0, {}) is not None
      and nac_harness.service_spec_refusal("up", "f", IMG, "gitea.sock", "/d", 1000, {}) is None
      and nac_harness.service_spec_refusal("up", "f", IMG, "gitea.sock", "/d", 0, {}) is not None)
check("the mirror is as strict as the box on trailing newlines and a leading //",
      nac_harness.service_spec_refusal("up", "f", "nginx:1\n", "s.sock", "/d", 0, {}) is not None
      and nac_harness.service_spec_refusal("up", "f", IMG, "s.sock", "/d", 0, {"A\n": "x"}) is not None
      and nac_harness.service_spec_refusal("up", "f", IMG, "s.sock", "//d", 0, {}) is not None
      and nac_harness.service_spec_refusal("up", "forge\n", IMG, "s.sock", "/d", 0, {}) is not None)


class ServicesPool(FakePool):
    def __init__(self, caps):
        self.caps = caps

    async def fetchval(self, q, *a):
        if "capabilities" in q:
            return self.caps
        return await super().fetchval(q, *a)


async def drive_advertised():
    import json as _json
    n0 = len(asked)
    as_list = await nac_harness.run_harness_op(
        ServicesPool(["embed", "services"]), 1, "box", "a1", image="  " + IMG + " ", mode="service",
        socket="gitea.sock", service_op="up", service_name="forge", data_path="/var/lib/gitea", data_uid=1000)
    as_json = await nac_harness.run_harness_op(
        ServicesPool(_json.dumps(["services"])), 1, "box", "a2", image=IMG, mode="service",
        socket="gitea.sock", service_op="up", service_name="forge", data_path="/var/lib/gitea", data_uid=1000)
    return as_list, as_json, len(asked) - n0


adv_list, adv_json, n_asked = asyncio.run(drive_advertised())
check("a box advertising `services` (list or JSON text) proceeds to the approval and runs",
      adv_list.get("exit") == 0 and adv_json.get("exit") == 0 and n_asked == 2,
      f"{adv_list} {adv_json} asked={n_asked}")
check("the image is sent and bound exactly as checked (stripped)",
      relay.sent[-2].get("image") == IMG and asked[-2][1][3] == IMG, str(relay.sent[-2].get("image")))
check("the up wait covers the box's worst case (a 10-minute pull + remove + run, queued once)",
      nac_harness.SERVICE_WAIT_S["up"] >= 2 * (600 + 90))
check("a successful up says how to dial the endpoint (the Hub peer forward)",
      (up.get("result") or {}).get("via_peer_forward"), str(up.get("result")))

import ast as _ast  # noqa: E402
_tools = tools_source(ROOT)
check('purge is strictly the boolean true in the tool (never bool("false"))',
      'purge=a.get("purge") is True' in _tools and 'purge=bool(a.get("purge"))' not in _tools)
_route = open(os.path.join(ROOT, "server/routes/network.py")).read()
check("POST /harness/run passes the service fields", 'service_op=body.get("service_op")' in _route
      and 'purge=body.get("purge") is True' in _route)
_app = open(os.path.join(ROOT, "system-apps/network/app.js")).read()
check("the admit card leaves `services` unchecked (opt in)",
      "OPT_IN_CAPS = ['services']" in _app and "OPT_IN_CAPS.includes(c) ? '' : 'checked'" in _app)
_denied_msg_src = open(os.path.join(ROOT, "server/network/nac_harness.py")).read()
check("a refused down/status names no image token it never needed",
      'if nac_mgmt._needs_image(image, mode) else ""' in _denied_msg_src)

# ── 4c. mirror (bastion 0.38.0): standing work, no approval, the token rides the job ──
asked.clear()
n_sent = len(relay.sent)
from server.network import service_reach  # noqa: E402
from server.vault import api as _vault_api  # noqa: E402


opened = []


async def _open_secret(pool, *, entity_id, name, actor=None, include_deactivated=False):
    opened.append(name)
    return b"TOK-123\n" if name == "service-push:box:forge" else None

_vault_api.open_secret = _open_secret


async def drive_mirror():
    out = {}
    out["ok"] = await nac_harness.run_harness_op(FakePool(), 1, "box", "m1", mode="service",
                                                 service_op="mirror", service_name="forge")
    try:
        await nac_harness.run_harness_op(FakePool(), 1, "box", "m4", mode="service",
                                         service_op="mirror", service_name="wiki")
    except nac_mgmt.MgmtError as e:
        out["no_key"] = e
    try:
        await nac_harness.run_harness_op(FakePool(), 1, "box", "m3", image=IMG, mode="service",
                                         socket="gitea.sock", service_op="up", service_name="forge",
                                         data_path="/var/lib/gitea", data_uid=1000,
                                         mirrors=[{"url": "http://github.com/a/b", "repo": "m/b"}])
    except nac_mgmt.MgmtError as e:
        out["bad_mirror"] = e
    return out

mo = asyncio.run(drive_mirror())
sent_mirror = relay.sent[n_sent] if len(relay.sent) > n_sent else {}
check("a mirror sync asks no approval (standing work under the approved list)", not asked)
check("the mirror job carries the token opened from Keys, and the verb",
      sent_mirror.get("service_op") == "mirror" and sent_mirror.get("push_token") == "TOK-123"
      and "agent_creds" not in sent_mirror)
check("a Keys name with no credential is refused before dispatch",
      isinstance(mo.get("no_key"), nac_mgmt.MgmtError) and len(relay.sent) == n_sent + 1)
check("the mirror wait is sized from the grant's list (2 sources here)",
      relay.timeouts[n_sent] == service_reach.mirror_wait_s(2) > 2 * 2 * 600)
from mastercode.runtime import tool_call_ledger as _tcl
_prev = _tcl._preview({"push_token": "TOK-123", "server": "box"}, "mesh_harness_run")
check("the tool ledger never stores a push token", "TOK-123" not in _prev and "box" in _prev)
check("the only credential opened is the one bound to that machine and server",
      opened == ["service-push:box:forge", "service-push:box:wiki"])
check("the tool and the route take no token or key name at all",
      "push_token" not in open(os.path.join(ROOT, "brain/mastercode/public_tool_registry.py")).read()
      and "push_token" not in open(os.path.join(ROOT, "server/routes/network.py")).read())
check("a bad mirror source on up is refused", isinstance(mo.get("bad_mirror"), nac_mgmt.MgmtError)
      and mo["bad_mirror"].status == 400)

# ── 5. the family ─────────────────────────────────────────────────────────────
check("services is a pairing-grant family", "services" in pairing_grants.CAPS)

print(f"\nbastion service slot: {'PASS' if not failures else 'FAIL'} ({len(failures)} failing)")
sys.exit(1 if failures else 0)
