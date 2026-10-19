#!/usr/bin/env python3
"""Gate: the Workbench's Index, the server half (server/routes/workbench_index.py
and the Index's half of server/connectors/thread_pick.py), against a real kernel
schema holding the Host
scripts/test_project_list_speed_pg.py seeds (300 projects, 75,000 project turn rows,
25,000 Circles turn rows), plus the pages each check is about.

  R. ranking (POST /api/projects/index): archived pages are listed and marked; a
     page's conversation lifts it (a page matched only in its turns is listed, more
     such turns rank higher, and of two pages the text matches alike, the one whose
     turns hold the words comes first); a page whose name holds the typed text stays
     first over pages that matched only in their conversation, also in the case
     reciprocal-rank fusion gets wrong and where the name holds it only inside a word;
     where no conversation matches, the order is the Start dialog's; the excerpt marks
     the matched words and says where it came from; a word too common to tell pages
     apart is named and left out; another entity's rows and artifacts never list a page.
  S. the Index, scoring the vectors with pgvector, orders as the Start dialog's search
     (POST /api/projects/search) does where no conversation matches, and a stored
     vector pgvector refuses drops the embedding arm, never the search.
  P. the conversation arm reads entity_memory only through the GIN index on
     search_tsv, under a Limit, never a sequential scan (custom and generic plans,
     before and after ANALYZE), and reads at most CONV_ROWS rows whatever is typed;
     the excerpt read goes by primary key.
  T. wall-clock medians of the Start dialog's search and of the Index, and
     a backstop ceiling; the budget itself is the plan check above.
  C. the pick's list: the first INDEX_PICK_K pages of the Index's own search for the
     message, in its order, open and archived, with turns or none yet, ticket projects
     too, then the open page when the search did not rank it; never an artifact or
     another entity's project; the open page marked and kept by any budget; each
     page's newest rows read through idx_entity_memory_project_turns. The call is
     one, with one tool, pick_page, in the Index's words.
  K. the pick's cost does not grow with the pages: on a Host of 50
     pages and on one of 300, the pick's input (system, message, tool; chars/4) is
     within PICK_BUDGET_TOKENS and lists at most INDEX_PICK_K + 1 pages; the send's
     wall clock, without the model's own time, is printed and held to a backstop.
  X. a page the search ranks below the first INDEX_PICK_K is not in the Host's list,
     and a pick naming it lands nowhere: the person is asked, nothing is written.
  D. secrets: a pasted key is cut (brain/mastercode/secret_shapes, as the turn cuts
     it) before the search embeds the words, before the pick's call, before a new
     page takes its title from them and before the turn starts.
  M. the marker: "Index message, picked this page: <reason>" (or "started"), a turn
     row on the page (role assistant, index_pick, never mined), never shown as a
     page's last exchange to the Index or to a connector.
  N. a "new" pick makes a Workbench project with the Host's title, writes "started
     this page" on it and starts the turn there; a marker that cannot be written
     leaves no project behind.
  A. an archived pick is restored and the turn starts there; a refused restore
     (409) is reported with its reason, the page stays archived, nothing is written
     and no turn starts; a marker that cannot be written leaves the page archived
     (the restore is in the landing's transaction).
  F. a failed pick (no call, a page not in the list, a model error, a call that
     raises, no chat model) starts nothing, writes nothing, makes nothing, and hands
     back the pages the Host was shown (or would have been) to choose from.
  W. the turn starts through brain_service.send_message as POST /api/brain/messages
     starts one: the words verbatim, the calling app and its permissions, the
     project, its thread key; the answer names the turn and where to follow it. The
     routes carry their scopes and are mounted, the search ahead of
     /api/projects/{project_id}, and the send is walled from the Host
     (runtime/kernel_ops), the person's own message.
  H. the two searches the Workbench runs as the person types, over HTTP (FastAPI's
     own parsing, validation and answer, on this gate's database): POST
     /api/projects/index and POST /api/projects/search take {q, limit} in the body,
     answer what the service answers, cut a pasted key before the embedding call,
     refuse what the GETs refused (limit and length), answer a lone surrogate half
     (JSON can carry one; a query string never did), keep their scopes' guard, and
     the GETs are gone: the words are never in a URL (CWE-598).
  Q. the pick's fill, clip and one send per request id:
     - fill: a message the search finds fewer than INDEX_PICK_K pages for is shown
       them first, then the most recently active open pages, INDEX_PICK_K in all
       (one the search finds nothing for gets the recent pages, not only "new");
     - clip: the pick reads at most the first INDEX_PICK_MESSAGE_CHARS of a long
       message and is told how much more follows; the turn gets it whole, and the
       pick's input stays within PICK_BUDGET_TOKENS;
     - one send per request id: a second request with the id while the first's
       pick is held, or one after the answer, are one pick, one marker and one
       turn with one answer; another id is another send; one id with other words
       is refused (409), an id of another shape too (400); a send that failed is
       not kept, so the same id runs again; a request that goes away mid-pick
       loses nothing; the route sends the id. The pick is held until the check
       lets it answer (HeldModel), never timed with a sleep.
     Every Q check fails on the code before these three.
  R. the same, across a restart: the request id is kept with the delivery,
     in the database, so a retry after the server restarted (the in-process record of
     sends cleared: index_send_once's map is all a restart takes) is answered with
     the first send's answer and delivers nothing again:
     - a restart: a send, the record cleared, the same send: one user row, one turn,
       one pick, one marker, and the same answer; the same id for other words is
       still refused (409);
     - two concurrent retries after a restart are one answer, one delivery;
     - a send whose pick landed but whose words never reached the page (the turn
       did not start) runs again under its id and delivers once;
     - a send that became a nudge into the turn running on the page: the registry
       hands the request id to the runtime's nudge queue (session.nudge_ids), the
       nudge's row keeps it, and the retry after a restart delivers nothing again;
     - the lookup is by idx_entity_memory_client_id (custom and generic plans), and
       v218's file, run twice on a Host that has not got the index, builds the same
       index the schema declares;
     - a retry after the in-memory window (no restart): words
       nudged into a turn that ends without reading them go on as the thread's next
       turn, whose user row keeps the id (two such sends: one turn, one row keeping
       both ids as a list), and the retry delivers nothing again; a send of the words
       the page is already running attaches to that turn, its marker records it, and
       the retry after that turn ended starts nothing; the same when the unread words
       are the very words the thread's next turn already waits on (sent again under
       another id): they attach to that turn, the marker of their id records it, and
       their late retry starts nothing;
     - PII mode keeps a digest of the id on the marker and the user row, never the
       id as written, and the retry still finds the send by it.
     The new turn's user row is written here by the real memory_engine.write_turn_row
     with the tags the runtime gives it (a stand-in for _send_message_core, as the
     scripted turn is for the rest of this gate); that the runtime puts client_id on
     the row, and on the row an identity hold saves, is proven against the real
     runtime in scripts/test_late_nudge_once.py (section I).
     Every R check fails on the code before the request id was kept.

The Host's pick is scripted through choose_page's call_model seam, as the connector
gates script choose_thread. The turn's body is a scripted
brain_service._send_message_inner behind the real send_message and turn registry.
No model is called; no project directory or Code Repository is made.

Fresh clone of the kernel schema on TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN, else a
throwaway cseries-postgres through docker. Run against another tree (APP_ROOT=<that tree>) it reports which checks that tree
fails.

  PYTHONPATH=brain python3 scripts/test_workbench_index_pg.py
"""
import asyncio
import contextlib
import datetime
import json
import os
import pathlib
import re
import statistics
import subprocess
import sys
import time
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
          "this gate needs a real database to prove the Index's SQL and plans.")
    sys.exit(2)

import test_project_list_speed_pg as speed  # noqa: E402  (its seed is the Host the budget is about)

FAILED = 0
E = speed.E
OTHER_E = speed.OTHER_E
CEILING_MS = 1_000
APP = "workbench"
PERMS = ["write:session", "write:projects", "read:brain_memory"]
ROUTE = {"provider": "anthropic", "model": "claude-opus-5-5"}
WORST = "iki epm enih fnh lidin npo ppcleg bffc"
# Thirty words of a message: the seed's commonest words and some English, the
# shape of a message typed straight into the Index.
LONG = ("could you look at the iki epm numbers again and fix the enih fnh export before friday, the lidin "
        "npo report and ppcleg bffc totals still disagree with the quokka sheet we sent")
# The pick's list: the Index's best pages for the message, at most this many
# (thread_pick.INDEX_PICK_K), plus the open page.
K = 12
# The pick's input, in the estimate the picker itself sizes by (chars/4,
# context_budget.est_tokens): the system message, the message and the tool's
# schema. Fixed, whatever the number of pages. Its parts, measured at their
# limits: a listed page is at most 193 tokens (a 120-character
# title, the archived note, the last-active time, and two exchange lines of
# 280 characters, thread_pick._TITLE_CHARS and _EXCHANGE_CHARS), the
# instruction 162 and pick_page's schema 176, so thirteen pages with this
# message come to 2,872 at most. Every page listed, as before, the pick took
# 8,412 at 50 pages and 48,713 at 321.
PICK_BUDGET_TOKENS = 3_500
# A message the seed's every page matches ("project"), so the list is full.
KMSG = "Can you update the project plan before Friday?"
# A pasted key's shape (secret_shapes.json anthropic-key), made up.
SECRET = "sk-ant-api03-" + "Wv5" * 20


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {detail}"))
    if not cond:
        FAILED += 1


def load(name):
    """A module of the tree under test, or the exception that stopped it (a
    tree without the Index fails the checks that need it)."""
    try:
        return __import__(name, fromlist=["_"])
    except Exception as e:  # noqa: BLE001
        return e


def load_app():
    """server.main's FastAPI app, imported without the container's dirs (as
    scripts/test_api_route_guards.py imports it), or the exception."""
    import starlette.staticfiles as _sf
    orig = _sf.StaticFiles.__init__

    def _init(self, *a, **k):
        k["check_dir"] = False
        return orig(self, *a, **k)
    _sf.StaticFiles.__init__ = _init
    try:
        from server.main import app
        return app
    except Exception as e:  # noqa: BLE001
        return e
    finally:
        _sf.StaticFiles.__init__ = orig


def served_routes(app) -> list:
    """The routes the app dispatches to, in either FastAPI shape (the image's
    copies each included route onto app.routes; newer ones keep the included
    router and expand it in iter_route_contexts), as
    scripts/test_api_route_guards.py reads them."""
    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:
        return list(app.routes)
    out = []
    for rc in iter_route_contexts(app.routes):
        ctx = getattr(rc, "_route_context", None)
        out.append(rc.route if ctx is None else (getattr(ctx, "starlette_route", None) or ctx))
    return [r for r in out if getattr(r, "methods", None)]


# ── the pages the checks are about ──────────────────────────────────────────

async def project(pool, name, *, status="active", description="", kind="project", config=None,
                  repo=None, entity=E, days_ago=0):
    return await pool.fetchval(
        "INSERT INTO projects (entity_id, name, description, status, kind, config, repo, created_at, updated_at)"
        " VALUES ($1, $2, $3, $4, $5, COALESCE($6::jsonb, '{}'::jsonb), $7,"
        "         now() - make_interval(days => $8), now() - make_interval(days => $8)) RETURNING id",
        entity, name, description, status, kind, json.dumps(config) if config else None, repo, days_ago)


async def turn(pool, pid, content, role="user", *, mins_ago=600, entity=E):
    md = {"role": role, "source_kind": "chat_turn", "app_id": APP,
          "source_context": {"kind": "project", "ref": str(pid)}}
    return await pool.fetchval(
        "INSERT INTO entity_memory (entity_id, content, memory_type, importance, status, project_id, metadata,"
        "                           created_at)"
        " VALUES ($1, $2, 'turn', 0.5, 'active', $3, $4::jsonb, now() - make_interval(mins => $5)) RETURNING id",
        entity, content, pid, json.dumps(md), mins_ago)


async def phase(pool, pid, n, title):
    await pool.execute("INSERT INTO project_phases (project_id, phase_number, title, status)"
                       " VALUES ($1, $2, $3, 'planning')", pid, n, title)


async def pages(pool):
    """The fixture pages, by name."""
    pg = {}
    pg["qk_name"] = await project(pool, "Quokka integration", days_ago=30)
    pg["qk_many"] = await project(pool, "Field notes", days_ago=20)
    for i in range(5):
        await turn(pool, pg["qk_many"], f"Saw the quokka near the feeder again, visit {i}.", mins_ago=900 - i)
    await turn(pool, pg["qk_many"], "Here are the notes from the last visit.", "assistant", mins_ago=800)
    pg["qk_one"] = await project(pool, "Garden", days_ago=10)
    await turn(pool, pg["qk_one"], "A quokka once wandered in here.", mins_ago=700)
    await turn(pool, pg["qk_one"], "Noted.", "assistant", mins_ago=699)
    pg["qk_arch"] = await project(pool, "Old zoo", status="archived", days_ago=40)
    await turn(pool, pg["qk_arch"], "The quokka enclosure needs a new gate.", mins_ago=5000)
    await turn(pool, pg["qk_arch"], "I priced the quokka gate at forty dollars.", "assistant", mins_ago=4999)
    pg["qk_none"] = await project(pool, "Plain page", days_ago=5)
    await turn(pool, pg["qk_none"], "Nothing about animals here.", mins_ago=500)
    pg["art"] = await project(pool, "Quokka sketch", kind="artifact", days_ago=2)
    await turn(pool, pg["art"], "A quokka drawing.", mins_ago=400)
    pg["ticket"] = await project(pool, "Ticket 77", config={"ticket": {"ref": "T-77"}}, days_ago=3)
    pg["other"] = await project(pool, "Quokka elsewhere", entity=OTHER_E, days_ago=1)
    await turn(pool, pg["other"], "Another Host's quokka.", entity=OTHER_E, mins_ago=10)
    # Another entity's row naming quokka, pointing at this entity's page.
    await turn(pool, pg["qk_none"], "quokka quokka quokka", entity=OTHER_E, mins_ago=5)
    pg["led_old"] = await project(pool, "Ledger one", description="the billing dashboard", days_ago=50)
    await turn(pool, pg["led_old"], "The dashboard numbers were off by one.", mins_ago=6000)
    pg["led_new"] = await project(pool, "Ledger two", description="the billing dashboard", days_ago=1)
    pg["stripe_n"] = await project(pool, "Stripe integration", days_ago=60)
    pg["stripe_w1"] = await project(pool, "Checkout", days_ago=61)
    await phase(pool, pg["stripe_w1"], 1, "Stripe checkout")
    await turn(pool, pg["stripe_w1"], "Is stripe live yet?", mins_ago=7000)
    pg["stripe_w2"] = await project(pool, "Payouts", description="payouts go out through stripe", days_ago=62)
    await turn(pool, pg["stripe_w2"], "Did the stripe payout land?", mins_ago=7100)
    pg["nar_name"] = await project(pool, "Narwhal tusk", status="archived", days_ago=70)
    pg["nar_desc"] = await project(pool, "Sea life", description="a narwhal survey of the bay", days_ago=71)
    pg["nar_phase"] = await project(pool, "Reef study", days_ago=72)
    await phase(pool, pg["nar_phase"], 1, "Narwhal sightings")
    # A name that holds the text inside a word (the name boost, no word match)
    # and a page whose turns are all about it: the boost alone must still win.
    pg["bot_name"] = await project(pool, "Robotics lab", days_ago=90)
    pg["bot_conv"] = await project(pool, "Helpdesk", days_ago=2)
    for i in range(3):
        await turn(pool, pg["bot_conv"], f"The bot crashed again, attempt {i}.", mins_ago=300 - i)
    pg["repo_live"] = await project(pool, "Shared repo live", repo="alex/shared-x", days_ago=80)
    pg["repo_arch"] = await project(pool, "Shared repo old", status="archived", repo="alex/shared-x", days_ago=81)
    for table in ("projects", "entity_memory", "project_phases"):
        await pool.execute(f"ANALYZE {table}")
    return pg


