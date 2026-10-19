"""runtime/skill_standard.py — the platform standard a Host-written skill meets.

A skill is the
steps a Host follows to do one kind of job with what it already has: its
tools, installed apps, connections and machines, how to check the result and
what to do when it fails. It is never a program (a program is an App Store
app), never a rule (rules live in memory), never a permission (a skill's
`allowed-tools` records what it uses and grants nothing).

ONE PURE FUNCTION. `validate(name, text, registry_tools=..., default_skill_names=...,
active_mcp_servers=...)` reads only its arguments: no database, no network, no
disk. It runs on every write a Host or a person makes (runtime/host_skills.py)
and returns EVERY failure at once, each with the name of the rule it broke, so
a rewrite takes one pass.

Where each number comes from:
  - 12,000, the core: prefire_tools.SKILL_INJECT_CHARS, the cut a skill body
    rides a turn up to. Everything before `## Reference` fits it, so the whole
    procedure rides uncut.
  - 64,000, the whole: context_ops.READ_PAGE_CHARS, one `context` read page.
  - 2,000, a fenced code block: set just above the largest block
    in any procedure-shaped platform skill (bug-review.md, 1,818 characters).
  - 64 and 1,024, the name and the description: the open Agent Skills format.
The first two are imported, never copied, so the standard moves with the cut.

The 37 platform skills in default-skills/ are platform code, reviewed as code;
this standard binds what a Host or a person writes.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from mastercode.runtime.context_ops import READ_PAGE_CHARS
from mastercode.runtime.input_guard import _CONFUSABLES
from mastercode.runtime.prefire_tools import SKILL_INJECT_CHARS
from mastercode.runtime.skill_loader import _parse_frontmatter

# The core rides a turn up to this cut (prefire_tools.py SKILL_INJECT_CHARS).
CORE_MAX = SKILL_INJECT_CHARS
# The whole skill is one context read page (context_ops.py READ_PAGE_CHARS).
WHOLE_MAX = READ_PAGE_CHARS
# A skill is not a program.
CODE_BLOCK_MAX = 2_000
# Agent Skills format limits.
NAME_MAX = 64
DESCRIPTION_MAX = 1_024

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

REQUIRED_SECTIONS = ("When to use", "Steps", "Checks", "When it fails")
REFERENCE_SECTION = "Reference"

# Every rule, by the name a refusal reports.
RULES = (
    "frontmatter", "name", "name-matches-path", "name-not-default", "description",
    "allowed-tools", "tools-declared", "sources", "mcp-server", "sections", "core-size", "size",
    "code-block", "secrets", "hidden-text", "no-pipe-to-interpreter", "no-instruction-loading",
    "no-url-before-reference", "injection",
)


@dataclass(frozen=True)
class Failure:
    rule: str
    message: str

    def as_dict(self) -> dict:
        return {"rule": self.rule, "message": self.message}


@dataclass
class Result:
    name: str
    failures: list[Failure] = field(default_factory=list)
    description: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    mcp_server: str | None = None
    body: str = ""
    core: str = ""

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def rules_broken(self) -> list[str]:
        out: list[str] = []
        for f in self.failures:
            if f.rule not in out:
                out.append(f.rule)
        return out

    def refusal(self) -> str:
        """One message naming every rule the text broke, in rule order."""
        lines = [f"skills/{self.name} was not saved: it breaks {len(self.failures)} "
                 f"rule{'s' if len(self.failures) != 1 else ''} of the skill standard. "
                 "Fix every one and write it again."]
        lines += [f"- [{f.rule}] {f.message}" for f in self.failures]
        return "\n".join(lines)


# ── the body's shape ─────────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_H2_RE = re.compile(r"^ {0,3}##(?!#)[ \t]+(.*?)[ \t]*#*[ \t]*$")


def _lines_with_fences(body: str):
    """(line, inside_fence) for every line; a fence line itself counts as
    inside so a `## x` on it is never read as a heading."""
    fence: str | None = None
    for line in body.split("\n"):
        m = _FENCE_RE.match(line)
        if fence is None and m:
            fence = m.group(1)
            yield line, True
            continue
        if fence is not None:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) \
                    and not line.strip()[len(m.group(1)):].strip():
                fence = None
            yield line, True
            continue
        yield line, False


def headings(body: str) -> list[tuple[str, int]]:
    """Every level-2 heading outside a code fence: (text, character offset)."""
    out: list[tuple[str, int]] = []
    pos = 0
    for line, fenced in _lines_with_fences(body):
        if not fenced:
            m = _H2_RE.match(line)
            if m:
                out.append((m.group(1).strip(), pos))
        pos += len(line) + 1
    return out


def split_reference(body: str) -> tuple[str, str]:
    """(core, reference): the core is everything before the `## Reference`
    heading, the reference that heading and what follows. No heading: the
    whole body is the core."""
    for text, at in headings(body):
        if text.lower() == REFERENCE_SECTION.lower():
            return body[:at].strip(), body[at:].strip()
    return body.strip(), ""


def section(body: str, title: str) -> str:
    """The text under one level-2 heading, up to the next one ("" if absent)."""
    hs = headings(body)
    for i, (text, at) in enumerate(hs):
        if text.lower() == title.lower():
            end = hs[i + 1][1] if i + 1 < len(hs) else len(body)
            return body[at:end]
    return ""


def code_blocks(body: str) -> list[tuple[int, int]]:
    """(first line number, characters) for every fenced block; an unclosed
    fence runs to the end."""
    out: list[tuple[int, int]] = []
    fence: str | None = None
    start = 0
    size = 0
    for n, line in enumerate(body.split("\n"), start=1):
        m = _FENCE_RE.match(line)
        if fence is None:
            if m:
                fence, start, size = m.group(1), n, 0
            continue
        if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) \
                and not line.strip()[len(m.group(1)):].strip():
            out.append((start, size))
            fence = None
            continue
        size += len(line) + 1
    if fence is not None:
        out.append((start, size))
    return out


# ── what the checks read ─────────────────────────────────────────────────────
# Every content check (secrets, pipes, instruction loading, URLs, injection)
# reads the text as a model does, not as a regex sees its code points.
# Measured: a zero-width space inside `curl`, a full-width
# `ｃｕｒｌ`, a Cyrillic `ѕһ`, a soft hyphen inside `install`, or a full-width
# slash in `skills／x` each walked past every check while a model reads the
# word as written. So the checks run on one view of the text: NFKC (full-width
# and compatibility forms folded), every invisible character removed, and
# Cyrillic/Greek lookalikes folded to Latin — input_guard's own detection fold
# (input_guard._CONFUSABLES) plus the few it lacks. The view is for matching
# only; what is stored is the text as written. Line numbers survive it: no
# step adds or removes a line break.
_EXTRA_CONFUSABLES = str.maketrans({
    "һ": "h", "ɡ": "g", "ı": "i", "ȷ": "j", "ո": "n", "ս": "u", "օ": "o",
    "ϲ": "c", "ϳ": "j", "ԍ": "g", "ԝ": "w", "ɑ": "a", "ʏ": "y",
})
# Characters that draw nothing: Hangul fillers, the combining grapheme joiner
# and the Mongolian vowel separator. Every format character (Unicode category
# Cf — zero-width space and joiners, bidi marks, overrides and isolates, the
# soft hyphen, word joiner, BOM, and the tag block U+E0000-E007F that "ASCII
# smuggling" hides whole sentences in) and every variation selector are
# invisible too (_invisible).
_BLANK = frozenset("ᅟᅠㅤﾠ͏᠎")
# The three an emoji legitimately carries: VS15/VS16 after a symbol (⚠️), and
# the joiner between two pictographs (👩‍💻). Anywhere else they hide text.
_EMOJI_MARKS = frozenset("‍︎️")


def _invisible(ch: str) -> bool:
    if ch in "\t\n\r":
        return False
    o = ord(ch)
    return (ch in _BLANK or unicodedata.category(ch) in ("Cf", "Cc")
            or 0xFE00 <= o <= 0xFE0F or 0xE0100 <= o <= 0xE01EF)


def _symbolic(ch: str) -> bool:
    return bool(ch) and (unicodedata.category(ch) in ("So", "Sk") or ch in "#*0123456789")


def hidden_characters(text: str) -> list[tuple[int, str]]:
    """(offset, code point) for every character a reader cannot see but a
    model reads, except the emoji marks in their place."""
    out: list[tuple[int, str]] = []
    n = len(text)
    for i, ch in enumerate(text):
        if not _invisible(ch):
            continue
        if ch in _EMOJI_MARKS:
            prev = text[i - 1] if i else ""
            nxt = text[i + 1] if i + 1 < n else ""
            if ch == "‍":
                # Between two pictographs, a VS16 allowed on the left.
                left = text[i - 2] if prev == "️" and i >= 2 else prev
                if _symbolic(left) and _symbolic(nxt) and left not in "#*0123456789":
                    continue
            elif _symbolic(prev) and nxt not in _EMOJI_MARKS - {"‍"}:
                continue
        out.append((i, f"U+{ord(ch):04X}"))
    return out


def match_view(text: str) -> str:
    """The text as the checks read it (see above). Never stored."""
    s = unicodedata.normalize("NFKC", text or "")
    s = "".join(ch for ch in s if not _invisible(ch))
    return s.translate(_CONFUSABLES).translate(_EXTRA_CONFUSABLES)


# An HTML comment draws nothing in a markdown view of the skill, and the model
# reads every word of it.
_HTML_COMMENT_RE = re.compile(r"<!--")
# Encoded text no person approving the skill can read: a long unbroken run of
# the base64 alphabet carrying upper case, lower case and digits. A commit or
# a sha256 (hex: no upper case) and a long-name identifier (no digits mixed
# with both cases, or broken by `-`/`_`/`.`) do not qualify.
_BLOB_RE = re.compile(r"[A-Za-z0-9+/]{100,}={0,2}")


def _is_blob(s: str) -> bool:
    return bool(re.search(r"[A-Z]", s) and re.search(r"[a-z]", s) and re.search(r"[0-9]", s))


# ── fetching more instructions ───────────────────────────────────────────────

_INTERPRETERS = (r"(?:(?:ba|z|da|k|c|tc|fi)?sh|python\d*(?:\.\d+)?|perl|ruby|node|php|iex|"
                 r"invoke-expression|bun|deno|pwsh|powershell)")
_DOWNLOADERS = r"(?:curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod)"
# Text that is not readable until decoded: piped into an interpreter it is a
# program nobody reviewed.
_DECODERS = r"(?:base64|base32|xxd|openssl|uudecode|gunzip|zcat|unxz|bunzip2)"
# One character of a command line, a `\`-continued line break included.
_CMD = r"(?:\\\n|[^\n])"
# A path in front of a program (`/bin/sh`, `/usr/bin/env`, `./x`).
_PATH = r"(?:\.{0,2}/?(?:[\w.-]+/)+)?"
# What runs the piped text: optional sudo, any of the wrappers that only pass
# it on (`env`, `command`, `exec`, `xargs`, `nohup`, `busybox`), then an
# interpreter, each with or without a path.
_RUNNER = (r"(?:sudo\s+(?:-\S+\s+)*)?(?:" + _PATH
           + r"(?:env|command|exec|xargs|nohup|busybox)\s+(?:-\S+\s+)*)*" + _PATH + _INTERPRETERS + r"\b")
_PIPE_RES = (
    # curl … | sh, wget -qO- … | sudo bash, curl … | /bin/sh, curl … | env bash,
    # curl … \<newline> | sh, iwr … | iex
    re.compile(rf"(?i)\b{_DOWNLOADERS}\b{_CMD}*?\|\s*{_RUNNER}"),
    # echo … | base64 -d | sh: decoded text into an interpreter
    re.compile(rf"(?i)\b{_DECODERS}\b{_CMD}*?\|\s*{_RUNNER}"),
    # sh -c "$(curl …)", /bin/bash -c "`wget …`"
    re.compile(rf"(?i){_PATH}\b{_INTERPRETERS}\s+(?:-\w+\s+)*[\"']?(?:\$\(|`)\s*{_DOWNLOADERS}\b"),
    # eval "$(curl …)", source "$(wget …)", exec `curl …`
    re.compile(rf"(?i)\b(?:eval|source|exec)\s+[\"']?(?:\$\(|`)\s*{_DOWNLOADERS}\b"),
    # bash <(curl …), source <(wget …), . <(curl …)
    re.compile(rf"(?i)(?:{_PATH}\b{_INTERPRETERS}|\bsource|(?:^|\s)\.)\s+<\(\s*{_DOWNLOADERS}\b"),
    # curl -o i.sh … && bash i.sh; wget x; chmod +x x; ./x — a download, then
    # the next command runs it. The interpreter must be handed a file (a path
    # or a name with an extension), so a prose line that only begins with
    # "Python" or "Node" after a curl line is not a command.
    re.compile(rf"(?i)\b{_DOWNLOADERS}\b{_CMD}*?(?:&&|\|\||;|\n)\s*(?:{_RUNNER}\s+(?:-\S+\s+)*"
               r"[\"']?[\w~$./-]*?(?:/|\.\w{1,4}\b)"
               r"|chmod\s+(?:[ugoa]*\+[rw]*x|[0-7]{3,4})\b|\./[\w.-]+|source\s+\S|\.\s+[\w~$./-]*/)"),
    # iex (iwr …), IEX (New-Object Net.WebClient).DownloadString(…)
    re.compile(rf"(?i)\b(?:iex|invoke-expression)\b{_CMD}*?\b(?:iwr|irm|invoke-webrequest|"
               r"invoke-restmethod|downloadstring|downloadfile|net\.webclient)\b"),
)

# Up to N words between a verb and its object, inside one sentence: a word
# carries no period, so a match never runs on into the next sentence. A
# markdown link's brackets separate words too: "follow the instructions at
# [the runbook](https://…)" is the same sentence as without them.
_W = r"(?:[\s,;:'\"()`*_\[\]<>-]+[\w'-]+){0,%d}?[\s,;:'\"()`*_\[\]<>-]+"
_INSTRUCTION_FILES = (r"(?:SKILL\.md|AGENTS\.md|CLAUDE\.md|GEMINI\.md|copilot-instructions\.md|"
                      r"\.cursorrules|\.clinerules|\.windsurfrules)")
_LOAD_RES = (
    # install / load / import / fetch / download … a skill, an instruction file,
    # instructions. The verb, not the adjective: "installed apps, skills" lists
    # what a Host holds and asks for nothing.
    re.compile(r"(?i)\b(?:install|load|import|fetch|download)(?:s|ing)?\b" + (_W % 5)
               + r"(?:skills?|system[ -]prompts?|prompt[ -]files?|instructions?(?:[ -]files?)?|"
                 r"rules?[ -]files?|" + _INSTRUCTION_FILES + r")(?![\w-])"),
    # follow / obey / apply / execute … another / external / remote … skill, prompt, instructions
    re.compile(r"(?i)\b(?:follow|obey|apply|execute|run|use|carry out)\b" + (_W % 6)
               + r"(?:another|other|external|remote|upstream|downloaded|fetched|linked|"
                 r"third[- ]party)" + (_W % 2) + r"(?:skills?|prompts?|instructions?)\b"),
    # follow the instructions / steps at a URL, a page, a file, a repository
    re.compile(r"(?i)\b(?:follow|obey|apply|execute|carry out|do what)\b" + (_W % 6)
               + r"(?:instructions?|steps|directions|prompts?)" + (_W % 4) + r"(?:at|from|in|on)"
               + (_W % 3) + r"(?:url|link|page|site|website|file|repo|repository|readme|gist|"
                 r"https?://)"),
    # read / follow / load a known agent instruction file
    re.compile(r"(?i)\b(?:read|open|load|follow|obey|install|fetch|download|apply|use)\b"
               + (_W % 6) + _INSTRUCTION_FILES),
    # Indirection, whatever the target is called: "do what it says", "follow
    # whatever the page lists", "run every step it gives". The steps a skill
    # leads to are its own; what another text says is not reviewed with it
    # ("read the bridge-ship skill and do what it says" and "fetch … and do
    # every step it lists" must not pass).
    re.compile(r"(?i)\b(?:do|follow|obey|execute|run|carry\s+out|perform|act\s+on)\s+"
               r"(?:what|whatever|everything|all\s+(?:that|of\s+what))\s+"
               r"(?:it|they|that|this|the\s+[\w-]+(?:\s+[\w-]+)?)\s+"
               r"(?:says?|said|lists?|tells?\s+you|asks?|instructs?|contains?|describes?|gives?)\b"),
    re.compile(r"(?i)\b(?:do|follow|obey|execute|run|carry\s+out|perform)\s+"
               r"(?:every|each|all(?:\s+(?:of\s+)?the)?)\s+(?:steps?|instructions?|commands?|lines?|items?)\s+"
               r"(?:it|they|that\s+[\w-]+|the\s+[\w-]+(?:\s+[\w-]+)?)\s+"
               r"(?:lists?|gives?|contains?|says?|has|holds|describes?)\b"),
    # read X and follow it / fetch X, then run it: the text's object is
    # whatever was just read.
    re.compile(r"(?i)\b(?:read|open|fetch|load|download|decode|visit|consult|get)\b(?:[^.!?\n]|[.!?](?=[\w/])){0,160}?"
               r"\b(?:and|then)\s+(?:then\s+)?(?:follow|obey|execute|apply|carry\s+out)\s+"
               r"(?:it|them|that|those|its\s+(?:steps|instructions|directions|commands)|"
               r"their\s+(?:steps|instructions|directions|commands))\b"),
    re.compile(r"(?i)\b(?:fetch|download|decode|curl|wget)\b(?:[^.!?\n]|[.!?](?=[\w/])){0,160}?"
               r"\b(?:and|then)\s+(?:then\s+)?(?:run|execute|exec|eval|source|install)\s+"
               r"(?:it|them|that|those|the\s+(?:script|file|result|output|payload))\b"),
)
# A context read of another skill: the skill tells the Host to load more
# instructions through its own store. Not inside a URL or a longer path (a
# repository's `…/skills/tree/main` in `## Reference` is a link, not a read).
# A path built by concatenation (`'skills/' + name`) is a read of a skill the
# text does not name.
_SKILL_PATH_RE = re.compile(r"(?i)(?<![\w./-])(?:default-)?skills/([a-z0-9][a-z0-9_.-]*|"
                            r"(?=[\"'`]\s*\+))")

# A link: a scheme, a `www.` host, or a host with a path and no scheme
# (`raw.githubusercontent.com/x/y`, `evil.example/run.txt`) — the scheme-less
# form would otherwise walk past. The scheme-less form needs a dot in
# the host, a letters-only last label that is not a file extension (so
# `Node.js/Deno` and `notes.md/` are not links) and a path after it.
_FILE_EXTS = (r"(?:md|markdown|txt|py|js|mjs|cjs|ts|tsx|jsx|sh|bash|zsh|json|ya?ml|toml|ini|cfg|conf|"
              r"html?|css|scss|sql|go|rs|rb|php|pl|java|kt|swift|c|h|cc|cpp|hpp|cs|lock|env|csv|xml|pdf|"
              r"png|jpe?g|gif|svg|webp|zip|tar|gz|tgz|whl|deb|rpm|exe|dll|so|bin|ps1|bat|cmd|log|vue)")
_URL_RE = re.compile(
    r"(?i)\b(?:https?|ftp|wss?|git|ssh|file)://\S+|\bwww\.[a-z0-9-]+\.[a-z]{2,}\S*"
    r"|(?<![\w@./-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?!" + _FILE_EXTS + r"\b)[a-z]{2,24}"
    r"(?::\d{2,5})?/[^\s)\]>'\"`]+"
    r"|(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?::\d{2,5})?/[^\s)\]>'\"`]*")

# ── the tools a skill names ──────────────────────────────────────────────────
# `allowed-tools` is the record of what a skill uses: the line a person reads
# when deciding whether to trust it. A step that calls a
# tool the line leaves out makes the record wrong, so every registry tool the
# text names must be on it (rule `tools-declared`). The whole text counts,
# `## Reference` included: the Host reads the reference and acts on it when a
# step sends it there (host_skills.ride_text points at it), and a section the
# rule skipped is where an undeclared call would go. Links are different —
# they ARE reference material, so `no-url-before-reference` stops at the
# heading; a tool call is a step wherever it is written.
#
# What counts as naming a tool, so a skill's prose can still say "context",
# "recall" or "build" as English words:
#   - a call: `context(`, `turn_type(` — the name followed by `(`;
#   - the target of a router: `tool='manage_site'`, `"tool": "x"`;
#   - an identifier-shaped name (one carrying `_` or `.`, which no English
#     word does) anywhere: `list_skills`, `ladder.status`;
#   - a code span that is the name, or opens with it as a call
#     (`` `operate` ``, `` `turn_type op='draft'` ``), and a line of a code block
#     that opens with it the same way;
#   - "the <name> tool".
# A name inside a longer word, path or file name (`context_ops.py`,
# `skills/context`, `recall-notes`) is not the tool.
_NAME_LEFT = r"(?<![\w./\\-])"
_NAME_RIGHT = r"(?![\w-]|\.\w)"
_TOOLS_RE_CACHE: dict[frozenset, tuple] = {}


def _tool_patterns(registry: frozenset):
    cached = _TOOLS_RE_CACHE.get(registry)
    if cached is not None:
        return cached
    names = sorted((n for n in registry if n and re.fullmatch(r"[\w.-]+", n)), key=len, reverse=True)
    if not names:
        out: tuple = ()
    else:
        alt = "(" + "|".join(re.escape(n) for n in names) + ")"
        ident = [n for n in names if "_" in n or "." in n]
        # Tool names are matched as written (the registry's are lower case);
        # only the words around them ignore case.
        anywhere = [
            re.compile(_NAME_LEFT + alt + r"\("),
            re.compile(r"(?i:\btool)\s*[\"']?\s*[=:]\s*[\"'`]?" + alt + _NAME_RIGHT),
            re.compile(r"(?i:\bthe)\s+`?" + alt + r"`?\s+(?i:tool)\b"),
        ]
        if ident:
            anywhere.append(re.compile(
                _NAME_LEFT + "(" + "|".join(re.escape(n) for n in ident) + ")" + _NAME_RIGHT))
        # The opening of a code span or a code-block line: the name alone, a
        # call, or the name followed by `key=` arguments.
        lead = re.compile(r"^\s*" + alt + r"(?:\s*$|\(|\s+[\w-]+\s*=)")
        out = (anywhere, lead)
    if len(_TOOLS_RE_CACHE) > 8:
        _TOOLS_RE_CACHE.clear()
    _TOOLS_RE_CACHE[registry] = out
    return out


_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")


def named_tools(text: str, registry: Iterable[str]) -> list[tuple[str, int]]:
    """(tool, line) for every registry tool `text` names (see above), each
    once, at its first line, in the order they first appear. Reads the text
    as given; validate() hands it the match view."""
    pats = _tool_patterns(frozenset(registry or ()))
    if not pats:
        return []
    anywhere, lead = pats
    first: dict[str, int] = {}

    def seen(tool: str, at: int) -> None:
        if tool not in first or at < first[tool]:
            first[tool] = at

    for rx in anywhere:
        for m in rx.finditer(text):
            seen(m.group(1), m.start(1))
    pos = 0
    for line, fenced in _lines_with_fences(text):
        if fenced:
            m = lead.match(line)
            if m:
                seen(m.group(1), pos + m.start(1))
        else:
            for s in _SPAN_RE.finditer(line):
                m = lead.match(s.group(2))
                if m:
                    seen(m.group(1), pos + s.start(2) + m.start(1))
        pos += len(line) + 1
    return [(t, _line_of(text, at)) for t, at in sorted(first.items(), key=lambda kv: kv[1])]


_COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")
_REPO_RE = re.compile(r"^repo:([A-Za-z0-9][\w.-]*/[\w.-]+)(/[^@\s]*)?@([^@\s]+)$")
_HTTP_RE = re.compile(r"^https?://[^\s/]+\S*$", re.I)
_MCP_RE = re.compile(r"^mcp:([a-z0-9]+(?:-[a-z0-9]+)*)$")


def _excerpt(text: str, m: re.Match, width: int = 80) -> str:
    s = text[m.start():m.end()]
    s = " ".join(s.split())
    return s if len(s) <= width else s[:width - 1] + "…"


def _line_of(text: str, at: int) -> int:
    return text.count("\n", 0, at) + 1


def parse_sources(raw: str) -> tuple[list[dict], list[str]]:
    """The `sources` line: comma-separated entries, each one of
    `repo:<owner/name>/<path>@<commit>`, an http(s) URL, `mcp:<server>` or
    `conversation`. Returns (parsed, problems)."""
    parsed: list[dict] = []
    problems: list[str] = []
    for item in [s.strip() for s in (raw or "").split(",") if s.strip()]:
        if item == "conversation":
            parsed.append({"kind": "conversation", "ref": item})
            continue
        m = _REPO_RE.match(item)
        if m:
            if not _COMMIT_RE.match(m.group(3)):
                problems.append(f"{item!r} is pinned to {m.group(3)!r}, which is not a commit "
                                "(7-64 hex characters); pin the exact version read")
            else:
                parsed.append({"kind": "repo", "ref": item, "repo": m.group(1),
                               "path": (m.group(2) or "").lstrip("/"), "commit": m.group(3)})
            continue
        if item.startswith("repo:"):
            problems.append(f"{item!r} is not repo:<owner/name>/<path>@<commit>")
            continue
        m = _MCP_RE.match(item)
        if m:
            parsed.append({"kind": "mcp", "ref": item, "server": m.group(1)})
            continue
        if _HTTP_RE.match(item):
            parsed.append({"kind": "url", "ref": item})
            continue
        problems.append(f"{item!r} is not one of repo:<owner/name>/<path>@<commit>, a URL, "
                        "mcp:<server> or conversation")
    return parsed, problems


def validate(name: str, text: str, *, registry_tools: Iterable[str],
             default_skill_names: Iterable[str],
             active_mcp_servers: Iterable[str] | Mapping[str, Iterable[str]] = ()) -> Result:
    """Check one skill against the standard. `name` is the path's name
    (`skills/<name>`); `registry_tools` every tool in this Host's registry;
    `default_skill_names` the platform skills' names; `active_mcp_servers`
    the Host's active MCP connections — a name set, or a mapping of name to
    that server's tool names when they are known. Returns every failure."""
    text = text if isinstance(text, str) else ("" if text is None else str(text))
    res = Result(name=str(name or ""))
    fail = lambda rule, msg: res.failures.append(Failure(rule, msg))  # noqa: E731
    registry = set(registry_tools or ())
    defaults = {str(d).lower() for d in (default_skill_names or ())}
    if isinstance(active_mcp_servers, Mapping):
        mcp_tools = {str(k): {str(t) for t in (v or ())} for k, v in active_mcp_servers.items()}
    else:
        mcp_tools = {str(k): None for k in (active_mcp_servers or ())}

    # ── frontmatter ──────────────────────────────────────────────────────
    meta, body = _parse_frontmatter(text)
    if not text.startswith("---") or (not meta and body == text):
        fail("frontmatter", "the skill must open with a `---` frontmatter block of `key: value` "
                            "lines (name, description, allowed-tools, sources), closed by `---`")
        meta, body = {}, text
    res.body = body

    fm_name = (meta.get("name") or "").strip()
    if not fm_name:
        fail("name", "`name` is required")
    elif len(fm_name) > NAME_MAX:
        fail("name", f"`name` is {len(fm_name)} characters; at most {NAME_MAX}")
    elif not NAME_RE.match(fm_name):
        fail("name", f"`name` {fm_name!r} must be lowercase letters, digits and single hyphens, "
                     "with no hyphen at the start or end")
    if fm_name and fm_name != res.name:
        fail("name-matches-path", f"`name` is {fm_name!r} but the path is skills/{res.name}; "
                                  "they must be the same")
    for n in {fm_name, res.name} - {""}:
        if n.lower() in defaults:
            fail("name-not-default", f"{n!r} is a platform skill's name; platform skills are "
                                     f"platform code. Write your own under a name of its own")
            break

    desc = (meta.get("description") or "").strip()
    res.description = desc
    if not desc:
        fail("description", "`description` is required: what the skill does and when to use it")
    elif len(desc) > DESCRIPTION_MAX:
        fail("description", f"`description` is {len(desc)} characters; at most {DESCRIPTION_MAX}")

    mcp_server = (meta.get("mcp-server") or "").strip() or None
    res.mcp_server = mcp_server
    raw_tools = meta.get("allowed-tools")
    tools = [t.strip() for t in (raw_tools or "").split(",") if t.strip()]
    res.allowed_tools = tools
    # Every registry tool the text names (named_tools), read on the match
    # view like every content check, with the three lines that are records
    # rather than text left out: `allowed-tools` itself, `sources` and
    # `mcp-server`.
    named = named_tools(match_view(_without_keys(text, ("allowed-tools", "sources", "mcp-server"))),
                        registry)
    if not tools:
        # No list at all is one problem, and the refusal says what to put in
        # it, so one rewrite fixes it (tools-declared would only repeat it).
        fail("allowed-tools", "`allowed-tools` is required: the comma-separated tools this skill "
                              "uses (it records them; it grants nothing)"
             + (". The text names " + ", ".join(t for t, _ln in named) if named else ""))
    else:
        unknown = []
        for t in tools:
            if t in registry:
                continue
            if mcp_server and t.startswith(f"mcp:{mcp_server}/"):
                # The declared server's own tool. A server that is not active
                # is the mcp-server rule's failure, reported once there; on an
                # active one whose tool list is known, the tool must be on it.
                known = mcp_tools.get(mcp_server)
                if known is None or t.split("/", 1)[1] in known:
                    continue
            unknown.append(t)
        if unknown:
            fail("allowed-tools", "not tools in this Host's registry: " + ", ".join(unknown))

    # Every registry tool the text names is on `allowed-tools`.
    undeclared = [(t, ln) for t, ln in named if t not in tools]
    if tools and undeclared:
        fail("tools-declared", "the text names tools `allowed-tools` does not list: "
             + ", ".join(f"{t} (line {ln})" for t, ln in undeclared)
             + ". Add each one to `allowed-tools` (it records what the skill uses and grants "
               "nothing), or write the step without naming it")

    if "sources" not in meta or not (meta.get("sources") or "").strip():
        fail("sources", "`sources` is required: what this skill was written from — "
                        "repo:<owner/name>/<path>@<commit>, a URL, mcp:<server> or conversation")
    else:
        parsed, problems = parse_sources(meta.get("sources") or "")
        res.sources = parsed
        for p in problems:
            fail("sources", p)

    if mcp_server is not None:
        if mcp_server not in mcp_tools:
            fail("mcp-server", f"{mcp_server!r} is not an active MCP connection on this Host")
        elif not any(t.startswith(f"mcp:{mcp_server}/") for t in tools):
            fail("mcp-server", f"`allowed-tools` must name {mcp_server!r}'s tools "
                               f"(mcp:{mcp_server}/<tool>)")

    # ── body ─────────────────────────────────────────────────────────────
    hs = [h for h, _ in headings(body)]
    want = [s.lower() for s in REQUIRED_SECTIONS]
    seen = [h.lower() for h in hs]
    shape_ok = (seen[:len(want)] == want
                and seen[len(want):] in ([], [REFERENCE_SECTION.lower()]))
    if not shape_ok:
        missing = [s for s in REQUIRED_SECTIONS if s.lower() not in seen]
        extra = [h for h in hs if h.lower() not in want + [REFERENCE_SECTION.lower()]]
        dup = sorted({h for h in hs if seen.count(h.lower()) > 1})
        parts = []
        if missing:
            parts.append("missing " + ", ".join(f"`## {s}`" for s in missing))
        if extra:
            parts.append("not a section of the standard: " + ", ".join(f"`## {h}`" for h in extra)
                         + " (use `###` inside a section)")
        if dup:
            parts.append("repeated: " + ", ".join(f"`## {h}`" for h in dup))
        if not parts:
            parts.append("out of order")
        fail("sections", "the body's `##` sections must be exactly `## When to use`, `## Steps`, "
                         "`## Checks`, `## When it fails`, in that order, then an optional "
                         "`## Reference` last — " + "; ".join(parts))

    core, _ref = split_reference(body)
    res.core = core
    if len(core) > CORE_MAX:
        fail("core-size", f"everything before `## Reference` is {len(core):,} characters; at most "
                          f"{CORE_MAX:,} so the whole procedure rides a turn uncut. Move reference "
                          "material under `## Reference`, or split the job into several skills")
    if len(text) > WHOLE_MAX:
        fail("size", f"the skill is {len(text):,} characters; at most {WHOLE_MAX:,} (one read)")

    for line_no, size in code_blocks(body):
        if size > CODE_BLOCK_MAX:
            fail("code-block", f"the code block at body line {line_no} is {size:,} characters; at "
                               f"most {CODE_BLOCK_MAX:,}. A skill is steps, not a program: a "
                               "program is an App Store app the skill says to install")

    # ── what may never be in one ─────────────────────────────────────────
    # Every check below reads the text as a model reads it (match_view), and
    # the whole text, frontmatter included: a key the standard does not name
    # is stored and read with the rest. Only the `sources` line is left out
    # of the instruction checks; parse_sources already holds it to four
    # shapes, and a repository path there is a record, not a step.
    view = match_view(text)

    hidden = hidden_characters(text)
    if hidden:
        at_lines = sorted({_line_of(text, at) for at, _cp in hidden})
        cps = sorted({cp for _at, cp in hidden})
        fail("hidden-text", f"{len(hidden)} invisible character{'s' if len(hidden) != 1 else ''} "
                            f"({', '.join(cps[:6])}{', …' if len(cps) > 6 else ''}) on "
                            f"line{'s' if len(at_lines) != 1 else ''} "
                            f"{', '.join(map(str, at_lines[:10]))}: text a person reading the skill "
                            "cannot see, which a model still reads. Remove them")
    for line_no, (line, fenced) in enumerate(_lines_with_fences(text), start=1):
        if not fenced and _HTML_COMMENT_RE.search(line):
            fail("hidden-text", f"an HTML comment at line {line_no}: a markdown view does not show "
                                "it, and a model reads it. Write it as plain text or remove it")
    unlinked = _URL_RE.sub(lambda m: " " * len(m.group()), view)
    for m in _BLOB_RE.finditer(unlinked):
        if _is_blob(m.group()):
            fail("hidden-text", f"{len(m.group())} characters of encoded text at line "
                                f"{_line_of(unlinked, m.start())}: nobody approving the skill can "
                                "read it. Write what it says in plain words")

    try:
        from mastercode import secret_shapes
        # The text as written, then the view: a key written in full-width
        # letters or split by a zero-width space is still a key.
        hard = [(f, text) for f in secret_shapes.hard(secret_shapes.scan(text))]
        hard += [(f, view) for f in secret_shapes.hard(secret_shapes.scan(view))]
    except Exception as e:  # noqa: BLE001 - the check fails closed
        hard = []
        fail("secrets", f"the secret check could not run ({type(e).__name__}); nothing was saved")
    seen_secrets: set[tuple[str, int]] = set()
    for f, scanned in hard:
        # Both keep every line break, so one key is one (label, line).
        key = (f.label, _line_of(scanned, f.start))
        if key in seen_secrets:
            continue
        seen_secrets.add(key)
        fail("secrets", f"a {f.label} at line {key[1]}; a skill never carries "
                        "a credential (store it in the Keys app and name it)")

    for rx in _PIPE_RES:
        for m in rx.finditer(view):
            fail("no-pipe-to-interpreter", f"line {_line_of(view, m.start())} pipes a download or "
                                           f"decoded text into an interpreter: {_excerpt(view, m)!r}")
    instructions = match_view(_without_keys(text, ("sources",)))
    for rx in _LOAD_RES:
        for m in rx.finditer(instructions):
            fail("no-instruction-loading", f"tells the Host to take on more instructions: "
                                           f"{_excerpt(instructions, m)!r}")
    own = {res.name.lower(), fm_name.lower()} - {""}
    for m in _SKILL_PATH_RE.finditer(instructions):
        other = m.group(1).lower().rstrip(".")
        other = other[:-3] if other.endswith(".md") else other
        if other not in own:
            fail("no-instruction-loading", f"reads another skill ({_excerpt(instructions, m)!r}); a "
                                           "skill carries its own steps")

    # Links belong in `## Reference` and `sources`. Everything before
    # `## Reference` rides a turn (host_skills.ride_text), so a link anywhere
    # in it — `## When to use`, `## Checks` and `## When it fails` as much as
    # `## Steps` — is a link on every typed turn ("fetch <URL> and do every
    # step it lists" must not pass in `## Checks`). The
    # description is what a list shows.
    for where, part in (("the description", match_view(desc)), ("the text before `## Reference`", match_view(core))):
        for m in _URL_RE.finditer(part):
            fail("no-url-before-reference",
                 f"a URL in {where} ({_excerpt(part, m)!r}); the procedure names tools and "
                 "connections — links belong in `## Reference` and `sources`")

    try:
        from mastercode.runtime.untrusted_content import screen_for_model
        flags: list[str] = []
        for t in (text, view) if view != text else (text,):
            _clean, f = screen_for_model(t, source="skill_standard")
            flags += [x for x in f if x not in flags]
    except Exception as e:  # noqa: BLE001 - the check fails closed
        flags = [f"screen_error:{type(e).__name__}"]
    if flags:
        fail("injection", "the text carries injection markers (" + ", ".join(flags) + "); "
                          "rewrite those lines in plain words")
    return res


def _without_keys(text: str, keys: tuple[str, ...]) -> str:
    """`text` with the frontmatter lines of `keys` emptied (the line breaks
    kept, so line numbers hold). The frontmatter is what the loader reads
    (skill_loader._parse_frontmatter): `---` to the next `\\n---`."""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end < 0:
        return text
    want = tuple(f"{k}:" for k in keys)
    head = "\n".join("" if ln.strip().lower().startswith(want) else ln
                     for ln in text[:end].split("\n"))
    return head + text[end:]


__all__ = ["validate", "Result", "Failure", "RULES", "CORE_MAX", "WHOLE_MAX", "CODE_BLOCK_MAX",
           "NAME_MAX", "DESCRIPTION_MAX", "NAME_RE", "REQUIRED_SECTIONS", "REFERENCE_SECTION",
           "split_reference", "section", "headings", "code_blocks", "parse_sources",
           "match_view", "hidden_characters", "named_tools"]
