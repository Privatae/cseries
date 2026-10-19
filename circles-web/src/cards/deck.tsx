import { useState } from "react";
import { MachineRequestWidget } from "./machine-request";
import { lookupAlias, requestByEmail, requestByPhone } from "@/lib/api";
import { doorCall, openApp, type SupportDraftRow } from "@/lib/cards-api";
import {
  clockOf,
  dayWord,
  doorCallOk,
  doorWords,
  groupJoinOutcome,
  notNowWords,
  waitingWhere,
  whenWord,
  type Card,
  type CardLights,
  type DoorItem,
  type WaitingDecision,
} from "@/lib/cards";
import type { CalendarEvent, InboxMessage } from "@/lib/types";
import { HostName } from "@/components/host-card";
import { Face, hueOf, Light, TeamFace, Working } from "./bits";

// ── A card in the deck ──────────────────────────────────────────────────────

/** One card of the deck (mockup:287-305): the face, the name and last line,
 * and down the right edge the Host's square face with its lights. */
export function DeckCard({
  card,
  lights,
  held,
  lift,
  working,
  hostName,
  onOpen,
}: {
  card: Card;
  lights: CardLights;
  held: boolean;
  lift: boolean;
  /** Your Host is working (your card's edge). */
  working: boolean;
  hostName: string;
  onOpen: () => void;
}) {
  const hue = card.kind === "you" ? 0 : hueOf(card.email || card.key);
  const cls = ["dc", card.kind === "host" || card.kind === "room" ? "hostonly" : "", lift ? "lift" : ""]
    .filter(Boolean)
    .join(" ");
  const subAmber = card.kind === "person" ? card.unread > 0 : card.kind === "support" && lights.name === "amber";
  const face =
    card.kind === "support" ? (
      <TeamFace size={36} />
    ) : card.kind === "host" || card.kind === "room" ? (
      <Face host hue={hue} name={card.name} size={36} />
    ) : (
      <Face hue={hue} name={card.kind === "you" ? "You" : card.name} size={36} />
    );
  const edge =
    card.kind === "you" ? (
      <span className="edge" data-edge>
        <Face host hue={0} name={hostName} size={24} />
        {working ? <Working label={`${hostName} is working`} /> : null}
        <Light light={lights.edgeAmber ? "amber" : null} fresh={lift} title="something waits on you" />
        <Light light={lights.edgeGreen ? "green" : null} fresh={lift} title="something landed" />
      </span>
    ) : card.kind === "person" && !card.blankBack && card.hostName ? (
      <span className="edge" data-edge title={`${card.hostName}, their Host, is on the back`}>
        <Face host hue={hue} name={card.hostName} size={24} />
        <Light light={lights.edgeGreen ? "green" : null} fresh={lift} title={`${card.hostName} wrote`} />
      </span>
    ) : card.kind === "group" ? (
      <span className="edge" data-edge title="their Hosts keep the roster and the votes">
        <Face host hue={3} name={card.name} size={18} />
      </span>
    ) : null;
  return (
    <button
      aria-current={held ? "true" : undefined}
      aria-label={`${card.name}: ${card.sub}`}
      className={cls}
      data-card={card.key}
      data-card-light={lights.name ?? undefined}
      data-lift={lift || undefined}
      onClick={onOpen}
      type="button"
    >
      <span className="face">
        {face}
        <span className="txt">
          <span className="nm">
            {card.name} <Light light={lights.name} fresh={lift} />
          </span>
          <span className={`sb${subAmber ? " amber" : ""}`}>{card.sub || " "}</span>
        </span>
      </span>
      {edge}
    </button>
  );
}

// ── At the door ─────────────────────────────────────────────────────────────

/** A machine request's card, the one the Inbox draws, fed from the door's
 * item (inbox-pane.tsx MachineRequestWidget reads these fields only). */
function asInboxMessage(i: DoorItem): InboxMessage {
  return {
    message_id: i.id,
    thread_id: "",
    from: i.from?.name ?? "",
    to: "",
    subject: i.subject ?? null,
    body_format: "text/plain",
    kind: "machine_request",
    sent_at: i.at ?? null,
    delivered_at: null,
    read_at: null,
    archived_at: null,
    from_entity_id: i.from?.entity_id ?? null,
    to_entity_id: null,
    body: i.note ?? null,
    payload: { from_display_name: i.from?.name ?? undefined, models_asked: i.models_asked ?? [] },
  };
}

/** A card at the door (mockup:648-649): who knocks, in today's terms, with
 * the Accept and Not now the server named for it (circles_deck.door). Not
 * now tells nobody (mockup:663). */
