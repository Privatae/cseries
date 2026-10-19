// The calls the cards make (src/cards/), beside lib/api.ts, which keeps the
// rail's. Each route is named with the server function that answers it; the
// shapes are the ones those functions return, and scripts/test_circles_cards
// .py serves the same shapes to the built app.

import { getJSON, request, type ApiResult } from "@kit/api";
import type { RunningTotals } from "@kit/book/page-count";
import type { WhereThisStands } from "@kit/book/page-top";
import type { ThreadMessageRaw } from "./api";
import type { CalendarEvent, Contact, Ticket } from "./types";
import type { DeckPayload, DoorCall, DoorItem, ThreadRow, TopicRow, WaitingDecision } from "./cards";

/** GET /api/circles/conversations (list_conversations, after circles_deck
 * .pair; groups from circles_deck.groups). */
export function fetchDeck(): Promise<ApiResult<DeckPayload>> {
  return getJSON<DeckPayload>("/api/circles/conversations");
}

/** Contact `id`, read back from the contacts list by its handle (the list is
 * paged, so `q=` finds it wherever it sorts): its status and, on a Host,
 * `blocked_through`, the blocked people it is blocked through
 * (server/routes/contacts.py list_contacts_service). Null when not found. */
export async function readContact(id: number, handle: string): Promise<ApiResult<Contact | null>> {
  const r = await getJSON<{ contacts: Contact[] }>(`/api/contacts/list?q=${encodeURIComponent(handle)}`);
  if (!r.ok) return r;
  return { ok: true, data: (r.data.contacts ?? []).find((c) => c.id === id) ?? null };
}

/** GET /api/circles/threads (circles_threads.list_threads): your Host's
 * pages, the original first, each with its marks. */
export function fetchHostThreads(): Promise<ApiResult<{ threads: ThreadRow[] }>> {
  return getJSON<{ threads: ThreadRow[] }>("/api/circles/threads");
}

/** One of your Host's pages (circles_marks.page_data): where this stands,
 * its running totals, its marks. */
export interface HostPage {
  kind: "self";
  id: string;
  title: string | null;
  where_this_stands: WhereThisStands | null;
  totals: RunningTotals | null;
  seen_at: string | null;
  last_activity_at: string | null;
  last_landed_at: string | null;
  unseen_landed_at: string | null;
  waiting_on_you: number;
}

export function fetchHostPage(threadId: string): Promise<ApiResult<HostPage>> {
  return getJSON<HostPage>(`/api/circles/thread/self/${encodeURIComponent(threadId)}/page`);
}

/** PUT /api/circles/thread/{kind}/{id}/seen (circles_marks.mark_seen): the
 * person has this conversation open. A turn token marks that reply watched
 * (your Host's pages only); a topic marks one topic of a Host's conversation. */
export function markSeen(
  kind: "self" | "entity" | "dm" | "circle",
  id: string,
  opts: { turnToken?: string | null; topic?: string | null } = {},
): Promise<ApiResult<{ kind: string; id: string; seen_at: string }>> {
  const q = opts.topic ? `?topic=${encodeURIComponent(opts.topic)}` : "";
  return request(
    "PUT",
    `/api/circles/thread/${encodeURIComponent(kind)}/${encodeURIComponent(id)}/seen${q}`,
    opts.turnToken ? { turn_token: opts.turnToken } : {},
  );
}

/** GET /api/circles/thread/entity/{peer}/topics (topics.list_topics). */
export function fetchTopics(peer: number): Promise<ApiResult<{ kind: string; id: string; topics: TopicRow[] }>> {
  // The route's kind for another Host, as fetchChat takes it.
  const kind = "entity";
  return getJSON(`/api/circles/thread/${kind}/${peer}/topics`);
}

/** A send to another Host (POST /api/circles/messages, target_kind entity):
 * into a topic it has, or a new one (cards slice 3, _topic_for_send). */
export function sendToHost(
  peer: number,
  body: string,
  topic: { id: string } | { title: string },
): Promise<ApiResult<{ ok: boolean; topic?: { id: string; title: string } }>> {
  const t = "id" in topic ? { topic_id: topic.id } : { new_topic: topic.title };
  return request("POST", "/api/circles/messages", {
    target_kind: "entity",
    target_id: String(peer),
    body,
    ...t,
  });
}

/** PUT /api/circles/thread/dm/{email}/reading (set_circles_reading). */
export function setReading(
  email: string,
  on: boolean,
): Promise<
  ApiResult<{ kind: string; id: string; on: boolean; changed: boolean; notice?: string; delivered?: boolean }>
