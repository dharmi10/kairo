// Thin fetch wrapper over the seven backend endpoints (PRD sec. 8). No
// data is hardcoded here or anywhere else in the dashboard -- the one
// exception is the 20-seed fixture, which is a static asset synced from
// backend/metrics/output/multi_seed_range.json (see scripts/sync-fixture.mjs
// and DECISIONS.md, "M8 dashboard: precomputed fixture, not a live sweep
// endpoint") and fetched with plain fetch(), not through this client.

const BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
    this.status = status;
    this.detail = detail;
  }
}

async function request(path, options = {}) {
  const response = await fetch(`${BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      // body wasn't JSON -- keep statusText
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return null;
  return response.json();
}

export const api = {
  simulateRun: (body) => request("/simulate/run", { method: "POST", body: JSON.stringify(body ?? {}) }),
  resultsSummary: () => request("/results/summary"),
  resultsByBucket: () => request("/results/by-bucket"),
  audit: (params = {}) => {
    const query = new URLSearchParams(
      Object.fromEntries(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ""))
    );
    const suffix = query.toString() ? `?${query.toString()}` : "";
    return request(`/audit${suffix}`);
  },
  auditDetail: (decisionId) => request(`/audit/${decisionId}`),
  health: () => request("/health"),
};

// A 404 from /results/summary and /results/by-bucket specifically means
// "no simulation has run yet" -- a normal, expected state (a fresh DB,
// or right after POST /reset), not a failure. Callers branch on this
// rather than showing an error toast for it.
export function isNoRunYet(error) {
  return error instanceof ApiError && error.status === 404;
}
