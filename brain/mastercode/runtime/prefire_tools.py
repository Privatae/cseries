"""runtime/prefire_tools.py — the word-scoring primitives the turn-type
classifier and live context share, the thread's tool-trail names, and the
detector for a turn ABOUT the Host.

This module once also picked which tool groups rode `tools=` (the PREFIRE
pick), pushed a group's skill body into the turn, and rendered the whole
inventory on a self turn. Search mode is the only tool mode now: the list is
fixed, and find_tools reaches every tool, skill and app on demand, so those
are gone.
"""
from __future__ import annotations

import re

# Message tokens that are too common to mean a group on their own.
_STOP: frozenset[str] = frozenset({
    "the", "and", "for", "you", "your", "with", "this", "that", "what", "when",
    "how", "can", "could", "would", "should", "please", "thanks", "just", "now",
    "are", "was", "were", "have", "has", "had", "not", "but", "its", "it's",
    "about", "from", "into", "onto", "some", "any", "all", "one", "two", "let",
    "lets", "let's", "get", "got", "make", "made", "want", "need", "like",
})
# Keywords that are ALSO everyday words count HALF: a group loads on one
# strong word or two weak ones, so "what's the plan for dinner, let's start"
# ("plan" → build, "start" → system, one weak hit each) loads nothing, while
# "restart the app" ("restart" is strong) loads system. Measured: without
# this, "share your thoughts on the rules of chess, the key
# point is the model of the task" loaded five groups — 63 KB.
WEAK_WORDS: frozenset[str] = frozenset({
    "look", "lookup", "plan", "start", "stop", "stopped", "running", "key", "keys",
    "model", "models", "task", "tasks", "rule", "rules", "share", "shared", "read",
    "write", "edit", "file", "files", "code", "coding", "post", "index", "context",
    "status", "event", "events", "message", "messages", "page", "open", "search",
    "find", "link", "docs", "app", "apps", "store", "site", "sites", "daily",
    "weekly", "log", "logs", "activity", "engine", "engines", "protocol", "skill",
    "skills", "route", "routing", "process", "processes", "explain", "explanation",
    "meeting", "phone", "circle", "circles", "port", "ports", "machine", "machines",
    "web", "article", "documentation", "compile", "implement", "parallel", "compare",
    "preference", "preferences", "conversation", "conversations", "thread", "threads",
    "document", "documents", "remember", "forget", "contact", "contacts", "domain",
    "domains", "browser", "catalog", "buy", "purchase", "decide", "propose", "board",
    "forum", "community", "picture", "photo", "capability", "capabilities", "broken",
    "workspace", "folder", "directory", "function", "symbol", "token", "tokens",
    "login", "password", "passwords", "compute", "rent", "cursor", "selection",
})
STRONG_HIT = 1.0
WEAK_HIT = 0.5
LOAD_THRESHOLD = 1.0
def trail_tool_names(trail_turns: list[dict], *, max_turns: int) -> list[str]:
    """The tool names the thread called in its newest `max_turns` tool-calling
    turns, chronological, as the pick reads them: an operate chip names the
    tool it OPERATED (`operate_tool`), because operate(tool='revoke_nac_rule')
    is network work and `operate` itself is in no group; a search-mode read
    chip names the tool it READ (`read_tool`) for the same reason."""
    out: list[str] = []
    turns = (trail_turns or [])[-int(max_turns):] if max_turns else []
    for t in turns:
        if not isinstance(t, dict):
            continue
        for c in (t.get("tool_calls") or []):
            if not isinstance(c, dict) or not c.get("tool"):
                continue
            if c.get("tool") == "operate" and c.get("operate_tool"):
                out.append(str(c["operate_tool"]))
            elif c.get("tool") == "read" and c.get("read_tool"):
                # A search-mode read(tool=X) chip names X the same way
                # (session.py writes `read_tool` only for that door).
                out.append(str(c["read_tool"]))
            else:
                out.append(str(c["tool"]))
    return out

