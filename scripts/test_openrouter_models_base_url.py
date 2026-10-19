#!/usr/bin/env python3
"""The OpenRouter model list is fetched from OPENROUTER_BASE_URL, the same
setting (and the same default) the chat calls use.

brain/mastercode/inference_config.py builds the chat endpoint from
OPENROUTER_BASE_URL (default https://openrouter.ai/api/v1), and
server/vault/providers.py reads the same variable. The catalog build in
server/routes/inference.py fetched a hardcoded https://openrouter.ai/api/v1/models,
so a Host pointed at another OpenRouter base listed models from one place and
called them at another.

Container-free: no network. urllib's opener is a stand-in that records the URL.
"""

import importlib
import os
import sys
import urllib.request

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))

FAILS: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"[openrouter-base] FAIL {name} {extra}")


SEEN: list[str] = []


class _Resp:
    def read(self):
        return b'{"data": [{"id": "x/y", "name": "Y"}]}'


class _Opener:
    def open(self, req, timeout=None):
        SEEN.append(req.full_url)
        return _Resp()


urllib.request.build_opener = lambda *a, **kw: _Opener()


def models_url(base):
    """The URL the catalog build fetches when OPENROUTER_BASE_URL is `base`
    (None: unset). The setting is read when inference_config is imported, as
    it is in a running server, so both modules are reloaded under it."""
    if base is None:
        os.environ.pop("OPENROUTER_BASE_URL", None)
    else:
        os.environ["OPENROUTER_BASE_URL"] = base
    import mastercode.inference_config as IC
    importlib.reload(IC)
    from server.routes import inference as INF
    importlib.reload(INF)
    SEEN.clear()
    out = INF._build_model_catalog()
    return (SEEN[0] if SEEN else None), out, IC


url, out, IC = models_url("http://or-mirror.test/api/v1/")
check("1 a set OPENROUTER_BASE_URL is where the model list comes from",
      url == "http://or-mirror.test/api/v1/models", url)
check("1b ...and the list it returns is the catalog", out.get("total_openrouter") == 1, out.get("total_openrouter"))
check("1c ...the same base the chat endpoint uses",
      IC.OPENROUTER_URL == "http://or-mirror.test/api/v1/chat/completions", IC.OPENROUTER_URL)

url, _, IC = models_url(None)
check("2 unset, the default is OpenRouter's own (unchanged)",
      url == "https://openrouter.ai/api/v1/models", url)
check("2b ...and the chat endpoint's default is unchanged",
      IC.OPENROUTER_URL == "https://openrouter.ai/api/v1/chat/completions", IC.OPENROUTER_URL)

if FAILS:
    print(f"\n{len(FAILS)} FAIL")
    sys.exit(1)
print("\nall pass")
