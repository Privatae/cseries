#!/usr/bin/env python3
"""Owner-named model servers (brain/mastercode/owner_endpoints.py), the parts
an end-to-end run does not pin down:

  1. Addresses are stored in one normal form, and bad ones are refused.
  2. A bastion serving the same model is listed, and matched, before the
     owner's server.
  3. The owner's servers are still listed when the bastion table cannot be
     read.

No database or container: the pool and the probe are stand-ins.

    python3 scripts/test_owner_endpoints_unit.py   (repo root, image's deps)
"""
import asyncio
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "brain")]

from mastercode import model_backends as MB  # noqa: E402
from mastercode import owner_endpoints as OE  # noqa: E402

failed = 0


def check(cond, what, detail=""):
    global failed
    print(("OK    " if cond else "FAIL  ") + what + ("" if cond else f" {detail}"))
    if not cond:
        failed = 1


def normal_forms():
    cases = {
        "http://192.168.1.50:11434/": "http://192.168.1.50:11434",
        "http://192.168.1.50:11434/v1/": "http://192.168.1.50:11434",
        "https://gpu.home/ollama/v1": "https://gpu.home/ollama",
        " http://[fd00::5]:8080 ": "http://[fd00::5]:8080",
    }
    for raw, want in cases.items():
        got = OE.normalize(raw)
        check(got == want, f"{raw.strip()!r} is stored as {want!r}", f"(got {got!r})")
    for bad in ("ftp://x", "http://u:p@h", "http://h/?q=1", "http://h/#f", "not a url", "http://h:99999"):
        try:
            OE.normalize(bad)
            check(False, f"{bad!r} is refused")
        except OE.EndpointError:
            check(True, f"{bad!r} is refused")
    check(OE.label("http://[fd00::5]:8080") == "[fd00::5]:8080" and OE.label("https://gpu.home") == "gpu.home:443",
          "a server is named host:port")


class Pool:
    def __init__(self, rows=None, fail=False):
        self.rows, self.fail = rows or [], fail

    async def fetch(self, *a):
        if self.fail:
            raise RuntimeError("no table")
        return self.rows


async def ordering():
    bastion_row = {"hostname": "desk", "reported_ip": "10.0.0.5", "models": ["tiny:latest"],
                   "llm_models": [], "llm_port": None}

    async def owner_models(pool, eid):
        return [{"id": "tiny:latest", "bastion": "192.168.1.50:11434",
                 "endpoint": "http://192.168.1.50:11434", "kind": "ollama", "source": "owner"},
                {"id": "big:latest", "bastion": "192.168.1.50:11434",
                 "endpoint": "http://192.168.1.50:11434", "kind": "ollama", "source": "owner"}]

    saved = OE.models
    OE.models = owner_models
    try:
        rows = await MB.list_local_models(Pool([bastion_row]), 1)
        check([r["bastion"] for r in rows] == ["desk", "192.168.1.50:11434", "192.168.1.50:11434"],
              "bastions are listed before the owner's servers", f"({rows})")
        hit = await MB.match_local_model(Pool([bastion_row]), 1, "tiny")
        check(hit and hit["bastion"] == "desk", "a model both serve is matched to the bastion", f"({hit})")
        hit = await MB.match_local_model(Pool([bastion_row]), 1, "big:latest")
        check(hit and hit["endpoint"] == "http://192.168.1.50:11434",
              "a model only the owner's server serves is matched to it", f"({hit})")
        rows = await MB.list_local_models(Pool(fail=True), 1)
        check(len(rows) == 2 and all(r["source"] == "owner" for r in rows),
              "the owner's servers are listed when the bastion table cannot be read", f"({rows})")
        try:
            await MB.list_local_models(Pool(fail=True), 1, strict=True)
            check(False, "strict mode still raises when the bastion table cannot be read")
        except RuntimeError:
            check(True, "strict mode still raises when the bastion table cannot be read")
    finally:
        OE.models = saved


normal_forms()
asyncio.run(ordering())
sys.exit(failed)
