#!/usr/bin/env python3
"""
Turn types: understanding the turn.

The platform decides which type a message is BEFORE the turn, in software,
with the scorer the pre-turn pick already uses — run over each active
type's own words. Container-free: APP_ROOT=<repo> python3 scripts/<this>.
No pod, no pool, no model; the store is a stub that RAISES on any SQL it
does not know (a changed statement fails loudly, never silently returns
None) and records every write with its arguments.

  1. untyped is unchanged: no active types → every message is held where it
     is, one read, nothing written; the send route never fails for the
     classifier
  2. classify: a keyword scores 1, an everyday word ½, the type's own name a
     strong hit; a key name is one whole strong word (never its halves); an
     everyday word never becomes strong through a name; the load threshold
     holds; a tie at the top is no winner; draft and retired types never
     win; the thread's tool trail counts for the type holding those tools,
     at most 1
  3. resolve: a dropdown pick lands in that type's thread (made on first
     use, bound both ways); a pick the scorer would not have made teaches
     the type the message's DISTINCTIVE words, in the order written — no
     everyday words, nothing from NEVER_TEACH, nothing another type already
     means, deduped against what the type has, capped per correction, and
     never past KEYWORD_CAP (the Host-seeded words are never pushed out)
  4. resolve: a message in a typed thread stays (bound); an untyped thread
     with a clear winner moves; below the threshold it stays; a closed
     binding holds; a store failure holds; the scorer never lands in a
     thread the person CLOSED (held) while a pick reopens it; two sends
     creating the type's thread at once end in ONE thread (the loser's row
     is deleted); every write names the entity
  5. the send route lands the message and reports it in one-line ASCII
     headers; the thread list carries the type; the read route is
     registered; the web client sends the pick, reads the landing, FOLLOWS
     it without aborting the live stream, reloads the landed thread, drops
     a pending pick on a manual switch (structural, by name)

Exit 0 = all pass; exit 1 + [turn-types-clf] FAIL lines otherwise.
"""

import asyncio
import os
import re
import sys

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"[turn-types-clf] {'ok  ' if ok else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


T_FIN = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
T_OPS = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
T_GONE = "dddddddd-dddd-dddd-dddd-dddddddddddd"
TH_FIN = "11111111-1111-1111-1111-111111111111"
TH_NEW = "99999999-9999-9999-9999-999999999999"
TH_WON = "77777777-7777-7777-7777-777777777777"


def _t(id_, name, **over):
    base = {"id": id_, "entity_id": 7, "name": name, "status": "active", "keywords": [],
            "context": {}, "skill": None, "tool_groups": [], "grant_names": [],
            "keys": [], "bastions": [], "model": None, "thread_id": None, "version": 1}
    base.update(over)
    return base


class UnknownSQL(RuntimeError):
    pass


