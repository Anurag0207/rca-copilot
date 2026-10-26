import React, { useEffect, useRef, useState } from "react";
import type { LLMProvider } from "../api/client";

interface ModelPickerProps {
  providers: LLMProvider[];
  selectedProvider: string | null;
  onProviderChange: (providerId: string) => void;
  models: string[];
  selectedModel: string | null;
  onModelChange: (modelId: string) => void;
  loading: boolean;
  error: string | null;
}

export default function ModelPicker({
  providers,
  selectedProvider,
  onProviderChange,
  models,
  selectedModel,
  onModelChange,
  loading,
  error,
}: ModelPickerProps): React.JSX.Element {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent): void {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    function onKeyDown(e: KeyboardEvent): void {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const providerName =
    providers.find((p) => p.id === selectedProvider)?.name ?? "Select provider";
  const buttonLabel = loading
    ? "Loading models…"
    : error
    ? "Models unavailable"
    : selectedModel ?? "Select model";

  return (
    <div ref={rootRef} style={s.root}>
      <button
        type="button"
        className="picker-trigger"
        style={{ ...s.trigger, ...(open ? s.triggerOpen : {}) }}
        onClick={() => setOpen((v) => !v)}
        title={`${providerName} · ${buttonLabel}`}
      >
        <span style={s.dot} />
        <span style={s.triggerLabel}>{buttonLabel}</span>
        <svg
          width="11"
          height="11"
          viewBox="0 0 24 24"
          fill="none"
          style={{ flexShrink: 0, transform: open ? "rotate(180deg)" : "none", transition: "transform 0.15s" }}
        >
          <path d="M6 9l6 6 6-6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>

      {open && (
        <div style={s.panel}>
          <div style={s.sectionLabel}>Provider</div>
          {providers.map((p) => (
            <button
              key={p.id}
              type="button"
              className="picker-row"
              style={s.row}
              onClick={() => onProviderChange(p.id)}
            >
              <span style={s.rowLabel}>{p.name}</span>
              {p.id === selectedProvider && <CheckIcon />}
            </button>
          ))}

          <div style={{ ...s.sectionLabel, marginTop: 10 }}>Model</div>
          {loading && <div style={s.hint}>Fetching available models…</div>}
          {!loading && error && <div style={s.hintError}>{error}</div>}
          {!loading && !error && models.length === 0 && (
            <div style={s.hint}>No models found — pull one with `ollama pull &lt;model&gt;`.</div>
          )}
          {!loading &&
            !error &&
            models.map((m) => (
              <button
                key={m}
                type="button"
                className="picker-row"
                style={s.row}
                onClick={() => {
                  onModelChange(m);
                  setOpen(false);
                }}
              >
                <span style={s.rowLabel}>{m}</span>
                {m === selectedModel && <CheckIcon />}
              </button>
            ))}
        </div>
      )}
    </div>
  );
}

function CheckIcon(): React.JSX.Element {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" style={{ flexShrink: 0, color: "var(--color-accent-text)" }}>
      <path d="M20 6L9 17l-5-5" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

const s: Record<string, React.CSSProperties> = {
  root: { position: "relative" },
  trigger: {
    display: "flex", alignItems: "center", gap: 7,
    padding: "5px 10px",
    background: "var(--color-bg-surface)",
    border: "var(--border-width) solid var(--color-border-strong)",
    borderRadius: "var(--radius-pill)",
    color: "var(--color-text-primary)", fontSize: 13, fontWeight: 500,
    cursor: "pointer", fontFamily: "inherit",
    transition: "all 0.15s",
    maxWidth: 220,
  },
  triggerOpen: {
    borderColor: "var(--color-accent-border)",
    background: "var(--color-bg-elevated)",
  },
  dot: {
    width: 6, height: 6, borderRadius: "50%",
    background: "var(--color-accent)", flexShrink: 0,
  },
  triggerLabel: {
    overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
  },
  panel: {
    position: "absolute", top: "calc(100% + 6px)", right: 0,
    minWidth: 240, maxWidth: 320,
    background: "var(--color-bg-panel)",
    border: "var(--border-width) solid var(--color-border-strong)",
    borderRadius: "var(--radius-md)",
    boxShadow: "0 12px 32px rgba(0,0,0,0.5)",
    padding: "8px 6px",
    zIndex: 100,
  },
  sectionLabel: {
    fontSize: 11, fontWeight: 700, letterSpacing: "0.06em",
    textTransform: "uppercase", color: "var(--color-text-muted)",
    padding: "4px 8px 6px",
  },
  row: {
    display: "flex", alignItems: "center", justifyContent: "space-between",
    width: "100%", gap: 8,
    padding: "7px 8px",
    background: "transparent", border: "none", borderRadius: "var(--radius-sm)",
    color: "var(--color-text-secondary)", fontSize: 14.5,
    textAlign: "left", cursor: "pointer",
    fontFamily: "inherit",
  },
  rowLabel: {
    overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
  },
  hint: {
    padding: "6px 8px", fontSize: 13.5, color: "var(--color-text-muted)", lineHeight: 1.5,
  },
  hintError: {
    padding: "6px 8px", fontSize: 13.5, color: "var(--color-danger)", lineHeight: 1.5,
  },
};
