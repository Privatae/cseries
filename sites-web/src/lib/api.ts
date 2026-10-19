// Sites API layer. Same request/ApiResult conventions as circles-web /
// workbench-web — every endpoint here is a caller-scoped kernel route under
// /api/applications/sites that the vanilla Sites app already exercised
// (server/routes/applications.py: sites_* handlers -> bridge webapp.* ops).
// entity_id is fixed by the bridge mTLS handshake; never sent from here.

export const BASE = "/api/applications/sites";

// Shared client core — see app-web-kit/api.ts (ApiResult, the 401-retry
// request loop, getJSON). Endpoint functions below stay app-local.
export type { ApiResult } from "@kit/api";
import { getJSON, request, type ApiResult } from "@kit/api";

// ---- Types ----

export type ExposureTier =
  | "public"
  | "ip_allowlist"
  | "password"
  | "authenticated"
  | "customers"
  | "paywall";

export interface Exposure {
  tier: ExposureTier;
  bot_deterrence?: boolean;
  allow_emails?: string[];
  allow_ips?: string[];
  // customers tier (2b)
  signup?: "open" | "invite";
  // paywall tier (3)
  checkout_url?: string;
  product_key?: string;
  grace_days?: number;
  // Search engines may index the site only when this is true. Absent = not
  // indexed: the zone sends X-Robots-Tag: noindex and a deny-all robots.txt.
  // A policy that omits it keeps the stored value (controller set_exposure).
  indexable?: boolean;
}

export interface Site {
  site_id: number;
  slug: string;
  zone?: string;
  hostname: string;
  status: string; // pending | live | suspended | retired
  live_version_id?: number | null;
  latest_version_id?: number | null;
  last_error?: string | null;
  bundle_sha256?: string | null;
  bundle_size_bytes?: number | null;
  published_at?: string | null;
  created_at?: string;
  updated_at?: string;
  exposure?: Exposure; // Batch 1 list_sites enrichment
  // Catalog apps (service catalog): kind='catalog' + the template it runs.
  kind?: "bundle" | "catalog";
  template_key?: string | null;
}

export interface AnalyticsSeriesPoint {
  hour: string;
  count: number;
}
export interface AnalyticsEntry {
  series: AnalyticsSeriesPoint[];
  total: number;
}
// hostname -> { series, total } (normalized from the raw {series,totals} response)
export type Analytics = Record<string, AnalyticsEntry>;

// ---- Read endpoints (ungated: read:sites) ----

export async function fetchSites(): Promise<ApiResult<Site[]>> {
  const r = await getJSON<Site[] | { sites: Site[] }>(`${BASE}/list`);
  if (!r.ok) return r;
  const list = Array.isArray(r.data) ? r.data : (r.data.sites ?? []);
  return { ok: true, data: list };
}

interface RawAnalytics {
  series?: Record<string, AnalyticsSeriesPoint[]>;
  totals?: Record<string, number>;
}

/** GET /analytics?hours — best-effort; normalizes the raw {series,totals}
 * shape into hostname -> {series, total} (mirrors the vanilla loadAnalytics). */
export async function fetchAnalytics(hours = 168): Promise<ApiResult<Analytics>> {
  const r = await getJSON<RawAnalytics>(`${BASE}/analytics?hours=${hours}`);
  if (!r.ok) return r;
  const series = r.data.series ?? {};
  const totals = r.data.totals ?? {};
  const out: Analytics = {};
  for (const h of Object.keys(series)) {
    out[h] = { series: series[h] ?? [], total: totals[h] ?? 0 };
  }
  return { ok: true, data: out };
}

// ---- Lifecycle actions (Overview tab; POST, gated publish:sites) ----

export type SiteAction = "suspend" | "resume" | "restart" | "unpublish";

/** POST /{action} {slug} — the site pod-control ops the bridge forwards to
 * the controller (webapp.suspend/resume/restart/unpublish). */
export function siteAction(
  action: SiteAction,
  slug: string,
): Promise<ApiResult<unknown>> {
  return request("POST", `${BASE}/${action}`, { slug });
}

