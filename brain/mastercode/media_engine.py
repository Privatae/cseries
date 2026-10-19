"""
Media embeddings — pictures and video, embedded the same way text is.

Media is treated no differently than text — either a model on the
user's own network or an endpoint to call.

So this module is memory_engine's embed lane with three differences, and it
exists as its own file rather than more branches inside memory_engine because
each difference is load-bearing:

 1. ITS OWN COLUMN FAMILY. A media model's vectors are a different space from
    the text model's even at the same dim, so they can never share a column —
    see MEDIA_COLUMN_BY_DIM below and migrations/v135.

 2. NO SINGLE WIRE FORMAT. Text got away with one body shape because OpenAI and
    Ollama converged on {"model", "input"} (memory_engine.py:503). Image
    embedding did not converge. Checked against the vendors' own docs
    2026-09-12: Jina takes `input: [{"image": "data:..."}]`; Cohere embed v4
    takes `input_type: "image"` beside an `images: ["data:..."]` array (or
    `inputs[].content[]` for mixed text+image). Those two cannot be served by
    one body. So the shape is MEASURED at switch time the same way the dim
    already is (server/routes/embedding.py:456-471 does exactly this for MRL
    truncation) and the winner is persisted. No vendor table to keep accurate,
    no pin, and a vendor we have never heard of works the moment one of the
    shapes fits.

 3. TWO TOWERS, ONE SPACE. Searching pictures with typed words means the query
    goes through the model's TEXT tower and lands in the same space as the
    image vectors. A model that embeds images but whose text tower answers at a
    different dim cannot serve that search at all, so the probe below gates on
    BOTH towers and refuses the switch otherwise — rather than shipping a
    search that silently returns noise.

NOT SET UP IS NOT AN OUTAGE. No media model
configured means pictures are stored and findable by filename, and the honest
sentence is "not set up yet". There is no platform media embedder, no caption
workaround standing in as the design, and no default model anywhere in here.

BASTION LANE NOTE. A bastion target is reached by forward-dialling its endpoint
through the Hub's host→peer forward (memory_engine._post_json_via_peer_forward,
which carries an arbitrary payload), NOT through the embed reverse relay: that
relay's frame is hardcoded to {"model", "input": text} at
server/network/embed_relay.py:96, so it cannot carry an image body until the
bastion side learns a media frame. Forward-dial is the path the text lane's own
switch-time probe already uses for bastions (server/routes/embedding.py:174-182),
so this is an exercised route, not a new one.

Import-safe: no heavy deps at import time.
"""
from __future__ import annotations

import base64
import logging
from typing import Optional

log = logging.getLogger("privatae.media_engine")


# ── Storage ──────────────────────────────────────────────────────────────────

# Dim → user_context column. Mirrors memory_engine.EMBEDDING_COLUMN_BY_DIM's job
# for the media family, and every value MUST be a real column (migrations/v135 +
# the lazy USER_CONTEXT_SCHEMA in server/routes/context.py — BOTH, or a host
# created in the gap between them has nowhere to put a vector, which is the 2560
# bug v071 left behind).
#
# The dims are what the candidate models actually output, not the text set:
#   512  X-CLIP, CLIP ViT-B      768  SigLIP 2 base, nomic-embed-vision
#   1024 jina-clip-v2, Voyage     1152 SigLIP 2 so400m
#   1536 Cohere embed v4          3072 Gemini Embedding native
MEDIA_COLUMN_BY_DIM = {
    512:  "media_embedding_512",
    768:  "media_embedding_768",
    1024: "media_embedding_1024",
    1152: "media_embedding_1152",
    1536: "media_embedding_1536",
    3072: "media_embedding_3072",
}


def media_cast_type(dim: int) -> str:
    """SQL type to cast a media embedding literal to. ALWAYS halfvec — the whole
    family is halfvec, so there is no dim-dependent branch here the way
    memory_engine.embedding_cast_type needs one. One type means one cast rule
    and one opclass, and 3072 is under pgvector's 4000d halfvec HNSW cap, so
    unlike embedding_4096 no media column is ever left unindexed."""
    return f"halfvec({dim})"


# Truncation targets for a model whose native dim is off the grid above, tried
# largest-first so the user keeps as much of their model as fits. Same move the
# text lane makes at server/routes/embedding.py:457-466, and the same rule
# applies: a truncated dim is persisted ONLY after the endpoint provably serves
# it, never because the arithmetic works out.
MEDIA_MRL_CANDIDATE_DIMS = sorted(MEDIA_COLUMN_BY_DIM, reverse=True)


# ── The probe image ──────────────────────────────────────────────────────────

