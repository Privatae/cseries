"""A finished Host reply as the PARTS a messaging app can send as they are.

A messaging app sends whole messages and cannot run a page, so the reply is
taken apart, in its own order, into three kinds of part:

  text   the reply with its fences taken out, split where they were. A plain
         ```html or other code block stays text: it is source the Host is
         showing, not a page (app-web-kit/visuals/visual.ts VISUAL_LANG).
  image  a ```visual drawn by Takumi, or a generated image sent as the file
         it already is (generated_images).
  link   an artifact, opened in the Host's own Artifacts window, or the
         thread when something in the reply could not be drawn here.

What the renderer cannot draw goes as a link. No browser is woken for it, not
even as a fallback: a picture or an artifact, nothing else. Nothing is saved: a
picture is made for
this send and returned inline, never written to generated_images or anywhere
else, because a visual is kept only when the person clicks.

A link is server/deep_link.app_link made absolute by reach-out's
doorbell_link, which can only ever point at the Host's own address. It
carries the app id and the artifact or thread id and nothing else: Telegram,
Slack and Discord fetch links from their own servers to build previews, so a
token in a link would be handed to the messaging company. A preview fetcher
gets the sign-in page.

The connector decides how each part fits its platform (photo or file, caption
or separate message, text split to the length limit); nothing here knows a
platform's limits.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import uuid
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional, Sequence

from server.deep_link import app_link

log = logging.getLogger("privatae.connectors.reply_parts")

# What a person reads when part of a reply cannot be shown in the messaging
# app. Each is followed (once per reply) by a link to the thread, where the
# reply shows in full.
UNDRAWN_PICTURE = "This reply has a picture I couldn't draw here."
UNATTACHED_IMAGE = "This reply has an image I couldn't attach here."
THREAD_LINK_LABEL = "Open this thread in your Host"

# The fence languages that are taken out of the text. Everything else stays
# text, exactly as written.
VISUAL_LANG = "visual"
ARTIFACT_LANG = "artifact"

# Generated-image ids and the formats the Host serves them as
# (server/routes/images.py _ID_RE and _SERVABLE): the same rules here, so a
# part can only ever carry what that route would serve.
_GIMG_RE = re.compile(r"gimg_[A-Za-z0-9_-]{22}")    # always fullmatch: `$` passes a trailing \n
_MAX_SERIAL = 2**31 - 1
_SERVABLE = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}

# The largest image sent as an image part: the most one file of a connector's
# platform call may be (server/network/nac_connector.PLATFORM_FILE_MAX, pinned
# equal by scripts/test_connector_reply_parts.py). A generated image can be
# larger; it goes as a line and the thread link, where it shows in full.
IMAGE_PART_MAX = 10 * 1024 * 1024

# A generated image the Host put in its reply text as markdown. Only the
# Host's own /api/images path is recognised, relative or on the Host's own
# origin (_split_images checks it): every Host serves the same path, so a
# peer Host's image link carries an id this Host does not hold, and stays text.
_MD_IMAGE_RE = re.compile(
    r"!\[[^\]\n]*\]\(\s*<?(https://[^\s/)>]+)?/api/images/(gimg_[A-Za-z0-9_-]{22})>?"
    r"(?:\s+\"[^\"\n]*\")?\s*\)")

# The artifact ops after which there is a page worth linking to, and the
# shapes a tool call's artifact id can come in: a port of
# app-web-kit/visuals/artifact-ref.ts (artifactIdOf / artifactsOfTurn), so a
# messaging app links exactly the artifacts the Host's own chat shows cards
# for.
_SHOWN_OPS = frozenset({"create", "write", "promote", "import", "revert", "desktop_from_url"})
_PREVIEW_URL_RE = re.compile(r"/api/artifacts/([0-9]+)/render")   # JavaScript's \d: ASCII only
# A call that did not do its work leaves nothing to show. A persisted chip
# says "ok" by default (session.tool_call_chip) and a live record says
# "unknown" when the result was not an envelope.
_DONE_STATUSES = frozenset({"", "ok", "unknown", "success"})

_HEADING_RE = re.compile(r"<(h[1-3]|title)\b[^>]*>(.*?)</\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


# ── the fences ───────────────────────────────────────────────────────────────
#
# The chat finds fences with micromark (CommonMark) and draws one only when
# the first word of its info string is exactly `visual`: streamdown matches a
# renderer's language case-sensitively. This scanner follows the same rules,
# so a messaging app is sent a picture exactly where the chat draws one and
# text everywhere the chat shows text:
#   - a line ends at \n, \r\n or \r and nowhere else (str.splitlines also
#     splits at U+2028 and its kin, which would open a fence the chat never
#     sees);
#   - a fence can open inside a list item, on the item's own marker line or
#     below it, or inside a block quote, and it ends where that item or quote
#     ends, closed or not. Without this a list item's fence went out as source
#     and its closing line opened a fence over the rest of the reply, and an
#     unclosed one swallowed the paragraph after the list;
#   - a backtick fence's info string holds no backtick; a tilde fence's may;
#   - a fence line inside a raw HTML block (a <div> run up to a blank line, a
#     <pre> or <script> up to its end tag, a comment) is the block's text.

_LINE_RE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+\Z")
_FENCE_RE = re.compile(r"( {0,3})(`{3,}|~{3,})(.*)\Z")
_QUOTE_RE = re.compile(r" {0,3}> ?")
_MARKER_RE = re.compile(r" {0,3}(?:[-+*]|([0-9]{1,9})[.)])(?= |\Z)")
_THEMATIC_RE = re.compile(r" {0,3}([-*_])[ \t]*(?:\1[ \t]*){2,}\Z")
_ATX_RE = re.compile(r" {0,3}#{1,6}(?: |\Z)")


# CommonMark's HTML block starts (spec 4.6), each with the line that ends it:
# None ends at a blank line, a pattern ends on the line it matches.
_HTML_BLOCK_TAGS = (
    "address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|"
    "details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|"
    "h1|h2|h3|h4|h5|h6|head|header|hr|html|iframe|legend|li|link|main|menu|menuitem|nav|"
    "noframes|ol|optgroup|option|p|param|search|section|summary|table|tbody|td|tfoot|th|"
    "thead|title|tr|track|ul")
_HTML_STARTS = (
    (re.compile(r" {0,3}<(?:pre|script|style|textarea)(?:[ \t>]|\Z)", re.I),
     re.compile(r"</(?:pre|script|style|textarea)>", re.I)),
    (re.compile(r" {0,3}<!--"), re.compile(r"-->")),
    (re.compile(r" {0,3}<\?"), re.compile(r"\?>")),
    (re.compile(r" {0,3}<![A-Za-z]"), re.compile(r">")),
    (re.compile(r" {0,3}<!\[CDATA\["), re.compile(r"\]\]>")),
    (re.compile(r" {0,3}</?(?:" + _HTML_BLOCK_TAGS + r")(?:[ \t>]|/>|\Z)", re.I), None),
)
# Type 7, any complete tag alone on its line; it cannot interrupt a paragraph.
_HTML_TAG_LINE_RE = re.compile(
    r" {0,3}(?:<[A-Za-z][A-Za-z0-9-]*(?:[ \t]+[A-Za-z_:][A-Za-z0-9_.:-]*"
    r"(?:[ \t]*=[ \t]*(?:[^ \t\"'=<>`]+|'[^']*'|\"[^\"]*\"))?)*[ \t]*/?>"
    r"|</[A-Za-z][A-Za-z0-9-]*[ \t]*>)[ \t]*\Z")


def _html_block_end(rest: str, para: bool):
    """For a line that starts a raw HTML block: the pattern of the line that
    ends it, or None when a blank line does. False when it starts none."""
    for start, end in _HTML_STARTS:
        if start.match(rest):
            return end
    if not para and _HTML_TAG_LINE_RE.match(rest):
        return None
    return False


def _indent(s: str) -> int:
    return len(s) - len(s.lstrip(" "))


def _fence_open(rest: str) -> Optional[tuple[int, str, str]]:
    """(indent, fence mark, language) when the line opens a fence, else None.
    The language is the info string's first word, exactly as written."""
    m = _FENCE_RE.match(rest)
    if not m:
        return None
    mark, info = m.group(2), m.group(3).strip(" \t")
    if mark[0] == "`" and "`" in info:
        return None
    return len(m.group(1)), mark, re.split(r"[ \t]", info, maxsplit=1)[0]


