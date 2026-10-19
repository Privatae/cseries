#!/usr/bin/env python3
"""The `recall` tool and
the search route (POST /api/search) never hand out a platform notice as the
Host's words. A turn row's notice is named apart in `platform_notice`, split
by the one reader every other path uses (memory_engine.host_part).

Container-free: APP_ROOT=<repo> python3 scripts/test_recall_notice_not_host_words.py.
A stand-in pool answers the lexical arm; no pod, no DB, no model, no network.

  a  a row with model_content: the search route's hit carries the Host's
     words as content and the notice kind in platform_notice
  b  an old-shape row (main's fixed stall text, no model_content): the same
  c  a row whose model_content is EMPTY (notice only): content is empty
  d  rows with no notice, and a non-turn row ending in the stall text, are
     untouched
  e  the recall tool's text: the notice is a tag, never in the Host's text

Exit 0 = all pass; exit 1 + FAIL lines otherwise.
"""

import asyncio
import json
import os
import sys

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))

FAILS: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"[g1-r12-n4] FAIL {name} {extra}")


from mastercode import memory_retrieval as MR  # noqa: E402

STOP = ("This turn reached its budget before a reply was written. "
        "The choice to go on is in the question box above.")
STALL = ("I hit a snag — the model returned its reasoning but no actual reply, twice in a row. "
         "Nothing was lost and no work was undone.")
WORDS = "Checking the dock schedule."


def row(i, content, md, memory_type="turn"):
    return {"id": i, "content": content, "memory_type": memory_type, "importance": 0.5,
            "confidence": 1.0, "access_count": 0, "status": "active",
            "metadata": json.dumps(md), "created_at": None, "project_id": None,
            "sctx_kind": "thread", "sctx_ref": "t1", "lex_rank": 0.5 - i / 100}


ROWS = [
    # a: written by this branch — model_content holds the Host's words
    row(1, WORDS + "\n\n" + STOP,
        {"role": "assistant", "model_content": WORDS, "platform_notice": ["budget"],
         "notice_len": len("\n\n" + STOP)}),
    # b: an old row — main wrote the stall text into the reply, no tags
    row(2, "Dock one is free.\n\n" + STALL, {"role": "assistant"}),
    # c: a reply that was only the notice
    row(3, STOP, {"role": "assistant", "model_content": "", "platform_notice": ["budget"],
                  "notice_len": len(STOP)}),
    # d: untouched
    row(4, "The dock opens at nine.", {"role": "assistant"}),
    row(5, "Ask about the dock.\n\n" + STALL, {"role": "user"}),
    row(6, "Dock notes.\n\n" + STALL, {"role": "assistant"}, memory_type="preference"),
]


class Pool:
    async def fetch(self, sql, *args):
        if "search_tsv" in sql:
            return [dict(r) for r in ROWS]
        return []


async def _no_embed(pool, entity_id, query):
    return None, "no embedder (stand-in)"


MR._embed_query = _no_embed
MR._read_pool = lambda p: p
MR._priv_pool = lambda p: p


async def main():
    out = await MR.unified_search(Pool(), 1, "dock", stores=["memory"], limit=10)
    hits = {h["id"]: h for h in (out.get("memory") or {}).get("hits") or []}
    check("search route returned the six rows", sorted(hits) == [1, 2, 3, 4, 5, 6],
          repr(out.get("memory"))[:300])

    h = hits.get(1) or {}
    check("a: model_content row — content is the Host's words", h.get("content") == WORDS,
          repr(h.get("content")))
    check("a: model_content row — notice named apart", h.get("platform_notice") == ["budget"],
          repr(h.get("platform_notice")))
    h = hits.get(2) or {}
    check("b: old-shape row — content is the Host's words", h.get("content") == "Dock one is free.",
          repr(h.get("content")))
    check("b: old-shape row — stall notice named apart", h.get("platform_notice") == ["stall"],
          repr(h.get("platform_notice")))
    h = hits.get(3) or {}
    check("c: notice-only row — no Host words, notice named apart",
          h.get("content") == "" and h.get("platform_notice") == ["budget"], repr(h))
    for i, want in ((4, "The dock opens at nine."), (5, "Ask about the dock.\n\n" + STALL),
                    (6, "Dock notes.\n\n" + STALL)):
        h = hits.get(i) or {}
        check(f"d: row {i} untouched", h.get("content") == want and "platform_notice" not in h,
              repr(h.get("content")))

    fed = await MR.federated_recall(Pool(), 1, "dock", scope="memory", limit=10)
    text = MR.format_recall_text(fed)
    check("e: recall text never carries the budget stop line", STOP not in text, text[:600])
    check("e: recall text carries the old row's words without the stall text",
          "Dock one is free." in text and text.count(STALL) == 2, text[:900])
    check("e: recall text names the notice in the hit's tags",
          "platform notice: budget" in text and "platform notice: stall" in text, text[:600])


asyncio.run(main())


# f: the vector arm hands out the same split — it reads
# its own rows, so it is checked on its own. (The graph arm, g, went with the
# memory graph.)
class ArmPool:
    def __init__(self, rel):
        self.rel = rel

    async def fetch(self, sql, *args):
        return [dict(r, **{self.rel: 0.9}) for r in ROWS[:3]]

    async def execute(self, sql, *args):
        return "OK"

    def acquire(self):
        p = self

        class _C:
            async def __aenter__(self):
                return p

            async def __aexit__(self, *e):
                return False
        return _C()

    transaction = acquire


async def _col(pool, entity_id):
    return "embedding", 1024


async def arms():
    from mastercode import memory_engine as ME
    ME.active_embedding_column = _col
    vh, ran, _ = await MR._vector_arm(ArmPool("relevance"), 1, "dock", where_sql="true", params=[],
                                      candidates=10, vec=[0.1] * 4)
    for lbl, hits in (("vector", vh),):
        by = {h["id"]: h for h in hits}
        check(f"f: {lbl} arm — the stopped reply is the Host's words, the notice named apart",
              (by.get(1) or {}).get("content") == WORDS and (by.get(1) or {}).get("platform_notice") == ["budget"],
              repr(by.get(1))[:300])
        check(f"f: {lbl} arm — the old-shape row's stall text is named apart",
              (by.get(2) or {}).get("content") == "Dock one is free."
              and (by.get(2) or {}).get("platform_notice") == ["stall"], repr(by.get(2))[:300])
        check(f"f: {lbl} arm — a notice-only row comes back (not dropped) with empty words and the notice",
              3 in by and by[3].get("content") == "" and by[3].get("platform_notice") == ["budget"],
              repr(by.get(3))[:300])


asyncio.run(arms())
if FAILS:
    print(f"[g1-r12-n4] {len(FAILS)} FAIL")
    sys.exit(1)
print("[g1-r12-n4] all pass")
