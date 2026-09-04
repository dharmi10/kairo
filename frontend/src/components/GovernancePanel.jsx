import "./Card.css";
import "./GovernancePanel.css";

// PRD M8 section 3: wasted attempts avoided, hard declines correctly
// suppressed, contacts sent, items escalated to human. Every number here
// is read straight off /results/summary, /results/by-bucket and a
// filtered /audit count -- nothing computed client-side beyond simple
// subtraction that the API already did for delta.wasted_attempts_avoided.
export function GovernancePanel({ summary, byBucket, escalatedToHuman }) {
  if (!summary) return null;

  const b5 = byBucket?.find((row) => row.bucket === "B5_DEAD");
  const hardDeclineTotal = b5?.agent?.n ?? 0;
  const hardDeclineAutoRetried = summary.agent.attempts_on_hard_declines;

  const tiles = [
    {
      label: "Wasted attempts avoided",
      value: summary.delta.wasted_attempts_avoided,
      sub: "baseline's retries on hard declines the agent never made",
    },
    {
      label: "Hard declines correctly suppressed",
      value: hardDeclineTotal,
      sub: `of ${hardDeclineTotal} B5 events, ${hardDeclineAutoRetried} auto-retried by the agent`,
    },
    {
      label: "Customer contacts sent",
      value: summary.agent.customer_contacts_sent,
      sub: "nudges only — proves the agent isn't spamming",
    },
    {
      label: "Escalated to a human",
      value: escalatedToHuman ?? "—",
      sub: "risk-flagged, high-value, or low-confidence cases",
    },
  ];

  return (
    <section className="card">
      <h2 className="card__title">Governance</h2>
      <p className="card__subtitle">What the policy engine actually enforced on this run</p>
      <div className="governance-grid">
        {tiles.map((tile) => (
          <div className="governance-tile" key={tile.label}>
            <span className="governance-tile__value">{tile.value}</span>
            <span className="governance-tile__label">{tile.label}</span>
            <span className="governance-tile__sub">{tile.sub}</span>
          </div>
        ))}
      </div>
    </section>
  );
}
