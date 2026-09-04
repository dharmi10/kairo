import { useCallback, useEffect, useState } from "react";
import { api, isNoRunYet } from "./api";
import { HeadlineStrip } from "./components/HeadlineStrip";
import { BucketChart } from "./components/BucketChart";
import { GovernancePanel } from "./components/GovernancePanel";
import { AuditTable } from "./components/AuditTable";
import "./App.css";

export default function App() {
  const [summary, setSummary] = useState(null);
  const [byBucket, setByBucket] = useState(null);
  const [escalatedToHuman, setEscalatedToHuman] = useState(null);
  const [fixture, setFixture] = useState(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);
  const [hasRun, setHasRun] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);

  // The one static asset: the committed 20-seed fixture, synced from
  // backend/metrics/output/multi_seed_range.json (scripts/sync-fixture.mjs).
  // Fetched once, independent of whether a live run exists yet.
  useEffect(() => {
    fetch("/multi_seed_range.json")
      .then((r) => (r.ok ? r.json() : null))
      .then(setFixture)
      .catch(() => setFixture(null));
  }, []);

  const loadResults = useCallback(async () => {
    try {
      const [summaryData, byBucketData] = await Promise.all([api.resultsSummary(), api.resultsByBucket()]);
      setSummary(summaryData);
      setByBucket(byBucketData.buckets);
      setHasRun(true);
      const escalations = await api.audit({ action: "HUMAN_QUEUE", limit: 1 });
      setEscalatedToHuman(escalations.total);
    } catch (err) {
      if (isNoRunYet(err)) {
        setHasRun(false);
        setSummary(null);
        setByBucket(null);
      } else {
        setError(err.message);
      }
    }
  }, []);

  // On mount: show whatever the backend already has (a page refresh
  // during a demo must not lose the last live run -- SimulationRun rows
  // persist across restarts). Does NOT trigger a live run itself --
  // POST /simulate/run only ever fires from the button, exactly once per
  // click, per the brief.
  useEffect(() => {
    loadResults();
  }, [loadResults]);

  async function handleRun() {
    setRunning(true);
    setError(null);
    try {
      await api.simulateRun({ count: 500, batch_seed: 42, sim_seed: 20260903 });
      await loadResults();
      setRefreshKey((k) => k + 1);
    } catch (err) {
      setError(err.message);
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="app">
      <header className="app__header">
        <span className="app__logo">Kairo</span>
        <span className="app__tagline">UPI mandate recovery — agent vs. Razorpay's baseline retry</span>
      </header>

      <main className="app__main">
        {error && summary && <div className="app__error-banner">Simulation run failed: {error}</div>}
        <HeadlineStrip summary={summary} fixture={fixture} running={running} onRun={handleRun} error={!summary ? error : null} />

        {hasRun && (
          <>
            <BucketChart byBucket={byBucket} />
            <GovernancePanel summary={summary} byBucket={byBucket} escalatedToHuman={escalatedToHuman} />
          </>
        )}

        <AuditTable hasRun={hasRun} refreshKey={refreshKey} />
      </main>

      <footer className="app__footer">
        Synthetic data, documented assumptions — see <code>README.md</code> and <code>DECISIONS.md</code>.
      </footer>
    </div>
  );
}
