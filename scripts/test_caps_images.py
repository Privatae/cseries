#!/usr/bin/env python3
"""Gate: the image engine passes the Host's asks through.

Four invented limits sat on the user's own image and vision models (the vision
model is gone — the Host's own model sees pictures — so its two items are no
longer pinned here):

  - generate clamped n to 4 silently and could not ask for a size or quality;
  - generate gave the provider 90 s and vision 60 s, so a slow high-quality
    call was cut while the provider was still working (and billing);
  - vision sent max_tokens=2048 and never read finish_reason, so a long
    description was cut with no marker;
  - the gallery was LIMIT 20 with no paging: older images were unreachable.

What this pins (no network, no DB — a captured aiohttp session and a fake pool):
   1. n is sent as asked (6 → 6), and the provider's refusal is the limit
   2. size and quality reach the request body; a malformed size is refused
      with a sentence, before any call
   3. generation runs under the provider-client default timeout (600 s)
   5. gallery pages over BOTH stores (gallery entries and the stored images
      no entry names — an app's render): total is the true count of both,
      newest first across them, next_offset set while more remain
   6. the image_generate tool passes size/quality/offset/limit through and its
      schema states size, quality and offset

Run: APP_ROOT=<repo> python3 scripts/test_caps_images.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path
from tools_source import tools_source  # tools.py and the modules split out of it

REPO = os.environ.get("APP_ROOT") or str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "brain"))

from mastercode import image_engine as IE  # noqa: E402

_fail = 0


def check(label, cond, detail=""):
    global _fail
    if cond:
        print(f"[caps-g2-images] ok   {label}")
    else:
        _fail += 1
        print(f"[caps-g2-images] FAIL {label}" + (f" — {detail}" if detail else ""))


class Ctx:
    def __init__(self, args, pool=None):
        self.args, self.entity_id, self.pool = args, 1, pool

    def log(self, *a):
        pass


class Pool:
    def __init__(self, total=0, rows=()):
        self.total, self.rows, self.calls = total, list(rows), []

    async def execute(self, *a, **k):
        return None

    async def fetchval(self, sql, *a):
        self.calls.append(("fetchval", sql, a))
        return self.total

    async def fetch(self, sql, *a):
        self.calls.append(("fetch", sql, a))
        return self.rows


def capture(reply):
    sent = []

    class Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return reply

        async def text(self):
            return json.dumps(reply)

    class Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, headers=None, json=None, timeout=None):
            sent.append({"url": url, "json": json, "timeout": timeout})
            return Resp()

    IE.aiohttp.ClientSession = Session
    return sent


async def route(context, role):
    return {"base": "https://api.example/v1", "model": "img-model", "key": "k",
            "std_provider": None}


def run(coro):
    return asyncio.run(coro)


def main() -> int:
    IE._resolve_image_route = route
    try:
        from mastercode.runtime import model_standard as MS
        MS.refusal_image = lambda *a, **k: None
        MS.refusal = lambda *a, **k: None
        MS.pins_moderation = lambda *a, **k: False
    except Exception as e:                                   # pragma: no cover
        print(f"[caps-g2-images] note: model_standard not patched ({e})")

    # ── 1-3. generate ───────────────────────────────────────────────────
    sent = capture({"data": [{"url": f"https://img/{i}.png"} for i in range(6)]})
    r = run(IE._generate(Ctx({"prompt": "a cat", "n": 6, "size": "1024x1536",
                              "quality": "high"}, Pool())))
    body = sent[0]["json"] if sent else {}
    check("1: n is sent as asked (6), not clamped to 4", body.get("n") == 6, str(body))
    check("1: …and all 6 images come back", r.get("count") == 6, str(r)[:200])
    check("2: size and quality reach the provider",
          body.get("size") == "1024x1536" and body.get("quality") == "high", str(body))
    t = sent[0]["timeout"] if sent else None
    check("3: generate waits the provider-client default (600 s), not 90 s",
          getattr(t, "total", None) == 600, str(t))
    sent.clear()
    r = run(IE._generate(Ctx({"prompt": "a cat", "size": "huge"}, Pool())))
    check("2: a malformed size is refused with a sentence, before any call",
          r.get("status") == "error" and "size" in r.get("result", "") and not sent, str(r))

    # ── 5. gallery ──────────────────────────────────────────────────────
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 10, 11, tzinfo=timezone.utc)

    class GalleryPool:
        """Two stores: gallery entries (entity_context) and stored images no
        entry names (generated_images) — an app's render lives only there."""
        def __init__(self, entries, unlisted, n_entries, n_unlisted):
            self.entries, self.unlisted = entries, unlisted
            self.n_entries, self.n_unlisted, self.calls = n_entries, n_unlisted, []

        async def fetchval(self, sql, *a):
            self.calls.append(("fetchval", sql, a))
            return self.n_unlisted if "FROM generated_images" in sql else self.n_entries

        async def fetch(self, sql, *a):
            self.calls.append(("fetch", sql, a))
            return self.unlisted if "FROM generated_images" in sql else self.entries

    entries = [{"file_path": f"images/img_{i}.json",
                "content": json.dumps({"prompt": f"p{i}", "url": f"u{i}"}),
                "updated_at": t0 - timedelta(hours=i)} for i in range(40)]
    renders = [{"id": "gimg_" + str(i) * 22, "prompt": "blender-engine/run_script",
                "model": "app:blender-engine", "created_at": t0 + timedelta(minutes=i + 1)}
               for i in range(3)]
    pool = GalleryPool(entries, renders, n_entries=57, n_unlisted=3)
    r = run(IE._gallery(Ctx({"offset": 0, "limit": 5}, pool)))
    check("5: total is the true count of BOTH stores (57 entries + 3 renders)",
          r.get("total") == 60, str({k: v for k, v in r.items() if k != "images"}))
    urls = [i["url"] for i in r.get("images") or []]
    check("5: newest first across both: the three renders, then the newest entries",
          urls[:3] == ["/api/images/gimg_" + "2" * 22, "/api/images/gimg_" + "1" * 22,
                       "/api/images/gimg_" + "0" * 22] and urls[3:] == ["u0", "u1"], urls)
    fetch = [c for c in pool.calls if c[0] == "fetch"]
    check("5: each store is read deep enough to cut the page (offset + limit)",
          len(fetch) == 2 and all(c[2][-1] == 5 for c in fetch), [c[2][-1:] for c in fetch])
    check("5: the engine's own bookkeeping rows are never listed as pictures",
          any(["images/last_attachment.json"] in c[2] and "images/analysis_%" in c[2]
              for c in pool.calls), [c[2] for c in pool.calls][:2])
    pool = GalleryPool(entries, renders, n_entries=57, n_unlisted=3)
    r = run(IE._gallery(Ctx({"offset": 20, "limit": 20}, pool)))
    check("5: a later page is cut from the merge, and next_offset is set while more remain",
          r.get("returned") == 20 and r.get("next_offset") == 40, str({k: v for k, v in r.items() if k != "images"}))

    # ── 6. the tool and its schema ──────────────────────────────────────
    src = tools_source(REPO)
    i = src.find("async def _exec_image_generate")
    seg = src[i:i + 6000]
    check("6: the tool passes size/quality/offset/limit to the engine",
          all(f'"{k}": a.get("{k}")' in seg for k in ("size", "quality", "offset", "limit")))
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS
    sch = PUBLIC_TOOL_SCHEMAS["image_generate"]["function"]
    props = sch["parameters"]["properties"]
    check("6: the schema states size, quality and offset",
          all(k in props for k in ("size", "quality", "offset")), sorted(props))
    check("6: the description no longer promises n 1-4",
          "1-4" not in sch["description"] and "1-4" not in props["n"]["description"])

    print(f"\n[caps-g2-images] {'PASS' if not _fail else 'FAIL'} ({_fail} failing)")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
