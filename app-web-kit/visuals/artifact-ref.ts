// Which artifact a tool call made or changed — so the turn can show it as a
// live thumbnail instead of only a card of JSON.
//
// An artifact call reaches this app in two shapes:
//   live    — the `9:` frame's args and the `a:` frame's result envelope
//             ({status, result: {artifact_id, …}}); writes go through
//             operate(tool='artifact', args={op, artifact_id, …});
//   history — the persisted chip (session.py tool_call_chip): `tool`, `op`,
//             `operate_tool`, and `digest` (runtime/tool_trail.py), which
//             carries artifact_id and preview_url.
//
// Pure, so node --test covers both shapes. Shared by Circles and the
// Workbench; both apps' ToolInvocation fit ArtifactToolLike.

/** The fields of a tool call this module reads — both apps' ToolInvocation
 * have them (the last two only on persisted chips). */
export interface ArtifactToolLike {
  name: string;
  args: unknown;
  verb?: string;
  result?: unknown;
  state: string;
  operateTool?: string;
  digest?: Record<string, unknown>;
}

/** The ops after which there is a page worth showing. Not render/list/
 * history (they change nothing), delete (it is gone), convert (it is an app
 * now), set_desktop (the desktop itself shows it) or publish_design. */
const SHOWN_OPS = new Set(["create", "write", "promote", "import", "revert", "desktop_from_url"]);

function obj(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
}

function idOf(v: unknown): number | null {
  const n = typeof v === "string" && /^\d+$/.test(v) ? Number(v) : v;
  return typeof n === "number" && Number.isInteger(n) && n > 0 ? n : null;
}

function idFromPreviewUrl(v: unknown): number | null {
  const m = typeof v === "string" ? /\/api\/artifacts\/(\d+)\/render/.exec(v) : null;
  return m ? idOf(m[1]) : null;
}

/** The artifact this call left behind, or null. */
export function artifactIdOf(tool: ArtifactToolLike): number | null {
  if (tool.state === "output-error") return null;
  const args = obj(tool.args);
  let inner: Record<string, unknown>;
  if (tool.name === "artifact") inner = args;
  else if (tool.name === "operate" && (args.tool === "artifact" || tool.operateTool === "artifact")) {
    inner = obj(args.args);
  } else return null;

  const op = String(inner.op || inner.action || tool.verb || "create").trim().toLowerCase().replace(/ /g, "_");
  if (!SHOWN_OPS.has(op)) return null;

  const env = obj(tool.result);
  const res = obj(env.result);
  const digest = obj(tool.digest);
  return (
    idOf(res.artifact_id) ??
    idOf(env.artifact_id) ??
    idOf(inner.artifact_id) ??
    idOf(digest.artifact_id) ??
    idFromPreviewUrl(res.preview_url) ??
    idFromPreviewUrl(digest.preview_url)
  );
}

/** The distinct artifacts a turn touched, in the order each was LAST
 * touched — a create followed by three writes is one artifact, shown once,
 * in its final state. */
export function artifactsOfTurn(tools: ArtifactToolLike[]): number[] {
  const order: number[] = [];
  for (const t of tools) {
    const id = artifactIdOf(t);
    if (id == null) continue;
    const at = order.indexOf(id);
    if (at >= 0) order.splice(at, 1);
    order.push(id);
  }
  return order;
}
