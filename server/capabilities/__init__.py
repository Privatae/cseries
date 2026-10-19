"""
Capability dispatcher — picks the platform-specific implementation.

Shipping model:
  - Linux: Docker image `personal-computer-linux:*` → dispatcher binds
    `server.capabilities.linux.*`.
  - Windows: native PyInstaller-built installer (.exe / .msi). Process
    runs on the Windows host — `sys.platform == 'win32'` — and the
    dispatcher binds `server.capabilities.windows.*` (mss, pyautogui,
    pyperclip) for real Win32 screen + input + clipboard.
  - macOS: native PyInstaller-built installer (.pkg / .app). Dispatcher
    binds `server.capabilities.macos.*` when that module lands (pyobjc
    Quartz + Cocoa).

`file_access` is platform-agnostic — pure Python, stays at package root.
Override detection with `PRIVATAE_PC_PLATFORM=linux|windows|macos` for
tests and cross-platform dev.
"""

import os
import sys

# Platform detection — PRIVATAE_PC_PLATFORM override for test/debug,
# otherwise sys.platform. Windows containers report "win32", Linux
# containers (Alpine + glibc variants) report "linux".
_PLATFORM = os.environ.get("PRIVATAE_PC_PLATFORM", sys.platform).lower()

if _PLATFORM.startswith("win"):
    from server.capabilities.windows import screen_capture, mouse_keyboard, clipboard  # noqa: F401
else:
    from server.capabilities.linux import screen_capture, mouse_keyboard, clipboard  # noqa: F401

# file_access stays at package root — identical behavior across OS
from server.capabilities import file_access  # noqa: F401

__all__ = ["screen_capture", "mouse_keyboard", "clipboard", "file_access"]
