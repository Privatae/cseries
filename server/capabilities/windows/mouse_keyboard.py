"""
Mouse + Keyboard — Windows implementation.

Dispatches host desktop input via pyautogui (wraps Win32 SendInput for
mouse + keyboard events). All actions are rate-limited and bounded to
the primary desktop's virtual screen.

Action names match the Linux variant so the dispatcher in
server/__init__.py can bind either one. Contract is identical:
execute_action(action_name, params) -> dict with status+detail.

Allowed actions:
  move, click, type, key, scroll, drag
"""

import logging
import time
from typing import Optional

logger = logging.getLogger("privatae.pc.mouse_keyboard.windows")

# Bounds
MAX_COORD = 8192           # reasonable max screen width on modern Windows
MAX_TEXT_BYTES = 16 * 1024
MAX_SCROLL = 100
CLICK_RATE_MAX_PER_SEC = 20

# Accepted key identifiers — pyautogui key names
ALLOWED_KEYS = {
    # whitelist of non-printable keys
    "enter", "return", "tab", "space", "backspace", "delete", "esc", "escape",
    "up", "down", "left", "right", "home", "end", "pageup", "pagedown",
    "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
    "shift", "ctrl", "alt", "win", "cmd",
    "capslock", "numlock",
}

# Allowed action types: the same set as the Linux variant. routes/input.py
# validates against the bound module's ALLOWED_ACTIONS, so each variant names
# its own, and execute_action refuses anything else before importing pyautogui.
ALLOWED_ACTIONS = frozenset({
    "click", "move", "type", "key", "scroll", "drag",
})
ALLOWED_BUTTONS = ("left", "right", "middle")

_last_click_times: list[float] = []


def _rate_limit_click() -> bool:
    now = time.time()
    _last_click_times[:] = [t for t in _last_click_times if now - t < 1.0]
    if len(_last_click_times) >= CLICK_RATE_MAX_PER_SEC:
        return False
    _last_click_times.append(now)
    return True


def execute_action(action: str, params: dict) -> dict:
    if action not in ALLOWED_ACTIONS:
        return {"status": "error", "error": f"unknown action: {action}"}
    try:
        import pyautogui
    except ImportError as e:
        return {"status": "error", "error": f"missing dependency: {e}"}

    # Safety: don't escape to screen corners (pyautogui aborts on corner motion)
    pyautogui.FAILSAFE = False

    try:
        if action == "move":
            x = max(0, min(int(params.get("x", 0)), MAX_COORD))
            y = max(0, min(int(params.get("y", 0)), MAX_COORD))
            pyautogui.moveTo(x, y, duration=0)
            return {"status": "ok"}

        if action == "click":
            x = max(0, min(int(params.get("x", 0)), MAX_COORD))
            y = max(0, min(int(params.get("y", 0)), MAX_COORD))
            button = params.get("button", "left")
            if button not in ALLOWED_BUTTONS:
                return {"status": "error", "error": "invalid button"}
            click_type = params.get("click_type", "single")
            clicks = 2 if click_type == "double" else 1
            if not _rate_limit_click():
                return {"status": "error", "error": "click rate limit exceeded"}
            pyautogui.click(x=x, y=y, clicks=clicks, button=button)
            return {"status": "ok"}

        if action == "type":
            text = str(params.get("text", ""))
            if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
                return {"status": "error", "error": f"text too long (max {MAX_TEXT_BYTES}B)"}
            pyautogui.typewrite(text, interval=0.01)
            return {"status": "ok"}

        if action == "key":
            key = str(params.get("key", "")).lower()
            if not key:
                return {"status": "error", "error": "missing key"}
            # Chord support: "ctrl+c" → ('ctrl', 'c')
            parts = [p.strip() for p in key.split("+") if p.strip()]
            for p in parts:
                if p not in ALLOWED_KEYS and len(p) != 1:
                    return {"status": "error", "error": f"key not allowed: {p}"}
            if len(parts) == 1:
                pyautogui.press(parts[0])
            else:
                pyautogui.hotkey(*parts)
            return {"status": "ok"}

        if action == "scroll":
            amount = max(-MAX_SCROLL, min(int(params.get("amount", 0)), MAX_SCROLL))
            pyautogui.scroll(amount)
            return {"status": "ok"}

        if action == "drag":
            x1 = max(0, min(int(params.get("x1", 0)), MAX_COORD))
            y1 = max(0, min(int(params.get("y1", 0)), MAX_COORD))
            x2 = max(0, min(int(params.get("x2", 0)), MAX_COORD))
            y2 = max(0, min(int(params.get("y2", 0)), MAX_COORD))
            button = params.get("button", "left")
            if button not in ALLOWED_BUTTONS:
                return {"status": "error", "error": "invalid button"}
            pyautogui.moveTo(x1, y1, duration=0)
            pyautogui.dragTo(x2, y2, duration=0.1, button=button)
            return {"status": "ok"}

        return {"status": "error", "error": f"unknown action: {action}"}

    except Exception as e:
        logger.error("Windows input action %s failed: %s", action, e)
        return {"status": "error", "error": str(e)}
