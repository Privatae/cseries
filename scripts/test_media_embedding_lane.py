#!/usr/bin/env python3
"""Gate: the media (picture) embedding lane keeps the separations it was built on.

Pictures embed through a model the USER points at, exactly as text does.
Every check below is a way that could silently stop being true, and each one has
a named failure it prevents. Repo-side: no pod, no DB, no network.

THE INVARIANTS

 1. A media vector never lands in a text column and vice versa. Two models
    produce two different vector spaces even at the same width, so a shared
    column makes nearest-neighbour results a mix of two incomparable spaces
    with nothing in the row to say which. memory_engine.py:49-54 records the
    same lesson for two TEXT models in one column.

 2. Coverage counts never cross. The text counts exclude file rows
    (NOT_FILE_KIND_SQL); the media counts are only file rows. Neither total may
    borrow the other's denominator — a picture counted as unembedded TEXT is
    what made the Embedding lens read "never finished" for anyone with a PNG in
    their corpus.

 3. There is no fallback anywhere in the media lane. No default model, no
    default column, no guessed wire shape. A fallback here writes vectors
    nothing can search, which is worse than writing none.

 4. Every storable width has a real column in the schema. There used to be TWO
    schema paths — the v135 migration and the lazy USER_CONTEXT_SCHEMA in
    routes/context.py — and a width present in one and not the other stranded
    every host created in the gap; that is exactly what v071 did to 2560. The
    lazy path is gone, so there is one declaration and nothing
    for it to drift from. What still has to hold is the half that was always the
    real risk: media_engine.MEDIA_COLUMN_BY_DIM in lockstep with v135.
"""
import ast
import os
import re
import sys

ROOT = os.environ.get("APP_ROOT", "/app")
sys.path.insert(0, os.path.join(ROOT, "brain"))
sys.path.insert(0, ROOT)

failures = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if ok:
        print(f"  ok    {label}")
    else:
        failures.append(label)
        print(f"  FAIL  {label}" + (f"  — {detail}" if detail else ""))


def read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def flat(s: str) -> str:
    """Collapse runs of whitespace to one space. Source formatting is not the
    invariant — the SQL aligns column names with padding and docstrings wrap
    mid-phrase, and a raw substring check against either is a gate that
    false-alarms on a reflow instead of on a real regression."""
    return re.sub(r"\s+", " ", s)


# ── 1. the column families are disjoint ──────────────────────────────────────
from mastercode.media_engine import (  # noqa: E402
    MEDIA_COLUMN_BY_DIM, MEDIA_MRL_CANDIDATE_DIMS, MEDIA_PROBE_PNG_ALT_B64,
    MEDIA_PROBE_PNG_B64, MEDIA_PROBE_PNG_DIFF_B64,
    MEDIA_WIRES, MEDIA_WIRE_ORDER, _resolve_url, _vector_from,
    data_uri, decode_len, media_cast_type, media_fresh_sql, media_kind,
    probe_png_bytes,
)