# ── reading ─────────────────────────────────────────────────────────────────

async def index(W, pool, q, limit=100):
    return await W.index_search_service(pool, E, q, limit=limit, lane=(None, ""), refresh_in_background=False)


def ids(out):
    return [p["id"] for p in out["pages"]]


async def want_pick_pages(W, pool, q, *, entity=E):
    """The pages the Host should be shown for `q`, by the fill rule, read
    here on its own (the projects' own rows, not the code under test's fill):
    the Index's first K for it, then the most recently active open pages,
    up to K in all. GREATEST never reads NULL here (updated_at, created_at)."""
    got = ids(await W.index_search_service(pool, entity, q, limit=K, lane=(None, ""),
                                           refresh_in_background=False, snippets=False))[:K]
    import server.routes.projects as P
    recent = await pool.fetch(f"SELECT p.id, {P._LAST_ACTIVITY_SQL} AS at FROM projects p WHERE p.entity_id = $1"
                              " AND p.kind = 'project' AND p.status <> 'archived' ORDER BY at DESC, p.id DESC",
                              entity)
    for r in recent:
        if len(got) >= K:
            break
        if r["id"] not in got:
            got.append(r["id"])
    return got


def page(out, pid):
    return next((p for p in out["pages"] if p["id"] == pid), None)


def marked(snippet):
    return [x["text"] for x in (snippet or {}).get("parts") or [] if x.get("mark")]


def walk(plan, parent=None, out=None):
    out = [] if out is None else out
    out.append((plan.get("Node Type"), plan.get("Relation Name"), plan.get("Index Name"), parent,
                plan.get("Actual Rows"), plan.get("Actual Loops"), plan.get("Rows Removed by Filter")))
    for c in plan.get("Plans") or []:
        walk(c, plan.get("Node Type"), out)
    return out


async def plan_nodes(pool, q, a, *, generic=False, analyze=False):
    async with pool.acquire() as c:
        if generic:
            await c.execute("SET plan_cache_mode = force_generic_plan")
        try:
            raw = await c.fetchval(f"EXPLAIN ({'ANALYZE, ' if analyze else ''}FORMAT JSON) " + q, *a)
        finally:
            if generic:
                await c.execute("RESET plan_cache_mode")
    j = json.loads(raw) if isinstance(raw, str) else raw
    return walk(j[0]["Plan"])


def through_gin(nodes) -> bool:
    """entity_memory read only as a bitmap heap scan under a Limit, its bitmap
    only from the GIN index on search_tsv."""
    on_mem = [n for n in nodes if n[1] == "entity_memory"]
    return (bool(on_mem) and all(n[0] == "Bitmap Heap Scan" and n[3] == "Limit" for n in on_mem)
            and {n[2] for n in nodes if n[0] == "Bitmap Index Scan"} == {"idx_entity_memory_search_tsv"})


async def median_ms(fn, runs=5):
    t = []
    for _ in range(runs):
        t0 = time.perf_counter()
        await fn()
        t.append((time.perf_counter() - t0) * 1000)
    return statistics.median(t)


# ── the pick and the turn, scripted ─────────────────────────────────────────

def pick_call(**args):
    return {"content": "", "tool_calls": [{"id": "c1", "type": "function",
                                           "function": {"name": "pick_page", "arguments": json.dumps(args)}}]}


class Model:
    """The Host's one pick call, scripted: records what it was sent."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    async def __call__(self, messages, **kw):
        self.calls.append((messages, kw))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


INNER_CALLS: list = []


async def scripted_inner(*args, **kwargs):
    """The turn's body behind the real send_message and registry: records how
    the turn was asked for, and ends at once."""
    INNER_CALLS.append(dict(kwargs))
    yield '0:"On it."\n'


async def counts(pool):
    return (await pool.fetchval("SELECT count(*) FROM projects"),
            await pool.fetchval("SELECT count(*) FROM entity_memory WHERE metadata->>'source_kind' = 'index_pick'"))


async def settle(turn_id):
    from server.turn_registry import get_turn_registry
    rec = get_turn_registry().get(turn_id) if turn_id else None
    if rec is not None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(rec.done.wait(), 10)
    return rec


async def send(W, pool, text, answer, *, route=ROUTE, current=None, entity=E):
    model = Model(answer)
    status, out = await W.index_send_service(pool, entity, text, current_project_id=current, app_id=APP,
                                             app_permissions=PERMS, call_model=model, route=route)
    rec = await settle((out.get("turn") or {}).get("turn_id"))
    return status, out, rec, model


def listed(model) -> list:
    """The page ids the Host was shown in the pick's call, in order."""
    msgs = model.calls[0][0] if model.calls else [{}]
    return [int(x) for x in re.findall(r"^\[project:(\d+)\]", msgs[0].get("content", ""), re.M)]


def pick_input_tokens(model) -> int:
    """The pick's input as the picker estimates it (chars/4): every message
    and the tools' schemas."""
    msgs, kw = model.calls[0]
    return (sum(len(m.get("content") or "") for m in msgs) + len(json.dumps(kw.get("tools")))) // 4


async def attempt(call):
    """(value, None), or (None, the exception) for `call()`, a coroutine
    function of no arguments: a tree without this slice's calls (another
    signature raises as it is called) fails the check instead of stopping
    the run."""
    try:
        return await call(), None
    except Exception as e:  # noqa: BLE001
        return None, e


# ── Q: fill, clip and one send per request id ───────────────────────────────

class HeldModel(Model):
    """The pick call, scripted, held until the check lets it answer
    (release): a second request comes while the first is picking because the
    check sends it then, never because a sleep was long enough."""

    def __init__(self, answer):
        super().__init__(answer)
        self.go = asyncio.Event()

    def release(self):
        self.go.set()

    async def __call__(self, messages, **kw):
        self.calls.append((messages, kw))
        await self.go.wait()
        return self.answer


async def picking(model, n=1, secs=10.0):
    """Until the pick has been called `n` times, that is, the send is out and
    the Host is picking (a backstop of `secs`, never a wait for its own
    sake)."""
    t0 = time.perf_counter()
    while len(model.calls) < n and time.perf_counter() - t0 < secs:
        await asyncio.sleep(0.005)
    return len(model.calls) >= n


async def send_once(W, pool, text, rid, model, *, current=None):
    """index_send_once as the route calls it, the pick scripted by `model`."""
    return await W.index_send_once(pool, E, text, request_id=rid, current_project_id=current, app_id=APP,
                                   app_permissions=PERMS, call_model=model, route=ROUTE)


async def q_slice5(W, tp, pool, pg):
    print("-- Q: the pick's list filled to K, a long message clipped, one send per request id")

    # Fill: "quokka" is found on four pages; the rest of the list is the most
    # recently active open pages. Each check below is one the code before
    # these rules fails: what it also asserts of the search, the landing or the
    # turn is the ground it stands on, held in the same check.
    found = ids(await index(W, pool, "quokka"))
    want = await want_pick_pages(W, pool, "quokka")
    status, out, rec, model = await send(W, pool, "quokka", pick_call(page=f"project:{pg['qk_many']}", reason="r"),
                                         current=pg["qk_one"])
    shown = listed(model)
    arch = {r["id"] for r in await pool.fetch("SELECT id FROM projects WHERE status = 'archived'")}
    check(f"Q fill: the search finds {len(found)} of {K} pages for 'quokka'; the Host is shown them first, in "
          f"its order, then the most recently active open pages (never archived ones, each once), {K} in all, "
          "and the send lands as before",
          0 < len(found) < K and shown == want and len(shown) == K and shown[:len(found)] == found
          and not (set(shown[len(found):]) & arch) and len(set(shown)) == len(shown)
          and status == 200 and out.get("state") == "started" and out.get("project_id") == pg["qk_many"],
          (found, shown, want, status, out.get("state")))
    nothing = "Plan the zyxwvut gathering"
    want = await want_pick_pages(W, pool, nothing)
    none_found = ids(await index(W, pool, nothing))
    status, out, rec, model = await send(W, pool, nothing, pick_call(new=True, title="Gathering", reason="r"))
    shown = listed(model)
    check(f"Q fill: a message the search finds no page for is shown the {K} most recently active pages, "
          "not only 'new'", not none_found and shown == want and len(shown) == K, (none_found[:3], shown, want))
    t0 = time.perf_counter()
    for _ in range(3):
        _s, o, _r, filled = await send(W, pool, "quokka", pick_call(page=f"project:{pg['qk_many']}", reason="r"))
    fill_ms = (time.perf_counter() - t0) * 1000 / 3
    print(f"   a send whose list is filled, the model's own time left out: {fill_ms:.1f} ms (mean of 3)")
    check(f"Q fill: a send whose list is filled to {K} lands under {CEILING_MS} ms without the model's time",
          fill_ms < CEILING_MS and len(listed(filled)) == K, (round(fill_ms), len(listed(filled))))

    # Clip: the pick reads the opening of a long message; the turn reads it all.
    cap = getattr(tp, "INDEX_PICK_MESSAGE_CHARS", None)
    check("Q clip: the pick reads at most 2,000 characters of a message", cap == 2_000, cap)
    cap = 2_000
    long_msg = "Please look at the quokka feeder notes again. " + "The camera log says " * 1200 + "END-OF-IT"
    n_inner = len(INNER_CALLS)
    status, out, rec, model = await send(W, pool, long_msg, pick_call(page=f"project:{pg['qk_many']}", reason="r"),
                                         current=pg["qk_one"])
    msgs = model.calls[0][0] if model.calls else [{}, {}]
    copy = msgs[-1].get("content", "")
    sysmsg = msgs[0].get("content", "")
    check(f"Q clip: a {len(long_msg):,}-character message reaches the pick as its opening, ended with '…', "
          "while the turn on the page reads it whole",
          len(copy) <= cap + 1 and copy.endswith("…") and copy.startswith("Please look at the quokka feeder")
          and "END-OF-IT" not in copy and len(INNER_CALLS) == n_inner + 1
          and INNER_CALLS[-1].get("message") == long_msg, (len(copy), copy[-40:]))
    left = len(long_msg) - (len(copy) - 1)
    check("Q clip: ...cut at a space, and the Host is told how much more follows",
          long_msg.startswith(copy[:-1]) and long_msg[len(copy) - 1] == " "
          and f"{left:,} more characters follow" in sysmsg and "gets the whole message" in sysmsg,
          sysmsg[-200:])
    size = pick_input_tokens(model) if model.calls else None
    check(f"Q clip: the pick's input with a long message and {len(listed(model))} pages listed stays within "
          f"{PICK_BUDGET_TOKENS} tokens (chars/4): {size}", size is not None and size <= PICK_BUDGET_TOKENS, size)
    exact = "q" * cap
    m = tp.build_index_messages(exact, [])
    copy_fn = getattr(tp, "index_pick_copy", None)
    got = copy_fn("x" * (cap + 1)) if copy_fn else None
    check("Q clip: at exactly 2,000 characters a message is whole, with no note; one more with no space to cut "
          "at is cut at 2,000, one character left out",
          m[-1]["content"] == exact and "more characters follow" not in m[0]["content"]
          and got == ("x" * cap + "…", 1), (len(m[-1]["content"]), got and (len(got[0]), got[1])))

    # One send per request id.
    have = hasattr(W, "index_send_once")
    check("Q the send runs once per request id (index_send_once)", have)
    if not have:
        return
    before, n_inner = await counts(pool), len(INNER_CALLS)
    model = HeldModel(pick_call(page=f"project:{pg['qk_many']}", reason="It is about the quokka notes."))
    text = "Did the quokka eat today?"
    first = asyncio.ensure_future(send_once(W, pool, text, "rq-1", model))
    out = await picking(model)
    # The second request runs before the pick is let go: it is scheduled first.
    second = asyncio.ensure_future(send_once(W, pool, text, "rq-1", model))
    await asyncio.sleep(0)
    held = not first.done() and not second.done()
    model.release()
    (s1, o1), (s2, o2) = await asyncio.gather(first, second)
    await settle((o1.get("turn") or {}).get("turn_id"))
    check("Q a double Enter (a second request with the id while the first's pick is held): one pick, one "
          "marker, one turn",
          out and held and len(model.calls) == 1 and await counts(pool) == (before[0], before[1] + 1)
          and len(INNER_CALLS) == n_inner + 1, (out, held, len(model.calls), await counts(pool), before,
                                                len(INNER_CALLS) - n_inner))
    check("Q ...and both requests get the one answer, naming the one turn",
          s1 == s2 == 200 and o1 == o2 and o1.get("state") == "started" and (o1.get("turn") or {}).get("turn_id"),
          (o1, o2))
    s3, o3 = await send_once(W, pool, text, "rq-1", model)
    check("Q a retry after the answer gets that answer again: no pick, no turn",
          (s3, o3) == (s1, o1) and len(model.calls) == 1 and len(INNER_CALLS) == n_inner + 1, (s3, o3))
    s4, o4 = await send_once(W, pool, text, "rq-2", model)
    await settle((o4.get("turn") or {}).get("turn_id"))
    check("Q another id is another send, the same words or not",
          s4 == 200 and len(model.calls) == 2 and len(INNER_CALLS) == n_inner + 2
          and (o4.get("turn") or {}).get("turn_id") != (o1.get("turn") or {}).get("turn_id"), (s4, o4))
    before, n_calls = await counts(pool), len(model.calls)
    s5, o5 = await send_once(W, pool, "Something else entirely", "rq-1", model)
    check("Q an id already used for other words is refused (409), nothing picked or written",
          s5 == 409 and o5.get("state") == "request_id_reused" and len(model.calls) == n_calls
          and await counts(pool) == before, (s5, o5))
    s6, o6 = await send_once(W, pool, text, "not an id!", model)
    s7, _o7 = await send_once(W, pool, text, "x" * 65, model)
    check("Q an id of another shape is a 400, and the Host is asked nothing",
          s6 == 400 and s7 == 400 and len(model.calls) == n_calls, (s6, o6, s7))

    # A send that failed is not kept: the same id runs again.
    real_write = tp.write_index_marker

    async def broken(*a, **k):
        raise RuntimeError("the marker write failed")
    tp.write_index_marker = broken
    raised = False
    try:
        await send_once(W, pool, "Is the quokka gate shut?", "rq-3", model)
    except RuntimeError:
        raised = True
    finally:
        tp.write_index_marker = real_write
    n_calls, n_inner = len(model.calls), len(INNER_CALLS)
    s8, o8 = await send_once(W, pool, "Is the quokka gate shut?", "rq-3", model)
    await settle((o8.get("turn") or {}).get("turn_id"))
    check("Q a send that failed (its landing raised, nothing written) is not kept: the same id runs again",
          raised and s8 == 200 and o8.get("state") == "started" and len(model.calls) == n_calls + 1
          and len(INNER_CALLS) == n_inner + 1, (raised, s8, o8.get("state")))

    # A request that goes away mid-pick: the send goes on, once.
    before, n_inner = await counts(pool), len(INNER_CALLS)
    slow = HeldModel(pick_call(page=f"project:{pg['qk_many']}", reason="r"))
    gone = asyncio.ensure_future(send_once(W, pool, "Count the quokkas.", "rq-4", slow))
    out = await picking(slow)
    gone.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await gone
    went = out and gone.cancelled()
    slow.release()
    s9, o9 = await send_once(W, pool, "Count the quokkas.", "rq-4", slow)
    await settle((o9.get("turn") or {}).get("turn_id"))
    check("Q a request that goes away mid-pick loses nothing: the send lands once, and the retry gets its answer",
          went and s9 == 200 and o9.get("state") == "started" and len(slow.calls) == 1
          and await counts(pool) == (before[0], before[1] + 1) and len(INNER_CALLS) == n_inner + 1,
          (s9, o9.get("state"), len(slow.calls), len(INNER_CALLS) - n_inner))

    # The route sends the id it is given.
    seen: list = []
    real_service = W.index_send_service

    async def counting(*a, **k):
        seen.append(a[2] if len(a) > 2 else k.get("text"))
        return 200, {"state": "ask", "failure": "scripted", "candidates": []}
    W.index_send_service = counting
    req = types.SimpleNamespace(state=types.SimpleNamespace(app_token=types.SimpleNamespace(
        app_id=APP, permissions=PERMS)))
    try:
        a, err_a = await attempt(lambda: W.index_send(W.IndexSend(text="Route words", request_id="rq-route"), req))
        b, err_b = await attempt(lambda: W.index_send(W.IndexSend(text="Route words", request_id="rq-route"), req))
    finally:
        W.index_send_service = real_service
    check("Q the route sends the book's request id: two posts with one id are one send",
          seen == ["Route words"] and a == b and a is not None, (seen, a, b, err_a, err_b))


