#!/usr/bin/env python3
"""
Turn types: the Host drafts, only the person's click decides.

Container-free: APP_ROOT=<repo> python3 scripts/<this>. No pod, no DB, no
model; the store is a stub that RAISES on SQL it does not know and records
every write.

  1. the spec validator: unknown tool groups, admin / shell_exec, unknown
     capability names, a missing skill, a bad memory scope, over-long lists
     are all refused by name; a change keeps what it does not name
  2. the card is the platform's words: what the type holds and what it
     CANNOT use; the Host's reason is one flattened line; Once is offered
     only when a change adds something
  3. draft writes an INERT row (status 'draft') and files ONE platform card
     (kind, payload, user_only, no_pattern); the result carries the ask
  4. change refuses a draft and a second card for the same type; retire is
     a card too (retiring widens)
  5. apply: From now on activates / changes compare-and-set on the card's
     version; Not now writes nothing; a stale card applies nothing; Once
     writes only once_grant; retire retires
  6. Once is taken by the next turn and cleared in one statement, and
     widens that turn only
  7. nothing but turn_type_approval writes a type's status or profile (the
     repo is swept); POST /api/tickets strips the kind and the payload so a
     Host cannot forge a card; the decide route applies it; the executor,
     caps, operate lane, typed-turn access, ask card and session are wired

Exit 0 = all pass; exit 1 + [turn-types-approve] FAIL lines otherwise.
"""

import asyncio
import json
import os
import re
import sys
from turn_source import turn_source  # session.py and the modules split out of it
from tools_source import tools_source  # tools.py and the modules split out of it

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"[turn-types-approve] {'ok  ' if ok else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


class UnknownSQL(RuntimeError):
    pass


TID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class _Pool:
    def __init__(self, row=None, open_card=None, once=None, by_name=None):
        self.row = row
        self.by_name = by_name
        self.open_card = open_card
        self.once = once
        self.writes: list[tuple[str, tuple]] = []

    async def fetchval(self, sql, *a):
        if "FROM entity_tickets" in sql and "turn_type_proposal" in sql:
            return self.open_card
        if "display_name FROM entity_registry" in sql:
            return "Host 9003"
        if "UPDATE turn_types t SET once_grant = '{}'::jsonb" in sql:
            g, self.once = self.once, None
            self.writes.append(("consume_once", a))
            return json.dumps(g) if g else None
        raise UnknownSQL(sql)

    async def fetchrow(self, sql, *a):
        if "INSERT INTO turn_types" in sql:
            # The row must be written INERT: the literal status in the SQL.
            if "'draft'" not in sql:
                raise UnknownSQL("a turn type INSERT that is not a draft: " + sql)
            self.writes.append(("insert", a))
            return {"id": TID, "version": 1}
        if sql.startswith("SELECT") and "lower(name) = lower($2)" in sql:
            return self.by_name
        if sql.startswith("SELECT") and "FROM turn_types WHERE id" in sql:
            return self.row
        if sql.startswith("UPDATE turn_types SET"):
            self.writes.append((sql.split(" WHERE ")[0], a))
            # CAS: the stub row's status and version decide.
            if self.row is None:
                return None
            want_status = re.search(r"AND status = '(\w+)'", sql).group(1)
            if self.row["status"] != want_status:
                return None
            # Compare-and-set is read from the SQL itself: a statement with
            # no `version = $n` in its WHERE is not a CAS, and the stub then
            # refuses to pretend it was one.
            where = sql.split(" WHERE ", 1)[1]
            if "version = version + 1" not in sql.split(" WHERE ", 1)[0] and "once_grant" not in sql \
                    and "status = 'retired'" not in sql:
                self.writes.append(("NO-BUMP", a))
            m = re.search(r"version = \$(\d+)", where)
            if m is None:
                self.writes.append(("NO-CAS", a))
            elif self.row["version"] != a[int(m.group(1)) - 1]:
                return None
            return {"version": self.row["version"] + 1, "id": TID}
        raise UnknownSQL(sql)

    async def fetch(self, sql, *a):
        raise UnknownSQL(sql)

    async def execute(self, sql, *a):
        raise UnknownSQL(sql)


