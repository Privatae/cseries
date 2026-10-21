"""
mastercode/pii_queue.py — in-memory pending queue for the PII write path.

Design: reports/2026-08-03-pii-sterilization-platform-design.md §3 (v0.1
amendment: IN-MEMORY queue — the first draft's `pii_pending` TABLE would
have persisted raw text in the pod DB, which is the leak the contract
forbids). With PII mode on, covered write sites hand text to this queue;
the drain worker sterilizes via the home service and invokes the item's
writer with the TOKENIZED text. The write site never writes raw. Service
down → items stay queued and the worker backs off; nothing raw is ever
written as a fallback.

Bounded: past the bound the OLDEST item is dropped and the `dropped`
counter increments ("deferred or dropped, and observable as such").
stats() feeds the honest surface (Context Manager badge or equivalent)
when it ships. A pod restart drops the queue wholesale — inherent to the
in-memory design; the last stats() snapshot is the observability.

Counter semantics:
  pending    — items waiting in the queue right now
  sterilized — items landed tokenized (writer ran)
  failed     — drain ATTEMPTS that failed (sterilize error or writer
               error); the item stays queued either way, so failed counts
               attempts, not lost items — loss is only ever `dropped`
  dropped    — items LOST unlanded (overflow). Never silent.
"""

import asyncio
import collections
import contextlib
import hashlib
import logging
import os
import re

log = logging.getLogger("cseries.mastercode.pii_queue")

# Queue bound. 500 turn-row-sized items is ~minutes of chat backlog; past
# that the pod is long-partitioned from home and dropping oldest (counted)
# beats unbounded memory growth in the entity process.
_BOUND = 500

_queue: collections.deque = collections.deque(maxlen=None)
_counts = {"sterilized": 0, "failed": 0, "dropped": 0}
_worker: asyncio.Task | None = None
_backoff = 1.0
_BACKOFF_MAX = 30.0


def enabled() -> bool:
    """Per-entity enable check. Interim: env PII_MODE=on (one pod = one
    entity, so pod env is per-entity). Default OFF = zero behavior change.
    TODO(Q follow-up): the real per-entity toggle location rides the
    same decision as the key store (see pii_client._api_key)."""
    return os.environ.get("PII_MODE") == "on"


# A message's sender id (session.push_nudge client_id, session.row_client_id;
# the Workbench Index's request id) is a token the CLIENT makes up: 1-64 of
# [A-Za-z0-9_-], room enough for a few words an app packs into it. A row keeps
# it under metadata.client_id (one id, or a list of ids for one turn joined
# from several messages) so a retry of a send can find its words delivered
# (server/connectors/thread_pick.find_index_send). In PII mode a row keeps no
# such token as written: it keeps a digest of each id under
# CLIENT_ID_DIGEST_KEY instead, and a lookup matches by the digest
# (client_id_key). A key of its own, not client_id: the Workbench page reads a
# nudge row's client_id as the id it sent (workbench-web chat-pane.tsx,
# lib/pending-sends sameNudge), which a digest would never equal; with no
# client_id the page matches the row by its words, as before ids were kept.
CLIENT_ID_DIGEST_KEY = "client_id_digest"


def client_id_digest(value):
    """`value`, a sender id or a list of them, as a PII-mode row keeps it:
    each id's sha256 (hex, 64), a list for a list."""
    if isinstance(value, (list, tuple)):
        return [client_id_digest(v) for v in value]
    return hashlib.sha256(b"client_id\0" + str(value).encode("utf-8", "surrogatepass")).hexdigest()


def client_id_key(client_id: str) -> str:
    """What a lookup by the sender id `client_id` matches a row by: the id
    itself, or in PII mode its digest (what the row keeps then)."""
    return client_id_digest(client_id) if enabled() else client_id


