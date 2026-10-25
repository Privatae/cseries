"""Where a support request from this Host can go, decided before anything is drafted.

A support ticket is drafted for the owner to approve and Send. Until this
module, it was drafted whatever the Host was connected to, and only the Send
said it could not go: "bridge not connected" on a standalone Host, "not
offered by the network: platform.ticket" on a network member. Now the Host
asks first, from the client it actually holds (server.main.get_bridge_client)
and that client's own list of the ops it carries, never from config:

  * platform   a cloud bridge to the platform: private tickets and bug PRs,
               as before.
  * network    a network member whose client carries a private support op
               (SUPPORT_OP). None does yet. The client's list is the Host's
               own copy of the door's op table (server/bridge/member_client.py
               NETWORK_OPS), so the day the door offers the op, a C-Series
               update that adds it to that list is all it takes: drafting and
               sending use it with no other change.
  * board      a network member without that op: the members' community
               board (community.post), which every member can read. Nothing
               private goes there; the Host writes a public-safe post for the
               owner to approve.
  * standalone no network at all (server/bridge/no_platform.NoPlatform, or no
               client yet): nothing can be sent to Privatae. A bug, a question
               or a feature request can be a PUBLIC issue on the repository,
               which the owner opens in their own browser; a security problem
               goes by email, as SECURITY.md asks.

The public text (public_draft) never carries what a private ticket would: no
shared context at all (no memory, conversations, files, errors, runs or
project), only the card fields that describe the fault, with secret shapes,
provider ids, random-looking tokens, e-mail addresses, phone numbers, network
addresses, links, folder paths, local machine names and the Host's name taken
out. That is pattern-matching, and a name or an address written in plain words
gets through it, so the owner always sees the whole text before it goes
anywhere: the issue in their own browser's GitHub form, the board post on the
card over the chat that sends it only on their click
(brain/mastercode/runtime/board_post_approval.py).
"""

from __future__ import annotations

import re
from urllib.parse import quote

LANE_PLATFORM = "platform"
LANE_NETWORK = "network"
LANE_BOARD = "board"
LANE_STANDALONE = "standalone"
# A member whose door offers neither a support op nor the board.
LANE_MEMBER_NONE = "member_none"

# The private support op a member door may offer one day. Feature-detected:
# a Host uses it as soon as its client lists it.
SUPPORT_OP = "support.ticket"
BOARD_OP = "community.post"

# Lanes where a private ticket can be drafted and sent.
PRIVATE_LANES = (LANE_PLATFORM, LANE_NETWORK)

# The repository's own channels (README.md "Issues and pull requests are
# welcome on GitHub"; SECURITY.md "Email security@privatae.ai"). Private
# vulnerability reporting is not enabled on the repository, so the Security
# tab is not offered.
REPO = "github.com/Privatae/cseries"
ISSUE_NEW_URL = "https://github.com/Privatae/cseries/issues/new"
SECURITY_EMAIL = "security@privatae.ai"
SECURITY_POLICY_URL = "https://github.com/Privatae/cseries/blob/main/SECURITY.md"

# Categories that may become a public issue. Billing and access are about the
# owner's own account: never a public post.
PUBLIC_ISSUE_CATEGORIES = ("bug", "question", "feature")
# The new-issue link carries the text; GitHub refuses very long addresses.
ISSUE_URL_MAX = 7_000


def client():
    """The client this Host holds now, or None before the server has one."""
    try:
        from server.main import get_bridge_client
    except Exception:
        return None
    return get_bridge_client()


def offers(bc, op: str) -> bool:
    """Whether `bc` carries `op`, by the client's own list. A cloud bridge has
    no list here (the platform answers every op it knows), so it carries all;
    a NoPlatform and a member client say for themselves."""
    if bc is None:
        return False
    fn = getattr(bc, "offers", None)
    if callable(fn):
        return bool(fn(op))
    return True


