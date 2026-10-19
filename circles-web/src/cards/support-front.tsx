import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { discardSupport, getSupport, listSupport, sendSupport, setSupportShared } from "@/lib/api";
import { clockOf, dayWord } from "@/lib/cards";
import type { InboxMessage, SharedItem, SupportTicket } from "@/lib/types";
import { TeamFace } from "./bits";

/** The card's views of your tickets: by default the open ones (drafts and
 * sent, filtered on the server so an old draft never falls off a newest-50
 * page, as the Tickets pane's Active did) and the answered ones; or one
 * state, the discarded included. */
const VIEWS = [
  { value: "", label: "Open and answered" },
  { value: "draft", label: "Waiting for you" },
  { value: "sent", label: "With the platform" },
  { value: "closed", label: "Closed" },
  { value: "discarded", label: "Discarded" },
];

/** A support ticket's state, in the card's words (mockup:703-709). */
function stateOf(t: SupportTicket): { label: string; done: boolean } {
  if (t.status === "draft") return { label: "Draft · not sent", done: false };
  if (t.status === "discarded") return { label: "Discarded", done: true };
  const ps = t.platform_status || "";
  if (t.status === "closed" || ps === "fixed" || ps === "rejected" || (t.resolution || "").trim()) {
    return { label: "Answered", done: true };
  }
  if (ps === "needs_info") return { label: "The platform's team asks you something", done: false };
  return { label: `Sent${ps ? ` · ${ps.replace(/_/g, " ")}` : ""}`, done: false };
}

/**
 * A draft waiting for your Send: what your Host wrote, each thing it would
 * share with a tick box, Send and Not now (server/routes/support.py: the
 * ticks are stored with POST /{id}/shared, then Send names the draft's
 * revision, so nothing unseen goes).
 */
function Draft({
  t,
  onSent,
  onToast,
  onOpen,
}: {
  t: SupportTicket;
  onSent: () => void;
  onToast: (s: string) => void;
  /** The ticket in full (the Tickets pane's SupportDetail). */
  onOpen: () => void;
}) {
  const [full, setFull] = useState<SupportTicket | null>(null);
  const [ticks, setTicks] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);
  const [aside, setAside] = useState(false);
  const [discarding, setDiscarding] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [readTick, setReadTick] = useState(0);
  useEffect(() => {
    let off = false;
    void getSupport(t.id).then((r) => {
      if (off || !r.ok) return;
      setFull(r.data);
      const next: Record<string, boolean> = {};
      for (const s of r.data.shared_context ?? []) next[s.key] = !!s.included;
      setTicks(next);
    });
    return () => {
      off = true;
    };
  }, [t.id, readTick]);
  const items: SharedItem[] = full?.shared_context ?? [];
  const send = async () => {
    if (!full || busy) return;
    setBusy(true);
    setErr(null);
    const s = await setSupportShared(t.id, ticks);
    if (!s.ok) {
      setBusy(false);
      setErr(`Couldn't keep your ticks: ${s.message}`);
      return;
    }
    const r = await sendSupport(t.id, full.revision ?? 0);
    setBusy(false);
    if (!r.ok) {
      if (r.status === 409) {
        // The Host rewrote the draft after it was read here: read it again
        // (its new words and items), so the next Send names the new one.
        setErr("Your Host changed this draft while you read it. Here it is again; read it, then send.");
        setReadTick((x) => x + 1);
        return;
      }
      setErr(r.message);
      return;
    }
    onToast("Sent. Only what you ticked went.");
    onSent();
  };
  if (aside) {
    return (
      <div className="slip" data-support-ticket={t.id}>
        <span className="st">Draft · set aside</span>
        <h4>{t.title}</h4>
        <div className="acts">
          <button className="btn quiet" onClick={() => setAside(false)} type="button">
            Open it again
          </button>
        </div>
      </div>
    );
  }
  return (
    <div className="slip" data-support-draft={t.id} data-support-ticket={t.id}>
      <span className="st">Draft · not sent</span>
      <h4>{t.title}</h4>
      <div className="body">
        {(full?.problem || t.problem || "").trim() || "Your Host wrote this up."} Untick anything you don't want to
        share, then send.
      </div>
      {full == null ? (
        <div className="body">Reading what it would share…</div>
      ) : (
        items.map((s) => (
          <div className="shareitem" key={s.key}>
            <label>
              <input
                checked={!!ticks[s.key]}
                data-share={s.key}
                onChange={(e) => setTicks((cur) => ({ ...cur, [s.key]: e.target.checked }))}
                type="checkbox"
              />
              <span>{s.label}</span>
            </label>
            {/* What exactly would go, before it goes (the Tickets pane showed
                each item's content and size). */}
            {s.content ? (
              <details data-share-content={s.key}>
                <summary>
                  What this shares · {s.content.length.toLocaleString()} characters
                </summary>
                <pre>{s.content}</pre>
              </details>
            ) : null}
          </div>
        ))
      )}
      {err && (
        <div className="err" role="alert">
          {err}
        </div>
      )}
      <div className="acts">
        <button
          className="btn primary"
          data-support-send
          disabled={busy || full == null}
          onClick={() => void send()}
          type="button"
        >
          {busy ? "Sending…" : "Send"}
        </button>
        <button className="btn quiet" onClick={() => setAside(true)} type="button">
          Not now
        </button>
        <button className="btn quiet" data-support-open={t.id} onClick={onOpen} type="button">
          Read in full
        </button>
        {discarding ? (
          <span className="row" data-support-discard-ask>
            <span className="small">Discard this draft? Nothing is sent.</span>
            <button
              className="btn"
              data-support-discard-yes
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                const r = await discardSupport(t.id);
                setBusy(false);
                if (!r.ok) {
                  setErr(`Couldn't discard it: ${r.message}`);
                  return;
                }
                onToast("Discarded. Nothing was sent.");
                onSent();
              }}
              type="button"
            >
              Discard
            </button>
            <button className="btn quiet" onClick={() => setDiscarding(false)} type="button">
              Keep it
            </button>
          </span>
        ) : (
          <button className="btn quiet" data-support-discard onClick={() => setDiscarding(true)} type="button">
            Discard
          </button>
        )}
      </div>
    </div>
  );
}

