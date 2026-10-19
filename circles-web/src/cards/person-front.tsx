import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { sendPlainMessage } from "@/lib/api";
import { clearChat, fetchChat, markSeen, setReading } from "@/lib/cards-api";
import {
  askPicker,
  clockOf,
  dayWord,
  readingBanner,
  readingNotice,
  threadTitle,
  type Card,
  type ThreadRow,
} from "@/lib/cards";
import type { ThreadMessageRaw } from "@/lib/api";
import { SafeMarkdown } from "./guards";
import { Corner, Face, Pop } from "./bits";
import { PlainComposer } from "./composer";
import { Earlier, useEarlier } from "./use-earlier";
import { AskFirst, ContactActions, useCardContact } from "./contacts";

const CARRIED_KEY = "circles.cards.carried";

/** Messages given to your Host from a person's chat, remembered on this
 * device so their "given to …" note survives a reload. Only you see it; the
 * other person is never told (slice 4). */
function carriedIds(): Set<string> {
  try {
    const v = JSON.parse(window.localStorage.getItem(CARRIED_KEY) || "[]");
    return new Set(Array.isArray(v) ? v.map(String).slice(-500) : []);
  } catch {
    return new Set();
  }
}
function rememberCarried(id: string, keep = true): void {
  try {
    const s = carriedIds();
    if (keep) s.add(id);
    else s.delete(id);
    window.localStorage.setItem(CARRIED_KEY, JSON.stringify([...s].slice(-500)));
  } catch {
    /* private mode: for this page only */
  }
}

/**
 * A person's front (mockup:734-742): their one continuous chat, as bubbles,
 * a day at a time. Under each of their messages, "ask <Host>" hands that one
 * message to a page of your Host's that you pick (mismatch 13's default: the
 * open pages, last used first, no model call); nothing is sent to them. The
 * reading switch sits in the header (mismatch 4's default: under the name on
 * a phone), and while it is on a banner says so; the notices it writes into
 * the chat are drawn from their value (lib/cards.ts readingNotice).
 */
