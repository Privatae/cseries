import { test } from "node:test";
import assert from "node:assert/strict";
import {
  clampHeight,
  datedName,
  fileNameFor,
  frameDoc,
  isOwnHostTurn,
  MAX_FRAME_H,
  MIN_FRAME_H,
  nameFor,
  SIZE_SCRIPT,
  tokenStyleBlock,
  type Sandbox,
} from "../../app-web-kit/visuals/visual.ts";

test("own-Host gate: the mark decides, never the author fields; unmarked fails closed", () => {
  // A self-thread reply as the server sends it (routes/circles.py
  // _normalize_turn_rows: author_kind "entity", author_name = the Host's
  // name) and as the self thread now maps it.
  assert.equal(isOwnHostTurn({ role: "assistant", ownHost: true }), true);
  // The same row unmarked — a Peers thread's host message has this exact
  // shape, so nothing about it may run.
  assert.equal(isOwnHostTurn({ role: "assistant" }), false);
  assert.equal(isOwnHostTurn({ role: "assistant", ownHost: false }), false);
  // The user's own words never run, marked or not.
  assert.equal(isOwnHostTurn({ role: "user", ownHost: true }), false);
});

// The real policy's shape (dashboard/base/js/sandbox-csp.js withSandboxCsp):
// the meta goes after a leading doctype, else at index 0, then `extra`.
const META = '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'">';
const sandbox: Sandbox = {
  withSandboxCsp(html, extra) {
    const m = /^\s*<!DOCTYPE[^>]*>/i.exec(html);
    const at = m ? m[0].length : 0;
    return html.slice(0, at) + META + (extra || "") + html.slice(at);
  },
  externalRefs(html) {
    return /src="https?:\/\//.test(html) ? ["https://cdn.example/x.js"] : [];
  },
};

test("frameDoc fails CLOSED with no sandbox policy loaded", () => {
  assert.equal(frameDoc("<p>hi</p>", undefined, "x"), null);
});

test("frameDoc: CSP first, then the desktop style and size reporter, then the page", () => {
  const page = '<!DOCTYPE html><html><head><style>body{color:red}</style></head><body>x</body></html>';
  const out = frameDoc(page, sandbox, ":root{--cseries-ink:#000}");
  assert.ok(out);
  const d = out.doc;
  assert.ok(d.startsWith("<!DOCTYPE html>" + META), "policy right after the doctype");
  const style = d.indexOf("data-cseries-artifact-style");
  const size = d.indexOf("data-visual-size");
  const own = d.indexOf("body{color:red}");
  assert.ok(META.length < style && style < size && size < own, "page's own rules come last and win");
  assert.deepEqual(out.blocked, []);
});

test("frameDoc: a fragment with no doctype is sealed at index 0", () => {
  const out = frameDoc('<svg viewBox="0 0 10 10"></svg>', sandbox, "");
  assert.ok(out && out.doc.startsWith(META + SIZE_SCRIPT));
});

test("frameDoc: a desktop design keeps its own tokens (no style block)", () => {
  const page = '<meta name="cseries-desktop" content="html"><body></body>';
  const out = frameDoc(page, sandbox, ":root{--cseries-ink:#000}");
  assert.ok(out && !out.doc.includes("data-cseries-artifact-style"));
});

test("frameDoc names what the policy will block", () => {
  const out = frameDoc('<script src="https://cdn.example/x.js"></script>', sandbox, "");
  assert.deepEqual(out?.blocked, ["https://cdn.example/x.js"]);
});

test("tokenStyleBlock: drops @font-face, defuses </style>, empty sheet adds nothing", () => {
  assert.equal(tokenStyleBlock(""), "");
  assert.equal(tokenStyleBlock("   "), "");
  const b = tokenStyleBlock("@font-face{font-family:Inter;src:url(/fonts/i.woff2)}:root{--a:1}</STYLE>");
  assert.ok(!b.includes("@font-face"));
  assert.ok(b.includes(":root{--a:1}"));
  assert.ok(!/<\/style>[\s\S]*<\/style>/i.test(b), "only the block's own closing tag");
  assert.ok(b.includes("<\\/STYLE>"));
});

test("clampHeight bounds the frame and ignores junk", () => {
  assert.equal(clampHeight(10), MIN_FRAME_H);
  assert.equal(clampHeight(300.2), 301);
  assert.equal(clampHeight(99999), MAX_FRAME_H);
  for (const bad of [NaN, Infinity, "300", null, undefined, {}]) assert.equal(clampHeight(bad), null);
});

// server/applications/artifacts.py _NAME_RE — what the save must satisfy.
const SERVER_NAME_RE = /^[\p{L}\p{N}_ .,()\-–—'’"“”:;&!?+]{1,120}$/u;

test("nameFor: fence title wins, then <title>, cleaned to what the server accepts", () => {
  assert.equal(nameFor("<title>Page</title>", 'title="Orbit demo"'), "Orbit demo");
  assert.equal(nameFor("<html><title>Sales / Q3 » plan</title>"), "Sales Q3 plan");
  assert.equal(nameFor("<p>no title</p>"), null);
  assert.equal(nameFor("<title>///</title>"), null);
  const long = nameFor(`<title>${"a".repeat(300)}</title>`);
  assert.equal(long?.length, 120);
  for (const n of ["Orbit demo", "Sales Q3 plan", long]) assert.ok(SERVER_NAME_RE.test(n!), n!);
});

test("datedName and fileNameFor", () => {
  const n = datedName(new Date(2026, 9, 3, 7, 5));
  assert.equal(n, "From chat 2026-10-03 07:05");
  assert.ok(SERVER_NAME_RE.test(n));
  assert.equal(fileNameFor("Orbit demo!"), "orbit-demo.html");
  assert.equal(fileNameFor("///"), "page.html");
});
