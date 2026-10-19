#!/usr/bin/env python3
"""Desktop token declaration — the parser gate (server/applications/desktop_tokens.py).

A desktop document recolours the shell and every app through ONE meta tag.
The values land in first-party CSS, so the grammar is a whitelist; this gate
pins what gets through and, more importantly, what does not."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.applications.desktop_tokens import extract_tokens, tokens_css  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not ok:
        fails.append(name)


DOC = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name='cseries-desktop' content='desktop'>
<meta name="cseries-tokens" content="--cseries-ink:#0b0b10; --cseries-ember: rgba(120, 80, 200, 0.9);
  --cseries-dusk:#C9A; --font-display: Georgia, serif; --cseries-font-body: 'Iowan Old Style', Georgia, serif;
  --cseries-bad: url(http://x); --cseries-worse: var(--cseries-ink); --cseries-brace: #fff}; --cseries-taskbar-h: 60px">
</head><body></body></html>"""

t = extract_tokens(DOC)
check("accepts hex / rgba / short hex / quoted font stack / length",
      t == {"--cseries-ink": "#0b0b10", "--cseries-ember": "rgba(120, 80, 200, 0.9)",
            "--cseries-dusk": "#C9A", "--cseries-font-body": "'Iowan Old Style', Georgia, serif",
            "--cseries-taskbar-h": "60px"}, repr(t))
check("drops url(), var(), braces, and non --cseries- names",
      not any(k in t for k in ("--cseries-bad", "--cseries-worse", "--cseries-brace", "--font-display")))
check("attribute order does not matter",
      extract_tokens('<meta content="--cseries-ink:#123" name="cseries-tokens">') == {"--cseries-ink": "#123"})
check("no meta → {}", extract_tokens("<html><head></head></html>") == {})
check("meta added past the static head window is ignored",
      extract_tokens("<html>" + " " * 70000 + '<meta name="cseries-tokens" content="--cseries-ink:#123">') == {})
big = ";".join(f"--cseries-t{i}:#{i:03x}" for i in range(200))
check("capped at 64 tokens", len(extract_tokens(f'<meta name="cseries-tokens" content="{big}">')) == 64)
check("javascript-ish and escaped values are refused",
      extract_tokens('<meta name="cseries-tokens" content="--cseries-ink:expression(1); --cseries-x:\\61 ; --cseries-y:red<b>">') == {})
css = tokens_css({"--cseries-ink": "#123"})
check("css block is a :root override", css.strip().startswith("/*") and ":root {" in css and "--cseries-ink: #123;" in css)
check("no tokens → empty css (base file byte-identical)", tokens_css({}) == "")

print("RESULT:", "FAIL " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
