import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { IncidentListItem, IncidentStats, PendingApproval } from "../types";
import { StateBadge, SeverityBadge } from "../components/Badge";
import {
  ActivityIcon,
  AlertCircleIcon,
  CheckCircleIcon,
  ChevronRightIcon,
  ClockIcon,
  RefreshIcon,
  ShieldAlertIcon,
  XCircleIcon,
} from "../components/Icons";

export const DashboardPage: React.FC = () => {
  const [stats, setStats] = useState<IncidentStats | null>(null);
  const [recentIncidents, setRecentIncidents] = useState<IncidentListItem[]>([]);
  const [pendingApprovals, setPendingApprovals] = useState<PendingApproval[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [lastRefreshed, setLastRefreshed] = useState<Date>(new Date());
  const [error, setError] = useState<string | null>(null);

  const fetchData = async (isManual = false) => {
    if (isManual) setRefreshing(true);
    setError(null);
    try {
      const [statsRes, incidentsRes, approvalsRes] = await Promise.all([
        api.getStats(),
        api.getIncidents({ limit: 6 }),
        api.getPendingApprovals(),
      ]);
      setStats(statsRes);
      setRecentIncidents(incidentsRes.items);
      setPendingApprovals(approvalsRes);
      setLastRefreshed(new Date());
    } catch (err: any) {
      setError(err?.message || "Failed to load dashboard data");
    } finally {
      setLoading(false);
      if (isManual) setRefreshing(false);
    }
  };

  useEffect(() => {
    fetchData();
    const timer = setInterval(() => {
      fetchData();
    }, 12000);
    return () => clearInterval(timer);
  }, []);

  return (
    <div className="page-container">
      {/* Top action header */}
      <div className="page-header">
        <div>
          <h1 className="page-title">Operations Dashboard</h1>
          <p className="page-description">
            Live overview of autonomous incident intake, LLM investigation, policy
            routing, execution, and verification.
          </p>
        </div>
        <div className="header-actions">
          <span className="text-muted text-sm">
            Updated {lastRefreshed.toLocaleTimeString()}
          </span>
          <button
            onClick={() => fetchData(true)}
            className="btn-secondary btn-icon"
            disabled={refreshing}
            title="Refresh now"
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

      {/* Pending Approvals Alert Banner */}
      {pendingApprovals.length > 0 && (
        <div className="banner-attention">
          <div className="banner-attention-icon">
            <ShieldAlertIcon size={24} />
          </div>
          <div className="banner-attention-content">
            <h3>
              {pendingApprovals.length}{" "}
              {pendingApprovals.length === 1 ? "Incident" : "Incidents"} Awaiting
              Human Approval
            </h3>
            <p>
              High-risk actions or production workloads require operator sign-off
              before remediation Jobs are launched.
            </p>
          </div>
          <Link to="/approvals" className="btn-warning">
            Review Approvals
            <ChevronRightIcon size={16} />
          </Link>
        </div>
      )}

      {/* KPI Stats Grid */}
      <div className="stats-grid">
        <div className="stat-card">
          <div className="stat-header">
            <span className="stat-label">Active Incidents</span>
            <ActivityIcon size={18} className="text-info" />
          </div>
          <div className="stat-value text-info">
            {loading ? "—" : stats?.active ?? 0}
          </div>
          <div className="stat-footer">Under active remediation</div>
        </div>

        <div className="stat-card">
          <div className="stat-header">
            <span className="stat-label">Awaiting Approval</span>
            <ClockIcon size={18} className="text-warning" />
          </div>
          <div className="stat-value text-warning">
            {loading ? "—" : stats?.awaiting_approval ?? 0}
          </div>
          <div className="stat-footer">Pending human review</div>
        </div>

        <div className="stat-card">
          <div className="stat-header">
            <span className="stat-label">Executing / Verifying</span>
            <RefreshIcon size={18} className="text-purple" />
          </div>
          <div className="stat-value text-purple">
            {loading ? "—" : (stats?.executing ?? 0) + (stats?.verifying ?? 0)}
          </div>
          <div className="stat-footer">
            {stats?.executing ?? 0} jobs · {stats?.verifying ?? 0} verifying
          </div>
        </div>

        <div className="stat-card">
          <div className="stat-header">
            <span className="stat-label">Resolved</span>
            <CheckCircleIcon size={18} className="text-success" />
          </div>
          <div className="stat-value text-success">
            {loading ? "—" : stats?.resolved ?? 0}
          </div>
          <div className="stat-footer">Closed-loop verified</div>
        </div>

        <div className="stat-card">
          <div className="stat-header">
            <span className="stat-label">Escalated</span>
            <AlertCircleIcon size={18} className="text-danger" />
          </div>
          <div className="stat-value text-danger">
            {loading ? "—" : stats?.escalated ?? 0}
          </div>
          <div className="stat-footer">Handed over to SRE</div>
        </div>
      </div>

      {/* Recent Incidents Table */}
      <div className="panel mt-6">
        <div className="panel-header">
          <div>
            <h2 className="panel-title">Recent Incidents</h2>
            <p className="panel-subtitle">Latest alerts processed by the pipeline</p>
          </div>
          <Link to="/incidents" className="btn-link">
            View all incidents <ChevronRightIcon size={14} />
          </Link>
        </div>

        {loading ? (
          <div className="panel-loading">
            <div className="spinner" />
            <p>Loading incidents...</p>
          </div>
        ) : recentIncidents.length === 0 ? (
          <div className="panel-empty">
            <CheckCircleIcon size={40} className="text-muted" />
            <h3>No incidents recorded</h3>
            <p className="text-muted">
              Alertmanager webhook has not dispatched any alerts yet.
            </p>
          </div>
        ) : (
          <div className="table-responsive">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Alert / Workload</th>
                  <th>Namespace</th>
                  <th>Severity</th>
                  <th>State</th>
                  <th>Recommendation</th>
                  <th>Age</th>
                  <th className="text-right">Action</th>
                </tr>
              </thead>
              <tbody>
                {recentIncidents.map((incident) => (
                  <tr key={incident.id}>
                    <td>
                      <div className="cell-primary">
                        <Link
                          to={`/incidents/${incident.id}`}
                          className="font-medium text-highlight"
                        >
                          {incident.alert_name}
                        </Link>
                        <span className="text-muted text-xs block">
                          {incident.target_kind ? `${incident.target_kind}/` : ""}
                          {incident.target_name || incident.fingerprint.slice(0, 12)}
                        </span>
                      </div>
                    </td>
                    <td>
                      <span className="badge-dim">
                        {incident.namespace || "—"}
                      </span>
                    </td>
                    <td>
                      <SeverityBadge severity={incident.severity} />
                    </td>
                    <td>
                      <StateBadge state={incident.state} />
                    </td>
                    <td>
                      {incident.recommended_action ? (
                        <code className="code-pill">
                          {incident.recommended_action}
                        </code>
                      ) : (
                        <span className="text-muted text-xs">—</span>
                      )}
                    </td>
                    <td className="text-muted text-sm">
                      {formatTimeAgo(incident.created_at)}
                    </td>
                    <td className="text-right">
                      <Link
                        to={`/incidents/${incident.id}`}
                        className="btn-ghost btn-sm"
                      >
                        Inspect
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
};

function formatTimeAgo(isoDate: string): string {
  try {
    const diffSeconds = Math.floor(
      (Date.now() - new Date(isoDate).getTime()) / 1000
    );
    if (diffSeconds < 60) return `${diffSeconds}s ago`;
    const diffMinutes = Math.floor(diffSeconds / 60);
    if (diffMinutes < 60) return `${diffMinutes}m ago`;
    const diffHours = Math.floor(diffMinutes / 60);
    if (diffHours < 24) return `${diffHours}h ago`;
    return `${Math.floor(diffHours / 24)}d ago`;
  } catch {
    return isoDate;
  }
}
