import React from "react";
import { IncidentState } from "../types";

export const StateBadge: React.FC<{ state: IncidentState | string }> = ({ state }) => {
  let bg = "#253143";
  let text = "#9cacc0";
  let border = "#3a4a61";

  switch (state) {
    case "AWAITING_APPROVAL":
      bg = "#3b2a0c";
      text = "#f6c35e";
      border = "#634714";
      break;
    case "APPROVED":
      bg = "#0f3629";
      text = "#77e2bd";
      border = "#1a5b45";
      break;
    case "EXECUTING":
      bg = "#1b2a4a";
      text = "#70a6ff";
      border = "#2b457a";
      break;
    case "VERIFYING":
      bg = "#221d45";
      text = "#bda7fc";
      border = "#3e327a";
      break;
    case "RESOLVED":
      bg = "#0f3629";
      text = "#77e2bd";
      border = "#1a5b45";
      break;
    case "INVESTIGATING":
    case "ENRICHED":
    case "ANALYZED":
    case "READY":
      bg = "#122c3f";
      text = "#64b5f6";
      border = "#1e4c6e";
      break;
    case "ESCALATED":
    case "FAILED":
    case "REJECTED":
      bg = "#3e1719";
      text = "#f87171";
      border = "#6e292d";
      break;
    case "EXPIRED":
    case "UNSCOPED":
    case "RECEIVED":
    default:
      bg = "#1c2430";
      text = "#9cacc0";
      border = "#2d3b4f";
      break;
  }

  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "6px",
        padding: "3px 9px",
        borderRadius: "6px",
        fontSize: "11px",
        fontWeight: 650,
        letterSpacing: "0.04em",
        backgroundColor: bg,
        color: text,
        border: `1px solid ${border}`,
        textTransform: "uppercase",
        whiteSpace: "nowrap",
      }}
    >
      <span
        style={{
          width: "6px",
          height: "6px",
          borderRadius: "50%",
          backgroundColor: text,
        }}
      />
      {state.replace(/_/g, " ")}
    </span>
  );
};

export const SeverityBadge: React.FC<{ severity?: string | null }> = ({
  severity,
}) => {
  if (!severity) return <span style={{ color: "#627285", fontSize: "12px" }}>—</span>;

  const s = severity.toLowerCase();
  let bg = "#1c2430";
  let text = "#9cacc0";
  let border = "#2d3b4f";

  if (s.includes("crit") || s === "p1") {
    bg = "#3e1719";
    text = "#f87171";
    border = "#6e292d";
  } else if (s.includes("warn") || s === "p2") {
    bg = "#3b2a0c";
    text = "#f6c35e";
    border = "#634714";
  } else if (s.includes("info") || s === "p3" || s === "p4") {
    bg = "#122c3f";
    text = "#64b5f6";
    border = "#1e4c6e";
  }

  return (
    <span
      style={{
        display: "inline-block",
        padding: "2px 7px",
        borderRadius: "4px",
        fontSize: "11px",
        fontWeight: 600,
        backgroundColor: bg,
        color: text,
        border: `1px solid ${border}`,
        textTransform: "uppercase",
      }}
    >
      {severity}
    </span>
  );
};

export const RoleBadge: React.FC<{ role?: string | null }> = ({ role }) => {
  if (!role) return null;
  const isApprover = role === "approver";
  const isAdmin = role === "admin";

  return (
    <span
      style={{
        padding: "2px 8px",
        borderRadius: "12px",
        fontSize: "11px",
        fontWeight: 600,
        textTransform: "uppercase",
        backgroundColor: isAdmin ? "#281b3b" : isApprover ? "#0f3629" : "#1a2332",
        color: isAdmin ? "#c084fc" : isApprover ? "#77e2bd" : "#94a3b8",
        border: `1px solid ${
          isAdmin ? "#4c2875" : isApprover ? "#1a5b45" : "#2d3b4f"
        }`,
      }}
    >
      {role}
    </span>
  );
};