// ---- Visibility / exposure (the flip; POST, gated publish:sites) ----

export interface ExposurePolicy {
  tier: ExposureTier;
  // For `authenticated` with allow_emails OMITTED, the bridge seeds the
  // caller's own verified email, so only the owner can open it. Passing an
  // explicit non-empty list scopes to those addresses instead.
  allow_emails?: string[];
  allow_ips?: string[];
  password?: { secret?: string; hash?: string; user?: string };  // customers tier: open lets logged-in non-members self-request access
  // (the /__cseries-join edge route); invite = owner-added only.
  signup?: "open" | "invite";
  // paywall tier: checkout_url is REQUIRED (https), product_key groups a
  // product across sites, grace_days covers webhook/dunning lag.
  checkout_url?: string;
  product_key?: string;
  grace_days?: number;
  // Search engines may index the site only when this is true. Absent = not
  // indexed: the zone sends X-Robots-Tag: noindex and a deny-all robots.txt.
  // A policy that omits it keeps the stored value (controller set_exposure).
  indexable?: boolean;
}

export interface ExposureResult {
  site_id: number;
  hostname: string;
  // Normalized policy the server stored (seeded emails visible here). Optional
  // and guarded at the callsite — the controller echo shape isn't guaranteed.
  exposure?: Exposure;
}

/** POST /exposure {slug, policy} — pure pass-through to the bridge
 * webapp.set_exposure op (creator-seed + fail-closed live in the bridge). The
 * response's exposure.allow_emails reveals the seeded creator email, so the UI
 * can show WHICH account to log in with. */
export function setExposure(
  slug: string,
  policy: ExposurePolicy,
): Promise<ApiResult<ExposureResult>> {
  return request("POST", `${BASE}/exposure`, { slug, policy });
}

// ---- Custom domains (gated publish:sites; reads ungated) ----

export interface DnsInstructions {
  method?: "connect" | "delegate";
  nameservers?: string[];
  ds?: string[];
  txt?: { name: string; value: string };
  route?: { type: string; name: string; value: string };
}
export interface Domain {
  domain: string;
  slug: string;
  status: string; // pending | verified | live | failed
  connect_method?: "connect" | "delegate";
  instructions?: DnsInstructions;
  last_error?: string | null;
}
// A record-SET as the controller returns it: one row per (name, type) with
// its list of values, plus `managed` for rows the platform owns (the site's
// own A/AAAA routing — deleting these takes the site dark, so the UI must
// not offer delete on them; this UI guard is the only guard, kernel + bridge
// don't block it).
export interface DnsRecordSet {
  name: string;
  type: string;
  records: string[]; // values
  managed?: boolean;
}

export async function fetchDomains(): Promise<ApiResult<Domain[]>> {
  const r = await getJSON<{ domains?: Domain[] }>(`${BASE}/domains`);
  if (!r.ok) return r;
  return { ok: true, data: r.data.domains ?? [] };
}

// Nameserver delegation (Lane A) is retired: there is no authoritative
// server left to delegate to. The backend now answers 410
// for method='delegate'; "connect" is the only lane.
export function addDomain(
  slug: string,
  domain: string,
  method: "connect" = "connect",
): Promise<ApiResult<Domain>> {
  return request("POST", `${BASE}/domains/add`, { slug, domain, method });
}

export function domainAction(
  action: "verify" | "remove",
  domain: string,
): Promise<ApiResult<{ status?: string; error?: string }>> {
  return request("POST", `${BASE}/domains/${action}`, { domain });
}

/** POST /domains/records {domain} — the list route reads {domain} from the
 * JSON body (POST, not GET). Returns record-SETS. */
export async function fetchRecords(
  domain: string,
): Promise<ApiResult<DnsRecordSet[]>> {
  const r = await request<{ records?: DnsRecordSet[] }>(
    "POST",
    `${BASE}/domains/records`,
    { domain },
  );
  if (!r.ok) return r;
  return { ok: true, data: r.data.records ?? [] };
}