# A real 16x16 PNG in four coloured quadrants — structurally valid (signature,
# IHDR/IDAT/IEND with correct CRCs, 89 bytes) and small enough that a cold model
# load stays cheap. Deliberately not 1x1: encoders that reject degenerate input
# would fail the probe for the wrong reason, and a probe that fails for the
# wrong reason teaches the user nothing. Never stored anywhere.
MEDIA_PROBE_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAIElEQVR42mP4/78BK2LAiUY1"
    "0EQDQwNWxIALjWqghQYAOv5/EFnQdUYAAAAASUVORK5CYII="
)
MEDIA_PROBE_DATA_URI = "data:image/png;base64," + MEDIA_PROBE_PNG_B64

# ── The two-tower discriminator ────────────────
#
# Matching widths CANNOT tell an image tower from a text tower, and the first
# version of this gate believed they could. The hole was concrete: point the
# media role at a TEXT model — Ollama serving nomic-embed-text, say. The wire
# hands it {"input": ["data:image/png;base64,..."]}, which is a STRING. A text
# model embeds any string, so it answers 768d; the text probe also answers
# 768d; the widths match and the switch is accepted. Every picture is then
# embedded as base64 gibberish, search ranks pictures against nonsense, and
# coverage reports them as searchable. The skill promises "the host refuses
# rather than doing it", so the code has to be able to.
#
# ALT is the SAME PICTURE — byte-for-byte identical pixels, verified by
# inflating and unfiltering both IDATs — re-encoded with a different PNG row
# filter, a different zlib level, and a tEXt comment. So:
#
#   an IMAGE tower decodes both to one pixel buffer   -> cosine ~ 1.0
#   a TEXT  tower sees a 120-char string and an 856-char one that share only a
#           47-char prefix                            -> nowhere near it
#
# The picture is held constant and the bytes are varied, which is exactly the
# difference between the two kinds of model.
MEDIA_PROBE_PNG_ALT_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAACEnRFWHRDb21tZW50AHByaXZh"
    "dGFlIGNhcGFiaWxpdHkgcHJvYmU7IHBpeGVscyBpZGVudGljYWwgdG8gdGhlIGZpcnN0LiBw"
    "cml2YXRhZSBjYXBhYmlsaXR5IHByb2JlOyBwaXhlbHMgaWRlbnRpY2FsIHRvIHRoZSBmaXJz"
    "dC4gcHJpdmF0YWUgY2FwYWJpbGl0eSBwcm9iZTsgcGl4ZWxzIGlkZW50aWNhbCB0byB0aGUg"
    "Zmlyc3QuIHByaXZhdGFlIGNhcGFiaWxpdHkgcHJvYmU7IHBpeGVscyBpZGVudGljYWwgdG8g"
    "dGhlIGZpcnN0LiBwcml2YXRhZSBjYXBhYmlsaXR5IHByb2JlOyBwaXhlbHMgaWRlbnRpY2Fs"
    "IHRvIHRoZSBmaXJzdC4gcHJpdmF0YWUgY2FwYWJpbGl0eSBwcm9iZTsgcGl4ZWxzIGlkZW50"
    "aWNhbCB0byB0aGUgZmlyc3QuIHByaXZhdGFlIGNhcGFiaWxpdHkgcHJvYmU7IHBpeGVscyBp"
    "ZGVudGljYWwgdG8gdGhlIGZpcnN0LiBwcml2YXRhZSBjYXBhYmlsaXR5IHByb2JlOyBwaXhl"
    "bHMgaWRlbnRpY2FsIHRvIHRoZSBmaXJzdC4gcHJpdmF0YWUgY2FwYWJpbGl0eSBwcm9iZTsg"
    "cGl4ZWxzIGlkZW50aWNhbCB0byB0aGUgZmlyc3QuIJd6JnsAAAArSURBVHgBY/r/vwErYvjf"
    "gBUxMZAIRjUQE2BMDIwMJKHRYCUuWIlRhaQGAFwNIBlhuTdEAAAAAElFTkSuQmCC"
)
MEDIA_PROBE_ALT_DATA_URI = "data:image/png;base64," + MEDIA_PROBE_PNG_ALT_B64

# A DIFFERENT picture (the same 16x16 layout with every channel inverted) for
# the other half of the discriminator: an endpoint that ignores its input and
# answers with one constant vector sails through the identical-pixels check
# above, because a constant is trivially identical to itself. This is the
# control that catches it. The bar sits almost at 1.0 on purpose — the question
# is only "did the answer change AT ALL when the picture changed", never "are
# these two pictures far enough apart", which is the model's business.
MEDIA_PROBE_PNG_DIFF_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAIUlEQVR42mNgYKjHiv7jQAyj"
    "Gmij4X89VvQfBxrVQBMNAHe/fhDiLHTHAAAAAElFTkSuQmCC"
)
MEDIA_PROBE_DIFF_DATA_URI = "data:image/png;base64," + MEDIA_PROBE_PNG_DIFF_B64