export function DoorCard({
  item,
  onDone,
  onToast,
  onOpen,
}: {
  item: DoorItem;
  /** The knock in full, as the Inbox showed it: everything recorded on it. */
  onOpen: () => void;
  /** Answered; `accepted` names a person now connected. */
  onDone: (accepted: { email?: string | null; hostId?: number | null } | null) => void;
  onToast: (s: string) => void;
}) {
  const w = doorWords(item);
  const [busy, setBusy] = useState(false);
  const [gone, setGone] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [share, setShare] = useState(false);
  const [outcome, setOutcome] = useState<string | null>(null);
  const person = !!item.from?.person;
  const when = item.at ? `${dayWord(item.at)} ${clockOf(item.at)}`.trim() : "";
  const hue = hueOf(item.from?.email || item.from?.name || item.id);
  const accept = async () => {
    const c = item.accept;
    if (!doorCallOk(c) || busy) return;
    if (item.kind === "machine_request") {
      setShare(true);
      return;
    }
    setBusy(true);
    setErr(null);
    const r = await doorCall(c);
    setBusy(false);
    if (!r.ok) {
      setErr(r.message || "That didn't go through.");
      return;
    }
    if (item.kind === "group_join") {
      // Say what letting them in did, as the Inbox did, before the card goes.
      setOutcome(
        groupJoinOutcome(
          r.data as Parameters<typeof groupJoinOutcome>[0],
          w.who,
          item.group?.name || "your group",
        ),
      );
      return;
    }
    setGone(true);
    onDone({ email: item.from?.email ?? null, hostId: item.from?.entity_id ?? null });
  };
  const notNow = async () => {
    if (busy) return;
    const calls = (item.not_now ?? []).filter(doorCallOk);
    setBusy(true);
    setErr(null);
    for (const c of calls) {
      const r = await doorCall(c);
      if (!r.ok) {
        setBusy(false);
        setErr(r.message || "That didn't go through.");
        return;
      }
    }
    setBusy(false);
    setGone(true);
    onToast(notNowWords(item));
    onDone(null);
  };
  return (
    <div className="dc door" data-door={item.id} data-door-kind={item.kind} data-gone={gone || undefined}>
      <span className="face">
        <span className="row">
          {item.kind === "rendezvous_intro" ? (
            <TeamFace size={30} />
          ) : (
            <Face host={!person} hue={hue} name={w.who} size={30} />
          )}
          <span className="txt">
            <span className="nm">
              {!person && item.from?.entity_id ? (
                <HostName fallback={w.who} focusable hostId={item.from.entity_id} />
              ) : (
                w.who
              )}
            </span>
            <span className="sb" style={{ whiteSpace: "normal" }}>
              {w.what}
            </span>
            {when && (
              <span className="sb" data-door-when>
                {when}
              </span>
            )}
          </span>
        </span>
        {item.subject && !/^(connection request|new peer)/i.test(item.subject) && item.kind !== "rendezvous_intro" ? (
          <span className="said" data-door-subject>
            {item.subject}
          </span>
        ) : null}
        {item.note ? <span className="said">“{item.note}”</span> : null}
        {item.kind === "group_join" && !outcome && (
          <span className="said" data-door-explain>
            Letting them in adds them as a contact and puts their Host on the group's roster; nothing else is shared. Not
            now tells them nothing.
          </span>
        )}
        {outcome ? (
          <span className="acts" data-door-outcome>
            <span className="said">{outcome}</span>
            <button
              className="btn"
              onClick={() => {
                setGone(true);
                onDone({ email: null, hostId: item.from?.entity_id ?? null });
              }}
              type="button"
            >
              Done
            </button>
          </span>
        ) : share ? (
          <MachineRequestWidget message={asInboxMessage(item)} onAnswered={() => onDone(null)} />
        ) : (
          <span className="acts">
            {doorCallOk(item.accept) && (
              <button
                className="btn primary"
                data-door-accept
                disabled={busy || gone}
                onClick={() => void accept()}
                type="button"
              >
                {w.accept}
              </button>
            )}
            <button
              className="btn quiet"
              data-door-not-now
              disabled={busy || gone}
              onClick={() => void notNow()}
              type="button"
            >
              Not now
            </button>
            <button className="btn quiet" data-door-open onClick={onOpen} type="button">
              In full
            </button>
          </span>
        )}
        {err && (
          <span className="door-err" role="alert">
            {err}
          </span>
        )}
      </span>
    </div>
  );
}

// ── Connect with someone new ────────────────────────────────────────────────

/** Mismatch 11's default: "+" at the end of Your cards, "Connect by email or
 * number", the two routes the Address book used (contacts-pane.tsx:74-153).
 * A number is looked up first, as the Address book's Find did: whose Host it
 * is shows before anything is sent (POST /api/phone-alias/lookup). */
