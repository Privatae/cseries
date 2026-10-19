#!/usr/bin/env python3
"""A harness's bare-key OpenRouter default follows OPENROUTER_BASE_URL, the
same setting (and the same default) the kernel's chat calls and model list use.

system-apps/_harness_runtime/harness_core.py wraps a bare (non-JSON) key for a
provider_json harness to HarnessSpec.bare_key_base_url. Its default was a
hardcoded https://openrouter.ai/api/v1 while brain/mastercode/inference_config.py
OPENROUTER_BASE_URL (and server/vault/providers.py) read the env, so a Host
pointed at another OpenRouter base had its harness keys dialled at openrouter.ai.
In-container harnesses inherit the kernel's env (server/applications/
supervisor.py _child_env), so the setting reaches them.

Each case runs in a fresh interpreter: the setting is read at import, as in a
running harness. A spec that takes the default (KIMI's fields minus its own
bare-key bases) reads a bare key from a temp creds file through the real
_read_creds. Container-free: no network (no alt bases, so no probe).
"""

import json
import os
import subprocess
import sys
import tempfile

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAILS: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"[harness-openrouter-base] FAIL {name} {extra}")


CHILD = r"""
import dataclasses, json, os, sys
from pathlib import Path
root = sys.argv[1]
sys.path.insert(0, os.path.join(root, "cli"))
sys.path.insert(0, os.path.join(root, "system-apps"))
sys.path.insert(0, os.path.join(root, "brain"))
from _harness_runtime import harness_core as hc
from _harness_runtime import specs
import mastercode.inference_config as IC
creds = Path(sys.argv[2])
creds.write_text("sk-or-v1-bare")
kw = {f.name: getattr(specs.KIMI, f.name) for f in dataclasses.fields(specs.KIMI)
      if f.name not in ("bare_key_base_url", "bare_key_alt_base_urls")}
kw["creds_path"] = creds
spec = hc.HarnessSpec(**kw)
got = hc._read_creds(spec)
print(json.dumps({"base": (got or {}).get("base_url"), "chat": IC.OPENROUTER_URL,
                  "kimi": specs.KIMI.bare_key_base_url}))
"""


def run(base):
    env = {k: v for k, v in os.environ.items() if k not in ("OPENROUTER_BASE_URL", "MOONSHOT_BASE_URL")}
    if base is not None:
        env["OPENROUTER_BASE_URL"] = base
    with tempfile.TemporaryDirectory() as td:
        r = subprocess.run([sys.executable, "-c", CHILD, APP_ROOT, os.path.join(td, "creds")],
                           env=env, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return {"error": r.stderr[-800:]}
    return json.loads(r.stdout.strip().splitlines()[-1])


got = run("http://or-mirror.test/api/v1/")
check("1 a set OPENROUTER_BASE_URL is where a bare key is wrapped to",
      got.get("base") == "http://or-mirror.test/api/v1", got)
check("1b ...the same base the kernel's chat endpoint is built from",
      got.get("chat") == f"{got.get('base')}/chat/completions", got)
check("1c ...and a harness with its own vendor base (kimi) keeps it",
      got.get("kimi") == "https://api.moonshot.ai/v1", got)

got = run(None)
check("2 unset, the default is OpenRouter's own (unchanged)",
      got.get("base") == "https://openrouter.ai/api/v1", got)
check("2b ...matching the kernel's default", got.get("chat") == "https://openrouter.ai/api/v1/chat/completions", got)

if FAILS:
    print(f"\n{len(FAILS)} FAIL")
    sys.exit(1)
print("\nall pass")
