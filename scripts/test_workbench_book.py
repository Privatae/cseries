#!/usr/bin/env python3
"""Gate: the Workbench's book of pages, in a real browser.

Driven against the BUILT app (system-apps/workbench/index.html, which is the
book) over a mock kernel API that keeps
what the book writes (the seen marks with the turn they name, the last open
page, what was sent and nudged) and lets a scenario land a reply, start, nudge
or end a turn, the way the real routes would show it. Its turns stream the
runtime's frames (the session frame with the turn's token, the turn frame,
text, the context frame with the turn's spend record and price) from a buffer
a reattach replays, as the turn registry does; a reply's row lands after its
stream ends; and the list computes unseen_landed_at as the server's SQL does
(projects._PAGE_MARKS_JOINS: the newest landing after the mark, less replies
whose turn token the page marked as watched; scripts/test_workbench_seen_mark
_pg.py proves that SQL on a real database):

  B. the book: index.html opens it, on the last open page with
     its head (the project's kind and the not-a-person disclosure), title,
     "Where this stands" from GET /api/projects/{id}/page (lines written since
     the person last looked are tinted), the conversation and the composer;
     the right side's "This page" (the model, the effort pick reading the
     model's own default, the context panel) above the bench's sections, Runs
     closed; seven tabs in the list's order, then the Index tab, which turns
     to the Index; opening the page marks it seen; every font comes from the
     bundle, none from a font service or the desktop's /fonts/, and the
     bundle carries their licence while the page shows no credit for it.
     Under "Ship this project" the site-name box's placeholder fits the box
     and the address hint is a line of its own, whole (also on a phone, P).
  F. flipping: a tab click turns the page (mid-turn, a copy of the old page
     turns in 3D about its left edge over the new one) and → turns to the
     next older page; with prefers-reduced-motion it is a crossfade.
  L. lit tabs: a reply landing on another project (its turn shown running,
     with working dots, then done) lights that tab green; opening the page
     marks it seen; turning back, its tab is dark.
  A. a page with a Code Approval waiting is amber at once, and one counted
     elsewhere (no turn ends) lights amber on the list's own read, in 10 s.
  S. the seen mark when a turn lands on the page in front: the page's own
     turn ending (sent from its composer) marks it, naming the turn by its
     token, so its reply's row landing after the stream needs no second mark;
     a turn from elsewhere ending on it marks it again (the list shows it).
  W. a reply read and left at once: its row lands after the turn away, and
     that page's tab never lights, however slowly the row is saved (six
     seconds and several list reads after the mark), and also while the build
     its turn started has the page active after the mark (where 3a's time
     rule lit it); a landing after it lights the tab. A report that lands
     while that reply is still being written lights it.
  C. the running token count (3b): the head shows the page's total, "214.6k
     tokens · $1.84"; hover shows what it is made of and, on a second line,
     the engines' runs; dollars marked partial when a reply has no price,
     "since Oct 3" when older turns are uncounted, "tokens not counted" when
     none is; each Host reply shows its own "+N tokens" and price, the live
     one at once; and the total ticks up when the turn's row lands.
  N. Nudge (3b): while a turn runs, Send is Nudge and Stop stays; the words
     go into the turn (POST /api/brain/turns/{id}/nudge) and show as their
     own line, once; the line survives a reload before the turn read them
     (the replayed nudge frame) and after (the saved row); when the turn has
     ended first (409), the words go as the next message and the line gives
     way to it.
  H. hold-to-think (3b): a tap sends at the page's effort; holding the press
     climbs to high, then max, showing the level as it climbs, and sends at
     the level reached; Enter held in the box does the same, and so does a
     touch long-press; a model without max offers only high.
  P. the phone (3b): a full-width page, the tabs as a strip along the bottom
     with the Index tab in it, the right side a sheet the strip opens; a lit
     tab and the running count there too.
  K. the tabs are a vertical tablist: one selected tab, one tab stop, each
     controlling the page; ↓ ↑ Home End move along the tabs and ← → turn pages
     with the keyboard's focus kept on the tab; a click hands the composer its
     focus; "/" opens the Index with Shift held too (a German keyboard's "/");
     a tab that lights is said in a polite status line.
  I. the Index, a page of the book: typing re-sorts every page,
     open and archived, with no model call; Enter (send, the default) gives
     the message to the page the Host picks, one message at a time (a second
     Enter, or one from the Index closed and opened again mid-pick, sends
     nothing; the send carries its own request id) and lands there with a
     page turn, the Host's reason written on it, also when it is the page in
     front; an archived pick is restored and riffles up, a refused restore
     says why and nothing runs, and when the Host cannot pick the person
     does; Enter in "open" mode opens the top page; an archived page opened
     stays archived until written on, then comes back first, or says why it
     cannot; Escape from anywhere in the Index goes back to the page.
     "Starts a new page", a third mode chosen each time and never kept: Enter
     makes a page for the words as the Start box makes one (POST /api/projects,
     the title suggested from the words, the words its description), turns to
     it, and the words are its first message, sent once and shown once. The
     Index says "Starting the page…" meanwhile and takes no second Enter (also
     from the Index closed and opened again), a page that could not be made
     says why and the words stay; with reduced motion the book crossfades to
     it; on a phone the chip sits whole in the mode strip and a tap reaches
     it; an empty book starts one too; and the page's own late answer to "is a
     turn running" does not draw its first turn twice. A secret pasted into the
     box, or typed a character at a time with a pause between keys, never goes
     into a search URL: each search is a POST whose body has the secret shapes
     cut, and whose URL carries nothing.
  G. chapters: the buckets, each with its archived pages, then
     "Loose pages"; opening one riffles through it (the sheets turn one after
     another) and lands on its pages; renamed and coloured as a bucket is,
     each written once; a tab dragged onto one files its page there (PATCH
     place); a new chapter is a new bucket; with reduced motion a chapter or
     an archived page opens with the short crossfade, never a riffle.
     I and G wait on state, never on time: the page's clock is Playwright's,
     paused while a check looks at a riffle or a turn, and the mock's pick
     answers when the check lets it.
  R. the switch: index.html is the book, with no rail, no
     chat header and no bench column, and no book.html beside it; a
     project:<id> link opens that page, and an old #view=book link opens the
     book where it is; "Archive it" under This page asks first, archives the
     page (DELETE /api/projects/{id}), turns to the page after it and leaves
     it in its chapter in the Index; a chapter keeps the seven colours a
     bucket had, and "Delete" on it asks first and deletes the bucket, its
     pages staying; a run card opened from the right side and expanded is
     in front of the page, not under it.
  D. the desktop's colours (the page once did not take on the set
     desktop UI colours): the shell's tokens.css, then two token sets set on
     the frame's root as the shell's applyTokens sets them, the stock dark set
     (read off dashboard/base/css/tokens.css) and a light design the desktop
     contract passes. The page, the desk, the words, the accent, the lit tabs,
     the Index tab, the run card and its Open, the strands, the scrollbars, the
     Index, a dialog and, on a phone, the strip and the sheet take the tokens'
     colours, and their words read on them; the dark class and the colour
     scheme, which the code blocks' theme follows, go with the design. An
     engine the colour table does not name draws in the desktop's secondary
     ink and reads at 3:1 on the page on both sets; a Mermaid fence in the
     Host's words, which the markdown has no engine to draw, is a listing in
     the design's ink at 4.5:1 on its panel on both.
  O. This page's Open for the latest finished build (a finished
     app or site once could not be opened without scrolling up to the run): with the run's cards far up the conversation and the
     conversation at its end, an Open per finished engine is on screen
     without scrolling and opens what the run card's Open opens (the same
     window, label, tab and preview, on its changes when the run left no
     page); a newer run going leaves it, a newer run finished takes it; a page
     with no finished run shows none; on a phone it is in the sheet.

The screenshots a reviewer reads (desktop and phone): C-count-hover,
N-nudge-line, H-hold-mid-climb, L-lit and their phone twins, and
P-phone-strip; for the Index, I-index-typing, I-send-landed, G-chapter-open,
G-riffle-frame and their phone twins, and I-riffle-frame; for the new-page
mode, I-new-page-typed and I-new-page-landed and their phone twins; for the
desktop's colours, D-colours-, D-run-card-, D-index-, D-dialog- and D-mermaid- on
the dark and the light set, and D-colours- and D-sheet- -phone; for the Open,
O-open-on-this-page, O-open-moved and O-open-phone.

Needs Playwright + Chromium (python3 -m playwright install chromium) and a
free local port: no pod, no DB, no network beyond loopback. Run:

  python3 scripts/test_workbench_book.py

SHOTS_DIR=<dir> keeps the screenshots there. ONLY=scenario_phone,scenario_hold
runs only those scenarios, from any group (a quick look while working).

The scenarios run as three gates, one group each, so that no one process
outgrows the gate pool's 180 s CPU budget (scripts/pre_merge_gates.sh,
PER_GATE_TIMEOUT): this file runs "book", test_workbench_book_index.py runs
"index", and test_workbench_book_colours.py runs "colours". All of them in one
process spent over 180 s of CPU, and hang_watch stopped it.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
APP_DIR = ROOT / "system-apps" / "workbench"
_OWN_SHOTS = not os.environ.get("SHOTS_DIR")
SHOTS = Path(os.environ["SHOTS_DIR"]) if not _OWN_SHOTS else Path(tempfile.mkdtemp(prefix="wb-book-"))
SHOTS.mkdir(parents=True, exist_ok=True)

fails = 0
ONLY = {x.strip() for x in os.environ.get("ONLY", "").split(",") if x.strip()}


def check(name, ok, detail=""):
    global fails
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        fails += 1


# ── the mock kernel ──────────────────────────────────────────────────────────
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse  # noqa: E402
import uvicorn  # noqa: E402

from gate_loopback import IDLE_KEEP_ALIVE_S  # noqa: E402

STATE: dict = {}
ENTITY = 9128


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime | None) -> str | None:
    # The server's own spelling: datetime.isoformat(), six fraction digits.
    return t.isoformat() if t else None


def proj(i, name, minutes_ago, *, status="active", seen=None, landed=None, asks=0,
         approvals=0, repo=None):
    # `last_landed_at` here is a landing that carries no turn token (an agent's
    # report, a finished task, a turn from elsewhere); `replies` are the Host's
    # replies with their turn's token, as their rows carry it; `seen_turns` the
    # tokens the page marked as watched (v217).
    return {"id": i, "name": name, "status": status,
            "group": {"active": "active", "planning": "pending", "complete": "completed",
                      "archived": "archived"}.get(status, "pending"),
            "description": "", "bucket_id": None, "sort_order": None, "phase_count": 0,
            "repo": repo, "code_on_machine": False, "step_in_flight_title": None,
            "last_activity_at": now() - timedelta(minutes=minutes_ago),
            "seen_at": seen, "last_landed_at": landed, "replies": [], "seen_turns": [],
            "ask_cards_open": asks, "approvals_waiting": approvals,
            "totals": None, "engines": None}


def totals(**extra):
    """context_budget.running_spend's shape: a page's counted turns."""
    out = {"scope": {"kind": "project", "ref": "21"}, "turns_counted": 12, "turns_unknown": 0,
           "first_counted_at": iso(datetime(2026, 10, 3, 9, 15, tzinfo=timezone.utc)), "calls": 41,
           "tokens": {"uncached": 30_100, "cache_read": 160_212, "cache_write": 9_900, "output": 14_400},
           "tokens_total": 214_612, "calls_usage_unknown": 0, "est_usd": 1.8412, "turns_priced": 12,
           "turns_unpriced": 0, "usd_partial": False, "usd_label": "ESTIMATE — each reply's list price"}
    out.update(extra)
    return out


# compute.project_run_usage's shape: the page's coding-engine runs.
ENGINES = {"runs": 2, "ok_runs": 2, "accepted": 1, "acceptance_rate": 0.5, "committed_files": 3,
           "tokens": 1_204_000, "tokens_covered_runs": 2, "compute_s": 744.0, "wall_clock_s": 744.0,
           "billed_usd": 0.31, "est_compute_usd": 0.28, "privatae_usd": 0.59, "cost_per_accepted_usd": 0.59,
           "vendor_est_usd": None, "vendor_est_runs": 0,
           "lanes": {"billed_usd": "billed", "est_compute_usd": "estimated", "vendor_est_usd": "ESTIMATE"}}

# The Host model the mock's effort options name (below), and the levels the
# page offers by default: the Anthropic model's own list, max included.
OPUS_EFFORTS = ["low", "medium", "high", "max"]


def reset():
    t = now()
    STATE.clear()
    STATE.update({
        # Newest activity first: 21..28 are open, 29 is archived (the newest of
        # all, and not in the book), 30 never had its page opened.
        "projects": [
            proj(21, "Storefront", 1, seen=t - timedelta(minutes=5), landed=t - timedelta(minutes=2),
                 repo="alex/storefront"),
            proj(22, "Newsletter signup", 14, seen=t - timedelta(minutes=20), landed=t - timedelta(minutes=30)),
            proj(23, "Photo resizer", 52, seen=t - timedelta(hours=2), landed=t - timedelta(hours=3), approvals=1),
            proj(24, "Recipe site", 120, seen=t - timedelta(hours=2), landed=t - timedelta(hours=3)),
            proj(25, "Inventory sync", 300, seen=t - timedelta(hours=5), landed=t - timedelta(hours=6)),
            proj(26, "Garden planner", 600, status="planning", seen=t - timedelta(hours=9)),
            proj(27, "Tax papers", 900, status="complete", seen=t - timedelta(hours=14)),
            proj(28, "Old blog", 2000, seen=t - timedelta(days=2)),
            proj(29, "Archived thing", 0, status="archived"),
            proj(30, "Reading list", 3000, seen=None, landed=t - timedelta(days=2)),
        ],
        "last_open": "21",
        "seen_puts": [],
        "turns": [],
        "messages": [],
        # 3b: what was sent (POST /api/brain/messages bodies), nudged and
        # stopped; how the mock's turns behave.
        "sent": [],
        "nudges": [],
        "cancels": [],
        # The Index's "starts a new page": what POST /api/projects was asked
        # (the Start box's own route), the hold a check puts on it while the
        # server makes the page, and an error it can be made to answer.
        "creates": [],
        "create_hold": None,
        "create_fails": None,
        "effort_efforts": list(OPUS_EFFORTS),
        "turn_secs": 0.3,
        "reply_write_s": REPLY_WRITE_S,
        "read_nudges": True,
        "nudge_409": False,
        # PII mode: the nudge route writes nothing and answers held: false,
        # so words a turn never reads are the page's to send.
        "pii": False,
        # Words the server sent on as a page's next turn (requeue), the
        # pending list's reads, and words a gone process left held, by page.
        "requeued": [],
        "pending_reads": [],
        "restart_held": {},
        # Seconds the turn's id frame (2:[{type:"turn"}]) comes after its
        # session frame: a slow start.
        "turn_frame_delay": 0,
    })
    row(21)["totals"] = totals()
    row(21)["engines"] = dict(ENGINES)
    row(22)["totals"] = totals(turns_counted=4, turns_unknown=30, tokens_total=48_200, calls=9,
                               tokens={"uncached": 6_000, "cache_read": 36_000, "cache_write": 2_000, "output": 4_200},
                               est_usd=0.42, turns_priced=3, turns_unpriced=1, usd_partial=True)
    row(24)["totals"] = totals(turns_counted=0, turns_unknown=5, tokens_total=0, calls=0,
                               tokens={"uncached": 0, "cache_read": 0, "cache_write": 0, "output": 0},
                               est_usd=None, turns_priced=0, turns_unpriced=5, usd_partial=True,
                               first_counted_at=None)
    TURNS.clear()


def row(pid):
    return next(p for p in STATE["projects"] if p["id"] == pid)


def landed(p):
    """(last_landed_at, unseen_landed_at) as the server's list computes them
    (projects._PAGE_MARKS_COLS): the newest landing, and the newest after the
    mark that is not a reply the page marked as watched. A landing with no
    token is never one."""
    anon = p["last_landed_at"]
    reps = p["replies"]
    last = max([x for x in [anon] + [a for a, _t in reps] if x], default=None)
    seen = p["seen_at"]
    cands = ([anon] if anon else []) + [a for a, tok in reps if tok not in p["seen_turns"]]
    unseen = max([c for c in cands if seen and c > seen], default=None)
    return last, unseen


def wire(p):
    d = {k: v for k, v in p.items() if k not in ("replies", "seen_turns", "totals", "engines")}
    d["last_landed_at"], d["unseen_landed_at"] = landed(p)
    for k in ("last_activity_at", "seen_at", "last_landed_at", "unseen_landed_at"):
        d[k] = iso(d[k])
    d["waiting_on_you"] = d["ask_cards_open"] + (d["approvals_waiting"] or 0)
    d["approvals_checked_at"] = None
    return d


app = FastAPI()


@app.get("/api/projects")
def list_projects(include_archived: bool = False, limit: int = 50):
    rows = [wire(p) for p in STATE["projects"] if include_archived or p["status"] != "archived"]
    rows.sort(key=lambda r: (r["last_activity_at"] or ""), reverse=True)
    return {"projects": rows[:limit], "offset": 0, "next_offset": None}


@app.get("/api/projects/last-open")
async def last_open():
    # A slow answer, STATE["last_open_delay"] seconds: the book's boot waits
    # for it while the list it read in parallel already drew the tabs.
    if STATE.get("last_open_delay"):
        await asyncio.sleep(STATE["last_open_delay"])
    STATE["last_open_answered"] = time.time()
    return {"ref": STATE["last_open"]}


@app.put("/api/projects/last-open")
async def put_last_open(req: Request):
    body = await req.json()
    STATE["last_open"] = str(body.get("ref") or body.get("project_id") or "")
    # How many have landed: a scenario that leaves a page waits for its own
    # write instead of racing it (scenario_switch).
    STATE["last_open_n"] = STATE.get("last_open_n", 0) + 1
    return {"ref": STATE["last_open"]}


@app.put("/api/projects/{pid}/seen")
async def seen(pid: int, req: Request):
    """projects.mark_project_seen_service, in memory: GREATEST(seen_at, now()),
    and the turn the body names kept on the page as watched (the last 32)."""
    raw = await req.body()
    token = (json.loads(raw) or {}).get("turn_token") if raw else None
    p = row(pid)
    t = now()
    p["seen_at"] = max(p["seen_at"], t) if p["seen_at"] else t
    if token:
        p["seen_turns"] = ([x for x in p["seen_turns"] if x != token] + [token])[-32:]
    STATE["seen_puts"].append({"id": pid, "at": t, "token": token})
    return {"project_id": pid, "seen_at": iso(p["seen_at"])}


@app.get("/api/projects/{pid}/page")
def page(pid: int):
    p = row(pid)
    stands = None
    if pid == 21:
        t = now()
        stands = {
            "text": "Where this thread stands …",
            "goal": "Checkout usable on phones in under 2 s",
            "goal_at": iso(t - timedelta(hours=3)),
            "next_step": "Pick which engine's version lands on the live site",
            "open_work": ["Codex is still testing: 15 of 20", "The payment form still loads first"],
            "identifiers": ["alex/storefront", "run #702"],
            "handoff_at": iso(p["last_landed_at"]),
        }
    elif pid == 22:
        stands = {"text": "…", "goal": "A newsletter signup in the footer, double opt-in",
                  "goal_at": iso(now() - timedelta(hours=1)), "next_step": None, "open_work": [],
                  "identifiers": [], "handoff_at": None}
    w = wire(p)
    STATE.setdefault("page_reads", []).append(pid)
    return {"project_id": pid, "scope": {"kind": "workbench", "ref": str(pid)},
            "where_this_stands": stands, "totals": p["totals"], "engines": p["engines"],
            **{k: w[k] for k in ("seen_at", "last_landed_at", "unseen_landed_at", "waiting_on_you",
                                 "ask_cards_open", "approvals_waiting", "approvals_checked_at")}}


@app.get("/api/projects/{pid}/repository")
def repository(pid: int):
    p = row(pid)
    return {"project_id": pid, "repo": p["repo"], "on_machine": False, "machine": None, "url": "",
            "owner": None, "owned_by_host": True, "default_branch": "main", "can_delete": False,
            "delete_refusal": None, "host_can_merge": False, "auto_approve": False}


@app.get("/api/projects/{pid}/modules")
def modules(pid: int):
    return {"project_id": pid, "root_app": None, "modules": [
        {"name": "checkout", "target": "public_site", "validation": "proof", "description": "",
         "current_version": {"number": 3, "run_id": 690, "commit": "abc1234", "source": "build", "at": iso(now())},
         "version_count": 3, "app": None, "candidates": []}]} if pid == 21 else {
        "project_id": pid, "root_app": None, "modules": []}


@app.get("/api/projects/{pid}/phases")
def phases(pid: int):
    if pid != 21:
        return {"phases": []}
    return {"phases": [
        {"id": 1, "phase_number": 1, "title": "Measure checkout on a phone", "status": "complete",
         "module": None, "validation": "proof", "acceptance_checks": 0, "step_count": 2, "steps_done": 2, "gate": "none"},
        {"id": 2, "phase_number": 2, "title": "Load the payment form last", "status": "implementing",
         "module": None, "validation": "proof", "acceptance_checks": 1, "step_count": 3, "steps_done": 1, "gate": "none"},
    ]}


@app.get("/api/workbench/approvals")
def approvals(project_id: int):
    rows = []
    if project_id == 23:
        rows = [{"number": 7, "title": "Add “resize all” with a progress bar", "summary": "3 files change.",
                 "state": "waiting", "state_words": "waiting for you", "ready": True, "models": ["claude-opus-5-5"],
                 "merged_by": None, "announce": False}]
    return {"connected": True, "repo": row(project_id)["repo"], "approvals": rows}


@app.get("/api/compute/runs")
def runs(project_id: int | None = None, window: str = "", limit: int = 40):
    if project_id != 21:
        return {"runs": []}
    return {"runs": [
        {"id": 702, "session_id": "sess-a", "project_id": 21, "project_name": "Storefront", "harness": "claude-code",
         "model": "opus", "ok": True, "status": "active", "exit_code": 0, "has_error": False, "diff_bytes": 10,
         "elapsed_s": 372, "created_at": iso(now())}]}


# ── the bench in the book: a recorded compare ───────────────────────────────
# scripts/fixtures/workbench_book_run.json: two engines' frames in the harness
# runtime's shape, each at its time after the brief was sent. A scenario sends
# the run `ago` seconds in the past on a server clock `offset` seconds off the
# browser's (the pod's clock): frames written by now replay at once, the rest
# arrive when they are due, and the stream returns at the engine's terminal
# frame, as review.py's stream route does. The run's rows, detail, patch and
# version follow the frames.
BENCH = json.loads((Path(__file__).resolve().parent / "fixtures" / "workbench_book_run.json").read_text())
BENCH_SID = BENCH["session"]["session_id"]
BENCH_HS = list(BENCH["session"]["harnesses"])


def bench_start(ago: float, offset: float = 0.0) -> None:
    STATE["bench"] = {"t0": time.time() - ago, "offset": offset, "opens": {}}


def bench_elapsed() -> float:
    return time.time() - STATE["bench"]["t0"]


def bench_ts(t: float) -> float:
    """A moment of the run on the server's (the pod's) clock."""
    return STATE["bench"]["t0"] + STATE["bench"]["offset"] + t


def bench_frame(h: str, i: int) -> dict:
    t, kind, payload = BENCH["engines"][h]["frames"][i]
    return {"ts": bench_ts(t), "seq": i, "kind": kind, "payload": payload}


def bench_state(h: str) -> dict:
    frames = BENCH["engines"][h]["frames"]
    el = bench_elapsed()
    end = frames[-1]
    if el >= end[0]:
        return {"state": "done", "elapsed_seconds": end[2].get("elapsed_seconds"), "model": end[2].get("model"),
                "diff_bytes": len(BENCH["engines"][h]["patch"])}
    began = [f for f in frames if f[0] <= el and not (f[1] == "status" and f[2].get("state") == "waiting")]
    if began:
        return {"state": "running", "model": BENCH["engines"][h]["model"]}
    if frames[0][2].get("state") == "waiting" and frames[0][0] <= el:
        return {"state": "queued", "queued_for": "memory", "queued_note": frames[0][2]["detail"]}
    return {"state": "queued"}


def bench_row() -> dict:
    per = {h: bench_state(h) for h in BENCH_HS}
    over = all(v["state"] in ("done", "failed") for v in per.values())
    last = max(BENCH["engines"][h]["frames"][-1][0] for h in BENCH_HS)
    return {"session_id": BENCH_SID, "prompt": BENCH["session"]["prompt"][:120], "started_at": bench_ts(0),
            "completed_at": bench_ts(last + 0.4) if over else None, "harnesses": BENCH_HS,
            "harness_count": len(BENCH_HS) if over else 0, "has_picks": False,
            "status": "done" if over else "running", "per_harness": per, "project_id": 21, "max_minutes": 20,
            "samples": {}, "seeded": True, "vcpus": None}


def patch_counts(h: str) -> tuple[int, int]:
    add = dele = 0
    for line in BENCH["engines"][h]["patch"].split("\n"):
        if line.startswith(("+++", "---")):
            continue
        add += line.startswith("+")
        dele += line.startswith("-")
    return add, dele


# A newer run on the page than the recorded compare (scenario_open): a build
# on one engine, running until a check finishes it, as review.py lists it.
def extra(sid: str) -> dict | None:
    return next((x for x in STATE.get("extra_sessions", []) if x["session_id"] == sid), None)


def extra_row(x: dict) -> dict:
    done = x["state"] == "done"
    return {"session_id": x["session_id"], "prompt": "You are working in the project's own worktree …",
            "started_at": x["t0"], "completed_at": x["t0"] + 40 if done else None, "harnesses": [x["harness"]],
            "harness_count": 1 if done else 0, "has_picks": False, "status": x["state"],
            "per_harness": {x["harness"]: {"state": x["state"], "model": "kimi-k2"}},
            "project_id": x["project_id"], "max_minutes": 20, "samples": {}, "seeded": True, "vcpus": None}


@app.get("/api/review/sessions")
def sessions(project_id: int | None = None, limit: int = 20):
    rows = [bench_row()] if STATE.get("bench") and project_id in (None, 21) else []
    rows += [extra_row(x) for x in STATE.get("extra_sessions", []) if project_id in (None, x["project_id"])]
    # Newest sent first, as list_sessions_service sorts them.
    rows.sort(key=lambda r: r["started_at"] or 0, reverse=True)
    return {"sessions": rows}


@app.get("/api/review/sessions/{sid}")
def session_detail(sid: str):
    x = extra(sid)
    if x:
        row = extra_row(x)
        return {"session_id": sid, "status": row["status"],
                "manifest": {"prompt": row["prompt"], "harnesses": row["harnesses"], "project_id": x["project_id"],
                             "seeded": True, "module": None, "started_at": row["started_at"],
                             "completed_at": row["completed_at"]},
                "per_harness": row["per_harness"], "entrypoints": {x["harness"]: True}}
    if sid != BENCH_SID or not STATE.get("bench"):
        return JSONResponse({"detail": "no such session"}, status_code=404)
    row = bench_row()
    # The module the compare was built for, and the engines whose worktree has
    # no page to show (review.py _session_entrypoints), when a scenario says.
    return {"session_id": sid, "status": row["status"],
            "manifest": {"prompt": BENCH["session"]["prompt"], "harnesses": BENCH_HS, "project_id": 21,
                         "seeded": True, "module": STATE.get("bench_module"), "started_at": row["started_at"],
                         "completed_at": row["completed_at"]},
            "per_harness": row["per_harness"],
            "entrypoints": {h: h not in STATE.get("bench_no_entry", ()) for h in BENCH_HS}}


@app.get("/api/review/sessions/{sid}/stream/{h}")
async def session_stream(sid: str, h: str, from_seq: int = 0):
    x = extra(sid)
    if x and h == x["harness"]:
        async def build_tail():
            # A command at once; the end when the check finishes the build.
            if from_seq <= 0:
                yield json.dumps({"ts": x["t0"] + 1, "seq": 0, "kind": "tool_call",
                                  "payload": {"tool": "Bash", "args": {"command": "npm test"}}}) + "\n"
            while x["state"] != "done":
                if extra(sid) is not x:  # a later scenario reset the mock
                    return
                await asyncio.sleep(0.2)
            if from_seq <= 1:
                yield json.dumps({"ts": x["t0"] + 40, "seq": 1, "kind": "status",
                                  "payload": {"state": "complete", "ok": True, "exit_code": 0,
                                              "elapsed_seconds": 39.0, "model": "kimi-k2", "stderr": ""}}) + "\n"

        return StreamingResponse(build_tail(), media_type="application/x-ndjson")
    if sid != BENCH_SID or not STATE.get("bench") or h not in BENCH["engines"]:
        return JSONResponse({"detail": "no such stream"}, status_code=404)
    run = STATE["bench"]
    run["opens"][h] = run["opens"].get(h, 0) + 1
    frames = BENCH["engines"][h]["frames"]

    async def tail():
        i = max(0, from_seq)
        while i < len(frames):
            if STATE.get("bench") is not run:  # a later scenario reset the mock
                return
            wait = run["t0"] + frames[i][0] - time.time()
            if wait > 0:
                await asyncio.sleep(min(wait, 0.2))
                continue
            yield json.dumps(bench_frame(h, i)) + "\n"
            i += 1

    return StreamingResponse(tail(), media_type="application/x-ndjson")


@app.get("/api/review/sessions/{sid}/diff/{h}")
def session_diff(sid: str, h: str):
    if extra(sid):
        return {"diff": ""}
    if sid != BENCH_SID or not STATE.get("bench") or h not in BENCH["engines"]:
        return JSONResponse({"detail": "no such run"}, status_code=404)
    STATE["bench"].setdefault("diff_reads", []).append(h)
    return {"diff": BENCH["engines"][h]["patch"] if bench_state(h)["state"] == "done" else ""}


@app.get("/api/review/sessions/{sid}/preview/{h}")
def session_preview(sid: str, h: str):
    x = extra(sid)
    if x and h == x["harness"]:
        return Response(f"<!doctype html><html><body style=\"font:16px sans-serif;padding:48px\">"
                        f"<h1>Storefront</h1><p>The version {sid} built.</p></body></html>", media_type="text/html")
    if (sid != BENCH_SID or not STATE.get("bench") or h not in BENCH["engines"]
            or h in STATE.get("bench_no_entry", ())):
        return JSONResponse({"detail": "nothing renderable in this version"}, status_code=404)
    return Response(f"<!doctype html><html><body style=\"font:16px sans-serif;padding:48px;background:#f6f3ec\">"
                    f"<h1>Checkout</h1><p>The version {h} built.</p><button>Pay</button></body></html>",
                    media_type="text/html")


@app.get("/api/agents")
def agents(limit: int = 100, active_only: bool = False):
    return {"agents": []}


@app.get("/api/agents/scheduled")
def scheduled():
    return {"scheduled": []}


@app.get("/api/tickets")
def tickets(status: str = "", limit: int = 50, asked_in: str = ""):
    return {"tickets": []}


@app.get("/api/network/nac/connector")
def connectors():
    return {"connectors": []}


# The Host model the mock's effort options name, and the level it runs at when
# none is picked: the server's own table (inference_config
# anthropic_default_effort, what routes/inference.py effort_options answers),
# never a value typed here. A hand-typed "high" for claude-opus-5-5 had the
# screenshots show "Default · high" for a model that runs at medium.
HOST_MODEL = "claude-opus-5-5"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "brain"))
from mastercode.inference_config import anthropic_default_effort  # noqa: E402

HOST_MODEL_DEFAULT_EFFORT = anthropic_default_effort(HOST_MODEL)


@app.get("/api/inference/effort-options")
def effort_options():
    return {"provider": "anthropic", "model": HOST_MODEL, "efforts": STATE.get("effort_efforts", OPUS_EFFORTS),
            "default": HOST_MODEL_DEFAULT_EFFORT, "known": True}


@app.get("/api/brain/thread-context")
def thread_context(app_id: str = "", project_id: int | None = None):
    return {"window": 1_000_000, "budget": None, "summary_version": None, "consolidated_at": None,
            "blocks": [{"name": "system", "chars": 41000}], "history_preview": [], "summary_excerpt": None,
            "thread_state": None, "last_prefired": [], "prefire_state": "thread", "turn_allowance_usd": None,
            "allowance_basis": "usd", "allowance_tokens": None, "projected_next_turn_usd": 0.04, "last_turn": None}


def _turn(i, role, text, minutes_ago, **md):
    return {"id": i, "role": role, "content": text, "created_at": iso(now() - timedelta(minutes=minutes_ago)),
            "metadata": md}


@app.get("/api/brain/chat")
def chat(project_id: int | None = None, app_id: str = "", limit: int = 60, order: str = "asc", since: str = ""):
    turns = []
    if project_id == 21:
        turns = [
            _turn(501, "user", "Checkout feels slow on phones. Find out why and fix it. Try two engines and show me.", 40),
            _turn(502, "assistant",
                  "Measured first: on a mid-range phone, checkout takes **3.8 s** to become usable, mostly the "
                  "payment form loading before anything else. Same brief to Claude Code and Codex, each in its "
                  "own sandbox.", 39, model="claude-opus-5-5", provider="anthropic", gen_ms=21400,
                  tokens_in=18200, tokens_out=1200, tokens_total=19400, tokens_cached=12000,
                  turn_token="tok-502", est_cost_usd=0.0921, price_basis="list",
                  context_ledger={"calls": 3, "spend": {"calls": 3, "tokens": {
                      "uncached": 1_200, "cache_read": 12_000, "cache_write": 5_000, "output": 1_200}}},
                  effort={"sent": "high", "default": "medium", "chosen": "high"}),
            _turn(503, "assistant",
                  "Claude Code's version is ready: usable in **1.1 s**. The preview is live; Codex's number goes "
                  "beside it when it lands.", 2, model="claude-opus-5-5", provider="anthropic", gen_ms=6200,
                  tokens_in=21000, tokens_out=400, tokens_total=21400, tokens_cached=19000,
                  turn_token="tok-503", est_cost_usd=0.1842, price_basis="list",
                  context_ledger={"calls": 2, "spend": {"calls": 2, "tokens": {
                      "uncached": 900, "cache_read": 19_000, "cache_write": 1_100, "output": 400}}}),
        ]
        if STATE.get("bench"):
            # The Host's compare, as its turn row keeps it: the build chip with
            # the compare's session (session.py _tc_meta), between the brief and
            # the reply that reported it.
            turns.insert(2, _turn(504, "assistant", "Same brief to Claude Code and Codex, each in its own sandbox.",
                                  7, model="claude-opus-5-5", provider="anthropic", gen_ms=3100,
                                  tool_calls=[{"tool": "build", "op": "run", "status": "ok",
                                               "args_summary": "compare · claude-code, codex",
                                               "session_id": BENCH_SID, "task_id": 9001}]))
    elif project_id == 22:
        turns = [
            _turn(601, "user", "Add a newsletter signup to the footer. Double opt-in, nothing pushy.", 33),
            _turn(602, "assistant", "On it. One field, one button, a thank-you line, and a confirmation email "
                  "you read before it's connected.", 31, model="claude-opus-5-5", provider="anthropic", gen_ms=5100),
        ]
    turns += STATE.get("old_turns", {}).get(project_id, [])
    turns += [m for m in STATE["messages"] if m["project_id"] == project_id]
    return {"turns": list(reversed(turns))}  # newest first, as the route answers order=desc


# The turn registry in miniature (scripts/book_mock_turns.py, shared with the
# cards' harness): every turn's frames in a buffer that the sender tails and a
# reattach replays from the start; a nudge goes into the running turn as the
# registry's `nudge` frame, and the turn reads it at its next step (a
# `nudge_read` frame, and the words saved as a user row with metadata.nudge);
# a turn that has stopped reading answers a nudge 409; the reply's row lands
# REPLY_WRITE_S after the stream ends.
from book_mock_turns import (  # noqa: E402
    REPLY_TAIL, REPLY_TOKENS, REPLY_USD, REPLY_WRITE_S, MockTurn as _BookTurn, TurnHost)

TURNS: dict = {}


class _WorkbenchTurns(TurnHost):
    model = "claude-opus-5-5"

    def settings(self) -> dict:
        return STATE

    def thread_key(self, turn) -> str:
        return f"project|{ENTITY}|{turn.page}"

    def session_id(self, turn) -> str:
        return f"s-{turn.page}"

    def write_row(self, turn, role, content, created_at, metadata) -> None:
        STATE["messages"].append({"project_id": turn.page, "id": 900 + len(STATE["messages"]), "role": role,
                                  "content": content, "created_at": created_at, "metadata": metadata})

    def live(self, turn) -> bool:
        return TURNS.get(turn.id) is turn

    def requeue(self, turn, words):
        """turn_registry._requeue_undelivered: the nudge route's words are the
        server's (pending_words), so words the turn ended without
        reading start the page's next turn, joined. In PII mode nothing was
        held: they are the page's to send."""
        if STATE["pii"]:
            return None
        nxt = MockTurn(turn.page, "\n\n".join(t for t, _at, _cid in words))
        TURNS[nxt.id] = nxt
        STATE["requeued"].append({"from": turn.id, "to": nxt.id, "text": nxt.message})
        asyncio.get_running_loop().create_task(nxt.run())
        return nxt

    def landed(self, turn, at) -> None:
        """The page's totals count the reply that landed."""
        if turn.page is None:
            return
        p = row(turn.page)
        p["replies"].append((at, turn.token))
        STATE["reply_landed"] = at
        tot = p["totals"]
        if tot is not None:
            tot["turns_counted"] += 1
            tot["calls"] += 2
            for k, v in REPLY_TOKENS.items():
                tot["tokens"][k] += v
            tot["tokens_total"] += sum(REPLY_TOKENS.values())
            tot["est_usd"] = round((tot["est_usd"] or 0) + REPLY_USD, 6)
            tot["turns_priced"] += 1