# Identical pixels through an image tower are identical tensors, so the cosine
# is 1.0 up to float noise; 0.999 leaves room for a nondeterministic kernel and
# none for a text model. Two base64 strings this different do not land here.
#
# CHOSEN, NOT MEASURED AGAINST A REAL TEXT MODEL. The stand-in in
# scripts/test_media_probe_discriminates.py scores 0.759, but it hashes its
# input, which is the easiest case there is and says nothing about the margin a
# real sentence-transformer would leave. The structural argument is what this
# rests on: as data URIs the two probes are 142 and 878 characters sharing a
# 69-character prefix, and 0.999 needs near-identical token sequences. Someone
# with a real text embedder to hand should run the probe against it once and
# write the number here.
MEDIA_SAME_PICTURE_MIN_COS = 0.999
# A changed picture must move the answer at all. Only a literally constant
# vector fails this.
MEDIA_DIFF_PICTURE_MAX_COS = 0.9999

# The largest ORIGINAL picture that goes out in one embed call.
#
# CHOSEN, not measured against any vendor's published limit. A picture travels
# as a data URI, so base64 inflates it by a third: 10 MiB of photo is a ~13.4
# MiB request body, which is around where ordinary HTTP services start refusing. The cap matters because a refusal is not a slow row
# — the fill picks rows by "media column IS NULL", so a picture the endpoint
# will not accept is retried on every job, for ever. Skipping it lets the rest
# of the batch finish and leaves one honest gap in coverage.
#
# THE REAL FIX IS DOWNSCALING, not a bigger number here: CLIP-family encoders
# resize to a few hundred pixels anyway, so a 25 MiB photo and a 2 MiB one
# produce the same tensor, and the extra bytes are shipped only to be thrown
# away. A 384px downscale measured ~10x smaller. Until that lands
# this is a stopgap, and a photo past the cap is skipped rather than silently
# retried into the ground.
MEDIA_MAX_EMBED_BYTES = 10 * 1024 * 1024

# One deterministic phrase for the TEXT tower half of the probe. Its content is
# irrelevant — what is being measured is whether the text tower answers, and at
# what dim.
MEDIA_PROBE_TEXT = "privatae media embedding capability probe"


# ── The wire ─────────────────────────────────────────────────────────────────
#
# Each entry builds the request body for one vendor family. `image` takes a
# data URI ("data:image/png;base64,..."); `text` takes a query string. The
# NAME is what gets persisted, so a host always sends the shape its probe
# proved — never a re-guess at call time.

def _wire_openai(model: str, *, image: str | None = None, text: str | None = None) -> dict:
    """OpenAI-shaped servers that took CLIP models on faith — Infinity, TEI and
    the self-hosted crowd: the image rides in `input` as a plain data URI or
    URL, indistinguishable in shape from a text input."""
    return {"model": model, "input": [image if image is not None else text]}


def _wire_jina(model: str, *, image: str | None = None, text: str | None = None) -> dict:
    """Jina (jina-clip-v2). Confirmed against docs.jina.ai 2026-09-12: `input`
    is an array of OBJECTS, and an image is the `image` key holding a URL or a
    data URI."""
    item = {"image": image} if image is not None else {"text": text}
    return {"model": model, "input": [item]}


def _wire_cohere(model: str, *, image: str | None = None, text: str | None = None) -> dict:
    """Cohere embed v4. Confirmed 2026-09-12: an image-only call sets
    input_type='image' beside an `images` array of data URIs; the text tower is
    the ordinary `texts` array with a query input_type. embedding_types is
    named explicitly so the response shape is predictable."""
    if image is not None:
        return {"model": model, "input_type": "image", "images": [image],
                "embedding_types": ["float"]}
    return {"model": model, "input_type": "search_query", "texts": [text],
            "embedding_types": ["float"]}


def _wire_voyage(model: str, *, image: str | None = None, text: str | None = None) -> dict:
    """Voyage multimodal. The docs page pointed at a reference we could not read
    2026-09-12, so this shape is a CANDIDATE the probe either proves or
    discards — it is never assumed to be right. That is the whole reason the
    wire is measured rather than tabulated."""
    part = ({"type": "image_base64", "image_base64": image} if image is not None
            else {"type": "text", "text": text})
    return {"model": model, "inputs": [{"content": [part]}]}


def _wire_ollama_images(model: str, *, image: str | None = None, text: str | None = None) -> dict:
    """Ollama-shaped local servers that carry images in a sibling `images` array
    of RAW base64 (no data: prefix) — the same split its chat API uses."""
    if image is not None:
        raw = image.split(",", 1)[1] if image.startswith("data:") else image
        return {"model": model, "input": "", "images": [raw]}
    return {"model": model, "input": text}


