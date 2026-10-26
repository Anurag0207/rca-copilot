/**
 * API client for the NL-to-Data Insight Platform backend.
 *
 * Wraps all backend REST endpoints with typed request/response interfaces.
 * Attaches the JWT access token to every request via an axios request interceptor.
 * Handles 401 responses by clearing the token and redirecting to the login page.
 *
 * Token storage: kept in module-level memory (not localStorage) to avoid XSS exposure.
 *
 * Usage:
 *   import { apiClient, setAuthToken, auth, chat, output } from "./client";
 */

import axios, { AxiosInstance, AxiosResponse } from "axios";

// ─────────────────────────────────────────────────────────────────────────────
// Type definitions matching backend Pydantic schemas
// ─────────────────────────────────────────────────────────────────────────────

export type OutputFormat = "chart" | "pdf" | "ppt";
export type ChartType = "bar" | "line" | "pie" | "table";
export type MessageRole = "user" | "assistant";

export interface UserResponse {
  id: string;
  username: string;
  email: string;
  is_active: boolean;
  created_at: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
}

export interface RegisterRequest {
  username: string;
  email: string;
  password: string;
}

export interface CreateSessionRequest {
  connection_id: string;
}

export interface SessionSummary {
  session_id: string;
  connection_id: string;
  created_at: string;
  last_activity: string;
  turn_count: number;
}

export interface SessionResponse {
  session_id: string;
  connection_id: string;
  created_at: string;
}

export interface MessageResponse {
  role: MessageRole;
  content: string;
  needs_clarification: boolean;
  clarification_question: string | null;
  download_token: string | null;
  pdf_download_token: string | null;
  output_format: OutputFormat | null;
  summary_stats: Record<string, unknown> | null;
  reasoning: string | null;
  generated_query: string | null;
  target_tables: string[] | null;
  model_used: string | null;
  timestamp: string;
}

export interface LLMProvider {
  id: string;
  name: string;
}

export interface SessionDetailResponse {
  session_id: string;
  connection_id: string;
  history: MessageResponse[];
  created_at: string;
  last_activity: string;
}

export interface MessageRequest {
  content: string;
  model_id?: string | null;
}

// ─────────────────────────────────────────────────────────────────────────────
// Axios instance
// ─────────────────────────────────────────────────────────────────────────────

let _authToken: string | null = null;

/** Set the JWT to be attached to all subsequent requests. */
export function setAuthToken(token: string | null): void {
  _authToken = token;
}

/** Return the current in-memory JWT. */
export function getAuthToken(): string | null {
  return _authToken;
}

const apiClient: AxiosInstance = axios.create({
  baseURL: "/",
  headers: { "Content-Type": "application/json" },
  timeout: 180000, // 3 min — LLM pipeline (3 model calls + chart + PDF) can take 90-120 s
});

// Attach JWT to every request
apiClient.interceptors.request.use((config) => {
  if (_authToken) {
    config.headers.Authorization = `Bearer ${_authToken}`;
  }
  return config;
});

// Clear token and redirect on 401
apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      setAuthToken(null);
      // TODO: redirect to /login via React Router or window.location
      window.location.href = "/login";
    }
    return Promise.reject(error);
  }
);

// ─────────────────────────────────────────────────────────────────────────────
// Auth API
// ─────────────────────────────────────────────────────────────────────────────

export const auth = {
  /**
   * Login with username and password (OAuth2 password flow).
   * Returns a JWT on success.
   */
  async login(username: string, password: string): Promise<TokenResponse> {
    // TODO: implement
    // OAuth2 password flow requires form-encoded body
    const params = new URLSearchParams({ username, password });
    const response: AxiosResponse<TokenResponse> = await apiClient.post(
      "/auth/token",
      params.toString(),
      { headers: { "Content-Type": "application/x-www-form-urlencoded" } }
    );
    return response.data;
  },

  /** Register a new user account. */
  async register(body: RegisterRequest): Promise<UserResponse> {
    // TODO: implement
    const response: AxiosResponse<UserResponse> = await apiClient.post(
      "/auth/register",
      body
    );
    return response.data;
  },

  /** Refresh the current JWT. Requires a valid Bearer token already set. */
  async refresh(): Promise<TokenResponse> {
    // TODO: implement
    const response: AxiosResponse<TokenResponse> = await apiClient.post(
      "/auth/refresh"
    );
    return response.data;
  },
};

// ─────────────────────────────────────────────────────────────────────────────
// RCA Copilot types (mirror backend/app/rca/models.py)
// ─────────────────────────────────────────────────────────────────────────────

export interface IncidentRequest {
  service: string;
  start: string; // ISO 8601, UTC
  end: string;
  symptom: string;
  connectors?: string[] | null;
}

export interface ConnectorHealth {
  name: string;
  source_type: string;
  healthy: boolean;
  latency_ms: number | null;
  detail: string;
}

export interface EvidenceRecord {
  id: string;
  timestamp: string;
  source: string;
  service: string | null;
  level: string | null;
  message: string;
  attributes: Record<string, unknown>;
}

export interface Hypothesis {
  rank: number;
  title: string;
  description: string;
  confidence: number;
  supporting_evidence_ids: string[];
}

export interface TimelineEvent {
  timestamp: string;
  source: string;
  kind: "change" | "error_spike_onset" | "metric_anomaly" | "new_error_signature";
  description: string;
  record_ids: string[];
  offset_seconds: number | null;
}

