import { FormEvent, useEffect, useMemo, useState } from 'react';

type DashboardSummary = {
  total_incidents: number;
  critical_incidents: number;
  resolved_incidents: number;
  open_incidents: number;
  mttr_days: number;
};

type Incident = {
  incident_id: string;
  title: string;
  summary: string;
  severity: string;
  services: string;
  source: string;
  status: string;
  logs: string;
  root_cause?: string;
  recommended_resolution?: string;
  evidence?: string[];
  confidence?: number;
  human_investigation_required?: boolean;
  historical_matches?: Record<string, unknown>[];
  outcome_notes?: string;
  created_at?: string;
  updated_at?: string;
  memory_status?: string;
};

type IncidentAction = {
  action_id: string;
  incident_id: string;
  description: string;
  rationale: string;
  status: 'pending' | 'approved' | 'rejected' | 'executed';
  decision_note: string;
  execution_note: string;
};

type Diagnostics = {
  memory: {
    available: boolean;
    mode: string;
    bank_id: string;
    base_url_configured: boolean;
    reachable: boolean;
    connection_error: string | null;
  };
  llm: {
    configured: boolean;
    connected: boolean;
    status: string;
    model: string;
  };
  approval: {
    configured: boolean;
  };
};

const API_BASE = '/api';

function displayText(value: unknown, maxLength = 320): string {
  if (typeof value !== 'string') return '';
  const text = value.replace(/\s+/g, ' ').trim();
  return text.length > maxLength ? `${text.slice(0, maxLength - 3)}...` : text;
}

