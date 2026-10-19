import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { loadPatch, runFeed, subscribeRunFeed, type RunFeed } from "@/lib/run-feed";
import type { PatchFile } from "@/lib/run-steps";

/**
 * React's side of lib/run-feed.ts: an engine's stream, shared by every
 * component that shows it (the run card on the page, the strands on the
 * book's right side). The first reader opens the stream and the last to go
 * closes it.
 */
export function useRunFeed(sessionId: string, harness: string): RunFeed {
  const subscribe = useCallback(
    (cb: () => void) => subscribeRunFeed(sessionId, harness, cb),
    [sessionId, harness],
  );
  const get = useCallback(() => runFeed(sessionId, harness), [sessionId, harness]);
  return useSyncExternalStore(subscribe, get, get);
}

/** Every engine of one run (the strands): the same array until one of them
 * changes, so the board re-renders only when a frame arrives. */
export function useRunFeeds(sessionId: string | null, harnesses: readonly string[]): RunFeed[] {
  const key = sessionId == null ? "" : [sessionId, ...harnesses].join("\u0000");
  const ids = useRef<{ sid: string | null; hs: readonly string[] }>({ sid: sessionId, hs: harnesses });
  ids.current = { sid: sessionId, hs: harnesses };
  const cache = useRef<{ key: string; feeds: RunFeed[] }>({ key: "", feeds: [] });
  const subscribe = useCallback(
    (cb: () => void) => {
      const { sid, hs } = ids.current;
      if (sid == null) return () => {};
      const offs = hs.map((h) => subscribeRunFeed(sid, h, cb));
      return () => offs.forEach((off) => off());
    },
    // The ids are read through the ref; the key names them.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [key],
  );
  const get = useCallback(() => {
    const { sid, hs } = ids.current;
    const feeds = sid == null ? [] : hs.map((h) => runFeed(sid, h));
    const c = cache.current;
    if (c.key === key && c.feeds.length === feeds.length && c.feeds.every((f, i) => f === feeds[i])) return c.feeds;
    cache.current = { key, feeds };
    return feeds;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return useSyncExternalStore(subscribe, get, get);
}

/** What a landed run changed, per file, off its patch (the diff route serves
 * diffs/<harness>.patch). Not the stream's terminal `diff` field: on one
 * edit-mode build that was the whole worktree against an empty tree — 21
 * files, all "new", 0 deletions — where the patch says 7 files, +1393 −28.
 * Read once for the card and the scoreboard (lib/run-feed.ts loadPatch). */
export function useRunPatch(sessionId: string, harness: string, enabled: boolean): PatchFile[] | null {
  const [files, setFiles] = useState<PatchFile[] | null>(null);
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    void loadPatch(sessionId, harness).then((f) => {
      // No counts is better than wrong counts: a failed read leaves none, and
      // the card falls back to the files the engine reported writing.
      if (alive && f) setFiles(f);
    });
    return () => {
      alive = false;
    };
  }, [sessionId, harness, enabled]);
  return files;
}

/** Each landed engine's patch, for the scoreboard's Changes. */
export function useRunPatches(
  sessionId: string | null,
  harnesses: readonly string[],
  landed: readonly boolean[],
): (PatchFile[] | null)[] {
  const [files, setFiles] = useState<Record<string, PatchFile[]>>({});
  const want = sessionId == null ? "" : harnesses.filter((_, i) => landed[i]).join("\u0000");
  useEffect(() => {
    setFiles({});
  }, [sessionId]);
  useEffect(() => {
    if (sessionId == null || !want) return;
    let alive = true;
    for (const h of want.split("\u0000")) {
      void loadPatch(sessionId, h).then((f) => {
        if (alive && f) setFiles((cur) => (cur[h] === f ? cur : { ...cur, [h]: f }));
      });
    }
    return () => {
      alive = false;
    };
  }, [sessionId, want]);
  return harnesses.map((h, i) => (landed[i] ? (files[h] ?? null) : null));
}
