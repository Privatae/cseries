#!/usr/bin/env python3
"""Embeddings: UNCONFIGURED reads as SETUP, never as an outage.

WHAT THIS PROTECTS.
There is no platform-wide embedding system. Embeddings compute only on an
endpoint the user provides (their own machine, or a provider they hold a key
for). A host with no embedding model configured is therefore in a SETUP state,
and every surface that reads it must say "not set up yet" — never "lane down",
"system-wide", or a raw placeholder.

The stranger interview of a fresh entity produced exactly the wrong sentence:
"embedding lane down (model=: circuit_open)". Three couplings made it:

  1. THE EMPTY-MODEL DIAL. server/routes/context.py resolved model="" and
     passed it straight into _embed_text_config_driven, which dialed the bridge
     with no model, tripped its circuit, and marked the lane DOWN with the
     literal "model=:" in the reason. The guard now lives in
     _embed_text_config_driven itself (every caller, present and future) AND
     in the two context.py callers.

  2. THE CHECK ORDER. memory_retrieval._embed_query asked "is the lane down?"
     before "is a model configured?". Once (1) had poisoned the health gate, a
     merely-unconfigured host reported an outage. Unconfigured is checked first.

  3. THE LANGUAGE. The embedding skill and the embedding_route tool description
     described a "platform endpoint" / "managed lane" the host could fall back
     to. It does not exist (the bridge catalog serves zero models, measured
     once). A host that believes in a platform embedder will describe its
     absence as an outage. The words are gone from the host-facing text.

WHAT THIS DOES NOT CATCH. Parts 1 and 2 execute the real functions with no
pool (the guard sits before any I/O). Part 3 reads source text for phrases; a
rewording that keeps the wrong idea in new words would pass. The durable
protection is the doctrine at each site; this is the cheap tripwire.

Container-free. Run: APP_ROOT=<repo> python3 scripts/test_embed_unconfigured.py
"""
import asyncio
import os
import sys

FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILED
    if ok:
        print(f"  ok   {label}")
        return
    FAILED += 1
    print(f"  FAIL {label}")
    if detail:
        print("       " + detail.replace("\n", "\n       "))


def _read(repo: str, rel: str) -> str:
    with open(os.path.join(repo, rel), encoding="utf-8") as fh:
        return fh.read()


