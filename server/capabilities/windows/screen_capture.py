"""
Screen Capture — Windows implementation.

Captures the Windows desktop via native Win32 APIs. Runs inside a Windows
container (microsoft/windows/servercore or nanoserver base) with access
to the host session via `--isolation=process` OR as a Hyper-V isolated
container with RDP/session sharing.

mss supports Windows natively (uses GDI BitBlt); falls back to Pillow
ImageGrab if mss import fails.

API identical to the Linux implementation — same bounds, same response
shape — so the dispatcher can bind either one without the caller caring.
"""

import base64
import io
import logging
from typing import Optional

logger = logging.getLogger("privatae.pc.screen_capture.windows")

MIN_QUALITY = 10
MAX_QUALITY = 95
DEFAULT_QUALITY = 60
MAX_DIMENSION = 3840
MAX_OUTPUT_BYTES = 10 * 1024 * 1024


def capture_screen(
    quality: int = DEFAULT_QUALITY,
    monitor: int = 0,
    region: Optional[dict] = None,
) -> dict:
    """Capture the Windows desktop. Same contract as Linux variant."""
    quality = max(MIN_QUALITY, min(MAX_QUALITY, quality))

    try:
        from PIL import Image

        img = None

        # Preferred: mss (native BitBlt on Windows, fast multi-monitor)
        try:
            import mss
            with mss.mss() as sct:
                if region:
                    grab_area = {
                        "left": int(region.get("left", 0)),
                        "top": int(region.get("top", 0)),
                        "width": min(int(region.get("width", 1920)), MAX_DIMENSION),
                        "height": min(int(region.get("height", 1080)), MAX_DIMENSION),
                    }
                else:
                    monitors = sct.monitors
                    if monitor < 0 or monitor >= len(monitors):
                        monitor = 0
                    grab_area = monitors[monitor]
                shot = sct.grab(grab_area)
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        except Exception as e:
            logger.debug("mss failed on Windows (%s) — falling back to ImageGrab", e)

        # Fallback: PIL.ImageGrab (uses Windows DesktopWindow)
        if img is None:
            from PIL import ImageGrab
            if region:
                bbox = (
                    int(region.get("left", 0)),
                    int(region.get("top", 0)),
                    int(region.get("left", 0)) + min(int(region.get("width", 1920)), MAX_DIMENSION),
                    int(region.get("top", 0)) + min(int(region.get("height", 1080)), MAX_DIMENSION),
                )
                img = ImageGrab.grab(bbox=bbox, all_screens=True)
            else:
                img = ImageGrab.grab(all_screens=(monitor == 0))

        if img.width > MAX_DIMENSION or img.height > MAX_DIMENSION:
            ratio = min(MAX_DIMENSION / img.width, MAX_DIMENSION / img.height)
            new_size = (int(img.width * ratio), int(img.height * ratio))
            img = img.resize(new_size, Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        raw = buf.getvalue()

        if len(raw) > MAX_OUTPUT_BYTES:
            return {"status": "error", "error": f"screenshot too large: {len(raw)} bytes"}

        return {
            "status": "ok",
            "image": base64.b64encode(raw).decode("ascii"),
            "width": img.width,
            "height": img.height,
            "size": len(raw),
            "quality": quality,
        }

    except ImportError as e:
        return {"status": "error", "error": f"missing dependency: {e}"}
    except Exception as e:
        logger.error("Screen capture failed: %s", e)
        return {"status": "error", "error": str(e)}
