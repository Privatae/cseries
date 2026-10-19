import type { Contact } from "@/lib/types";

function nameList(names: string[]): string {
  if (names.length <= 1) return names[0] ?? "";
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/** What Block does for this contact, in one line under its name. Blocking a Host leaves the people on it reachable; blocking a
 * person blocks the Host they use as well. The server enforces both from the
 * contacts' status (server/routes/contacts.py block_refuses). */
export function blockNote(c: Contact, all: Contact[]): string {
  const name = c.display_name || c.handle;
  const host = typeof c.peer_entity_id === "number" ? c.peer_entity_id : null;
  if (c.kind === "human") {
    if (host === null) {
      return c.status === "blocked"
        ? "Their Host isn't known yet, so it isn't blocked."
        : `Blocking ${name} stops their messages. Their Host isn't known yet, so it won't be blocked.`;
    }
    const h = all.find((x) => x.kind === "host" && x.peer_entity_id === host);
    const hostName = h ? `their Host, ${h.display_name || h.handle}` : "their Host";
    return c.status === "blocked"
      ? `This also blocks ${hostName}.`
      : `Blocking ${name} also blocks ${hostName}.`;
  }
  // A Host added by hand by its handle: the server blocks by Host id, which
  // this contact does not carry until a connection with it records one.
  if (host === null) {
    return "Your Host doesn't know which Host this is yet, so blocking it can't take effect.";
  }
  const people = all
    .filter((x) => x.kind === "human" && x.peer_entity_id === host
      && x.status !== "blocked")
    .map((x) => x.display_name || x.handle);
  const who = people.length ? nameList(people) : "the people who use it";
  const through = c.blocked_through ?? [];
  if (c.status !== "blocked" && through.length) {
    return `Blocked because you blocked ${nameList(through)}. Messages from ${who} still arrive.`;
  }
  return c.status === "blocked"
    ? `Messages from ${who} still arrive.`
    : `Blocking ${name} doesn't stop messages from ${who}.`;
}
