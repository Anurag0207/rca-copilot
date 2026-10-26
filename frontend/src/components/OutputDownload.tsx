import React, { useState } from "react";
import { output } from "../api/client";

interface OutputDownloadProps {
  downloadToken: string;
  outputFormat: string;
}

const FORMAT_META: Record<string, { label: string; ext: string; color: string; icon: React.ReactNode }> = {
  chart: {
    label: "Chart Image", ext: "png", color: "#7c3aed",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
        <path d="M18 20V10M12 20V4M6 20v-6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
      </svg>
    ),
  },
  pdf: {
    label: "PDF Report", ext: "pdf", color: "#ef4444",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
        <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
        <path d="M14 2v6h6M16 13H8M16 17H8M10 9H8" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
      </svg>
    ),
  },
  ppt: {
    label: "PowerPoint", ext: "pptx", color: "#f97316",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
        <rect x="2" y="3" width="20" height="14" rx="2" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
        <path d="M8 21h8M12 17v4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
      </svg>
    ),
  },
};

export default function OutputDownload({ downloadToken, outputFormat }: OutputDownloadProps): React.JSX.Element {
  const [downloading, setDownloading] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const meta = FORMAT_META[outputFormat] ?? { label: "File", ext: "bin", color: "#6b6b82", icon: null };
  const filename = `insight_${downloadToken.slice(0, 8)}.${meta.ext}`;

  async function handleDownload(): Promise<void> {
    setError(null);
    setDownloading(true);
    try {
      await output.downloadFile(downloadToken, filename);
      setDone(true);
    } catch {
      setError("Download failed. The file may have expired.");
    } finally {
      setDownloading(false);
    }
  }

  return (
    <div style={s.root}>
      <div style={{ ...s.card, borderColor: `${meta.color}30` }}>
        {/* Icon */}
        <div style={{ ...s.iconWrap, background: `${meta.color}18`, color: meta.color }}>
          {meta.icon}
        </div>

        {/* Info */}
        <div style={s.info}>
          <span style={{ ...s.label, color: meta.color }}>{meta.label} ready</span>
          <span style={s.filename}>{filename}</span>
        </div>

        {/* Action */}
        {done ? (
          <div style={s.doneTag}>
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none">
              <path d="M20 6L9 17l-5-5" stroke="#10b981" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
            Downloaded
          </div>
        ) : (
          <button
            onClick={handleDownload}
            disabled={downloading}
            style={{ ...s.btn, background: meta.color, opacity: downloading ? 0.6 : 1 }}
          >
            {downloading ? (
              <><span style={s.spinner} /> Downloading…</>
            ) : (
              <>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
                  <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M7 10l5 5 5-5M12 15V3" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
                </svg>
                Download
              </>
            )}
          </button>
        )}
      </div>

      {error && (
        <div style={s.errorBox}>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" style={{ flexShrink: 0 }}>
            <circle cx="12" cy="12" r="10" stroke="#f87171" strokeWidth="2"/>
            <path d="M12 8v4M12 16h.01" stroke="#f87171" strokeWidth="2" strokeLinecap="round"/>
          </svg>
          {error}
        </div>
      )}
    </div>
  );
}

const s: Record<string, React.CSSProperties> = {
  root: { padding: "0 20px 16px", flexShrink: 0 },
  card: {
    display: "flex", alignItems: "center", gap: 14,
    padding: "14px 16px",
    background: "#111118",
    border: "1px solid #2d2d3e",
    borderRadius: 12,
    boxShadow: "0 2px 12px rgba(0,0,0,0.2)",
  },
  iconWrap: {
    width: 40, height: 40, borderRadius: 10, flexShrink: 0,
    display: "flex", alignItems: "center", justifyContent: "center",
  },
  info: { flex: 1, display: "flex", flexDirection: "column", gap: 2 },
  label: { fontSize: 13, fontWeight: 600 },
  filename: { fontSize: 11, color: "#4d4d62", fontFamily: "monospace" },
  btn: {
    display: "flex", alignItems: "center", gap: 7,
    padding: "9px 16px",
    border: "none", borderRadius: 8,
    color: "#fff", fontSize: 13, fontWeight: 600,
    cursor: "pointer",
    flexShrink: 0,
    boxShadow: "0 2px 8px rgba(0,0,0,0.3)",
    transition: "opacity 0.15s",
  },
  spinner: {
    width: 12, height: 12,
    border: "2px solid rgba(255,255,255,0.3)",
    borderTopColor: "#fff",
    borderRadius: "50%",
    animation: "spin 0.7s linear infinite",
    display: "inline-block",
  },
  doneTag: {
    display: "flex", alignItems: "center", gap: 6,
    padding: "8px 14px",
    background: "rgba(16,185,129,0.1)",
    border: "1px solid rgba(16,185,129,0.2)",
    borderRadius: 8,
    color: "#10b981", fontSize: 13, fontWeight: 500,
    flexShrink: 0,
  },
  errorBox: {
    display: "flex", alignItems: "center", gap: 8,
    marginTop: 8, padding: "9px 12px",
    background: "rgba(248,113,113,0.08)",
    border: "1px solid rgba(248,113,113,0.2)",
    borderRadius: 8, color: "#f87171", fontSize: 13,
  },
};
