# gate-scope: whole-tree — every system app's assets
"""Gate: installed apps' content-hashed build assets are cached; nothing else is.

Origin, 2026-09-23: every non-HTML file of an installed app was served with
`no-store`, so the Workbench's 3.2 MB main chunk (596 KB gzipped) was
re-downloaded every time its window opened. Vite already puts a content hash in
every file it emits, and the HTML that names those files stays no-store, so the
hashed files can be kept by the browser for good: a new build is a new URL.

What this pins (no pod, no network, no DB), against the real _serve_static:
  1. a Vite-hashed file under assets/ of a first-party app (provenance native
     or standard) -> private, max-age=31536000, immutable
  2. the same file in a HOST-BUILT (custom) or COMMUNITY app -> no-store.
     Host-built apps have no build step — the coding agent writes files by hand
     and a re-ship edits them in place — so a name never tracks its bytes;
     community bundles are third-party. (A first cut cached custom apps, and
     a fix to assets/app-settings.js would have
     stayed invisible for a year.)
  3. an app whose manifest cannot be read -> no-store (it is treated as
     community, the fail-closed reading proxy._app_meta already applies)
  4. index.html, an un-hashed file (a vanilla app's app.js, vendor/xterm.js),
     a hashed-looking name OUTSIDE assets/, and hyphenated plain names under
     assets/ that merely LOOK like <name>-<8 chars> (app-settings.js,
     main-controls.js, inter-variable.woff2, my-app-main.js) -> no-store
  5. every real Vite asset shipped in system-apps/*/assets matches the hashed
     pattern — so if a Vite upgrade changes its naming, this goes red instead
     of every app silently falling back to no-store.
  6. the manifest is parsed ONCE across many asset requests (an earlier release
     parsed app.yaml on every hashed-asset request: +32 ms each on one Host,
     first open ~100 ms slower), and a rewritten app.yaml takes effect on the
     very next request — same size too, as long as the mtime moved.

Run: APP_ROOT=<repo> python3 scripts/test_app_asset_caching.py
"""
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[asset-caching] ok   {label}")
    else:
        FAILED.append(label)
        print(f"[asset-caching] FAIL {label}" + (f" — {detail}" if detail else ""))


def make_app(root: Path, app_id: str, manifest: str | None) -> Path:
    d = root / app_id
    (d / "assets").mkdir(parents=True)
    (d / "vendor").mkdir()
    if manifest is not None:
        (d / "app.yaml").write_text(manifest)
    (d / "index.html").write_text('<script type="module" src="/x/assets/index-CF55QF1v.js"></script>')
    for f in ("assets/index-CF55QF1v.js", "assets/index-CK44mTno.css",
              "assets/mermaid-GHXKKRXX-BygFI7-B.js", "assets/index-3-JjGFKc.js",
              "assets/wasm-BnjxR4X6.js", "app.js", "vendor/xterm.js",
              "vendor/lib-CF55QF1v.js", "assets/logo.png", "assets/app-settings.js",
              "assets/main-controls.js", "assets/inter-variable.woff2", "assets/my-app-main.js"):
        (d / f).write_text("x")
    return d


def cache_header(install_dir: Path, path: str) -> str:
    from server.applications import proxy as P
    return P._serve_static(install_dir, path, install_dir.name).headers.get("cache-control", "")


def main() -> int:
    from server.applications import proxy as P

    immutable, no_store = "private, max-age=31536000, immutable", "no-store, max-age=0"
    root = Path(tempfile.mkdtemp())
    apps = {
        "standard": make_app(root, "wb", "id: wb\nprovenance: standard\n"),
        "native": make_app(root, "nat", "id: nat\nprovenance: native\n"),
        "custom": make_app(root, "mine", "id: mine\nprovenance: custom\n"),
        "community": make_app(root, "pub", "id: pub\nprovenance: community\n"),
        "unreadable": make_app(root, "broken", "id: [unclosed\n"),
    }

    hashed = ("assets/index-CF55QF1v.js", "assets/index-CK44mTno.css",
              "assets/mermaid-GHXKKRXX-BygFI7-B.js", "assets/index-3-JjGFKc.js",
              "assets/wasm-BnjxR4X6.js")
    for prov in ("standard", "native"):
        for p in hashed:
            got = cache_header(apps[prov], p)
            check(f"{prov}: {p} is immutable", got == immutable, got)
    for prov in ("custom", "community", "unreadable"):
        for p in hashed:
            got = cache_header(apps[prov], p)
            check(f"{prov}: {p} stays no-store", got == no_store, got)
    for p in ("index.html", "app.js", "vendor/xterm.js", "vendor/lib-CF55QF1v.js", "assets/logo.png",
              "assets/app-settings.js", "assets/main-controls.js", "assets/inter-variable.woff2",
              "assets/my-app-main.js"):
        got = cache_header(apps["standard"], p)
        check(f"standard: {p} stays no-store", got.startswith("no-store"), got)

    shipped = sorted((_ROOT / "system-apps").glob("*/assets/*"))
    shipped = [f for f in shipped if f.suffix in (".js", ".css", ".wasm", ".woff2", ".woff")]
    check("system-apps ship Vite assets to check (the pattern is not vacuous)",
          len(shipped) >= 4, f"found {len(shipped)}")
    for f in shipped:
        rel = f"assets/{f.name}"
        check(f"shipped {f.parent.parent.name}/{rel} matches the hashed pattern",
              bool(P._HASHED_ASSET_RE.match(rel)))

    # 6. parse once; a rewrite invalidates.
    import yaml
    real_load, loads = yaml.safe_load, []
    def counting(t):
        loads.append(1)
        return real_load(t)
    P.yaml.safe_load = counting
    try:
        cached = make_app(root, "cachecheck", "id: cachecheck\nprovenance: standard\n")
        for _ in range(20):
            cache_header(cached, "assets/index-CF55QF1v.js")
        check("20 hashed-asset requests parse app.yaml once", len(loads) == 1, f"{len(loads)} parses")
        (cached / "app.yaml").write_text("id: cachecheck\nprovenance: community\n")
        got = cache_header(cached, "assets/index-CF55QF1v.js")
        check("a manifest rewritten to community is no-store on the next request", got == no_store, got)
        st = (cached / "app.yaml").stat()
        (cached / "app.yaml").write_text("id: cachecheck\nprovenance: standard\n ")  # same size as above
        os.utime(cached / "app.yaml", ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        got = cache_header(cached, "assets/index-CF55QF1v.js")
        check("a same-size rewrite back to standard is immutable again (mtime moved)", got == immutable, got)
        # The dangerous direction with the mtime FORGED back: a metadata-preserving
        # copy (cp -p / tar / shutil.copy2) replacing the file with a same-size
        # community manifest and the old mtime. ctime/inode must still invalidate.
        import shutil
        before = (cached / "app.yaml").stat()
        src = root / "forged.yaml"
        src.write_text("id: cachecheck\nprovenance: community\n")  # same size as the current file
        os.utime(src, ns=(before.st_atime_ns, before.st_mtime_ns))
        shutil.copy2(src, cached / "app.yaml.new")
        os.replace(cached / "app.yaml.new", cached / "app.yaml")
        after = (cached / "app.yaml").stat()
        check("forged copy really kept mtime and size (the case is not vacuous)",
              (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size))
        got = cache_header(cached, "assets/index-CF55QF1v.js")
        check("a community manifest copied in with the OLD mtime and size is no-store at once",
              got == no_store, got)
    finally:
        P.yaml.safe_load = real_load

    if FAILED:
        print(f"[asset-caching] {len(FAILED)} FAILED")
        return 1
    print("[asset-caching] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
