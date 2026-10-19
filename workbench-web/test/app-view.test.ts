import { test } from "node:test";
import assert from "node:assert/strict";
import { installViewInbox, resetViewInboxForTests, onAppView, projectOfView, viewFromHash } from "../src/lib/app-view.ts";

test("viewFromHash reads the view the desktop put on the frame, decoded", () => {
  assert.equal(viewFromHash("#view=project%3A7"), "project:7");
  assert.equal(viewFromHash("#x=1&view=project:12"), "project:12");
  assert.equal(viewFromHash(""), null);
  assert.equal(viewFromHash("#view="), null);
  assert.equal(viewFromHash("#view=%E0%A4%A"), null); // malformed escape
  assert.equal(viewFromHash(undefined), null);
});

test("projectOfView names a project only for project:<digits>", () => {
  assert.equal(projectOfView("project:7"), 7);
  assert.equal(projectOfView("project:007"), 7);
  for (const bad of ["project:", "project:x", "project:7a", "projects:7", "finding:7", "", null, undefined]) {
    assert.equal(projectOfView(bad as string | null | undefined), null, String(bad));
  }
});

test("only the framing desktop's views are taken, and unsubscribe stops delivery", () => {
  resetViewInboxForTests();
  const listeners: ((ev: unknown) => void)[] = [];
  const parent = {};
  const g = globalThis as unknown as { window: unknown };
  const prior = g.window;
  g.window = { parent, addEventListener: (_t: string, f: (ev: unknown) => void) => listeners.push(f) };
  try {
    const got: string[] = [];
    const stop = onAppView((v) => got.push(v));
    assert.equal(listeners.length, 1, "one listener, installed once");
    listeners[0]({ source: parent, data: { type: "cseries:app-view", view: "a:1" } });
    listeners[0]({ source: {}, data: { type: "cseries:app-view", view: "a:2" } });
    listeners[0]({ source: parent, data: { type: "other", view: "a:3" } });
    listeners[0]({ source: parent, data: { type: "cseries:app-view", view: 4 } });
    assert.deepEqual(got, ["a:1"]);
    stop();
    listeners[0]({ source: parent, data: { type: "cseries:app-view", view: "a:5" } });
    assert.deepEqual(got, ["a:1"], "nothing after unsubscribe");
  } finally {
    g.window = prior;
  }
});

test("a view posted BEFORE anyone subscribed is held and delivered on subscribe", () => {
  // The desktop posts on the frame's load; React's effects run after. The
  // inbox is installed first (main.tsx), so the early view is not lost.
  const listeners: ((ev: unknown) => void)[] = [];
  const parent = {};
  const g = globalThis as unknown as { window: unknown };
  const prior = g.window;
  g.window = {
    parent,
    addEventListener: (_t: string, f: (ev: unknown) => void) => listeners.push(f),
    removeEventListener: () => {},
  };
  try {
    resetViewInboxForTests();
    installViewInbox();
    const inbox = listeners[listeners.length - 1];
    inbox({ source: parent, data: { type: "cseries:app-view", view: "held:1" } });
    const got: string[] = [];
    const stop = onAppView((v) => got.push(v));
    assert.deepEqual(got, ["held:1"], "the early view reaches the first subscriber");
    inbox({ source: parent, data: { type: "cseries:app-view", view: "live:2" } });
    assert.deepEqual(got, ["held:1", "live:2"]);
    stop();
  } finally {
    g.window = prior;
  }
});

test("main.tsx installs the inbox before the first render", async () => {
  const fs = await import("node:fs");
  const src = fs.readFileSync(new URL("../src/main.tsx", import.meta.url), "utf8");
  const book = fs.readFileSync(new URL("../src/book/book.tsx", import.meta.url), "utf8");
  assert.ok(src.indexOf("installViewInbox();") > -1, "installed");
  // The first render is mountBook's createRoot (src/book/book.tsx).
  assert.ok(book.indexOf("createRoot(") > -1 && src.indexOf("mountBook(root)") > -1, "the book renders");
  assert.ok(src.indexOf("installViewInbox();") < src.indexOf("mountBook(root)"), "before render");
  assert.equal(src.indexOf("createRoot("), -1, "main.tsx renders nothing before it");
});
