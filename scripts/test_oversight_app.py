#!/usr/bin/env python3
"""The Oversight app and the route it adds.

Runs as pytest AND as a plain script in the container-free gate lane
(APP_ROOT=<repo> python3 scripts/test_oversight_app.py). No DB, no network: the
route is driven for real against a recording stub pool, with the heartbeat rows
fed through `topology.list_bastions`.

WHAT THIS FILE DEFENDS.

1. ONE DESCRIPTION OF A MACHINE. GET /api/oversight/machines reports each
   machine's alert and detection states by calling the host's OWN derivations
   (runtime/tools.py). The test compares the route's answer with those functions
   on the same row, so a route that re-derived a state — and could therefore
   disagree with what the host tells the user — fails by name.

2. NEVER AN ALL-CLEAR FROM A READ FAILURE. An unreadable ledger is a 503, never
   zero open findings; an unreadable machine list is a 503, never "no machines".
   list_bastions returns [] on a read failure (topology.py), so the route checks
   an empty answer against the token table, and both halves of that are driven.

3. THE RECORD'S THREE VALUES STAY THREE: {} whole, a map of holes, absent not
   reported (v163). "not reported" must never come out as "whole".

4. THE APP'S CONTRACT. It declares exactly the two oversight scopes, calls only
   routes that exist under the scope it declares, writes nothing a machine
   named through innerHTML, is image-baked with a current signed bundle, and has
   a dock glyph.

MUTATION RESULTS (each shown red before this file was finished, see the PR):
  derive    the route's alerts_state replaced by a literal "quiet"
  empty     the token-table probe removed (an unreadable list became "no machines")
  ledger    the open-count query's failure swallowed into {} counts
  record    _record_state(None) -> "whole"
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import zipfile

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

from fastapi import HTTPException                            # noqa: E402

from mastercode.runtime import tools                         # noqa: E402
from mastercode.runtime import oversight as ov               # noqa: E402
from server import brain_service                             # noqa: E402
from server.network import topology                          # noqa: E402
from server.routes import oversight as routes                # noqa: E402

APP_DIR = os.path.join(_ROOT, "system-apps", "oversight")


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


SEEN = "2026-09-23T00:00:30+00:00"
ALERTS_FIRING = {"firing": 18, "critical": 0, "warning": 2,
                 "since": "2026-08-31T00:45:00Z", "at": "2026-09-23T00:00:00Z",
                 "fingerprints": ["876fb88ff9331a86", "7920948936be8e54"]}


def bastion(**kw) -> dict:
    """An ONLINE bastion row in the shape topology._bastion_row produces."""
    b = {"id": 63, "name": "box-a", "hostname": "box-a",
         "online": True, "last_seen_at": SEEN, "bastion_version": "0.31.2",
         "platform": "linux", "alerts": None, "detect": None, "posture": None,
         "record_dropped": None}
    b.update(kw)
    return b


class StubPool:
    """Answers the route's three queries; any of them can be made to raise."""

    def __init__(self, open_rows=None, ingest=None, tokens=0,
                 ledger_raises=False, tokens_raise=False, ingest_raises=False):
        self.open_rows = open_rows or []
        self.ingest = ingest
        self.tokens = tokens
        self.ledger_raises = ledger_raises
        self.tokens_raise = tokens_raise
        self.ingest_raises = ingest_raises
        self.calls = []

    async def fetch(self, q, *a):
        self.calls.append(("fetch", q, a))
        if "FROM oversight_findings" in q:
            if self.ledger_raises:
                raise RuntimeError('relation "oversight_findings" does not exist')
            return self.open_rows
        raise AssertionError(f"unexpected fetch: {q[:80]}")

    async def fetchrow(self, q, *a):
        self.calls.append(("fetchrow", q, a))
        if "FROM calendar_events" in q:
            if self.ingest_raises:
                raise RuntimeError("calendar_event_runs is locked")
            return self.ingest
        raise AssertionError(f"unexpected fetchrow: {q[:80]}")

    async def fetchval(self, q, *a):
        self.calls.append(("fetchval", q, a))
        if "FROM mcp_client_tokens" in q:
            if self.tokens_raise:
                raise RuntimeError('relation "mcp_client_tokens" does not exist')
            return self.tokens
        raise AssertionError(f"unexpected fetchval: {q[:80]}")


