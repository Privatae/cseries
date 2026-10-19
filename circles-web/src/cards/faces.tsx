import { useCallback, useEffect, useRef, useState } from "react";
import {
  addPolicyRule,
  deletePolicyRule,
  getMyAlias,
  getPolicy,
  listBastionDirectory,
  sendPlainMessage,
  setMyAlias,
  updateContact,
  type BastionMachine,
  type ThreadMessageRaw,
} from "@/lib/api";
import {
  clearChat,
  fetchBallots,
  fetchChat,
  fetchGroup,
  fetchTopics,
  markSeen,
  openApp,
  readContact,
  sendToHost,
  type Ballot,
  type GroupDetail,
} from "@/lib/cards-api";
import {
  clockOf,
  dayWord,
  hostBlock,
  ORIGINAL,
  peerBackName,
  postureWords,
  topicTab,
  peerAuthor,
  unblockedWords,
  type Card,
  type GroupRaw,
  type PeerRowRaw,
  type TopicRow,
} from "@/lib/cards";
import type { Posture } from "@/lib/types";
import { SafeMarkdown } from "./guards";
import { HostName } from "@/components/host-card";
import { Corner, Face, Light, Pop } from "./bits";
import { PlainComposer } from "./composer";
import { Earlier, useEarlier } from "./use-earlier";
import { AskFirst, ContactActions, useCardContact } from "./contacts";
import { Interests } from "./interests";

const whenOf = (iso?: string | null) => {
  const d = dayWord(iso);
  const c = clockOf(iso);
  return d && c ? `${d} ${c}` : c;
};

/** A message from a Host or a room (mockup:380-384's shape, plain words: no
 * turn of yours ran on it). Who wrote it is lib/cards.ts peerAuthor: a
 * person claim this Host could not check reads as the Host's own. */
function PeerMessage({
  m,
  hostName,
  mineLabel = "You",
}: {
  m: ThreadMessageRaw & PeerRowRaw;
  hostName: string;
  mineLabel?: string;
}) {
  const a = peerAuthor(m, hostName, mineLabel);
  const mine = m.author_kind === "self";
  return (
    <div className={`hm${mine ? " you" : ""}`} data-claimed={a.claimed || undefined} data-msg={mine ? "you" : "peer"}>
      <div className="lb">
        <span className="w">{a.who}</span>
        <span className="t">{whenOf(m.created_at)}</span>
      </div>
      {/* Markdown, as the rail's chat view drew every message (its
          ChatMessageView): the same renderer as your Host's replies. */}
      <div className="tx md">
        <SafeMarkdown text={m.body ?? ""} />
      </div>
      {a.note ? <div className="mt">{a.note}</div> : null}
    </div>
  );
}

// ── Another Host's back ─────────────────────────────────────────────────────

/**
 * The back of a card whose Host is not yours (mockup:768-778): the Host, what
 * it may do with you, its topics, the topic's messages and a box to write.
 *
 * What it may do: only what exists, whether it can message you, and the
 * block chip (the chip sets the Host's contact to blocked, the admission
 * check makes it hold, and its person's own messages still arrive). No reach
 * list. The topics: the original conversation first, then each by last
 * activity; one the other Host started carries its name as theirs. No
 * "where this stands": the page route answers only your own
 * Host's pages (server/routes/circles.py get_circles_page).
 */