def main() -> int:
    repo = os.environ.get("APP_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo)
    sys.path.insert(0, os.path.join(repo, "brain"))

    print("embeddings — unconfigured is setup, never outage")

    # ── 1. The empty-model dial ─────────────────────────────────────────
    import mastercode.memory_engine as me  # noqa: E402

    me._embed_mark_up()  # clean slate
    for bad in ("", None, "   "):
        vec = asyncio.run(me._embed_text_config_driven(
            "hello", bad, entity_id=None, pool=None))
        check(f"empty model {bad!r} embeds nothing", vec is None, repr(vec))
    health = me.embedding_health()
    check("empty model never marks the lane down",
          not health.get("down") and not health.get("reason"),
          repr(health))

    # The three mark-down reasons carry the model NAME, never a "model=" tag —
    # the reason is repeated verbatim to the host and then to the user.
    src_me = _read(repo, "brain/mastercode/memory_engine.py")
    check("no 'model={model}' placeholder in any mark-down reason",
          'f"model={model}' not in src_me)

    # ── 2. The check order ──────────────────────────────────────────────
    import mastercode.memory_retrieval as mr  # noqa: E402

    async def _none(*_a, **_k):
        return None

    real_get = me._get_memory_embedding
    me._get_memory_embedding = _none
    try:
        # Poison the health gate the way the old bug did, with NO model set.
        me._embed_mark_down("x: circuit_open")
        me._last_embed_model = ""
        vec, why = asyncio.run(mr._embed_query(None, 1, "anything"))
        check("unconfigured wins over a poisoned health gate",
              vec is None and (why or "").startswith("embeddings not set up yet"),
              repr(why))
        for banned in ("lane down", "system-wide", "model=:", "outage"):
            check(f"unconfigured reason never says {banned!r}",
                  banned not in (why or ""), repr(why))
        check("unconfigured reason says nothing is broken and how one is set up "
              "(a fact, never an instruction to offer)",
              "Nothing is broken" in (why or "") and "can be set up" in (why or "")
              and "offer" not in (why or ""),
              repr(why))

        # Configured model, endpoint genuinely not answering → names the model.
        me._last_embed_model = "qwen3-embedding:4b"
        me._embed_mark_down("qwen3-embedding:4b: circuit_open")
        vec, why = asyncio.run(mr._embed_query(None, 1, "anything"))
        check("configured-but-unreachable names the endpoint, not a 'lane'",
              vec is None and (why or "").startswith("embedding endpoint not answering")
              and "qwen3-embedding:4b" in (why or ""),
              repr(why))
        check("configured-but-unreachable never says 'lane down'",
              "lane down" not in (why or ""), repr(why))
    finally:
        me._get_memory_embedding = real_get
        me._embed_mark_up()
        me._last_embed_model = ""

    # A config that could not be READ (or a brain that is not up yet) is not
    # "unconfigured": the reader must not say "nothing is broken" over it.
    asyncio.run(me._entity_embedding_config(None, None))
    check("no pool marks the config unreadable, not unconfigured",
          me.embed_config_unreadable() is True)
    me._get_memory_embedding = _none
    try:
        me._last_embed_model = ""
        vec, why = asyncio.run(mr._embed_query(None, 1, "anything"))
        check("unreadable config reports a transient read failure, not setup",
              vec is None and "could not be read" in (why or "")
              and "Nothing is broken" not in (why or ""), repr(why))
    finally:
        me._get_memory_embedding = real_get
        me._embed_config_unreadable = False

    # The two direct callers guard too (defense in depth; the engine guard is
    # the one that binds).
    src_ctx = _read(repo, "server/routes/context.py")
    check("context.py guards both embed callers on an empty model",
          src_ctx.count("if not model:") >= 2,
          f"found {src_ctx.count('if not model:')} guard(s)")

    # ── 3. The language ─────────────────────────────────────────────────
    banned_phrases = ("platform endpoint", "managed lane", "platform default",
                      "platform MANAGED lane", "works out of the box",
                      "lane is down", "lane down")
    for rel in ("default-skills/embedding.md",
                "default-skills/user-documents.md"):
        text = _read(repo, rel).lower()
        for phrase in banned_phrases:
            check(f"{rel} does not say {phrase!r}", phrase.lower() not in text)
        check(f"{rel} says embeddings can be 'not set up yet'",
              "not set up yet" in text)

    src_reg = _read(repo, "brain/mastercode/public_tool_registry.py")
    start = src_reg.find('"embedding_route": {')
    end = src_reg.find('"embed_jobs": {', start)
    block = src_reg[start:end] if start >= 0 and end > start else ""
    check("embedding_route tool description located", bool(block))
    for phrase in ("platform MANAGED lane", "works out of the box",
                   "platform managed lane", "move back to the platform",
                   "platform-served"):
        check(f"embedding_route description does not say {phrase!r}",
              phrase not in block)
    check("embedding_route description states there is no platform embedder",
          "NO PLATFORM EMBEDDER" in block or "no platform embedder" in block)
    # The three catalog-backed ops are gone from the schema: the catalog they
    # consulted serves nothing, so `switch` refused every model and
    # `use_managed` was a route to platform compute dressed as "clear".
    check("embedding_route schema enum carries no catalog-backed op",
          all(f'"{o}"' not in block.split('"enum": [', 1)[-1].split("]", 1)[0]
              for o in ("catalog", "switch", "use_managed")),
          block.split('"enum": [', 1)[-1].split("]", 1)[0])

    # ── 4. The retired ops refuse before any I/O and name the real paths ──
    try:
        from mastercode.runtime import tools as _tools  # noqa: E402
        for op in ("catalog", "switch", "use_managed"):
            r = asyncio.run(_tools._exec_embedding_route({"op": op, "model": "x"}))
            txt = str((r or {}).get("result") or "")
            check(f"op={op!r} is refused as retired",
                  (r or {}).get("status") == "error" and "retired" in txt, repr(r))
            check(f"op={op!r} refusal names the real setup paths",
                  "use_bastion" in txt and "Orchestrator" in txt
                  and "no platform embedder" in txt, txt[:200])
            for banned in ("lane down", "system-wide", "outage"):
                check(f"op={op!r} refusal never says {banned!r}",
                      banned not in txt, txt[:200])
    except ImportError as exc:  # pragma: no cover — tree without the runtime deps
        check("mastercode.runtime.tools importable for the retired-op gate",
              False, str(exc))

    src_brain = _read(repo, "server/routes/brain.py")
    check("memory stats coverage carries configured + a setup note",
          '"configured": _emb_configured' in src_brain
          and "aren't set up yet" in src_brain)
    check("memory stats never calls a failed config read 'not set up'",
          "_emb_configured is False" in src_brain)

    # The two host-prompt lines that still said "embedding lane is down".
    src_sess = _read(repo, "brain/mastercode/runtime/session.py")
    check("session.py host prompt no longer says 'embedding lane is down'",
          "embedding lane is" not in src_sess and "lane is down" not in src_sess)

    print()
    if FAILED:
        print(f"FAIL embed_unconfigured: {FAILED} check(s) failed")
        return 1
    print("PASS embed_unconfigured: unconfigured reads as setup, never outage")
    return 0


if __name__ == "__main__":
    sys.exit(main())
