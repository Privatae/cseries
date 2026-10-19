// Place a built React bundle into the system-app dir the kernel static
// server serves (server/applications/proxy.py: _serve_static) and that
// scripts/rebuild_factory_bundles.sh packs into the signed <app>.cseries.
// The React SOURCE lives at the repo root (<app>-web/) — it must NOT live
// inside the app dir, because bundles.pack() recursively includes everything
// there (node_modules would otherwise be packed into the bundle).
//
// Usage (from the app's web dir, via npm run ship):
//   node ../app-web-kit/place.mjs <app-id>
// Layout after ship:
//   system-apps/<app-id>/index.html    <- React shell
//   system-apps/<app-id>/assets/*      <- hashed JS/CSS chunks

import { access, cp, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";

const appId = process.argv[2];
if (!appId || !/^[a-z0-9-]+$/.test(appId)) {
  console.error("usage: node place.mjs <app-id>");
  process.exit(1);
}

const CWD = new URL(`file://${process.cwd()}/`);
// The pod-side secret-shape rules the composers' scan imports at build time
// (circles-web and workbench-web src/lib/secret-shapes.ts).
const EXTRA_INPUTS = {
  circles: ["brain/mastercode/secret_shapes.json"],
  workbench: ["brain/mastercode/secret_shapes.json"],
};
const DIST = new URL("dist/", CWD);
const APP = new URL(`../system-apps/${appId}/`, CWD);

const exists = async (url) => {
  try {
    await access(url);
    return true;
  } catch {
    return false;
  }
};

async function main() {
  if (process.argv.includes("--print-rev")) {
    // The stamp this tree WOULD get, without building or placing anything
    // (scripts/test_web_stamp.py compares it with the Python side).
    console.log(await srcRev());
    return;
  }
  if (!(await exists(new URL("index.html", DIST)))) {
    throw new Error("dist/index.html not found — run `npm run build` first");
  }
  if (!(await exists(new URL("app.yaml", APP)))) {
    throw new Error(`no app.yaml at system-apps/${appId}/ — wrong app id?`);
  }

  // Replace assets wholesale (hashed names, so stale chunks must go).
  await rm(new URL("assets", APP), { recursive: true, force: true });
  await cp(new URL("index.html", DIST), new URL("index.html", APP));
  await cp(new URL("assets", DIST), new URL("assets", APP), { recursive: true });

  // Web-source freshness stamp (2026-07-23): hash the SOURCE this build came
  // from into the app dir. scripts/test_bundle_freshness.py recomputes the
  // same hash pre-ship — src edits without an `npm run ship` go RED instead
  // of silently shipping the old bundle (the miss that once shipped week-old
  // chat UI over a rebuilt backend). Spec (must match the
  // Python side): sha256 over sorted-relpath + NUL + bytes + NUL for every
  // file under src/ plus package-lock.json.
  await writeFile(new URL(".web-src-rev", APP), (await srcRev()) + "\n");

  console.log("placed React build into", APP.pathname);
}

// Ported from the per-app scripts this file replaces (circles-web/scripts and
// workbench-web/scripts, both at +35 lines on 2026-07-23). ONE DELIBERATE
// CHANGE: those hashed `WEB`, derived from `import.meta.url` — correct there,
// because the script sat at <app>-web/scripts/. This file lives in
// app-web-kit/ and is invoked as `node ../app-web-kit/place.mjs <app-id>` FROM
// the app's web dir, so import.meta.url points at app-web-kit/ and would hash
// a src/ that does not exist. CWD is the app's web dir and is the correct root
// here; it is also what DIST already uses two lines up.
async function srcRev() {
  const files = [];
  async function walk(dir, rel) {
    for (const ent of (await readdir(dir, { withFileTypes: true })).sort((a, b) =>
      a.name.localeCompare(b.name),
    )) {
      if (ent.name === "node_modules" || ent.name === "dist") continue;
      const u = new URL(ent.name + (ent.isDirectory() ? "/" : ""), dir);
      if (ent.isDirectory()) await walk(u, `${rel}${ent.name}/`);
      else files.push([`${rel}${ent.name}`, u]);
    }
  }
  await walk(new URL("src/", CWD), "src/");
  // THE SHARED KIT IS IN THE HASH. app-web-kit/ compiles into this app via the
  // @kit alias but lives at the repo root, inside no app's src/ — so hashing
  // src/ alone let an edit to the kit change all four apps' output with no
  // stamp moving and test_bundle_freshness.py staying green. That is the
  // failure this stamp exists to stop, and the extraction that
  // created app-web-kit/ is what reopened it. The `../app-web-kit/` prefix
  // cannot collide with a src/ path, and the Python side walks the identical
  // set with the identical prefix — they are load-bearing on each other, and
  // any disagreement fails the gate rather than passing it.
  await walk(new URL("../app-web-kit/", CWD), "../app-web-kit/");
  // Repo files outside src/ and the kit that this app's build READS. They are in
  // the stamp for the same reason the kit is: an edit to one changes the built
  // output while no src/ byte moves. scripts/test_bundle_freshness.py keeps the
  // identical table (WEB_EXTRA_INPUTS) and keys: "../" + the repo-relative path.
  for (const rel of EXTRA_INPUTS[appId] || []) {
    files.push([`../${rel}`, new URL(`../${rel}`, CWD)]);
  }
  files.push(["package-lock.json", new URL("package-lock.json", CWD)]);
  files.sort((a, b) => (a[0] < b[0] ? -1 : 1));
  const h = createHash("sha256");
  for (const [rel, u] of files) {
    h.update(rel);
    h.update("\0");
    h.update(await readFile(u));
    h.update("\0");
  }
  return h.digest("hex");
}

main().catch((e) => {
  console.error(e.message);
  process.exit(1);
});
