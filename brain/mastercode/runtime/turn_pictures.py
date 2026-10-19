"""Pictures a tool hands to the Host's own turn.

Images are not separate; they are part of the Host. A picture the Host's tools produce or fetch (an app's render, one of its
own stored images, a picture the person pasted earlier) goes to the Host's own
model, the way a picture the person pastes does, instead of being described by
a second model on a second route.

How a picture travels:

  - the tool calls `offer(...)` with the picture's bytes and one line naming
    what it is;
  - the Host's loop (session.py) `drain()`s after each tool call and, once the
    round's tool results are in, adds ONE user-role message holding a label and
    the pictures;
  - provider_gateway converts that message like any pasted picture
    (attachment_parts.for_wire): an image block on the Anthropic wire, an
    image_url part on the OpenAI wire, a note for a model whose catalog row
    says it reads no pictures, and one re-dial with notes if the provider
    refuses the request.

The pictures never ride inside a tool result's text, so nothing that bounds,
stores, logs or replays a tool result ever carries their base64.

Who receives pictures: a Host turn (`begin_turn`, at
session.EntityRuntime.send_message). A dispatched agent runs under
`begin_agent`, which takes none — an agent's tools describe a picture in words
and keep it in the gallery. Anything running outside a turn takes none either.
A tool must therefore always return text that stands on its own, and treat a
False from `offer` as "say where the picture is kept instead".

Untrusted pictures (a page on the web) never come through here: they are
looked at in a separate call that holds no tools (image_engine._look_fenced),
as web_search does with pages.
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field


@dataclass
class _Pending:
    pictures: list[dict] = field(default_factory=list)
    # Closed when the turn ends. A background task the turn started (an app
    # call's watch) holds this same object; once the turn is over, its offers
    # are refused, so its text never claims a picture "is shown" to a turn that
    # will never draw it.
    open: bool = True
    # Pictures a provider refused in this turn, by the route that refused them:
    # (provider, model) → {sha256 of the base64 → the refusal, as the note says
    # it}. Never sent to that route again this turn, so a refusal costs one
    # call, not one per round (36 in one turn). Another
    # route (the failover) is not bound by it: its provider has its own limits.
    refused: dict = field(default_factory=dict)


_PENDING: ContextVar[_Pending | None] = ContextVar("turn_pictures", default=None)


def begin_turn() -> Token:
    """A Host turn takes pictures. Returns the token for end()."""
    return _PENDING.set(_Pending())


def begin_agent() -> Token:
    """A dispatched agent's run takes none (its tools answer in words)."""
    return _PENDING.set(None)


def end(token: Token) -> None:
    st = _PENDING.get()
    if st is not None:
        st.open = False
    try:
        _PENDING.reset(token)
    except (ValueError, RuntimeError):  # pragma: no cover - a token from another context
        _PENDING.set(None)


def wanted() -> bool:
    """True inside a running Host turn: a picture offered now reaches the Host's model."""
    st = _PENDING.get()
    return st is not None and st.open


def offer(media_type: str, data_b64: str, label: str) -> bool:
    """Hand one picture to the running Host turn. False when no Host turn takes
    pictures here (an agent, a background job): the caller's text must then say
    where the picture is kept."""
    st = _PENDING.get()
    if st is None or not st.open or not data_b64:
        return False
    st.pictures.append({"media_type": media_type or "image/png",
                        "data": data_b64, "label": (label or "a picture")[:300]})
    return True


def note_refused(route, shas, why: str) -> None:
    """`route` ((provider, model)) refused a request carrying these pictures
    and took the same request without them (provider_gateway). For the rest of
    this turn they go to that route as notes saying so."""
    st = _PENDING.get()
    if st is None:
        return
    mine = st.refused.setdefault(tuple(route), {})
    for s in shas or ():
        mine.setdefault(s, why)


def refused(route) -> dict:
    """The pictures `route` refused this turn (sha256 → why); empty outside a turn."""
    st = _PENDING.get()
    return dict(st.refused.get(tuple(route), {})) if st is not None else {}


def drain() -> list[dict]:
    """The pictures offered since the last drain, in the order offered."""
    st = _PENDING.get()
    if st is None or not st.pictures:
        return []
    out, st.pictures = st.pictures, []
    return out


def message_for(pictures: list[dict]) -> dict | None:
    """The one user-role message that carries a round's pictures to the model.
    The label says plainly that these are what the tools returned, not words
    from the person. Image blocks are Anthropic-native; attachment_parts
    converts them for the wire actually dialled."""
    if not pictures:
        return None
    lines = [f"{i}. {p['label']}" for i, p in enumerate(pictures, 1)]
    parts: list[dict] = [{"type": "text", "text": (
        "[What your tools returned this round, as pictures, in this order. "
        "They come from your tools, not from the person:\n" + "\n".join(lines) + "]")}]
    for p in pictures:
        parts.append({"type": "image", "source": {
            "type": "base64", "media_type": p["media_type"], "data": p["data"]},
            "_title": p["label"]})
    return {"role": "user", "content": parts}