/** POST /domains/records/set {domain, name, type, values:[...]} — the route
 * requires a non-empty `values` list; name defaults to '@' (the root). */
export function setRecord(
  domain: string,
  rec: { name: string; type: string; value: string },
): Promise<ApiResult<unknown>> {
  return request("POST", `${BASE}/domains/records/set`, {
    domain,
    name: rec.name.trim() || "@",
    type: rec.type,
    values: [rec.value],
  });
}

/** POST /domains/records/delete {domain, name, type}. */
export function deleteRecord(
  domain: string,
  rec: { name: string; type: string },
): Promise<ApiResult<unknown>> {
  return request("POST", `${BASE}/domains/records/delete`, {
    domain,
    name: rec.name,
    type: rec.type,
  });
}

// ---- Customer pools (2b — the Access tab; owner-scoped, spans all the
//      owner's Members-gated sites). Gated publish:sites. ----

export interface Customer {
  email: string;
  status: "active" | "suspended";
  source: "self_signup" | "owner_added" | "import";
  authentik_subject?: string | null;
  created_at?: string;
  updated_at?: string;
}

export async function fetchCustomers(): Promise<ApiResult<Customer[]>> {
  const r = await getJSON<{ customers?: Customer[] }>(`${BASE}/customers`);
  if (!r.ok) return r;
  return { ok: true, data: r.data.customers ?? [] };
}

/** POST /customers/add {email, status?} — upsert; status:"suspended" doubles
 * as suspend, "active" as reactivate. */
export function addCustomer(
  email: string,
  status?: "active" | "suspended",
): Promise<ApiResult<unknown>> {
  return request(
    "POST",
    `${BASE}/customers/add`,
    status ? { email, status } : { email },
  );
}

export function removeCustomer(email: string): Promise<ApiResult<unknown>> {
  return request("POST", `${BASE}/customers/remove`, { email });
}

/** POST /customers/import {emails:[...]} — bulk upsert; invalid addresses are
 * reported, never fatal. */
export function importCustomers(
  emails: string[],
): Promise<ApiResult<{ imported: number; invalid: string[] }>> {
  return request("POST", `${BASE}/customers/import`, { emails });
}

// ---- BYO-Stripe (3 — the Payments tab; owner-scoped, one connection per
//      owner). Gated publish:sites. Money flows to the OWNER's Stripe. ----

export interface Subscriber {
  email: string;
  product_key: string;
  status: "active" | "past_due" | "canceled";
  current_period_end?: string | null;
}

export interface StripeStatus {
  connected: boolean;
  mode?: string; // "connect" (hosted onboarding) | "byo_webhook" (manual paste)
  status?: string;
  connect_status?: "onboarding" | "active" | "deauthorized" | null;
  stripe_account_id?: string | null; // acct_… — an identifier, not a secret
  webhook_url?: string; // byo_webhook rows only; Connect uses the platform endpoint
  subscribers?: Subscriber[];
}

export function fetchStripe(): Promise<ApiResult<StripeStatus>> {
  return getJSON<StripeStatus>(`${BASE}/stripe`);
}

/** POST /stripe/connect {webhook_secret, publishable_key?} — stores the
 * owner's whsec_ signing secret (sealed server-side, never echoed) and returns
 * the webhook URL to register in their Stripe dashboard. */
export function connectStripe(
  webhook_secret: string,
  publishable_key?: string,
): Promise<ApiResult<{ connected: boolean; webhook_url: string }>> {
  return request("POST", `${BASE}/stripe/connect`, {
    webhook_secret,
    publishable_key: publishable_key ?? "",
  });
}

/** POST /stripe/onboard — starts Stripe Connect onboarding (the recommended
 * lane). Returns the Stripe-hosted URL to open in a new tab; the account
 * links automatically when onboarding completes. 409 until the platform has
 * Connect configured. */
export function onboardStripe(): Promise<ApiResult<{ url: string }>> {
  return request("POST", `${BASE}/stripe/onboard`, {});
}

