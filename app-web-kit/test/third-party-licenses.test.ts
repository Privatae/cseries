import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import {
  bareName,
  cssBareImports,
  describePackages,
  packageDirOf,
  renderNotice,
} from "../third-party-licenses.mjs";

// The build's THIRD-PARTY-LICENSES.txt (app-web-kit/third-party-licenses.mjs):
// a bundled file maps to the package that owns it, each package's licence
// files are carried in full, and a package without one is named as such.

function fixture() {
  const root = mkdtempSync(path.join(tmpdir(), "tpl-"));
  const put = (rel: string, body: string) => {
    const p = path.join(root, rel);
    mkdirSync(path.dirname(p), { recursive: true });
    writeFileSync(p, body);
  };
  put("node_modules/a/package.json", JSON.stringify({ name: "a", version: "1.0.0", license: "MIT" }));
  put("node_modules/a/LICENSE", "MIT text for a\r\n");
  put("node_modules/a/license.js", "not a licence");
  // A nameless package.json inside dist/ must not stop the walk.
  put("node_modules/a/dist/esm/package.json", JSON.stringify({ type: "module" }));
  put("node_modules/a/dist/esm/index.js", "");
  put("node_modules/@s/b/package.json", JSON.stringify({ name: "@s/b", version: "2.0.0", license: { type: "ISC" } }));
  put("node_modules/@s/b/index.js", "");
  put("src/main.ts", "");
  return root;
}

test("a bundled file maps to the nearest named package, and our own source to none", () => {
  const root = fixture();
  try {
    assert.equal(packageDirOf(path.join(root, "node_modules/a/dist/esm/index.js")), path.join(root, "node_modules/a"));
    assert.equal(packageDirOf(path.join(root, "node_modules/@s/b/index.js")), path.join(root, "node_modules/@s/b"));
    assert.equal(packageDirOf(path.join(root, "src/main.ts")), null);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("licence texts are carried in full and a package without one is marked", () => {
  const root = fixture();
  try {
    const pkgs = describePackages(new Set([path.join(root, "node_modules/@s/b"), path.join(root, "node_modules/a")]));
    assert.deepEqual(pkgs.map((p) => `${p.name}@${p.version} ${p.licence} ${p.files.map((f) => f.name)}`), [
      "@s/b@2.0.0 ISC ",
      "a@1.0.0 MIT LICENSE",
    ]);
    const text = renderNotice("x", pkgs);
    assert.match(text, /2 packages, 1 without a licence file\./);
    assert.match(text, /--- LICENSE ---\n\nMIT text for a\n/);
    assert.match(text, /@s\/b@2\.0\.0\s+ISC\s+\(no licence file in package\)/);
    assert.doesNotMatch(text, /not a licence/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("stylesheet @imports of a package name are found; relative ones are not", () => {
  const css = '/* @import "commented"; */\n@import "tailwindcss";\n@import url("@scope/pkg/x.css");\n@import "./local.css";\n@import "https://x/y.css";';
  assert.deepEqual(cssBareImports(css), ["tailwindcss", "@scope/pkg"]);
  assert.equal(bareName("../x"), null);
});
