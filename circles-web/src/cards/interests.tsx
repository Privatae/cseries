import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { listInterests, type RendezvousInterest, setInterests, withdrawInterest } from "@/lib/api";

// Moved here from Circles' Settings pane (settings-pane.tsx), whole, when the
// rail went: interests live on your card's front. It is the same editor: add (the POST is total),
// remove one, withdraw all, the "not listed yet" marks and why, the rules
// for a tag, and what was dropped.

// Interests — the host side of rendezvous. It lives in Settings rather than
// getting a rail entry of its own because it is a standing per-user preference
// with no inbox, no list to browse and nothing to answer: exactly the shape of
// every other section on this pane. Giving it a rail icon would also read as a
// place to BROWSE tags, which is the directory the whole feature exists to
// avoid needing.
//
// THERE IS NO CAP ON THE NUMBER OF TAGS, deliberately: a
// person's interests are theirs. What is bounded is one tag's LENGTH and its
// character set, and only because the tag becomes a community group slug on the
// platform — communityd's own validSlug rule, applied here as guidance rather
// than as a second normaliser.
//
// NORMALISATION IS THE SERVER'S, NOT THIS COMPONENT'S. One normaliser on the
// host is the design's core invariant — a tag that reads one way here and
// matches another way on the platform is invisible from both ends. So this
// posts what the user typed and renders back what the server stored.
export function Interests() {
  const [rows, setRows] = useState<RendezvousInterest[]>([]);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [dropped, setDropped] = useState<string[]>([]);

  const load = useCallback(async () => {
    const r = await listInterests();
    if (!r.ok) {
      setState("error");
      return;
    }
    setRows(r.data.interests ?? []);
    setState("ready");
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // The POST is TOTAL — it carries the whole list and the platform makes its
  // stored set equal to it (rendezvous.py:129-145). So adding is "post
  // everything plus the new one", never a delta.
  const post = async (tags: string[]) => {
    setBusy(true);
    setNote(null);
    setDropped([]);
    const r = await setInterests(tags);
    setBusy(false);
    if (!r.ok) {
      setNote(r.message || "Could not save those interests.");
      return false;
    }
    // Two different outcomes and only one of them is success. `listed` false
    // means the tags are on this Host but the platform does not have them —
    // the bridge was unreachable — and the next save is what sends them.
    setDropped(r.data.dropped ?? []);
    setNote(
      r.data.listed
        ? "Saved and listed with the platform."
        : r.data.error ||
            "Saved on your Host, but not listed with the platform yet.",
    );
    await load();
    return true;
  };

  const add = async () => {
    const typed = draft
      // Split on commas so a person can type several at once. This is
      // TOKENISING THE FIELD, not normalising a tag — whitespace, case and the
      // charset are all the server's to rule on, and anything it refuses comes
      // back in `dropped` below rather than being silently trimmed here.
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);
    if (typed.length === 0) return;
    const ok = await post([...rows.map((r) => r.tag), ...typed]);
    if (ok) setDraft("");
  };

  const remove = async (tag: string) => {
    setBusy(true);
    setNote(null);
    setDropped([]);
    const r = await withdrawInterest(tag);
    setBusy(false);
    if (!r.ok) {
      setNote(r.message || "Could not withdraw that interest.");
      return;
    }
    // `ok` here is the PLATFORM half. The local withdrawal has happened either
    // way, so this reports the difference instead of claiming both landed.
    setNote(
      r.data.ok
        ? `Withdrew ${tag}.`
        : `Withdrew ${tag} here, but the platform has not been told yet — it still has it until the next save.`,
    );
    await load();
  };

  const withdrawAll = async () => {
    setBusy(true);
    setNote(null);
    setDropped([]);
    const r = await withdrawInterest();
    setBusy(false);
    if (!r.ok) {
      setNote(r.message || "Could not withdraw your interests.");
      return;
    }
    setNote(
      r.data.ok
        ? "Withdrew every interest."
        : "Withdrew every interest here, but the platform has not been told yet.",
    );
    await load();
  };

  if (state === "loading") {
    return <p className="py-2 text-muted-foreground text-sm">Loading interests…</p>;
  }
  if (state === "error") {
    // Never "None listed" over a failed read: a Keep from an empty list
    // would post an empty list and withdraw every stored interest.
    return (
      <p className="py-2 text-muted-foreground text-sm" data-interests-error role="alert">
        Couldn’t load your interests.{" "}
        <button className="link" onClick={() => void load()} type="button">
          Try again
        </button>
      </p>
    );
  }

  const unlisted = rows.filter((r) => !r.listed_at);
  const errored = rows.find((r) => r.last_error)?.last_error ?? null;

  return (
    <div className="space-y-3">
      <p className="text-muted-foreground text-xs leading-relaxed">
        List an interest and the platform, once an hour, introduces you to a
        group for any tag two or more people listed. Only the tag list ever
        leaves your Host — never your memory, never a profile.
      </p>

      {rows.length === 0 ? (
        <p className="text-muted-foreground text-sm">
          No interests listed. Nothing about you is on the platform.
        </p>
      ) : (
        <div className="flex flex-wrap gap-2">
          {rows.map((r) => (
            <span
              className="flex items-center gap-2 rounded-full border border-border px-3 py-1 text-xs"
              key={r.tag}
            >
              <span className="text-foreground">{r.tag}</span>
              {!r.listed_at && (
                <span className="text-muted-foreground">not listed yet</span>
              )}
              <button
                aria-label={`Remove ${r.tag}`}
                className="text-muted-foreground hover:text-foreground"
                disabled={busy}
                onClick={() => remove(r.tag)}
                type="button"
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}

      {unlisted.length > 0 && (
        <p className="text-muted-foreground text-xs leading-relaxed">
          {unlisted.length === rows.length
            ? "Saved on your Host, but the platform does not have this list yet"
            : `${unlisted.length} of these are saved on your Host but not listed with the platform yet`}
          {errored ? ` — ${errored}` : "."} Nothing is lost: saving again sends
          the whole list.
        </p>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <input
          aria-label="Add interests, separated by commas"
          className="min-w-48 flex-1 rounded-md border border-border bg-card p-2 text-sm"
          data-interests-input
          disabled={busy}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              void add();
            }
          }}
          placeholder="bonsai, home-lab"
          value={draft}
        />
        <Button data-interests-add disabled={busy || draft.trim() === ""} onClick={add} size="sm">
          Add
        </Button>
        {rows.length > 0 && (
          <Button disabled={busy} onClick={withdrawAll} size="sm" variant="outline">
            Withdraw all
          </Button>
        )}
      </div>

      <p className="text-muted-foreground text-xs leading-relaxed">
        List as many as you like. Each tag becomes a community group’s name, so
        one tag is up to 64 characters of a-z, 0-9, “-” or “_” — spaces and
        capitals are folded into that, and anything left over is refused rather
        than guessed at.
      </p>

      {dropped.length > 0 && (
        <p className="text-foreground text-xs leading-relaxed">
          Not listed: <span className="font-medium">{dropped.join(", ")}</span>.
          A tag has to be 1-64 characters of a-z, 0-9, “-” or “_” once spaces
          and capitals are folded in, and these left nothing usable. The rest of
          your list was saved.
        </p>
      )}

      {note && <p className="text-muted-foreground text-xs">{note}</p>}
    </div>
  );
}