> {
  return request("PUT", `/api/circles/thread/dm/${encodeURIComponent(email)}/reading`, { on });
}

/** GET /api/circles/door (circles_deck.door). */
export function fetchDoor(): Promise<ApiResult<{ items: DoorItem[]; count: number }>> {
  return getJSON<{ items: DoorItem[]; count: number }>("/api/circles/door");
}

/** One call the door named (its Accept, or one of its Not now). */
export function doorCall(c: DoorCall): Promise<ApiResult<unknown>> {
  const method = String(c.method).toUpperCase();
  return request(method, c.path, method === "GET" ? undefined : (c.body ?? {}));
}

/** A support draft waiting for Send (circles_deck.waiting). */
export interface SupportDraftRow {
  id: number;
  title: string;
  category?: string;
  revision?: number;
  shared_items?: number;
  link?: string;
}

/** GET /api/circles/waiting (circles_deck.waiting). */
export interface WaitingPayload {
  decisions: Array<WaitingDecision & Partial<Ticket>>;
  support_drafts: SupportDraftRow[];
  door: number;
  count: number;
}

export function fetchWaiting(): Promise<ApiResult<WaitingPayload>> {
  return getJSON<WaitingPayload>("/api/circles/waiting");
}

/** Coming up: the next events, soonest first (GET /api/calendar/list with
 * order=asc, slice 6), from now. */
export function fetchComingUp(limit = 4): Promise<ApiResult<{ events: CalendarEvent[]; count: number }>> {
  const since = new Date().toISOString();
  return getJSON(`/api/calendar/list?since=${encodeURIComponent(since)}&status=scheduled&order=asc&limit=${limit}`);
}

/** A group's roster (GET /api/groups/{id}). */
export interface GroupDetail {
  group: { group_id: string; name: string; description?: string | null; owner_entity_id: number };
  owned: boolean;
  members: Array<{ member_entity_id: number; role: string; display_name?: string | null; removed_at?: string | null }>;
}

export function fetchGroup(id: string): Promise<ApiResult<GroupDetail>> {
  return getJSON<GroupDetail>(`/api/groups/${encodeURIComponent(id)}`);
}

/** A group's ballots (GET /api/groups/{id}/ballots). */
export interface Ballot {
  ballot_id: string;
  question: string;
  choices: string[];
  closes_at?: string | null;
  closed_at?: string | null;
}

export function fetchBallots(id: string): Promise<ApiResult<{ group_id: string; ballots: Ballot[] }>> {
  return getJSON(`/api/groups/${encodeURIComponent(id)}/ballots`);
}

/** Open another app of the desktop, on a view of it (the shell's
 * cseries:open-app, dashboard/base/js/app-runner.js). Same origin only. */
export function openApp(appId: string, view?: string): void {
  try {
    const msg: Record<string, unknown> = { type: "cseries:open-app", app_id: appId };
    if (view) msg.view = view;
    (window.parent || window).postMessage(msg, window.location.origin);
  } catch {
    /* not framed */
  }
}

/** A conversation's newest page with what the route says about it
 * (GET /api/circles/thread/{kind}/{id}): a person's chat carries
 * policy.host_reads, the reading switch (_read_people_thread); another
 * Host's, read one topic at a time with `topic` (slice 3). */
export interface ChatPage {
  messages: ThreadMessageRaw[];
  policy?: { host_reads?: boolean; people?: boolean };
  total?: number;
  next_before?: string | null;
  topic_id?: string;
}

export function fetchChat(
  kind: "dm" | "entity" | "circle",
  id: string,
  opts: { topic?: string | null; before?: string | null } = {},
): Promise<ApiResult<ChatPage>> {
  // `before` reads the page before the oldest one on screen: the cursor a
  // page's next_before named (the rail's Load earlier, R4-1).
  const qs = new URLSearchParams();
  if (opts.topic) qs.set("topic", opts.topic);
  if (opts.before) qs.set("before", opts.before);
  const q = qs.toString();
  return getJSON<ChatPage>(`/api/circles/thread/${kind}/${encodeURIComponent(id)}${q ? `?${q}` : ""}`);
}

/** Clear a conversation (DELETE /api/circles/thread/{kind}/{ref},
 * circles.delete_thread): a person's chat or a Host's conversation is
 * archived on this Host, nothing erased; a room is left (and archived for
 * everyone when this Host made it). */
export function clearChat(
  kind: "dm" | "entity" | "circle",
  ref: string,
): Promise<ApiResult<{ ok: boolean; cleared: number }>> {
  return request("DELETE", `/api/circles/thread/${kind}/${encodeURIComponent(ref)}`);
}
