import React, { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import { useAuth } from "../context/AuthContext";
import { IncidentDetail, IncidentState } from "../types";
import { StateBadge, SeverityBadge } from "../components/Badge";
import {
  ActivityIcon,
  AlertCircleIcon,
  CheckCircleIcon,
  ChevronRightIcon,
  ClockIcon,
  CpuIcon,
  FileTextIcon,
  RefreshIcon,
  ShieldAlertIcon,
  TerminalIcon,
  XCircleIcon,
} from "../components/Icons";

const PIPELINE_STAGES: Array<{ key: string; label: string }> = [
  { key: "received", label: "Intake" },
  { key: "enriched", label: "Enrichment" },
  { key: "investigating", label: "Investigation" },
  { key: "analyzed", label: "RCA" },
  { key: "decision", label: "Policy Decision" },
  { key: "executing", label: "K8s Execution" },
  { key: "verifying", label: "Verification" },
  { key: "resolved", label: "Resolved" },
];

function getStageIndex(state: IncidentState): number {
  switch (state) {
    case "RECEIVED":
      return 0;
    case "UNSCOPED":
      return 0;
    case "ENRICHED":
      return 1;
    case "INVESTIGATING":
      return 2;
    case "ANALYZED":
      return 3;
    case "READY":
    case "AWAITING_APPROVAL":
    case "APPROVED":
    case "REJECTED":
    case "EXPIRED":
      return 4;
    case "EXECUTING":
      return 5;
    case "VERIFYING":
      return 6;
    case "RESOLVED":
      return 7;
    case "ESCALATED":
    case "FAILED":
      return 4; // escalated at whatever point
    default:
      return 0;
  }
}

export const IncidentDetailPage: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { canApprove } = useAuth();

  const [incident, setIncident] = useState<IncidentDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<
    "rca" | "decision" | "execution" | "verification" | "evidence" | "audit"
  >("rca");

  // Approval modal state
  const [showRejectModal, setShowRejectModal] = useState(false);
  const [rejectionComment, setRejectionComment] = useState("");
  const [submittingAction, setSubmittingAction] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const fetchIncident = async (isManual = false) => {
    if (!id) return;
    if (isManual) setRefreshing(true);
    setError(null);
    try {
      const data = await api.getIncident(id);
      setIncident(data);
    } catch (err: any) {
      setError(err?.message || "Failed to load incident detail");
    } finally {
      setLoading(false);
      if (isManual) setRefreshing(false);
    }
  };

  useEffect(() => {
    fetchIncident();
    const interval = setInterval(() => {
      fetchIncident();
    }, 10000);
    return () => clearInterval(interval);
  }, [id]);

  const handleApprove = async () => {
    if (!incident) return;
    setActionError(null);
    setSubmittingAction(true);
    try {
      await api.approveIncident(incident.id);
      await fetchIncident(true);
    } catch (err: any) {
      setActionError(err?.message || "Failed to approve incident");
    } finally {
      setSubmittingAction(false);
    }
  };

  const handleReject = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!incident || !rejectionComment.trim()) return;
    setActionError(null);
    setSubmittingAction(true);
    try {
      await api.rejectIncident(incident.id, rejectionComment.trim());
      setShowRejectModal(false);
      setRejectionComment("");
      await fetchIncident(true);
    } catch (err: any) {
      setActionError(err?.message || "Failed to reject incident");
    } finally {
      setSubmittingAction(false);
    }
  };

  if (loading) {
    return (
      <div className="page-container">
        <div className="panel-loading">
          <div className="spinner" />
          <p>Loading incident details...</p>
        </div>
      </div>
    );
  }

  if (error || !incident) {
    return (
      <div className="page-container">
        <div className="alert-error mb-4">{error || "Incident not found"}</div>
        <Link to="/incidents" className="btn-secondary">
          ← Back to Incidents
        </Link>
      </div>
    );
  }

  const currentStageIndex = getStageIndex(incident.state);
  const isAwaitingApproval = incident.state === "AWAITING_APPROVAL";

  return (
    <div className="page-container">
      {/* Breadcrumb */}
      <div className="breadcrumb">
        <Link to="/incidents">Incidents</Link>
        <span className="breadcrumb-separator">/</span>
        <span className="breadcrumb-current">{incident.alert_name}</span>
      </div>

      {/* Incident Header Card */}
      <div className="incident-header-panel">
        <div className="incident-header-top">
          <div className="incident-title-block">
            <div className="flex-row items-center gap-3">
              <h1 className="incident-title">{incident.alert_name}</h1>
              <StateBadge state={incident.state} />
              <SeverityBadge severity={incident.severity} />
            </div>
            <div className="incident-meta-row">
              <span className="meta-item">
                <strong>Target:</strong>{" "}
                {incident.target_kind ? `${incident.target_kind}/` : ""}
                {incident.target_name || "Unknown"}
              </span>
              <span className="meta-item">
                <strong>Namespace:</strong> {incident.namespace || "—"}
              </span>
              <span className="meta-item">
                <strong>Environment:</strong> {incident.environment || "production"}
              </span>
              <span className="meta-item">
                <strong>Fingerprint:</strong>{" "}
                <code className="code-inline">{incident.fingerprint.slice(0, 16)}...</code>
              </span>
              <span className="meta-item">
                <strong>Detected:</strong> {new Date(incident.created_at).toLocaleString()}
              </span>
            </div>
          </div>

          <div className="incident-header-actions">
            <button
              onClick={() => fetchIncident(true)}
              className="btn-secondary btn-icon"
              disabled={refreshing}
            >
              <RefreshIcon
                size={15}
                className={refreshing ? "spin-animation" : ""}
              />
              {refreshing ? "Refreshing..." : "Refresh"}
            </button>
          </div>
        </div>

        {/* Pipeline Stage Tracker */}
        <div className="pipeline-stepper">
          {PIPELINE_STAGES.map((stage, idx) => {
            const isCompleted = idx < currentStageIndex;
            const isCurrent = idx === currentStageIndex;
            const isFailed =
              isCurrent &&
              (incident.state === "FAILED" ||
                incident.state === "ESCALATED" ||
                incident.state === "REJECTED");

            return (
              <div
                key={stage.key}
                className={`stepper-step ${
                  isCompleted
                    ? "step-completed"
                    : isCurrent
                    ? isFailed
                      ? "step-failed"
                      : "step-active"
                    : "step-pending"
                }`}
              >
                <div className="step-circle">
                  {isCompleted ? (
                    <CheckCircleIcon size={14} />
                  ) : isFailed ? (
                    <XCircleIcon size={14} />
                  ) : (
                    <span>{idx + 1}</span>
                  )}
                </div>
                <span className="step-label">{stage.label}</span>
              </div>
            );
          })}
        </div>
      </div>

      {/* Approval Required Banner */}
      {isAwaitingApproval && (
        <div className="banner-approval-action mt-4">
          <div className="flex-row items-center gap-3">
            <ShieldAlertIcon size={28} className="text-warning" />
            <div>
              <h3>Action Requires Human Sign-Off</h3>
              <p>
                Recommended action:{" "}
                <strong>
                  <code>
                    {incident.decision?.recommended_action || "Catalog Action"}
                  </code>
                </strong>
                . Risk reason:{" "}
                <em>{incident.decision?.risk_reason || "Policy constraint"}</em>
              </p>
            </div>
          </div>
          <div className="approval-action-buttons">
            {canApprove ? (
              <>
                <button
                  onClick={() => setShowRejectModal(true)}
                  className="btn-danger"
                  disabled={submittingAction}
                >
                  Reject...
                </button>
                <button
                  onClick={handleApprove}
                  className="btn-success"
                  disabled={submittingAction}
                >
                  {submittingAction ? "Approving..." : "Approve & Execute"}
                </button>
              </>
            ) : (
              <span className="text-muted text-sm">
                Viewer role: approver or admin required to authorize execution.
              </span>
            )}
          </div>
        </div>
      )}

      {actionError && <div className="alert-error mt-4">{actionError}</div>}

      {/* Detail Tabs */}
      <div className="tabs-header mt-6">
        <button
          className={`tab-btn ${activeTab === "rca" ? "tab-btn-active" : ""}`}
          onClick={() => setActiveTab("rca")}
        >
          <CpuIcon size={16} />
          Root Cause Analysis
        </button>
        <button
          className={`tab-btn ${
            activeTab === "decision" ? "tab-btn-active" : ""
          }`}
          onClick={() => setActiveTab("decision")}
        >
          <ActivityIcon size={16} />
          Policy Decision
        </button>
        <button
          className={`tab-btn ${
            activeTab === "execution" ? "tab-btn-active" : ""
          }`}
          onClick={() => setActiveTab("execution")}
        >
          <TerminalIcon size={16} />
          Remediation Job
        </button>
        <button
          className={`tab-btn ${
            activeTab === "verification" ? "tab-btn-active" : ""
          }`}
          onClick={() => setActiveTab("verification")}
        >
          <CheckCircleIcon size={16} />
          Closed-Loop Verification
        </button>
        <button
          className={`tab-btn ${
            activeTab === "evidence" ? "tab-btn-active" : ""
          }`}
          onClick={() => setActiveTab("evidence")}
        >
          <FileTextIcon size={16} />
          Evidence ({incident.evidence?.length || 0})
        </button>
        <button
          className={`tab-btn ${
            activeTab === "audit" ? "tab-btn-active" : ""
          }`}
          onClick={() => setActiveTab("audit")}
        >
          <ClockIcon size={16} />
          Audit Trail ({incident.audit_events?.length || 0})
        </button>
      </div>

      {/* Tab Contents */}
      <div className="tab-body">
        {/* TAB 1: RCA */}
        {activeTab === "rca" && (
          <div className="tab-pane">
            {incident.rca_json ? (
              <div className="space-y-4">
                <div className="info-card">
                  <h3 className="section-title">Root Cause Summary</h3>
                  <p className="text-body mt-2 leading-relaxed">
                    {incident.rca_json.summary ||
                      incident.rca_json.root_cause ||
                      "Root cause identified by agent analysis."}
                  </p>
                </div>

                {incident.rca_json.claims && incident.rca_json.claims.length > 0 && (
                  <div className="info-card">
                    <h3 className="section-title">Investigated Claims & Evidence Citations</h3>
                    <div className="claims-list mt-3">
                      {incident.rca_json.claims.map((claimObj: any, index: number) => (
                        <div key={index} className="claim-item">
                          <span className="claim-number">#{index + 1}</span>
                          <div className="claim-content">
                            <p className="claim-text">{claimObj.claim}</p>
                            {claimObj.cited_evidence_ids && (
                              <div className="claim-citations">
                                <span className="text-xs text-muted">Cited Evidence: </span>
                                {claimObj.cited_evidence_ids.map((cid: string) => (
                                  <span key={cid} className="evidence-chip">
                                    {cid.slice(0, 8)}
                                  </span>
                                ))}
                              </div>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {incident.rca_json.recommended_action && (
                  <div className="info-card">
                    <h3 className="section-title">Recommended Remediation Action</h3>
                    <div className="flex-row items-center gap-3 mt-2">
                      <code className="code-pill font-bold text-base">
                        {incident.rca_json.recommended_action}
                      </code>
                    </div>
                    {incident.rca_json.action_parameters && (
                      <div className="mt-3">
                        <span className="text-xs text-muted block mb-1">Parameters:</span>
                        <pre className="code-block">
                          {JSON.stringify(incident.rca_json.action_parameters, null, 2)}
                        </pre>
                      </div>
                    )}
                  </div>
                )}
              </div>
            ) : (
              <div className="panel-empty">
                <ActivityIcon size={36} className="text-muted" />
                <h3>RCA in progress or not yet available</h3>
                <p className="text-muted">
                  Evidence collection and LLM investigation have not produced an RCA yet.
                </p>
              </div>
            )}
          </div>
        )}

        {/* TAB 2: Decision */}
        {activeTab === "decision" && (
          <div className="tab-pane">
            {incident.decision ? (
              <div className="space-y-4">
                <div className="grid-2-col">
                  <div className="info-card">
                    <h3 className="section-title">Policy Routing Outcome</h3>
                    <div className="stat-value-lg mt-2 text-highlight">
                      {incident.decision.outcome}
                    </div>
                    <p className="text-muted text-sm mt-1">
                      Reason: {incident.decision.reason}
                    </p>
                  </div>

                  <div className="info-card">
                    <h3 className="section-title">Composite Confidence</h3>
                    <div className="stat-value-lg mt-2 text-info">
                      {(incident.decision.confidence * 100).toFixed(1)}%
                    </div>
                    <div className="confidence-breakdown mt-3">
                      <div className="score-row">
                        <span>LLM Confidence (20%):</span>
                        <span className="font-mono">
                          {(incident.decision.llm_confidence * 100).toFixed(0)}%
                        </span>
                      </div>
                      <div className="score-row">
                        <span>Grounding Score (40%):</span>
                        <span className="font-mono">
                          {(incident.decision.grounding_score * 100).toFixed(0)}%
                        </span>
                      </div>
                      <div className="score-row">
                        <span>Runbook Similarity (40%):</span>
                        <span className="font-mono">
                          {(incident.decision.runbook_score * 100).toFixed(0)}%
                        </span>
                      </div>
                    </div>
                  </div>
                </div>

                <div className="info-card">
                  <h3 className="section-title">Risk Analysis</h3>
                  <div className="score-row mt-2">
                    <span>Evaluated Risk Score:</span>
                    <span className="font-mono font-bold">
                      {(incident.decision.risk_score * 100).toFixed(0)}%
                    </span>
                  </div>
                  <div className="score-row mt-1">
                    <span>Matched Policy Rule:</span>
                    <code className="code-pill">{incident.decision.risk_reason}</code>
                  </div>
                </div>
              </div>
            ) : (
              <div className="panel-empty">
                <ClockIcon size={36} className="text-muted" />
                <h3>No Decision Persisted</h3>
                <p className="text-muted">
                  Policy engine evaluates incidents after investigation completes.
                </p>
              </div>
            )}
          </div>
        )}

        {/* TAB 3: Execution */}
        {activeTab === "execution" && (
          <div className="tab-pane">
            {incident.execution ? (
              <div className="space-y-4">
                <div className="info-card">
                  <div className="flex-row justify-between items-center">
                    <div>
                      <h3 className="section-title">Kubernetes Execution Job</h3>
                      <p className="text-muted text-sm mt-1">
                        Action: <strong>{incident.execution.action_id}</strong>
                      </p>
                    </div>
                    <span className="badge-dim font-bold">
                      {incident.execution.status}
                    </span>
                  </div>
                  <div className="meta-grid mt-3">
                    <div>
                      <span className="text-xs text-muted block">Job Name</span>
                      <code>{incident.execution.job_name || "—"}</code>
                    </div>
                    <div>
                      <span className="text-xs text-muted block">Snapshot Hash</span>
                      <code>{incident.execution.snapshot_hash || "—"}</code>
                    </div>
                    <div>
                      <span className="text-xs text-muted block">Started At</span>
                      <span className="text-sm">
                        {new Date(incident.execution.started_at).toLocaleString()}
                      </span>
                    </div>
                  </div>
                </div>

                <div className="info-card">
                  <h3 className="section-title flex-row items-center gap-2">
                    <TerminalIcon size={16} />
                    Executor Pod Logs
                  </h3>
                  <pre className="terminal-logs mt-2">
                    {incident.execution.logs || "No executor logs recorded."}
                  </pre>
                </div>
              </div>
            ) : (
              <div className="panel-empty">
                <TerminalIcon size={36} className="text-muted" />
                <h3>No Execution Job Created</h3>
                <p className="text-muted">
                  Jobs are created only after policy authorization or operator approval.
                </p>
              </div>
            )}
          </div>
        )}

        {/* TAB 4: Verification */}
        {activeTab === "verification" && (
          <div className="tab-pane">
            {incident.verification ? (
              <div className="space-y-4">
                <div className="info-card">
                  <div className="flex-row justify-between items-center">
                    <div>
                      <h3 className="section-title">Closed-Loop Verification</h3>
                      <p className="text-muted text-sm mt-1">
                        Effectiveness Score:{" "}
                        <strong>
                          {(incident.verification.effectiveness_score * 100).toFixed(0)}%
                        </strong>
                      </p>
                    </div>
                    <span
                      className={`badge-pill ${
                        incident.verification.status === "PASSED"
                          ? "badge-success"
                          : incident.verification.status === "RUNNING"
                          ? "badge-warning"
                          : "badge-danger"
                      }`}
                    >
                      {incident.verification.status}
                    </span>
                  </div>
                  {incident.verification.resolution_summary && (
                    <p className="text-body mt-3">
                      {incident.verification.resolution_summary}
                    </p>
                  )}
                </div>

                <div className="info-card">
                  <h3 className="section-title">Automated Health Checks</h3>
                  <div className="table-responsive mt-3">
                    <table className="data-table">
                      <thead>
                        <tr>
                          <th>Check</th>
                          <th>Status</th>
                          <th>Details</th>
                        </tr>
                      </thead>
                      <tbody>
                        {incident.verification.checks.map((check, idx) => (
                          <tr key={idx}>
                            <td className="font-medium">{check.name}</td>
                            <td>
                              {check.passed ? (
                                <span className="text-success flex-row items-center gap-1">
                                  <CheckCircleIcon size={14} /> Passed
                                </span>
                              ) : (
                                <span className="text-danger flex-row items-center gap-1">
                                  <XCircleIcon size={14} /> Failed
                                </span>
                              )}
                            </td>
                            <td className="text-muted text-sm">
                              {check.detail || "—"}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              </div>
            ) : (
              <div className="panel-empty">
                <CheckCircleIcon size={36} className="text-muted" />
                <h3>Verification Not Initiated</h3>
                <p className="text-muted">
                  Verification triggers automatically once an executor Job succeeds.
                </p>
              </div>
            )}
          </div>
        )}

        {/* TAB 5: Evidence */}
        {activeTab === "evidence" && (
          <div className="tab-pane">
            {incident.evidence && incident.evidence.length > 0 ? (
              <div className="space-y-4">
                {incident.evidence.map((ev) => (
                  <div key={ev.id} className="info-card">
                    <div className="flex-row justify-between items-center">
                      <div className="flex-row items-center gap-2">
                        <code className="code-pill font-bold">{ev.tool_name}</code>
                        <span className="text-muted text-xs">
                          {new Date(ev.created_at).toLocaleTimeString()}
                        </span>
                      </div>
                      <span className="text-muted text-xs">
                        ID: {ev.id.slice(0, 8)}
                      </span>
                    </div>

                    {ev.args_json && Object.keys(ev.args_json).length > 0 && (
                      <div className="mt-2">
                        <span className="text-xs text-muted block mb-1">Arguments:</span>
                        <pre className="code-block">
                          {JSON.stringify(ev.args_json, null, 2)}
                        </pre>
                      </div>
                    )}

                    <div className="mt-2">
                      <span className="text-xs text-muted block mb-1">Result:</span>
                      <pre className="terminal-logs">{ev.result_text}</pre>
                    </div>

                    {ev.error && (
                      <div className="alert-error mt-2">Error: {ev.error}</div>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              <div className="panel-empty">
                <FileTextIcon size={36} className="text-muted" />
                <h3>No Evidence Recorded</h3>
                <p className="text-muted">
                  The investigator collects read-only cluster and metric evidence.
                </p>
              </div>
            )}
          </div>
        )}

        {/* TAB 6: Audit */}
        {activeTab === "audit" && (
          <div className="tab-pane">
            {incident.audit_events && incident.audit_events.length > 0 ? (
              <div className="timeline">
                {incident.audit_events.map((audit) => (
                  <div key={audit.id} className="timeline-item">
                    <div className="timeline-point" />
                    <div className="timeline-content">
                      <div className="flex-row justify-between items-center">
                        <div>
                          <span className="font-semibold text-highlight">
                            {audit.from_state ? `${audit.from_state} → ` : ""}
                            {audit.to_state}
                          </span>
                          <span className="text-muted text-xs ml-2">
                            by {audit.actor}
                          </span>
                        </div>
                        <span className="text-muted text-xs">
                          {new Date(audit.created_at).toLocaleString()}
                        </span>
                      </div>
                      {audit.detail_json && Object.keys(audit.detail_json).length > 0 && (
                        <pre className="code-block mt-2">
                          {JSON.stringify(audit.detail_json, null, 2)}
                        </pre>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="panel-empty">
                <ClockIcon size={36} className="text-muted" />
                <h3>No Audit Events</h3>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Reject Modal */}
      {showRejectModal && (
        <div className="modal-backdrop">
          <div className="modal-dialog">
            <h3 className="modal-title">Reject Incident Remediation</h3>
            <p className="modal-description">
              Provide a reason for rejecting this automated action. The incident will
              transition to <strong>ESCALATED</strong> for human investigation.
            </p>

            <form onSubmit={handleReject}>
              <div className="form-group mt-3">
                <label htmlFor="rejection-comment">Rejection Comment (required)</label>
                <textarea
                  id="rejection-comment"
                  rows={4}
                  required
                  placeholder="Explain why the recommended action was rejected..."
                  className="textarea-input"
                  value={rejectionComment}
                  onChange={(e) => setRejectionComment(e.target.value)}
                />
              </div>

              <div className="modal-actions mt-4">
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={() => setShowRejectModal(false)}
                  disabled={submittingAction}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="btn-danger"
                  disabled={submittingAction || !rejectionComment.trim()}
                >
                  {submittingAction ? "Rejecting..." : "Confirm Rejection"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
};
