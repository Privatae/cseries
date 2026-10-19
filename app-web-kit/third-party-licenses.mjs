// Write assets/THIRD-PARTY-LICENSES.txt into every build: each third-party
// npm package compiled into the app's JavaScript and CSS, with its version,
// its declared licence and the full text of its LICENSE / LICENCE / COPYING /
// NOTICE files. Most of these licences (MIT, ISC, BSD, Apache-2.0) require
// their notice to travel with every copy, and a built bundle is a copy.
//
// It runs inside the build, so the list is the bundle's own module list and
// cannot drift from what ships. It only ADDS an asset: it runs after the
// chunks are rendered and changes no chunk, so the JS and CSS hashes are the
// same with or without it.
//
// Usage (each <app>-web/vite.config.ts):
//   import thirdPartyLicenses from "../app-web-kit/third-party-licenses.mjs";
//   plugins: [react(), tailwindcss(), thirdPartyLicenses()],
//
// What counts as a third-party package:
//   - every module in an output chunk whose file sits under node_modules/,
//     mapped to the nearest package.json that has a name;
//   - the files an output asset was made from (fonts, wasm) under node_modules/;
//   - the bundler's own helpers injected as virtual modules (\0vite/...,
//     \0rolldown/...), which are Vite's and Rolldown's code;
//   - packages a bundled stylesheet pulls in with `@import "<package>"`
//     (tailwindcss, tw-animate-css): Tailwind inlines those itself, so they
//     never appear as modules.
// The app's own src/ and this kit are not listed: they are this project's
// code, under its own licence.

import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";

const FILE_NAME = "THIRD-PARTY-LICENSES.txt";

// LICENSE, LICENCE, COPYING, NOTICE with any suffix (LICENSE.md, LICENSE-MIT,
// COPYING.LESSER), plus the <kind>-LICENSE form (MIT-LICENSE.txt). Source
// files that happen to share the name are not licence texts.
const LICENCE_FILE_RE = /^(?:licen[cs]e|copying|notice)(?:[-._].*)?$|^[a-z0-9]+[-_]licen[cs]e(?:\.(?:md|txt))?$/i;
const NOT_TEXT_RE = /\.(?:[cm]?js|ts|json|map)$/i;

// Virtual modules the bundler injects: their code is the named package's.
const VIRTUAL_OWNERS = [
  ["\0vite/", "vite"],
  ["\0rolldown/", "rolldown"],
];

const NM = `${path.sep}node_modules${path.sep}`;