MEDIA_WIRES = {
    "openai": _wire_openai,
    "jina": _wire_jina,
    "cohere": _wire_cohere,
    "voyage": _wire_voyage,
    "ollama_images": _wire_ollama_images,
}

# Probe order. Nothing here is a preference ranking between vendors — it is
# just the order the shapes are tried in until one answers.
MEDIA_WIRE_ORDER = ("openai", "jina", "cohere", "voyage", "ollama_images")


def _vector_from(data) -> Optional[list[float]]:
    """Pull the vector out of whatever the endpoint answered with. Covers the
    three shapes memory_engine._embed_via_byo already handles (OpenAI `data[]`,
    Ollama `embedding`, Ollama-batch `embeddings[]`) plus Cohere's
    `embeddings.float[]`, which is a dict where the others are a list."""
    if not isinstance(data, dict):
        return None
    d = data.get("data")
    if isinstance(d, list) and d and isinstance(d[0], dict):
        vec = d[0].get("embedding")
        if vec:
            return list(vec)
    emb = data.get("embeddings")
    if isinstance(emb, dict):
        # Cohere: {"embeddings": {"float": [[...]]}}
        fl = emb.get("float")
        if isinstance(fl, list) and fl:
            return list(fl[0])
    if isinstance(emb, list) and emb:
        first = emb[0]
        if isinstance(first, dict):          # Voyage: [{"embedding": [...]}]
            vec = first.get("embedding")
            if vec:
                return list(vec)
        if isinstance(first, list):
            return list(first)
    if data.get("embedding"):
        return list(data["embedding"])
    return None


def _resolve_url(base_url: str) -> str:
    """The embeddings route for `base_url`, mirroring memory_engine's rule
    (:488-502) so a bare host:port behaves the same on both lanes. A provider
    spec that names a full route arrives here already resolved and falls
    through the first branch untouched."""
    url = (base_url or "").rstrip("/")
    low = url.lower()
    if (low.endswith("/embeddings") or low.endswith("/embed")
            or low.endswith("/multimodalembeddings")):
        return url
    if low.endswith("/v1"):
        return url + "/embeddings"
    if low.endswith("/api"):
        return url + "/embed"
    return url + "/v1/embeddings"


async def _post(url: str, payload: dict, cfg: dict, *, entity_id=None, pool=None,
                timeout: float = 60.0) -> tuple[int, dict]:
    """POST `payload`, over whichever of the three transports this config names:
    a brokered call when a sealed token is registered (the raw key never reaches
    this layer), the Hub peer-forward for a bastion on the WG overlay, or a
    plain session otherwise. Same three-way split as
    memory_engine._embed_via_byo:509-542, deliberately — the media lane must not
    invent a fourth way to reach the user's endpoint.

    Returns (status, parsed_json). status is 0 when the transport itself failed.
    """
    cred_name = cfg.get("cred_name")
    try:
        if cred_name:
            from server import brain_service as _bs
            from server.vault import authz as _authz
            # brain_service.get_pool() is Optional (brain_service.py:207) — a
            # pre-init pod has none and use_credential would fault on it.
            # _entity_id is NOT optional (brain_service.py:81 declares
            # `_entity_id: int = 1`), so it needs no guard: one was written
            # here once and was dead code.
            _pool = pool or _bs.get_pool()
            if _pool is None:
                log.warning("media embed: no brain pool for the brokered lane")
                return 0, {}
            _eid = entity_id if entity_id is not None else _bs._entity_id
            res = await _authz.use_credential(
                _pool, entity_id=_eid, name=cred_name, action="http_call",
                request={"url": url, "method": "POST", "json": payload,
                         "auth_scheme": "bearer"},
                actor=f"media_embed:{_eid}")
            return int(res.get("status") or 200), (res.get("json") or {})
        if cfg.get("peer_forward"):
            from mastercode.memory_engine import _post_json_via_peer_forward
            return await _post_json_via_peer_forward(
                cfg["peer_forward"], url, payload, timeout_s=timeout)
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                    url, json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = {}
                return resp.status, (body if isinstance(body, dict) else {})
    except Exception as exc:
        # Name the type: asyncio.TimeoutError stringifies to '' and the text
        # lane spent a whole incident reading "media embed failed url=...: ".
        log.warning("media embed failed url=%s: %s: %s", url, type(exc).__name__, exc)
        return 0, {}