/**
 * Support's card, a front only (mockup:703-709, 729-732): your Host writes
 * the ticket; you choose what is shared and send it. The answered tickets,
 * the ones sent and waiting, and the drafts, a day at a time.
 */
export function SupportFront({
  hostName,
  focusId,
  refreshKey,
  notices,
  onChanged,
  onToast,
  onOpenTicket,
  onOpenNote,
}: {
  hostName: string;
  /** support:<id>: the ticket a link asked for. */
  focusId: number | null;
  refreshKey: number;
  /** Notices (inbox system_notification): the platform's, and your Host's
   * own weekly changelog (plan: "the platform's notices go on the Support
   * card"). */
  notices: readonly InboxMessage[];
  onChanged: () => void;
  onToast: (s: string) => void;
  /** A ticket in full, or (null) a new one. */
  onOpenTicket: (id: number | null) => void;
  /** A notice in full (reading it marks it read). */
  onOpenNote: (messageId: string) => void;
}) {
  const [rows, setRows] = useState<SupportTicket[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // The Tickets pane's support filter: active (draft and sent, filtered on
  // the server so an old draft never falls off a newest-50 page), waiting
  // for you, with the platform, closed, discarded. The list read also pulls
  // the platform's newest status for each sent ticket.
  const [filter, setFilter] = useState("");
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const load = useCallback(async () => {
    const rs = filter
      ? [await listSupport({ status: filter })]
      : await Promise.all([listSupport({ status: "active" }), listSupport({ status: "closed" })]);
    const bad = rs.find((r) => !r.ok);
    if (bad && !bad.ok) {
      setErr(bad.message);
      return;
    }
    setErr(null);
    const seen = new Set<number>();
    const all: SupportTicket[] = [];
    for (const r of rs)
      for (const t of r.ok ? (r.data.tickets ?? []) : [])
        if (!seen.has(t.id)) {
          seen.add(t.id);
          all.push(t);
        }
    setRows(all);
  }, [filter]);
  useEffect(() => {
    void load();
  }, [load, refreshKey]);
  // A link to one ticket keeps it in sight while what sits above it arrives
  // (the notices are read apart from the tickets), for a few seconds and only
  // until the person scrolls or touches the card themselves.
  const focusUntil = useRef(0);
  useEffect(() => {
    focusUntil.current = focusId == null ? 0 : Date.now() + 5000;
  }, [focusId]);
  useEffect(() => {
    const body = bodyRef.current;
    if (!body) return;
    const stop = () => {
      focusUntil.current = 0;
    };
    const kinds = ["wheel", "touchstart", "pointerdown", "keydown"] as const;
    for (const k of kinds) body.addEventListener(k, stop, { passive: true });
    return () => {
      for (const k of kinds) body.removeEventListener(k, stop);
    };
  }, []);
  useEffect(() => {
    if (focusId == null || rows == null || Date.now() > focusUntil.current) return;
    const el = bodyRef.current?.querySelector<HTMLElement>(`[data-support-ticket="${focusId}"]`);
    el?.scrollIntoView({ block: "center" });
  }, [focusId, rows, notices]);
  const ordered = [...(rows ?? [])].sort((a, b) => (a.opened_at < b.opened_at ? -1 : 1));
  let lastDay = "";
  return (
    <>
      <div className="chead">
        <span className="kind">● The platform's support team · people</span>
        <TeamFace size={52} />
        <div className="who">
          <h2>Support</h2>
          <div className="sub">{hostName} writes the ticket; you choose what's shared and send it</div>
          <div className="row" style={{ marginTop: 8 }}>
            <select
              aria-label="Which support tickets"
              className="turntype"
              data-support-filter
              onChange={(e) => setFilter(e.target.value)}
              value={filter}
            >
              {VIEWS.map((f) => (
                <option key={f.value} value={f.value}>
                  {f.label}
                </option>
              ))}
            </select>
            <button className="btn" data-support-new onClick={() => onOpenTicket(null)} type="button">
              New ticket
            </button>
          </div>
        </div>
      </div>
      <div className="cbody" data-support ref={bodyRef}>
        {notices.length > 0 && (
          <div data-notices>
            <div className="day">Notices</div>
            {notices.map((n) => (
              <button
                className="slip notice-slip"
                data-notice={n.message_id}
                data-unread={!n.read_at || undefined}
                key={n.message_id}
                onClick={() => onOpenNote(n.message_id)}
                type="button"
              >
                <span className="st">
                  {n.read_at ? "Notice" : "New notice"}
                  {n.sent_at ? ` · ${dayWord(n.sent_at)} ${clockOf(n.sent_at)}` : ""}
                </span>
                <h4>{n.subject || "A notice from the platform"}</h4>
              </button>
            ))}
          </div>
        )}
        {err && rows == null ? (
          <div className="blank" role="alert">
            Couldn’t read your tickets: {err}
          </div>
        ) : rows == null ? (
          <div className="blank">Reading…</div>
        ) : rows.length === 0 ? (
          <div className="blank">
            No tickets. When something goes wrong, ask {hostName}; it writes one here for you to send.
          </div>
        ) : (
          ordered.map((t) => {
            const day = dayWord(t.opened_at);
            const sep = day && day !== lastDay ? day : null;
            lastDay = day || lastDay;
            const st = stateOf(t);
            return (
              <Fragment key={t.id}>
                {sep && <div className="day">{sep}</div>}
                {t.status === "draft" ? (
                  <Draft
                    onOpen={() => onOpenTicket(t.id)}
                    onSent={() => {
                      void load();
                      onChanged();
                    }}
                    onToast={onToast}
                    t={t}
                  />
                ) : (
                  <div className="slip" data-support-ticket={t.id}>
                    <span className={`st${st.done ? " done" : ""}`}>{st.label}</span>
                    <h4>{t.title}</h4>
                    <div className="body">
                      {(t.resolution || "").trim() ||
                        (t.thread ?? []).filter((m) => m.author === "platform").slice(-1)[0]?.body ||
                        (t.status === "discarded" ? "Discarded; nothing was sent." : "The platform's team has it. Their answer lands here.")}
                    </div>
                    <div className="acts">
                      <button className="btn quiet" data-support-open={t.id} onClick={() => onOpenTicket(t.id)} type="button">
                        {st.label.startsWith("The platform's team asks") ? "Answer it" : "Read in full"}
                      </button>
                    </div>
                  </div>
                )}
              </Fragment>
            );
          })
        )}
      </div>
    </>
  );
}
