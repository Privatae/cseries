"""
Clipboard — Windows implementation.

Reads and writes the Windows system clipboard via pyperclip (wraps
OpenClipboard/SetClipboardData on Win32). Size-bounded to prevent
resource abuse.
"""

import logging

logger = logging.getLogger("privatae.pc.clipboard.windows")

MAX_READ_BYTES = 1 * 1024 * 1024   # 1 MB
MAX_WRITE_BYTES = 1 * 1024 * 1024


def read_clipboard() -> dict:
    try:
        import pyperclip
    except ImportError as e:
        return {"status": "error", "error": f"missing dependency: {e}"}

    try:
        content = pyperclip.paste()
        if content is None:
            return {"status": "ok", "content": "", "size": 0}
        encoded = content.encode("utf-8", errors="replace")
        if len(encoded) > MAX_READ_BYTES:
            encoded = encoded[:MAX_READ_BYTES]
            content = encoded.decode("utf-8", errors="replace")
        return {"status": "ok", "content": content, "size": len(encoded)}
    except Exception as e:
        logger.error("Windows clipboard read failed: %s", e)
        return {"status": "error", "error": str(e)}


def write_clipboard(content: str) -> dict:
    try:
        import pyperclip
    except ImportError as e:
        return {"status": "error", "error": f"missing dependency: {e}"}

    if content is None:
        content = ""
    encoded = str(content).encode("utf-8", errors="replace")
    if len(encoded) > MAX_WRITE_BYTES:
        return {"status": "error", "error": f"content too large (max {MAX_WRITE_BYTES}B)"}

    try:
        pyperclip.copy(content)
        return {"status": "ok", "size": len(encoded)}
    except Exception as e:
        logger.error("Windows clipboard write failed: %s", e)
        return {"status": "error", "error": str(e)}
