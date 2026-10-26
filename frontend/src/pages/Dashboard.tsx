import React, { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { chat, LLMProvider, SessionSummary } from "../api/client";
import ChatPanel from "../components/ChatPanel";
import ModelPicker from "../components/ModelPicker";
import SessionHistory from "../components/SessionHistory";
import { useAuth } from "../App";

// Below this width the 220px sidebar no longer coexists comfortably with the
// chat column, so it collapses into an off-canvas drawer. Above it (tablet
// landscape and up) the sidebar is always visible per the design spec.
const MOBILE_BREAKPOINT = 768;

function useIsMobile(): boolean {
  const [mobile, setMobile] = useState(window.innerWidth < MOBILE_BREAKPOINT);
  useEffect(() => {
    const h = (): void => setMobile(window.innerWidth < MOBILE_BREAKPOINT);
    window.addEventListener("resize", h);
    return () => window.removeEventListener("resize", h);
  }, []);
  return mobile;
}

export default function Dashboard(): React.JSX.Element {
  const { logout } = useAuth();
  const navigate = useNavigate();
  const isMobile = useIsMobile();

  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [loadingSessions, setLoadingSessions] = useState(true);
  const [isQuerying, setIsQuerying] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);

  // ── LLM provider / model picker state ──────────────────────────────────────
  const [providers, setProviders] = useState<LLMProvider[]>([]);
  const [selectedProvider, setSelectedProvider] = useState<string | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [selectedModel, setSelectedModel] = useState<string | null>(null);
  const [modelsLoading, setModelsLoading] = useState(true);
  const [modelsError, setModelsError] = useState<string | null>(null);

  // Close sidebar on mobile by default
  useEffect(() => {
    if (isMobile) setSidebarOpen(false);
  }, [isMobile]);

  useEffect(() => {
    loadSessions();
    loadProviders();
    loadModels();
  }, []);

  async function loadProviders(): Promise<void> {
    try {
      const data = await chat.listProviders();
      setProviders(data);
      if (data.length > 0) setSelectedProvider((prev) => prev ?? data[0].id);
    } catch {
      /* provider list is cosmetic — model picker still works without it */
    }
  }

  async function loadModels(): Promise<void> {
    setModelsLoading(true);
    setModelsError(null);
    try {
      const data = await chat.listModels();
      setModels(data);
      setSelectedModel((prev) => (prev && data.includes(prev) ? prev : data[0] ?? null));
    } catch {
      setModelsError("Couldn't reach Ollama — check it's running.");
    } finally {
      setModelsLoading(false);
    }
  }

  async function loadSessions(): Promise<void> {
    try {
      const data = await chat.listSessions();
      setSessions(data);
      if (data.length > 0) setActiveSessionId(data[0].session_id);
    } catch {
      /* ignore */
    } finally {
      setLoadingSessions(false);
    }
  }

  const handleNewSession = useCallback(async (connectionId: string): Promise<void> => {
    const session = await chat.createSession({ connection_id: connectionId });
    setSessions((prev) => [
      { ...session, turn_count: 0, last_activity: session.created_at },
      ...prev,
    ]);
    setActiveSessionId(session.session_id);
    if (isMobile) setSidebarOpen(false);
  }, [isMobile]);

  const handleSelectSession = useCallback((sessionId: string): void => {
    setActiveSessionId(sessionId);
    if (isMobile) setSidebarOpen(false);
  }, [isMobile]);

  const handleDeleteSession = useCallback(async (sessionId: string): Promise<void> => {
    await chat.deleteSession(sessionId);
    setSessions((prev) => {
      const remaining = prev.filter((s) => s.session_id !== sessionId);
      setActiveSessionId((activePrev) =>
        activePrev === sessionId ? remaining[0]?.session_id ?? null : activePrev
      );
      return remaining;
    });
  }, []);

  const handleSessionExpired = useCallback(async (): Promise<void> => {
    // Session expired in Redis — refresh list (backend auto-closes expired ones)
    // then fall back to the first live session or null
    setActiveSessionId(null);
    try {
      const data = await chat.listSessions();
      setSessions(data);
      if (data.length > 0) setActiveSessionId(data[0].session_id);
    } catch {
      setSessions([]);
    }
  }, []);

  function handleLogout(): void {
    logout();
    navigate("/login", { replace: true });
  }

  const activeSession = sessions.find((s) => s.session_id === activeSessionId);

  return (
    <div style={s.root}>
      {/* ── Sidebar overlay on mobile ── */}
      {isMobile && sidebarOpen && (
        <div style={s.overlay} onClick={() => setSidebarOpen(false)} />
      )}

      {/* ── Sidebar — fixed 220px on tablet/desktop, off-canvas drawer on mobile ── */}
      <aside
        style={{
          ...s.sidebar,
          ...(isMobile
            ? {
                position: "fixed",
                top: 0, left: 0, bottom: 0, zIndex: 50,
                transform: sidebarOpen ? "translateX(0)" : "translateX(-100%)",
                transition: "transform 0.22s ease",
              }
            : {}),
        }}
      >
        <SessionHistory
          sessions={sessions}
          activeSessionId={activeSessionId}
          onSelectSession={handleSelectSession}
          onNewSession={handleNewSession}
          onDeleteSession={handleDeleteSession}
          loading={loadingSessions}
        />
        <button style={s.logoutBtn} onClick={handleLogout}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
            <path
              d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4M16 17l5-5-5-5M21 12H9"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
          Sign out
        </button>
      </aside>

      {/* ── Main ──
          No marginLeft here: on desktop the sidebar is a normal flex sibling
          (it already occupies its own width in the row), and on mobile it's
          position:fixed and overlays on top — in neither case does `main`
          need to additionally offset itself, or the sidebar's width gets
          double-counted as a dead gap between the two columns. */}
      <main style={s.main}>
        {/* Top bar */}
        <div style={s.topBar}>
          {/* Hamburger — mobile only; sidebar is always visible above the breakpoint */}
          {isMobile && (
            <button style={s.hamburger} onClick={() => setSidebarOpen((v) => !v)} title="Toggle sidebar">
              <svg width="19" height="19" viewBox="0 0 24 24" fill="none">
                <path d="M3 12h18M3 6h18M3 18h18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
              </svg>
            </button>
          )}

          {/* Active session indicator */}
          {activeSession && (
            <div style={s.sessionIndicator}>
              <div
                style={{
                  ...s.statusDot,
                  animation: isQuerying
                    ? "pulse-dot-query 1s ease-in-out infinite"
                    : "pulse-dot 3s ease-in-out infinite",
                  background: isQuerying ? "var(--color-warning)" : "var(--color-success)",
                }}
              />
              <span style={s.sessionName}>{activeSession.connection_id}</span>
            </div>
          )}

          {/* RCA Copilot entry point */}
          <button style={s.rcaLink} onClick={() => navigate("/rca")}>
            RCA Copilot
          </button>

          {/* MongoDB badge */}
          <div style={s.mongoBadge}>MongoDB</div>

          {/* Model picker */}
          <ModelPicker
            providers={providers}
            selectedProvider={selectedProvider}
            onProviderChange={setSelectedProvider}
            models={models}
            selectedModel={selectedModel}
            onModelChange={setSelectedModel}
            loading={modelsLoading}
            error={modelsError}
          />
        </div>

        {/* Chat panel */}
        {activeSessionId ? (
          <ChatPanel
            sessionId={activeSessionId}
            connectionId={activeSession?.connection_id ?? "default"}
            modelId={selectedModel}
            onQueryStart={() => setIsQuerying(true)}
            onQueryEnd={() => setIsQuerying(false)}
            onSessionExpired={handleSessionExpired}
          />
        ) : (
          <EmptyState />
        )}
      </main>
    </div>
  );
}

