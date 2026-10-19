import { useCallback, useEffect, useState } from "react";
import {
  addPolicyRule,
  createContact,
  deleteContact,
  deletePolicyRule,
  getPolicy,
  listContacts,
  updateContact,
} from "@/lib/api";
import { blockNote } from "@/lib/block-note";
import type { CardRef } from "@/lib/cards";
import { useHostCard } from "@/lib/host-cards";
import { HostCardBody } from "@/components/host-card";
import type { Contact } from "@/lib/types";
import { Sheet } from "./bits";

/**
 * What the Address book did to a contact, now on its card (plan, "Address
 * book and Inbox": a person's card holds notes, Block and Remove; the cards
 * are the address book). Block keeps the address book and the domain policy
 * together, as the Address book's did (contacts-pane.tsx toggleBlock): the
 * contact's status is what the server enforces (contacts.block_refuses), and
 * a deny rule goes beside it and leaves with Unblock.
 */
export async function setBlocked(c: Contact, block: boolean, from = "Circles"): Promise<string | null> {
  const r = await updateContact(c.id, { status: block ? "blocked" : "active" });
  if (!r.ok) return r.message || "That didn't change.";
  if (block) {
    await addPolicyRule({
      peer: c.handle,
      allow_inbox: false,
      allow_dm: false,
      allow_circle_invite: false,
      note: `blocked from ${from}`,
    });
  } else {
    const pol = await getPolicy();
    const rule = pol.ok ? pol.data.rules.find((x) => x.peer === c.handle && !x.allow_inbox) : undefined;
    if (rule) await deletePolicyRule(rule.id);
  }
  return null;
}

/** Every contact you hold (GET /api/contacts/list), for Block's line and
 * for the full list; read again after each change. */
export function useContacts(open: boolean) {
  const [all, setAll] = useState<Contact[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const read = useCallback(async () => {
    const r = await listContacts();
    if (r.ok) {
      setAll(r.data.contacts ?? []);
      setErr(null);
    } else setErr(r.message || "Couldn't read your contacts.");
  }, []);
  useEffect(() => {
    if (open) void read();
  }, [open, read]);
  return { all, err, read };
}

/** Notes, Block or Unblock, and Remove for one contact. `extra` is what the
 * card adds beside them (Clear this chat). */
export function ContactActions({
  contact,
  all,
  onChanged,
  onToast,
  onDone,
  extra,
}: {
  contact: Contact;
  all: readonly Contact[];
  /** The contact changed: read the list (and the deck) again. */
  onChanged: () => void;
  onToast: (s: string) => void;
  /** The actions are finished with (a remove, a block that takes the card away). */
  onDone?: () => void;
  extra?: React.ReactNode;
}) {
  const name = contact.display_name || contact.handle;
  const blocked = contact.status === "blocked";
  const [notes, setNotes] = useState(contact.notes ?? "");
  const [mode, setMode] = useState<"menu" | "remove" | "block">("menu");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => setNotes(contact.notes ?? ""), [contact.id, contact.notes]);
  const dirty = (contact.notes ?? "") !== notes;
  const run = async (f: () => Promise<string | null>, done: string, leave = false) => {
    setBusy(true);
    setErr(null);
    const e = await f();
    setBusy(false);
    if (e) {
      setErr(e);
      return;
    }
    onToast(done);
    setMode("menu");
    onChanged();
    if (leave) onDone?.();
  };
  return (
    <div className="contact-acts" data-contact={contact.id} data-contact-status={contact.status}>
      <label className="notes">
        <span>Notes, only you see them</span>
        <textarea
          aria-label={`Notes on ${name}`}
          data-contact-notes
          maxLength={2000}
          onChange={(e) => setNotes(e.target.value)}
          rows={3}
          value={notes}
        />
      </label>
      {dirty && (
        <span className="row">
          <button
            className="btn primary"
            data-contact-notes-keep
            disabled={busy}
            onClick={() =>
              void run(async () => {
                const r = await updateContact(contact.id, { notes });
                return r.ok ? null : `Couldn't keep the notes: ${r.message}`;
              }, "Notes kept.")
            }
            type="button"
          >
            Keep notes
          </button>
          <button className="btn quiet" onClick={() => setNotes(contact.notes ?? "")} type="button">
            Undo
          </button>
        </span>
      )}
      <p className="small" data-block-note>
        {blockNote(contact, [...all])}
      </p>
      {mode === "menu" && (
        <span className="row">
          <button
            className="btn"
            data-contact-block={blocked ? "unblock" : "block"}
            disabled={busy}
            onClick={() =>
              blocked
                ? void run(() => setBlocked(contact, false), `${name} is unblocked.`)
                : setMode("block")
            }
            type="button"
          >
            {blocked ? `Unblock ${name}` : `Block ${name}`}
          </button>
          <button className="btn quiet" data-contact-remove onClick={() => setMode("remove")} type="button">
            Remove
          </button>
          {extra}
        </span>
      )}
      {mode === "block" && (
        <span className="row" data-contact-block-ask>
          <span className="small">Block {name}? Their messages are refused.</span>
          <button
            className="btn primary"
            data-contact-block-yes
            disabled={busy}
            onClick={() => void run(() => setBlocked(contact, true), `${name} is blocked.`, true)}
            type="button"
          >
            Block
          </button>
          <button className="btn quiet" onClick={() => setMode("menu")} type="button">
            Not now
          </button>
        </span>
      )}
      {mode === "remove" && (
        <span className="row" data-contact-remove-ask>
          <span className="small">Remove {name} from your contacts?</span>
          <button
            className="btn primary"
            data-contact-remove-yes
            disabled={busy}
            onClick={() =>
              void run(
                async () => {
                  const r = await deleteContact(contact.id);
                  return r.ok ? null : `Couldn't remove ${name}: ${r.message}`;
                },
                `${name} is removed.`,
                true,
              )
            }
            type="button"
          >
            Remove
          </button>
          <button className="btn quiet" onClick={() => setMode("menu")} type="button">
            Not now
          </button>
        </span>
      )}
      {err && (
        <p className="err" role="alert">
          {err}
        </p>
      )}
    </div>
  );
}

