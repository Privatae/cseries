#!/usr/bin/env python3
"""Extract DPO candidate pairs from raw Claude Code session jsonls.

A pair = (assistant turn that got pushback) + (short user correction) + (next assistant turn).

Output JSONL at dpo_pairs.jsonl with full untruncated text. Stats at dpo_pairs.stats.json.

Command line: extract_dpo_pairs.py OUT_DIR SOURCE_DIR [SOURCE_DIR ...]
(no default locations; every path is given).
"""

import json
import re
import sys
from pathlib import Path


CORRECTION_RE = re.compile(
    r"\b(no|nope|stop|wait|don'?t|dont|actually|incorrect|wrong|fuck|shit|"
    r"that'?s (not|wrong|bad)|why (did|are) you|i (said|told)|"
    r"you (just|already|literally|never)|not (that|what|the))\b",
    re.I,
)
ACK_RE = re.compile(
    r"^(ok|okay|yes|yep|yeah|thanks|thank you|sure|go ahead|continue|"
    r"sounds good|perfect|great|nice|cool|done|next)[.!\s]*$",
    re.I,
)

CONTEXT_TURNS = 4
REJECTED_MIN_LEN = 100
CHOSEN_MIN_LEN = 50
USER_MAX_LEN = 600


def extract_assistant_content(content):
    if isinstance(content, str):
        return content, []
    text_parts, tools = [], []
    for block in (content or []):
        if not isinstance(block, dict):
            continue
        t = block.get("type")
        if t == "text":
            text_parts.append(block.get("text") or "")
        elif t == "thinking":
            text_parts.append("[thinking]\n" + (block.get("thinking") or ""))
        elif t == "tool_use":
            tools.append({
                "name": block.get("name"),
                "input_keys": list((block.get("input") or {}).keys()),
            })
    return "\n\n".join(p for p in text_parts if p).strip(), tools


def extract_user_text(content):
    """Return real user text, or None if this is a tool_result-only record."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    text_parts = []
    has_non_tool_result = False
    for block in content:
        if not isinstance(block, dict):
            continue
        t = block.get("type")
        if t == "text":
            has_non_tool_result = True
            text_parts.append(block.get("text") or "")
        elif t == "tool_result":
            continue
        else:
            has_non_tool_result = True
    if not has_non_tool_result:
        return None
    return "\n".join(text_parts).strip() or None


def classify_correction(text):
    if not text:
        return None
    t = text.strip()
    n = len(t)
    if t == "[Request interrupted by user]":
        return "interrupt", {"interrupted": True, "len": n}
    if n > USER_MAX_LEN:
        return None
    if ACK_RE.match(t):
        return None
    if CORRECTION_RE.search(t):
        return "tokens", {"tokens_matched": True, "len": n}
    if n < 200:
        return "short", {"len": n}
    return None


def walk_session(path):
    with path.open() as f:
        for i, line in enumerate(f):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("type") not in ("user", "assistant"):
                continue
            if r.get("isSidechain") or r.get("isMeta"):
                continue
            msg = r.get("message") or {}
            content = msg.get("content")
            role = msg.get("role")
            uuid = r.get("uuid")
            if role == "user":
                txt = extract_user_text(content)
                if txt is None:
                    continue
                stripped = txt.lstrip()
                if stripped.startswith((
                    "<local-command-",
                    "<command-name>",
                    "<command-message>",
                    "<task-notification>",
                    "<system-reminder>",
                    "<bash-stdout>",
                    "<bash-stderr>",
                )):
                    continue
                yield {"role": "user", "uuid": uuid, "text": txt, "tools": [], "raw_idx": i}
            elif role == "assistant":
                txt, tools = extract_assistant_content(content)
                if not txt and not tools:
                    continue
                yield {"role": "assistant", "uuid": uuid, "text": txt, "tools": tools, "raw_idx": i}


def merge_turns(turns):
    """Coalesce consecutive same-role records into one logical turn."""
    merged = []
    for t in turns:
        if merged and merged[-1]["role"] == t["role"]:
            prev = merged[-1]
            if t["text"]:
                prev["text"] = (prev["text"] + "\n\n" + t["text"]).strip()
            prev["tools"].extend(t["tools"])
        else:
            merged.append({"role": t["role"], "uuid": t["uuid"], "text": t["text"], "tools": list(t["tools"])})
    return merged


def extract_pairs(path):
    history = merge_turns(list(walk_session(path)))
    sid = path.stem
    out = []
    for i in range(1, len(history) - 1):
        u = history[i]
        if u["role"] != "user":
            continue
        rej, chosen = history[i - 1], history[i + 1]
        if rej["role"] != "assistant" or chosen["role"] != "assistant":
            continue
        if len(rej["text"]) < REJECTED_MIN_LEN and not rej["tools"]:
            continue
        if len(chosen["text"]) < CHOSEN_MIN_LEN and not chosen["tools"]:
            continue
        cls = classify_correction(u["text"])
        if cls is None:
            continue
        kind, signals = cls
        ctx = []
        for c in history[max(0, i - 1 - CONTEXT_TURNS):i - 1]:
            ctx.append({"role": c["role"], "text": c["text"][:2000]})
        out.append({
            "pair_id": f"{sid}:{rej['uuid']}",
            "session_id": sid,
            "source": str(path),
            "context": ctx,
            "rejected": {"text": rej["text"], "tools": rej["tools"], "len": len(rej["text"])},
            "correction": {"text": u["text"], "len": len(u["text"]), "kind": kind},
            "chosen": {"text": chosen["text"], "tools": chosen["tools"], "len": len(chosen["text"])},
            "signals": signals,
        })
    return out


def main():
    if len(sys.argv) < 3:
        print("usage: extract_dpo_pairs.py OUT_DIR SOURCE_DIR [SOURCE_DIR ...]",
              file=sys.stderr)
        sys.exit(2)
    out_dir = Path(sys.argv[1])
    SOURCES = [Path(a) for a in sys.argv[2:]]
    OUT_PATH = out_dir / "dpo_pairs.jsonl"
    STATS_PATH = out_dir / "dpo_pairs.stats.json"
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    n_sessions = 0
    n_pairs = 0
    kind_counts = {"interrupt": 0, "tokens": 0, "short": 0}
    with OUT_PATH.open("w") as fout:
        for src in SOURCES:
            if not src.exists():
                continue
            for p in sorted(src.rglob("*.jsonl")):
                n_sessions += 1
                try:
                    for pair in extract_pairs(p):
                        fout.write(json.dumps(pair) + "\n")
                        n_pairs += 1
                        kind_counts[pair["correction"]["kind"]] += 1
                except Exception as e:
                    print(f"  ERR {p}: {e}", file=sys.stderr)
    stats = {
        "sessions_scanned": n_sessions,
        "pairs_extracted": n_pairs,
        "by_kind": kind_counts,
    }
    STATS_PATH.write_text(json.dumps(stats, indent=2))
    print(json.dumps(stats, indent=2))
    print(f"output: {OUT_PATH}")


if __name__ == "__main__":
    main()