function EmptyState(): React.JSX.Element {
  return (
    <div style={s.emptyState}>
      <div style={s.emptyIcon}>
        <svg width="34" height="34" viewBox="0 0 24 24" fill="none">
          <path
            d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"
            stroke="var(--color-accent-text)"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </div>
      <h2 style={s.emptyTitle}>No session selected</h2>
      <p style={s.emptySub}>Open the sidebar to create or select a session.</p>
    </div>
  );
}

const s: Record<string, React.CSSProperties> = {
  root: { display: "flex", height: "100vh", background: "var(--color-bg-app)", overflow: "hidden" },

  overlay: {
    position: "fixed", inset: 0,
    background: "var(--color-bg-overlay)",
    zIndex: 40,
  },

  sidebar: {
    width: "var(--sidebar-width)", flexShrink: 0,
    display: "flex", flexDirection: "column",
    background: "var(--color-bg-sidebar)",
    borderRight: "var(--border-width) solid var(--color-border-sidebar)",
  },
  logoutBtn: {
    display: "flex", alignItems: "center", gap: "var(--space-2)",
    margin: "0 var(--space-2) var(--space-4)",
    padding: "8px var(--space-3)",
    background: "transparent",
    border: "var(--border-width) solid transparent",
    borderRadius: "var(--radius-sm)",
    color: "var(--color-text-faint)", fontSize: 14,
    cursor: "pointer", transition: "all 0.15s",
    fontFamily: "inherit",
    flexShrink: 0,
  },

  main: {
    flex: 1, display: "flex", flexDirection: "column",
    overflow: "hidden", background: "var(--color-bg-app)",
    minWidth: 0,
  },

  topBar: {
    display: "flex", alignItems: "center", gap: "var(--space-2)",
    height: "var(--topbar-height)", boxSizing: "border-box",
    padding: "0 var(--space-4)",
    borderBottom: "var(--border-width) solid var(--color-border-topbar)",
    background: "var(--color-bg-topbar)",
    flexShrink: 0,
    flexWrap: "wrap",
  },
  hamburger: {
    width: 34, height: 34,
    display: "flex", alignItems: "center", justifyContent: "center",
    background: "transparent",
    border: "var(--border-width) solid transparent",
    borderRadius: "var(--radius-sm)",
    color: "var(--color-text-muted)",
    cursor: "pointer",
    transition: "all 0.15s",
    flexShrink: 0,
  },
  sessionIndicator: {
    display: "flex", alignItems: "center", gap: 7,
  },
  statusDot: {
    width: 7, height: 7, borderRadius: "50%",
    flexShrink: 0,
  },
  sessionName: { fontSize: 14, fontWeight: 500, color: "var(--color-text-tertiary)" },
  rcaLink: {
    marginLeft: "auto",
    padding: "4px 12px",
    background: "var(--color-accent-bg)",
    border: "var(--border-width) solid var(--color-accent-border)",
    borderRadius: "var(--radius-sm)",
    fontSize: 13, fontWeight: 500, color: "var(--color-accent-text)",
    cursor: "pointer", fontFamily: "inherit",
  },
  mongoBadge: {
    padding: "2px 9px",
    background: "var(--color-engine-bg)",
    border: "var(--border-width) solid var(--color-engine-border)",
    borderRadius: "var(--radius-sm)",
    fontSize: 12, fontWeight: 500, color: "var(--color-engine-text)",
  },

  emptyState: {
    flex: 1,
    display: "flex", flexDirection: "column",
    alignItems: "center", justifyContent: "center",
    gap: 12, padding: 40,
  },
  emptyIcon: {
    width: 68, height: 68, borderRadius: "var(--radius-lg)",
    background: "linear-gradient(135deg, #1a0a2e, #0a1040)",
    border: "var(--border-width) solid var(--color-border)",
    display: "flex", alignItems: "center", justifyContent: "center",
    marginBottom: 8,
  },
  emptyTitle: { fontSize: 19, fontWeight: 600, color: "var(--color-text-secondary)", letterSpacing: "-0.3px" },
  emptySub: { fontSize: 14, color: "var(--color-text-faint)", textAlign: "center", maxWidth: 280 },
};