def _row(**over):
    base = {"id": TID, "name": "Finance", "status": "active", "keywords": ["invoice"],
            "context": {}, "skill": None, "tool_groups": ["vault"], "grant_names": ["read_context"],
            "keys": ["stripe-live"], "bastions": [], "model": None, "thread_id": None, "version": 3}
    base.update(over)
    return base


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, os.path.join(app_root, "brain"))
    sys.path.insert(0, app_root)

    from mastercode.runtime import turn_type_approval as tta
    from mastercode.runtime import turn_types as tt
    import server.routes.tickets as tk

    filed: list[dict] = []

    async def fake_create(pool, eid, **kw):
        filed.append(kw)
        return {"id": 41 + len(filed)}
    tk.create_ticket_service = fake_create
    run = asyncio.run

    groups = sorted(tta.known_groups())
    g0 = "vault" if "vault" in groups else groups[0]

    # ── 1. The spec validator.
    def refused(spec, needle, base=None):
        try:
            tta.normalize_spec(spec, base=base)
            return False
        except tta.SpecError as e:
            return needle in str(e)
    check("an unknown tool group is refused by name", refused({"tool_groups": ["nope"]}, "unknown tool groups: nope"))
    check("admin can never be held", refused({"grant": ["admin"]}, "can never hold: admin"))
    check("shell_exec can never be held", refused({"grant": ["shell_exec"]}, "can never hold: shell_exec"))
    check("a capability that does not exist is refused", refused({"grant": ["root"]}, "do not exist: root"))
    check("a missing skill is refused", refused({"skill": "no-such-skill-xyz"}, "no skill named"))
    check("a bad memory scope is refused", refused({"context": {"memory": "most"}}, "context.memory must be one of"))
    check("an over-long list is refused", refused({"keys": [f"k{i}" for i in range(41)]}, "at most 40"))
    p = tta.normalize_spec({"tool_groups": [g0], "grant": ["READ_CONTEXT"], "keywords": ["Invoice"]})
    check("a good spec normalizes (grant and keywords lower-cased)",
          p["tool_groups"] == [g0] and p["grant"] == ["read_context"] and p["keywords"] == ["invoice"], f"{p}")
    base = {"tool_groups": [g0], "grant_names": ["read_context"], "keys": ["stripe-live"],
            "bastions": [], "keywords": ["invoice"], "skill": None, "context": {}, "model": "m1"}
    p = tta.normalize_spec({"keys": ["stripe-live", "bank"]}, base=base)
    check("a change keeps every field it does not name",
          p["tool_groups"] == [g0] and p["grant"] == ["read_context"] and p["model"] == "m1"
          and p["keys"] == ["stripe-live", "bank"], f"{p}")

    # ── 2. The card.
    prop = tta.normalize_spec({"tool_groups": [g0], "grant": ["read_context"], "keys": ["stripe-live"]})
    s = tta.card_sitrep("Host 9003", "Finance", prop, action="activate", reason="because\nthe user asked")
    check("the card names the Host and the type", s.startswith("Host 9003 wants a Finance turn type."), s)
    check("the card says what the type CANNOT use", "It cannot use:" in s and g0 not in s.split("It cannot use:")[1], s)
    check("the Host's reason is one flattened line", "Why: because the user asked" in s and s.count("\n") == 3, s)
    opts = [o["id"] for o in tta.card_options("activate")]
    check("a draft card offers From now on / Not now", opts == ["activate", "keep"], f"{opts}")
    check("a change card offers Once only when it adds something",
          [o["id"] for o in tta.card_options("change", widens=True)] == ["change", "once", "keep"]
          and [o["id"] for o in tta.card_options("change", widens=False)] == ["change", "keep"])
    s = tta.card_sitrep("Host 9003", "Finance", prop, action="retire")
    check("the retire card says the thread would run with everything the Host holds",
          "run with everything your Host holds" in s, s)

    # ── 3. draft.
    pool = _Pool()
    filed.clear()
    r = run(tta.draft(pool, 7, "Finance", {"tool_groups": [g0]}, "set up finance"))
    ins = [w for w in pool.writes if w[0] == "insert"]
    check("draft writes one INERT row", len(ins) == 1 and "'draft'" not in str(ins[0][1]), f"{pool.writes}")
    md = filed[0]["metadata"] if filed else {}
    check("draft files ONE platform card: kind, payload, user_only, no_pattern",
          len(filed) == 1 and md.get("kind") == tta.PROPOSAL_KIND and md.get("user_only") is True
          and md.get("no_pattern") is True and md[tta.PAYLOAD_KEY]["action"] == "activate"
          and md[tta.PAYLOAD_KEY]["type_id"] == TID and md[tta.PAYLOAD_KEY]["base_version"] == 1, f"{filed}")
    check("draft returns the ask the chat card reads",
          r.get("status") == "ok" and r["result"]["ask"]["ticket_id"] == 42
          and [o["id"] for o in r["result"]["ask"]["options"]] == ["activate", "keep"], f"{r}")
    r = run(tta.draft(_Pool(), 7, "", {}, ""))
    check("draft needs a name", r.get("status") == "error")
    r = run(tta.draft(_Pool(), 7, "X", {"grant": ["admin"]}, ""))
    check("draft refuses a bad spec before writing anything", r.get("status") == "error" and "admin" in r["result"])

    # ── 4. change / retire.
    r = run(tta.request_change(_Pool(row=_row(status="draft")), 7, TID, {}, ""))
    check("change refuses a draft", r.get("status") == "error" and "draft" in r["result"], f"{r}")
    r = run(tta.request_change(_Pool(row=_row(), open_card=9), 7, TID, {}, ""))
    check("one card per type at a time", r.get("status") == "error" and "already waiting" in r["result"], f"{r}")
    filed.clear()
    r = run(tta.request_change(_Pool(row=_row()), 7, TID, {"keys": ["stripe-live", "bank"]}, ""))
    pl = filed[0]["metadata"][tta.PAYLOAD_KEY] if filed else {}
    check("a widening change offers Once and records what it adds",
          [o["id"] for o in r["result"]["ask"]["options"]] == ["change", "once", "keep"]
          and pl.get("adds") == {"keys": ["bank"]} and pl.get("base_version") == 3, f"{r} {pl}")
    filed.clear()
    r = run(tta.request_retire(_Pool(row=_row()), 7, TID, ""))
    check("retire is a card, never a write", r.get("status") == "ok"
          and filed and filed[0]["metadata"][tta.PAYLOAD_KEY]["action"] == "retire", f"{r}")

    # ── 5. apply.
    md_act = {tta.PAYLOAD_KEY: {"type_id": TID, "action": "activate", "base_version": 1,
                                "proposed": tta.normalize_spec({"tool_groups": [g0]})}}
    pool = _Pool(row=_row(status="draft", version=1))
    r = run(tta.apply_proposal(pool, 7, md_act, option_id="activate", ticket_id=42))
    check("From now on activates, compare-and-set on the draft's version",
          r.get("applied") is True and any("SET status = 'active'" in w[0] for w in pool.writes)
          and not any(w[0] == "NO-CAS" for w in pool.writes), f"{r} {pool.writes}")
    pool = _Pool(row=_row(status="draft", version=2))
    r = run(tta.apply_proposal(pool, 7, md_act, option_id="activate", ticket_id=42))
    check("a stale card applies nothing and says so", r.get("applied") is False and "changed" in r.get("reason", ""), f"{r}")
    pool = _Pool(row=_row(status="draft", version=1))
    r = run(tta.apply_proposal(pool, 7, md_act, option_id="keep", ticket_id=42))
    check("Not now writes nothing", r.get("applied") is False and pool.writes == [], f"{r} {pool.writes}")
    r = run(tta.apply_proposal(_Pool(row=_row(status="draft", version=1)), 7, md_act,
                               option_id="retire", ticket_id=42))
    check("an option the card does not offer applies nothing", r.get("applied") is False)
    md_once = {tta.PAYLOAD_KEY: {"type_id": TID, "action": "change", "base_version": 3,
                                 "proposed": {}, "adds": {"keys": ["bank"]}}}
    pool = _Pool(row=_row())
    r = run(tta.apply_proposal(pool, 7, md_once, option_id="once", ticket_id=43))
    check("Once writes only once_grant", r.get("applied") is True and len(pool.writes) == 1
          and pool.writes[0][0].startswith("UPDATE turn_types SET once_grant"), f"{pool.writes}")
    md_ret = {tta.PAYLOAD_KEY: {"type_id": TID, "action": "retire", "base_version": 3}}
    pool = _Pool(row=_row())
    r = run(tta.apply_proposal(pool, 7, md_ret, option_id="retire", ticket_id=44))
    check("Retire it retires", r.get("applied") is True and "status = 'retired'" in pool.writes[0][0], f"{pool.writes}")
    r = run(tta.apply_proposal(_Pool(row=_row()), 7, {tta.PAYLOAD_KEY: {"type_id": TID}},
                               option_id="activate", ticket_id=45))
    check("a card with no version applies nothing", r.get("applied") is False)

    # a draft is never stuck.
    filed.clear()
    pool = _Pool(row=_row(status="draft", version=2), by_name=_row(status="draft", version=2))
    r = run(tta.draft(pool, 7, "Finance", {"tool_groups": [g0]}, "again"))
    check("re-drafting bumps the version in the SQL itself", not any(w[0] == "NO-BUMP" for w in pool.writes), f"{pool.writes}")
    check("re-drafting a DRAFT name updates the draft, bumps its version and files a new card",
          r.get("status") == "ok" and filed and filed[0]["metadata"][tta.PAYLOAD_KEY]["base_version"] == 3
          and any("status = 'draft'" in str(w) or w[0].startswith("UPDATE turn_types SET keywords") for w in pool.writes),
          f"{r} {pool.writes}")
    # Ships: a re-draft writes its always-on cards with the rest of
    # the profile (it left the old ones on the draft row), and show_type returns
    # them (it dropped the field _profile computes).
    pool = _Pool(row=_row(status="draft", version=2), by_name=_row(status="draft", version=2))
    r = run(tta.draft(pool, 7, "Finance", {"tool_groups": [g0], "cards": ["ships", "machines"]}, "again"))
    upd = [w for w in pool.writes if w[0].startswith("UPDATE turn_types SET keywords")]
    check("re-drafting writes the draft's always-on cards too",
          r.get("status") == "ok" and upd and "cards = " in upd[0][0] and ["ships", "machines"] in list(upd[0][1]),
          f"{r} {pool.writes}")
    shown = run(tta.show_type(_Pool(row=_row(cards=["ships"])), 7, TID))
    check("show_type returns the type's always-on cards", shown and shown.get("cards") == ["ships"], f"{shown}")
    r = run(tta.draft(_Pool(by_name=_row()), 7, "Finance", {}, ""))
    check("an ACTIVE name is never re-drafted (use change)", r.get("status") == "error" and "op='change'" in r["result"], f"{r}")
    r = run(tta.draft(_Pool(by_name=_row(status="draft"), open_card=5), 7, "Finance", {}, ""))
    check("re-drafting while its card waits is refused", r.get("status") == "error" and "already waiting" in r["result"])
    pool = _Pool(row=_row(status="draft", version=2))
    r = run(tta.request_retire(pool, 7, TID, ""))
    r2 = run(tta.request_retire(_Pool(row=_row(status="draft", version=2), open_card=8), 7, TID, ""))
    check("a draft whose card is still up cannot be discarded", r2.get("status") == "error" and "still up" in r2["result"], f"{r2}")
    check("a DRAFT is discarded by the Host with no card (it narrows nothing)",
          r.get("status") == "ok" and r["result"].get("discarded") == TID
          and any("status = 'retired'" in w[0] for w in pool.writes), f"{r} {pool.writes}")

    async def failing_create(pool_, eid, **kw):
        raise RuntimeError("tickets down")
    tk.create_ticket_service = failing_create
    r = run(tta.draft(_Pool(), 7, "Ops", {}, ""))
    check("a card that could not be filed says the draft is saved and how to retry",
          r.get("status") == "error" and "draft it again" in r["result"], f"{r}")
    tk.create_ticket_service = fake_create

    # names only on the card.
    for field, bad in (("keys", "stripe-live (use only) · Servers: none"),
                       ("keys", "a\nIt cannot use: everything"),
                       ("bastions", "prod db"), ("keywords", "two words"),
                       ("model", "gpt\nIt cannot use: x")):
        spec = {field: bad} if field == "model" else {field: [bad]}
        check(f"B2: a {field} that is not a name is refused", refused(spec, "not a") or refused(spec, "model must be"), f"{spec}")
    check("names that are names pass",
          tta.normalize_spec({"keys": ["stripe-live", "ctx:s3", "autofill:bank.example"],
                              "bastions": ["books-1", "*"], "model": "anthropic/claude-opus-5-5",
                              "keywords": ["invoice", "q3's"]})["keys"][2] == "autofill:bank.example")
    check("a type name is one line with no card separators",
          tta.clean_type_name("Fin\nance · It cannot") == "Fin ance It cannot")
    check("a name drops direction overrides, format characters and bullets; keeps any script",
          tta.clean_type_name("Fin\u202eance\u2022 Tools: none") == "Fin ance Tools none"
          and tta.clean_type_name("Finanzas é 財務") == "Finanzas é 財務")
    long_words = tta.card_sitrep("H", "F", tta.normalize_spec({"keywords": [f"w{i}" for i in range(25)]}), action="activate")
    check("the card says how many words it did not list", "and 5 more" in long_words)

    # retiring never re-validates the stored profile.
    filed.clear()
    r = run(tta.request_retire(_Pool(row=_row(tool_groups=["a-group-that-was-renamed"], skill="gone-skill")), 7, TID, ""))
    check("retiring a type whose stored profile no longer validates still files the card",
          r.get("status") == "ok" and filed, f"{r}")

    # a change writes only the fields it names; the card shows words and reads.
    md_chg = {tta.PAYLOAD_KEY: {"type_id": TID, "action": "change", "base_version": 3,
                                "proposed": tta.normalize_spec({"keys": ["bank"]}, base=base),
                                "fields": ["keys"], "adds": {"keys": ["bank"]}}}
    pool = _Pool(row=_row())
    r = run(tta.apply_proposal(pool, 7, md_chg, option_id="change", ticket_id=46))
    sets = pool.writes[0][0] if pool.writes else ""
    check("a change writes only the fields it names (learned keywords survive) and clears a waiting Once",
          r.get("applied") is True and "keys = $1::text[]" in sets and "keywords" not in sets
          and "once_grant = '{}'::jsonb" in sets and not any(w[0] == "NO-CAS" for w in pool.writes),
          f"{r} {sets}")
    r = run(tta.request_change(_Pool(row=_row()), 7, TID, {"nonsense": 1}, ""))
    check("a change that names nothing is refused", r.get("status") == "error" and "nothing" in r["result"])
    s2 = tta.card_sitrep("Host 9003", "Finance", tta.normalize_spec({"keywords": ["invoice"]}), action="activate")
    check("the card always says what the type reads and the words that mean it",
          "Reads: everything your Host reads" in s2 and "Words that mean it: invoice" in s2, s2)

    # ── 6. Once.
    pool = _Pool(once={"keys": ["bank"], "tool_groups": ["network"]})
    g = run(tta.consume_once(pool, 7, TID))
    check("the next turn takes the Once grant", g == {"keys": ["bank"], "tool_groups": ["network"]}, f"{g}")
    check("...and it is gone for the turn after", run(tta.consume_once(pool, 7, TID)) == {})
    t = tta.with_once(_row(), g)
    check("Once widens THIS turn only (the stored row is untouched)",
          t["keys"] == ["stripe-live", "bank"] and t["tool_groups"] == ["vault", "network"]
          and _row()["keys"] == ["stripe-live"] and t["once"] == g, f"{t}")

    # ── 7. Structural.
    # SQL written as adjacent Python string literals is joined first, then a
    # SET list is read up to its WHERE / FROM inside one literal — so a
    # multi-line statement is read whole and a stub's `"… SET x" in sql`
    # test (no WHERE in the literal) is not mistaken for a writer.
    writer_re = re.compile(r'(UPDATE\s+turn_types\s+(?:t\s+)?SET\s+([^"]*?)\s+(?:WHERE|FROM)\b|INSERT\s+INTO\s+turn_types)',
                           re.I)
    allowed_elsewhere = {"thread_id", "keywords"}   # the type's thread, learned words
    offenders = []
    for base_dir in ("brain", "server", "scripts", "system-apps"):
        for dp, _dn, fns in os.walk(os.path.join(app_root, base_dir)):
            for fn in fns:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dp, fn)
                if path.endswith("turn_type_approval.py") or path.endswith("test_turn_types_approval.py"):
                    continue
                src = open(path, encoding="utf-8", errors="ignore").read()
                src = re.sub(r'"\s*\n\s*f?"', "", src)
                for m in writer_re.finditer(src):
                    cols = {c.split("=")[0].strip().strip('"').lower()
                            for c in re.split(r",(?![^()]*\))", re.sub(r"[\"'\s]+", " ", m.group(2) or ""))
                            if "=" in c}
                    if m.group(0).upper().startswith("INSERT") or not cols or not cols <= allowed_elsewhere | {"updated_at"}:
                        offenders.append(f"{os.path.relpath(path, app_root)}: {m.group(0)}")
    check("nothing but turn_type_approval writes a type's status or profile (repo swept)",
          not offenders, f"{offenders}")
    stripped = tk._strip_platform_locks({"kind": "turn_type_proposal", "turn_type_proposal": {"x": 1},
                                         "turn_type_apply": {}, "user_only": True, "note": "k"})
    check("POST /api/tickets strips the kind and the payload (a Host cannot forge a card)",
          stripped == {"note": "k"}, f"{stripped}")
    check("the two kind constants agree", tk.TURN_TYPE_PROPOSAL_KIND == tta.PROPOSAL_KIND)
    tsrc = open(os.path.join(app_root, "server", "routes", "tickets.py"), encoding="utf-8").read()
    check("the decide route applies a turn type card after the decide, and records it",
          'if md.get("kind") == TURN_TYPE_PROPOSAL_KIND:' in tsrc
          and "jsonb_build_object('turn_type_apply'" in tsrc)
    tools_src = tools_source(app_root)
    check("the executor is dispatched", 'elif name == "turn_type":\n        return await _exec_turn_type(' in tools_src)
    i = tools_src.find("async def _exec_turn_type(")
    body = tools_src[i:tools_src.find("\nasync def ", i + 10)]
    check("the executor has no op that activates, widens or retires",
          not re.search(r"UPDATE\s+turn_types", body) and "apply_proposal" not in body.split('"""', 2)[-1]
          and "getattr(_tta" not in body and "import_module" not in body
          and "vars(" not in body and "__dict__" not in body
          and set(re.findall(r"_tta\.(\w+)", body)) <= {"list_types", "show_type", "draft",
                                                         "request_change", "request_retire"},
          f"{set(re.findall(r'_tta.(\\w+)', body))}")
    from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS, READ_SAFE_OPS
    check("caps: registered, list/show read-safe",
          "turn_type" in META_TOOL_CAPS and READ_SAFE_OPS.get("turn_type") == {"list": 1, "show": 1})
    from mastercode.runtime.direct_ops import DIRECT_OP_TOOLS
    check("the operate lane holds it", "turn_type" in DIRECT_OP_TOOLS)
    check("a typed turn can always ask about types", "turn_type" in tt.ALWAYS_ALLOWED)
    kit = open(os.path.join(app_root, "app-web-kit", "ask.ts"), encoding="utf-8").read()
    check("the chat renders its card", '"turn_type"]);' in kit and "RESULT_ASK_TOOLS" in kit)
    ssrc = turn_source(app_root)
    check("the session takes a Once grant ONLY for an active type",
          '(sess.turn_type is not None and sess.turn_type.get("status") == "active"\n'
          '                    and not _pinned):\n'
          '                from mastercode.runtime.turn_type_approval import consume_once, with_once' in ssrc)
    i_post = tsrc.find("async def create_ticket(")
    check("the POST route still strips caller metadata",
          i_post > 0 and "_strip_platform_locks(" in tsrc[i_post:i_post + 2500])
    asrc = open(os.path.join(app_root, "brain", "mastercode", "runtime", "turn_type_approval.py"), encoding="utf-8").read()
    once_sql = re.search(r'"UPDATE turn_types SET once_grant = \$1::jsonb, updated_at = now\(\) "', asrc)
    check("Once's UPDATE sets only once_grant (and updated_at)", once_sql is not None)
    for stmt in re.findall(r'"UPDATE turn_types SET[^"]*"', asrc):
        pass
    non_apply = asrc.split("async def apply_proposal", 1)[0] + asrc.split("async def consume_once", 1)[1]
    check("outside apply_proposal every write targets a DRAFT only (or clears Once)",
          all("status = 'draft'" in w or "once_grant = '{}'" in w
              for w in re.findall(r'"UPDATE turn_types (?:t )?SET.*?(?:RETURNING[^"]*"|once_grant", )', non_apply, re.S)),
          "a non-apply writer touches a live type")
    check("the skill exists", os.path.exists(os.path.join(app_root, "default-skills", "turn-types.md")))

    print()
    if FAILS:
        print(f"[turn-types-approve] {len(FAILS)} FAILED: {FAILS}")
        return 1
    print("[turn-types-approve] all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
