import { useCallback, useEffect, useState } from "react";
import { ExternalLink, Loader2, Plus, Trash2 } from "lucide-react";
import {
  addDomain,
  deleteRecord,
  domainAction,
  fetchDomains,
  fetchRecords,
  fetchSites,
  setRecord,
  type DnsRecordSet,
  type Domain,
  type Site,
} from "@/lib/api";
import { cn } from "@/lib/utils";

// Unit 4 — Custom domains. Faithful port of the vanilla domains flow
// (app.js:302-533): domain add, per-domain DNS instructions, verify/remove,
// and the DNS-records modal. Endpoints unchanged; all under BASE, gated
// publish:sites.
//
// "Connect" is the only lane. Nameserver delegation (Lane A) is retired;
// the backend answers 410 for it. The
// isDelegate read-path below is kept only so a legacy row still renders
// rather than showing a blank panel — nothing can create one any more.

const DOMAIN_STATUS: Record<string, string> = {
  pending: "Awaiting DNS",
  verified: "Verified",
  live: "Live",
  failed: "Failed",
};
const DNS_HINT: Record<string, string> = {
  A: "203.0.113.10",
  AAAA: "2001:db8::1",
  CNAME: "target.example.com",
  TXT: "verification value",
  MX: "10 mail.example.com",
};

function statusPill(st: string): string {
  if (st === "verified" || st === "live") return "border-success/40 text-success";
  if (st === "failed") return "border-destructive/40 text-destructive-foreground";
  return "border-warning/40 text-warning";
}

