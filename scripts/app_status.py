#!/usr/bin/env python3
"""
app status — the 3-state view for any app: SHIPPED / PUBLISHED / INSTALLED, plus
DRIFT between the shipped bundle and the installed copy. The legibility tool from
docs/app-architecture.md §1b: look in ONE place and see exactly where an app is.

Runs INSIDE an entity pod (reads that entity's own image bundles + its installed
registry), so it works on any image version:
    PYTHONPATH=/app python3 /tmp/app_status.py [app_id]

States:
  SHIPPED   = a signed bundle baked in the image (/app/native-bundles + /app/factory-bundles)
  PUBLISHED = a row in the central store (best-effort via the entity's store endpoint)
  INSTALLED = an extracted app under /var/lib/cseries/apps + its registry row
Drift catches: installed-but-no-longer-shipped (orphan), operator_only flag not applied,
version mismatch between shipped and installed.
"""
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("PyYAML not available in this pod", file=sys.stderr)
    sys.exit(2)

NATIVE_DIR = Path("/app/native-bundles")
FACTORY_DIR = Path("/app/factory-bundles")
INSTALL_ROOT = Path("/var/lib/cseries/apps")


def _operator_only(manifest: dict) -> bool:
    # operator_only is canonical; platform is the legacy alias (read both).
    return bool(manifest.get("operator_only") or manifest.get("platform"))


def _peek_bundle(cseries_path: Path):
    try:
        with zipfile.ZipFile(cseries_path) as z:
            for n in ("manifest.yaml", "app.yaml"):
                if n in z.namelist():
                    return yaml.safe_load(z.read(n).decode())
    except Exception:
        return None
    return None


def shipped() -> dict:
    out = {}
    for d in (NATIVE_DIR, FACTORY_DIR):
        if not d.exists():
            continue
        for f in sorted(d.glob("*.cseries")):
            m = _peek_bundle(f)
            if m and m.get("id"):
                out.setdefault(m["id"], {
                    "version": m.get("version"),
                    "operator_only": _operator_only(m),
                    "where": d.name,
                })
    return out


def installed() -> dict:
    out = {}
    if not INSTALL_ROOT.exists():
        return out
    for d in sorted(INSTALL_ROOT.iterdir()):
        ay = d / "app.yaml"
        if d.is_dir() and ay.exists():
            try:
                m = yaml.safe_load(ay.read_text())
            except Exception:
                continue
            if m and m.get("id"):
                out[m["id"]] = {
                    "version": m.get("version"),
                    "operator_only": _operator_only(m),
                }
    return out


def published():
    """Best-effort store query via the entity's own store endpoint. Returns None
    if the store can't be reached (shown as '?' rather than a false '—')."""
    sec = os.environ.get("INTERNAL_API_SECRET", "")
    if not sec:
        return None
    for path in ("/api/applications/store/catalog",):
        try:
            r = subprocess.run(
                ["curl", "-sf", "-H", f"x-cseries-internal-auth: {sec}",
                 f"http://[::1]:8800{path}"],
                capture_output=True, timeout=10,
            )
            if r.returncode != 0 or not r.stdout:
                continue
            data = json.loads(r.stdout)
            apps = data if isinstance(data, list) else (data.get("apps") or data.get("applications") or [])
            return {
                (a.get("app_id") or a.get("id")): {
                    "version": a.get("latest_version") or a.get("version"),
                    "audience": a.get("audience"),
                }
                for a in apps if (a.get("app_id") or a.get("id"))
            }
        except Exception:
            continue
    return None


def main():
    want = sys.argv[1] if len(sys.argv) > 1 else None
    sh, ins, pub = shipped(), installed(), published()
    ids = sorted(set(sh) | set(ins) | set(pub or {}))
    if want:
        ids = [want]

    def s_cell(d):
        return f"v{d['version']}{' [op]' if d['operator_only'] else ''}" if d else "—"

    print(f"{'APP':22} {'SHIPPED':16} {'PUBLISHED':12} {'INSTALLED':16} DRIFT")
    print("-" * 96)
    drift_count = 0
    for app in ids:
        s, i = sh.get(app), ins.get(app)
        p = (pub or {}).get(app)
        p_cell = (f"v{p.get('version')}" if p else ("—" if pub is not None else "?"))
        drift = []
        # An installed app is legitimately SOURCED if it's shipped (native image
        # bundle) OR published (in the store). Only installed-AND-neither is a true
        # orphan — and we can only say so when the store query succeeded.
        if i and not s and not p:
            if pub is not None:
                drift.append("ORPHAN: installed, not shipped, not in store")
            else:
                drift.append("not shipped (store unknown)")
        if i and not s and p and i.get("version") != p.get("version"):
            drift.append(f"store update: inst v{i['version']} vs store v{p.get('version')}")
        if s and i and s.get("operator_only") != i.get("operator_only"):
            drift.append(f"operator_only ship={s['operator_only']} inst={i['operator_only']}")
        if s and i and s.get("version") != i.get("version"):
            drift.append(f"version ship={s['version']} inst={i['version']}")
        if drift:
            drift_count += 1
        print(f"{app:22} {s_cell(s):16} {p_cell:12} {s_cell(i):16} {'; '.join(drift)}")
    store_note = "" if pub is not None else "  (PUBLISHED unavailable — store endpoint unreachable from pod)"
    print("-" * 96)
    print(f"{len(ids)} apps; {drift_count} with drift.{store_note}")


if __name__ == "__main__":
    main()
