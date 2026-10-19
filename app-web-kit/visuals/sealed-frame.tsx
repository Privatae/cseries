import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";
import { fetchTokenSheet } from "./api";
import { clampHeight, frameDoc, isDesktopDoc, MIN_FRAME_H, SIZE_MSG, type Sandbox } from "./visual";

/*
 * SEALED. Every document drawn here was written by a Host, and the frame's
 * sandbox gives it an opaque origin but NOT a network: measured in Chromium,
 * a sandbox="allow-scripts" srcdoc frame can fetch() (dashboard/base/js/
 * sandbox-csp.js). So it mounts under the shared sandbox CSP — the one
 * definition the desktop, Artifacts, Designs, the Network map and the
 * Workbench version window use — and fails CLOSED when that file did not
 * load. Never add allow-same-origin: the frame would then share the app's
 * session.
 *
 * ONE frame element, fed only from `doc`, which only ever holds what
 * frameDoc() returned — pinned by scripts/test_sandbox_csp.py, driven in the
 * built apps by scripts/test_chat_visuals.py.
 */

declare global {
  interface Window {
    /** dashboard/base/js/sandbox-csp.js, loaded by the app's index.html. */
    CSeriesSandbox?: Sandbox;
    /** dashboard/base/js/desktop-preview.js, loaded by the app's index.html. */
    DesktopPreview?: {
      instrument: (html: string, opts: { apps?: { id: string; name: string }[]; host?: string }) => string;
    };
  }
}

export const SANDBOX_MISSING =
  "This can't be shown right now — the sandbox policy (/js/sandbox-csp.js) did not load, " +
  "and a page your Host wrote is never shown without it. Reload the app.";

/** Ask the desktop to open the Artifacts window on one artifact — the same
 * message the host's open_app call relays. */
export function openArtifactWindow(id: number) {
  try {
    (window.parent || window).postMessage(
      { type: "cseries:open-app", app_id: "artifacts", view: `artifact:${id}` },
      window.location.origin,
    );
  } catch {
    /* not framed */
  }
}

/** Mounted only while near the screen: a long history of animated replies
 * must not keep every one of them running. */
function useNearScreen<T extends Element>() {
  const ref = useRef<T | null>(null);
  const [near, setNear] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === "undefined") {
      setNear(true);
      return;
    }
    const io = new IntersectionObserver((es) => setNear(es.some((e) => e.isIntersecting)), {
      rootMargin: "300px 0px",
    });
    io.observe(el);
    return () => io.disconnect();
  }, []);
  return [ref, near] as const;
}

const STAGE_W = 1280;
const STAGE_H = 800;

/**
 * One host-authored document in a sealed frame.
 *  - fit:   width of the bubble, height follows the content (the frame
 *           reports it; bounded by MIN/MAX_FRAME_H);
 *  - fill:  fills its parent (the full-size dialog);
 *  - stage: a 1280×800 page scaled down to the parent's width (thumbnails —
 *           the stage the Artifacts gallery uses).
 * The frame is dropped while far off screen and keeps its last height.
 */
export function SealedFrame({
  html,
  mode,
  title,
  className,
  onBlocked,
}: {
  html: string;
  mode: "fit" | "fill" | "stage";
  title: string;
  className?: string;
  onBlocked?: (refs: string[]) => void;
}) {
  const [box, near] = useNearScreen<HTMLDivElement>();
  const frame = useRef<HTMLIFrameElement | null>(null);
  const [doc, setDoc] = useState<string | null | undefined>(undefined); // undefined = preparing
  const [height, setHeight] = useState<number>(160);
  const [scale, setScale] = useState(0.25);

  useEffect(() => {
    let live = true;
    void fetchTokenSheet().then((sheet) => {
      if (!live) return;
      // A desktop design reads window.CSeriesDesktop first and draws nothing
      // without it; the inert stand-in is a picture aid, not a control.
      const dp = window.DesktopPreview;
      const page = dp && isDesktopDoc(html) ? dp.instrument(html, {}) : html;
      const out = frameDoc(page, window.CSeriesSandbox, sheet);
      setDoc(out ? out.doc : null);
      if (out) onBlocked?.(out.blocked);
    });
    return () => {
      live = false;
    };
    // onBlocked is a notice, not an input to the document.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [html]);

  useEffect(() => {
    if (mode !== "fit") return;
    const onMsg = (e: MessageEvent) => {
      // Identity, not origin: the frame's origin is opaque ("null").
      if (!frame.current || e.source !== frame.current.contentWindow) return;
      const d = e.data as { type?: unknown; h?: unknown } | null;
      if (!d || d.type !== SIZE_MSG) return;
      const h = clampHeight(d.h);
      if (h != null) setHeight(h);
    };
    window.addEventListener("message", onMsg);
    return () => window.removeEventListener("message", onMsg);
  }, [mode]);

  useEffect(() => {
    const el = box.current;
    if (mode !== "stage" || !el) return;
    const ro = new ResizeObserver(() => setScale(el.clientWidth / STAGE_W || 0.25));
    ro.observe(el);
    return () => ro.disconnect();
  }, [mode, box]);

  if (doc === null) {
    return (
      <div
        className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-destructive text-xs"
        data-sandbox-missing
      >
        {SANDBOX_MISSING}
      </div>
    );
  }

  const stage = mode === "stage";
  return (
    <div
      className={cn("w-full", stage && "relative overflow-hidden bg-muted/30", mode === "fill" && "h-full", className)}
      ref={box}
      style={
        stage
          ? { height: Math.round(STAGE_H * scale) }
          : mode === "fit"
            ? { height: Math.max(height, MIN_FRAME_H) }
            : undefined
      }
    >
      {near && typeof doc === "string" && (
        <iframe
          aria-hidden={stage || undefined}
          className={cn(
            "border-0",
            stage ? "pointer-events-none absolute top-0 left-0 origin-top-left" : "block size-full",
          )}
          ref={frame}
          sandbox="allow-scripts"
          srcDoc={doc}
          style={stage ? { width: STAGE_W, height: STAGE_H, transform: `scale(${scale})` } : undefined}
          tabIndex={stage ? -1 : undefined}
          title={title}
        />
      )}
    </div>
  );
}
