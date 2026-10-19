#!/usr/bin/env python3
"""Platform notices never read as the Host's words, by ANY reader.

Container-free: APP_ROOT=<repo> python3 scripts/test_platform_notices_never_host_words.py.
Stand-in pools; no pod, no DB, no model, no network.

  - rows written before this change: main's two fixed stall texts (exact)
    are a stall note, never the Host's words; a Host quoting one mid-reply
    is untouched
  - PII mode: platform_notice and notice_len survive the scrub, and recall
    still splits the notice off
  - the real session history path keeps a note-only reply as its note
    (kills mEmptyFilter)
  - history never starts on a platform note
  - the rolling summary's input carries the platform note
  - a local route stores server text only for vLLM's own templates

The batch consolidator's prompt and stamp went with the batch consolidator:
memory forms in the turn, so no background extraction prompt is left to keep
notices out of.

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
        print(f"[g1-round12] FAIL {name} {extra}")


from mastercode import memory_engine as ME  # noqa: E402

STOP = "This turn reached its budget before a reply was written. The choice to go on is in the question box above."
NOTICE_ROW = "Checking the dock.\n\n" + STOP
S1 = ("I hit a snag — the model returned its reasoning but no actual reply, twice in a row. "
      "Nothing was lost and no work was undone.")
S2 = "The open choice is in the question box above."


def row(i, role, content, extra=None):
    return {"id": i, "content": content, "metadata": json.dumps({"role": role, **(extra or {})}),
            "created_at": None, "project_id": None, "source_session": "s"}


class P:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    async def fetch(self, q, *a):
        return list(reversed(self.rows))

    async def execute(self, q, *a):
        self.executed.append(q)
        return "OK"

    async def fetchrow(self, q, *a):
        return None

    async def fetchval(self, q, *a):
        return None


def recall(rows):
    return asyncio.run(ME.recall_recent_turns(P(rows), 1, "s"))


# ── old rows (no model_content) ────────────────────────────────────────
t = recall([row(5, "assistant", S1)])
check("an old row that IS main's stall text reads as nothing of the Host's, plus a stall note",
      t and t[0]["content"] == "" and t[0].get("platform_note") == ["stall"], t)
t = recall([row(6, "assistant", "Checking the dock.\n\n" + S2)])
check("narration followed by main's other fixed text keeps the narration, plus a stall note",
      t and t[0]["content"] == "Checking the dock." and t[0].get("platform_note") == ["stall"], t)
_quote = "I told you earlier: " + S1 + " Then I fixed it."
t = recall([row(7, "assistant", _quote)])
check("a Host that quotes the sentence mid-reply is untouched (exact match only)",
      t and t[0]["content"] == _quote and not t[0].get("platform_note"), t)

# ── PII mode ───────────────────────────────────────────────────────────
from mastercode import pii_queue as PQ  # noqa: E402
_tags = {"role": "assistant", "model_content": "Checking the dock.", "platform_notice": ["budget"],
         "notice_len": len(NOTICE_ROW) - len("Checking the dock.")}
_scr = PQ.scrub_metadata(_tags)
check("the PII scrub keeps platform_notice and notice_len (no user text) and drops model_content",
      _scr.get("platform_notice") == ["budget"] and isinstance(_scr.get("notice_len"), int)
      and "model_content" not in _scr, _scr)
_tok = "Checking the [PERSON_1] dock."           # PII mode stores the tokenized words
_pii_row = _tok + "\n\n" + STOP
t = recall([row(8, "assistant", _pii_row, _scr)])
check("in PII mode recall still splits the notice off (counted from the end) and keeps the note",
      t and t[0]["content"] == _tok and t[0].get("platform_note") == ["budget"], t)

# ── the REAL session history path keeps a note-only reply's note ────────
T = os.path.join(APP_ROOT, "scripts", "test_turn_cut_output.py")
_src = open(T).read()
_g = {"__file__": T, "__name__": "round12"}
exec(compile(_src.split("# 1 + 2: the cut tool call")[0], "harness", "exec"), _g)
Harness = _g["Harness"]
_hist_rows = [row(10, "user", "Build the dock."),
              row(11, "assistant", "This turn reached its budget before a reply was written.",
                  {"model_content": "", "platform_notice": ["budget"], "notice_len": 57}),
              row(12, "user", "Go on with the dock."),
              row(13, "assistant", NOTICE_ROW, {"model_content": "Checking the dock.", "platform_notice": ["budget"]})]
_real_recall = ME.recall_recent_turns


async def _recall_rows(pool, entity_id, session_id, **kw):
    return await _real_recall(P(_hist_rows), entity_id, session_id, **kw)
ME.recall_recent_turns = _recall_rows
try:
    h = asyncio.run(Harness([{"content": "Done."}]).run("Where were we?"))
finally:
    ME.recall_recent_turns = _real_recall
_first = h.calls[0] if h.calls else []
_asst = [m.get("content") for m in _first if m.get("role") == "assistant"]
_notes = [m.get("content") for m in _first if m.get("role") == "system"
          and m.get("content") == ME.PLATFORM_NOTES["budget"]]
check("through the real history path, a note-only stopped reply survives as its platform note "
      "(two notes, one per stopped turn), and no assistant message carries a notice",
      len(_notes) == 2 and "Checking the dock." in _asst
      and all("reached its budget" not in (a or "") for a in _asst), (_asst, len(_notes)))

# ── history never starts on a platform note ────────────────────────────
_hm = ME.history_messages([{"role": "assistant", "content": "", "platform_note": ["budget"]},
                           {"role": "user", "content": "Go on."}])
check("a cut that starts on a note-only reply drops the leading note (no history-first system note)",
      _hm and _hm[0] == {"role": "user", "content": "Go on."}, _hm)

# ── the rolling summary's input carries the note ───────────────────────
_tc_src = open(os.path.join(APP_ROOT, "brain/mastercode/thread_consolidator.py")).read()
check("the rolling summary reads recalled turns through history_messages (the platform note rides along)",
      "turns = _hm(turns)" in _tc_src)

# ── a local route stores server text only for vLLM's templates ──────────
from mastercode import provider_gateway as PG  # noqa: E402


def _local(body):
    class _R:
        status = 400

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def text(self):
            return body

    class _S:
        def post(self, *a, **kw):
            return _R()

    async def _gs(_b):
        return _S()
    saved = PG._get_session
    PG._get_session = _gs
    try:
        return asyncio.run(PG._call_local([{"role": "user", "content": "x"}], "m", base_url="http://10.0.0.5:8000"))
    finally:
        PG._get_session = saved

_echo = _local(json.dumps({"message": "Bad request: input 'my notes about the context window and SECRET' rejected"}))
check("a non-template local error that echoes input mentioning 'context window' stores only its status",
      _echo.get("error") == "Local model error: 400", _echo.get("error"))
for _tpl in ("This model's maximum context length is 32768 tokens. However, your request has 32836 input tokens. "
             "Please reduce the length of the input messages.",
             "This model's maximum context length is 8192 tokens. However, you requested 9000 tokens in the input "
             "for embedding generation. Please reduce the length of the input."):
    _r = _local(json.dumps({"message": _tpl}))
    check(f"vLLM's own template is kept: {_tpl[:60]}…", _tpl[:40] in str(_r.get("error")), _r.get("error"))

if FAILS:
    print(f"\n{len(FAILS)} FAIL")
    sys.exit(1)
print("\nall pass")