def _machines(pool, bastions, machine=None):
    real = topology.list_bastions

    async def fake(_pool, _eid):
        return bastions

    topology.list_bastions = fake
    brain_service._pool = pool
    brain_service._entity_id = 3
    try:
        # `machine` passed EXPLICITLY: the route's default is FastAPI's Query(None)
        # object, which a direct call would receive as a truthy filter value.
        return _run(routes.list_machines(machine=machine))
    finally:
        topology.list_bastions = real
        brain_service._pool = None


def _open(machine, severity, n):
    return {"machine": machine, "severity": severity, "n": n}


# ── 1. one description of a machine ─────────────────────────────────────────

def test_the_states_are_the_hosts_own_derivations():
    rows = [
        bastion(alerts=ALERTS_FIRING),
        bastion(id=64, name="box-b", hostname="box-b"),
        bastion(id=65, name="box-c", hostname="box-c", online=False,
                last_seen_at="2026-09-08T14:08:00+00:00", alerts=ALERTS_FIRING),
    ]
    out = _machines(StubPool(), rows)
    by = {m["machine"]: m for m in out["machines"]}
    for b in rows:
        m = by[ov.machine_of(b)]
        assert (m["alerts_state"], m["alerts_reason"]) == tools._bastion_alerts_state(b), m["machine"]
        assert (m["detect_state"], m["detect_reason"]) == tools._bastion_detect_state(b), m["machine"]
        assert m["alerts"] == tools._alerts_counts(b.get("alerts"))
        assert m["detect"] == tools._detect_counts(b.get("detect"))
    # And the three readings the pane most depends on come out as the host says.
    assert by["box-a"]["alerts_state"] == "firing"
    assert by["box-b"]["alerts_state"] == "unconfigured", \
        "a machine with no alert source must read as unwatched, never quiet"


def test_the_card_names_the_machine_the_ledger_files_it_under():
    # Findings are keyed by ov.machine_of(); a card named any other way would
    # show "nothing open" beside a machine the ledger has rows for. The first
    # fixture has a pairing name DIFFERENT from its hostname, so a card named
    # by pairing name first (the wrong order) is caught, not just a missing one.
    rows = [bastion(id=4, hostname="real-host", name="pairing-name"),
            bastion(hostname=None, name="laptop"), bastion(id=9, hostname=None, name=None)]
    out = _machines(StubPool(open_rows=[_open("real-host", "critical", 1),
                                        _open("laptop", "high", 2),
                                        _open("token:9", "warn", 1)]), rows)
    names = {m["machine"]: m for m in out["machines"]}
    assert set(names) == {"real-host", "laptop", "token:9"}, set(names)
    assert names["real-host"]["open"]["critical"] == 1, "hostname must win over the pairing name"
    assert names["laptop"]["open"]["high"] == 2
    assert names["token:9"]["open"]["warn"] == 1
    assert out["other"] == [], "a paired machine's rows were reported as orphaned"


def test_open_counts_carry_every_severity_and_sort_worst_first():
    rows = [bastion(id=1, name="a", hostname="a"), bastion(id=2, name="b", hostname="b"),
            bastion(id=3, name="c", hostname="c")]
    pool = StubPool(open_rows=[_open("c", "critical", 1), _open("b", "warn", 4),
                               _open("b", "notice", 2)])
    out = _machines(pool, rows)
    assert [m["machine"] for m in out["machines"]] == ["c", "b", "a"]
    assert out["machines"][2]["open"] == {"notice": 0, "warn": 0, "high": 0, "critical": 0}
    assert out["machines"][1]["open"] == {"notice": 2, "warn": 4, "high": 0, "critical": 0}


def test_rows_for_a_machine_no_longer_paired_are_listed_not_dropped():
    out = _machines(StubPool(open_rows=[_open("privatae-server", "high", 3),
                                        _open("box-a", "warn", 18)]),
                    [bastion()])
    assert out["other"] == [{"machine": "privatae-server", "kind": "unpaired",
                             "open": {"notice": 0, "warn": 0, "high": 3, "critical": 0}}]