def is_standalone(bc) -> bool:
    if bc is None:
        return True
    try:
        from server.bridge.no_platform import NoPlatform
    except Exception:
        return False
    return isinstance(bc, NoPlatform)


def lane_of(bc) -> str:
    if is_standalone(bc):
        return LANE_STANDALONE
    if getattr(bc, "network_member", False):
        if offers(bc, SUPPORT_OP):
            return LANE_NETWORK
        if offers(bc, BOARD_OP):
            return LANE_BOARD
        return LANE_MEMBER_NONE
    return LANE_PLATFORM


def current_lane() -> str:
    return lane_of(client())


def connected(bc) -> bool:
    return bool(bc is not None and getattr(bc, "_connected", False))


# ── What the owner reads ─────────────────────────────────────────────────────

NOT_ON_NETWORK = "This Host isn't on the Privatae network, so it can't send a report to Privatae."
NO_PRIVATE_SUPPORT = ("The Privatae network doesn't take private support requests yet, "
                      "so this Host can't send a private ticket.")
SECURITY_SAY = (f"For a security problem, don't post it anywhere public: email {SECURITY_EMAIL}, "
                "as the C-Series security policy asks.")
# What the public text leaves out, in the owner's words. Only what the code
# does: it attaches nothing, and takes out the shapes it can find. A name or an
# address written in plain words can still be in the text, so the owner is
# asked to read it.
_PUBLIC_TEXT_SAY = ("Your Host writes it from the problem alone and attaches no memory, "
                    "conversations or files. It takes out the e-mail addresses, phone numbers, "
                    "network addresses, links, folder paths, keys and its own name that it can "
                    "find, but it can miss a name or an address written in plain words, so read "
                    "the text before it goes.")
ISSUE_OFFER = (f"Your Host can write it up as an issue on {REPO} instead. That issue is public. "
               f"{_PUBLIC_TEXT_SAY} You open it in your own browser, where you see exactly what "
               "it says and can change it, and you post it yourself.")
BOARD_OFFER = ("Your Host can write a post for the members' community board instead. Every "
               f"member can read the board. {_PUBLIC_TEXT_SAY} The post shows on a card over "
               "the chat exactly as it will go, and it goes out only when you press Post it there, "
               "with a code from your authenticator.")
ACCOUNT_SAY = ("A payment or account question can't go in a public issue, and this Host has "
               "no private way to send it.")
NOTHING_SENT = "Nothing was sent."


def cannot_send(lane: str, category: str = "") -> str:
    """The sentence for a request this Host cannot deliver privately, naming
    what the owner can do instead. Empty for a lane that can deliver."""
    if lane == LANE_STANDALONE or lane == LANE_MEMBER_NONE:
        head = NOT_ON_NETWORK if lane == LANE_STANDALONE else NO_PRIVATE_SUPPORT
        if category and category not in PUBLIC_ISSUE_CATEGORIES:
            return f"{head} {ACCOUNT_SAY} {SECURITY_SAY}"
        return f"{head} {ISSUE_OFFER} {SECURITY_SAY}"
    if lane == LANE_BOARD:
        return f"{NO_PRIVATE_SUPPORT} {BOARD_OFFER} {SECURITY_SAY}"
    return ""


def unreachable(lane: str) -> str:
    """A lane that can deliver, but whose connection is down right now: a
    real, temporary fault, said as one."""
    who = "the platform" if lane == LANE_PLATFORM else "the Privatae network"
    return (f"Your Host can't reach {who} right now. {NOTHING_SENT} The draft is kept; "
            "press Send again in a few minutes.")


def head_of(lane: str) -> str:
    """Why this Host can't send a private ticket, in one sentence; empty for a
    lane that can. A member is on the network, so it never reads
    NOT_ON_NETWORK."""
    if lane == LANE_STANDALONE:
        return NOT_ON_NETWORK
    if lane in (LANE_BOARD, LANE_MEMBER_NONE):
        return NO_PRIVATE_SUPPORT
    return ""


