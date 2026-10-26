/**
 * RCA Copilot page: incident form, connector health strip, cross-source
 * timeline and ranked root causes with expandable evidence.
 *
 * All times are treated as UTC end to end (form inputs, API payload, charts).
 */

import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  ActivityBucket,
  ConnectorHealth,
  EvidenceRecord,
  Hypothesis,
  rca,
  RCAResult,
  TimelineEvent,
} from "../api/client";

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────

const EVENT_COLORS: Record<TimelineEvent["kind"], string> = {
  change: "var(--color-accent-text)",
  error_spike_onset: "var(--color-danger)",
  metric_anomaly: "var(--color-warning)",
  new_error_signature: "var(--color-danger)",
};

const EVENT_LABELS: Record<TimelineEvent["kind"], string> = {
  change: "Change",
  error_spike_onset: "Error spike",
  metric_anomaly: "Metric anomaly",
  new_error_signature: "New error",
};

/** "2026-01-15T12:00" (UTC wall time from the form) -> ISO string with Z. */
function formToIso(value: string): string {
  return `${value}:00Z`;
}

/** ISO string -> "YYYY-MM-DDTHH:mm" for datetime-local inputs, in UTC. */
function isoToForm(iso: string): string {
  return new Date(iso).toISOString().slice(0, 16);
}

function clock(iso: string): string {
  return new Date(iso).toISOString().slice(11, 19);
}

function offsetLabel(seconds: number | null): string {
  if (seconds === null) return "";
  const sign = seconds < 0 ? "-" : "+";
  const abs = Math.abs(Math.round(seconds));
  return `${sign}${Math.floor(abs / 60)}m${String(abs % 60).padStart(2, "0")}s`;
}

function defaultWindow(): { start: string; end: string } {
  const end = new Date();
  end.setUTCSeconds(0, 0);
  const start = new Date(end.getTime() - 20 * 60_000);
  return { start: isoToForm(start.toISOString()), end: isoToForm(end.toISOString()) };
}

// ─────────────────────────────────────────────────────────────────────────────
// Page
// ─────────────────────────────────────────────────────────────────────────────