def test_findings_about_the_host_or_a_nameless_store_are_not_called_unpaired():
    # oversight_store files the missing-CA finding under "this host" and a
    # store with no server under "store:<id>". Neither is a machine that went
    # away, and the pane must not say so.
    from mastercode.runtime import oversight_store as ovs
    src = open(ovs.__file__, encoding="utf-8").read()
    assert f'machine="{routes.HOST_MACHINE_NAME}"' in src, \
        "the store fold no longer files host findings under the name this route labels"
    assert f'f"{routes.STORE_NAME_PREFIX}' in src
    out = _machines(StubPool(open_rows=[_open("this host", "high", 1),
                                        _open("store:7", "high", 1),
                                        _open("gone-box", "warn", 2)]), [bastion()])
    kinds = {o["machine"]: o["kind"] for o in out["other"]}
    assert kinds == {"this host": "host", "store:7": "store", "gone-box": "unpaired"}, kinds
    js = _read("system-apps/oversight/app.js")
    for kind in ("'host'", "'store'", "'unpaired'"):
        assert kind in js, f"the app has no label for kind {kind}"


def test_open_total_counts_each_name_once_and_marks_shared_names():
    # Two bastions reporting one hostname are two cards carrying the SAME
    # name's count; adding the cards up double-counted. open_total is summed
    # over the ledger's distinct names instead.
    rows = [bastion(id=1, name="raspberrypi", hostname="raspberrypi"),
            bastion(id=2, name="raspberrypi", hostname="raspberrypi"),
            bastion(id=3, name="solo", hostname="solo")]
    out = _machines(StubPool(open_rows=[_open("raspberrypi", "critical", 1),
                                        _open("gone-box", "warn", 2)]), rows)
    assert out["open_total"] == {"notice": 0, "warn": 2, "high": 0, "critical": 1}, out["open_total"]
    flags = sorted((m["machine"], m["shares_name"]) for m in out["machines"])
    assert flags == [("raspberrypi", True), ("raspberrypi", True), ("solo", False)], flags
    js = _read("system-apps/oversight/app.js")
    assert "state.openTotal" in js and "if (state.openTotal) return state.openTotal" in js, \
        "the tiles must read the route's total, not add the cards up"


def test_the_summary_comes_before_the_machines_and_machine_narrows():
    # kernel_op cuts a result at 20,000 characters; what survives must be the
    # summary and the worst machines, so the summary keys come FIRST.
    rows = [bastion(id=i, name=f"box-{i}", hostname=f"box-{i}") for i in range(1, 4)]
    out = _machines(StubPool(open_rows=[_open("box-2", "high", 1)]), rows)
    keys = list(out)
    assert keys == ["ingest", "count", "open_total", "other", "machines"], keys
    one = _machines(StubPool(open_rows=[_open("box-2", "high", 1)]), rows, machine="box-2")
    assert [m["machine"] for m in one["machines"]] == ["box-2"] and one["count"] == 1
    assert one["open_total"]["high"] == 1


# ── 2. never an all-clear from a read failure ────────────────────────────────

def test_an_unreadable_ledger_is_a_503_never_zero_open():
    try:
        _machines(StubPool(ledger_raises=True), [bastion()])
    except HTTPException as e:
        assert e.status_code == 503 and "ledger unavailable" in str(e.detail)
    else:
        raise AssertionError("an unreadable ledger produced counts")


def test_no_machines_is_only_said_when_the_token_table_agrees():
    out = _machines(StubPool(tokens=0), [])
    assert out["machines"] == [] and out["count"] == 0
    try:
        _machines(StubPool(tokens_raise=True), [])
    except HTTPException as e:
        assert e.status_code == 503 and "machine list unavailable" in str(e.detail)
    else:
        raise AssertionError('an unreadable machine list was reported as "no machines"')
    # THE CASE THAT IS EASY TO MISS: the token table reads fine and names paired
    # machines, but the list came back empty — list_bastions' own read failed
    # (it returns [] on error, with the same token filter the count uses).
    try:
        _machines(StubPool(tokens=5), [])
    except HTTPException as e:
        assert e.status_code == 503 and "5 paired machine(s)" in str(e.detail), str(e.detail)
    else:
        raise AssertionError('5 paired machines beside an empty list was reported as "no machines"')