# ── R: the send, durable ────────────────────────────────────────────────────

ROW_TASKS: list = []


def restart(W):
    """The server restarts. What index_send_once holds in memory goes with the
    process; the database stays. (The scripted turns of this section have all
    ended by then, as a restart ends the real ones.)"""
    W._SENDS.clear()


def _user_row_tags(kwargs, turn_row=False) -> dict:
    """The tags session.py's _send_message_core gives the user row of a turn:
    the app, the page's scope, and the sender's id when the send carried one,
    with the ids of words that attached to the turn before its row was
    written (session._turn_row_ids; absent before it: the send's own)."""
    from mastercode.memory_engine import derive_source_context
    from mastercode.runtime import session as S
    tags = {"app_id": kwargs.get("app_id"),
            "source_context": derive_source_context("chat", project_id=kwargs.get("project_id"))}
    ids = (getattr(S, "_turn_row_ids", lambda c: c)(kwargs.get("client_id")) if turn_row
           else kwargs.get("client_id"))
    cid = S.row_client_id(ids)
    if cid:
        tags["client_id"] = cid
    return tags


def _note_row(task) -> None:
    """session.py notes the task writing the turn's user row."""
    from mastercode.runtime import session as S
    getattr(S, "_turn_row_task", lambda t: None)(task)


async def durable_inner(*args, **kwargs):
    """The turn's body for this section: records how the turn was asked for,
    and writes the person's user row as the runtime does, in a task of its own
    that lands after the turn's first frame (the row is written after an embed
    in session.py, so it lands after the answer)."""
    from mastercode.memory_engine import write_turn_row
    from server import brain_service as bs
    INNER_CALLS.append(dict(kwargs))
    ROW_TASKS.append(asyncio.ensure_future(write_turn_row(
        bs.get_pool(), E, f"durable-{len(INNER_CALLS)}", "user", kwargs.get("message", ""),
        project_id=kwargs.get("project_id"), tags=_user_row_tags(kwargs, turn_row=True),
        created_at=datetime.datetime.now(datetime.timezone.utc))))
    _note_row(ROW_TASKS[-1])
    yield '0:"On it."\n'


async def dying_inner(*args, **kwargs):
    """A turn that dies before it writes the person's row (the process stops
    first, the runtime fails first): the answer was given, nothing was
    delivered."""
    DIED_CALLS.append(dict(kwargs))
    yield '3:"the turn stopped before it read the message"\n'


DIED_CALLS: list = []


class RunningTurn:
    """A turn in its tool loop on a page, held open until released. It names
    its session in its first frame, as the runtime does (the registry reads it
    to push nudges: turn_registry._observe), and when let go it drains the
    runtime's own nudge queue as session.py's drain does: the senders' ids are
    read first, then the words, and each is written as a user row stamped with
    when it was sent and keeping its sender's id."""

    def __init__(self, pid, sid, own_row=False):
        self.pid, self.sid = pid, sid
        self.go = asyncio.Event()
        self.drained: list = []              # (words, the sender's id) as the queue held them
        # Writes the turn's own user row first, as the runtime does before its
        # tool loop (session.py), so the row has taken its ids.
        self.own_row = own_row

    async def inner(self, *args, **kwargs):
        from mastercode.memory_engine import write_turn_row
        from mastercode.runtime import session as S
        from server import brain_service as bs
        INNER_CALLS.append(dict(kwargs))
        if self.own_row:
            ROW_TASKS.append(asyncio.ensure_future(write_turn_row(
                bs.get_pool(), E, self.sid, "user", kwargs.get("message", ""), project_id=self.pid,
                tags=_user_row_tags(kwargs, turn_row=True),
                created_at=datetime.datetime.now(datetime.timezone.utc))))
            _note_row(ROW_TASKS[-1])
        yield "2:" + json.dumps([{"type": "session", "session_id": self.sid, "turn_token": "tk-" + self.sid}]) + "\n"
        await self.go.wait()
        cids = S.nudge_ids(self.sid)
        timed = S.consume_nudges_timed(self.sid)
        for i, (text, at) in enumerate(timed):
            cid = cids[i] if i < len(cids) else None
            self.drained.append((text, cid))
            await write_turn_row(bs.get_pool(), E, self.sid, "user", text, project_id=self.pid,
                                 tags={**_user_row_tags({"app_id": APP, "project_id": self.pid}), "nudge": True,
                                       **({"client_id": cid} if cid else {})},
                                 created_at=at)
        yield '0:"Done."\n'


class UnreadTurn:
    """A turn in its tool loop on a page that ends WITHOUT reading the words
    sent into it: it names its session as RunningTurn does, and when let go it
    runs the runtime's TurnEnd (session.end_turn_input), as every exit of a
    real turn does, which takes the words off its queue for the thread's next
    turn (turn_registry._requeue_undelivered)."""

    def __init__(self, pid, sid):
        self.pid, self.sid = pid, sid
        self.go = asyncio.Event()

    async def inner(self, *args, **kwargs):
        from mastercode.runtime import session as S
        INNER_CALLS.append(dict(kwargs))
        yield "2:" + json.dumps([{"type": "session", "session_id": self.sid, "turn_token": "tk-" + self.sid}]) + "\n"
        await self.go.wait()
        S.end_turn_input(self.sid)
        yield '0:"Done, without reading what came in."\n'


REAL: dict = {}                    # brain_service's own _send_message_inner, before the run scripts it


def real_signature(gen_fn):
    """`gen_fn` behind the signature of the real _send_message_inner: the
    follow-up builder binds a turn's arguments against it (brain_service
    ._followup_maker), so the thread's next turn is asked for as in production."""
    import functools

    @functools.wraps(REAL["inner"])
    async def inner(*a, **k):
        async for f in gen_fn(*a, **k):
            yield f
    return inner


def past_window(W):
    """SEND_REPEAT_S later, in the same process: the record of sends has let the
    ids go (_forget_sends drops what is older), so a retry now is answered from
    the database or runs again. No restart."""
    for e in list(W._SENDS.values()):
        e.at -= W.SEND_REPEAT_S + 1


async def requeued_turn(rec):
    """The thread's next turn that the ended turn `rec` sent its unread words on
    as (its nudge_requeued frame names it), once it has ended, with its row."""
    tid = None
    for f in (rec.frames if rec is not None else []):
        if "nudge_requeued" in f:
            tid = (json.loads(f[2:]) or [{}])[0].get("turn_id")
    nxt = await settle(tid)
    await asyncio.gather(*ROW_TASKS)
    return nxt


async def carrying(pool, rid):
    """(markers, user rows) that carry the request id, read off the table: as a
    row's client_id, or in the list of ids a row of several messages keeps."""
    rows = await pool.fetch("SELECT id, project_id, content, metadata FROM entity_memory"
                            " WHERE entity_id = $1 AND (metadata->'client_id') @> to_jsonb($2::text) ORDER BY id",
                            E, rid)
    mds = [(r, json.loads(r["metadata"]) if isinstance(r["metadata"], str) else r["metadata"]) for r in rows]
    return ([r for r, md in mds if md.get("source_kind") == "index_pick"],
            [(r, md) for r, md in mds if md.get("role") == "user"])


def _TR_PENDING():
    """The registry's detached row writes in flight (markers included)."""
    from server import turn_registry as TR
    return set(getattr(TR, "_PENDING_ROW_WRITES", set()))


def without_turn(body):
    """A send's answer less the live handle of its turn, which a restart ends."""
    return {k: v for k, v in body.items() if k not in ("turn", "stream")}


