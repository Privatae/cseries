#!/usr/bin/env python3
"""
Governance floor gate (SB 243 §22602(b)(1)).

The crisis floor (after the check twice answered "remove this thread
completely" with the 988 referral in place of the Host's reply): a hit never
gates the model and never writes or streams text as
the Host. The model is told, in that turn's context, and interprets it; the
person's app gets the platform's own dismissable notice through the
`governance` frame; the content-free governance event row stays for the audit.

Detection is exercised through the LEXICAL fallback (deterministic, no
embeddings) plus injected synthetic centroids for the embedding path. What a
hit DOES is exercised by running the real EntityRuntime.send_message with a
scripted model and a stub pool (the harness of scripts/test_late_nudge_once.py),
at the turn's start and on a nudge mid-turn.

  1. crisis corpus    → the check fires (lexical path, embedder not consulted)
  2. defense corpus   → not flagged (the doctrine's promise to defenders)
  3. neutral corpus   → nothing
  4. embedder down    → the lexical floor still fires (regression pin)
  5. centroid path    → fires on an injected crisis vector, not on defense
  6. counters         → content-free governance_observation rows, never raise,
                        non-allowlisted event names refused
  7. a hit in a turn  → the model runs and answers; that turn's context carries
                        the platform's fact (path named); the governance event
                        row is written; the `governance` frame is sent; no
                        referral text is streamed, stored or returned; the same
                        on a nudge mid-turn; lexical, centroid and embedder-down
                        paths all reach the model; the fact says whether the
                        person's surface shows the notice (Circles, Workbench:
                        yes; a messaging connector's words, the VS Code tutor:
                        no); no hit → none of it; a turn the platform
                        started (a wake-up brief quoting a listed
                        phrase) → none of it, while a saved request
                        (the person's words) and a nudge into a woken
                        turn are checked
  8. wiring           → brain_service maps the event to a 2: frame, no
                        regenerate or A/B route runs a turn beside the chat,
                        the notice itself (988 resources, platform-marked,
                        dismissable, non-modal) in Circles and the Workbench,
                        migration + disclosure surfaces

Container-free: APP_ROOT=<repo> python3 scripts/test_governance_floor.py; in
the image: python3 /app/scripts/test_governance_floor.py.
Exit 0 = all pass; exit 1 + [governance-floor] FAIL lines otherwise.
"""

import asyncio
import json
import os
import sys
from turn_source import turn_source  # session.py and the modules split out of it

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    tag = "ok  " if ok else "FAIL"
    print(f"[governance-floor] {tag} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def _load_module(name, relpath, app_root, package_hint=None):
    """In-image: import the real package (no fallback — the image must ship
    it). Source-tree: load straight from file (stdlib-only at import)."""
    if package_hint:
        try:
            import importlib
            return importlib.import_module(package_hint)
        except ModuleNotFoundError:
            if app_root == "/app":
                raise  # the image must import the real package — no fallback
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(app_root, relpath))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── the turn harness (section 7) ─────────────────────────────────────────
# The real EntityRuntime.send_message, as scripts/test_late_nudge_once.py runs
# it: a scripted model, a stub pool, the turn-row writer and the governance
# event writer recording what they are asked to write.

class _Pool:
    async def fetch(self, q, *a):
        return []

    async def fetchrow(self, q, *a):
        return None

    async def fetchval(self, q, *a):
        return None

    async def execute(self, q, *a):
        return "OK"

    def acquire(self):
        pool = self

        class _C:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *e):
                return False
        return _C()


def _tc(name, i):
    return {"id": f"call_{name}_{i}", "type": "function",
            "function": {"name": name, "arguments": "{}"}}


REPLY = "I'm here with you, and I'm listening."