def _continued(stack: list[tuple[str, int]], s: str) -> tuple[int, str]:
    """How many of the open containers (block quotes ">" and list items
    "li" with their content column) this line stays inside, and the line with
    their markers and indentation taken off."""
    rest = s
    for depth, (kind, width) in enumerate(stack):
        if kind == ">":
            m = _QUOTE_RE.match(rest)
            if not m:
                return depth, rest
            rest = rest[m.end():]
        elif not rest.strip():
            rest = ""
        elif _indent(rest) >= width:
            rest = rest[width:]
        else:
            return depth, rest
    return len(stack), rest


def _starts_block(rest: str) -> bool:
    """Whether a line starts a block of its own, so it cannot be a lazy
    continuation of the paragraph above it."""
    return (not rest.strip() or _fence_open(rest) is not None
            or _html_block_end(rest, True) is not False
            or bool(_THEMATIC_RE.match(rest) or _ATX_RE.match(rest)
                    or _QUOTE_RE.match(rest) or _MARKER_RE.match(rest)))


# Deeper than this a line opens no more containers. No reply nests that far,
# and without a bound one line of "- - - …" made every later line walk
# thousands of them (24 KB took 3 s, growing with the square of the length).
_MAX_DEPTH = 32


def _open_containers(stack: list[tuple[str, int]], rest: str, para: bool) -> tuple[str, bool]:
    """Push the block quotes and list items this line opens; the rest of the
    line after their markers, and whether a paragraph is still open."""
    while len(stack) < _MAX_DEPTH:
        m = _QUOTE_RE.match(rest)
        if m:
            stack.append((">", 0))
            rest, para = rest[m.end():], False
            continue
        m = _MARKER_RE.match(rest)
        if not m or _THEMATIC_RE.match(rest):
            return rest, para
        after = rest[m.end():]
        # A list interrupting a paragraph must have content, and an ordered
        # one must start at 1.
        if para and (not after.strip() or (m.group(1) is not None and int(m.group(1)) != 1)):
            return rest, para
        spaces = _indent(after)
        if not after.strip():
            width, rest = m.end() + 1, ""
        elif spaces >= 5:          # one space, then indented code
            width, rest = m.end() + 1, after[1:]
        else:
            width, rest = m.end() + spaces, after[spaces:]
        stack.append(("li", width))
        para = False
    return rest, para


