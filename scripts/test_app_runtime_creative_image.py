#!/usr/bin/env python3
"""The creative toolchain image stays the python image plus its programs.

The operator runs all three containers of a process app (fetch, the app, the
egress wall) from whichever toolchain image its manifest names. So a creative app's
egress wall is the egress.py copied into sidecars/app-runtime-creative/
Dockerfile, and the wall's credential fixes reach creative apps only if that file
copies the same runtime files as the python image. This holds the two
Dockerfiles together, holds every program the creative image downloads to a
sha256 it verifies before use (Blender by its own pin, the ArtCraft programs by
crafts.txt), and holds the bridge and the engine apps to the same programs.

No docker, no network. The built image is proven separately by
scripts/app_runtime_creative_proof.py (the ship runbook runs it).

Run: APP_ROOT=<repo> python3 scripts/test_app_runtime_creative_image.py
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
PY = (ROOT / "sidecars/app-runtime-python/Dockerfile").read_text()
CR = (ROOT / "sidecars/app-runtime-creative/Dockerfile").read_text()
FAILS: list[str] = []


def check(label: str, ok: bool, extra: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + label + ("" if ok else f"  [{extra}]"))
    if not ok:
        FAILS.append(label)


def code(text: str) -> list[str]:
    """Dockerfile instructions with comments dropped and continuations joined."""
    lines, cur = [], ""
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        cur += (" " if cur else "") + s.rstrip("\\").strip()
        if not s.endswith("\\"):
            lines.append(cur)
            cur = ""
    return lines


py, cr = code(PY), code(CR)
py_from = [ln for ln in py if ln.startswith("FROM ")]
cr_from = [ln for ln in cr if ln.startswith("FROM ")]
base = py_from[0].split()[1]
check("the python image has one pinned base", len(py_from) == 1 and "@sha256:" in base, str(py_from))
check("every creative stage uses the python image's exact base",
      bool(cr_from) and all(ln.split()[1] == base for ln in cr_from), str(cr_from))

runtime_copies = [ln for ln in py if ln.startswith("COPY ")]
for ln in runtime_copies:
    check(f"creative copies the same runtime file: {ln.split()[-1]}", ln in cr, ln)
check("the runtime files are fetch.py, run.py and the egress wall",
      sorted(ln.split()[-1] for ln in runtime_copies)
      == ["/opt/app-runtime/egress.py", "/opt/app-runtime/fetch.py", "/opt/app-runtime/run.py"],
      str(runtime_copies))

final = cr[max(i for i, ln in enumerate(cr) if ln.startswith("FROM ")):]
for ln in py:
    if ln.startswith(("ENV ", "USER ", "WORKDIR ")) or ln.startswith("RUN groupadd"):
        check(f"creative keeps the python image's step: {ln[:60]}", ln in final, ln)


def apt_packages(lines: list[str]) -> set[str]:
    ln = next(x for x in lines if "apt-get install" in x)
    return set(re.search(r"--no-install-recommends (.+?) &&", ln).group(1).split())


py_pkgs, cr_pkgs = apt_packages(py), apt_packages(final)
check("creative installs every package the python toolchain has",
      py_pkgs <= cr_pkgs, str(sorted(py_pkgs - cr_pkgs)))
check("neither image sets an ENTRYPOINT (the operator renders the command)",
      not any(ln.startswith("ENTRYPOINT") for ln in py + final))
check("the creative image runs as uid 1000", final[-2:] == ["USER 1000", "WORKDIR /app-src"],
      str(final[-2:]))

args = dict(re.findall(r"^ARG (\w+)=(\S+)$", "\n".join(cr), re.M))
programs = sorted(k[:-4] for k in args if k.endswith("_URL"))
check("Blender is fetched by its own pinned URL", programs == ["BLENDER"], str(programs))
url, digest = args.get("BLENDER_URL", ""), args.get("BLENDER_SHA256", "")
check("blender: an https URL to a fixed release, not 'latest'",
      url.startswith("https://") and "latest" not in url, url)
check("blender: pinned to a sha256", bool(re.fullmatch(r"[0-9a-f]{64}", digest)), digest)
fetch = next(ln for ln in cr if '"$BLENDER_SHA256"' in ln)
check("blender: the checksum is verified before unpacking",
      "sha256sum -c" in fetch and fetch.index("sha256sum -c") < fetch.index("tar "), fetch)

# The ArtCraft programs: one pinned line each in crafts.txt, the Dockerfile's
# loop over it, and an engine app per program.
pins = [ln.split() for ln in (ROOT / "sidecars/app-runtime-creative/crafts.txt").read_text().splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")]
CRAFTS = [p[0] for p in pins]
check("crafts.txt: every line is <name> <version> <sha256>",
      all(len(p) == 3 and re.fullmatch(r"[a-z]+craft", p[0]) and re.fullmatch(r"\d+\.\d+\.\d+", p[1])
          and re.fullmatch(r"[0-9a-f]{64}", p[2]) for p in pins), str([p for p in pins if len(p) != 3]))
check("crafts.txt: each program once", len(set(CRAFTS)) == len(CRAFTS), str(CRAFTS))
loop = next((ln for ln in cr if "done < pins" in ln), "")
check("the image fetches every line of crafts.txt", "COPY sidecars/app-runtime-creative/crafts.txt /dl/crafts.txt" in cr
      and "github.com/storytold/$name/releases/download/v$ver/$f" in loop, loop[:120])
check("each program's checksum is verified before its archive is unpacked",
      "sha256sum -c" in loop and loop.index("sha256sum -c") < loop.index("tar "), loop[:200])
body = loop[loop.index("do ") + 3:loop.index("done < pins")]
steps = [st.strip() for st in body.split(";") if st.strip() and not st.strip().startswith("f=")]
check("every step of the loop stops the build when it fails (sh ignores set -e there)",
      steps and all(st.endswith("|| exit 1") for st in steps), str([st for st in steps if not st.endswith("|| exit 1")]))
check("the build checks it unpacked as many programs as crafts.txt pins",
      'test "$(ls /opt/craft | wc -l)" -eq "$(wc -l < pins)"' in loop, loop[-120:])
notices = [ln.split() for ln in (ROOT / "sidecars/app-runtime-creative/craft-notices.txt").read_text().splitlines()
           if ln.strip() and not ln.lstrip().startswith("#")]
pinned = {p[0]: p[1] for p in pins}
check("craft-notices.txt: every line is <name> <version> <file> <sha256>",
      all(len(n) == 4 and re.fullmatch(r"[0-9a-f]{64}", n[3]) for n in notices), str([n for n in notices if len(n) != 4]))
check("craft-notices.txt: each notice is from the version crafts.txt pins",
      all(pinned.get(n[0]) == n[1] for n in notices), str([n[:2] for n in notices if pinned.get(n[0]) != n[1]]))
check("craft-notices.txt: every pinned program has its NOTICE",
      sorted({n[0] for n in notices if n[2] == "NOTICE"}) == sorted(CRAFTS),
      str(sorted(set(CRAFTS) - {n[0] for n in notices if n[2] == "NOTICE"})))
nloop = next((ln for ln in cr if "done < notices" in ln), "")
nbody = nloop[nloop.index("do ") + 3:nloop.index("done < notices")] if nloop else ""
nsteps = [st.strip() for st in nbody.split(";") if st.strip()]
check("each notice is checked against its sha256, every step stopping the build when it fails",
      "sha256sum -c" in nloop and nsteps and all(st.endswith("|| exit 1") for st in nsteps), nloop[:200])
check("the build refuses a program without its NOTICE", 'test -f "$d/doc/NOTICE"' in nloop, nloop[-160:])
check("every program's command line is on the PATH",
      'for b in /opt/craft/*/bin/*-cli; do ln -s "$b" /usr/local/bin/; done' in "\n".join(final))

bridge = "COPY --chown=1000:1000 sidecars/app-runtime-creative/creative_mcp.py /opt/app-runtime/creative_mcp.py"
check("the creative image carries the MCP bridge its store apps run", bridge in final, bridge)
import ast  # noqa: E402
_tree = ast.parse((ROOT / "sidecars/app-runtime-creative/creative_mcp.py").read_text())
_mods = {n.names[0].name.split(".")[0] for n in ast.walk(_tree) if isinstance(n, ast.Import)} | \
        {(n.module or "").split(".")[0] for n in ast.walk(_tree) if isinstance(n, ast.ImportFrom)}
check("the bridge imports only the standard library (nothing is pip-installed in the image)",
      _mods <= set(sys.stdlib_module_names) | {"__future__"}, sorted(_mods - set(sys.stdlib_module_names)))
import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("creative_mcp", ROOT / "sidecars/app-runtime-creative/creative_mcp.py")
cm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cm)
check("the bridge serves exactly the pinned programs and Blender",
      sorted(cm.PROGRAMS) == sorted(CRAFTS + ["blender"]), str(sorted(set(cm.PROGRAMS) ^ set(CRAFTS + ["blender"]))))
engines = sorted(d.name[:-len("-engine")] for d in (ROOT / "system-apps").iterdir() if d.name.endswith("-engine"))
check("an engine app for every program, and no engine without one",
      engines == sorted(CRAFTS + ["blender"]), str(sorted(set(engines) ^ set(CRAFTS + ["blender"]))))
for program in engines:
    text = (ROOT / "system-apps" / f"{program}-engine" / "app.yaml").read_text()
    check(f"{program}-engine runs the bridge for {program} from the creative image",
          "image: creative" in text
          and f'command: ["python3", "/opt/app-runtime/creative_mcp.py", "{program}"]' in text, program)

sys.path.insert(0, str(ROOT))
from server.applications import manifest  # noqa: E402

check("manifest accepts runtime.image: creative", "creative" in manifest.ALLOWED_RUNTIME_IMAGES,
      str(manifest.ALLOWED_RUNTIME_IMAGES))

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("\nall passed")
