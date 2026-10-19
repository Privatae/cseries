#!/usr/bin/env python3
"""A picture is fitted to the provider's limits with the real Pillow (in the image).

attachment_parts brings every picture to what the provider takes before it is
sent (Anthropic "Vision" → "Image limits and costs", read 2026-10-12): at most a
2576 px long edge (the most current models see), 2000 px a side when a request
carries more than 20 pictures, and 10 MB of base64 each. The policy is driven
container-free with a stand-in (scripts/test_host_sees_pictures.py §8); this
drives the Pillow half itself, which is installed in the image only
(requirements.txt), so smoke runs it there:

  docker exec <container> python3 /app/scripts/test_picture_fit_image.py

Locally: <python with Pillow 12.3.0> scripts/test_picture_fit_image.py
"""
import asyncio
import base64
import io
import os
import sys
from pathlib import Path

REPO = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(REPO / "brain"))
sys.path.insert(0, str(REPO))

from PIL import Image  # noqa: E402

from mastercode import attachment_parts as ap  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print(("  ok    " if ok else "  FAIL  ") + name + ("" if ok else f" :: {str(detail)[:300]}"))
    if not ok:
        FAILS.append(name)


def encoded(im, fmt, **kw):
    buf = io.BytesIO()
    im.save(buf, format=fmt, **kw)
    return buf.getvalue()


def part(data, media):
    return {"type": "image", "source": {"type": "base64", "media_type": media,
                                         "data": base64.b64encode(data).decode()}}


def sent(p, wire="anthropic"):
    out = asyncio.run(ap.for_wire([{"role": "user", "content": [p, {"type": "text", "text": "q"}]}],
                                  wire))
    return out[0]["content"][0]


def opened(block):
    return Image.open(io.BytesIO(base64.b64decode(block["source"]["data"])))


# A 4K-class screenshot: a gradient, so it compresses like a real one.
grad = Image.linear_gradient("L").resize((5000, 3000)).convert("RGB")
blk = sent(part(encoded(grad, "PNG"), "image/png"))
im = opened(blk)
check("a 5000x3000 picture is brought to a 2576 px long edge, its shape kept",
      max(im.size) == ap.NATIVE_LONG_EDGE and abs(im.size[0] / im.size[1] - 5 / 3) < 0.01, im.size)
check("...and is within 10 MB of base64", len(blk["source"]["data"]) <= ap.MAX_PICTURE_B64)
check("...and its type is named from what was written",
      blk["source"]["media_type"] == {"PNG": "image/png", "JPEG": "image/jpeg",
                                      "WEBP": "image/webp"}.get(im.format), (blk["source"]["media_type"], im.format))

alpha = Image.new("RGBA", (4000, 4000), (255, 0, 0, 0))
alpha.paste((0, 0, 255, 255), (1000, 1000, 3000, 3000))
blk = sent(part(encoded(alpha, "PNG"), "image/png"))
im = opened(blk)
check("a transparent picture keeps its transparency", im.mode in ("RGBA", "LA", "PA")
      or "transparency" in im.info, im.mode)

jpeg_as_png = encoded(Image.new("RGB", (320, 240), (200, 10, 10)), "JPEG", quality=85)
blk = sent(part(jpeg_as_png, "image/png"))
check("a JPEG labelled image/png goes as image/jpeg, its bytes unchanged",
      blk["source"]["media_type"] == "image/jpeg"
      and base64.b64decode(blk["source"]["data"]) == jpeg_as_png, blk["source"]["media_type"])

small = encoded(Image.new("RGB", (640, 480), (10, 200, 30)), "JPEG", quality=85)
blk = sent(part(small, "image/jpeg"))
check("a picture already within the limits goes byte for byte as it came",
      base64.b64decode(blk["source"]["data"]) == small)

frames = [Image.new("RGB", (3000, 3000), c) for c in ((255, 0, 0), (0, 255, 0))]
buf = io.BytesIO()
frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=100)
blk = sent(part(buf.getvalue(), "image/gif"))
im = opened(blk)
check("an animation that must shrink goes as its first frame, as the provider uses",
      getattr(im, "n_frames", 1) == 1 and max(im.size) <= ap.NATIVE_LONG_EDGE
      and im.convert("RGB").getpixel((10, 10))[0] > 200, (getattr(im, "n_frames", 1), im.size))

many = [part(encoded(Image.new("RGB", (2400, 1200), (i, i, i)), "PNG"), "image/png") for i in range(21)]
ap._FIT_CACHE.clear()
out = asyncio.run(ap.for_wire([{"role": "user", "content": [*many, {"type": "text", "text": "q"}]}],
                              "anthropic"))