async def r_late(W, tp, pool, pg):
    """A retry that comes after the in-process record let its id go (no
    restart), for the two ways a send can leave no row with the id."""
    print("-- R: a late retry, when the words went on as the next turn, or attached to the same words")
    from mastercode.runtime import session as S
    from server import brain_service as bs
    from server.turn_registry import get_turn_registry
    real_inner = bs._send_message_inner

    async def running_on(page, holder, words):
        bs._send_message_inner = real_signature(holder.inner)
        meta, err = await W.start_page_turn(E, page, words, app_id=APP, app_permissions=PERMS)
        rec = get_turn_registry().get((meta or {}).get("turn_id"))
        for _ in range(200):
            if rec is not None and rec.session_id == holder.sid:
                break
            await asyncio.sleep(0.01)
        return rec, err

    try:
        # (a) one send nudged into a turn that ends without reading it: its words go on as the
        #     thread's next turn, whose row must keep the id.
        page = pg["qk_none"]
        unread = UnreadTurn(page, "sess-unread-a")
        rec, err = await running_on(page, unread, "Sort the shed today.")
        rid, text = "rr-unread", "Water the tomatoes too."
        model = Model(pick_call(page=f"project:{page}", reason="It is the plain page."))
        n_inner = len(INNER_CALLS)
        s1, o1 = await send_once(W, pool, text, rid, model, current=page)
        check("R(a) a send onto a page whose turn runs is a nudge, its id in the runtime's queue",
              s1 == 200 and o1.get("state") == "nudged" and S.nudge_ids(unread.sid) == [rid],
              (s1, o1.get("state"), S.nudge_ids(unread.sid), err))
        bs._send_message_inner = durable_inner          # the thread's next turn
        unread.go.set()
        await settle(rec.turn_id if rec is not None else None)
        nxt = await requeued_turn(rec)
        markers, users = await carrying(pool, rid)
        check("R(a) the turn ends without reading it: the words go on as the thread's next turn, once, and that "
              "turn's user row keeps the id",
              nxt is not None and len(INNER_CALLS) == n_inner + 1 and len(users) == 1
              and users[0][0]["content"] == text and users[0][0]["project_id"] == page
              and users[0][1].get("client_id") == rid and len(markers) == 1,
              (nxt is not None, len(INNER_CALLS) - n_inner, [(u[0]["content"], u[1].get("client_id")) for u in users],
               len(markers)))
        past_window(W)
        s2, o2 = await send_once(W, pool, text, rid, model, current=page)
        markers, users = await carrying(pool, rid)
        check("R(a) a retry after the in-memory window (no restart) is answered from the database: the page, its "
              "marker and reason, the words started as their own turn",
              s2 == 200 and o2.get("project_id") == page and o2.get("marker") == o1.get("marker")
              and o2.get("reason") == o1.get("reason") and o2.get("state") == "started", (s2, o2))
        check("R(a) ...and delivers nothing again: no second pick, marker or turn",
              len(model.calls) == 1 and len(markers) == 1 and len(users) == 1 and len(INNER_CALLS) == n_inner + 1,
              (len(model.calls), len(markers), len(users), len(INNER_CALLS) - n_inner))

        # (a) two sends nudged into one turn that reads neither: they go on joined as one turn, whose one
        #     row keeps both ids as a list; a late retry of either finds its words delivered.
        page = pg["led_new"]
        unread = UnreadTurn(page, "sess-unread-b")
        rec, err = await running_on(page, unread, "Check the ledger totals.")
        (rid_a, text_a), (rid_b, text_b) = ("rr-join-a", "And the March column."), ("rr-join-b", "Also the April one.")
        model_a = Model(pick_call(page=f"project:{page}", reason="The ledger page."))
        model_b = Model(pick_call(page=f"project:{page}", reason="The ledger page."))
        n_inner = len(INNER_CALLS)
        sa, oa = await send_once(W, pool, text_a, rid_a, model_a, current=page)
        sb, ob = await send_once(W, pool, text_b, rid_b, model_b, current=page)
        bs._send_message_inner = durable_inner
        unread.go.set()
        await settle(rec.turn_id if rec is not None else None)
        await requeued_turn(rec)
        _ma, users_a = await carrying(pool, rid_a)
        _mb, users_b = await carrying(pool, rid_b)
        joined = users_a[0][1].get("client_id") if users_a else None
        check("R(a) two unread sends go on as ONE turn: one row with both words, keeping both ids as a list",
              sa == sb == 200 and oa.get("state") == ob.get("state") == "nudged" and len(INNER_CALLS) == n_inner + 1
              and len(users_a) == len(users_b) == 1 and users_a[0][0]["id"] == users_b[0][0]["id"]
              and joined == [rid_a, rid_b] and text_a in users_a[0][0]["content"] and text_b in users_a[0][0]["content"],
              (sa, sb, oa.get("state"), ob.get("state"), len(INNER_CALLS) - n_inner, joined, len(users_a), len(users_b)))
        past_window(W)
        ra = await send_once(W, pool, text_a, rid_a, model_a, current=page)
        rb = await send_once(W, pool, text_b, rid_b, model_b, current=page)
        check("R(a) ...a late retry of either is answered from the database, and nothing is delivered again",
              ra[0] == rb[0] == 200 and ra[1].get("project_id") == rb[1].get("project_id") == page
              and ra[1].get("state") == rb[1].get("state") == "started"
              and len(model_a.calls) == len(model_b.calls) == 1 and len(INNER_CALLS) == n_inner + 1,
              (ra, rb, len(model_a.calls), len(model_b.calls), len(INNER_CALLS) - n_inner))

        # (b) the same words already running on the page, whose user row is written: the send attaches
        #     to that turn and no row carries its id; the marker says so once that row is written
        #     and a late retry, after that turn ended, starts nothing.
        page = pg["bot_conv"]
        words = "Restart the helpdesk bot."
        held = RunningTurn(page, "sess-same-words", own_row=True)
        rec, err = await running_on(page, held, words)
        await asyncio.gather(*ROW_TASKS)
        rid = "rr-reattached"
        model = Model(pick_call(page=f"project:{page}", reason="The helpdesk page."))
        n_inner = len(INNER_CALLS)
        s1, o1 = await send_once(W, pool, words, rid, model, current=page)
        md = {}
        for _ in range(300):                            # marked once the turn's row is written
            markers, users = await carrying(pool, rid)
            md = (json.loads(markers[-1]["metadata"]) if isinstance(markers[-1]["metadata"], str)
                  else markers[-1]["metadata"]) if markers else {}
            if (md.get("index_send") or {}).get("reattached") is True:
                break
            await asyncio.sleep(0.01)
        check("R(b) a send of the words the page is already running attaches to that turn (reattached), and its "
              "marker records it (index_send.reattached)",
              s1 == 200 and o1.get("state") == "reattached" and len(markers) == 1 and not users
              and (md.get("index_send") or {}).get("reattached") is True and len(INNER_CALLS) == n_inner,
              (s1, o1.get("state"), len(markers), len(users), md.get("index_send"), len(INNER_CALLS) - n_inner))
        bs._send_message_inner = durable_inner
        held.go.set()
        await settle(rec.turn_id if rec is not None else None)
        past_window(W)
        s2, o2 = await send_once(W, pool, words, rid, model, current=page)
        await settle((o2.get("turn") or {}).get("turn_id"))
        await asyncio.gather(*ROW_TASKS)
        markers, users = await carrying(pool, rid)
        check("R(b) a retry after the window, the running turn over, is answered from the marker (reattached)",
              s2 == 200 and o2 == without_turn(o1), (s2, o2, without_turn(o1)))
        check("R(b) ...and starts nothing: no second pick, marker or turn, no row with the id",
              len(model.calls) == 1 and len(markers) == 1 and not users and len(INNER_CALLS) == n_inner,
              (len(model.calls), len(markers), len(users), len(INNER_CALLS) - n_inner))

        # (d) the words a turn ends without reading are the very words the thread's next turn already
        #     waits on (the same words sent again, under another id, after the first turn stopped
        #     reading): they attach to that turn before the nudge path (start_or_attach, "reattached"),
        #     so no row can carry their id. The marker records it, and a late retry starts nothing.
        page = pg["stripe_w2"]
        unread = UnreadTurn(page, "sess-unread-d")
        rec, err = await running_on(page, unread, "Look at the payout schedule.")
        words, rid_x, rid_y = "Chase the March payout.", "rr-same-x", "rr-same-y"
        model_x = Model(pick_call(page=f"project:{page}", reason="The payouts page."))
        model_y = Model(pick_call(page=f"project:{page}", reason="The payouts page."))
        n_inner = len(INNER_CALLS)
        sx, ox = await send_once(W, pool, words, rid_x, model_x, current=page)
        S.release_holds(unread.sid)                     # the turn reads no more: words now start a turn of their own
        bs._send_message_inner = durable_inner          # that next turn's body
        sy, oy = await send_once(W, pool, words, rid_y, model_y, current=page)
        unread.go.set()
        await settle(rec.turn_id if rec is not None else None)
        nxt = await requeued_turn(rec)
        await asyncio.gather(*ROW_TASKS)
        _mx, users_x = await carrying(pool, rid_x)
        _my, users_y = await carrying(pool, rid_y)
        check("R(d) the first send is a nudge into the turn; the same words sent after it stopped reading wait as "
              "a turn of their own",
              sx == sy == 200 and ox.get("state") == "nudged" and oy.get("state") == "started"
              and S.nudge_ids(unread.sid) == [], (sx, sy, ox.get("state"), oy.get("state"), S.nudge_ids(unread.sid), err))
        check("R(d) the unread words go on to the waiting turn on the same words (it is the turn they attach to; "
              "nothing starts for them) and the Host has the words once, on that turn's row, which keeps both "
              "ids (the attached id rides the row)",
              nxt is not None and nxt.turn_id == (oy.get("turn") or {}).get("turn_id")
              and len(INNER_CALLS) == n_inner + 1 and len(users_y) == 1 and users_y[0][0]["content"] == words
              and users_y[0][1].get("client_id") == [rid_y, rid_x] and len(users_x) == 1
              and users_x[0][0]["id"] == users_y[0][0]["id"],
              (nxt is not None and nxt.turn_id, (oy.get("turn") or {}).get("turn_id"), len(INNER_CALLS) - n_inner,
               [(u[0]["content"], u[1].get("client_id")) for u in users_y], len(users_x)))
        markers_x, _u = await carrying(pool, rid_x)
        marker_md = (json.loads(markers_x[-1]["metadata"]) if isinstance(markers_x[-1]["metadata"], str)
                     else markers_x[-1]["metadata"]) if markers_x else {}
        check("R(d) no flag is set on the attached id's marker before (or instead of) that row",
              len(markers_x) == 1 and (marker_md.get("index_send") or {}).get("reattached") is not True,
              (len(markers_x), marker_md.get("index_send")))
        past_window(W)
        s2, o2 = await send_once(W, pool, words, rid_x, model_x, current=page)
        await settle((o2.get("turn") or {}).get("turn_id"))
        await asyncio.gather(*ROW_TASKS)
        markers_x, users_x = await carrying(pool, rid_x)
        held_words = await pool.fetchval(
            "SELECT count(*) FROM entity_memory WHERE entity_id = $1 AND project_id = $2"
            " AND metadata->>'role' = 'user' AND content = $3", E, page, words)
        check("R(d) a retry of the attached id after the window is answered from the row that delivered it, on "
              "the same page", s2 == 200 and o2.get("state") in ("started", "reattached")
              and o2.get("project_id") == page and o2.get("marker") == ox.get("marker"), (s2, o2))
        check("R(d) ...and starts nothing: no second pick, marker or turn, one row with the id, the words once",
              len(model_x.calls) == 1 and len(markers_x) == 1 and len(users_x) == 1
              and len(INNER_CALLS) == n_inner + 1 and held_words == 1,
              (len(model_x.calls), len(markers_x), len(users_x), len(INNER_CALLS) - n_inner, held_words))

        # (e) the same as (d), but the process ends after the unread words attached to the
        #     waiting turn and before that turn wrote its row: the turn never runs. A late retry of the
        #     attached id must then run the words, never be answered "reattached" from a flag.
        page = pg["stripe_w2"]
        unread = UnreadTurn(page, "sess-unread-e")
        rec, err = await running_on(page, unread, "Look at the refund schedule.")
        words, rid_x, rid_y = "Chase the April refund.", "rr-gone-x", "rr-gone-y"
        model_x = Model(pick_call(page=f"project:{page}", reason="The payouts page."))
        model_y = Model(pick_call(page=f"project:{page}", reason="The payouts page."))
        sx, ox = await send_once(W, pool, words, rid_x, model_x, current=page)
        S.release_holds(unread.sid)
        bs._send_message_inner = real_signature(dying_inner)   # the waiting turn dies before its row
        sy, oy = await send_once(W, pool, words, rid_y, model_y, current=page)
        n_died = len(DIED_CALLS)
        unread.go.set()
        await settle(rec.turn_id if rec is not None else None)
        nxt = await requeued_turn(rec)
        await asyncio.gather(*list(_TR_PENDING()))
        markers_x, users_x = await carrying(pool, rid_x)
        marker_md = (json.loads(markers_x[-1]["metadata"]) if isinstance(markers_x[-1]["metadata"], str)
                     else markers_x[-1]["metadata"]) if markers_x else {}
        check("R(e) the unread words attached to the waiting turn, which died before its row: no row and no "
              "flag carries the attached id",
              sx == sy == 200 and ox.get("state") == "nudged" and len(DIED_CALLS) == n_died + 1
              and not users_x and (marker_md.get("index_send") or {}).get("reattached") is not True,
              (sx, sy, ox.get("state"), len(DIED_CALLS) - n_died, len(users_x), marker_md.get("index_send")))
        restart(W)
        bs._send_message_inner = durable_inner
        s2, o2 = await send_once(W, pool, words, rid_x, model_x, current=page)
        await settle((o2.get("turn") or {}).get("turn_id"))
        await asyncio.gather(*ROW_TASKS)
        check("R(e) after the restart, a retry of the attached id is not answered as attached to a turn that "
              "never ran (the words are still held for the boot pass, pending_words)",
              s2 == 200 and o2.get("state") != "reattached", (s2, o2.get("state")))

        # (f) the Index's own attach: a send of the words a turn on the page is waiting to run
        #     (it has not written its row) attaches to it; the process ends before that turn runs. A
        #     late retry must run the words.
        page = pg["bot_conv"]
        words = "Rotate the helpdesk keys."
        held = RunningTurn(page, "sess-not-yet")        # running, its own row not written
        rec, err = await running_on(page, held, words)
        rid = "rr-attach-gone"
        model = Model(pick_call(page=f"project:{page}", reason="The helpdesk page."))
        s1, o1 = await send_once(W, pool, words, rid, model, current=page)
        await asyncio.gather(*list(_TR_PENDING()))
        markers, users = await carrying(pool, rid)
        md = (json.loads(markers[-1]["metadata"]) if isinstance(markers[-1]["metadata"], str)
              else markers[-1]["metadata"]) if markers else {}
        check("R(f) a send of the words the page is about to run attaches to that turn (reattached), and no flag "
              "is set on its marker before that turn's row",
              s1 == 200 and o1.get("state") == "reattached" and len(markers) == 1 and not users
              and (md.get("index_send") or {}).get("reattached") is not True,
              (s1, o1.get("state"), len(markers), len(users), md.get("index_send")))
        if rec is not None and rec.task is not None:
            rec.task.cancel()                           # the process ends: that turn never runs on
            await settle(rec.turn_id)
        restart(W)
        bs._send_message_inner = durable_inner
        n_inner = len(INNER_CALLS)
        s2, o2 = await send_once(W, pool, words, rid, model, current=page)
        await settle((o2.get("turn") or {}).get("turn_id"))
        await asyncio.gather(*ROW_TASKS)
        markers, users = await carrying(pool, rid)
        check("R(f) after the restart, a retry runs the words: one turn, one row with the id",
              s2 == 200 and o2.get("state") == "started" and len(INNER_CALLS) == n_inner + 1 and len(users) == 1,
              (s2, o2.get("state"), len(INNER_CALLS) - n_inner, len(users)))
    finally:
        bs._send_message_inner = real_inner


async def r_pii(W, tp, pool, pg):
    """(c) PII mode: a request id is a token the client made up (room for a few
    words), so no row keeps it as written; the marker and the user row keep its
    digest, and the lookup matches by the digest."""
    print("-- R: PII mode keeps a digest of the request id, never the id")
    from mastercode import pii_client
    from mastercode import pii_queue as PQ
    from server import brain_service as bs
    saved = {k: os.environ.get(k) for k in ("PII_MODE", "PII_QUEUE_WORKER")}
    real_inner, real_sterilize = bs._send_message_inner, pii_client.sterilize

    async def sterilize(text, scope, verify=False, pool=None, entity_id=None):
        return pii_client.PiiResult(ok=True, text=text)
    os.environ["PII_MODE"], os.environ["PII_QUEUE_WORKER"] = "on", "off"
    pii_client.sterilize = sterilize
    bs._send_message_inner = durable_inner
    try:
        page = pg["nar_desc"]
        rid, text = "rr-pii-tell-them-my-name-is-ada", "Note the narwhal count for the bay."
        model = Model(pick_call(page=f"project:{page}", reason="The bay survey."))
        n_inner = len(INNER_CALLS)
        s1, o1 = await send_once(W, pool, text, rid, model, current=page)
        await settle((o1.get("turn") or {}).get("turn_id"))
        await asyncio.gather(*ROW_TASKS)
        for _ in range(20):                      # the queue lands the user row (the worker, driven here)
            if not await PQ.drain_once():
                break
        rows = await pool.fetch("SELECT id, metadata FROM entity_memory WHERE entity_id = $1 AND project_id = $2"
                                " AND metadata::text LIKE '%client_id%' ORDER BY id", E, page)
        mds = [json.loads(r["metadata"]) if isinstance(r["metadata"], str) else r["metadata"] for r in rows]
        marker = [m for m in mds if m.get("source_kind") == "index_pick"]
        user = [m for m in mds if m.get("role") == "user"]
        raw_left = await pool.fetchval("SELECT count(*) FROM entity_memory WHERE entity_id = $1"
                                       " AND strpos(metadata::text, $2) > 0", E, rid)
        stored = [m.get("client_id_digest") for m in marker + user]
        check("R(c) PII mode: the marker and the user row keep a digest of the request id, not the id, and no row "
              "holds the id as written",
              s1 == 200 and o1.get("state") == "started" and len(marker) == 1 and len(user) == 1
              and all(isinstance(v, str) and v != rid and re.fullmatch(r"[0-9a-f]{64}", v) for v in stored)
              and len(set(stored)) == 1 and all("client_id" not in m for m in marker + user) and raw_left == 0,
              (s1, o1.get("state"), len(marker), len(user), stored, [sorted(m) for m in marker + user], raw_left))
        past_window(W)
        s2, o2 = await send_once(W, pool, text, rid, model, current=page)
        check("R(c) ...and a late retry still finds the send by it: the first answer, nothing delivered again",
              s2 == 200 and o2 == without_turn(o1) and len(model.calls) == 1 and len(INNER_CALLS) == n_inner + 1,
              (s2, o2, len(model.calls), len(INNER_CALLS) - n_inner))
        scrubbed = PQ.scrub_metadata({"client_id": ["rr-a", "rr-b"], "agent_title": "free text"})
        digest = getattr(PQ, "client_id_digest", None)
        check("R(c) PII mode keeps the digest of each of several ids (a joined turn's list), and drops what is not "
              "on its allowlist",
              digest is not None and scrubbed == {"client_id_digest": [digest("rr-a"), digest("rr-b")]}, scrubbed)
    finally:
        bs._send_message_inner, pii_client.sterilize = real_inner, real_sterilize
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        while PQ.stats()["pending"]:
            PQ._queue.popleft()