async function callApi<T>(
  path: string,
  method = 'GET',
  body?: unknown,
  headers: Record<string, string> = {},
): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method,
    headers: {
      'Content-Type': 'application/json',
      ...headers,
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try {
      const payload = await response.json() as { detail?: string };
      detail = payload.detail || detail;
    } catch {
      detail = response.statusText || detail;
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

export default function App() {
  const [activeTab, setActiveTab] = useState('dashboard');
  const [dashboard, setDashboard] = useState<{ summary: DashboardSummary; severity_distribution: Record<string, number>; recent_incidents: Incident[]; frequent_errors: Array<[string, number]>; memory_activity: { status: string; bank: string } } | null>(null);
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [incidentActions, setIncidentActions] = useState<IncidentAction[]>([]);
  const [diagnostics, setDiagnostics] = useState<Diagnostics | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [analysis, setAnalysis] = useState<Record<string, unknown> | null>(null);
  const [outcomeNote, setOutcomeNote] = useState('');
  const [approvalNote, setApprovalNote] = useState('');
  const [approvalToken, setApprovalToken] = useState('');
  const [actionDescription, setActionDescription] = useState('');
  const [actionRationale, setActionRationale] = useState('');
  const [loading, setLoading] = useState(true);
  const [investigating, setInvestigating] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [savingOutcome, setSavingOutcome] = useState(false);
  const [savingAction, setSavingAction] = useState(false);
  const [updatingActionId, setUpdatingActionId] = useState<string | null>(null);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');

  const loadData = async () => {
    setLoading(true);
    try {
      const [dashboardData, incidentsData, diagnosticsData] = await Promise.all([
        callApi<any>('/dashboard'),
        callApi<Incident[]>('/incidents'),
        callApi<Diagnostics>('/diagnostics'),
      ]);
      setDashboard(dashboardData);
      setIncidents(incidentsData);
      setDiagnostics(diagnosticsData);
      if (!selectedId && incidentsData.length) {
        setSelectedId(incidentsData[0].incident_id);
      }
    } catch (loadError) {
      setError((loadError as Error).message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadData();
  }, []);

  const loadActions = async (incidentId: string) => {
    try {
      const actions = await callApi<IncidentAction[]>(`/incidents/${incidentId}/actions`);
      setIncidentActions(actions);
    } catch (actionError) {
      setError((actionError as Error).message);
    }
  };

  useEffect(() => {
    if (selectedId) {
      void loadActions(selectedId);
    } else {
      setIncidentActions([]);
    }
  }, [selectedId]);

  const selectedIncident = useMemo(
    () => incidents.find((incident) => incident.incident_id === selectedId) ?? null,
    [incidents, selectedId],
  );
  const displayedAnalysis = analysis ?? (selectedIncident?.root_cause ? {
    root_cause: selectedIncident.root_cause,
    suggested_resolution: selectedIncident.recommended_resolution,
    confidence: selectedIncident.confidence,
    supporting_evidence: selectedIncident.evidence,
    memory_status: selectedIncident.memory_status,
    memory_impact: selectedIncident.outcome_notes,
    memory_influenced: Boolean(selectedIncident.historical_matches?.length),
  } : null);
  const memoryReferences = (analysis?.memory_references as Array<Record<string, unknown>> | undefined) ?? undefined;
  const displayedMemoryReferences = memoryReferences?.length
    ? memoryReferences
    : selectedIncident?.historical_matches ?? [];
  const supportingEvidence = Array.isArray(analysis?.supporting_evidence)
    ? analysis.supporting_evidence.filter((item): item is string => typeof item === 'string')
    : selectedIncident?.evidence ?? [];

  const submitIncident = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError('');
    setNotice('');
    setSubmitting(true);
    const formData = new FormData(event.currentTarget);
    const payload = {
      title: String(formData.get('title') || ''),
      summary: String(formData.get('summary') || ''),
      severity: String(formData.get('severity') || 'High'),
      services: String(formData.get('services') || ''),
      source: String(formData.get('source') || 'manual'),
      logs: String(formData.get('logs') || ''),
    };

    try {
      const created = await callApi<Incident>('/incidents', 'POST', payload);
      setSelectedId(created.incident_id);
      setActiveTab('investigation');
      await loadData();
    } catch (submitError) {
      setError((submitError as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  const investigateCurrent = async () => {
    if (!selectedIncident) {
      return;
    }
    setError('');
    setNotice('');
    setInvestigating(true);
    try {
      const response = await callApi<Record<string, unknown>>(
        `/incidents/${selectedIncident.incident_id}/analyze`,
        'POST',
        {
          title: selectedIncident.title,
          services: selectedIncident.services,
          logs: selectedIncident.logs,
        },
      );
      setAnalysis(response);
      setOutcomeNote('');
      setNotice('Investigation completed.');
      await loadData();
    } catch (analysisError) {
      setError((analysisError as Error).message);
    } finally {
      setInvestigating(false);
    }
  };

  const recordOutcome = async (success: boolean) => {
    if (!selectedIncident) {
      return;
    }
    setError('');
    setNotice('');
    setSavingOutcome(true);
    const suggestedResolution = String(analysis?.suggested_resolution ?? selectedIncident.recommended_resolution ?? 'the recommended resolution');
    const outcome = outcomeNote.trim() || `The recommended resolution ${success ? 'succeeded' : 'failed'}: ${suggestedResolution}`;
    try {
      await callApi<Incident>(`/incidents/${selectedIncident.incident_id}/outcome`, 'POST', {
        status: success ? 'resolved' : 'investigating',
        outcome,
        success,
      });
      setOutcomeNote('');
      await loadData();
      setNotice(`Resolution outcome recorded as ${success ? 'successful' : 'failed'}.`);
    } catch (outcomeError) {
      setError((outcomeError as Error).message);
    } finally {
      setSavingOutcome(false);
    }
  };

  const submitAction = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedIncident) return;
    setError('');
    setNotice('');
    setSavingAction(true);
    try {
      await callApi<IncidentAction>(`/incidents/${selectedIncident.incident_id}/actions`, 'POST', {
        description: actionDescription,
        rationale: actionRationale,
      });
      setActionDescription('');
      setActionRationale('');
      await loadActions(selectedIncident.incident_id);
      setNotice('Action proposal is pending approval. IncidentIQ did not execute it.');
    } catch (actionError) {
      setError((actionError as Error).message);
    } finally {
      setSavingAction(false);
    }
  };

  const updateAction = async (
    action: IncidentAction,
    operation: 'approved' | 'rejected' | 'executed',
  ) => {
    if (!selectedIncident) return;
    setError('');
    setNotice('');
    setUpdatingActionId(action.action_id);
    const headers = { Authorization: `Bearer ${approvalToken}` };
    try {
      if (operation === 'executed') {
        await callApi(
          `/incidents/${selectedIncident.incident_id}/actions/${action.action_id}/execution`,
          'POST',
          { execution_note: approvalNote },
          headers,
        );
      } else {
        await callApi(
          `/incidents/${selectedIncident.incident_id}/actions/${action.action_id}/approval`,
          'POST',
          { decision: operation, note: approvalNote },
          headers,
        );
      }
      setApprovalNote('');
      await loadActions(selectedIncident.incident_id);
      setNotice(
        operation === 'executed'
          ? 'External execution was recorded. IncidentIQ did not run a command.'
          : `Action ${operation}.`,
      );
    } catch (actionError) {
      setError((actionError as Error).message);
    } finally {
      setUpdatingActionId(null);
    }
  };

  const memoryMode = diagnostics?.memory.mode ?? dashboard?.memory_activity.status ?? 'unknown';
  const memoryLabel = diagnostics
    ? memoryMode === 'hindsight' && diagnostics.memory.reachable
      ? 'Hindsight connected'
      : diagnostics.memory.base_url_configured
        ? 'SQLite fallback (Hindsight unreachable)'
        : 'SQLite fallback (Hindsight not configured)'
    : 'Memory status unavailable';
  const groqLabel = diagnostics?.llm.status === 'connected'
    ? 'Groq connected'
    : diagnostics?.llm.status === 'not-configured'
      ? 'Groq not configured'
      : diagnostics?.llm.status === 'request-failed'
        ? 'Groq request failed'
        : diagnostics ? 'Groq configured, not yet verified' : 'Groq status unavailable';

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-block">
          <div className="brand-badge">IQ</div>
          <div>
            <p className="eyebrow">HACKATHON AI OPS</p>
            <h1>IncidentIQ</h1>
          </div>
        </div>

        <nav className="nav-pills">
          {['dashboard', 'create', 'investigation', 'history', 'memory', 'analytics'].map((tab) => (
            <button
              key={tab}
              className={activeTab === tab ? 'nav-button active' : 'nav-button'}
              onClick={() => setActiveTab(tab)}
            >
              {tab.charAt(0).toUpperCase() + tab.slice(1)}
            </button>
          ))}
        </nav>

        <div className="sidebar-card">
          <p className="tiny-label">Memory status</p>
          <strong>{memoryLabel}</strong>
          <small>{dashboard?.memory_activity?.bank ?? 'incidentiq-memory'}</small>
          <p className="tiny-label">AI provider</p>
          <strong>{groqLabel}</strong>
          {diagnostics?.llm && <small>Model: {diagnostics.llm.model}</small>}
        </div>
      </aside>

      <main className="content-panel">
        <div className="service-status-strip" aria-label="Integration status">
          <span className={memoryLabel === 'Hindsight connected' ? 'status-indicator connected' : 'status-indicator fallback'}>
            Memory: {memoryLabel}
          </span>
          <span className={diagnostics?.llm.connected ? 'status-indicator connected' : 'status-indicator fallback'}>
            {groqLabel}
          </span>
        </div>
        {error && <div className="inline-error" role="alert">{error}</div>}
        {notice && <div className="success-message" role="status">{notice}</div>}
        {loading && <div className="loading-message" role="status">Loading IncidentIQ data…</div>}

        {activeTab === 'dashboard' && dashboard && (
          <>
            <header className="section-header">
              <div>
                <p className="eyebrow">Operations overview</p>
                <h2>Executive dashboard</h2>
              </div>
            </header>

            <div className="stats-grid">
              <div className="stat-card accent">
                <span>Total incidents</span>
                <strong>{dashboard.summary.total_incidents}</strong>
              </div>
              <div className="stat-card">
                <span>Critical</span>
                <strong>{dashboard.summary.critical_incidents}</strong>
              </div>
              <div className="stat-card">
                <span>Resolved</span>
                <strong>{dashboard.summary.resolved_incidents}</strong>
              </div>
              <div className="stat-card">
                <span>Open</span>
                <strong>{dashboard.summary.open_incidents}</strong>
              </div>
            </div>

            <div className="two-col-grid">
              <div className="panel">
                <div className="panel-header">
                  <h3>Severity distribution</h3>
                </div>
                <div className="bars">
                  {Object.entries(dashboard.severity_distribution).map(([name, value]) => (
                    <div key={name} className="bar-row">
                      <span>{name}</span>
                      <div className="bar-track">
                        <div className="bar-fill" style={{ width: `${Math.min((value / Math.max(dashboard.summary.total_incidents, 1)) * 100, 100)}%` }} />
                      </div>
                      <strong>{value}</strong>
                    </div>
                  ))}
                </div>
              </div>

              <div className="panel">
                <div className="panel-header">
                  <h3>Quick metrics</h3>
                </div>
                <ul className="metric-list">
                  <li><span>Mean time to resolution</span><strong>{dashboard.summary.mttr_days} days</strong></li>
                  <li><span>Active memory provider</span><strong>{memoryLabel}</strong></li>
                  <li><span>Most frequent errors</span><strong>{dashboard.frequent_errors[0]?.[0] ?? 'None'}</strong></li>
                </ul>
              </div>
            </div>

            <div className="panel">
              <div className="panel-header">
                <h3>Recent incidents</h3>
              </div>
              <div className="incident-list compact">
                {dashboard.recent_incidents.length === 0 && <p className="muted">No incidents recorded yet.</p>}
                {dashboard.recent_incidents.map((incident) => (
                  <button key={incident.incident_id} className="incident-row" onClick={() => { setSelectedId(incident.incident_id); setAnalysis(null); setActiveTab('investigation'); }}>
                    <div>
                      <strong>{incident.title}</strong>
                      <small>{incident.severity} • {incident.status}</small>
                    </div>
                    <span>{incident.incident_id}</span>
                  </button>
                ))}
              </div>
            </div>
          </>
        )}

        {activeTab === 'create' && (
          <>
            <header className="section-header">
              <div>
                <p className="eyebrow">New case</p>
                <h2>Create incident</h2>
              </div>
            </header>
            <form className="incident-form panel" onSubmit={submitIncident}>
              <div className="field-row two">
                <label>
                  <span>Incident title</span>
                  <input name="title" placeholder="API latency spike in checkout" required />
                </label>
                <label>
                  <span>Severity</span>
                  <select name="severity" defaultValue="High">
                    <option>Critical</option>
                    <option>High</option>
                    <option>Medium</option>
                    <option>Low</option>
                  </select>
                </label>
              </div>

              <label>
                <span>Summary</span>
                <textarea name="summary" rows={4} placeholder="Describe impact and context" required />
              </label>

              <div className="field-row two">
                <label>
                  <span>Affected services</span>
                  <input name="services" placeholder="api, auth, payments" />
                </label>
                <label>
                  <span>Source</span>
                  <select name="source" defaultValue="manual">
                    <option value="manual">Manual</option>
                    <option value="paste">Pasted logs</option>
                  </select>
                </label>
              </div>

              <label>
                <span>Logs</span>
                <textarea name="logs" rows={8} placeholder="Paste log snippets or stack traces" />
              </label>

              <button className="primary-button" type="submit" disabled={submitting}>
                {submitting ? 'Creating…' : 'Create incident'}
              </button>
            </form>
          </>
        )}

        {activeTab === 'investigation' && selectedIncident && (
          <>
            <header className="section-header">
              <div>
                <p className="eyebrow">Incident investigation</p>
                <h2>{selectedIncident.title}</h2>
              </div>
              <button className="primary-button" onClick={() => void investigateCurrent()} disabled={investigating}>
                {investigating ? 'Investigating…' : 'Run investigation'}
              </button>
            </header>

            <div className="two-col-grid">
              <div className="panel">
                <div className="panel-header">
                  <h3>Original logs</h3>
                </div>
                <pre className="log-block">{selectedIncident.logs || 'No logs recorded yet.'}</pre>
              </div>

              <div className="panel">
                <div className="panel-header">
                  <h3>AI root cause analysis</h3>
                </div>
                {displayedAnalysis ? (
                  <div className="analysis-stack">
                    <div className="status-stack">
                      <span className="pill">{String(displayedAnalysis.provider ?? 'fallback') === 'groq' ? 'Groq diagnosis' : 'Local diagnosis'}</span>
                      <span className={displayedAnalysis.memory_influenced ? 'pill' : 'pill muted'}>
                        {displayedAnalysis.memory_influenced ? 'Historical memory influenced diagnosis' : 'No historical memory influence'}
                      </span>
                    </div>
                    {displayText(displayedAnalysis.summary) && <p><strong>Summary:</strong> {displayText(displayedAnalysis.summary)}</p>}
                    <p><strong>Root cause:</strong> {displayText(displayedAnalysis.root_cause, 500) || 'Not available'}</p>
                    <p><strong>Confidence:</strong> {String(displayedAnalysis.confidence ?? 'N/A')}</p>
                    <p><strong>Human investigation required:</strong> {String(displayedAnalysis.human_investigation_required ?? false)}</p>
                    <p><strong>Provider used for this analysis:</strong> {String(displayedAnalysis.memory_status ?? 'unknown') === 'hindsight' ? 'Hindsight' : 'SQLite local fallback'}</p>
                    <p><strong>Memory influence:</strong> {displayText(displayedAnalysis.memory_impact, 200) || 'No prior incident influenced the recommendation.'}</p>
                    <p><strong>Suggested resolution:</strong> {displayText(displayedAnalysis.suggested_resolution, 500) || 'Not available'}</p>
                    <label>
                      <span>Resolution outcome notes</span>
                      <textarea
                        rows={3}
                        value={outcomeNote}
                        onChange={(event) => setOutcomeNote(event.target.value)}
                        placeholder="Describe what happened after applying the recommendation"
                      />
                    </label>
                    <div className="outcome-actions">
                      <button className="primary-button" onClick={() => void recordOutcome(true)} disabled={savingOutcome}>
                        {savingOutcome ? 'Saving…' : 'Record resolution succeeded'}
                      </button>
                      <button className="secondary-button" onClick={() => void recordOutcome(false)} disabled={savingOutcome}>
                        {savingOutcome ? 'Saving…' : 'Record resolution failed'}
                      </button>
                    </div>
                  </div>
                ) : (
                  <p className="muted">No analysis yet. Run the investigation to generate findings.</p>
                )}
              </div>
            </div>

            <div className="panel">
              <div className="panel-header">
                <h3>Evidence and historical matches</h3>
              </div>
              <div className="meta-grid">
                <div>
                  <h4>Supporting evidence</h4>
                  <ul>
                    {supportingEvidence.length ? supportingEvidence.slice(0, 5).map((item, index) => <li key={`${index}-${item.slice(0, 48)}`}>{displayText(item, 400)}</li>) : <li>No evidence captured yet.</li>}
                  </ul>
                </div>
                <div>
                  <h4>Historical incident matches</h4>
                  <ul>
                  {displayedMemoryReferences.length ? displayedMemoryReferences.slice(0, 5).map((match, index) => (
                    <li key={`${String(match.incident_id ?? match.id ?? 'match')}-${index}`}>
                      <strong>{displayText(match.title ?? match.incident_id, 160) || 'Historical case'}</strong>
                      {displayText(match.summary) && <div>{displayText(match.summary)}</div>}
                      <small>{displayText(match.reason, 160)}</small>
                      {match.success === true && (
                        <>
                          <div><strong>Recorded successful outcome:</strong> {displayText(match.outcome, 320) || 'No outcome details recorded.'}</div>
                          {displayText(match.resolution, 320) && <div><strong>Previous resolution:</strong> {displayText(match.resolution, 320)}</div>}
                        </>
                      )}
                      {match.success === false && (
                        <div><strong>Recorded unsuccessful outcome:</strong> {displayText(match.outcome, 320) || 'No outcome details recorded.'}</div>
                      )}
                    </li>
                  )) : <li>No relevant historical matches available.</li>}
                  </ul>
                </div>
              </div>
            </div>

            <section className="panel approval-panel">
              <div className="panel-header">
                <h3>Human approval gate</h3>
                <span className={diagnostics?.approval.configured ? 'pill' : 'pill muted'}>
                  {diagnostics?.approval.configured ? 'Approvals enabled' : 'Approval token not configured'}
                </span>
              </div>
              <p className="muted">
                Proposals never run commands. An authorized human must approve first, perform the work outside IncidentIQ, and then confirm execution here.
              </p>
              <label>
                <span>Server approval token</span>
                <input
                  type="password"
                  autoComplete="off"
                  value={approvalToken}
                  onChange={(event) => setApprovalToken(event.target.value)}
                  placeholder="Configured by the backend operator"
                />
              </label>
              <form className="incident-form" onSubmit={(event) => void submitAction(event)}>
                <label>
                  <span>Risky action proposal</span>
                  <textarea
                    rows={3}
                    minLength={10}
                    required
                    value={actionDescription}
                    onChange={(event) => setActionDescription(event.target.value)}
                    placeholder="Describe the proposed operational change; do not enter credentials"
                  />
                </label>
                <label>
                  <span>Reason / validation plan</span>
                  <textarea
                    rows={2}
                    value={actionRationale}
                    onChange={(event) => setActionRationale(event.target.value)}
                    placeholder="Why is it needed, and how will success be verified?"
                  />
                </label>
                <button className="primary-button" type="submit" disabled={savingAction}>
                  {savingAction ? 'Submitting…' : 'Submit for human approval'}
                </button>
              </form>
              <label>
                <span>Approval / execution note</span>
                <input
                  value={approvalNote}
                  onChange={(event) => setApprovalNote(event.target.value)}
                  placeholder="Decision rationale or external execution confirmation"
                />
              </label>
              <div className="approval-list">
                {incidentActions.length === 0 && <p className="muted">No proposed actions for this incident.</p>}
                {incidentActions.map((action) => (
                  <article className="approval-card" key={action.action_id}>
                    <div className="approval-card-header">
                      <strong>{action.description}</strong>
                      <span className={`approval-state ${action.status}`}>{action.status}</span>
                    </div>
                    {action.rationale && <p>{action.rationale}</p>}
                    <small>{action.action_id}</small>
                    {action.status === 'pending' && (
                      <div className="outcome-actions">
                        <button className="primary-button" disabled={!approvalToken || updatingActionId === action.action_id} onClick={() => void updateAction(action, 'approved')}>Approve</button>
                        <button className="secondary-button" disabled={!approvalToken || updatingActionId === action.action_id} onClick={() => void updateAction(action, 'rejected')}>Reject</button>
                      </div>
                    )}
                    {action.status === 'approved' && (
                      <>
                        <p className="muted">Approved. A human may now perform this action outside IncidentIQ.</p>
                        <button
                          className="primary-button"
                          disabled={!approvalToken || !approvalNote.trim() || updatingActionId === action.action_id}
                          onClick={() => void updateAction(action, 'executed')}
                        >
                          Confirm performed externally
                        </button>
                      </>
                    )}
                    {action.status === 'rejected' && <p className="muted">Rejected actions cannot be executed.</p>}
                    {action.status === 'executed' && <p className="muted">External execution was confirmed and recorded.</p>}
                  </article>
                ))}
              </div>
            </section>
          </>
        )}
        {activeTab === 'investigation' && !selectedIncident && (
          <div className="panel">
            <h2>No incident selected</h2>
            <p className="muted">Create an incident or select one from the dashboard or history to investigate it.</p>
            <button className="primary-button" onClick={() => setActiveTab('create')}>Create incident</button>
          </div>
        )}

        {activeTab === 'history' && (
          <>
            <header className="section-header">
              <div>
                <p className="eyebrow">Case register</p>
                <h2>Incident history</h2>
              </div>
            </header>
            <div className="incident-list panel">
              {incidents.length === 0 && <p className="muted">No incidents recorded yet.</p>}
              {incidents.map((incident) => (
                <button key={incident.incident_id} className="incident-row large" onClick={() => { setSelectedId(incident.incident_id); setAnalysis(null); setActiveTab('investigation'); }}>
                  <div>
                    <strong>{incident.title}</strong>
                    <small>{incident.summary}</small>
                  </div>
                  <div className="status-stack">
                    <span className="pill">{incident.severity}</span>
                    <span className="pill muted">{incident.status}</span>
                  </div>
                </button>
              ))}
            </div>
          </>
        )}

        {activeTab === 'memory' && (
          <>
            <header className="section-header">
              <div>
                <p className="eyebrow">Persistent recall</p>
                <h2>Hindsight memory explorer</h2>
              </div>
            </header>
            <div className="panel memory-box">
              <p>Memories are retained using the configured Hindsight bank. If the service is unavailable, the system stores the same artifacts locally so the app still demonstrates the workflow without silently pretending the retrieval succeeded.</p>
              <ul>
                <li>Retain: incident summary, symptoms, root cause, and suggested recovery guidance.</li>
                <li>Recall: use semantic similarity against service names, stack traces, and error signatures.</li>
                <li>Reflect: compare archived resolved and failed outcomes before recommending actions.</li>
              </ul>
            </div>
          </>
        )}

        {activeTab === 'analytics' && (
          <>
            <header className="section-header">
              <div>
                <p className="eyebrow">Insights</p>
                <h2>Analytics snapshot</h2>
              </div>
            </header>
            <div className="panel">
              <h3>Frequently occurring errors</h3>
              <div className="tag-cloud">
                {(dashboard?.frequent_errors ?? []).map(([errorName, count]) => (
                  <span className="tag" key={errorName}>{errorName}: {count}</span>
                ))}
              </div>
            </div>
          </>
        )}
      </main>
    </div>
  );
}
