#!/usr/bin/env python3
"""Gate wrapper: "Use managed instead" in the Network app, under node.

scripts/test_embed_use_managed_button.js loads the REAL system-apps/network/
app.js in a minimal DOM sandbox and checks the button is offered only when the
managed embedder's catalog carries the current model. This wrapper exists so
the container-free gate list, which runs python files, can run it.
"""
import os
import subprocess
import sys


def member_catalog_is_empty() -> bool:
    """On a network member the Host's client is the member door's
    (server/bridge/member_client.py), which offers no embeddings op, so the
    managed catalog the apps read is empty there too and the button stays
    hidden. Runs the real catalog route against the real member client."""
    import asyncio
    import types
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(root, "brain"))
    sys.path.insert(0, root)
    from server.bridge.member_client import NETWORK_OPS, MemberClient
    door = MemberClient.__new__(MemberClient)       # no passport: request_full refuses first
    stub = types.ModuleType("server.main")
    stub.get_bridge_client = lambda: door
    saved = sys.modules.get("server.main")
    sys.modules["server.main"] = stub
    try:
        from server.routes import embedding
        out = asyncio.run(embedding.catalog())
    finally:
        if saved is not None:
            sys.modules["server.main"] = saved
        else:
            sys.modules.pop("server.main", None)
    ok = "embeddings.catalog" not in NETWORK_OPS and out == {"embedding_models": []}
    print(f"  {'ok  ' if ok else 'FAIL'}  a network member's managed catalog is empty, so the button stays hidden"
          + ("" if ok else f" (got {out})"))
    return ok


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    r = subprocess.run(["node", os.path.join(here, "test_embed_use_managed_button.js")],
                       capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    member_ok = member_catalog_is_empty()
    return r.returncode or (0 if member_ok else 1)


if __name__ == "__main__":
    sys.exit(main())
