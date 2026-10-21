#!/usr/bin/env python3
"""Gate: every door that reads a person's words, or a message a connector or an
outside client sends, before the turn does, cuts their secret shapes first
(server/secret_cut.py), as runtime/session.send_message cuts a
message before anything of the turn sees it.

Each door runs for real against the kernel's schema. Only the endpoints behind
the doors are fakes, and each records what it is handed: the Host's embedding
endpoint (memory_engine._embed_text_config_driven, the dialer every text embed
of the Host's goes through, with _entity_embedding_config saying a model is set
up), the media model's text tower (media_engine.embed_media), the code index's
embed and rerank (codesearch.bridge_embed), and the Host's chat model
(runtime/tools._call_entity_model, and provider_gateway.call_model, which
the factory's planner dials; no door before a connector message's turn dials
either: the Host picks its thread in the turn). A made-up key
in a shape brain/mastercode/secret_shapes.json knows is planted in what is sent.
Each "planted key" check is that the endpoint was called (a word only that
door's input carries reached it) and never with the key.

  S  the Start dialog's search (POST /api/projects/search): the embedding call.
     A search with a key pasted in finds the pages the same words find without
     it. A new project the dialog makes from what was typed (POST
     /api/projects) keeps no key in its name or description, so the project
     search's refresh, which embeds them, is never handed it.
  D  the Context Manager's search (POST /api/brain/context/search): the
     embedding call and the media model's text tower.
  M  the Memory app's searches (GET /api/brain/memory/semantic and
     GET /api/brain/memory): recall's embedding call.
  U  the one index (POST /api/search): every store's embedding call.
  K  code search (GET /api/codesearch/search): the embed and the rerank.
  O  words with no hard shape reach each search door's endpoint, and shape's
     and recall's, exactly as typed.
  C  a connector message no thread claims: no model call places it; the
     placement's search (the embedding call) is never handed the key, the
     thread made for it is named without it, and the turn it starts reads its
     words and the open threads its frame lists (where the Host picks) without
     it. /new <text> names the thread without it, so the next message's frame,
     which lists that thread, never carries it either. F: a forward, its
     author's name and id carrying keys, through the same doors.
  X  the Synaptive MCP doors an outside client calls: shape(message) and
     recall(query) (the embedding call), and signal(user_message,
     model_response) (the embedding call and the rows kept). order_software
     is not checked: the /mcp `factory` scope it needed is retired (no mint
     path issued it).
  H  the helper: hard shapes become the turn's own markers (the text
     secret_shapes.redact returns, the call session.send_message makes), soft
     shapes and plain words stay as they are, a search drops the markers too,
     and a screen that fails leaves the words and logs, as
     session.send_message does.

The CEIGAS gate in front of the Synaptive doors is allowed by a stub (no domain
controller runs here, and it is not what this gate is about); the MCP bearer is
a real token row checked by the real validator. The connector's turn body is a
scripted brain_service._send_message_inner behind the real send_message and
turn registry, as scripts/test_workbench_index_pg.py runs the Index's.

Run against another tree (APP_ROOT=<that tree>) it reports which checks that
tree fails: on the tree before this change every planted-key check fails.

Fresh clone of the kernel schema on TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN,
else a throwaway cseries-postgres through docker.

  PYTHONPATH=brain python3 scripts/test_secret_cut_doors_pg.py
"""
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import pathlib
import subprocess
import sys
import types

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
for p in (str(ROOT / "brain"), str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)
import asyncpg  # noqa: E402

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate runs the doors against a real database.")
    sys.exit(2)

FAILED = 0
KEY_FAILED = 0
E = 7
os.environ["ENTITY_ID"] = str(E)     # synaptive_handler._entity_id reads it
ROUTE = {"provider": "anthropic", "model": "claude-opus-5-5"}

# Made-up keys in shapes secret_shapes.json knows (hard), and the run of each
# that must never reach an endpoint.
ANTHROPIC = "sk-ant-api03-" + "Wv5" * 20
GITHUB = "ghp_" + "Qz7" * 12
AWS = "AKIA" + "QZ7X" * 4
BODIES = {ANTHROPIC: "Wv5Wv5Wv5", GITHUB: "Qz7Qz7Qz7", AWS: "QZ7XQZ7XQZ7X"}
# Words with no hard shape: a soft assignment, a random-looking token (soft),
# a hash (nothing).
SOFT_ASSIGN = "api_key: hunter2hunter2xyz"
SOFT_TOKEN = "Zx8Qp2Lm9Rt4Vw7Ny3Kb6Hc1Jd5Fg0Sa"
HASH = "4b24b79b8a1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e"