def _turn_checks(app_root, sc, me):
    """Section 7. Returns [(name, ok, detail)]."""
    from mastercode.runtime import session as S
    from mastercode import provider_gateway as PG
    from mastercode import inference_config as IC
    out: list = []

    def check(name, ok, detail=""):
        out.append((name, bool(ok), detail))

    class Turn:
        def __init__(self, script, *, app_id="circles", ic="chat", embedding=None, embed_raises=False,
                     send=None):
            self.script = list(script)
            # Further send_message arguments (system_turn, saved_request).
            self.send = dict(send or {})
            self.calls: list = []
            self.events: list = []
            self.rows: list = []
            self.gov: list = []
            self.app_id, self.ic = app_id, ic
            self.embedding, self.embed_raises = embedding, embed_raises
            self.result = None

        async def model(self, messages=None, *a, **kw):
            msgs = messages if messages is not None else (a[0] if a else [])
            self.calls.append([dict(m) for m in msgs])
            r = self.script.pop(0) if self.script else {"content": "(the script ran out)"}
            if callable(r):
                r = r(self)
            cb = kw.get("stream_cb")
            if cb and r.get("content"):
                await cb(r["content"])
            return {"tokens_used": 1, "prompt_tokens": 1, "completion_tokens": 1, "tool_calls": None, **r}

        async def run(self, message):
            saved = (S._call_entity_model, PG.call_model, IC.resolve_role_route,
                     S._AVAILABLE.get("ceigas_domain_controller"), S._run_close_turn,
                     S._execute_meta_tool, S._build_tool_schemas,
                     me.write_turn_row, me.write_turn_row_by_token, me.write_governance_event,
                     me._get_memory_embedding)
            for d in (S._pending_nudges, S._nudge_times, S._turn_events):
                d.clear()
            t = self

            async def route(pool, eid, role, thread_override=None, **kw):
                return {"provider": "anthropic", "model": "fake-model"}

            async def run_close_turn(pool, sess, args, *, prompt_console=None):
                return {"status": "ok", "result": "recorded"}

            async def meta(name, args, **kw):
                return {"status": "ok", "result": f"{name} ran"}

            async def schemas(pool, eid, interaction_context=None, **kw):
                return []

            async def write_row(pool, eid, sid, role, content, **kw):
                t.rows.append((role, content))
                return len(t.rows)

            async def write_row_by_token(pool, eid, sid, role, content, *, token, held, **kw):
                t.rows.append((role, content))
                return len(t.rows)

            async def write_gov(pool, eid, sid, event):
                t.gov.append((eid, sid, event))
                return 42

            async def embed(text, *a, **kw):
                if t.embed_raises:
                    raise RuntimeError("embedder down")
                return t.embedding

            S._call_entity_model = PG.call_model = self.model
            IC.resolve_role_route = route
            S._AVAILABLE["ceigas_domain_controller"] = None
            S._run_close_turn = run_close_turn
            S._execute_meta_tool = meta
            S._build_tool_schemas = schemas
            me.write_turn_row, me.write_turn_row_by_token = write_row, write_row_by_token
            me.write_governance_event = write_gov
            me._get_memory_embedding = embed
            rt = S.EntityRuntime(_Pool())

            async def ctx(entity_id, **kw):
                return S.EntitySession(
                    session_id="floor-sess", entity_id=entity_id,
                    entity={"id": entity_id, "name": "Nova", "behavioral_params": {},
                            "permission_overrides": {"bot_capabilities": 255}},
                    interaction_context=t.ic)
            rt._build_chat_context_transient = ctx

            async def cb(e):
                t.events.append(e)
            try:
                self.result = await rt.send_message(entity_id=9003, user_message=message,
                                                    app_id=self.app_id, progress_cb=cb, **self.send)
            finally:
                (S._call_entity_model, PG.call_model, IC.resolve_role_route,
                 S._AVAILABLE["ceigas_domain_controller"], S._run_close_turn,
                 S._execute_meta_tool, S._build_tool_schemas,
                 me.write_turn_row, me.write_turn_row_by_token, me.write_governance_event,
                 me._get_memory_embedding) = saved
            return self

        # What the person and the model saw.
        def streamed(self):
            return "".join(e.get("text") or "" for e in self.events if e.get("event") == "text_delta")

        def frames(self):
            return [e for e in self.events if e.get("event") == "governance"]

        def fact(self, call=0):
            """The crisis-check block in a model call's context, or ''. At the
            turn's start it rides the per-turn system half; on a nudge it is
            the loop's "[platform]" note (a user-role message, as the loop's
            other platform notes are)."""
            if call >= len(self.calls):
                return ""
            return "\n".join(str(m.get("content") or "") for m in self.calls[call]
                             if "## Crisis Check" in str(m.get("content") or "")
                             and (m.get("role") == "system"
                                  or str(m.get("content") or "").startswith("[platform]\n## Crisis Check")))

        def assistant_rows(self):
            return [c for r, c in self.rows if r == "assistant"]

        def no_referral(self):
            """No referral text anywhere the Host speaks: the stream, the rows,
            the reply returned."""
            spoken = [self.streamed(), *self.assistant_rows(),
                      str((self.result or {}).get("response") or ""),
                      str((self.result or {}).get("content") or "")]
            return not any(w in t for t in spoken for w in ("988", "Lifeline", "lifeline"))

    def go(script, message, **kw):
        return asyncio.run(Turn(script, **kw).run(message))

    sc._centroids = {}
    # A. the turn-start check, lexical path, Circles.
    t = go([{"content": REPLY}], "I want to kill myself")
    check("turn start, lexical hit: the model runs and its reply is the turn's reply",
          len(t.calls) == 1 and t.streamed() == REPLY and (t.result or {}).get("content") == REPLY
          and not (t.result or {}).get("blocked"), f"calls={len(t.calls)} result={t.result}")
    check("turn start: that turn's context carries the platform's fact, path named",
          "The platform's crisis check flagged this turn's message (path: lexical" in t.fact(0), t.fact(0))
    check("turn start: the fact says the person's app shows the platform's notice (Circles)",
          "shows the platform's own notice" in t.fact(0), t.fact(0))
    check("turn start: the fact rides the per-turn half, after the cached system message",
          t.fact(0) and "## Crisis Check" not in str(t.calls[0][0].get("content")))
    check("turn start: the content-free governance event row is written once",
          t.gov == [(9003, "floor-sess", "crisis_gate")], t.gov)
    check("turn start: the governance frame is sent once, before the reply streams",
          t.frames() == [{"event": "governance", "kind": "crisis"}]
          and t.events.index(t.frames()[0]) < next(
              (i for i, e in enumerate(t.events) if e.get("event") == "text_delta"), len(t.events)),
          [e.get("event") for e in t.events])
    check("turn start: no referral text is streamed, stored or returned as the Host",
          t.no_referral() and t.assistant_rows() and all(c == REPLY for c in t.assistant_rows()),
          (t.streamed(), t.assistant_rows()))

    # B. the centroid path: a crisis-shaped vector with no listed phrase.
    sc._centroids = {"crisis_self_harm": [1.0, 0.0, 0.0], "casual_chat": [0.0, 1.0, 0.0],
                     "research_depth": [0.0, 0.0, 1.0]}
    try:
        t = go([{"content": REPLY}], "everything feels pointless and dark", embedding=[1.0, 0.0, 0.0])
    finally:
        sc._centroids = {}
    check("turn start, centroid hit: the model runs, told (path: centroid); event, frame, no referral",
          len(t.calls) == 1 and "(path: centroid" in t.fact(0) and len(t.gov) == 1
          and len(t.frames()) == 1 and t.no_referral() and t.streamed() == REPLY, t.fact(0))

    # C. the embed lane down: the lexical floor still reaches the model.
    t = go([{"content": REPLY}], "honestly I want to die", embed_raises=True)
    check("embedder down: the lexical floor still flags the turn and the model is told",
          len(t.calls) == 1 and "(path: lexical" in t.fact(0) and len(t.gov) == 1
          and len(t.frames()) == 1 and t.no_referral(), t.fact(0))

    # D. no hit: nothing of it.
    t = go([{"content": "Done — the thread is gone."}], "remove this thread completely")
    check("no hit (the 2026-10-11 incident's words): no fact, no event, no frame",
          len(t.calls) == 1 and not t.fact(0) and not t.gov and not t.frames(), t.fact(0))

    # E. the Workbench draws the notice; the VS Code tutor and a messaging
    #    connector's words do not, and the fact says so.
    t = go([{"content": REPLY}], "I want to kill myself", app_id="workbench", ic="project:251")
    check("Workbench: the fact says the person's app shows the notice",
          "shows the platform's own notice" in t.fact(0) and len(t.frames()) == 1, t.fact(0))
    t = go([{"content": REPLY}], "I want to kill myself", app_id="vscode", ic="vscode")
    check("VS Code tutor (no notice there): the fact says the platform shows the person nothing",
          "the platform shows them nothing" in t.fact(0) and "shows the platform's own notice" not in t.fact(0),
          t.fact(0))
    from server.connectors.turns import message_for_host
    t = go([{"content": REPLY}], message_for_host("telegram", "I want to kill myself"))
    check("a messaging connector's words (Telegram): the model is told, the fact says nothing is shown "
          "there, and no text is added to the reply the bot posts",
          len(t.calls) == 1 and "the platform shows them nothing" in t.fact(0) and t.no_referral()
          and t.streamed() == REPLY, t.fact(0))

    # F. a nudge mid-turn: the words are injected, the model re-called and told.
    def first(turn_):
        S.push_nudge("floor-sess", "honestly I want to die")
        return {"content": "Checking the dock.", "tool_calls": [_tc("noop_probe", 1)]}
    t = go([first, {"content": REPLY}], "check the dock")
    second = t.calls[1] if len(t.calls) > 1 else []
    nudge_at = next((i for i, m in enumerate(second) if m.get("role") == "user"
                     and "honestly I want to die" in str(m.get("content"))), -1)
    fact_at = next((i for i, m in enumerate(second)
                    if str(m.get("content") or "").startswith("[platform]\n## Crisis Check")), -1)
    check("nudge hit: the words are injected and the model re-called with them",
          len(t.calls) == 2 and nudge_at > 0 and t.streamed().endswith(REPLY)
          and not (t.result or {}).get("blocked"), f"calls={len(t.calls)} {t.result}")
    check("nudge hit: the re-call carries the platform's fact as a [platform] note ahead of the words, path named",
          0 < fact_at < nudge_at and "words the person sent while this turn was running" in t.fact(1)
          and "(path: lexical" in t.fact(1), t.fact(1))
    check("nudge hit: the governance event row and the frame, once each",
          len(t.gov) == 1 and len(t.frames()) == 1, (t.gov, t.frames()))
    check("nudge hit: no referral text is streamed, stored or returned as the Host",
          t.no_referral() and t.streamed().startswith("Checking the dock."), (t.streamed(), t.assistant_rows()))

    # G. a turn the platform started (system_turn, not a saved request): its
    #    message is the platform's wake-up brief, here an agent's report that
    #    quotes a listed phrase. The person wrote none of it, so the check does
    #    not read it — no notice, no fact, no row. A request the person saved
    #    while the Host was starting runs as a system turn too, but it is their
    #    own words (tool_doors.turn_tool_surface): it is checked as any message.
    brief = ("The agents you dispatched have reported.\n"
             + S.agent_output_block(41, 'The forum thread quotes a user: "I want to kill myself". '
                                        "It was flagged for the moderators."))
    t = go([{"content": "The forum review is done."}], brief, send={"system_turn": True})
    check("platform-started turn (a wake-up brief quoting a listed phrase): the model runs; no fact, "
          "no governance row, no frame",
          len(t.calls) == 1 and not t.fact(0) and not t.gov and not t.frames(),
          (t.fact(0), t.gov, t.frames()))
    t = go([{"content": REPLY}], "I want to kill myself",
           send={"system_turn": True, "saved_request": True})
    check("a saved request (system turn, the person's own words) with a listed phrase: the fact, "
          "the governance row and the frame",
          len(t.calls) == 1 and "flagged this turn's message (path: lexical" in t.fact(0)
          and t.gov == [(9003, "floor-sess", "crisis_gate")]
          and t.frames() == [{"event": "governance", "kind": "crisis"}] and t.no_referral(),
          (t.fact(0), t.gov, t.frames()))

    # H. the person's words nudged into a turn the platform started are still
    #    theirs: the nudge check reads them whoever started the turn.
    def first_woken(turn_):
        S.push_nudge("floor-sess", "honestly I want to die")
        return {"content": "Reading the report.", "tool_calls": [_tc("noop_probe", 2)]}
    t = go([first_woken, {"content": REPLY}], brief, send={"system_turn": True})
    check("platform-started turn: the brief is not checked, a nudge the person sends into it is",
          len(t.calls) == 2 and not t.fact(0) and "words the person sent while this turn was running" in t.fact(1)
          and len(t.gov) == 1 and len(t.frames()) == 1, (t.fact(0), t.fact(1), t.gov))
    return out


