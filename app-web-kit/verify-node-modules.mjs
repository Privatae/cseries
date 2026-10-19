// Refuse to build against a node_modules that is not the one package-lock.json
// describes. A signed app bundle is what every Host reinstalls, so it must be
// built from pinned third-party code: a node_modules left over from another
// branch (or `npm install` that quietly moved a version) builds different
// content-hashed chunks and ships code nobody reviewed.
//
// Usage (from an app's web dir; every <app>-web `ship` script runs it first):
//   node ../app-web-kit/verify-node-modules.mjs
//
// It compares npm's own record of what is installed (node_modules/
// .package-lock.json, written by npm on every install) with the committed
// package-lock.json: every installed package must be in the lockfile at the
// same version, integrity and resolved URL, and every lockfile package that
// npm would install must be installed. On any difference it exits 1 saying
// "run npm ci".

import { readFile } from "node:fs/promises";

const CWD = new URL(`file://${process.cwd()}/`);

async function readJson(rel) {
  try {
    return JSON.parse(await readFile(new URL(rel, CWD), "utf8"));
  } catch (e) {
    return { __error: e.code === "ENOENT" ? "missing" : String(e.message || e) };
  }
}

export function compare(lock, installed) {
  const problems = [];
  const want = lock.packages || {};
  const have = installed.packages || {};
  for (const [key, h] of Object.entries(have)) {
    if (!key.startsWith("node_modules/")) continue;
    const w = want[key];
    if (!w) {
      problems.push(`${key}@${h.version} is installed but not in package-lock.json`);
      continue;
    }
    if (w.version !== h.version) {
      problems.push(`${key}: installed ${h.version}, lockfile ${w.version}`);
    } else if ((w.integrity || "") !== (h.integrity || "")) {
      problems.push(`${key}@${h.version}: integrity differs from the lockfile`);
    } else if ((w.resolved || "") !== (h.resolved || "")) {
      problems.push(`${key}@${h.version}: resolved URL differs from the lockfile`);
    }
  }
  for (const [key, w] of Object.entries(want)) {
    if (!key.startsWith("node_modules/")) continue;
    if (w.optional || w.devOptional) continue;       // npm skips what the platform cannot use
    if (w.link) continue;
    if (!have[key]) problems.push(`${key}@${w.version} is in the lockfile but not installed`);
  }
  return problems;
}

async function main() {
  const lock = await readJson("package-lock.json");
  if (lock.__error) {
    console.error(`verify-node-modules: package-lock.json ${lock.__error} — run npm ci`);
    return 1;
  }
  const installed = await readJson("node_modules/.package-lock.json");
  if (installed.__error) {
    console.error(
      `verify-node-modules: node_modules/.package-lock.json ${installed.__error} — ` +
        "node_modules was not installed by npm from the lockfile. Run npm ci.",
    );
    return 1;
  }
  const problems = compare(lock, installed);
  if (problems.length) {
    console.error(
      `verify-node-modules: node_modules does not match package-lock.json ` +
        `(${problems.length} difference(s)). Run npm ci, then ship again:`,
    );
    for (const p of problems.slice(0, 12)) console.error("  " + p);
    if (problems.length > 12) console.error(`  … and ${problems.length - 12} more`);
    return 1;
  }
  console.log(
    `verify-node-modules: node_modules matches package-lock.json ` +
      `(${Object.keys(installed.packages || {}).length} packages)`,
  );
  return 0;
}

if (import.meta.url === new URL(process.argv[1], "file://" + process.cwd() + "/").href) {
  process.exit(await main());
}
