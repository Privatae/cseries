// The artifact calls a visual and an artifact card make — the same routes and
// scopes the Artifacts window uses (server/routes/artifacts.py: read:projects
// for the render, write:projects for Save). Shared by Circles and the
// Workbench; both apps declare both scopes.

import { request, sleep, type ApiResult } from "../api";

/** An artifact's live build as HTML text (the render route wears the
 * desktop). request() parses JSON, so this is its own small loop with the
 * same app-token 401 retry. */
export async function fetchArtifactHtml(id: number): Promise<ApiResult<string>> {
  for (let attempt = 0; ; attempt++) {
    try {
      const res = await fetch(`/api/artifacts/${id}/render`, { headers: { Accept: "text/html" } });
      if (res.status === 401 && attempt < 3) {
        await sleep(120 * (attempt + 1));
        continue;
      }
      if (!res.ok) {
        return { ok: false, status: res.status, message: res.status === 404 ? "This artifact no longer exists." : `HTTP ${res.status}` };
      }
      return { ok: true, data: await res.text() };
    } catch (e) {
      return { ok: false, status: 0, message: (e as Error).message || "network error" };
    }
  }
}

let tokenSheet: Promise<string> | null = null;

/** /css/tokens.css — the sheet this app (and every artifact render) wears,
 * fetched once. '' when unreadable: the block then shows as written. */
export function fetchTokenSheet(): Promise<string> {
  tokenSheet ??= fetch("/css/tokens.css")
    .then((r) => (r.ok ? r.text() : ""))
    .catch(() => "");
  return tokenSheet;
}

/** Keep a visual as an artifact — only ever on the user's click. */
export function saveHtmlAsArtifact(name: string, html: string) {
  return request<{ artifact_id: number; name: string }>("POST", "/api/artifacts", {
    name,
    description: "A visual saved from a chat reply",
    files: [{ path: "index.html", content: html }],
  });
}