_WB_TURNS = _WorkbenchTurns()


class MockTurn(_BookTurn):
    """A Host turn on a Workbench page (`pid`, the project)."""

    def __init__(self, pid, message):
        super().__init__(_WB_TURNS, pid, message)

    @property
    def pid(self):
        return self.page


@app.get("/api/brain/turns/active")
async def turns_active(project_id: int | None = None):
    # A page asking whether its own thread's turn runs (the pane's reattach)
    # can be made to wait for the answer, STATE["active_delay"] seconds; the
    # answer is computed after the wait, and kept for a check to read.
    if project_id is not None and STATE.get("active_delay"):
        await asyncio.sleep(STATE["active_delay"])
    live = list(TURNS.values())
    turn = next((t.describe() for t in live if t.pid == project_id and not t.done), None)
    if project_id is not None:
        STATE.setdefault("active_answers", []).append((project_id, turn is not None))
    return {"ok": True, "turn": turn, "turns": STATE["turns"] + [t.describe() for t in live]}


@app.post("/api/brain/messages")
async def messages(req: Request):
    """A Host turn on the page in front: it streams for STATE["turn_secs"],
    reading any nudge on its way, the stream ends, and the reply's row lands
    STATE["reply_write_s"] later. The body is kept (its `effort` above all)."""
    body = await req.json()
    STATE["sent"].append(body)
    if body.get("project_id") is not None:
        # The list's last activity counts the page's newest turn row
        # (projects._LAST_ACTIVITY_SQL): the page written on is the newest.
        row(int(body["project_id"]))["last_activity_at"] = now()
    turn = MockTurn(body.get("project_id"), str(body.get("message") or ""))
    TURNS[turn.id] = turn
    asyncio.get_running_loop().create_task(turn.run())
    return StreamingResponse(turn.tail(0), media_type="text/event-stream")


@app.get("/api/brain/turns/{turn_id}/stream")
def turn_stream(turn_id: str, from_seq: int = 0):
    STATE.setdefault("stream_reads", []).append(turn_id)  # a page attaching to a turn
    t = TURNS.get(turn_id)
    if t is None:
        return JSONResponse({"detail": "unknown turn"}, status_code=404)
    return StreamingResponse(t.tail(max(0, from_seq)), media_type="text/event-stream")


@app.post("/api/brain/turns/{turn_id}/nudge")
async def turn_nudge(turn_id: str, req: Request):
    """brain.py brain_turn_nudge: 409 once the turn has ended or stopped
    reading (STATE["nudge_409"] makes it so while it still streams)."""
    body = await req.json()
    text = str(body.get("text") or "").strip()
    t = TURNS.get(turn_id)
    cid = body.get("client_id") if isinstance(body.get("client_id"), str) else None
    ok = t is not None and t.nudge(text, cid, refused=STATE["nudge_409"])
    STATE["nudges"].append({"turn": turn_id, "text": text, "client_id": cid, "status": 200 if ok else 409})
    if not ok:
        return JSONResponse({"detail": "the turn has ended"}, status_code=409)
    STATE["nudges"][-1]["client_id"] = cid
    STATE["nudges"][-1]["held"] = not STATE["pii"]
    t.emit("2:" + json.dumps([{"type": "nudge", "text": text[:500],
                               **({"client_id": cid} if cid else {})}]) + "\n")
    return {"ok": True, "turn_id": turn_id, "queued_chars": len(text), "held": not STATE["pii"]}


@app.get("/api/brain/pending")
def pending(project_id: int | None = None):
    """GET /api/brain/pending: the words the server holds for the page — sent
    into its running turn and not read yet, or left by a process that is gone
    (pending_words). Nothing in PII mode."""
    words = [] if STATE["pii"] else [
        {"id": f"pw-{t.id}-{i}", "text": text, "client_id": cid, "received_at": iso(at)}
        for t in TURNS.values() if t.pid == project_id and not t.done
        for i, (text, at, cid) in enumerate(t.pending)]
    words += [{"id": f"pw-left-{i}", "text": text, "client_id": cid, "received_at": iso(at)}
              for i, (text, at, cid) in enumerate(STATE["restart_held"].get(project_id, []))]
    STATE["pending_reads"].append(len(words))
    return {"ok": True, "words": words}


@app.post("/api/brain/turns/{turn_id}/cancel")
def turn_cancel(turn_id: str):
    t = TURNS.get(turn_id)
    STATE["cancels"].append(turn_id)
    if t is None:
        return JSONResponse({"detail": "unknown turn"}, status_code=404)
    t.cancel()
    return {"ok": True, "status": "cancelled"}


# ── the Index and its chapters ──────────────────────────────────────────────
# What the book's Index reads and writes, in memory: the search
# (workbench_index.index_search_service: every page, open and archived, in
# order only), the send (index_send_service: the Host's pick, scripted by
# STATE["pick"], answered once the check lets it, STATE["pick_hold"]; an
# archived pick restored or refused, the marker written on the page, the turn
# started there), the buckets (the chapters), a drop's place, and the restore
# (PUT status).
REFUSAL = "Its Code Repository alex/storefront is used by Storefront; bring this page back after that one is archived."


def _words(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) >= 3]


def index_fixture() -> None:
    """Chapters (buckets) with archived pages, each with a line of its own
    conversation, for the Index's scenarios."""
    STATE["buckets"] = [{"id": 7, "name": "Storefront work", "color": "#10b981", "sort_order": 1},
                        {"id": 8, "name": "Launch", "color": None, "sort_order": 2},
                        {"id": 9, "name": "Ideas", "color": "#8b5cf6", "sort_order": 3}]
    old = [(31, "Checkout redesign", 20, 7, "Moved checkout to one page. On phones it became usable in 4.4 s."),
           (32, "Shipping rates", 25, 7, "Flat rate under 2 kg, live rates above; Canada sees duties up front."),
           (33, "Gift cards", 30, 7, "Gift cards in 25, 50 and 100 dollars; codes are emailed once."),
           (34, "Product photos", 35, 7, "Reshot 31 products on white; phone versions were never made."),
           (35, "Logo", 60, None, "Three logo directions; you picked the plain wordmark."),
           (36, "Old domain", 45, 8, "The old domain moved; its repository is the storefront's.")]
    for i, name, days, bucket, convo in old:
        p = proj(i, name, days * 24 * 60, status="archived")
        p["bucket_id"], p["convo"] = bucket, convo
        STATE["projects"].append(p)
        STATE.setdefault("old_turns", {})[i] = [
            _turn(i * 10, "user", f"Where did {name.lower()} end up?", days * 24 * 60 + 5),
            _turn(i * 10 + 1, "assistant", convo, days * 24 * 60, model=HOST_MODEL, provider="anthropic")]
    row(21)["convo"] = "Checkout feels slow on phones. Find out why and fix it."
    row(22)["convo"] = "Add a newsletter signup to the footer. Double opt-in, and the email you read first."
    STATE.update({"index_sends": [], "index_reads": [], "index_urls": [], "places": [], "project_puts": [], "bucket_writes": [],
                  "refuse_restore": {36}, "pick": None, "pick_hold": None})


def _index_page(p, snippet=None) -> dict:
    return {"id": p["id"], "name": p["name"], "status": p["status"], "group": p["group"],
            "archived": p["status"] == "archived", "bucket_id": p["bucket_id"],
            "last_activity_at": iso(p["last_activity_at"]), "snippet": snippet}


@app.post("/api/projects/index")
async def index_search(req: Request):
    """The search, in miniature: a word's first six letters in the name count
    three, in the page's conversation one; ties to the newer page. No score
    goes out, only the order, as the server's. The words come in the body
    ({q, limit}), as the server's route takes them: the URL it was asked on
    is kept in STATE["index_urls"] so a check can show it carried nothing."""
    body = await req.json()
    q, limit = str(body.get("q") or ""), int(body.get("limit") or 20)
    STATE.setdefault("index_reads", []).append(q)
    STATE.setdefault("index_urls", []).append(str(req.url))
    words = _words(q)
    rows = STATE["projects"]
    if not words:
        recent = sorted((p for p in rows if p["status"] != "archived"), key=lambda p: p["last_activity_at"],
                        reverse=True)
        return {"q": q, "pages": [_index_page(p) for p in recent[:limit]], "semantic": False,
                "conversation": {"used": [], "too_common": []}}
    scored = []
    for p in rows:
        name, convo = p["name"].lower(), (p.get("convo") or "").lower()
        score, hit = 0, None
        for w in words:
            stem = w[:6]
            if stem in name:
                score += 3
            if stem in convo:
                score += 1
                hit = hit or stem
        if score:
            scored.append((score, p["last_activity_at"], p, hit))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    out = []
    for _s, _at, p, hit in scored[:limit]:
        snippet = None
        if hit:
            c = p["convo"]
            i = c.lower().find(hit)
            snippet = {"from": "conversation", "text": c, "role": "assistant", "at": iso(p["last_activity_at"]),
                       "parts": [x for x in ({"text": c[:i], "mark": False}, {"text": c[i:i + len(hit)], "mark": True},
                                             {"text": c[i + len(hit):], "mark": False}) if x["text"]]}
        out.append(_index_page(p, snippet))
    return {"q": q, "pages": out, "semantic": False, "conversation": {"used": words, "too_common": []}}


@app.post("/api/workbench/index/send")
async def index_send(req: Request):
    """The Host's pick (STATE["pick"]): {"page": id, "reason"} lands there
    (an archived page restored, or a 409 when STATE["refuse_restore"] holds
    it), {"new": title} makes a page, {"ask": [ids]} asks the person. A
    landing writes the marker on the page and starts the turn there. While
    STATE["pick_hold"] (a threading.Event) is not set, the Host is still
    picking: a check holds the pick for as long as it looks at the wait."""
    body = await req.json()
    STATE["index_sends"].append(body)
    hold = STATE.get("pick_hold")
    while hold is not None and not hold.is_set():
        await asyncio.sleep(0.02)
    pick = STATE.get("pick") or {}
    text = str(body.get("text") or "")
    if pick.get("ask"):
        return {"state": "ask", "failure": "the model did not call pick_page",
                "candidates": [{"project_id": i, "name": row(i)["name"], "archived": row(i)["status"] == "archived",
                                "last_message_at": None} for i in pick["ask"]]}
    created = bool(pick.get("new"))
    if created:
        pid = max(p["id"] for p in STATE["projects"]) + 1
        STATE["projects"].append(proj(pid, pick["new"], 0, status="planning"))
    else:
        pid = int(pick["page"])
    p = row(pid)
    restored = False
    if p["status"] == "archived":
        if pid in STATE.get("refuse_restore", ()):
            return JSONResponse({"detail": {"state": "restore_refused", "project_id": pid, "project_name": p["name"],
                                            "reason": REFUSAL, "pick_reason": pick.get("reason")}}, status_code=409)
        p["status"], p["group"], restored = "active", "active", True
    marker = f"Index message, {'started' if created else 'picked'} this page: {pick.get('reason')}"
    STATE["messages"].append({"project_id": pid, "id": 900 + len(STATE["messages"]), "role": "assistant",
                              "content": marker, "created_at": iso(now()),
                              "metadata": {"source_kind": "index_pick"}})
    p["last_activity_at"] = now()
    turn = MockTurn(pid, text)
    TURNS[turn.id] = turn
    asyncio.get_running_loop().create_task(turn.run())
    return {"state": "started", "project_id": pid, "project_name": p["name"], "restored": restored,
            "created": created, "marker": marker, "reason": pick.get("reason"),
            "turn": {"turn_id": turn.id, "mode": "started", "status": "running"},
            "stream": f"/api/brain/turns/{turn.id}/stream"}


@app.post("/api/projects")
async def project_create(req: Request):
    """projects.create_project in miniature, the Start box's own route: a
    project with the table's default status ('planning'), named and described
    as asked, in the list from then on. While STATE["create_hold"] (a
    threading.Event) is not set the server is still making it (its Code
    Repository), as a check looks at the wait; STATE["create_fails"] makes it
    answer that error instead."""
    body = await req.json()
    STATE["creates"].append(body)
    hold = STATE.get("create_hold")
    while hold is not None and not hold.is_set():
        await asyncio.sleep(0.02)
    if STATE.get("create_fails"):
        return JSONResponse({"detail": STATE["create_fails"]}, status_code=502)
    pid = max(p["id"] for p in STATE["projects"]) + 1
    p = proj(pid, str(body.get("name") or ""), 0, status="planning")
    p["description"] = str(body.get("description") or "")
    STATE["projects"].append(p)
    return {"id": pid, "name": p["name"], "status": "planning", "repo": None, "repo_note": None}


@app.get("/api/projects/buckets")
def buckets_list():
    out = []
    for b in STATE.get("buckets", []):
        mine = [p for p in STATE["projects"] if p["bucket_id"] == b["id"]]
        n_arch = sum(p["status"] == "archived" for p in mine)
        out.append({**b, "counts": {"active": len(mine) - n_arch, "pending": 0, "completed": 0,
                                    "archived": n_arch, "total": len(mine)}})
    return {"buckets": out, "unfiled": {"counts": {"active": 0, "pending": 0, "completed": 0, "archived": 0,
                                                   "total": 0}}}


@app.post("/api/projects/buckets")
async def bucket_create(req: Request):
    body = await req.json()
    bid = max([b["id"] for b in STATE.get("buckets", [])] + [0]) + 1
    b = {"id": bid, "name": body.get("name"), "color": body.get("color"), "sort_order": bid}
    STATE.setdefault("buckets", []).append(b)
    STATE.setdefault("bucket_writes", []).append(("create", body))
    return b


@app.patch("/api/projects/buckets/{bid}")
async def bucket_patch(bid: int, req: Request):
    body = await req.json()
    STATE.setdefault("bucket_writes", []).append(("patch", bid, body))
    b = next(x for x in STATE["buckets"] if x["id"] == bid)
    for k in ("name", "color", "sort_order"):
        if k in body:
            b[k] = body[k] or None if k == "color" else body[k]
    return {"updated": True}


@app.patch("/api/projects/{pid}/place")
async def project_place(pid: int, req: Request):
    body = await req.json()
    STATE.setdefault("places", []).append({"id": pid, **body})
    row(pid)["bucket_id"] = body.get("bucket_id")
    return {"placed": True, "bucket_id": body.get("bucket_id"), "order": [pid]}


@app.put("/api/projects/{pid}")
async def project_put(pid: int, req: Request):
    """The restore (PUT {status: active}), refused for STATE["refuse_restore"]
    as projects.py refuses one whose Code Repository a live page holds."""
    body = await req.json()
    STATE.setdefault("project_puts", []).append({"id": pid, **body})
    p = row(pid)
    if body.get("status") == "active" and p["status"] == "archived":
        if pid in STATE.get("refuse_restore", ()):
            return JSONResponse({"detail": REFUSAL}, status_code=409)
        p["status"], p["group"] = "active", "active"
    return {"updated": True}


@app.delete("/api/projects/buckets/{bid}")
def bucket_delete(bid: int):
    """A chapter deleted: the bucket goes and its pages are unfiled, none
    deleted (projects.py delete_bucket)."""
    STATE.setdefault("bucket_writes", []).append(("delete", bid))
    STATE["buckets"] = [b for b in STATE.get("buckets", []) if b["id"] != bid]
    mine = [p for p in STATE["projects"] if p["bucket_id"] == bid]
    for p in mine:
        p["bucket_id"] = None
    return {"deleted": True, "unfiled": len(mine)}


@app.delete("/api/projects/{pid}")
def project_archive(pid: int, delete_repo: bool = False):
    """Archive (projects.py archive_project): status archived, nothing else
    goes; the repository too only when asked."""
    STATE.setdefault("archives", []).append({"id": pid, "delete_repo": delete_repo})
    p = row(pid)
    p["status"], p["group"] = "archived", "archived"
    return {"archived": True}


@app.get("/css/tokens.css")
def tokens():
    # The shell's own stylesheet when a scenario serves it (the desktop's
    # colours, scenario_colours); a bare root otherwise.
    return Response(STATE.get("tokens_css", ":root{}"), media_type="text/css")


@app.get("/js/{name}")
def shell_js(name: str):
    """The shell scripts the kernel serves at /js/ (sandbox-csp.js and
    desktop-preview.js, which index.html loads)."""
    target = (ROOT / "dashboard" / "base" / "js" / name).resolve()
    if target.parent != (ROOT / "dashboard" / "base" / "js").resolve() or not target.is_file():
        return Response(status_code=404)
    return FileResponse(str(target), media_type="text/javascript")


@app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def other_api(rest: str):
    return JSONResponse({"detail": "not mocked"}, status_code=404)


@app.get("/system-apps/workbench/{rest:path}")
def static(rest: str):
    target = (APP_DIR / (rest or "index.html")).resolve()
    if APP_DIR.resolve() not in target.parents:
        return Response(status_code=403)
    return FileResponse(str(target)) if target.is_file() else Response(status_code=404)


def serve() -> int:
    # Loopback on purpose: the only client is the headless Chromium this same
    # process launches on this same box.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # The browser hangs up first, never this server (gate_loopback.py
    # IDLE_KEEP_ALIVE_S says why). A refused read of the project list at boot
    # left the book with no page open (seen on CI).
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error",
                                           timeout_keep_alive=IDLE_KEEP_ALIVE_S))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    return port


# ── scenarios ────────────────────────────────────────────────────────────────
# The live page, never the copy that turns away over it (src/book/flip.ts).
SHEET = "article.book-sheet:not(.book-ghost)"
TITLE = f"{SHEET} h1.book-title"
# The live page by its id, which every copy a turn or a riffle lays over the
# book drops (src/book/flip.ts copyOfPage): the page's own title, never the
# Index's.
LIVE = "#book-page"
PAGE_TITLE = f"{LIVE} > .book-inner > h1.book-title"


def open_book(page, base):
    page.goto(f"{base}/system-apps/workbench/index.html", wait_until="domcontentloaded", timeout=15_000)
    expect(page.locator(TITLE)).to_have_text("Storefront", timeout=10_000)
    # The conversation is read and the composer is up.
    expect(page.locator(f"{SHEET} textarea")).to_be_visible(timeout=10_000)


def tab_ids(page):
    return [int(x) for x in page.locator(".book-tabs [data-tab]").evaluate_all(
        "els => els.map(e => e.dataset.tab)")]


def puts_for(pid):
    return [p for p in STATE["seen_puts"] if p["id"] == pid]


# The placeholder the site-name box had before the book: 217 px of text in a
# box of 182 px on a desktop and 151 px on a phone.
OLD_SLUG_PLACEHOLDER = "site name (becomes name.dev…)"


def publish_fits(page):
    """Under "Ship this project": the site-name box's placeholder, measured in
    the box's own font, fits inside its padding, and the address hint under
    it is a whole line on screen (nothing scrolled away or clipped). The old
    placeholder is measured in the same box as the control: it must not fit,
    or this check could not have caught it."""
    page.locator("aside.book-right [data-publish-slug]").scroll_into_view_if_needed(timeout=5_000)
    return page.evaluate("""old => {
        const i = document.querySelector('aside.book-right [data-publish-slug]');
        const h = document.querySelector('aside.book-right [data-publish-slug-hint]');
        if (!i || !h) return {ok: false, why: 'missing', input: !!i, hint: !!h};
        const cs = getComputedStyle(i);
        const ctx = document.createElement('canvas').getContext('2d');
        ctx.font = cs.font;
        const box = i.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
        const text = ctx.measureText(i.placeholder).width;
        const was = ctx.measureText(old).width;
        const hr = h.getBoundingClientRect();
        const side = document.querySelector('aside.book-right').getBoundingClientRect();
        const whole = h.scrollWidth <= h.clientWidth + 1 && hr.width > 0 && hr.height > 0
            && hr.left >= side.left - 0.5 && hr.right <= side.right + 0.5;
        return {ok: text <= box && whole, old_cut: was > box, placeholder: i.placeholder,
                text: Math.round(text * 10) / 10, box: Math.round(box * 10) / 10,
                old: Math.round(was * 10) / 10, hint: h.textContent.trim(), hint_whole: whole};
    }""", OLD_SLUG_PLACEHOLDER)


def scenario_book(page, base):
    print("B. the book: the page, the right side, seven tabs, the seen mark, the fonts")
    reset()
    open_book(page, base)
    # The window the checks that something does NOT happen get (no rail header,
    # the other tabs dark, a lit tab not read out). Whatever the page has
    # still to read and draw is waited for by condition below, never by this.
    page.wait_for_timeout(600)
    check("B: the book is on (data-page=book) in the desktop's colours: dark, on the stock tokens (scenario_colours "
          "takes a light design too)",
          wait_for(lambda: page.evaluate("document.documentElement.dataset.page") == "book"
                   and page.evaluate("document.documentElement.classList.contains('dark')"), SLOW_S))
    head_l = page.locator(f"{SHEET} [data-page-head]")
    head = lambda: head_l.text_content() or ""  # noqa: E731
    check("B: the head names the kind and the Code Repository",
          wait_for(lambda: "Project" in head() and "alex/storefront" in head(), SLOW_S), head())
    check("B: the head carries the not-a-person disclosure", "an AI, not a person" in head(), head())
    # "Where this stands" is one line until it is opened
    # (workbench-web/src/book/page-top.tsx Stands): its label and
    # the first line the open box shows. Opened, it is the kit's box in full.
    toggle = page.locator(f"{SHEET} [data-stands-toggle]")
    tl = lambda: toggle.text_content() or ""  # noqa: E731
    check("B: 'Where this stands' starts as one line: its label and the goal, closed",
          wait_for(lambda: "Goal: Checkout usable on phones in under 2 s" in tl(), SLOW_S)
          and toggle.get_attribute("aria-expanded") == "false" and "Where this stands" in tl()
          and page.locator(f"{SHEET} [data-stands]").count() == 0, tl()[:200])
    toggle.click()
    stands = page.locator(f"{SHEET} [data-stands]")
    st = lambda: stands.text_content() or ""  # noqa: E731
    check("B: 'Where this stands' shows the goal, the next step and the open work (GET /page)",
          wait_for(lambda: "Checkout usable on phones in under 2 s" in st() and "Pick which engine" in st()
                   and "15 of 20" in st(), SLOW_S), st()[:200])
    check("B: what was written since the person last looked is tinted (the handoff), the older goal is not",
          wait_for(lambda: stands.locator("[data-line='next'][data-tinted]").count() == 1, SLOW_S)
          and stands.locator("[data-line='goal'][data-tinted]").count() == 0)
    convo_l = page.locator(f"{SHEET} [data-chrome='page']")
    convo = lambda: convo_l.text_content() or ""  # noqa: E731
    check("B: the conversation is on the page (the rail layout's chat, in page chrome)",
          wait_for(lambda: "Checkout feels slow on phones" in convo() and "usable in" in convo(), SLOW_S),
          convo()[:160])
    check("B: the composer is the page's ('Write on this page…')",
          page.locator(f"{SHEET} textarea").get_attribute("placeholder") == "Write on this page…")
    check("B: no rail-layout chat header on the page",
          page.locator(f"{SHEET} header:has-text('project thread')").count() == 0)
    right = page.locator("aside.book-right")
    expect(right.locator("[data-page-controls]")).to_be_visible(timeout=5_000)
    check("B: 'This page' shows the model, never a picker", right.locator("[data-ctl='model'] .book-pill")
          .text_content() == "claude-opus-5-5")
    check("B: 'This page' has the effort pick", right.locator("[data-ctl='effort'] select").count() == 1)
    blank_l = right.locator("[data-ctl='effort'] select option[value='']")
    check(f"B: the effort pick's blank reads the level the model runs at by default "
          f"({HOST_MODEL}: {HOST_MODEL_DEFAULT_EFFORT}, inference_config)",
          HOST_MODEL_DEFAULT_EFFORT is not None
          and wait_for(lambda: blank_l.text_content() == f"Default · {HOST_MODEL_DEFAULT_EFFORT}", SLOW_S),
          repr(blank_l.text_content() if blank_l.count() else None))
    keys = page.locator(".book-keys").text_content() or ""
    check("B: the hint line names the keys and nothing else (no typeface credit)",
          "turn the page" in keys and all(w not in keys for w in ("OFL", "Newsreader", "Inter", "JetBrains", "type:")),
          keys)
    lic = sorted((APP_DIR / "assets").glob("FONT-LICENSES-*.txt"))
    lic_text = lic[0].read_text() if len(lic) == 1 else ""
    check("B: the bundle carries the fonts' licence beside them: SIL OFL 1.1 and every family's copyright",
          "SIL OPEN FONT LICENSE Version 1.1" in lic_text
          and all(f"The {f} Project Authors" in lic_text for f in ("Newsreader", "Inter", "JetBrains Mono")),
          repr([p.name for p in lic]))
    check("B: 'This page' has the context and budget panel",
          right.locator("[data-ctl='context'] button[aria-label='Context settings']").count() == 1)
    sections = ("Modules", "Plan", "Runs", "Code Approvals", "Ship this project")
    wait_for(lambda: all(s in (right.text_content() or "") for s in sections), SLOW_S)
    rt = right.text_content() or ""
    check("B: the bench's sections follow, in the rail layout's order",
          all(s in rt for s in sections)
          and rt.index("Modules") < rt.index("Plan") < rt.index("Runs") < rt.index("Code Approvals")
          < rt.index("Ship this project"), rt[:300])
    check("B: Runs is closed by default",
          right.locator("[data-state='closed']:has-text('Runs')").count() >= 1
          and "No runs yet" not in rt and "claude-code" not in rt)
    fit = publish_fits(page)
    check("B: Ship this project: the site-name placeholder fits its box, and the address hint under it is whole "
          "(the old placeholder, measured in the same box, did not fit)", fit.get("ok") and fit.get("old_cut"), fit)
    page.screenshot(path=str(SHOTS / "S-ship-placeholder.png"))
    check("B: seven tabs, the open pages newest first; the eighth, the archived and the oldest have none",
          tab_ids(page) == [21, 22, 23, 24, 25, 26, 27], repr(tab_ids(page)))
    check("B: the page in front is the selected tab",
          page.locator(".book-tabs [data-tab='21']").get_attribute("aria-selected") == "true")
    check("B: an Index tab sits under the tabs", page.locator("[data-index-tab]").count() == 1)
    check("A: a page with a Code Approval waiting lights amber",
          page.locator(".book-tabs [data-tab='23']").get_attribute("data-light") == "amber"
          and "1 Code Approval waits" in (page.locator(".book-tabs [data-tab='23']").text_content() or ""))
    check("B: the other tabs are dark",
          all(page.locator(f".book-tabs [data-tab='{i}']").get_attribute("data-light") is None
              for i in (22, 24, 25, 26, 27)))
    check("B: a tab lit when the book opens is not read out: its name carries it",
          page.locator("[data-tabs-said]").count() == 1
          and (page.locator("[data-tabs-said]").text_content() or "") == "")
    check("B: opening the page marked it seen (PUT /api/projects/21/seen)",
          wait_for(lambda: len(puts_for(21)) >= 1, SLOW_S) and len(puts_for(21)) == 1, repr(STATE["seen_puts"]))
    check("B: the open page is remembered (PUT last-open)", STATE["last_open"] == "21", STATE["last_open"])
    fonts_js = """() => performance.getEntriesByType('resource')
        .map(e => e.name).filter(n => /\\.(woff2?|ttf|otf)(\\?|$)/.test(n) || /fonts\\.(googleapis|gstatic)/.test(n))"""
    # The fonts load as the page first draws text in them, so they are read
    # once all three are in, never at a fixed time after the load.
    wait_for(lambda: len(page.evaluate(fonts_js)) >= 3
             and page.evaluate("document.fonts.check('40px \"Book Newsreader\"')"), SLOW_S)
    fonts = page.evaluate(fonts_js)
    check("B: fonts load from the bundle only: Newsreader, Inter, JetBrains Mono",
          len(fonts) >= 3 and all("/system-apps/workbench/assets/" in f for f in fonts)
          and any("newsreader" in f for f in fonts) and any("inter-" in f for f in fonts), repr(fonts))
    check("B: the title is set in the bundled Newsreader",
          "Book Newsreader" in page.locator(TITLE).evaluate("e => getComputedStyle(e).fontFamily")
          and page.evaluate("document.fonts.check('40px \"Book Newsreader\"')"))
    page.screenshot(path=str(SHOTS / "B-book.png"))
    page.locator("[data-index-tab]").click()
    expect(page.locator(f"{LIVE} [data-index]")).to_be_visible(timeout=3_000)
    expect(page.locator(f"{LIVE} [data-index-q]")).to_be_focused(timeout=3_000)
    check("B: the Index tab turns to the Index, its box ready for words", True)
    page.keyboard.press("Escape")
    expect(page.locator("[data-index]")).to_have_count(0, timeout=3_000)
    expect(page.locator(".book-ghost")).to_have_count(0, timeout=3_000)
    check("B: on a desktop the phone strip's 'This page' tab is there but out of the way (the right side shows)",
          page.locator("[data-sheet-tab]").count() == 1 and not page.locator("[data-sheet-tab]").is_visible()
          and page.locator("aside.book-right").is_visible())


