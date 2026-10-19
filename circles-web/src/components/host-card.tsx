// The contact card for another Host: wherever the dashboard
// names another Host, the name opens a card like a traditional contacts entry —
// name, address, Host #, classification, contact number, relationship, user.
// Lines a Host has not shared read "Not shared", never a guess.
import type { ReactNode } from "react";
import { Avatar } from "@/components/layout";
import {
  HoverCard,
  HoverCardContent,
  HoverCardTrigger,
} from "@/components/ui/hover-card";
import { type HostCard, useHostCard } from "@/lib/host-cards";
import { cn } from "@/lib/utils";

function Row({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 text-xs">
      <span className="shrink-0 text-muted-foreground">{label}</span>
      <span className="min-w-0 truncate text-right text-foreground">{value}</span>
    </div>
  );
}

const notShared = <span className="text-muted-foreground">Not shared</span>;

/** The card itself, for inline use (a Contacts detail) or inside a hover. */
export function HostCardBody({ card, className }: { card: HostCard; className?: string }) {
  return (
    <div className={cn("space-y-2", className)} data-testid="host-card">
      <div className="flex items-center gap-3">
        <Avatar label={card.display_name} />
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="truncate font-semibold text-foreground text-sm">
              {card.display_name}
            </span>
            {card.mine && (
              // From the account, never from anything a peer sent — so a peer
              // whose owner shares your name can never wear it.
              <span className="shrink-0 rounded bg-primary/15 px-1.5 py-0.5 font-medium text-[10px] text-primary">
                Your host
              </span>
            )}
          </div>
          <div className="truncate text-muted-foreground text-xs">
            {card.address ?? card.number}
          </div>
        </div>
      </div>
      <div className="space-y-1 border-border border-t pt-2">
        <Row label="Host" value={card.number} />
        <Row label="Classification" value={card.classification} />
        <Row label="Contact number" value={card.contact_number ?? notShared} />
        <Row label="Relationship" value={card.relationship_label} />
        {card.connection_label !== card.relationship_label && (
          <Row label="Connection" value={card.connection_label} />
        )}
        <Row label="User" value={card.user ?? notShared} />
        {card.address && <Row label="Address" value={card.address} />}
        {card.can_message && <Row label="Messages" value="You can message this host" />}
      </div>
    </div>
  );
}

/**
 * A Host's name that opens its card on hover or focus. `fallback` is what the
 * surface already showed, so nothing blanks while the card loads.
 */
export function HostName({
  hostId,
  fallback,
  className,
  focusable = false,
}: {
  hostId: number | null | undefined;
  fallback: string;
  className?: string;
  /** Keyboard-reachable. Off inside a row that is already a button. */
  focusable?: boolean;
}) {
  const card = useHostCard(hostId);
  const label = card?.display_name ?? fallback;
  if (!card) return <span className={className}>{label}</span>;
  return (
    <HoverCard openDelay={250}>
      <HoverCardTrigger asChild>
        <span
          className={cn("cursor-default underline decoration-dotted underline-offset-2", className)}
          tabIndex={focusable ? 0 : undefined}
        >
          {label}
        </span>
      </HoverCardTrigger>
      <HoverCardContent
        align="start"
        className="w-72"
        // The card renders in a portal, and React still bubbles its events to
        // the row's button — a click inside the card must not open the chat.
        onClick={(e) => e.stopPropagation()}
        onPointerDown={(e) => e.stopPropagation()}
      >
        <HostCardBody card={card} />
      </HoverCardContent>
    </HoverCard>
  );
}
