import { test } from "node:test";
import assert from "node:assert/strict";
import { artifactIdOf, artifactsOfTurn } from "../../app-web-kit/visuals/artifact-ref.ts";
import type { ToolInvocation } from "../src/lib/types.ts";

const t = (o: Partial<ToolInvocation>): ToolInvocation => ({
  id: Math.random().toString(36),
  name: "artifact",
  args: {},
  summary: "",
  state: "output-available",
  ...o,
});

test("live create: id from the result envelope", () => {
  const tool = t({
    args: { op: "create", name: "Orbit", files: [] },
    result: { status: "ok", result: { artifact_id: 41, preview_url: "/api/artifacts/41/render" } },
  });
  assert.equal(artifactIdOf(tool), 41);
});

test("live write through operate: id from the inner args before the result lands", () => {
  const tool = t({
    name: "operate",
    args: { tool: "artifact", intent: "tweak", args: { op: "write", artifact_id: 41, files: [] } },
    state: "input-available",
  });
  assert.equal(artifactIdOf(tool), 41);
});

test("persisted chips: create keeps preview_url, write keeps artifact_id in the digest", () => {
  const create = t({ verb: "create", digest: { preview_url: "/api/artifacts/7/render" } });
  const write = t({ name: "operate", operateTool: "artifact", verb: "write", digest: { artifact_id: 7 } });
  assert.equal(artifactIdOf(create), 7);
  assert.equal(artifactIdOf(write), 7);
  // mapToolCall turns op "desktop_from_url" into the verb "desktop from url".
  assert.equal(artifactIdOf(t({ verb: "desktop from url", digest: { artifact_id: 9 } })), 9);
});

test("ops that leave nothing to show, failed calls and other tools give null", () => {
  for (const op of ["render", "list", "history", "delete", "convert", "set_desktop", "publish_design"]) {
    assert.equal(artifactIdOf(t({ args: { op, artifact_id: 5 } })), null, op);
  }
  assert.equal(artifactIdOf(t({ args: { op: "write", artifact_id: 5 }, state: "output-error" })), null);
  assert.equal(artifactIdOf(t({ name: "operate", args: { tool: "set_desktop", args: { artifact_id: 5 } } })), null);
  assert.equal(artifactIdOf(t({ name: "web_search", args: { artifact_id: 5 } })), null);
  for (const bad of [0, -3, 1.5, "x", null]) {
    assert.equal(artifactIdOf(t({ args: { op: "write", artifact_id: bad } })), null, String(bad));
  }
});

test("a turn shows each artifact once, ordered by its last touch", () => {
  const tools = [
    t({ args: { op: "create" }, result: { status: "ok", result: { artifact_id: 1 } } }),
    t({ args: { op: "create" }, result: { status: "ok", result: { artifact_id: 2 } } }),
    t({ name: "operate", args: { tool: "artifact", args: { op: "write", artifact_id: 1 } } }),
    t({ name: "web_search", args: { query: "x" } }),
  ];
  assert.deepEqual(artifactsOfTurn(tools), [2, 1]);
  assert.deepEqual(artifactsOfTurn([]), []);
});
