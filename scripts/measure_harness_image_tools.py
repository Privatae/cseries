#!/usr/bin/env python3
"""Re-measure what the harness images put on PATH and diff it against
system-apps/_harness_runtime/image_tools.py. NOT a gate (needs docker and the
images); run it when a harness Dockerfile or the shared base changes, then
paste what it prints into the module.

  python3 scripts/measure_harness_image_tools.py            # diff vs the module
  python3 scripts/measure_harness_image_tools.py --emit     # print the _COMMON tuple

Images: the newest LOCAL tag of $HARNESS_IMAGE_PREFIX<id> (for example
HARNESS_IMAGE_PREFIX=registry.example/cseries-harness-), for every <id> in
image_tools.known_images(). Pass --tag <id>=<tag> to pin. Without
HARNESS_IMAGE_PREFIX it refuses to run.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT / "system-apps"))
from _harness_runtime import image_tools as T  # noqa: E402

# Where the harness images are named; no default.
_REGS = tuple(p for p in [(os.environ.get("HARNESS_IMAGE_PREFIX") or "").strip()] if p)
_LS = 'for d in $(echo $PATH | tr : " "); do ls -1 "$d" 2>/dev/null; done | sort -u'


def _newest_tag(image: str) -> str | None:
    """'<repo>:<tag>' for the newest local v-tag of `image`, or None."""
    for reg in _REGS:
        out = subprocess.run(
            ["docker", "images", "--format", "{{.Tag}}", reg + image],
            capture_output=True, text=True, check=True).stdout.split()
        tags = [t for t in out if t.startswith("v")]
        if tags:
            newest = sorted(tags, key=lambda t: [int(x) for x in t[1:].split(".") if x.isdigit()])[-1]
            return f"{reg}{image}:{newest}"
    return None


def _measure(ref: str) -> set[str]:
    out = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "sh", ref, "-c", _LS],
        capture_output=True, text=True, check=True).stdout
    return set(out.split())


def main(argv: list[str]) -> int:
    pins: dict[str, str] = {}
    emit = "--emit" in argv
    for i, a in enumerate(argv):
        if a == "--tag" and i + 1 < len(argv):
            k, _, v = argv[i + 1].partition("=")
            pins[k] = v
    if not _REGS:
        print("!! set HARNESS_IMAGE_PREFIX to the harness images' name prefix")
        return 2
    measured: dict[str, set[str]] = {}
    for image in T.known_images():
        ref = f"{_REGS[0]}{image}:{pins[image]}" if pins.get(image) else _newest_tag(image)
        if not ref:
            print(f"!! no local image for {image} — pull one or pass --tag {image}=<tag>")
            return 2
        measured[image] = _measure(ref)
        print(f"measured {ref} — {len(measured[image])} executables on PATH")
    common = set.intersection(*measured.values())
    if emit:
        print(textwrap.fill(", ".join(repr(n) for n in sorted(common)), width=76,
                            initial_indent="    ", subsequent_indent="    ") + ",")
        return 0
    rc = 0
    if common != T.COMMON_EXECUTABLES:
        rc = 1
        print("COMMON drift: +", sorted(common - T.COMMON_EXECUTABLES),
              " -", sorted(T.COMMON_EXECUTABLES - common))
    for image, have in measured.items():
        extra = have - common
        recorded = T.IMAGE_EXTRA_EXECUTABLES.get(image, frozenset())
        if extra != recorded:
            rc = 1
            print(f"{image} extras drift: measured {sorted(extra)} vs recorded {sorted(recorded)}")
    print("in sync with image_tools.py" if rc == 0 else "UPDATE image_tools.py (see above)")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
