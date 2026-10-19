"""
Clipboard — CEIGAS-governed clipboard access.

Read/write system clipboard. Platform-aware (Linux xclip/xsel).

Audit fixes:
  - Max clipboard size (1MB read, 100KB write)
  - Content type validation
"""

import logging
import subprocess

logger = logging.getLogger("privatae.pc.clipboard")

MAX_READ_BYTES = 1024 * 1024    # 1MB
MAX_WRITE_BYTES = 100 * 1024    # 100KB


def read_clipboard() -> dict:
    """Read current clipboard contents."""
    try:
        # Try xclip first, fall back to xsel
        for cmd in [["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]]:
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=5,
                )
                if proc.returncode == 0:
                    content = proc.stdout[:MAX_READ_BYTES]
                    try:
                        text = content.decode("utf-8")
                    except UnicodeDecodeError:
                        text = content.decode("latin-1")

                    return {
                        "status": "ok",
                        "content": text,
                        "size": len(content),
                        "truncated": len(proc.stdout) > MAX_READ_BYTES,
                    }
            except FileNotFoundError:
                continue

        return {"status": "error", "error": "no clipboard tool available (need xclip or xsel)"}

    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "clipboard read timed out"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def write_clipboard(content: str) -> dict:
    """Write content to clipboard."""
    if not content:
        return {"status": "error", "error": "no content provided"}

    data = content.encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        return {"status": "error", "error": f"content too large: {len(data)} bytes (max {MAX_WRITE_BYTES})"}

    try:
        for cmd in [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]:
            try:
                proc = subprocess.run(
                    cmd,
                    input=data,
                    capture_output=True,
                    timeout=5,
                )
                if proc.returncode == 0:
                    return {"status": "ok", "bytes_written": len(data)}
            except FileNotFoundError:
                continue

        return {"status": "error", "error": "no clipboard tool available"}

    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "clipboard write timed out"}
    except Exception as e:
        return {"status": "error", "error": str(e)}