export function Connect({ onClose, onToast }: { onClose: () => void; onToast: (s: string) => void }) {
  const [v, setV] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [found, setFound] = useState<{ alias: string; name: string } | null>(null);
  const x = v.trim();
  const byEmail = x.includes("@");
  const go = async () => {
    if (!x || busy) return;
    setBusy(true);
    setErr(null);
    if (!byEmail && found?.alias !== x) {
      // Find first: who uses this number.
      const r = await lookupAlias(x);
      setBusy(false);
      if (r.ok && r.data.found) setFound({ alias: x, name: r.data.display_name || x });
      else setErr(r.ok ? "No Host is using that number." : r.message || "That didn't go through.");
      return;
    }
    const r = byEmail ? await requestByEmail(x) : await requestByPhone(x);
    setBusy(false);
    if (!r.ok) {
      setErr(r.message || "That didn't go through.");
      return;
    }
    onToast(byEmail ? `Asked. Once ${x} accepts, they're one of your cards.` : `Asked ${found?.name ?? x} to connect.`);
    onClose();
  };
  return (
    <form
      className="connect"
      data-connect
      onSubmit={(e) => {
        e.preventDefault();
        void go();
      }}
    >
      <span className="caps">Connect by email or number</span>
      <input
        aria-label="Their email or number"
        autoFocus
        onChange={(e) => {
          setV(e.target.value);
          setFound(null);
          setErr(null);
        }}
        placeholder="name@example.com or their number"
        value={v}
      />
      {found && found.alias === x ? (
        <p data-connect-found>That number is {found.name}. Ask them to connect?</p>
      ) : (
        <p>
          {byEmail || !x
            ? "They're asked to connect. Nothing else is said, and whether the address exists is never told to you."
            : "A number is looked up first, so you see whose Host it is before anything is sent."}
        </p>
      )}
      {err && (
        <span className="err" role="alert">
          {err}
        </span>
      )}
      <span className="row">
        <button className="btn primary" data-connect-go disabled={busy || !x} type="submit">
          {busy ? (byEmail || found ? "Asking…" : "Looking…") : byEmail || (found && found.alias === x) ? "Ask to connect" : "Find"}
        </button>
        <button className="btn quiet" onClick={onClose} type="button">
          Not now
        </button>
      </span>
    </form>
  );
}

// ── Waiting on you, Coming up ───────────────────────────────────────────────

/** One thing waiting on you: a decision, a support draft, or a knock. */
export type WaitItem =
  | { kind: "decision"; d: WaitingDecision & { sitrep?: string | null } }
  | { kind: "draft"; s: SupportDraftRow }
  | { kind: "door"; i: DoorItem }
  /** The platform's team asked you something on a support ticket (the rail's
   * Tickets mark, support badge-counts needs_info). */
  | { kind: "support_q"; n: number }
  /** An older ask of your Host's, from the decisions inbox (server/routes/
   * decisions.py: an inbox message of kind decision or decision_review), or
   * a permission request: the Inbox showed them. */
  | { kind: "note"; m: InboxMessage };

export function waitKey(w: WaitItem): string {
  switch (w.kind) {
    case "decision":
      return `decision:${w.d.id}`;
    case "draft":
      return `draft:${w.s.id}`;
    case "door":
      return `door:${w.i.id}`;
    case "support_q":
      return "support:asks";
    default:
      return `note:${w.m.message_id}`;
  }
}

export function waitWords(w: WaitItem, hostName: string): { title: string; line: string } {
  if (w.kind === "decision") {
    const first = String(w.d.sitrep ?? "")
      .split("\n")[0]
      .trim();
    return {
      title: waitingWhere(w.d, hostName),
      line: first.length > 90 ? `${first.slice(0, 88)}…` : first || "A decision for you",
    };
  }
  if (w.kind === "draft") return { title: "Support", line: `A ticket to send: ${w.s.title}` };
  if (w.kind === "support_q")
    return {
      title: "Support",
      line: w.n === 1 ? "The platform's team asks you something" : `The platform's team asks you ${w.n} things`,
    };
  if (w.kind === "note") {
    const what =
      w.m.kind === "decision_review"
        ? "decided on its own, for your review"
        : w.m.kind === "permission_request"
          ? "a permission request"
          : "an older ask";
    return { title: `${hostName} · ${what}`, line: w.m.subject || "Open it to read it" };
  }
  const dw = doorWords(w.i);
  return { title: dw.who, line: dw.what };
}