/** A module id without its ?query or #hash ("x.css?inline" -> "x.css"). */
function cleanId(id) {
  const q = id.search(/[?#]/);
  return q === -1 ? id : id.slice(0, q);
}

function readJson(file) {
  try {
    return JSON.parse(readFileSync(file, "utf8"));
  } catch {
    return null;
  }
}

/**
 * The directory of the package that owns `file`, or null if the file is not
 * under node_modules/. Walks up to the nearest package.json that has a name
 * (dist/esm/package.json files that only say {"type":"module"} are skipped),
 * never above the last node_modules/ segment.
 */
export function packageDirOf(file) {
  const at = file.lastIndexOf(NM);
  if (at === -1) return null;
  const floor = file.slice(0, at + NM.length - 1);
  let dir = path.dirname(file);
  while (dir.length > floor.length && dir.startsWith(floor)) {
    const pj = readJson(path.join(dir, "package.json"));
    if (pj && typeof pj.name === "string" && pj.name) return dir;
    const up = path.dirname(dir);
    if (up === dir) break;
    dir = up;
  }
  return null;
}

/** node_modules/<name> found from `fromDir` upward, the way Node resolves it. */
export function findPackageDir(fromDir, name) {
  let dir = fromDir;
  for (;;) {
    const cand = path.join(dir, "node_modules", ...name.split("/"));
    if (existsSync(path.join(cand, "package.json"))) return cand;
    const up = path.dirname(dir);
    if (up === dir) return null;
    dir = up;
  }
}

/** The package name of a bare specifier ("@scope/pkg/x.css" -> "@scope/pkg"). */
export function bareName(spec) {
  if (/^(?:[./]|[a-z][a-z0-9+.-]*:)/i.test(spec)) return null; // relative, absolute, url
  const parts = spec.split("/");
  if (spec.startsWith("@")) return parts.length >= 2 ? `${parts[0]}/${parts[1]}` : null;
  return parts[0] || null;
}

/** Bare `@import "x"` / `@import url("x")` specifiers in a stylesheet. */
export function cssBareImports(css) {
  const out = [];
  const noComments = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const re = /@import\s+(?:url\(\s*)?["']([^"']+)["']/g;
  for (let m; (m = re.exec(noComments)); ) {
    const name = bareName(m[1]);
    if (name) out.push(name);
  }
  return out;
}

function declaredLicence(pj) {
  const l = pj.license ?? pj.licence;
  if (typeof l === "string" && l.trim()) return l.trim();
  if (l && typeof l === "object" && typeof l.type === "string") return l.type;
  const ls = pj.licenses ?? pj.licences;
  if (Array.isArray(ls) && ls.length) {
    const types = ls.map((x) => (typeof x === "string" ? x : x && x.type)).filter(Boolean);
    if (types.length) return types.length === 1 ? types[0] : `(${types.join(" OR ")})`;
  }
  return "not declared";
}

function licenceFiles(dir) {
  let names;
  try {
    names = readdirSync(dir);
  } catch {
    return [];
  }
  return names
    .filter((n) => LICENCE_FILE_RE.test(n) && !NOT_TEXT_RE.test(n))
    .filter((n) => {
      try {
        return statSync(path.join(dir, n)).isFile();
      } catch {
        return false;
      }
    })
    .sort()
    .map((n) => ({
      name: n,
      text: readFileSync(path.join(dir, n), "utf8").replace(/\r\n?/g, "\n").replace(/\s+$/, ""),
    }));
}

/**
 * One record per name@version from a set of package directories, sorted by
 * name then version. A package installed at two paths with the same version
 * is listed once.
 */
export function describePackages(dirs) {
  const byKey = new Map();
  for (const dir of [...dirs].sort()) {
    const pj = readJson(path.join(dir, "package.json"));
    if (!pj || !pj.name) continue;
    const version = String(pj.version ?? "unversioned");
    const key = `${pj.name}@${version}`;
    if (byKey.has(key)) continue;
    byKey.set(key, {
      name: pj.name,
      version,
      licence: declaredLicence(pj),
      files: licenceFiles(dir),
    });
  }
  return [...byKey.values()].sort((a, b) =>
    a.name === b.name ? (a.version < b.version ? -1 : 1) : a.name < b.name ? -1 : 1,
  );
}

/** The text of THIRD-PARTY-LICENSES.txt for `packages` (describePackages). */
export function renderNotice(appName, packages) {
  const rule = "=".repeat(78);
  const thin = "-".repeat(78);
  const missing = packages.filter((p) => p.files.length === 0);
  const lines = [
    `Third-party software in ${appName}`,
    rule,
    "",
    "The JavaScript and CSS in this folder include the third-party npm packages",
    "listed below. Each entry gives the package version, the licence the package",
    "declares, and the full text of the licence and notice files it ships with.",
    "A package that ships no such file is marked \"no licence file in package\".",
    "This file is generated by the build (app-web-kit/third-party-licenses.mjs).",
    "",
    `${packages.length} packages, ${missing.length} without a licence file.`,
    "",
    "Summary",
    thin,
  ];
  const w = Math.max(0, ...packages.map((p) => `${p.name}@${p.version}`.length));
  for (const p of packages) {
    const id = `${p.name}@${p.version}`;
    lines.push(`${id.padEnd(w)}  ${p.licence}${p.files.length ? "" : "  (no licence file in package)"}`);
  }
  for (const p of packages) {
    lines.push("", "", rule, `${p.name}@${p.version}`, `Licence: ${p.licence}`, rule);
    if (!p.files.length) {
      lines.push("", `Declared licence: ${p.licence}. No licence file in package.`);
      continue;
    }
    for (const f of p.files) {
      lines.push("", `--- ${f.name} ---`, "", f.text);
    }
  }
  return lines.join("\n") + "\n";
}

export default function thirdPartyLicenses() {
  let root = process.cwd();
  let assetsDir = "assets";
  return {
    name: "third-party-licenses",
    apply: "build",
    configResolved(config) {
      root = config.root;
      assetsDir = config.build.assetsDir || "assets";
    },
    generateBundle(_options, bundle) {
      const dirs = new Set();
      const add = (dir) => dir && dirs.add(dir);
      const cssFiles = [];
      const unknownVirtual = new Set();

      const fromFile = (raw) => {
        const id = cleanId(raw);
        for (const [prefix, owner] of VIRTUAL_OWNERS) {
          if (id.startsWith(prefix)) return add(findPackageDir(root, owner));
        }
        if (id.startsWith("\0")) return unknownVirtual.add(id);
        const file = path.resolve(root, id);
        if (/\.css$/i.test(file)) cssFiles.push(file);
        add(packageDirOf(file));
      };

      for (const out of Object.values(bundle)) {
        if (out.type === "chunk") {
          for (const id of out.moduleIds ?? Object.keys(out.modules ?? {})) fromFile(id);
        } else {
          for (const f of out.originalFileNames ?? []) fromFile(f);
        }
      }

      // Packages a bundled stylesheet @imports by name, and what THEIR
      // stylesheets @import in turn.
      const seenCss = new Set();
      while (cssFiles.length) {
        const file = cssFiles.pop();
        if (seenCss.has(file)) continue;
        seenCss.add(file);
        let css;
        try {
          css = readFileSync(file, "utf8");
        } catch {
          continue;
        }
        for (const name of cssBareImports(css)) {
          const dir = findPackageDir(path.dirname(file), name);
          if (!dir) {
            this.warn(`${FILE_NAME}: cannot find package "${name}" imported by ${file}`);
            continue;
          }
          add(dir);
          const pj = readJson(path.join(dir, "package.json")) || {};
          const entry = pj.style || (typeof pj.exports?.["."]?.style === "string" && pj.exports["."].style) || "index.css";
          const cand = path.join(dir, entry);
          if (existsSync(cand)) cssFiles.push(cand);
        }
      }

      for (const id of unknownVirtual) {
        this.warn(`${FILE_NAME}: virtual module ${JSON.stringify(id)} has no known owner; not listed`);
      }

      const own = readJson(path.join(root, "package.json")) || {};
      const appName = own.name ? `${own.name} (built web app)` : "this web app";
      this.emitFile({
        type: "asset",
        fileName: path.posix.join(assetsDir, FILE_NAME),
        source: renderNotice(appName, describePackages(dirs)),
      });
    },
  };
}