def offer_of(lane: str) -> str:
    """Where a request can go instead, in the owner's words; empty for a lane
    that can send."""
    if lane in (LANE_STANDALONE, LANE_MEMBER_NONE):
        return ISSUE_OFFER
    if lane == LANE_BOARD:
        return BOARD_OFFER
    return ""


def route_view(lane: str | None = None) -> dict:
    """What the Support card shows about where requests go (GET
    /api/support/route). The card reads `head` and `offer` as they are, so
    its words are these sentences and never a second copy of them."""
    lane = lane or current_lane()
    out = {"lane": lane, "can_send": lane in PRIVATE_LANES, "say": cannot_send(lane),
           "head": head_of(lane), "offer": offer_of(lane),
           "security": {"email": SECURITY_EMAIL, "policy": SECURITY_POLICY_URL,
                        "say": SECURITY_SAY}}
    if lane in (LANE_STANDALONE, LANE_MEMBER_NONE):
        out["issue_url"] = ISSUE_NEW_URL
    if lane == LANE_BOARD:
        out["board"] = True
    return out


# ── The public text ──────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"\b(?:https?|ftp|wss?|ssh|git)://[^\s<>\"')\]]+", re.I)
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b")
# An IPv6 address has "::" or at least five groups, so a clock time (12:30:45)
# is left alone.
_IPV6_RE = re.compile(r"(?<![\w:])(?=[0-9A-Fa-f:]*::|(?:[0-9A-Fa-f]{1,4}:){4})"
                      r"(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])")
# A number written as a phone number: an international one (+...), or the
# 3-3-4 shape. A date (2026-10-25) or a size is not one.
_PHONE_RE = re.compile(r"(?<![\w+])(?:\+\d[\d ().-]{6,}\d|\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4})(?![\w])")
# A folder path names the owner's folders and files (/srv/data/<client>/...,
# ~/Documents/..., D:\\Clients\\...): the whole path goes, not only a home
# folder's user name. An absolute path with two or more parts, any ~/ path,
# and any drive-letter path.
_PATH_RE = re.compile(
    r"(?:(?<![\w.~/\\])~[/\\][^\s\"'`<>()\[\]]*"
    r"|(?<![\w.~/:\\])/[^\s/\"'`<>()\[\]]+(?:/[^\s/\"'`<>()\[\]]*)+"
    r"|(?<![\w])[A-Za-z]:\\[^\s\"'`<>()\[\]]*)")
_HOSTNAME_RE = re.compile(r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\."
                          r"(?:local|lan|home|internal|corp|box|localdomain|arpa|intranet|private|localhost)\b",
                          re.I)

# (pattern, what it was, what goes in its place), applied in order.
_PUBLIC_CUTS = (
    (_URL_RE, "link", "[link removed]"),
    (_EMAIL_RE, "e-mail address", "[e-mail removed]"),
    (_PATH_RE, "folder path", "[path removed]"),
    (_IPV4_RE, "network address", "[address removed]"),
    (_IPV6_RE, "network address", "[address removed]"),
    (_HOSTNAME_RE, "machine name", "[machine name removed]"),
    (_PHONE_RE, "phone number", "[number removed]"),
)

# Only the fields that describe the fault. context / recommendation /
# pushback carry what the owner was doing and thinking: never public.
PUBLIC_FIELDS = (("problem", "What happens"), ("repro_steps", "Steps to reproduce"),
                 ("expected", "What I expected"), ("actual", "What happened instead"))


