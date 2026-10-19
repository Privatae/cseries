#!/usr/bin/env python3
"""Init step of a process app's runtime pod: pull the app's install dir from
its kernel and unpack it into the working directory the main container runs
in. Stdlib only — this runs before anything the app installs exists.

Env (rendered by the operator, kopf_handlers _reconcile_app_runtime):
  CSERIES_KERNEL_URL              http://entity-<id>-0.entity-<id>.entity-<id>.svc.cluster.local:8800
  PRIVATAE_APP_ID                  the app id
  APP_RUNTIME_SOURCE_TOKEN      the per-app capability (Secret; see
                                cseries harness_backend.app_source_token)
  APP_SRC                       target dir (default /app-src)

The source route (cseries routes/applications.py:app_source) is the ONLY thing
this token can reach, and it answers with this one app's own files. A pod
that cannot fetch its source has nothing to run, so any failure exits
non-zero and the wake reports it (the operator scales the pod back).

Proxy env is deliberately IGNORED for this call: the kernel is in-cluster
(NO_PROXY covers .svc), and urllib must not try the node squid for it.
"""
import io
import os
import sys
import tarfile
import urllib.request


def main() -> int:
    kernel = os.environ.get("CSERIES_KERNEL_URL", "").rstrip("/")
    app_id = os.environ.get("PRIVATAE_APP_ID", "")
    token = os.environ.get("APP_RUNTIME_SOURCE_TOKEN", "")
    target = os.environ.get("APP_SRC", "/app-src")
    if not kernel or not app_id or not token:
        print("fetch: CSERIES_KERNEL_URL, PRIVATAE_APP_ID and APP_RUNTIME_SOURCE_TOKEN "
              "are required", file=sys.stderr)
        return 2
    url = f"{kernel}/api/applications/{app_id}/source"
    req = urllib.request.Request(
        url, headers={"X-App-Runtime-Source-Token": token}, method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=60) as resp:
            data = resp.read()
    except Exception as e:  # noqa: BLE001 — the reason IS the output
        print(f"fetch: {url}: {e}", file=sys.stderr)
        return 1
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for m in tar.getmembers():
                # Refuse anything that would land outside the target: the
                # archive is the kernel's, but a path check costs nothing.
                if m.name.startswith("/") or ".." in m.name.split("/"):
                    print(f"fetch: refusing member {m.name!r}", file=sys.stderr)
                    return 1
                if m.issym() or m.islnk():
                    continue
            tar.extractall(target)
    except tarfile.TarError as e:
        print(f"fetch: bad archive from {url}: {e}", file=sys.stderr)
        return 1
    n = sum(len(fs) for _d, _ds, fs in os.walk(target))
    print(f"fetch: {app_id}: {len(data)} bytes, {n} files into {target}")
    bad = verify_attestation(target)
    if bad:
        for line in bad[:10]:
            print(f"fetch: attestation: {line}", file=sys.stderr)
        print(f"fetch: {app_id}: the fetched source does not match the platform's "
              f"build record ({len(bad)} problem(s)) — refusing to run it",
              file=sys.stderr)
        return 78  # EX_CONFIG: the pod's inputs are wrong, not the app
    return 0


def verify_attestation(target: str) -> list:
    """The platform's build record (attestation.json, written into the bundle
    at pack time) lists every packed file with its sha256. Check the fetched
    tree against it: every listed file must be present and byte-identical.
    Files the record does not list (a runtime cache the kernel preserved
    across a re-install, the record itself, app.yaml which the signature
    covers) are ignored — this proves the app's own files are the ones the
    platform packed, nothing more. No record → nothing to check (a custom app
    packed before records existed); the store bar requires one for anything
    it admits, so a store app always has one."""
    import hashlib
    import json
    path = os.path.join(target, "attestation.json")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "rb") as fh:
            att = json.loads(fh.read().decode("utf-8"))
        files = att.get("files") or {}
        if not isinstance(files, dict):
            return ["files map missing or malformed"]
    except (OSError, ValueError) as e:
        return [f"unreadable: {e}"]
    problems = []
    for rel, want in sorted(files.items()):
        if rel.startswith("/") or ".." in rel.split("/"):
            problems.append(f"{rel}: unsafe path in record")
            continue
        p = os.path.join(target, rel)
        if not os.path.isfile(p):
            problems.append(f"{rel}: listed in the record, absent from the source")
            continue
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != want:
            problems.append(f"{rel}: differs from the record")
    return problems


if __name__ == "__main__":
    sys.exit(main())
