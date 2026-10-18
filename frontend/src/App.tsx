export default function App() {
  return (
    <main className="shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="IncidentPilot home">
          <span className="brand-mark">IP</span>
          <span>IncidentPilot</span>
        </a>
        <span className="phase">Foundation · Phase 1</span>
      </header>
      <section className="welcome">
        <p className="eyebrow">KUBERNETES INCIDENT RESPONSE</p>
        <h1>From alert to verified recovery.</h1>
        <p className="intro">
          IncidentPilot is being built phase by phase. The initial foundation
          connects local services and a demo workload; incident handling will
          appear here as those phases are completed.
        </p>
        <div className="status-card">
          <span className="status-dot" />
          <div>
            <strong>Foundation workspace</strong>
            <p>Incident intake and monitoring integration are not configured yet.</p>
          </div>
        </div>
      </section>
      <footer>Evidence first. Safe actions. Verified outcomes.</footer>
    </main>
  );
}

