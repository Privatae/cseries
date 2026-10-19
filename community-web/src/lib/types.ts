// Wire types — mirror server/routes/community.py (which mirrors communityd's
// forum package). Keep field names identical to the JSON; no client renames.

export type Category = {
  slug: string;
  title: string;
  description: string;
  position: number;
};

export type Thread = {
  id: number;
  category: string;
  author_entity: number;
  author_name: string;
  title: string;
  body: string;
  status: "open" | "locked" | "resolved";
  proposal_ref?: number;
  votes: number;
  replies: number;
  created_at: string;
  edited_at?: string;
};

export type Post = {
  id: number;
  thread_id: number;
  parent_id?: number | null;
  author_entity: number;
  author_name: string;
  body: string;
  deleted: boolean;
  votes: number;
  created_at: string;
  edited_at?: string;
};

export type Sort = "hot" | "new" | "top";

// A post with its nested replies — the flat parent-linked list becomes a tree
// client-side (the server deliberately returns it flat, ordered by id).
export type PostNode = Post & { children: PostNode[] };

export function buildTree(posts: Post[]): PostNode[] {
  const byId = new Map<number, PostNode>();
  for (const p of posts) byId.set(p.id, { ...p, children: [] });
  const roots: PostNode[] = [];
  for (const node of byId.values()) {
    const parent = node.parent_id ? byId.get(node.parent_id) : undefined;
    if (parent) parent.children.push(node);
    else roots.push(node);
  }
  return roots;
}