BEARER = "mcp_" + "g7" * 20
CALLER_ACCOUNT = "4242"


def check(label, cond, detail=""):
    global FAILED, KEY_FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {detail}"))
    if not cond:
        FAILED += 1
        if "planted key" in label:
            KEY_FAILED += 1


def load(name):
    """A module of the tree under test, or the exception that stopped it."""
    try:
        return __import__(name, fromlist=["_"])
    except Exception as e:  # noqa: BLE001
        return e


# ── the endpoints: fakes that record what they are handed ──────────────────

SENT: list = []        # (endpoint, text), in the order the endpoints were handed them


async def fake_embed_config(pool, entity_id):
    return "fake", "fake-embed"


async def fake_embed_text(text, model, **kw):
    SENT.append(("embedding endpoint", str(text)))
    return None


async def fake_media_target(pool, entity_id):
    return "media_embedding", 1024, "fake-media"


async def fake_media_config(pool, entity_id):
    return {"media_embedding_model": "fake-media"}


async def fake_embed_media(cfg, *, image=None, text=None, **kw):
    SENT.append(("media text tower", str(text)))
    return None


async def fake_code_embed(text, *, space=None):
    SENT.append(("code embed", str(text)))
    return None


async def fake_code_space():
    """The Host's configured embedding model, as bridge_embed.active_space
    answers it (fake_embed_config's model; the 2560 column the seed writes)."""
    from codesearch import store
    return store.Space(model="fake-embed", dim=2560), ""


async def fake_code_rerank(query, docs):
    SENT.append(("code rerank", str(query)))
    return None


async def fake_entity_model(messages, **kw):
    SENT.append(("chat model", json.dumps(messages)))
    return {"content": "", "tool_calls": []}


async def fake_gateway_model(messages, **kw):
    SENT.append(("chat model (gateway)", json.dumps(messages)))
    return {"content": ""}


async def fake_route(pool, entity_id, role, *a, **kw):
    return dict(ROUTE)


async def fake_window(model, provider=None):
    return 200_000


async def allow_ceigas(entity_id, token_id):
    return True, "allowed by the gate's stub"


INNER_CALLS: list = []


async def scripted_inner(*args, **kwargs):
    """The turn's body behind the real send_message and registry: records how
    the turn was asked for, and ends at once."""
    INNER_CALLS.append(dict(kwargs))
    yield '0:"On it."\n'


def mark() -> int:
    return len(SENT)


def door_check(label, since: int, word: str, key: str, endpoints=None):
    """The endpoint was handed this door's input (`word` reached it) and never
    `key`. `endpoints` narrows which fakes count."""
    got = [(e, t) for e, t in SENT[since:] if endpoints is None or e in endpoints]
    called = any(word in t for _e, t in got)
    leaked = sorted({e for e, t in got if BODIES[key] in t})
    check(f"{label}: planted key — the endpoint was called and never handed the key",
          called and not leaked,
          f"called={called} key reached {leaked or 'nothing'}; handed: "
          f"{[(e, t[:120]) for e, t in got if word in t or BODIES[key] in t][:4]}")


def handed(since: int, endpoint: str, word: str) -> list:
    return [t for e, t in SENT[since:] if e == endpoint and word in t]


