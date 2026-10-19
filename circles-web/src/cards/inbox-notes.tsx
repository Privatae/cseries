import { useCallback, useEffect, useState } from "react";
import { archiveInbox, listInbox, readInbox } from "@/lib/api";
import { clockOf, dayWord } from "@/lib/cards";
import type { InboxMessage } from "@/lib/types";
import { Sheet } from "./bits";

/** The inbox kinds the door does not take that a person still reads (plan,
 * "The inbox's other kinds": the platform's notices go on the Support card;
 * the machine records, grants, revokes, contracts, software and app delivery,
 * are not shown). An older ask of your Host's (the decisions inbox,
 * server/routes/decisions.py), its own decision for your review, and a
 * permission request go in Waiting on you. */
export const NOTICE_KIND = "system_notification";
export const ASK_KINDS = ["decision", "decision_review", "permission_request"] as const;

/** Those messages, not archived, newest first: one read per kind
 * (GET /api/inbox/list?kind=…). The list carries no body; opening one reads
 * it (GET /api/inbox/read/{id}, which marks it read). */
export function useInboxNotes() {
  const [notes, setNotes] = useState<InboxMessage[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const read = useCallback(async () => {
    const rs = await Promise.all([NOTICE_KIND, ...ASK_KINDS].map((kind) => listInbox({ kind, limit: 100 })));
    const bad = rs.find((r) => !r.ok);
    if (bad && !bad.ok) setErr(bad.message || "Couldn't read your messages.");
    else setErr(null);
    const all = rs.flatMap((r) => (r.ok ? (r.data.messages ?? []) : []));
    all.sort((a, b) => String(b.sent_at ?? "").localeCompare(String(a.sent_at ?? "")));
    setNotes(all);
  }, []);
  useEffect(() => {
    void read();
  }, [read]);
  return { notes, err, read };
}

function whenOf(iso?: string | null): string {
  return iso ? `${dayWord(iso)} ${clockOf(iso)}`.trim() : "";
}

/**
 * One of those messages in full, as the Inbox showed it: who it is from and
 * when, its subject and words (as text), everything recorded on it, and
 * Archive. Opening it marks it read.
 */
export function NoteSheet({
  id,
  hostName,
  onClose,
  onArchived,
  onTellHost,
}: {
  id: string;
  hostName: string;
  onClose: () => void;
  onArchived: () => void;
  /** An older ask is answered by telling your Host: the back opens on the
   * blank page, and the answer picks its topic. */
  onTellHost: () => void;
}) {
  const [m, setM] = useState<InboxMessage | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let off = false;
    void readInbox(id).then((r) => {
      if (off) return;
      if (r.ok) setM(r.data);
      else setErr(r.message || "Couldn't read the message.");
    });
    return () => {
      off = true;
    };
  }, [id]);
  const ask = !!m && (ASK_KINDS as readonly string[]).includes(m.kind);
  const payload = m?.payload ?? {};
  return (
    <Sheet data="note" onClose={onClose} title={m?.subject || (err ? "A message" : "Reading…")}>
      <div className="notebody" data-note={id} data-note-kind={m?.kind}>
        {err ? (
          <p className="err" role="alert">
            {err}
          </p>
        ) : !m ? (
          <p className="small">Reading…</p>
        ) : (
          <>
            <p className="small">
              {/* A notice is the platform's, or your Host's own (its weekly
                  changelog): the row does not say which, so neither does this. */}
              {m.kind === "system_notification"
                ? "A notice"
                : !m.from || /@host$/.test(m.from)
                  ? `From ${hostName}`
                  : `From ${m.from}`}
              {m.sent_at ? ` · ${whenOf(m.sent_at)}` : ""}
            </p>
            {/* Text, never markdown: a body can carry words another machine
                chose (a found Host's name, server/network/peer_discovery.py),
                and a link in them must never be live. The Inbox drew it so. */}
            {m.body ? (
              <div className="notetext" data-note-body>
                {m.body}
              </div>
            ) : null}
            {ask && (
              <p className="small">
                Answer it by telling {hostName} in Circles; {hostName} resolves it.
              </p>
            )}
            {Object.keys(payload).length > 0 && (
              <details>
                <summary className="small">Everything recorded on it</summary>
                <pre className="payload">{JSON.stringify(payload, null, 2)}</pre>
              </details>
            )}
            <span className="row">
              {ask && (
                <button className="btn primary" data-note-answer onClick={onTellHost} type="button">
                  Tell {hostName}
                </button>
              )}
              <button
                className="btn"
                data-note-archive
                disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  const r = await archiveInbox(id);
                  setBusy(false);
                  if (!r.ok) {
                    setErr(`Couldn't archive it: ${r.message}`);
                    return;
                  }
                  onArchived();
                  onClose();
                }}
                type="button"
              >
                Archive
              </button>
            </span>
          </>
        )}
      </div>
    </Sheet>
  );
}