def split_fences(text: str) -> list[tuple[str, str]]:
    """The reply as segments, in order: ("text", s), ("code", s) for a fenced
    block that stays text (kept verbatim, fence lines included), and
    ("visual", body) / ("artifact", body) for the fences taken out.

    CommonMark's rule for where a fence ends: a line of the same character,
    at least as long as the opening one, with up to three spaces before it and
    nothing after it, or the end of the list item or quote the fence is in;
    an unclosed fence at the top runs to the end of the reply."""
    lines = _LINE_RE.findall(text or "")
    out: list[tuple[str, str]] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            out.append(("text", "".join(buf)))
            buf.clear()

    stack: list[tuple[str, int]] = []
    para = False            # the line above was paragraph text
    i = 0
    while i < len(lines):
        s = lines[i].rstrip("\r\n").expandtabs(4)
        depth, rest = _continued(stack, s)
        if depth < len(stack):
            if para and not _starts_block(rest):
                buf.append(lines[i])        # a lazy paragraph line: nothing closes
                i += 1
                continue
            del stack[depth:]
            para = False
        rest, para = _open_containers(stack, rest, para)
        fence = _fence_open(rest)
        end = _html_block_end(rest, para) if fence is None else False
        if end is not False:
            # A raw HTML block: text up to its end, fence lines included.
            buf.append(lines[i])
            i += 1
            if end is None or not end.search(rest):
                while i < len(lines):
                    d2, r2 = _continued(stack, lines[i].rstrip("\r\n").expandtabs(4))
                    if d2 < len(stack) or (end is None and not r2.strip()):
                        break
                    buf.append(lines[i])
                    i += 1
                    if end is not None and end.search(r2):
                        break
            para = False
            continue
        if fence is None:
            buf.append(lines[i])
            if not rest.strip():
                para = False
            elif para or _indent(rest) < 4:   # indented code is no paragraph
                para = not (_THEMATIC_RE.match(rest) or _ATX_RE.match(rest))
            i += 1
            continue
        indent, mark, lang = fence
        close = re.compile(r" {0,3}" + re.escape(mark[0]) + "{" + str(len(mark)) + r",}[ \t]*\Z")
        body: list[str] = []
        j = i + 1
        while j < len(lines):
            s2 = lines[j].rstrip("\r\n").expandtabs(4)
            d2, r2 = _continued(stack, s2)
            if d2 < len(stack):
                break                       # its item or quote ended, and the fence with it
            j += 1
            if close.match(r2):
                break
            body.append(r2[min(indent, _indent(r2)):] + "\n")
        flush()
        if lang in (VISUAL_LANG, ARTIFACT_LANG):
            out.append((lang, "".join(body)))
        else:
            out.append(("code", "".join(lines[i:j])))
        para = False
        i = j
    flush()
    return out