async def r_durable(W, tp, pool, pg):
    print("-- R: a send delivers once under its request id, across a restart")
    from mastercode import pii_queue as PQ
    from mastercode.runtime import session as S
    from server import brain_service as bs
    from server.turn_registry import get_turn_registry
    check("R thread_pick finds a send by its request id (find_index_send)", hasattr(tp, "find_index_send"))
    page = pg["qk_many"]
    pick = pick_call(page=f"project:{page}", reason="It is about the quokka notes.")
    real_inner = bs._send_message_inner
    bs._send_message_inner = durable_inner
    try:
        # 1. a restart between the send and its retry.
        rid, text = "rr-restart", "Did the quokka eat the whole feeder today?"
        model = Model(pick)
        before, n_inner = await counts(pool), len(INNER_CALLS)
        s1, o1 = await send_once(W, pool, text, rid, model, current=page)
        await settle((o1.get("turn") or {}).get("turn_id"))
        await asyncio.gather(*ROW_TASKS)
        markers, users = await carrying(pool, rid)
        check("R the first send keeps the id on its marker (the landing's) and on the turn's user row, and the "
              "row holds the words",
              s1 == 200 and o1.get("state") == "started" and len(markers) == 1 and len(users) == 1
              and users[0][0]["content"] == text and markers[0]["project_id"] == page
              and users[0][0]["project_id"] == page, (s1, o1.get("state"), len(markers), len(users)))
        restart(W)
        s2, o2 = await send_once(W, pool, text, rid, model, current=page)
        markers, users = await carrying(pool, rid)
        check("R after a restart, the retry is answered with the first send's answer (all but the live turn, which "
              "the restart ended)", s2 == 200 and o2 == without_turn(o1) and o2.get("state") == "started"
              and o2.get("project_id") == page and o2.get("marker") and o2.get("reason") == o1.get("reason"),
              (s2, o2, without_turn(o1)))
        check("R ...and delivers nothing again: one user row, one turn, one pick, one marker",
              len(users) == 1 and len(INNER_CALLS) == n_inner + 1 and len(model.calls) == 1
              and len(markers) == 1 and await counts(pool) == (before[0], before[1] + 1),
              (len(users), len(INNER_CALLS) - n_inner, len(model.calls), len(markers), await counts(pool), before))
        # The same id for other words is still refused after a restart.
        restart(W)
        n_calls = len(model.calls)
        s3, o3 = await send_once(W, pool, "Something else entirely.", rid, model, current=page)
        markers, users = await carrying(pool, rid)
        check("R after a restart an id used for other words is still refused (409): nothing picked, written or "
              "delivered", s3 == 409 and o3.get("state") == "request_id_reused" and len(model.calls) == n_calls
              and len(users) == 1 and len(markers) == 1 and len(INNER_CALLS) == n_inner + 1, (s3, o3, len(users)))
        # ...and the words under it are still answered.
        s3b, o3b = await send_once(W, pool, text, rid, model, current=page)
        check("R ...and the first words, retried after that, still get their answer",
              s3b == 200 and o3b == without_turn(o1), (s3b, o3b))

        # 2. two retries at once after a restart.
        rid2, text2 = "rr-concurrent", "Count the quokkas at the feeder."
        model2 = Model(pick)
        n_inner = len(INNER_CALLS)
        s, first = await send_once(W, pool, text2, rid2, model2, current=page)
        await settle((first.get("turn") or {}).get("turn_id"))
        await asyncio.gather(*ROW_TASKS)
        restart(W)
        (sa, a), (sb, b) = await asyncio.gather(send_once(W, pool, text2, rid2, model2, current=page),
                                                send_once(W, pool, text2, rid2, model2, current=page))
        markers, users = await carrying(pool, rid2)
        check("R two retries together after a restart: one answer, the first send's",
              sa == sb == 200 and a == b == without_turn(first), (sa, sb, a, b))
        check("R ...and one delivery in all: one user row, one turn, one pick, one marker",
              len(users) == 1 and len(INNER_CALLS) == n_inner + 1 and len(model2.calls) == 1 and len(markers) == 1,
              (len(users), len(INNER_CALLS) - n_inner, len(model2.calls), len(markers)))

        # 3. a pick that landed and a turn that died before it wrote the words: nothing was
        #    delivered, so the id runs again.
        rid3, text3 = "rr-died", "Is the quokka gate shut tonight?"
        model3 = Model(pick)
        bs._send_message_inner = dying_inner
        n_inner, n_died = len(INNER_CALLS), len(DIED_CALLS)
        s, o = await send_once(W, pool, text3, rid3, model3, current=page)
        await settle((o.get("turn") or {}).get("turn_id"))
        markers, users = await carrying(pool, rid3)
        check("R a send whose turn died before writing the words landed its pick and delivered nothing: a marker "
              "with the id, no user row", s == 200 and len(markers) == 1 and not users
              and len(DIED_CALLS) == n_died + 1, (s, o.get("state"), len(markers), len(users)))
        bs._send_message_inner = durable_inner
        restart(W)
        s, o = await send_once(W, pool, text3, rid3, model3, current=page)
        await settle((o.get("turn") or {}).get("turn_id"))
        await asyncio.gather(*ROW_TASKS)
        markers, users = await carrying(pool, rid3)
        check("R ...after a restart it runs again under its id and delivers once (a second pick, a second marker)",
              s == 200 and o.get("state") == "started" and len(users) == 1 and len(markers) == 2
              and len(model3.calls) == 2 and len(INNER_CALLS) == n_inner + 1,
              (s, o.get("state"), len(users), len(markers), len(model3.calls)))
        restart(W)
        s, again = await send_once(W, pool, text3, rid3, model3, current=page)
        markers, users = await carrying(pool, rid3)
        check("R ...and a retry after that is answered from the later marker, delivering nothing",
              s == 200 and again == without_turn(o) and len(users) == 1 and len(model3.calls) == 2
              and len(INNER_CALLS) == n_inner + 1, (s, again, len(users), len(model3.calls)))

        # 3b. the same in one process, no restart: the retry comes once the record of
        #     sends has let the id go (SEND_REPEAT_S) while the registry still lists
        #     the turn that died (it keeps a finished turn for DONE_TTL_S, longer).
        #     That turn never had the words, so the retry runs again and delivers
        #     once; it follows no turn by the id (brain_service.send_message).
        from server.turn_registry import DONE_TTL_S
        rid3b, text3b = "rr-died-window", "Did the quokka gate latch hold tonight?"
        model3b = Model(pick)
        bs._send_message_inner = dying_inner
        n_inner, n_died = len(INNER_CALLS), len(DIED_CALLS)
        s, o = await send_once(W, pool, text3b, rid3b, model3b, current=page)
        died = await settle((o.get("turn") or {}).get("turn_id"))
        bs._send_message_inner = durable_inner
        past_window(W)
        s, o = await send_once(W, pool, text3b, rid3b, model3b, current=page)
        await settle((o.get("turn") or {}).get("turn_id"))
        await asyncio.gather(*ROW_TASKS)
        markers, users = await carrying(pool, rid3b)
        check("R ...in one process too: a retry after the record of sends let the id go, while the registry "
              "still lists the turn that died, runs again and delivers once",
              W.SEND_REPEAT_S < DONE_TTL_S and died is not None
              and get_turn_registry().get(died.turn_id) is died and len(DIED_CALLS) == n_died + 1
              and s == 200 and o.get("state") == "started" and len(users) == 1 and len(markers) == 2
              and len(model3b.calls) == 2 and len(INNER_CALLS) == n_inner + 1,
              (s, o.get("state"), len(users), len(markers), len(model3b.calls), W.SEND_REPEAT_S, DONE_TTL_S))
        past_window(W)
        s, again = await send_once(W, pool, text3b, rid3b, model3b, current=page)
        markers, users = await carrying(pool, rid3b)
        check("R ...and a retry after that is answered from the later marker, delivering nothing",
              s == 200 and again == without_turn(o) and len(users) == 1 and len(model3b.calls) == 2
              and len(INNER_CALLS) == n_inner + 1, (s, again, len(users), len(model3b.calls)))

        # 4. a send that becomes a nudge into the turn running on the page.
        rid4, text4 = "rr-nudge", "Also check the north fence."
        page4 = pg["qk_one"]
        running = RunningTurn(page4, "sess-running-r")
        bs._send_message_inner = running.inner
        meta, err = await W.start_page_turn(E, page4, "Keep going with the garden notes.", app_id=APP,
                                            app_permissions=PERMS)
        rec = get_turn_registry().get((meta or {}).get("turn_id"))
        for _ in range(200):
            if rec is not None and rec.session_id == running.sid:
                break
            await asyncio.sleep(0.01)
        model4 = Model(pick_call(page=f"project:{page4}", reason="It is the garden page."))
        n_inner = len(INNER_CALLS)
        s4, o4 = await send_once(W, pool, text4, rid4, model4, current=page4)
        queued = S.nudge_ids(running.sid)
        check("R a send onto a page whose turn is running is a nudge, and the request id reaches the runtime's "
              "nudge queue as its sender's id", s4 == 200 and o4.get("state") == "nudged"
              and o4.get("project_id") == page4 and queued == [rid4], (s4, o4.get("state"), queued, err))
        running.go.set()
        await settle((meta or {}).get("turn_id"))
        markers, users = await carrying(pool, rid4)
        check("R ...the turn reads it: the nudge's user row keeps the id, and the marker has it",
              running.drained == [(text4, rid4)] and len(users) == 1 and users[0][1].get("nudge") is True
              and len(markers) == 1, (running.drained, len(users), len(markers)))
        restart(W)
        s5, o5 = await send_once(W, pool, text4, rid4, model4, current=page4)
        markers, users = await carrying(pool, rid4)
        check("R after a restart the retry gets the first send's answer (state nudged); no second pick, turn or nudge",
              s5 == 200 and o5 == without_turn(o4) and o5.get("state") == "nudged" and len(model4.calls) == 1
              and len(INNER_CALLS) == n_inner and S.nudge_ids(running.sid) == [] and len(users) == 1
              and len(markers) == 1, (s5, o5, len(model4.calls), len(INNER_CALLS) - n_inner, len(users)))

        # 4a. a pasted key: the digest the marker keeps is of the words with the key cut, never of the words as
        #     typed, and nothing written holds the key.
        import hashlib
        from server import secret_cut
        bs._send_message_inner = durable_inner
        rid6 = "rr-secret"
        typed6 = f"{SECRET} is the key for the garden gate"
        model6 = Model(pick_call(page=f"project:{page}", reason="r"))
        s6, o6 = await send_once(W, pool, typed6, rid6, model6, current=page)
        await settle((o6.get("turn") or {}).get("turn_id"))
        await asyncio.gather(*ROW_TASKS)
        markers, users = await carrying(pool, rid6)
        stored = (json.loads(markers[0]["metadata"]) if isinstance(markers[0]["metadata"], str)
                  else markers[0]["metadata"]).get("index_send", {}).get("digest") if markers else None
        cut = secret_cut.redact_words(typed6, door="index")
        as_cut = hashlib.sha256(f"{rid6}\0{cut}".encode()).hexdigest()
        as_typed = hashlib.sha256(f"{rid6}\0{typed6}".encode()).hexdigest()
        check("R a message with a pasted key: the marker's digest is of the words with the key cut, not as typed, "
              "and the retry after a restart is still the same send",
              s6 == 200 and len(users) == 1 and stored == as_cut and stored != as_typed and "[REDACTED:" in cut,
              (s6, len(users), stored, as_cut, as_typed))
        restart(W)
        s7, o7 = await send_once(W, pool, typed6, rid6, model6, current=page)
        check("R ...and the retry after a restart, with the key in it again, is answered, delivering nothing",
              s7 == 200 and o7 == without_turn(o6) and len(model6.calls) == 1, (s7, o7))
    finally:
        bs._send_message_inner = real_inner

    # 4b. the marker keeps a digest of the id and the words (so a reused id is refused after a restart
    #     too), and in PII mode none.
    rid5, text5 = "rr-digest", "Is the garden gate shut?"
    seen_digest: list = []
    real_service = W.index_send_service

    async def recording(*a, **k):
        seen_digest.append(k.get("digest"))
        return 200, {"state": "ask", "failure": "scripted", "candidates": []}
    W.index_send_service = recording
    saved_pii = os.environ.get("PII_MODE")
    try:
        await W.index_send_once(pool, E, text5, request_id=rid5)
        restart(W)
        os.environ["PII_MODE"] = "on"
        await W.index_send_once(pool, E, text5, request_id=rid5)
    finally:
        W.index_send_service = real_service
        if saved_pii is None:
            os.environ.pop("PII_MODE", None)
        else:
            os.environ["PII_MODE"] = saved_pii
    restart(W)
    check("R the digest the marker keeps is of the id and the words (64 hex), and in PII mode there is none",
          len(seen_digest) == 2 and re.fullmatch(r"[0-9a-f]{64}", seen_digest[0] or "") and seen_digest[1] is None,
          seen_digest)
    markers, _users = await carrying(pool, "rr-restart")
    stored = (json.loads(markers[0]["metadata"]) if isinstance(markers[0]["metadata"], str)
              else markers[0]["metadata"]).get("index_send", {}) if markers else {}
    check("R ...and the marker holds the answer's fields beside it (title, restored, created, reason)",
          bool(re.fullmatch(r"[0-9a-f]{64}", str(stored.get("digest") or ""))) and stored.get("title") == "Field notes"
          and stored.get("restored") is False and stored.get("created") is False
          and stored.get("reason") == "It is about the quokka notes.", stored)

    # 5. the lookup is one index read; the migration and init-db.sql make the same index.
    await pool.execute("ANALYZE entity_memory")
    sql = getattr(tp, "INDEX_SEND_ROWS_SQL", None)
    for generic in (False, True):
        nodes = await plan_nodes(pool, sql, (E, "rr-restart"), generic=generic) if sql else []
        used = {n[2] for n in nodes if n[2]}
        seq = [n for n in nodes if n[0] == "Seq Scan"]
        check(f"R the lookup by request id reads idx_entity_memory_client_id, never the table in order "
              f"({'generic' if generic else 'custom'} plan)",
              used == {"idx_entity_memory_client_id"} and not seq, (sql is not None, used, seq))
    # The schema under test carries the index; the migration, run again at every boot, leaves it exactly as
    # declared.
    want = await pool.fetchval("SELECT pg_get_indexdef(to_regclass('idx_entity_memory_client_id'))")
    mig = ROOT / "migrations" / "v218_entity_memory_client_id.sql"
    got = None
    if mig.exists():
        await pool.execute(mig.read_text())
        await pool.execute(mig.read_text())          # and again: it is idempotent
        got = await pool.fetchval("SELECT pg_get_indexdef(to_regclass('idx_entity_memory_client_id'))")
    check("R the schema declares the index, and migration v218 run twice leaves it exactly as declared",
          bool(want) and got == want
          and "USING gin (COALESCE((metadata -> 'client_id'::text), (metadata -> 'client_id_digest'::text))"
              " jsonb_path_ops)" in (want or ""), (mig.exists(), want, got))
    # 6. PII mode keeps a digest of the id, never the id (the allowlist drops every key it does not name).
    scrubbed = PQ.scrub_metadata({"client_id": "rr-restart", "agent_title": "free text"})
    check("R PII mode keeps a digest of client_id on a row's tags, not the id, and drops what is not on its "
          "allowlist", list(scrubbed) == ["client_id_digest"]
          and re.fullmatch(r"[0-9a-f]{64}", str(scrubbed.get("client_id_digest") or "")) is not None, scrubbed)

    # 7. a late retry (no restart) of words that went on as the next turn, or that
    #    attached to the same words running; and PII mode's digest.
    await r_late(W, tp, pool, pg)
    await r_pii(W, tp, pool, pg)


# ── the run ─────────────────────────────────────────────────────────────────

