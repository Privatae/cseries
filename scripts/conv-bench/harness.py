#!/usr/bin/env python3
"""Conversation-depth benchmark harness.

Replays LoCoMo conversations through three context strategies and measures
QA accuracy + token cost:

  truncated — today's cseries behavior: last 8000 chars of verbatim turns.
  curated   — the thread consolidator: rolling summary (rewritten every
              CONSOLIDATE_EVERY turns by the model, corrections rule
              included) + last 8000 chars verbatim.
  full      — the whole thread (upper bound).

Scoring: token-overlap F1 against the gold answer (no judge calls — the key
budget goes to context calls, not scoring). Token usage is summed from the
API's own usage fields: QA calls for all strategies, plus consolidator
calls for curated.

Usage: KIMI_API_KEY=sk-... python3 harness.py [--smoke] [--convs 0,1] [--questions 30]
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request

BASE = "https://api.kimi.com/coding/v1/chat/completions"
MODEL = "kimi-for-coding"
KEY = os.environ.get("KIMI_API_KEY", "")

RECENT_CHARS = 8000            # cseries parity (session.py max_hist); --recent-chars overrides
SUMMARY_TARGET = "under 250 words"
CONSOLIDATE_EVERY = 60         # turns between consolidations; --cadence overrides

CONSOLIDATOR_PROMPT = """You maintain the rolling summary of a long conversation between {a} and {b}.

Previous summary:
{prev}

Transcript since (and overlapping) the previous summary — oldest to newest:
{window}

Rewrite the summary to carry forward: decisions made, open threads, facts established about each person, plans and dates.
CORRECTIONS RULE: when newer messages contradict or replace older ones, the summary carries ONLY the current position, and notes that a correction happened. Never keep both versions.
Be dense and factual, {target}. Write in third person. Output ONLY the summary."""

QA_PROMPT = """You are answering questions about a conversation between two people. Use ONLY the provided context. If the answer is not in the context, say "I don't know". Answer as briefly as possible — a few words or a date, not sentences.

CONTEXT:
{ctx}

