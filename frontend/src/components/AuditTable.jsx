import { useEffect, useState } from "react";
import { api } from "../api";
import { formatBucketLabel, formatActionLabel, formatDateTime, formatInr, formatPct } from "../format";
import { OutcomeBadge, VerdictBadge, Pill } from "./StatusBadge";
import "./Card.css";
import "./AuditTable.css";

const PAGE_SIZE = 20;

const BUCKETS = ["B1_CONGESTION", "B2_BALANCE", "B3_TRANSIENT", "B4_STRUCTURAL", "B5_DEAD", "B_UNKNOWN"];
const ACTIONS = ["RETRY_SCHEDULED", "NUDGE_SENT", "STOPPED", "HUMAN_QUEUE"];
const VERDICTS = ["ALLOW", "BLOCK", "ESCALATE"];
const OUTCOMES = ["RECOVERED", "FAILED", "PENDING", "NOT_ATTEMPTED"];

const EMPTY_FILTERS = { bucket: "", action: "", policy_verdict: "", outcome: "", q: "" };

// PRD M8 section 4: searchable, one row per decision, expandable to show
// signals + explanation. The user's brief allowed cutting search/expand
// under time pressure -- kept here because both ride entirely on data
// GET /audit already returns per row (see app/api.py::_decision_summary),
// so neither costs a second round trip per row. See the write-up for
// what WAS cut instead.
export function AuditTable({ hasRun, refreshKey }) {
  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [offset, setOffset] = useState(0);
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [expandedId, setExpandedId] = useState(null);

  useEffect(() => {
    setOffset(0);
  }, [filters, refreshKey]);

  useEffect(() => {
    if (!hasRun) {
      setResult(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .audit({ ...filters, limit: PAGE_SIZE, offset })
      .then((data) => {
        if (!cancelled) setResult(data);
      })
      .catch((err) => {
        if (!cancelled) setError(err.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [hasRun, filters, offset, refreshKey]);

  if (!hasRun) {
    return (
      <section className="card">
        <h2 className="card__title">Audit trail</h2>
        <p className="card__subtitle">Run a simulation to populate the decision log.</p>
      </section>
    );
  }

  const total = result?.total ?? 0;
  const decisions = result?.decisions ?? [];
  const page = Math.floor(offset / PAGE_SIZE) + 1;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

  function updateFilter(key, value) {
    setFilters((prev) => ({ ...prev, [key]: value }));
  }

  return (
    <section className="card">
      <div className="audit-header">
        <div>
          <h2 className="card__title">Audit trail</h2>
          <p className="card__subtitle">{total.toLocaleString("en-IN")} decisions this run — one row per action taken</p>
        </div>
      </div>

      <div className="audit-filters">
        <input
          className="audit-search"
          type="search"
          placeholder="Search decision or event id…"
          value={filters.q}
          onChange={(e) => updateFilter("q", e.target.value)}
        />
        <select value={filters.bucket} onChange={(e) => updateFilter("bucket", e.target.value)}>
          <option value="">All buckets</option>
          {BUCKETS.map((b) => (
            <option key={b} value={b}>
              {formatBucketLabel(b)}
            </option>
          ))}
        </select>
        <select value={filters.action} onChange={(e) => updateFilter("action", e.target.value)}>
          <option value="">All actions</option>
          {ACTIONS.map((a) => (
            <option key={a} value={a}>
              {formatActionLabel(a)}
            </option>
          ))}
        </select>
        <select value={filters.policy_verdict} onChange={(e) => updateFilter("policy_verdict", e.target.value)}>
          <option value="">All verdicts</option>
          {VERDICTS.map((v) => (
            <option key={v} value={v}>
              {v}
            </option>
          ))}
        </select>
        <select value={filters.outcome} onChange={(e) => updateFilter("outcome", e.target.value)}>
          <option value="">All outcomes</option>
          {OUTCOMES.map((o) => (
            <option key={o} value={o}>
              {formatActionLabel(o)}
            </option>
          ))}
        </select>
        {(filters.bucket || filters.action || filters.policy_verdict || filters.outcome || filters.q) && (
          <button className="audit-clear" onClick={() => setFilters(EMPTY_FILTERS)}>
            Clear filters
          </button>
        )}
      </div>

      {error && <p className="audit-error">{error}</p>}

      <div className="audit-table-wrap" aria-busy={loading}>
        <table className="audit-table">
          <thead>
            <tr>
              <th>Decision</th>
              <th>Bucket</th>
              <th>Action</th>
              <th>Verdict</th>
              <th>Outcome</th>
              <th>Recovered</th>
              <th>Scheduled for</th>
            </tr>
          </thead>
          <tbody>
            {decisions.length === 0 && !loading && (
              <tr>
                <td colSpan={7} className="audit-table__empty">
                  No decisions match these filters.
                </td>
              </tr>
            )}
            {decisions.map((d) => (
              <AuditRow
                key={d.decision_id}
                decision={d}
                expanded={expandedId === d.decision_id}
                onToggle={() => setExpandedId(expandedId === d.decision_id ? null : d.decision_id)}
              />
            ))}
          </tbody>
        </table>
      </div>

      <div className="audit-pagination">
        <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
          ← Previous
        </button>
        <span>
          Page {page} of {pageCount}
        </span>
        <button disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>
          Next →
        </button>
      </div>
    </section>
  );
}

function AuditRow({ decision: d, expanded, onToggle }) {
  return (
    <>
      <tr className={`audit-row ${expanded ? "audit-row--expanded" : ""}`} onClick={onToggle}>
        <td className="audit-row__id">
          <button className="audit-row__toggle" aria-expanded={expanded} aria-label="Toggle details">
            {expanded ? "▾" : "▸"}
          </button>
          <span className="mono">{d.decision_id}</span>
        </td>
        <td>{formatBucketLabel(d.classified_bucket)}</td>
        <td>
          <Pill>{formatActionLabel(d.action)}</Pill>
        </td>
        <td>
          <VerdictBadge verdict={d.policy_verdict} />
        </td>
        <td>
          <OutcomeBadge outcome={d.outcome} />
        </td>
        <td className="mono">{d.amount_recovered_inr ? formatInr(d.amount_recovered_inr) : "—"}</td>
        <td className="audit-row__time">{formatDateTime(d.scheduled_for)}</td>
      </tr>
      {expanded && (
        <tr className="audit-detail">
          <td colSpan={7}>
            <div className="audit-detail__grid">
              <div>
                <span className="audit-detail__label">Confidence</span>
                <span>{formatPct(d.confidence * 100)}</span>
              </div>
              <div>
                <span className="audit-detail__label">Window snapped</span>
                <span>{d.window_snapped ? "Yes" : "No"}</span>
              </div>
              <div>
                <span className="audit-detail__label">Signals</span>
                <div className="audit-detail__tags">
                  {d.signals.map((s) => (
                    <Pill key={s}>{s}</Pill>
                  ))}
                </div>
              </div>
              <div>
                <span className="audit-detail__label">Policy reasons</span>
                <div className="audit-detail__tags">
                  {d.policy_reasons.map((r) => (
                    <Pill key={r}>{r}</Pill>
                  ))}
                </div>
              </div>
              <div className="audit-detail__explanation">
                <span className="audit-detail__label">
                  Explanation
                  {d.explanation_source && <span className="audit-detail__source"> · {d.explanation_source}</span>}
                </span>
                <p>{d.explanation || "Not yet explained."}</p>
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
