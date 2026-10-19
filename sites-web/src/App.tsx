import { useState } from "react";
import {
  CreditCard,
  Database as DbIcon,
  Globe,
  HardDrive,
  Link2,
  Store,
  Users,
} from "lucide-react";
import { SitesList } from "@/components/SitesList";
import { Catalog } from "@/components/Catalog";
import { Domains } from "@/components/Domains";
import { Access } from "@/components/Access";
import { Payments } from "@/components/Payments";
import { Database } from "@/components/Database";
import { Data } from "@/components/Data";
import { cn } from "@/lib/utils";

// Sites — React/shadcn control panel over the caller-scoped
// /api/applications/sites routes. My Sites (list/Overview + Visibility flip),
// Domains, Access (2b customer pools) and Payments (3
// BYO-Stripe). Access + Payments are owner-scoped (they span all the owner's
// Members/Paywall sites); the per-site tier is set in each card's Visibility.
// There is NO upload surface here (2026-07-11): publishing is
// artifact-from-source only — Workbench "Publish as site" / the host's
// publish_site tool.

type View =
  | "sites"
  | "catalog"
  | "domains"
  | "access"
  | "payments"
  | "database"
  | "data";

const TABS: { id: View; label: string; icon: typeof Globe }[] = [
  { id: "sites", label: "My Sites", icon: Globe },
  { id: "catalog", label: "Catalog", icon: Store },
  { id: "domains", label: "Domains", icon: Link2 },
  { id: "access", label: "Access", icon: Users },
  { id: "payments", label: "Payments", icon: CreditCard },
  { id: "database", label: "Database", icon: DbIcon },
  { id: "data", label: "Data", icon: HardDrive },
];

export default function App() {
  const [view, setView] = useState<View>("sites");

  return (
    <div className="flex h-full flex-col bg-background text-foreground">
      <header className="flex items-center gap-3 border-b border-border px-3 py-3 md:px-4">
        <Globe className="size-5 shrink-0 text-primary" />
        <h1 className="hidden text-sm font-semibold md:block">Sites</h1>
        {/* Phone: the five tabs don't fit 390px — the nav scrolls; the "Sites"
            title yields (the window titlebar already names the app). */}
        <nav className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto md:ml-4 md:flex-none md:overflow-visible">
          {TABS.map((t) => {
            const Icon = t.icon;
            return (
              <button
                key={t.id}
                type="button"
                onClick={() => setView(t.id)}
                className={cn(
                  "flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-md px-2.5 py-1 text-xs transition-colors",
                  view === t.id
                    ? "bg-secondary text-secondary-foreground"
                    : "text-muted-foreground hover:bg-accent hover:text-foreground",
                )}
              >
                <Icon className="size-3.5" />
                {t.label}
              </button>
            );
          })}
        </nav>
      </header>

      <main className="min-h-0 flex-1 overflow-auto p-4">
        {view === "sites" && <SitesList />}
        {view === "catalog" && <Catalog />}
        {view === "domains" && <Domains />}
        {view === "access" && <Access />}
        {view === "payments" && <Payments />}
        {view === "database" && <Database />}
        {view === "data" && <Data />}
      </main>
    </div>
  );
}
