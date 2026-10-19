"""mastercode.embed_windows — embed a text of ANY length as one vector, never a
cut one.

Every embed call on the context lane has sent at most EMBED_INPUT_CHARS of a
text since 2026-06 (server/routes/context.py, server/routes/embed.py). Past
that the rest was sliced off with nothing said, so the end of a long row could
never be found by meaning.

A text longer than the window is now split into windows of that size, each
window is embedded, and the vectors are combined into ONE vector, weighted by
each window's length. That is the published method for inputs longer than an
embedding model takes (OpenAI cookbook, "Embedding texts that are longer than
the model's maximum context length": chunk, embed each chunk, average weighted
by chunk size). The row keeps one vector, so nothing about the table changes,
and a text that fits one window is embedded exactly as before — its vector is
byte-for-byte the one the old code produced, so no existing embedding moves.

Every distance on these columns is cosine (`<=>`), which a vector's length does
not change; the combined vector is still rescaled to the windows' mean length
so it sits in the same range as a single-call vector.

A window that fails to embed fails the whole text (None): a vector over part of
a text would look complete and is not. The row stays unembedded and coverage
reports it, the same as any other failed embed.

Stdlib only.
"""
from __future__ import annotations

import math
from typing import Awaitable, Callable, Optional, Sequence

# The window every context embed call already sends (unchanged since 2026-06;
# the embed lane's existing per-call size, not a new number). A longer text is
# windowed at this size, never cut.
EMBED_INPUT_CHARS = 8000


def windows(text: str, size: int = EMBED_INPUT_CHARS) -> list[str]:
    """`text` as consecutive windows of at most `size` characters that together
    hold every character of it. A window ends on whitespace when there is any
    in its last tenth, so a word is not split across two windows."""
    text = text or ""
    if size <= 0:
        raise ValueError("window size must be positive")
    if len(text) <= size:
        return [text]
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        end = min(i + size, n)
        if end < n:
            cut = max(text.rfind(" ", i + size - size // 10, end),
                      text.rfind("\n", i + size - size // 10, end))
            if cut > i:
                end = cut + 1
        out.append(text[i:end])
        i = end
    return out


def combine(vectors: Sequence[Sequence[float]], weights: Sequence[int]) -> Optional[list[float]]:
    """One vector from window vectors: the weighted mean direction, scaled to
    the windows' weighted mean length. None when the vectors disagree on size
    (two models answered) or there are none."""
    if not vectors or len(vectors) != len(weights):
        return None
    dim = len(vectors[0])
    if dim == 0 or any(len(v) != dim for v in vectors):
        return None
    total = float(sum(weights)) or 1.0
    acc = [0.0] * dim
    mean_norm = 0.0
    for v, w in zip(vectors, weights):
        share = w / total
        mean_norm += share * math.sqrt(sum(x * x for x in v))
        for k, x in enumerate(v):
            acc[k] += share * x
    norm = math.sqrt(sum(x * x for x in acc))
    if norm == 0.0:
        return acc
    scale = mean_norm / norm
    return [x * scale for x in acc]


async def embed_whole(text: str,
                      embed_one: Callable[[str], Awaitable[Optional[Sequence[float]]]],
                      size: int = EMBED_INPUT_CHARS) -> Optional[list[float]]:
    """Embed every character of `text` through `embed_one` (one call per
    window). A text that fits one window is one call with the text unchanged."""
    parts = windows(text, size)
    if len(parts) == 1:
        vec = await embed_one(parts[0])
        return list(vec) if vec else None
    vecs = []
    for part in parts:
        vec = await embed_one(part)
        if not vec:
            return None
        vecs.append(vec)
    return combine(vecs, [len(p) for p in parts])


def to_literal(vec: Optional[Sequence[float]]) -> Optional[str]:
    """A pgvector literal, or None."""
    if not vec:
        return None
    return "[" + ",".join(str(x) for x in vec) + "]"
