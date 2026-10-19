#!/usr/bin/env python3
"""Gate: the user's stated preferences are kept whole.

A Host handles continuous 24/7 work on large data, efficiently — and the taste
store was silently losing the user's own words:

  - the 26th free-text note deleted the oldest, FIFO, and every note was cut
    at 500 chars;
  - a stated like/dislike kept 5 texts per target, each cut at 500 chars;
  - the build brief named only the first 5 likes and 5 dislikes in ALPHABETICAL
    order, so a 6th dislike never reached any build, with nothing said;
  - a design-token value was cut at 200 chars silently (font stacks, gradients)
    and a 41st token was refused (a real design system passes 40).

What this pins (no DB, no pod, no network; PROFILES_ROOT is redirected):
   1. every note is kept, whole
   2. every distinct stated text is kept, whole
   3. the brief NAMES every liked and disliked target, most-said first
   4. quoted words are never cut: rendered whole or counted, and the count says
      how to read the rest (preference op='list')
   5. op='list' returns the free-text notes (where the rest is read)
   6. design tokens: a 120-token system is stored, a 300-char value is whole,
      and every token is rendered into the brief

Run: APP_ROOT=<repo> python3 scripts/test_caps_preferences.py
"""
import os
import sys
import tempfile
from pathlib import Path

REPO = os.environ.get("APP_ROOT") or str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "brain"))

from server.routes import _preference_profile as pp  # noqa: E402

_fail = 0


def check(label, cond, detail=""):
    global _fail
    if cond:
        print(f"[caps-g2-prefs] ok   {label}")
    else:
        _fail += 1
        print(f"[caps-g2-prefs] FAIL {label}" + (f" — {detail}" if detail else ""))


def reset(root: Path):
    for f in root.glob("*.json"):
        f.unlink()
    pp.PROFILES_ROOT = root


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="caps-g2-prefs-",
                                dir=os.environ.get("TMPDIR") or None))
    # Removed when the interpreter leaves, pass, fail or raise.
    import atexit as _atexit
    import shutil as _shutil
    _atexit.register(_shutil.rmtree, tmp, True)

    # ── 1. notes: every one, whole ──────────────────────────────────────
    reset(tmp)
    long_note = "keep the sidebar narrow and the type large " * 50   # ~2.2k chars
    for i in range(30):
        pp.update_profiles(5, {"harness": "codex", "note": f"note {i}"})
    pp.update_profiles(5, {"harness": "codex", "note": long_note})
    notes = pp.load_user_profile()["notes"]
    check("1: 31 notes folded → 31 kept (no FIFO deletion of the user's words)",
          len(notes) == 31, f"kept {len(notes)}")
    check("1: …the first note is still there",
          any(n.get("text") == "note 0" for n in notes))
    check("1: …and a long note is kept whole (no 500-char cut)",
          notes[-1].get("text") == long_note.strip(), f"len={len(notes[-1].get('text') or '')}")

    # ── 2. stated texts: every distinct one, whole ──────────────────────
    reset(tmp)
    long_said = "the timeline on this one reads like a story " * 40
    for i in range(8):
        pp.record_stated(None, polarity="like", target="codex", subject="harness",
                         text=f"reason {i}")
    pp.record_stated(None, polarity="like", target="codex", subject="harness",
                     text=long_said)
    texts = pp.load_user_profile()["stated"]["like"]["codex"]["texts"]
    check("2: 9 distinct things said about one target → 9 kept",
          len(texts) == 9, f"kept {len(texts)}")
    check("2: …a long statement is whole (no 500-char cut)",
          texts[-1] == long_said.strip(), f"len={len(texts[-1])}")

    # ── 3. the brief names every target, most-said first ────────────────
    reset(tmp)
    dislikes = [f"zz-model-{c}" for c in "abcdefgh"]               # 8 dislikes
    # Recorded on a project: the brief quotes a project's own words only.
    for t in dislikes:
        pp.record_stated(5, polarity="dislike", target=t, subject="model",
                         text=f"too slow: {t}")
    for _ in range(3):                                               # said 3x
        pp.record_stated(5, polarity="dislike", target="zz-model-h", subject="model")
    s = pp.summarize_for_prompt(5)
    line = next((ln for ln in s.splitlines() if "DISLIKE" in ln), "")
    check("3: all 8 disliked targets are named in the brief (was: first 5 A-Z)",
          all(t in line for t in dislikes), line)
    check("3: …ranked by how often it was said (zz-model-h, said 4x, leads)",
          line.split(":", 1)[-1].strip().startswith("zz-model-h"), line)

    # ── 4. quotes are whole or counted, never cut ───────────────────────
    reset(tmp)
    big = "x" * 4000
    for i in range(4):
        pp.record_stated(5, polarity="like", target=f"arm-{i}", subject="model",
                         text=f"{i}{big}")
    s = pp.summarize_for_prompt(5)
    quoted = [ln for ln in s.splitlines() if ln.strip().startswith("·")]
    check("4: every quote rendered is whole",
          all(len(q.split(": ", 1)[-1].strip('"')) == 4001 for q in quoted),
          str([len(q) for q in quoted]))
    check("4: what did not fit is COUNTED, with how to read it",
          "not quoted here" in s and "preference op='list'" in s, s[-400:])

    # ── 5. op='list' carries the notes ──────────────────────────────────
    reset(tmp)
    pp.update_profiles(None, {"harness": "codex", "note": "dark, dense, no deps"})
    view = pp.list_preferences(None)
    check("5: list_preferences returns the free-text notes",
          any(n.get("text") == "dark, dense, no deps"
              for n in (view["user"].get("notes") or [])), str(view["user"])[:300])

    # ── 6. design tokens: the user's own system, whole ──────────────────
    reset(tmp)
    system = {f"ramp-{i}": f"oklch({i}% 0.1 250)" for i in range(120)}
    stack = ("'Inter Variable', 'Inter', ui-sans-serif, system-ui, -apple-system, "
             "BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, "
             "'Noto Sans', sans-serif, 'Apple Color Emoji', 'Segoe UI Emoji', "
             "'Segoe UI Symbol', 'Noto Color Emoji'")
    system["font-body"] = stack
    res = pp.set_design_tokens(None, system)
    stored = pp.load_user_profile()["design_tokens"]
    check("6: a 121-token design system is stored (was: refused past 40)",
          res.get("ok") is True and len(stored) == 121, str(res)[:200])
    check("6: …a long font stack is whole (was: cut at 200 chars silently)",
          stored.get("font-body") == stack and len(stack) > 200, str(len(stored.get("font-body") or "")))
    block = pp.design_tokens_block(None)
    check("6: …and every token is rendered into the brief",
          all(f"--{k}: {v}" in block for k, v in system.items()))

    print(f"\n[caps-g2-prefs] {'PASS' if not _fail else 'FAIL'} ({_fail} failing)")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
