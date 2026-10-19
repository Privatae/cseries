import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { HostName } from "@/components/host-card";
import { acceptMachineOffer, declineMachineOffer, listBastions, listMachineOffers } from "@/lib/api";
import type { MachineOffer, ShareableBastion } from "@/lib/api";
import type { InboxMessage } from "@/lib/types";
import { cn } from "@/lib/utils";

// Moved whole from the Inbox pane (inbox-pane.tsx) when the rail went: the
// door draws it for a
// machine request (cards/deck.tsx DoorCard), as the Inbox drew it.

// Another host asking to use a model on one of the user's machines.
//
// The card exists to make ONE sentence decidable: "the only thing they can do
// with this machine is use these models." So it shows who is asking and what
// for, the user picks WHICH machine and WHICH of that machine's models — never
// the names the asker typed, which are a hint and nothing more — and approving
// takes the step-up code, because a host gaining a lane on the user's machine
// is a widen like any other.
//
// Declining is deliberately the plainer path, and says so: nothing is written
// on the machine, the box is never asked.
export function MachineRequestWidget({
  message,
  onAnswered,
}: {
  message: InboxMessage;
  onAnswered: () => void;
}) {
  const [offer, setOffer] = useState<MachineOffer | null>(null);
  const [machines, setMachines] = useState<ShareableBastion[]>([]);
  const [server, setServer] = useState("");
  const [models, setModels] = useState<string[]>([]);
  const [code, setCode] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const p = (message.payload ?? {}) as Record<string, unknown>;
  const who = String(p.from_display_name ?? p.from_handle ?? message.from ?? "A host");
  const fromEntityId = message.from_entity_id;
  const asked = Array.isArray(p.models_asked) ? (p.models_asked as string[]) : [];

  useEffect(() => {
    let live = true;
    void (async () => {
      // The offer row carries the id the accept needs. It is matched on the
      // VERIFIED sender of the message, never on anything in the payload.
      const [o, b] = await Promise.all([listMachineOffers("in"), listBastions()]);
      if (!live) return;
      if (o.ok) {
        const mine = o.data.offers.find(
          (x) => x.peer_entity_id === message.from_entity_id && x.state === "open",
        );
        setOffer(mine ?? null);
        if (!mine) setStatus("This request is no longer open.");
      }
      if (b.ok) {
        // Only machines this host anchors can admit anyone, so only those are
        // offerable. A machine with no anchor is somebody else's to decide.
        setMachines(b.data.bastions.filter((m) => m.hostname && m.anchor?.anchor_host));
      }
    })();
    return () => {
      live = false;
    };
  }, [message.from_entity_id]);

  const chosen = machines.find((m) => m.hostname === server);
  const available = chosen?.models ?? [];

  const toggle = (name: string) =>
    setModels((prev) => (prev.includes(name) ? prev.filter((m) => m !== name) : [...prev, name]));

  const approve = async () => {
    if (!offer) return;
    setBusy(true);
    setStatus("Sharing…");
    const r = await acceptMachineOffer({
      offer_id: offer.id,
      server,
      models,
      stepup_code: code,
    });
    setBusy(false);
    if (r.ok) {
      setStatus(`Shared. They can use ${r.data.may_use} on ${server} — nothing else.`);
      onAnswered();
    } else {
      setStatus(r.message || "Could not share that machine.");
    }
  };

  const decline = async () => {
    if (!offer) {
      onAnswered();
      return;
    }
    setBusy(true);
    const r = await declineMachineOffer(offer.id);
    setBusy(false);
    if (r.ok) onAnswered();
    else setStatus(r.message || "Could not decline.");
  };

  return (
    <div className="mt-4 rounded-lg border border-primary/30 bg-primary/5 p-4">
      <div className="text-foreground text-sm">
        <span className="font-medium">
          <HostName fallback={who} focusable hostId={fromEntityId} />
        </span>{" "}
        <span className="text-muted-foreground">(a host)</span> wants to use a model on one of
        your machines.
        {asked.length > 0 && (
          <>
            {" "}
            They asked for{" "}
            <span className="font-medium">{asked.join(", ")}</span>.
          </>
        )}
      </div>
      <p className="mt-2 text-muted-foreground text-xs">
        Sharing a model gives them that model and nothing else — no folders, no context, no
        other models. You pick which machine and which models; what they asked for is only a
        hint.
      </p>

      <label className="mt-3 block text-foreground text-xs" htmlFor="machine-offer-server">
        Machine
      </label>
      <select
        className="mt-1 w-full rounded-md border border-border bg-card p-2 text-sm"
        disabled={busy}
        id="machine-offer-server"
        onChange={(e) => {
          setServer(e.target.value);
          setModels([]);
        }}
        value={server}
      >
        <option value="">Choose one of your machines…</option>
        {machines.map((m) => (
          <option key={m.hostname} value={m.hostname ?? ""}>
            {m.hostname}
            {m.online ? "" : " (offline)"}
          </option>
        ))}
      </select>

      {server && (
        <>
          <div className="mt-3 text-foreground text-xs">Models to share</div>
          {available.length === 0 ? (
            <p className="mt-1 text-muted-foreground text-xs">
              That machine has not reported any models on its last check-in. Pick another, or
              wait for it to check in.
            </p>
          ) : (
            <div className="mt-1 flex flex-wrap gap-2">
              {available.map((name) => (
                <button
                  className={cn(
                    "rounded-full border px-3 py-1 text-xs",
                    models.includes(name)
                      ? "border-primary bg-primary/15 text-foreground"
                      : "border-border text-muted-foreground",
                  )}
                  disabled={busy}
                  key={name}
                  onClick={() => toggle(name)}
                  type="button"
                >
                  {name}
                </button>
              ))}
            </div>
          )}
        </>
      )}

      <label className="mt-3 block text-foreground text-xs" htmlFor="machine-offer-code">
        Your 2FA code
      </label>
      <input
        autoComplete="one-time-code"
        className="mt-1 w-40 rounded-md border border-border bg-card p-2 text-sm"
        disabled={busy}
        id="machine-offer-code"
        inputMode="numeric"
        onChange={(e) => setCode(e.target.value)}
        value={code}
      />

      <div className="mt-3 flex gap-2">
        <Button
          disabled={busy || !offer || !server || models.length === 0 || !code}
          onClick={approve}
          size="sm"
        >
          Share these models
        </Button>
        <Button disabled={busy} onClick={decline} size="sm" variant="outline">
          Decline
        </Button>
      </div>
      <p className="mt-2 text-muted-foreground text-xs">
        Declining writes nothing on the machine — it is never asked.
      </p>
      {status && <div className="mt-2 text-muted-foreground text-xs">{status}</div>}
    </div>
  );
}
