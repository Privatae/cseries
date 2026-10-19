import { test } from "node:test";
import assert from "node:assert/strict";
import { installViewInbox, resetViewInboxForTests, onAppView, viewFromHash } from "../src/lib/app-view.ts";

test("viewFromHash reads the view the desktop put on the frame, decoded", () => {
  assert.equal(viewFromHash("#view=decision%3A12"), "decision:12");
  assert.equal(viewFromHash("#a=1&view=tickets"), "tickets");
  assert.equal(viewFromHash(""), null);
  assert.equal(viewFromHash("#view=%E0%A4%A"), null);
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
  assert.ok(src.indexOf("installViewInbox();") > -1, "installed");
  assert.ok(src.indexOf("installViewInbox();") < src.indexOf('import("./cards/cards")'), "before the cards load");
});