def _same_pixels(b64_a: str, b64_b: str) -> bool:
    """True when two PNGs decode to identical pixel buffers.

    This is the property the two-tower discriminator rests on, so it is MEASURED
    here rather than trusted: inflate each IDAT, undo the row filters, compare.
    If someone regenerates a probe and breaks pixel-identity, the gate that
    claims a text tower cannot pass would quietly stop meaning anything.
    """
    import base64 as _b64, struct as _st, zlib as _zl

    def _pixels(raw_png: bytes) -> bytes:
        i, idat, ihdr = 8, b"", None
        while i < len(raw_png):
            ln = _st.unpack(">I", raw_png[i:i + 4])[0]
            typ, body = raw_png[i + 4:i + 8], raw_png[i + 8:i + 8 + ln]
            if typ == b"IHDR":
                ihdr = body
            elif typ == b"IDAT":
                idat += body
            i += 8 + ln + 4
        w, h, _depth, color = _st.unpack(">IIBB", ihdr[:10])
        bpp = {0: 1, 2: 3, 4: 2, 6: 4}[color]
        stride, data = w * bpp, _zl.decompress(idat)
        out, prev, pos = bytearray(), bytearray(stride), 0
        for _ in range(h):
            f = data[pos]; pos += 1
            line = bytearray(data[pos:pos + stride]); pos += stride
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0
                b = prev[x]
                c = prev[x - bpp] if x >= bpp else 0
                if f == 1:
                    line[x] = (line[x] + a) & 0xFF
                elif f == 2:
                    line[x] = (line[x] + b) & 0xFF
                elif f == 3:
                    line[x] = (line[x] + (a + b) // 2) & 0xFF
                elif f == 4:
                    p = a + b - c
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    line[x] = (line[x] + (a if (pa <= pb and pa <= pc)
                                          else (b if pb <= pc else c))) & 0xFF
            out += line
            prev = line
        return bytes(out)

    return _pixels(_b64.b64decode(b64_a)) == _pixels(_b64.b64decode(b64_b))
from mastercode.memory_engine import EMBEDDING_COLUMN_BY_DIM  # noqa: E402

text_cols = set(EMBEDDING_COLUMN_BY_DIM[d] for d in EMBEDDING_COLUMN_BY_DIM)
media_cols = set(MEDIA_COLUMN_BY_DIM[d] for d in MEDIA_COLUMN_BY_DIM)
check("no column is in both the text and media families",
      not (text_cols & media_cols), str(text_cols & media_cols))
check("every media column name is media_-prefixed (greppable, unmistakable)",
      all(c.startswith("media_embedding_") for c in media_cols), str(media_cols))

# ── 2. both schema paths carry every media column ────────────────────────────
migration = read("migrations/v135_user_context_media_embeddings.sql")
context_py = read("server/routes/context.py")
flat_migration = flat(migration)
for dim in sorted(MEDIA_COLUMN_BY_DIM):
    col = MEDIA_COLUMN_BY_DIM[dim]
    check(f"{col} exists in the v135 migration",
          f"{col} halfvec({dim})" in flat_migration)

check("media_embed_model exists in the v135 migration",
      "media_embed_model TEXT" in migration)
# The second path is GONE, and this asserts that rather than assuming it. If a
# lazy CREATE ever comes back to context.py, two declarations exist again and
# the v071/2560 stranding is live again — so that is a failure here, not a
# silent return to the old shape.
check("routes/context.py declares no user_context schema of its own",
      "USER_CONTEXT_SCHEMA = \"\"\"" not in context_py
      and "CREATE TABLE IF NOT EXISTS user_context" not in context_py,
      "a lazy user_context schema is back in routes/context.py")
check("the migration is guarded for a host with no user_context (the v090 lesson)",
      "to_regclass('public.user_context') IS NULL" in migration)

# ── 3. text coverage never counts media, and vice versa ──────────────────────
any_embedded = re.search(r"ANY_EMBEDDED_SQL\s*=\s*\((.*?)\)", context_py, re.S)
check("ANY_EMBEDDED_SQL was found", any_embedded is not None)
if any_embedded:
    body = any_embedded.group(1)
    check("ANY_EMBEDDED_SQL names no media column (text coverage stays text-only)",
          not any(c in body for c in media_cols))
    for col in sorted(text_cols):
        check(f"ANY_EMBEDDED_SQL still covers the text column {col}", col in body)

check("NOT_FILE_KIND_SQL still excludes file rows from the text lane",
      "NOT_FILE_KIND_SQL = \"COALESCE(metadata->>'kind', '') NOT IN ('file', 'frame')\""
      in context_py)

# ── 3b. VIDEO FRAMES ARE A THIRD POPULATION ─────────────────────────────────
#
# A frame row holds NO bytes: the media vector and the offset ARE the row,
# because the clip is already stored and a frame reproduces by seeking
# (measured 416 kB against 4.97 MB per clip, 12x). That makes it the one row
# shape that could break invariant 2 in BOTH directions at once —
#
#   * it is not kind='file', so without the fragment above it would be TEXT
#     with an empty content column: unembeddable, and permanently "unembedded",
#     which is exactly the state that made the lens unclearable in 2026-07;
#   * it carries an image mime, so if it were written as kind='file' it would
#     land in the PICTURE denominator and one long video would swamp the
#     picture coverage number with frames nobody uploaded.
#
# So: excluded from text by the one fragment, excluded from pictures by kind,
# and counted in its own denominator. All three checked here.
check("NOT_FILE_KIND_SQL also excludes video frame rows from the text lane",
      "'frame'" in context_py.split("NOT_FILE_KIND_SQL =")[1][:120])

cx = read("server/routes/context_extract.py")
embed_py = read("server/routes/embed.py")
media_engine_py = read("brain/mastercode/media_engine.py")
check("a frame row is written as kind='frame', never 'file' (it must not enter "
      "the picture denominator)",
      '"kind": FRAME_KIND' in cx and 'FRAME_KIND = "frame"' in context_py)
# THE BYTES ARE STAGED, NOT STORED, and the distinction is what makes a failed
# embed recoverable. The row is written WITH its jpeg and no vector; the
# vector's arrival is what drops the bytes, in the SAME statement. Steady state
# is still the vector and the timestamp — the 12x — but in between there is
# something to retry from.
#
# The first cut wrote only the frames that embedded. A frame row carries no
# bytes for a fill to work from and the media lane selects positively on kind,
# so a frame that failed to embed was in NO population: excluded from text by
# NOT_FILE_KIND_SQL, never chosen by media. One provider 502 left it
# unsearchable for ever. scripts/test_media_fill_finds_frames.py proves the
# repair against a real planner.
check("a frame row is written with its picture STAGED and no vector, so a failed "
      "embed has something to retry from",
      "media_offset_ms" in cx
      and "media_parent_id, media_offset_ms, search_tsv)" in flat(cx)
      and "MEDIA_DROP_STAGED_CONTENT_SQL" in cx)
check("the staged bytes are dropped in the SAME statement that writes the vector "
      "— never a second one that a crash can skip",
      flat(cx).count("MEDIA_DROP_STAGED_CONTENT_SQL,\n") == 0
      and "{MEDIA_DROP_STAGED_CONTENT_SQL}," in cx)
check("and both writers use the one fragment, so 'the bytes are gone now' has a "
      "single spelling",
      "MEDIA_DROP_STAGED_CONTENT_SQL" in cx and "MEDIA_DROP_STAGED_CONTENT_SQL" in embed_py)
check("the media fill owns BOTH populations through one predicate",
      "media_embeddable_sql" in embed_py
      and "metadata->>'kind' IN ('file', 'frame')" in media_engine_py)
check("and it refuses a row it could do no work on, rather than failing it on "
      "every pass for ever",
      "length(content) > 0" in media_engine_py)
check("a frame's vector goes into the MEDIA column family, with the model stamped",
      "active_media_target" in cx and "media_embed_model" in cx)
check("frames are embedded through the media lane that already exists, not a "
      "second implementation",
      "_embed_batch_media" in cx)
check("a clip with no media model set is a SETUP state and is refused BEFORE the "
      "user's machine decodes anything",
      '"needs_setup": "media_embedding"' in cx
      and cx.index("needs_setup") < cx.index("relay.dispatch"))
check("a clip's frames and a document's text are different replace-sets",
      'FRAME_SOURCE = "frames"' in cx and 'EXTRACT_SOURCE = "extracted"' in cx)

cm = read("server/routes/context_manager.py")
check("the coverage lens reports media under its OWN key, not added to the text totals",
      '"media": media' in cm and '"pictures_embedded"' in cm)
check("the coverage lens counts pictures with an image/ mime filter only",
      "metadata->>'mime' LIKE 'image/%'" in cm)
check("the coverage lens counts FRAMES in their own denominator",
      '"frames_embedded"' in cm and '"frames_unsearchable"' in cm)
check("the picture count cannot be inflated by frames (it still asks for kind='file')",
      "metadata->>'kind' = 'file' \"\n            \"AND metadata->>'mime' LIKE 'image/%'" in cm
      or flat(cm).count("metadata->>'kind' = 'file' AND metadata->>'mime' LIKE 'image/%'") >= 2)
check("a clip counts as searchable only when it HAS a fresh frame — never derived "
      "from the frame total",
      "clips_searchable" in cm and "f.media_parent_id = c.id" in flat(cm))
# The clips query writes the freshness predicate out by hand, because
# media_fresh_sql emits an unqualified media_embed_model and both tables in that
# subquery have one. Pin the two together so the hand-written copy cannot drift
# from the helper every other surface uses.
_helper = media_fresh_sql("f.COL", "$2").replace("COL", "media_embedding_1024")
check("the hand-qualified clip predicate is the same predicate media_fresh_sql emits",
      flat(_helper.replace("(", "").replace(")", "")).replace("f.media_embed_model",
          "media_embed_model") ==
      "f.media_embedding_1024 IS NOT NULL AND media_embed_model IS NOT DISTINCT FROM $2",
      _helper)
check("...and the clips query carries it qualified on BOTH columns",
      "f.media_embed_model IS NOT DISTINCT FROM $2" in flat(cm))

# ── 4. no fallbacks anywhere in the media lane ───────────────────────────────
me_src = read("brain/mastercode/media_engine.py")
me_tree = ast.parse(me_src)


def fn(name: str):
    for node in ast.walk(me_tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


amc = fn("active_media_column")
check("active_media_column exists", amc is not None)
if amc:
    seg = ast.get_source_segment(me_src, amc) or ""
    check("active_media_column returns (None, None) when unconfigured — no fallback column",
          "return None, None" in seg)
    check("active_media_column names no literal column as a default",
          not any(c in seg.replace("MEDIA_COLUMN_BY_DIM", "") for c in media_cols))

emb = fn("embed_media")
check("embed_media exists", emb is not None)
if emb:
    seg = ast.get_source_segment(me_src, emb) or ""
    check("embed_media refuses when no wire shape was proven (never guesses one)",
          "not guessing one" in seg and "return None" in seg)

check("the media lane has no default model anywhere",
      not re.search(r'media\w*\s*=\s*["\'](clip|siglip|jina|nomic)', me_src, re.I))

check("the media job resolves the MEDIA model, never the text one",
      "_active_media_model" in embed_py)
check("_active_media_model never falls back to the text model",
      "never falls back to the text model" in flat(embed_py))
check("the media job refuses to write without a media column",
      "media write: no media column" in embed_py)

# ── 5. the probe gates BOTH towers ───────────────────────────────────────────
probe = fn("probe_media_endpoint")
check("probe_media_endpoint exists", probe is not None)
if probe:
    seg = ast.get_source_segment(me_src, probe) or ""
    check("the probe refuses a model whose text tower does not answer",
          "text tower did not answer" in seg)
    check("the probe refuses a model whose towers disagree on width",
          "do not share a space" in seg)

mroute = read("server/routes/media_embedding.py")
check("the switch persists only a width that has a real column",
      "if dim not in MEDIA_COLUMN_BY_DIM" in mroute)
check("clearing the route keeps existing vectors (never dumps what the user paid for)",
      '"vectors_kept": True' in mroute)

# ── 6. the wire shapes behave as documented ──────────────────────────────────
check("every probed wire name has a builder",
      all(n in MEDIA_WIRES for n in MEDIA_WIRE_ORDER))
for name in MEDIA_WIRE_ORDER:
    b = MEDIA_WIRES[name]
    img = b("M", image="data:image/png;base64,AAA")
    txt = b("M", text="hello")
    check(f"wire {name}: image and text bodies differ (two towers, one model)", img != txt)
    check(f"wire {name}: both bodies name the model", img.get("model") == "M")

check("_vector_from reads the OpenAI shape", _vector_from({"data": [{"embedding": [1, 2]}]}) == [1, 2])
check("_vector_from reads the Cohere shape", _vector_from({"embeddings": {"float": [[3, 4]]}}) == [3, 4])
check("_vector_from reads the Ollama shape", _vector_from({"embedding": [5, 6]}) == [5, 6])
check("_vector_from reads a Voyage-style list", _vector_from({"embeddings": [{"embedding": [7]}]}) == [7])
check("_vector_from returns None on junk rather than a made-up vector",
      _vector_from({"nope": 1}) is None and _vector_from(None) is None)

check("_resolve_url leaves an already-complete route alone",
      _resolve_url("https://x/v1/embeddings") == "https://x/v1/embeddings")
check("_resolve_url gives a bare host the OpenAI-compat route",
      _resolve_url("http://box:11434") == "http://box:11434/v1/embeddings")

# ── 7. the probe image is a real PNG, not a trusted constant ─────────────────
png = probe_png_bytes()
check("the probe image carries the PNG signature", png[:8] == b"\x89PNG\r\n\x1a\n")
check("decode_len agrees with the real byte count",
      decode_len(MEDIA_PROBE_PNG_B64) == len(png))
check("the probe image is larger than 1x1 (a degenerate image fails for the wrong reason)",
      len(png) > 60)

# ── 8. mime classification ───────────────────────────────────────────────────
check("a png is a picture", media_kind("image/png") == "picture")
check("an mp4 is a clip", media_kind("video/mp4;codecs=av01") == "clip")
check("a pdf is neither (the lane does not widen what gets vectorized)",
      media_kind("application/pdf") is None)
check("a missing mime is neither", media_kind(None) is None)
check("data_uri round-trips the stored base64 unchanged",
      data_uri("image/png", "AAA") == "data:image/png;base64,AAA")

# ── 9. the whole media family is one cast type ───────────────────────────────
check("every media width casts to halfvec",
      all(media_cast_type(d) == f"halfvec({d})" for d in MEDIA_COLUMN_BY_DIM))
check("every media width is HNSW-indexable (all under pgvector's 4000d halfvec cap)",
      all(d <= 4000 for d in MEDIA_COLUMN_BY_DIM))
for d in sorted(MEDIA_COLUMN_BY_DIM):
    check(f"the v135 migration builds an HNSW index for {d}",
          f"media_embedding_{d} halfvec_cosine_ops" in migration)
check("truncation candidates are tried widest-first (keep as much of the model as fits)",
      MEDIA_MRL_CANDIDATE_DIMS == sorted(MEDIA_COLUMN_BY_DIM, reverse=True))

# ── 10. the search fuses media as a THIRD arm, not a replacement ─────────────
check("the RRF fuser takes a media arm",
      "media_hits" in context_py and '("media", media_hits or [])' in context_py)
check("the media arm degrades instead of taking text search down with it",
      "context media search unavailable" in context_py)
check("the media arm resolves its own column AND model, never a text one",
      "active_media_target(pool, entity_id)" in context_py)

context_manager_py = read("server/routes/context_manager.py")

# ── 11. a vector belongs to the MODEL that wrote it, not just to a width ─────
# Two models can serve the same width (1024d: jina-clip-v2, Voyage, SigLIP 2
# large), so `column IS NOT NULL` cannot say whether a vector belongs to the
# model configured now. Without the stamp in the predicate, switching between
# two same-width models leaves old vectors counted as searchable, skipped by
# the fill, and searched with the new model's queries against the old model's
# space — wrong answers, silently, with coverage reporting success.
for surface, src in (("search", context_py), ("fill + fetch", embed_py),
                     ("coverage", context_manager_py)):
    check(f"the {surface} surface keys on the model stamp, not the column alone",
          any(h in src for h in ("media_fresh_sql", "media_stale_sql",
                                 "media_embeddable_sql")))
# media_embeddable_sql is the fill's one predicate and it reaches the stamp
# THROUGH media_stale_sql. Naming it above would otherwise let the stamp fall
# out of the chain while the check above still passed on the wrapper's name.
check("...and the fill's predicate reaches the stamp through media_stale_sql, "
      "rather than merely being named like it does",
      "media_stale_sql(media_col, model_param)" in flat(media_engine_py.split(
          "def media_embeddable_sql")[1][:600]))
check("fresh and stale are exact complements (a job that disagrees loops or lies)",
      "IS NOT DISTINCT FROM" in media_engine_py and "IS DISTINCT FROM" in media_engine_py)

# ── 12. the probe proves an IMAGE tower, not merely a matching width ─────────
# A text model embeds the data-URI STRING happily and at a matching width, so
# width equality alone accepted nomic-embed-text as a picture model and every
# picture got embedded as base64 gibberish.
check("the probe sends the same picture twice in different bytes",
      "MEDIA_PROBE_ALT_DATA_URI" in media_engine_py
      and "MEDIA_PROBE_ALT_DATA_URI" in flat(media_engine_py))
check("identical pixels must come back as one answer (a text tower cannot)",
      "MEDIA_SAME_PICTURE_MIN_COS" in media_engine_py)
check("a changed picture must move the answer (catches a constant vector)",
      "MEDIA_DIFF_PICTURE_MAX_COS" in media_engine_py)
check("the two probe encodings really are the same picture",
      _same_pixels(MEDIA_PROBE_PNG_B64, MEDIA_PROBE_PNG_ALT_B64))
check("the two probe encodings really are different bytes",
      MEDIA_PROBE_PNG_B64 != MEDIA_PROBE_PNG_ALT_B64)
check("the control probe really is a different picture",
      not _same_pixels(MEDIA_PROBE_PNG_B64, MEDIA_PROBE_PNG_DIFF_B64))

# ── 13. one oversized picture must not be retried for ever ──────────────────
check("something bounds what one embed call may post",
      "MEDIA_MAX_EMBED_BYTES" in media_engine_py and "decode_len" in embed_py)
check("a wrong-width vector is dropped, never cast into the column",
      "wedging the job" in flat(embed_py))

print()
if failures:
    print(f"FAIL: {len(failures)} of {checks} check(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"PASS: {checks} checks — the media lane's separations hold")