# Metadata scrub (review): the queue tokenizes the text
# arg ONLY — sibling fields (metadata/tags on the row) would otherwise land
# RAW in entity_memory.metadata (circles replay's variant_prompt_preview,
# attachment filenames, agent titles). Strip-over-tokenize for those:
# filenames/previews/titles are display hints, not worth a sterilize
# round-trip, and the row stays renderable without them. The keep-set is an
# ALLOWLIST — an unknown future key must fail toward dropped, never leaked.
_STRUCTURAL_META_KEYS = frozenset({
    # row bookkeeping. turn_token is a uuid4 hex minted per turn
    # (session.py) — the chat windows swap their live copy for the row by it.
    "source_kind", "role", "origin", "turn_token",
    # app/thread routing ids (never user prose)
    "app_id", "originating_app", "artifact_id", "cc_thread_id",
    "project_id", "source_context",
    # agent-delivery bookkeeping (agent_title is free text — NOT kept)
    "task_id", "agent_status", "agent_cost_usd",
    # replay bookkeeping (variant_prompt_preview is free text — NOT kept).
    # variant_metrics is numbers and model/provider ids only — the writer
    # (circles._store_replay_variant) drops the failover's error sentence
    # before it lands, so the block carries no prose.
    "replay_of", "variant_model", "variant_metrics",
    # scheduler bookkeeping: ids and flags only. The runner looks the plan
    # entry up by scheduled_event_id before a task runs (turn types step 5),
    # so dropping it here would stop every PII-mode task from running.
    "scheduled_task_id", "scheduled_event_id", "scheduled_plan",
    "scheduled_plan_withdrawn", "task_template_id",
    # a Code Approval's merge line: the approval's number, which the Workbench
    # chat waits for after a merge (code_approvals.announce_merge)
    "code_approval",
    # a turn holding open wrote its reply so far (memory_engine
    # write_turn_row_by_token): a bare true, which the consolidator reads to
    # leave the row alone until the turn rewrites it
    "held",
    # a user row written for words the person sent into a running turn
    # (session.py's drain): a bare true
    "nudge",
    # the digest of a message's sender id, which a row keeps in PII mode in
    # place of the id itself (CLIENT_ID_DIGEST_KEY above; scrub_metadata
    # turns a client_id into it): hex, no user text
    "client_id_digest",
    # which app sent each of a row's ids: "<app>/<digest>" (pending_words
    # .sender_keys, written with the digest in PII mode): an app id and the
    # digest, no user text
    "client_keys",
    # a reply that carried a platform notice (the budget stop line, the stall
    # notice): the notice KINDS ("budget"/"stall", platform words) and the
    # notice's length in characters from the end — no user text — so recall
    # can keep the notice out of the Host's words in PII mode too
    # (memory_engine.host_part)
    "platform_notice", "notice_len",
    # the Host's tool surface on a search-mode turn ("search"; a groups-mode
    # row never carries it) — a platform word, no user text
    "tool_surface",
})

# A search-mode turn's tool chips keep the tool NAMES only (design §3.5): its
# history notes render from them, and a name is a registry id, never user
# text. Every other chip field (args summary, error, digest, next step) can
# carry user text and is dropped. A groups-mode row's chips are dropped whole,
# as before: its trail and pick read nothing new in PII mode.
_CHIP_NAME_KEYS = ("tool", "operate_tool", "read_tool")
# A door's target is the model's own string: only a name-shaped one is kept
# (a sentence written where a tool name goes is dropped, not stored).
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:/\-]{1,80}$")


def _chip_names(chips) -> list:
    out = []
    for c in chips if isinstance(chips, list) else []:
        if isinstance(c, dict):
            kept = {k: c[k] for k in _CHIP_NAME_KEYS
                    if isinstance(c.get(k), str) and _TOOL_NAME_RE.match(c[k])}
            if kept.get("tool"):
                out.append(kept)
    return out

# Attachment descriptors keep only shape/counts; the filename is free text.
_ATTACHMENT_STRUCTURAL_KEYS = ("kind", "media_type", "size")


