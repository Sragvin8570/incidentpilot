import {
  IncidentDetail,
  IncidentListResponse,
  IncidentStats,
  LoginResponse,
  PendingApproval,
} from "../types";

const TOKEN_KEY = "incidentpilot_token";
const USER_KEY = "incidentpilot_user";

class ApiError extends Error {
  status: number;
  data: any;

  constructor(status: number, message: string, data?: any) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.data = data;
  }
}

export const authStorage = {
  getToken(): string | null {
    return localStorage.getItem(TOKEN_KEY);
  },
  setToken(token: string): void {
    localStorage.setItem(TOKEN_KEY, token);
  },
  getUser(): { username: string; role: string } | null {
    const raw = localStorage.getItem(USER_KEY);
    if (!raw) return null;
    try {
      return JSON.parse(raw);
    } catch {
      return null;
    }
  },
  setUser(user: { username: string; role: string }): void {
    localStorage.setItem(USER_KEY, JSON.stringify(user));
  },
  clear(): void {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
  },
};

async function request<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const token = authStorage.getToken();
  const headers = new Headers(options.headers || {});

  if (!headers.has("Content-Type") && !(options.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }

  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  const response = await fetch(path, {
    ...options,
    headers,
  });

  if (response.status === 401) {
    authStorage.clear();
    // If not already on login page, redirect
    if (window.location.pathname !== "/login") {
      window.location.href = "/login";
    }
  }

  let data: any = null;
  const contentType = response.headers.get("content-type");
  if (contentType && contentType.includes("application/json")) {
    try {
      data = await response.json();
    } catch {
      data = null;
    }
  }

  if (!response.ok) {
    let message = "Request failed";
    if (data && typeof data === "object") {
      if (typeof data.detail === "string") {
        message = data.detail;
      } else if (Array.isArray(data.detail) && data.detail[0]?.msg) {
        message = data.detail[0].msg;
      } else if (typeof data.message === "string") {
        message = data.message;
      }
    }
    throw new ApiError(response.status, message, data);
  }

  return data as T;
}

export const api = {
  // Auth
  async login(username: string, password: string): Promise<LoginResponse> {
    const res = await request<LoginResponse>("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    authStorage.setToken(res.access_token);
    authStorage.setUser({ username, role: res.role });
    return res;
  },

  logout(): void {
    authStorage.clear();
  },

  // Health / Readiness
  async getHealth(): Promise<{ status: string }> {
    return request<{ status: string }>("/healthz");
  },

  async getReadiness(): Promise<{ status: string; dependencies?: string[] }> {
    return request<{ status: string; dependencies?: string[] }>("/readyz");
  },

  // Incidents
  async getIncidents(params: {
    state?: string;
    severity?: string;
    namespace?: string;
    environment?: string;
    search?: string;
    limit?: number;
    offset?: number;
  } = {}): Promise<IncidentListResponse> {
    const searchParams = new URLSearchParams();
    if (params.state) searchParams.set("state", params.state);
    if (params.severity) searchParams.set("severity", params.severity);
    if (params.namespace) searchParams.set("namespace", params.namespace);
    if (params.environment) searchParams.set("environment", params.environment);
    if (params.search) searchParams.set("search", params.search);
    if (params.limit !== undefined) searchParams.set("limit", params.limit.toString());
    if (params.offset !== undefined) searchParams.set("offset", params.offset.toString());

    const qs = searchParams.toString();
    return request<IncidentListResponse>(`/api/v1/incidents${qs ? `?${qs}` : ""}`);
  },

  async getIncident(id: string): Promise<IncidentDetail> {
    return request<IncidentDetail>(`/api/v1/incidents/${id}`);
  },

  async getStats(): Promise<IncidentStats> {
    return request<IncidentStats>("/api/v1/incidents/stats");
  },

  // Approvals
  async getPendingApprovals(): Promise<PendingApproval[]> {
    return request<PendingApproval[]>("/api/v1/approvals/pending");
  },

  async approveIncident(id: string): Promise<{ incident_id: string; state: string }> {
    return request<{ incident_id: string; state: string }>(
      `/api/v1/approvals/${id}/approve`,
      { method: "POST" }
    );
  },

  async rejectIncident(
    id: string,
    comment: string
  ): Promise<{ incident_id: string; state: string }> {
    return request<{ incident_id: string; state: string }>(
      `/api/v1/approvals/${id}/reject`,
      {
        method: "POST",
        body: JSON.stringify({ comment }),
      }
    );
  },
};

export { ApiError };