def scenario_flip(page, base):
    print("F. a click turns the page (3D, about the left edge); → turns to the next older page")
    reset()
    page.clock.install()
    open_book(page, base)
    page.wait_for_timeout(400)
    clock_pause(page)
    page.locator(".book-tabs [data-tab='24']").click()
    ghost = page.locator(".book-ghost")
    mid = turn_seen(page, "F-mid-turn.png")
    check("F: mid-turn, a copy of the old page turns over the new one",
          mid is not None and mid["motion"] == "turn" and mid["turning"] and mid["title"] == "Storefront", repr(mid))
    check("F: it turns in 3D (perspective + rotateY), about its left edge",
          mid is not None and "transform" in mid["props"] and mid["transform"].startswith("matrix3d(")
          and mid["origin"].startswith("0px"), repr(mid))
    page.clock.run_for(800)  # flip.ts removes the copy at the turn's 720 ms + 40
    page.clock.resume()
    expect(page.locator(TITLE)).to_have_text("Recipe site", timeout=3_000)
    expect(ghost).to_have_count(0, timeout=3_000)
    check("F: the click turned to that page, and the copy is gone after the turn", True)
    check("F: the tab follows the page", page.locator(".book-tabs [data-tab='24']").get_attribute("aria-selected") == "true")
    check("F: opening it marked it seen", len(puts_for(24)) == 1, repr(STATE["seen_puts"]))
    page.wait_for_timeout(300)
    page.keyboard.press("ArrowRight")
    expect(page.locator(TITLE)).to_have_text("Inventory sync", timeout=3_000)
    check("F: → turns to the next older page (the composer is empty, so the arrow is the book's)", True)
    expect(ghost).to_have_count(0, timeout=3_000)
    page.keyboard.press("ArrowLeft")
    expect(page.locator(TITLE)).to_have_text("Recipe site", timeout=3_000)
    check("F: ← turns back", True)
    expect(ghost).to_have_count(0, timeout=3_000)
    box = page.locator(f"{SHEET} textarea")
    box.click()
    box.fill("draft")
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(900)
    check("F: in a composer with words in it, the arrows move the caret and the page stays",
          page.locator(TITLE).text_content() == "Recipe site")


def scenario_reduced(page, base):
    print("F. with prefers-reduced-motion the turn is a short crossfade")
    reset()
    page.clock.install()
    open_book(page, base)
    page.wait_for_timeout(400)
    clock_pause(page)
    page.locator(".book-tabs [data-tab='22']").click()
    ghost = page.locator(".book-ghost")
    seen = turn_seen(page)
    check("F: reduced motion: a crossfade, no 3D turn", seen is not None and seen["motion"] == "fade"
          and seen["turning"] and seen["props"] == ["opacity"] and seen["transform"] == "none", repr(seen))
    # Half a second on the page's clock from the click (48 ms of it run above).
    page.clock.run_for(452)
    check("F: and a short one (under half a second)",
          seen is not None and max(seen["secs"]) < 0.5 and ghost.count() == 0,
          (seen and seen["secs"], ghost.count()))
    page.clock.resume()
    expect(page.locator(TITLE)).to_have_text("Newsletter signup", timeout=2_000)


def scenario_lit(page, base):
    print("L. a reply lands on another page: dots while its turn runs, then green; dark once read")
    reset()
    open_book(page, base)
    tab = page.locator(".book-tabs [data-tab='22']")
    check("L: before: the tab is dark", tab.get_attribute("data-light") is None)
    STATE["turns"] = [{"turn_id": "t-22", "thread_key": f"project|{ENTITY}|22", "status": "running",
                       "started_at": time.time(), "elapsed_s": 1, "user_message": "build it", "seq": 3}]
    expect(tab).to_have_attribute("data-working", "true", timeout=10_000)
    check("L: the Host's running turn shows working dots on that tab", tab.locator(".book-dots").count() == 1)
    page.screenshot(path=str(SHOTS / "L-working.png"))
    # The reply lands: the turn ends and the project's row shows it after the mark.
    row(22)["last_landed_at"] = now()
    STATE["turns"][0]["status"] = "done"
    expect(tab).to_have_attribute("data-light", "green", timeout=10_000)
    check("L: the tab lights green when the reply lands (the turn ending sent the book to the list)",
          "something landed" in (tab.text_content() or ""))
    said = page.locator("[data-tabs-said]")
    check("L: and it is said, politely: a status line names the page and what landed",
          said.count() == 1 and said.get_attribute("role") == "status"
          and said.get_attribute("aria-live") == "polite"
          and (said.text_content() or "") == "Newsletter signup: something landed",
          repr(said.text_content() if said.count() else None))
    check("L: the dots are gone", tab.locator(".book-dots").count() == 0)
    page.screenshot(path=str(SHOTS / "L-lit.png"))
    # The page adopts the mark its PUT answers (book-app.tsx markSeen), so
    # once that answer is in, the tab has no reason to light when the person
    # turns away: the check after the turn back reads every light it shows.
    with page.expect_response(lambda r: r.request.method == "PUT" and "/api/projects/22/seen" in r.url,
                              timeout=SLOW_S * 1000):
        tab.click()
    expect(page.locator(TITLE)).to_have_text("Newsletter signup", timeout=5_000)
    deadline = time.time() + 5
    while time.time() < deadline and not puts_for(22):
        time.sleep(0.1)
    check("L: opening the page marks it seen", len(puts_for(22)) == 1, repr(STATE["seen_puts"]))
    check("L: the server's mark is now after the landing", row(22)["seen_at"] > row(22)["last_landed_at"])
    page.wait_for_timeout(900)
    page.locator(".book-tabs [data-tab='21']").click()
    expect(page.locator(TITLE)).to_have_text("Storefront", timeout=5_000)
    # The page in front's tab is always dark (app-web-kit/book/rules.ts
    # tabLight), so this tab is dark the moment the book turns back and a poll
    # for dark would pass on its first read. What is checked is that it stays
    # dark: every light it shows over the window, none of them green.
    lights = []
    deadline = time.time() + 1.5
    while time.time() < deadline:
        lights.append(tab.get_attribute("data-light"))
        page.wait_for_timeout(100)
    lights.append(tab.get_attribute("data-light"))
    check("L: turned back, its tab is dark", set(lights) == {None}, repr(lights))


def scenario_amber(page, base):
    print("A. something waiting that no turn announces lights its tab amber on the list's own read")
    reset()
    open_book(page, base)
    tab = page.locator(".book-tabs [data-tab='24']")
    check("A: before: the tab is dark", tab.get_attribute("data-light") is None)
    # A Code Approval opened by a build, counted in another window: no turn
    # starts or ends, so only the list's own read (LIST_POLL_MS, 10 s) sees it.
    t0 = time.time()
    row(24)["approvals_waiting"] = 1
    expect(tab).to_have_attribute("data-light", "amber", timeout=14_000)
    check("A: it lights amber within the list's 10 s", time.time() - t0 < 13, f"{time.time() - t0:.1f}s")
    check("A: and says what waits", "1 Code Approval waits" in (tab.text_content() or ""))


def scenario_seen_on_landing(page, base):
    print("S. a turn landing on the page in front marks it seen")
    reset()
    open_book(page, base)
    need("opening the page marked it seen (PUT /api/projects/21/seen), before the turn", lambda: puts_for(21))
    n0 = len(puts_for(21))
    box = page.locator(f"{SHEET} textarea")
    box.fill("Make the button say Pay now.")
    box.press("Enter")
    expect(page.locator(f"{SHEET} [data-chrome='page']")).to_contain_text("I will look at it now",
                                                                                     timeout=5_000)
    wait_for(lambda: len(puts_for(21)) > n0, SLOW_S)
    check("S: the page's own turn ending marks it seen", len(puts_for(21)) == n0 + 1, repr(STATE["seen_puts"]))
    turn = next(t for t in TURNS.values() if t.pid == 21)
    check("S: ...and that mark names the turn (its token, off the stream's session frame)",
          puts_for(21)[-1]["token"] == turn.token, repr(puts_for(21)[-1]))
    # The reply's row lands REPLY_WRITE_S after the stream, after that mark:
    # the server leaves it out of what lights the page (unseen_landed_at).
    deadline = time.time() + 15
    while time.time() < deadline and not STATE.get("reply_landed"):
        time.sleep(0.1)
    page.wait_for_timeout(500)
    check("S: the reply's row landed after the mark, and nothing on the page is unseen: no second mark needed",
          bool(STATE.get("reply_landed")) and STATE["reply_landed"] > puts_for(21)[-1]["at"]
          and landed(row(21))[1] is None, f"landed {STATE.get('reply_landed')} unseen {landed(row(21))[1]}")
    # A turn from elsewhere (another window, a connector) ends on the page in front.
    n1 = len(puts_for(21))
    STATE["turns"] = [{"turn_id": "t-21", "thread_key": f"project|{ENTITY}|21", "status": "running",
                       "started_at": time.time(), "elapsed_s": 1, "user_message": "from my phone", "seq": 2}]
    expect(page.locator(".book-tabs [data-tab='21']")).to_have_attribute("data-working", "true", timeout=10_000)
    row(21)["last_landed_at"] = now()
    STATE["turns"][0]["status"] = "done"
    deadline = time.time() + 10
    while time.time() < deadline and len(puts_for(21)) == n1:
        time.sleep(0.1)
    check("S: a turn from elsewhere landing on it marks it seen (the list shows the landing)",
          len(puts_for(21)) == n1 + 1, repr(STATE["seen_puts"]))
    check("S: the open page's tab never lit", page.locator(".book-tabs [data-tab='21']").get_attribute("data-light") is None)


def scenario_reply_written(page, base):
    print("W. a reply read and then left at once: its row lands long after the turn away, and the tab stays dark")
    reset()
    # A slow save: six seconds after the stream (a failing embedder holds it
    # ~92 s), so the row lands after the turn away and after list reads.
    STATE["reply_write_s"] = 6.0
    open_book(page, base)
    need("opening the page marked it seen (PUT /api/projects/21/seen), before the turn", lambda: puts_for(21))
    box = page.locator(f"{SHEET} textarea")
    box.fill("Make the button say Pay now.")
    box.press("Enter")
    expect(page.locator(f"{SHEET} [data-chrome='page']")).to_contain_text("I will look at it now", timeout=5_000)
    # Turn away the moment the reply is read, before its row lands.
    page.locator(".book-tabs [data-tab='22']").click()
    expect(page.locator(TITLE)).to_have_text("Newsletter signup", timeout=5_000)
    tab = page.locator(".book-tabs [data-tab='21']")
    lights = set()
    deadline = time.time() + 19   # the slow landing, then at least one list read
    while time.time() < deadline:
        lights.add(tab.get_attribute("data-light"))
        page.wait_for_timeout(200)
    mark = puts_for(21)[-1] if puts_for(21) else {}
    check("W: the reply's row landed after the turn away, seconds after the mark",
          bool(STATE.get("reply_landed")) and bool(mark) and STATE["reply_landed"] > mark["at"] + timedelta(seconds=5),
          f"landed {STATE.get('reply_landed')} mark {mark}")
    check("W: the mark the turn's end sent named that turn",
          bool(mark) and mark.get("token") == next(t for t in TURNS.values() if t.pid == 21).token, repr(mark))
    check("W: its tab never lit for the reply the person read", "green" not in lights, repr(lights))
    # Something new after that mark (an agent's report): it lights.
    row(21)["last_landed_at"] = now()
    expect(tab).to_have_attribute("data-light", "green", timeout=14_000)
    check("W: a landing after the mark is something new, and lights the tab", True)


def scenario_reply_with_build(page, base):
    print("W. a reply read and left at once, while the build its turn started begins: the tab stays dark")
    reset()
    STATE["reply_write_s"] = 6.0
    open_book(page, base)
    need("opening the page marked it seen (PUT /api/projects/21/seen), before the turn", lambda: puts_for(21))
    box = page.locator(f"{SHEET} textarea")
    box.fill("Start the build.")
    box.press("Enter")
    expect(page.locator(f"{SHEET} [data-chrome='page']")).to_contain_text("I will look at it now", timeout=5_000)
    page.locator(".book-tabs [data-tab='22']").click()
    expect(page.locator(TITLE)).to_have_text("Newsletter signup", timeout=5_000)
    # The build the turn started begins after the stream ended: the page has
    # been active since the mark (its task's started_at), though nothing new
    # has landed. A time rule takes the reply landing after that for
    # something new; the mark that names the turn does not.
    row(21)["last_activity_at"] = now()
    tab = page.locator(".book-tabs [data-tab='21']")
    lights = set()
    deadline = time.time() + 19
    while time.time() < deadline:
        lights.add(tab.get_attribute("data-light"))
        page.wait_for_timeout(200)
    dark = bool(STATE.get("reply_landed")) and "green" not in lights
    # The build finishes: that lands, and lights it.
    row(21)["last_landed_at"] = now()
    try:
        expect(tab).to_have_attribute("data-light", "green", timeout=14_000)
        lit = True
    except AssertionError:
        lit = False
    check("W: its tab stayed dark while the reply the person read landed, though the page was active after the "
          "mark, and lit when the build finished", dark and lit,
          f"landed {STATE.get('reply_landed')} lights {lights} lit after {lit}")


def scenario_report_while_written(page, base):
    print("W. a report that lands while the read reply is still being written lights the tab")
    reset()
    open_book(page, base)
    need("opening the page marked it seen (PUT /api/projects/21/seen), before the turn", lambda: puts_for(21))
    box = page.locator(f"{SHEET} textarea")
    box.fill("Start the build.")
    box.press("Enter")
    expect(page.locator(f"{SHEET} [data-chrome='page']")).to_contain_text("I will look at it now", timeout=5_000)
    # The mark the stream's end sends, naming its turn, is in before the
    # report is stamped: the check below compares the two.
    need("the turn's end marked the page seen, naming the turn (PUT /seen with its token)",
         lambda: any(p["token"] for p in puts_for(21)))
    page.locator(".book-tabs [data-tab='22']").click()
    expect(page.locator(TITLE)).to_have_text("Newsletter signup", timeout=5_000)
    # An agent's report lands on Storefront before the reply's row is written
    # (REPLY_WRITE_S). Its writer stamps it as it lands (session.py
    # deliver_agent_result: created_at now), after the mark the book sent at
    # the stream's end, so the list shows the page active since that mark.
    t = now()
    row(21)["last_landed_at"] = t
    row(21)["last_activity_at"] = t
    tab = page.locator(".book-tabs [data-tab='21']")
    expect(tab).to_have_attribute("data-light", "green", timeout=14_000)
    check("W: a report that landed after the reply was read lights the tab, while that reply was still being written",
          True)
    check("W: and the book did not mark the page seen past the report on the person's behalf",
          row(21)["seen_at"] < t, f"seen {row(21)['seen_at']} report {t}")


def scenario_keyboard(page, base):
    print("K. the tabs are a tablist: one selected, one tab stop, the keys move along them, the focus stays")
    reset()
    open_book(page, base)
    page.wait_for_timeout(600)
    tl = page.locator(".book-tabs")
    check("K: the tabs are a vertical tablist named Pages",
          tl.get_attribute("role") == "tablist" and tl.get_attribute("aria-orientation") == "vertical"
          and tl.get_attribute("aria-label") == "Pages")
    tabs = page.locator(".book-tabs [data-tab]")
    attrs = tabs.evaluate_all("els => els.map(e => [e.getAttribute('role'), e.getAttribute('aria-selected'), "
                              "e.getAttribute('tabindex'), e.getAttribute('aria-controls')])")
    check("K: each page's tab is a tab, and the page in front's alone is selected",
          [a[0] for a in attrs] == ["tab"] * 7 and [a[1] for a in attrs] == ["true"] + ["false"] * 6, repr(attrs))
    check("K: one tab stop, the selected tab", [a[2] for a in attrs] == ["0"] + ["-1"] * 6, repr(attrs))
    panel = attrs[0][3] if attrs else None
    check("K: every tab controls the page, a tabpanel named for the page in front",
          bool(panel) and all(a[3] == panel for a in attrs)
          and page.locator(f"#{panel}").get_attribute("role") == "tabpanel"
          and page.locator(f"#{panel}").get_attribute("aria-label") == "Storefront", repr(attrs))

    def focused():
        return page.evaluate("() => { const e = document.activeElement;"
                             " return e?.dataset?.tab ? 'tab:' + e.dataset.tab : e?.tagName.toLowerCase() }")

    def settle():
        # The turn is over, and the page's conversation has been read, which
        # is when the composer takes the focus if it is going to.
        expect(page.locator(".book-ghost")).to_have_count(0, timeout=3_000)
        page.wait_for_timeout(1_200)

    # "/" with Shift held: Shift+7 on a German keyboard, Shift+: on a French one.
    page.locator(".book-tabs [data-tab='21']").focus()
    page.evaluate("() => document.activeElement.dispatchEvent(new KeyboardEvent('keydown',"
                  " {key: '/', code: 'Digit7', shiftKey: true, bubbles: true, cancelable: true}))")
    page.wait_for_timeout(400)
    expect(page.locator(f"{LIVE} [data-index]")).to_have_count(1, timeout=3_000)
    check("K: '/' typed with Shift held opens the Index", True)
    page.keyboard.press("Escape")
    expect(page.locator("[data-index]")).to_have_count(0, timeout=3_000)
    expect(page.locator(".book-ghost")).to_have_count(0, timeout=3_000)

    page.locator(".book-tabs [data-tab='21']").focus()
    for key, title, tab_id, says in (
            ("ArrowDown", "Newsletter signup", "22", "↓ on a tab turns to the next tab's page"),
            ("End", "Tax papers", "27", "End turns to the last tab's page"),
            ("ArrowUp", "Garden planner", "26", "↑ turns to the tab before"),
            ("Home", "Storefront", "21", "Home turns to the first tab's page"),
            ("ArrowRight", "Newsletter signup", "22", "→ on a tab turns the page, as anywhere in the book")):
        page.keyboard.press(key)
        try:
            expect(page.locator(TITLE)).to_have_text(title, timeout=3_000)
        except AssertionError:
            check(f"K: {says}", False, f"the page is {page.locator(TITLE).text_content()!r}")
            return
        settle()
        check(f"K: {says}, and the keyboard's focus stays on that tab", focused() == f"tab:{tab_id}", focused())
    check("K: the tab stop moved with the page",
          tabs.evaluate_all("els => els.filter(e => e.tabIndex === 0).map(e => e.dataset.tab)") == ["22"])
    page.locator(".book-tabs [data-tab='24']").click()
    expect(page.locator(TITLE)).to_have_text("Recipe site", timeout=3_000)
    # The focus is waited for, then the page settles (the turn over, the
    # conversation read) and it is read again: a focus the page hands the
    # composer and then takes back still fails.
    got = wait_for(lambda: focused() == "textarea", SLOW_S)
    settle()
    check("K: a tab clicked hands the composer its focus, so the person can write at once",
          got and focused() == "textarea", focused())


# How long a check waits for something the page or the mock will do. Every
# one of these is a poll that returns the moment the thing is true; the time
# is only how long a loaded CI box gets (a full gate run beside this one
# once failed a 600 ms sleep), never a pause.
SLOW_S = 20.0


def _holds(cond) -> bool:
    # A condition that reads the newest of a list the mock has not filled yet
    # (STATE["nudges"][-1] before the page's POST is in) has simply not come
    # true yet. It is not an error: a CI run stopped scenario_kept_words
    # with an IndexError on exactly that read.
    try:
        return bool(cond())
    except LookupError:
        return False


def wait_for(cond, secs=10.0, step=0.1):
    """Poll a condition (the mock's side or the page's) until it holds or
    time runs out."""
    deadline = time.time() + secs
    while time.time() < deadline:
        if _holds(cond):
            return True
        time.sleep(step)
    return _holds(cond)


def need(what, cond, secs=SLOW_S):
    """A step's precondition: wait until it holds, or stop the scenario
    saying what never became true (the scenario's 'completed' check fails
    with this message)."""
    if not wait_for(cond, secs):
        raise AssertionError(f"never became true within {secs:.0f}s: {what}")


def turn_over(page, secs=15_000):
    """The page's turn has ended: the press is back in the box."""
    expect(page.locator(f"{SHEET} [data-press]")).to_be_visible(timeout=secs)


def turns_all_over(page, pid=21, secs=SLOW_S):
    """Every turn on the page has ended on the server, a turn it started with
    words the last one never read (requeue) among them, and the press is back.
    turn_over alone can see the press in the moment between a turn's end and
    the page following the requeued turn, and the next step would then type
    into that turn as a nudge."""
    need(f"every turn on page {pid} has ended on the server",
         # list(): the server's thread adds the requeued turn to TURNS while
         # this one reads it, and a dict changed mid-iteration raises.
         lambda: not any(t.pid == pid and not t.done for t in list(TURNS.values())), secs)
    turn_over(page, int(secs * 1000))


def scenario_count(page, base):
    print("C. the running token count: the head's total, its hover, each reply's own, the tick when a turn lands")
    reset()
    open_book(page, base)
    tally = page.locator(f"{SHEET} [data-tally]")
    expect(tally).to_have_text("214.6k tokens · $1.84", timeout=5_000)
    check("C: the head shows the page's total, every turn added up: '214.6k tokens · $1.84'", True)
    tip = page.locator(f"{SHEET} [data-tally-tip]")
    check("C: what it is made of stays hidden until asked for", not tip.is_visible())
    tally.hover()
    expect(tip).to_be_visible(timeout=2_000)
    tt = tip.text_content() or ""
    check("C: hover says what it is made of: in, from cache, out, model calls",
          "Every Host turn on this page, added up: 200.2k in (160.2k of it read from cache, 9.9k written to "
          "cache) and 14.4k out, over 41 model calls." in tt, tt)
    check("C: ...and that the dollars are an estimate, never a bill", "Dollars: an estimate" in tt, tt)
    eng = page.locator(f"{SHEET} [data-tally-engines]")
    check("C: the engines' runs launched from the page are a second line on hover",
          eng.count() == 1 and eng.is_visible()
          and (eng.text_content() or "") == "Coding engines run from this page: 2 runs, 1.2M tokens, $0.59 in compute.",
          eng.text_content() if eng.count() else None)
    page.screenshot(path=str(SHOTS / "C-count-hover.png"))
    side = page.locator("aside.book-right [data-ctl='count']")
    check("C: the right side's 'This page' carries the same count",
          side.count() == 1 and "On this page, all turns" in (side.text_content() or "")
          and side.locator("[data-tally]").text_content() == "214.6k tokens · $1.84",
          side.text_content() if side.count() else None)
    page.mouse.move(5, 5)
    expect(tip).to_be_hidden(timeout=2_000)
    tally.focus()
    expect(tip).to_be_visible(timeout=2_000)
    check("C: the keyboard reaches it too (focus shows the same)", True)
    page.locator(f"{SHEET} textarea").focus()
    adds = page.locator(f"{SHEET} [data-turn-adds]").evaluate_all(
        "els => els.map(e => [e.dataset.turnAdds, e.dataset.turnPrice || null])")
    check("C: each Host reply shows its own '+N tokens' and its price",
          adds == [["+19.4k tokens", "$0.09"], ["+21.4k tokens", "$0.18"]], adds)
    meta = page.locator(f"{SHEET} [data-turn-adds='+21.4k tokens']").text_content() or ""
    check("C: ...in the reply's meta line, with its cache split kept",
          "+21.4k tokens (21.0k in, 19.0k of it cached / 400 out) · $0.18" in meta, meta)
    first = page.locator(f"{SHEET} [data-turn-adds='+19.4k tokens']").text_content() or ""
    check("C: a reply that ran at a chosen effort says so ('thought at high')", "thought at high" in first, first)

    # A turn on this page: its own count at once, the total when it lands.
    box = page.locator(f"{SHEET} textarea")
    box.fill("Make the button say Pay now.")
    box.press("Enter")
    expect(page.locator(f"{SHEET} [data-chrome='page']")).to_contain_text("I will look at it now", timeout=5_000)
    live = page.locator(f"{SHEET} [data-turn-adds]").last
    expect(live).to_have_attribute("data-turn-adds", "+1.2k tokens", timeout=3_000)
    check("C: the reply just streamed shows its own count and price at once (its context frame)",
          live.get_attribute("data-turn-price") == "$0.0062", live.get_attribute("data-turn-price"))
    check("C: the total waits for the turn to land (its row is not written yet)",
          tally.get_attribute("data-total") == "214612" and not STATE.get("reply_landed"),
          tally.get_attribute("data-total"))
    t0 = time.time()
    expect(tally).to_have_attribute("data-total", str(214_612 + 1_240), timeout=12_000)
    bumped = tally.get_attribute("data-bump") == "true"
    check(f"C: when the turn lands, the total ticks up to the new figure ({time.time() - t0:.1f}s after the reply)",
          bool(STATE.get("reply_landed")), STATE.get("reply_landed"))
    check("C: ...marked for a moment as it ticks", bumped, tally.get_attribute("data-bump"))
    expect(tally).to_have_text("215.9k tokens · $1.85", timeout=3_000)
    check("C: ...and lands on it: '215.9k tokens · $1.85'", True)

    # Partial dollars and older turns uncounted; none counted; none at all.
    page.locator(".book-tabs [data-tab='22']").click()
    expect(page.locator(TITLE)).to_have_text("Newsletter signup", timeout=5_000)
    expect(tally).to_have_text("48.2k tokens since Oct 3 · $0.42 (partial)", timeout=5_000)
    check("C: a reply without a price marks the dollars partial; uncounted older turns read 'since Oct 3'", True)
    check("C: ...flagged partial for styling and tests", tally.get_attribute("data-partial") == "true")
    tally.hover()
    expect(tip).to_be_visible(timeout=2_000)
    tt = tip.text_content() or ""
    check("C: hover says why: the reply with no price, and the older replies never counted",
          "Partial: 1 reply has no price, so the dollars leave it out." in tt
          and "Counted since Oct 3: 30 older replies are from before the Workbench counted tokens." in tt, tt)
    page.mouse.move(5, 5)
    page.wait_for_timeout(900)
    page.locator(".book-tabs [data-tab='24']").click()
    expect(page.locator(TITLE)).to_have_text("Recipe site", timeout=5_000)
    expect(tally).to_have_text("tokens not counted", timeout=5_000)
    check("C: replies all from before the count: 'tokens not counted', no figure", tally.get_attribute("data-state") == "unknown")
    page.wait_for_timeout(900)
    page.locator(".book-tabs [data-tab='25']").click()
    expect(page.locator(TITLE)).to_have_text("Inventory sync", timeout=5_000)
    page.wait_for_timeout(800)
    check("C: a page whose totals could not be read shows no figure", tally.count() == 0)


def scenario_nudge(page, base):
    print("N. Nudge: while a turn runs, Send is Nudge and Stop stays; its own line; a reload; a 409")
    reset()
    STATE["turn_secs"] = 6
    open_book(page, base)
    box = page.locator(f"{SHEET} textarea")
    box.fill("Find out why checkout is slow on phones.")
    box.press("Enter")
    nudge = page.locator(f"{SHEET} [data-nudge-btn]")
    expect(nudge).to_be_visible(timeout=5_000)
    check("N: while the turn runs, Send becomes Nudge", (nudge.text_content() or "").strip() == "Nudge"
          and page.locator(f"{SHEET} [data-press]").count() == 0)
    check("N: ...the box says so", box.get_attribute("placeholder") == "Nudge the Host while it works…",
          box.get_attribute("placeholder"))
    hint = page.locator(f"{SHEET} [data-composer-hint]").text_content() or ""
    check("N: ...and the line under it says what Enter does now", hint.startswith("Enter nudges"), hint)
    check("N: Stop stays beside it", page.locator(f"{SHEET} [data-stop]").is_visible())
    box.fill("Use the red button.")
    box.press("Enter")
    line = page.locator(f"{SHEET} [data-nudge-line]")
    expect(line).to_have_count(1, timeout=3_000)
    check("N: the words went into the running turn (POST /api/brain/turns/{id}/nudge), not a new turn",
          [(n["text"], n["status"]) for n in STATE["nudges"]] == [("Use the red button.", 200)]
          and len(STATE["sent"]) == 1, (STATE["nudges"], len(STATE["sent"])))
    text = line.text_content() or ""
    check("N: they show as their own line: NUDGE, when, the words", text.startswith("NUDGE · ")
          and text.endswith("Use the red button."), text)
    page.wait_for_timeout(900)  # the registry's nudge frame is back on the stream by now
    check("N: the registry's own nudge frame does not draw them twice", line.count() == 1, line.count())
    check("N: Stop is still there while the turn runs", page.locator(f"{SHEET} [data-stop]").is_visible())
    page.screenshot(path=str(SHOTS / "N-nudge-line.png"))
    turn_over(page)
    page.wait_for_timeout(600)
    check("N: the turn read them (a nudge_read receipt), so they never go again as a message",
          len(STATE["sent"]) == 1 and line.count() == 1, (len(STATE["sent"]), line.count()))
    page.reload(wait_until="domcontentloaded")
    expect(page.locator(TITLE)).to_have_text("Storefront", timeout=10_000)
    expect(page.locator(f"{SHEET} [data-nudge-line]")).to_have_count(1, timeout=10_000)
    check("N: after a reload they are still their own line: the user row the turn saved when it read them",
          "Use the red button." in (page.locator(f"{SHEET} [data-nudge-line]").text_content() or ""))

    # Reloaded before the turn read them: the reattach replays the turn's
    # frames, the registry's nudge frame among them.
    STATE["read_nudges"] = False
    STATE["turn_secs"] = 9
    box = page.locator(f"{SHEET} textarea")
    box.fill("Now check the payment form.")
    box.press("Enter")
    expect(page.locator(f"{SHEET} [data-nudge-btn]")).to_be_visible(timeout=5_000)
    box.fill("Skip the images.")
    box.press("Enter")
    skip = page.locator(f"{SHEET} [data-nudge-line]:has-text('Skip the images.')")
    expect(skip).to_have_count(1, timeout=3_000)
    # The reload comes once the turn has the words, as a person's would after
    # seeing the line: before that the reload would only test a lost POST.
    need("the nudge 'Skip the images.' reached the turn (POST /nudge answered 200)",
         lambda: any(n["text"] == "Skip the images." and n["status"] == 200 for n in STATE["nudges"]))
    page.reload(wait_until="domcontentloaded")
    expect(page.locator(TITLE)).to_have_text("Storefront", timeout=10_000)
    expect(skip).to_have_count(1, timeout=10_000)
    check("N: reloaded before the turn read them, they are still their own line (the replayed nudge frame)",
          skip.count() == 1)
    STATE["read_nudges"] = True
    turn_over(page, 20_000)
    page.wait_for_timeout(1_500)
    check("N: once the turn reads them and ends, still one line", skip.count() == 1, skip.count())

    # The turn ended before it read them (409): they go as the next message.
    STATE["nudge_409"] = True
    STATE["turn_secs"] = 3
    sent0 = len(STATE["sent"])
    box = page.locator(f"{SHEET} textarea")
    box.fill("Make it faster.")
    box.press("Enter")
    expect(page.locator(f"{SHEET} [data-nudge-btn]")).to_be_visible(timeout=5_000)
    box.fill("Then deploy it.")
    box.press("Enter")
    note = page.locator(f"{SHEET} [data-chrome='page'] footer [role='status']")
    expect(note).to_contain_text("That turn ended before it read your words, so they go as your next message.",
                                 timeout=5_000)
    check("N: the turn had stopped reading: the nudge was refused (409), and the page says what happens",
          {k: v for k, v in STATE["nudges"][-1].items() if k != "client_id"}
          == {"turn": STATE["nudges"][-1]["turn"], "text": "Then deploy it.", "status": 409},
          STATE["nudges"][-1])
    ok = wait_for(lambda: len(STATE["sent"]) == sent0 + 2 and STATE["sent"][-1].get("message") == "Then deploy it.", 15)
    check("N: when the turn ended, the words went as the next message (POST /api/brain/messages)", ok,
          [b.get("message") for b in STATE["sent"][sent0:]])
    gone = page.locator(f"{SHEET} [data-nudge-line]:has-text('Then deploy it.')")
    msg = page.locator(f"{SHEET} .is-user:has-text('Then deploy it.')")
    check("N: ...and their nudge line gave way to that message",
          wait_for(lambda: gone.count() == 0 and msg.count() == 1, SLOW_S), (gone.count(), msg.count()))
    STATE["nudge_409"] = False
    turn_over(page)
    # The poll above passes the moment the message is drawn once; a second
    # copy drawn after that (the turn's own row read back) fails here.
    check("N: ...drawn once, still, when that turn is over", msg.count() == 1, msg.count())

    # Stop ends the turn for real.
    STATE["turn_secs"] = 8
    box.fill("One more.")
    box.press("Enter")
    expect(page.locator(f"{SHEET} [data-stop]")).to_be_visible(timeout=5_000)
    page.locator(f"{SHEET} [data-stop]").click()
    turn_over(page, 5_000)
    check("N: Stop stops the turn server-side (POST /api/brain/turns/{id}/cancel)", len(STATE["cancels"]) == 1,
          STATE["cancels"])