def scrub_metadata(md):
    """Drop free-text fields from a metadata/tags mapping, keep structural
    keys. Applied by write sites on the queue path (mode ON) only — mode
    OFF never touches the mapping (zero behavior change)."""
    if not isinstance(md, dict):
        return md
    out = {}
    for k, v in md.items():
        if k in _STRUCTURAL_META_KEYS:
            out[k] = v
        elif k == "client_id" and v:
            # A token the client made up: kept as its digest, never as written.
            out[CLIENT_ID_DIGEST_KEY] = client_id_digest(v)
        elif k == "attachments" and isinstance(v, list):
            out[k] = [
                {kk: a[kk] for kk in _ATTACHMENT_STRUCTURAL_KEYS if kk in a}
                if isinstance(a, dict) else {}
                for a in v
            ]
        elif k == "tool_calls" and md.get("tool_surface") in ("search", "groups"):
            # A row that names its surface is a search-capable Host's: its
            # tool NAMES feed the history notes of its later search-mode turns
            # (a person's turn on a search_platform Host runs groups mode, the
            # wake-up after it runs search). An untagged row is a groups Host's.
            out[k] = _chip_names(v)
        # anything not allowlisted is free-text-shaped: dropped, not leaked
    return out


class _Item:
    __slots__ = ("writer", "text", "scope", "verify", "pool", "entity_id", "turn", "landed")

    def __init__(self, writer, text, scope, verify, pool, entity_id, turn=None):
        self.writer = writer      # async callable(tokenized_text) — lands the row
        self.text = text          # raw text, in-memory only, never persisted;
        #                           None for a step (after_turn): nothing to sterilize
        self.scope = scope        # thread:<session_id> | entity:<id>:memory | doc:<uuid>
        self.verify = verify      # verify:true for durable memory (design §3)
        self.pool = pool          # for endpoint resolution at drain time
        self.entity_id = entity_id
        self.turn = turn          # the token of the turn that queued it (_turn_now)
        self.landed = asyncio.Event()   # set once it landed, or was dropped (never to land)


def _turn_now() -> str | None:
    """The token of the turn running in the caller's task (memory_engine.
    CURRENT_TURN_ROW, set from the turn's start), read as a write is queued:
    a move of the turn waits for what the turn queued (wait_turn). None
    outside a turn."""
    try:
        from mastercode.memory_engine import CURRENT_TURN_ROW
        cur = CURRENT_TURN_ROW.get()
    except Exception:  # noqa: BLE001 — an untagged item is only not waited for
        return None
    tok = cur.get("token") if isinstance(cur, dict) else None
    return str(tok) if tok else None


def _append(item: _Item) -> None:
    if len(_queue) >= _BOUND:
        _queue.popleft().landed.set()
        _counts["dropped"] += 1
        log.error("pii queue full (%d) — dropped OLDEST item unlanded "
                  "(dropped total=%d)", _BOUND, _counts["dropped"])
    _queue.append(item)
    _ensure_worker()


async def submit(text, scope: str, writer, verify: bool = False,
                 *, pool=None, entity_id: int | None = None):
    """The one write-site helper. Mode OFF: calls writer(text) directly —
    byte-identical behavior to before the PII path existed. Mode ON:
    enqueues (writer, text, scope, verify) and returns None; the drain
    worker lands the tokenized write. Never raises the caller's write away:
    writer errors surface on the direct path exactly as before."""
    if not enabled() or not (text or "").strip():
        return await writer(text)
    _append(_Item(writer, text, scope, verify, pool, entity_id, turn=_turn_now()))
    return None


def _queued_for(token) -> list:
    return [it for it in list(_queue) if token and it.turn == str(token) and not it.landed.is_set()]


async def wait_turn(token, timeout: float) -> bool:
    """Wait, at most `timeout` seconds, until every write the turn `token`
    queued so far has landed (or was dropped, and so never lands): True then,
    at once when it has none queued (mode OFF queues nothing). False when the
    bound passed with one still queued. A move of the turn waits here, so
    nothing the turn queued before it lands afterwards in the place the turn
    left (runtime/thread_choice.move_exchange, turn_home.rehome_turn). A
    write queued after the call is not waited for."""
    waits = [it.landed.wait() for it in _queued_for(token)]
    if not waits:
        return True
    try:
        await asyncio.wait_for(asyncio.gather(*waits), timeout=timeout)
    except TimeoutError:
        return False
    return True


