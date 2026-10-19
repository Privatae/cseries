import { useCallback, useEffect, useRef, useState } from "react";
import type { ThreadMessageRaw } from "@/lib/api";
import { fetchChat, type ChatPage } from "@/lib/cards-api";
import { withOlder } from "@/lib/cards";

/**
 * A person's chat, another Host's topic or a room, read a page at a time
 * (the rail's Load earlier, conversations-pane.tsx loadEarlier): the
 * newest page is the face's own read, and the pages before it are read here
 * when asked for, kept above it until the conversation changes. A read for a
 * conversation left since is dropped, so one chat's older page never lands in
 * another.
 */
export function useEarlier(kind: "dm" | "entity" | "circle", id: string, topic?: string | null) {
  const [older, setOlder] = useState<ThreadMessageRaw[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [total, setTotal] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const reqRef = useRef(0);
  const olderRef = useRef(older);
  olderRef.current = older;
  const key = `${kind}:${id}:${topic ?? ""}`;

  useEffect(() => {
    reqRef.current += 1;
    setOlder([]);
    setCursor(null);
    setTotal(null);
    setBusy(false);
    setErr(null);
  }, [key]);

  /** The face read the newest page: its total, and while no older page is
   * on screen, the cursor to the one before it. */
  const sawNewest = useCallback((page: ChatPage) => {
    setTotal(typeof page.total === "number" ? page.total : null);
    if (olderRef.current.length === 0) setCursor(page.next_before ?? null);
  }, []);

  const loadEarlier = useCallback(async () => {
    if (!cursor || busy) return;
    const req = ++reqRef.current;
    setBusy(true);
    setErr(null);
    const r = await fetchChat(kind, id, { topic: topic ?? null, before: cursor });
    if (req !== reqRef.current) return;
    setBusy(false);
    if (!r.ok) {
      setErr(r.message || "Couldn't read the earlier messages.");
      return;
    }
    setOlder((o) => [...(r.data.messages ?? []), ...o]);
    setCursor(r.data.next_before ?? null);
  }, [cursor, busy, kind, id, topic]);

  const merge = useCallback((newest: readonly ThreadMessageRaw[]) => withOlder(older, newest), [older]);

  return { merge, sawNewest, loadEarlier, cursor, total, busy, err, olderCount: older.length };
}

/** "Load earlier messages (N more)", above the oldest message on screen. */
export function Earlier({
  pages,
  shown,
}: {
  pages: ReturnType<typeof useEarlier>;
  /** How many messages are on screen. */
  shown: number;
}) {
  if (!pages.cursor) return null;
  const more = pages.total != null ? Math.max(0, pages.total - shown) : null;
  if (more === 0) return null;
  return (
    <>
      <button
        className="earlier"
        data-earlier
        disabled={pages.busy}
        onClick={() => void pages.loadEarlier()}
        type="button"
      >
        {pages.busy ? "Reading earlier messages…" : `Load earlier messages${more != null ? ` (${more} more)` : ""}`}
      </button>
      {pages.err && (
        <p className="err" role="alert" style={{ textAlign: "center" }}>
          {pages.err}
        </p>
      )}
    </>
  );
}