/** A clear, asked first: a person's chat or a Host's conversation is
 * archived on this Host (nothing erased); a room is left. */
export function AskFirst({
  label,
  question,
  yes,
  onYes,
  data,
}: {
  label: string;
  question: string;
  yes: string;
  onYes: () => Promise<string | null>;
  data: string;
}) {
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  if (!asking)
    return (
      <button className="btn quiet" data-ask-first={data} onClick={() => setAsking(true)} type="button">
        {label}
      </button>
    );
  return (
    <span className="row" data-ask-first-ask={data}>
      <span className="small">{question}</span>
      <button
        className="btn primary"
        data-ask-first-yes={data}
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          setErr(null);
          const e = await onYes();
          setBusy(false);
          if (e) setErr(e);
          else setAsking(false);
        }}
        type="button"
      >
        {yes}
      </button>
      <button className="btn quiet" onClick={() => setAsking(false)} type="button">
        Not now
      </button>
      {err && (
        <span className="err" role="alert">
          {err}
        </span>
      )}
    </span>
  );
}

/** The contact behind a card: read from the list by its id, with the list
 * Block's line needs. Null while reading or when it is gone. */
export function useCardContact(id: number | null | undefined, open: boolean) {
  const { all, err, read } = useContacts(open && id != null);
  const contact = id != null ? ((all ?? []).find((c) => c.id === id) ?? null) : null;
  return { contact, all: all ?? [], err, read };
}

/** The card a contact is on, if it has one: a person by their address, a
 * Host by the Host it is (the rail's Message, contacts-pane.tsx). */
export function contactCard(c: Pick<Contact, "kind" | "handle" | "peer_entity_id">): CardRef | null {
  if (c.kind === "human") return c.handle.includes("@") ? { kind: "dm", email: c.handle.toLowerCase() } : null;
  return typeof c.peer_entity_id === "number" && c.peer_entity_id > 0 ? { kind: "entity", id: c.peer_entity_id } : null;
}

/** A Host contact's card, as the Address book showed it beside the contact. */
function ContactHostCard({ hostId }: { hostId: number }) {
  const card = useHostCard(hostId);
  if (!card) return null;
  return (
    <div data-contact-host-card={hostId}>
      <HostCardBody card={card} />
    </div>
  );
}

/**
 * Every contact you hold, the Address book's list (plan: the cards are the
 * address book; this is where a blocked person or Host, who leaves the deck,
 * is unblocked, and where a contact with no conversation yet is found), and
 * adding one by its handle (the Address book's Add contact).
 */