async def after_turn(token, step, *, pool=None, entity_id: int | None = None) -> None:
    """Run `step()` (async, no arguments) once every write the turn `token`
    queued so far has landed: at once when none is queued, else as the
    queue's own item behind them (one drain worker, oldest first), with no
    words for the service. In memory like every item: a restart drops it
    with the writes it follows. A step that raises is logged and dropped,
    so it never holds the queue. Never raises."""
    async def _run(_text=None):
        try:
            await step()
        except Exception as e:  # noqa: BLE001
            log.error("pii queue step after a turn's writes raised: %s", type(e).__name__)
    if not _queued_for(token):
        await _run()
        return
    _append(_Item(_run, None, "step", False, pool, entity_id, turn=str(token)))


def stats() -> dict:
    """The honest surface: {pending, sterilized, failed, dropped}."""
    return {"pending": len(_queue), **_counts}


def _landed(item: _Item) -> None:
    """`item` landed: off the queue (the head, unless an overflow dropped it
    while it was landing, and then not some other item in its place), and
    no longer waited for."""
    if _queue and _queue[0] is item:
        _queue.popleft()
    else:
        with contextlib.suppress(ValueError):
            _queue.remove(item)
    item.landed.set()


async def drain_once() -> int:
    """Drain ONE queued item: sterilize, then invoke its writer with the
    tokenized text. Returns 1 when an item landed, 0 otherwise. Exported
    (not just the worker's) so the gate test can drive drains
    deterministically (PII_QUEUE_WORKER=off). NEVER raises: a raised drain
    with a non-empty queue is how items strand silently."""
    if not _queue:
        return 0
    try:
        item = _queue[0]
        if item.text is None:
            # A step (after_turn): it runs in queue order, and never raises.
            await item.writer(None)
            _landed(item)
            return 1
        from mastercode import pii_client
        res = await pii_client.sterilize(
            item.text, item.scope, verify=item.verify,
            pool=item.pool, entity_id=item.entity_id,
        )
        if not res.ok:
            # Service down/unauthorized/no output — the item STAYS QUEUED.
            # Deferred, never written raw; the worker backs off.
            _counts["failed"] += 1
            return 0
        try:
            await item.writer(res.text)
        except Exception as e:
            # The tokenized text was safe; the land failed (DB hiccup). Keep
            # the item queued and defer like any other failure — overflow is
            # the only loss path, and it is counted.
            _counts["failed"] += 1
            log.error("pii writer raised (scope=%s) — item stays queued: %s",
                      item.scope, type(e).__name__)
            return 0
        _landed(item)
        _counts["sterilized"] += 1
        log.info("pii drain landed tokenized write (scope=%s, %dms, findings=%d)",
                 item.scope, res.latency_ms, res.findings)
        return 1
    except Exception as e:
        _counts["failed"] += 1
        log.error("pii drain raised unexpectedly — item stays queued: %s",
                  type(e).__name__)
        return 0


def _ensure_worker():
    """Start the background drain worker once per process. Tests set
    PII_QUEUE_WORKER=off and drive drain_once() directly (deterministic,
    no task races)."""
    global _worker
    if os.environ.get("PII_QUEUE_WORKER") == "off":
        return
    if _worker is None or _worker.done():
        _worker = asyncio.create_task(_worker_loop())


async def _worker_loop():
    global _backoff
    while True:
        try:
            if not _queue:
                _backoff = 1.0
                await asyncio.sleep(0.5)
                continue
            landed = await drain_once()
            if landed:
                _backoff = 1.0
                continue  # more items may be waiting — drain back-to-back
            _backoff = min(_backoff * 2, _BACKOFF_MAX)
            await asyncio.sleep(_backoff)
        except asyncio.CancelledError:
            raise  # process shutdown — nothing else may kill this loop
        except Exception as e:
            # A dead drain loop with a non-empty queue
            # strands items SILENTLY (nothing re-submits → nothing
            # re-ensures the worker). The loop must not die; drain_once
            # already never raises, this is the belt for its callers.
            log.error("pii drain worker iteration raised (%s) — worker continues",
                      type(e).__name__)
            await asyncio.sleep(_backoff)