def scenario_nudge_held(page, base):
    print("W. Words typed while the Host works are the server's: never sent again, kept across a reload "
          "and a restart; in PII mode the page's copy goes")
    reset()
    open_book(page, base)
    box = page.locator(f"{SHEET} textarea")
    # The server holds the words it took (pending_words):
    # a turn that ends without reading them sends them on as the page's next
    # turn; the page keeps no copy and never sends them again — with a reload
    # in between, or without one.
    for reload_mid_turn, words in ((False, "Somewhere quiet."), (True, "And a high chair.")):
        tag = "after a reload" if reload_mid_turn else "no reload"
        turns_all_over(page)
        STATE["read_nudges"] = False
        STATE["turn_secs"] = 7
        requeued0 = len(STATE["requeued"])
        box = page.locator(f"{SHEET} textarea")
        box.fill(f"Find a table ({tag}).")
        box.press("Enter")
        expect(page.locator(f"{SHEET} [data-nudge-btn]")).to_be_visible(timeout=5_000)
        box.fill(words)
        box.press("Enter")
        check(f"W ({tag}): the route takes the words (200, the server holds them)",
              wait_for(lambda: bool(STATE["nudges"]) and STATE["nudges"][-1]["text"] == words
                       and STATE["nudges"][-1]["status"] == 200 and STATE["nudges"][-1].get("held") is True),
              STATE["nudges"][-1:])
        line = page.locator(f"{SHEET} [data-nudge-line]:has-text('{words}')")
        expect(line).to_have_count(1, timeout=3_000)
        if reload_mid_turn:
            page.wait_for_timeout(400)
            page.reload(wait_until="domcontentloaded")
            expect(page.locator(TITLE)).to_have_text("Storefront", timeout=10_000)
            check(f"W ({tag}): the words are on the page again after the reload, sent and not read yet",
                  wait_for(lambda: line.count() == 1, 10), line.count())
        check(f"W ({tag}): the turn ends without reading them, and the server starts the page's next turn "
              "with them",
              wait_for(lambda: len(STATE["requeued"]) == requeued0 + 1
                       and STATE["requeued"][-1]["text"] == words, 20), STATE["requeued"][-2:])
        mine = page.locator(f"{SHEET} .is-user:has-text('{words}')")
        check(f"W ({tag}): the page follows that turn, and its nudge line gave way to the message",
              wait_for(lambda: line.count() == 0 and mine.count() == 1, SLOW_S), (line.count(), mine.count()))
        turns_all_over(page)
        page.wait_for_timeout(1_500)
        check(f"W ({tag}): the page never sends the words again (one turn carries them)",
              not any(b.get("message") == words for b in STATE["sent"])
              and sum(1 for q in STATE["requeued"] if q["text"] == words) == 1,
              [b.get("message") for b in STATE["sent"][-4:]])
        STATE["read_nudges"] = True

    # The Host restarted with words held: the turn they went into is gone,
    # and the words are in its database. A reload shows them as sent and not
    # yet read, from the server.
    STATE["restart_held"][21] = [("Bring the menu.", datetime.now(timezone.utc), "n-left-1")]
    page.reload(wait_until="domcontentloaded")
    expect(page.locator(TITLE)).to_have_text("Storefront", timeout=10_000)
    left = page.locator(f"{SHEET} [data-nudge-line]:has-text('Bring the menu.')")
    check("W: after a restart, a reload shows the words the server still holds (GET /api/brain/pending)",
          wait_for(lambda: left.count() == 1, 10), (left.count(), STATE["pending_reads"][-3:]))
    STATE["restart_held"] = {}

    # PII mode: nothing is written, the route says so (held: false), and the
    # page keeps its copy: a turn that never reads them, the copy goes as the
    # next message, once.
    STATE["pii"] = True
    STATE["read_nudges"] = False
    STATE["turn_secs"] = 5
    requeued0 = len(STATE["requeued"])
    box = page.locator(f"{SHEET} textarea")
    box.fill("Plan the route.")
    box.press("Enter")
    expect(page.locator(f"{SHEET} [data-nudge-btn]")).to_be_visible(timeout=5_000)
    box.fill("Avoid tolls.")
    box.press("Enter")
    check("W (PII mode): the route takes the words but says it did not hold them (held: false)",
          wait_for(lambda: STATE["nudges"][-1]["text"] == "Avoid tolls."
                   and STATE["nudges"][-1].get("held") is False), STATE["nudges"][-1:])
    check("W (PII mode): the turn ended without reading them, so the page sends its copy as the next "
          "message, once",
          wait_for(lambda: sum(1 for b in STATE["sent"] if b.get("message") == "Avoid tolls.") == 1, 20)
          and len(STATE["requeued"]) == requeued0, [b.get("message") for b in STATE["sent"][-3:]])
    turn_over(page, 20_000)
    page.wait_for_timeout(1_000)
    check("W (PII mode): ...and only once",
          sum(1 for b in STATE["sent"] if b.get("message") == "Avoid tolls.") == 1)
    STATE["pii"] = False
    STATE["read_nudges"] = True


def scenario_kept_words(page, base):
    """K. PII mode keeps the page's copy in this tab's sessionStorage, so a
    reload mid-turn does not lose words the turn has not read; outside PII
    mode nothing is kept there."""
    size = "phone" if page.viewport_size["width"] < 600 else "desktop"
    print(f"K ({size}). PII mode: the page's copy of words typed mid-turn survives a reload, in sessionStorage")
    reset()
    open_book(page, base)
    kept = lambda: page.evaluate("() => JSON.parse(sessionStorage.getItem('workbench.keptWords') || '[]')")  # noqa: E731
    box = page.locator(f"{SHEET} textarea")

    def start(first, words):
        box.fill(first)
        box.press("Enter")
        expect(page.locator(f"{SHEET} [data-nudge-btn]")).to_be_visible(timeout=5_000)
        box.fill(words)
        box.press("Enter")

    def reload():
        page.reload(wait_until="domcontentloaded")
        expect(page.locator(TITLE)).to_have_text("Storefront", timeout=10_000)

    # Outside PII mode the server holds the words: nothing is kept in the tab.
    STATE["read_nudges"], STATE["turn_secs"] = False, 5
    start("Check the cart.", "Also the badge.")
    check(f"K ({size}): outside PII mode the route holds the words (held: true)",
          wait_for(lambda: STATE["nudges"][-1]["text"] == "Also the badge." and STATE["nudges"][-1].get("held") is True),
          STATE["nudges"][-1:])
    check(f"K ({size}): ...and sessionStorage is untouched",
          page.evaluate("() => sessionStorage.getItem('workbench.keptWords')") is None and kept() == [], kept())
    # The turn ends without reading the words, so the server starts the next
    # turn with them: both are over before PII mode begins.
    turns_all_over(page)
    page.wait_for_timeout(1_500)
    check(f"K ({size}): ...still untouched after the turn", page.evaluate(
        "() => sessionStorage.getItem('workbench.keptWords')") is None)

    # PII mode, the turn never reads them, a reload mid-turn: the copy is sent
    # as the next message after the turn ends, once.
    STATE["pii"], STATE["read_nudges"], STATE["turn_secs"] = True, False, 7
    start("Plan the route.", "Avoid tolls.")
    check(f"K ({size}, PII): the route takes the words but did not hold them (held: false)",
          wait_for(lambda: STATE["nudges"][-1]["text"] == "Avoid tolls." and STATE["nudges"][-1].get("held") is False),
          STATE["nudges"][-1:])
    check(f"K ({size}, PII): the page keeps its copy in this tab", wait_for(
        lambda: [w["text"] for w in kept()] == ["Avoid tolls."]), kept())
    reload()
    line = page.locator(f"{SHEET} [data-nudge-line]:has-text('Avoid tolls.')")
    check(f"K ({size}, PII): after the reload the words are on the page, not read yet",
          wait_for(lambda: line.count() == 1, 10), line.count())
    check(f"K ({size}, PII): the turn ended without reading them, so the reloaded page sends the copy as the "
          "next message", wait_for(lambda: any(b.get("message") == "Avoid tolls." for b in STATE["sent"]), 25),
          [b.get("message") for b in STATE["sent"][-3:]])
    turns_all_over(page)
    page.wait_for_timeout(1_500)
    check(f"K ({size}, PII): ...exactly once, and the tab's copy is gone",
          sum(1 for b in STATE["sent"] if b.get("message") == "Avoid tolls.") == 1
          and wait_for(lambda: kept() == [], SLOW_S),
          ([b.get("message") for b in STATE["sent"][-3:]], kept()))
    # The copy goes under the id it was nudged with: the server answers a second
    # send under it (a reload before the answer, a duplicated tab) from the turn
    # that has the words.
    nudged = next((n for n in reversed(STATE["nudges"]) if n["text"] == "Avoid tolls."), {})
    resent = next((b for b in STATE["sent"] if b.get("message") == "Avoid tolls."), {})
    check(f"K ({size}, PII): the copy is sent under the id it was nudged with (client_id), a random tag",
          bool(nudged.get("client_id")) and resent.get("client_id") == nudged.get("client_id")
          and re.fullmatch(r"u-\d+-\d{11,}", nudged["client_id"]) is not None, (nudged, resent))

    # PII mode, a reload mid-turn, and the turn reads them after it: they
    # reach the Host once, in that turn, and the page never sends them.
    STATE["read_nudges"], STATE["turn_secs"] = False, 7
    start("Plan the trip.", "Take the coast road.")
    check(f"K ({size}, PII, read): kept in this tab", wait_for(
        lambda: [w["text"] for w in kept()] == ["Take the coast road."]), kept())
    reload()
    STATE["read_nudges"] = True
    check(f"K ({size}, PII, read): the turn reads them after the reload, and the tab's copy goes",
          wait_for(lambda: kept() == [], 15), kept())
    turn_over(page, 20_000)
    page.wait_for_timeout(2_000)
    check(f"K ({size}, PII, read): ...delivered once, by the turn: the page never sends them",
          not any(b.get("message") == "Take the coast road." for b in STATE["sent"]),
          [b.get("message") for b in STATE["sent"][-3:]])
    STATE["pii"], STATE["read_nudges"] = False, True


# Longer than the registry's nudge frame carries (turn_registry._nudge:
# text[:500], code points), with an emoji before the cut, so a cut counted in
# UTF-16 units (JS slice) would not match the server's.
LONG_NUDGE = ("Rework the checkout now: " + "keep the steps short, " * 20 + "\U0001F642 "
              + "and the totals clear. " * 10).strip()
assert len(LONG_NUDGE) > 500 and LONG_NUDGE.index("\U0001F642") < 500


def scenario_nudge_edges(page, base):
    print("E. A nudge past the frame's 500 characters: one line, live and across reloads")
    reset()
    open_book(page, base)
    box = page.locator(f"{SHEET} textarea")
    long_line = page.locator(f"{SHEET} [data-nudge-line]:has-text('Rework the checkout now:')")

    # F1: a nudge over 500 characters, live: the frame (cut) is matched to the
    # line on screen (whole), so it is one line.
    STATE["read_nudges"] = False
    STATE["turn_secs"] = 12
    box.fill("Tidy the checkout page.")
    box.press("Enter")
    expect(page.locator(f"{SHEET} [data-nudge-btn]")).to_be_visible(timeout=5_000)
    box.fill(LONG_NUDGE)
    box.press("Enter")
    expect(long_line).to_have_count(1, timeout=3_000)
    ok = wait_for(lambda: any(n["text"] == LONG_NUDGE and n["status"] == 200 for n in STATE["nudges"]), 5)
    page.wait_for_timeout(900)  # the registry's (cut) nudge frame is back on the stream by now
    check("E: a nudge over 500 characters went into the turn whole", ok, [len(n["text"]) for n in STATE["nudges"]])
    check("E: ...and its cut nudge frame does not draw it twice", long_line.count() == 1, long_line.count())
    # Reloaded before the turn read it: the replayed frame draws it (cut);
    # then the turn reads it and its saved row (whole) replaces that line.
    page.reload(wait_until="domcontentloaded")
    expect(page.locator(TITLE)).to_have_text("Storefront", timeout=10_000)
    expect(long_line).to_have_count(1, timeout=10_000)
    STATE["read_nudges"] = True
    ok = wait_for(lambda: any(m.get("content") == LONG_NUDGE for m in STATE["messages"]), 5)
    turn_over(page, 20_000)
    page.wait_for_timeout(1_500)
    check("E: reloaded before the turn read it, then read: still one line (the saved row)",
          ok and long_line.count() == 1, (ok, long_line.count()))
    check("E: ...and the words went once (one message, no resend)", len(STATE["sent"]) == 1, len(STATE["sent"]))

    # Reloaded after the turn read it, while the turn still runs: the saved
    # row and the replayed frame are the same words.
    STATE["turn_secs"] = 10
    box = page.locator(f"{SHEET} textarea")
    box.fill("Now the receipt email.")
    box.press("Enter")
    expect(page.locator(f"{SHEET} [data-nudge-btn]")).to_be_visible(timeout=5_000)
    box.fill(LONG_NUDGE.replace("Rework the checkout now:", "Rework the receipt now:"))
    box.press("Enter")
    receipt = page.locator(f"{SHEET} [data-nudge-line]:has-text('Rework the receipt now:')")
    expect(receipt).to_have_count(1, timeout=3_000)
    ok = wait_for(lambda: any((m.get("content") or "").startswith("Rework the receipt now:") for m in STATE["messages"]), 5)
    page.reload(wait_until="domcontentloaded")
    expect(page.locator(TITLE)).to_have_text("Storefront", timeout=10_000)
    expect(receipt.first).to_be_visible(timeout=10_000)
    page.wait_for_timeout(1_500)  # the replay (its nudge frame among it) is in by now
    check("E: reloaded after the turn read it, while it runs: one line, not the row and the frame",
          ok and receipt.count() == 1, (ok, receipt.count()))
    turn_over(page, 20_000)
    page.wait_for_timeout(1_000)
    check("E: ...and once the turn ends, still one", receipt.count() == 1, receipt.count())


def scenario_nudge_trim(page, base):
    print("Q. Nudge typed with a leading newline: read once by the turn, never again as a message")
    reset()
    open_book(page, base)
    box = page.locator(f"{SHEET} textarea")
    # Q: words with a leading newline. The turn reads them stripped
    # (session.push_nudge), and its receipt carries them so; the copy the page
    # holds must be the same words, or words the turn read would ALSO go as
    # the next message. The line says the turn read them.
    STATE["turn_secs"] = 4
    sent0 = len(STATE["sent"])
    box = page.locator(f"{SHEET} textarea")
    box.fill("Check the totals.")
    box.press("Enter")
    expect(page.locator(f"{SHEET} [data-nudge-btn]")).to_be_visible(timeout=5_000)
    box.fill("\n  Round to cents.")
    box.press("Enter")
    cents = page.locator(f"{SHEET} [data-nudge-line]:has-text('Round to cents.')")
    expect(cents).to_have_count(1, timeout=3_000)
    read = page.locator(f"{SHEET} [data-nudge-line][data-nudge-read]:has-text('Round to cents.')")
    try:
        expect(read).to_have_count(1, timeout=4_000)
    except AssertionError:
        pass
    check("Q: the line says the turn read the words ('read by the Host')",
          read.count() == 1 and "read by the Host" in (read.first.text_content() if read.count() else ""),
          cents.first.text_content() if cents.count() else None)
    turn_over(page, 20_000)
    page.wait_for_timeout(1_500)
    check("Q: words the turn read never go again as the next message (they reach the Host once)",
          len(STATE["sent"]) == sent0 + 1, [b.get("message") for b in STATE["sent"][sent0:]])


def scenario_nudge_secret(page, base):
    print("K2. Nudge with a pasted key: one line, and the words reach the Host once, live and after a reload")
    reset()
    open_book(page, base)
    # The server cuts secret shapes from a nudge before the turn reads or saves
    # it (session.push_nudge), so the receipt and the saved row say
    # [REDACTED:...] where the page's copy has the key — in the first 48
    # characters, so no text rule can match them. The page sends an id with
    # the words and matches the frame, the receipt and the row by it.
    key = "ZZSECRET" + "48151623"  # the mock server's stand-in secret shape (MockTurn._read_nudges)
    words = f"use {key} for the deploy, then make the dock blue"
    STATE["turn_secs"] = 10
    sent0 = len(STATE["sent"])
    box = page.locator(f"{SHEET} textarea")
    box.fill("Deploy the storefront.")
    box.press("Enter")
    expect(page.locator(f"{SHEET} [data-nudge-btn]")).to_be_visible(timeout=5_000)
    box.fill(words)
    box.press("Enter")
    lines = page.locator(f"{SHEET} [data-nudge-line]:has-text('then make the dock blue')")
    expect(lines).to_have_count(1, timeout=3_000)
    ok = wait_for(lambda: any("[REDACTED:test-shape]" in (m.get("content") or "")
                              for m in STATE["messages"]), 6)
    sent_id = (STATE["nudges"][-1] if STATE["nudges"] else {}).get("client_id")
    check("K2: the page sends an id with the nudge", bool(sent_id), STATE["nudges"][-1:])
    read = page.locator(f"{SHEET} [data-nudge-line][data-nudge-read]:has-text('then make the dock blue')")
    try:
        expect(read).to_have_count(1, timeout=4_000)
    except AssertionError:
        pass
    check("K2: the turn read the words with the key cut out, and the page knows its own line by the id "
          "('read by the Host', one line)", ok and lines.count() == 1 and read.count() == 1,
          (ok, lines.count(), read.count()))
    # Live, no reload: the turn ends, and the words it read do not go again.
    turn_over(page, 20_000)
    page.wait_for_timeout(1_500)
    check("K2: live: the words reach the Host once, never again as the next message",
          len(STATE["sent"]) == sent0 + 1, [b.get("message") for b in STATE["sent"][sent0:]])
    # A second turn, the same way, then a reload while it still runs.
    sent0 = len(STATE["sent"])
    box.fill("Deploy it again.")
    box.press("Enter")
    expect(page.locator(f"{SHEET} [data-nudge-btn]")).to_be_visible(timeout=5_000)
    words2 = words.replace("the dock blue", "the dock green")
    box.fill(words2)
    box.press("Enter")
    lines2 = page.locator(f"{SHEET} [data-nudge-line]:has-text('then make the dock green')")
    expect(lines2).to_have_count(1, timeout=3_000)
    wait_for(lambda: sum("[REDACTED:test-shape]" in (m.get("content") or "")
                         for m in STATE["messages"]) >= 2, 6)
    # Reload while the turn still runs: the saved row (key cut) and the
    # replayed frame (the words as sent) are one nudge, by the id.
    page.reload(wait_until="domcontentloaded")
    expect(page.locator(TITLE)).to_have_text("Storefront", timeout=10_000)
    any_line = page.locator(f"{SHEET} [data-nudge-line]:has-text('for the deploy, then make the dock green')")
    expect(any_line.first).to_be_visible(timeout=10_000)
    page.wait_for_timeout(1_500)  # the replay (its nudge frame among it) is in by now
    check("K2: after a reload: one line, not the saved row and the replayed frame",
          any_line.count() == 1, any_line.count())
    turn_over(page, 20_000)
    page.wait_for_timeout(1_500)
    check("K2: ...reloaded: the words reach the Host once, never again as the next message",
          len(STATE["sent"]) == sent0 + 1, [b.get("message") for b in STATE["sent"][sent0:]])
    check("K2: ...and once the turn ends, still one line", any_line.count() == 1, any_line.count())


def scenario_nudge_slow_id(page, base):
    print("S. Nudge while the turn's id is slow to come: no false 'that turn ended', words into the turn once")
    reset()
    open_book(page, base)
    box = page.locator(f"{SHEET} textarea")
    # F3: the turn's id comes after 4 s (a slow start). The nudge waits for it
    # while the turn runs: no false "that turn ended", and the words go into
    # the turn once, never again as a message.
    STATE["turn_frame_delay"] = 4.0
    STATE["turn_secs"] = 3
    sent0, nudges0 = len(STATE["sent"]), len(STATE["nudges"])
    box = page.locator(f"{SHEET} textarea")
    box.fill("Check the coupon field.")
    box.press("Enter")
    expect(page.locator(f"{SHEET} [data-nudge-btn]")).to_be_visible(timeout=5_000)
    box.fill("Only on phones.")
    box.press("Enter")
    note = page.locator(f"{SHEET} [data-chrome='page'] footer [role='status']")
    page.wait_for_timeout(3_600)  # past the old 3 s wait, before the id comes
    said = note.text_content() if note.count() else ""
    check("E: a slow turn id is not taken for a turn that ended (no 'That turn ended' note)",
          "That turn ended" not in (said or ""), said)
    ok = wait_for(lambda: len(STATE["nudges"]) > nudges0, 5)
    check("E: ...the words went INTO the turn once its id came (200)",
          ok and [(n["text"], n["status"]) for n in STATE["nudges"][nudges0:]] == [("Only on phones.", 200)],
          STATE["nudges"][nudges0:])
    turn_over(page, 20_000)
    page.wait_for_timeout(1_500)
    check("E: ...and never again as a message", len(STATE["sent"]) == sent0 + 1,
          [b.get("message") for b in STATE["sent"][sent0:]])
    said = note.text_content() if note.count() else ""
    check("E: ...and the page never said the turn ended", "That turn ended" not in (said or ""), said)
    STATE["turn_frame_delay"] = 0


def scenario_ime_enter(page, base):
    print("I. Safari's IME Enter (keyCode 229, isComposing false) does not send")
    reset()
    open_book(page, base)
    box = page.locator(f"{SHEET} textarea")
    # F4: Safari ends an IME composition with an Enter whose isComposing is
    # false and keyCode 229. It must not send (nor start a hold).
    sent0 = len(STATE["sent"])
    box = page.locator(f"{SHEET} textarea")
    box.fill("日本語")
    code = box.evaluate("""el => {
        const down = new KeyboardEvent('keydown', {key: 'Enter', keyCode: 229, bubbles: true, cancelable: true});
        el.dispatchEvent(down);
        el.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', keyCode: 13, bubbles: true, cancelable: true}));
        return [down.keyCode, down.isComposing];
    }""")
    page.wait_for_timeout(1_500)
    check("E: (the test's Enter is Safari's: keyCode 229, isComposing false)", code == [229, False], code)
    check("E: Safari's IME Enter (keyCode 229) does not send", len(STATE["sent"]) == sent0,
          [b.get("message") for b in STATE["sent"][sent0:]])
    check("E: ...and the words stay in the box", box.input_value() == "日本語", box.input_value())
    box.press("Enter")
    ok = wait_for(lambda: len(STATE["sent"]) == sent0 + 1, 5)
    check("E: a plain Enter still sends", ok and STATE["sent"][-1].get("message") == "日本語",
          [b.get("message") for b in STATE["sent"][sent0:]])
    turn_over(page)


def press_box(page):
    bb = page.locator(f"{SHEET} [data-press]").bounding_box()
    return bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2


# ── the page's clock: holds and turns measured in exact milliseconds ─────────
# A hold reads performance.now on each animation frame and again when it is
# let go (press-button.tsx), and a turn removes its copy on a setTimeout
# (flip.ts). Playwright's clock (page.clock) stands in for all three, so with
# it paused a hold is exactly the milliseconds the check runs it for. On real
# timers a loaded machine overshoots: on CI a hold meant to
# land between high (0.5 s) and max (1.2 s) read MAX, a finger held for the
# same time sent max where it should send high, and the crossfade's copy, read
# 60 ms after the click, was not there to be read. The clock is installed
# before the page loads, so every timer the page sets is the clock's (one the
# real setTimeout set could not be cleared by the clock's clearTimeout), and
# it runs at the real pace until a check pauses it. Checks that pause it
# resume it when they are done.
def clock_pause(page):
    """Stop the page's clock. pause_at only jumps forward, and the clock may
    have moved on since Date.now was read, so it stops a second on. The Python
    API takes the time in seconds (Date.now is milliseconds)."""
    for _ in range(5):
        try:
            page.clock.pause_at((page.evaluate("Date.now()") + 1_000) / 1_000)
            return
        except Exception as e:  # "Cannot fast-forward to the past": the read is stale
            if "past" not in str(e):
                raise
    raise AssertionError("the page's clock would not pause")


def drawn(page):
    """Wait for the page to draw what the clock's last tick set. React renders
    on a MessageChannel task, which page.clock does not hold; two round trips
    through another channel land after it."""
    page.evaluate("""() => new Promise(done => {
        const ch = new MessageChannel(); let n = 0;
        ch.port1.onmessage = () => (++n < 2 ? ch.port2.postMessage(0) : done());
        ch.port2.postMessage(0);
    })""")


def turn_seen(page, shot=None):
    """The copy a turn lays over the book (flip.ts), read with the page's clock
    paused before the click, so it stays until the clock runs on. Its two
    animation frames are run so it is turning. Its CSS transitions run on the
    browser's own animation clock, which page.clock does not hold, so what it
    turns to is read with them run to their end, never sampled mid-flight."""
    ghost = page.locator(".book-ghost")
    try:
        expect(ghost).to_have_count(1, timeout=3_000)
    except AssertionError:
        return None
    page.clock.run_for(48)  # flip.ts: two animation frames, then .is-turning
    try:
        expect(ghost).to_have_class(re.compile(r"\bis-turning\b"), timeout=3_000)
    except AssertionError:
        pass
    if shot:
        page.screenshot(path=str(SHOTS / shot))
    return ghost.evaluate("""g => {
        const s = getComputedStyle(g);
        const seen = {motion: g.dataset.motion, turning: g.classList.contains('is-turning'),
                      props: s.transitionProperty.split(', '),
                      secs: s.transitionDuration.split(', ').map(parseFloat),
                      origin: s.transformOrigin, title: g.querySelector('h1')?.textContent};
        g.getAnimations().forEach(a => a.finish());
        seen.transform = getComputedStyle(g).transform;
        return seen;
    }""")


def scenario_hold(page, base):
    print("H. hold-to-think: a tap at the page's effort; held, high then max, the level shown; Enter held too")
    reset()
    page.clock.install()
    open_book(page, base)
    hint = page.locator(f"{SHEET} [data-composer-hint]")
    want = f"Enter sends at {HOST_MODEL_DEFAULT_EFFORT} · hold Enter or the button to think harder (high, then max)"
    try:
        # The model's levels arrive with GET /api/inference/effort-options.
        expect(hint).to_have_text(want, timeout=5_000)
    except AssertionError:
        pass
    check("H: the line under the box says what a tap and a hold do", hint.text_content() == want,
          hint.text_content())
    check("H: the press says it can be held", page.locator(f"{SHEET} [data-press]").get_attribute("aria-label")
          == "Send. Hold to think harder")
    box = page.locator(f"{SHEET} textarea")
    lvl = page.locator(f"{SHEET} .book-lvl")

    box.fill("A tap.")
    page.locator(f"{SHEET} [data-press]").click()
    ok = wait_for(lambda: len(STATE["sent"]) == 1, 5)
    check("H: a tap sends at the page's effort (no level of its own: the model's default runs)",
          ok and "effort" not in STATE["sent"][0], STATE["sent"])
    turn_over(page)

    box.fill("Think hard about this.")
    x, y = press_box(page)
    page.mouse.move(x, y)
    clock_pause(page)  # from here a hold is the milliseconds this check runs it for
    page.mouse.down()
    page.clock.run_for(300)
    drawn(page)
    check("H: under half a second it is still a tap: no level shown yet", lvl.count() == 0)
    page.clock.run_for(450)  # 750 ms in
    drawn(page)
    check("H: held past half a second, it reads HIGH, 'thinks longer'",
          lvl.count() == 1 and lvl.get_attribute("data-level") == "high"
          and (lvl.text_content() or "") == "HIGHthinks longer", lvl.text_content() if lvl.count() else None)
    check("H: ...said politely to a screen reader as it climbs",
          lvl.get_attribute("role") == "status" and lvl.get_attribute("aria-live") == "polite")
    check("H: ...and the press shows it is held (its ring)",
          page.locator(f"{SHEET} [data-press]").get_attribute("data-holding") == "true")
    page.screenshot(path=str(SHOTS / "H-hold-mid-climb.png"))
    page.clock.run_for(600)  # 1350 ms in
    drawn(page)
    check("H: held on past 1.2 s, it climbs to MAX, 'thinks hardest, costs most'",
          lvl.get_attribute("data-level") == "max" and "thinks hardest, costs most" in (lvl.text_content() or ""))
    page.mouse.up()
    page.clock.resume()
    ok = wait_for(lambda: len(STATE["sent"]) == 2, 5)
    check("H: letting go sends at the level reached (effort max on POST /api/brain/messages)",
          ok and STATE["sent"][1].get("effort") == "max" and STATE["sent"][1].get("message") == "Think hard about this.",
          STATE["sent"][-1:])
    check("H: the message says it was held: 'held · max'",
          "held · max" in (page.locator(f"{SHEET} [data-chrome='page']").text_content() or ""), None)
    check("H: the level shown goes once it is sent", lvl.count() == 0)
    turn_over(page)

    box.fill("Enter held.")
    box.focus()
    clock_pause(page)
    page.keyboard.down("Enter")
    page.clock.run_for(750)
    drawn(page)
    check("H: Enter held in the box climbs the same way (HIGH)",
          lvl.count() == 1 and lvl.get_attribute("data-level") == "high")
    page.keyboard.up("Enter")
    page.clock.resume()
    ok = wait_for(lambda: len(STATE["sent"]) == 3, 5)
    check("H: ...and letting go of Enter sends at high", ok and STATE["sent"][2].get("effort") == "high",
          STATE["sent"][-1:])
    check("H: no newline went into the box while Enter was held", box.input_value() == "", repr(box.input_value()))
    turn_over(page)

    box.fill("A quick Enter.")
    box.press("Enter")
    ok = wait_for(lambda: len(STATE["sent"]) == 4, 5)
    check("H: a quick Enter is a tap: the page's effort", ok and "effort" not in STATE["sent"][3], STATE["sent"][-1:])
    turn_over(page)

    box.fill("Let go off the button.")
    x, y = press_box(page)
    page.mouse.move(x, y)
    page.mouse.down()
    page.wait_for_timeout(700)
    page.mouse.move(x - 300, y - 200)
    page.mouse.up()
    page.wait_for_timeout(600)
    check("H: letting go off the button sends nothing, and the words stay in the box",
          len(STATE["sent"]) == 4 and box.input_value() == "Let go off the button.", (len(STATE["sent"]), box.input_value()))

    # A model without max: only high is offered.
    STATE["effort_efforts"] = ["low", "medium", "high"]
    page.reload(wait_until="domcontentloaded")
    expect(page.locator(TITLE)).to_have_text("Storefront", timeout=10_000)
    box = page.locator(f"{SHEET} textarea")
    expect(box).to_be_visible(timeout=10_000)
    try:
        expect(page.locator(f"{SHEET} [data-composer-hint]")).to_have_text(re.compile(r"think harder \(high\)$"),
                                                                         timeout=5_000)
    except AssertionError:
        pass
    check("H: a model that takes no max: the hint offers high only",
          (page.locator(f"{SHEET} [data-composer-hint]").text_content() or "").endswith("think harder (high)"),
          page.locator(f"{SHEET} [data-composer-hint]").text_content())
    box.fill("As hard as it goes.")
    x, y = press_box(page)
    page.mouse.move(x, y)
    page.mouse.down()
    seen_levels = set()
    for _ in range(9):
        page.wait_for_timeout(200)
        if lvl.count():
            seen_levels.add(lvl.get_attribute("data-level"))
    page.mouse.up()
    ok = wait_for(lambda: len(STATE["sent"]) == 5, 5)
    check("H: ...and a long hold stops at high, never a level the model does not take",
          seen_levels == {"high"} and ok and STATE["sent"][4].get("effort") == "high", (seen_levels, STATE["sent"][-1:]))


def touch(cdp, kind, x=None, y=None):
    """A real touch through the browser's input pipeline (CDP), so the press
    sees pointer events of type touch, as a phone sends them. One CDP session
    carries a touch from its start to its end."""
    pts = [{"x": x, "y": y, "radiusX": 4, "radiusY": 4, "force": 1}] if kind != "touchEnd" else []
    cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": pts})


