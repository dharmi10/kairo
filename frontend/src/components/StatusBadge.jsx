import "./StatusBadge.css";

// Status color + icon + label, never color alone (palette.md: several of
// these steps are sub-3:1 on the light surface by design -- the pairing
// is the mitigation, not decoration).
const OUTCOME_STATUS = {
  RECOVERED: { token: "good", icon: "●", label: "Recovered" },
  FAILED: { token: "critical", icon: "●", label: "Failed" },
  PENDING: { token: "warning", icon: "●", label: "Pending" },
  NOT_ATTEMPTED: { token: "muted", icon: "○", label: "Not attempted" },
};

export function OutcomeBadge({ outcome }) {
  const status = OUTCOME_STATUS[outcome] ?? { token: "muted", icon: "○", label: outcome ?? "Unknown" };
  return (
    <span className={`badge badge--${status.token}`}>
      <span aria-hidden="true">{status.icon}</span>
      {status.label}
    </span>
  );
}

const VERDICT_STATUS = {
  ALLOW: { token: "good", label: "Allow" },
  BLOCK: { token: "critical", label: "Block" },
  ESCALATE: { token: "warning", label: "Escalate" },
};

export function VerdictBadge({ verdict }) {
  const status = VERDICT_STATUS[verdict] ?? { token: "muted", label: verdict ?? "Unknown" };
  return <span className={`badge badge--${status.token}`}>{status.label}</span>;
}

export function Pill({ children }) {
  return <span className="pill">{children}</span>;
}