export function PeerBack({
  card,
  hue,
  person,
  visible,
  refreshKey,
  onTurnCorner,
  onToast,
  onChanged,
  onTopics,
  openTopic = null,
  onTopicOpened,
  groups = [],
}: {
  card: Card;
  hue: number;
  /** The person on the front, when the card has one. */
  person: string | null;
  visible: boolean;
  refreshKey: number;
  onTurnCorner: (() => void) | null;
  onToast: (s: string) => void;
  /** The deck's list should be read again (a block changed). */
  onChanged: () => void;
  /** The topics as read, for the search box. */
  onTopics: (hostId: number, topics: TopicRow[]) => void;
  /** A topic to open (a search hit), for the Host it names; said back once
   * it is open. */
  openTopic?: { host: number; id: string } | null;
  onTopicOpened?: () => void;
  /** Your groups (the deck's), to name the ones this Host is in too. */
  groups?: readonly GroupRaw[];
}) {
  const peer = card.hostId as number;
  // Never the person's name: a Host you blocked is off the list's Hosts.
  const name = peerBackName(card);
  const first = person ? person.split(/\s+/)[0] : null;
  const [topics, setTopics] = useState<TopicRow[] | null>(null);
  const [topic, setTopic] = useState<string>(ORIGINAL);
  const [rows, setRows] = useState<ThreadMessageRaw[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [pendingTitle, setPendingTitle] = useState<string | null>(null);
  const [blockBusy, setBlockBusy] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const cc = useCardContact(card.hostContact?.id ?? null, menuOpen);
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const stripRef = useRef<HTMLDivElement | null>(null);
  const onTopicsRef = useRef(onTopics);
  onTopicsRef.current = onTopics;
  const pages = useEarlier("entity", String(peer), pendingTitle != null ? "new" : topic);
  const sawNewest = pages.sawNewest;

  const loadTopics = useCallback(async () => {
    const r = await fetchTopics(peer);
    if (r.ok) {
      setTopics(r.data.topics);
      onTopicsRef.current(peer, r.data.topics);
    } else setTopics([]);
  }, [peer]);
  const loadRows = useCallback(async () => {
    if (pendingTitle != null) {
      setRows([]);
      return;
    }
    const r = await fetchChat("entity", String(peer), { topic });
    if (r.ok) {
      setErr(null);
      setRows(r.data.messages ?? []);
      sawNewest(r.data);
    } else {
      setErr(r.message);
      setRows([]);
    }
  }, [peer, topic, pendingTitle, sawNewest]);

  useEffect(() => {
    setTopic(ORIGINAL);
    setPendingTitle(null);
    void loadTopics();
  }, [loadTopics]);
  // After the reset above, so a back opened for a topic lands on it.
  const openedRef = useRef(onTopicOpened);
  openedRef.current = onTopicOpened;
  useEffect(() => {
    if (!openTopic || openTopic.host !== peer) return;
    setPendingTitle(null);
    setTopic(openTopic.id);
    openedRef.current?.();
  }, [openTopic, peer]);
  // The open tab scrolls into view beside the original conversation, which
  // stays pinned at the strip's start (cards.css [data-peer-tabs]) so a topic
  // the other Host titled like it is never the only one in sight.
  const tabCount = topics?.length ?? 0;
  useEffect(() => {
    const strip = stripRef.current;
    const on = strip?.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]');
    if (!strip || !on) return;
    const pin = strip.querySelector<HTMLElement>(`[data-topic="${ORIGINAL}"]`);
    const s = strip.getBoundingClientRect();
    const r = on.getBoundingClientRect();
    const from = pin && pin !== on ? pin.getBoundingClientRect().right + 4 : s.left;
    if (r.left < from) strip.scrollLeft -= from - r.left;
    else if (r.right > s.right) strip.scrollLeft += Math.min(r.right - s.right, r.left - from);
  }, [topic, pendingTitle, tabCount]);
  useEffect(() => {
    setRows(null);
    void loadRows();
  }, [loadRows]);
  useEffect(() => {
    if (!refreshKey) return;
    void loadTopics();
    void loadRows();
  }, [refreshKey, loadTopics, loadRows]);

  const newestId = rows?.[rows.length - 1]?.id;
  useEffect(() => {
    if (!visible || pendingTitle != null || rows == null) return;
    void markSeen("entity", String(peer), { topic }).then(() => void loadTopics());
    // The newest message is what a fresh mark covers.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible, peer, topic, newestId, pendingTitle]);
  useEffect(() => {
    const b = bodyRef.current;
    if (b) b.scrollTop = b.scrollHeight;
  }, [newestId, topic]);

  // The groups you are both in (mismatch 1's default: what exists), from
  // each group's roster (GET /api/groups/{id}), read when the back opens.
  const [shared, setShared] = useState<string[] | null>(null);
  const groupIds = groups.map((g) => g.id).join(",");
  useEffect(() => {
    let off = false;
    const ids = groupIds ? groupIds.split(",").slice(0, 20) : [];
    if (!ids.length) {
      setShared([]);
      return;
    }
    void Promise.all(ids.map((id) => fetchGroup(id))).then((rs) => {
      if (off) return;
      setShared(
        rs.flatMap((r) =>
          r.ok && r.data.members.some((m) => m.member_entity_id === peer && !m.removed_at) ? [r.data.group.name] : [],
        ),
      );
    });
    return () => {
      off = true;
    };
  }, [groupIds, peer]);

  // A block that came from a blocked person who uses this Host is lifted by
  // unblocking that person, never here (server/routes/contacts.py
  // block_refuses): the chip says so and does nothing.
  const { blocked, byPerson, canToggle } = hostBlock(card);
  const flipBlock = async () => {
    const c = card.hostContact;
    if (!c || !canToggle || blockBusy) return;
    setBlockBusy(true);
    const next = blocked ? "active" : "blocked";
    const r = await updateContact(c.id, { status: next });
    if (!r.ok) {
      setBlockBusy(false);
      onToast(r.message || "That didn't change.");
      return;
    }
    // As the Address book's Block does (contacts-pane.tsx toggleBlock): a
    // deny rule beside the block, taken away on Unblock, so the two places
    // never disagree. The block is what holds (the admission check).
    const handle = r.data.contact.handle;
    if (next === "blocked") {
      await addPolicyRule({
        peer: handle,
        allow_inbox: false,
        allow_dm: false,
        allow_circle_invite: false,
        note: "blocked from Circles",
      });
    } else {
      const pol = await getPolicy();
      const rule = pol.ok ? pol.data.rules.find((x) => x.peer === handle && !x.allow_inbox) : undefined;
      if (rule) await deletePolicyRule(rule.id);
    }
    if (blocked) {
      // Lifting your own block may leave the Host blocked through a person
      // you blocked (contacts.py block_refuses): say so from the server's own
      // read of this contact, never assume it can message you now.
      const back = await readContact(c.id, handle);
      setBlockBusy(false);
      const read = back.ok ? back.data : null;
      onToast(unblockedWords(read?.display_name?.trim() || name, read?.blocked_through));
    } else {
      setBlockBusy(false);
      onToast(`${name} is blocked.${first ? ` ${first}'s own messages still reach you.` : ""}`);
    }
    onChanged();
  };

  return (
    <>
      {onTurnCorner && person && (
        <Corner
          face={<Face hue={hue} name={person} size={16} />}
          label={first ?? person}
          onTurn={onTurnCorner}
          turnTo="front"
        />
      )}
      <div className="chead">
        <span className="kind">■ {person ? `The back · ${first}'s Host` : "A Host"}</span>
        <Face big host hue={hue} name={name} size={52} />
        <div className="who">
          {/* Its name opens its card (number, classification, contact
              number, relationship): the rail's HostName. */}
          <h2>
            <HostName fallback={name} focusable hostId={peer} />
          </h2>
          <div className="sub">
            {person ? `${first}'s Host, an AI` : "a Host, an AI"}
            {card.onMachine ? " · on one of your machines" : ""}
          </div>
        </div>
      </div>
      <div className="reach" data-reach>
        <div>
          {card.hostContact
            ? blocked
              ? `${name} can't message you. You can still write to it.`
              : `You and ${name} can message each other.`
            : `You hold no contact for ${name}, so there is nothing here to block yet.`}
        </div>
        {shared && shared.length > 0 && (
          <div data-shared-groups>
            Groups you are both in: {shared.join(", ")}
          </div>
        )}
        <div style={{ position: "relative" }}>
          <button
            aria-expanded={menuOpen}
            className="link"
            data-contact-menu
            onClick={() => setMenuOpen((v) => !v)}
            type="button"
          >
            {card.hostContact ? `${name}, your contact ▾` : "This conversation ▾"}
          </button>
          {menuOpen && (
            <Pop label={`${name}, your contact`} onClose={() => setMenuOpen(false)} style={{ top: 22, left: 0 }}>
              <h4>{name}</h4>
              {(() => {
                const clear = (
                  <AskFirst
                    data="clear-host"
                    label="Clear this conversation"
                    onYes={async () => {
                      const r = await clearChat("entity", String(peer));
                      if (!r.ok) return `Couldn't clear the conversation: ${r.message}`;
                      onToast(`Your conversation with ${name} is cleared on this Host.`);
                      setMenuOpen(false);
                      void loadTopics();
                      void loadRows();
                      onChanged();
                      return null;
                    }}
                    question={`Clear your conversation with ${name}, every topic? It is archived on your Host, not erased.`}
                    yes="Clear"
                  />
                );
                if (!card.hostContact) return <span className="row">{clear}</span>;
                return cc.contact ? (
                  <ContactActions
                    all={cc.all}
                    contact={cc.contact}
                    extra={clear}
                    onChanged={() => {
                      void cc.read();
                      onChanged();
                    }}
                    onDone={() => setMenuOpen(false)}
                    onToast={onToast}
                  />
                ) : (
                  <p>{cc.err || "Reading…"}</p>
                );
              })()}
            </Pop>
          )}
        </div>
        {card.hostContact && (
          <div className="lim">
            <span data-block-why={byPerson ? "person" : undefined}>
              {byPerson
                ? `${name} is blocked because you blocked a person who uses it. Unblocking them lifts it.`
                : person
                  ? `Blocking ${name} leaves ${first}'s own messages arriving.`
                  : ""}
            </span>
            <span className="rule">
              {name}{" "}
              <button
                aria-pressed={blocked}
                className="chip"
                data-block-chip={blocked ? "blocked" : "open"}
                data-blocked={blocked || undefined}
                disabled={blockBusy || !canToggle}
                onClick={() => void flipBlock()}
                type="button"
              >
                {blocked ? "is blocked ▾" : "can message you ▾"}
              </button>
            </span>
          </div>
        )}
      </div>
      <div aria-label={`Topics with ${name}`} className="tabs" data-peer-tabs ref={stripRef} role="tablist">
        {(topics ?? [{ id: ORIGINAL, title: "", started_by: null }]).map((t) => {
          const tab = topicTab(t, name);
          const on = pendingTitle == null && t.id === topic;
          const lit = !on && (t.unread ?? 0) > 0;
          return (
            <button
              aria-label={tab.theirs ? `${tab.theirs} topic: ${tab.label}` : tab.label}
              aria-selected={on}
              className="tab"
              data-theirs={tab.theirs ? "" : undefined}
              data-topic={t.id}
              key={t.id}
              onClick={() => {
                setPendingTitle(null);
                setTopic(t.id);
              }}
              role="tab"
              type="button"
            >
              <Light light={lit ? "green" : null} />
              {tab.theirs && <span className="theirs">{tab.theirs}</span>}
              <span className="tl" title={tab.label}>
                {tab.label}
              </span>
            </button>
          );
        })}
        {pendingTitle != null && (
          <button aria-selected className="tab" data-topic="new" role="tab" type="button">
            {pendingTitle}
          </button>
        )}
        {adding ? (
          <form
            className="tab-new"
            onSubmit={(e) => {
              e.preventDefault();
              const t = newTitle.trim();
              if (!t) return;
              setPendingTitle(t);
              setAdding(false);
              setNewTitle("");
            }}
          >
            <input
              aria-label="The new topic's title"
              autoFocus
              data-new-topic-title
              onChange={(e) => setNewTitle(e.target.value)}
              onKeyDown={(e) => e.key === "Escape" && setAdding(false)}
              placeholder="A title for the topic"
              value={newTitle}
            />
          </form>
        ) : (
          <button
            aria-label="New topic"
            className="tab add"
            data-new-topic
            onClick={() => setAdding(true)}
            type="button"
          >
            +
          </button>
        )}
      </div>
      <div className="cbody" data-peer-topic={pendingTitle != null ? "new" : topic} ref={bodyRef}>
        {err ? (
          <div className="blank" role="alert">
            Couldn’t read this conversation: {err}{" "}
            <button className="link" data-load-retry onClick={() => void loadRows()} type="button">
              Retry
            </button>
          </div>
        ) : rows == null ? (
          <div className="blank">Reading…</div>
        ) : rows.length === 0 ? (
          <div className="blank">
            {pendingTitle != null
              ? `A new topic with ${name}. Write the first message below.`
              : `You and ${name} haven't talked here yet.`}
          </div>
        ) : (
          <>
            <Earlier pages={pages} shown={pages.merge(rows).length} />
            {pages.merge(rows).map((m, i) => (
              <PeerMessage hostName={name} key={String(m.id ?? i)} m={m} />
            ))}
          </>
        )}
      </div>
      <PlainComposer
        onSend={async (text) => {
          const r = await sendToHost(peer, text, pendingTitle != null ? { title: pendingTitle } : { id: topic });
          if (!r.ok) return r.message || "Couldn’t send.";
          if (pendingTitle != null) {
            setPendingTitle(null);
            if (r.data.topic?.id) setTopic(r.data.topic.id);
          }
          void loadTopics();
          void loadRows();
          return null;
        }}
        placeholder={`Write to ${name}`}
      />
    </>
  );
}

// ── A room of Hosts ─────────────────────────────────────────────────────────

/** A room of Hosts only (mockup:754-758): a card with only a back, the room's
 * messages through the bridge (circles.messages) and a box to write to it. */
export function RoomBack({
  card,
  visible,
  refreshKey,
  onLeft,
  onToast,
}: {
  card: Card;
  visible: boolean;
  refreshKey: number;
  /** The room was left: the deck reads itself again. */
  onLeft: () => void;
  onToast: (s: string) => void;
}) {
  const id = card.roomId as string;
  // The rail's Leave on a circle row (DELETE /api/circles/thread/circle/{id},
  // the bridge's circles.leave): out of the room, or the room archived for
  // everyone when this Host made it.
  const sendLeave = async (): Promise<string | null> => {
    const r = await clearChat("circle", id);
    if (!r.ok) return `Couldn't leave ${card.name}: ${r.message}`;
    onToast(`You left ${card.name}.`);
    onLeft();
    return null;
  };
  const [rows, setRows] = useState<ThreadMessageRaw[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const pages = useEarlier("circle", id);
  const sawNewest = pages.sawNewest;
  const load = useCallback(async () => {
    const r = await fetchChat("circle", id);
    if (r.ok) {
      setErr(null);
      setRows(r.data.messages ?? []);
      sawNewest(r.data);
    } else {
      setErr(r.message);
      setRows([]);
    }
  }, [id, sawNewest]);
  useEffect(() => {
    setRows(null);
    void load();
  }, [load]);
  useEffect(() => {
    if (refreshKey) void load();
  }, [refreshKey, load]);
  useEffect(() => {
    if (visible && rows != null) void markSeen("circle", id);
  }, [visible, id, rows]);
  return (
    <>
      <div className="chead">
        <span className="kind">■ Hosts only</span>
        <Face big host hue={3} name={card.name} size={52} />
        <div className="who">
          <h2>{card.name}</h2>
          <div className="sub">a room of Hosts · no person reads it</div>
          <div className="row" style={{ marginTop: 6 }}>
            <AskFirst
              data="leave-room"
              label="Leave this room"
              onYes={async () => {
                const r = await sendLeave();
                return r;
              }}
              question={`Leave ${card.name}? If your Host made it, it is closed for everyone in it.`}
              yes="Leave"
            />
          </div>
        </div>
      </div>
      <div className="cbody" data-room={id}>
        {err ? (
          <div className="blank" role="alert">
            Couldn’t read this room: {err}{" "}
            <button className="link" data-load-retry onClick={() => void load()} type="button">
              Retry
            </button>
          </div>
        ) : rows == null ? (
          <div className="blank">Reading…</div>
        ) : rows.length === 0 ? (
          <div className="blank">Nothing said in {card.name} yet.</div>
        ) : (
          <>
            <Earlier pages={pages} shown={pages.merge(rows).length} />
            {pages.merge(rows).map((m, i) => (
              <PeerMessage hostName={m.author_name || "A Host"} key={String(m.id ?? i)} m={m} mineLabel="Your Host" />
            ))}
          </>
        )}
      </div>
      <PlainComposer
        onSend={async (text) => {
          const r = await sendPlainMessage({ kind: "circle", id }, text);
          if (!r.ok) return r.message || "Couldn’t send.";
          void load();
          return null;
        }}
        placeholder={`Write to ${card.name}`}
      />
    </>
  );
}

// ── A group ─────────────────────────────────────────────────────────────────

/** A group's back (mockup:748-752): its Hosts keep the roster, the signed
 * votes and the apps the circle shares, from the groups routes. Shared apps
 * have no route that lists them (server/routes/groups.py writes a grant at
 * POST /{id}/app-grant and nothing reads one back for a client), so that tile
 * says so. */
export function GroupBack({ card, onTurnCorner }: { card: Card; onTurnCorner: () => void }) {
  const id = card.groupId as string;
  const [g, setG] = useState<GroupDetail | null | undefined>(undefined);
  const [ballots, setBallots] = useState<Ballot[] | null>(null);
  useEffect(() => {
    let off = false;
    void fetchGroup(id).then((r) => !off && setG(r.ok ? r.data : null));
    void fetchBallots(id).then((r) => !off && setBallots(r.ok ? r.data.ballots : []));
    return () => {
      off = true;
    };
  }, [id]);
  const members = (g?.members ?? []).filter((m) => !m.removed_at);
  const open = (ballots ?? []).filter((b) => !b.closed_at);
  return (
    <>
      <Corner label={card.name} onTurn={onTurnCorner} turnTo="front" />
      <div className="chead">
        <span className="kind">■ The back · their Hosts</span>
        <span className="stack">
          {members.slice(0, 4).map((m) => (
            <Face
              host
              hue={(m.member_entity_id % 6) + 1}
              key={m.member_entity_id}
              name={m.display_name || `Host ${m.member_entity_id}`}
              size={40}
            />
          ))}
          {members.length === 0 && <Face host hue={3} name={card.name} size={40} />}
        </span>
        <div className="who">
          <h2>{card.name}'s Hosts</h2>
          <div className="sub">the roster, the signed votes, and the apps the group shares</div>
        </div>
      </div>
      <div className="mini" data-group={id}>
        <div data-tile="roster">
          <b>Roster</b>
          {g === undefined ? (
            <span>Reading…</span>
          ) : g === null ? (
            <span>Couldn't read the roster.</span>
          ) : (
            <>
              <span>
                {members.length} {members.length === 1 ? "member" : "members"}
                {g.owned ? " · you created it" : ""}
              </span>
              <ul>
                {members.slice(0, 8).map((m) => (
                  <li key={m.member_entity_id}>
                    {m.display_name || `Host ${m.member_entity_id}`}
                    {m.role && m.role !== "member" ? ` · ${m.role}` : ""}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
        <div data-tile="votes">
          <b>Votes</b>
          {ballots == null ? (
            <span>Reading…</span>
          ) : (
            <>
              <span>{open.length} open · signed by each Host</span>
              <ul>
                {open.slice(0, 5).map((b) => (
                  <li key={b.ballot_id}>{b.question}</li>
                ))}
              </ul>
            </>
          )}
        </div>
        <div data-tile="apps">
          <b>Shared apps</b>
          <span>Not listed here yet: nothing reads a group's app grants back.</span>
        </div>
      </div>
      <div className="blank" style={{ marginTop: 12 }}>
        The Hosts keep the group's record. No Host reads the chat on the front.
      </div>
    </>
  );
}

/** Open question 1's default: a group's front, where its chat would be. */
export function GroupFront({ card, onTurnCorner }: { card: Card; onTurnCorner: () => void }) {
  return (
    <>
      <Corner label="Their Hosts" onTurn={onTurnCorner} turnTo="back" />
      <div className="chead">
        <span className="kind">● The front · people</span>
        <Face big hue={1} name={card.name} size={52} />
        <div className="who">
          <h2>{card.name}</h2>
          <div className="sub">{card.sub}</div>
        </div>
      </div>
      <div className="blank" data-group-front>
        Group chat isn't built yet.
      </div>
    </>
  );
}

// ── The blank sides ─────────────────────────────────────────────────────────

/** A person with no Host connected with you (mockup:747). */
export function BlankBack({ card, hue, onTurnCorner }: { card: Card; hue: number; onTurnCorner: () => void }) {
  const first = card.name.split(/\s+/)[0] || card.name;
  return (
    <>
      <Corner face={<Face hue={hue} name={card.name} size={16} />} label={first} onTurn={onTurnCorner} turnTo="front" />
      <div className="blank" data-blank-back>
        {card.name} hasn't connected a Host with you, so this side is blank.
      </div>
    </>
  );
}

/** A card with only a back (mockup:719). */
export function NoFront({ card }: { card: Card }) {
  return (
    <div className="blank" data-no-front>
      This card has no front: only Hosts sit in {card.name}.
    </div>
  );
}

// ── Your front ──────────────────────────────────────────────────────────────

/**
 * Your card's front, how people see you (mockup:720-727): your interests
 * (Circles' Settings pane's editor, moved here whole), who can reach you (the
 * domain posture, in its own words: mismatch 14), the machines you're on, and
 * your number, which you set here (plan, "Address book": your own number goes
 * on your card's front). Your email is not shown: no route answers it to
 * Circles.
 */
export function YouFront({ hostName, hue, onTurnCorner }: { hostName: string; hue: number; onTurnCorner: () => void }) {
  const [posture, setPosture] = useState<Posture | null | undefined>(undefined);
  const [machines, setMachines] = useState<BastionMachine[] | null>(null);
  const [alias, setAlias] = useState<string | null | undefined>(undefined);
  // Your number, set here (the Address book's "Your number": PUT
  // /api/phone-alias). People connect with you by it.
  const [numberDraft, setNumberDraft] = useState<string | null>(null);
  const [numberNote, setNumberNote] = useState<string | null>(null);
  const saveNumber = async () => {
    const v = (numberDraft ?? "").trim();
    if (!v) return;
    setNumberNote("Saving…");
    const r = await setMyAlias(v);
    if (r.ok) {
      setAlias(r.data.alias);
      setNumberDraft(null);
      setNumberNote("Saved.");
    } else setNumberNote(r.message || "Try a full number with its country code.");
  };
  useEffect(() => {
    let off = false;
    void getPolicy().then((r) => !off && setPosture(r.ok ? r.data.posture : null));
    void listBastionDirectory().then((r) => !off && setMachines(r.ok ? r.data.machines : []));
    void getMyAlias().then((r) => !off && setAlias(r.ok ? r.data.alias : null));
    return () => {
      off = true;
    };
  }, []);
  return (
    <>
      <Corner
        face={<Face host hue={hue} name={hostName} size={16} />}
        label={hostName}
        onTurn={onTurnCorner}
        turnTo="back"
      />
      <div className="chead">
        <span className="kind">● The front · how people see you</span>
        <Face big hue={hue} name="You" size={52} />
        <div className="who">
          <h2>You</h2>
          <div className="sub">{alias ? `your number · ${alias}` : "a person, with your Host on the back"}</div>
        </div>
      </div>
      <div className="cbody" data-you-front>
        <div className="pcard" data-interests>
          <h3>Your interests</h3>
          <Interests />
          <p className="small">
            When others list the same thing, an introduction arrives at the door. Accepting it joins a Community group;
            nobody is named to you, and you aren't named to them.
          </p>
        </div>
        <div className="pcard">
          <h3>Who can reach you</h3>
          <p>{posture === undefined ? "Reading…" : postureWords(posture)}</p>
          <button className="elsewhere" data-open-network onClick={() => openApp("network")} type="button">
            Change it in Network ›
          </button>
        </div>
        <div className="pcard">
          <h3>Machines you're on</h3>
          {machines == null ? (
            <p>Reading…</p>
          ) : machines.length === 0 ? (
            <p>None yet.</p>
          ) : (
            machines.map((m) => {
              const others = (m.hosts ?? [])
                .filter((h) => !h.is_anchor && h.state !== "revoked")
                .map((h) => h.handle || h.host_name);
              return (
                <p key={m.token_id}>
                  {m.machine}
                  {others.length
                    ? ` · ${others.slice(0, 3).join(", ")} ${others.length === 1 ? "is" : "are"} on it too`
                    : ""}
                </p>
              );
            })
          )}
          <button className="elsewhere" onClick={() => openApp("network")} type="button">
            Manage machines in Network ›
          </button>
        </div>
        <div className="pcard" data-your-number>
          <h3>Your number</h3>
          <p>
            {alias === undefined
              ? "Reading…"
              : alias
                ? alias
                : "You haven't set one. People can connect with you by your email."}
          </p>
          {numberDraft == null ? (
            <button className="link" data-number-change onClick={() => setNumberDraft(alias ?? "")} type="button">
              {alias ? "Change" : "Set one"}
            </button>
          ) : (
            <form
              className="connect"
              onSubmit={(e) => {
                e.preventDefault();
                void saveNumber();
              }}
            >
              <input
                aria-label="Your number, with its country code"
                autoFocus
                data-number-input
                onChange={(e) => setNumberDraft(e.target.value)}
                placeholder="+1 415 555 1234"
                value={numberDraft}
              />
              <span className="row">
                <button className="btn primary" data-number-save disabled={!numberDraft.trim()} type="submit">
                  Save
                </button>
                <button className="btn quiet" onClick={() => setNumberDraft(null)} type="button">
                  Not now
                </button>
              </span>
            </form>
          )}
          {numberNote && <p className="small">{numberNote}</p>}
        </div>
      </div>
    </>
  );
}

/** The group card's raw row, kept for the deck's faces. */
export type { GroupRaw };
