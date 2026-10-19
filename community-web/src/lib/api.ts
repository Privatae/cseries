// Community API layer — same request/ApiResult conventions as workbench-web,
// including the app-token delivery-race retry (the shell wraps this iframe's
// fetch on 'load'; mount-time calls can beat the shim and 401).
//
// Writes carry a caller-stable op_id minted ONCE per user action: a timeout
// retried with the same op_id returns the already-created row server-side, so
// double-submits are structurally impossible.

import type { Category, Post, Sort, Thread } from "@/lib/types";

// Shared client core — see app-web-kit/api.ts (ApiResult, the 401-retry
// request loop, getJSON). Endpoint functions below stay app-local.
export type { ApiResult } from "@kit/api";
import { getJSON, request, type ApiResult } from "@kit/api";

/** Stable per-action operation id (idempotency key). Mint once per user
 * action and reuse across retries of THAT action — never per request. */
export const mintOpId = () =>
  crypto.randomUUID ? crypto.randomUUID() : `op-${Date.now()}-${Math.random().toString(36).slice(2)}`;

// ---- reads ----

export async function fetchCategories(): Promise<ApiResult<Category[]>> {
  const r = await getJSON<{ categories: Category[] }>("/api/community/categories");
  return r.ok ? { ok: true, data: r.data.categories ?? [] } : r;
}

export async function fetchThreads(
  category: string,
  sort: Sort,
  offset = 0,
  limit = 50,
): Promise<ApiResult<Thread[]>> {
  const q = new URLSearchParams({ sort, limit: String(limit), offset: String(offset) });
  if (category) q.set("category", category);
  const r = await getJSON<{ threads: Thread[] }>(`/api/community/threads?${q}`);
  return r.ok ? { ok: true, data: r.data.threads ?? [] } : r;
}

export async function fetchThread(
  id: number,
): Promise<ApiResult<{ thread: Thread; posts: Post[] }>> {
  const r = await getJSON<{ thread: Thread; posts: Post[] | null }>(
    `/api/community/threads/${id}`,
  );
  return r.ok
    ? { ok: true, data: { thread: r.data.thread, posts: r.data.posts ?? [] } }
    : r;
}

export async function searchThreads(
  query: string,
  limit = 50,
): Promise<ApiResult<Thread[]>> {
  const q = new URLSearchParams({ q: query, limit: String(limit) });
  const r = await getJSON<{ threads: Thread[] }>(`/api/community/search?${q}`);
  return r.ok ? { ok: true, data: r.data.threads ?? [] } : r;
}

// ---- writes (op_id-idempotent) ----

export async function createThread(args: {
  op_id: string;
  category: string;
  title: string;
  body: string;
}): Promise<ApiResult<{ thread: Thread }>> {
  return request("POST", "/api/community/threads", args);
}

export async function createReply(args: {
  op_id: string;
  thread_id: number;
  parent_id?: number | null;
  body: string;
}): Promise<ApiResult<{ post: Post }>> {
  return request("POST", "/api/community/posts", args);
}

export async function vote(
  target_kind: "thread" | "post",
  target_id: number,
  up: boolean,
): Promise<ApiResult<{ votes: number }>> {
  return request("POST", "/api/community/vote", { target_kind, target_id, up });
}

/** The ladder: escalate a thread into the governed proposal queue. The
 * member's host opens the community/proposals PR (votes snapshot into its
 * popularity) and the thread gets stamped with the PR number. */
export async function propose(
  thread_id: number,
): Promise<ApiResult<{ proposal_ref: number; url?: string; already_proposed?: boolean }>> {
  return request("POST", `/api/community/threads/${thread_id}/propose`, {}, 60000);
}

export async function flag(
  target_kind: "thread" | "post",
  target_id: number,
  reason: string,
): Promise<ApiResult<{ flagged: boolean }>> {
  return request("POST", "/api/community/flag", { target_kind, target_id, reason });
}
