"""
Screen Capture — CEIGAS-governed screenshot capability.

Captures the user's screen via mss (Linux/X11).
Returns base64 JPEG with configurable quality bounds.

Audit fixes:
  - Quality bounded to [10, 95] (prevents resource abuse)
  - Max dimension cap (4K)
  - Output size limit (10MB base64)
"""

import base64
import io
import logging
from typing import Optional

logger = logging.getLogger("privatae.pc.screen_capture")

# Quality bounds (audit fix: prevents resource abuse)
MIN_QUALITY = 10
MAX_QUALITY = 95
DEFAULT_QUALITY = 60
MAX_DIMENSION = 3840  # 4K cap
MAX_OUTPUT_BYTES = 10 * 1024 * 1024  # 10MB base64 limit


def capture_screen(
    quality: int = DEFAULT_QUALITY,
    monitor: int = 0,
    region: Optional[dict] = None,
) -> dict:
    """
    Capture a screenshot.

    Args:
        quality: JPEG quality (10-95, bounded)
        monitor: Monitor index (0 = all monitors combined)
        region: Optional {"left": x, "top": y, "width": w, "height": h}

    Returns:
        {"status": "ok", "image": base64_jpeg, "width": w, "height": h, "size": bytes}
    """
    # Bound quality
    quality = max(MIN_QUALITY, min(MAX_QUALITY, quality))

    try:
        import mss
        from PIL import Image

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

            screenshot = sct.grab(grab_area)
            img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")

            # Cap dimensions
            if img.width > MAX_DIMENSION or img.height > MAX_DIMENSION:
                ratio = min(MAX_DIMENSION / img.width, MAX_DIMENSION / img.height)
                new_size = (int(img.width * ratio), int(img.height * ratio))
                img = img.resize(new_size, Image.LANCZOS)

            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            raw = buf.getvalue()

            if len(raw) > MAX_OUTPUT_BYTES:
                return {
                    "status": "error",
                    "error": f"screenshot too large: {len(raw)} bytes (max {MAX_OUTPUT_BYTES})",
                }

            b64 = base64.b64encode(raw).decode("ascii")

            return {
                "status": "ok",
                "image": b64,
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
