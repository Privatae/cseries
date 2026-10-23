#!/usr/bin/env python3
"""Gate: what the Host reads sends what a person tells it about themselves to
memory, and keeps its own database for data it builds for a task.

Facts a person tells their Host about themselves (what they own, how they
live, what they like) go into the Host's memory, kept in the turn with
close_turn's preferences or memory_curate, where the newest entry in a topic
wins. The schema `world` (the `data` tool, skill 'world') is for data the Host
builds for a task: a meal plan, a tracker, a list the work produces.

The texts the Host reads are where it learns this, and the old rule sat in
four of them: personality_state's note and the memory skill said what the
Host learns from the user "lands in rules and your own tables", the world
skill's worked example stored the cooking tools a person has in a `kitchen`
table, and the Memory app's wiki page (a page the Host can hand over from a
capability search) said a fact "with a shape of its own" is kept in tables.
Any one of them teaches the Host the rule this one replaced.

Reads files only; no database, no container, no network.
Run: APP_ROOT=<repo> python3 scripts/test_personal_facts_are_memory.py
"""
import glob
import os
import re
import sys
from tools_source import tools_paths, tools_source  # tools.py and the modules split out of it

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAILS: list[str] = []


def check(label: str, cond, detail="") -> None:
    if cond:
        print(f"  ok    {label}")
    else:
        FAILS.append(label)
        print(f"  FAIL  {label}" + (f"\n        {str(detail)[:600]}" if detail != "" else ""))


def src(rel: str) -> str:
    with open(os.path.join(APP_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


# Wording that sends what the person says, or what the Host learns from them,
# into the Host's own tables. Each was live in a Host-facing text.
_OLD_RULE = re.compile(
    r"shape\s+of\s+its\s+own|facts\s+that\s+have\s+a\s+shape|a\s+table\s+holds\s+the\s+shape"
    r"|learn\s+from\s+the\s+user\s+lands\s+in[^.]*tables",
    re.I | re.S)


def host_texts() -> dict[str, str]:
    """Every text the Host reads that could say where a person's facts go:
    the default skills, the tool registry (descriptions are in its source),
    the tool results in runtime/tools.py and the modules split out of it, the wiki pages a capability
    search hands it, and the apps' own text, which the person reads."""
    out = {}
    for path in sorted(glob.glob(os.path.join(APP_ROOT, "default-skills", "*.md"))):
        out[os.path.relpath(path, APP_ROOT)] = src(os.path.relpath(path, APP_ROOT))
    for rel in ("brain/mastercode/public_tool_registry.py",
                *(os.path.relpath(p, APP_ROOT) for p in tools_paths(APP_ROOT))):
        out[rel] = src(rel)
    for path in sorted(glob.glob(os.path.join(APP_ROOT, "docs", "wiki", "pages", "*.md"))):
        out[os.path.relpath(path, APP_ROOT)] = src(os.path.relpath(path, APP_ROOT))
    # The apps' own words reach the person, who then tells the Host what they
    # read: the Memory app's Data lens once said tables hold "something with
    # a shape of its own". A plain app's words are in its app.js and
    # index.html; Circles, Community, Sites and Workbench are built from
    # <app>-web/src into minified assets, so their words are read at source.
    for pattern in ("system-apps/*/app.js", "system-apps/*/index.html",
                    "*-web/src/**/*.ts", "*-web/src/**/*.tsx"):
        for path in sorted(glob.glob(os.path.join(APP_ROOT, pattern), recursive=True)):
            out[os.path.relpath(path, APP_ROOT)] = src(os.path.relpath(path, APP_ROOT))
    return out


def test_no_text_keeps_the_old_rule():
    texts = host_texts()
    check("the Host's texts were read (skills, registry, tool results, wiki pages)",
          any(k.startswith("default-skills/") for k in texts)
          and any(k.startswith("docs/wiki/pages/") for k in texts), sorted(texts)[:5])
    # A glob that stops matching (a renamed app, a moved web tree) would read
    # nothing and pass: each kind of app text must have been read.
    check("the apps' own text was read (the Memory app, an app's page, a web app's source)",
          "system-apps/oracle/app.js" in texts
          and any(k.startswith("system-apps/") and k.endswith("/index.html") for k in texts)
          and any(k.startswith("circles-web/src/") for k in texts), sorted(texts)[-5:])
    hits = []
    for rel, text in texts.items():
        for m in _OLD_RULE.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            hits.append(f"{rel}:{line}: {m.group(0)!r}")
    check("no Host-facing text sends what the person says, or what is learned from them, to a table",
          not hits, "; ".join(hits))


def test_personality_note():
    s = tools_source()
    i = s.find("These weights are what the user set")
    seg = " ".join(s[i:i + 600].replace('"', " ").split())
    check("personality_state's note says what is learned from the user lands in memory",
          i > 0 and "learn from the user lands in your memory" in seg, seg)


def test_world_skill():
    text = src("default-skills/world.md")
    head = text.split("---", 2)[1] if text.startswith("---") else ""
    desc = next((ln for ln in head.splitlines() if ln.startswith("description:")), "")
    check("the world skill's description says it is for data built for a task, and the person's own words are memory",
          "for a task" in desc and "is memory, not a table" in desc, desc)
    when = text.split("## When to use", 1)[-1].split("## Steps", 1)[0]
    check("the world skill's When to use names the memory calls for what the person tells about themselves",
          "about themselves" in when and "close_turn" in when and "memory_curate" in when, when)
    # The worked example is what a model copies. Its table must be data built
    # for a task: a COMMENT that says what a person has, owns or keeps is the
    # retired rule in SQL.
    blocks = re.findall(r"```sql\n(.*?)```", text, re.S)
    comments = [m.group(1) for b in blocks for m in re.finditer(r"COMMENT ON TABLE \w+ IS '((?:[^']|'')*)'", b)]
    check("the world skill has a worked example with a table COMMENT", bool(comments), blocks)
    personal = [c for c in comments
                if re.search(r"\b(has|have|owns|own|keeps|she|he|they|her|his|their)\b", c, re.I)]
    check("the world skill's worked example builds data for a task, not what the person has",
          not personal, personal)


def test_memory_skill():
    text = src("default-skills/memory.md")
    flat = " ".join(text.split())
    i = flat.find("What the person tells you about themselves")
    seg = flat[i:i + 400]
    check("the memory skill says what the person tells about themselves is kept as a preference, and names the calls",
          i >= 0 and "as a preference" in seg and "close_turn" in seg and "op='add'" in seg
          and "about='person'" in seg, seg)
    check("the memory skill keeps the database for data built for a task",
          "Your own database (skill `world`) is for data you build for a task" in flat)


def test_wiki_memory_app():
    text = " ".join(src("docs/wiki/pages/memory-app.md").split())
    i = text.find("**Data**")
    seg = text[i:i + 400]
    check("the Memory app's wiki page says the database holds data built for a task and the person's words stay in memory",
          i >= 0 and "for a task" in seg and "stays in its memory" in seg, seg)


def main() -> int:
    for fn in (test_no_text_keeps_the_old_rule, test_personality_note, test_world_skill,
               test_memory_skill, test_wiki_memory_app):
        print(fn.__name__)
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — a part that cannot run fails
            check(f"{fn.__name__} ran", False, repr(e))
    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s): " + "; ".join(FAILS))
        return 1
    print("PASS: personal facts are memory")
    return 0


if __name__ == "__main__":
    sys.exit(main())