async def h_post_routes(P, W, pool):
    """Section H: the Index's and the Start dialog's searches over HTTP."""
    import httpx
    from fastapi import FastAPI
    from fastapi.encoders import jsonable_encoder

    def mount(*, guarded: bool):
        app = FastAPI()
        app.include_router(P.router)
        if not guarded:
            # The scope checks stand aside here: they are read off the route
            # table in section W, and the middleware that mints the token they
            # need is not under test.
            for r in P.router.routes:
                for d in getattr(getattr(r, "dependant", None), "dependencies", []):
                    app.dependency_overrides[d.call] = lambda: None
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gate")

    embedded: list = []

    async def recording_embed(text):
        embedded.append(text)
        return [0.8, 0.2, 0.3, 0.2]

    async def recording_lane(_pool, _eid):
        return recording_embed, "fake-embed"

    async def no_lane(_pool, _eid):
        return None, ""
    real_lane = P._search_embedding_lane
    try:
        async with mount(guarded=False) as http:
            P._search_embedding_lane = no_lane
            for route, svc, kw, default_n in (
                    ("/api/projects/index",
                     lambda q, n: W.index_search_service(pool, E, q, limit=n, lane=(None, ""), refresh_in_background=False),
                     {}, P.INDEX_LIMIT_DEFAULT),
                    ("/api/projects/search",
                     lambda q, n: P.search_projects_service(pool, E, q, limit=n, lane=(None, ""), refresh_in_background=False),
                     {}, 8)):
                tag = route.rsplit("/", 1)[1]
                key = "pages" if tag == "index" else "projects"
                r = await http.post(route, json={"q": "quokka", "limit": 5})
                want = jsonable_encoder(await svc("quokka", 5))
                check(f"H {tag}: a POST with {{q, limit}} in the body answers what the service answers, field for field",
                      r.status_code == 200 and r.json() == want and bool(want[key]),
                      (r.status_code, str(r.json())[:200], str(want)[:200]))
                r = await http.post(route, json={})
                want = jsonable_encoder(await svc("", default_n))
                check(f"H {tag}: an empty body is the empty query at the default limit (the recent open pages)",
                      r.status_code == 200 and r.json() == want and 0 < len(want[key]) <= default_n,
                      (r.status_code, str(r.json())[:200]))
                big = 2000 if tag == "index" else 500
                limit_max = P.INDEX_LIMIT_MAX if tag == "index" else 50
                for label, body in ((f"a limit of 0", {"q": "x", "limit": 0}),
                                    (f"a limit over {limit_max}", {"q": "x", "limit": limit_max + 1}),
                                    (f"words over {big} characters", {"q": "x" * (big + 1)}),
                                    ("a limit that is not a number", {"q": "x", "limit": "many"})):
                    r = await http.post(route, json=body)
                    check(f"H {tag}: {label} is refused (422), as the GET refused it", r.status_code == 422, r.status_code)
                r = await http.post(route, json={"q": "x" * big, "limit": limit_max})
                check(f"H {tag}: the longest words and the largest limit are answered", r.status_code == 200, r.status_code)
                r = await http.post(route, content=b'{"q": "quokka \\ud800 tail", "limit": 5}',
                                    headers={"content-type": "application/json"})
                check(f"H {tag}: half of a surrogate pair in the words (JSON can carry one) is answered, not a 500 or a 422",
                      r.status_code == 200 and "\ufffd" in r.json().get("q", ""), (r.status_code, r.text[:160]))
                r = await http.get(f"{route}?q=quokka&limit=5")
                check(f"H {tag}: the GET is gone: GET {route}?q= answers no {key} (the path falls to /{{project_id}} and is refused)",
                      r.status_code != 200 and key not in r.text, (r.status_code, r.text[:120]))
            check("H neither path has a GET route any more",
                  not [r for r in P.router.routes if r.path in ("/api/projects/index", "/api/projects/search")
                       and "GET" in r.methods],
                  [(sorted(r.methods), r.path) for r in P.router.routes if r.path in ("/api/projects/index", "/api/projects/search")])

            # A pasted key is cut before the embedding call, as it was with the GET.
            P._search_embedding_lane = recording_lane
            for route in ("/api/projects/index", "/api/projects/search"):
                tag = route.rsplit("/", 1)[1]
                embedded.clear()
                r = await http.post(route, json={"q": f"quokka {SECRET}", "limit": 5})
                check(f"H {tag}: a key pasted into the words is cut before the embedding call (the call happened, and held no key)",
                      r.status_code == 200 and bool(embedded) and not any("sk-ant" in t or "Wv5Wv5" in t for t in embedded),
                      (r.status_code, embedded))
                check(f"H {tag}: ...and the answer echoes the cut words, never the key",
                      "sk-ant" not in r.text and "Wv5Wv5" not in r.text, r.text[:160])
            for _ in range(100):  # a background re-embed the recording lane started
                if E not in P._EMBED_REFRESH_INFLIGHT:
                    break
                await asyncio.sleep(0.05)
        # The guard on the route is the scope's, now on a POST.
        P._search_embedding_lane = no_lane
        async with mount(guarded=True) as http:
            for route in ("/api/projects/index", "/api/projects/search"):
                r = await http.post(route, json={"q": "quokka"})
                check(f"H {route.rsplit('/', 1)[1]}: without an app token the POST is refused (401), its scopes still guard it",
                      r.status_code == 401, (r.status_code, r.text[:120]))
    finally:
        P._search_embedding_lane = real_lane
        await pool.execute("UPDATE projects SET search_embedding = NULL, search_embedding_model = NULL")