def scenario_phone(page, base):
    print("P. the phone: a full-width page, the tabs a strip along the bottom with the Index tab, a sheet for the rest")
    reset()
    page.clock.install()
    open_book(page, base)
    page.wait_for_timeout(700)
    vw = page.viewport_size["width"]
    vh = page.viewport_size["height"]
    sheet = page.locator(SHEET).bounding_box()
    check(f"P: the page is full width ({vw} px)", sheet is not None and sheet["x"] <= 1
          and abs(sheet["width"] - vw) <= 1, sheet)
    tl = page.locator(".book-tabs")
    strip = page.locator("[data-book-edge]").bounding_box()
    check("P: the tabs are a strip along the bottom", strip is not None and abs(strip["y"] + strip["height"] - vh) <= 1
          and strip["height"] <= 70 and abs(strip["width"] - vw) <= 1, strip)
    check("P: ...a horizontal tablist", tl.get_attribute("aria-orientation") == "horizontal")
    tabs = page.locator(".book-tabs [data-tab]").evaluate_all(
        "els => els.map(e => { const r = e.getBoundingClientRect(); return [Number(e.dataset.tab), Math.round(r.y)] })")
    check("P: ...the seven tabs side by side, newest first", [t[0] for t in tabs] == [21, 22, 23, 24, 25, 26, 27]
          and len({t[1] for t in tabs}) == 1, tabs)
    ix = page.locator("[data-index-tab]").bounding_box()
    check("P: the Index tab is in the strip", ix is not None and strip is not None and ix["y"] >= strip["y"] - 1
          and ix["y"] + ix["height"] <= strip["y"] + strip["height"] + 1, (ix, strip))
    check("P: the page ends where the strip begins", sheet is not None and strip is not None
          and sheet["y"] + sheet["height"] <= strip["y"] + 1, (sheet, strip))
    check("P: the right side is not squeezed beside the page", not page.locator("aside.book-right").is_visible())
    check("P: no 'needs a wider window' note", page.locator(".book-narrow").count() == 0)
    page.screenshot(path=str(SHOTS / "P-phone-strip.png"))
    sheet_tab = page.locator("[data-sheet-tab]")
    check("P: the strip's 'This page' says it opens the right side, closed for now",
          sheet_tab.get_attribute("aria-expanded") == "false"
          and sheet_tab.get_attribute("aria-controls") == page.locator("aside.book-right").get_attribute("id"))
    sheet_tab.click()
    right = page.locator("aside.book-right")
    expect(right).to_be_visible(timeout=2_000)
    check("P: ...and open once tapped", sheet_tab.get_attribute("aria-expanded") == "true")
    rt = right.text_content() or ""
    check("P: 'This page' in the strip opens the right side over the page: the model, the effort, the bench",
          "This page" in rt and "Modules" in rt and right.locator("[data-ctl='effort'] select").count() == 1, rt[:160])
    fit = publish_fits(page)
    check("P: Ship this project on a phone: the site-name placeholder fits its box, and the address hint is whole "
          "(the old placeholder did not fit here)", fit.get("ok") and fit.get("old_cut"), fit)
    page.screenshot(path=str(SHOTS / "S-ship-placeholder-phone.png"))
    page.locator("[data-sheet-close]").click()
    expect(right).to_be_hidden(timeout=2_000)
    check("P: 'Back to the page' closes it", True)
    page.locator("[data-index-tab]").click()
    expect(page.locator(f"{LIVE} [data-index]")).to_be_visible(timeout=3_000)
    check("P: the strip's Index tab opens the Index", True)
    page.keyboard.press("Escape")
    expect(page.locator("[data-index]")).to_have_count(0, timeout=3_000)
    expect(page.locator(".book-ghost")).to_have_count(0, timeout=3_000)

    # The count, tapped (a phone has no hover).
    tally = page.locator(f"{SHEET} [data-tally]")
    expect(tally).to_have_text("214.6k tokens · $1.84", timeout=5_000)
    tally.tap()
    expect(page.locator(f"{SHEET} [data-tally-tip]")).to_be_visible(timeout=2_000)
    check("P: the count is in the page's head, and a tap shows what it is made of", True)
    page.screenshot(path=str(SHOTS / "C-count-hover-phone.png"))
    page.locator(f"{SHEET} h1").tap()

    # Hold-to-think with a finger: a long press climbs.
    box = page.locator(f"{SHEET} textarea")
    box.fill("Think it through.")
    x, y = press_box(page)
    cdp = page.context.new_cdp_session(page)
    clock_pause(page)  # the finger is down for exactly 750 ms of the page's time
    touch(cdp, "touchStart", x, y)
    page.clock.run_for(750)
    drawn(page)
    lvl = page.locator(f"{SHEET} .book-lvl")
    check("P: a touch long-press climbs: HIGH at half a second", lvl.count() == 1 and lvl.get_attribute("data-level") == "high")
    page.screenshot(path=str(SHOTS / "H-hold-mid-climb-phone.png"))
    touch(cdp, "touchEnd")
    cdp.detach()
    page.clock.resume()
    ok = wait_for(lambda: len(STATE["sent"]) == 1, 5)
    check("P: ...and lifting the finger sends at high", ok and STATE["sent"][0].get("effort") == "high", STATE["sent"])
    turn_over(page)

    # A nudge on the phone.
    STATE["turn_secs"] = 5
    box.fill("Check it on a small screen.")
    box.press("Enter")
    expect(page.locator(f"{SHEET} [data-nudge-btn]")).to_be_visible(timeout=5_000)
    box.fill("Use the bottom sheet.")
    page.locator(f"{SHEET} [data-nudge-btn]").tap()
    expect(page.locator(f"{SHEET} [data-nudge-line]")).to_have_count(1, timeout=3_000)
    check("P: Nudge works from the phone too, with its own line", STATE["nudges"][-1]["text"] == "Use the bottom sheet.")
    page.screenshot(path=str(SHOTS / "N-nudge-line-phone.png"))
    turn_over(page)

    # A lit tab in the strip.
    tab = page.locator(".book-tabs [data-tab='22']")
    row(22)["last_landed_at"] = now()
    expect(tab).to_have_attribute("data-light", "green", timeout=14_000)
    check("P: a page something landed on lights its tab in the strip", True)
    page.screenshot(path=str(SHOTS / "L-lit-phone.png"))
    tab.tap()
    expect(page.locator(TITLE)).to_have_text("Newsletter signup", timeout=5_000)
    check("P: tapping a tab in the strip turns to its page", True)


def secs_of(text):
    """'6:36' or '1:02:03' as seconds."""
    parts = [int(x) for x in (text or "").strip().split(":")]
    out = 0
    for x in parts:
        out = out * 60 + x
    return out


def scroll_convo(page, where):
    """Scroll the page's conversation (the card's scrolling ancestor)."""
    page.locator(f"{SHEET} [data-run-card]").first.evaluate("""(el, where) => {
        let s = el.parentElement;
        while (s && !(s.scrollHeight > s.clientHeight + 4 && /auto|scroll/.test(getComputedStyle(s).overflowY))) s = s.parentElement;
        if (s) s.scrollTop = where === 'top' ? 0 : s.scrollHeight;
    }""", where)
    page.wait_for_timeout(400)


def bring(page, loc):
    """Scroll an element into its own scroller's view (the conversation), as the
    book does: never the clipped sheet around it."""
    loc.evaluate("""el => {
        let s = el.parentElement;
        while (s && !(s.scrollHeight > s.clientHeight + 1 && /auto|scroll/.test(getComputedStyle(s).overflowY))) s = s.parentElement;
        if (s) s.scrollTop += el.getBoundingClientRect().top - s.getBoundingClientRect().top - 40;
    }""")
    page.wait_for_timeout(300)


def bench_open(page, base, ago=396.0, offset=-45.0):
    """The book on Storefront with the recorded compare sent `ago` seconds
    ago, the pod's clock `offset` seconds off the browser's: Claude Code has
    finished (at 372 s) and Codex is in its last steps (it ends at 409 s)."""
    reset()
    bench_start(ago, offset)
    open_book(page, base)
    # Attached: on a phone the right side is a sheet, closed until opened.
    expect(page.locator("aside.book-right [data-bench]")).to_have_count(1, timeout=10_000)
    expect(page.locator(f"{SHEET} [data-run-card][data-harness='codex']")).to_have_count(1, timeout=10_000)


def card(page, h):
    return page.locator(f"{SHEET} [data-run-card][data-session='{BENCH_SID}'][data-harness='{h}']")


def in_view(page, loc):
    """The element is inside the page's visible conversation."""
    box = loc.bounding_box()
    sheet = page.locator(SHEET).bounding_box()
    return bool(box and sheet and box["y"] < sheet["y"] + sheet["height"] - 40 and box["y"] + box["height"] > sheet["y"] + 40)


def last_clears(page, bar_sel):
    """Scroll every scroller on the right side to its end: its last item must
    then sit wholly above the bar held under it (the key hints on a desktop,
    the tab strip on a phone) and inside what scrolls it, not under either."""
    return page.evaluate("""bar => {
        const side = document.querySelector('aside.book-right');
        const b = document.querySelector(bar);
        if (!side || !b) return {ok: false, why: 'missing', side: !!side, bar: !!b};
        const all = [side, ...side.querySelectorAll('*')];
        const scrollers = all.filter(e => {
            const o = getComputedStyle(e).overflowY;
            return (o === 'auto' || o === 'scroll') && e.scrollHeight > e.clientHeight + 1;
        });
        for (const s of scrollers) s.scrollTop = s.scrollHeight;
        const pane = side.querySelector('[data-chrome="page"] > .overflow-y-auto') || side;
        const last = pane.lastElementChild;
        if (!last) return {ok: false, why: 'no last item'};
        const r = last.getBoundingClientRect();
        const barTop = b.getBoundingClientRect().top;
        const clip = Math.min(...[side, pane, ...scrollers].map(s => s.getBoundingClientRect().bottom));
        return {ok: r.bottom <= barTop + 0.5 && r.bottom <= clip + 0.5,
                last: (last.textContent || '').trim().slice(0, 48), bottom: r.bottom, barTop, clip,
                scrollers: scrollers.length};
    }""", bar_sel)


def scenario_bench(page, base):
    print("T. the bench in the book: strands per engine, the pod's clock, a strand to its card, the scoreboard,"
          " markdown tables, the per-file Changes, the grid")
    bench_open(page, base)
    bench = page.locator("aside.book-right [data-bench]")
    lanes = bench.locator("[data-strands] [data-lane]")
    check("T: the brief splits into one strand per engine, in the run's order",
          lanes.evaluate_all("els => els.map(e => e.dataset.lane)") == ["claude-code", "codex"],
          repr(lanes.evaluate_all("els => els.map(e => e.dataset.lane)")))
    check("T: each strand forks from the brief's dot", bench.locator(".book-brief-dot").count() == 1
          and bench.locator(".book-strand-fork").count() == 2)
    expect(bench.locator("[data-lane='claude-code']")).to_have_attribute("data-state", "done", timeout=5_000)
    expect(bench.locator("[data-lane='codex']")).to_have_attribute("data-state", "running", timeout=5_000)
    check("T: mid-run, the finished engine ends in its finish mark and the running one in a live head at now",
          bench.locator("[data-lane='claude-code'] [data-end='done']").count() == 1
          and bench.locator("[data-lane='codex']").get_attribute("data-state") == "running"
          and bench.locator("[data-lane='codex'] [data-end='live']").count() == 1
          and bench.locator(".book-strand-now").count() == 1)
    marks = bench.locator("[data-lane='claude-code'] .book-strand-mark").evaluate_all(
        "els => els.map(e => e.dataset.kind)")
    check("T: a strand carries its engine's steps: commands and reads, files written, a call that failed",
          marks.count("edit") == 3 and marks.count("error") == 1 and marks.count("tool") == 5, repr(marks))
    check("T: its thinking shows as soft bands (the quiet blocks under 3 s do not)",
          bench.locator("[data-lane='claude-code'] .book-strand-think").count() == 3)
    check("T: Codex's wait for memory is its own band before its strand",
          bench.locator("[data-lane='codex'] .book-strand-queued").count() == 1)
    check("T: the run header names the compare", (bench.locator("[data-run-title]").text_content() or "")
          == "Compare · 2 engines")
    # The pod's clock is 45 s behind the browser's: read on the browser's
    # clock, every live time would be 45 s too long. The frames replayed on
    # opening say nothing about the clocks (their age is unknown); the first
    # frame written live sets it (Codex's next, at 398 s).
    clk = bench.locator("[data-session-clock]")
    ok = wait_for(lambda: abs(secs_of(clk.text_content()) - bench_elapsed()) <= 2, 10)
    check("T: the session clock reads the pod's time: the run's real age, not 45 s more",
          ok, f"shown {clk.text_content()}, run is {bench_elapsed():.1f}s old")
    codex_started = next(t for t, k, p in BENCH["engines"]["codex"]["frames"]
                         if not (k == "status" and p.get("state") in ("waiting", "thread.started", "turn.started")))
    cclk = card(page, "codex").locator("header .tabular-nums").last
    ok = wait_for(lambda: abs(secs_of(cclk.text_content()) - (bench_elapsed() - codex_started)) <= 2, 10)
    check("T: the running card's own clock is on the pod's time too (the Bench's clock correction)",
          ok, f"card {cclk.text_content()}, codex has run {bench_elapsed() - codex_started:.1f}s")
    check("T: screen readers get the strands as a plain list, the drawing hidden",
          bench.locator("[data-strands]").get_attribute("aria-hidden") == "true"
          and bench.locator("[data-strands-list] li").count() == 2
          and (bench.locator("[data-strands-list] li").first.text_content() or "").startswith("Claude Code: done"))
    page.screenshot(path=str(SHOTS / "S-strands-mid-run.png"))
    got = last_clears(page, ".book-keys")
    check("T: with the bench in it, the right side scrolls its last item clear of the key hints under it",
          got.get("ok"), repr(got))

    # A strand turns the page to its engine's card: from the top of the
    # conversation, where neither card is in view.
    scroll_convo(page, "top")
    check("T: (before: Codex's card is out of view)", not in_view(page, card(page, "codex")))
    title_y = page.locator(TITLE).bounding_box()["y"]
    # force: the live head breathes, so the lane is never still for the click's
    # stability wait; the click still lands on the strand at that point.
    bench.locator("[data-lane='codex'] .book-strand-hit").click(force=True)
    expect(card(page, "codex")).to_have_attribute("data-flash", "", timeout=2_000)
    check("T: clicking Codex's strand scrolls its card into the page's view, and it glows once",
          wait_for(lambda: in_view(page, card(page, "codex")), SLOW_S))
    # The card is in view part way through the scroll, so the head is read
    # over the time the scroll had before (900 ms), every read where it was.
    head_ys = []
    deadline = time.time() + 0.9
    while time.time() < deadline:
        head_ys.append(page.locator(TITLE).bounding_box()["y"])
        page.wait_for_timeout(100)
    head_ys.append(page.locator(TITLE).bounding_box()["y"])
    check("T: only the conversation scrolls: the page's head stays where it was",
          all(abs(y - title_y) < 1 for y in head_ys), (title_y, head_ys))
    scroll_convo(page, "top")
    bench.locator("[data-strand='claude-code']").click()
    expect(card(page, "claude-code")).to_have_attribute("data-flash", "", timeout=2_000)
    check("T: the name beside a strand does the same, from the keyboard too (a button)",
          wait_for(lambda: in_view(page, card(page, "claude-code")), SLOW_S)
          and bench.locator("[data-strand='claude-code']").evaluate("e => e.tagName") == "BUTTON")
    check("T: the card wears its strand's colour",
          card(page, "codex").evaluate("e => getComputedStyle(e).getPropertyValue('--engine-color').trim()")
          == "#3a67d0")

    # Markdown tables in an engine's own words.
    cc = card(page, "claude-code")
    cc.get_by_role("button", name="Show activity").click()
    table = cc.locator("[data-md-table]")
    expect(table).to_have_count(1, timeout=3_000)
    check("T: a markdown table in the engine's prose is a table: its header row, two rows, no separator row",
          table.locator("thead th").evaluate_all("els => els.map(e => e.textContent)") == ["Profile", "Usable", "Payment form"]
          and table.locator("tbody tr").count() == 2
          and "---" not in (table.text_content() or ""))
    bring(page, table)
    page.screenshot(path=str(SHOTS / "S-markdown-table.png"))

    # Codex finishes: the scoreboard says who was first.
    expect(bench.locator("[data-lane='codex']")).to_have_attribute("data-state", "done", timeout=25_000)
    check("T: once every engine is done the now line goes and the clock stops",
          bench.locator(".book-strand-now").count() == 0
          and bench.locator(".book-live").text_content() == "Finished")
    rows = bench.locator("[data-scoreboard] [data-score-row]")
    expect(rows.nth(1).locator(".book-num").last).not_to_have_text("—", timeout=8_000)
    ids = rows.evaluate_all("els => els.map(e => e.dataset.scoreRow)")
    cells = rows.evaluate_all("els => els.map(e => [...e.querySelectorAll('td')].map(td => td.textContent.trim()))")
    check("T: the scoreboard lists the engines in the run's order", ids == ["claude-code", "codex"], repr(ids))
    check("T: Claude Code finished first (its last frame came 37 s before Codex's, though each ran 6:10)",
          "first to finish" in cells[0][0] and "first to finish" not in cells[1][0], repr(cells))
    want = [f"+{a} −{d}" for a, d in (patch_counts("claude-code"), patch_counts("codex"))]
    check("T: each row reads its run time and the lines its patch changed",
          [c[2] for c in cells] == ["6:10", "6:10"] and [c[3] for c in cells] == want, repr(cells))
    check("T: and what each engine did",
          cells[0][0].endswith("5 commands · 1 read · 2 written · 2 messages · 1 failed")
          and cells[1][0].endswith("6 commands · 2 written · 2 messages"), repr([c[0] for c in cells]))
    secs = secs_of(bench.locator("[data-session-clock]").text_content())
    check("T: the session clock stopped at the run's end", abs(secs - 409.4) <= 1.5, secs)
    check("T: each engine's stream was read once, by its card and its strand together",
          STATE["bench"]["opens"] == {"claude-code": 1, "codex": 1}, repr(STATE["bench"]["opens"]))
    page.screenshot(path=str(SHOTS / "S-scoreboard-finished.png"))
    rows.nth(1).click()
    expect(card(page, "codex")).to_have_attribute("data-flash", "", timeout=2_000)
    check("T: a scoreboard row turns to its card as well", True)

    # The per-file list in Changes.
    cc.locator("[data-open-version]").click()
    view = page.get_by_test_id("version-view")
    expect(view).to_be_visible(timeout=3_000)
    view.get_by_role("button", name="Changes").click()
    files = view.locator("[data-diff-by-file] [data-diff-file]")
    expect(files).to_have_count(4, timeout=5_000)
    check("T: Changes lists the run's files, each with its own +/− counts",
          files.evaluate_all("els => els.map(e => e.dataset.diffFile)")
          == ["checkout.js", "checkout.test.js", "pay-later.js", "pay.css"]
          and (files.nth(0).text_content() or "").endswith("+8−2"), repr(files.nth(0).text_content()))
    files.nth(1).click()
    part_l, body_l = view.locator("[data-diff-part='checkout.test.js']"), view.locator(".book-diff-body")

    def scrolled_to_part():
        p, b = part_l.bounding_box(), body_l.bounding_box()
        return p is not None and b is not None and abs(p["y"] - b["y"]) < 40

    wait_for(scrolled_to_part, SLOW_S)
    part = part_l.bounding_box()
    body = body_l.bounding_box()
    check("T: a file in the list scrolls to its part of the diff",
          part is not None and body is not None and abs(part["y"] - body["y"]) < 40, (part, body))
    page.screenshot(path=str(SHOTS / "S-changes-by-file.png"))
    page.keyboard.press("Escape")
    view.get_by_role("button", name="Close").click()
    expect(view).to_have_count(0, timeout=3_000)

    # The engine grid: the compare laid side by side across the window.
    page.locator(f"{SHEET} [data-side-by-side]").click()
    wide = page.locator(f"{SHEET} .book-compare[data-wide]")
    expect(wide).to_be_visible(timeout=2_000)
    a, b = card(page, "claude-code").bounding_box(), card(page, "codex").bounding_box()
    check("T: 'Side by side' lays the engines' cards next to each other (the Bench's grid)",
          a is not None and b is not None and abs(a["y"] - b["y"]) < 2 and b["x"] > a["x"] + a["width"] - 1,
          (a, b))
    page.screenshot(path=str(SHOTS / "S-side-by-side.png"))
    page.keyboard.press("Escape")
    expect(wide).to_have_count(0, timeout=2_000)
    check("T: Escape puts them back on the page", True)


def scenario_bench_reduced(page, base):
    print("T. less motion: the live strand does not breathe, the badge does not blink, the card does not glow")
    bench_open(page, base, ago=380.0)
    bench = page.locator("aside.book-right [data-bench]")
    halo = bench.locator("[data-lane='codex'] .book-strand-halo")
    expect(halo).to_have_count(1, timeout=5_000)
    check("T: reduced motion: the live head is still", halo.evaluate("e => getComputedStyle(e).animationName") == "none")
    check("T: reduced motion: the live badge's dot is still",
          bench.locator(".book-live").evaluate("e => getComputedStyle(e, '::before').animationName") == "none")
    bench.locator("[data-strand='codex']").click()
    expect(card(page, "codex")).to_have_attribute("data-flash", "", timeout=2_000)
    check("T: reduced motion: the card turned to shows a still ring, no animation",
          card(page, "codex").evaluate("e => getComputedStyle(e).animationName") == "none"
          and card(page, "codex").evaluate("e => getComputedStyle(e).boxShadow") != "none")


def scenario_bench_phone(page, base):
    print("T. the phone: the strands in the right side's sheet; a strand closes the sheet and shows its card")
    bench_open(page, base)
    page.locator("[data-sheet-tab]").click()
    bench = page.locator("aside.book-right [data-bench]")
    expect(bench).to_be_visible(timeout=3_000)
    check("T: the strands are in the sheet, one per engine", bench.locator("[data-strands] [data-lane]").count() == 2)
    page.screenshot(path=str(SHOTS / "S-strands-mid-run-phone.png"))
    got = last_clears(page, ".book-edge")
    check("T: the phone's sheet scrolls its last item clear of the tab strip", got.get("ok"), repr(got))
    bench.locator("[data-strand='codex']").tap()
    expect(page.locator("aside.book-right")).to_be_hidden(timeout=2_000)
    expect(card(page, "codex")).to_have_attribute("data-flash", "", timeout=2_000)
    check("T: tapping a strand closes the sheet and brings its card into view",
          wait_for(lambda: in_view(page, card(page, "codex")), SLOW_S))
    cc = card(page, "claude-code")
    cc.get_by_role("button", name="Show activity").tap()
    table = cc.locator("[data-md-table]")
    expect(table).to_have_count(1, timeout=3_000)
    bring(page, table)
    page.screenshot(path=str(SHOTS / "S-markdown-table-phone.png"))
    check("T: the table fits the phone's page (it scrolls on its own if it must)",
          table.evaluate("e => e.parentElement.scrollWidth <= e.parentElement.clientWidth + 1 || "
                         "getComputedStyle(e.parentElement).overflowX === 'auto'"))
    expect(page.locator("[data-sheet-tab]")).to_be_visible(timeout=2_000)
    deadline = time.time() + 25
    while time.time() < deadline and bench_state("codex")["state"] != "done":
        time.sleep(0.3)
    page.wait_for_timeout(1500)
    page.locator("[data-sheet-tab]").click()
    expect(bench.locator("[data-lane='codex']")).to_have_attribute("data-state", "done", timeout=10_000)
    expect(bench.locator("[data-scoreboard] [data-score-row]").nth(1).locator(".book-num").last).not_to_have_text(
        "—", timeout=8_000)
    check("T: the scoreboard reads right on the phone too",
          "first to finish" in (bench.locator("[data-score-row='claude-code']").text_content() or ""))
    page.screenshot(path=str(SHOTS / "S-scoreboard-finished-phone.png"))
    got = last_clears(page, ".book-edge")
    check("T: ...and with the scoreboard in it, still clear of the tab strip", got.get("ok"), repr(got))
    page.locator("[data-sheet-close]").click()
    cc.locator("[data-open-version]").tap()
    view = page.get_by_test_id("version-view")
    expect(view).to_be_visible(timeout=3_000)
    view.get_by_role("button", name="Changes").tap()
    expect(view.locator("[data-diff-by-file] [data-diff-file]")).to_have_count(4, timeout=5_000)
    check("T: the per-file Changes list on the phone: the files above their diff",
          view.locator(".book-diff-files").bounding_box()["y"] < view.locator(".book-diff-body").bounding_box()["y"])
    page.screenshot(path=str(SHOTS / "S-changes-by-file-phone.png"))


# ── the Index and its chapters ───────────────────────────────────────────────
# These scenarios wait on what the page and the mock show, never on time. The
# page's clock is Playwright's, installed before the page loads (index_open):
# it runs at the real pace until a check about a riffle or a turn pauses it
# (clock_pause, above), and then the sheets a riffle or a turn lays over the
# book stay there until the check runs the clock on; a turn's copy is read
# with turn_seen. The mock's pick answers when the check lets it
# (STATE["pick_hold"]).
INDEX = f"{LIVE} [data-index]"
IXQ = f"{LIVE} [data-index-q]"
# Longer, on the page's clock, than any riffle (src/lib/book-index.ts
# riffleSchedule: the page in front and seven pages end at 1,640 ms) and any
# turn (lib/book.ts TURN_MS 720, removed 40 ms after).
RIFFLE_DONE_MS = 2_500


def over_the_book(page):
    """The sheets a riffle lays over the book (src/book/riffle.ts): how many,
    the page in front's copy among them, and how many are turning. A turn's
    or a crossfade's single copy is read with turn_seen."""
    return page.evaluate("""() => ({
        leaves: document.querySelectorAll('.book-leaf').length,
        cover: document.querySelectorAll('[data-riffle-cover]').length,
        turning: document.querySelectorAll('.book-leaf.is-turning').length})""")


def riffle_frame(page, shot=None):
    """A paused riffle run 400 ms on (src/book/riffle.ts: a sheet starts to
    turn every 120 ms from 60 ms, so three are turning). Their CSS animations
    run on the browser's own clock, which page.clock does not hold; for the
    screenshot they are held where they would be 400 ms in. Returns what lies
    over the book then."""
    page.clock.run_for(400)
    drawn(page)
    seen = over_the_book(page)
    page.evaluate("""() => {
        const turning = [...document.querySelectorAll('.book-leaf.is-turning')];
        for (const a of document.getAnimations()) {
            const i = turning.indexOf(a.effect && a.effect.target);
            if (i < 0) continue;
            a.pause();
            a.currentTime = Math.max(0, 340 - 120 * i);
        }
    }""")
    if shot:
        page.screenshot(path=str(SHOTS / shot))
    return seen


def index_done(page):
    """Run the paused clock past any riffle or turn, let it go, and wait
    until nothing lies over the book."""
    page.clock.run_for(RIFFLE_DONE_MS)
    page.clock.resume()
    index_settled(page)


def index_settled(page):
    expect(page.locator(".book-leaf")).to_have_count(0, timeout=5_000)
    expect(page.locator(".book-ghost")).to_have_count(0, timeout=5_000)


def index_open(page, base, *, via="tab"):
    """The book on Storefront with the Index fixture, the page's clock
    installed, and the Index open: its box has the keyboard and the recent
    pages are listed."""
    reset()
    index_fixture()
    page.clock.install()
    open_book(page, base)
    if via == "tab":
        page.locator("[data-index-tab]").click()
    else:
        page.keyboard.press("/")
    expect(page.locator(INDEX)).to_be_visible(timeout=3_000)
    expect(page.locator(IXQ)).to_be_focused(timeout=3_000)
    index_settled(page)
    expect(page.locator(f"{INDEX} [data-index-result]").first).to_be_visible(timeout=5_000)


def reopen_index(page):
    page.locator("[data-index-tab]").click()
    expect(page.locator(INDEX)).to_be_visible(timeout=3_000)
    index_settled(page)


def results(page):
    return [int(x) for x in page.locator(f"{INDEX} [data-index-result]").evaluate_all(
        "els => els.map(e => e.dataset.indexResult)")]


def results_become(page, ok, secs=5.0):
    """The results once `ok(results)` holds (the answer for what is typed
    now has been drawn; an earlier keystroke's may be on screen first), or as
    they stand when `secs` run out."""
    deadline = time.time() + secs
    got = results(page)
    while not ok(got) and time.time() < deadline:
        page.wait_for_timeout(50)
        got = results(page)
    return got


def type_slowly(page, text):
    page.locator(IXQ).press_sequentially(text, delay=40)


def glided(page):
    """Wait until the results have glided to their places (index-page.tsx
    animates a re-sort with the Web Animations API, on the browser's own
    clock): a screenshot then shows the order, not the move."""
    page.evaluate("""() => Promise.all(document.getAnimations()
        .filter(a => a.effect && a.effect.target && a.effect.target.matches('[data-index-result]'))
        .map(a => a.finished.catch(() => null)))""")


def ixtab_fits(page) -> dict:
    """The Index tab's word and subtitle against the tab's own box: every
    line of what it shows inside it (a phone shows no subtitle)."""
    return page.locator("[data-index-tab]").evaluate("""b => {
        const r = b.getBoundingClientRect();
        const parts = [...b.querySelectorAll('b, span')].map(e => e.getBoundingClientRect())
            .filter(x => x.width > 0 && x.height > 0);
        return {inside: parts.every(x => x.top >= r.top - 0.5 && x.bottom <= r.bottom + 0.5
                                       && x.left >= r.left - 0.5 && x.right <= r.right + 0.5)
                        && b.scrollHeight <= b.clientHeight,
                box_bottom: r.bottom, bottoms: parts.map(x => x.bottom),
                scroll: b.scrollHeight, client: b.clientHeight};
    }""")


def scenario_index_typing(page, base):
    print("I. the Index: typing re-sorts every page, open and archived, with no model call")
    index_open(page, base)
    fit = ixtab_fits(page)
    check("I: the Index tab's word and subtitle sit inside the tab", fit["inside"], fit)
    check("I: the Index is a page of the book: the Index tab is pressed, no page's tab is selected",
          page.locator("[data-index-tab]").get_attribute("aria-pressed") == "true"
          and page.locator(".book-tabs [aria-selected='true']").count() == 0)
    check("I: its box has the keyboard at once", page.evaluate("document.activeElement?.hasAttribute('data-index-q')"))
    check("I: Enter sends the message by default ('send' mode)",
          page.locator(f"{INDEX} [data-mode-btn='send']").get_attribute("aria-pressed") == "true")
    check("I: before anything is typed: the open pages, most recent first",
          results(page)[:4] == [21, 22, 23, 24] and "Most recent" in (page.locator(INDEX).text_content() or ""),
          results(page))
    check("I: the page underneath is untouched (still there, kept from the keyboard)",
          page.locator(PAGE_TITLE).text_content() == "Storefront"
          and page.locator(f"{LIVE} > .book-inner").get_attribute("inert") is not None)
    type_slowly(page, "phone")
    first = results_become(page, lambda r: r[:3] == [21, 31, 34])
    check("I: 'phone': the pages whose words hold it, open and archived, the newest first",
          first[:3] == [21, 31, 34], first)
    glided(page)
    page.screenshot(path=str(SHOTS / "I-index-typing.png"))
    type_slowly(page, " photos")
    second = results_become(page, lambda r: r[:1] == [34])
    glided(page)
    page.screenshot(path=str(SHOTS / "I-index-typing-more.png"))
    check("I: '... photos' re-sorts them as it is typed: the archived page named for it moves to the top",
          second[:1] == [34] and set(first[:3]) <= set(second), (first, second))
    top = page.locator(f"{INDEX} [data-index-result='34']")
    check("I: an archived page says so, with its chapter", "Storefront work chapter" in (top.text_content() or "")
          and "archived" in (top.text_content() or "").lower() and top.get_attribute("data-archived") == "true")
    check("I: the excerpt marks the words that matched", page.locator(f"{INDEX} [data-index-result='34'] mark")
          .count() >= 1)
    check("I: no relevance number is shown", not re.search(r"\b\d\.\d{2}\b|score|relevance",
                                                            page.locator(f"{INDEX} [data-index-results]").text_content() or "", re.I))
    reads = STATE["index_reads"]
    check("I: the search was asked as the person typed (POST /api/projects/index), and never the Host",
          any(r.startswith("phone ph") for r in reads) and not STATE["index_sends"], (reads[-4:], STATE["index_sends"]))
    # Escape from anywhere in the Index: out of it, back to the page.
    page.locator(f"{INDEX} [data-mode-btn='send']").focus()
    page.keyboard.press("Escape")
    expect(page.locator("[data-index]")).to_have_count(0, timeout=3_000)
    check("I: Escape (the keyboard on one of its buttons) turns back to the page in front",
          page.locator(PAGE_TITLE).text_content() == "Storefront")
    index_settled(page)
    page.locator(".book-tabs [data-tab='22']").focus()
    page.keyboard.press("/")
    expect(page.locator(INDEX)).to_be_visible(timeout=3_000)
    expect(page.locator(IXQ)).to_be_focused(timeout=3_000)
    index_settled(page)
    page.locator(f"{INDEX} h1.book-title").click()
    page.keyboard.press("Escape")
    expect(page.locator("[data-index]")).to_have_count(0, timeout=3_000)
    check("I: '/' opens it again, and Escape closes it with the keyboard nowhere in particular", True)


