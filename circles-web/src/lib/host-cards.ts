// Contact cards for OTHER Hosts (2026-09-22). One resolver on the Host
// (server/hosts/card.py, POST /api/hosts/cards) behind every surface that
// names another Host — never a bare handle or "Entity N".
//
// Batched and cached: a sidebar of forty peers asks once, not forty times.
import { useEffect, useState } from "react";
import { request, type ApiResult } from "@kit/api";

export type HostCard = {
  host_id: number;
  number: string; // "Host #9163"
  classification: string; // "Host"
  display_name: string;
  address: string | null; // "0123456789abcdef.hosts.example.test"
  relationship: "contact" | "pending" | "blocked" | "shares_machine" | "known";
  relationship_label: string; // "My host" / "Alex's host", else the connection
  connection_label: string; // "Contact" / "Connection pending" / ...
  mine: boolean;
  shares_machine: boolean;
  can_message: boolean;
  contact_number: string | null;
  user: string | null;
  sources: string[];
};

// Cached briefly, and cleared outright after a contact changes (clearHostCards).
const TTL_MS = 60_000;
const MAX_IDS = 500; // the resolver's own per-request ceiling
const cache = new Map<number, HostCard>();
const cachedAt = new Map<number, number>();
// Cards on screen re-fetch when the cache is cleared (a contact changed).
const listeners = new Set<() => void>();
const waiting = new Map<number, Array<(c: HostCard | null) => void>>();
let queued = new Set<number>();
let timer: ReturnType<typeof setTimeout> | null = null;

async function flush(): Promise<void> {
  const ids = [...queued];
  queued = new Set();
  timer = null;
  if (!ids.length) return;
  const cards: Record<string, HostCard> = {};
  for (let i = 0; i < ids.length; i += MAX_IDS) {
    const r: ApiResult<{ cards: Record<string, HostCard> }> = await request(
      "POST",
      "/api/hosts/cards",
      { ids: ids.slice(i, i + MAX_IDS) },
    );
    if (r.ok) Object.assign(cards, r.data.cards);
  }
  for (const id of ids) {
    const c = cards[String(id)] ?? null;
    if (c) {
      cache.set(id, c);
      cachedAt.set(id, Date.now());
    }
    for (const done of waiting.get(id) ?? []) done(c);
    waiting.delete(id);
  }
}

function fresh(id: number): HostCard | undefined {
  const at = cachedAt.get(id);
  return at !== undefined && Date.now() - at < TTL_MS ? cache.get(id) : undefined;
}

export function loadHostCard(id: number): Promise<HostCard | null> {
  const hit = fresh(id);
  if (hit) return Promise.resolve(hit);
  return new Promise((resolve) => {
    waiting.set(id, [...(waiting.get(id) ?? []), resolve]);
    queued.add(id);
    if (!timer) timer = setTimeout(flush, 30);
  });
}

/** The card for a Host id, or null while loading / when there is none. */
export function useHostCard(id: number | null | undefined): HostCard | null {
  const n = typeof id === "number" && Number.isFinite(id) && id > 0 ? id : null;
  const [card, setCard] = useState<HostCard | null>(n ? fresh(n) ?? null : null);
  const [gen, setGen] = useState(0);
  useEffect(() => {
    const bump = () => setGen((g) => g + 1);
    listeners.add(bump);
    return () => {
      listeners.delete(bump);
    };
  }, []);
  useEffect(() => {
    let live = true;
    if (!n) {
      setCard(null);
      return;
    }
    loadHostCard(n).then((c) => live && setCard(c));
    return () => {
      live = false;
    };
  }, [n, gen]);
  return card;
}

/** Forget cached cards — after accepting, blocking or deleting a contact. */
export function clearHostCards(): void {
  cache.clear();
  cachedAt.clear();
  for (const bump of listeners) bump();
}
