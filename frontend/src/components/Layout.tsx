import React from "react";
import { Navbar } from "./Navbar";

export const Layout: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  return (
    <div className="app-shell">
      <Navbar />
      <main className="main-content">{children}</main>
      <footer className="app-footer">
        <div className="footer-content">
          <span>IncidentPilot · Automated Kubernetes Incident Response</span>
          <span className="footer-phase">Evidence First · Safe Actions · Closed-Loop Verification</span>
        </div>
      </footer>
    </div>
  );
};