_BACKTICKS_RE = re.compile(r"`+")
_BLANK_LINE_RE = re.compile(r"\n[ \t]*\n")


def _code_ranges(text: str) -> list[tuple[int, int]]:
    """Where a text segment is code the chat shows literally: CommonMark code
    spans (a run of N backticks closed by the next run of exactly N, never
    across a blank line) and indented code blocks (4 spaces or a tab, after a
    blank line or the segment's start, so a paragraph's continuation line is
    not one). An image written there is the Host showing the syntax."""
    ranges: list[tuple[int, int]] = []
    pos, prev_blank, in_block = 0, True, False
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        blank = not body.strip()
        if not blank and (body.startswith("    ") or body.startswith("\t")) and (prev_blank or in_block):
            ranges.append((pos, pos + len(line)))
            in_block = True
        elif not blank:
            in_block = False
        prev_blank = blank
        pos += len(line)
    runs = [m.span() for m in _BACKTICKS_RE.finditer(text)]
    i = 0
    while i < len(runs):
        start, end = runs[i]
        width = end - start
        close = next((k for k in range(i + 1, len(runs))
                      if runs[k][1] - runs[k][0] == width), None)
        if close is None or _BLANK_LINE_RE.search(text, start, runs[close][0]):
            i += 1
            continue
        ranges.append((start, runs[close][1]))
        i = close + 1
    return ranges


def _split_images(text: str, host_origin: str = "") -> list[tuple[str, str]]:
    """A text segment as ("text", s) and ("gimg", id) pieces: a markdown
    image of the Host's own generated image becomes the image itself, unless
    it is written as code (_code_ranges), where the chat shows it as text."""
    own = str(host_origin or "").rstrip("/").lower()
    code = _code_ranges(text)
    out: list[tuple[str, str]] = []
    pos = 0
    for m in _MD_IMAGE_RE.finditer(text):
        if m.group(1) and m.group(1).lower() != own:
            continue                # another site's /api/images: not this Host's
        if any(s < m.end() and m.start() < e for s, e in code):
            continue                # shown as code in the chat: stays text
        out.append(("text", text[pos:m.start()]))
        out.append(("gimg", m.group(2)))
        pos = m.end()
    out.append(("text", text[pos:]))
    return out


def _artifact_fence_id(body: str) -> Optional[int]:
    """The id an ```artifact fence names: a JSON object with `id`, the shape
    the turn row reads (runtime/session.py, `_assistant_tags["artifact_id"]`)."""
    try:
        emitted = json.loads(body or "")
    except (ValueError, TypeError):
        return None
    return _positive_id(emitted.get("id")) if isinstance(emitted, dict) else None


