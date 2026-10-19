import { scrollWithin } from "@/lib/book";
import { parsePatch, type PatchFile } from "@/lib/run-steps";
import { isFetchFailure, onSessionBack, SESSION_ENDED_CHANGES, SESSION_ENDED_VERSION, sessionEnded } from "@/lib/session";
import { cn } from "@/lib/utils";
import { XIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";

/**
 * VersionView — what "open version" opens. One window for
 * every kind of version the Workbench shows:
 *
 *   run     a run's OWN worktree — adopted or not, installed or not — through
 *           the per-run preview route (/api/review/sessions/{id}/preview/{h}),
 *           with its diff beside it;
 *   module  a module version's snapshot (or the module as it is now) through
 *           the project preview route (?module=&version=).
 *
 * The render is the server's (it inlines local assets and jails to the tree);
 * a tree with nothing renderable answers with the server's own reason, and a
 * run then opens on its changes instead, so the one button always shows
 * something true about what the run built.
 *
 * WHICH page is the server's too (entry_pages.choose_entry: the pages the run
 * itself changed first). When several qualify it answers 300 with the list,
 * and this window shows the list instead of opening one at random — a run
 * that built five themes used to open whichever the filesystem listed first. The chosen page rides back as ?entry=, and a switcher in the
 * header moves between the pages. The default: when the root index.html
 * is among the pages, the server opens it and names the rest in
 * X-Preview-Entries, so the switcher starts on the root.
 *
 * SEALED. Every page here was written by a harness, and the frame's sandbox
 * attribute gives it an opaque origin but NOT a network: measured in
 * Chromium, a sandbox="allow-scripts" srcdoc frame can fetch() and load an
 * external script (dashboard/base/js/sandbox-csp.js). So the document is
 * mounted under the shared sandbox CSP — the one definition the desktop,
 * Artifacts, Designs and the Network map use — and fails CLOSED when that
 * file did not load. What the policy blocks is named above the frame
 * (CSeriesSandbox.externalRefs), so a page that relied on a CDN reads as
 * "these were not loaded", not as a broken build.
 */
export type VersionSrc =
  | { kind: "run"; sessionId: string; harness: string; label: string }
  | { kind: "module"; projectId: number; module: string; version?: number | null; label: string };

export function versionPreviewUrl(src: VersionSrc, entry?: string | null): string {
  if (src.kind === "run") {
    const base = `/api/review/sessions/${encodeURIComponent(src.sessionId)}/preview/${encodeURIComponent(src.harness)}`;
    return entry ? `${base}?${new URLSearchParams({ entry }).toString()}` : base;
  }
  const q = new URLSearchParams({ module: src.module });
  if (src.version != null) q.set("version", String(src.version));
  if (entry) q.set("entry", entry);
  return `/api/projects/${src.projectId}/preview?${q.toString()}`;
}

export type PageChoice = { detail: string; entries: string[] };

/*
 * A DESKTOP DESIGN is the one kind of page that cannot render on its own. It
 * reads window.CSeriesDesktop from its first script line (desktop_contract: the
 * shell injects that bridge "before your scripts run"), and outside the shell
 * nothing does — so shown raw it throws there, before its canvas or app grid
 * ever start, and what is left is its bare background (a plain black
 * desktop). The Designs shelf
 * and the Artifacts window give such a document the inert stand-in from
 * /js/desktop-preview.js; this window uses the same one, with this Host's
 * installed apps, so the design draws and animates as it would on the desktop.
 */
type PreviewApp = { id: string; name: string };

declare global {
  interface Window {
    /** dashboard/base/js/desktop-preview.js, loaded by index.html. */
    DesktopPreview?: {
      instrument: (html: string, opts: { apps?: PreviewApp[]; host?: string }) => string;
    };
    /** dashboard/base/js/sandbox-csp.js, loaded by index.html. */
    CSeriesSandbox?: {
      withSandboxCsp: (html: string, extra?: string) => string;
      externalRefs: (html: string) => string[];
    };
  }
}

/** Why nothing is shown when the shared policy is absent — the same refusal
 * the Artifacts window and Designs shelf make. */
const SANDBOX_MISSING =
  "This can't be shown right now — the sandbox policy (/js/sandbox-csp.js) did not load, " +
  "and a page a harness wrote is never shown without it. Reload the Workbench.";

/** Does the document declare itself a desktop design? The same test the
 * Artifacts window uses before instrumenting (system-apps/artifacts/app.js). */
function isDesktopDoc(html: string): boolean {
  return /<meta[^>]+name\s*=\s*["']cseries-desktop["']/i.test(html.slice(0, 65536));
}

async function installedApps(): Promise<PreviewApp[]> {
  try {
    const r = await fetch("/api/applications", { headers: { Accept: "application/json" } });
    if (!r.ok) return [];
    const d = (await r.json()) as { applications?: { id?: string; name?: string }[] };
    return (d.applications ?? [])
      .filter((a) => a.id)
      .map((a) => ({ id: String(a.id), name: String(a.name || a.id) }));
  } catch {
    return [];
  }
}

/** The document as this window mounts it, or null when it must not be shown.
 *
 * Sealed first and always: the shared CSP goes in ahead of the page's own
 * markup (and ahead of the desktop stand-in, which is inline and allowed by
 * it). No policy, no render — FAIL CLOSED, because an unsealed frame looks
 * exactly like a sealed one. A desktop design also gets the stand-in bridge;
 * if THAT script did not load the design renders bare, since the stand-in is
 * a picture aid, not a security control. `blocked` is what the policy will
 * stop, named for the notice above the frame. */
async function mountable(html: string): Promise<{ doc: string; blocked: string[] } | null> {
  const sb = window.CSeriesSandbox;
  if (!sb) return null;
  const blocked = sb.externalRefs(html);
  const dp = window.DesktopPreview;
  const page = dp && isDesktopDoc(html) ? dp.instrument(html, { apps: await installedApps() }) : html;
  return { doc: sb.withSandboxCsp(page), blocked };
}

/** The other pages the server named when it opened one by default
 * (X-Preview-Entries / X-Preview-Entry, JSON), or null. */
function pageHeaders(res: Response): { entries: string[]; shown: string } | null {
  try {
    const list = res.headers.get("X-Preview-Entries");
    const one = res.headers.get("X-Preview-Entry");
    if (!list || !one) return null;
    const entries = JSON.parse(list) as unknown;
    const shown = JSON.parse(one) as unknown;
    if (!Array.isArray(entries) || typeof shown !== "string" || entries.length < 2) return null;
    return { entries: entries.map(String), shown };
  } catch {
    return null;
  }
}

/** The notice above a frame whose page reaches outside it: what the policy
 * stopped, by name (the Artifacts window's wording). */
export function blockedNotice(refs: string[]): string {
  const n = refs.length;
  return (
    `Not loaded — this preview has no network, so ${n} external reference${n === 1 ? " was" : "s were"} ` +
    `blocked: ${refs.slice(0, 3).join(", ")}${n > 3 ? " …" : ""}. ` +
    "The page may look incomplete here; ask your Host to inline them."
  );
}

/** One version's page as a frame may show it: the server's render, sealed
 * (mountable), with the choice the server offers when several pages qualify
 * and the reason when there is nothing to render. Shared by this window and
 * the run card's inline preview, so the two can never seal or choose
 * differently. */
export type VersionDoc = {
  html: string | null;
  err: string | null;
  blocked: string[];
  pages: PageChoice | null;
  /** The page the server opened by default while others qualify (the root). */
  shown: string | null;
  /** The server answered 404: nothing renderable in this version. */
  notFound: boolean;
  /** The request failed because the Host session ended (err says so in
   * plain words); the version loads again by itself once it is back. */
  ended: boolean;
};

export function useVersionDoc(src: VersionSrc, entry: string | null): VersionDoc {
  const [html, setHtml] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [blocked, setBlocked] = useState<string[]>([]);
  const [pages, setPages] = useState<PageChoice | null>(null);
  const [shown, setShown] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [ended, setEnded] = useState(false);
  const [attempt, setAttempt] = useState(0);
  // The version itself (no page chosen) — a new version forgets the old choice.
  const srcKey = versionPreviewUrl(src);
  const url = versionPreviewUrl(src, entry);

  useEffect(() => {
    setPages(null);
    setShown(null);
  }, [srcKey]);

  useEffect(() => {
    let alive = true;
    setHtml(null);
    setErr(null);
    setBlocked([]);
    setNotFound(false);
    setEnded(false);
    (async () => {
      // Same app-token delivery-race backoff the run stream uses.
      let res: Response | null = null;
      for (let attempt = 0; ; attempt++) {
        res = await fetch(url, { headers: { Accept: "text/html" }, cache: "no-store" });
        if (res.status === 401 && attempt < 3) {
          await new Promise((r) => setTimeout(r, 120 * (attempt + 1)));
          continue;
        }
        break;
      }
      if (!alive) return;
      if (res.status === 300) {
        // Several pages qualify: the person picks, the server never guesses.
        const d = (await res.json()) as { detail?: string; entries?: string[] };
        if (alive) setPages({ detail: d.detail || "choose a page to open", entries: d.entries ?? [] });
        return;
      }
      if (!res.ok) {
        let detail = `preview HTTP ${res.status}`;
        try {
          const d = (await res.json()) as { detail?: string };
          if (d.detail) detail = d.detail;
        } catch {
          if (res.status === 404) detail = "nothing renderable in this version";
        }
        setErr(detail);
        if (res.status === 404) setNotFound(true);
        return;
      }
      const others = pageHeaders(res);
      if (others) {
        setPages({ detail: "", entries: others.entries });
        setShown(others.shown);
      }
      const mounted = await mountable(await res.text());
      if (!alive) return;
      if (mounted === null) {
        setErr(SANDBOX_MISSING);
        return;
      }
      setBlocked(mounted.blocked);
      setHtml(mounted.doc);
    })().catch(async (e: unknown) => {
      // A rejected fetch here is, most often, the session ending: the router
      // redirects to the sign-in site and the browser refuses to follow. Ask
      // before saying so; anything else keeps its own message.
      if (alive && isFetchFailure(e) && (await sessionEnded())) {
        if (alive) {
          setErr(SESSION_ENDED_VERSION);
          setEnded(true);
        }
        return;
      }
      if (alive) setErr((e as Error).message || "preview failed");
    });
    return () => {
      alive = false;
    };
  }, [url, attempt]);

  // Once the session is back (the desktop's renewal succeeded, or a tab-wake
  // finds it alive), load the same version again — once per return.
  useEffect(() => {
    if (!ended) return;
    return onSessionBack(() => setAttempt((n) => n + 1));
  }, [ended]);

  return { html, err, blocked, pages, shown, notFound, ended };
}

/** How a load error reads: the session ending is a plain sentence, not a
 * failure in red. */
function errClass(ended: boolean): string {
  return ended ? "text-muted-foreground" : "text-destructive";
}

export function VersionView({
  src,
  startOn,
  onClose,
}: {
  src: VersionSrc;
  /** "changes" when the caller already knows there is nothing to render. */
  startOn?: "preview" | "changes";
  onClose: () => void;
}) {
  const hasChanges = src.kind === "run";
  const [tab, setTab] = useState<"preview" | "changes">(startOn === "changes" && hasChanges ? "changes" : "preview");
  const [entry, setEntry] = useState<string | null>(null);
  const srcKey = versionPreviewUrl(src);
  useEffect(() => {
    setEntry(null);
  }, [srcKey]);
  const { html, err, blocked, pages, shown, notFound, ended } = useVersionDoc(src, entry);
  // A run with no page to render opens on what it changed.
  useEffect(() => {
    if (notFound && hasChanges) setTab("changes");
  }, [notFound, hasChanges]);

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-background p-3" data-testid="version-view">
      <div className="mb-2 flex items-center gap-3 font-mono text-sm">
        <span className="min-w-0 truncate">{src.label}</span>
        {pages && (entry ?? shown) && tab === "preview" && (
          <select
            aria-label="Page"
            className="max-w-[40%] shrink-0 truncate rounded border border-border bg-background px-1.5 py-0.5 font-mono text-xs"
            data-entry-switch
            onChange={(e) => setEntry(e.target.value)}
            value={entry ?? shown ?? ""}
          >
            {pages.entries.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        )}
        {hasChanges && (
          <div className="flex shrink-0 items-center gap-1 font-sans text-xs">
            {(["preview", "changes"] as const).map((t) => (
              <button
                className={cn(
                  "rounded px-2 py-0.5",
                  tab === t ? "bg-foreground text-background" : "text-muted-foreground hover:text-foreground",
                )}
                key={t}
                onClick={() => setTab(t)}
                type="button"
              >
                {t === "preview" ? "Preview" : "Changes"}
              </button>
            ))}
          </div>
        )}
        <button
          aria-label="Close"
          className="ml-auto text-muted-foreground hover:text-foreground"
          onClick={onClose}
          type="button"
        >
          <XIcon className="size-4" />
        </button>
      </div>
      {tab === "changes" && src.kind === "run" ? (
        <div className="min-h-0 flex-1 overflow-auto">
          {err && !ended && (
            <p className="mb-2 px-1 text-muted-foreground text-xs">{err} — here is what the run changed.</p>
          )}
          <RunDiff harness={src.harness} sessionId={src.sessionId} />
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-md border bg-background">
          {err ? (
            <div className={cn("p-3 font-mono text-xs", errClass(ended))} data-version-error={ended ? "session" : "other"}>
              {err}
            </div>
          ) : pages && !entry && !shown ? (
            <div className="p-3 text-foreground" data-entry-choices>
              <p className="mb-2 text-muted-foreground text-xs">{pages.detail}</p>
              <ul className="flex flex-col items-start gap-1">
                {pages.entries.map((p) => (
                  <li key={p}>
                    <button
                      className="rounded border border-border px-2 py-1 font-mono text-xs hover:bg-secondary"
                      data-entry={p}
                      onClick={() => setEntry(p)}
                      type="button"
                    >
                      {p}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ) : html == null ? (
            <div className="p-3 font-mono text-muted-foreground text-xs">loading this version…</div>
          ) : (
            <>
              {blocked.length > 0 && (
                <div
                  className="shrink-0 border-b border-warning/40 bg-warning/10 px-3 py-1.5 text-[11px] text-foreground"
                  data-blocked-refs
                >
                  {blockedNotice(blocked)}
                </div>
              )}
              {/* The version's own page, on the canvas a browser gives a
                  page: white unless the page says otherwise. */}
              <iframe
                className="min-h-0 w-full flex-1 border-0 bg-white"
                sandbox="allow-scripts"
                srcDoc={html}
                title={`${src.label} preview`}
              />
            </>
          )}
        </div>
      )}
    </div>
  );
}

/** A run's DIFF — what it changed on disk, from the per-harness diff route.
 * A review in the shape of a git PR, for a run that changed code but built no
 * page. */
export function RunDiff({ sessionId, harness }: { sessionId: string; harness: string }) {
  // What the run changed reads file by file (DiffByFile).
  const [diff, setDiff] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [ended, setEnded] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let alive = true;
    setErr(null);
    setEnded(false);
    (async () => {
      try {
        const r = await fetch(
          `/api/review/sessions/${encodeURIComponent(sessionId)}/diff/${encodeURIComponent(harness)}`,
          { headers: { Accept: "application/json" } },
        );
        if (!r.ok) {
          if (alive) setErr(`diff HTTP ${r.status}`);
          return;
        }
        const d = (await r.json()) as { diff?: string };
        if (alive) setDiff(d.diff ?? "");
      } catch (e) {
        if (alive && isFetchFailure(e) && (await sessionEnded())) {
          if (alive) {
            setErr(SESSION_ENDED_CHANGES);
            setEnded(true);
          }
          return;
        }
        if (alive) setErr((e as Error).message || "diff failed");
      }
    })();
    return () => {
      alive = false;
    };
  }, [sessionId, harness, attempt]);

  useEffect(() => {
    if (!ended) return;
    return onSessionBack(() => setAttempt((n) => n + 1));
  }, [ended]);

  if (err) {
    return (
      <p className={cn("mb-2 px-3 py-2 text-xs", errClass(ended))} data-diff-error={ended ? "session" : "other"}>
        {err}
      </p>
    );
  }
  if (diff === null) {
    return <p className="mb-2 px-3 py-2 text-muted-foreground text-xs">loading diff…</p>;
  }
  if (!diff.trim()) {
    // A run can legitimately change nothing — say so rather than showing an
    // empty box that reads as a loading failure.
    return <p className="mb-2 px-3 py-2 text-muted-foreground text-xs">this run left no changes on disk</p>;
  }
  return <DiffByFile files={parsePatch(diff)} />;
}

/** Lines shown per file before "… N more lines in this file": one generated
 * file must not bury the rest of the change. */
export const DIFF_FILE_CAP = 1500;

/** What a run changed, file by file: the files with their +/− counts, each a
 * link to its own part of the diff below, which keeps its file's name in view
 * while it scrolls. */
function DiffByFile({ files }: { files: PatchFile[] }) {
  const parts = useRef<(HTMLDivElement | null)[]>([]);
  const reduced =
    typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  if (!files.length) {
    return <p className="mb-2 px-3 py-2 text-muted-foreground text-xs">this run left no changes on disk</p>;
  }
  const add = files.reduce((a, f) => a + f.add, 0);
  const del = files.reduce((a, f) => a + f.del, 0);
  return (
    <div className="book-diff" data-diff-by-file>
      <nav aria-label="Changed files" className="book-diff-files">
        <p className="book-diff-sum">
          {files.length} file{files.length === 1 ? "" : "s"} · <span className="book-add">+{add}</span>{" "}
          <span className="book-del">−{del}</span>
        </p>
        {files.map((f, i) => (
          <button
            data-diff-file={f.path}
            key={`${f.path}-${i}`}
            onClick={() => {
              const part = parts.current[i];
              if (part) scrollWithin(part, "start", !reduced);
            }}
            title={f.path}
            type="button"
          >
            <span className="book-diff-path">{f.path}</span>
            {f.status !== "modified" && <span className="book-diff-status">{f.status}</span>}
            <span className="book-add">+{f.add}</span>
            <span className="book-del">−{f.del}</span>
          </button>
        ))}
      </nav>
      <div className="book-diff-body">
        {files.map((f, i) => (
          <div data-diff-part={f.path} key={`${f.path}-${i}`} ref={(el) => {
              parts.current[i] = el;
            }}>
            <div className="book-diff-head">
              <span className="book-diff-path">{f.path}</span>
              <span className="book-add">+{f.add}</span>
              <span className="book-del">−{f.del}</span>
            </div>
            {f.lines.slice(0, DIFF_FILE_CAP).map((l, j) => (
              <div
                className={cn(
                  "book-diff-line",
                  l.startsWith("@@") ? "is-hunk" : l.startsWith("+") ? "is-add" : l.startsWith("-") ? "is-del" : "",
                )}
                key={j}
              >
                {l || " "}
              </div>
            ))}
            {f.lines.length > DIFF_FILE_CAP && (
              <div className="book-diff-more">… {f.lines.length - DIFF_FILE_CAP} more lines in this file</div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