def test_a_nonempty_list_does_not_pay_for_the_probe():
    pool = StubPool()
    _machines(pool, [bastion()])
    assert not [c for c in pool.calls if c[0] == "fetchval"], \
        "the token-table probe ran although machines were listed"


# ── 3. the record's three values ─────────────────────────────────────────────

def test_the_record_keeps_three_values():
    out = _machines(StubPool(), [
        bastion(id=1, name="whole", hostname="whole", record_dropped={}),
        bastion(id=2, name="holes", hostname="holes", record_dropped={"observe:net": 4}),
        bastion(id=3, name="silent", hostname="silent", record_dropped=None),
    ])
    by = {m["machine"]: m for m in out["machines"]}
    assert (by["whole"]["record"], by["whole"]["record_holes"]) == ("whole", None)
    assert (by["holes"]["record"], by["holes"]["record_holes"]) == ("holes", {"observe:net": 4})
    assert by["silent"]["record"] == "not reported", \
        "a box that did not report its record read as whole"


# ── the ingest line ──────────────────────────────────────────────────────────

def test_the_ingest_line_reports_the_standing_op_and_never_raises():
    from datetime import datetime, timezone
    ran = datetime(2026, 9, 23, 0, 3, 1, tzinfo=timezone.utc)
    nxt = datetime(2026, 9, 23, 0, 7, 55, tzinfo=timezone.utc)
    row = {"status": "scheduled", "start_time": nxt, "recurrence": "every:300",
           "last_ran_at": ran, "last_ok": True, "last_error": None,
           "runs_24h": 288, "failures_24h": 0}
    pool = StubPool(ingest=row)
    out = _machines(pool, [bastion()])
    assert out["ingest"] == {"status": "scheduled", "recurrence": "every:300",
                             "next_fire_at": nxt.isoformat(), "last_ran_at": ran.isoformat(),
                             "last_ok": True, "last_error": None,
                             "runs_24h": 288, "failures_24h": 0}
    ing_call = [c for c in pool.calls if c[0] == "fetchrow"][0]
    assert ing_call[2] == (3, ov.OVERSIGHT_TOOL), "the ingest is looked up by the ledger's own op name"
    assert _machines(StubPool(ingest=None), [bastion()])["ingest"] is None
    broken = _machines(StubPool(ingest_raises=True), [bastion()])["ingest"]
    assert broken["status"] == "unknown", "an unreadable schedule must not sink the machines pane"


# ── 4. the app's contract ────────────────────────────────────────────────────

def test_the_manifest_loads_and_declares_exactly_the_two_scopes():
    from pathlib import Path
    from server.applications import manifest, scopes
    m = manifest.load(Path(APP_DIR) / "app.yaml")
    assert m.id == "oversight"
    assert sorted(m.permissions) == ["read:oversight", "write:oversight"], m.permissions
    for p in m.permissions:
        assert p in scopes.PERMISSIONS, p
    assert not m.target_entities, "every host writes a ledger, so every host gets the window"
    assert m.prompt_behavior.user_path, "describe_app's where_to_find_it would come back empty"


