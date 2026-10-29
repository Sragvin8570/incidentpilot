import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useAuth } from "../context/AuthContext";
import { PendingApproval } from "../types";
import { SeverityBadge } from "../components/Badge";
import {
  CheckCircleIcon,
  ChevronRightIcon,
  ClockIcon,
  RefreshIcon,
  ShieldAlertIcon,
  XCircleIcon,
} from "../components/Icons";

export const ApprovalsPage: React.FC = () => {
  const { canApprove } = useAuth();
  const [approvals, setApprovals] = useState<PendingApproval[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Rejection modal state
  const [selectedIncidentId, setSelectedIncidentId] = useState<string | null>(
    null
  );
  const [comment, setComment] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const fetchApprovals = async (isManual = false) => {
    if (isManual) setRefreshing(true);
    setError(null);
    try {
      const data = await api.getPendingApprovals();
      setApprovals(data);
    } catch (err: any) {
      setError(err?.message || "Failed to load pending approvals");
    } finally {
      setLoading(false);
      if (isManual) setRefreshing(false);
    }
  };

  useEffect(() => {
    fetchApprovals();
    const interval = setInterval(() => {
      fetchApprovals();
    }, 10000);
    return () => clearInterval(interval);
  }, []);

  const handleApprove = async (id: string) => {
    setActionError(null);
    setSubmitting(true);
    try {
      await api.approveIncident(id);
      await fetchApprovals(true);
    } catch (err: any) {
      setActionError(err?.message || "Failed to approve incident");
    } finally {
      setSubmitting(false);
    }
  };

  const handleReject = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedIncidentId || !comment.trim()) return;
    setActionError(null);
    setSubmitting(true);
    try {
      await api.rejectIncident(selectedIncidentId, comment.trim());
      setSelectedIncidentId(null);
      setComment("");
      await fetchApprovals(true);
    } catch (err: any) {
      setActionError(err?.message || "Failed to reject incident");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="page-container">
      {/* Header */}
      <div className="page-header">
        <div>
          <h1 className="page-title">Pending Approvals</h1>
          <p className="page-description">
            High-risk or production remediation actions that require explicit operator
            confirmation before running Kubernetes Jobs.
          </p>
        </div>
        <div className="header-actions">
          <button
            onClick={() => fetchApprovals(true)}
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

      {error && <div className="alert-error mb-4">{error}</div>}
      {actionError && <div className="alert-error mb-4">{actionError}</div>}

      {!canApprove && (
        <div className="alert-warning mb-4">
          <ShieldAlertIcon size={18} />
          <span>
            You are logged in with <strong>Viewer</strong> privileges. Only users
            with the <strong>Approver</strong> or <strong>Admin</strong> role can
            authorize or reject remediation actions.
          </span>
        </div>
      )}

      {/* Approvals List */}
      {loading ? (
        <div className="panel-loading">
          <div className="spinner" />
          <p>Checking for pending approvals...</p>
        </div>
      ) : approvals.length === 0 ? (
        <div className="panel panel-empty">
          <CheckCircleIcon size={44} className="text-success" />
          <h3>All Clear — No Pending Approvals</h3>
          <p className="text-muted">
            There are currently no incidents awaiting operator review.
          </p>
          <Link to="/incidents" className="btn-secondary mt-3">
            Browse All Incidents
          </Link>
        </div>
      ) : (
        <div className="approvals-grid">
          {approvals.map((item) => (
            <div key={item.incident_id} className="approval-card">
              <div className="approval-card-header">
                <div className="flex-row items-center gap-2">
                  <h3 className="approval-card-title">{item.alert_name}</h3>
                  <SeverityBadge severity={item.severity} />
                </div>
                <ExpiryCountdown expiresAt={item.expires_at} />
              </div>

              <div className="approval-meta-grid">
                <div>
                  <span className="text-xs text-muted block">Target Workload</span>
                  <strong>
                    {item.target_kind ? `${item.target_kind}/` : ""}
                    {item.target_name || "—"}
                  </strong>
                </div>
                <div>
                  <span className="text-xs text-muted block">Namespace</span>
                  <code>{item.namespace || "—"}</code>
                </div>
                <div className="col-span-2">
                  <span className="text-xs text-muted block">
                    Recommended Action
                  </span>
                  <code className="code-pill font-bold">
                    {item.recommended_action}
                  </code>
                </div>
                <div className="col-span-2">
                  <span className="text-xs text-muted block">Routing Reason</span>
                  <p className="text-body text-sm mt-1">{item.reason}</p>
                </div>
              </div>

              <div className="approval-card-footer">
                <Link
                  to={`/incidents/${item.incident_id}`}
                  className="btn-ghost btn-sm"
                >
                  Inspect RCA & Evidence
                  <ChevronRightIcon size={14} />
                </Link>

                {canApprove && (
                  <div className="flex-row items-center gap-2">
                    <button
                      onClick={() => {
                        setSelectedIncidentId(item.incident_id);
                        setComment("");
                      }}
                      className="btn-danger btn-sm"
                      disabled={submitting}
                    >
                      Reject
                    </button>
                    <button
                      onClick={() => handleApprove(item.incident_id)}
                      className="btn-success btn-sm"
                      disabled={submitting}
                    >
                      {submitting ? "Processing..." : "Approve & Execute"}
                    </button>
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Reject Modal */}
      {selectedIncidentId && (
        <div className="modal-backdrop">
          <div className="modal-dialog">
            <h3 className="modal-title">Reject Remediation Action</h3>
            <p className="modal-description">
              Please enter the operational reason for rejecting this automated
              action. The incident will transition to <strong>ESCALATED</strong>.
            </p>

            <form onSubmit={handleReject}>
              <div className="form-group mt-3">
                <label htmlFor="rejection-comment">Rejection Comment (required)</label>
                <textarea
                  id="rejection-comment"
                  rows={4}
                  required
                  placeholder="e.g., Scheduled maintenance in progress or manual failover planned..."
                  className="textarea-input"
                  value={comment}
                  onChange={(e) => setComment(e.target.value)}
                />
              </div>

              <div className="modal-actions mt-4">
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={() => setSelectedIncidentId(null)}
                  disabled={submitting}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="btn-danger"
                  disabled={submitting || !comment.trim()}
                >
                  {submitting ? "Rejecting..." : "Confirm Rejection"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
};

function ExpiryCountdown({ expiresAt }: { expiresAt: string }) {
  const [timeLeft, setTimeLeft] = useState<string>("");
  const [isUrgent, setIsUrgent] = useState(false);

  useEffect(() => {
    const update = () => {
      const diff = new Date(expiresAt).getTime() - Date.now();
      if (diff <= 0) {
        setTimeLeft("Expired");
        setIsUrgent(true);
        return;
      }
      const mins = Math.floor(diff / 60000);
      const secs = Math.floor((diff % 60000) / 1000);
      setTimeLeft(`${mins}m ${secs}s left`);
      setIsUrgent(mins < 5);
    };

    update();
    const timer = setInterval(update, 1000);
    return () => clearInterval(timer);
  }, [expiresAt]);

  return (
    <span
      className={`expiry-badge ${isUrgent ? "expiry-urgent" : ""}`}
      title={`Expires at ${new Date(expiresAt).toLocaleTimeString()}`}
    >
      <ClockIcon size={13} />
      {timeLeft}
    </span>
  );
}
