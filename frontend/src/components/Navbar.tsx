import React, { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import { api } from "../api/client";
import { RoleBadge } from "./Badge";
import { ShieldAlertIcon, LogOutIcon } from "./Icons";

export const Navbar: React.FC = () => {
  const { user, logout } = useAuth();
  const location = useLocation();
  const [pendingCount, setPendingCount] = useState<number>(0);
  const [isReady, setIsReady] = useState<boolean | null>(null);

  useEffect(() => {
    let mounted = true;

    const checkStatus = async () => {
      try {
        await api.getReadiness();
        if (mounted) setIsReady(true);
      } catch {
        if (mounted) setIsReady(false);
      }
    };

    const fetchPending = async () => {
      try {
        const approvals = await api.getPendingApprovals();
        if (mounted) setPendingCount(approvals.length);
      } catch {
        // Ignored
      }
    };

    checkStatus();
    fetchPending();

    const interval = setInterval(() => {
      fetchPending();
    }, 15000);

    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, []);

  const isActive = (path: string) => {
    if (path === "/" && location.pathname === "/") return true;
    if (path !== "/" && location.pathname.startsWith(path)) return true;
    return false;
  };

  return (
    <header className="navbar-container">
      <div className="navbar-left">
        <Link to="/" className="brand" aria-label="IncidentPilot Home">
          <span className="brand-mark">IP</span>
          <span className="brand-title">IncidentPilot</span>
        </Link>
        <span className="navbar-tag">v2.0</span>

        <nav className="navbar-links">
          <Link
            to="/"
            className={`nav-link ${isActive("/") ? "nav-link-active" : ""}`}
          >
            Dashboard
          </Link>
          <Link
            to="/incidents"
            className={`nav-link ${
              isActive("/incidents") ? "nav-link-active" : ""
            }`}
          >
            Incidents
          </Link>
          <Link
            to="/approvals"
            className={`nav-link ${
              isActive("/approvals") ? "nav-link-active" : ""
            }`}
          >
            <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <ShieldAlertIcon size={15} />
              Approvals
              {pendingCount > 0 && (
                <span className="nav-badge-pill">{pendingCount}</span>
              )}
            </span>
          </Link>
        </nav>
      </div>

      <div className="navbar-right">
        {/* System readiness indicator */}
        <div
          className="system-health-pill"
          title={
            isReady === true
              ? "Cluster API & Data services ready"
              : isReady === false
              ? "Backend or dependencies not ready"
              : "Checking system status..."
          }
        >
          <span
            className={`health-dot ${
              isReady === true
                ? "health-dot-online"
                : isReady === false
                ? "health-dot-offline"
                : "health-dot-unknown"
            }`}
          />
          <span className="health-label">
            {isReady === true ? "Cluster Ready" : isReady === false ? "Degraded" : "Connecting"}
          </span>
        </div>

        {user && (
          <div className="user-profile">
            <span className="user-name">{user.username}</span>
            <RoleBadge role={user.role} />
            <button
              onClick={logout}
              className="btn-logout"
              title="Sign out"
              aria-label="Sign out"
            >
              <LogOutIcon size={16} />
            </button>
          </div>
        )}
      </div>
    </header>
  );
};
