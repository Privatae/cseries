#!/usr/bin/env python3
"""Route guards for the image/voice lanes added 2026-08-31 (review N5).

These are the branches that decide what the Orchestrator and the host are
ALLOWED to say and store. Each exists because getting it wrong is SILENT:

  - capable-models must distinguish "couldn't look" from "none qualify". An
    empty list where an error belongs tells the user their provider has no
    image models when we simply had no key to ask with.
  - a provider that publishes no modality data at all is a THIRD state. xAI is
    the live case: it serves grok-imagine-image* but declares nothing, and
    OpenRouter carries no image-output x-ai row to inherit from, so filtering
    silently would hide exactly the models the user holds a key for.
  - image_gen must be SETTABLE. It sat in ROLES' blind spot: resolvable by
    resolve_role_route, asked for by image_engine, and rejected by the PUT —
    so the Orchestrator's image picker could never have worked. ('vision' sat
    there with it; since 2026-10-12 it is no role at all — the Host model reads
    images itself — while 'vision' stays a catalog CAPABILITY, checked below.)
  - voice must refuse a provider that cannot mint OR has no key, at WRITE time,
    not at the moment the user first tries to speak.

Usage: APP_ROOT=<repo> python3 scripts/test_inference_role_guards.py
"""
from __future__ import annotations

import asyncio
import json as _json
import os
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, os.path.join(_ROOT, "brain")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.environ.setdefault("ENTITY_ID", "1")

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[role-guards] {'ok  ' if ok else 'FAIL'} {name}"
          + (f" — {detail}" if not ok and detail else ""))
    if not ok:
        FAILS.append(name)


async def main() -> int:
    from fastapi import HTTPException
    from server.routes import inference as I
    from server.routes import realtime as R
    import server.brain_service as bs

    # ── ROLES: the blind spot that made the image picker's Set button a 400 ──
    check("image_gen is a settable role", "image_gen" in I.ROLES, str(I.ROLES))
    check("vision is not a role (the Host model reads images)", "vision" not in I.ROLES,
          str(I.ROLES))
    text_roles = [r for r in I.ROLES
                  if r not in ("embedding", "image", "image_gen")]
    check("image/embedding roles are excluded from the text-role set",
          not ({"image_gen", "embedding", "image"} & set(text_roles)),
          str(text_roles))

    # ── capable-models: three distinct states ───────────────────────────────
    DECLARED = [
        {"id": "v/sees", "input_modalities": ["text", "image"], "output_modalities": ["text"]},
        {"id": "v/draws", "input_modalities": ["text"], "output_modalities": ["image"]},
        {"id": "v/plain", "input_modalities": ["text"], "output_modalities": ["text"]},
    ]
    SILENT = [{"id": "s/one"}, {"id": "s/two"}]        # declares nothing (the xAI shape)
    INHERITED = [{"id": "i/one", "input_modalities": ["text", "image"],
                  "modality_source": "vendor list via OpenRouter"}]

    async def fake(p):
        return {"openrouter": (DECLARED, None),
                "xai": (SILENT, None),
                "openai": (INHERITED, None)}.get(p, ([], f"no {p} key in Keys"))

    real_dm, I._direct_models = I._direct_models, fake
    try:
        pane = (await I.get_capable_models("vision", provider="openrouter"))["providers"]["openrouter"]
        check("the vision CAPABILITY still filters to image-INPUT models",
              [m["id"] for m in pane["models"]] == ["v/sees"], str(pane))
        check("declared capability is flagged published",
              pane["capability_published"] is True)

        pane = (await I.get_capable_models("image_gen", provider="openrouter"))["providers"]["openrouter"]
        check("image_gen filters to image-OUTPUT models",
              [m["id"] for m in pane["models"]] == ["v/draws"], str(pane))

        pane = (await I.get_capable_models("image_gen", provider="xai"))["providers"]["xai"]
        check("a provider publishing NO modalities returns its FULL list, flagged",
              pane["capability_published"] is False
              and [m["id"] for m in pane["models"]] == ["s/one", "s/two"], str(pane))
        check("unpublished rows are never marked verified",
              all(m["verified"] is False for m in pane["models"]))

        pane = (await I.get_capable_models("vision", provider="openai"))["providers"]["openai"]
        check("suffix-inherited capability is marked unverified",
              bool(pane["models"]) and pane["models"][0]["verified"] is False, str(pane))

        pane = (await I.get_capable_models("vision", provider="anthropic"))["providers"]["anthropic"]
        check("a keyless provider returns an ERROR, never an empty list",
              "error" in pane and "models" not in pane, str(pane))

        try:
            await I.get_capable_models("telepathy", provider="openrouter")
            check("unknown capability is refused", False, "accepted")
        except HTTPException as e:
            check("unknown capability is refused", e.status_code == 400)
        try:
            await I.get_capable_models("vision", provider="nosuchprovider")
            check("unknown provider is refused", False, "accepted")
        except HTTPException as e:
            check("unknown provider is refused", e.status_code == 400)
    finally:
        I._direct_models = real_dm

    # ── voice: refuse unmintable AND keyless, at write time ─────────────────
    class _Pool:
        async def fetchrow(self, *a):
            return None

        async def execute(self, *a):
            return None

    async def opts_none(pool, eid):
        return [{"provider": "xai", "has_key": False}]

    async def opts_keyed(pool, eid):
        return [{"provider": "xai", "has_key": True}]

    real_pool, bs.get_pool = bs.get_pool, lambda: _Pool()
    real_opts = R.available_voice_providers
    try:
        R.available_voice_providers = opts_none
        try:
            await I.set_voice_provider(I.SetVoiceProvider(provider="xai"))
            check("a keyless voice provider is refused at write time", False, "stored it")
        except HTTPException as e:
            check("a keyless voice provider is refused at write time",
                  e.status_code == 400 and "key" in str(e.detail).lower(), str(e.detail))

        R.available_voice_providers = opts_keyed
        try:
            await I.set_voice_provider(I.SetVoiceProvider(provider="not-a-voice-provider"))
            check("an unmintable voice provider is refused", False, "stored it")
        except HTTPException as e:
            check("an unmintable voice provider is refused", e.status_code == 400, str(e.detail))
    finally:
        R.available_voice_providers = real_opts
        bs.get_pool = real_pool

    # ── stored_voice_provider: every non-choice reads as "not chosen" ───────
    class Row(dict):
        def __getitem__(self, k):
            return dict.get(self, k)

    class P:
        def __init__(self, bp):
            self.bp = bp

        async def fetchrow(self, *a):
            return Row(behavioral_params=self.bp)

    check("voice unset reads None", await R.stored_voice_provider(P(None), 1) is None)
    check("voice normalizes case and space",
          await R.stored_voice_provider(P({"voice_provider": " XAI "}), 1) == "xai")
    check("voice parses a json-string behavioral_params",
          await R.stored_voice_provider(P(_json.dumps({"voice_provider": "xai"})), 1) == "xai")
    check("voice blank reads None",
          await R.stored_voice_provider(P({"voice_provider": "  "}), 1) is None)
    check("voice malformed reads None", await R.stored_voice_provider(P(["nope"]), 1) is None)
    check("voice with no pool reads None", await R.stored_voice_provider(None, 1) is None)

    if FAILS:
        print(f"[role-guards] {len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("[role-guards] PASS — image roles settable; capable-models keeps "
          "couldn't-look / none / unpublished distinct; voice refuses unmintable "
          "and keyless at write time")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