function WaitFace({ w, hostName, size }: { w: WaitItem; hostName: string; size: number }) {
  if (w.kind === "decision" || w.kind === "note") return <Face host hue={0} name={hostName} size={size} />;
  if (w.kind === "draft" || w.kind === "support_q") return <TeamFace size={size} />;
  return (
    <Face
      host={!w.i.from?.person}
      hue={hueOf(w.i.from?.email || w.i.from?.name || w.i.id)}
      name={doorWords(w.i).who}
      size={size}
    />
  );
}

/** The desktop's right column (mockup:832-837). */
export function Today({
  items,
  hostName,
  coming,
  focus,
  onGo,
  onAllDecisions,
}: {
  items: WaitItem[] | null;
  hostName: string;
  coming: CalendarEvent[] | null;
  focus: string | null;
  onGo: (w: WaitItem) => void;
  /** Every decision, decided ones too (the Tickets pane's queue). */
  onAllDecisions: () => void;
}) {
  return (
    <aside aria-label="Across every card" className="today">
      <div className="box" data-waiting>
        <div className="caps">
          <span>Waiting on you</span>
          <button data-decisions-open onClick={onAllDecisions} type="button">
            All decisions ›
          </button>
        </div>
        {items == null ? (
          <div className="empty">Reading…</div>
        ) : items.length === 0 ? (
          <div className="empty">Nothing waits on you.</div>
        ) : (
          items.map((w) => {
            const t = waitWords(w, hostName);
            const k = waitKey(w);
            return (
              <button
                className="item"
                data-focus={focus === k || undefined}
                data-wait={k}
                key={k}
                onClick={() => onGo(w)}
                type="button"
              >
                <WaitFace hostName={hostName} size={22} w={w} />
                <b>{t.title}</b>
                <span className="it2">{t.line}</span>
              </button>
            );
          })
        )}
      </div>
      <div className="box" data-coming-up>
        <div className="caps">
          <span>Coming up · from Calendar</span>
          <button data-open-calendar onClick={() => openApp("calendar")} type="button">
            Open ›
          </button>
        </div>
        {coming == null ? (
          <div className="empty">Reading…</div>
        ) : coming.length === 0 ? (
          <div className="empty">Nothing on the calendar.</div>
        ) : (
          <div className="cu">
            {coming.map((e) => (
              <span key={e.event_id} style={{ display: "contents" }}>
                <span className="w">{whenWord(e.event_at)}</span>
                <span>
                  {e.summary}
                  {e.event_type === "scheduled_task" ? ` (${hostName})` : ""}
                </span>
              </span>
            ))}
          </div>
        )}
      </div>
      <div className="legend">
        <span className="s" style={{ borderRadius: "50%" }} />a person, always a human: the front of a card
        <br />
        <span className="s" style={{ borderRadius: 2 }} />a Host, an AI: the back of a card
        <br />
        Click the card you're holding to turn it over.
      </div>
    </aside>
  );
}

/** The phone's strip of what waits on you, and its one Coming up line
 * (mockup:652-656). */
export function PhoneToday({
  items,
  hostName,
  coming,
  focus,
  onGo,
  onAllDecisions,
}: {
  items: WaitItem[] | null;
  hostName: string;
  coming: CalendarEvent[] | null;
  focus: string | null;
  onGo: (w: WaitItem) => void;
  onAllDecisions: () => void;
}) {
  const next = coming?.[0];
  return (
    <>
      <div className="caps phoneonly" style={{ display: "flex", justifyContent: "space-between" }}>
        <span>Waiting on you</span>
        <button className="link" data-decisions-open onClick={onAllDecisions} type="button">
          All decisions ›
        </button>
      </div>
      <div aria-label="Waiting on you" className="strip" data-strip>
        {items == null ? (
          <span className="empty">Reading…</span>
        ) : items.length === 0 ? (
          <span className="empty">Nothing waits on you.</span>
        ) : (
          items.map((w) => {
            const t = waitWords(w, hostName);
            const k = waitKey(w);
            return (
              <button data-focus={focus === k || undefined} data-wait={k} key={k} onClick={() => onGo(w)} type="button">
                <b>
                  <WaitFace hostName={hostName} size={18} w={w} />
                  <span>{t.title.replace(`${hostName} · `, "")}</span>
                </b>
                <span className="s2">{t.line}</span>
              </button>
            );
          })
        )}
      </div>
      <button className="soon" data-soon onClick={() => openApp("calendar")} type="button">
        {next ? (
          <>
            <span className="w">{whenWord(next.event_at)}</span>
            <span className="what">{next.summary}</span>
          </>
        ) : (
          <span className="what">Nothing coming up</span>
        )}
        <span className="go">Calendar ›</span>
      </button>
    </>
  );
}

export { Working };