export function Domains() {
  const [domains, setDomains] = useState<Domain[] | null>(null);
  const [sites, setSites] = useState<Site[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<{ text: string; err?: boolean } | null>(null);

  const [slug, setSlug] = useState("");
  const [input, setInput] = useState("");
  const [adding, setAdding] = useState(false);
  const [dnsFor, setDnsFor] = useState<string | null>(null);

  const load = useCallback(async () => {
    const d = await fetchDomains();
    if (d.ok) {
      setDomains(d.data);
      setError(null);
    } else {
      setError(d.message);
    }
    const s = await fetchSites();
    if (s.ok) setSites(s.data);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const selectable = sites.filter(
    (s) => s.status === "live" || s.status === "pending",
  );
  // keep the site selector valid
  useEffect(() => {
    if (!slug && selectable.length) setSlug(selectable[0].slug);
  }, [slug, selectable]);

  async function add() {
    const domain = input.trim().toLowerCase();
    if (!slug || !domain) return;
    setAdding(true);
    const r = await addDomain(slug, domain);
    setAdding(false);
    if (r.ok) {
      setInput("");
      setNote({ text: "Added — add the DNS records below, then Verify." });
      void load();
    } else {
      setNote({ text: `Failed: ${r.message}`, err: true });
    }
  }

  async function act(action: "verify" | "remove", domain: string) {
    const r = await domainAction(action, domain);
    if (r.ok && action === "verify" && r.data.status !== "verified") {
      setNote({
        text: `Not verified yet: ${r.data.error || "check your DNS records"}`,
        err: true,
      });
    } else if (r.ok) {
      setNote({ text: `${action} ${domain} ✓` });
    } else {
      setNote({ text: `${action} failed: ${r.message}`, err: true });
    }
    void load();
  }

  return (
    <div className="space-y-4">
      {/* Add form */}
      <div className="rounded-md border border-border bg-card p-3">
        <div className="flex flex-wrap items-end gap-2">
          <div>
            <label className="text-xs text-muted-foreground">Site</label>
            <select
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              className="mt-1 block rounded-md border border-border bg-background px-2 py-1.5 text-sm"
            >
              {selectable.map((s) => (
                <option key={s.site_id} value={s.slug}>
                  {s.hostname}
                </option>
              ))}
            </select>
          </div>
          <div className="min-w-[12rem] flex-1">
            <label className="text-xs text-muted-foreground">Domain</label>
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="www.example.com"
              className="mt-1 block w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm outline-none"
            />
          </div>
          <button
            type="button"
            disabled={adding || !slug || !input.trim()}
            onClick={add}
            className="flex items-center gap-1 rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground disabled:opacity-50"
          >
            {adding ? (
              <Loader2 className="size-4 animate-spin" />
            ) : (
              <Plus className="size-4" />
            )}
            Add
          </button>
        </div>
        <p className="mt-2 text-xs text-muted-foreground">
          Add a TXT + routing record at your registrar. You keep your own
          nameservers — we never take over your DNS.
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

      {/* Domain list */}
      {error ? (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive-foreground">
          Couldn't load domains: {error}
        </div>
      ) : domains === null ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> Loading domains…
        </div>
      ) : domains.length === 0 ? (
        <div className="rounded-md border border-border bg-card px-4 py-6 text-center text-sm text-muted-foreground">
          <div className="font-medium text-foreground">No custom domains</div>
          Add one above to serve a site on your own domain.
        </div>
      ) : (
        <ul className="space-y-2">
          {domains.map((d) => (
            <DomainCard
              key={d.domain}
              d={d}
              onAct={act}
              onDns={() => setDnsFor(d.domain)}
            />
          ))}
        </ul>
      )}

      {dnsFor && <DnsModal domain={dnsFor} onClose={() => setDnsFor(null)} />}
    </div>
  );
}

function DomainCard({
  d,
  onAct,
  onDns,
}: {
  d: Domain;
  onAct: (action: "verify" | "remove", domain: string) => void;
  onDns: () => void;
}) {
  const st = d.status || "pending";
  const inst = d.instructions ?? {};
  const isDelegate = inst.method === "delegate" || d.connect_method === "delegate";
  const needsDns = st === "pending" || st === "failed";
  const linked = st === "live" || st === "verified";

  return (
    <li className="rounded-md border border-border bg-card px-4 py-3">
      <div className="flex flex-wrap items-center gap-2">
        {linked ? (
          <a
            href={`https://${d.domain}`}
            target="_blank"
            rel="noreferrer"
            className="flex items-center gap-1 text-sm font-medium text-link hover:underline"
          >
            {d.domain}
            <ExternalLink className="size-3" />
          </a>
        ) : (
          <span className="text-sm font-medium text-foreground">{d.domain}</span>
        )}
        <span
          className={cn(
            "rounded-full border px-2 py-0.5 text-xs",
            statusPill(st),
          )}
        >
          {DOMAIN_STATUS[st] ?? st}
        </span>
        <span className="text-xs text-muted-foreground">→ {d.slug}</span>
        <div className="ml-auto flex items-center gap-2">
          {needsDns && (
            <button
              type="button"
              onClick={() => onAct("verify", d.domain)}
              className="rounded-md bg-primary px-2.5 py-1 text-xs text-primary-foreground"
            >
              Verify
            </button>
          )}
          {isDelegate && (
            <button
              type="button"
              onClick={onDns}
              className="rounded-md border border-border px-2.5 py-1 text-xs text-foreground hover:bg-accent"
            >
              DNS records
            </button>
          )}
          <button
            type="button"
            onClick={() => onAct("remove", d.domain)}
            className="rounded-md border border-destructive/40 px-2.5 py-1 text-xs text-destructive-foreground hover:bg-destructive/10"
          >
            Remove
          </button>
        </div>
      </div>

      {needsDns && (
        <div className="mt-2 space-y-1 border-t border-border pt-2 text-xs">
          {isDelegate ? (
            <>
              <div className="text-muted-foreground">
                Set these nameservers at your registrar, then Verify:
              </div>
              {(inst.nameservers ?? []).map((ns) => (
                <Rec key={ns} kind="NS" cells={[ns]} />
              ))}
              {inst.ds && inst.ds.length > 0 && (
                <>
                  <div className="text-muted-foreground">
                    Optional — for DNSSEC, also add this DS record:
                  </div>
                  <Rec kind="DS" cells={[inst.ds[0]]} />
                </>
              )}
            </>
          ) : (
            <>
              <div className="text-muted-foreground">
                Add these at your registrar, then Verify:
              </div>
              {inst.txt && (
                <Rec kind="TXT" cells={[inst.txt.name, inst.txt.value]} />
              )}
              {inst.route && (
                <Rec
                  kind={inst.route.type}
                  cells={[inst.route.name, inst.route.value]}
                />
              )}
            </>
          )}
          {d.last_error && (
            <div className="text-destructive-foreground">{d.last_error}</div>
          )}
        </div>
      )}
    </li>
  );
}

function Rec({ kind, cells }: { kind: string; cells: string[] }) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="rounded bg-secondary px-1.5 py-0.5 font-mono text-[10px] text-secondary-foreground">
        {kind}
      </span>
      {cells.map((c, i) => (
        <code
          key={i}
          className="break-all rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-foreground"
        >
          {c}
        </code>
      ))}
    </div>
  );
}

