import { useCallback, useEffect, useState } from "react";
import {
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  CreditCard,
  ExternalLink,
  Loader2,
} from "lucide-react";
import {
  connectStripe,
  fetchStripe,
  onboardStripe,
  type StripeStatus,
  type Subscriber,
} from "@/lib/api";
import { cn } from "@/lib/utils";

// Payments — the owner's OWN Stripe, two lanes. Recommended: Stripe Connect
// hosted onboarding (we store only the acct_… id; payments settle in their
// Stripe; events arrive on the platform endpoint). Advanced fallback: paste a
// webhook SIGNING secret (sealed server-side, verify-only — it can't move
// money) and register the minted webhook URL manually. Money flows to their
// Stripe, never Privatae, on both lanes. Drives /sites/stripe* -> bridge
// webapp.stripe.* -> controller. Set a site to "Paywall" visibility to
// actually gate it.

const SUB_PILL: Record<string, string> = {
  active: "border-success/40 text-success",
  past_due: "border-warning/40 text-warning",
  canceled: "border-border text-muted-foreground",
};

const CONNECT_PILL: Record<string, string> = {
  active: "border-success/40 text-success",
  onboarding: "border-warning/40 text-warning",
  deauthorized: "border-destructive/40 text-destructive-foreground",
};

export function Payments() {
  const [st, setSt] = useState<StripeStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<{ text: string; err?: boolean } | null>(null);

  const [secret, setSecret] = useState("");
  const [pk, setPk] = useState("");
  const [saving, setSaving] = useState(false);
  const [editing, setEditing] = useState(false);
  const [copied, setCopied] = useState(false);
  const [onboarding, setOnboarding] = useState(false);
  const [advanced, setAdvanced] = useState(false);

  const load = useCallback(async () => {
    const r = await fetchStripe();
    if (r.ok) {
      setSt(r.data);
      setError(null);
    } else {
      setError(r.message);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function connect() {
    const ws = secret.trim();
    if (!ws.startsWith("whsec_")) {
      setNote({ text: "Signing secret must start with whsec_", err: true });
      return;
    }
    setSaving(true);
    const r = await connectStripe(ws, pk.trim() || undefined);
    setSaving(false);
    if (r.ok) {
      setSecret("");
      setPk("");
      setEditing(false);
      setNote({ text: "Stripe connected — register the webhook URL below." });
      void load();
    } else {
      setNote({ text: r.message, err: true });
    }
  }

  async function onboard() {
    setOnboarding(true);
    const r = await onboardStripe();
    setOnboarding(false);
    if (r.ok && r.data.url) {
      window.open(r.data.url, "_blank", "noopener");
      setNote({
        text: "Finish onboarding in the Stripe tab — your account links automatically when you're done. Come back and refresh to see it.",
      });
    } else if (!r.ok) {
      setNote({ text: r.message, err: true });
    }
  }

  function copyUrl(url: string) {
    void navigator.clipboard?.writeText(url).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  }

  if (error) {
    return (
      <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive-foreground">
        Couldn't load payment settings: {error}
      </div>
    );
  }
  if (st === null) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" /> Loading…
      </div>
    );
  }

  const connected = st.connected;
  const viaConnect = connected && st.mode === "connect";
  const showForm = !connected || editing;

  return (
    <div className="space-y-4">
      <div>
        <h2 className="flex items-center gap-1.5 text-sm font-semibold">
          <CreditCard className="size-4 text-primary" /> Payments
        </h2>
        <p className="mt-0.5 text-xs text-muted-foreground">
          Charge for access with your own Stripe account. Payments go straight
          to you — we never touch the money. Then set a site to{" "}
          <span className="text-foreground">Paywall</span> visibility.
        </p>
      </div>

      {note && (
        <div
          className={cn(
            "rounded-md px-3 py-1.5 text-xs",
            note.err
              ? "border border-destructive/40 bg-destructive/10 text-destructive-foreground"
              : "border border-border bg-card text-muted-foreground",
          )}
        >
          {note.text}
        </div>
      )}

      {connected && (
        <div className="rounded-md border border-border bg-card p-3">
          <div className="flex items-center justify-between">
            <span className="flex items-center gap-1.5 text-sm text-foreground">
              <Check className="size-4 text-success" /> Connected
              <span className="text-xs text-muted-foreground">
                ({viaConnect ? "Stripe Connect" : "manual webhook"})
              </span>
              {viaConnect && st.connect_status && (
                <span
                  className={cn(
                    "rounded-full border px-2 py-0.5 text-[11px]",
                    CONNECT_PILL[st.connect_status] ??
                      "border-border text-muted-foreground",
                  )}
                >
                  {st.connect_status}
                </span>
              )}
            </span>
            {viaConnect ? (
              <button
                type="button"
                disabled={onboarding}
                onClick={onboard}
                className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] text-foreground hover:bg-accent disabled:cursor-not-allowed disabled:opacity-50"
              >
                {onboarding && <Loader2 className="size-3 animate-spin" />}
                Reconnect
              </button>
            ) : (
              <button
                type="button"
                onClick={() => setEditing((v) => !v)}
                className="rounded-md border border-border px-2 py-1 text-[11px] text-foreground hover:bg-accent"
              >
                {editing ? "Cancel" : "Replace secret"}
              </button>
            )}
          </div>
          {viaConnect && st.stripe_account_id && (
            <div className="mt-2 text-xs text-muted-foreground">
              Linked account:{" "}
              <code className="text-foreground">{st.stripe_account_id}</code>
              {" — "}payouts and refunds live in your own Stripe dashboard.
            </div>
          )}
          {st.webhook_url && (
            <div className="mt-3">
              <div className="text-xs text-muted-foreground">
                Register this webhook URL in Stripe (
                <span className="text-foreground">
                  Developers → Webhooks → Add endpoint
                </span>
                ), sending checkout &amp; subscription events:
              </div>
              <div className="mt-1 flex items-center gap-2">
                <code className="min-w-0 flex-1 truncate rounded-md border border-border bg-background px-2 py-1 text-xs text-foreground">
                  {st.webhook_url}
                </code>
                <button
                  type="button"
                  onClick={() => copyUrl(st.webhook_url!)}
                  className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] text-foreground hover:bg-accent"
                >
                  {copied ? (
                    <Check className="size-3 text-success" />
                  ) : (
                    <Copy className="size-3" />
                  )}
                  {copied ? "Copied" : "Copy"}
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {showForm && (
        <div className="rounded-md border border-border bg-card p-3">
          <div className="text-xs text-muted-foreground">
            Link your own Stripe account through Stripe's hosted onboarding.
            Payments settle straight into your Stripe — no keys or secrets
            ever pass through this app.
          </div>
          <div className="mt-2">
            <button
              type="button"
              disabled={onboarding}
              onClick={onboard}
              className="flex items-center gap-1 rounded-md border border-primary bg-primary px-3 py-1.5 text-xs text-primary-foreground transition-colors hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {onboarding && <Loader2 className="size-3 animate-spin" />}
              Connect with Stripe (recommended)
            </button>
          </div>

          <button
            type="button"
            onClick={() => setAdvanced((v) => !v)}
            className="mt-3 flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          >
            {advanced ? (
              <ChevronDown className="size-3" />
            ) : (
              <ChevronRight className="size-3" />
            )}
            Advanced: manual webhook
          </button>
          {advanced && (
            <div className="mt-2 border-t border-border pt-2">
              <div className="text-xs text-muted-foreground">
                Prefer not to link the platform to your Stripe? In your Stripe
                dashboard, add a webhook endpoint and paste its{" "}
                <span className="text-foreground">Signing secret</span> (starts
                with <code className="text-foreground">whsec_</code>) here.
                It's verify-only — it can't move money — and your API keys
                never leave Stripe.
              </div>
              <input
                type="password"
                value={secret}
                onChange={(e) => setSecret(e.target.value)}
                placeholder="whsec_…"
                autoComplete="off"
                className="mt-2 w-full rounded-md border border-border bg-background px-2.5 py-1.5 text-sm outline-none focus:border-primary"
              />
              <input
                type="text"
                value={pk}
                onChange={(e) => setPk(e.target.value)}
                placeholder="Publishable key (optional, pk_…)"
                autoComplete="off"
                className="mt-2 w-full rounded-md border border-border bg-background px-2.5 py-1.5 text-sm outline-none focus:border-primary"
              />
              <div className="mt-2 flex justify-end">
                <button
                  type="button"
                  disabled={saving || !secret.trim()}
                  onClick={connect}
                  className="flex items-center gap-1 rounded-md border border-border px-3 py-1.5 text-xs text-foreground hover:bg-accent disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {saving && <Loader2 className="size-3 animate-spin" />}
                  {connected ? "Replace secret" : "Save signing secret"}
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      <div>
        <h3 className="text-xs font-semibold text-foreground">Subscribers</h3>
        {!st.subscribers || st.subscribers.length === 0 ? (
          <p className="mt-1 text-xs text-muted-foreground">
            No subscribers yet. They appear here once someone pays through your
            Checkout on a Paywall site.
          </p>
        ) : (
          <ul className="mt-1 divide-y divide-border overflow-hidden rounded-md border border-border">
            {st.subscribers.map((s: Subscriber) => (
              <li
                key={`${s.email}:${s.product_key}`}
                className="flex flex-wrap items-center gap-3 bg-card px-3 py-2"
              >
                <span className="min-w-0 flex-1 truncate text-sm text-foreground">
                  {s.email}
                </span>
                <span className="text-[11px] text-muted-foreground">
                  {s.product_key}
                </span>
                <span
                  className={cn(
                    "rounded-full border px-2 py-0.5 text-[11px]",
                    SUB_PILL[s.status] ?? "border-border text-muted-foreground",
                  )}
                >
                  {s.status}
                </span>
                {s.current_period_end && (
                  <span className="text-[11px] text-muted-foreground">
                    until {s.current_period_end.slice(0, 10)}
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      <a
        href="https://dashboard.stripe.com/webhooks"
        target="_blank"
        rel="noreferrer"
        className="inline-flex items-center gap-1 text-xs text-link hover:underline"
      >
        Open Stripe webhooks <ExternalLink className="size-3" />
      </a>
    </div>
  );
}
