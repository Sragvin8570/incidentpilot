export type UserRole = "viewer" | "approver" | "admin";

export interface User {
  username: string;
  role: UserRole;
}

export interface LoginResponse {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
  role: UserRole;
}

export type IncidentState =
  | "RECEIVED"
  | "UNSCOPED"
  | "ENRICHED"
  | "INVESTIGATING"
  | "ANALYZED"
  | "READY"
  | "AWAITING_APPROVAL"
  | "APPROVED"
  | "REJECTED"
  | "EXPIRED"
  | "EXECUTING"
  | "VERIFYING"
  | "RESOLVED"
  | "ESCALATED"
  | "FAILED";

export interface IncidentListItem {
  id: string;
  fingerprint: string;
  alert_name: string;
  severity?: string | null;
  namespace?: string | null;
  environment?: string | null;
  priority?: string | null;
  target_kind?: string | null;
  target_name?: string | null;
  state: IncidentState;
  created_at: string;
  resolved_at?: string | null;
  has_rca: boolean;
  recommended_action?: string | null;
  confidence?: number | null;
  risk_score?: number | null;
}

export interface IncidentListResponse {
  items: IncidentListItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface IncidentStats {
  total: number;
  active: number;
  awaiting_approval: number;
  executing: number;
  verifying: number;
  resolved: number;
  escalated: number;
  failed: number;
}

export interface EvidenceItem {
  id: string;
  tool_name: string;
  args_json: Record<string, any>;
  result_text: string;
  error?: string | null;
  created_at: string;
}

export interface DecisionDetail {
  id: string;
  recommended_action: string;
  outcome: string;
  confidence: number;
  llm_confidence: number;
  grounding_score: number;
  claim_grounding: Array<{
    claim: string;
    cited_evidence_ids: string[];
    is_grounded: boolean;
    reason?: string;
  }>;
  runbook_score: number;
  risk_score: number;
  risk_reason: string;
  reason: string;
  created_at: string;
}

export interface ExecutionDetail {
  id: string;
  action_id: string;
  status: string;
  job_name?: string | null;
  target_snapshot?: Record<string, any> | null;
  snapshot_hash?: string | null;
  applied_spec_hash?: string | null;
  patch_json?: Record<string, any> | null;
  logs: string;
  error?: string | null;
  started_at: string;
  completed_at?: string | null;
}

export interface VerificationCheck {
  name: string;
  passed: boolean;
  detail?: string;
  actual?: any;
  expected?: any;
}

export interface VerificationDetail {
  id: string;
  status: string;
  checks: VerificationCheck[];
  effectiveness_score: number;
  resolution_summary: string;
  started_at: string;
  completed_at?: string | null;
}

export interface AuditEventDetail {
  id: number;
  from_state?: string | null;
  to_state: string;
  actor: string;
  detail_json: Record<string, any>;
  created_at: string;
}

export interface AlertOccurrenceDetail {
  id: string;
  status: string;
  payload_json: Record<string, any>;
  received_at: string;
}

export interface IncidentDetail {
  id: string;
  fingerprint: string;
  alert_name: string;
  severity?: string | null;
  namespace?: string | null;
  environment?: string | null;
  priority?: string | null;
  target_kind?: string | null;
  target_name?: string | null;
  state: IncidentState;
  created_at: string;
  resolved_at?: string | null;
  rca_json?: {
    summary?: string;
    root_cause?: string;
    contributing_factors?: string[];
    recommended_action?: string;
    action_parameters?: Record<string, any>;
    confidence?: number;
    claims?: Array<{
      claim: string;
      cited_evidence_ids: string[];
    }>;
    [key: string]: any;
  } | null;
  decision?: DecisionDetail | null;
  execution?: ExecutionDetail | null;
  verification?: VerificationDetail | null;
  evidence: EvidenceItem[];
  audit_events: AuditEventDetail[];
  occurrences: AlertOccurrenceDetail[];
}

export interface PendingApproval {
  incident_id: string;
  alert_name: string;
  severity?: string | null;
  namespace?: string | null;
  target_kind?: string | null;
  target_name?: string | null;
  recommended_action: string;
  reason: string;
  created_at: string;
  expires_at: string;
}