export interface ActivityBucket {
  timestamp: string;
  total: number;
  errors: number;
}

export interface CorrelationReport {
  baseline_window: { start: string; end: string };
  incident_window: { start: string; end: string };
  first_anomaly_at: string | null;
  timeline: TimelineEvent[];
  activity: Record<string, ActivityBucket[]>;
}

export interface EvidenceSummary {
  source: string;
  source_type: string;
  record_count: number;
  truncated: boolean;
  elapsed_ms: number;
  query_used: string;
  planned_by: "llm" | "fallback";
  plan_note: string;
}

export interface RCAResult {
  incident: IncidentRequest;
  hypotheses: Hypothesis[];
  correlation: CorrelationReport | null;
  evidence: EvidenceSummary[];
  cited_records: Record<string, EvidenceRecord>;
  source_failures: Record<string, string>;
  warnings: string[];
  timings_ms: Record<string, number>;
}

// ─────────────────────────────────────────────────────────────────────────────
// Chat API
// ─────────────────────────────────────────────────────────────────────────────

export const chat = {
  /** List connection_id values that can be passed to createSession(). */
  async listConnections(): Promise<string[]> {
    const response: AxiosResponse<string[]> = await apiClient.get(
      "/chat/connections"
    );
    return response.data;
  },

  /** List LLM providers available for the model picker. */
  async listProviders(): Promise<LLMProvider[]> {
    const response: AxiosResponse<LLMProvider[]> = await apiClient.get(
      "/chat/providers"
    );
    return response.data;
  },

  /** List model ids available from the active LLM provider (e.g. Ollama). */
  async listModels(): Promise<string[]> {
    const response: AxiosResponse<string[]> = await apiClient.get(
      "/chat/models"
    );
    return response.data;
  },

  /** Create a new conversation session for a given warehouse connection. */
  async createSession(body: CreateSessionRequest): Promise<SessionResponse> {
    // TODO: implement
    const response: AxiosResponse<SessionResponse> = await apiClient.post(
      "/chat/sessions",
      body
    );
    return response.data;
  },

  /** List all sessions for the authenticated user. */
  async listSessions(): Promise<SessionSummary[]> {
    // TODO: implement
    const response: AxiosResponse<SessionSummary[]> = await apiClient.get(
      "/chat/sessions"
    );
    return response.data;
  },

  /** Get the full conversation history for a session. */
  async getSession(sessionId: string): Promise<SessionDetailResponse> {
    // TODO: implement
    const response: AxiosResponse<SessionDetailResponse> = await apiClient.get(
      `/chat/sessions/${sessionId}`
    );
    return response.data;
  },

  /**
   * Send a natural-language message and get the assistant's response.
   * The response may include a download_token if a file was generated.
   */
  async sendMessage(
    sessionId: string,
    body: MessageRequest
  ): Promise<MessageResponse> {
    // TODO: implement
    const response: AxiosResponse<MessageResponse> = await apiClient.post(
      `/chat/sessions/${sessionId}/messages`,
      body
    );
    return response.data;
  },

  /** Delete a session and its associated Redis state. */
  async deleteSession(sessionId: string): Promise<void> {
    // TODO: implement
    await apiClient.delete(`/chat/sessions/${sessionId}`);
  },
};

// ─────────────────────────────────────────────────────────────────────────────
// RCA Copilot API
// ─────────────────────────────────────────────────────────────────────────────

export const rca = {
  /** Run a root cause analysis. Can take a while: it includes local LLM calls. */
  async analyze(body: IncidentRequest): Promise<RCAResult> {
    const response: AxiosResponse<RCAResult> = await apiClient.post("/rca/analyze", body, {
      timeout: 5 * 60 * 1000,
    });
    return response.data;
  },

  /** Health of every enabled evidence source. */
  async connectorsHealth(): Promise<ConnectorHealth[]> {
    const response: AxiosResponse<ConnectorHealth[]> = await apiClient.get("/rca/connectors/health");
    return response.data;
  },
};

// ─────────────────────────────────────────────────────────────────────────────
// Output download
// ─────────────────────────────────────────────────────────────────────────────

export const output = {
  /**
   * Return the download URL for a given download token.
   *
   * The URL is constructed client-side and hit directly by the browser for
   * streaming — the JWT is attached via the Authorization header on the
   * GET /output/{token} request.
   */
  getDownloadUrl(downloadToken: string): string {
    return `/output/${downloadToken}`;
  },

  /**
   * Fetch the file behind a download token and return a temporary object URL.
   * Caller is responsible for calling URL.revokeObjectURL() when done.
   */
  async fetchBlobUrl(downloadToken: string): Promise<string> {
    const response = await apiClient.get(`/output/${downloadToken}`, {
      responseType: "blob",
    });
    return window.URL.createObjectURL(new Blob([response.data]));
  },

  /**
   * Trigger a browser file download for the given token.
   *
   * Fetches the file as a Blob with the Authorization header, creates
   * a temporary object URL, and clicks an invisible anchor element.
   */
  async downloadFile(downloadToken: string, filename: string): Promise<void> {
    // TODO: implement
    const response = await apiClient.get(`/output/${downloadToken}`, {
      responseType: "blob",
    });
    const url = window.URL.createObjectURL(new Blob([response.data]));
    const link = document.createElement("a");
    link.href = url;
    link.setAttribute("download", filename);
    document.body.appendChild(link);
    link.click();
    link.parentNode?.removeChild(link);
    window.URL.revokeObjectURL(url);
  },
};

export { apiClient };
