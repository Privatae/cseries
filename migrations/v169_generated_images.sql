-- v169 — generated_images: the bytes of an image this Host generated.
--
-- WHY IT EXISTS. image_engine._generate kept only provider responses that carry
-- a `url` (the OpenAI shape). OpenRouter — the provider the Keys lane routes the
-- image role through (image_engine._resolve_image_route) — returns
-- the image INLINE as `b64_json` with no url. Measured against
-- google/gemini-2.5-flash-image: 200, data[0] = {b64_json, media_type:
-- image/png}, $0.0387 charged, and the engine returned {"count": 0}. Every such
-- generation was paid for and thrown away.
--
-- An inline image has nowhere to live but the Host that made it, so it lands
-- here and is served back at /api/images/{id}. It stays on the user's own Host:
-- nothing about it touches the platform.
--
-- `id` is the only thing a reader holds, and the serving route does not ask for
-- an API token (an <img> tag or a clicked link cannot send one — the same reason
-- the wallpaper route takes none, server/routes/appearance.py serve_custom_image),
-- so it is 128 random bits, never a counter or a timestamp.
--
-- Keep in lockstep with the same block in init-db.sql.

CREATE TABLE IF NOT EXISTS generated_images (
    id            TEXT PRIMARY KEY,
    entity_id     INTEGER NOT NULL
                  REFERENCES entity_registry(id) ON DELETE CASCADE,
    content_type  TEXT NOT NULL,
    bytes         BYTEA NOT NULL,
    prompt        TEXT,
    model         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_generated_images_entity_created
    ON generated_images (entity_id, created_at DESC);