export function ContactsSheet({
  onClose,
  onToast,
  onChanged,
  onMessage,
}: {
  onClose: () => void;
  onToast: (s: string) => void;
  onChanged: () => void;
  /** Open the card a contact is on. */
  onMessage?: (ref: CardRef) => void;
}) {
  const { all, err, read } = useContacts(true);
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const [adding, setAdding] = useState(false);
  const [handle, setHandle] = useState("");
  const [name, setName] = useState("");
  const [kind, setKind] = useState<"host" | "human">("host");
  const [addErr, setAddErr] = useState<string | null>(null);
  const changed = () => {
    void read();
    onChanged();
  };
  const shown = (all ?? []).filter((c) => {
    const s = q.trim().toLowerCase();
    return !s || c.handle.toLowerCase().includes(s) || (c.display_name || "").toLowerCase().includes(s);
  });
  const blocked = shown.filter((c) => c.status === "blocked");
  const rest = shown.filter((c) => c.status !== "blocked");
  const row = (c: Contact) => (
    <li key={c.id}>
      <button
        aria-expanded={open === c.id}
        data-contact-row={c.id}
        onClick={() => setOpen((x) => (x === c.id ? null : c.id))}
        type="button"
      >
        <span>{c.display_name || c.handle}</span>
        <span className="why">
          {c.kind === "host" ? "a Host" : "a person"}
          {c.status !== "active" ? ` · ${c.status}` : ""}
        </span>
      </button>
      {open === c.id && (
        <>
          {c.kind === "host" && typeof c.peer_entity_id === "number" && <ContactHostCard hostId={c.peer_entity_id} />}
          <ContactActions
            all={all ?? []}
            contact={c}
            extra={
              onMessage && c.status !== "blocked" && contactCard(c) ? (
                <button
                  className="btn"
                  data-contact-message
                  onClick={() => onMessage(contactCard(c) as CardRef)}
                  type="button"
                >
                  Message
                </button>
              ) : null
            }
            onChanged={changed}
            onDone={() => setOpen(null)}
            onToast={onToast}
          />
        </>
      )}
    </li>
  );
  return (
    <Sheet data="contacts" onClose={onClose} title="Your contacts">
      <div className="sheet" data-contacts-sheet>
        <input
          aria-label="Find a contact"
          className="find"
          data-contacts-find
          onChange={(e) => setQ(e.target.value)}
          placeholder="Find a contact"
          value={q}
        />
        {err && all == null ? (
          <p className="err" role="alert">
            {err}{" "}
            <button className="link" onClick={() => void read()} type="button">
              Try again
            </button>
          </p>
        ) : all == null ? (
          <p>Reading…</p>
        ) : shown.length === 0 ? (
          <p>{q.trim() ? "No contact by that name." : "No contacts yet."}</p>
        ) : (
          <>
            <ul className="sheetlist" data-contacts-list>{rest.map(row)}</ul>
            {blocked.length > 0 && (
              <>
                <h4 style={{ marginTop: 12 }}>Blocked</h4>
                <ul className="sheetlist" data-contacts-blocked>{blocked.map(row)}</ul>
              </>
            )}
          </>
        )}
        {adding ? (
          <form
            className="connect"
            data-contact-add
            onSubmit={async (e) => {
              e.preventDefault();
              const h = handle.trim();
              if (!h) return;
              setAddErr(null);
              const r = await createContact({ handle: h, display_name: name.trim() || null, kind });
              if (!r.ok) {
                setAddErr(r.message || "Couldn't add the contact. Try again.");
                return;
              }
              onToast(`${name.trim() || h} is one of your contacts.`);
              setHandle("");
              setName("");
              setAdding(false);
              changed();
            }}
          >
            <span className="caps">Add a contact by handle</span>
            <input
              aria-label="Handle or address"
              autoFocus
              data-contact-add-handle
              maxLength={200}
              onChange={(e) => setHandle(e.target.value)}
              placeholder={kind === "human" ? "their email address" : "the Host's handle"}
              value={handle}
            />
            <input
              aria-label="Display name (optional)"
              maxLength={200}
              onChange={(e) => setName(e.target.value)}
              placeholder="How you know them (optional)"
              value={name}
            />
            <span className="row">
              <label>
                <input checked={kind === "host"} name="kind" onChange={() => setKind("host")} type="radio" /> a Host
              </label>
              <label>
                <input checked={kind === "human"} name="kind" onChange={() => setKind("human")} type="radio" /> a
                person
              </label>
            </span>
            {addErr && (
              <span className="err" role="alert">
                {addErr}
              </span>
            )}
            <span className="row">
              <button className="btn primary" data-contact-add-yes disabled={!handle.trim()} type="submit">
                Add
              </button>
              <button className="btn quiet" onClick={() => setAdding(false)} type="button">
                Not now
              </button>
            </span>
          </form>
        ) : (
          <button className="link" data-contact-add-open onClick={() => setAdding(true)} type="button">
            + Add a contact by handle
          </button>
        )}
      </div>
    </Sheet>
  );
}
