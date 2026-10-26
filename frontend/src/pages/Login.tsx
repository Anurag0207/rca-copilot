import React, { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";
import { auth } from "../api/client";
import { useAuth } from "../App";

export default function Login(): React.JSX.Element {
  const { login } = useAuth();
  const navigate = useNavigate();

  const [tab, setTab] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: FormEvent<HTMLFormElement>): Promise<void> {
    e.preventDefault();
    setError(null);
    if (!username.trim() || !password.trim()) { setError("All fields are required."); return; }
    setLoading(true);
    try {
      if (tab === "register") {
        if (!email.trim()) { setError("Email is required."); setLoading(false); return; }
        await auth.register({ username, email, password });
      }
      const tokenResponse = await auth.login(username, password);
      login(tokenResponse.access_token);
      navigate("/", { replace: true });
    } catch {
      setError(tab === "register" ? "Registration failed. Username or email may already exist." : "Invalid username or password.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div style={s.root}>
      {/* Background grid */}
      <div style={s.grid} />

      <div style={s.card}>
        {/* Logo */}
        <div style={s.logoRow}>
          <div style={s.logoIcon}>
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
              <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5" stroke="#a78bfa" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </div>
          <span style={s.logoText}>NL Data Insight</span>
        </div>

        <h1 style={s.heading}>
          {tab === "login" ? "Welcome back" : "Create account"}
        </h1>
        <p style={s.subheading}>
          {tab === "login" ? "Query your warehouse in plain English" : "Start querying your data with AI"}
        </p>

        {/* Tab switcher */}
        <div style={s.tabs}>
          <button style={{ ...s.tab, ...(tab === "login" ? s.tabActive : {}) }} onClick={() => { setTab("login"); setError(null); }}>Sign in</button>
          <button style={{ ...s.tab, ...(tab === "register" ? s.tabActive : {}) }} onClick={() => { setTab("register"); setError(null); }}>Register</button>
        </div>

        <form onSubmit={handleSubmit} style={s.form}>
          <div style={s.field}>
            <label style={s.label}>Username</label>
            <input style={s.input} type="text" value={username} onChange={e => setUsername(e.target.value)} placeholder="your-username" disabled={loading} autoComplete="username" />
          </div>

          {tab === "register" && (
            <div style={s.field}>
              <label style={s.label}>Email</label>
              <input style={s.input} type="email" value={email} onChange={e => setEmail(e.target.value)} placeholder="you@example.com" disabled={loading} />
            </div>
          )}

          <div style={s.field}>
            <label style={s.label}>Password</label>
            <input style={s.input} type="password" value={password} onChange={e => setPassword(e.target.value)} placeholder="••••••••" disabled={loading} autoComplete={tab === "login" ? "current-password" : "new-password"} />
          </div>

          {error && (
            <div style={s.errorBox}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" style={{ flexShrink: 0 }}>
                <circle cx="12" cy="12" r="10" stroke="#f87171" strokeWidth="2"/>
                <path d="M12 8v4M12 16h.01" stroke="#f87171" strokeWidth="2" strokeLinecap="round"/>
              </svg>
              {error}
            </div>
          )}

          <button type="submit" style={{ ...s.btn, ...(loading ? s.btnDisabled : {}) }} disabled={loading}>
            {loading ? (
              <span style={s.spinner} />
            ) : (
              tab === "login" ? "Sign in" : "Create account"
            )}
          </button>
        </form>
      </div>
    </div>
  );
}

const s: Record<string, React.CSSProperties> = {
  root: {
    minHeight: "100vh",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    background: "radial-gradient(ellipse 80% 60% at 50% -10%, #1e1030 0%, #0a0a0f 60%)",
    position: "relative",
    overflow: "hidden",
  },
  grid: {
    position: "absolute",
    inset: 0,
    backgroundImage: "linear-gradient(rgba(124,58,237,0.05) 1px, transparent 1px), linear-gradient(90deg, rgba(124,58,237,0.05) 1px, transparent 1px)",
    backgroundSize: "40px 40px",
    pointerEvents: "none",
  },
  card: {
    position: "relative",
    width: 400,
    padding: "36px 40px",
    background: "rgba(17,17,24,0.9)",
    border: "1px solid #2d2d3e",
    borderRadius: 16,
    backdropFilter: "blur(20px)",
    boxShadow: "0 24px 64px rgba(0,0,0,0.5), 0 0 0 1px rgba(124,58,237,0.08)",
  },
  logoRow: { display: "flex", alignItems: "center", gap: 10, marginBottom: 28 },
  logoIcon: {
    width: 36, height: 36, borderRadius: 10,
    background: "linear-gradient(135deg, #4c1d95, #1e40af)",
    display: "flex", alignItems: "center", justifyContent: "center",
    boxShadow: "0 4px 12px rgba(124,58,237,0.3)",
  },
  logoText: { fontSize: 16, fontWeight: 700, color: "#e8e8f0", letterSpacing: "-0.3px" },
  heading: { fontSize: 22, fontWeight: 700, color: "#f1f1f8", letterSpacing: "-0.5px", marginBottom: 6 },
  subheading: { fontSize: 13, color: "#6b6b82", marginBottom: 24 },
  tabs: {
    display: "flex",
    background: "#0d0d14",
    borderRadius: 8,
    padding: 3,
    marginBottom: 24,
    border: "1px solid #1e1e2e",
  },
  tab: {
    flex: 1, padding: "7px 0", border: "none", background: "transparent",
    color: "#6b6b82", fontSize: 13, fontWeight: 500, cursor: "pointer",
    borderRadius: 6, transition: "all 0.15s",
  },
  tabActive: {
    background: "#1e1e2e", color: "#e8e8f0",
    boxShadow: "0 1px 4px rgba(0,0,0,0.3)",
  },
  form: { display: "flex", flexDirection: "column", gap: 16 },
  field: { display: "flex", flexDirection: "column", gap: 6 },
  label: { fontSize: 12, fontWeight: 500, color: "#9090a8", textTransform: "uppercase", letterSpacing: "0.5px" },
  input: {
    padding: "10px 14px",
    background: "#0d0d14",
    border: "1px solid #2d2d3e",
    borderRadius: 8,
    color: "#e8e8f0",
    fontSize: 14,
    outline: "none",
    transition: "border-color 0.15s",
  },
  errorBox: {
    display: "flex", alignItems: "center", gap: 8,
    padding: "10px 12px",
    background: "rgba(248,113,113,0.08)",
    border: "1px solid rgba(248,113,113,0.2)",
    borderRadius: 8,
    color: "#f87171",
    fontSize: 13,
  },
  btn: {
    marginTop: 4,
    padding: "12px 0",
    background: "linear-gradient(135deg, #7c3aed, #4f46e5)",
    color: "#fff",
    border: "none",
    borderRadius: 8,
    fontSize: 14,
    fontWeight: 600,
    cursor: "pointer",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    boxShadow: "0 4px 16px rgba(124,58,237,0.35)",
    transition: "opacity 0.15s",
    height: 44,
  },
  btnDisabled: { opacity: 0.6, cursor: "not-allowed" },
  spinner: {
    width: 18, height: 18,
    border: "2px solid rgba(255,255,255,0.2)",
    borderTopColor: "#fff",
    borderRadius: "50%",
    animation: "spin 0.7s linear infinite",
    display: "inline-block",
  },
};