async def embed_media(cfg: dict, *, image: str | None = None, text: str | None = None,
                      entity_id=None, pool=None,
                      timeout: float = 60.0) -> Optional[list[float]]:
    """ONE media embed against the host's own endpoint, in the wire shape its
    switch-time probe proved. Exactly one of `image` (a data URI) or `text` (a
    search query, routed to the model's text tower) — passing neither or both is
    a caller bug and returns None.

    Returns the vector, or None. Never raises for an endpoint problem: an
    unreachable endpoint is the USER's endpoint being down, which the caller
    surfaces as coverage, not as a crash.
    """
    if (image is None) == (text is None):
        log.warning("embed_media: pass exactly one of image/text")
        return None
    model = (cfg.get("model") or "").strip()
    base_url = (cfg.get("base_url") or "").strip()
    if not model or not base_url:
        return None
    builder = MEDIA_WIRES.get(cfg.get("wire") or "")
    if builder is None:
        # A config with no proven wire cannot be embedded with. This is the
        # unconfigured state, not a fallback opportunity — guessing a shape here
        # is how a lane starts writing vectors nobody can search.
        log.info("embed_media: no proven wire on this config — not guessing one")
        return None
    payload = builder(model, image=image, text=text)
    if cfg.get("dimensions"):
        # Only ever set when the probe verified the endpoint honours it.
        payload["dimensions"] = int(cfg["dimensions"])
    url = _resolve_url(base_url)
    status, data = await _post(url, payload, cfg, entity_id=entity_id, pool=pool,
                               timeout=timeout)
    if status != 200:
        if status:
            log.warning("media embed HTTP %s for %s", status, url)
        return None
    return _vector_from(data)


def _cosine(a: list, b: list) -> Optional[float]:
    """Cosine similarity, or None when either vector is degenerate (different
    widths, or an all-zero vector that has no direction to compare)."""
    if not a or not b or len(a) != len(b):
        return None
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return None
    return dot / (na * nb)