def test_every_route_the_app_calls_exists_under_a_scope_it_declares():
    # EXACT, not loose: each call the app builds is reduced to a template —
    # a literal, then `{x}` where it splices encodeURIComponent(...), then any
    # literal suffix — and that template must equal a router path with its
    # parameter names normalised the same way. A typo in `/resolve` fails here.
    js = _read("system-apps/oversight/app.js")
    rx = re.compile(r"'(/api/oversight/[a-z/]*?)/?\??'"
                    r"(\s*\+\s*encodeURIComponent\([^)]*\)(?:\s*\+\s*'(/[a-z]+)')?)?")
    called = set()
    for lit, spliced, suffix in rx.findall(js):
        called.add(lit + ("/{x}" + (suffix or "") if spliced else ""))
    assert called == {"/api/oversight/findings", "/api/oversight/findings/{x}",
                      "/api/oversight/findings/{x}/resolve", "/api/oversight/machines"}, called
    paths = {}
    for r in routes.router.routes:
        scope = [getattr(d.call, "_app_scope", None) for d in r.dependant.dependencies]
        paths[re.sub(r"\{[^}]+\}", "{x}", r.path)] = [s for s in scope if s][0]
    declared = {"read:oversight", "write:oversight"}
    for c in called:
        assert c in paths, f"the app calls {c}, which no route serves"
        assert paths[c] in declared, f"{c} needs {paths[c]}, which the app does not declare"


def test_nothing_a_machine_names_reaches_innerhtml():
    # Hostnames, titles and evidence are written by the machines themselves.
    # Matched as CODE (an assignment or a call), so the header comment that
    # states the rule does not trip the rule.
    js = _read("system-apps/oversight/app.js")
    for rx in (r"\.innerHTML\s*[+]?=", r"\.outerHTML\s*[+]?=", r"insertAdjacentHTML\s*\(",
               r"document\.write\s*\(", r"\.srcdoc\s*="):
        assert not re.search(rx, js), f"app.js writes HTML ({rx})"