// ---- Managed Postgres (SDK station 4 — the Database tab; one managed DB per
//      site, owner-scoped, gated publish:sites). The zone provisions it; the
//      controller seals the DATABASE_URL for a deliberate, audited reveal. ----

export type DatabaseStatus = "pending" | "active" | "error" | "deleting";

export interface SiteDatabase {
  db_id: number;
  site_id: number;
  slug: string;
  db_name: string;
  status: DatabaseStatus;
  last_error?: string | null;
  has_url: boolean;
  size_bytes?: number | null;
  created_at?: string | null;
}

export async function fetchDatabases(): Promise<ApiResult<SiteDatabase[]>> {
  const r = await getJSON<{ databases?: SiteDatabase[] }>(`${BASE}/db`);
  if (!r.ok) return r;
  return { ok: true, data: r.data.databases ?? [] };
}

/** POST /db/attach {slug} — request a managed Postgres DB for a site.
 * Idempotent: re-attaching returns the current status, never a second DB. */
export function attachDatabase(
  slug: string,
): Promise<ApiResult<{ attached: boolean; status: DatabaseStatus; db_name: string }>> {
  return request("POST", `${BASE}/db/attach`, { slug });
}

/** POST /db/detach {slug} — tear down a site's managed DB. DESTRUCTIVE of the
 * data in it; the caller confirms first. */
export function detachDatabase(slug: string): Promise<ApiResult<unknown>> {
  return request("POST", `${BASE}/db/detach`, { slug });
}

/** POST /db/reveal {slug} — reveal the sealed DATABASE_URL (deliberate +
 * audited). Only an 'active' DB reveals. The UI copies this straight to the
 * clipboard and never renders it — on-screen reveal lives in the Keys app
 * behind the TOTP step-up. */
export function revealDatabase(
  slug: string,
): Promise<ApiResult<{ conn_url: string }>> {
  return request("POST", `${BASE}/db/reveal`, { slug });
}

/** POST /db/save-to-keys {slug} — deposit the DATABASE_URL into the Keys
 * vault server-side; the plaintext never reaches the browser. Returns the
 * vault entry name + a masked URL for display (masked_url is null when the
 * URL couldn't be masked safely — display nothing in that case). */
export function saveDatabaseToKeys(
  slug: string,
): Promise<ApiResult<{ ok: boolean; name: string; masked_url: string | null }>> {
  return request("POST", `${BASE}/db/save-to-keys`, { slug });
}

// ---- Backups / restore (SDK station 5). Dumps route up through the controller
//      → S3; restore reverses it. ----

export type BackupStatus = "pending" | "done" | "error";

export interface DbBackup {
  backup_id: number;
  kind: "manual" | "scheduled";
  status: BackupStatus;
  size_bytes?: number | null;
  last_error?: string | null;
  created_at?: string | null;
  completed_at?: string | null;
}

export interface DbRestore {
  restore_id: number;
  backup_id: number;
  status: BackupStatus;
  last_error?: string | null;
  created_at?: string | null;
  completed_at?: string | null;
}

export async function fetchBackups(
  slug: string,
): Promise<ApiResult<{ backups: DbBackup[]; restores: DbRestore[] }>> {
  const r = await getJSON<{ backups?: DbBackup[]; restores?: DbRestore[] }>(
    `${BASE}/db/backups?slug=${encodeURIComponent(slug)}`,
  );
  if (!r.ok) return r;
  return {
    ok: true,
    data: { backups: r.data.backups ?? [], restores: r.data.restores ?? [] },
  };
}

/** POST /db/backup {slug} — queue a backup of a site's managed DB. */
export function backupDatabase(
  slug: string,
): Promise<ApiResult<{ queued: boolean; backup_id: number }>> {
  return request("POST", `${BASE}/db/backup`, { slug });
}

/** POST /db/restore {slug, backup_id} — restore a backup into the DB.
 * DESTRUCTIVE of current data; the caller confirms first. */