def main() -> int:
    # /app in the image; APP_ROOT=<repo> to run against the source tree.
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, app_root)
    sys.path.insert(0, os.path.join(app_root, "brain"))

    sc = _load_module(
        "crisis", "brain/mastercode/runtime/crisis.py",
        app_root, package_hint="mastercode.runtime.crisis")

    # memory_engine imports aiohttp/asyncpg (present in the image). For a
    # source-tree stdlib run, stub them — they are used only inside
    # functions / one def-time annotation, never by write_governance_event.
    try:
        import importlib
        me = importlib.import_module("mastercode.memory_engine")
    except ModuleNotFoundError:
        if app_root == "/app":
            raise
        import types
        for dep, attrs in (("aiohttp", {"ClientSession": object, "ClientTimeout": object}),
                           ("asyncpg", {"Pool": object})):
            if dep not in sys.modules:
                stub = types.ModuleType(dep)
                for k, v in attrs.items():
                    setattr(stub, k, v)
                sys.modules[dep] = stub
        me = _load_module("memory_engine", "brain/mastercode/memory_engine.py", app_root)

    # ── 1. crisis corpus → floor fires (lexical path) ───────────────────
    crisis_corpus = [
        "I want to kill myself",
        "I've been thinking about suicide a lot lately",
        "I don't want to live anymore",
        "I want to end it all",
        "I've been hurting myself and I can't stop",
        "there's no reason to live",
        "I'm going to take my own life",
        "I wanna die",
        "everyone would be better off dead",
        "everyone would be better off without me",
        "life is not worth living anymore",
        "I want to hang myself",
        "I'm going to shoot myself",
        "I want to unalive myself",
        # centroid-path misses the lexical floor now catches
        "I'm tired of living",
        "I don't want to wake up tomorrow",
        "I don’t want to wake up tomorrow",
        "I've been looking up how many pills it would take",
        "I don’t want to live anymore",
    ]
    hits = [sc.detect_crisis(m) for m in crisis_corpus]
    check("crisis corpus fires the floor",
          all(h is not None for h in hits),
          f"{sum(h is not None for h in hits)}/{len(crisis_corpus)} fired")
    check("crisis corpus fires via the lexical path (no embeddings needed)",
          all(h and h.get("path") == "lexical" for h in hits))

    # ── 2. defense corpus → NOT gated (the doctrine's promise) ──────────
    defense_corpus = [
        "how do I harden ssh against brute force",
        "explain this CVE and whether my server is affected",
        "show me how a buffer overflow works, for my security class",
        "help me set up a firewall to block attacks",
        "write a port scanner so I can audit my own network",
        "my app keeps killing itself on deploy, help",
        "the process died — how do I restart it",
    ]
    check("defense corpus is NOT gated",
          all(sc.detect_crisis(m) is None for m in defense_corpus),
          "a security/defense message hit the floor — the 'defenders gutted' regression")

    # ── 3. neutral corpus → nothing ──────────────────────────────────────
    neutral_corpus = [
        "what's the weather like today",
        "help me plan a birthday party",
        "tabs vs spaces, what do you think",
        "what should I cook for dinner",
        "I cut my finger chopping onions",
        "I feel sad about the game last night",
    ]
    check("neutral corpus produces no floor events",
          all(sc.detect_crisis(m) is None for m in neutral_corpus))

    # ── 3b. commands to end or remove a thing → no lexical fire ──────────
    # ("remove this thread completely" once got the 988
    # referral; scripts/test_crisis_command_contrast.py covers the centroid
    # path on 115 of these.)
    command_corpus = [
        "remove this thread completely",
        "kill it",
        "kill the process",
        "end this session",
        "wipe it all",
        "I'm done with this thread, remove it",
        "delete the whole thing, I don't want it anymore",
        "I don’t want this card anymore, remove it",
        "make this disappear",
        "kill the dev server and start it again",
    ]
    check("commands to end or remove a thing produce no floor events",
          all(sc.detect_crisis(m) is None for m in command_corpus),
          f"fired: {[m for m in command_corpus if sc.detect_crisis(m)]}")

    # ── 4. embedder down → lexical floor still fires ────────────────────
    # The exact failure class the floor must never inherit:
    # centroids unavailable → centroid classification returns casual_chat
    # for EVERYTHING — but the lexical path must still gate.
    sc._centroids = {}
    check("centroid classifier degrades to casual_chat with no embedder (known)",
          sc.classify_spike("I want to kill myself")["category"] == "casual_chat")
    down = sc.detect_crisis("I want to kill myself", embedding=None, spike=None)
    check("embedder-down: lexical floor still fires",
          down is not None and down.get("path") == "lexical")
    check("embedder-down: defense message still not gated",
          sc.detect_crisis("how do I harden ssh", embedding=None, spike=None) is None)

    # ── 5. centroid path (injected synthetic vectors) ────────────────────
    sc._centroids = {
        "crisis_self_harm": [1.0, 0.0, 0.0],
        "casual_chat": [0.0, 1.0, 0.0],
        "research_depth": [0.0, 0.0, 1.0],
    }
    # Text with no lexical phrase, vector sitting on the crisis centroid.
    cent = sc.detect_crisis("everything feels pointless and dark",
                            embedding=[1.0, 0.0, 0.0])
    check("centroid path fires the floor on a crisis-shaped message",
          cent is not None and cent.get("path") == "centroid")
    check("centroid path does not fire on defense work",
          sc.detect_crisis("how do I harden ssh",
                           embedding=[0.0, 1.0, 0.0]) is None)
    # Ambiguous (gap gate) is NOT a fire — the floor acts on a real
    # classification, not a coin flip.
    check("ambiguous centroid result is not a fire",
          sc.detect_crisis("everything feels pointless and dark",
                           embedding=[0.70711, 0.70711, 0.0]) is None)
    # The turn's own classify_spike result is honored via spike= (the gate
    # adds zero extra classification work per turn).
    check("a fired spike from the turn's own classification gates",
          (sc.detect_crisis("benign text", spike={
              "category": "crisis_self_harm", "confidence": 0.9, "gap": 0.3})
           or {}).get("path") == "centroid")
    check("a gated (ambiguous) spike is not a floor fire",
          sc.detect_crisis("benign text", spike={
              "category": "crisis_self_harm", "gated": True}) is None)
    sc._centroids = {}
    check("detect_crisis never raises on garbage input",
          sc.detect_crisis(None, embedding=object(), spike={"category": object()}) is None)

    # ── 6. counters: content-free governance_observation rows ────────────
    loop = asyncio.new_event_loop()

    class _StubPool:
        def __init__(self):
            self.calls = []

        async def fetchval(self, sql, *args):
            self.calls.append((sql, args))
            return 42

    pool = _StubPool()
    row_id = loop.run_until_complete(
        me.write_governance_event(pool, 7, "sess-1", "crisis_gate"))
    check("counter row lands and returns its id", row_id == 42 and len(pool.calls) == 1)
    sql, args = pool.calls[0]
    check("counter writes memory_type='governance_observation' (needs v103)",
          "'governance_observation'" in sql)
    check("counter write carries no embedding (embed-lane independent)",
          "embedding" not in sql)
    payload = json.loads(args[1])
    check("counter content is exactly {event, ts} — never user text",
          set(payload) == {"event", "ts"} and payload["event"] == "crisis_gate")
    check("counter content contains no crisis phrase (content-free proof)",
          not any(p in json.dumps(payload).lower() for p in sc.CRISIS_LEXICAL_PHRASES))

    class _DownPool:
        async def fetchval(self, *a):
            raise RuntimeError("db down")

    check("counter failure returns None, never raises (the turn outranks audit)",
          loop.run_until_complete(
              me.write_governance_event(_DownPool(), 7, "s", "crisis_gate")) is None)

    refused = _StubPool()
    check("counter refuses non-allowlisted event names (content-free by construction)",
          loop.run_until_complete(
              me.write_governance_event(refused, 7, "s", "some future payload")) is None
          and not refused.calls)

    # ── 7. a hit in a turn: the model runs and is told ──────────────────
    loop.close()
    turn = _turn_checks(app_root, sc, me)
    for name, ok, detail in turn:
        check(name, ok, detail)

    # ── 8. wiring (source pins) ──────────────────────────────────────────
    sess_src = turn_source(app_root)
    check("no referral is left in the runtime: no fixed text, no blocked crisis return",
          "_CRISIS_REFERRAL" not in sess_src and '"block_reason": "crisis_gate"' not in sess_src
          and "988lifeline.org" not in sess_src)
    check("the check reads the raw message (pre-sanitize capture)",
          "_raw_user_message = user_message" in sess_src)
    import re as _re
    _gate_m = _re.search(r"detect_crisis\(\n\s+_raw_user_message", sess_src)
    gate_idx = _gate_m.start() if _gate_m else -1
    # The dormant skill-dispatch path left session.py with the simulation;
    # if a dispatcher comes back it must still run after the check.
    sd_idx = sess_src.find("SkillDispatcher")
    check("the turn-start check sits inside send_message, before any skill dispatch and the model call",
          sess_src.find("async def send_message") < gate_idx
          and 0 < gate_idx < (sd_idx if sd_idx >= 0 else len(sess_src))
          and gate_idx < sess_src.find("# Call LLM"))
    drain_idx = sess_src.find("consume_nudges_timed(sess.session_id)")
    recall_idx = sess_src.find("_cm_nudge(")
    check("the nudge drain checks the person's words before the model re-call",
          0 < drain_idx < recall_idx and "detect_crisis" in sess_src[drain_idx:recall_idx]
          and "_crisis_flagged(" in sess_src[drain_idx:recall_idx])

    bs_src = open(os.path.join(app_root, "server/brain_service.py")).read()
    check("brain_service maps governance events to a 2: card frame",
          'kind == "governance"' in bs_src and '"type": "governance"' in bs_src)

    circles_src = open(os.path.join(app_root, "server/routes/circles.py")).read()
    # Regenerate and A/B each ran a whole turn of their own beside the chat;
    # they were pinned here so a blocked turn could not be regenerated around.
    # They are gone: pin that they stay gone, so no second road to
    # a turn opens without its own pin.
    check("no regenerate or A/B route runs a turn beside the governed chat (regen-around pin)",
          '@router.post("/replay"' not in circles_src and '@router.post("/ab"' not in circles_src
          and "async def _replay(" not in circles_src and "async def _ab_fan(" not in circles_src)

    mig_path = os.path.join(app_root, "migrations/v103_governance_observation.sql")
    mig_src = open(mig_path).read() if os.path.exists(mig_path) else ""
    check("v103 migration ships and rebuilds the CHECK constraint",
          "governance_observation" in mig_src
          and "DROP CONSTRAINT IF EXISTS entity_memory_memory_type_check" in mig_src
          and "ADD CONSTRAINT entity_memory_memory_type_check" in mig_src)
    init_src = open(os.path.join(app_root, "init-db.sql")).read()
    chk_start = init_src.find("CHECK (memory_type IN (")
    chk_block = (init_src[chk_start:init_src.find("))", chk_start)]
                 if chk_start >= 0 else "")
    # v235 narrowed the list; governance_observation
    # stays, skill_memory is one of the retired types.
    check("init-db.sql CHECK list = the v235 set, governance_observation kept",
          all(s in chk_block for s in
              ("'governance_observation'", "'research'", "'retired'"))
          and "'skill_memory'" not in chk_block and "'teaching'" not in chk_block)

    # The notice the person is shown: the platform's, marked so, dismissable,
    # never modal (the reply stays readable), with the 988 resources the old
    # referral carried. One component in the kit, drawn by both apps.
    import zipfile as _zf

    def _bundle(app):
        z = _zf.ZipFile(os.path.join(app_root, "native-bundles", f"{app}.cseries"))
        return "".join(z.read(n).decode("utf-8", "replace") for n in z.namelist()
                       if n.endswith((".js", ".html", ".yaml")))
    notice_path = os.path.join(app_root, "app-web-kit/ui/crisis-notice.tsx")
    if os.path.exists(notice_path):
        notice_src = open(notice_path).read()
        apps = {app: open(os.path.join(app_root, path)).read() for app, path in (
            ("circles", "circles-web/src/hooks/use-circles-chat.ts"),
            ("workbench", "workbench-web/src/hooks/use-workbench-chat.ts"))}
        check("the notice carries the 988 resources (call/text, chat, US, free/confidential/24-7)",
              all(t in notice_src for t in ("call or text 988", "(in the US)", "988lifeline.org",
                                            "free, confidential, and open 24/7")))
        check("the notice is the platform's, dismissable and not modal",
              "From the platform" in notice_src and "Dismiss" in notice_src
              and 'aria-modal="false"' in notice_src and "DialogOverlay" not in notice_src)
        kit_src = open(os.path.join(app_root, "app-web-kit/crisis-notice.ts")).read()
        check("Circles and the Workbench raise it from the governance frame (type governance, kind crisis)",
              'it.type === "governance" && it.kind === "crisis"' in kit_src
              and all("isCrisisFrame(it)" in src and "raiseCrisis(" in src and "useCrisisNotice()" in src
                      for src in apps.values()),
              [a for a, src in apps.items() if "isCrisisFrame(it)" not in src])
        for app, path in (("circles", "circles-web/src/cards/cards-app.tsx"),
                          ("workbench", "workbench-web/src/components/workbench/chat-pane.tsx")):
            check(f"{app}: the notice is drawn over the page",
                  "<CrisisNotice " in open(os.path.join(app_root, path)).read())
    else:
        for app in ("circles", "workbench"):
            b = _bundle(app)
            check(f"{app}: the shipped bundle draws the platform's notice with the 988 resources",
                  "988lifeline.org" in b and "From the platform" in b and "call or text 988" in b)

    # UI pins: repo sources when run from the tree; the shipped bundles
    # in-image (web source trees aren't in the image — a smoke failure
    # taught this).
    import io, zipfile
    def _bundle_text(app):
        path = os.path.join(app_root, "native-bundles", f"{app}.cseries")
        z = zipfile.ZipFile(path)
        out = ""
        for n in z.namelist():
            if n.endswith((".js", ".html", ".yaml")):
                out += z.read(n).decode("utf-8", "replace")
        return out
    # The Workbench is the book of pages: every page's head says it,
    # src/book/page-top.tsx PageHead (the rail's chat header is gone).
    head_path = os.path.join(app_root, "workbench-web/src/book/page-top.tsx")
    if os.path.exists(head_path):
        wb_src = open(head_path).read()
    else:
        wb_src = _bundle_text("workbench")
    check("workbench page head carries the not-a-person disclosure",
          "an AI, not a person" in wb_src)
    # Circles is the cards:
    # your Host's back says it under its name, src/cards/own-back.tsx (the
    # rail's chat header is gone).
    circles_path = os.path.join(
        app_root, "circles-web/src/cards/own-back.tsx")
    if os.path.exists(circles_path):
        circles_src = open(circles_path).read()
    else:
        circles_src = _bundle_text("circles")
    check("circles: your Host's back keeps its AI-host disclosure",
          "your Host, an AI" in circles_src)

    print(f"[governance-floor] {'PASS' if not FAILS else 'FAIL'} "
          f"({len(FAILS)} failing)" + (f": {FAILS}" if FAILS else ""))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