async def probe_media_endpoint(base_url: str, model: str, *,
                               cred_name: str | None = None,
                               peer_forward: str | None = None,
                               dimensions: int | None = None,
                               entity_id=None,
                               pool=None) -> tuple[Optional[str], Optional[int], str]:
    """Find the wire shape and dim this endpoint actually serves `model` at, by
    embedding a real picture over the exact path production embeds take.

    Returns (wire_name, dim, reason). wire_name None means the endpoint could
    not serve the model in ANY known shape, and `reason` says what happened.

    THREE THINGS ARE GATED, and the first exists because matching widths do not
    imply an image tower:

    1. IT READS PICTURES, NOT FILES. The same picture is sent twice in different
       bytes; an image tower decodes both to one pixel buffer and answers the
       same way, a text model embedding the data-URI string does not. Without
       this a text model passes on width alone and every picture ends up
       embedded as base64 gibberish.
    2. IT ACTUALLY LOOKS. A second, different picture must move the answer at
       all — an endpoint returning one constant vector would otherwise pass (1)
       trivially, a constant being identical to itself.
    3. BOTH TOWERS SHARE A SPACE. The same wire's TEXT tower must answer at the
       SAME dim. A model whose towers disagree cannot answer "find the picture
       of the whiteboard": the query vector would land in a different space from
       the pictures.

    All three are refused HERE rather than at search time, when the only symptom
    would be irrelevant results and a coverage number claiming success.
    """
    cfg = {"base_url": base_url, "cred_name": cred_name,
           "peer_forward": peer_forward, "dimensions": dimensions}
    url = _resolve_url(base_url)
    tried: list[str] = []
    # A wire that answered but failed a discriminator has a reason worth more
    # than the generic "nothing reached it" at the bottom — keep it.
    refusals: list[str] = []
    for name in MEDIA_WIRE_ORDER:
        builder = MEDIA_WIRES[name]
        payload = builder(model, image=MEDIA_PROBE_DATA_URI)
        if dimensions:
            payload["dimensions"] = int(dimensions)
        status, data = await _post(url, payload, cfg, entity_id=entity_id,
                                   pool=pool, timeout=60.0)
        vec = _vector_from(data) if status == 200 else None
        if not vec:
            tried.append(f"{name}:{status or 'unreachable'}")
            continue
        img_dim = len(vec)
        # ── Is this an IMAGE tower at all? ──────────────────────────────────
        # Width alone cannot answer that (see the ALT probe's note): a text
        # model embeds the data-URI STRING happily and at a matching width.
        # Send the same picture again in different bytes — an image tower
        # decodes both to one pixel buffer and must answer the same way.
        apayload = builder(model, image=MEDIA_PROBE_ALT_DATA_URI)
        if dimensions:
            apayload["dimensions"] = int(dimensions)
        astatus, adata = await _post(url, apayload, cfg, entity_id=entity_id,
                                     pool=pool, timeout=60.0)
        avec = _vector_from(adata) if astatus == 200 else None
        same = _cosine(vec, avec) if avec else None
        if same is None:
            return (None, None,
                    f"'{model}' answered the first picture at {img_dim}d but "
                    f"could not be checked with a second (HTTP "
                    f"{astatus or 'unreachable'}) — without that check there is "
                    f"no way to tell a picture model from a text one")
        if same < MEDIA_SAME_PICTURE_MIN_COS:
            # `continue`, not `return`: a genuinely multimodal model whose
            # OpenAI-shaped path treats a bare string as TEXT would fail here on
            # the FIRST wire while its native shape, further down
            # MEDIA_WIRE_ORDER, would pass. Moving on costs nothing in safety —
            # a text-only model does not slip through, because the object-bodied
            # shapes reject it and the ollama_images shape sends the same empty
            # string for all three probes, which the constant check below
            # refuses. The refusal is remembered so the reason a user reads is
            # this one, not a bare "no known request shape reached it".
            tried.append(f"{name}:reads-the-file({same:.3f})")
            refusals.append(
                f"'{model}' is reading the FILE, not the picture: handed the "
                f"same image encoded two ways it gave two different answers "
                f"(similarity {same:.3f}) in the {name} shape. That is what a "
                f"text-embedding model does with the encoded bytes, and it "
                f"would make every picture searchable only by gibberish. Point "
                f"the media role at an image model")
            continue
        # And it must actually LOOK: an endpoint returning one constant vector
        # passes the check above trivially, since a constant matches itself.
        dpayload = builder(model, image=MEDIA_PROBE_DIFF_DATA_URI)
        if dimensions:
            dpayload["dimensions"] = int(dimensions)
        dstatus, ddata = await _post(url, dpayload, cfg, entity_id=entity_id,
                                     pool=pool, timeout=60.0)
        dvec = _vector_from(ddata) if dstatus == 200 else None
        diff = _cosine(vec, dvec) if dvec else None
        if diff is not None and diff > MEDIA_DIFF_PICTURE_MAX_COS:
            # Record and try the next wire, for the same reason check 1 does —
            # and so the REASON the user finally reads is the most informative
            # one across all wires, not whichever wire happened to be last. A
            # constant endpoint fails this on every shape, so continuing cannot
            # let one through.
            tried.append(f"{name}:constant({diff:.5f})")
            refusals.append(
                f"'{model}' returned the same vector for two different "
                f"pictures (similarity {diff:.5f}) in the {name} shape — it is "
                f"not looking at what it is sent, so nothing embedded with it "
                f"could be searched")
            continue
        # Text tower, same wire.
        tpayload = builder(model, text=MEDIA_PROBE_TEXT)
        if dimensions:
            tpayload["dimensions"] = int(dimensions)
        tstatus, tdata = await _post(url, tpayload, cfg, entity_id=entity_id,
                                     pool=pool, timeout=60.0)
        tvec = _vector_from(tdata) if tstatus == 200 else None
        if not tvec:
            return (None, None,
                    f"'{model}' embedded the picture at {img_dim}d in the {name} "
                    f"shape, but its text tower did not answer (HTTP "
                    f"{tstatus or 'unreachable'}) — without it, typed words "
                    f"cannot search pictures")
        if len(tvec) != img_dim:
            return (None, None,
                    f"'{model}' embeds pictures at {img_dim}d but text at "
                    f"{len(tvec)}d — the two do not share a space, so words "
                    f"cannot search pictures with this model")
        return name, img_dim, f"served in the {name} shape at {img_dim}d"
    if refusals:
        # It ANSWERED — it just isn't a picture model. Say that, rather than
        # "nothing reached it", which would send the user to check the address.
        return None, None, refusals[0]
    return (None, None,
            f"no known request shape reached '{model}' at {url} (tried "
            + ", ".join(tried) + ")")


# ── Config ───────────────────────────────────────────────────────────────────

async def media_config(pool, entity_id) -> dict:
    """The host's media-embedding config, or {} when it is not set up.

    Keys mirror the text lane's behavioral_params names with a media_ prefix, so
    the two are read the same way and neither can ever pick up the other's
    endpoint by accident. {} is the honest unconfigured answer — there is no
    default model, no default endpoint, and no inheriting the text model (whose
    vectors live in a different space anyway).
    """
    cfg, _unreadable = await media_config_state(pool, entity_id)
    return cfg