def scenario_index_send(page, base):
    print("I. Enter sends the message to the page the Host picks; the book lands there")
    index_open(page, base)
    STATE.update({"turn_secs": 4, "pick_hold": threading.Event(),
                  "pick": {"page": 22, "reason": "It is about the newsletter's confirmation email."}})
    words = "Is the confirmation email ready to read?"
    page.locator(IXQ).fill(words)
    page.locator(IXQ).press("Enter")
    line = page.locator(f"{INDEX} [data-index-line]")
    expect(line).to_have_text("Picking the page…", timeout=3_000)
    check("I: while the Host picks, the Index says so and holds the words",
          wait_for(lambda: len(STATE["index_sends"]) == 1, 5) and page.locator(IXQ).get_attribute("readonly") is not None)
    page.locator(IXQ).press("Enter")
    page.locator(IXQ).press("Enter")
    # Closed and opened again while the Host picks: still the one message.
    page.keyboard.press("Escape")
    expect(page.locator("[data-index]")).to_have_count(0, timeout=3_000)
    index_settled(page)
    reopen_index(page)
    expect(line).to_have_text("Picking the page…", timeout=3_000)
    check("I: closed and opened again mid-pick, the Index still says the Host is picking, the words in its box",
          page.locator(IXQ).input_value() == words and page.locator(IXQ).get_attribute("readonly") is not None,
          page.locator(IXQ).input_value())
    page.locator(IXQ).press("Enter")
    check("I: no Enter while the Host picks sends anything more, from the Index opened again too: one send",
          len(STATE["index_sends"]) == 1, STATE["index_sends"])
    sent = STATE["index_sends"][0] if STATE["index_sends"] else {}
    check("I: the send is the words as typed, the page in front, and an id of its own",
          sent.get("text") == words and sent.get("current_project_id") == 21
          and re.fullmatch(r"[0-9a-f]{32}", str(sent.get("request_id") or "")) is not None, sent)
    clock_pause(page)
    STATE["pick_hold"].set()
    expect(page.locator(".book-ghost")).to_have_count(1, timeout=6_000)
    of_index = page.locator(".book-ghost section[data-index]").count() == 1
    mid = turn_seen(page)
    expect(page.locator(PAGE_TITLE)).to_have_text("Newsletter signup", timeout=3_000)
    check("I: the Host's pick lands with a page turn: the Index turns away over the page it picked",
          mid is not None and mid["motion"] == "turn" and mid["turning"] and of_index
          and page.locator(".book-leaf").count() == 0, (mid, of_index))
    index_done(page)
    expect(page.locator("[data-index]")).to_have_count(0, timeout=3_000)
    convo = page.locator(f"{LIVE} [data-chrome='page']")
    expect(convo).to_contain_text("Index message, picked this page: It is about the newsletter's confirmation email.",
                                  timeout=5_000)
    check("I: the page the Host picked is in front, with the Host's reason written on it", True)
    expect(convo).to_contain_text(words, timeout=5_000)
    page.screenshot(path=str(SHOTS / "I-send-landed.png"))
    expect(convo).to_contain_text(REPLY_TAIL, timeout=10_000)
    check("I: the turn the send started streams on the page", True)
    check("I: the page goes to the top of the book", tab_ids(page)[:1] == [22], tab_ids(page))
    check("I: the page's own box was not used (nothing went to /api/brain/messages)", not STATE["sent"],
          STATE["sent"])
    turn_over(page)

    # The Host picks the page already in front: its note and the turn show
    # there at once (the page reads its conversation again).
    reopen_index(page)
    STATE.update({"turn_secs": 2, "pick_hold": None, "pick": {"page": 22, "reason": "The subject line is part of it."}})
    page.locator(IXQ).fill("And the subject line?")
    page.locator(IXQ).press("Enter")
    expect(page.locator("[data-index]")).to_have_count(0, timeout=6_000)
    index_settled(page)
    expect(convo).to_contain_text("Index message, picked this page: The subject line is part of it.", timeout=5_000)
    expect(convo).to_contain_text("And the subject line?", timeout=5_000)
    check("I: the page in front picked: the Host's note and the turn are on it at once, without opening it again",
          STATE["index_sends"][-1].get("current_project_id") == 22, STATE["index_sends"][-1:])
    turn_over(page)

    # An archived pick: brought back, and it riffles up.
    reopen_index(page)
    STATE.update({"turn_secs": 2, "pick": {"page": 32, "reason": "Rates for Canada were decided there."}})
    page.locator(IXQ).fill("What do we charge to ship to Canada?")
    clock_pause(page)
    page.locator(IXQ).press("Enter")
    expect(page.locator("[data-riffle-cover]")).to_have_count(1, timeout=6_000)
    seen = over_the_book(page)
    check("I: an archived pick riffles up: the page in front and its chapter's pages lie over it, to turn away",
          seen["leaves"] >= 4 and seen["cover"] == 1 and seen["turning"] == 0, seen)
    expect(page.locator(PAGE_TITLE)).to_have_text("Shipping rates", timeout=3_000)
    mid = riffle_frame(page, "I-riffle-frame.png")
    check("I: ...and they turn away one after another, the first already turning, the last still waiting",
          0 < mid["turning"] < mid["leaves"], mid)
    index_done(page)
    check("I: it was brought back (restored) and is in the book again, at the top",
          row(32)["status"] == "active" and tab_ids(page)[:1] == [32]
          and page.locator("[data-archived-note]").count() == 0, (row(32)["status"], tab_ids(page)))
    turn_over(page)

    # A pick that cannot come back says why, and nothing runs.
    reopen_index(page)
    STATE.update({"pick": {"page": 36, "reason": "The domain moved there."}})
    n_msgs, n_turns = len(STATE["messages"]), len(TURNS)
    page.locator(IXQ).fill("Did the old domain renew?")
    page.locator(IXQ).press("Enter")
    expect(line).to_contain_text("could not come back from the archive", timeout=5_000)
    check("I: a refused restore says why, in the Index, and the words stay",
          REFUSAL in (line.text_content() or "") and page.locator(IXQ).input_value() == "Did the old domain renew?",
          line.text_content())
    check("I: ...nothing was written and no turn started",
          not [m for m in STATE["messages"][n_msgs:] if m["project_id"] == 36]
          and not [t for t in list(TURNS.values())[n_turns:] if t.pid == 36] and row(36)["status"] == "archived",
          [m for m in STATE["messages"][n_msgs:] if m["project_id"] == 36])

    # The Host could not pick: the person does, and the words wait in that page's box.
    STATE.update({"pick": {"ask": [22, 33]}})
    page.locator(IXQ).fill("Remind me what we settled.")
    page.locator(IXQ).press("Enter")
    expect(page.locator(f"{INDEX} [data-ask-page='22']")).to_be_visible(timeout=5_000)
    check("I: when the Host cannot pick, the Index asks which page, from the pages it was shown",
          page.locator(f"{INDEX} [data-ask-page]").count() == 2)
    page.locator(f"{INDEX} [data-ask-page='22']").click()
    expect(page.locator(PAGE_TITLE)).to_have_text("Newsletter signup", timeout=5_000)
    expect(page.locator(f"{LIVE} textarea")).to_have_value("Remind me what we settled.", timeout=3_000)
    check("I: ...the page picked opens with the words in its box, sent only when the person sends them",
          not STATE["sent"], STATE["sent"])


def scenario_index_open(page, base):
    print("I. 'open' mode: Enter opens the top page, no model call; an archived page comes back when written on")
    index_open(page, base)
    page.locator(f"{INDEX} [data-mode-btn='open']").click()
    check("I: 'only opens the page' is a mode of its own",
          page.locator(f"{INDEX} [data-mode-btn='open']").get_attribute("aria-pressed") == "true"
          and page.evaluate("localStorage.getItem('workbench.book.index.mode')") == "open")
    page.locator(IXQ).fill("newsletter")
    expect(page.locator(f"{INDEX} [data-index-result]").first).to_have_attribute("data-index-result", "22",
                                                                                 timeout=5_000)
    clock_pause(page)
    page.locator(IXQ).press("Enter")
    mid = turn_seen(page)
    expect(page.locator(PAGE_TITLE)).to_have_text("Newsletter signup", timeout=3_000)
    check("I: Enter opens the top page with a page turn, and asks the Host nothing",
          mid is not None and mid["motion"] == "turn" and mid["turning"]
          and page.locator(".book-leaf").count() == 0 and not STATE["index_sends"], (mid, STATE["index_sends"]))
    index_done(page)

    reopen_index(page)
    check("I: the mode is remembered", page.locator(f"{INDEX} [data-mode-btn='open']").get_attribute("aria-pressed")
          == "true")
    page.locator(IXQ).fill("gift")
    expect(page.locator(f"{INDEX} [data-index-result]").first).to_have_attribute("data-index-result", "33",
                                                                                 timeout=5_000)
    clock_pause(page)
    page.locator(IXQ).press("Enter")
    expect(page.locator("[data-riffle-cover]")).to_have_count(1, timeout=3_000)
    seen = over_the_book(page)
    check("I: an archived page opens with a riffle through its chapter", seen["leaves"] >= 3 and seen["cover"] == 1,
          seen)
    expect(page.locator(PAGE_TITLE)).to_have_text("Gift cards", timeout=3_000)
    index_done(page)
    note = page.locator(f"{LIVE} [data-archived-note]")
    check("I: it says it is archived, in which chapter, and that writing brings it back",
          "Storefront work chapter" in (note.text_content() or "")
          and "comes back to the top of the book" in (note.text_content() or ""), note.text_content())
    check("I: opening it changed nothing: still archived, no tab, nothing asked of the Host",
          row(33)["status"] == "archived" and 33 not in tab_ids(page) and not STATE["index_sends"]
          and not STATE["project_puts"])
    convo = page.locator(f"{LIVE} [data-chrome='page']")
    expect(convo).to_contain_text("Gift cards in 25, 50 and 100 dollars", timeout=5_000)
    box = page.locator(f"{LIVE} textarea")
    box.fill("Add a 200 dollar card.")
    box.press("Enter")
    ok = wait_for(lambda: len(STATE["sent"]) == 1, 6)
    check("I: written on, it comes back first (PUT status active), then the message goes",
          ok and STATE["project_puts"][:1] == [{"id": 33, "status": "active"}]
          and STATE["sent"][0].get("project_id") == 33, (STATE["project_puts"], STATE["sent"]))
    expect(page.locator(".book-tabs [data-tab='33']")).to_have_count(1, timeout=6_000)
    check("I: ...and it is in the book again, with a tab", row(33)["status"] == "active")
    turn_over(page)

    # A page that cannot come back says why, and the words stay in its box.
    reopen_index(page)
    page.locator(IXQ).fill("old domain")
    expect(page.locator(f"{INDEX} [data-index-result]").first).to_have_attribute("data-index-result", "36",
                                                                                 timeout=5_000)
    page.locator(IXQ).press("Enter")
    expect(page.locator(PAGE_TITLE)).to_have_text("Old domain", timeout=5_000)
    index_settled(page)
    n_sent = len(STATE["sent"])
    box.fill("Renew it.")
    box.press("Enter")
    refused = page.locator(f"{LIVE} [data-restore-refused]")
    expect(refused).to_be_visible(timeout=5_000)
    check("I: a page that cannot come back says why when written on, and nothing is sent",
          REFUSAL in (refused.text_content() or "") and len(STATE["sent"]) == n_sent
          and box.input_value() == "Renew it." and row(36)["status"] == "archived", refused.text_content())


# The words of a page started from the Index, and the title the Start box's
# own suggestion (lib/project-groups.ts suggestTitle) makes of them: the first
# clause, "I want to" dropped, at most eight words.
NEW_WORDS = "I want to open a kite shop for my sister's birthday. Start with the name."
NEW_TITLE = "Open a kite shop for my sister's birthday"
# The fixture's own pages end at 36: a page the Index made is a higher id.
FIXTURE_LAST_ID = 36


def made_pages():
    return [p for p in STATE["projects"] if p["id"] > FIXTURE_LAST_ID]


def kept_mode(page):
    return page.evaluate("localStorage.getItem('workbench.book.index.mode')")


def scenario_index_new(page, base):
    print("I. 'starts a new page': Enter makes a page for the words, as its first message, and the book turns to it")
    index_open(page, base)
    modes = page.locator(f"{INDEX} .book-ix-mode [data-mode-btn]").evaluate_all("els => els.map(e => e.dataset.modeBtn)")
    chip = page.locator(f"{INDEX} [data-mode-btn='new']")
    check("I: 'starts a new page' is a third mode, in the strip with the other two", modes == ["send", "open", "new"], modes)
    if modes != ["send", "open", "new"]:
        return
    check("I: the chip says so, and the Index still opens in 'send'",
          (chip.text_content() or "").strip() == "starts a new page"
          and chip.get_attribute("aria-pressed") == "false"
          and page.locator(f"{INDEX} [data-mode-btn='send']").get_attribute("aria-pressed") == "true")
    chip.click()
    check("I: chosen, it is pressed and the box says it starts a page",
          chip.get_attribute("aria-pressed") == "true"
          and "starts a new page" in (page.locator(IXQ).get_attribute("placeholder") or ""))
    check("I: ...and it is not kept: nothing is stored for the next visit", kept_mode(page) is None, kept_mode(page))

    STATE["create_hold"] = threading.Event()
    before = [p["id"] for p in STATE["projects"]]
    page.locator(IXQ).fill(NEW_WORDS)
    page.screenshot(path=str(SHOTS / "I-new-page-typed.png"))
    page.locator(IXQ).press("Enter")
    line = page.locator(f"{INDEX} [data-index-line]")
    expect(line).to_have_text("Starting the page…", timeout=3_000)
    check("I: while the page is made, the Index says so and holds the words",
          wait_for(lambda: len(STATE["creates"]) == 1, 5) and page.locator(IXQ).get_attribute("readonly") is not None)
    page.locator(IXQ).press("Enter")
    page.locator(IXQ).press("Enter")
    # Closed and opened again while the page is made: still the one message.
    page.keyboard.press("Escape")
    expect(page.locator("[data-index]")).to_have_count(0, timeout=3_000)
    index_settled(page)
    reopen_index(page)
    expect(line).to_have_text("Starting the page…", timeout=3_000)
    check("I: closed and opened again meanwhile, the Index still says so, the words in its box",
          page.locator(IXQ).input_value() == NEW_WORDS and page.locator(IXQ).get_attribute("readonly") is not None,
          page.locator(IXQ).input_value())
    page.locator(IXQ).press("Enter")
    check("I: a second Enter while the page is made asks for nothing more: one page, and the Host picked nothing",
          len(STATE["creates"]) == 1 and not STATE["index_sends"], (STATE["creates"], STATE["index_sends"]))
    ask = STATE["creates"][0] if STATE["creates"] else {}
    check("I: the page is asked for as the Start box asks for one: a title from the words, the words its description",
          ask.get("name") == NEW_TITLE and ask.get("description") == NEW_WORDS and "repo" not in ask, ask)

    clock_pause(page)
    STATE["create_hold"].set()
    expect(page.locator(".book-ghost")).to_have_count(1, timeout=6_000)
    of_index = page.locator(".book-ghost section[data-index]").count() == 1
    mid = turn_seen(page)
    expect(page.locator(PAGE_TITLE)).to_have_text(NEW_TITLE, timeout=3_000)
    check("I: the book turns to the new page with a page turn: the Index turns away over it",
          mid is not None and mid["motion"] == "turn" and mid["turning"] and of_index
          and page.locator(".book-leaf").count() == 0, (mid, of_index))
    index_done(page)
    expect(page.locator("[data-index]")).to_have_count(0, timeout=3_000)
    made = [p for p in made_pages() if p["id"] not in before]
    new_id = made[0]["id"] if len(made) == 1 else None
    check("I: one page was made", len(made) == 1 and len(STATE["creates"]) == 1, [p["id"] for p in made])
    check("I: its tab is there, first, and it is the page in front",
          new_id is not None and tab_ids(page)[:1] == [new_id]
          and page.locator(f".book-tabs [data-tab='{new_id}']").get_attribute("aria-selected") == "true"
          and page.locator(SHEET).get_attribute("data-page-id") == str(new_id), (new_id, tab_ids(page)))
    convo = page.locator(f"{LIVE} [data-chrome='page']")
    expect(convo).to_contain_text(NEW_WORDS, timeout=5_000)
    ok = wait_for(lambda: len(STATE["sent"]) == 1, 6)
    check("I: the words are its first message: one message to the Host, on the new page, as typed",
          ok and STATE["sent"][0].get("message") == NEW_WORDS and STATE["sent"][0].get("project_id") == new_id,
          STATE["sent"])
    written = [m for m in STATE["messages"] if m["project_id"] == new_id]
    check("I: ...and the first thing written on it", bool(written) and written[0]["role"] == "user"
          and written[0]["content"] == NEW_WORDS, written[:1])
    expect(convo).to_contain_text(REPLY_TAIL, timeout=10_000)
    check("I: the Host's reply streams on the new page, and the words are shown once",
          page.locator(f"{SHEET} .is-user:has-text('kite shop')").count() == 1)
    check("I: the composer is empty: the words were sent, not left in it", page.locator(f"{LIVE} textarea").input_value() == "")
    page.screenshot(path=str(SHOTS / "I-new-page-landed.png"))
    turn_over(page)

    # A page that could not be made says why, and the words stay.
    reopen_index(page)
    page.locator(f"{INDEX} [data-mode-btn='new']").click()
    STATE.update({"create_hold": None, "create_fails": "The Code Repository could not be made."})
    tabs0, sent0, asked0 = tab_ids(page), len(STATE["sent"]), len(STATE["creates"])
    page.locator(IXQ).fill("Plan the garden beds for spring, then order seeds.")
    page.locator(IXQ).press("Enter")
    expect(line).to_contain_text("could not be started", timeout=5_000)
    check("I: a page that could not be made says why in the Index, and the words stay",
          "The Code Repository could not be made." in (line.text_content() or "")
          and page.locator(IXQ).input_value() == "Plan the garden beds for spring, then order seeds.", line.text_content())
    check("I: ...no page and no message",
          len(STATE["creates"]) == asked0 + 1 and len(made_pages()) == 1 and tab_ids(page) == tabs0
          and len(STATE["sent"]) == sent0, (STATE["creates"][asked0:], tab_ids(page)))
    STATE["create_fails"] = None
    page.locator(IXQ).press("Enter")
    expect(page.locator(PAGE_TITLE)).to_have_text("Plan the garden beds for spring, then order", timeout=6_000)
    index_settled(page)
    check("I: the same words entered again make the page, and it is the second", len(made_pages()) == 2
          and wait_for(lambda: len(STATE["sent"]) == sent0 + 1, 6), [p["id"] for p in made_pages()])
    turn_over(page)

    # The mode is for the one use: the Index opens in the mode kept before it.
    reopen_index(page)
    check("I: the next visit opens in 'send' again, not in 'starts a new page'",
          page.locator(f"{INDEX} [data-mode-btn='send']").get_attribute("aria-pressed") == "true"
          and page.locator(f"{INDEX} [data-mode-btn='new']").get_attribute("aria-pressed") == "false")
    page.locator(f"{INDEX} [data-mode-btn='open']").click()
    page.locator(f"{INDEX} [data-mode-btn='new']").click()
    page.keyboard.press("Escape")
    expect(page.locator("[data-index]")).to_have_count(0, timeout=3_000)
    index_settled(page)
    reopen_index(page)
    check("I: ...and in 'only opens' when that was the mode kept",
          page.locator(f"{INDEX} [data-mode-btn='open']").get_attribute("aria-pressed") == "true"
          and kept_mode(page) == "open", kept_mode(page))


def scenario_index_new_reduced(page, base):
    print("I. 'starts a new page', reduced motion: the book crossfades to the new page")
    index_open(page, base)
    chip = page.locator(f"{INDEX} [data-mode-btn='new']")
    check("I: the mode is there with reduced motion too", chip.count() == 1)
    if chip.count() != 1:
        return
    chip.click()
    page.locator(IXQ).fill(NEW_WORDS)
    clock_pause(page)
    page.locator(IXQ).press("Enter")
    seen = turn_seen(page)
    page.clock.run_for(452)  # half a second on the page's clock from the Enter (48 ms of it run above)
    drawn(page)
    gone = page.locator(".book-ghost").count() == 0
    page.clock.resume()
    expect(page.locator(PAGE_TITLE)).to_have_text(NEW_TITLE, timeout=6_000)
    check("I: reduced motion: the new page comes with the short crossfade, never a page turn",
          seen is not None and seen["motion"] == "fade" and seen["props"] == ["opacity"] and max(seen["secs"]) < 0.5
          and gone and page.locator(".book-leaf").count() == 0, (seen, gone))
    index_settled(page)
    new_id = made_pages()[0]["id"] if len(made_pages()) == 1 else None
    check("I: ...to one new page, whose tab is first and whose first message is the words",
          new_id is not None and tab_ids(page)[:1] == [new_id]
          and wait_for(lambda: len(STATE["sent"]) == 1, 6)
          and STATE["sent"][0].get("message") == NEW_WORDS and STATE["sent"][0].get("project_id") == new_id,
          (new_id, tab_ids(page), STATE["sent"]))
    expect(page.locator(f"{LIVE} [data-chrome='page']")).to_contain_text(REPLY_TAIL, timeout=10_000)
    turn_over(page)


def scenario_index_new_empty(page, base):
    print("I. 'starts a new page' in an empty book: the pane that opens with the page sends its first message")
    reset()
    index_fixture()
    for p in STATE["projects"]:
        p["status"], p["group"] = "archived", "archived"
    STATE["last_open"] = ""  # no page to open: the book is empty
    page.clock.install()
    page.goto(f"{base}/system-apps/workbench/index.html", wait_until="domcontentloaded", timeout=15_000)
    expect(page.locator("[data-book-empty]")).to_be_visible(timeout=10_000)
    page.locator("[data-index-tab]").click()
    expect(page.locator(INDEX)).to_be_visible(timeout=3_000)
    chip = page.locator(f"{INDEX} [data-mode-btn='new']")
    check("I: an empty book's Index has the mode too", chip.count() == 1)
    if chip.count() != 1:
        return
    chip.click()
    page.locator(IXQ).fill(NEW_WORDS)
    page.locator(IXQ).press("Enter")
    expect(page.locator(PAGE_TITLE)).to_have_text(NEW_TITLE, timeout=6_000)
    index_settled(page)
    new_id = made_pages()[0]["id"] if len(made_pages()) == 1 else None
    check("I: the page is made and opened: its tab is the only one, and the empty book's note is gone",
          new_id is not None and tab_ids(page) == [new_id] and page.locator("[data-book-empty]").count() == 0,
          (new_id, tab_ids(page)))
    convo = page.locator(f"{LIVE} [data-chrome='page']")
    expect(convo).to_contain_text(REPLY_TAIL, timeout=10_000)
    check("I: ...with the words as its first message, sent once, and shown once",
          len(STATE["sent"]) == 1 and STATE["sent"][0].get("message") == NEW_WORDS
          and STATE["sent"][0].get("project_id") == new_id
          and page.locator(f"{SHEET} .is-user:has-text('kite shop')").count() == 1, STATE["sent"])
    turn_over(page)


def scenario_index_new_late_answer(page, base):
    print("I. 'starts a new page': a late answer to 'is a turn running' does not draw the page's own turn twice")
    index_open(page, base)
    chip = page.locator(f"{INDEX} [data-mode-btn='new']")
    check("I: the mode is there", chip.count() == 1)
    if chip.count() != 1:
        return
    # The new page asks whether a turn runs on it as it opens (the pane's
    # reattach); the answer comes after its first message has started one,
    # which is still streaming then.
    STATE.update({"turn_secs": 5, "active_delay": 1.5})
    chip.click()
    page.locator(IXQ).fill(NEW_WORDS)
    page.locator(IXQ).press("Enter")
    expect(page.locator(PAGE_TITLE)).to_have_text(NEW_TITLE, timeout=6_000)
    new_id = made_pages()[0]["id"] if len(made_pages()) == 1 else None
    late = wait_for(lambda: (new_id, True) in STATE.get("active_answers", []), 8)
    check("I: the page was told a turn runs on it, after its first message started one", late and new_id is not None,
          STATE.get("active_answers"))
    convo = page.locator(f"{LIVE} [data-chrome='page']")
    expect(convo).to_contain_text(REPLY_TAIL, timeout=15_000)
    check("I: ...and it attached to nothing: the one stream is the message's own",
          STATE.get("stream_reads", []) == [], STATE.get("stream_reads"))
    check("I: the words and the reply are each drawn once",
          page.locator(f"{SHEET} .is-user:has-text('kite shop')").count() == 1
          and page.locator(f"{SHEET} .is-assistant:has-text('{REPLY_TAIL}')").count() == 1
          and len(STATE["sent"]) == 1,
          (page.locator(f"{SHEET} .is-user").count(), page.locator(f"{SHEET} .is-assistant").count()))
    turn_over(page)


SECRET_KEY = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdefghij"
SECRET_SOFT = "hunter2hunter2"
SECRET_WORDS = f"Build a bot with {SECRET_KEY} and password = {SECRET_SOFT} for the shop"


def scenario_index_new_secret(page, base):
    print("I. 'starts a new page' with a secret in the words: the page is made without it, the words wait in its box")
    index_open(page, base)
    chip = page.locator(f"{INDEX} [data-mode-btn='new']")
    check("I: the mode is there", chip.count() == 1)
    if chip.count() != 1:
        return
    chip.click()
    page.locator(IXQ).fill(SECRET_WORDS)
    page.locator(IXQ).press("Enter")
    expect(page.locator(f"{SHEET} textarea")).to_have_value(SECRET_WORDS, timeout=8_000)
    made = STATE["creates"][0] if len(STATE["creates"]) == 1 else {}
    kept = f"{made.get('name', '')}\n{made.get('description', '')}"
    check("I: the page is made with no part of a secret in its name or description (POST /api/projects)",
          len(STATE["creates"]) == 1 and "sk-ant" not in kept and SECRET_SOFT not in kept, STATE["creates"])
    check("I: ...and the words are not sent: they wait in the new page's box for the composer's strip",
          not STATE["sent"], STATE["sent"])


def scenario_index_secret_query(page, base):
    print("I. a secret pasted into the Index never goes into its search: not the URL, and not the body")
    index_open(page, base)
    page.locator(IXQ).fill(SECRET_WORDS)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and not any("Build a bot" in r for r in STATE.get("index_reads", [])):
        page.wait_for_timeout(50)
    reads = STATE.get("index_reads", [])
    check("I: the search still runs on the words around the secret",
          any("Build a bot" in r and "for the shop" in r for r in reads), reads[-3:])
    check("I: ...and no search carries any part of a secret, hard or soft",
          not any("sk-ant" in r or SECRET_SOFT in r for r in reads), [r[:80] for r in reads[-3:]])
    urls = STATE.get("index_urls", [])
    check("I: ...and each was asked on the bare route: the words are in the body, never in a URL (a URL lands in access logs)",
          bool(urls) and all(urlsplit(u).query == "" and urlsplit(u).path == "/api/projects/index" for u in urls),
          urls[-2:])


# ── a secret TYPED into the Index ────────────────────────────────────────────
# A person types a password or a key a character at a time, and the Index
# searches after every pause in the typing (src/book/index-page.tsx, a wait of
# INDEX_DEBOUNCE_MS after the last key). A cut that finds a secret once it is
# whole (src/lib/book-index.ts indexQueryText) lets the shorter prefixes of it
# through, so a search whose words were in its URL put the early part of a
# password or a key into whatever kept the URL. The words go in a POST body
# now, so the URL carries nothing. This types the secrets instead of pasting
# them (fill() would put the whole text in at once, and the cut would see all
# of it).
SEARCH_PATHS = ("/api/projects/index", "/api/projects/search")
# Longer than INDEX_DEBOUNCE_MS (120, src/lib/book-index.ts), on the page's
# clock: each key's search goes out before the next key is pressed.
TYPING_PAUSE_MS = 200


def watch_searches(page) -> list:
    """Every search request the page makes from now on, as the browser sends
    it: method, whole URL, body. Read in the browser and not at the mock, so
    a request the mock would refuse (a GET to a route that takes a POST) is
    seen all the same."""
    seen: list = []

    def note(req):
        if urlsplit(req.url).path in SEARCH_PATHS:
            seen.append({"method": req.method, "url": req.url, "body": req.post_data})

    page.on("request", note)
    return seen


def pump_until(page, cond, secs=5.0) -> bool:
    """Wait for `cond()`, letting the browser's events in meanwhile: the sync
    API delivers them inside a page call, never during time.sleep."""
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        if cond():
            return True
        page.wait_for_timeout(20)
    return cond()


def pause_for_search(page, seen) -> bool:
    """Let the page's clock run past the Index's wait, then wait for the
    search that wait sends."""
    n = len(seen)
    drawn(page)  # React has drawn the last key and set the Index's timer
    page.clock.run_for(TYPING_PAUSE_MS)
    return pump_until(page, lambda: len(seen) > n)


def type_with_pauses(page, text, seen) -> bool:
    """`text` typed into the Index a key at a time, with a pause after each
    that is longer than the Index's wait, so the search for what is in the
    box so far goes out before the next key. The page's clock is paused by
    the caller, so a pause is exactly TYPING_PAUSE_MS and each key is
    followed by one search, however loaded the machine."""
    page.locator(IXQ).focus()
    for ch in text:
        page.keyboard.type(ch)
        if not pause_for_search(page, seen):
            return False
    return True


def scenario_index_secret_typed(page, base):
    print("I. a secret TYPED into the Index, a key at a time with a pause after each, never goes into a search URL")
    index_open(page, base)
    seen = watch_searches(page)
    clock_pause(page)
    password = f"password = {SECRET_SOFT}"
    typed = {}
    for what, text in (("password", password), ("key", SECRET_KEY)):
        start = len(seen)
        ok = type_with_pauses(page, text, seen)
        typed[what] = {"text": text, "ok": ok, "searches": seen[start:]}
        page.locator(IXQ).fill("")  # clear the box for the next
        pause_for_search(page, seen)
    page.clock.resume()

    searches = list(seen)
    keys = len(password) + len(SECRET_KEY)
    check(f"I: typed a key at a time, a search went out after every key ({keys} typed): the checks below are not vacuous",
          all(t["ok"] for t in typed.values()) and len(searches) >= keys,
          (len(searches), [(k, t["ok"], len(t["searches"])) for k, t in typed.items()]))
    raw = [x["url"] for x in searches]
    urls = [unquote(u) for u in raw]
    leaked = [u for r, u in zip(raw, urls) if urlsplit(r).query or any(f in u for f in ("pass", "hunter", "sk-ant", "api03"))]

    def most_held(secret, texts):
        return max((k for k in range(len(secret), 0, -1) if any(secret[:k] in t for t in texts)), default=0)

    password_in_urls = SECRET_SOFT[:most_held(SECRET_SOFT, urls)]
    key_in_urls = SECRET_KEY[:most_held(SECRET_KEY, urls)]
    check("I: no search URL holds any typed text: each is the bare route, with no query string and no part of either secret",
          not leaked and all(urlsplit(r).path in SEARCH_PATHS for r in raw),
          f"{len(leaked)} of {len(urls)} URLs carry typed text, e.g. {[u.split('/api/')[-1] for u in leaked[:3]]}; "
          f"the most of the password's value in a URL: {password_in_urls!r}; of the key: {key_in_urls!r}")
    check("I: ...every search is a POST", all(x["method"] == "POST" for x in searches),
          sorted({x["method"] for x in searches}))
    bodies = []
    for x in searches:
        try:
            bodies.append(json.loads(x["body"] or ""))
        except ValueError:
            bodies.append(None)
    check("I: ...with the words in the body, as {q, limit}",
          all(isinstance(b, dict) and set(b) == {"q", "limit"} and isinstance(b["q"], str) for b in bodies),
          [x["body"] for x in searches[:2]])
    qs = [b["q"] for b in bodies if isinstance(b, dict) and isinstance(b.get("q"), str)]
    check("I: no body holds a whole secret, at any key: not the key, not the password's value",
          bool(qs) and not any(SECRET_KEY in q or SECRET_SOFT in q for q in qs),
          [q[:60] for q in qs if SECRET_KEY in q or SECRET_SOFT in q][:2])

    def last_q(what):
        found = [json.loads(x["body"] or "{}").get("q", "") for x in typed[what]["searches"] if x["body"]]
        return found[-1] if found else None

    done_pw, done_key = last_q("password"), last_q("key")
    check("I: once the password is all typed its search keeps the words around it and none of its value",
          done_pw is not None and "password" in done_pw and "hunter2" not in done_pw, done_pw)
    check("I: once the key is all typed its search holds no part of it",
          done_key is not None and "sk-ant" not in done_key and "api03" not in done_key, done_key)

    # Not a check: what the cut cannot do. A secret is recognised once it is
    # whole, so the prefixes before that go out in the body (never a URL).
    print(f"        (a prefix is still sent before the shape is whole: {most_held(SECRET_SOFT, qs)} of the password "
          f"value's {len(SECRET_SOFT)} characters, {most_held(SECRET_KEY, qs)} of the key's {len(SECRET_KEY)}; in a body)")


