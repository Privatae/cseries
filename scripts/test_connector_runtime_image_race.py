#!/usr/bin/env python3
"""The connector gates' runtime image survives two gates building it at once.

The Telegram, Slack and Discord gates each need the app runtime image, tagged
by the hash of what goes into it and built only when absent
(connector_gate_kit.runtime_image). They run side by side, so two can find
the tag absent and build together; the one that tags second fails with
"image ... already exists". One CI run failed the Slack gate's
part M that way. That image is the one this gate needs, so the build that
lost the race uses it. A build that fails with no image left behind still
fails.

Docker is stood in for, so this runs anywhere: the race is decided by what
`docker image inspect` answers after the build, which is what is checked.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import connector_gate_kit as kit  # noqa: E402

RACE = "ERROR: failed to solve: image \"docker.io/library/cseries-app-runtime-python-gate:x\": already exists\n"
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def fake_docker(inspects: list[int], build_rc: int, calls: list[list[str]]):
    """subprocess.run for docker: each `image inspect` answers the next exit
    code in `inspects`; `build` answers `build_rc`, with the race's message."""
    real = subprocess.run

    def run(argv, *args, **kwargs):
        if argv[:1] != ["docker"]:
            return real(argv, *args, **kwargs)
        calls.append(list(argv))
        if argv[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(argv, inspects.pop(0), "", "")
        if argv[1] == "build":
            return subprocess.CompletedProcess(argv, build_rc, "", RACE if build_rc else "")
        raise AssertionError(f"unexpected docker call: {argv}")
    return run


def runtime_image_with(inspects: list[int], build_rc: int) -> tuple[tuple[str | None, str], list[list[str]]]:
    calls: list[list[str]] = []
    saved = kit.subprocess.run
    kit.subprocess.run = fake_docker(inspects, build_rc, calls)
    try:
        return kit.runtime_image(ROOT, "race-test"), calls
    finally:
        kit.subprocess.run = saved


def main() -> int:
    (image, how), calls = runtime_image_with(inspects=[1, 0], build_rc=1)
    check("a build that loses the tag race uses the image the other gate tagged",
          image is not None and image.startswith("cseries-app-runtime-python-gate:"), f"got {image!r}: {how}")
    check("and says so", "beside" in how, how)
    check("it built once, and looked for the tag before and after",
          [c[1] for c in calls] == ["image", "build", "image"], str(calls))

    (image, how), _ = runtime_image_with(inspects=[1, 1], build_rc=1)
    check("a build that fails with no image left behind still fails",
          image is None and how.startswith("docker build failed"), f"got {image!r}: {how}")

    (image, how), calls = runtime_image_with(inspects=[0], build_rc=0)
    check("an image already built is used without a build",
          image is not None and how == "built from this tree before" and len(calls) == 1, f"{how} {calls}")

    (image, how), _ = runtime_image_with(inspects=[1], build_rc=0)
    check("a build that succeeds is used", image is not None and how == "built from this tree now", how)

    import test_connector_telegram_example as telegram
    check("the Telegram gate builds through the same function",
          telegram.runtime_image.__code__.co_names.count("runtime_image") == 1
          and "connector_gate_kit" in telegram.runtime_image.__code__.co_names)

    print(f"\nruntime image race: {'all gates passed' if not failures else f'{len(failures)} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
