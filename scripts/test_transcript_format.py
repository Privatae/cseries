#!/usr/bin/env python3
"""
Verify the JSONL transcript format the CC sidecar writes is the exact
shape `brain.training_memory.extract_dpo_pairs.extract_pairs` walks.

Why a separate script: smoke_test.sh runs in an environment without
Claude SDK auth, so we can't drive a real SDK turn end-to-end. But we
CAN synthesize the JSONL lines exactly as the sidecar writes them
(same block dicts, same outer envelope), feed them through the real
extractor, and assert a DPO pair pops out.

If this script fails after a sidecar edit, the SDK training loop is
broken — corrections will never reach behavioral_corrections because
the extractor won't find the rejected/correction/chosen triple in the
JSONL. Hence: wired into smoke_test.sh as a hard gate.
"""
import json
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

# Brain is on PYTHONPATH inside the container.
from training_memory.extract_dpo_pairs import extract_pairs


def _line(role: str, blocks: list) -> dict:
    return {
        "type": "user" if role == "user" else "assistant",
        "uuid": f"sdk-{uuid4().hex}",
        "isSidechain": False,
        "isMeta": False,
        "message": {"role": role, "content": blocks},
    }


def main() -> int:
    """Synthesize a 4-turn SDK transcript that the extractor SHOULD pick
    up as a DPO triple, then assert."""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / f"{uuid4().hex}.jsonl"
        # Turn 1: user asks for a thing
        # Turn 2: assistant responds (the "rejected" — wrong/incomplete)
        # Turn 3: user pushes back with a correction-shaped message
        # Turn 4: assistant fixes it (the "chosen")
        lines = [
            _line("user", [{"type": "text",
                            "text": "Build me a function that doubles a list."}]),
            _line("assistant", [
                {"type": "text",
                 "text": ("Sure — `def double(x): return x * 2`. "
                          + "x" * 120)},
                {"type": "tool_use", "name": "Write",
                 "input": {"file_path": "/tmp/double.py"},
                 "id": "tu_1"},
            ]),
            _line("user", [{"type": "text",
                            "text": "no, that doubles a number not a list. wrong."}]),
            _line("assistant", [{"type": "text",
                                  "text": ("You're right — for a list use a "
                                           "comprehension: `[x*2 for x in lst]`. "
                                           + "y" * 60)}]),
        ]
        with p.open("w") as f:
            for ln in lines:
                f.write(json.dumps(ln) + "\n")

        pairs = list(extract_pairs(p))
        if not pairs:
            print("FAIL: no pairs extracted from synthetic transcript",
                  file=sys.stderr)
            return 1
        pair = pairs[0]
        # Sanity-check the extracted triple's shape
        required = ("pair_id", "context", "rejected", "correction", "chosen")
        missing = [k for k in required if k not in pair]
        if missing:
            print(f"FAIL: pair missing keys {missing}", file=sys.stderr)
            return 1
        if not pair["rejected"]["text"]:
            print("FAIL: rejected has no text", file=sys.stderr)
            return 1
        if not pair["correction"]["text"]:
            print("FAIL: correction has no text", file=sys.stderr)
            return 1
        if pair["correction"]["kind"] not in ("tokens", "short", "interrupt"):
            print(f"FAIL: unexpected correction kind: "
                  f"{pair['correction']['kind']}", file=sys.stderr)
            return 1
        if not pair["chosen"]["text"]:
            print("FAIL: chosen has no text", file=sys.stderr)
            return 1
        # Tool block survives in rejected.tools
        if not any(t.get("name") == "Write" for t in pair["rejected"]["tools"]):
            print(f"FAIL: tool_use block lost in extraction "
                  f"(got: {pair['rejected']['tools']})", file=sys.stderr)
            return 1
        print(f"ok    transcript_format — extracted {len(pairs)} pair(s)"
              f" with kind={pair['correction']['kind']}, "
              f"tools={[t['name'] for t in pair['rejected']['tools']]}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