def scenario_index_new_phone(page, base):
    print("I. 'starts a new page' on a phone: the chip is in the strip, whole and reachable; the page lands")
    index_open(page, base)
    chip = page.locator(f"{INDEX} [data-mode-btn='new']")
    check("I: the mode is there on a phone", chip.count() == 1)
    if chip.count() != 1:
        return
    vw = page.viewport_size["width"]
    chip.scroll_into_view_if_needed()
    box, strip = chip.bounding_box(), page.locator(f"{INDEX} .book-ix-mode").bounding_box()
    others = page.locator(f"{INDEX} .book-ix-mode [data-mode-btn]:not([data-mode-btn='new'])").evaluate_all(
        "els => els.map(e => { const r = e.getBoundingClientRect(); return {x: r.x, right: r.right}; })")
    top_is_chip = page.evaluate(
        "([x, y]) => { const e = document.elementFromPoint(x, y); return !!(e && e.closest('[data-mode-btn=\"new\"]')); }",
        [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2])
    check(f"I: on a phone ({vw} px) the chip sits whole inside the screen and the mode strip, and a tap lands on it",
          box["x"] >= 0 and box["x"] + box["width"] <= vw and strip["x"] - 0.5 <= box["x"]
          and box["x"] + box["width"] <= strip["x"] + strip["width"] + 0.5 and top_is_chip
          and len(others) == 2, (box, strip, vw, top_is_chip))
    chip.tap()
    expect(chip).to_have_attribute("aria-pressed", "true", timeout=3_000)
    page.locator(IXQ).fill(NEW_WORDS)
    page.screenshot(path=str(SHOTS / "I-new-page-typed-phone.png"))
    page.locator(IXQ).press("Enter")
    expect(page.locator(PAGE_TITLE)).to_have_text(NEW_TITLE, timeout=6_000)
    index_settled(page)
    new_id = made_pages()[0]["id"] if len(made_pages()) == 1 else None
    check("I: a page started from the phone's Index is one new page, first among the tabs, in front",
          new_id is not None and tab_ids(page)[:1] == [new_id]
          and page.locator(f".book-tabs [data-tab='{new_id}']").get_attribute("aria-selected") == "true",
          (new_id, tab_ids(page)))
    convo = page.locator(f"{LIVE} [data-chrome='page']")
    expect(convo).to_contain_text(NEW_WORDS, timeout=5_000)
    expect(convo).to_contain_text(REPLY_TAIL, timeout=10_000)
    check("I: ...with the words as its first message, sent once",
          len(STATE["sent"]) == 1 and STATE["sent"][0].get("message") == NEW_WORDS
          and STATE["sent"][0].get("project_id") == new_id and len(STATE["creates"]) == 1, STATE["sent"])
    page.screenshot(path=str(SHOTS / "I-new-page-landed-phone.png"))
    turn_over(page)


def scenario_chapters(page, base):
    print("G. chapters: the buckets with their archived pages; a riffle opens one; a tab dropped files its page")
    index_open(page, base)
    chaps = page.locator(f"{INDEX} [data-chapter]")
    names = chaps.evaluate_all("els => els.map(e => [e.dataset.chapter, e.querySelector('b').textContent,"
                               " e.querySelector('span').textContent])")
    check("G: every bucket is a chapter, in its order, with its archived pages; then Loose pages",
          names == [["7", "Storefront work", "4 pages"], ["8", "Launch", "1 page"], ["9", "Ideas", "0 pages"],
                    ["loose", "Loose pages", "2 pages"]], names)
    clock_pause(page)
    page.locator(f"{INDEX} [data-chapter='7']").click()
    expect(page.locator("[data-riffle-cover]")).to_have_count(1, timeout=3_000)
    seen = over_the_book(page)
    check("G: opening a chapter riffles through its pages: the page in front and the chapter's four",
          seen["leaves"] == 5 and seen["cover"] == 1 and seen["turning"] == 0, seen)
    mid = riffle_frame(page, "G-riffle-frame.png")
    check("G: ...turning away one after another", 0 < mid["turning"] < mid["leaves"], mid)
    index_done(page)
    opened = page.locator(f"{LIVE} [data-chapter-open='7']")
    expect(opened).to_be_visible(timeout=5_000)
    check("G: ...and lands on the chapter: its pages, newest first",
          results(page) == [31, 32, 33, 34], results(page))
    glided(page)
    page.screenshot(path=str(SHOTS / "G-chapter-open.png"))
    opened.locator("[data-chapter-rename]").click()
    name_box = opened.locator("[data-chapter-name-input]")
    name_box.fill("Shop work")
    name_box.press("Enter")
    expect(opened.locator("[data-chapter-title]")).to_have_text("Shop work", timeout=5_000)
    check("G: a chapter is renamed as a bucket is (PATCH /api/projects/buckets/7), once",
          [w for w in STATE["bucket_writes"] if w[0] == "patch" and "name" in w[2]] == [("patch", 7, {"name": "Shop work"})],
          STATE["bucket_writes"])
    opened.locator("[data-swatch='#0ea5e9']").click()
    expect(opened.locator("[data-swatch='#0ea5e9']")).to_have_attribute("aria-pressed", "true", timeout=5_000)
    check("G: ...and coloured as one", ("patch", 7, {"color": "#0ea5e9"}) in STATE["bucket_writes"],
          STATE["bucket_writes"])
    page.keyboard.press("Escape")
    expect(page.locator(f"{LIVE} [data-chapter-open]")).to_have_count(0, timeout=3_000)
    check("G: Escape in a chapter goes back to every page, the Index still open",
          page.locator(INDEX).count() == 1)
    page.locator(f"{INDEX} [data-chapter='7']").click()
    expect(opened).to_be_visible(timeout=5_000)
    index_settled(page)
    opened.locator("[data-index-result='32']").click()
    expect(page.locator(PAGE_TITLE)).to_have_text("Shipping rates", timeout=5_000)
    index_settled(page)
    check("G: a page of the chapter opens from it", row(32)["status"] == "archived")

    # A tab dropped on a chapter files its page there.
    reopen_index(page)
    page.locator(".book-tabs [data-tab='22']").drag_to(page.locator(f"{INDEX} [data-chapter='9']"))
    ok = wait_for(lambda: bool(STATE["places"]), 5)
    check("G: a tab dragged onto a chapter files its page there (PATCH /api/projects/22/place)",
          ok and STATE["places"] == [{"id": 22, "bucket_id": 9, "before_id": None}], STATE["places"])
    expect(page.locator(f"{INDEX} [data-chapter='9'] span")).to_have_text("0 pages · 1 open", timeout=5_000)
    check("G: ...and the chapter counts it, open in the book", tab_ids(page).count(22) == 1)
    page.locator(f"{INDEX} [data-new-chapter]").click()
    page.locator(f"{INDEX} [data-new-chapter-input]").fill("Garden")
    page.locator(f"{INDEX} [data-new-chapter-input]").press("Enter")
    expect(page.locator(f"{INDEX} [data-chapter] b", has_text="Garden")).to_have_count(1, timeout=5_000)
    check("G: a new chapter is a new bucket (POST /api/projects/buckets), made once",
          [w for w in STATE["bucket_writes"] if w[0] == "create"] == [("create", {"name": "Garden"})],
          STATE["bucket_writes"])


def scenario_index_reduced(page, base):
    print("G. reduced motion: no riffle, a crossfade")
    index_open(page, base)
    clock_pause(page)
    page.locator(f"{INDEX} [data-chapter='7']").click()
    seen = turn_seen(page)
    leaves = page.locator(".book-leaf").count()
    check("G: reduced motion: opening a chapter draws no riffle, it crossfades",
          seen is not None and seen["motion"] == "fade" and seen["turning"] and seen["props"] == ["opacity"]
          and leaves == 0, (seen, leaves))
    page.clock.run_for(452)  # half a second on the page's clock from the click (48 ms of it run above)
    drawn(page)
    check("G: ...a short one (under half a second), its copy gone by then",
          seen is not None and max(seen["secs"]) < 0.5 and page.locator(".book-ghost").count() == 0,
          (seen and seen["secs"], over_the_book(page)))
    page.clock.resume()
    expect(page.locator(f"{LIVE} [data-chapter-open='7']")).to_be_visible(timeout=3_000)
    index_settled(page)
    clock_pause(page)
    page.locator(f"{INDEX} [data-index-result='31']").click()
    seen = turn_seen(page)
    leaves = page.locator(".book-leaf").count()
    page.clock.run_for(452)
    drawn(page)
    gone = page.locator(".book-ghost").count() == 0
    page.clock.resume()
    expect(page.locator(PAGE_TITLE)).to_have_text("Checkout redesign", timeout=3_000)
    check("G: ...and an archived page opens with the crossfade too, as short, never a riffle",
          seen is not None and seen["motion"] == "fade" and max(seen["secs"]) < 0.5 and leaves == 0 and gone,
          (seen, leaves, gone))


def scenario_index_phone(page, base):
    print("I. the Index on a phone: the strip's Index tab, full width; send; a chapter")
    index_open(page, base)
    vw = page.viewport_size["width"]
    box = page.locator(INDEX).bounding_box()
    check(f"I: on a phone the Index is the full-width page ({vw} px)", box is not None and box["x"] <= 1
          and abs(box["width"] - vw) <= 1, box)
    fit = ixtab_fits(page)
    check("I: on a phone the Index tab's word sits inside the tab", fit["inside"], fit)
    type_slowly(page, "phone")
    first = results_become(page, lambda r: r[:3] == [21, 31, 34])
    glided(page)
    page.screenshot(path=str(SHOTS / "I-index-typing-phone.png"))
    type_slowly(page, " photos")
    got = results_become(page, lambda r: r[:1] == [34])
    check("I: typing re-sorts the pages on a phone too", first[:3] == [21, 31, 34] and got[:1] == [34],
          (first, got))
    page.locator(f"{INDEX} [data-chapter='7']").scroll_into_view_if_needed()
    clock_pause(page)
    page.locator(f"{INDEX} [data-chapter='7']").tap()
    expect(page.locator("[data-riffle-cover]")).to_have_count(1, timeout=3_000)
    mid = riffle_frame(page, "G-riffle-frame-phone.png")
    check("I: a chapter riffles open on a phone", mid["cover"] == 1 and 0 < mid["turning"] < mid["leaves"], mid)
    index_done(page)
    expect(page.locator(f"{LIVE} [data-chapter-open='7']")).to_be_visible(timeout=5_000)
    glided(page)
    page.screenshot(path=str(SHOTS / "G-chapter-open-phone.png"))
    check("I: ...and lands on its pages", results(page) == [31, 32, 33, 34], results(page))
    page.locator(f"{LIVE} [data-chapter-back]").tap()
    STATE.update({"turn_secs": 4, "pick": {"page": 22, "reason": "It is about the newsletter's confirmation email."}})
    page.locator(IXQ).fill("Is the confirmation email ready?")
    page.locator(IXQ).press("Enter")
    expect(page.locator(PAGE_TITLE)).to_have_text("Newsletter signup", timeout=6_000)
    index_settled(page)
    convo = page.locator(f"{LIVE} [data-chrome='page']")
    expect(convo).to_contain_text("Index message, picked this page", timeout=5_000)
    expect(convo).to_contain_text("Is the confirmation email ready?", timeout=5_000)
    page.screenshot(path=str(SHOTS / "I-send-landed-phone.png"))
    check("I: a send from the phone's Index lands on the picked page", len(STATE["index_sends"]) == 1)
    turn_over(page)


# A bucket's seven colours, as the rail offered them and as
# the chapters keep them (lib/project-groups.ts BUCKET_COLORS).
BUCKET_SWATCHES = ["#64748b", "#ef4444", "#f59e0b", "#10b981", "#0ea5e9", "#8b5cf6", "#ec4899"]


def scenario_switch(page, base):
    print("R. the switch: index.html is the book; archiving a page; a chapter's colours and its delete")
    reset()
    # A bucket with a page in it, for the chapter checks below.
    STATE["buckets"] = [{"id": 7, "name": "Storefront work", "color": "#10b981", "sort_order": 1}]
    row(22)["bucket_id"] = 7
    asked: list[str] = []
    page.on("request", lambda r: asked.append(r.url))
    open_book(page, base)
    check("R: index.html opens the book, in the desktop's colours (dark on the stock tokens), its seven tabs and "
          "the page in front",
          page.evaluate("document.documentElement.dataset.page") == "book"
          and page.evaluate("document.documentElement.classList.contains('dark')")
          and page.locator("[data-book]").count() == 1 and tab_ids(page) == [21, 22, 23, 24, 25, 26, 27])
    check("R: no rail, chat and bench columns: no project rail rows, no 'The bench' column, no chat header",
          page.locator("[data-project-id]").count() == 0
          and page.locator("aside:has-text('The bench')").count() == 0
          and page.locator("header:has-text('project thread')").count() == 0)
    check("R: no link to a rail layout", page.locator("a:has-text('Rail layout')").count() == 0)
    check("R: book.html is gone (the book is index.html) and nothing asks for it",
          not (APP_DIR / "book.html").exists() and not any("book.html" in u for u in asked),
          [u for u in asked if "book.html" in u])
    # A link to a page (server/deep_link.py: /?open=workbench&view=project:<id>):
    # the desktop opens a new window on index.html#view=project:<id>, so the
    # page loads afresh (a hash change alone would not reload it).
    page.goto("about:blank")
    page.goto(f"{base}/system-apps/workbench/index.html#view=project%3A23", wait_until="domcontentloaded",
              timeout=15_000)
    expect(page.locator(PAGE_TITLE)).to_have_text("Photo resizer", timeout=10_000)
    check("R: a project:<id> link opens that page, and the book remembers it",
          page.locator(".book-tabs [data-tab='23']").get_attribute("aria-selected") == "true"
          and wait_for(lambda: STATE["last_open"] == "23", 5), STATE["last_open"])
    # The book's own earlier view (never shipped): nothing to
    # redirect to, the book opens where it is.
    page.goto("about:blank")
    n0 = STATE.get("last_open_n", 0)
    page.goto(f"{base}/system-apps/workbench/index.html#view=book", wait_until="domcontentloaded", timeout=15_000)
    expect(page.locator(PAGE_TITLE)).to_have_text("Photo resizer", timeout=10_000)
    # Its own "last open" write lands before the next part resets the mock.
    wait_for(lambda: STATE.get("last_open_n", 0) > n0, 5)
    check("R: an old #view=book link opens the book on index.html, at the last open page",
          page.url.split("#")[0].endswith("/system-apps/workbench/index.html"), page.url)

    # Archiving the page in front, from "This page".
    reset()
    STATE["buckets"] = [{"id": 7, "name": "Storefront work", "color": "#10b981", "sort_order": 1}]
    row(22)["bucket_id"] = 7
    open_book(page, base)
    right = page.locator("aside.book-right")
    arch = right.locator("[data-ctl='archive'] [data-archive-page]")
    expect(arch).to_be_visible(timeout=5_000)
    arch.click()
    dlg = page.get_by_role("dialog")
    expect(dlg).to_be_visible(timeout=3_000)
    check("R: 'Archive it' asks first, naming the page, and archives nothing yet",
          "Archive “Storefront”?" in (dlg.text_content() or "") and not STATE.get("archives"),
          (dlg.text_content() or "")[:160])
    dlg.get_by_role("button", name="Cancel").click()
    expect(dlg).to_have_count(0, timeout=3_000)
    check("R: Cancel archives nothing", not STATE.get("archives") and row(21)["status"] == "active")
    arch.click()
    page.get_by_role("dialog").get_by_role("button", name="Archive", exact=True).click()
    ok = wait_for(lambda: bool(STATE.get("archives")), 5)
    check("R: Archive archives the page (DELETE /api/projects/21), keeping its repository",
          ok and STATE["archives"] == [{"id": 21, "delete_repo": False}], STATE.get("archives"))
    expect(page.locator(PAGE_TITLE)).to_have_text("Newsletter signup", timeout=5_000)
    expect(page.locator(".book-tabs [data-tab='21']")).to_have_count(0, timeout=12_000)
    check("R: the book turns to the page after it, and the archived page has no tab",
          tab_ids(page)[:1] == [22], repr(tab_ids(page)))
    # The turn is over (a press during a turn is dropped, as in the mockup).
    expect(page.locator(".book-ghost")).to_have_count(0, timeout=5_000)
    page.locator("[data-index-tab]").click()
    expect(page.locator(f"{LIVE} [data-index]")).to_be_visible(timeout=3_000)
    loose = page.locator(f"{LIVE} [data-chapter='loose']")
    expect(loose).to_be_visible(timeout=3_000)
    loose.click()
    expect(page.locator(f"{LIVE} [data-chapter-open='loose'] [data-index-result='21']")).to_be_visible(timeout=8_000)
    check("R: the Index keeps it, archived, in its chapter (Loose pages)", True)
    page.locator(f"{LIVE} [data-chapter-back]").click()

    # A chapter: its colours, then Delete.
    ch = page.locator(f"{LIVE} [data-chapter='7']")
    expect(ch).to_be_visible(timeout=3_000)
    ch.click()
    open_ch = page.locator(f"{LIVE} [data-chapter-open='7']")
    expect(open_ch).to_be_visible(timeout=8_000)
    swatches = open_ch.locator("[data-swatch]").evaluate_all("els => els.map(e => e.dataset.swatch)")
    check("R: a chapter's colours are a bucket's seven, in their order", swatches == BUCKET_SWATCHES, swatches)
    said: list[str] = []
    page.once("dialog", lambda d: (said.append(d.message), d.dismiss()))
    open_ch.locator("[data-chapter-delete]").click()
    # The question was answered inside the click; the chapter is still open.
    expect(open_ch.locator("[data-chapter-delete]")).to_be_visible(timeout=3_000)
    check("R: Delete on a chapter asks first; dismissed, nothing is deleted",
          said and "Storefront work" in said[0] and not any(w[0] == "delete" for w in STATE.get("bucket_writes", [])),
          said)
    page.once("dialog", lambda d: d.accept())
    open_ch.locator("[data-chapter-delete]").click()
    ok = wait_for(lambda: any(w[0] == "delete" for w in STATE.get("bucket_writes", [])), 5)
    check("R: ...accepted, the bucket is deleted (DELETE /api/projects/buckets/7)", ok, STATE.get("bucket_writes"))
    expect(page.locator(f"{LIVE} [data-chapter='7']")).to_have_count(0, timeout=5_000)
    check("R: the chapter is gone from the Index and its page stays, in no chapter",
          row(22)["bucket_id"] is None and row(22)["status"] == "active" and 22 in tab_ids(page))

    # A run card opened from the right side's Runs and expanded is in front of
    # the page. The right side is its own layer, under the page, so a fixed
    # card drawn in it was hidden behind the page (book.css, the :has rule).
    reset()
    open_book(page, base)
    right = page.locator("aside.book-right")
    right.locator("button[data-state]").filter(has_text=re.compile(r"^\s*Runs")).first.click()
    right.locator("button").filter(has_text="#702").first.click()
    card = right.locator("[data-run-card][data-session='sess-a']").first
    expect(card).to_be_visible(timeout=5_000)
    card.locator("button[title='Expand']").click()
    expect(right.locator("[data-run-card].fixed")).to_have_count(1, timeout=3_000)
    top = page.evaluate("""() => {
        const c = document.querySelector('aside.book-right [data-run-card].fixed');
        const r = c.getBoundingClientRect();
        const at = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        return {inCard: !!(at && c.contains(at)), at: at ? at.className.toString().slice(0, 60) : null};
    }""")
    check("R: a run card from the right side's Runs, expanded, is in front of the page", top["inCard"], top)
    page.screenshot(path=str(SHOTS / "R-run-card-expanded.png"))


# ── the desktop's colours (the set desktop UI colours) ─────────────────────
# The shell's stylesheet with the stock tokens, served in these scenarios as
# the kernel serves it, and the desktop contract that names the tokens.
TOKENS_CSS = ROOT / "dashboard" / "base" / "css" / "tokens.css"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.applications.desktop_contract import (  # noqa: E402
    OPTIONAL_TOKENS, REQUIRED_TOKENS, check_palette, parse_color)


def stock_tokens() -> dict[str, str]:
    """The desktop's colour tokens as the shell ships them: every one the
    desktop contract names with a colour for its value, read off
    dashboard/base/css/tokens.css, never typed here."""
    names = set(REQUIRED_TOKENS) | set(OPTIONAL_TOKENS)
    out: dict[str, str] = {}
    for m in re.finditer(r"(--cseries-[a-z0-9-]+)\s*:\s*([^;]+);", TOKENS_CSS.read_text()):
        name, value = m.group(1), " ".join(m.group(2).split())
        if name in names and name not in out and parse_color(value) is not None:
            out[name] = value
    return out


# A light design: the palette the desktop contract's own gate passes as a
# coherent light one (scripts/test_desktop_contract.py LIGHT), and checked
# against that contract again before it is used. It names the eleven required
# tokens only: the rest stay the stock ones, as on a real desktop.
LIGHT_TOKENS = {
    "--cseries-ink": "#ffffff", "--cseries-night": "#f4f5f7", "--cseries-twilight": "#e9ebef",
    "--cseries-silk": "#dfe2e8", "--cseries-veil": "#d3d7df", "--cseries-mist": "#b9bfc9",
    "--cseries-paper": "#14171c", "--cseries-linen": "#4a5260", "--cseries-ash": "#767e8c",
    "--cseries-ember": "#1f6f52", "--cseries-dusk": "#0f4f39",
}


def colour_sets():
    """(name, the tokens the shell sets, every token's value then, dark?)."""
    stock = stock_tokens()
    errors, warnings = check_palette(LIGHT_TOKENS)
    if errors or warnings or set(LIGHT_TOKENS) != set(REQUIRED_TOKENS):
        raise RuntimeError(f"the light design is not one the desktop contract passes: {errors} {warnings}")
    return (("the stock dark set", stock, stock, True),
            ("a light design", dict(LIGHT_TOKENS), {**stock, **LIGHT_TOKENS}, False))


def apply_tokens(page, tokens):
    """A desktop design's tokens on this frame's root, the way the shell sets
    them (dashboard/base/js/desktop-surface.js applyTokens): inline custom
    properties on the document element, the last set's other names removed."""
    page.evaluate("""t => {
        const root = document.documentElement;
        for (const n of [...root.style]) if (n.startsWith('--cseries-') && !(n in t)) root.style.removeProperty(n);
        for (const [n, v] of Object.entries(t)) root.style.setProperty(n, v);
    }""", tokens)
    page.wait_for_timeout(300)


def transitions_over(page):
    """Wait for every CSS transition on the page to end, so a colour is read
    where it landed: a run card's edge eases to a new design's colour."""
    page.evaluate("""() => Promise.all(document.getAnimations()
        .filter(a => a instanceof CSSTransition).map(a => a.finished.catch(() => null)))""")


# Each element's colour as the browser computed it, drawn on a canvas to
# read it back as sRGB bytes, whatever form the computed value takes (a
# color-mix() is computed as oklab() or color(srgb …)).
MEASURE_JS = """want => {
    const cv = document.createElement('canvas');
    cv.width = cv.height = 1;
    const cx = cv.getContext('2d', {willReadFrequently: true});
    const rgb = css => {
        const first = /^(?:rgba?|oklab|oklch|lab|lch|color|hsla?)\\([^)]*\\)|^#[0-9a-f]+/i.exec(String(css || '').trim());
        if (!first || !CSS.supports('color', first[0])) return null;
        cx.clearRect(0, 0, 1, 1);
        cx.fillStyle = first[0];
        cx.fillRect(0, 0, 1, 1);
        const d = cx.getImageData(0, 0, 1, 1).data;
        return [d[0], d[1], d[2], Math.round(d[3] / 255 * 1000) / 1000];
    };
    const out = {};
    for (const [key, [q, prop]] of Object.entries(want)) {
        const el = document.querySelector(q);
        out[key] = el ? rgb(getComputedStyle(el)[prop]) : null;
    }
    return out;
}"""


def measure(page, want):
    return page.evaluate(MEASURE_JS, want)


def measure_one(page, css):
    """One computed colour, read back as sRGB bytes as MEASURE_JS reads them."""
    return page.evaluate("""css => {
        const cv = document.createElement('canvas');
        cv.width = cv.height = 1;
        const cx = cv.getContext('2d', {willReadFrequently: true});
        if (!CSS.supports('color', css)) return null;
        cx.fillStyle = css;
        cx.fillRect(0, 0, 1, 1);
        const d = cx.getImageData(0, 0, 1, 1).data;
        return [d[0], d[1], d[2], Math.round(d[3] / 255 * 1000) / 1000];
    }""", css)


def token_rgb(value):
    c = parse_color(value)
    return None if c is None else [c[0], c[1], c[2], c[3]]


def same(a, b, tol=2.0):
    return a is not None and b is not None and all(abs(a[i] - b[i]) <= tol for i in range(3))


def dist(a, b):
    return 1e9 if a is None or b is None else sum((a[i] - b[i]) ** 2 for i in range(3)) ** 0.5


def between(c, a, b, tol=2.0):
    return c is not None and a is not None and b is not None and all(
        min(a[i], b[i]) - tol <= c[i] <= max(a[i], b[i]) + tol for i in range(3))


def contrast(fg, bg):
    """The WCAG contrast of words in `fg` over an opaque `bg`; 0 when either
    is missing."""
    if fg is None or bg is None:
        return 0.0

    def lum(c):
        def ch(v):
            v = v / 255
            return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
        return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])
    a = fg[3]
    f = [fg[i] * a + bg[i] * (1 - a) for i in range(3)]
    hi, lo = sorted((lum(f), lum(bg)), reverse=True)
    return round((hi + 0.05) / (lo + 0.05), 2)


def colours_ready(page, base, turns=()):
    """Storefront on the shell's own tokens.css, its compare finished (the run
    cards, the strands and This page's Open to colour), Recipe site lit green
    and Photo resizer amber. `turns` are more of the Host's replies, after the
    one with code in it."""
    reset()
    STATE["tokens_css"] = TOKENS_CSS.read_text()
    row(24)["last_landed_at"] = now() - timedelta(minutes=1)
    # A reply with code in it, last on the page.
    STATE.setdefault("old_turns", {})[21] = [_turn(
        900, "assistant", "Add the totals like this:\n\n```js\nconst total = items.reduce((sum, item) => sum + item.price, 0);"
        "\nconsole.log(`Total: ${total}`);\n```", 1.5, model="claude-opus-5-5", provider="anthropic")] + list(turns)
    bench_start(500.0)
    open_book(page, base)
    expect(page.locator(f"{SHEET} [data-run-card][data-harness='codex']")).to_have_count(1, timeout=10_000)
    expect(page.locator(".book-tabs [data-tab='24'][data-light='green']")).to_have_count(1, timeout=10_000)
    # "Where this stands" opened (one line until then, page-top.tsx Stands),
    # so its rule and label are drawn to be coloured.
    toggle = page.locator(f"{LIVE} [data-stands-toggle][aria-expanded='false']")
    if toggle.count():
        toggle.first.click()
    expect(page.locator(f"{LIVE} .book-stands")).to_have_count(1, timeout=10_000)
    page.wait_for_timeout(1_200)


# An engine the colour table (lib/strands.ts ENGINE_COLORS) does not name is
# drawn in its fallback colour. The colour checks put one on the compare, so
# that colour is read where the person sees it: Codex's run under another id.
UNLISTED_ENGINE = "aider"


def engine_table() -> set[str]:
    """The engines lib/strands.ts ENGINE_COLORS names a colour for, read off
    the file, never typed here."""
    src = (ROOT / "workbench-web" / "src" / "lib" / "strands.ts").read_text()
    table = re.search(r"export const ENGINE_COLORS[^=]*=\s*\{(.*?)\n\};", src, re.S)
    return set(re.findall(r'^\s*"?([A-Za-z0-9-]+)"?\s*:', table.group(1), re.M)) if table else set()


@contextlib.contextmanager
def unlisted_engine():
    """The compare with one more engine on it, one the colour table does not
    name; the mock's compare as it was when this is over."""
    BENCH["engines"][UNLISTED_ENGINE] = copy.deepcopy(BENCH["engines"]["codex"])
    BENCH_HS.append(UNLISTED_ENGINE)
    try:
        yield
    finally:
        BENCH_HS.remove(UNLISTED_ENGINE)
        del BENCH["engines"][UNLISTED_ENGINE]


def scenario_colours(page, base):
    with unlisted_engine():
        colours_checks(page, base)