async def media_config_state(pool, entity_id) -> tuple[dict, bool]:
    """(config, unreadable) — media_config plus the state it cannot express.

    An unreadable config is NOT an unconfigured host, and flattening the two is
    the exact confusion to prevent. _load_behavioral_params marks
    a failed read (memory_engine.py:196-205: "an unreadable config is not an
    unconfigured host"), and media_config drops that mark because its contract
    is a plain dict. So a DB blip mid-job read as "no media model set up": the
    fetch returned no rows, the job reported DONE with every picture still
    unembedded, and coverage told a user who HAD configured a model that they
    had not.

    Callers that must tell the two apart — the job lane and the coverage lens —
    use this. Callers that only need "can I embed right now" keep using
    media_config, where {} is the right answer either way.
    """
    from mastercode.memory_engine import _BP_UNREADABLE_KEY, _load_behavioral_params
    bp = await _load_behavioral_params(pool, entity_id)
    if bp.get(_BP_UNREADABLE_KEY):
        return {}, True
    model = (bp.get("media_embedding_model") or "").strip()
    base_url = (bp.get("media_embedding_base_url") or "").strip()
    wire = (bp.get("media_embedding_wire") or "").strip()
    if not (model and base_url and wire):
        return {}, False
    try:
        dim = int(bp.get("media_embedding_dim") or 0) or None
    except (TypeError, ValueError):
        dim = None
    if dim not in MEDIA_COLUMN_BY_DIM:
        return {}, False
    cfg = {
        "model": model,
        "base_url": base_url,
        "wire": wire,
        "dim": dim,
        "provider": (bp.get("media_embedding_provider") or "").strip() or None,
        "bastion": (bp.get("media_embedding_bastion") or "").strip() or None,
        "cred_name": (bp.get("media_embedding_cred_name") or "").strip() or None,
    }
    if bp.get("media_embedding_dimensions"):
        try:
            cfg["dimensions"] = int(bp["media_embedding_dimensions"])
        except (TypeError, ValueError):
            pass
    if cfg["bastion"]:
        # The bastion's address is re-resolved from its IDENTITY every call, so
        # a re-enrolled box's new address is picked up instead of a dead pin —
        # the same rule the text lane learned at embedding.py:96-102.
        # resolve_endpoint_for_hostname always yields a plain http:// address
        # (embed_backends.py:146 hands resolve_embed_endpoint only reported_ip,
        # so its relay-sentinel branch cannot fire) — which is what this lane
        # wants anyway, since the reverse relay is text-only.
        try:
            from mastercode.embed_backends import resolve_endpoint_for_hostname
            live = await resolve_endpoint_for_hostname(pool, entity_id, cfg["bastion"])
            if live:
                cfg["base_url"] = live
        except Exception as e:
            log.debug("media: bastion endpoint re-resolve failed (%s)", e)
        import os as _os
        cfg["peer_forward"] = (
            (_os.environ.get("HUB_PEER_FORWARD_ADDR") or "").strip() or "[::1]:3129")
    return cfg, False


async def active_media_column(pool, entity_id) -> tuple[Optional[str], Optional[int]]:
    """The user_context column + dim holding this host's media vectors, or
    (None, None) when media embedding is not set up.

    Unlike memory_engine.active_embedding_column, this has NO fallback column.
    The text lane can fall back to the 1024d base column because that column
    predates multi-dim and an unconfigured host never embeds anyway; here a
    fallback would name a column whose space belongs to whatever model wrote it
    last, and searching it would return confident nonsense.
    """
    cfg = await media_config(pool, entity_id)
    if not cfg:
        return None, None
    dim = cfg["dim"]
    return MEDIA_COLUMN_BY_DIM.get(dim), dim


async def active_media_target(pool, entity_id) -> tuple[Optional[str], Optional[int],
                                                        Optional[str]]:
    """active_media_column plus the MODEL those vectors must have come from, or
    (None, None, None) when media embedding is not set up.

    The model is not decoration. Two different models can output the same width
    — 1024d is served by jina-clip-v2, Voyage and SigLIP 2 large among others —
    so the column alone cannot say whether a vector belongs to the model now
    configured. Without the model in the predicate, switching between two
    same-width models leaves every old vector counted as searchable, skipped by
    the fill, and searched with the NEW model's query vector against the OLD
    model's space: confident nonsense, silently.
    """
    cfg = await media_config(pool, entity_id)
    if not cfg:
        return None, None, None
    dim = cfg["dim"]
    return MEDIA_COLUMN_BY_DIM.get(dim), dim, cfg.get("model")


def media_fresh_sql(media_col: str, model_param: str) -> str:
    """SQL predicate for "this row IS embedded for the model configured now".

    `model_param` is the placeholder carrying the active model ($3, say) — never
    an interpolated value. IS NOT DISTINCT FROM so a row whose stamp is NULL
    (written before the stamp existed) reads as belonging to no current model,
    which is what it is.
    """
    return (f"({media_col} IS NOT NULL "
            f"AND media_embed_model IS NOT DISTINCT FROM {model_param})")


def media_stale_sql(media_col: str, model_param: str) -> str:
    """The exact complement of media_fresh_sql: rows the fill still owes work.

    Written as the complement on purpose — when these two disagree a job either
    loops on rows it will not count or reports done with work outstanding.
    """
    return (f"({media_col} IS NULL "
            f"OR media_embed_model IS DISTINCT FROM {model_param})")