async def wait_for(cond, timeout: float = 5.0) -> bool:
    """`cond()` once it holds, polling while background tasks run; its last
    value after `timeout` seconds."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return bool(cond())


def request(body=None):
    async def _json():
        return body or {}
    return types.SimpleNamespace(state=types.SimpleNamespace(app_token=None), json=_json,
                                 headers={}, query_params={})


def vec_of(text: str) -> list:
    h = hashlib.sha256(text.encode()).digest()
    return [b / 255.0 + 0.01 for b in h[:4]]


async def lane_embed(text):
    return vec_of(text)


LANE = (lane_embed, "fake-embed")


async def seed(pool, P):
    # Each seeded page's vector is stamped as current for its text, so the
    # project search's background refresh embeds only a page made later.
    for name, desc, vec in (("Quokka tracker", "count the quokkas on the island", [0.9, 0.1, 0.3, 0.2]),
                            ("Billing dashboard", "invoices and the quokka sheet totals", [0.1, 0.9, 0.1, 0.1]),
                            ("Stripe integration", "payments for the shop", [0.5, 0.5, 0.2, 0.1]),
                            ("Weather app", "forecast dashboard with radar tiles", [0.2, 0.2, 0.9, 0.3])):
        await pool.execute(
            "INSERT INTO projects (entity_id, name, description, search_embedding, search_embedding_model,"
            " search_embedding_hash) VALUES ($1, $2, $3, $4::real[], 'fake-embed', $5)",
            E, name, desc, vec, P._search_hash("fake-embed", P._search_text_of(name, desc, "")))
    await pool.execute(
        "INSERT INTO code_chunks (project, file_path, chunk_index, content, language, chunk_kind,"
        " symbol, start_line, end_line, file_hash, embedding)"
        " VALUES ('demo', 'billing.py', 0, $1, 'python', 'function', 'invoice_total', 1, 2, 'h0',"
        " $2::halfvec(2560))",
        "def invoice_total(items):\n    return sum(i.price for i in items)",
        "[" + ",".join(["0.01"] * 2560) + "]")
    await pool.execute(
        "INSERT INTO mcp_client_tokens (entity_id, name, token_hash, url_handle, scopes)"
        " VALUES ($1, 'Claude Desktop', $2, 'gate', ARRAY['shape', 'signal', 'factory'])",
        E, hashlib.sha256(BEARER.encode()).digest())


def install(patches) -> list:
    saved = []
    for mod, attr, val in patches:
        saved.append((mod, attr, getattr(mod, attr)))
        setattr(mod, attr, val)
    return saved


def restore(saved) -> None:
    for mod, attr, val in reversed(saved):
        setattr(mod, attr, val)


# ── H: the helper itself ────────────────────────────────────────────────────

def helper_checks(SC):
    print("-- H: the helper")
    if isinstance(SC, Exception):
        check("H server/secret_cut.py loads (redact_words, search_words)", False, SC)
        return
    from mastercode import secret_shapes as ss
    hard_samples = [f"rotate {ANTHROPIC} today", f"token {GITHUB} here", f"{AWS} is the key",
                    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
                    "postgres://app:hunter2pass@db.example/x", f"Bearer {'Ab9' * 10}"]
    same, leaks = [], []
    for t in hard_samples:
        out = SC.redact_words(t, door="gate")
        if out != ss.redact(t)[0]:
            same.append((t[:30], out))
        if not ss.hard(ss.scan(t)) or ss.hard(ss.scan(SC.search_words(t, door="gate"))):
            leaks.append(t[:30])
    check("H a hard shape becomes the turn's own marker: redact_words is secret_shapes.redact's text, "
          "the call session.send_message makes", not same, same)
    check("H each sample holds a hard shape, and none survives search_words", not leaks, leaks)
    check("H the marker reads [REDACTED:<rule id>], as the turn writes it",
          SC.redact_words(f"rotate {ANTHROPIC} today", door="gate") == "rotate [REDACTED:anthropic-key] today")
    check("H a search drops the marker's own words too",
          SC.search_words(f"quokka {ANTHROPIC}", door="gate").split() == ["quokka"],
          SC.search_words(f"quokka {ANTHROPIC}", door="gate"))
    plain = [f"quokka {SOFT_ASSIGN}", f"quokka {SOFT_TOKEN}", f"fix {HASH}", "billing dashboard", ""]
    changed = [(t, SC.redact_words(t, door="gate"), SC.search_words(t, door="gate")) for t in plain
               if SC.redact_words(t, door="gate") != t or SC.search_words(t, door="gate") != t]
    check("H soft shapes, a hash and plain words are left exactly as typed", not changed, changed)
    check("H no text is read as empty, as the Index read it", SC.redact_words(None, door="gate") == "")

    records: list = []

    class _H(logging.Handler):
        def emit(self, rec):
            records.append(rec)
    lg = logging.getLogger("gate.secret_cut")
    lg.addHandler(_H())
    lg.propagate = False
    orig = ss.redact

    def boom(*a, **k):
        raise RuntimeError("rules file unreadable")
    ss.redact = boom
    try:
        out = SC.redact_words(f"rotate {ANTHROPIC}", door="gate door", log=lg)
    finally:
        ss.redact = orig
    check("H a screen that fails leaves the words and logs a warning naming the door, as "
          "session.send_message does", out == f"rotate {ANTHROPIC}" and any(
              r.levelno == logging.WARNING and "gate door" in r.getMessage() for r in records),
          (out[:30], [r.getMessage() for r in records]))
    records.clear()
    SC.redact_words(f"rotate {ANTHROPIC}", door="gate door", log=lg)
    check("H a cut is logged at the door, naming the rule and never the value",
          any("anthropic-key" in r.getMessage() and BODIES[ANTHROPIC] not in r.getMessage()
              for r in records), [r.getMessage() for r in records])


# ── the doors ───────────────────────────────────────────────────────────────

async def run(dsn):
    from server import brain_service as bs
    from mastercode import memory_engine, media_engine, inference_config, context_budget, provider_gateway
    from mastercode.runtime import tools as rt_tools
    from codesearch import bridge_embed
    SC = load("server.secret_cut")
    P = load("server.routes.projects")
    CTX = load("server.routes.context")
    BR = load("server.routes.brain")
    SR = load("server.routes.search")
    CS = load("server.routes.codesearch")
    T = load("server.connectors.turns")
    NC = load("server.network.nac_connector")
    SH = load("server.synaptive_handler")
    MCP = load("server.routes.mcp")
    for name, mod in (("projects", P), ("context", CTX), ("brain", BR), ("search", SR),
                      ("codesearch", CS), ("connectors.turns", T), ("nac_connector", NC),
                      ("synaptive_handler", SH), ("mcp", MCP)):
        check(f"the tree's {name} module loads", not isinstance(mod, Exception), mod)

    helper_checks(SC)

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    saved = install([
        (bs, "_pool", pool), (bs, "_entity_id", E), (bs, "_send_message_inner", scripted_inner),
        (memory_engine, "_entity_embedding_config", fake_embed_config),
        (memory_engine, "_embed_text_config_driven", fake_embed_text),
        (media_engine, "active_media_target", fake_media_target),
        (media_engine, "media_config", fake_media_config),
        (media_engine, "embed_media", fake_embed_media),
        (bridge_embed, "embed", fake_code_embed), (bridge_embed, "rerank", fake_code_rerank),
        (bridge_embed, "active_space", fake_code_space),
        (rt_tools, "_call_entity_model", fake_entity_model),
        (provider_gateway, "call_model", fake_gateway_model),
        (inference_config, "resolve_role_route", fake_route),
        (context_budget, "model_context_window", fake_window),
        (SH, "_ceigas_gate", allow_ceigas),
    ])
    try:
        await seed(pool, P)

        # ── S: the Start dialog's search ────────────────────────────────────
        print("-- S: the Start dialog's search (POST /api/projects/search)")
        m = mark()
        await P.search_projects(P.SearchProjects(q=f"quokka-s1 {ANTHROPIC}", limit=8))
        door_check("S the Start dialog's search", m, "quokka-s1", ANTHROPIC)
        a = await P.search_projects_service(pool, E, "quokka", limit=50, lane=LANE, refresh_in_background=False)
        b = await P.search_projects_service(pool, E, f"quokka {ANTHROPIC}", limit=50, lane=LANE,
                                            refresh_in_background=False)
        check("S a search with a key pasted in finds the pages, in the order, the same words find without it",
              bool(a["projects"]) and [p["id"] for p in a["projects"]] == [p["id"] for p in b["projects"]],
              ([p["name"] for p in a["projects"]], [p["name"] for p in b["projects"]]))

        # ── D: the Context Manager's search ─────────────────────────────────
        print("-- D: the Context Manager's search (POST /api/brain/context/search)")
        m = mark()
        await CTX.search_context(request({"query": f"quokka-d1 invoice {ANTHROPIC}", "limit": 20}))
        door_check("D the documents search, the embedding endpoint", m, "quokka-d1", ANTHROPIC,
                   endpoints={"embedding endpoint"})
        door_check("D the documents search, the media model's text tower", m, "quokka-d1", ANTHROPIC,
                   endpoints={"media text tower"})

        # ── M: the Memory app's searches ────────────────────────────────────
        print("-- M: the Memory app's searches (GET /api/brain/memory/semantic, GET /api/brain/memory)")
        m = mark()
        await BR.semantic_search(request(), q=f"quokka-m1 {ANTHROPIC}", limit=20, memory_type=None,
                                 include_superseded=False)
        door_check("M the semantic memory search", m, "quokka-m1", ANTHROPIC)
        m = mark()
        await BR.search_memory(request(), q=f"quokka-m2 {ANTHROPIC}", limit=10)
        door_check("M the memory search", m, "quokka-m2", ANTHROPIC)

        # ── U: the one index ────────────────────────────────────────────────
        print("-- U: the one index (POST /api/search)")
        m = mark()
        out = await SR.unified_search(SR.UnifiedSearch(q=f"quokka-u1 invoice {ANTHROPIC}", limit=10))
        door_check("U the one index, every store's embedding call", m, "quokka-u1", ANTHROPIC)
        stores = sorted({e for e, t in SENT[m:] if "quokka-u1" in t})
        check("U the stores that embed reached their endpoints (the check above is not vacuous)",
              {"embedding endpoint", "code embed"} <= set(stores), (stores, out.get("unavailable")))

        # ── K: code search ──────────────────────────────────────────────────
        print("-- K: code search (GET /api/codesearch/search)")
        m = mark()
        await CS.search(project="demo", q=f"invoice quokka-k1 {ANTHROPIC}", limit=10)
        door_check("K code search, the embed", m, "quokka-k1", ANTHROPIC, endpoints={"code embed"})
        door_check("K code search, the rerank", m, "quokka-k1", ANTHROPIC, endpoints={"code rerank"})

        # ── O: words with no hard shape reach each endpoint as typed ────────
        print("-- O: words with no hard shape reach each search endpoint exactly as typed")
        typed = f"quokka-o1 {SOFT_ASSIGN} {SOFT_TOKEN} {HASH}"
        m = mark()
        await P.search_projects(P.SearchProjects(q=typed, limit=8))
        await CTX.search_context(request({"query": typed}))
        await BR.semantic_search(request(), q=typed, limit=20, memory_type=None, include_superseded=False)
        await CS.search(project="demo", q=f"invoice {typed}", limit=10)
        got = [(e, t) for e, t in SENT[m:] if "quokka-o1" in t]
        wanted = {("embedding endpoint", typed), ("media text tower", typed), ("code embed", f"invoice {typed}"),
                  ("code rerank", f"invoice {typed}")}
        check("O plain words, a soft assignment, a random-looking token and a hash reach the embedding "
              "endpoint, the media text tower, the code embed and its rerank exactly as typed",
              wanted <= set(got) and all(t in (typed, f"invoice {typed}") for _e, t in got),
              sorted(set((e, t[:60]) for e, t in got)))

        # ── C: a connector message ──────────────────────────────────────────
        # No model call places it (thread_pick.place_connector): a clear match
        # joins its thread, else it lands in the chat's last thread or a new
        # one named after its first line, and the Host picks in its own turn
        # from the open threads its frame lists (runtime/thread_choice). The
        # doors before that turn: the placement's search (the embedding call),
        # the new thread's name, and the turn's words and frame.
        print("-- C: a connector message (POST /api/connector/messages)")
        caller = NC.ConnectorCaller(app_id="telegram-gate", stages=frozenset({"active"}), grant_id=1,
                                    platform="telegram", account_id=CALLER_ACCOUNT)

        def body(mid, text, chat="100"):
            return {"account_id": CALLER_ACCOUNT, "chat_id": chat, "message_id": mid, "text": text}

        def choice_of(call) -> str:
            return json.dumps(((call.get("frame") or {}).get("thread_choice")) or {})

        async def titles() -> list:
            return [t for (t,) in await pool.fetch("SELECT title FROM circles_threads WHERE entity_id = $1", E)]
        m, n0 = mark(), len(INNER_CALLS)
        msg = T.parse_message(caller, body("c1", f"quokka-c1 can you rotate this one {GITHUB} please"))
        r = await T.route(pool, caller, msg)
        door_check("C the placement's search of a message no thread claims", m, "quokka-c1", GITHUB,
                   endpoints={"embedding endpoint"})
        check("C no model call places the message before its turn (the Host picks in the turn)",
              not handed(m, "chat model", "quokka-c1"), [t[:200] for t in handed(m, "chat model", "quokka-c1")])
        started = INNER_CALLS[n0:]
        check("C the message starts its turn in the thread made for it (the check below is not vacuous)",
              getattr(r, "state", None) == "started" and len(started) == 1
              and "quokka-c1" in started[0].get("message", ""), (getattr(r, "state", r), started))
        check("C planted key — the turn the message started reads its words and its frame without the key",
              bool(started) and not any(BODIES[GITHUB] in json.dumps(c, default=str) for c in started), started)
        named = [t for t in await titles() if "quokka-c1" in (t or "")]
        check("C planted key — the thread made for the message is named after it without the key",
              bool(named) and not any(BODIES[GITHUB] in t for t in named), await titles())
        m = mark()
        msg = T.parse_message(caller, body("c2", f"/new fix the deploy {GITHUB}", chat="101"))
        await T.route(pool, caller, msg)
        check("C /new <text> made the thread (the check below is not vacuous)",
              any("fix the deploy" in (t or "") for t in await titles()), await titles())
        check("C planted key — /new <text> names the thread without the key",
              not any(BODIES[GITHUB] in (t or "") for t in await titles()), await titles())
        n0 = len(INNER_CALLS)
        msg = T.parse_message(caller, body("c3", "quokka-c3 and the deploy?", chat="102"))
        await T.route(pool, caller, msg)
        listed = [choice_of(c) for c in INNER_CALLS[n0:] if "quokka-c3" in c.get("message", "")]
        check("C the next message's turn lists that thread in its frame (the check below is not vacuous)",
              any("fix the deploy" in f for f in listed), listed)
        check("C planted key — the frame that lists that thread never holds the key",
              bool(listed) and not any(BODIES[GITHUB] in f for f in listed), listed)
        check("C the turn /new started read the words without the key",
              bool(INNER_CALLS) and not any(BODIES[GITHUB] in json.dumps(c, default=str) for c in INNER_CALLS),
              INNER_CALLS[-1:])

        # ── F: a forward, whose author's name and id carry keys ─────────────
        # The header names who wrote a forward (origin_label): the pick reads
        # it, the turn reads it and keeps it as the row's connector_origin, and
        # the connector keeps the words it nudged (Reply.nudges) to match what
        # push_nudge queued, which push_nudge cuts again.
        print("-- F: a forwarded connector message, its author's name and id carrying keys")
        from mastercode import secret_shapes as ss
        from mastercode.runtime import session as SESS

        def forward(mid, text, chat):
            return {**body(mid, text, chat=chat),
                    "origin": {"kind": "forward", "source": "bot", "name": f"Deploy bot {GITHUB}", "id": AWS}}

        def keyless(t) -> bool:
            return BODIES[GITHUB] not in t and BODIES[AWS] not in t
        m, n0 = mark(), len(INNER_CALLS)
        r = await T.route(pool, caller, T.parse_message(caller, forward("f1", "quokka-f1 the deploy is green", "110")))
        searched = handed(m, "embedding endpoint", "quokka-f1")
        check("F the placement's search read the forward and its author (the check below is not vacuous)",
              bool(searched) and any("Deploy bot" in t for t in searched), [t[:300] for t in searched])
        check("F planted key — the placement's search of a forward is never handed its author's name or id",
              bool(searched) and all(keyless(t) for t in searched), [t[:300] for t in searched])
        started = json.dumps(INNER_CALLS[n0:], default=str)
        check("F the forward starts its turn, with its author (the check below is not vacuous)",
              getattr(r, "state", None) == "started" and "quokka-f1" in started and "Deploy bot" in started,
              (getattr(r, "state", r), INNER_CALLS[n0:]))
        check("F planted key — the turn a forward started, and its frame, hold neither its author's key nor id",
              keyless(started), INNER_CALLS[n0:])
        n0 = len(INNER_CALLS)
        await T.route(pool, caller, T.parse_message(caller, body("f2", "/new", chat="111")))
        msg = T.parse_message(caller, forward("f3", f"quokka-f3 ship notes {GITHUB}", "111"))
        await T.route(pool, caller, msg)
        titles = [t for (t,) in await pool.fetch("SELECT title FROM circles_threads WHERE entity_id = $1", E)]
        check("F /new then a forward made the thread (the check below is not vacuous)",
              any("quokka-f3" in (t or "") for t in titles), titles)
        check("F planted key — the thread /new names after a forward holds no key",
              all(keyless(t or "") for t in titles), titles)
        turn = json.dumps(INNER_CALLS[n0:], default=str)
        check("F the turn of the forward started, with its author (the check below is not vacuous)",
              "quokka-f3" in turn and "Deploy bot" in turn and "connector_origin" in turn, INNER_CALLS[n0:])
        check("F planted key — the turn reads the forward's header and keeps its connector_origin without a key",
              keyless(turn), INNER_CALLS[n0:])
        for_host = T.message_for_host(caller.platform, msg.text, msg.origin)
        again, found = ss.redact(for_host)
        check("F planted key — the forward's words for the Host hold no key, and cutting them again "
              "changes nothing", keyless(for_host) and again == for_host and not ss.hard(found),
              (for_host[:300], again[:300], [f.id for f in found]))
        sid = "quokka-f-session"
        SESS.push_nudge(sid, for_host)
        queued = SESS.consume_nudges(sid)
        check("F push_nudge queues the very words the connector keeps for a nudge (its reply follows them)",
              queued == [for_host.strip()], (queued, for_host.strip()))

        # ── X: the Synaptive MCP doors ──────────────────────────────────────
        print("-- X: the Synaptive MCP doors an outside client calls (POST /mcp)")
        m = mark()
        await SH.handle_synaptive_shape({"_internal": True, "message": f"quokka-x1 here is my key {ANTHROPIC}"})
        door_check("X shape(message)", m, "quokka-x1", ANTHROPIC, endpoints={"embedding endpoint"})
        m = mark()
        out = await SH.handle_synaptive_recall({"bearer": BEARER, "query": f"quokka-x2 {ANTHROPIC}"})
        door_check("X recall(query)", m, "quokka-x2", ANTHROPIC, endpoints={"embedding endpoint"})
        check("X recall ran past the token and the gate (the check above is not vacuous)",
              out.get("ok") is True, out)
        m = mark()
        out = await SH.handle_synaptive_signal({"bearer": BEARER,
                                                "user_message": f"quokka-x3 deploy with {AWS}",
                                                "model_response": f"Noted, I will deploy with {AWS}."})
        door_check("X signal(user_message, model_response)", m, "quokka-x3", AWS,
                   endpoints={"embedding endpoint"})
        rows = [c for (c,) in await pool.fetch(
            "SELECT content FROM entity_memory WHERE entity_id = $1 AND source_session LIKE 'synaptive:%'", E)]
        check("X signal kept the turn (the check below is not vacuous)",
              out.get("ok") is True and any("quokka-x3" in (c or "") for c in rows), (out, rows))
        check("X planted key — signal keeps no row holding the key, either side of the turn",
              not any(BODIES[AWS] in (c or "") for c in rows), rows)
        m = mark()
        typed = f"quokka-o2 {SOFT_ASSIGN} {SOFT_TOKEN}"
        await SH.handle_synaptive_shape({"_internal": True, "message": typed})
        await SH.handle_synaptive_recall({"bearer": BEARER, "query": typed})
        got = [t for e, t in SENT[m:] if e == "embedding endpoint" and "quokka-o2" in t]
        check("O shape's message and recall's query with no hard shape reach the embedding endpoint as typed",
              len(got) >= 2 and all(t == typed for t in got), got)

        # ── R: a call the Host refuses runs no screen and logs no rule ───────
        print("-- R: a Synaptive call the Host refuses runs no secret screen and logs nothing of it")
        from mastercode import secret_shapes as ss
        screens: list = []
        real_redact = ss.redact

        def counting_redact(text, *a, **kw):
            screens.append(text)
            return real_redact(text, *a, **kw)
        records: list = []

        class _All(logging.Handler):
            def emit(self, record):
                records.append(record)
        root, h = logging.getLogger(), _All(level=logging.DEBUG)
        root_level = root.level
        root.addHandler(h)
        root.setLevel(logging.DEBUG)

        async def deny_ceigas(entity_id, token_id):
            return False, "denied by the gate's stub"
        stranger = "mcp_" + "zq" * 20
        refused = [
            ("recall, a token the Host never minted", "unauthorized", None,
             lambda: SH.handle_synaptive_recall({"bearer": stranger, "query": f"quokka-r1 {ANTHROPIC}"})),
            ("signal, a token the Host never minted", "unauthorized", None,
             lambda: SH.handle_synaptive_signal({"bearer": stranger, "user_message": f"quokka-r2 {AWS}",
                                                 "model_response": f"ok {AWS}"})),
            ("shape, a token the Host never minted", "unauthorized", None,
             lambda: SH.handle_synaptive_shape({"bearer": stranger, "message": f"quokka-r3 {ANTHROPIC}"})),
            ("recall, its gate says no", "denied", deny_ceigas,
             lambda: SH.handle_synaptive_recall({"bearer": BEARER, "query": f"quokka-r4 {ANTHROPIC}"})),
            ("signal, its gate says no", "denied", deny_ceigas,
             lambda: SH.handle_synaptive_signal({"bearer": BEARER, "user_message": f"quokka-r5 {AWS}"})),
        ]
        try:
            for label, status, gate, call in refused:
                screens.clear()
                records.clear()
                g = install([(ss, "redact", counting_redact)] + ([(SH, "_ceigas_gate", gate)] if gate else []))
                try:
                    out = await call()
                finally:
                    restore(g)
                said = [r.getMessage() for r in records]
                cut_logs = [s for s in said if "secret" in s.lower()
                            or any(i in s for i in ("anthropic-key", "aws-access-key"))]
                check(f"R {label}: refused ({status})", out.get("status") == status, out)
                check(f"R {label}: no secret screen ran and no log line names a rule",
                      not screens and not cut_logs, (len(screens), cut_logs))
        finally:
            root.removeHandler(h)
            root.setLevel(root_level)

        # The turn screens again what a door cut (session.py _send_message_core,
        # which logs when redact() returns ANY finding). The connector's cut,
        # handed to that call, finds nothing: no marker rewritten, no cut
        # logged that did not happen.
        for text in (f"quokka-t1 git clone https://alex:{GITHUB}@github.com/x/y.git",
                     f"quokka-t2 git clone https://{GITHUB}@github.com/x/y.git",
                     f"quokka-t3 password: {GITHUB}"):
            cut = T.parse_message(caller, body("t", text)).text
            again, found = ss.redact(cut)
            check(f"T the turn's second screen of the connector's cut changes nothing and logs nothing: "
                  f"{text.split(' ')[0]}",
                  "[REDACTED:github-token]" in cut and again == cut and not found,
                  (cut, again, [f.id for f in found]))
        # ── S: a new project from the Start dialog ─────────────────────────
        # When nothing fits, the dialog makes what was typed the new project's
        # description, with a title suggested from it (POST /api/projects);
        # the project search's background refresh then embeds both.
        print("-- S: a new project from the Start dialog (POST /api/projects)")
        await wait_for(lambda: E not in P._EMBED_REFRESH_INFLIGHT)
        made_dirs = []

        async def no_repo(_pool, pid, name):
            return {"repo": None, "created": False, "reason": "no forge in this gate"}
        dirs_repo = install([(P, "ensure_project_dir", made_dirs.append), (P, "ensure_project_repo", no_repo)])
        try:
            m = mark()
            out = await P.create_project(P.CreateProject(
                name=f"quokka-p1 rotate {ANTHROPIC}",
                description=f"quokka-p1-desc rotate {ANTHROPIC} in the deploy, then ship"))
            row = await pool.fetchrow("SELECT name, description FROM projects WHERE id = $1", out["id"])
            check("S the Start dialog made the project (the checks below are not vacuous)",
                  row is not None and "quokka-p1-desc" in (row["description"] or ""), (out, row))
            check("S planted key — the new project keeps no key in its name or description",
                  row is not None and BODIES[ANTHROPIC] not in (row["name"] + (row["description"] or "")),
                  dict(row) if row else None)
            await P.search_projects(P.SearchProjects(q="quokka-p1", limit=8))
            await wait_for(lambda: any("quokka-p1-desc" in t for _e, t in SENT[m:])
                           and E not in P._EMBED_REFRESH_INFLIGHT)
            door_check("S the new project's text, embedded by the project search's refresh", m,
                       "quokka-p1-desc", ANTHROPIC)
        finally:
            restore(dirs_repo)

    finally:
        restore(saved)
        await pool.close()


def main():
    with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as srv, \
            kernel_schema_db(srv, entity_id=E) as dsn:
        asyncio.run(run(dsn))
    print(f"planted-key checks failed: {KEY_FAILED}")
    print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
