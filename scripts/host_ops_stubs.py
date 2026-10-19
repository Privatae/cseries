"""host_ops_stubs — the container-free stand-ins the host-ops gates share
(test_host_ops_rung6.py, test_host_ops_rung7.py): host_op_runs and
entity_tickets in memory, the heartbeat / device lookups the approval
machinery makes, the module table, the device relay and the management
relay the box answers on. The real nac_ops / nac_approval / tickets code
runs against them.

The SQL matching is by fragment, on purpose: a query the code changes
shape on falls through to None / [] and the gate that needed it fails,
which is the signal to read the new query and teach the stub."""
from __future__ import annotations

import base64
import datetime as dt
import json


def terms_of(b64: str) -> dict:
    return json.loads(base64.b64decode(b64))


class Pool:
    def __init__(self, enforcing=None, devices=None):
        self.enforcing = enforcing
        self.devices = devices or {}
        self.runs: list[dict] = []
        self.tickets: dict[int, dict] = {}
        self.audits: list = []
        # bastion_update_refusals (v190): (token_id, version) the person refused
        self.refusals: set = set()

    @staticmethod
    def _rep(r):
        rep = r.get("report")
        return json.loads(rep) if isinstance(rep, str) else (rep or {})

    @staticmethod
    def _margin():
        from server.network import nac_ops
        return dt.timedelta(seconds=nac_ops.lapse_margin_s())

    def _live(self, r):
        # A dry run is live with the apply's budget to spare; a row whose
        # apply already left ('sent') is read until its terms lapse; a grant
        # until its day.
        now = dt.datetime.now(dt.timezone.utc)
        if r["kind"] == "op_grant" or r["state"] == "sent":
            return r["expires_dt"] > now
        return r["expires_dt"] > now + self._margin()

    def _run_by_id(self, rid):
        return next((r for r in self.runs if r["id"] == rid), None)

    async def fetch(self, sql, *a):
        if "FROM project_modules WHERE target = 'op'" in sql:
            return [{"project_id": 1, "name": "ops-mod"}]
        if "FROM host_op_runs" in sql and "ORDER BY created_at DESC LIMIT 20" in sql:
            eid, server, sha = a[0], a[1], a[2]
            rows = [r for r in self.runs if r["entity_id"] == eid and r["server"] == server and r["sha256"] == sha
                    and r["kind"] == "op_run" and r["state"] in ("dry_run", "awaiting") and self._live(r)
                    and r.get("changes_sha256") and not self._rep(r).get("under_grant")]
            return sorted(rows, key=lambda r: -r["id"])
        return []

    async def fetchval(self, sql, *a):
        if "FROM bastion_update_refusals" in sql:
            return 1 if (int(a[0]), str(a[1])) in self.refusals else None
        if "SELECT enforcing FROM network_bastion_heartbeat" in sql:
            return json.dumps(self.enforcing) if self.enforcing is not None else None
        if "SELECT enforcing->>'stepup'" in sql:
            return str(bool((self.enforcing or {}).get("stepup"))).lower()
        if "SELECT token_id FROM network_bastion_heartbeat WHERE hostname" in sql:
            return 77
        if "count(DISTINCT h.token_id)" in sql or "SELECT 1 FROM network_bastion_heartbeat WHERE token_id" in sql:
            return 1
        if "count(*) FROM host_op_runs" in sql:
            eid, server, nonce = a
            return sum(1 for r in self.runs if r["entity_id"] == eid and r["server"] == server and r["kind"] == "op_run"
                       and self._rep(r).get("under_grant") == nonce and self._rep(r).get("mode") == "apply"
                       and (r["state"] in ("applied", "sent") or (r["state"] == "refused" and self._rep(r).get("spent") is True)))
        if "INSERT INTO host_op_runs" in sql:
            cols = ["entity_id", "server", "token_id", "kind", "op", "version", "sha256", "privilege",
                    "params", "bounds", "uses", "nonce", "expires", "changes_sha256", "report", "state"]
            row = dict(zip(cols, a))
            row["id"] = len(self.runs) + 1
            # The real driver (asyncpg) takes a datetime for timestamptz and
            # refuses a string — the fake insists on the same.
            if not isinstance(row["expires"], dt.datetime) or row["expires"].tzinfo is None:
                raise TypeError("expires must be a tz-aware datetime for a timestamptz column")
            row["expires_dt"] = row["expires"]
            row["created_at"] = dt.datetime.now(dt.timezone.utc)
            self.runs.append(row)
            return row["id"]
        if "SELECT metadata FROM entity_tickets WHERE id=$1" in sql:
            t = self.tickets.get(a[0])
            return json.dumps(t["metadata"]) if t else None
        if "SELECT status FROM entity_tickets" in sql:
            t = self.tickets.get(a[0])
            return t["status"] if t else None
        if "SELECT expires_at FROM entity_tickets WHERE id=$1 AND entity_id=$2" in sql:
            t = self.tickets.get(a[0])
            return t["expires_at"] if t and t["entity_id"] == a[1] else None
        return None

    async def fetchrow(self, sql, *a):
        if "approval_device_key" in sql:
            for k, (tid, host) in self.devices.items():
                if k in a[1]:
                    return {"token_id": tid, "hostname": host, "last_seen_at": None}
            return None
        if "INSERT INTO entity_tickets" in sql:
            eid, source, source_ref, circle_id, requester, sitrep, options, context, priority, expires_at, metadata = a
            tid = len(self.tickets) + 1
            self.tickets[tid] = {"id": tid, "entity_id": eid, "status": "open", "sitrep": sitrep,
                                 "options": json.loads(options), "context": json.loads(context),
                                 "metadata": json.loads(metadata), "decision": {}, "circle_id": circle_id,
                                 "source": source, "expires_at": expires_at, "decided_at": None}
            return {"id": tid, "ticket_uuid": f"u-{tid}", "opened_at": dt.datetime.now(dt.timezone.utc)}
        if "SELECT id, circle_id, status, options, metadata" in sql:
            t = self.tickets.get(a[0])
            return dict(t) if t and t["entity_id"] == a[1] else None
        if "SELECT metadata FROM entity_tickets WHERE id = $1 AND entity_id = $2 AND status = 'open'" in sql:
            t = self.tickets.get(a[0])
            return {"metadata": dict(t["metadata"])} if t and t["entity_id"] == a[1] and t["status"] == "open" else None
        if "SELECT id, status, decision, metadata, sitrep, options, decided_at, expires_at FROM entity_tickets" in sql:
            # a built-in write's card, by the act it binds and the box (rung 8)
            eid, kind, purpose, tok = a
            rows = [t for t in self.tickets.values() if t["entity_id"] == eid and t["metadata"].get("kind") == kind
                    and (t["metadata"].get("op_ask") or {}).get("purpose") == purpose
                    and (tok is None or str((t["metadata"].get("op_ask") or {}).get("token_id")) == tok)]
            rows.sort(key=lambda t: -t["id"])
            return dict(rows[0]) if rows else None
        if "SELECT id, status, decision, metadata, sitrep, options FROM entity_tickets" in sql:
            eid = a[0]
            rows = [t for t in self.tickets.values() if t["entity_id"] == eid and t["metadata"].get("kind") == a[1]]
            if "run_id" in sql:
                rows = [t for t in rows if int(t["metadata"]["op_ask"]["run_id"]) == a[2]]
            else:
                rows = [t for t in rows if t["status"] == "open" and t["metadata"]["op_ask"]["server"] == a[2]
                        and t["metadata"]["op_ask"]["sha256"] == a[3]]
            rows.sort(key=lambda t: -t["id"])
            return dict(rows[0]) if rows else None
        if "FROM host_op_runs WHERE id=$1 AND entity_id=$2" in sql:
            r = self._run_by_id(a[0])
            return dict(r) if r and r["entity_id"] == a[1] else None
        if "state='sent'" in sql and "under_grant" in sql:
            eid, server, nonce, params = a
            rows = [r for r in self.runs if r["entity_id"] == eid and r["server"] == server and r["kind"] == "op_run"
                    and self._rep(r).get("under_grant") == nonce and self._rep(r).get("mode") == "apply"
                    and r["params"] == params and r["state"] == "sent"
                    and not (r.get("result") or {}).get("superseded_by")]
            rows.sort(key=lambda r: -r["id"])
            return dict(rows[0]) if rows else None
        if "FROM host_op_runs" in sql:
            eid, server, sha = a[0], a[1], a[2]
            rows = [r for r in self.runs if r["entity_id"] == eid and r["server"] == server and r["sha256"] == sha and self._live(r)]
            if "kind='op_run'" in sql:
                rows = [r for r in rows if r["kind"] == "op_run" and r["params"] == a[3]
                        and r["state"] in ("dry_run", "awaiting", "sent") and r.get("changes_sha256")
                        and not self._rep(r).get("under_grant")]
            elif "state='installed'" in sql:
                rows = [r for r in rows if r["kind"] == "op_grant" and r["state"] == "installed"]
            rows.sort(key=lambda r: -r["id"])
            return dict(rows[0]) if rows else None
        return None

    async def execute(self, sql, *a):
        if "INSERT INTO bastion_update_refusals" in sql:
            self.refusals.add((int(a[0]), str(a[1])))
            return "INSERT 0 1"
        if "UPDATE host_op_runs" in sql:
            rid, state, approval, result, report, changes = a
            r = self._run_by_id(rid)
            if r is not None:
                r["state"] = state
                if approval:
                    r["approval"] = approval
                if result:
                    r["result"] = json.loads(result)
                if report:
                    r["report"] = report
                if changes:
                    r["changes_sha256"] = changes
            return "UPDATE 1"
        if "SET status = 'decided'" in sql:
            decision, eid, tid = a[0], a[1], a[2]
            t = self.tickets.get(tid)
            if not t or t["status"] != "open":
                return "UPDATE 0"
            t["status"], t["decision"] = "decided", json.loads(decision)
            t["decided_at"] = dt.datetime.now(dt.timezone.utc)
            return "UPDATE 1"
        if "SET status = 'dismissed'" in sql:
            note, eid, tid, _eid2 = a
            t = self.tickets.get(tid)
            if not t or t["status"] != "open":
                return "UPDATE 0"
            t["status"], t["decision"] = "dismissed", {"note": note, "dismissed": True}
            t["decided_at"] = dt.datetime.now(dt.timezone.utc)
            return "UPDATE 1"
        if "SET status='expired'" in sql and "WHERE id=$1 AND entity_id=$2 AND status='open'" in sql:
            tid, eid, note = a
            t = self.tickets.get(tid)
            if not t or t["entity_id"] != eid or t["status"] != "open":
                return "UPDATE 0"
            t["status"], t["decision"] = "expired", {"expired": True, "note": note}
            t["decided_at"] = dt.datetime.now(dt.timezone.utc)
            return "UPDATE 1"
        if "SET status='expired'" in sql and "->>'shape' = 'mgmt'" in sql:
            eid, kind, op, server, purpose, note, tok = a
            n = 0
            for t in self.tickets.values():
                ask = t["metadata"].get("op_ask") or {}
                if t["entity_id"] == eid and t["status"] == "open" and t["metadata"].get("kind") == kind \
                        and ask.get("shape") == "mgmt" and ask.get("op") == op \
                        and (server is None or ask.get("server") == server) \
                        and ask.get("purpose") != purpose and (tok is None or str(ask.get("token_id")) == tok):
                    t["status"], t["decision"] = "expired", {"expired": True, "note": note}
                    t["decided_at"] = dt.datetime.now(dt.timezone.utc)
                    n += 1
            return f"UPDATE {n}"
        if "SET status='expired'" in sql:
            eid, kind, server, sha, op, replaced, lapsed, margin = a
            n = 0
            for t in self.tickets.values():
                ask = t["metadata"].get("op_ask") or {}
                if t["entity_id"] == eid and t["status"] == "open" and t["metadata"].get("kind") == kind \
                        and ask.get("server") == server and ask.get("op") == op:
                    stale_sha = ask.get("sha256") != sha
                    lapsed_run = dt.datetime.strptime(ask["expires"], "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=dt.timezone.utc) <= dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=margin)
                    if stale_sha or lapsed_run:
                        t["status"] = "expired"
                        t["decision"] = {"expired": True, "note": replaced if stale_sha else lapsed}
                        n += 1
            return f"UPDATE {n}"
        if "jsonb_build_object('grant_run_id'" in sql:
            self.tickets[a[1]]["metadata"].setdefault("op_ask_result", {})["grant_run_id"] = a[0]
            return "UPDATE 1"
        if "COALESCE(metadata->'op_ask_result', '{}'::jsonb) || $1::jsonb" in sql:
            self.tickets[a[1]]["metadata"].setdefault("op_ask_result", {}).update(json.loads(a[0]))
            return "UPDATE 1"
        if "jsonb_build_object('op_ask_result'" in sql:
            self.tickets[a[1]]["metadata"]["op_ask_result"] = json.loads(a[0])
            return "UPDATE 1"
        if "jsonb_build_object('op_ask'" in sql:
            self.tickets[a[1]]["metadata"]["op_ask"] = json.loads(a[0])
            return "UPDATE 1"
        return "UPDATE 1"


class Relay:
    """nac_dcn._dispatch_box stand-in (the person's approval device)."""

    def __init__(self):
        self.calls: list = []
        self.device_state = "pending"
        self.console_state = "pending"
        self.console_status_state = None
        self.offline = False

    async def __call__(self, pool, eid, server, op, args, token_id=None, timeout=None):
        self.calls.append((server, op, list(args), token_id))
        if self.offline:
            return {"_offline": True}
        if op == "device_request":
            if self.device_state == "approved":
                return {"exit": 0, "stdout": json.dumps({"id": "req-1", "state": "approved", "approval": '{"v":1,"sig":"x"}'}), "stderr": ""}
            if self.device_state == "denied":
                # the device's own sentence (device.go deviceNewRequest)
                return {"exit": 126, "stdout": "", "stderr": "you refused this on your device; it cannot be asked again for 24h"}
            if self.device_state == "error":
                # a device-side failure exits 126 like a refusal (device.go handleDeviceRequest):
                # only the words tell the two apart
                return {"exit": 126, "stdout": "", "stderr": "device: could not save the request"}
            return {"exit": 0, "stdout": json.dumps({"id": "req-1", "state": "pending"}), "stderr": ""}
        if op == "device_status":
            return {"exit": 0, "stdout": json.dumps({"state": self.device_state, "approval": '{"v":1,"sig":"x"}'}), "stderr": ""}
        # the machine's own console (a factor, no device): stepup.go
        if op == "stepup_request":
            if self.console_state == "denied":
                return {"exit": 126, "stdout": "", "stderr": "you refused this on this machine; it cannot be asked again for 24h"}
            if self.console_state == "error":
                return {"exit": 126, "stdout": "", "stderr": "stepup: could not save the request"}
            if self.console_state == "approved":
                return {"exit": 0, "stdout": json.dumps({"state": "approved", "request": "con-1", "approval": "con-appr"}), "stderr": ""}
            return {"exit": 0, "stdout": json.dumps({"state": "pending", "request": "con-1"}), "stderr": ""}
        if op == "stepup_status":
            # the poll on a filed request (stepup.go handleStepupStatus): pending / approved /
            # denied / expired — the approval only with "approved"; `console_status_state`
            # lets a request file as pending and then be refused at the console while polled
            st = self.console_status_state or self.console_state
            body = {"state": st, "approval": "con-appr"} if st == "approved" else {"state": st}
            return {"exit": 0, "stdout": json.dumps(body), "stderr": ""}
        return {"exit": 0, "stdout": "", "stderr": ""}

    def filed(self) -> list:
        """The details of every device_request, decoded, in order."""
        return [json.loads(base64.b64decode(c[2][0])) for c in self.calls if c[1] == "device_request"]


class MRelay:
    """The management relay the box answers on."""

    def __init__(self):
        self.calls: list = []
        self.refuse: dict = {}   # op -> stderr words (exit 126)
        self.changes_sha = "c" * 64
        self.status: dict = {"found": False, "nonce_burned": False}

    def worker_count(self, token_id):
        return 1

    async def dispatch_detailed(self, token_id, op, args, timeout, approval=""):
        self.calls.append((token_id, op, list(args), timeout))
        if op in self.refuse:
            return (b"", self.refuse[op].encode(), 126), ""
        if op == "op_run_status":
            return (json.dumps({"nonce": args[0], **self.status}).encode(), b"", 0), ""
        if op == "op_sync":
            t = terms_of(args[0])
            out = {"state": "held", "op": t["op"], "sha256": t["sha256"], "kind": t["kind"]}
            if t["kind"] == "op_grant":
                out["grant"] = t["nonce"]
            return (json.dumps(out).encode(), b"", 0), ""
        rep = {"mode": "dry_run" if op == "op_dry_run" else "apply", "exit": 0, "seconds": 0.01, "output": "hi\n",
               "changes": [{"path": "/srv/hostop/hello.txt", "kind": "created", "size": 6, "mode": "0644",
                            "sha256": "d" * 64, "diff": "+ hello\n"}],
               "changes_sha256": self.changes_sha}
        if op == "op_apply" or (op == "op_grant_run" and args[1] == "apply"):
            rep["committed"] = ["created /srv/hostop/hello.txt"]
        return (json.dumps(rep).encode(), b"", 0), ""

    def ops(self) -> list:
        return [c[1] for c in self.calls]
