"""
Mouse & Keyboard — CEIGAS-governed input simulation.

Uses xdotool (Linux/X11) for desktop interaction.
Entity sees the screen, CEIGAS authorizes each input action.

Audit fixes:
  - Key validation: only allow known key names (prevents injection)
  - Coordinate bounds checking
  - Action type whitelist
"""

import logging
import subprocess

logger = logging.getLogger("privatae.pc.mouse_keyboard")

# Allowed key names (xdotool key names)
ALLOWED_KEYS = frozenset({
    # Letters
    *"abcdefghijklmnopqrstuvwxyz",
    *"ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    # Numbers
    *"0123456789",
    # Modifiers
    "shift", "ctrl", "alt", "super", "Meta_L", "Meta_R",
    "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
    # Navigation
    "Return", "Escape", "Tab", "BackSpace", "Delete",
    "Home", "End", "Page_Up", "Page_Down",
    "Up", "Down", "Left", "Right",
    # Function keys
    "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12",
    # Symbols
    "space", "minus", "equal", "bracketleft", "bracketright",
    "backslash", "semicolon", "apostrophe", "grave", "comma",
    "period", "slash", "plus", "underscore",
})

# Allowed key combo modifiers
ALLOWED_MODIFIERS = frozenset({"ctrl", "alt", "shift", "super"})

# Screen bounds (validated before action)
MAX_X = 7680   # 8K width
MAX_Y = 4320   # 8K height

# Allowed action types
ALLOWED_ACTIONS = frozenset({
    "click", "move", "type", "key", "scroll", "drag",
})


def _validate_coords(x: int, y: int) -> tuple[bool, str]:
    """Validate screen coordinates are within bounds."""
    if not isinstance(x, int) or not isinstance(y, int):
        return False, "coordinates must be integers"
    if x < 0 or x > MAX_X or y < 0 or y > MAX_Y:
        return False, f"coordinates ({x}, {y}) out of bounds"
    return True, ""


def _validate_key(key: str) -> tuple[bool, str]:
    """Validate a key name is allowed."""
    # Handle key combos like "ctrl+c"
    parts = key.split("+")
    for part in parts:
        part = part.strip()
        if part not in ALLOWED_KEYS and part not in ALLOWED_MODIFIERS:
            return False, f"key '{part}' not in allowed key set"
    return True, ""


def _run_xdotool(args: list[str], timeout: int = 5) -> dict:
    """Run xdotool with args, return result."""
    try:
        proc = subprocess.run(
            ["xdotool", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            return {"status": "error", "error": proc.stderr.strip() or "xdotool failed"}
        return {"status": "ok"}
    except FileNotFoundError:
        return {"status": "error", "error": "xdotool not installed"}
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "xdotool timed out"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def execute_action(action: str, params: dict) -> dict:
    """
    Execute a mouse/keyboard action.

    Args:
        action: One of click, move, type, key, scroll, drag
        params: Action-specific parameters

    Returns:
        {"status": "ok"} or {"status": "error", "error": "..."}
    """
    if action not in ALLOWED_ACTIONS:
        return {"status": "error", "error": f"unknown action: {action}"}

    if action == "click":
        x = params.get("x", 0)
        y = params.get("y", 0)
        button = params.get("button", 1)
        ok, err = _validate_coords(x, y)
        if not ok:
            return {"status": "error", "error": err}
        if button not in (1, 2, 3):
            return {"status": "error", "error": "button must be 1, 2, or 3"}
        result = _run_xdotool(["mousemove", str(x), str(y)])
        if result["status"] != "ok":
            return result
        return _run_xdotool(["click", str(button)])

    elif action == "move":
        x = params.get("x", 0)
        y = params.get("y", 0)
        ok, err = _validate_coords(x, y)
        if not ok:
            return {"status": "error", "error": err}
        return _run_xdotool(["mousemove", str(x), str(y)])

    elif action == "type":
        text = params.get("text", "")
        if not text:
            return {"status": "error", "error": "no text provided"}
        if len(text) > 1000:
            return {"status": "error", "error": "text too long (max 1000 chars)"}
        return _run_xdotool(["type", "--clearmodifiers", text])

    elif action == "key":
        key = params.get("key", "")
        if not key:
            return {"status": "error", "error": "no key provided"}
        ok, err = _validate_key(key)
        if not ok:
            return {"status": "error", "error": err}
        return _run_xdotool(["key", key])

    elif action == "scroll":
        x = params.get("x", 0)
        y = params.get("y", 0)
        direction = params.get("direction", "down")
        clicks = min(params.get("clicks", 3), 20)
        ok, err = _validate_coords(x, y)
        if not ok:
            return {"status": "error", "error": err}
        result = _run_xdotool(["mousemove", str(x), str(y)])
        if result["status"] != "ok":
            return result
        button = "5" if direction == "down" else "4"
        return _run_xdotool(["click", "--repeat", str(clicks), button])

    elif action == "drag":
        x1 = params.get("x1", 0)
        y1 = params.get("y1", 0)
        x2 = params.get("x2", 0)
        y2 = params.get("y2", 0)
        ok1, err1 = _validate_coords(x1, y1)
        ok2, err2 = _validate_coords(x2, y2)
        if not ok1:
            return {"status": "error", "error": err1}
        if not ok2:
            return {"status": "error", "error": err2}
        result = _run_xdotool(["mousemove", str(x1), str(y1)])
        if result["status"] != "ok":
            return result
        result = _run_xdotool(["mousedown", "1"])
        if result["status"] != "ok":
            return result
        result = _run_xdotool(["mousemove", str(x2), str(y2)])
        if result["status"] != "ok":
            return result
        return _run_xdotool(["mouseup", "1"])

    return {"status": "error", "error": f"unhandled action: {action}"}