# ── WHICH ROWS THE MEDIA FILL OWNS — one definition, three readers ──────────
#
# Two populations embed through this lane and they arrive very differently:
#
#   kind='file'  + an image mime — a PICTURE the user uploaded. Its base64 IS
#                  the stored file. It is theirs, it is kept, and re-embedding
#                  it after a model switch costs nothing.
#   kind='frame' + an image mime — a FRAME derived from a clip. Its
#                  bytes are NOT storage: measured at 416 kB against 4.97 MB per
#                  clip, 12x, so the row keeps the vector and the timestamp and
#                  the picture is thrown away. The bytes exist only as STAGING,
#                  between the moment the user's machine hands them over and the
#                  moment a vector lands.
#
# THE `length(content) > 0` CLAUSE IS THE POINT, and it is what makes one
# predicate safe for both. The fill embeds a row's CONTENT; a row with none can
# never be embedded, and the batch loop counts an empty one failed and advances
# — so selecting it means failing it again on every future fill, for ever, while
# also holding a place in a denominator that can never be satisfied.
#
# It changes one pre-existing behaviour, deliberately: a PICTURE whose content
# is empty used to be selected and failed on every pass. It is now out of the
# fill and out of the fill's denominator. It is still counted as an unsearchable
# picture by the coverage lens, which counts by kind and not by content — so the
# user still sees it and the job stops spinning on it.
#
# A NOTE ON WHAT THIS DOES NOT REPAIR. After a media model switch, a frame whose
# bytes were already dropped is stale with nothing to re-embed from. It is
# excluded here rather than retried for ever; the coverage lens reports the clip
# as unsearchable, and the remedy is to run extraction on the clip again — the
# clip is still stored, and one decode is the honest price of not keeping
# derived bytes. That is a different cost from the one this predicate fixes,
# which is an embedding that failed for a reason having nothing to do with the
# frames.
MEDIA_STAGED_KIND = "frame"


def media_lane_rows_sql() -> str:
    """Every row the MEDIA lane embeds: uploaded pictures and derived frames.

    The coverage lens deliberately does NOT use this — it counts pictures and
    frames in separate denominators so neither can borrow the other's.
    One definition for "what the fill works on", two for "what the user is
    told", and the difference is on purpose.
    """
    return ("metadata->>'kind' IN ('file', 'frame') "
            "AND metadata->>'mime' LIKE 'image/%'")


def media_embeddable_sql(media_col: str, model_param: str) -> str:
    """A media row the fill still owes work AND can actually do work on.

    Read by the fetch and by its denominator — a disagreement between the two
    reports done with work outstanding, or never reaches its own total.
    """
    return (f"{media_lane_rows_sql()} AND length(content) > 0 "
            f"AND {media_stale_sql(media_col, model_param)}")


# The SET fragment that drops a frame's STAGED bytes the moment its vector
# lands. Written once and used by both writers — the inline pass at extraction
# time and the fill — because two spellings of "the bytes are gone now" is how
# one of them ends up keeping 5 MB per clip that nothing ever reads.
#
# A picture is untouched: its content is the user's stored file, not staging.
MEDIA_DROP_STAGED_CONTENT_SQL = (
    "content = CASE WHEN metadata->>'kind' = '" + MEDIA_STAGED_KIND + "' "
    "THEN '' ELSE content END")


# ── What a row's bytes are ───────────────────────────────────────────────────

# Mime prefixes this lane will embed. Everything else (PDFs, spreadsheets,
# archives) stays a stored file exactly as it is today: media embedding does not
# widen what gets vectorized, it adds pictures and video to it.
MEDIA_IMAGE_PREFIX = "image/"
MEDIA_VIDEO_PREFIX = "video/"


def media_kind(mime: str | None) -> Optional[str]:
    """'picture', 'clip', or None for a mime this lane does not handle."""
    m = (mime or "").split(";")[0].strip().lower()
    if m.startswith(MEDIA_IMAGE_PREFIX):
        return "picture"
    if m.startswith(MEDIA_VIDEO_PREFIX):
        return "clip"
    return None


def data_uri(mime: str, b64: str) -> str:
    """The data URI every wire shape above is fed. `b64` is the row's stored
    base64 content, used as-is — re-encoding a multi-megabyte picture to
    normalize it would double the pod's memory for no gain."""
    m = (mime or "application/octet-stream").split(";")[0].strip()
    return f"data:{m};base64,{b64}"


def decode_len(b64: str) -> int:
    """True byte count behind a base64 string, without decoding it. Lets a
    caller refuse an oversized picture before it is loaded into memory."""
    s = (b64 or "").strip()
    if not s:
        return 0
    pad = s.count("=", -2)
    return (len(s) * 3) // 4 - pad


def probe_png_bytes() -> bytes:
    """The probe image as raw bytes, for tests that want to assert it is a real
    PNG rather than trusting the constant."""
    return base64.b64decode(MEDIA_PROBE_PNG_B64)