# ── the tool calls ───────────────────────────────────────────────────────────

def _obj(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _positive_id(v: Any) -> Optional[int]:
    """artifact-ref.ts idOf: a positive whole number, or a string of ASCII
    digits. str.isdigit() is not that test: it passes "²", which int() then
    refuses, and one ```artifact fence the model wrote cost the whole reply."""
    if isinstance(v, bool):
        return None
    if isinstance(v, str):
        v = int(v) if re.fullmatch(r"[0-9]+", v) else None
    elif isinstance(v, float) and v.is_integer():
        v = int(v)                  # JSON's 41.0 is JavaScript's 41
    return v if isinstance(v, int) and v > 0 else None


def _id_from_preview_url(v: Any) -> Optional[int]:
    m = _PREVIEW_URL_RE.search(v) if isinstance(v, str) else None
    return _positive_id(m.group(1)) if m else None


def _call_done(call: Mapping) -> bool:
    status = call.get("status", call.get("result_status", ""))
    return str(status or "").lower() in _DONE_STATUSES


def _inner_call(call: Mapping, tool: str) -> Optional[dict]:
    """The arguments of `tool` in this call, whether it was called directly or
    through `operate` (the lane every write takes); None for any other tool."""
    name = call.get("tool") or call.get("name")
    args = _obj(call.get("args"))
    if name == tool:
        return args
    if name == "operate" and (args.get("tool") == tool or call.get("operate_tool") == tool):
        return _obj(args.get("args"))
    return None


def artifact_id_of(call: Mapping) -> Optional[int]:
    """The artifact a tool call left behind, or None. Reads a live call
    record (tool, args, result_status, result) and a persisted chip (tool,
    op, operate_tool, digest, status) alike."""
    if not _call_done(call):
        return None
    inner = _inner_call(call, "artifact")
    if inner is None:
        return None
    op = str(inner.get("op") or inner.get("action") or call.get("op") or "create")
    if op.strip().lower().replace(" ", "_") not in _SHOWN_OPS:
        return None
    env = _obj(call.get("result"))
    res = _obj(env.get("result"))
    digest = _obj(call.get("digest"))
    for candidate in (res.get("artifact_id"), env.get("artifact_id"),
                      inner.get("artifact_id"), digest.get("artifact_id")):
        aid = _positive_id(candidate)
        if aid is not None:
            return aid
    return _id_from_preview_url(res.get("preview_url")) or _id_from_preview_url(digest.get("preview_url"))


def artifacts_of_turn(calls: Iterable[Mapping]) -> list[int]:
    """The distinct artifacts a turn touched, in the order each was LAST
    touched: a create followed by three writes is one artifact, once."""
    order: list[int] = []
    for call in calls:
        aid = artifact_id_of(call) if isinstance(call, Mapping) else None
        if aid is None:
            continue
        if aid in order:
            order.remove(aid)
        order.append(aid)
    return order


def generated_images_of_turn(calls: Iterable[Mapping]) -> tuple[list[str], int]:
    """(the Host-stored image ids the turn generated, in order; how many
    generated images live only at a provider's link). A provider link is
    never sent on: a link in a part only ever opens the Host."""
    ids: list[str] = []
    elsewhere = 0
    for call in calls:
        if not isinstance(call, Mapping) or not _call_done(call):
            continue
        inner = _inner_call(call, "image_generate")
        if inner is None:
            continue
        if str(inner.get("action") or inner.get("op") or "generate").strip() != "generate":
            continue
        env = _obj(call.get("result"))
        payload = _obj(env.get("result")) or env
        for img in payload.get("images") or []:
            gid = _obj(img).get("id")
            if isinstance(gid, str) and _GIMG_RE.fullmatch(gid):
                if gid not in ids:
                    ids.append(gid)
            else:
                elsewhere += 1
    return ids, elsewhere


# ── where things open ────────────────────────────────────────────────────────

def thread_link_path(scope_kind: str, scope_ref: str) -> Optional[str]:
    """The Host-relative link that opens a connector thread, or None.

    A connector only ever targets three scopes (design 4.2): a Circles thread
    (`thread:<uuid>`), a project (`project:<id>`, opened in the Workbench) and
    the original Circles conversation (`app:circles`)."""
    kind, ref = str(scope_kind or ""), str(scope_ref or "")
    if kind == "thread":
        try:
            return app_link("circles", f"thread:{uuid.UUID(ref)}")
        except ValueError:
            return None
    if kind == "project":
        pid = _positive_id(ref)
        return app_link("workbench", f"project:{pid}") if pid else None
    if kind == "app" and ref == "circles":
        return app_link("circles", "thread:original")
    return None


def _absolute(path: Optional[str], host_origin: str) -> str:
    """A Host-relative link on the Host's own origin, or "" when there is no
    origin to put it on (a relative link means nothing in a messaging app).
    doorbell_link is the one strict builder: whatever it is given, it can only
    answer the Host's own address."""
    if not path or not host_origin:
        return ""
    from server.reachout.service import doorbell_link
    return doorbell_link(path, host_origin)


def _picture_name(html: str, fallback: str) -> str:
    """A file name for a drawn picture: its first heading or title, else the
    fallback."""
    m = _HEADING_RE.search(html or "")
    words = _TAG_RE.sub(" ", m.group(2)) if m else ""
    slug = re.sub(r"[^a-z0-9]+", "-", words.lower()).strip("-")[:48].strip("-")
    return f"{slug or fallback}.png"


# ── what the Host holds ──────────────────────────────────────────────────────

class HostReads:
    """The three reads the builder makes, all of the Host's own rows and
    entity-scoped. Nothing here writes."""

    async def artifact_name(self, artifact_id: int) -> Optional[str]:
        from server import brain_service
        pool = brain_service.get_pool()
        # projects.id is SERIAL (init-db.sql): a larger id names nothing, and
        # bound as $1 it would raise instead and cost the whole reply.
        if pool is None or not 0 < int(artifact_id) <= _MAX_SERIAL:
            return None
        return await pool.fetchval(
            "SELECT name FROM projects WHERE id = $1 AND entity_id = $2 AND kind = 'artifact'",
            int(artifact_id), int(brain_service._entity_id))

    async def artifact_page(self, artifact_id: int) -> Optional[str]:
        from server import brain_service
        from server.applications import artifacts as artifact_service
        pool = brain_service.get_pool()
        if pool is None:
            return None
        try:
            return await artifact_service.render(pool, int(brain_service._entity_id), int(artifact_id))
        except Exception as e:  # noqa: BLE001 — no page is no picture, never a failed reply
            log.info("connector reply: artifact %s has no page to draw (%s)", artifact_id, e)
            return None

    async def image(self, image_id: str) -> Optional[tuple[str, bytes]]:
        from server import brain_service
        pool = brain_service.get_pool()
        if pool is None or not _GIMG_RE.fullmatch(image_id or ""):
            return None
        row = await pool.fetchrow(
            "SELECT content_type, bytes FROM generated_images WHERE id = $1 AND entity_id = $2",
            image_id, int(brain_service._entity_id))
        if row is None or row["content_type"] not in _SERVABLE:
            return None
        return row["content_type"], bytes(row["bytes"])


# ── the builder ──────────────────────────────────────────────────────────────

# Turns a page into an object with `.png` (bytes, or None for no picture) and
# `.reason`. The connector route passes picture.draw_picture (Takumi in its
# confined child).
Draw = Callable[[str], Awaitable[Any]]


async def build_reply_parts(
    text: str,
    tool_calls: Sequence[Mapping] = (),
    *,
    scope_kind: str,
    scope_ref: str,
    host_origin: str,
    draw: Draw,
    reads: Optional[HostReads] = None,
) -> list[dict]:
    """The reply as parts, `seq` numbered from 0 in the order they are sent.

    `text` is the finished reply as the Host wrote it, `tool_calls` the
    turn's calls (live records or persisted chips), and scope_kind/scope_ref
    the thread it ran in, which is where every "see it in your Host" link
    goes."""
    reads = reads or HostReads()
    parts: list[dict] = []
    placed_artifacts: set[int] = set()
    placed_images: set[str] = set()
    thread_linked = False

    def add(part: dict) -> None:
        part["seq"] = len(parts)
        parts.append(part)

    def add_text(s: str) -> None:
        s = (s or "").strip("\n")
        if not s.strip():
            return
        if parts and parts[-1]["type"] == "text":
            parts[-1]["text"] = parts[-1]["text"].rstrip("\n") + "\n\n" + s
            return
        add({"type": "text", "text": s})

    def add_thread_link() -> None:
        nonlocal thread_linked
        if thread_linked:
            return
        url = _absolute(thread_link_path(scope_kind, scope_ref), host_origin)
        if url:
            add({"type": "link", "url": url, "label": THREAD_LINK_LABEL})
            thread_linked = True

    async def picture(html: str) -> Optional[bytes]:
        try:
            drawn = await draw(html)
        except Exception as e:  # noqa: BLE001 — a renderer failure is a link, never a lost reply
            log.warning("connector reply: the picture renderer failed (%s); sending a link instead", e)
            return None
        png = getattr(drawn, "png", None)
        return bytes(png) if isinstance(png, (bytes, bytearray)) and png else None

    async def add_generated(image_id: str) -> None:
        if image_id in placed_images:
            return
        placed_images.add(image_id)
        got = await reads.image(image_id)
        if got is None:
            add_text(UNATTACHED_IMAGE)
            add_thread_link()
            return
        mime, data = got
        if len(data) > IMAGE_PART_MAX:
            # More than the connector can upload through its Host in one file:
            # sending the bytes would only make it read past them.
            add_text(UNATTACHED_IMAGE)
            add_thread_link()
            return
        add({"type": "image", "mime": mime, "data_b64": base64.b64encode(data).decode("ascii"),
             "name": f"{image_id}.{_SERVABLE[mime]}"})

    async def add_artifact(artifact_id: int) -> None:
        if artifact_id in placed_artifacts:
            return
        placed_artifacts.add(artifact_id)
        name = await reads.artifact_name(artifact_id)
        if name is None:
            return        # not this Host's artifact, or deleted since: nothing to open
        url = _absolute(app_link("artifacts", f"artifact:{artifact_id}"), host_origin)
        if not url:
            add_text(f"“{name}” is in your Host's Artifacts window.")
            return
        add({"type": "link", "url": url, "label": f"Open “{name}” in your Host"})
        page = await reads.artifact_page(artifact_id)
        png = await picture(page) if page else None
        if png:
            add({"type": "image", "mime": "image/png",
                 "data_b64": base64.b64encode(png).decode("ascii"),
                 "name": _picture_name(page, f"artifact-{artifact_id}")})

    visuals = 0
    for kind, body in split_fences(text):
        if kind == "code":
            add_text(body)
        elif kind == "text":
            for piece, value in _split_images(body, host_origin):
                if piece == "gimg":
                    await add_generated(value)
                else:
                    add_text(value)
        elif kind == VISUAL_LANG:
            visuals += 1
            png = await picture(body)
            if png:
                add({"type": "image", "mime": "image/png",
                     "data_b64": base64.b64encode(png).decode("ascii"),
                     "name": _picture_name(body, f"picture-{visuals}")})
            else:
                add_text(UNDRAWN_PICTURE)
                add_thread_link()
        elif kind == ARTIFACT_LANG:
            aid = _artifact_fence_id(body)
            if aid is not None:
                await add_artifact(aid)

    calls = [c for c in (tool_calls or ()) if isinstance(c, Mapping)]
    image_ids, elsewhere = generated_images_of_turn(calls)
    for gid in image_ids:
        await add_generated(gid)
    if elsewhere:
        add_text(UNATTACHED_IMAGE)
        add_thread_link()
    for aid in artifacts_of_turn(calls):
        await add_artifact(aid)
    return parts