async def run(dsn):
    from server import brain_service as bs
    import server.routes.projects as P
    W = load("server.routes.workbench_index")
    tp = load("server.connectors.thread_pick")
    have_w = not isinstance(W, Exception)
    check("the tree has the Index (server/routes/workbench_index.py)", have_w, W)
    have_tp = (not isinstance(tp, Exception) and hasattr(tp, "list_index_candidates")
               and hasattr(tp, "choose_page") and hasattr(tp, "land_index_pick"))
    check("thread_pick has the Index's entry (list_index_candidates, choose_page, land_index_pick)", have_tp)

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    saved = (bs._pool, bs._entity_id, bs._send_message_inner, P.ensure_project_dir, P.ensure_project_repo)
    made_dirs, made_repos = [], []

    async def no_repo(_pool, pid, name):
        made_repos.append((pid, name))
        return {"repo": None, "created": False, "reason": "no forge in this gate"}
    try:
        print("-- seeding (the Host of test_project_list_speed_pg) and the pages")
        t0 = time.perf_counter()
        await speed.seed(pool)
        pg = await pages(pool)
        print(f"   {await pool.fetchval('SELECT count(*) FROM entity_memory WHERE memory_type = $1', 'turn')} turn rows, "
              f"{await pool.fetchval('SELECT count(*) FROM projects WHERE entity_id = $1', E)} projects, "
              f"{round(time.perf_counter() - t0, 1)} s")
        bs._pool, bs._entity_id = pool, E
        REAL["inner"] = bs._send_message_inner   # its signature: real_signature
        bs._send_message_inner = scripted_inner
        P.ensure_project_dir = made_dirs.append
        P.ensure_project_repo = no_repo

        # ── S: the Start dialog's search, as main answers it ────────────────
        print("-- S: the Index orders as the Start dialog's search")
        await pool.execute("UPDATE projects SET search_embedding = ARRAY[0.9, 0.1, 0.3, 0.2]::real[],"
                           " search_embedding_model = 'fake-embed' WHERE id = ANY($1::int[])",
                           [pg["led_old"], pg["stripe_w2"], pg["qk_one"]])
        await pool.execute("UPDATE projects SET search_embedding = ARRAY[0.1, 0.9, 0.1, 0.1]::real[],"
                           " search_embedding_model = 'fake-embed' WHERE id = ANY($1::int[])",
                           [pg["nar_desc"], pg["led_new"]])

        async def fake_embed(text):
            return [0.8, 0.2, 0.3, 0.2]
        lanes = {"no embedding model": (None, ""), "an embedding model": (fake_embed, "fake-embed")}
        if have_w:
            # The Index scores the same vectors in the database (pgvector), not
            # in Python: the same order where no conversation matches.
            diffs = []
            for q in ("what builds", "project 4", "narwhal"):
                start = await P.search_projects_service(pool, E, q, limit=50, lane=lanes["an embedding model"],
                                                        refresh_in_background=False)
                out = await W.index_search_service(pool, E, q, limit=100, lane=lanes["an embedding model"],
                                                   refresh_in_background=False)
                theirs = [p["id"] for p in start["projects"]]
                if not out["semantic"] or ids(out)[:len(theirs)] != theirs:
                    diffs.append((q, ids(out)[:5], theirs[:5], out["semantic"]))
            check("S with an embedding model, the Index orders as the Start dialog where no conversation matches",
                  not diffs, diffs)
            await pool.execute("UPDATE projects SET search_embedding = ARRAY['NaN'::real, 1, 1, 1]"
                               " WHERE id = $1", pg["nar_desc"])
            out = await W.index_search_service(pool, E, "narwhal", lane=lanes["an embedding model"],
                                               refresh_in_background=False)
            check("S a stored vector pgvector refuses (NaN) drops the embedding arm, never the search",
                  out["semantic"] is False and ids(out)[:3] == [pg["nar_name"], pg["nar_desc"], pg["nar_phase"]],
                  (out["semantic"], ids(out)[:3]))
        await pool.execute("UPDATE projects SET search_embedding = NULL, search_embedding_model = NULL")

        if not have_w or not have_tp:
            print("-- the rest needs the Index; stopping here")
            check("R, P, T, C, M, N, A, F, W ran", False, "no Index in this tree")
            return

        # ── R: ranking ──────────────────────────────────────────────────────
        print("-- R: ranking")
        out = await index(W, pool, "quokka")
        got = ids(out)
        want = [pg["qk_name"], pg["qk_many"], pg["qk_arch"], pg["qk_one"]]
        check("R 'quokka': the page named for it first, then the pages whose turns hold it, more turns first",
              got == want, (got, want))
        check("R ...never another entity's rows, another entity's project, or an artifact",
              not {pg["qk_none"], pg["art"], pg["other"]} & set(got), got)
        arch = page(out, pg["qk_arch"]) or {}
        check("R an archived page is listed and marked archived",
              arch.get("archived") is True and arch.get("status") == "archived" and arch.get("group") == "archived",
              arch)
        live = page(out, pg["qk_many"]) or {}
        check("R an open page is marked open", live.get("archived") is False and live.get("status") == "active", live)
        check("R a page: id, name, status, group, archived, bucket, last activity, snippet; no score",
              set(live) == {"id", "name", "status", "group", "archived", "bucket_id", "last_activity_at", "snippet"},
              sorted(live))
        newest = await pool.fetchval("SELECT max(id) FROM entity_memory WHERE project_id = $1 AND content ILIKE '%quokka%'",
                                     pg["qk_many"])
        sn = live.get("snippet") or {}
        check("R its excerpt: the conversation's newest turn holding the word, the word marked",
              sn.get("from") == "conversation" and sn.get("turn_row_id") == newest
              and [m.lower() for m in marked(sn)] == ["quokka"] and "feeder" in sn.get("text", "")
              and sn.get("role") == "user" and sn.get("at") is not None, sn)
        sn = (page(out, pg["qk_name"]) or {}).get("snippet") or {}
        check("R a page found by its name: the name, the word marked", sn.get("from") == "name"
              and marked(sn) == ["Quokka"] and sn.get("text") == "Quokka integration", sn)
        check("R the conversation's terms are named", out.get("conversation", {}).get("used") == ["quokka*"],
              out.get("conversation"))

        start = await P.search_projects_service(pool, E, "billing dashboard", limit=50, lane=(None, ""),
                                                refresh_in_background=False)
        out = await index(W, pool, "billing dashboard")
        check("R two pages the text matches alike: the Start dialog puts the more recent first",
              [p["id"] for p in start["projects"]][:2] == [pg["led_new"], pg["led_old"]],
              [p["id"] for p in start["projects"]][:2])
        check("R ...and the Index lifts the one whose turns hold the words",
              ids(out)[:2] == [pg["led_old"], pg["led_new"]], ids(out)[:2])
        check("R ...its excerpt is that turn, 'dashboard' marked",
              [m.lower() for m in marked((page(out, pg["led_old"]) or {}).get("snippet"))] == ["dashboard"],
              (page(out, pg["led_old"]) or {}).get("snippet"))

        out = await index(W, pool, "stripe")
        got = ids(out)
        check("R 'stripe': the page named Stripe first, over two pages matching weakly in both arms",
              got[:3] == [pg["stripe_n"], pg["stripe_w2"], pg["stripe_w1"]], got[:3])
        # The same two arms fused by rank (memory_retrieval._rrf_fuse, k=60): the
        # case the bounded share is there for.
        rows = await pool.fetch(P._SEARCH_ROWS_SQL, E, P._search_tsquery("stripe"), "%stripe%",
                                P._SEARCH_CANDIDATE_CAP)
        scored, _stale = P.search_text_scores(rows, model="", qvec=None, semantic=False)
        text_order = [int(r["id"]) for _s, _rec, r in sorted(scored, key=lambda t: (t[0], t[1]), reverse=True)]
        conv = await W._conversation_arm(pool, E, "stripe")
        conv_order = sorted(conv["scores"], key=lambda k: (-conv["scores"][k], k))
        from mastercode.memory_retrieval import RRF_K
        rrf = {}
        for order in (text_order, conv_order):
            for rank, pid in enumerate(order):
                rrf[pid] = rrf.get(pid, 0.0) + 1.0 / (RRF_K + rank + 1)
        rrf_order = sorted(rrf, key=lambda k: -rrf[k])
        check("R ...where rank fusion of the same two arms puts both weak pages above it",
              pg["stripe_n"] in rrf_order and rrf_order.index(pg["stripe_n"]) > rrf_order.index(pg["stripe_w1"])
              and rrf_order.index(pg["stripe_n"]) > rrf_order.index(pg["stripe_w2"]), rrf_order[:4])
        check("R the conversation's share stays under the name boost", W.CONV_SHARE < P._SEARCH_NAME_BOOST,
              (W.CONV_SHARE, P._SEARCH_NAME_BOOST))
        out = await index(W, pool, "bot")
        check("R 'bot': a name holding it inside a word (the boost alone) stays over a page whose every turn "
              "is about it", ids(out)[:2] == [pg["bot_name"], pg["bot_conv"]], ids(out)[:2])
        sn = (page(out, pg["bot_name"]) or {}).get("snippet") or {}
        check("R ...its excerpt is the name with the typed text marked inside the word",
              sn.get("from") == "name" and marked(sn) == ["bot"] and sn.get("text") == "Robotics lab", sn)

        out = await index(W, pool, "narwhal")
        by = {p["id"]: p for p in out["pages"]}
        trio = (pg["nar_name"], pg["nar_desc"], pg["nar_phase"])
        check("R 'narwhal': the archived page named for it first, then its description, then its phase title",
              ids(out)[:3] == list(trio) and by[pg["nar_name"]]["archived"] is True, ids(out)[:3])
        check("R ...each excerpt from where it matched, the word marked",
              all(k in by for k in trio)
              and [(by[k]["snippet"] or {}).get("from") for k in trio] == ["name", "description", "phases"]
              and all([m.lower() for m in marked(by[k]["snippet"])] == ["narwhal"] for k in trio),
              [by.get(k, {}).get("snippet") for k in trio])

        diffs = []
        for q in ("project", "project 4", "what builds"):
            start = await P.search_projects_service(pool, E, q, limit=50, lane=(None, ""), refresh_in_background=False)
            out = await index(W, pool, q)
            theirs = [p["id"] for p in start["projects"]]
            if not theirs or ids(out)[:len(theirs)] != theirs or out["conversation"]["used"]:
                diffs.append((q, ids(out)[:5], theirs[:5], out["conversation"]))
        check("R where no conversation matches, the order is the Start dialog's", not diffs, diffs)
        out = await index(W, pool, "iki epm enih")
        check("R words too common to tell pages apart are named and left out (the seed's commonest words)",
              out["conversation"]["used"] == [] and set(out["conversation"]["too_common"]) == {"iki", "epm", "enih*"},
              out["conversation"])
        recent = await pool.fetch(f"SELECT p.id, {P._LAST_ACTIVITY_SQL} AS at FROM projects p WHERE p.entity_id = $1"
                                  " AND p.kind = 'project' AND p.status <> 'archived' ORDER BY at DESC, p.id DESC"
                                  " LIMIT 20", E)
        out = await index(W, pool, "", limit=20)
        check("R nothing typed: the open pages, most recent first", ids(out) == [r["id"] for r in recent],
              (ids(out)[:5], [r["id"] for r in recent][:5]))

        # ── P: the plans ────────────────────────────────────────────────────
        print("-- P: the conversation arm's plans")
        for label, text in (("a rare word", "quokka"), ("eight common words", WORST),
                            ("one common prefix", "iki"), ("a long message", LONG)):
            a = (E, W.conversation_terms(text), W.CONV_ROWS, W.CONV_TERMS_MAX)
            for how, generic in (("", False), (", generic plan", True)):
                nodes = await plan_nodes(pool, W._CONV_PROBE_SQL, a, generic=generic)
                check(f"P {label}{how}: entity_memory read only through the GIN index, under a Limit",
                      through_gin(nodes), [n for n in nodes if n[1] == "entity_memory" or n[2]])
            nodes = await plan_nodes(pool, W._CONV_PROBE_SQL, a, analyze=True)
            read = sum((n[4] or 0) * (n[5] or 1) for n in nodes if n[1] == "entity_memory")
            check(f"P {label}: reads at most {W.CONV_ROWS} rows of entity_memory (read {read})", read <= W.CONV_ROWS, read)
        await pool.execute("ANALYZE entity_memory")
        nodes = await plan_nodes(pool, W._CONV_PROBE_SQL, (E, W.conversation_terms(WORST), W.CONV_ROWS, W.CONV_TERMS_MAX))
        check("P after ANALYZE: still only through the GIN index, under a Limit", through_gin(nodes), nodes)
        nodes = await plan_nodes(pool, W._TURN_SNIPPETS_SQL,
                                 (E, "'quokka'", W._HEADLINE_OPTS, W._MARK_ON + W._MARK_OFF, [newest, newest - 1]),
                                 generic=True)
        check("P the excerpt read goes by primary key, never a scan of entity_memory",
              not [n for n in nodes if n[0] == "Seq Scan" and n[1] == "entity_memory"]
              and "entity_memory_pkey" in {n[2] for n in nodes}, nodes)

        # ── T: time ─────────────────────────────────────────────────────────
        print("-- T: time (wall clock, median of 5; the ceiling is a backstop)")
        raw = await pool.fetchval(
            "EXPLAIN (ANALYZE, FORMAT JSON) SELECT project_id, count(*) FROM entity_memory"
            " WHERE entity_id = $1 AND memory_type = 'turn' AND project_id IS NOT NULL"
            " AND search_tsv @@ to_tsquery('english', 'ab:*') GROUP BY 1", E)
        naive = (json.loads(raw) if isinstance(raw, str) else raw)[0]["Execution Time"]
        print(f"   for the record, the conversation read asked naively (one prefix as a constant): {naive:.0f} ms")
        for label, text in (("a rare word", "quokka"), ("two words, one typed", "quokka fee"),
                            ("a page's name", "Stripe integration"), ("the seed's names", "project 4"),
                            ("eight common words", WORST), ("one common prefix", "iki"),
                            ("a long message", LONG)):
            before = await median_ms(lambda: P.search_projects_service(
                pool, E, text, limit=50, lane=(None, ""), refresh_in_background=False))
            after = await median_ms(lambda: index(W, pool, text, limit=P.INDEX_LIMIT_DEFAULT))
            after_max = await median_ms(lambda: index(W, pool, text, limit=P.INDEX_LIMIT_MAX))
            print(f"   {label:<22} Start dialog {before:5.1f} ms   Index {after:5.1f} ms "
                  f"(limit {P.INDEX_LIMIT_DEFAULT}), {after_max:5.1f} ms (limit {P.INDEX_LIMIT_MAX})")
            check(f"T {label}: the Index answers under {CEILING_MS} ms", max(after, after_max) < CEILING_MS,
                  round(max(after, after_max)))
        # The embedding arm, with a model whose vector costs nothing to make:
        # what the search itself adds when every page carries a vector (the
        # model's own time is the Host's endpoint's, not measured here).
        for dim in (1024, 4096):
            model_name = f"fake-embed-{dim}"
            # One vector a page (the subquery names the row, so it runs per row).
            await pool.execute(
                "UPDATE projects p SET search_embedding = (SELECT array_agg((random() + 0 * g)::real)"
                "  FROM generate_series(1, $1) g WHERE p.id > 0), search_embedding_model = $2"
                " WHERE p.entity_id = $3", dim, model_name, E)
            qv = [0.5] * dim

            async def embed_now(_text, qv=qv):
                return qv
            lane = (embed_now, model_name)
            before = await median_ms(lambda: P.search_projects_service(pool, E, "quokka", limit=50, lane=lane,
                                                                       refresh_in_background=False))
            after = await median_ms(lambda: W.index_search_service(pool, E, "quokka", lane=lane,
                                                                   refresh_in_background=False))
            out = await W.index_search_service(pool, E, "quokka", lane=lane, refresh_in_background=False)
            print(f"   embedding arm, {dim} dims on every page: Start dialog {before:6.1f} ms   Index {after:6.1f} ms")
            check(f"T the embedding arm ({dim} dims) runs, and the Index answers under {CEILING_MS} ms",
                  out["semantic"] is True and after < CEILING_MS, (out["semantic"], round(after)))
        await pool.execute("UPDATE projects SET search_embedding = NULL, search_embedding_model = NULL")

        # ── C: the pick's list ──────────────────────────────────────────────
        print("-- C: the pick's list and call")
        check("C the Host is shown at most INDEX_PICK_K of the search's pages",
              getattr(tp, "INDEX_PICK_K", None) == K, getattr(tp, "INDEX_PICK_K", None))
        # A message the search answers with an archived page, a page with no
        # turns yet and a ticket's page; the open page is one it does not rank.
        cmsg = "Any news on the quokka ticket?"
        top = ids(await index(W, pool, cmsg))[:K]
        cur = ("project", str(pg["led_new"]))
        check("C (the search ranks an archived page, a page with no turns, a ticket's page, not the open one)",
              {pg["qk_arch"], pg["qk_name"], pg["ticket"]} <= set(top) and pg["led_new"] not in top, top)
        cands, err = await attempt(lambda: tp.list_index_candidates(pool, E, top, budget_tokens=10 ** 9, current=cur))
        keys = [c.key for c in cands or []]
        check("C the list: the search's pages in its order, then the open page",
              keys == [f"project:{i}" for i in top] + [f"project:{pg['led_new']}"], (keys, top, err))
        by = {c.key: c for c in cands or []}
        qa, qn = by.get(f"project:{pg['qk_arch']}"), by.get(f"project:{pg['qk_name']}")
        check("C ...archived ones marked, a page with no turns yet, a ticket's project",
              qa is not None and qa.archived and qn is not None and qn.last_activity is None
              and not qn.archived and f"project:{pg['ticket']}" in by, (qa, qn, err))
        qm = by.get(f"project:{pg['qk_many']}")
        check("C ...each with its last exchange and when it was last active",
              qm is not None and qm.last_user == "Saw the quokka near the feeder again, visit 4."
              and qm.last_host == "Here are the notes from the last visit." and qm.last_activity is not None, qm)
        got, err = await attempt(lambda: tp.list_index_candidates(
            pool, E, [pg["art"], pg["other"], 999999, pg["qk_one"], pg["qk_one"]], budget_tokens=10 ** 9))
        check("C ...never an artifact, another entity's project or an id that is no page; each once",
              [c.key for c in got or []] == [f"project:{pg['qk_one']}"], ([c.key for c in got or []], err))
        many = [r["id"] for r in await pool.fetch(
            "SELECT id FROM projects WHERE entity_id = $1 AND kind = 'project' ORDER BY id LIMIT 40", E)]
        got, err = await attempt(lambda: tp.list_index_candidates(pool, E, many, budget_tokens=10 ** 9, current=cur))
        check(f"C ...at most {K} of the pages it is given, then the open page",
              [c.key for c in got or []] == [f"project:{i}" for i in many[:K]] + [f"project:{pg['led_new']}"],
              ([c.key for c in got or []][:3], err))
        tiny, err = await attempt(lambda: tp.list_index_candidates(pool, E, top, budget_tokens=1, current=cur))
        check("C a budget that fits nothing still keeps the open page",
              [c.key for c in tiny or []] == [f"project:{pg['led_new']}"], ([c.key for c in tiny or []], err))
        sql = getattr(tp, "_index_pages_sql", None)
        v216 = {"idx_entity_memory_project_turns", "idx_entity_memory_project_replies"}
        for how, generic in (("", False), (", generic plan", True)):
            nodes = await plan_nodes(pool, sql(), (E, top), generic=generic) if sql else []
            on_mem = [n for n in nodes if n[1] == "entity_memory"]
            # Under a Limit, or an Incremental Sort (which reads only the
            # first rows of the index's order): never a Sort, which would read
            # every row of the page.
            check(f"C each listed page's newest rows: a few entries of v216's indexes, never every row{how}",
                  bool(on_mem) and all(n[0] in ("Index Scan", "Index Only Scan") and n[2] in v216
                                       and n[3] in ("Limit", "Incremental Sort") for n in on_mem),
                  [n for n in nodes if n[1] == "entity_memory" or n[2]])
        # Measured: what the read touches for a page of 250 turns.
        busiest = await pool.fetchval(
            "SELECT project_id FROM entity_memory WHERE entity_id = $1 AND memory_type = 'turn'"
            " AND project_id IS NOT NULL GROUP BY 1 ORDER BY count(*) DESC LIMIT 1", E)
        nodes = await plan_nodes(pool, sql(), (E, [busiest]), analyze=True) if sql else []
        read = sum(((n[4] or 0) + (n[6] or 0)) * (n[5] or 1) for n in nodes if n[1] == "entity_memory")
        n_turns = await pool.fetchval("SELECT count(*) FROM entity_memory WHERE project_id = $1", busiest)
        check(f"C ...the busiest page ({n_turns} turn rows): {read} entity_memory rows read, at most 25",
              bool(nodes) and read <= 25, read)
        model = Model(pick_call(page=f"project:{pg['qk_many']}", reason="It is about the quokka notes."))
        res, err = await attempt(lambda: tp.choose_page(pool, E, message=cmsg, pages=top, current=cur,
                                                call_model=model, route=ROUTE))
        pick, shown = res if res is not None else (tp.Pick(ok=False, failure=repr(err)), [])
        msgs, kw = model.calls[0] if model.calls else ([{}, {}], {})
        sysmsg = msgs[0].get("content", "") if msgs else ""
        check("C one call, its only tool pick_page, on the Host's chat route",
              len(model.calls) == 1 and kw.get("tools") == [tp.INDEX_PICK_TOOL]
              and (kw.get("provider"), kw.get("provider_model")) == ("anthropic", "claude-opus-5-5"),
              (kw.get("tools"), err))
        check("C the message is the user turn, verbatim",
              msgs[1:] == [{"role": "user", "content": cmsg}], msgs[1:])
        check("C the Index's words: no messaging app, pages not threads, the Index's own matches",
              "typed this message into the Workbench's Index" in sysmsg and "pick_page" in sysmsg
              and "The pages the Index found for this message, best match first" in sysmsg
              and "pick_thread" not in sysmsg and "Telegram" not in sysmsg and "arrived from" not in sysmsg,
              sysmsg[:300])
        check("C the Host's list is that list", listed(model) == top + [pg["led_new"]], (listed(model), top))
        check("C the open page is marked, once", sysmsg.count("The person has this page open") == 1
              and f"[project:{pg['led_new']}] Ledger two, " in sysmsg.split("The person has this page open")[0][-400:],
              sysmsg[-300:])
        check("C an archived page and a page with no turns are in the list, marked so",
              f"[project:{pg['qk_arch']}] Old zoo (archived), last active" in sysmsg
              and f"[project:{pg['qk_name']}] Quokka integration, no messages yet" in sysmsg)
        check("C the pick lands where it named, with the Host's reason",
              pick.ok and (pick.kind, pick.ref, pick.new, pick.reason)
              == ("project", str(pg["qk_many"]), False, "It is about the quokka notes.")
              and len(shown) == len(top) + 1, pick)
        p2 = tp.read_pick(pick_call(new=True, reason="nothing fits"), shown, message="Plan the kite day\nmore",
                          shape=tp._PAGE)
        check("C a new page with no title takes the message's first line, and is a project",
              p2.ok and p2.new and p2.kind == "project" and p2.title == "Plan the kite day", p2)
        p3 = tp.read_pick({"tool_calls": [{"function": {"name": "pick_thread", "arguments": json.dumps(
            {"thread": f"project:{pg['qk_many']}"})}}]}, shown, shape=tp._PAGE)
        check("C the connector's tool is not a pick on the Index", not p3.ok, p3)

        # ── W, M: a send that lands on a page ───────────────────────────────
        print("-- W, M: a send from the Index")
        before = await counts(pool)
        text = "  Is the quokka feeder fixed?\nCheck the camera too.  "
        status, out, rec, _m = await send(W, pool, text, pick_call(page=f"project:{pg['qk_many']}",
                                                                   reason="It is about the quokka notes."),
                                          current=pg["qk_one"])
        check("W 200, started, on the page the Host picked, not restored, not made",
              status == 200 and out.get("state") == "started" and out.get("project_id") == pg["qk_many"]
              and out.get("restored") is False and out.get("created") is False, (status, out))
        inner = INNER_CALLS[-1] if INNER_CALLS else {}
        check("W the turn runs through send_message as POST /api/brain/messages runs one: the words verbatim, "
              "the app, its permissions, the project",
              (inner.get("entity_id"), inner.get("message"), inner.get("app_id"), inner.get("project_id"),
               inner.get("app_permissions")) == (E, text, APP, pg["qk_many"], PERMS), inner)
        check("W ...in the project's conversation (its thread key), and the answer names that turn",
              rec is not None and rec.thread_key == f"project|{E}|{pg['qk_many']}"
              and out.get("turn", {}).get("turn_id") == rec.turn_id and out["turn"].get("mode") == "started"
              and out.get("stream") == f"/api/brain/turns/{rec.turn_id}/stream",
              (out.get("turn"), rec and rec.thread_key))
        marker = "Index message, picked this page: It is about the quokka notes."
        row = await pool.fetchrow("SELECT content, metadata, project_id FROM entity_memory"
                                  " WHERE metadata->>'source_kind' = 'index_pick' ORDER BY id DESC LIMIT 1")
        md = (json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]) if row else {}
        check("M the marker's words, in the answer and on the page", out.get("marker") == marker
              and row is not None and row["content"] == marker and row["project_id"] == pg["qk_many"],
              (out.get("marker"), row))
        check("M a turn row of the page: role assistant, index_pick, the project's descriptor",
              md.get("role") == "assistant" and md.get("source_kind") == "index_pick"
              and md.get("source_context") == {"kind": "project", "ref": str(pg["qk_many"])}, md)
        check("M exactly one marker written, nothing else made", await counts(pool) == (before[0], before[1] + 1),
              (before, await counts(pool)))
        cands, err = await attempt(lambda: tp.list_index_candidates(pool, E, [pg["qk_many"]], budget_tokens=10 ** 9))
        mine = next((c for c in cands or [] if c.key == f"project:{pg['qk_many']}"), None)
        conn_cands = await tp.list_candidates(pool, E, budget_tokens=10 ** 9)
        theirs = next((c for c in conn_cands if c.key == f"project:{pg['qk_many']}"), None)
        check("M the marker is never a page's last exchange, to the Index or to a connector",
              mine is not None and mine.last_host == "Here are the notes from the last visit."
              and theirs is not None and theirs.last_host == "Here are the notes from the last visit.",
              (mine and mine.last_host, theirs and theirs.last_host))
        check("M the connector's list is as before: no archived page, no page without turns, no ticket's project",
              not {f"project:{pg[k]}" for k in ("qk_arch", "qk_name", "ticket")} & {c.key for c in conn_cands})
        check("M the words: picked, started, and with no reason",
              tp.index_marker_text(tp.Pick(ok=True, kind="project", ref="1", reason="r"))
              == "Index message, picked this page: r"
              and tp.index_marker_text(tp.Pick(ok=True, kind="project", new=True, reason="r"))
              == "Index message, started this page: r"
              and tp.index_marker_text(tp.Pick(ok=True, kind="project", ref="1")) == "Index message, picked this page.")

        # ── N: a new page ───────────────────────────────────────────────────
        print("-- N: a new page")
        before = await counts(pool)
        status, out, rec, _m = await send(W, pool, "Plan the kite festival for May",
                                          pick_call(new=True, title="Kite festival",
                                                    reason="Nothing open is about kites."))
        new = await pool.fetchrow("SELECT id, name, kind, status, entity_id FROM projects WHERE name = 'Kite festival'")
        check("N a new Workbench project with the Host's title",
              new is not None and (new["kind"], new["status"], new["entity_id"]) == ("project", "planning", E), new)
        check("N the answer: created, started, on it",
              status == 200 and out.get("state") == "started" and new is not None
              and out.get("project_id") == new["id"] and out.get("created") is True and out.get("restored") is False,
              (status, out))
        check("N 'started this page' is written on it",
              new is not None and await pool.fetchval(
                  "SELECT content FROM entity_memory WHERE project_id = $1 AND metadata->>'source_kind' = 'index_pick'",
                  new["id"]) == "Index message, started this page: Nothing open is about kites.")
        check("N the turn starts there",
              new is not None and rec is not None and rec.thread_key == f"project|{E}|{new['id']}"
              and INNER_CALLS[-1].get("project_id") == new["id"], INNER_CALLS[-1:])
        check("N its directory and Code Repository are asked for, as POST /api/projects asks",
              new is not None and made_dirs[-1:] == [new["id"]] and made_repos[-1:] == [(new["id"], "Kite festival")],
              (made_dirs[-1:], made_repos[-1:]))
        check("N one project and one marker more", await counts(pool) == (before[0] + 1, before[1] + 1),
              (before, await counts(pool)))
        real_write = tp.write_index_marker

        async def broken(*a, **k):
            raise RuntimeError("the marker write failed")
        tp.write_index_marker = broken
        n_inner, raised = len(INNER_CALLS), False
        try:
            await send(W, pool, "x", pick_call(new=True, title="Kite orphan", reason="r"))
        except RuntimeError:
            raised = True
        finally:
            tp.write_index_marker = real_write
        check("N a marker that cannot be written leaves no new project, and no turn",
              raised and not await pool.fetchval("SELECT count(*) FROM projects WHERE name = 'Kite orphan'")
              and len(INNER_CALLS) == n_inner, (raised, len(INNER_CALLS) - n_inner))

        # ── A: archived picks ───────────────────────────────────────────────
        print("-- A: an archived pick")
        status, out, rec, _m = await send(W, pool, "What did the quokka gate cost?",
                                          pick_call(page=f"project:{pg['qk_arch']}",
                                                    reason="The quokka gate was priced there."))
        check("A an archived pick is restored first and the turn starts there",
              status == 200 and out.get("state") == "started" and out.get("restored") is True
              and await pool.fetchval("SELECT status FROM projects WHERE id = $1", pg["qk_arch"]) == "active"
              and rec is not None and rec.thread_key == f"project|{E}|{pg['qk_arch']}", (status, out))
        check("A ...and the pick is written there",
              await pool.fetchval("SELECT content FROM entity_memory WHERE project_id = $1 AND"
                                  " metadata->>'source_kind' = 'index_pick'", pg["qk_arch"])
              == "Index message, picked this page: The quokka gate was priced there.")
        before, n_inner = await counts(pool), len(INNER_CALLS)
        status, out, rec, _m = await send(W, pool, "Where is the shared repo?",
                                          pick_call(page=f"project:{pg['repo_arch']}", reason="It holds the repo."))
        check("A a refused restore is a 409 that says why",
              status == 409 and out.get("state") == "restore_refused" and out.get("project_id") == pg["repo_arch"]
              and "Code Repository" in (out.get("reason") or ""), (status, out))
        check("A ...the page stays archived, nothing is written, no turn starts",
              await pool.fetchval("SELECT status FROM projects WHERE id = $1", pg["repo_arch"]) == "archived"
              and await counts(pool) == before and len(INNER_CALLS) == n_inner and rec is None,
              (await counts(pool), before, len(INNER_CALLS) - n_inner))
        # The restore and the marker land together: a marker that cannot be
        # written leaves the page archived, never restored with nothing on it.
        tank = await project(pool, "Old aquarium", status="archived", days_ago=45)
        await turn(pool, tank, "The aquarium pump hums again.", mins_ago=6000)
        tp.write_index_marker = broken
        n_inner, raised = len(INNER_CALLS), False
        try:
            await send(W, pool, "Is the aquarium pump fixed?", pick_call(page=f"project:{tank}", reason="r"))
        except RuntimeError:
            raised = True
        finally:
            tp.write_index_marker = real_write
        check("A an archived pick whose marker cannot be written stays archived, and no turn starts",
              raised and await pool.fetchval("SELECT status FROM projects WHERE id = $1", tank) == "archived"
              and len(INNER_CALLS) == n_inner,
              (raised, await pool.fetchval("SELECT status FROM projects WHERE id = $1", tank),
               len(INNER_CALLS) - n_inner))

        # ── F: failed picks ─────────────────────────────────────────────────
        print("-- F: failed picks")
        # What the Host is shown for this message (or would have been): the
        # search's best for it, an archived page among them, and the open page.
        fmsg = "Where does the narwhal survey go?"
        fexpect = set(await want_pick_pages(W, pool, fmsg)) | {pg["qk_name"]}
        for label, answer, route in (
                ("no call", {"content": "Sure.", "tool_calls": []}, ROUTE),
                ("a page not in the list", pick_call(page="project:999999", reason="r"), ROUTE),
                ("a model error", {"error": "429 rate limited"}, ROUTE),
                ("a call that raises", RuntimeError("provider down"), ROUTE),
                ("no chat model set up", None, {"provider": "", "model": "", "unconfigured": "set up a chat model"})):
            before, n_inner = await counts(pool), len(INNER_CALLS)
            status, out, rec, _m = await send(W, pool, fmsg, answer, route=route, current=pg["qk_name"])
            cand = {c["project_id"]: c for c in out.get("candidates") or []}
            check(f"F {label}: 200 ask, the pages the Host was shown (or would have been) to choose from, "
                  "archived and turnless ones marked",
                  status == 200 and out.get("state") == "ask" and bool(out.get("failure"))
                  and pg["nar_name"] in cand and cand[pg["nar_name"]]["archived"] is True
                  and pg["qk_name"] in cand and set(cand) == fexpect,
                  (status, out.get("state"), out.get("failure"), sorted(cand), sorted(fexpect)))
            check(f"F {label}: nothing written, nothing made, no turn",
                  await counts(pool) == before and len(INNER_CALLS) == n_inner and rec is None,
                  (before, await counts(pool)))
        status, out, rec, model = await send(W, pool, "   ", pick_call(page=f"project:{pg['qk_one']}", reason="r"))
        check("F an empty message is a 400 and asks the Host nothing", status == 400 and not model.calls, (status, out))

        # ── D: a pasted key ─────────────────────────────────────────────────
        print("-- D: a pasted key is cut before the search, the pick and the turn")
        embedded: list = []

        async def recording_embed(text):
            embedded.append(text)
            return [0.8, 0.2, 0.3, 0.2]

        async def recording_lane(_pool, _eid):
            return recording_embed, "fake-embed"
        real_lane = P._search_embedding_lane
        P._search_embedding_lane = recording_lane
        try:
            await W.index_search_service(pool, E, f"quokka {SECRET}", refresh_in_background=False)
            check("D the search, as the person types: the key never reaches the embedding endpoint",
                  bool(embedded) and not any("sk-ant" in t or "Wv5Wv5" in t for t in embedded), embedded)
            embedded.clear()
            before, n_inner = await counts(pool), len(INNER_CALLS)
            dtext = f"{SECRET} is the key, keep it safe\nand plan the quokka feeder"
            status, out, rec, model = await send(W, pool, dtext, pick_call(new=True, reason="Nothing open fits."))
            msgs = model.calls[0][0] if model.calls else []
            check("D the send runs the Index's search, which embeds the words with the key cut",
                  bool(embedded) and not any("sk-ant" in t or "Wv5Wv5" in t for t in embedded), embedded)
            check("D the pick's call never carries the key: the message reaches it cut, as the turn reads it",
                  bool(msgs) and "Wv5Wv5" not in json.dumps(msgs)
                  and msgs[-1].get("content", "").startswith("[REDACTED:anthropic-key] is the key"), msgs[-1:])
            made = await pool.fetchrow("SELECT id, name FROM projects WHERE id = $1", out.get("project_id") or -1)
            check("D a new page titled from the message's first line takes it without the key",
                  made is not None and "Wv5Wv5" not in made["name"] and "sk-ant" not in made["name"]
                  and made["name"].startswith("[REDACTED:anthropic-key] is the key"), made)
            check("D the turn starts on the words with the key cut",
                  len(INNER_CALLS) == n_inner + 1 and "Wv5Wv5" not in INNER_CALLS[-1].get("message", "")
                  and INNER_CALLS[-1].get("message", "").startswith("[REDACTED:anthropic-key]"),
                  INNER_CALLS[-1:] if len(INNER_CALLS) > n_inner else "no turn")
            check("D nothing written holds the key", not await pool.fetchval(
                "SELECT count(*) FROM entity_memory WHERE strpos(content, $1) > 0", SECRET)
                and not await pool.fetchval("SELECT count(*) FROM projects WHERE strpos(name, $1) > 0", "Wv5Wv5"),
                await counts(pool))
        finally:
            P._search_embedding_lane = real_lane
            # The send's search may have started a background re-embed with
            # the recording lane; let it end, then drop what it wrote.
            for _ in range(100):
                if E not in P._EMBED_REFRESH_INFLIGHT:
                    break
                await asyncio.sleep(0.05)
            await pool.execute("UPDATE projects SET search_embedding = NULL, search_embedding_model = NULL")

        # ── X: a page outside the Host's list ───────────────────────────────
        print("-- X: a page the search ranks below the first K")
        order = ids(await index(W, pool, KMSG))
        outside = order[30] if len(order) > 30 else None
        before, n_inner = await counts(pool), len(INNER_CALLS)
        status, out, rec, model = await send(W, pool, KMSG, pick_call(page=f"project:{outside}", reason="r"),
                                             current=pg["qk_one"])
        shown_ids = listed(model)
        check(f"X the Host is shown the search's first {K} for the message, then the open page",
              shown_ids == order[:K] + [pg["qk_one"]], (shown_ids[:14], order[:K]))
        check("X a page the search ranks 31st is not in the Host's list",
              outside is not None and outside not in shown_ids, (outside, len(shown_ids)))
        check("X ...named anyway, it lands nowhere: the person is asked, nothing written, no turn",
              status == 200 and out.get("state") == "ask" and "not in the list" in (out.get("failure") or "")
              and await counts(pool) == before and len(INNER_CALLS) == n_inner and rec is None,
              (status, out.get("state"), out.get("failure"), len(INNER_CALLS) - n_inner))

        # ── K: the pick's cost, at 50 pages and at 300 ──────────────────────
        print("-- K: the pick's cost does not grow with the pages")
        e50 = 3
        await pool.execute("INSERT INTO entity_registry (id, display_name) VALUES ($1, 'fifty pages')"
                           " ON CONFLICT (id) DO NOTHING", e50)
        p50 = [await pool.fetchval(
            "INSERT INTO projects (entity_id, name, description, status, kind, config, created_at, updated_at)"
            " VALUES ($1, $2, $3, 'active', 'project', '{}'::jsonb, now() - make_interval(days => $4),"
            "         now() - make_interval(days => $4)) RETURNING id",
            e50, f"Project {i}", f"what project {i} builds", 50 - i) for i in range(50)]
        # The seed's own turns, as dense: 250 a page.
        await pool.execute(speed._PROJECT_TURNS_SQL, e50, p50, len(p50), 250 * len(p50))
        for table in ("projects", "entity_memory"):
            await pool.execute(f"ANALYZE {table}")
        sizes = {}
        for eid in (e50, E):
            n = await pool.fetchval("SELECT count(*) FROM projects WHERE entity_id = $1 AND kind = 'project'", eid)
            label = f"{n} pages"
            order = ids(await W.index_search_service(pool, eid, KMSG, limit=100, lane=(None, ""),
                                                     refresh_in_background=False))
            # The open page one the search ranks low: the list's longest, K + 1.
            cur_id = order[-1]
            status, out, rec, model = await send(W, pool, KMSG, {"content": "", "tool_calls": []},
                                                 current=cur_id, entity=eid)
            size, shown_n = (pick_input_tokens(model), len(listed(model))) if model.calls else (None, None)
            sizes[n] = size
            check(f"K {label}: the pick's input is within {PICK_BUDGET_TOKENS} tokens (chars/4): {size}",
                  size is not None and size <= PICK_BUDGET_TOKENS, size)
            check(f"K {label}: it lists at most {K} + 1 pages: {shown_n}",
                  shown_n is not None and shown_n <= K + 1, shown_n)

            async def one_send(eid=eid, cur_id=cur_id, first=order[0]):
                model = Model(pick_call(page=f"project:{first}", reason="r"))
                t0 = time.perf_counter()
                _status, out = await W.index_send_service(pool, eid, KMSG, current_project_id=cur_id, app_id=APP,
                                                          app_permissions=PERMS, call_model=model, route=ROUTE)
                ms = (time.perf_counter() - t0) * 1000
                await settle((out.get("turn") or {}).get("turn_id"))
                return ms, out
            runs = [await one_send() for _ in range(5)]
            ms = statistics.median(r[0] for r in runs)
            print(f"   {label}: the pick's input ~{size} tokens, {shown_n} pages listed; a send, the model's own "
                  f"time left out: {ms:.1f} ms (median of 5)")
            check(f"K {label}: a send lands and starts its turn, under {CEILING_MS} ms without the model's time",
                  all(r[1].get("state") == "started" for r in runs) and ms < CEILING_MS,
                  (round(ms), [r[1].get("state") for r in runs]))
        print(f"   the pick's input by Host size: {sizes}")

        # ── Q: fill, clip and one send per request id ───────────────────────
        await q_slice5(W, tp, pool, pg)

        # ── R: the send, across a restart ───────────────────────────────────
        await r_durable(W, tp, pool, pg)

        # ── W: the routes ───────────────────────────────────────────────────
        def scopes_of(router, path):
            return {(sorted(r.methods)[0], r.path): sorted(getattr(d.call, "_app_scope", "")
                                                           for d in r.dependant.dependencies)
                    for r in router.routes if r.path == path}
        scopes = {**scopes_of(P.router, "/api/projects/index"), **scopes_of(P.router, "/api/projects/search"),
                  **scopes_of(W.router, "/api/workbench/index/send")}
        check("W the routes and their scopes (the searches are POSTs: their words are in the body)",
              scopes == {("POST", "/api/projects/index"): ["read:brain_memory", "read:projects"],
                         ("POST", "/api/projects/search"): ["read:projects"],
                         ("POST", "/api/workbench/index/send"): ["read:brain_memory", "write:projects",
                                                                 "write:session"]}, scopes)
        for lit in ("/api/projects/index", "/api/projects/search"):
            order = [r.path for r in P.router.routes if r.path in (lit, "/api/projects/{project_id}")]
            check(f"W {lit} is registered before /api/projects/{{project_id}}, which would take '{lit.rsplit('/', 1)[1]}'",
                  order[:1] == [lit], order)
        app = load_app()
        mounted = set() if isinstance(app, Exception) else {
            (sorted(m for m in r.methods if m not in ("HEAD", "OPTIONS"))[0], r.path)
            for r in served_routes(app) if (getattr(r, "path", "") or "") in ("/api/projects/index",
                                                                              "/api/projects/search",
                                                                              "/api/workbench/index/send")}
        check("W all three are mounted on the server", mounted == set(scopes), (mounted, app))
        from mastercode.runtime import kernel_ops as KO
        check("W the send is walled from the Host (the person's own message), with its reason",
              "/api/workbench/index/send" in KO._WALLED_PATHS and "/api/workbench/index/send" in KO._WALL_REASONS
              and KO.exposed_route("POST", "/api/workbench/index/send") is None)

        # ── H: the searches over HTTP, their words in a POST body ───────────
        print("-- H: the Index's and the Start dialog's searches take their words in a POST body")
        await h_post_routes(P, W, pool)
    finally:
        bs._pool, bs._entity_id, bs._send_message_inner, P.ensure_project_dir, P.ensure_project_repo = saved
        await pool.close()


def main():
    with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as srv, \
            kernel_schema_db(srv) as dsn:
        asyncio.run(run(dsn))
    print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