QUESTION: {q}
ANSWER:"""


def call(messages, max_tokens=1200, retries=3):
    body = json.dumps({"model": MODEL, "messages": messages,
                       "max_tokens": max_tokens}).encode()
    for attempt in range(retries):
        req = urllib.request.Request(BASE, data=body, headers={
            "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.loads(r.read())
            choice = d["choices"][0]
            msg = choice["message"]
            usage = d.get("usage") or {}
            content = (msg.get("content") or "").strip()
            # Thinking models can spend the whole budget on reasoning and
            # return finish_reason=length with EMPTY content — that's a
            # truncated call, not an answer; retry with a bigger budget so
            # an empty pred never masquerades as a wrong answer.
            if not content and choice.get("finish_reason") == "length" and max_tokens < 4000:
                max_tokens *= 2
                body = json.dumps({"model": MODEL, "messages": messages,
                                   "max_tokens": max_tokens}).encode()
                continue
            return content, usage
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(3 * (attempt + 1))
    return "", {}


def flatten(conv):
    """Chronological turn list with session date markers."""
    sessions = sorted(
        (k for k in conv if re.fullmatch(r"session_\d+", k)),
        key=lambda k: int(k.split("_")[1]))
    turns = []
    for s in sessions:
        dt = conv.get(s + "_date_time", "")
        if dt:
            turns.append(f"[{s} — {dt}]")
        for t in conv[s]:
            turns.append(f"{t['speaker']}: {t['text']}")
    return turns


def tail_chars(turns, limit):
    out, total = [], 0
    for t in reversed(turns):
        if total + len(t) > limit:
            break
        out.append(t)
        total += len(t)
    return "\n".join(reversed(out))


def f1(pred, gold):
    norm = lambda s: re.findall(r"[a-z0-9]+", s.lower())
    p, g = norm(pred), norm(gold)
    if not p or not g:
        return 0.0
    common = 0
    gs = list(g)
    for w in p:
        if w in gs:
            gs.remove(w)
            common += 1
    if not common:
        return 0.0
    prec, rec = common / len(p), common / len(g)
    return 2 * prec * rec / (prec + rec)


def run_conv(ci, conv_obj, n_questions, only=None):
    conv = conv_obj["conversation"]
    spk = conv.get("speaker_a", "A"), conv.get("speaker_b", "B")
    turns = flatten(conv)
    print(f"  conv {ci}: {len(turns)} turns, speakers {spk}")

    # --- curated: replay with periodic consolidation ---
    summary = "(nothing yet)"
    consolidator_tokens = {"prompt": 0, "completion": 0, "calls": 0}
    for i in range(0, len(turns), CONSOLIDATE_EVERY):
        window = "\n".join(turns[i:i + CONSOLIDATE_EVERY])
        text, usage = call([{"role": "user", "content": CONSOLIDATOR_PROMPT.format(
            a=spk[0], b=spk[1], prev=summary, window=window,
            target=SUMMARY_TARGET)}])
        if text:
            summary = text
        consolidator_tokens["prompt"] += usage.get("prompt_tokens", 0)
        consolidator_tokens["completion"] += usage.get("completion_tokens", 0)
        consolidator_tokens["calls"] += 1
    print(f"  consolidator: {consolidator_tokens['calls']} calls, "
          f"{consolidator_tokens['prompt']} prompt tokens")

    contexts = {
        "truncated": tail_chars(turns, RECENT_CHARS),
        "curated": f"[Conversation summary so far]\n{summary}\n\n[Recent messages]\n" + tail_chars(turns, RECENT_CHARS),
        "full": "\n".join(turns),
    }
    if only:
        contexts = {k: v for k, v in contexts.items() if k in only}
    for name, ctx in contexts.items():
        print(f"  context {name}: ~{len(ctx) // 4} tokens")

    # Stratified sample: equal shares per category (the dataset's first-N
    # skews to early categories and would bias the comparison).
    by_cat: dict[int, list] = {}
    for q in conv_obj["qa"]:
        by_cat.setdefault(q["category"], []).append(q)
    per = max(1, n_questions // max(len(by_cat), 1))
    qa = []
    for cat in sorted(by_cat):
        bucket = by_cat[cat]
        step = max(1, len(bucket) // per)
        qa.extend(bucket[::step][:per])
    qa = qa[:n_questions]
    results = {name: [] for name in contexts}
    tokens = {name: {"prompt": 0, "completion": 0} for name in contexts}
    for name, ctx in contexts.items():
        for q in qa:
            ans, usage = call([{"role": "user", "content": QA_PROMPT.format(
                ctx=ctx, q=q["question"])}], max_tokens=600)
            # Category 5 = adversarial: the question is NOT answered in the
            # conversation and 'adversarial_answer' is the tempting WRONG
            # answer. Score = resisted the trap (low overlap with the wrong
            # answer, incl. honest "I don't know") = 1, took the bait = 0.
            if q["category"] == 5:
                gold = str(q.get("adversarial_answer", ""))
                score = 1.0 if f1(ans, gold) < 0.3 else 0.0
            else:
                gold = str(q["answer"])
                score = f1(ans, gold)
            results[name].append({
                "q": q["question"], "gold": gold, "pred": ans,
                "f1": score, "cat": q["category"]})
            tokens[name]["prompt"] += usage.get("prompt_tokens", 0)
            tokens[name]["completion"] += usage.get("completion_tokens", 0)
            print(".", end="", flush=True)
        avg = sum(r["f1"] for r in results[name]) / max(len(results[name]), 1)
        print(f"\n  {name}: F1={avg:.3f} over {len(results[name])} questions, "
              f"prompt_tokens={tokens[name]['prompt']}", flush=True)
        # Checkpoint after every strategy — a rate-limit kill mid-run must
        # never take a whole conversation's spend with it.
        with open(os.path.join(os.path.dirname(__file__),
                               f"checkpoint_conv{ci}.json"), "w") as f:
            json.dump({"conv": ci, "results": results, "qa_tokens": tokens,
                       "consolidator_tokens": consolidator_tokens,
                       "context_chars": {k: len(v) for k, v in contexts.items()}},
                      f, indent=1)
    return {"conv": ci, "results": results, "qa_tokens": tokens,
            "consolidator_tokens": consolidator_tokens,
            "context_chars": {k: len(v) for k, v in contexts.items()}}


def main():
    global RECENT_CHARS, CONSOLIDATE_EVERY
    smoke = "--smoke" in sys.argv
    n_q = 5 if smoke else int(sys.argv[sys.argv.index("--questions") + 1]) if "--questions" in sys.argv else 30
    convs = [0] if smoke else ([int(x) for x in sys.argv[sys.argv.index("--convs") + 1].split(",")]
                               if "--convs" in sys.argv else [0, 1])
    if "--recent-chars" in sys.argv:
        RECENT_CHARS = int(sys.argv[sys.argv.index("--recent-chars") + 1])
    if "--cadence" in sys.argv:
        CONSOLIDATE_EVERY = int(sys.argv[sys.argv.index("--cadence") + 1])
    # --strategies truncated,curated,full — for settings sweeps run only
    # "curated" so baselines aren't re-bought on every dial turn.
    only = (sys.argv[sys.argv.index("--strategies") + 1].split(",")
            if "--strategies" in sys.argv else None)
    data = json.load(open(os.path.join(os.path.dirname(__file__), "locomo10.json")))
    out = []
    for ci in convs:
        out.append(run_conv(ci, data[ci], n_q, only))
        with open(os.path.join(os.path.dirname(__file__), "results.json"), "w") as f:
            json.dump(out, f, indent=1)
    # aggregate
    print("\n== AGGREGATE ==")
    names = sorted({s for c in out for s in c["results"]})
    for name in names:
        rs = [r for c in out for r in c["results"][name]]
        if not rs:
            continue
        avg = sum(r["f1"] for r in rs) / len(rs)
        ptoks = sum(c["qa_tokens"][name]["prompt"] for c in out)
        extra = sum(c["consolidator_tokens"]["prompt"] + c["consolidator_tokens"]["completion"]
                    for c in out) if name == "curated" else 0
        print(f"{name:10s} F1={avg:.3f}  n={len(rs)}  qa_prompt_tokens={ptoks}  consolidator_tokens={extra}")
        bycat = {}
        for r in rs:
            bycat.setdefault(r["cat"], []).append(r["f1"])
        print("           by category:", {k: round(sum(v) / len(v), 3) for k, v in sorted(bycat.items())})


if __name__ == "__main__":
    main()