function DnsModal({ domain, onClose }: { domain: string; onClose: () => void }) {
  const [records, setRecords] = useState<DnsRecordSet[] | null>(null);
  const [type, setType] = useState("A");
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [status, setStatus] = useState<string | null>(null);

  const load = useCallback(async () => {
    const r = await fetchRecords(domain);
    if (r.ok) setRecords(r.data);
    else setStatus(r.message);
  }, [domain]);

  useEffect(() => {
    void load();
  }, [load]);

  async function add() {
    if (!name || !value) return;
    setStatus("Saving…");
    const r = await setRecord(domain, { type, name, value });
    setStatus(r.ok ? null : `Failed: ${r.message}`);
    if (r.ok) {
      setName("");
      setValue("");
      void load();
    }
  }
  async function del(rec: DnsRecordSet) {
    const r = await deleteRecord(domain, { name: rec.name, type: rec.type });
    if (r.ok) void load();
    else setStatus(`Delete failed: ${r.message}`);
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-lg rounded-lg border border-border bg-popover p-4 text-popover-foreground shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-sm font-semibold">DNS records — {domain}</h2>

        <div className="mt-3 flex flex-wrap items-end gap-2">
          <select
            value={type}
            onChange={(e) => setType(e.target.value)}
            className="rounded-md border border-border bg-background px-2 py-1.5 text-sm"
          >
            {Object.keys(DNS_HINT).map((t) => (
              <option key={t}>{t}</option>
            ))}
          </select>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="name (@ for root)"
            className="w-28 rounded-md border border-border bg-background px-2 py-1.5 text-sm outline-none"
          />
          <input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={DNS_HINT[type] ?? "value"}
            className="min-w-0 flex-1 rounded-md border border-border bg-background px-2 py-1.5 text-sm outline-none"
          />
          <button
            type="button"
            disabled={!name || !value}
            onClick={add}
            className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground disabled:opacity-50"
          >
            Add
          </button>
        </div>

        <div className="mt-3 max-h-60 overflow-auto">
          {records === null ? (
            <div className="text-xs text-muted-foreground">Loading records…</div>
          ) : records.length === 0 ? (
            <div className="text-xs text-muted-foreground">No records yet.</div>
          ) : (
            <ul className="space-y-1">
              {records.map((r, i) => (
                <li
                  key={`${r.name}-${r.type}-${i}`}
                  className="flex items-center gap-2 rounded border border-border px-2 py-1 text-xs"
                >
                  <span className="font-mono text-muted-foreground">
                    {r.type}
                  </span>
                  <code className="text-foreground">{r.name}</code>
                  <code className="min-w-0 flex-1 truncate text-muted-foreground">
                    {r.records.join(", ")}
                  </code>
                  {r.managed ? (
                    <span
                      title="Managed for your site"
                      className="rounded bg-secondary px-1.5 py-0.5 text-[10px] text-secondary-foreground"
                    >
                      site
                    </span>
                  ) : (
                    <button
                      type="button"
                      onClick={() => del(r)}
                      className="text-destructive-foreground hover:opacity-80"
                    >
                      <Trash2 className="size-3.5" />
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>

        {status && (
          <div className="mt-2 text-xs text-muted-foreground">{status}</div>
        )}
        <div className="mt-4 flex justify-end">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-border px-2.5 py-1 text-xs text-foreground hover:bg-accent"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
