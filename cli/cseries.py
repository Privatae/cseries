#!/usr/bin/env python3
"""
cseries — command-line interface for the applications runtime.

Subcommands:
  pack       Pack a source directory into an unsigned .cseries bundle.
  sign       Sign an unsigned bundle with an Ed25519 private key (PEM).
  verify     Verify a signed bundle against a public key (raw, 32 bytes).
  install    Install a signed bundle into the running kernel via API.
  uninstall  Uninstall an app from the running kernel via API.
  list       List installed apps from the kernel API.
  keys       List trusted signing keys.
  bastion pair-token
             Mint a pairing token for a bastion on the machine and print it
             as JSON with the Host CA and the redeem URL.

The pack/sign/verify subcommands are pure file ops and work without a
running kernel (used by the image build to mint factory bundles).
The install/uninstall/list/keys subcommands talk to the kernel at
http://127.0.0.1:8800. bastion pair-token talks to the Host's database,
inside the Host's container.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

# Make `server.applications.bundles` importable when invoked from the
# repo root or a deployed install. The image build sets PYTHONPATH=/app;
# local dev runs against a source checkout.
sys.path.insert(0, "/app")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.applications import bundles  # noqa: E402


DEFAULT_API = os.environ.get("CSERIES_API", "http://127.0.0.1:8800")


def _http(method: str, path: str, *, body: bytes | None = None,
          content_type: str = "application/json",
          headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    hdrs = dict(headers or {})
    if body is not None and "Content-Type" not in hdrs:
        hdrs["Content-Type"] = content_type
    req = urllib.request.Request(
        DEFAULT_API + path,
        method=method,
        data=body,
        headers=hdrs,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _mint_token(app_id: str = "app-store") -> str | None:
    """Mint a per-window app token for `app_id` so install/uninstall
    requests can carry the X-Application-Token header. The /launch
    endpoint is itself unauthenticated (it IS the system that mints
    tokens), so this works without prior credentials. Returns None if
    minting fails."""
    code, resp = _http("POST", f"/api/applications/{app_id}/launch")
    if code != 200:
        return None
    try:
        return json.loads(resp).get("token")
    except Exception:
        return None


def cmd_pack(args) -> int:
    src = Path(args.source).resolve()
    if not (src / "app.yaml").exists():
        print(f"error: no app.yaml in {src}", file=sys.stderr)
        return 2
    raw = bundles.pack(src)
    out = Path(args.out) if args.out else Path.cwd() / f"{src.name}.unsigned.cseries"
    out.write_bytes(raw)
    print(f"packed {src.name}: {len(raw)} bytes -> {out}")
    return 0


def cmd_sign(args) -> int:
    bundle_bytes = Path(args.bundle).read_bytes()
    priv_pem = Path(args.key).read_bytes()
    signed = bundles.sign(bundle_bytes, priv_pem)
    out = Path(args.out) if args.out else Path(args.bundle).with_suffix(".cseries")
    out.write_bytes(signed)
    print(f"signed: {len(signed)} bytes -> {out}")
    return 0


def cmd_verify(args) -> int:
    bundle_bytes = Path(args.bundle).read_bytes()
    pub_raw = Path(args.key).read_bytes()
    try:
        contents = bundles.verify(bundle_bytes, public_key_raw=pub_raw)
    except bundles.BundleError as e:
        print(f"verify FAILED: {e}", file=sys.stderr)
        return 1
    print(f"verify OK — manifest {len(contents.manifest_yaml)}B, "
          f"payload {len(contents.payload)} files, "
          f"fingerprint {bundles.fingerprint(pub_raw)[:16]}...")
    return 0


def cmd_install(args) -> int:
    # /install now requires execute:install (B7 audit fix). Mint an
    # app-store token to carry the scope. The /launch endpoint is itself
    # unauthenticated and returns a token with the requested app's scopes.
    token = _mint_token("app-store")
    if not token:
        print("error: could not mint app-store token (is the kernel up?)",
              file=sys.stderr)
        return 2
    body = Path(args.bundle).read_bytes()
    code, resp = _http("POST", "/api/applications/install",
                       body=body, content_type="application/zip",
                       headers={"X-Application-Token": token})
    print(f"HTTP {code}")
    try:
        print(json.dumps(json.loads(resp), indent=2))
    except Exception:
        print(resp.decode(errors="replace"))
    return 0 if code == 200 else 1


def cmd_uninstall(args) -> int:
    code, resp = _http("DELETE", f"/api/applications/{args.app_id}")
    print(f"HTTP {code}")
    try:
        print(json.dumps(json.loads(resp), indent=2))
    except Exception:
        print(resp.decode(errors="replace"))
    return 0 if code == 200 else 1


def cmd_list(args) -> int:
    code, resp = _http("GET", "/api/applications")
    if code != 200:
        print(f"HTTP {code}: {resp.decode(errors='replace')}", file=sys.stderr)
        return 1
    apps = json.loads(resp).get("applications", [])
    print(f"{'ID':<16} {'PROVENANCE':<10} {'VERSION':<8} PERMS  SIGNER")
    for a in apps:
        sig = (a.get("source_signature") or "")[:16] + ("..." if a.get("source_signature") else "")
        prov = a.get("provenance") or a.get("tier") or "?"
        print(f"{a['id']:<16} {prov:<10} {a['version']:<8} {len(a['permissions']):>5}  {sig}")
    return 0


def cmd_keys(args) -> int:
    import asyncio
    sys.path.insert(0, "/app")
    from server import brain_service
    from server.applications import trust_store

    async def main():
        await brain_service.initialize(entity_id=0)
        pool = brain_service.get_pool()
        keys = await trust_store.list_active_keys(pool)
        print(f"{'PUBLISHER':<16} {'PROVENANCE':<28} FINGERPRINT")
        for k in keys:
            provs = ",".join(k.provenance_allowed)
            print(f"{k.publisher:<16} {provs:<28} {k.fingerprint[:32]}...")
    asyncio.run(main())
    return 0


def pair_token_payload(minted: dict, host_ca_pem: str, enroll: str) -> dict:
    """What `bastion pair-token` prints: exactly these three keys."""
    return {"token": minted["token"], "host_ca_pem": host_ca_pem, "enroll": enroll}


def cmd_bastion_pair_token(args) -> int:
    """A one-time pairing token (15 minutes, single use: the one the Host's
    "Set up a bastion" mints), the Host CA the bastion must trust, and the
    redeem URL on the enroll port. Run through the container's exec channel
    by whoever installed the Host, so pairing adds no HTTP route that mints a
    token."""
    import asyncio
    import asyncpg
    from server.home_origin import HOME
    from server.network import bastion_enroll, host_ca, local_bastion

    async def run() -> dict:
        dsn = os.environ.get("DATABASE_URL", "postgresql://entity@/hostmem?host=/var/run/postgresql")
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
        try:
            await host_ca.ensure(pool)
            # The handle a standalone Host redeems under: its own setting
            # (bastion_enroll.resolve_self_handle on a Host with no platform).
            minted = await bastion_enroll.mint_pairing_token(
                pool, entity_id=int(os.environ.get("ENTITY_ID", "1")),
                handle=HOME.handle, name=args.name)
            ca = await host_ca.ca_cert_pem(pool)
        finally:
            await pool.close()
        return pair_token_payload(minted, ca, local_bastion.enroll_url())

    print(json.dumps(asyncio.run(run())))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="cseries", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("pack", help="pack a source directory into an unsigned bundle")
    sp.add_argument("source")
    sp.add_argument("--out", "-o")
    sp.set_defaults(func=cmd_pack)

    sp = sub.add_parser("sign", help="sign an unsigned bundle")
    sp.add_argument("bundle")
    sp.add_argument("--key", "-k", required=True, help="Ed25519 private key PEM")
    sp.add_argument("--out", "-o")
    sp.set_defaults(func=cmd_sign)

    sp = sub.add_parser("verify", help="verify a signed bundle against a public key")
    sp.add_argument("bundle")
    sp.add_argument("--key", "-k", required=True, help="raw 32-byte Ed25519 public key")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("install", help="install a signed bundle via API")
    sp.add_argument("bundle")
    sp.set_defaults(func=cmd_install)

    sp = sub.add_parser("uninstall", help="uninstall an app via API")
    sp.add_argument("app_id")
    sp.set_defaults(func=cmd_uninstall)

    sp = sub.add_parser("list", help="list installed apps")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("keys", help="list trusted signing keys")
    sp.set_defaults(func=cmd_keys)

    sp = sub.add_parser("bastion", help="pair the bastion on this machine")
    bsub = sp.add_subparsers(dest="bastion_cmd", required=True)
    bp = bsub.add_parser("pair-token", help="print a pairing token, the Host CA and the "
                                             "redeem URL as JSON")
    bp.add_argument("--name", default="this machine",
                    help="what the Host calls the bastion (default: this machine)")
    bp.set_defaults(func=cmd_bastion_pair_token)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
