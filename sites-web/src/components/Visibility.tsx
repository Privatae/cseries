import { useState } from "react";
import { CreditCard, Globe, Loader2, Lock, Users } from "lucide-react";
import {
  setExposure,
  type ExposurePolicy,
  type ExposureTier,
  type Site,
} from "@/lib/api";
import { cn } from "@/lib/utils";

// The flip. Drives POST /sites/exposure (pure pass-through to the bridge).
// Four owner-selectable tiers: Public, Private (authenticated, scoped to the
// owner's email), Members (2b customer pool), Paywall (3 BYO-Stripe). Members and
// Paywall carry extra policy the owner sets inline; password / ip_allowlist
// are set elsewhere and shown read-only.
//
// Search engines: a published site is not indexed unless the owner turns it
// on here (exposure.indexable). Offered on Public only, the one tier a crawler
// can read. The tier buttons send no `indexable`, which keeps the stored value.

const TIER_LABEL: Record<ExposureTier, string> = {
  public: "Public",
  authenticated: "Private",
  password: "Password",
  ip_allowlist: "IP allowlist",
  customers: "Members",
  paywall: "Paywall",
};


export function Visibility({
  site,
  onChanged,
}: {
  site: Site;
  onChanged: () => void;
}) {
  const current: ExposureTier = site.exposure?.tier ?? "public";
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [seeded, setSeeded] = useState<string[] | null>(
    current === "authenticated" ? (site.exposure?.allow_emails ?? null) : null,
  );

  // Paywall inline form state (seeded from the stored policy).
  const [checkout, setCheckout] = useState(site.exposure?.checkout_url ?? "");
  const [product, setProduct] = useState(site.exposure?.product_key ?? "");
  // Composing a paywall from a non-paywall tier: clicking Paywall opens the
  // checkout composer BEFORE the flip is committed — otherwise the URL input
  // (rendered only for paywall) would never be reachable to set one up.
  const [composing, setComposing] = useState(false);

  const signup = site.exposure?.signup ?? "invite";
  const indexable = site.exposure?.indexable === true;

  async function apply(policy: ExposurePolicy) {
    if (busy) return;
    setBusy(true);
    setErr(null);
    const r = await setExposure(site.slug, policy);
    setBusy(false);
    if (!r.ok) {
      setErr(r.message);
      return;
    }
    const ex = r.data.exposure;
    setSeeded(ex?.tier === "authenticated" ? (ex.allow_emails ?? []) : null);
    setComposing(false);
    onChanged();
  }

  // password / ip_allowlist: read-only here (the two-way flip doesn't offer them)
  const isOther = current === "password" || current === "ip_allowlist";

  return (
    <div className="mt-2 border-t border-border pt-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted-foreground">Visibility</span>
        {isOther ? (
          <span className="rounded-md border border-border px-2 py-0.5 text-xs text-foreground">
            {TIER_LABEL[current]}
          </span>
        ) : (
          <div className="inline-flex overflow-hidden rounded-md border border-border">
            <Seg
              active={current === "public"}
              disabled={busy}
              onClick={() => apply({ tier: "public" })}
            >
              <Globe className="size-3" /> Public
            </Seg>
            <Seg
              active={current === "authenticated"}
              disabled={busy}
              onClick={() => apply({ tier: "authenticated" })}
            >
              <Lock className="size-3" /> Private
            </Seg>
            <Seg
              active={current === "customers"}
              disabled={busy}
              onClick={() =>
                current !== "customers" && apply({ tier: "customers", signup })
              }
            >
              <Users className="size-3" /> Members
            </Seg>
            <Seg
              active={current === "paywall" || composing}
              disabled={busy}
              onClick={() => {
                // Open the checkout composer below; its Save button commits the
                // flip once a Checkout URL is entered. (Already-paywall sites
                // edit the same composer, always rendered.)
                if (current === "paywall") return;
                setErr(null);
                setComposing(true);
              }}
            >
              <CreditCard className="size-3" /> Paywall
            </Seg>
          </div>
        )}
        {busy && <Loader2 className="size-3 animate-spin text-muted-foreground" />}
      </div>

      {/* Public: may search engines list it */}
      {current === "public" && (
        <div className="mt-1.5 flex flex-wrap items-center gap-2">
          <span className="text-xs text-muted-foreground">Search engines</span>
          <div className="inline-flex overflow-hidden rounded-md border border-border">
            <Seg
              active={!indexable}
              disabled={busy}
              onClick={() =>
                indexable && apply({ tier: "public", indexable: false })
              }
            >
              Hidden
            </Seg>
            <Seg
              active={indexable}
              disabled={busy}
              onClick={() =>
                !indexable && apply({ tier: "public", indexable: true })
              }
            >
              Listed
            </Seg>
          </div>
          <span className="text-xs text-muted-foreground">
            {indexable
              ? "Search engines may list this site."
              : "Anyone with the link can open it; search engines are asked not to list it."}
          </span>
        </div>
      )}

      {/* Private: which account can see it */}
      {current === "authenticated" && seeded && seeded.length > 0 && (
        <p className="mt-1 text-xs text-muted-foreground">
          Only <span className="text-foreground">{seeded.join(", ")}</span> can
          see this — log in with that account.
        </p>
      )}
      {current === "authenticated" && seeded && seeded.length === 0 && (
        <p className="mt-1 text-xs text-warning">
          Any logged-in account can see this. Click Private to limit it to you.
        </p>
      )}

      {/* Members: signup policy */}
      {current === "customers" && (
        <div className="mt-1.5 flex flex-wrap items-center gap-2">
          <span className="text-xs text-muted-foreground">Signups</span>
          <div className="inline-flex overflow-hidden rounded-md border border-border">
            <Seg
              active={signup === "open"}
              disabled={busy}
              onClick={() =>
                signup !== "open" &&
                apply({ tier: "customers", signup: "open" })
              }
            >
              Open
            </Seg>
            <Seg
              active={signup === "invite"}
              disabled={busy}
              onClick={() =>
                signup !== "invite" &&
                apply({ tier: "customers", signup: "invite" })
              }
            >
              Invite only
            </Seg>
          </div>
          <span className="text-xs text-muted-foreground">
            {signup === "open"
              ? "Visitors can request access; manage them in Access."
              : "Only people you add in Access get in."}
          </span>
        </div>
      )}

      {/* Paywall: checkout URL + product key (composing, or already paywall) */}
      {(current === "paywall" || composing) && (
        <div className="mt-1.5 space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <input
              type="url"
              value={checkout}
              onChange={(e) => setCheckout(e.target.value)}
              placeholder="https://buy.stripe.com/… (your Checkout link)"
              className="min-w-0 flex-1 rounded-md border border-border bg-background px-2 py-1 text-xs outline-none focus:border-primary"
            />
            <input
              type="text"
              value={product}
              onChange={(e) => setProduct(e.target.value)}
              placeholder="product key (optional)"
              className="w-40 rounded-md border border-border bg-background px-2 py-1 text-xs outline-none focus:border-primary"
            />
            <button
              type="button"
              disabled={busy || !checkout.trim()}
              onClick={() =>
                apply({
                  tier: "paywall",
                  checkout_url: checkout.trim(),
                  product_key: product.trim() || undefined,
                })
              }
              className="rounded-md border border-border px-2.5 py-1 text-xs text-foreground hover:bg-accent disabled:cursor-not-allowed disabled:opacity-50"
            >
              Save
            </button>
            {composing && current !== "paywall" && (
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  setComposing(false);
                  setErr(null);
                }}
                className="rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground hover:bg-accent"
              >
                Cancel
              </button>
            )}
          </div>
          <p className="text-xs text-muted-foreground">
            Unpaid visitors are sent to your Checkout. Connect Stripe under{" "}
            <span className="text-foreground">Payments</span> so payments unlock
            access automatically.
          </p>
        </div>
      )}

      {err && <p className="mt-1 text-xs text-destructive-foreground">{err}</p>}
    </div>
  );
}

function Seg({
  children,
  active,
  disabled,
  onClick,
}: {
  children: React.ReactNode;
  active: boolean;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "flex items-center gap-1 px-2.5 py-1 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-60",
        active
          ? "bg-primary text-primary-foreground"
          : "text-foreground hover:bg-accent",
      )}
    >
      {children}
    </button>
  );
}
