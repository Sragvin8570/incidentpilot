import React, { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { IncidentListItem, IncidentState } from "../types";
import { StateBadge, SeverityBadge } from "../components/Badge";
import {
  CheckCircleIcon,
  ChevronRightIcon,
  RefreshIcon,
  SearchIcon,
} from "../components/Icons";

const ALL_STATES: IncidentState[] = [
  "RECEIVED",
  "UNSCOPED",
  "ENRICHED",
  "INVESTIGATING",
  "ANALYZED",
  "READY",
  "AWAITING_APPROVAL",
  "APPROVED",
  "REJECTED",
  "EXPIRED",
  "EXECUTING",
  "VERIFYING",
  "RESOLVED",
  "ESCALATED",
  "FAILED",
];

export const IncidentsPage: React.FC = () => {
  const [searchParams, setSearchParams] = useSearchParams();

  const [incidents, setIncidents] = useState<IncidentListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Filters from query params
  const stateFilter = searchParams.get("state") || "";
  const severityFilter = searchParams.get("severity") || "";
  const searchFilter = searchParams.get("search") || "";
  const page = parseInt(searchParams.get("page") || "1", 10);
  const limit = 15;
  const offset = (page - 1) * limit;

  const [searchInput, setSearchInput] = useState(searchFilter);

  const fetchIncidents = async (isManual = false) => {
    if (isManual) setRefreshing(true);
    setError(null);
    try {
      const res = await api.getIncidents({
        state: stateFilter || undefined,
        severity: severityFilter || undefined,
        search: searchFilter || undefined,
        limit,
        offset,
      });
      setIncidents(res.items);
      setTotal(res.total);
    } catch (err: any) {
      setError(err?.message || "Failed to load incidents");
    } finally {
      setLoading(false);
      if (isManual) setRefreshing(false);
    }
  };

  useEffect(() => {
    fetchIncidents();
  }, [stateFilter, severityFilter, searchFilter, page]);

  const updateParam = (key: string, value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value) {
      next.set(key, value);
    } else {
      next.delete(key);
    }
    next.set("page", "1"); // reset page on filter change
    setSearchParams(next);
  };

  const handleSearchSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    updateParam("search", searchInput.trim());
  };

  const totalPages = Math.max(1, Math.ceil(total / limit));

  return (
    <div className="page-container">
      {/* Page Header */}
      <div className="page-header">
        <div>
          <h1 className="page-title">Incidents Catalog</h1>
          <p className="page-description">
            Complete record of alerts, root cause analyses, policy decisions, and
            execution verifications.
          </p>
        </div>
        <div className="header-actions">
          <button
            onClick={() => fetchIncidents(true)}
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

      {/* Filter Toolbar */}
      <div className="filter-bar">
        <form onSubmit={handleSearchSubmit} className="search-box">
          <SearchIcon size={16} className="search-icon" />
          <input
            type="text"
            placeholder="Search by alert name or target..."
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
          />
          {searchInput && (
            <button
              type="button"
              className="search-clear-btn"
              onClick={() => {
                setSearchInput("");
                updateParam("search", "");
              }}
            >
              ×
            </button>
          )}
        </form>

        <div className="filter-group">
          <label htmlFor="state-filter" className="filter-label">
            State:
          </label>
          <select
            id="state-filter"
            className="select-input"
            value={stateFilter}
            onChange={(e) => updateParam("state", e.target.value)}
          >
            <option value="">All States</option>
            {ALL_STATES.map((s) => (
              <option key={s} value={s}>
                {s.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </div>

        <div className="filter-group">
          <label htmlFor="severity-filter" className="filter-label">
            Severity:
          </label>
          <select
            id="severity-filter"
            className="select-input"
            value={severityFilter}
            onChange={(e) => updateParam("severity", e.target.value)}
          >
            <option value="">All Severities</option>
            <option value="critical">Critical</option>
            <option value="warning">Warning</option>
            <option value="info">Info</option>
          </select>
        </div>

        {(stateFilter || severityFilter || searchFilter) && (
          <button
            className="btn-ghost btn-sm"
            onClick={() => {
              setSearchInput("");
              setSearchParams(new URLSearchParams());
            }}
          >
            Reset Filters
          </button>
        )}
      </div>

      {/* Table Panel */}
      <div className="panel mt-4">
        {loading ? (
          <div className="panel-loading">
            <div className="spinner" />
            <p>Loading incidents...</p>
          </div>
        ) : incidents.length === 0 ? (
          <div className="panel-empty">
            <CheckCircleIcon size={36} className="text-muted" />
            <h3>No matching incidents found</h3>
            <p className="text-muted">
              Try adjusting your search criteria or state filter.
            </p>
          </div>
        ) : (
          <div className="table-responsive">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Alert Name / Workload</th>
                  <th>Namespace / Env</th>
                  <th>Severity</th>
                  <th>Status</th>
                  <th>Recommendation</th>
                  <th>Confidence</th>
                  <th>Created</th>
                  <th className="text-right">Action</th>
                </tr>
              </thead>
              <tbody>
                {incidents.map((inc) => (
                  <tr key={inc.id}>
                    <td>
                      <div className="cell-primary">
                        <Link
                          to={`/incidents/${inc.id}`}
                          className="font-medium text-highlight"
                        >
                          {inc.alert_name}
                        </Link>
                        <span className="text-muted text-xs block">
                          {inc.target_kind ? `${inc.target_kind}/` : ""}
                          {inc.target_name || inc.fingerprint.slice(0, 12)}
                        </span>
                      </div>
                    </td>
                    <td>
                      <div className="text-sm">
                        <span>{inc.namespace || "—"}</span>
                        {inc.environment && (
                          <span className="text-muted text-xs block">
                            {inc.environment}
                          </span>
                        )}
                      </div>
                    </td>
                    <td>
                      <SeverityBadge severity={inc.severity} />
                    </td>
                    <td>
                      <StateBadge state={inc.state} />
                    </td>
                    <td>
                      {inc.recommended_action ? (
                        <code className="code-pill">
                          {inc.recommended_action}
                        </code>
                      ) : (
                        <span className="text-muted text-xs">—</span>
                      )}
                    </td>
                    <td>
                      {inc.confidence !== undefined && inc.confidence !== null ? (
                        <span className="font-mono text-sm">
                          {(inc.confidence * 100).toFixed(0)}%
                        </span>
                      ) : (
                        <span className="text-muted text-xs">—</span>
                      )}
                    </td>
                    <td className="text-muted text-sm">
                      {new Date(inc.created_at).toLocaleString()}
                    </td>
                    <td className="text-right">
                      <Link
                        to={`/incidents/${inc.id}`}
                        className="btn-ghost btn-sm"
                      >
                        Inspect
                        <ChevronRightIcon size={14} />
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* Pagination */}
        <div className="pagination-bar">
          <span className="text-muted text-sm">
            Showing {total === 0 ? 0 : offset + 1}–
            {Math.min(offset + limit, total)} of {total} incidents
          </span>
          <div className="pagination-controls">
            <button
              className="btn-secondary btn-sm"
              disabled={page <= 1}
              onClick={() => updateParam("page", (page - 1).toString())}
            >
              Previous
            </button>
            <span className="page-indicator text-sm">
              Page {page} of {totalPages}
            </span>
            <button
              className="btn-secondary btn-sm"
              disabled={page >= totalPages}
              onClick={() => updateParam("page", (page + 1).toString())}
            >
              Next
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};