export function restoreDatabase(
  slug: string,
  backup_id: number,
): Promise<ApiResult<{ queued: boolean; restore_id: number }>> {
  return request("POST", `${BASE}/db/restore`, { slug, backup_id });
}

// ---- Zone handle (your corner of the web: every new site lands under
//      the sites zone). Claimed once, permanent. ----

export interface ZoneHandle {
  handle: string | null;
  zone: string;
  // The bare zone. Kept
  // nullable because an older controller answers null for an owner with no
  // claimed handle, and the caller must not assume the new shape.
  suffix: string | null;
}

export function fetchHandle(): Promise<ApiResult<ZoneHandle>> {
  return getJSON<ZoneHandle>(`${BASE}/handle`);
}

// ---- Service catalog (the Catalog tab — one-click third-party apps).
//      Browse is ungated; deploy is gated publish:sites. A catalog app is a
//      site: hostname, Visibility, Domains all work the same afterward. ----

export interface CatalogTemplate {
  template_key: string;
  display_name: string;
  header: {
    slogan?: string;
    category?: string;
    tags?: string[];
    documentation?: string;
    port?: number;
  };
  // ${VAR:-default} knobs: var name -> default ("" = blank unless supplied)
  settings: Record<string, string>;
  stateful: boolean;
}

export async function fetchCatalog(): Promise<ApiResult<CatalogTemplate[]>> {
  const r = await getJSON<{ templates?: CatalogTemplate[] }>(
    `${BASE}/catalog/templates`,
  );
  if (!r.ok) return r;
  return { ok: true, data: r.data.templates ?? [] };
}

/** POST /catalog/deploy {slug, template_key, settings?} — the app is born
 * PRIVATE (authenticated tier); flip it public in Visibility when ready. */
export function deployCatalogApp(
  slug: string,
  template_key: string,
  settings?: Record<string, string>,
): Promise<ApiResult<{ site_id: number; hostname: string; status: string }>> {
  return request("POST", `${BASE}/catalog/deploy`, {
    slug,
    template_key,
    ...(settings && Object.keys(settings).length ? { settings } : {}),
  });
}

// ---- Data volumes (the Data tab — catalog apps' PVC backup/restore; the
//      volume sibling of the db lanes). Gated publish:sites. ----

export interface VolumeBackup {
  backup_id: number;
  volume: string;
  kind: "manual" | "scheduled";
  status: BackupStatus;
  size_bytes?: number | null;
  last_error?: string | null;
  created_at?: string | null;
  completed_at?: string | null;
}

export interface VolumeRestore {
  restore_id: number;
  backup_id: number;
  volume: string;
  status: BackupStatus;
  last_error?: string | null;
  created_at?: string | null;
  completed_at?: string | null;
}

export async function fetchVolumes(slug: string): Promise<
  ApiResult<{ volumes: string[]; backups: VolumeBackup[]; restores: VolumeRestore[] }>
> {
  const r = await getJSON<{
    volumes?: string[];
    backups?: VolumeBackup[];
    restores?: VolumeRestore[];
  }>(`${BASE}/volumes?slug=${encodeURIComponent(slug)}`);
  if (!r.ok) return r;
  return {
    ok: true,
    data: {
      volumes: r.data.volumes ?? [],
      backups: r.data.backups ?? [],
      restores: r.data.restores ?? [],
    },
  };
}

/** POST /volumes/backup {slug, volume?} — omit volume to back up every data
 * volume the app declares (one job per volume). */
export function backupVolumes(
  slug: string,
  volume?: string,
): Promise<ApiResult<{ queued: boolean; backup_ids: number[]; volumes: string[] }>> {
  return request("POST", `${BASE}/volumes/backup`, volume ? { slug, volume } : { slug });
}

/** POST /volumes/restore {slug, backup_id} — DESTRUCTIVE of the volume's
 * current contents; the caller confirms first. */
export function restoreVolume(
  slug: string,
  backup_id: number,
): Promise<ApiResult<{ queued: boolean; restore_id: number }>> {
  return request("POST", `${BASE}/volumes/restore`, { slug, backup_id });
}
