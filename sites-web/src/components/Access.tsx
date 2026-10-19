import { useCallback, useEffect, useState } from "react";
import { Loader2, Plus, Trash2, Upload, UserCheck, UserX } from "lucide-react";
import {
  addCustomer,
  fetchCustomers,
  importCustomers,
  removeCustomer,
  type Customer,
} from "@/lib/api";
import { cn } from "@/lib/utils";

// Access (Phase 2b) — the owner's customer pool. Customers belong to the
// OWNER and span every site set to the "Members" visibility tier; membership
// is checked at the edge (the agent-synced entitlement store), never here.
// Drives /sites/customers* -> bridge webapp.customers.* -> controller.

const SOURCE_LABEL: Record<string, string> = {
  self_signup: "Signed up",
  owner_added: "Added",
  import: "Imported",
};

export function Access() {
  const [rows, setRows] = useState<Customer[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<{ text: string; err?: boolean } | null>(null);
  const [busy, setBusy] = useState<Record<string, boolean>>({});

  const [email, setEmail] = useState("");
  const [adding, setAdding] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [importText, setImportText] = useState("");
  const [importing, setImporting] = useState(false);

  const load = useCallback(async () => {
    const r = await fetchCustomers();
    if (r.ok) {
      setRows(r.data);
      setError(null);
    } else {
      setError(r.message);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function add() {
    const e = email.trim().toLowerCase();
    if (!e || adding) return;
    setAdding(true);
    const r = await addCustomer(e);
    setAdding(false);
    if (r.ok) {
      setEmail("");
      setNote({ text: `Added ${e}` });
      void load();
    } else {
      setNote({ text: r.message, err: true });
    }
  }

  async function toggle(c: Customer) {
    const next = c.status === "active" ? "suspended" : "active";
    setBusy((b) => ({ ...b, [c.email]: true }));
    const r = await addCustomer(c.email, next);
    setBusy((b) => ({ ...b, [c.email]: false }));
    if (r.ok) void load();
    else setNote({ text: r.message, err: true });
  }

  async function remove(c: Customer) {
    setBusy((b) => ({ ...b, [c.email]: true }));
    const r = await removeCustomer(c.email);
    setBusy((b) => ({ ...b, [c.email]: false }));
    if (r.ok) {
      setNote({ text: `Removed ${c.email}` });
      void load();
    } else {
      setNote({ text: r.message, err: true });
    }
  }

  async function runImport() {
    const emails = importText
      .split(/[\s,;]+/)
      .map((s) => s.trim().toLowerCase())
      .filter(Boolean);
    if (!emails.length || importing) return;
    setImporting(true);
    const r = await importCustomers(emails);
    setImporting(false);
    if (r.ok) {
      const inv = r.data.invalid?.length
        ? ` (${r.data.invalid.length} skipped)`
        : "";
      setNote({ text: `Imported ${r.data.imported}${inv}` });
      setImportText("");
      setImportOpen(false);
      void load();
    } else {
      setNote({ text: r.message, err: true });
    }
  }

  if (error) {
    return (
      <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive-foreground">
        Couldn't load customers: {error}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-sm font-semibold">Customers</h2>
        <p className="mt-0.5 text-xs text-muted-foreground">
          People who can reach any site you set to{" "}
          <span className="text-foreground">Members</span> visibility. Add them
          here, or let them sign up themselves when a Members site allows open
          signups.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <input
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && add()}
          placeholder="customer@example.com"
          className="min-w-0 flex-1 rounded-md border border-border bg-background px-2.5 py-1.5 text-sm outline-none focus:border-primary"
        />
        <button
          type="button"
          disabled={adding || !email.trim()}
          onClick={add}
          className="flex items-center gap-1 rounded-md border border-border px-2.5 py-1.5 text-xs text-foreground transition-colors hover:bg-accent disabled:cursor-not-allowed disabled:opacity-50"
        >
          {adding ? (
            <Loader2 className="size-3 animate-spin" />
          ) : (
            <Plus className="size-3" />
          )}
          Add
        </button>
        <button
          type="button"
          onClick={() => setImportOpen((v) => !v)}
          className="flex items-center gap-1 rounded-md border border-border px-2.5 py-1.5 text-xs text-foreground transition-colors hover:bg-accent"
        >
          <Upload className="size-3" /> Import
        </button>
      </div>

      {importOpen && (
        <div className="rounded-md border border-border bg-card p-3">
          <textarea
            value={importText}
            onChange={(e) => setImportText(e.target.value)}
            rows={4}
            placeholder="Paste emails — one per line, or comma-separated"
            className="w-full resize-y rounded-md border border-border bg-background px-2.5 py-1.5 text-sm outline-none focus:border-primary"
          />
          <div className="mt-2 flex justify-end gap-2">
            <button
              type="button"
              onClick={() => setImportOpen(false)}
              className="rounded-md border border-border px-2.5 py-1 text-xs text-foreground hover:bg-accent"
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={importing || !importText.trim()}
              onClick={runImport}
              className="flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-foreground hover:bg-accent disabled:cursor-not-allowed disabled:opacity-50"
            >
              {importing && <Loader2 className="size-3 animate-spin" />}
              Import list
            </button>
          </div>
        </div>
      )}

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

      {rows === null ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> Loading…
        </div>
      ) : rows.length === 0 ? (
        <div className="rounded-md border border-border bg-card px-4 py-6 text-center text-sm text-muted-foreground">
          <div className="font-medium text-foreground">No customers yet</div>
          Add one above, or set a site to Members with open signups.
        </div>
      ) : (
        <ul className="divide-y divide-border overflow-hidden rounded-md border border-border">
          {rows.map((c) => {
            const b = !!busy[c.email];
            const suspended = c.status === "suspended";
            return (
              <li
                key={c.email}
                className="flex flex-wrap items-center gap-3 bg-card px-3 py-2"
              >
                <span
                  className={cn(
                    "min-w-0 flex-1 truncate text-sm",
                    suspended
                      ? "text-muted-foreground line-through"
                      : "text-foreground",
                  )}
                >
                  {c.email}
                </span>
                <span className="rounded-full border border-border px-2 py-0.5 text-[11px] text-muted-foreground">
                  {SOURCE_LABEL[c.source] ?? c.source}
                </span>
                {suspended && (
                  <span className="rounded-full border border-warning/40 px-2 py-0.5 text-[11px] text-warning">
                    Suspended
                  </span>
                )}
                <button
                  type="button"
                  disabled={b}
                  onClick={() => toggle(c)}
                  title={suspended ? "Reactivate" : "Suspend"}
                  className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] text-foreground hover:bg-accent disabled:opacity-50"
                >
                  {b ? (
                    <Loader2 className="size-3 animate-spin" />
                  ) : suspended ? (
                    <UserCheck className="size-3" />
                  ) : (
                    <UserX className="size-3" />
                  )}
                  {suspended ? "Reactivate" : "Suspend"}
                </button>
                <button
                  type="button"
                  disabled={b}
                  onClick={() => remove(c)}
                  title="Remove"
                  className="rounded-md border border-destructive/40 px-2 py-1 text-[11px] text-destructive-foreground hover:bg-destructive/10 disabled:opacity-50"
                >
                  <Trash2 className="size-3" />
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