sizes = [opened(p).size for p in out[0]["content"] if p.get("type") == "image"]
check("21 pictures in one request: every one within 2000 px a side",
      len(sizes) == 21 and all(max(s) <= ap.MANY_LONG_EDGE for s in sizes), sizes[:2])

blk = sent(part(encoded(grad, "PNG"), "image/png"), wire="openai")
url = blk["image_url"]["url"]
im = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
check("the OpenAI wire gets the fitted picture as its data URI", max(im.size) == ap.NATIVE_LONG_EDGE)

_real_max = Image.MAX_IMAGE_PIXELS
Image.MAX_IMAGE_PIXELS = 1_000_000
ap._FIT_CACHE.clear()
try:
    blk = sent(part(encoded(Image.new("RGB", (2000, 1000)), "PNG"), "image/png"))
finally:
    Image.MAX_IMAGE_PIXELS = _real_max
check("past Pillow's decompression-bomb line a picture is not decoded on the Host; a note says so",
      blk.get("type") == "text" and "too large to prepare" in blk.get("text", ""), blk)

# Past TWICE that line Pillow will not even open it (DecompressionBombError):
# the same verdict, not "sent as it came" for the provider to refuse.
Image.MAX_IMAGE_PIXELS = 1_000_000
ap._FIT_CACHE.clear()
try:
    blk = sent(part(encoded(Image.new("RGB", (3000, 1000)), "PNG"), "image/png"))
finally:
    Image.MAX_IMAGE_PIXELS = _real_max
check("past twice that line (Pillow refuses to open it) the same note, not the raw picture",
      blk.get("type") == "text" and "too large to prepare" in blk.get("text", ""), blk)

# A 16-bit grey PNG (scans, scientific and medical pictures): Pillow's convert
# clips at 255 rather than scaling, so a mid-grey picture went as pure white.
grey16 = Image.frombytes("I;16", (4000, 3000), (32768).to_bytes(2, "little") * (4000 * 3000))
ap._FIT_CACHE.clear()
im = opened(sent(part(encoded(grey16, "PNG"), "image/png")))
px = im.convert("L").getpixel((100, 100))
check("a 16-bit grey picture keeps its tones when it is shrunk (mid-grey stays mid-grey)",
      max(im.size) == ap.NATIVE_LONG_EDGE and 120 <= px <= 136, (im.mode, im.size, px))

# Memory: a picture is shrunk where it was decoded, not copied at full size
# first. Measured 2026-10-12 before this: the peak was 3.2-3.7x the decoded
# size (a 1.5 MB, 9400x9400 PNG took 1.16 GB). Measured in a fresh process by
# its VmHWM, which starts again at exec (ru_maxrss does not: Linux keeps it
# across execve, so a child would inherit this process's own high-water mark).
import subprocess  # noqa: E402
side = 5000
# RGB: Pillow's own alpha-correct resize premultiplies an RGBA picture at
# full size, so RGBA's floor is about 2.8x however this is written.
big = encoded(Image.new("RGB", (side, side), (10, 20, 30)), "PNG", compress_level=1)
child = (
    "import sys, warnings\n"
    f"sys.path[:0] = [{str(REPO / 'brain')!r}, {str(REPO)!r}]\n"
    "warnings.simplefilter('ignore')\n"
    "from PIL import Image\n"
    "Image.init()\n"
    "from mastercode import attachment_parts as ap\n"
    "def hwm():\n"
    "    for line in open('/proc/self/status'):\n"
    "        if line.startswith('VmHWM:'):\n"
    "            return int(line.split()[1]) * 1024\n"
    "data = sys.stdin.buffer.read()\n"
    "base = hwm()\n"
    "m, out = ap._fit_bytes(data, 'image/png', ap.NATIVE_LONG_EDGE, ap.MAX_PICTURE_B64)\n"
    "print(hwm() - base, m)\n")
r = subprocess.run([sys.executable, "-c", child], input=big, capture_output=True, timeout=300)
try:
    peak = int(r.stdout.split()[0])
except (IndexError, ValueError):
    peak = None
decoded = side * side * 4                  # Pillow holds RGB as 4 bytes a pixel
check("fitting holds at most twice the decoded picture at its peak",
      peak is not None and peak < 2 * decoded,
      (f"{peak / decoded:.2f}x of {decoded // 1_000_000} MB" if peak else r.stderr[-300:]))

print(f"\n{'OK picture fit: native edge, many-picture edge, byte limit, alpha, first frame' if not FAILS else f'{len(FAILS)} FAILED'}")
sys.exit(1 if FAILS else 0)