def _secret_cut(text: str, removed: set | None = None) -> str:
    try:
        from brain.mastercode import secret_shapes
    except Exception:
        try:
            from mastercode import secret_shapes  # type: ignore
        except Exception:
            secret_shapes = None
    if secret_shapes is None:
        # No redactor: nothing of the owner's leaves at all.
        raise RuntimeError("redaction unavailable")
    # Every shape the redactor knows, the random-looking tokens (its "soft"
    # findings) too: a public text has no use for a string that might be a key.
    cleaned, found = secret_shapes.redact(text, severities=("hard", "soft"))
    if found and removed is not None:
        removed.add("key or token")
    try:
        from server.routes.support import _scrub_provider_ids
        cleaned = _scrub_provider_ids(cleaned)
    except Exception:
        pass
    return cleaned


def scrub_public(text: str, removed: set | None = None, names: list[str] | None = None) -> str:
    """`text` with every private shape this module knows taken out. What was
    taken out is added to `removed` by kind, so the Host can say so."""
    text = _secret_cut(text or "", removed)
    for pat, what, mark in _PUBLIC_CUTS:
        text, n = pat.subn(mark, text)
        if n and removed is not None:
            removed.add(what)
    for name in names or []:
        name = (name or "").strip()
        if len(name) >= 2:
            text, n = re.subn(rf"(?i)\b{re.escape(name)}\b", "[name removed]", text)
            if n and removed is not None:
                removed.add("name")
    return text.strip()


def public_draft(*, category: str, title: str, problem: str, repro_steps: str = "",
                 expected: str = "", actual: str = "", version: str = "",
                 names: list[str] | None = None, issue: bool = True) -> dict:
    """The public text for an issue (issue=True) or a board post: a title and
    a body, from the fault's own fields only. Returns {title, body, removed,
    left_out, url?}; url is the new-issue link carrying the text."""
    removed: set = set()
    t = scrub_public(title, removed, names)[:200]
    parts = []
    for key, label in PUBLIC_FIELDS:
        v = {"problem": problem, "repro_steps": repro_steps, "expected": expected,
             "actual": actual}[key]
        v = scrub_public(v, removed, names)
        if v:
            parts.append(f"### {label}\n\n{v}")
    if version:
        parts.append(f"### Version\n\nC-Series {scrub_public(version, removed, names)}")
    body = "\n\n".join(parts)
    out = {"title": t, "body": body, "removed": sorted(removed),
           "left_out": ["memory", "conversations", "files", "errors and model-call failures",
                        "harness runs", "project", "what you were doing (context)",
                        "recommendation and pushback"]}
    if issue:
        out["url"], cut = _issue_url(t, body)
        if cut:
            out["link_cut"] = True
    return out


LINK_CUT_MARK = "\n\n[cut to fit the link: paste the rest yourself]"


def _issue_url(title: str, body: str) -> tuple[str, bool]:
    """The new-issue link carrying the text, and whether the body had to be
    cut to fit it (the full text is still in the draft the Host shows)."""
    base = f"{ISSUE_NEW_URL}?title={quote(title)}&body="
    url = base + quote(body)
    if len(url) <= ISSUE_URL_MAX:
        return url, False
    keep = len(body)
    while keep > 0 and len(base + quote(body[:keep] + LINK_CUT_MARK)) > ISSUE_URL_MAX:
        keep -= max(1, (len(base + quote(body[:keep] + LINK_CUT_MARK)) - ISSUE_URL_MAX) // 3)
    return base + quote(body[:max(0, keep)] + LINK_CUT_MARK), True


def board_category(cats) -> str:
    """The board category a help post goes in, from community.categories'
    answer (slugs are communityd's): the first whose slug or name speaks of
    help, support, bugs or questions, else the first."""
    rows = cats.get("categories") if isinstance(cats, dict) else cats
    rows = rows if isinstance(rows, list) else []
    def _slug(c):
        return str((c.get("slug") or c.get("id") or c.get("name")) if isinstance(c, dict) else c)
    for word in ("help", "support", "bug", "question"):
        for c in rows:
            label = (f"{c.get('slug', '')} {c.get('name', '')}" if isinstance(c, dict) else str(c)).lower()
            if word in label:
                return _slug(c)
    return _slug(rows[0]) if rows else ""
