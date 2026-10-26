import React, { FormEvent, useEffect, useRef, useState } from "react";
import { chat, SessionSummary } from "../api/client";
import { useTheme } from "../theme";

interface SessionHistoryProps {
  sessions: SessionSummary[];
  activeSessionId: string | null;
  onSelectSession: (sessionId: string) => void;
  onNewSession: (connectionId: string) => Promise<void>;
  onDeleteSession: (sessionId: string) => Promise<void>;
  loading: boolean;
}

export default function SessionHistory({
  sessions,
  activeSessionId,
  onSelectSession,
  onNewSession,
  onDeleteSession,
  loading,
}: SessionHistoryProps): React.JSX.Element {
  const [showForm, setShowForm] = useState(false);
  const [connectionId, setConnectionId] = useState("");
  const [connections, setConnections] = useState<string[]>([]);
  const [loadingConnections, setLoadingConnections] = useState(true);
  const [creating, setCreating] = useState(false);
  const { theme, toggleTheme } = useTheme();

  // Load the set of valid connection_ids once, so the picker can never
  // submit an id the backend's ConnectorRegistry would reject.
  useEffect(() => {
    let cancelled = false;
    chat
      .listConnections()
      .then((ids) => {
        if (cancelled) return;
        setConnections(ids);
        setConnectionId((prev) => (prev && ids.includes(prev) ? prev : ids[0] ?? ""));
      })
      .catch(() => {
        /* leave the list empty — form shows a disabled "no connections" state */
      })
      .finally(() => {
        if (!cancelled) setLoadingConnections(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleCreate(e: FormEvent<HTMLFormElement>): Promise<void> {
    e.preventDefault();
    if (!connectionId) return;
    setCreating(true);
    try {
      await onNewSession(connectionId);
      setShowForm(false);
    } finally {
      setCreating(false);
    }
  }

  return (
    <div style={s.root}>
      {/* ── Wordmark ── */}
      <div style={s.header}>
        <div style={s.logoMark}>
          <svg width="17" height="17" viewBox="0 0 24 24" fill="none">
            <path
              d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"
              stroke="var(--color-accent-text)"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </div>
        <span style={s.wordmark}>NL Data Insight</span>
        <button
          type="button"
          className="theme-toggle"
          style={s.themeToggle}
          onClick={toggleTheme}
          title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
        >
          {theme === "dark" ? (
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none">
              <circle cx="12" cy="12" r="4.5" stroke="currentColor" strokeWidth="2" />
              <path d="M12 2v2M12 20v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M2 12h2M20 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
            </svg>
          ) : (
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none">
              <path d="M21 12.8A9 9 0 1111.2 3 7 7 0 0021 12.8z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          )}
        </button>
      </div>

      {/* ── New session button ── */}
      <div style={s.newBtnArea}>
        <button
          style={s.newBtn}
          onClick={() => setShowForm((v) => !v)}
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none">
            <path d="M12 5v14M5 12h14" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          </svg>
          New Session
        </button>
      </div>

      {/* ── Inline create form ── */}
      {showForm && (
        <form onSubmit={handleCreate} style={s.form}>
          <label style={s.formLabel}>Data source</label>
          {loadingConnections ? (
            <div style={{ ...shimmer, width: "100%", height: 34, borderRadius: 7 }} />
          ) : connections.length === 0 ? (
            <div style={s.formEmptyNote}>No data sources configured on the backend.</div>
          ) : (
            <select
              value={connectionId}
              onChange={(e) => setConnectionId(e.target.value)}
              style={s.formInput}
              disabled={creating}
              autoFocus
            >
              {connections.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
          )}
          <div style={s.formActions}>
            <button
              type="button"
              style={s.cancelBtn}
              onClick={() => setShowForm(false)}
              disabled={creating}
            >
              Cancel
            </button>
            <button
              type="submit"
              style={s.createBtn}
              disabled={creating || !connectionId}
            >
              {creating ? "Creating…" : "Create"}
            </button>
          </div>
        </form>
      )}

      {/* ── Section label ── */}
      <div style={s.sectionLabel}>Recent</div>

      {/* ── Session list ── */}
      <div style={s.list}>
        {loading ? (
          [1, 2, 3].map((i) => <SkeletonItem key={i} />)
        ) : sessions.length === 0 ? (
          <EmptyList />
        ) : (
          sessions.map((session) => (
            <SessionItem
              key={session.session_id}
              session={session}
              isActive={session.session_id === activeSessionId}
              onClick={() => onSelectSession(session.session_id)}
              onDelete={onDeleteSession}
            />
          ))
        )}
      </div>
    </div>
  );
}

// ─── Session item ─────────────────────────────────────────────────────────────

function SessionItem({
  session,
  isActive,
  onClick,
  onDelete,
}: {
  session: SessionSummary;
  isActive: boolean;
  onClick: () => void;
  onDelete: (sessionId: string) => Promise<void>;
}): React.JSX.Element {
  const [hovered, setHovered] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menuOpen) return;
    function onDocClick(e: MouseEvent): void {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
        setConfirming(false);
      }
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [menuOpen]);

  async function handleDelete(): Promise<void> {
    setDeleting(true);
    try {
      await onDelete(session.session_id);
      // On success the session list re-renders without this item —
      // no need to reset local state, this component is about to unmount.
    } catch {
      setDeleting(false);
      setConfirming(false);
    }
  }

  return (
    <button
      className="session-item"
      onClick={onClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        ...s.item,
        ...(isActive ? s.itemActive : {}),
      }}
    >
      <div
        style={{
          ...s.itemIcon,
          background: isActive ? "var(--color-accent-border)" : "var(--color-bg-inset)",
          color: isActive ? "var(--color-accent-text)" : "var(--color-text-muted)",
        }}
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
          <path
            d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </div>
      <div style={s.itemBody}>
        <span style={{ ...s.itemTitle, color: isActive ? "var(--color-text-primary)" : "var(--color-text-tertiary)" }}>
          {truncate(session.connection_id, 24)}
        </span>
        <span style={s.itemMeta}>
          {session.turn_count} {session.turn_count === 1 ? "turn" : "turns"} ·{" "}
          {timeAgo(session.last_activity)}
        </span>
      </div>
      {(hovered || menuOpen) && (
        <div ref={menuRef} style={s.moreWrap} onClick={(e) => e.stopPropagation()}>
          <div
            style={{ ...s.moreIcon, ...(menuOpen ? s.moreIconOpen : {}) }}
            onClick={() => setMenuOpen((v) => !v)}
            title="More options"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
              <circle cx="12" cy="5" r="1.5" fill="currentColor" />
              <circle cx="12" cy="12" r="1.5" fill="currentColor" />
              <circle cx="12" cy="19" r="1.5" fill="currentColor" />
            </svg>
          </div>

          {menuOpen && (
            <div style={s.menuPanel}>
              {!confirming ? (
                <div
                  className="session-menu-row"
                  style={s.menuRow}
                  onClick={() => setConfirming(true)}
                >
                  <TrashIcon />
                  Delete session
                </div>
              ) : (
                <div style={s.confirmBox}>
                  <span style={s.confirmText}>Delete this session? This can't be undone.</span>
                  <div style={s.confirmActions}>
                    <div
                      className="session-menu-row"
                      style={s.confirmCancel}
                      onClick={() => setConfirming(false)}
                    >
                      Cancel
                    </div>
                    <div
                      className="session-menu-delete"
                      style={{ ...s.confirmDelete, ...(deleting ? { opacity: 0.6, pointerEvents: "none" } : {}) }}
                      onClick={handleDelete}
                    >
                      {deleting ? "Deleting…" : "Delete"}
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </button>
  );
}

function TrashIcon(): React.JSX.Element {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" style={{ flexShrink: 0 }}>
      <path
        d="M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6h14z"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function SkeletonItem(): React.JSX.Element {
  return (
    <div style={{ ...s.item, cursor: "default", pointerEvents: "none" }}>
      <div style={{ ...s.itemIcon, background: "var(--color-bg-inset)" }} />
      <div style={s.itemBody}>
        <div style={{ ...shimmer, width: "70%", height: 13, marginBottom: 6 }} />
        <div style={{ ...shimmer, width: "45%", height: 11 }} />
      </div>
    </div>
  );
}

function EmptyList(): React.JSX.Element {
  return (
    <div style={s.empty}>
      <svg width="28" height="28" viewBox="0 0 24 24" fill="none" style={{ opacity: 0.2 }}>
        <path
          d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"
          stroke="var(--color-text-tertiary)"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
      <span style={{ fontSize: 13, color: "var(--color-text-faint)" }}>No sessions yet</span>
    </div>
  );
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function timeAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff / 60_000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function truncate(str: string, n: number): string {
  return str.length > n ? str.slice(0, n) + "…" : str;
}

const shimmer: React.CSSProperties = {
  borderRadius: 4,
  background: "linear-gradient(90deg, var(--color-bg-inset) 25%, var(--color-border-strong) 50%, var(--color-bg-inset) 75%)",
  backgroundSize: "200% 100%",
  animation: "shimmer 1.6s ease-in-out infinite",
};

// ─── Styles ───────────────────────────────────────────────────────────────────

const s: Record<string, React.CSSProperties> = {
  root: {
    display: "flex", flexDirection: "column",
    height: "100%", width: "var(--sidebar-width)",
    background: "var(--color-bg-sidebar)",
    userSelect: "none",
  },

  // Header — same height as the chat top bar so the two rows line up and
  // read as one continuous strip instead of two mismatched boxes.
  header: {
    display: "flex", alignItems: "center", gap: "var(--space-2)",
    height: "var(--topbar-height)", boxSizing: "border-box",
    padding: "0 var(--space-4)",
    borderBottom: "var(--border-width) solid var(--color-border-sidebar)",
    flexShrink: 0,
  },
  themeToggle: {
    marginLeft: "auto", flexShrink: 0,
    width: 28, height: 28,
    display: "flex", alignItems: "center", justifyContent: "center",
    background: "transparent",
    border: "var(--border-width) solid var(--color-border)",
    borderRadius: "var(--radius-sm)",
    color: "var(--color-text-muted)",
    cursor: "pointer", transition: "all 0.15s",
  },
  logoMark: {
    width: 30, height: 30, borderRadius: "var(--radius-sm)", flexShrink: 0,
    background: "linear-gradient(135deg, #3b1575, #0f215e)",
    border: "var(--border-width) solid var(--color-accent-border)",
    display: "flex", alignItems: "center", justifyContent: "center",
  },
  wordmark: {
    fontSize: 14, fontWeight: 700,
    color: "var(--color-text-primary)", letterSpacing: "-0.2px",
    overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
  },

  // New session
  newBtnArea: { padding: "var(--space-3) var(--space-3) var(--space-1)", flexShrink: 0 },
  newBtn: {
    width: "100%", boxSizing: "border-box",
    display: "flex", alignItems: "center", justifyContent: "center", gap: 7,
    padding: "9px var(--space-3)",
    background: "var(--color-accent-bg)",
    border: "var(--border-width) solid var(--color-accent-border)",
    borderRadius: "var(--radius-md)",
    color: "var(--color-accent-text)",
    fontSize: 14, fontWeight: 500,
    cursor: "pointer",
    transition: "all 0.15s",
    fontFamily: "inherit",
  },

  // Create form
  form: {
    margin: "0 var(--space-3) var(--space-1)",
    padding: "var(--space-3)",
    background: "var(--color-bg-surface)",
    border: "var(--border-width) solid var(--color-border)",
    borderRadius: "var(--radius-md)",
    display: "flex", flexDirection: "column", gap: "var(--space-2)",
    flexShrink: 0,
  },
  formLabel: {
    fontSize: 11, fontWeight: 600, color: "var(--color-text-muted)",
    textTransform: "uppercase", letterSpacing: "0.7px",
  },
  formInput: {
    padding: "8px 10px",
    background: "var(--color-bg-app)",
    border: "var(--border-width) solid var(--color-border)",
    borderRadius: "var(--radius-sm)",
    color: "var(--color-text-primary)", fontSize: 14, outline: "none",
    fontFamily: "inherit",
    width: "100%", boxSizing: "border-box",
  },
  formEmptyNote: {
    padding: "8px 10px",
    fontSize: 13, color: "var(--color-text-muted)", fontStyle: "italic",
  },
  formActions: { display: "flex", gap: 6 },
  cancelBtn: {
    flex: 1, padding: "7px 0",
    background: "transparent",
    border: "var(--border-width) solid var(--color-border)",
    borderRadius: "var(--radius-sm)", color: "var(--color-text-muted)", fontSize: 13,
    cursor: "pointer", fontFamily: "inherit",
  },
  createBtn: {
    flex: 1, padding: "7px 0",
    background: "var(--color-accent-gradient)",
    border: "none", borderRadius: "var(--radius-sm)",
    color: "#fff", fontSize: 13, fontWeight: 600,
    cursor: "pointer", fontFamily: "inherit",
  },

  // Section label
  sectionLabel: {
    padding: "var(--space-4) var(--space-4) 5px",
    fontSize: 11, fontWeight: 600, color: "var(--color-text-faint)",
    textTransform: "uppercase", letterSpacing: "1px",
    flexShrink: 0,
  },

  // List
  list: { flex: 1, overflowY: "auto", padding: "0 var(--space-2) var(--space-3)" },
  empty: {
    display: "flex", flexDirection: "column", alignItems: "center", gap: 10,
    padding: "40px 20px",
  },

  // Session item — fixed height so every card matches regardless of content
  item: {
    width: "100%", boxSizing: "border-box",
    height: 56,
    display: "flex", alignItems: "center", gap: "var(--space-2)",
    padding: "0 var(--space-2)",
    background: "transparent",
    border: "var(--border-width) solid transparent",
    borderRadius: "var(--radius-md)",
    cursor: "pointer",
    textAlign: "left",
    transition: "all 0.12s",
    marginBottom: 2,
    fontFamily: "inherit",
    position: "relative",
  },
  // Active state is a full, visible border — not just a background tint —
  // per the design spec ("highlighted with a border not just background color").
  itemActive: {
    background: "var(--color-accent-bg)",
    border: "var(--border-width) solid var(--color-accent)",
  },
  itemIcon: {
    width: 32, height: 32, borderRadius: "var(--radius-sm)", flexShrink: 0,
    display: "flex", alignItems: "center", justifyContent: "center",
    transition: "all 0.12s",
  },
  itemBody: { flex: 1, display: "flex", flexDirection: "column", gap: 3, minWidth: 0 },
  itemTitle: {
    fontSize: 14, fontWeight: 500,
    overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
    transition: "color 0.12s",
  },
  itemMeta: { fontSize: 12, color: "var(--color-text-faint)" },
  moreWrap: { position: "relative", flexShrink: 0 },
  moreIcon: {
    flexShrink: 0, width: 24, height: 24, borderRadius: "var(--radius-sm)",
    display: "flex", alignItems: "center", justifyContent: "center",
    color: "var(--color-text-muted)",
    background: "var(--color-bg-hover)",
    transition: "all 0.12s",
  },
  moreIconOpen: {
    background: "var(--color-accent-border)",
    color: "var(--color-accent-text)",
  },
  menuPanel: {
    position: "absolute", top: "calc(100% + 4px)", right: 0,
    minWidth: 190,
    background: "var(--color-bg-panel)",
    border: "var(--border-width) solid var(--color-border-strong)",
    borderRadius: "var(--radius-md)",
    boxShadow: "0 12px 28px rgba(0,0,0,0.5)",
    padding: 5,
    zIndex: 60,
  },
  menuRow: {
    display: "flex", alignItems: "center", gap: 8,
    padding: "8px 9px",
    borderRadius: "var(--radius-sm)",
    color: "#e08a8a", fontSize: 14.5,
    cursor: "pointer",
  },
  confirmBox: { padding: "6px 8px 4px" },
  confirmText: {
    display: "block", fontSize: 13.5, color: "var(--color-text-secondary)",
    lineHeight: 1.5, marginBottom: 8,
  },
  confirmActions: { display: "flex", gap: 6 },
  confirmCancel: {
    flex: 1, textAlign: "center",
    padding: "6px 0", borderRadius: "var(--radius-sm)",
    color: "var(--color-text-tertiary)", fontSize: 13,
    cursor: "pointer",
    border: "var(--border-width) solid var(--color-border-strong)",
  },
  confirmDelete: {
    flex: 1, textAlign: "center",
    padding: "6px 0", borderRadius: "var(--radius-sm)",
    background: "var(--color-danger-bg-strong)",
    color: "var(--color-danger)", fontSize: 13, fontWeight: 600,
    cursor: "pointer",
    border: "var(--border-width) solid var(--color-danger-border-strong)",
  },
};