def colours_checks(page, base):
    print("D. the desktop's colours: the book wears the desktop's tokens, the stock dark set and a light design")
    table = engine_table()
    check(f"D: the engine added to the compare ({UNLISTED_ENGINE}) is one lib/strands.ts ENGINE_COLORS does not name "
          "(and the table was read)", len(table) >= 3 and UNLISTED_ENGINE not in table, sorted(table))
    colours_ready(page, base)
    schemes = []
    code = []
    for name, tokens, full, dark in colour_sets():
        apply_tokens(page, tokens)
        w = {k: token_rgb(v) for k, v in full.items()}
        L = f"D ({name})"
        right = "aside.book-right"
        m = measure(page, {
            "page": (LIVE, "backgroundColor"), "desk": ("body", "backgroundColor"),
            "title": (PAGE_TITLE, "color"), "prose": (f"{LIVE} .is-assistant > div", "color"),
            "head": (f"{LIVE} .book-head", "color"), "label": (f"{right} [data-ctl='count'] > span", "color"),
            "rule": (f"{LIVE} .book-stands", "borderLeftColor"), "k": (f"{LIVE} .book-stands .book-k", "color"),
            "tab": (".book-tabs [data-tab='22']", "backgroundColor"),
            "tab_title": (".book-tabs [data-tab='22'] .book-tab-title", "color"),
            "green": (".book-tabs [data-tab='24']", "backgroundColor"),
            "green_note": (".book-tabs [data-tab='24'] .book-tab-note", "color"),
            "amber": (".book-tabs [data-tab='23']", "backgroundColor"),
            "amber_note": (".book-tabs [data-tab='23'] .book-tab-note", "color"),
            "open_bg": (f"{right} [data-open-latest]", "backgroundColor"),
            "open_fg": (f"{right} [data-open-latest]", "color"),
            "card": (f"{LIVE} [data-run-card]", "backgroundColor"),
            "card_open_bg": (f"{LIVE} [data-run-card] [data-open-version]", "backgroundColor"),
            "card_open_fg": (f"{LIVE} [data-run-card] [data-open-version]", "color"),
            "grid": (f"{right} .book-strand-grid", "stroke"),
            "scroll": (f"{LIVE} [role='log']", "scrollbarColor"),
            "ixtab": ("[data-index-tab]", "backgroundColor"), "ixtab_fg": ("[data-index-tab] b", "color"),
        })
        schemes.append(page.evaluate(
            "() => [document.documentElement.classList.contains('dark'), getComputedStyle(document.documentElement).colorScheme]"))
        # The code in the reply: each highlighted word's colour, on its block.
        c = measure(page, {"bg": (f"{LIVE} .is-assistant pre", "backgroundColor")})
        if c["bg"] is not None and c["bg"][3] < 0.5:
            c["bg"] = m["page"]
        words = page.locator(f"{LIVE} .is-assistant pre code span[style*='--shiki-dark']").evaluate_all(
            "els => els.map(e => getComputedStyle(e).color)")
        c["words"] = [measure_one(page, x) for x in words]
        code.append(c)
        check(f"{L}: the page is the desktop's window colour (--cseries-night), the desk its desktop colour (--cseries-ink)",
              same(m["page"], w["--cseries-night"]) and same(m["desk"], w["--cseries-ink"]), (m["page"], m["desk"]))
        check(f"{L}: the words are the desktop's ink (--cseries-paper), at 4.5:1 or more on the page",
              same(m["title"], w["--cseries-paper"]) and same(m["prose"], w["--cseries-paper"])
              and contrast(m["prose"], m["page"]) >= 4.5, (m["title"], m["prose"], contrast(m["prose"], m["page"])))
        check(f"{L}: quieter words are its --cseries-linen and --cseries-ash, at 3:1 or more on the page",
              same(m["label"], w["--cseries-linen"]) and same(m["head"], w["--cseries-ash"])
              and contrast(m["label"], m["page"]) >= 3 and contrast(m["head"], m["page"]) >= 3,
              (m["label"], m["head"], contrast(m["label"], m["page"]), contrast(m["head"], m["page"])))
        check(f"{L}: the accent is the desktop's: the rule beside 'Where this stands' and the Open button are "
              "--cseries-ember, the label over them --cseries-dusk",
              same(m["rule"], w["--cseries-ember"]) and same(m["open_bg"], w["--cseries-ember"])
              and same(m["k"], w["--cseries-dusk"]), (m["rule"], m["open_bg"], m["k"]))
        check(f"{L}: the Open button is the desktop's primary button (--cseries-ember under --cseries-ink words, "
              "cseries-components.css .cseries-btn.primary), its words at 4:1 or more",
              same(m["open_fg"], w["--cseries-ink"]) and contrast(m["open_fg"], m["open_bg"]) >= 4.0,
              (m["open_fg"], contrast(m["open_fg"], m["open_bg"])))
        check(f"{L}: the run card's own Open version is the same primary button, its words at 4:1 or more on it "
              "(on paper it was white words on white)",
              same(m["card_open_bg"], w["--cseries-ember"]) and same(m["card_open_fg"], w["--cseries-ink"])
              and contrast(m["card_open_fg"], m["card_open_bg"]) >= 4.0,
              (m["card_open_bg"], m["card_open_fg"], contrast(m["card_open_fg"], m["card_open_bg"])))
        check(f"{L}: a lit tab is tinted with the desktop's colour for it, green with --cseries-ember and amber with "
              "--cseries-gold, and its note reads at 4.5:1 or more on the tint",
              dist(m["green"], w["--cseries-ember"]) < dist(m["tab"], w["--cseries-ember"])
              and dist(m["amber"], w["--cseries-gold"]) < dist(m["tab"], w["--cseries-gold"])
              and contrast(m["green_note"], m["green"]) >= 4.5 and contrast(m["amber_note"], m["amber"]) >= 4.5,
              {k: m[k] for k in ("tab", "green", "green_note", "amber", "amber_note")}
              | {"green_note_ratio": contrast(m["green_note"], m["green"]),
                 "amber_note_ratio": contrast(m["amber_note"], m["amber"])})
        check(f"{L}: a tab lies between the page and the desk, its name at 4.5:1 or more; the run card is the "
              "raised panel (--cseries-twilight); the strands' grid is the hairline (--cseries-mist)",
              between(m["tab"], w["--cseries-night"], w["--cseries-ink"]) and contrast(m["tab_title"], m["tab"]) >= 4.5
              and same(m["card"], w["--cseries-twilight"]) and same(m["grid"], w["--cseries-mist"]),
              (m["tab"], contrast(m["tab_title"], m["tab"]), m["card"], m["grid"]))
        # An engine the colour table does not name: its strand, its swatch and
        # its card's edge are one colour, and it reads on the page.
        transitions_over(page)
        o = measure(page, {
            "swatch": (f"aside.book-right button[data-strand='{UNLISTED_ENGINE}'] b i", "backgroundColor"),
            "line": (f"aside.book-right [data-lane='{UNLISTED_ENGINE}'] .book-strand-line", "stroke"),
            "edge": (f"{LIVE} [data-run-card][data-harness='{UNLISTED_ENGINE}']", "borderLeftColor"),
        })
        check(f"{L}: an engine the colour table does not name draws in one colour (its strand, its swatch, its "
              "card's edge), at 3:1 or more on the page",
              all(o.values()) and same(o["swatch"], o["line"]) and same(o["swatch"], o["edge"])
              and contrast(o["swatch"], m["page"]) >= 3.0,
              o | {"page": m["page"], "ratio": contrast(o["swatch"], m["page"])})
        check(f"{L}: the Index tab is the desktop's ink with the page's colour for its word; the scrollbars are the "
              "strong hairline (--cseries-mist-strong)",
              same(m["ixtab"], w["--cseries-paper"]) and same(m["ixtab_fg"], w["--cseries-night"])
              and same(m["scroll"], w["--cseries-mist-strong"]), (m["ixtab"], m["ixtab_fg"], m["scroll"]))
        shot = "dark" if dark else "light"
        page.screenshot(path=str(SHOTS / f"D-colours-{shot}.png"))
        # The run card on the page.
        bring(page, page.locator(f"{SHEET} [data-run-card][data-harness='claude-code']"))
        page.screenshot(path=str(SHOTS / f"D-run-card-{shot}.png"))
        scroll_convo(page, "bottom")

        # The Index over the page, and a dialog.
        page.locator("[data-index-tab]").click()
        ix = page.locator(INDEX)
        ok = True
        try:
            expect(ix).to_be_visible(timeout=3_000)
            expect(page.locator(f"{INDEX} .book-ix-res").first).to_be_visible(timeout=5_000)
            page.wait_for_timeout(500)
        except Exception:
            ok = False
        n = measure(page, {"ix": (INDEX, "backgroundColor"), "res": (f"{INDEX} .book-ix-res", "backgroundColor"),
                           "res_fg": (f"{INDEX} .book-ix-res", "color"),
                           "res_s": (f"{INDEX} .book-ix-res .book-ix-t small", "color")})
        page.screenshot(path=str(SHOTS / f"D-index-{shot}.png"))
        check(f"{L}: the Index is the page's colour, its results the raised panel, their words at 4.5:1 and 3:1 or more",
              ok and same(n["ix"], w["--cseries-night"]) and same(n["res"], w["--cseries-twilight"])
              and contrast(n["res_fg"], n["res"]) >= 4.5 and contrast(n["res_s"], n["res"]) >= 3, n)
        page.keyboard.press("Escape")
        expect(page.locator(INDEX)).to_have_count(0, timeout=3_000)
        expect(page.locator(".book-ghost")).to_have_count(0, timeout=3_000)
        page.locator("aside.book-right [data-archive-page]").click()
        dlg = page.get_by_role("dialog")
        expect(dlg).to_be_visible(timeout=3_000)
        page.wait_for_timeout(300)
        d = measure(page, {"bg": ("[role='dialog']", "backgroundColor"), "fg": ("[role='dialog']", "color")})
        page.screenshot(path=str(SHOTS / f"D-dialog-{shot}.png"))
        check(f"{L}: a dialog is the page's colour (--cseries-night) with the desktop's ink",
              same(d["bg"], w["--cseries-night"]) and same(d["fg"], w["--cseries-paper"]), d)
        dlg.get_by_role("button", name="Cancel").click()
        expect(dlg).to_have_count(0, timeout=3_000)
    check("D: dark or light as the design is: the dark class and the browser's colour scheme are dark on the stock "
          "set and light on the light design (the code blocks' theme and the controls follow them)",
          schemes == [[True, "dark"], [False, "light"]], schemes)
    lowest = [min((contrast(w, x["bg"]) for w in x["words"]), default=0.0) for x in code]
    check("D: code in the Host's words takes the design's dark or light theme: its first word another colour on "
          "each set, and every highlighted word at 3:1 or more on its block",
          len(code) == 2 and all(x["words"] for x in code) and not same(code[0]["words"][0], code[1]["words"][0])
          and all(v >= 3.0 for v in lowest), [(x["words"][:2], x["bg"]) for x in code] + [lowest])


def scenario_colours_phone(page, base):
    print("D. the desktop's colours on a phone: the strip along the bottom and the 'This page' sheet")
    colours_ready(page, base)
    for name, tokens, full, dark in colour_sets():
        apply_tokens(page, tokens)
        w = {k: token_rgb(v) for k, v in full.items()}
        L = f"D ({name}, phone)"
        shot = "dark" if dark else "light"
        page.screenshot(path=str(SHOTS / f"D-colours-{shot}-phone.png"))
        page.locator("[data-sheet-tab]").click()
        expect(page.locator("aside.book-right")).to_be_visible(timeout=3_000)
        page.wait_for_timeout(300)
        m = measure(page, {
            "sheet": ("aside.book-right", "backgroundColor"), "strip": ("[data-book-edge]", "backgroundColor"),
            "tab": (".book-tabs [data-tab='22']", "backgroundColor"),
            "tab_title": (".book-tabs [data-tab='22'] .book-tab-title", "color"),
            "sheettab": ("[data-sheet-tab]", "backgroundColor"), "sheettab_fg": ("[data-sheet-tab] b", "color"),
            "label": ("aside.book-right [data-ctl='count'] > span", "color"),
        })
        page.screenshot(path=str(SHOTS / f"D-sheet-{shot}-phone.png"))
        check(f"{L}: the sheet is the page's colour (--cseries-night), its words the desktop's; the strip lies between "
              "the page and the desk, its tabs' names at 4.5:1 or more",
              same(m["sheet"], w["--cseries-night"]) and same(m["label"], w["--cseries-linen"])
              and between(m["strip"], w["--cseries-night"], w["--cseries-ink"])
              and contrast(m["tab_title"], m["tab"]) >= 4.5 and contrast(m["sheettab_fg"], m["sheettab"]) >= 4.5, m)
        page.locator("[data-sheet-close]").click()
        expect(page.locator("aside.book-right")).to_be_hidden(timeout=3_000)


def shell_takes(value):
    """Whether the shell's token grammar lets a design's value through to the
    frame (dashboard/base/js/desktop-surface.js TOKEN_VALUE_RE, read off the
    file, never typed here)."""
    src = (ROOT / "dashboard" / "base" / "js" / "desktop-surface.js").read_text()
    m = re.search(r"const TOKEN_VALUE_RE = /(.+)/i;", src)
    return bool(m and re.match(m.group(1), value, re.I))


def scenario_colours_odd(page, base):
    print("D. a page colour the browser cannot draw (a digit dropped from the design's hex): dark or light goes by "
          "the desk the page then shows, never by the ink")
    colours_ready(page, base)
    for name, tokens, full, dark in colour_sets():
        # One digit short: the shell's grammar takes it (#[0-9a-f]{3,8}), CSS
        # cannot draw it, so the page is drawn clear over the desk.
        odd = full["--cseries-night"][:-1]
        apply_tokens(page, {**tokens, "--cseries-night": odd})
        w = {k: token_rgb(v) for k, v in full.items()}
        m = measure(page, {"page": (LIVE, "backgroundColor"), "desk": ("body", "backgroundColor")})
        scheme = page.evaluate(
            "() => [document.documentElement.classList.contains('dark'), getComputedStyle(document.documentElement).colorScheme]")
        page.screenshot(path=str(SHOTS / f"D-odd-{'dark' if dark else 'light'}.png"))
        check(f"D ({name}, --cseries-night {odd}): the shell lets the value through and the page is drawn clear over "
              "the desk (--cseries-ink)",
              shell_takes(odd) and m["page"] is not None and m["page"][3] == 0 and same(m["desk"], w["--cseries-ink"]), m)
        check(f"D ({name}, --cseries-night {odd}): dark or light is the desk's, {'dark' if dark else 'light'} (the ink "
              "the page's words are in is the opposite answer)",
              scheme == ([True, "dark"] if dark else [False, "light"]), scheme)


# A Mermaid fence in the Host's words. The Workbench's markdown has no Mermaid
# engine (components/ai-elements/message.tsx streamdownPlugins carries none, and
# the only file that imports @streamdown/mermaid, reasoning.tsx, is imported by
# nothing), so the fence is a listing of its source, as any code is.
MERMAID_REPLY = ("The order of the steps:\n\n```mermaid\nflowchart LR\n  A[Cart] --> B[Shipping] --> C[Totals]\n"
                 "  C --> D{Paid?}\n```")


def scenario_colours_mermaid(page, base):
    print("D. a Mermaid fence in the Host's words, dark and light: a listing of its source (no diagram is drawn), "
          "in the design's ink on its panel")
    colours_ready(page, base, turns=[_turn(901, "assistant", MERMAID_REPLY, 1.0, model="claude-opus-5-5",
                                           provider="anthropic")])
    fence = f"{LIVE} .is-assistant [data-streamdown='code-block'][data-language='mermaid']"
    for name, tokens, full, dark in colour_sets():
        apply_tokens(page, tokens)
        w = {k: token_rgb(v) for k, v in full.items()}
        L = f"D ({name})"
        expect(page.locator(fence)).to_have_count(1, timeout=5_000)
        bring(page, page.locator(fence))
        m = measure(page, {"fg": (f"{fence} code", "color"),
                           "bg": (f"{fence} [data-streamdown='code-block-body']", "backgroundColor")})
        diagrams = page.locator(f"{LIVE} [data-streamdown='mermaid-block'], {LIVE} [data-streamdown='mermaid']").count()
        page.screenshot(path=str(SHOTS / f"D-mermaid-{'dark' if dark else 'light'}.png"))
        check(f"{L}: the fence is a listing of its source, no diagram (the markdown has no Mermaid engine; if one "
              "is added, its theme must follow the design and this check must read the diagram's labels)",
              diagrams == 0 and "flowchart LR" in (page.locator(fence).text_content() or ""), diagrams)
        check(f"{L}: the listing's words are the desktop's ink (--cseries-paper), on a panel that is not clear, at "
              "4.5:1 or more",
              same(m["fg"], w["--cseries-paper"]) and m["bg"] is not None and m["bg"][3] == 1
              and contrast(m["fg"], m["bg"]) >= 4.5, m | {"ratio": contrast(m["fg"], m["bg"])})


# ── This page's Open for the latest finished build ─────────────────────────
LONG_REPLY = ("The shipping step reads its rates once and keeps them; the totals line waits for nothing else. " * 6).strip()


def open_ready(page, base, ago=500.0):
    """Storefront with its compare finished (Claude Code at 372 s, Codex at
    409 s), built for the module checkout, Codex's worktree with no page to
    show; then a long exchange after it, so its cards sit far up the
    conversation, which is at its end."""
    reset()
    bench_start(ago)
    STATE["bench_module"] = "checkout"
    STATE["bench_no_entry"] = {"codex"}
    STATE.setdefault("old_turns", {})[21] = [
        _turn(800 + i, "user", f"And the shipping step, part {i // 2 + 1}?", 1.9 - i * 0.1) if i % 2 == 0 else
        _turn(800 + i, "assistant", LONG_REPLY, 1.9 - i * 0.1, model="claude-opus-5-5", provider="anthropic")
        for i in range(14)]
    open_book(page, base)
    expect(page.locator(f"{SHEET} [data-run-card][data-harness='codex']")).to_have_count(1, timeout=10_000)
    page.wait_for_timeout(1_500)
    scroll_convo(page, "bottom")


def on_screen(page, sel):
    """The element is wholly on screen, nothing covers its middle, and no
    scroller round it was moved to show it."""
    return page.evaluate("""sel => {
        const el = document.querySelector(sel);
        if (!el) return {ok: false, why: 'missing'};
        const r = el.getBoundingClientRect();
        const at = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        const inView = r.width > 0 && r.height > 0 && r.top >= 0 && r.left >= 0
            && r.bottom <= innerHeight && r.right <= innerWidth;
        let s = el.parentElement, scrolled = 0;
        while (s) { scrolled += s.scrollTop; s = s.parentElement; }
        const hit = !!at && el.contains(at);
        return {ok: inView && hit && scrolled === 0, inView, hit, scrolled,
                box: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)]};
    }""", sel)


def version_seen(page, press):
    """Press an Open; what the version window shows: its label, the tab it is
    on, the page in it, and the preview it asked for. None when no window
    opened."""
    asked: list[str] = []

    def note(r):
        if "/preview/" in r.url:
            asked.append(urlsplit(r.url).path)
    page.on("request", note)
    try:
        press()
        view = page.get_by_test_id("version-view")
        expect(view).to_be_visible(timeout=3_000)
        page.wait_for_timeout(900)
        tab = [t for t in ("Preview", "Changes")
               if "bg-foreground" in (view.get_by_role("button", name=t, exact=True).get_attribute("class") or "")]
        frame = view.locator("iframe")
        seen = {"label": view.locator("span.truncate").first.text_content(), "tab": tab,
                "page": (frame.first.get_attribute("srcdoc") or "") if frame.count() else "",
                "files": view.locator("[data-diff-by-file] [data-diff-file]").count(), "asked": sorted(set(asked))}
        seen["page"] = "built." in seen["page"] and re.search(r"The version [\w-]+ built\.", seen["page"]).group(0)
        view.get_by_role("button", name="Close").click()
        expect(view).to_have_count(0, timeout=3_000)
        return seen
    except Exception as e:  # no window: the check that reads this fails
        print(f"        (no version window: {type(e).__name__})")
        return None
    finally:
        page.remove_listener("request", note)


def this_page_open(page, h):
    return page.locator(f"aside.book-right [data-latest-build] [data-open-latest='{h}']")


def press(loc):
    """Press it if it is there; a missing control is a failed check, not a
    scenario that stops."""
    def go():
        if loc.count() == 0:
            raise RuntimeError("not there")
        loc.first.click(timeout=3_000)
    return go


def poll_runs_now(page):
    """The bench reads the runs again at once (OpsPane: when the window
    shows), not at its next quiet poll."""
    page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")


def scenario_open(page, base):
    print("O. This page opens the latest finished build: no scroll up to the run, the card's own Open, and it moves "
          "to a newer build when that one finishes")
    open_ready(page, base)
    right = page.locator("aside.book-right")
    latest = right.locator("[data-latest-build]")
    try:
        expect(latest).to_have_attribute("data-latest-build", BENCH_SID, timeout=8_000)
    except Exception:
        pass
    card_open = card(page, "claude-code").locator("[data-open-version]")
    opens = right.locator("[data-latest-build] [data-open-latest]").evaluate_all("els => els.map(e => e.dataset.openLatest)")
    check("O: 'This page' offers the page's latest finished build, an Open per engine that finished, while that "
          "run's cards sit far up the conversation, which is at its end",
          latest.count() == 1 and latest.get_attribute("data-latest-build") == BENCH_SID
          and opens == ["claude-code", "codex"] and not in_view(page, card_open), (opens, in_view(page, card_open)))
    seen = on_screen(page, "aside.book-right [data-open-latest='claude-code']")
    check("O: the Open is on screen without scrolling: wholly in view, nothing over it, no scroller moved",
          seen.get("ok"), seen)
    page.screenshot(path=str(SHOTS / "O-open-on-this-page.png"))
    ours = version_seen(page, press(this_page_open(page, "claude-code")))
    bring(page, card_open)
    theirs = version_seen(page, press(card_open))
    check("O: it opens what the run card's Open opens: the same version window, label, tab, page and preview",
          ours is not None and ours == theirs and ours["tab"] == ["Preview"]
          and ours["page"] == "The version claude-code built." and ours["label"].endswith(" built for checkout"),
          (ours, theirs))
    # Codex's worktree has no page: the card's rule opens it on the changes.
    scroll_convo(page, "bottom")
    ours = version_seen(page, press(this_page_open(page, "codex")))
    codex_open = card(page, "codex").locator("[data-open-version]")
    bring(page, codex_open)
    theirs = version_seen(page, press(codex_open))
    check("O: a run that left no page opens on its changes from either Open, as the card's rule has it",
          ours is not None and ours == theirs and ours["tab"] == ["Changes"] and ours["files"] > 0, (ours, theirs))

    # A newer run on the page: going, it leaves the Open where it is; finished,
    # the Open moves to it.
    x = {"session_id": "factory-build-812", "harness": "kimi", "t0": time.time() - 20, "state": "running",
         "project_id": 21}
    STATE["extra_sessions"] = [x]
    poll_runs_now(page)
    page.wait_for_timeout(2_500)
    going = latest.get_attribute("data-latest-build") if latest.count() else None
    x["state"] = "done"
    poll_runs_now(page)
    try:
        expect(latest).to_have_attribute("data-latest-build", "factory-build-812", timeout=8_000)
    except Exception:
        pass
    moved = latest.get_attribute("data-latest-build") if latest.count() else None
    page.screenshot(path=str(SHOTS / "O-open-moved.png"))
    newer = version_seen(page, press(this_page_open(page, "kimi")))
    check("O: a newer run still going leaves the Open on the finished one; when it finishes the Open moves to it, "
          "and opens that run's version",
          going == BENCH_SID and moved == "factory-build-812" and newer is not None
          and newer["page"] == "The version factory-build-812 built."
          and newer["asked"] == ["/api/review/sessions/factory-build-812/preview/kimi"], (going, moved, newer))
    # A page with no run shows no Open.
    page.locator(".book-tabs [data-tab='22']").click()
    expect(page.locator(PAGE_TITLE)).to_have_text("Newsletter signup", timeout=5_000)
    expect(page.locator(".book-ghost")).to_have_count(0, timeout=5_000)
    page.wait_for_timeout(1_500)
    check("O: the Open is the page's own: Newsletter signup, which has no run, shows none (Storefront showed its own)",
          moved == "factory-build-812" and right.locator("[data-latest-build]").count() == 0
          and right.locator("[data-open-latest]").count() == 0, right.locator("[data-open-latest]").count())


def scenario_open_live(page, base):
    print("O. a run still going: no Open until an engine finishes, then that engine's")
    reset()
    bench_start(366.0)
    open_book(page, base)
    expect(page.locator("aside.book-right [data-bench]")).to_have_count(1, timeout=10_000)
    page.wait_for_timeout(800)
    before = page.locator("aside.book-right [data-open-latest]").count()
    ok = wait_for(lambda: page.locator("aside.book-right [data-open-latest]").count() > 0, 20)
    after = page.locator("aside.book-right [data-open-latest]").evaluate_all("els => els.map(e => e.dataset.openLatest)")
    codex = bench_state("codex")["state"]
    check("O: no Open while nothing on the page has finished; Claude Code's as soon as it does, with Codex still going",
          before == 0 and ok and after == ["claude-code"] and codex == "running", (before, after, codex))


def scenario_open_phone(page, base):
    print("O. the phone: the Open is in the 'This page' sheet")
    open_ready(page, base)
    sel = "aside.book-right [data-open-latest='claude-code']"
    hidden = not page.locator(sel).is_visible()
    page.locator("[data-sheet-tab]").click()
    expect(page.locator("aside.book-right")).to_be_visible(timeout=3_000)
    page.wait_for_timeout(400)
    seen = on_screen(page, sel)
    page.screenshot(path=str(SHOTS / "O-open-phone.png"))
    check("O (phone): the Open is in the 'This page' sheet: out of sight with the sheet closed, on screen at the "
          "sheet's top when the strip's 'This page' opens it",
          hidden and seen.get("ok") and page.locator("aside.book-right [data-sec='this-page'] [data-open-latest]").count() == 2,
          (hidden, seen))
    ours = version_seen(page, press(page.locator(sel)))
    if page.locator("[data-sheet-close]").is_visible():
        page.locator("[data-sheet-close]").click()
    card_open = card(page, "claude-code").locator("[data-open-version]")
    bring(page, card_open)
    theirs = version_seen(page, press(card_open))
    check("O (phone): it opens what the run card's Open opens",
          ours is not None and ours == theirs and ours["page"] == "The version claude-code built.", (ours, theirs))


PHONE = {"viewport": {"width": 390, "height": 844}, "has_touch": True, "is_mobile": True,
         "device_scale_factor": 2}
REDUCED = {"reduced_motion": "reduce"}


def scenario_boot_list_refused(page, base):
    print("Z. the book's first read of the list is refused (a dropped connection, a kernel reload): it reads the "
          "list again and opens the page")
    reset()
    refused = []

    def refuse_once(route):
        if not refused:
            refused.append(route.request.url)
            return route.abort("connectionrefused")
        return route.continue_()

    page.route(re.compile(r"/api/projects\?limit="), refuse_once)
    open_book(page, base)
    check("Z: the first list read was refused, and the book read it again and opened the page last open",
          len(refused) == 1 and page.locator(TITLE).text_content() == "Storefront", refused)


def scenario_boot_reads_malformed(page, base):
    print("Z. the book's first read of the list, and every read of the old General thread, answer a body that is "
          "not what they should be (null): the boot reads the list again and opens the page; the list poll is "
          "not left waiting")
    reset()
    odd = {"list": 0, "general": 0}

    def null_list_once(route):
        if not odd["list"]:
            odd["list"] += 1
            return route.fulfill(status=200, content_type="application/json", body="null")
        return route.continue_()

    def null_general(route):
        odd["general"] += 1
        return route.fulfill(status=200, content_type="application/json", body="null")

    page.route(re.compile(r"/api/projects\?limit="), null_list_once)
    page.route(re.compile(r"/api/brain/chat\?app_id=workbench"), null_general)
    open_book(page, base)
    check("Z: after a null list and a null General thread the book read the list again and opened the page last open",
          odd["list"] == 1 and odd["general"] >= 1 and page.locator(TITLE).text_content() == "Storefront", odd)

    # The read of the page last open answers null too. It is used after the
    # boot has ended (bootedRef), so a throw there left no page open.
    page.goto("about:blank")
    reset()
    odd["last_open"] = 0

    def null_last_open(route):
        if route.request.method != "GET":
            return route.continue_()
        odd["last_open"] += 1
        return route.fulfill(status=200, content_type="application/json", body="null")

    page.route(re.compile(r"/api/projects/last-open"), null_last_open)
    open_book(page, base)
    check("Z: a null answer for the page last open counts as none: the book opened its newest page",
          odd["last_open"] >= 1 and page.locator(TITLE).text_content() == "Storefront", odd)


def scenario_boot_keeps_turned_page(page, base):
    print("Z. a page turned to while the book's boot still waits on a read stays in front when the boot ends")
    reset()
    STATE["last_open_delay"] = 3.0
    page.goto(f"{base}/system-apps/workbench/index.html", wait_until="domcontentloaded", timeout=15_000)
    # The tabs come from the list, read before the slow last-open answers.
    page.locator(".book-tabs [data-tab='22']").click(timeout=10_000)
    expect(page.locator(TITLE)).to_have_text("Newsletter signup", timeout=5_000)
    turned = time.time()
    page.wait_for_timeout(4_500)  # the slow answer has come, and the boot has ended
    answered = STATE.get("last_open_answered")
    check("Z: the boot ended after the turn and left the page the person turned to in front",
          bool(answered) and turned < answered and page.locator(TITLE).text_content() == "Newsletter signup",
          (turned, answered, page.locator(TITLE).text_content()))


def scenario_boot_hidden_waits(page, base):
    print("Z. a refused boot read is not read again while the window is hidden, and is when it shows")
    reset()
    page.add_init_script("""Object.defineProperty(document, 'hidden', {configurable: true,
        get: () => window.__zHidden !== false});""")
    reads = []

    def refuse_while_hidden(route):
        reads.append(time.time())
        if STATE.get("z_refuse", True):
            return route.abort("connectionrefused")
        return route.continue_()

    page.route(re.compile(r"/api/projects\?limit="), refuse_while_hidden)
    page.goto(f"{base}/system-apps/workbench/index.html", wait_until="domcontentloaded", timeout=15_000)
    page.wait_for_timeout(4_000)  # past the 250, 500, 1000 and 2000 ms retries
    hidden_reads = len(reads)
    STATE["z_refuse"] = False
    page.evaluate("() => { window.__zHidden = false; document.dispatchEvent(new Event('visibilitychange')); }")
    expect(page.locator(TITLE)).to_have_text("Storefront", timeout=10_000)
    page.wait_for_timeout(1_000)  # a second read after the first would have been sent by now
    shown_reads = len(reads) - hidden_reads
    check("Z: while hidden the book read the list once and waited; shown, it read it exactly once more and "
          "opened the page", hidden_reads == 1 and shown_reads == 1,
          f"{hidden_reads} read(s) while hidden, {shown_reads} after it showed")


def scenario_boot_view_after_turn(page, base):
    print("Z. the newest choice wins while the boot still waits: a tab turned to, then a page the desktop asks "
          "for, which stays in front when the boot ends")
    reset()
    STATE["last_open_delay"] = 3.0
    page.goto(f"{base}/system-apps/workbench/index.html", wait_until="domcontentloaded", timeout=15_000)
    page.locator(".book-tabs [data-tab='22']").click(timeout=10_000)
    expect(page.locator(TITLE)).to_have_text("Newsletter signup", timeout=5_000)
    page.wait_for_timeout(900)  # the turn to it has ended
    # The desktop's view (lib/app-view.ts): a top window is its own parent.
    page.evaluate("() => window.postMessage({type: 'cseries:app-view', view: 'project:23'}, '*')")
    sent = time.time()
    expect(page.locator(TITLE)).to_have_text("Photo resizer", timeout=5_000)
    page.wait_for_timeout(4_000)  # the slow answer has come, and the boot has ended
    answered = STATE.get("last_open_answered")
    check("Z: the page the desktop asked for after the person's turn opened before the boot ended, and stayed",
          bool(answered) and sent < answered and page.locator(TITLE).text_content() == "Photo resizer",
          (sent, answered, page.locator(TITLE).text_content()))


def scenario_boot_view_during_turn(page, base):
    print("Z. a page the desktop asks for while a turn is still running is turned to, never dropped")
    reset()
    open_book(page, base)
    # The person turns to 22 and, while that turn runs (a press then is
    # dropped), the desktop asks for 23 (lib/app-view.ts).
    ghosts = page.evaluate("""() => {
        document.querySelector(".book-tabs [data-tab='22']").click();
        window.postMessage({type: 'cseries:app-view', view: 'project:23'}, '*');
        return document.querySelectorAll('.book-ghost').length; }""")
    expect(page.locator(".book-ghost")).to_have_count(0, timeout=5_000)
    page.wait_for_timeout(500)
    check("Z: the desktop's page, asked for during the person's turn, is in front",
          page.locator(TITLE).text_content() == "Photo resizer", (ghosts, page.locator(TITLE).text_content()))


def scenario_boot_closed_all(page, base):
    print("Z. every page closed while the boot still waits: the boot opens none")
    reset()
    for p in STATE["projects"]:
        if p["id"] != 22:
            p["status"], p["group"] = "archived", "archived"
    STATE["last_open"] = "22"
    STATE["last_open_delay"] = 3.0
    page.goto(f"{base}/system-apps/workbench/index.html", wait_until="domcontentloaded", timeout=15_000)
    page.locator(".book-tabs [data-tab='22']").click(timeout=10_000)
    expect(page.locator(TITLE)).to_have_text("Newsletter signup", timeout=5_000)
    arch = page.locator("aside.book-right [data-ctl='archive'] [data-archive-page]")
    arch.click(timeout=5_000)
    page.get_by_role("dialog").get_by_role("button", name="Archive", exact=True).click(timeout=3_000)
    wait_for(lambda: bool(STATE.get("archives")), 5)
    expect(page.locator("[data-book-empty]")).to_be_visible(timeout=5_000)
    closed = time.time()
    page.wait_for_timeout(4_000)  # the slow answer has come, and the boot has ended
    answered = STATE.get("last_open_answered")
    check("Z: the person closed the last page before the boot ended, and the boot left the book empty",
          bool(answered) and closed < answered and page.locator("[data-book-empty]").count() == 1,
          (closed, answered, page.locator(TITLE).count() and page.locator(TITLE).text_content()))


# One gate per group (see the docstring). Each keeps the order the single list
# had, and each runs in a process of its own, so it starts from a fresh mock
# kernel.
GROUPS = {
    "book": ((scenario_book, {}), (scenario_flip, {}), (scenario_reduced, REDUCED),
             (scenario_lit, {}), (scenario_amber, {}), (scenario_seen_on_landing, {}),
             (scenario_reply_written, {}), (scenario_reply_with_build, {}),
             (scenario_report_while_written, {}),
             (scenario_keyboard, {}), (scenario_count, {}), (scenario_nudge, {}), (scenario_nudge_held, {}),
             (scenario_kept_words, {}), (scenario_kept_words, PHONE), (scenario_nudge_edges, {}),
             (scenario_nudge_trim, {}), (scenario_nudge_slow_id, {}), (scenario_nudge_secret, {}),
             (scenario_ime_enter, {}), (scenario_hold, {}), (scenario_phone, PHONE), (scenario_bench, {}),
             (scenario_bench_reduced, REDUCED), (scenario_bench_phone, PHONE)),
    "index": ((scenario_index_typing, {}), (scenario_index_send, {}), (scenario_index_open, {}),
              (scenario_chapters, {}), (scenario_index_reduced, REDUCED),
              (scenario_index_phone, PHONE),
              (scenario_index_new, {}), (scenario_index_new_reduced, REDUCED),
              (scenario_index_new_phone, PHONE), (scenario_index_new_empty, {}),
              (scenario_index_new_late_answer, {}), (scenario_index_new_secret, {}),
              (scenario_index_secret_query, {}), (scenario_index_secret_typed, {}),
              (scenario_switch, {}), (scenario_boot_list_refused, {}),
              (scenario_boot_keeps_turned_page, {}), (scenario_boot_hidden_waits, {}),
              (scenario_boot_view_after_turn, {}), (scenario_boot_view_during_turn, {}),
              (scenario_boot_closed_all, {}), (scenario_boot_reads_malformed, {})),
    "colours": ((scenario_colours, {}), (scenario_colours_phone, PHONE), (scenario_colours_odd, {}),
                (scenario_colours_mermaid, {}),
                (scenario_open, {}), (scenario_open_live, {}), (scenario_open_phone, PHONE)),
}


def main(group: str = "book"):
    if not (APP_DIR / "index.html").is_file():
        print(f"FAIL  no built Workbench at {APP_DIR}/index.html — run `npm run ship` in workbench-web/")
        return 1
    # ONLY picks scenarios by name from every group; otherwise the gate's group.
    scenarios = [s for g in GROUPS.values() for s in g] if ONLY else list(GROUPS[group])
    port = serve()
    base = f"http://127.0.0.1:{port}"
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for fn, opts in scenarios:
            if ONLY and fn.__name__ not in ONLY:
                continue
            ctx = browser.new_context(**{"viewport": {"width": 1440, "height": 900}, **opts})
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            try:
                fn(page, base)
            except Exception as e:  # a scenario that throws is a failed gate, not a crash
                check(f"{fn.__name__}: completed", False, f"{type(e).__name__}: {e}")
                page.screenshot(path=str(SHOTS / f"{fn.__name__}-error.png"))
            check(f"{fn.__name__}: no uncaught page errors", not errors, "; ".join(errors)[:300])
            ctx.close()
        browser.close()
    label = "only" if ONLY else group
    print(f"\nPASS workbench_book ({label})" if not fails else f"\nFAIL workbench_book ({label}): {fails} check(s)")
    if _OWN_SHOTS and not fails:
        shutil.rmtree(SHOTS, ignore_errors=True)
    elif fails or not _OWN_SHOTS:
        print(f"screenshots: {SHOTS}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
