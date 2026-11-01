"""The browser sidecar installs its Python closure from a hash lock, and only from it.

The image pinned four direct packages and nothing under them, so starlette
was whatever fastapi allowed on build day (0.38.6, below four HIGH fixes), and pip
was upgraded unpinned. sidecars/browser/requirements.txt now locks every package
with hashes and the Dockerfile installs it with --require-hashes, then removes pip.
These checks read the two files only; no network, no docker.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SIDECAR = ROOT / "sidecars" / "browser"
LOCK = SIDECAR / "requirements.txt"
DOCKERFILE = SIDECAR / "Dockerfile"

# starlette 1.3.1 carries the last of the fixes the Dockerfile names
# (CVE-2024-47874, CVE-2025-62727, CVE-2026-48818, CVE-2026-54283).
STARLETTE_FLOOR = (1, 3, 1)

_HASH = re.compile(r"--hash=sha256:[0-9a-f]{64}$")


def _lock_entries() -> dict[str, tuple[str, list[str]]]:
    """name -> (version, hashes), joining backslash continuations."""
    assert LOCK.is_file(), "sidecars/browser/requirements.txt is missing"
    logical, buf = [], ""
    for raw in LOCK.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        logical.append(buf + line)
        buf = ""
    assert not buf, "requirements.txt ends inside a continuation"
    out: dict[str, tuple[str, list[str]]] = {}
    for entry in logical:
        spec, *opts = entry.split()
        m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9.+!-]+)", spec)
        assert m, f"not an exact pin: {spec!r}"
        name = re.sub(r"[-_.]+", "-", m.group(1)).lower()
        assert name not in out, f"{name} is pinned twice"
        bad = [o for o in opts if not _HASH.fullmatch(o)]
        assert not bad, f"{name}: unexpected option(s) {bad}"
        assert opts, f"{name}=={m.group(2)} has no --hash"
        out[name] = (m.group(2), opts)
    return out


def _dockerfile_runs() -> list[str]:
    """Each RUN instruction as one string, continuations joined."""
    runs, cur = [], None
    for raw in DOCKERFILE.read_text().splitlines():
        line = raw.strip()
        if cur is None:
            if not line.startswith("RUN "):
                continue
            cur = line[4:]
        else:
            if line.startswith("#"):
                continue
            cur += " " + line
        if cur.endswith("\\"):
            cur = cur[:-1]
            continue
        runs.append(cur)
        cur = None
    return runs


def test_every_lock_entry_is_an_exact_pin_with_hashes():
    entries = _lock_entries()
    for name in ("fastapi", "starlette", "uvicorn", "playwright", "pydantic", "pydantic-core",
                 "greenlet", "anyio", "h11", "websockets"):
        assert name in entries, f"{name} is not in the lock"


def test_starlette_is_at_or_above_the_cve_floor():
    version, _ = _lock_entries()["starlette"]
    parts = tuple(int(p) for p in re.findall(r"\d+", version)[:3])
    assert parts >= STARLETTE_FLOOR, f"starlette {version} is below {STARLETTE_FLOOR}"


def test_every_pip_install_in_the_image_requires_hashes():
    installs = [seg for run in _dockerfile_runs() for seg in run.split("&&")
                if re.search(r"\bpip3?\s+install\b", seg)]
    assert installs, "the Dockerfile installs nothing with pip"
    for seg in installs:
        assert "--require-hashes" in seg, f"unhashed pip install: {seg.strip()}"
        assert "--upgrade" not in seg.split(), f"unpinned upgrade: {seg.strip()}"
    assert any("-r /tmp/requirements.txt" in seg for seg in installs), \
        "the lock is not what the image installs"
    assert re.search(r"^COPY\s+requirements\.txt\s+/tmp/requirements\.txt$",
                     DOCKERFILE.read_text(), re.M), "requirements.txt is not copied in"


def test_pip_is_removed_after_the_install():
    runs = " && ".join(_dockerfile_runs())
    assert re.search(r"pip uninstall -y pip\b", runs), "pip stays in the image"
    assert "ensurepip/_bundled" in runs, "the bundled pip wheel stays in the image"


@pytest.mark.parametrize("pin", ["fastapi==", "uvicorn[standard]==", "playwright==", "pydantic=="])
def test_the_dockerfile_carries_no_pins_outside_the_lock(pin):
    body = "\n".join(_dockerfile_runs())
    assert pin not in body, f"{pin} is pinned in the Dockerfile; it belongs in requirements.txt"
