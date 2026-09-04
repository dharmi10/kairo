import { formatCompactInr, formatInr, formatPct, formatSignedPct, formatSignedPoints } from "../format";
import "./HeadlineStrip.css";

// DECISIONS.md, "M8 dashboard spec, finalized": the headline (largest
// text on the page) is the LIVE run's recovery-RATE delta, not the ₹
// figure -- the multi-seed sweep found recovery rate stable across 20
// independently-generated populations (a 12.0-18.6pt band) while ₹
// uplift swings widely on the same runs (a log-normal amount tail
// landing high-value events on different sides of a close race). The ₹
// delta is still shown, but always paired with the fixture's range
// immediately beside it -- never alone, as if it were a fixed, precise
// claim. See README "Multi-seed robustness: the uplift is a range, not a
// point estimate".

function DeltaText({ value, formatter, invert = false }) {
  const good = invert ? value < 0 : value > 0;
  return <span className={good ? "delta delta--good" : "delta delta--bad"}>{formatter(value)}</span>;
}

export function HeadlineStrip({ summary, fixture, running, onRun, error }) {
  if (!summary) {
    return (
      <section className="headline headline--empty">
        <div className="headline__empty-copy">
          <h1>No simulation has run yet</h1>
          <p>
            Run the agent and the baseline against a fresh 500-event batch to see the recovery-rate delta, the ₹
            recovered comparison, and the full audit trail.
          </p>
          {error && <p className="headline__error">{error}</p>}
        </div>
        <button className="run-button run-button--empty" onClick={onRun} disabled={running}>
          {running ? "Running…" : "Run simulation"}
        </button>
      </section>
    );
  }

  const { agent, baseline, delta } = summary;

  return (
    <section className="headline">
      <div className="headline__top">
        <div className="headline__hero">
          <span className="headline__label">Recovery-rate delta vs. Razorpay's baseline retry</span>
          <span className="headline__hero-value">
            <DeltaText value={delta.recovery_rate_points} formatter={formatSignedPoints} />
          </span>
          <span className="headline__sub">
            {formatPct(agent.recovery_rate_pct)} agent vs. {formatPct(baseline.recovery_rate_pct)} baseline, this
            run (n={agent.n} events, seed {summary.batch_seed})
          </span>
        </div>

        <button className="run-button" onClick={onRun} disabled={running}>
          {running ? "Running…" : "Run new simulation"}
        </button>
      </div>

      <div className="headline__row">
        <div className="stat-tile">
          <span className="stat-tile__label">₹ recovered — agent</span>
          <span className="stat-tile__value">{formatCompactInr(agent.rupees_recovered)}</span>
          <span className="stat-tile__sub">{formatInr(agent.rupees_recovered)}</span>
        </div>
        <div className="stat-tile">
          <span className="stat-tile__label">₹ recovered — baseline</span>
          <span className="stat-tile__value">{formatCompactInr(baseline.rupees_recovered)}</span>
          <span className="stat-tile__sub">{formatInr(baseline.rupees_recovered)}</span>
        </div>
        <div className="stat-tile">
          <span className="stat-tile__label">₹ delta, this run</span>
          <span className="stat-tile__value">
            <DeltaText value={delta.rupees_recovered} formatter={formatCompactInr} />
          </span>
          <span className="stat-tile__sub">
            <DeltaText value={delta.rs_uplift_pct} formatter={formatSignedPct} />
          </span>
        </div>

        {fixture && (
          <div className="stat-tile stat-tile--range">
            <span className="stat-tile__label">₹ uplift across 20 seeds (committed fixture, not live)</span>
            <span className="stat-tile__value stat-tile__value--range">
              {formatSignedPct(fixture.rs_uplift_pct.min, 0)} – {formatSignedPct(fixture.rs_uplift_pct.max, 0)}
            </span>
            <span className="stat-tile__sub">
              mean {formatSignedPct(fixture.rs_uplift_pct.mean, 0)} · recovery-rate delta{" "}
              {formatSignedPoints(fixture.recovery_rate_delta_points.min, 0)}–
              {formatSignedPoints(fixture.recovery_rate_delta_points.max, 0)} across {fixture.seeds.length} seeds
            </span>
          </div>
        )}
      </div>
    </section>
  );
}