def test_the_app_is_image_baked_with_a_current_signed_bundle():
    baked = [ln.strip() for ln in _read("scripts/image_baked_apps.txt").splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    assert "oversight" in baked
    assert baked == sorted(baked), "image_baked_apps.txt is kept sorted"
    # A .cseries is a zip of manifest.yaml (the app.yaml, byte for byte) plus
    # payload/<file> (server/applications/bundles.py). test_bundle_freshness.py
    # compares VERSIONS; this compares BYTES, because a first cut at 0.1.0 can
    # change a hundred times before its version does.
    members = {"manifest.yaml": "app.yaml"}
    members.update({f"payload/{n}": n for n in ("index.html", "app.js", "style.css", "icon.svg")})
    from server.applications import bundles
    with open(os.path.join(_ROOT, "etc", "cseries-apps.pub.raw"), "rb") as f:
        pub = f.read()
    for d in ("native-bundles", "factory-bundles"):
        path = os.path.join(_ROOT, d, "oversight.cseries")
        assert os.path.exists(path), f"{d}/oversight.cseries is missing"
        with zipfile.ZipFile(path) as z:
            for member, src in members.items():
                with open(os.path.join(APP_DIR, src), "rb") as f:
                    assert z.read(member) == f.read(), \
                        f"{d}/oversight.cseries carries a stale {src} — re-pack and re-sign it"
        # AND SIGNED by the first-party key the installer trusts — bytes that
        # match the source but carry a bad signature would never install.
        with open(path, "rb") as f:
            bundles.verify(f.read(), public_key_raw=pub)


def test_the_dock_has_a_glyph_for_it():
    icons = _read("dashboard/base/js/icons.js")
    assert re.search(r"^\s+oversight: `", icons, re.M), "the dock would fall back to a letter tile"


def test_the_host_reads_the_ledger_and_never_resolves_it():
    # The host WRITES this ledger every five minutes; it must be able to READ
    # it, or "how long has this been wrong" is answerable only in the window.
    # The resolve verb is a person's, and it fails closed twice: walled by
    # path, and write:oversight has no entity bits.
    from mastercode.runtime import kernel_ops as ko
    from mastercode.runtime.meta_tool_caps import ENTITY_SCOPE_TO_BITS
    from server.applications.api_contract import _route_scope
    resolve = "/api/oversight/findings/{finding_id}/resolve"
    # The contract rows are built from the REAL router with the builder's own
    # scope reader (api_contract._route_scope), not typed out by hand — the
    # table kernel_op reads in the image is built the same way.
    table = {}
    for r in routes.router.routes:
        for m in r.methods:
            table[(m, r.path)] = {"method": m, "path": r.path, "scope": _route_scope(r)}
    assert table[("POST", resolve)]["scope"] == "write:oversight"
    real = ko._contract_cache
    ko._contract_cache = table
    try:
        assert ko.exposed_route("GET", "/api/oversight/findings") is not None
        assert ko.exposed_route("GET", "/api/oversight/machines") is not None
        assert ko.exposed_route("POST", resolve) is None, "the host can resolve findings"
        assert ko.exposed_route("POST", "/api/oversight/findings/42/resolve") is None
        by_template = _run(ko.run_kernel_op({"method": "POST", "path": resolve}))
        # A REAL id — what a host actually sends — gets the wall's own reason
        # too, not the generic "not an exposed kernel op".
        by_id = _run(ko.run_kernel_op({"method": "POST",
                                       "path": "/api/oversight/findings/42/resolve"}))
    finally:
        ko._contract_cache = real
    for out in (by_template, by_id):
        assert out["status"] == "denied", out
        assert "Mark resolved" in out["result"] and "credential" not in out["result"], \
            f"the wall's reason tells the host something false: {out['result']}"
    assert ENTITY_SCOPE_TO_BITS.get("read:oversight") == 1
    assert "write:oversight" not in ENTITY_SCOPE_TO_BITS, \
        "write:oversight in the entity vocabulary makes resolve a host capability"
    # The skill tells the host how to read it, and the tri-state trap.
    skill = _read("default-skills/network.md")
    assert "GET `/api/oversight/findings`" in skill and "you get BOTH" in skill


def test_what_a_resolve_does_follows_how_the_row_is_made():
    # DRIVEN FROM THE FOLD, not from the wording: what the dialog promises must
    # be what merge_findings and the store fold actually do to a resolved row.
    from datetime import datetime, timezone
    from mastercode.runtime import oversight_store as ovs
    now = datetime(2026, 9, 23, tzinfo=timezone.utc)

    def reopens(f) -> bool:
        plan = ov.merge_findings(
            [{"id": 1, "key": f["key"], "severity": f["severity"], "resolved_at": now}],
            [f], {})
        return bool(plan["update"]) and plan["update"][0].get("reopened") is True

    # 1. The three REBUILT store kinds: while the condition holds, every store
    #    read derives the same key again, and a resolved row reopens.
    rebuilt = [ovs.unreadable_finding("box-1", "the list failed", ov._finding),
               ovs._accounting_finding("box-1", {"state": "unreadable"}, ov._finding),
               ovs._host_ca_finding("no CA at /etc/x", ov._finding)]
    for f in rebuilt:
        assert f["source"] == "store" and not f["kind"].startswith("detect:")
        assert reopens(f), f"{f['kind']} did not reopen when re-derived"
        assert routes.kind_of(f["key"], f["machine"]) == f["kind"], f["key"]

    # 2. A JOURNAL row: the `opened` line is read once; the next read carries
    #    only the day's snapshot (or nothing), which never re-asserts it — so the
    #    resolved row is never touched again until the box writes a change.
    opened = (b'{"transition": "opened", "event": {"fp": "fpA", "kind": "new_exe", '
              b'"level": "high", "entry": {"exe": "/tmp/x"}}}\n')
    first, _, _, _ = ovs.findings_from_journal(opened, "box-1", ov._finding)
    assert len(first) == 1 and first[0]["kind"].startswith("detect:"), first
    snapshot = b'{"transition": "day_open", "open": ["fpA"]}\n'
    again, _, _, _ = ovs.findings_from_journal(snapshot, "box-1", ov._finding)
    assert again == [], "a day_open snapshot re-asserted a journal finding"
    plan = ov.merge_findings([{"id": 1, "key": first[0]["key"], "severity": "high",
                               "resolved_at": now}], again, {})
    assert plan["update"] == [] and plan["new"] == 0, "the resolved journal row moved"
    assert routes.kind_of(first[0]["key"], "box-1") == first[0]["kind"]
    # ...and a CHANGED line for it does reopen it: "until the machine records a change".
    changed = opened.replace(b'"opened"', b'"changed"')
    later, _, _, _ = ovs.findings_from_journal(changed, "box-1", ov._finding)
    assert reopens(later[0])

    # 3. The dialog says exactly those three things, keyed on the row's kind.
    js = _read("system-apps/oversight/app.js")
    note = js[js.index("function resolveNote"):js.index("function askResolve")]
    assert "f.source === 'heartbeat' || f.source === 'alert'" in note
    assert "kind.indexOf('detect:') === 0" in note and "stays resolved until the machine" in note
    assert "about every fifteen minutes" in note
    assert "$('c-message').textContent = resolveNote(f)" in js
    html = _read("system-apps/oversight/index.html")
    assert 'id="c-message"' in html and "within five minutes" not in html
    # The manifest carries one paragraph; the resolve rule is
    # the skill's.
    for rel in ("default-skills/network.md",):
        text = " ".join(_read(rel).split())      # prose wraps; the claim does not
        assert "stays resolved until the machine records a change" in text, rel
        assert "detect:" in text and "fifteen minutes" in text, rel
    # The fifteen minutes is the fold's own cadence, not a figure of speech.
    assert ovs.STORE_POLL_EVERY_N_PASSES * ov.OVERSIGHT_PERIOD_S == 15 * 60


def test_every_finding_route_emits_the_kind():
    # The dialog's three cases key on `kind`; if a route stopped emitting it,
    # every journal finding would be told it "reopens within fifteen minutes".
    # Driven through all three routes.
    from datetime import datetime, timezone
    rec = {"id": 7, "key": "box-1|detect:new_exe|fpA", "severity": "high", "source": "store",
           "machine": "box-1", "first_seen": datetime(2026, 9, 23, tzinfo=timezone.utc),
           "last_seen": datetime(2026, 9, 23, tzinfo=timezone.utc), "resolved_at": None,
           "evidence": "{}", "recommendation": "", "pr_url": None}

    class RowPool:
        async def fetch(self, q, *a):
            return [rec]

        async def fetchrow(self, q, *a):
            return rec

    brain_service._pool = RowPool()
    try:
        listed = _run(routes.list_findings(severity=None, resolved=None, machine=None, limit=10))
        one = _run(routes.get_finding(7))
        resolved = _run(routes.resolve_finding(7))
    finally:
        brain_service._pool = None
    assert listed["findings"][0].get("kind") == "detect:new_exe", listed
    assert one.get("kind") == "detect:new_exe", one
    assert resolved["finding"].get("kind") == "detect:new_exe", resolved


def test_the_kind_round_trips_the_ledger_key():
    for machine, kind, fp in (("box-1", "alert", "fpA"), ("this host", "accounting-ca", "missing"),
                              ("store:7", "store-state", "unreadable"),
                              ("odd|name", "detect:new_exe", "fpB"), ("box-2", "offline", "")):
        key = ov.finding_key(machine, kind, fp)
        assert routes.kind_of(key, machine) == kind, (machine, kind, key)


def test_the_window_keeps_its_own_rules():
    js = _read("system-apps/oversight/app.js")
    # An href is the one place a value could run as code.
    assert "/^https?:\\/\\//i.test(String(f.pr_url))" in js, "pr_url is linked without a scheme check"
    # An offline machine shows no green anywhere.
    assert "(!m.online && t === 'allow') ? '' : t" in js
    # The deep-linked card is scrolled to ONCE, not on every refresh.
    assert "state.scrollToFocus = false" in js and "state.scrollToFocus = true" in js
    # The banner names no cause the route does not carry.
    assert "Agents app" not in js


def test_the_views_the_manifest_advertises_are_the_ones_the_app_parses():
    yaml_src = _read("system-apps/oversight/app.yaml")
    js = _read("system-apps/oversight/app.js")
    for view in ("finding:", "machine:"):
        assert view in yaml_src and f"'{view}'" in js, view
    assert "cseries:app-view" in js, "a view sent to an open window would be dropped"


def _main() -> int:
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"OK    {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL  {name}: {e}")
            except Exception as e:  # noqa: BLE001 — a crash is a failure, named
                # Reported, not raised: a KeyError used to end the run on stderr
                # with no FAIL line, which reads as silence to anyone grepping.
                fails += 1
                print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{'FAILED' if fails else 'PASSED'} — {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_main())