# ── Turns ABOUT the host itself ─────────────────────────────
# A question about the Host ("what tools do you have", "review your
# environment") reads a wider trail of its own calls in this thread, so a
# self-review quotes the record. (find_tools answers "what is in my
# inventory"; the whole inventory no longer rides such a turn as text.)
# The detector is lexical: a second-person marker
# AND a noun that names the host's own kit (one strong, or two weak), or one
# of a few whole phrases. Both halves are required: "review the architecture
# of this repo" and "analyze the gaps in the test coverage" are Workbench
# work with no marker, and "can you restart the app" carries a marker and no
# noun — none of them is a self turn (a verb-only path measured firing on
# 12 of 16 ordinary messages). Generic nouns that are also
# the user's own things (environment, architecture, setup) count only inside
# a phrase with the marker attached ("your environment"). The cost of a
# false positive is one block of text (~25 KB); the cost of a
# miss is a false statement to a user.
SELF_MARKERS: frozenset[str] = frozenset({
    "you", "your", "yours", "yourself", "you're", "youre", "u", "ur",
})
SELF_STRONG: frozenset[str] = frozenset({
    "tool", "tools", "tooling", "toolset", "capability", "capabilities",
    "limitation", "limitations", "gaps", "lanes", "inventory",
})
SELF_WEAK: frozenset[str] = frozenset({
    "skills", "access", "permissions", "setup", "prompt", "context",
    "abilities", "able", "limits", "gates", "version", "kernel", "memory",
    "network", "apps", "model", "models",
})
# Whole-phrase forms of the question, as regexes over the lowercased message.
# Each is anchored to the way the self question is actually asked, because
# the bare openers are also ordinary chat ("what are you doing tonight",
# "what can you do to help me with dinner", "how you work through a recipe")
# — 7 of 7 such openers measured firing on a plain-substring list. A phrase either ends the message, names the host's kit, or carries
# the "everything / any way / able to / did you" shape that only a question
# about the host takes.
SELF_PATTERNS: tuple[str, ...] = (
    # The openers end the clause: the next thing after "you" is sentence
    # punctuation or the end of the message ("what are you?" / "what are
    # you? also …" fire; "what are you doing tonight" does not). ONE
    # optional-whitespace run before the punctuation — two adjacent `\s*`
    # split a long whitespace run ambiguously and measured quadratic
    # (8.4 s on 80k spaces).
    r"\bwhat can you do\b(?!\s+(?:to|for|about|with|when|if|once)\b)",
    r"\b(?:what|who) are you\s*(?:[?!.]|$)",
    r"\bwhat do you have\b(?:\s+(?:here|available|access to))?\s*(?:[?!.]|$)",
    r"\bin front of you\b",
    r"\bhow (?:do )?you work\s*(?:[?!.]|$)",
    r"\babout yourself\b",
    r"\b(?:everything|all|anything) (?:that )?you (?:can|could|are able to|'re able to|know how to)\b",
    r"\bwhat you(?:'re| are) able\b",
    r"\bwhy (?:can't|couldn't|cant|couldnt|didn't|didnt|won't|wont|don't|dont) you\b",
    r"\byour (?:own )?(?:environment|tools|toolset|tooling|capabilities|architecture|setup|limitations|limits|gaps|lanes)\b",
    r"\b(?:do|did|don't|dont) you have (?:a |any |some )?(?:way|tool|tools|access|means|ability)\b",
    r"\bare you (?:allowed|able|permitted|supposed) to\b",
    r"\bwhat (?:did|have) you (?:do|done|call|called|try|tried|run|ran)\b",
    r"\b(?:summarize|summarise|recap|review|list|show) (?:me )?(?:what|everything|all) (?:that )?you (?:did|called|tried|have done|ran)\b",
)
_SELF_RE = tuple(re.compile(p) for p in SELF_PATTERNS)
# "what went wrong" is about the host when the host is in the message as
# "you" or "your", before or after it. Two substring tests, never one regex
# with `.*` on both sides of an alternation: that form measured quadratic on
# a line of repeated "your" (19.6 s on 200k chars).
_WENT_WRONG = "what went wrong"
_YOU_RE = re.compile(r"\byour?\b")
# The detector reads the user's QUESTION; a pasted log or transcript past
# this many characters cannot change the answer and is not scanned. The
# check runs synchronously on the request path, so its cost is bounded here
# rather than by what a user chooses to paste.
SELF_SCAN_CHARS = 20_000
# Phrases that carry a self marker + noun but are about the user's things:
# struck from the text before the checks above run.
SELF_EXCLUDE: tuple[str, ...] = (
    # Plural before singular: the singular is a prefix of the plural and
    # would leave an "s" behind while making the plural entry unreachable.
    "environment variables", "environment variable", "what you have in mind",
)
# The trail is three tool-calling turns on an ordinary turn (tool_trail.py):
# enough to carry a pending login or a build id. A self-review spans the
# thread, so on a self turn the record widens to this many turns / calls.
SELF_TRAIL_TURNS = 12
SELF_TRAIL_CALLS = 40


def is_self_turn(user_message: str) -> bool:
    """Is this message about the host itself — its tools, capabilities,
    environment, limits, or what it did in this thread? Deterministic, no
    model. `_tokens` drops "you"/"your" as stop words, so the markers are read
    off the raw words here."""
    # Cut with a margin, strike the exclusions, THEN cut to the bound: a
    # phrase straddling the boundary would otherwise lose its tail and read
    # as its own prefix ("your environment variables" → "your environment";
    # 9 offsets measured firing). The strike keeps
    # every offset — same-length padding, never a single space — so the
    # final cut is the first SELF_SCAN_CHARS characters of the lowercased
    # text exactly (lowercasing can lengthen a few code points; it only
    # pushes the margin's tail further out). A shortening strike pulled the
    # margin back inside the bound and its truncated phrase fired (41
    # combinations).
    text = (user_message or "")[:SELF_SCAN_CHARS + 64].lower()
    if not text.strip():
        return False
    for x in SELF_EXCLUDE:
        text = text.replace(x, " " * len(x))
    text = text[:SELF_SCAN_CHARS].strip()
    if any(rx.search(text) for rx in _SELF_RE):
        return True
    if _WENT_WRONG in text and _YOU_RE.search(text):
        return True
    raw = set(re.findall(r"[a-z][a-z']*", text))
    if not (raw & SELF_MARKERS):
        return False
    return bool(raw & SELF_STRONG) or len(raw & SELF_WEAK) >= 2


def _tokens(text: str) -> set[str]:
    toks = re.findall(r"[a-z0-9][a-z0-9_.\-']*", (text or "").lower())
    out: set[str] = set()
    for t in toks:
        t = t.strip(".-'")
        if len(t) >= 2 and t not in _STOP:
            out.add(t)
    return out


def surface_key(interaction_context: str | None) -> str:
    ic = (interaction_context or "").strip()
    if ic == "chat" or ic == "circles":
        return "circles"
    if ic == "workbench" or ic.startswith("project:"):
        return "workbench"
    return ic or "circles"


# The cut a skill body rides a turn up to (skill_standard.CORE_MAX is this
# value: a skill's core must fit inside it).
SKILL_INJECT_CHARS = 12_000
