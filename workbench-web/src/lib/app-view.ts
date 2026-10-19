// The view the desktop hands this window, and what the Workbench does with it.
//
// A window opened WITH a view gets `#view=<v>` on its frame address
// (dashboard/base/js/app-runner.js); a window already open gets a
// `cseries:app-view` message instead. Both come from the same places: the host's
// open_app tool, and a deep link — `/?open=workbench&view=project:<id>`, which
// is what a "build finished" reach-out carries (server/deep_link.py).
//
// The Workbench understands one view, `project:<id>`: that project, open.

/** The `view=` value in a frame hash (`#view=project%3A7`), decoded; or null. */
export function viewFromHash(hash: string | null | undefined): string | null {
  const m = /(?:^|[#&])view=([^&]*)/.exec(hash ?? "");
  if (!m) return null;
  try {
    return decodeURIComponent(m[1]) || null;
  } catch {
    return null;
  }
}

/** The project id a view names, or null for anything else. */
export function projectOfView(view: string | null | undefined): number | null {
  const m = /^project:(\d{1,12})$/.exec(view ?? "");
  return m ? Number(m[1]) : null;
}

// A view can arrive BEFORE any React effect has run: the desktop posts it on
// the frame's `load` (app-runner.js postView), and a React root mounts its
// effects just after — measured, the effect lost that race every time on a
// minimal page. So the listener is installed when this module first loads,
// from main.tsx before rendering, and a view that arrives with nobody
// subscribed is held until someone is.
let installed = false;
let held: string | null = null;
const subscribers = new Set<(view: string) => void>();

/** Listen for the desktop's views from now on. Idempotent; call it early. */
export function installViewInbox(): void {
  if (installed || typeof window === "undefined") return;
  installed = true;
  window.addEventListener("message", (ev: MessageEvent) => {
    // Only the desktop that framed this window may send one.
    if (ev.source !== window.parent) return;
    const d = ev.data as { type?: unknown; view?: unknown } | null;
    if (!d || d.type !== "cseries:app-view" || typeof d.view !== "string") return;
    if (subscribers.size === 0) held = d.view;
    else subscribers.forEach((fn) => fn(d.view as string));
  });
}

/**
 * Views sent while this window is open, including one that arrived before
 * this subscription (delivered at once). Returns the unsubscribe.
 */
export function onAppView(fn: (view: string) => void): () => void {
  installViewInbox();
  subscribers.add(fn);
  if (held != null) {
    const v = held;
    held = null;
    fn(v);
  }
  return () => {
    subscribers.delete(fn);
  };
}

/** Tests only: forget the installed listener and anything held or subscribed. */
export function resetViewInboxForTests(): void {
  installed = false;
  held = null;
  subscribers.clear();
}