export function PersonFront({
  card,
  hostName,
  hue,
  phone,
  visible,
  threads,
  refreshKey,
  onTurnCorner,
  onGive,
  onToast,
  onRead,
}: {
  card: Card;
  hostName: string;
  hue: number;
  phone: boolean;
  visible: boolean;
  threads: readonly ThreadRow[];
  /** Bumped when a message from this person may have arrived. */
  refreshKey: number;
  onTurnCorner: () => void;
  /** Ask <Host>: give message `id` to page `threadId`. Null on success. */
  onGive: (threadId: string, messageId: string, quote: string) => Promise<string | null>;
  onToast: (s: string) => void;
  onRead: () => void;
}) {
  const email = card.email ?? "";
  const first = card.name.split(/\s+/)[0] || card.name;
  const [rows, setRows] = useState<ThreadMessageRaw[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [reads, setReads] = useState(false);
  const [readBusy, setReadBusy] = useState(false);
  const [carried, setCarried] = useState<Set<string>>(() => carriedIds());
  const [asking, setAsking] = useState<string | null>(null);
  const [fresh, setFresh] = useState<string | null>(null);
  const lastIdRef = useRef<string | null>(null);
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const pages = useEarlier("dm", email);
  const sawNewest = pages.sawNewest;
  const [menuOpen, setMenuOpen] = useState(false);
  const cc = useCardContact(card.contactId, menuOpen);

  const load = useCallback(async () => {
    if (!email) return;
    const r = await fetchChat("dm", email);
    if (!r.ok) {
      setErr(r.message);
      return;
    }
    setErr(null);
    const msgs = r.data.messages ?? [];
    const newest = msgs[msgs.length - 1];
    if (lastIdRef.current && newest?.id && newest.id !== lastIdRef.current && newest.author_kind !== "self") {
      setFresh(String(newest.id));
    }
    lastIdRef.current = newest?.id ? String(newest.id) : null;
    setRows(msgs);
    sawNewest(r.data);
    setReads(!!r.data.policy?.host_reads);
  }, [email, sawNewest]);

  useEffect(() => {
    setRows(null);
    lastIdRef.current = null;
    void load();
  }, [load]);
  useEffect(() => {
    if (refreshKey) void load();
  }, [refreshKey, load]);

  // The seen mark: the person has this chat in front.
  const onReadRef = useRef(onRead);
  onReadRef.current = onRead;
  const newestId = rows?.[rows.length - 1]?.id;
  const loaded = rows != null;
  useEffect(() => {
    if (!visible || !email || !loaded) return;
    void markSeen("dm", email).then(() => onReadRef.current());
  }, [visible, email, newestId, loaded]);

  useEffect(() => {
    const b = bodyRef.current;
    if (b) b.scrollTop = b.scrollHeight;
  }, [newestId, visible, loaded]);

  const flipReading = async () => {
    if (readBusy) return;
    setReadBusy(true);
    const r = await setReading(email, !reads);
    setReadBusy(false);
    if (!r.ok) {
      onToast(r.message || "That didn't change.");
      return;
    }
    setReads(r.data.on);
    void load();
  };

  const give = async (threadId: string, m: ThreadMessageRaw) => {
    setAsking(null);
    const id = String(m.id ?? "");
    const t = threads.find((x) => x.id === threadId);
    onToast(`Given to ${hostName}, on ${t ? threadTitle(t) : "a thread"}. ${first} isn't told.`);
    rememberCarried(id);
    setCarried(carriedIds());
    const e = await onGive(threadId, id, m.body ?? "");
    if (e) {
      // Not given after all: the note goes, and the person is told why.
      rememberCarried(id, false);
      setCarried(carriedIds());
      onToast(e);
    }
  };

  const picker = askPicker(threads);
  const all = rows == null ? null : pages.merge(rows);
  let lastDay = "";
  const readChip = (
    <button
      aria-pressed={reads}
      className="chip readchip"
      data-reading={reads ? "on" : "off"}
      disabled={readBusy || card.pending || card.via == null}
      onClick={() => void flipReading()}
      style={phone ? undefined : { marginRight: 76 }}
      title={
        reads
          ? `Stop ${hostName} reading this chat; ${first} is told`
          : `Let ${hostName} read this chat; ${first} is told`
      }
      type="button"
    >
      {reads ? `${hostName} can read this card` : `${hostName} can't read this card`}
    </button>
  );
  return (
    <>
      <Corner
        face={card.hostName ? <Face host hue={hue} name={card.hostName} size={16} /> : undefined}
        label={card.blankBack ? "No Host" : (card.hostName ?? "Their Host")}
        onTurn={onTurnCorner}
        turnTo="back"
      />
      <div className="chead">
        <span className="kind">● The front · a person</span>
        <Face big hue={hue} name={card.name} size={52} />
        <div className="who">
          <h2>{card.name}</h2>
          <div className="sub">
            a person · {email} ·{" "}
            <span style={{ position: "relative" }}>
              <button
                aria-expanded={menuOpen}
                className="link"
                data-contact-menu
                onClick={() => setMenuOpen((v) => !v)}
                type="button"
              >
                Contact ▾
              </button>
              {menuOpen && (
                <Pop label={`${card.name}, your contact`} onClose={() => setMenuOpen(false)} style={{ top: 22, left: 0 }}>
                  <h4>{card.name}</h4>
                  {cc.contact ? (
                    <ContactActions
                      all={cc.all}
                      contact={cc.contact}
                      extra={
                        <AskFirst
                          data="clear-dm"
                          label="Clear this chat"
                          onYes={async () => {
                            const r = await clearChat("dm", email);
                            if (!r.ok) return `Couldn't clear the chat: ${r.message}`;
                            onToast(`Your chat with ${first} is cleared on this Host.`);
                            setMenuOpen(false);
                            void load();
                            onRead();
                            return null;
                          }}
                          question={`Clear your chat with ${first}? It is archived on your Host, not erased, and ${first} keeps theirs.`}
                          yes="Clear"
                        />
                      }
                      onChanged={() => {
                        void cc.read();
                        onRead();
                      }}
                      onDone={() => setMenuOpen(false)}
                      onToast={onToast}
                    />
                  ) : (
                    <p>{cc.err ? cc.err : card.contactId == null ? `${first} isn't one of your contacts.` : "Reading…"}</p>
                  )}
                </Pop>
              )}
            </span>
          </div>
          {phone && <div style={{ marginTop: 8 }}>{readChip}</div>}
        </div>
        {!phone && readChip}
      </div>
      {reads && (
        <div className="banner" data-reading-banner>
          <Face host hue={0} name={hostName} size={18} /> {readingBanner(hostName, first)}
        </div>
      )}
      <div className="cbody" data-person-chat={email} ref={bodyRef}>
        {err && rows == null ? (
          <div className="blank" role="alert">
            Couldn’t read this chat: {err}{" "}
            <button className="link" data-load-retry onClick={() => void load()} type="button">
              Retry
            </button>
          </div>
        ) : rows == null ? (
          <div className="blank">Reading…</div>
        ) : all == null || all.length === 0 ? (
          <div className="blank">
            {card.pending
              ? `You asked to connect with ${first}. Once they accept, this is your chat.`
              : `Nothing yet. Write to ${first} below.`}
          </div>
        ) : (
          <>
          <Earlier pages={pages} shown={all.length} />
          {all.map((m, i) => {
            const mine = m.author_kind === "self";
            const day = dayWord(m.created_at);
            const sep = day && day !== lastDay ? day : null;
            lastDay = day || lastDay;
            const id = String(m.id ?? i);
            const notice = readingNotice(m.reading_notice, mine, hostName, first);
            if (notice) {
              return (
                <Fragment key={id}>
                  {sep && <div className="day">{sep}</div>}
                  <div className="notice" data-reading-notice={m.reading_notice}>
                    {notice}
                  </div>
                </Fragment>
              );
            }
            return (
              <Fragment key={id}>
                {sep && <div className="day">{sep}</div>}
                <div className="msg" data-chat-msg={id}>
                  <div className={`pb md ${mine ? "me" : "them"}${fresh === id ? " fresh" : ""}`}>
                    <SafeMarkdown text={m.body ?? ""} />
                  </div>
                  {carried.has(id) ? (
                    <div className="pt" data-given>
                      ↳ given to {hostName} · only you see this
                    </div>
                  ) : (
                    <div className={`pt${mine ? " me" : ""}`} style={{ position: "relative" }}>
                      {clockOf(m.created_at)}
                      {!mine && (
                        <button
                          aria-expanded={asking === id}
                          className="askivy"
                          data-ask-host={id}
                          onClick={() => setAsking((cur) => (cur === id ? null : id))}
                          type="button"
                        >
                          <Face host hue={0} name={hostName} size={13} /> ask {hostName}
                        </button>
                      )}
                      {asking === id && (
                        <Pop
                          label={`Give this to ${hostName}`}
                          onClose={() => setAsking(null)}
                          style={{ top: 22, left: 0 }}
                        >
                          <h4>Give it to {hostName}, on</h4>
                          {picker.length === 0 ? (
                            <p style={{ margin: 0 }}>{hostName}'s pages are still loading.</p>
                          ) : (
                            <ul data-ask-picker>
                              {picker.slice(0, 8).map((t) => (
                                <li key={t.id}>
                                  <button data-give-to={t.id} onClick={() => void give(t.id, m)} type="button">
                                    {threadTitle(t)}
                                  </button>
                                </li>
                              ))}
                            </ul>
                          )}
                          <p style={{ margin: "10px 0 0", fontSize: 12, color: "var(--ink3)" }}>
                            Only you see this. {first} isn't told.
                          </p>
                        </Pop>
                      )}
                    </div>
                  )}
                </div>
              </Fragment>
            );
          })}
          </>
        )}
      </div>
      <PlainComposer
        disabled={card.pending || card.via == null}
        onSend={async (text) => {
          const r = await sendPlainMessage({ kind: "dm", id: email }, text);
          if (!r.ok) return r.message || "Couldn’t send.";
          void load();
          return null;
        }}
        placeholder={`Message ${first}`}
        round
        why={
          card.pending
            ? `${first} hasn't accepted yet.`
            : card.via == null
              ? `${first} can't be messaged yet: no Host of theirs has carried a message to you.`
              : null
        }
      />
    </>
  );
}