export default function Rca(): React.JSX.Element {
  const navigate = useNavigate();
  const initialWindow = useMemo(defaultWindow, []);

  const [service, setService] = useState("checkout-api");
  const [start, setStart] = useState(initialWindow.start);
  const [end, setEnd] = useState(initialWindow.end);
  const [symptom, setSymptom] = useState("");
  const [pasted, setPasted] = useState("");

  const [health, setHealth] = useState<ConnectorHealth[] | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<RCAResult | null>(null);

  const loadHealth = useCallback(async (): Promise<void> => {
    setHealthError(null);
    try {
      setHealth(await rca.connectorsHealth());
    } catch {
      setHealth(null);
      setHealthError("Couldn't load connector health.");
    }
  }, []);

  useEffect(() => {
    void loadHealth();
  }, [loadHealth]);

  /** Fill the form from the JSON printed by scripts/seed_incident.py. */
  function applyPastedIncident(): void {
    try {
      const parsed = JSON.parse(pasted) as { service: string; start: string; end: string; symptom?: string };
      setService(parsed.service);
      setStart(isoToForm(parsed.start));
      setEnd(isoToForm(parsed.end));
      setSymptom(parsed.symptom ?? "");
      setPasted("");
      setError(null);
    } catch {
      setError("That isn't valid incident JSON.");
    }
  }

  async function handleAnalyze(e: React.FormEvent): Promise<void> {
    e.preventDefault();
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      setResult(
        await rca.analyze({ service: service.trim(), start: formToIso(start), end: formToIso(end), symptom })
      );
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: unknown } } }).response?.data?.detail;
      setError(typeof detail === "string" ? detail : "Analysis failed. Check the backend logs.");
    } finally {
      setRunning(false);
    }
  }

  return (
    <div style={s.root}>
      <div style={s.topBar}>
        <button style={s.backBtn} onClick={() => navigate("/")}>
          ← Chat
        </button>
        <h1 style={s.title}>RCA Copilot</h1>
        <HealthStrip health={health} error={healthError} onRefresh={loadHealth} />
      </div>

      <div style={s.content}>
        <form style={s.card} onSubmit={handleAnalyze}>
          <div style={s.formGrid}>
            <label style={s.field}>
              <span style={s.label}>Service</span>
              <input style={s.input} value={service} onChange={(e) => setService(e.target.value)} required />
            </label>
            <label style={s.field}>
              <span style={s.label}>Start (UTC)</span>
              <input style={s.input} type="datetime-local" value={start} onChange={(e) => setStart(e.target.value)} required />
            </label>
            <label style={s.field}>
              <span style={s.label}>End (UTC)</span>
              <input style={s.input} type="datetime-local" value={end} onChange={(e) => setEnd(e.target.value)} required />
            </label>
          </div>
          <label style={s.field}>
            <span style={s.label}>Symptom</span>
            <textarea
              style={{ ...s.input, minHeight: 56, resize: "vertical" }}
              value={symptom}
              onChange={(e) => setSymptom(e.target.value)}
              placeholder="e.g. checkout-api is returning HTTP 500/503 errors to customers"
            />
          </label>
          <div style={s.formActions}>
            <input
              style={{ ...s.input, flex: 1, minWidth: 180 }}
              value={pasted}
              onChange={(e) => setPasted(e.target.value)}
              placeholder="Or paste the incident JSON printed by scripts/seed_incident.py"
            />
            <button type="button" style={s.secondaryBtn} onClick={applyPastedIncident} disabled={!pasted.trim()}>
              Load
            </button>
            <button type="submit" style={{ ...s.primaryBtn, opacity: running ? 0.6 : 1 }} disabled={running}>
              {running ? "Analysing…" : "Analyse incident"}
            </button>
          </div>
          {running && (
            <p style={s.hint}>Querying sources in parallel, then ranking with the local model. This can take a minute on CPU.</p>
          )}
          {error && <p style={s.errorText}>{error}</p>}
        </form>

        {result && <Results result={result} />}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Connector health strip
// ─────────────────────────────────────────────────────────────────────────────

function HealthStrip(props: {
  health: ConnectorHealth[] | null;
  error: string | null;
  onRefresh: () => void;
}): React.JSX.Element {
  return (
    <div style={s.healthStrip}>
      {props.error && <span style={s.errorText}>{props.error}</span>}
      {props.health?.length === 0 && <span style={s.hint}>No connectors enabled (RCA_CONNECTORS)</span>}
      {props.health?.map((h) => (
        <span key={h.name} style={s.pill} title={h.detail || `${h.source_type}`}>
          <span style={{ ...s.dot, background: h.healthy ? "var(--color-success)" : "var(--color-danger)" }} />
          {h.name}
          <span style={s.pillMeta}>
            {h.source_type}
            {h.healthy && h.latency_ms !== null ? ` · ${h.latency_ms}ms` : ""}
          </span>
        </span>
      ))}
      <button style={s.iconBtn} onClick={props.onRefresh} title="Refresh health">
        ↻
      </button>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Results
// ─────────────────────────────────────────────────────────────────────────────

function Results({ result }: { result: RCAResult }): React.JSX.Element {
  const failures = Object.entries(result.source_failures);
  return (
    <>
      {(result.warnings.length > 0 || failures.length > 0) && (
        <div style={s.warnBox}>
          {failures.map(([name, why]) => (
            <div key={name}>
              <strong>{name}</strong> failed: {why}
            </div>
          ))}
          {result.warnings.map((w, i) => (
            <div key={i}>{w}</div>
          ))}
        </div>
      )}

      {result.correlation && (
        <section style={s.card}>
          <h2 style={s.h2}>Cross-source timeline</h2>
          <Timeline result={result} />
        </section>
      )}

      <section style={s.card}>
        <h2 style={s.h2}>Ranked root causes</h2>
        {result.hypotheses.length === 0 ? (
          <p style={s.hint}>No evidence-backed hypotheses were produced for this incident.</p>
        ) : (
          result.hypotheses.map((h) => <HypothesisCard key={h.rank} hypothesis={h} records={result.cited_records} />)
        )}
        <p style={s.hint}>
          Confidence is reported by the language model and is not a calibrated probability. Timings:{" "}
          {Object.entries(result.timings_ms)
            .map(([k, v]) => `${k} ${(v / 1000).toFixed(1)}s`)
            .join(" · ")}
        </p>
      </section>

      <section style={s.card}>
        <h2 style={s.h2}>Sources queried</h2>
        {result.evidence.map((e) => (
          <details key={e.source} style={s.details}>
            <summary style={s.summary}>
              <strong>{e.source}</strong> ({e.source_type}) · {e.record_count} records · {e.elapsed_ms}ms · query by{" "}
              {e.planned_by}
              {e.truncated ? " · truncated" : ""}
            </summary>
            {e.plan_note && <p style={s.hint}>{e.plan_note}</p>}
            <pre style={s.pre}>{e.query_used}</pre>
          </details>
        ))}
      </section>
    </>
  );
}

function HypothesisCard(props: { hypothesis: Hypothesis; records: Record<string, EvidenceRecord> }): React.JSX.Element {
  const { hypothesis: h, records } = props;
  const [open, setOpen] = useState(h.rank === 1);
  return (
    <div style={s.hypothesis}>
      <button style={s.hypHeader} onClick={() => setOpen((v) => !v)}>
        <span style={s.rank}>#{h.rank}</span>
        <span style={{ flex: 1, textAlign: "left" }}>{h.title}</span>
        <span style={s.confTrack} title={`model-reported confidence ${(h.confidence * 100).toFixed(0)}%`}>
          <span style={{ ...s.confFill, width: `${h.confidence * 100}%` }} />
        </span>
        <span style={s.confText}>{(h.confidence * 100).toFixed(0)}%</span>
        <span>{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div style={s.hypBody}>
          <p style={s.desc}>{h.description}</p>
          <div style={s.label}>Supporting evidence ({h.supporting_evidence_ids.length})</div>
          {h.supporting_evidence_ids.map((id) => {
            const r = records[id];
            return (
              <div key={id} style={s.evidenceRow}>
                <code style={s.evId}>{id}</code>
                {r ? (
                  <>
                    <span style={s.evTime}>{clock(r.timestamp)}</span>
                    <span style={{ ...s.evLevel, color: r.level === "error" ? "var(--color-danger)" : "var(--color-text-muted)" }}>
                      {r.level ?? "—"}
                    </span>
                    <span style={s.evMsg}>{r.message}</span>
                  </>
                ) : (
                  <span style={s.hint}>record not returned</span>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Timeline: one lane per source, activity bars + event markers
// ─────────────────────────────────────────────────────────────────────────────

const LANE_H = 46;
const LABEL_W = 110;
const CHART_W = 860;

function Timeline({ result }: { result: RCAResult }): React.JSX.Element {
  const report = result.correlation!;
  const t0 = new Date(report.baseline_window.start).getTime();
  const t1 = new Date(report.incident_window.end).getTime();
  const incidentStart = new Date(report.incident_window.start).getTime();
  const x = (iso: string | number): number =>
    LABEL_W + ((new Date(iso).getTime() - t0) / (t1 - t0)) * (CHART_W - LABEL_W);

  const lanes = result.evidence.map((e) => e.source);
  const height = lanes.length * LANE_H + 24;
  const barW = Math.max(((CHART_W - LABEL_W) * 60_000) / (t1 - t0), 1.5);

  return (
    <>
      <div style={{ overflowX: "auto" }}>
        <svg width={CHART_W} height={height} role="img" aria-label="Cross-source timeline">
          {lanes.map((lane, i) => {
            const y = i * LANE_H;
            const buckets: ActivityBucket[] = report.activity[lane] ?? [];
            const maxTotal = Math.max(...buckets.map((b) => b.total), 1);
            return (
              <g key={lane}>
                <rect x={LABEL_W} y={y + 4} width={CHART_W - LABEL_W} height={LANE_H - 8} fill="var(--color-bg-inset)" rx={4} />
                <text x={4} y={y + LANE_H / 2 + 4} fontSize={12} fill="var(--color-text-tertiary)">
                  {lane}
                </text>
                {buckets.map((b) => {
                  const h = (b.total / maxTotal) * (LANE_H - 16);
                  const eh = (b.errors / maxTotal) * (LANE_H - 16);
                  return (
                    <g key={b.timestamp}>
                      <rect x={x(b.timestamp)} y={y + LANE_H - 8 - h} width={barW} height={h} fill="var(--color-border-strong)" />
                      <rect x={x(b.timestamp)} y={y + LANE_H - 8 - eh} width={barW} height={eh} fill="var(--color-danger)" />
                    </g>
                  );
                })}
                {report.timeline
                  .filter((ev) => ev.source === lane)
                  .map((ev, j) => (
                    <g key={j}>
                      <title>{`${clock(ev.timestamp)} ${EVENT_LABELS[ev.kind]}: ${ev.description}`}</title>
                      <line x1={x(ev.timestamp)} x2={x(ev.timestamp)} y1={y + 4} y2={y + LANE_H - 4} stroke={EVENT_COLORS[ev.kind]} strokeWidth={1.5} />
                      <circle cx={x(ev.timestamp)} cy={y + 10} r={4.5} fill={EVENT_COLORS[ev.kind]} />
                    </g>
                  ))}
              </g>
            );
          })}
          <line x1={x(incidentStart)} x2={x(incidentStart)} y1={0} y2={height - 20} stroke="var(--color-text-muted)" strokeDasharray="4 3" />
          <text x={x(incidentStart) + 4} y={height - 6} fontSize={11} fill="var(--color-text-muted)">
            incident start {clock(report.incident_window.start)}
          </text>
        </svg>
      </div>

      <div style={{ marginTop: "var(--space-3)" }}>
        {report.timeline.map((ev, i) => (
          <div key={i} style={s.eventRow}>
            <span style={{ ...s.dot, background: EVENT_COLORS[ev.kind] }} />
            <span style={s.evTime}>{clock(ev.timestamp)}</span>
            <span style={s.offset}>{offsetLabel(ev.offset_seconds)}</span>
            <span style={s.evSource}>{ev.source}</span>
            <span style={s.evMsg}>
              <strong>{EVENT_LABELS[ev.kind]}:</strong> {ev.description}
            </span>
          </div>
        ))}
        <p style={s.hint}>Offsets are relative to the first detected anomaly. Red bars are error counts per minute.</p>
      </div>
    </>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Styles (design tokens from styles/tokens.css)
// ─────────────────────────────────────────────────────────────────────────────

const s: Record<string, React.CSSProperties> = {
  root: { display: "flex", flexDirection: "column", height: "100vh", background: "var(--color-bg-app)", overflow: "hidden" },
  topBar: {
    display: "flex", alignItems: "center", gap: "var(--space-3)", flexWrap: "wrap",
    minHeight: "var(--topbar-height)", boxSizing: "border-box", padding: "0 var(--space-4)",
    borderBottom: "var(--border-width) solid var(--color-border-topbar)",
    background: "var(--color-bg-topbar)", flexShrink: 0,
  },
  title: { fontSize: 18, fontWeight: 600, color: "var(--color-text-secondary)", margin: 0 },
  backBtn: {
    background: "transparent", border: "var(--border-width) solid var(--color-border)",
    borderRadius: "var(--radius-sm)", color: "var(--color-text-tertiary)",
    padding: "5px 12px", cursor: "pointer", fontFamily: "inherit", fontSize: 13,
  },
  content: { flex: 1, overflowY: "auto", padding: "var(--space-5)", display: "flex", flexDirection: "column", gap: "var(--space-4)" },

  healthStrip: { marginLeft: "auto", display: "flex", alignItems: "center", gap: "var(--space-2)", flexWrap: "wrap" },
  pill: {
    display: "inline-flex", alignItems: "center", gap: 7, padding: "3px 10px",
    background: "var(--color-bg-surface)", border: "var(--border-width) solid var(--color-border)",
    borderRadius: "var(--radius-pill)", fontSize: 12, color: "var(--color-text-secondary)",
  },
  pillMeta: { color: "var(--color-text-muted)" },
  dot: { width: 8, height: 8, borderRadius: "50%", flexShrink: 0, display: "inline-block" },
  iconBtn: { background: "transparent", border: "none", color: "var(--color-text-muted)", cursor: "pointer", fontSize: 16 },

  card: {
    background: "var(--color-bg-surface)", border: "var(--border-width) solid var(--color-border)",
    borderRadius: "var(--radius-md)", padding: "var(--space-4)",
    display: "flex", flexDirection: "column", gap: "var(--space-3)",
  },
  h2: { margin: 0, fontSize: 15, fontWeight: 600, color: "var(--color-text-secondary)" },
  formGrid: { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))", gap: "var(--space-3)" },
  field: { display: "flex", flexDirection: "column", gap: 5 },
  label: { fontSize: 12, color: "var(--color-text-muted)" },
  input: {
    background: "var(--color-bg-inset)", border: "var(--border-width) solid var(--color-border)",
    borderRadius: "var(--radius-sm)", color: "var(--color-text-primary)",
    padding: "8px 10px", fontSize: 14, fontFamily: "inherit", boxSizing: "border-box",
  },
  formActions: { display: "flex", gap: "var(--space-2)", alignItems: "center", flexWrap: "wrap" },
  primaryBtn: {
    background: "var(--color-accent-gradient)", color: "var(--color-on-accent)", border: "none",
    borderRadius: "var(--radius-sm)", padding: "9px 18px", fontSize: 14, fontWeight: 500,
    cursor: "pointer", fontFamily: "inherit",
  },
  secondaryBtn: {
    background: "transparent", color: "var(--color-text-tertiary)",
    border: "var(--border-width) solid var(--color-border)", borderRadius: "var(--radius-sm)",
    padding: "8px 14px", fontSize: 13, cursor: "pointer", fontFamily: "inherit",
  },
  hint: { margin: 0, fontSize: 12, color: "var(--color-text-faint)" },
  errorText: { margin: 0, fontSize: 13, color: "var(--color-danger)" },
  warnBox: {
    background: "var(--color-warning-bg)", border: "var(--border-width) solid var(--color-warning-border)",
    borderRadius: "var(--radius-sm)", padding: "var(--space-3)", fontSize: 13,
    color: "var(--color-warning)", display: "flex", flexDirection: "column", gap: 4,
  },

  hypothesis: { border: "var(--border-width) solid var(--color-border)", borderRadius: "var(--radius-sm)", overflow: "hidden" },
  hypHeader: {
    width: "100%", display: "flex", alignItems: "center", gap: "var(--space-2)",
    padding: "10px 14px", background: "var(--color-bg-surface-2)", border: "none",
    color: "var(--color-text-primary)", fontSize: 14, fontFamily: "inherit", cursor: "pointer",
  },
  rank: { color: "var(--color-accent-text)", fontWeight: 600 },
  confTrack: { width: 90, height: 6, borderRadius: 3, background: "var(--color-bg-inset)", overflow: "hidden" },
  confFill: { display: "block", height: "100%", background: "var(--color-accent)" },
  confText: { width: 38, textAlign: "right", fontSize: 12, color: "var(--color-text-tertiary)" },
  hypBody: { padding: "var(--space-3) var(--space-4)", display: "flex", flexDirection: "column", gap: 8 },
  desc: { margin: 0, fontSize: 14, color: "var(--color-text-secondary)", lineHeight: 1.5 },
  evidenceRow: { display: "flex", gap: 10, alignItems: "baseline", fontSize: 12, flexWrap: "wrap" },
  evId: { color: "var(--color-mono-text)", fontSize: 11 },
  evTime: { color: "var(--color-text-muted)", fontVariantNumeric: "tabular-nums" },
  evLevel: { width: 44, textTransform: "uppercase", fontSize: 10 },
  evMsg: { color: "var(--color-text-secondary)", flex: 1, minWidth: 200, wordBreak: "break-word" },

  eventRow: { display: "flex", gap: 10, alignItems: "baseline", fontSize: 12, padding: "3px 0", flexWrap: "wrap" },
  offset: { width: 62, color: "var(--color-text-muted)", fontVariantNumeric: "tabular-nums" },
  evSource: { width: 90, color: "var(--color-text-tertiary)" },

  details: { fontSize: 13, color: "var(--color-text-secondary)" },
  summary: { cursor: "pointer", padding: "4px 0" },
  pre: {
    background: "var(--color-bg-inset)", padding: "var(--space-3)", borderRadius: "var(--radius-sm)",
    fontSize: 11, color: "var(--color-mono-text)", overflowX: "auto", whiteSpace: "pre-wrap", wordBreak: "break-all",
  },
};
