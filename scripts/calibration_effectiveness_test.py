#!/usr/bin/env python3
"""
calibration_effectiveness_test.py — does the per-model calibration block help,
hurt, or do nothing?

Measures the `## Model calibration` block (per-model style counterweight,
injected next to the personality expression block) the same way
directive_effectiveness_test.py measured the personality directive: real chat
calls through the brain API, a mode-file variant toggle, deterministic scoring,
markdown report.

The question: avoid anything that degrades output; adopt it
only if it measurably improves. So the verdict logic has a DEGRADATION VETO —
no tic improvement can outvote a correctness miss or a depth collapse.

Variants:
  on   — calibration block injected (proposed default)
  off  — block skipped entirely (current production behavior)

Variant toggle: write the variant name to /tmp/_calibration_test_mode.
PREREQUISITE: the model_style injection hook must exist in session.py and be
env-gated exactly like the expression hook (CSERIES_CALIBRATION_TEST_ENABLE=1
in the pod env; mode file read per turn, cached 1s; injected block mirrored to
/tmp/_calibration_test_log/blocks.log). Same pattern as
personality/expression.py:34-91 — copy it, don't reinvent it.

Metrics per response:
  - tic_density      claudism matches per 100 words (vendored claudisms.ai
                     list, CC0, + structural regexes)
  - trailing_caveat  bool: final paragraph opens with a caveat marker OR an
                     offer-to-do-more ("Want me to…", "Let me know if…")
  - mean_sentence_words / long_sentence_rate / passive_rate /
    long_paragraph_rate — the writing-standard block's rules (STE-derived):
                     words per sentence, share of sentences
                     over 25 words, be-verb+participle heuristic, share of
                     paragraphs over six sentences. The mode file turns the
                     writing_standard block off with the calibration block.
  - rough_tokens     word count × 1.3
  - echo_rate        3-gram overlap between reply and the calibration block
                     (block leaking into replies = instant fail signal)
  - correctness      per-probe required substrings (only probes with an
                     objective answer)
  - elapsed_ms

After collection:
  - per (probe, variant) means
  - cross-variant embedding cosine (same nomic path as the brain) — content
    preservation check: did the block change WHAT is said, not just how
  - verdict per metric family + overall, with the degradation veto

Method: 12 probes × 2 variants × 3 trials = 72 chat calls, sequential, each
in its OWN fresh conversation (a synthetic app id per call), variants
interleaved per probe, loopback opened without the pod's proxy, probe turns
deleted at the end. Any errored call makes the whole run INVALID.
Writes report to /tmp/calibration-test-<ISO>.md.

Usage (inside a C-Series container, CSERIES_CALIBRATION_TEST_ENABLE=1 set):
  docker exec <container> \
      python3 /app/scripts/calibration_effectiveness_test.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/brain")

import asyncpg


# ── Probes ──────────────────────────────────────────────────────────
#
# Each probe: (name, message, required_substrings, depth_required)
#   required_substrings — correctness guard; empty = no objective answer
#   depth_required      — True: a big token DROP on this probe under "on"
#                         counts toward degradation, not toward "win"

PROBES = [
    ("factual_simple",
     "What's the capital of Australia?",
     ["canberra"], False),
    ("factual_math",
     "What's 47 * 23?",
     ["1081"], False),
    ("bounded_list",
     "Give me exactly three dinner ideas using chicken.",
     [], False),
    ("explain_technical",
     "Explain how DNS resolution works.",
     ["server"], True),
    ("task_completion",
     "Write a two-sentence bio for a plumber named Sal.",
     ["sal"], False),
    ("tradeoff_summary",
     "Summarize the tradeoffs of buying vs renting a home.",
     [], True),
    ("advice_fork",
     "Should I learn Python or JavaScript first?",
     [], False),
    ("creative_short",
     "Write a four-line poem about rain.",
     [], False),
    ("opinion_bait",
     "What do you think of microservices?",
     [], False),
    ("casual_check_in",
     "hey, how's it going?",
     [], False),
    ("deep_reasoning",
     "Walk me through how the codebook + signal_bus architecture differs "
     "from a pure transformer.",
     [], True),
    ("single_word",
     "Why?",
     [], False),
]

VARIANTS = ["on", "off"]
TRIALS_PER_CELL = 3

BASE_URL = "http://[::1]:8800"
TEST_MODE_FILE = "/tmp/_calibration_test_mode"
TEST_LOG_FILE = "/tmp/_calibration_test_log/blocks.log"
REQUEST_TIMEOUT_S = 120


# ── Claudism detector ───────────────────────────────────────────────
#
# Distilled from claudisms.ai/claudisms.md (CC0 1.0, vendored 2026-08-10)
# plus the Opus-5 community tics the research sweep confirmed. Literal
# phrases are matched case-insensitively on word boundaries; structural
# tics via regex. This is a DETECTOR (scoring), deliberately NOT prompt
# text — official guidance says positive framing beats prohibitions, so
# the model never sees this list.

TIC_PHRASES = [
    # pseudo-reflective / false depth
    "sit with", "worth sitting with", "arriving at", "keep coming back to",
    "coming back to", "the question i keep", "can't unsee",
    "i didn't set out to", "i can't stop thinking about",
    # false singularity / crowned superlatives
    "if i had to pick one", "surprised me most", "stuck with me",
    "hits hardest", "lands hardest", "strikes hardest",
    "the whole game", "the entire point", "the only thing that matters",
    # value-announcement ("worth ___" family)
    "worth talking about", "worth noting", "worth asking", "worth a look",
    "worth considering", "worth exploring", "worth examining",
    "it's worth", "this matters", "and that matters",
    # metaphor overreach
    "load-bearing", "the physics of", "different physics",
    "the engine of", "is the engine", "throughline",
    # announcer / throat-clearing
    "the point is", "here's where it gets interesting", "here's the thing",
    "let's be clear", "to be clear,", "in today's rapidly",
    # corporate / consultant
    "at the end of the day", "double-click", "unpack", "lean into",
    "paradigm shift", "lessons learned", "north star", "pressure-test",
    "move the needle",
    # imported LLM-general tells
    "delve", "shed light on", "pave the way", "when it comes to",
    "that's the tell", "the tell is",
    # Opus-5 research-sweep additions
    "stated fairly", "worth stating plainly", "keep the signal",
]

TIC_REGEXES = [
    # negative parallelism: "it's not X, it's Y" / "this isn't X, it's Y"
    (r"\b(?:it|this|that)(?:'s| is)(?:n't| not) (?:just |only |merely )?"
     r"[^.;\n]{2,60},\s*(?:it|this|that)(?:'s| is)\b", "not_x_but_y"),
    # totalizing: "the whole/entire <noun>" (flag-level, some noise)
    (r"\bthe (?:whole|entire) \w+ (?:is|of)\b", "totalizing"),
    # "the only X that <verb>"
    (r"\bthe only \w+ that \w+", "only_x_that"),
    # bold-term list items: "- **Term:** explanation"
    (r"(?m)^\s*[-*]\s+\*\*[^*\n]+\*\*[::]", "bold_term_list"),
]

# Em dashes scored separately (count per 100 words), since a single one
# isn't a tic but a density of them is.
EM_DASH = "—"

# Trailing caveat / afterthought markers — the "one caveat" pattern.
# Checked against the OPENING of the final paragraph (or final sentence
# when the reply is a single paragraph).
CAVEAT_MARKERS = [
    "one caveat", "one thing to", "one thing worth", "a caveat", "caveat:",
    "worth noting", "note that", "just note", "a note:", "that said",
    "keep in mind", "however,", "heads up", "one consideration",
    "something to consider", "one thing i'd", "before you go",
    "side note", "quick note", "unrelated,", "separately,",
]
OFFER_MARKERS = [
    "want me to", "would you like me", "shall i", "let me know if",
    "if you'd like", "i can also", "i could also", "happy to",
    "just say the word", "if you want, i",
]


def word_count(text: str) -> int:
    return len(re.findall(r"[a-z']+", (text or "").lower()))


def tic_hits(text: str) -> list[str]:
    """Every claudism match in the text (phrase and regex), as labels."""
    low = (text or "").lower()
    hits = [p for p in TIC_PHRASES
            if re.search(r"(?<![a-z])" + re.escape(p) + r"(?![a-z])", low)]
    for pattern, label in TIC_REGEXES:
        if re.search(pattern, text or "", re.IGNORECASE):
            hits.append(f"~{label}")
    return hits


def tic_density(text: str) -> float:
    """Claudism matches per 100 words. 0 = clean."""
    wc = word_count(text)
    if wc == 0:
        return 0.0
    return 100.0 * len(tic_hits(text)) / wc


def em_dash_density(text: str) -> float:
    wc = word_count(text)
    if wc == 0:
        return 0.0
    return 100.0 * (text or "").count(EM_DASH) / wc


def trailing_caveat(text: str) -> str | None:
    """Return the marker if the reply ENDS on a caveat/offer, else None.

    Final paragraph's first 80 chars are checked for caveat markers; the
    final 200 chars anywhere for offer markers (offers can trail a caveat).
    """
    t = (text or "").strip()
    if not t:
        return None
    paras = [p.strip() for p in t.split("\n\n") if p.strip()]
    last = paras[-1] if paras else t
    head = last[:80].lower()
    for m in CAVEAT_MARKERS:
        if head.startswith(m) or f" {m}" in head[:40]:
            return m
    tail = t[-200:].lower()
    for m in OFFER_MARKERS:
        if m in tail:
            return m
    return None


# ── Writing-standard metrics ────────────────────────────────────────
#
# Scores the STE-derived rules in prompt_builder._build_writing_standard
# the same way the tic list scores the calibration block: deterministic,
# zero-inference, relative between variants. The passive-voice check is a
# heuristic (be-verb + participle); it under- and over-counts the same way
# in both variants, so the on/off delta is the signal, not the level.

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(\[])|\n+")
_LIST_MARKER = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
_PASSIVE = re.compile(
    r"\b(?:is|are|was|were|be|been|being|get|gets|got)\s+"
    r"(?:\w+ly\s+)?(?:\w+ed|\w+en|built|done|made|sent|set|put|kept|left|"
    r"run|seen|shown|known|given|taken|written|found)\b")


def _prose_only(text: str) -> str:
    """Drop fenced code and inline code — those are not sentences."""
    t = re.sub(r"```[\s\S]*?```", " ", text or "")
    return re.sub(r"`[^`\n]*`", "x", t)


def sentences(text: str) -> list[str]:
    out = []
    for s in _SENTENCE_END.split(_prose_only(text)):
        s = _LIST_MARKER.sub("", (s or "").strip())
        s = re.sub(r"^#+\s*", "", s)
        if len(re.findall(r"[A-Za-z']+", s)) >= 2:
            out.append(s)
    return out


def mean_sentence_words(text: str) -> float:
    ss = sentences(text)
    if not ss:
        return 0.0
    return sum(len(re.findall(r"[A-Za-z']+", s)) for s in ss) / len(ss)


def long_sentence_rate(text: str, limit: int = 25) -> float:
    """Share of sentences over `limit` words (STE: 25 for description)."""
    ss = sentences(text)
    if not ss:
        return 0.0
    return sum(1 for s in ss
               if len(re.findall(r"[A-Za-z']+", s)) > limit) / len(ss)


def passive_rate(text: str) -> float:
    ss = sentences(text)
    if not ss:
        return 0.0
    return sum(1 for s in ss if _PASSIVE.search(s.lower())) / len(ss)


def long_paragraph_rate(text: str, limit: int = 6) -> float:
    """Share of paragraphs over `limit` sentences (STE: six)."""
    paras = [p for p in re.split(r"\n\s*\n", _prose_only(text)) if p.strip()]
    if not paras:
        return 0.0
    return sum(1 for p in paras if len(sentences(p)) > limit) / len(paras)


def correctness(text: str, required: list[str]) -> bool | None:
    """True/False when the probe has required substrings; None otherwise.
    Digit groups are compared without separators: "1,081" is 1081 (the
    first live run scored every correct math answer as a miss)."""
    if not required:
        return None
    low = (text or "").lower()
    low = re.sub(r"(?<=\d)[,\s](?=\d{3}\b)", "", low)
    return all(r in low for r in required)


# ── Shared plumbing (same shapes as directive_effectiveness_test.py) ─


async def mint_app_token(pool, app_id: str, perms: list[str]) -> str:
    row = await pool.fetchrow(
        "SELECT secret FROM kernel_secrets WHERE id='app_token'"
    )
    secret = bytes(row["secret"])
    from server.applications.tokens import mint
    return mint(secret, app_id=app_id, permissions=perms, window_id="ct")[0]


def set_variant(mode: str) -> None:
    Path(TEST_MODE_FILE).write_text(mode + "\n")
    time.sleep(1.5)  # > the hook's 1s cache TTL


def clear_variant() -> None:
    try:
        os.remove(TEST_MODE_FILE)
    except FileNotFoundError:
        pass


def read_blocks_after(t_start: float) -> list[tuple[float, str]]:
    try:
        raw = Path(TEST_LOG_FILE).read_text()
    except FileNotFoundError:
        return []
    entries: list[tuple[float, str]] = []
    for match in re.finditer(
            r"---(\d+\.\d+)---\n([\s\S]*?)(?=---\d+\.\d+---|\Z)", raw):
        ts = float(match.group(1))
        if ts >= t_start:
            entries.append((ts, match.group(2).rstrip("\n")))
    return entries


# Loopback, never the proxy: the entity container sets http_proxy
# and urllib does not match the bracketed host `[::1]` against the `::1`
# no_proxy entry, so every probe went to the node squid, was denied in ~1ms,
# and the first live run scored 72 empty replies as ADOPT. An opener with
# no proxy handler cannot be misrouted.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fire_probe(token: str, message: str) -> dict:
    body = json.dumps({"message": message, "project_id": None}).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/api/brain/messages",
        data=body, method="POST",
        headers={
            "X-Application-Token": token,
            "Content-Type": "application/json",
            "Accept": "text/x-vercel-ai-data-stream",
        },
    )
    t_start = time.time()
    full_text = ""
    error = None
    try:
        r = _DIRECT.open(req, timeout=REQUEST_TIMEOUT_S)
        for raw_line in r:
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            if not line or line.startswith(":"):
                continue
            colon = line.find(":")
            if colon < 0:
                continue
            prefix, rest = line[:colon], line[colon + 1:]
            try:
                payload = json.loads(rest)
            except json.JSONDecodeError:
                continue
            if prefix == "0" and isinstance(payload, str):
                full_text += payload
            elif prefix == "3":
                error = payload if isinstance(payload, str) else str(payload)
    except Exception as e:
        error = f"client error: {e}"
    return {
        "reply": full_text, "error": error,
        "elapsed_ms": int((time.time() - t_start) * 1000),
        "t_start": t_start, "t_end": time.time(),
    }


def trigrams(text: str) -> set:
    words = re.findall(r"[a-z']+", (text or "").lower())
    return {(words[i], words[i + 1], words[i + 2])
            for i in range(len(words) - 2)}


def echo_rate(reply: str, block: str) -> float:
    b = trigrams(block)
    if not b:
        return 0.0
    return len(trigrams(reply) & b) / len(b)


def rough_tokens(text: str) -> int:
    return int(len((text or "").split()) * 1.3)


async def embed(text: str, *, entity_id: int, pool) -> list[float] | None:
    """The entity's OWN configured embedder. Without entity_id + pool the
    engine falls back to brain_service._entity_id, which is 1 in a script
    process (the pod's entity is not 1), resolved no embedding config, and
    the cosine column came back empty on the first live run."""
    try:
        from mastercode.memory_engine import _get_memory_embedding
        vec_str = await _get_memory_embedding(text, entity_id=entity_id, pool=pool)
        if not vec_str:
            return None
        return [float(v) for v in vec_str.strip("[]").split(",")]
    except Exception:
        return None


def cosine(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    ma = sum(x * x for x in a) ** 0.5
    mb = sum(y * y for y in b) ** 0.5
    return dot / (ma * mb) if ma and mb else 0.0


# ── Driver ──────────────────────────────────────────────────────────


async def run() -> dict:
    pool = await asyncpg.create_pool(
        dsn="postgresql:///hostmem?host=/var/run/postgresql",
        user="entity", min_size=1, max_size=1,
    )
    # The pod's own entity: ENTITY_ID in the container env is authoritative
    # (entrypoint.sh renumbers the seeded row to it). Never
    # brain_service._entity_id (1 in a script process) and never the lowest
    # entity_registry id — a Host's DB also holds id 0 "Platform", so the
    # earlier LIMIT 1 resolved 0 and the cosine column stayed empty for a
    # spurious reason (measured on an aborted rerun).
    _eid = os.environ.get("ENTITY_ID", "")
    if not _eid.isdigit():
        raise SystemExit("ENTITY_ID is not in this process env — run inside "
                         "the entity container (the server reads the same var, "
                         "provider_gateway._call_anthropic).")
    entity_id = int(_eid)
    run_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    results: dict = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "probes": [p[0] for p in PROBES],
        "variants": VARIANTS,
        "trials_per_cell": TRIALS_PER_CELL,
        "entity_id": entity_id,
        "run_tag": run_tag,
        "cells": [],
    }
    total = len(PROBES) * len(VARIANTS) * TRIALS_PER_CELL
    done = 0
    # ONE FRESH CONVERSATION PER CALL, VARIANTS INTERLEAVED.
    # The thread a turn lands in is (app_id, project_id) — turn_registry
    # .compose_thread_key — and the first live run minted one workbench
    # token and reused it, so all 72 calls shared one thread: by the 50th the
    # model was answering "Canberra — I just answered that upthread", and the
    # whole "off" arm ran after the whole "on" arm, so variant was confounded
    # with conversation depth. Each cell now gets its own synthetic app id
    # (a fresh thread; the middleware verifies the HMAC, not app membership)
    # and the two variants alternate per probe. The rows are deleted at the
    # end (see below) so the harness leaves no thread behind.
    for name, msg, required, depth in PROBES:
        for trial in range(TRIALS_PER_CELL):
            for variant in VARIANTS:
                set_variant(variant)
                done += 1
                print(f"  [{done:>2}/{total}] {name} t{trial+1} {variant}: ",
                      end="", flush=True)
                token = await mint_app_token(
                    pool, f"calibration-{run_tag}-{name}-{variant}-t{trial}",
                    ["write:session", "read:projects", "read:brain_memory"],
                )
                resp = fire_probe(token, msg)
                entries = read_blocks_after(resp["t_start"] - 0.5)
                within = [e for e in entries if e[0] <= resp["t_end"] + 0.5]
                block_text = within[-1][1] if within else ""
                if block_text.startswith(
                        "[OFF — no injection]\nwould-have-been:\n"):
                    block_text = block_text.split(
                        "\nwould-have-been:\n", 1)[1]
                cell = {
                    "probe": name, "probe_message": msg, "variant": variant,
                    "trial": trial, "reply": resp["reply"],
                    "block": block_text,
                    "tic_density": tic_density(resp["reply"]),
                    "tic_hits": tic_hits(resp["reply"]),
                    "em_dash_density": em_dash_density(resp["reply"]),
                    "trailing_caveat": trailing_caveat(resp["reply"]),
                    "mean_sentence_words": mean_sentence_words(resp["reply"]),
                    "long_sentence_rate": long_sentence_rate(resp["reply"]),
                    "passive_rate": passive_rate(resp["reply"]),
                    "long_paragraph_rate": long_paragraph_rate(resp["reply"]),
                    "echo_rate": echo_rate(resp["reply"], block_text),
                    "correct": correctness(resp["reply"], required),
                    "depth_required": depth,
                    "rough_tokens": rough_tokens(resp["reply"]),
                    "elapsed_ms": resp["elapsed_ms"],
                    "error": resp["error"],
                }
                print(f"{cell['elapsed_ms']}ms tok={cell['rough_tokens']} "
                      f"tics={cell['tic_density']:.1f} "
                      f"tail={cell['trailing_caveat'] or '-'} "
                      f"sent={cell['mean_sentence_words']:.0f}w "
                      f"long={cell['long_sentence_rate']:.2f}", flush=True)
                results["cells"].append(cell)
    clear_variant()
    results["finished_at"] = datetime.now(timezone.utc).isoformat()

    # Content-preservation cosine (on vs off), per probe
    print("\n=== embedding for content-preservation cosine ===", flush=True)
    similarity = {}
    for name, *_ in PROBES:
        pc = [c for c in results["cells"] if c["probe"] == name]
        var_embeds = {}
        for v in VARIANTS:
            embeds = []
            for c in (c for c in pc if c["variant"] == v and c["reply"]):
                em = await embed(c["reply"], entity_id=entity_id, pool=pool)
                if em:
                    embeds.append(em)
            if embeds:
                n = len(embeds[0])
                var_embeds[v] = [sum(e[i] for e in embeds) / len(embeds)
                                 for i in range(n)]
        if "on" in var_embeds and "off" in var_embeds:
            similarity[name] = cosine(var_embeds["on"], var_embeds["off"])
            print(f"  {name}: {similarity[name]:.3f}", flush=True)
    results["similarity"] = similarity
    if not similarity:
        # Only a direct http(s) BYO embedding endpoint is reachable from a
        # script process; the bridge and bastion-relay lanes live in the
        # kernel process. So an empty column means
        # "unmeasured", never "no embedder".
        print("  (no embeddings reachable from this process — content-"
              "preservation is UNMEASURED this run, not failed)", flush=True)

    # Leave no thread behind: every probe turn was tagged with this run's
    # synthetic app ids. Best effort — a failure here is reported, never
    # hidden, and never changes the verdict.
    try:
        deleted = await pool.execute(
            "DELETE FROM entity_memory WHERE metadata->>'app_id' LIKE $1 "
            "OR metadata->'source_context'->>'ref' LIKE $1",
            f"calibration-{run_tag}-%",
        )
        print(f"\ncleanup: {deleted} (probe turns removed)", flush=True)
        results["cleanup"] = deleted
    except Exception as e:
        print(f"\ncleanup FAILED (rows tagged calibration-{run_tag}-* remain): {e}",
              flush=True)
        results["cleanup"] = f"failed: {e}"

    await pool.close()
    return results


# ── Verdict ─────────────────────────────────────────────────────────


def verdict(results: dict) -> dict:
    """Per-metric deltas plus the overall call, with the degradation veto."""
    def cells(variant):
        return [c for c in results["cells"]
                if c["variant"] == variant and not c["error"]]

    on, off = cells("on"), cells("off")
    v: dict = {}
    errored = [c for c in results["cells"] if c["error"]]
    v["errored"] = len(errored)

    def mean(cs, key):
        vals = [c[key] for c in cs]
        return statistics.mean(vals) if vals else 0.0

    v["tic_density"] = {"on": mean(on, "tic_density"),
                        "off": mean(off, "tic_density")}
    v["em_dash"] = {"on": mean(on, "em_dash_density"),
                    "off": mean(off, "em_dash_density")}
    v["tokens"] = {"on": mean(on, "rough_tokens"),
                   "off": mean(off, "rough_tokens")}
    v["trailing_rate"] = {
        "on": sum(1 for c in on if c["trailing_caveat"]) / max(len(on), 1),
        "off": sum(1 for c in off if c["trailing_caveat"]) / max(len(off), 1),
    }
    v["echo_max_on"] = max((c["echo_rate"] for c in on), default=0.0)
    for key in ("mean_sentence_words", "long_sentence_rate",
                "passive_rate", "long_paragraph_rate"):
        v[key] = {"on": mean(on, key), "off": mean(off, key)}

    # ── degradation veto ────────────────────────────────────────────
    problems = []
    for variant, cs in (("on", on), ("off", off)):
        misses = [c["probe"] for c in cs if c["correct"] is False]
        v[f"correctness_misses_{variant}"] = misses
    extra_misses = (set(v["correctness_misses_on"])
                    - set(v["correctness_misses_off"]))
    if extra_misses:
        problems.append(f"correctness regressed on: {sorted(extra_misses)}")
    # depth collapse: depth-required probes shrinking >60% under "on"
    for name in {c["probe"] for c in on if c["depth_required"]}:
        t_on = mean([c for c in on if c["probe"] == name], "rough_tokens")
        t_off = mean([c for c in off if c["probe"] == name], "rough_tokens")
        if t_off > 0 and t_on < 0.4 * t_off:
            problems.append(f"depth collapse on {name}: "
                            f"{t_on:.0f} vs {t_off:.0f} tokens")
    # content drift: mean on↔off cosine below floor
    sims = list(results.get("similarity", {}).values())
    if sims and statistics.mean(sims) < 0.80:
        problems.append(f"content drift: mean on/off cosine "
                        f"{statistics.mean(sims):.3f} < 0.80")
    # block leaking into replies
    if v["echo_max_on"] > 0.10:
        problems.append(f"calibration block echoing into replies "
                        f"(max echo {v['echo_max_on']:.2f})")
    v["problems"] = problems

    # ── the call ────────────────────────────────────────────────────
    tic_better = (v["tic_density"]["off"] > 0 and
                  v["tic_density"]["on"] <= 0.7 * v["tic_density"]["off"])
    tail_better = (v["trailing_rate"]["off"] > 0 and
                   v["trailing_rate"]["on"] <=
                   0.5 * v["trailing_rate"]["off"])
    shorter = v["tokens"]["on"] <= 0.9 * v["tokens"]["off"]
    # Writing standard: sentences over 25 words down 30%+.
    fewer_long = (v["long_sentence_rate"]["off"] > 0 and
                  v["long_sentence_rate"]["on"] <=
                  0.7 * v["long_sentence_rate"]["off"])

    # An errored call is not a data point. The first live run had all 72
    # calls fail at the proxy in ~1ms and the empty replies scored as ADOPT.
    if errored:
        sample = (errored[0]["error"] or "")[:160]
        v["overall"] = (f"INVALID — {len(errored)}/{len(results['cells'])} calls "
                        f"errored; no verdict. First error: {sample}")
    elif problems:
        v["overall"] = "REJECT — degradation veto: " + "; ".join(problems)
    elif tic_better or tail_better or shorter or fewer_long:
        wins = [w for w, ok in (
            ("tic density -30%+", tic_better),
            ("trailing caveats halved", tail_better),
            ("10%+ shorter", shorter),
            ("long sentences -30%+", fewer_long)) if ok]
        v["overall"] = "ADOPT — no degradation; wins: " + ", ".join(wins)
    else:
        v["overall"] = ("INCONCLUSIVE — no degradation but no measured win; "
                        "block is prompt-token cost for nothing. Default: "
                        "don't ship it.")
    return v


def write_report(results: dict, v: dict) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(f"/tmp/calibration-test-{ts}.md")
    L = [f"# Calibration Block Effectiveness — {ts}\n",
         f"Started {results['started_at']} · finished "
         f"{results['finished_at']}",
         f"Probes {len(results['probes'])} × variants "
         f"{results['variants']} × {results['trials_per_cell']} trials\n",
         (results.get("method_note") or
          "Each call ran in its own fresh thread under a synthetic app id, so "
          "the prompt was the GENERIC surface (no Circles/Workbench briefing, "
          "near-toolless). Both arms share it; the result is about the two "
          "style blocks, not the production surface.") + "\n",
         "## Verdict\n", f"**{v['overall']}**\n",
         "| metric | on | off |", "|---|---|---|",
         f"| tic density /100w | {v['tic_density']['on']:.2f} "
         f"| {v['tic_density']['off']:.2f} |",
         f"| em-dash /100w | {v['em_dash']['on']:.2f} "
         f"| {v['em_dash']['off']:.2f} |",
         f"| trailing caveat/offer rate | {v['trailing_rate']['on']:.2f} "
         f"| {v['trailing_rate']['off']:.2f} |",
         f"| mean tokens | {v['tokens']['on']:.0f} "
         f"| {v['tokens']['off']:.0f} |",
         f"| mean words/sentence | {v['mean_sentence_words']['on']:.1f} "
         f"| {v['mean_sentence_words']['off']:.1f} |",
         f"| sentences over 25 words | {v['long_sentence_rate']['on']:.2f} "
         f"| {v['long_sentence_rate']['off']:.2f} |",
         f"| passive-voice rate (heuristic) | {v['passive_rate']['on']:.2f} "
         f"| {v['passive_rate']['off']:.2f} |",
         f"| paragraphs over 6 sentences | {v['long_paragraph_rate']['on']:.2f} "
         f"| {v['long_paragraph_rate']['off']:.2f} |",
         f"| correctness misses | {v['correctness_misses_on']} "
         f"| {v['correctness_misses_off']} |",
         "\n## Per-probe\n",
         "| probe | variant | tics/100w | tail | tokens | correct | ms |",
         "|---|---|---|---|---|---|---|"]
    for probe in results["probes"]:
        for variant in results["variants"]:
            cs = [c for c in results["cells"]
                  if c["probe"] == probe and c["variant"] == variant]
            if not cs:
                continue
            td = statistics.mean([c["tic_density"] for c in cs])
            tok = statistics.mean([c["rough_tokens"] for c in cs])
            ms = statistics.mean([c["elapsed_ms"] for c in cs])
            tails = sum(1 for c in cs if c["trailing_caveat"])
            corr = {c["correct"] for c in cs} - {None}
            corr_s = ("miss" if False in corr else
                      "ok" if corr else "-")
            L.append(f"| {probe} | {variant} | {td:.2f} | "
                     f"{tails}/{len(cs)} | {tok:.0f} | {corr_s} | "
                     f"{ms:.0f} |")
    L.append("\n## Content preservation (on↔off cosine)\n")
    if not results.get("similarity"):
        L.append("UNMEASURED — no embedder reachable from the process that "
                 "ran this; not a failure, not a pass.")
    for probe, s in results.get("similarity", {}).items():
        L.append(f"- {probe}: {s:.3f}")
    L.append("\n---\n\n# Raw responses\n")
    for probe in results["probes"]:
        pc = [c for c in results["cells"] if c["probe"] == probe]
        if not pc:
            continue
        L.append(f"\n## {probe}\n\n**Prompt:** "
                 f"`{pc[0]['probe_message']}`\n")
        for variant in results["variants"]:
            for c in (c for c in pc if c["variant"] == variant):
                reply = (c["reply"] or "(empty)")[:1500]
                hits = ", ".join(c["tic_hits"]) or "-"
                L.append(f"\n**{variant} t{c['trial']}** · "
                         f"tics=[{hits}] · tail={c['trailing_caveat']} · "
                         f"tok≈{c['rough_tokens']}\n\n> {reply}\n")
    out.write_text("\n".join(L))
    return out


def main():
    print("Calibration block effectiveness test starting.", flush=True)
    print(f"Total LLM calls: "
          f"{len(PROBES) * len(VARIANTS) * TRIALS_PER_CELL}", flush=True)
    results = asyncio.run(run())
    v = verdict(results)
    out = write_report(results, v)
    print(f"\n{v['overall']}", flush=True)
    print(f"Report: {out}", flush=True)


if __name__ == "__main__":
    main()
