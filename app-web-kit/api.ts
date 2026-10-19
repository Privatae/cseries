// app-web-kit/api.ts — the shared client core for the React system apps
// (circles, workbench, community, sites). Extracted 2026-07-16: four apps
// carried byte-identical copies of this block, and the app-token race fix
// had to be re-applied to each one by hand (workbench 0.6.3 -> circles
// 0.4.9 -> community/sites). One copy now.
//
// Every call returns a discriminated ApiResult so panes render a real error
// state instead of a silent empty/"Loading…". Auth is same-origin: the app
// window carries the launch token the kernel mints on launch_app.

export type ApiResult<T> =
  | { ok: true; data: T }
  | { ok: false; status: number; message: string };

// Exported because app-local endpoint functions need it too, not only the
// request loop below: workbench's uploadContextFile posts multipart/form-data
// and so cannot go through request(), but still has to turn a non-OK response
// into the same message shape. It was module-private when the four copies were
// per-app and each app had its own; the extraction has to keep it reachable.
export async function extractDetail(res: Response): Promise<string> {
  try {
    const j = (await res.json()) as {
      detail?: string | { message?: string };
      error?: string | { message?: string };
    };
    const d = j.detail ?? j.error;
    if (typeof d === "string") return d;
    if (d && typeof d === "object" && typeof d.message === "string") {
      return d.message;
    }
  } catch {
    /* non-JSON body */
  }
  return "";
}

export const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  timeoutMs = 30000,
  _attempt = 0,
): Promise<ApiResult<T>> {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(path, {
      method,
      signal: ctrl.signal,
      headers: body
        ? { "Content-Type": "application/json", Accept: "application/json" }
        : { Accept: "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    // App-token delivery race: the shell wraps this iframe's fetch on 'load',
    // which can be after our mount-time fetches. A token-less call 401s
    // ("application token required"); back off briefly and retry so the shim
    // has landed. Up to 3 tries (~0.6s total) then surface the 401 honestly.
    if (res.status === 401 && _attempt < 3) {
      clearTimeout(t);
      await sleep(120 * (_attempt + 1));
      return request<T>(method, path, body, timeoutMs, _attempt + 1);
    }
    if (!res.ok) {
      const detail = await extractDetail(res);
      return {
        ok: false,
        status: res.status,
        message: detail || res.statusText || `HTTP ${res.status}`,
      };
    }
    const text = await res.text();
    const data = text ? (JSON.parse(text) as T) : ({} as T);
    return { ok: true, data };
  } catch (e) {
    const err = e as Error;
    const message =
      err.name === "AbortError" ? "timed out" : err.message || "network error";
    return { ok: false, status: 0, message };
  } finally {
    clearTimeout(t);
  }
}

export const getJSON = <T>(path: string) => request<T>("GET", path);