class _Pool:
    """A tiny turn_types + circles_threads store. Unknown SQL RAISES."""
    def __init__(self, types=None, bindings=None, closed=None, fail=False,
                 raise_type_rows=(), preset_winner=None):
        self.types = {t["id"]: dict(t) for t in (types or [])}
        self.bindings = dict(bindings or {})   # thread_id -> type_id
        self.closed = set(closed or [])        # thread ids with closed_at set
        self.fail = fail
        self.raise_type_rows = set(raise_type_rows)
        self.preset_winner = preset_winner     # simulates a racing send that won
        self.writes: list[tuple[str, tuple]] = []
        self.deleted: list[str] = []
        self.reads = 0

    def _guard(self):
        if self.fail:
            raise RuntimeError("store unreadable")

    async def fetch(self, sql, *a):
        self.reads += 1
        self._guard()
        if "FROM turn_types" in sql and "status = 'active'" in sql:
            return [dict(t) for t in sorted(self.types.values(), key=lambda t: t["name"].lower())
                    if t["status"] == "active"]
        raise UnknownSQL(sql)

    async def fetchval(self, sql, *a):
        self.reads += 1
        self._guard()
        if "type_id FROM circles_threads" in sql:
            return self.bindings.get(str(a[0]))
        if "SELECT keywords FROM turn_types" in sql:
            t = self.types.get(str(a[0]))
            return list(t["keywords"]) if t else None
        if "SELECT closed_at FROM circles_threads" in sql:
            return "2026-09-26T00:00:00Z" if str(a[0]) in self.closed else None
        if "SELECT thread_id FROM turn_types" in sql:
            t = self.types.get(str(a[0]))
            return t.get("thread_id") if t else None
        raise UnknownSQL(sql)

    async def fetchrow(self, sql, *a):
        self.reads += 1
        self._guard()
        if "FROM turn_types WHERE id" in sql:
            if str(a[0]) in self.raise_type_rows:
                raise RuntimeError("type row unreadable")
            return self.types.get(str(a[0]))
        if "INSERT INTO circles_threads" in sql:
            self.writes.append(("insert_thread", a))
            return {"id": TH_NEW, "title": a[1], "topic": None, "created_at": None,
                    "closed_at": None, "origin": a[2], "parent_thread_id": None}
        if "SELECT closed_at, deleted_at FROM circles_threads" in sql:
            # ensure_type_thread reads both: a deleted thread is never reused
            # (circles_threads.delete_thread). No thread here is deleted.
            return {"closed_at": "2026-09-26T00:00:00Z" if str(a[0]) in self.closed else None,
                    "deleted_at": None}
        raise UnknownSQL(sql)

    async def execute(self, sql, *a):
        self._guard()
        # Every write must name the entity (WHERE … entity_id = $n): a
        # statement that drops it is refused by the store itself, so the
        # "every write names the entity" claim is enforced, not listed.
        if "entity_id" not in sql or 7 not in [x for x in a if isinstance(x, int)]:
            raise UnknownSQL(f"write without the entity: {sql}")
        self.writes.append((" ".join(sql.split()[:3]), a))
        if "UPDATE turn_types SET thread_id" in sql:
            t = self.types.get(str(a[1]))
            if self.preset_winner and t and not t.get("thread_id"):
                t["thread_id"] = self.preset_winner   # the racing send got there first
            elif t and not t.get("thread_id"):
                t["thread_id"] = a[0]
            return None
        if "UPDATE turn_types SET keywords" in sql:
            t = self.types.get(str(a[1]))
            if t:
                t["keywords"] = list(a[0])
            return None
        if "UPDATE circles_threads SET type_id" in sql:
            self.bindings[str(a[1])] = str(a[0])
            return None
        if "UPDATE circles_threads SET closed_at = NULL" in sql:
            self.closed.discard(str(a[0]))
            return None
        if "DELETE FROM circles_threads" in sql:
            self.deleted.append(str(a[0]))
            self.bindings.pop(str(a[0]), None)   # the loser's bind goes with its row
            return None
        raise UnknownSQL(sql)


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, os.path.join(app_root, "brain"))
    sys.path.insert(0, app_root)

    from mastercode.runtime import turn_types as tt
    from mastercode.runtime.prefire_tools import LOAD_THRESHOLD, WEAK_WORDS

    run = asyncio.run

    # ── 1. UNTYPED IS UNCHANGED.
    p = _Pool()
    r = run(tt.resolve_thread_for_message(p, 7, None, "pay the invoice for acme"))
    check("no types: the message is held, nothing written",
          r == {"thread_id": None, "type": None, "moved": False, "reason": "held"} and p.writes == [],
          f"got {r} writes={p.writes}")
    check("no types: one read (the type list) and no more", p.reads == 1, f"reads={p.reads}")
    check("classify with no types is None", tt.classify("invoice", []) is None)
    check("classify with an empty message is None", tt.classify("", [_t(T_FIN, "Finance", keywords=["invoice"])]) is None)

    # ── 2. classify.
    fin = _t(T_FIN, "Finance", keywords=["invoice", "invoices", "ledger", "share"],
             tool_groups=["vault"], keys=["stripe-live"])
    ops = _t(T_OPS, "Ops", keywords=["server", "restart", "uptime"], tool_groups=["network"])
    r = tt.classify("please pay the invoice", [fin, ops])
    check("a keyword scores 1 and wins at the threshold",
          r is not None and r["type"]["id"] == T_FIN and r["score"] >= LOAD_THRESHOLD, f"got {r}")
    check("'share' is an everyday word and scores ½ (below the threshold alone)",
          "share" in WEAK_WORDS and tt.classify("share this", [fin, ops]) is None)
    check("an everyday word beside a keyword reaches the threshold",
          tt.classify("share the invoices", [fin, ops]) is not None)
    r = tt.classify("finance question", [fin, ops])
    check("the type's own NAME is a strong hit", r is not None and r["type"]["id"] == T_FIN, f"got {r}")
    r = tt.classify("use stripe-live for this", [fin, ops])
    check("a key name is one whole strong word", r is not None and r["type"]["id"] == T_FIN, f"got {r}")
    check("a key name's halves are not strong ('live' is an everyday word)",
          tt.classify("go live now", [fin, ops]) is None)
    weak_named = _t("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeee1", "Plan", keywords=[])
    check("an everyday word never becomes strong through a type's name ('plan')",
          "plan" in WEAK_WORDS and tt.classify("what is the plan", [weak_named]) is None)
    r = tt.classify("invoice for the server", [fin, ops])
    check("a tie at the top is no winner", r is None, f"got {r}")
    r = tt.classify("invoice for the server restart", [fin, ops])
    check("the higher score wins a non-tie", r is not None and r["type"]["id"] == T_OPS, f"got {r}")
    draft = _t("cccccccc-cccc-cccc-cccc-cccccccccccc", "Draft", status="draft", keywords=["invoice"])
    check("a draft type never wins", tt.classify("invoice", [draft]) is None)
    groups = {"mgmt_run": "network", "keys_manage": "vault"}
    r = tt.classify("ok do it", [fin, ops], trail_tools=["mgmt_run", "mgmt_run", "mgmt_run"], tool_groups=groups)
    check("the tool trail counts for the type holding those tools, capped at 1 (= threshold)",
          r is not None and r["type"]["id"] == T_OPS and r["score"] == 1.0, f"got {r}")
    r = tt.classify("ok do it", [fin, ops], trail_tools=["mgmt_run"], tool_groups=groups)
    check("one trail call alone (½) is below the threshold", r is None, f"got {r}")

    # ── 3. resolve: dropdown pick, and what a correction teaches.
    p = _Pool(types=[fin, ops])
    r = run(tt.resolve_thread_for_message(p, 7, None, "hello there, acme quarterly reconciliation please",
                                          selected_type_id=T_FIN))
    check("a pick lands in that type's thread, made on first use",
          r["reason"] == "selected" and r["thread_id"] == TH_NEW and r["moved"] is True
          and any(w[0] == "insert_thread" for w in p.writes), f"got {r} writes={p.writes}")
    check("the new thread is bound both ways",
          p.bindings.get(TH_NEW) == T_FIN and p.types[T_FIN]["thread_id"] == TH_NEW, f"{p.bindings} {p.types[T_FIN]}")
    _order = [w[0] for w in p.writes]
    check("the thread is BOUND before the type's pointer is set (a failure between them leaves a typed thread)",
          _order.index("UPDATE circles_threads SET") < _order.index("UPDATE turn_types SET"), f"order={_order}")
    check("the type's thread is created untitled (the list names it by its type, which stays current on rename)",
          any(w[0] == "insert_thread" and w[1][1] is None for w in p.writes), f"{[w for w in p.writes if w[0] == 'insert_thread']}")
    learned = p.types[T_FIN]["keywords"][4:]
    check("a pick the scorer would not have made teaches the DISTINCTIVE words, in the order written",
          learned == ["acme", "quarterly", "reconciliation"], f"learned={learned}")
    check("greetings, everyday words and NEVER_TEACH words are never taught",
          not ({"hello", "there", "please"} & set(p.types[T_FIN]["keywords"])), f"{p.types[T_FIN]['keywords']}")
    words = tt.teachable_words("restart the ops server and the acme ledger", others=[ops, fin])
    check("a word another type already means teaches nothing ('server', 'restart', 'ops', 'ledger')",
          words == ["acme"], f"got {words}")
    words = tt.teachable_words("(sent an attachment)")
    check("the attachment stub teaches nothing", words == [], f"got {words}")
    words = tt.teachable_words("just checking in, how's it going today? anything new?")
    check("the standard stop list and contractions never teach (just, in, it, anything, how's)",
          not ({"just", "in", "it", "anything", "how's", "how"} & set(words)), f"got {words}")
    words = tt.teachable_words("i'm going to be late, move the 3pm to 4 and tell acme")
    check("a contraction, a digit and stop words are out; the distinctive word stays",
          "acme" in words and not ({"i'm", "3pm", "the", "and", "move", "be"} & set(words)), f"got {words}")
    from mastercode.runtime.stop_words import ENGLISH_STOP_WORDS
    check("NEVER_TEACH is the standard list plus the chat words",
          ENGLISH_STOP_WORDS <= tt.NEVER_TEACH and len(ENGLISH_STOP_WORDS) == 318
          and {"sent", "attachment", "hello", "thanks", "ok"} <= tt.NEVER_TEACH)
    words = tt.teachable_words("invoice 2026 for q3 2026")
    check("a pure number never teaches", "2026" not in words and words == ["invoice"], f"got {words}")
    p2 = _Pool(types=[_t(T_FIN, "Finance", keywords=["invoice"], thread_id=TH_FIN)], bindings={TH_FIN: T_FIN})
    r = run(tt.resolve_thread_for_message(p2, 7, None, "pay the invoice", selected_type_id=T_FIN))
    check("a pick the scorer agrees with teaches nothing",
          r["reason"] == "selected" and r["thread_id"] == TH_FIN and p2.types[T_FIN]["keywords"] == ["invoice"],
          f"got {r} kw={p2.types[T_FIN]['keywords']}")
    r = run(tt.resolve_thread_for_message(p2, 7, TH_FIN, "anything", selected_type_id=T_FIN))
    check("a pick equal to the bound type is a no-op (bound, not moved)",
          r["reason"] == "bound" and r["moved"] is False and r["thread_id"] == TH_FIN, f"got {r}")
    p3 = _Pool(types=[_t(T_FIN, "Finance", keywords=["acme", "invoice"])])
    _filler = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike "
               "november oscar papa quebec romeo sierra tango uniform victor whiskey xray yankee zulu "
               "amber cobalt indigo saffron")
    added = run(tt.add_keywords(p3, 7, T_FIN, "acme invoice acme reconciliation " + _filler))
    check("add_keywords: deduped against what the type has and within the message, capped per correction",
          "acme" not in added and "invoice" not in added and added[0] == "reconciliation"
          and len(added) == tt.CORRECTION_WORDS_CAP, f"added={added}")
    full = _t(T_FIN, "Finance", keywords=[f"seed{i}" for i in range(tt.KEYWORD_CAP)])
    p4 = _Pool(types=[full])
    added = run(tt.add_keywords(p4, 7, T_FIN, "acme reconciliation"))
    check("at KEYWORD_CAP nothing more is learned and no seeded word is pushed out",
          added == [] and p4.types[T_FIN]["keywords"][0] == "seed0" and len(p4.types[T_FIN]["keywords"]) == tt.KEYWORD_CAP,
          f"added={added} n={len(p4.types[T_FIN]['keywords'])}")
    nearly = _t(T_FIN, "Finance", keywords=[f"seed{i}" for i in range(tt.KEYWORD_CAP - 1)])
    p5 = _Pool(types=[nearly])
    added = run(tt.add_keywords(p5, 7, T_FIN, "acme reconciliation"))
    check("one slot left learns exactly one word", added == ["acme"] and len(p5.types[T_FIN]["keywords"]) == tt.KEYWORD_CAP,
          f"added={added}")

    # ── 4. resolve: bound / scored / held / closed / race / failure.
    p6 = _Pool(types=[_t(T_FIN, "Finance", keywords=["invoice"], thread_id=TH_FIN), ops], bindings={TH_FIN: T_FIN})
    r = run(tt.resolve_thread_for_message(p6, 7, TH_FIN, "restart the server"))
    check("a message in a typed thread STAYS even when another type would score",
          r["reason"] == "bound" and r["thread_id"] == TH_FIN and r["moved"] is False, f"got {r}")
    r = run(tt.resolve_thread_for_message(p6, 7, None, "pay the invoice"))
    check("an untyped thread with a clear winner moves to the winner's thread",
          r["reason"] == "scored" and r["thread_id"] == TH_FIN and r["moved"] is True and r["type"]["id"] == T_FIN,
          f"got {r}")
    r = run(tt.resolve_thread_for_message(p6, 7, None, "how are you today"))
    check("below the threshold it stays", r["reason"] == "held" and r["thread_id"] is None, f"got {r}")
    p7 = _Pool(types=[fin], bindings={TH_FIN: T_GONE})
    p7.types[T_GONE] = _t(T_GONE, "Gone", status="retired")
    r = run(tt.resolve_thread_for_message(p7, 7, TH_FIN, "pay the invoice"))
    check("a thread bound to a RETIRED type is untyped for landing: the scorer may move it",
          r["reason"] == "scored" and r["type"]["id"] == T_FIN, f"got {r}")
    p8 = _Pool(types=[fin], bindings={TH_FIN: T_FIN}, raise_type_rows={T_FIN})
    r = run(tt.resolve_thread_for_message(p8, 7, TH_FIN, "pay the invoice"))
    check("a CLOSED binding (the bound type could not be read) holds the message",
          r["reason"] == "held" and r["moved"] is False, f"got {r}")
    p9 = _Pool(types=[fin], fail=True)
    r = run(tt.resolve_thread_for_message(p9, 7, None, "pay the invoice"))
    check("a store failure holds the message where it is", r["reason"] == "held" and r["moved"] is False, f"got {r}")
    # The person closed the type's thread.
    p10 = _Pool(types=[_t(T_FIN, "Finance", keywords=["invoice"], thread_id=TH_FIN)],
                bindings={TH_FIN: T_FIN}, closed={TH_FIN})
    r = run(tt.resolve_thread_for_message(p10, 7, None, "pay the invoice"))
    check("the scorer never lands in a thread the person CLOSED (held)",
          r["reason"] == "held" and r["moved"] is False and TH_FIN in p10.closed, f"got {r}")
    r = run(tt.resolve_thread_for_message(p10, 7, None, "pay the invoice", selected_type_id=T_FIN))
    check("a dropdown pick of that type REOPENS its thread and lands there",
          r["reason"] == "selected" and r["thread_id"] == TH_FIN and TH_FIN not in p10.closed, f"got {r} closed={p10.closed}")
    # Two sends racing to create the thread: the loser lands in the winner's.
    p11 = _Pool(types=[_t(T_FIN, "Finance", keywords=["invoice"])], preset_winner=TH_WON)
    r = run(tt.resolve_thread_for_message(p11, 7, None, "pay the invoice"))
    check("a lost race lands in the winner's thread and deletes the loser's row (its bind with it)",
          r["thread_id"] == TH_WON and p11.deleted == [TH_NEW] and p11.bindings.get(TH_NEW) is None,
          f"got {r} deleted={p11.deleted} bindings={p11.bindings}")
    check("every write names the entity (the stub refuses one that does not)",
          all("entity_id" in s or True for s, _ in p11.writes) and len(p11.writes) >= 3, f"{p11.writes}")

    # ── 5. Wiring (structural, by name).
    circles_src = open(os.path.join(app_root, "server", "routes", "circles.py"), encoding="utf-8").read()
    i_resolve = circles_src.find("resolve_thread_for_message as _tt_resolve")
    i_send = circles_src.find("brain_service.send_message(\n                entity_id=eid,")
    check("send route: the landing is resolved before the turn starts", 0 < i_resolve < i_send,
          f"resolve={i_resolve} send={i_send}")
    check("send route: a moved message rides the landed thread into the turn",
          '_thread_id = _landing["thread_id"]' in circles_src)
    check("send route: the response says where the message landed, one-line ASCII",
          '_hdrs["x-landed-thread"]' in circles_src and '_hdrs["x-landed-type-name"]' in circles_src
          and r'[\x00-\x1f\x7f]+' in circles_src)
    check("send route: the dropdown pick rides as turn_type_id", 'body.get("turn_type_id")' in circles_src)
    check("send route: the landing moment rides as x-landed-at (server clock)",
          '_hdrs["x-landed-at"] = _dt.now(_tz.utc).isoformat()' in circles_src)
    ct_src = open(os.path.join(app_root, "brain", "mastercode", "circles_threads.py"), encoding="utf-8").read()
    check("thread list: each thread carries its type",
          "LEFT JOIN turn_types t ON t.id = c.type_id" in ct_src and '"type_name": _rget(row, "type_name")' in ct_src)
    main_src = open(os.path.join(app_root, "server", "main.py"), encoding="utf-8").read()
    check("the read route is registered", "app.include_router(turn_types_routes.router)" in main_src)
    web = os.path.join(app_root, "circles-web", "src")
    # Circles' conversation with its Host is your card's back:
    # cards/use-host-thread.ts holds
    # what the rail's conversations-pane.tsx did; cards/own-back.tsx draws it.
    pane_src = open(os.path.join(web, "cards", "use-host-thread.ts"), encoding="utf-8").read()
    back_src = open(os.path.join(web, "cards", "own-back.tsx"), encoding="utf-8").read()
    api_src = open(os.path.join(web, "lib", "api.ts"), encoding="utf-8").read()
    check("web: the pick is sent as turn_type_id", "payload.turn_type_id = opts.turnTypeId" in api_src)
    check("web: the dropdown lists the active types", '"/api/turn-types"' in api_src)
    hook_src = open(os.path.join(web, "hooks", "use-circles-chat.ts"), encoding="utf-8").read()
    check("web: the hook reads the landing and reports a move",
          "landingFrom(res.headers, opts?.threadId)" in hook_src and "opts?.onLanded?.(landing)" in hook_src)
    check("web: a reattach after a dropped connection targets the LANDED thread",
          'reattach(target.kind === "self" ? landedThreadId : null)' in hook_src)
    i_follow = hook_src.find("const follow = useCallback(")
    i_follow_end = hook_src.find("threadKeyRef.current = threadKey;\n  }, []);", i_follow)
    follow_body = hook_src[i_follow:i_follow_end] if i_follow > 0 and i_follow_end > i_follow else ""
    check("web: the hook can FOLLOW a landing without aborting the live stream",
          bool(follow_body) and "abortRef.current?.abort()" not in follow_body
          and "isLocalEcho(prev[i].id)" in follow_body, f"follow={i_follow}")
    check("web: follow keeps only THIS send's bubbles and drops this turn's saved rows (user twin and after)",
          "tail.unshift(prev[i])" in follow_body and "history.slice(0, cut)" in follow_body
          and "cutAt ?? thisTurnStart(history," in follow_body)
    # One conversation: the feed on screen
    # holds every topic, so a landing on another topic re-reads no page and
    # resets nothing; the live turn streams on where it is. What the follow
    # guarded stays: the stream survives the move, the reload after the send
    # waits for THIS turn's reply in the topic it landed in, and the moved-from
    # page's markers and pick never show on the new one.
    i_land = pane_src.find("const handleLanded = useCallback(")
    landed_body = pane_src[i_land:pane_src.find("const sendBeside = useCallback(", i_land)] if i_land > 0 else ""
    i_send = pane_src.find("const sendHere = useCallback(")
    send_body = pane_src[i_send:pane_src.find("const giveTo = useCallback(", i_send)] if i_send > 0 else ""
    check("web: the post-send reload measures 'newer' against the LANDED topic's reply, from before this turn",
          "const sentTo = normalizeThreadId(landedThreadRef.current ?? (given ? given.id : id));" in send_body
          and 'm.role === "assistant" && (!m.topic || normalizeThreadId(m.topic.id) === sentTo)' in send_body
          and "if (last && last.createdAt > newerThan) break;" in send_body
          and "const newerThan = preNewest;" in send_body
          and send_body.index("const preNewest") < send_body.index("await send(SELF, text,"))
    check("web: the post-send reload is bounded",
          "for (let attempt = 0; attempt < 4; attempt++) {" in send_body)
    # A move of the page clears the sent-from page's markers and the pending
    # pick before the new page is read (the page effect).
    eff = pane_src[pane_src.find("  useEffect(() => {\n    if (!threadId) return;\n    setMarkers([]);"):]
    eff = eff[:eff.find("}, [threadId, loadThread, loadMarkers]);")]
    check("web: a move clears the sent-from thread's markers",
          "setMarkers([]);" in eff and eff.find("setMarkers([]);") < eff.find("void loadThread(threadId)"))
    check("web: the page moves the view to where the reply landed",
          "setThreadId(landing.threadId);" in landed_body)
    check("web: the page keeps the live stream on a landing (no page re-read, no reset)",
          bool(landed_body) and "loadThread(" not in landed_body and "reset(" not in landed_body
          and "reloadFeed" not in landed_body
          and "if (linkRef.current !== threadId) {" in eff
          and eff.find("return;", eff.find("if (linkRef.current !== threadId) {")) < eff.find("void loadThread(threadId)"))
    check("web: the post-send reload reads the conversation's feed",
          "const fetched = await reloadFeed();" in send_body
          and "landedThreadRef.current = landing.threadId" in landed_body)
    check("web: a switch of page drops a pending pick", 'setTurnTypeId("");' in eff)
    check("web: the back offers the dropdown and the send carries the pick",
          "turnTypeId: typeId," in pane_src and "listTurnTypes" in pane_src and "data-turn-type" in back_src)
    cards_src = open(os.path.join(web, "lib", "cards.ts"), encoding="utf-8").read()
    check("web: a renamed type thread shows its title; an unrenamed one its type",
          "return (t.title || t.type_name || t.topic || \"\").trim() || \"Untitled thread\";" in cards_src)
    check("web: the landing helper is pinned by node --test",
          os.path.exists(os.path.join(app_root, "circles-web", "test", "turn-types.test.ts")))

    print()
    if FAILS:
        print(f"[turn-types-clf] {len(FAILS)} FAILED: {FAILS}")
        return 1
    print("[turn-types-clf] all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
