// New-thread composer — an inline panel (no modal chrome; the app IS a
// window). One op_id per open composer: a timed-out submit retried lands the
// same thread, never two.

import { useState } from "react";
import { Loader2, X } from "lucide-react";

import * as api from "@/lib/api";
import type { Category, Thread } from "@/lib/types";

export function Composer({
  categories,
  initialCategory,
  onCreated,
  onClose,
}: {
  categories: Category[];
  initialCategory: string;
  onCreated: (t: Thread) => void;
  onClose: () => void;
}) {
  const [category, setCategory] = useState(
    initialCategory || categories[0]?.slug || "",
  );
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [opId] = useState(api.mintOpId);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const submit = async () => {
    if (!title.trim() || !body.trim() || busy) return;
    setBusy(true);
    setErr("");
    const r = await api.createThread({ op_id: opId, category, title, body });
    setBusy(false);
    if (r.ok) onCreated(r.data.thread);
    else setErr(r.message);
  };

  return (
    <div className="border-b border-border bg-card px-4 py-3">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium text-foreground">New thread</span>
        <button onClick={onClose} className="text-muted-foreground hover:text-foreground">
          <X size={15} />
        </button>
      </div>
      <div className="mt-2 flex flex-col gap-2">
        <div className="flex gap-2">
          <select
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            className="rounded-md border border-input bg-background px-2 py-1.5 text-xs text-foreground outline-none focus:ring-1 focus:ring-ring"
          >
            {categories.map((c) => (
              <option key={c.slug} value={c.slug}>
                {c.title}
              </option>
            ))}
          </select>
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Title"
            maxLength={300}
            className="flex-1 rounded-md border border-input bg-background px-2 py-1.5 text-sm outline-none focus:ring-1 focus:ring-ring"
          />
        </div>
        <textarea
          value={body}
          onChange={(e) => setBody(e.target.value)}
          rows={5}
          placeholder="Write it up — markdown works, code blocks too."
          className="w-full resize-y rounded-md border border-input bg-background p-2 text-sm outline-none focus:ring-1 focus:ring-ring"
        />
        {err && <div className="text-xs text-destructive">{err}</div>}
        <div>
          <button
            onClick={() => void submit()}
            disabled={busy || !title.trim() || !body.trim()}
            className="flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground disabled:opacity-50 hover:opacity-90"
          >
            {busy && <Loader2 size={12} className="animate-spin" />} Post thread
          </button>
        </div>
      </div>
    </div>
  );
}
