import React, {
  FormEvent,
  KeyboardEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { motion, AnimatePresence } from "framer-motion";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Components } from "react-markdown";
import { chat, output, MessageResponse } from "../api/client";

interface ChatPanelProps {
  sessionId: string;
  connectionId: string;
  modelId?: string | null;
  onQueryStart?: () => void;
  onQueryEnd?: () => void;
  onSessionExpired?: () => void;
}

const CHIPS = [
  "What are the top 10 categories by revenue?",
  "Show me the distribution by type",
  "Which items have the highest average rating?",
  "Top 10 customers by total spend",
];

// Compact suggestions shown above the input at all times (not just the
// empty-state hero) — a persistent, low-commitment way to keep going.
const PERSISTENT_CHIPS = ["Top products by revenue", "Orders this month"];

export default function ChatPanel({
  sessionId,
  connectionId,
  modelId,
  onQueryStart,
  onQueryEnd,
  onSessionExpired,
}: ChatPanelProps): React.JSX.Element {
  const [messages, setMessages] = useState<MessageResponse[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showNewMsg, setShowNewMsg] = useState(false);

  const threadRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const atBottomRef = useRef(true);

  const hasMessages = messages.length > 0;

  // ── load history on session change ────────────────────────────────────────
  useEffect(() => {
    setMessages([]);
    setInput("");
    setError(null);
    setShowNewMsg(false);
    atBottomRef.current = true;
    loadHistory();
  }, [sessionId]);

  async function loadHistory(): Promise<void> {
    try {
      const session = await chat.getSession(sessionId);
      setMessages(session.history);
    } catch (err: unknown) {
      const status = (err as { response?: { status?: number } })?.response?.status;
      if (status === 404) {
        onSessionExpired?.();
      }
      /* new session has no history — ignore other errors */
    }
  }

  // ── scroll tracking ────────────────────────────────────────────────────────
  useEffect(() => {
    const el = threadRef.current;
    if (!el) return;
    const onScroll = (): void => {
      const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
      atBottomRef.current = atBottom;
      setShowNewMsg(!atBottom);
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, []);

  // ── auto-scroll when new content arrives ────────────────────────────────
  useEffect(() => {
    if (atBottomRef.current) {
      bottomRef.current?.scrollIntoView({ behavior: "smooth" });
    } else {
      setShowNewMsg(true);
    }
  }, [messages.length, loading]);

  const scrollToBottom = useCallback((): void => {
    atBottomRef.current = true;
    setShowNewMsg(false);
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, []);

  // ── submit ─────────────────────────────────────────────────────────────────
  async function handleSubmit(
    e?: FormEvent,
    overrideContent?: string
  ): Promise<void> {
    e?.preventDefault();
    const content = (overrideContent ?? input).trim();
    if (!content || loading) return;

    setInput("");
    setError(null);
    setLoading(true);
    atBottomRef.current = true;
    setShowNewMsg(false);
    onQueryStart?.();

    const userMsg: MessageResponse = {
      role: "user",
      content,
      needs_clarification: false,
      clarification_question: null,
      download_token: null,
      pdf_download_token: null,
      output_format: null,
      summary_stats: null,
      reasoning: null,
      generated_query: null,
      target_tables: null,
      model_used: null,
      timestamp: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, userMsg]);

    try {
      const response = await chat.sendMessage(sessionId, { content, model_id: modelId });
      setMessages((prev) => [...prev, response]);
    } catch (err: unknown) {
      setMessages((prev) => prev.slice(0, -1));
      const status = (err as { response?: { status?: number } })?.response?.status;
      if (status === 404) {
        onSessionExpired?.();
      } else {
        setError("Something went wrong. Please try again.");
      }
    } finally {
      setLoading(false);
      onQueryEnd?.();
      setTimeout(() => textareaRef.current?.focus(), 50);
    }
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>): void {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  }

  // ── render ─────────────────────────────────────────────────────────────────
  return (
    <div style={s.root}>
      {/* ── Scrollable thread ── */}
      <div ref={threadRef} style={s.thread}>
        {/* Welcome hero — visible only before first message */}
        <AnimatePresence>
          {!hasMessages && !loading && (
            <motion.div
              key="hero"
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -20, transition: { duration: 0.18 } }}
              transition={{ duration: 0.25 }}
              style={s.hero}
            >
              <div style={s.heroIcon}>
                <svg width="32" height="32" viewBox="0 0 24 24" fill="none">
                  <path
                    d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"
                    stroke="var(--color-accent-text)"
                    strokeWidth="1.5"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </div>
              <h2 style={s.heroTitle}>Ask anything about your data</h2>
              <p style={s.heroSub}>
                Connected to{" "}
                <span style={{ color: "var(--color-text-tertiary)", fontWeight: 500 }}>
                  {connectionId}
                </span>{" "}
                · MongoDB
              </p>
              <div style={s.chipsGrid}>
                {CHIPS.map((chip) => (
                  <button
                    key={chip}
                    className="prompt-chip"
                    style={s.chip}
                    onClick={() => handleSubmit(undefined, chip)}
                  >
                    {chip}
                  </button>
                ))}
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        {/* Message list */}
        {messages.map((msg, idx) =>
          msg.role === "user" ? (
            <UserBubble key={idx} message={msg} />
          ) : (
            <AssistantCard
              key={idx}
              message={msg}
              connectionId={connectionId}
              precedingUserContent={
                idx > 0 && messages[idx - 1].role === "user" ? messages[idx - 1].content : null
              }
              onEditPrevious={(content) => {
                setInput(content);
                setTimeout(() => textareaRef.current?.focus(), 50);
              }}
              onRegenerate={(content) => handleSubmit(undefined, content)}
            />
          )
        )}

        {/* Shimmer skeleton while waiting */}
        {loading && <ShimmerCard />}

        <div ref={bottomRef} style={{ height: 20 }} />
      </div>

      {/* ↓ New message pill */}
      <AnimatePresence>
        {showNewMsg && (
          <motion.button
            key="pill"
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 6 }}
            transition={{ duration: 0.15 }}
            style={s.newMsgPill}
            onClick={scrollToBottom}
          >
            ↓ New message
          </motion.button>
        )}
      </AnimatePresence>

      {/* ── Input area ── */}
      <div style={s.inputArea}>
        {hasMessages && (
          <div style={s.persistentChips}>
            {PERSISTENT_CHIPS.map((chip) => (
              <button
                key={chip}
                type="button"
                className="persistent-chip"
                style={s.persistentChip}
                onClick={() => handleSubmit(undefined, chip)}
                disabled={loading}
              >
                {chip}
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" style={{ flexShrink: 0 }}>
                  <path d="M7 17L17 7M7 7h10v10" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </button>
            ))}
          </div>
        )}
        {error && (
          <div style={s.errorBanner}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" style={{ flexShrink: 0 }}>
              <circle cx="12" cy="12" r="10" stroke="var(--color-danger)" strokeWidth="2" />
              <path d="M12 8v4M12 16h.01" stroke="var(--color-danger)" strokeWidth="2" strokeLinecap="round" />
            </svg>
            {error}
          </div>
        )}
        <div className="pill-input" style={s.pillInput}>
          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask anything about your data..."
            style={s.textarea}
            disabled={loading}
            rows={1}
          />
          <button
            className="send-btn"
            onClick={() => handleSubmit()}
            disabled={loading || !input.trim()}
            style={{
              ...s.sendBtn,
              ...(!input.trim() || loading ? s.sendBtnOff : {}),
            }}
          >
            {loading ? (
              <span style={s.sendSpinner} />
            ) : (
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
                <path
                  d="M22 2L11 13M22 2L15 22l-4-9-9-4 20-7z"
                  stroke="white"
                  strokeWidth="2.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            )}
          </button>
        </div>
        <p style={s.hint}>Enter to send · Shift+Enter for new line</p>
      </div>
    </div>
  );
}

// ─── User bubble ─────────────────────────────────────────────────────────────

function UserBubble({ message }: { message: MessageResponse }): React.JSX.Element {
  return (
    <motion.div
      initial={{ opacity: 0, x: 16 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ duration: 0.15, ease: "easeOut" }}
      style={s.userRow}
    >
      <div style={s.userBubble}>{message.content}</div>
    </motion.div>
  );
}

// ─── Assistant card ───────────────────────────────────────────────────────────

function AssistantCard({
  message,
  connectionId,
  precedingUserContent,
  onEditPrevious,
  onRegenerate,
}: {
  message: MessageResponse;
  connectionId: string;
  precedingUserContent: string | null;
  onEditPrevious: (content: string) => void;
  onRegenerate: (content: string) => void;
}): React.JSX.Element {
  const hasDownloads = !!(message.download_token || message.pdf_download_token);
  // A concrete target_tables list means the model interpreted the question
  // into an actual query plan — that's what the "Interpreted as" chip surfaces.
  const interpretation =
    message.target_tables && message.target_tables.length > 0
      ? message.target_tables.join(", ")
      : null;

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.2, ease: "easeOut" }}
      style={s.card}
    >
      {/* Card header */}
      <div style={s.cardHeader}>
        <div style={s.cardAvatar}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
            <path
              d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"
              stroke="var(--color-accent-text)"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </div>
        <span style={s.cardName}>NL Data Insight</span>
        <span style={s.cardTs}>{fmtTime(message.timestamp)}</span>
      </div>

      {/* Interpreted-as chip — only when a concrete query interpretation exists */}
      {interpretation && precedingUserContent && (
        <div style={s.interpretedChip}>
          <span style={s.interpretedLabel}>Interpreted as: {interpretation}</span>
          <button
            type="button"
            className="interpreted-change"
            style={s.interpretedChange}
            onClick={() => onEditPrevious(precedingUserContent)}
          >
            change
          </button>
        </div>
      )}

      {/* Clarification notice */}
      {message.needs_clarification && message.clarification_question && (
        <div style={s.clarify}>
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" style={{ flexShrink: 0, marginTop: 1 }}>
            <circle cx="12" cy="12" r="10" stroke="var(--color-warning)" strokeWidth="2" />
            <path d="M12 8v4M12 16h.01" stroke="var(--color-warning)" strokeWidth="2" strokeLinecap="round" />
          </svg>
          {message.clarification_question}
        </div>
      )}

      {/* Insight prose */}
      <div style={s.cardBody}>
        <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
          {message.content}
        </ReactMarkdown>
      </div>

      {/* Inline chart — rendered from download_token when present */}
      {message.download_token && (
        <ChartImage token={message.download_token} />
      )}

      {/* Collections footer */}
      <div style={s.cardFooter}>
        <DbIcon />
        <span style={s.footerLabel}>{connectionId}</span>
        <span style={s.footerDot}>·</span>
        <span style={s.footerLabel}>MongoDB</span>
        {message.model_used && (
          <>
            <span style={s.footerDot}>·</span>
            <span style={s.footerLabel}>{message.model_used}</span>
          </>
        )}
      </div>

      {/* Reasoning trace — collapsed by default */}
      {(message.reasoning || message.generated_query) && (
        <ReasoningPanel
          reasoning={message.reasoning}
          generatedQuery={message.generated_query}
          targetTables={message.target_tables}
        />
      )}

      {/* Download action bar */}
      {hasDownloads && (
        <DownloadBar
          chartToken={message.download_token}
          pdfToken={message.pdf_download_token}
        />
      )}

      {/* Message actions — copy / regenerate / feedback */}
      <MessageActions
        content={message.content}
        canRegenerate={!!precedingUserContent}
        onRegenerate={() => precedingUserContent && onRegenerate(precedingUserContent)}
      />
    </motion.div>
  );
}

// ─── Message action row ───────────────────────────────────────────────────────

const ICON_COPY = (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
    <rect x="9" y="9" width="14" height="14" rx="2" stroke="currentColor" strokeWidth="2" />
    <path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1" stroke="currentColor" strokeWidth="2" />
  </svg>
);

const ICON_REGENERATE = (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
    <path d="M21 12a9 9 0 11-3-6.7M21 3v5h-5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

const ICON_THUMBS_UP = (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
    <path d="M14 9V5a3 3 0 00-3-3l-4 9v11h11.3a2 2 0 002-1.7l1.4-9a2 2 0 00-2-2.3H14z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    <path d="M7 22H4a2 2 0 01-2-2v-7a2 2 0 012-2h3" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

const ICON_THUMBS_DOWN = (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" style={{ transform: "rotate(180deg)" }}>
    <path d="M14 9V5a3 3 0 00-3-3l-4 9v11h11.3a2 2 0 002-1.7l1.4-9a2 2 0 00-2-2.3H14z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    <path d="M7 22H4a2 2 0 01-2-2v-7a2 2 0 012-2h3" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

function MessageActions({
  content,
  canRegenerate,
  onRegenerate,
}: {
  content: string;
  canRegenerate: boolean;
  onRegenerate: () => void;
}): React.JSX.Element {
  const [copied, setCopied] = useState(false);
  // Local-only UI state — there's no backend endpoint to persist feedback yet.
  const [feedback, setFeedback] = useState<"up" | "down" | null>(null);

  async function handleCopy(): Promise<void> {
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      /* clipboard permission denied — silently ignore */
    }
  }

  return (
    <div style={s.actionsRow}>
      <button type="button" className="msg-action" style={s.actionBtn} onClick={handleCopy} title="Copy">
        {copied ? ICON_CHECK : ICON_COPY}
      </button>
      {canRegenerate && (
        <button type="button" className="msg-action" style={s.actionBtn} onClick={onRegenerate} title="Regenerate">
          {ICON_REGENERATE}
        </button>
      )}
      <button
        type="button"
        className="msg-action"
        style={{ ...s.actionBtn, ...(feedback === "up" ? s.actionBtnActiveGood : {}) }}
        onClick={() => setFeedback((v) => (v === "up" ? null : "up"))}
        title="Good response"
      >
        {ICON_THUMBS_UP}
      </button>
      <button
        type="button"
        className="msg-action"
        style={{ ...s.actionBtn, ...(feedback === "down" ? s.actionBtnActiveBad : {}) }}
        onClick={() => setFeedback((v) => (v === "down" ? null : "down"))}
        title="Bad response"
      >
        {ICON_THUMBS_DOWN}
      </button>
    </div>
  );
}

// ─── Reasoning trace panel ─────────────────────────────────────────────────────

function ReasoningPanel({
  reasoning,
  generatedQuery,
  targetTables,
}: {
  reasoning: string | null;
  generatedQuery: string | null;
  targetTables: string[] | null;
}): React.JSX.Element {
  const [open, setOpen] = useState(false);

  return (
    <div style={s.reasoningWrap}>
      <button
        type="button"
        className="reasoning-toggle"
        style={s.reasoningToggle}
        onClick={() => setOpen((v) => !v)}
      >
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" style={{ flexShrink: 0 }}>
          <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.6" />
          <path d="M9.5 9.5a2.5 2.5 0 114.2 1.8c-.7.6-1.2 1-1.2 2.2M12 17h.01" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        {open ? "Hide reasoning" : "Show reasoning"}
        <svg
          width="11" height="11" viewBox="0 0 24 24" fill="none"
          style={{ marginLeft: 2, transform: open ? "rotate(180deg)" : "none", transition: "transform 0.15s" }}
        >
          <path d="M6 9l6 6 6-6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>

      {open && (
        <div style={s.reasoningBody}>
          {reasoning && (
            <>
              <div style={s.reasoningLabel}>How the query was planned</div>
              <p style={s.reasoningProse}>{reasoning}</p>
            </>
          )}
          {targetTables && targetTables.length > 0 && (
            <div style={s.reasoningTables}>
              {targetTables.map((t) => (
                <span key={t} style={s.reasoningTableChip}>{t}</span>
              ))}
            </div>
          )}
          {generatedQuery && (
            <>
              <div style={{ ...s.reasoningLabel, marginTop: 10 }}>Query executed</div>
              <pre style={md.codeWrap}>
                <code style={md.code}>{_prettyQuery(generatedQuery)}</code>
              </pre>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function _prettyQuery(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

// ─── Inline chart image ───────────────────────────────────────────────────────

function ChartImage({ token }: { token: string }): React.JSX.Element {
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [blobUrl, setBlobUrl] = useState<string | null>(null);

  useEffect(() => {
    let revoked = false;
    output.fetchBlobUrl(token).then((url) => {
      if (!revoked) {
        setBlobUrl(url);
        setStatus("ready");
      } else {
        URL.revokeObjectURL(url);
      }
    }).catch(() => {
      if (!revoked) setStatus("error");
    });
    return () => {
      revoked = true;
      if (blobUrl) URL.revokeObjectURL(blobUrl);
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  if (status === "error") return <></>;

  return (
    <div style={s.chartWrap}>
      {status === "loading" && (
        <div style={s.chartShimmer} />
      )}
      {status === "ready" && blobUrl && (
        <img
          src={blobUrl}
          alt="Query result chart"
          style={s.chartImg}
        />
      )}
    </div>
  );
}

// ─── Shimmer skeleton ─────────────────────────────────────────────────────────

function ShimmerCard(): React.JSX.Element {
  return (
    <div style={s.card}>
      <div style={s.cardHeader}>
        <div style={{ ...s.cardAvatar, background: "var(--color-bg-elevated)" }} />
        <div style={{ ...shimmer, width: 118, height: 14 }} />
        <div style={{ marginLeft: "auto", ...shimmer, width: 60, height: 12 }} />
      </div>
      <div style={{ paddingTop: 4 }}>
        {[97, 89, 76, 61].map((w, i) => (
          <div
            key={i}
            style={{
              ...shimmer,
              width: `${w}%`,
              height: 14,
              marginBottom: i < 3 ? 12 : 0,
              animationDelay: `${i * 0.12}s`,
            }}
          />
        ))}
      </div>
      <div style={{ ...s.cardFooter, borderTopColor: "transparent" }}>
        <div style={{ ...shimmer, width: 138, height: 19, borderRadius: "var(--radius-md)" }} />
      </div>
    </div>
  );
}

// ─── Download action bar ──────────────────────────────────────────────────────

const ICON_CHART = (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
    <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    <polyline points="7 10 12 15 17 10" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    <line x1="12" y1="15" x2="12" y2="3" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
  </svg>
);

const ICON_PDF = (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
    <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    <path d="M14 2v6h6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    <line x1="16" y1="13" x2="8" y2="13" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    <line x1="16" y1="17" x2="8" y2="17" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    <line x1="10" y1="9" x2="8" y2="9" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
  </svg>
);

const ICON_CHECK = (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
    <path d="M20 6L9 17l-5-5" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

function DlButton({
  token, filename, label, icon,
}: { token: string; filename: string; label: string; icon: React.ReactNode }): React.JSX.Element {
  const [state, setState] = useState<"idle" | "loading" | "done">("idle");

  async function handle(): Promise<void> {
    setState("loading");
    try {
      await output.downloadFile(token, filename);
      setState("done");
      setTimeout(() => setState("idle"), 3500);
    } catch {
      setState("idle");
    }
  }

  return (
    <button
      className={state === "idle" ? "dl-btn" : ""}
      onClick={handle}
      disabled={state === "loading"}
      style={{
        ...s.dlBtn,
        ...(state === "done" ? s.dlBtnDone : {}),
        ...(state === "loading" ? { opacity: 0.65, cursor: "not-allowed" } : {}),
      }}
    >
      {state === "loading" && <span style={s.dlSpinner} />}
      {state === "done" && ICON_CHECK}
      {state === "idle" && icon}
      <span style={{ marginLeft: 6 }}>
        {state === "loading" ? "Saving…" : state === "done" ? "Saved" : label}
      </span>
    </button>
  );
}

function DownloadBar({ chartToken, pdfToken }: {
  chartToken: string | null;
  pdfToken: string | null;
}): React.JSX.Element {
  return (
    <div style={s.dlBar}>
      {chartToken && (
        <DlButton token={chartToken} filename="chart.png" label="Save PNG" icon={ICON_CHART} />
      )}
      {pdfToken && (
        <DlButton token={pdfToken} filename="report.pdf" label="Download PDF" icon={ICON_PDF} />
      )}
    </div>
  );
}

// ─── Helpers & sub-components ─────────────────────────────────────────────────

function DbIcon(): React.JSX.Element {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" style={{ flexShrink: 0 }}>
      <ellipse cx="12" cy="5" rx="9" ry="3" stroke="var(--color-icon-faint)" strokeWidth="1.5" />
      <path d="M3 5v14c0 1.657 4.03 3 9 3s9-1.343 9-3V5" stroke="var(--color-icon-faint)" strokeWidth="1.5" />
      <path d="M3 12c0 1.657 4.03 3 9 3s9-1.343 9-3" stroke="var(--color-icon-faint)" strokeWidth="1.5" />
    </svg>
  );
}

function fmtTime(iso: string): string {
  const d = new Date(iso);
  const diff = Date.now() - d.getTime();
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

const shimmer: React.CSSProperties = {
  borderRadius: "var(--radius-sm)",
  background: "linear-gradient(90deg, var(--color-bg-inset) 25%, var(--color-border-strong) 50%, var(--color-bg-inset) 75%)",
  backgroundSize: "200% 100%",
  animation: "shimmer 1.6s ease-in-out infinite",
};

// ─── Markdown component overrides ─────────────────────────────────────────────

const MD_COMPONENTS: Components = {
  p: ({ children }) => <p style={md.p}>{children}</p>,
  h1: ({ children }) => <h1 style={md.h1}>{children}</h1>,
  h2: ({ children }) => <h2 style={md.h2}>{children}</h2>,
  h3: ({ children }) => <h3 style={md.h3}>{children}</h3>,
  ul: ({ children }) => <ul style={md.ul}>{children}</ul>,
  ol: ({ children }) => <ol style={md.ol}>{children}</ol>,
  li: ({ children }) => <li style={md.li}>{children}</li>,
  strong: ({ children }) => <strong style={{ color: "var(--color-prose-strong)", fontWeight: 600 }}>{children}</strong>,
  em: ({ children }) => <em style={{ color: "var(--color-prose-em)", fontStyle: "italic" }}>{children}</em>,
  blockquote: ({ children }) => <blockquote style={md.blockquote}>{children}</blockquote>,
  hr: () => <hr style={md.hr} />,
  pre: ({ children }) => <>{children}</>,
  code: ({ className, children }) => {
    const isBlock = Boolean(className);
    if (isBlock) {
      return (
        <pre style={md.codeWrap}>
          <code style={md.code}>{children}</code>
        </pre>
      );
    }
    return <code style={md.inlineCode}>{children}</code>;
  },
  table: ({ children }) => (
    <div style={md.tableWrap}>
      <table style={md.table}>{children}</table>
    </div>
  ),
  thead: ({ children }) => <thead>{children}</thead>,
  th: ({ children }) => <th style={md.th}>{children}</th>,
  td: ({ children }) => <td style={md.td}>{children}</td>,
};

const md: Record<string, React.CSSProperties> = {
  p: { margin: "0 0 10px", fontSize: 16, lineHeight: 1.75, color: "var(--color-text-secondary)" },
  h1: { fontSize: 20, fontWeight: 700, color: "var(--color-prose-h1)", margin: "16px 0 8px", letterSpacing: "-0.3px" },
  h2: { fontSize: 17, fontWeight: 600, color: "var(--color-prose-h2)", margin: "14px 0 6px" },
  h3: { fontSize: 16, fontWeight: 600, color: "var(--color-prose-h3)", margin: "12px 0 5px" },
  ul: { paddingLeft: 22, margin: "0 0 10px" },
  ol: { paddingLeft: 22, margin: "0 0 10px" },
  li: { fontSize: 16, lineHeight: 1.7, color: "var(--color-text-secondary)", marginBottom: 3 },
  blockquote: {
    borderLeft: "3px solid var(--color-border-strong)",
    paddingLeft: 14, margin: "0 0 10px",
    color: "var(--color-text-tertiary)", fontSize: 15, fontStyle: "italic",
  },
  hr: { border: "none", borderTop: "var(--border-width) solid var(--color-border)", margin: "14px 0" },
  codeWrap: {
    background: "var(--color-bg-surface-2)",
    border: "var(--border-width) solid var(--color-border)",
    borderRadius: "var(--radius-sm)",
    padding: "12px 14px",
    overflowX: "auto",
    margin: "0 0 10px",
  },
  code: { fontSize: 13, fontFamily: "'JetBrains Mono', 'Fira Code', monospace", color: "var(--color-mono-text)", lineHeight: 1.65 },
  inlineCode: {
    background: "var(--color-bg-inset)",
    border: "var(--border-width) solid var(--color-border-strong)",
    borderRadius: "var(--radius-sm)",
    padding: "1px 5px",
    fontSize: 14.5,
    fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
    color: "var(--color-accent-text)",
  },
  tableWrap: { overflowX: "auto", margin: "0 0 10px" },
  table: { width: "100%", borderCollapse: "collapse", fontSize: 14 },
  th: {
    padding: "8px 12px", textAlign: "left",
    background: "var(--color-bg-inset)", color: "var(--color-text-tertiary)",
    fontWeight: 600, fontSize: 13,
    borderBottom: "var(--border-width) solid var(--color-border)",
  },
  td: {
    padding: "7px 12px",
    color: "var(--color-text-secondary)", fontSize: 14,
    borderBottom: "var(--border-width) solid var(--color-bg-inset)",
  },
};

// ─── Styles ───────────────────────────────────────────────────────────────────

const s: Record<string, React.CSSProperties> = {
  root: {
    display: "flex", flexDirection: "column",
    flex: 1, overflow: "hidden",
    background: "var(--color-bg-app)",
    position: "relative",
  },

  // Thread
  thread: { flex: 1, overflowY: "auto", scrollBehavior: "smooth" },

  // Hero
  hero: {
    display: "flex", flexDirection: "column", alignItems: "center",
    padding: "80px 32px 48px",
    maxWidth: 700, margin: "0 auto",
  },
  heroIcon: {
    width: 74, height: 74, borderRadius: "var(--radius-lg)",
    background: "var(--gradient-hero-icon)",
    border: "var(--border-width) solid var(--color-accent-border)",
    display: "flex", alignItems: "center", justifyContent: "center",
    marginBottom: 24,
    boxShadow: "0 10px 40px var(--color-accent-border), 0 0 0 1px rgba(var(--color-accent-rgb), 0.08)",
  },
  heroTitle: {
    fontSize: 28, fontWeight: 700, letterSpacing: "-0.5px",
    color: "var(--color-text-primary)", textAlign: "center", marginBottom: 10,
  },
  heroSub: {
    fontSize: 15, color: "var(--color-text-muted)",
    textAlign: "center", marginBottom: 40, lineHeight: 1.6,
  },
  chipsGrid: {
    display: "grid", gridTemplateColumns: "1fr 1fr",
    gap: 8, width: "100%",
  },
  chip: {
    padding: "13px 14px",
    background: "var(--color-bg-surface)",
    border: "var(--border-width) solid var(--color-border)",
    borderRadius: "var(--radius-md)",
    color: "var(--color-text-tertiary)", fontSize: 14,
    textAlign: "left", cursor: "pointer",
    lineHeight: 1.5, transition: "all 0.15s",
    fontFamily: "inherit",
  },

  // Messages
  userRow: {
    display: "flex", justifyContent: "flex-end",
    padding: "5px 24px",
  },
  userBubble: {
    maxWidth: "68%",
    padding: "12px 18px",
    background: "var(--color-accent-gradient)",
    borderRadius: "18px 18px 4px 18px",
    color: "var(--color-on-accent)", fontSize: 15, lineHeight: 1.65,
    boxShadow: "0 4px 18px rgba(var(--color-accent-rgb), 0.28)",
  },

  // Assistant card
  card: {
    margin: "8px 20px",
    padding: "20px 22px 16px",
    background: "var(--color-bg-surface)",
    border: "var(--border-width) solid var(--color-border)",
    borderRadius: "var(--radius-lg)",
    boxShadow: "0 1px 6px rgba(0,0,0,0.35)",
  },
  cardHeader: {
    display: "flex", alignItems: "center", gap: 9,
    marginBottom: 16,
  },
  cardAvatar: {
    width: 28, height: 28, borderRadius: "var(--radius-sm)", flexShrink: 0,
    background: "var(--gradient-avatar)",
    border: "var(--border-width) solid var(--color-accent-border)",
    display: "flex", alignItems: "center", justifyContent: "center",
  },
  cardName: { fontSize: 14, fontWeight: 600, color: "var(--color-text-tertiary)" },
  cardTs: { marginLeft: "auto", fontSize: 12, color: "var(--color-text-faint)" },
  cardBody: { minHeight: 0 },

  // Interpreted-as chip
  interpretedChip: {
    display: "flex", alignItems: "center", gap: "var(--space-2)",
    marginBottom: "var(--space-3)",
  },
  interpretedLabel: {
    fontSize: 13, color: "var(--color-text-tertiary)",
    overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
  },
  interpretedChange: {
    background: "transparent", border: "none", padding: 0,
    fontSize: 13, fontWeight: 600, color: "var(--color-accent-text)",
    textDecoration: "underline", cursor: "pointer", fontFamily: "inherit",
    flexShrink: 0,
  },

  // Message actions row
  actionsRow: {
    display: "flex", alignItems: "center", gap: "var(--space-1)",
    marginTop: "var(--space-2)",
  },
  actionBtn: {
    width: 28, height: 28,
    display: "flex", alignItems: "center", justifyContent: "center",
    background: "transparent",
    border: "var(--border-width) solid transparent",
    borderRadius: "var(--radius-sm)",
    color: "var(--color-text-muted)",
    cursor: "pointer", transition: "all 0.15s",
  },
  actionBtnActiveGood: {
    background: "var(--color-accent-bg)",
    color: "var(--color-success)",
  },
  actionBtnActiveBad: {
    background: "var(--color-danger-bg)",
    color: "var(--color-danger)",
  },
  cardFooter: {
    display: "flex", alignItems: "center", gap: 5,
    marginTop: 16, paddingTop: 12,
    borderTop: "var(--border-width) solid var(--color-border-soft)",
  },
  footerLabel: { fontSize: 12, color: "var(--color-text-faint)" },
  footerDot: { fontSize: 12, color: "var(--color-text-disabled)" },

  clarify: {
    display: "flex", alignItems: "flex-start", gap: 8,
    marginBottom: 14, padding: "10px 12px",
    background: "rgba(var(--color-warning-rgb), 0.05)",
    border: "var(--border-width) solid var(--color-warning-border)",
    borderRadius: "var(--radius-sm)", color: "var(--color-warning)", fontSize: 15, lineHeight: 1.5,
  },

  // Inline chart
  chartWrap: {
    margin: "16px 0 4px",
    borderRadius: "var(--radius-md)",
    overflow: "hidden",
    border: "var(--border-width) solid var(--color-border)",
    background: "var(--color-bg-surface-2)",
    minHeight: 48,
  },
  chartShimmer: {
    width: "100%", height: 240,
    background: "linear-gradient(90deg, var(--color-bg-surface-2) 25%, var(--color-border-topbar) 50%, var(--color-bg-surface-2) 75%)",
    backgroundSize: "200% 100%",
    animation: "shimmer 1.6s ease-in-out infinite",
  },
  chartImg: {
    display: "block",
    width: "100%",
    height: "auto",
  },

  // Reasoning trace
  reasoningWrap: {
    marginTop: 12,
  },
  reasoningToggle: {
    display: "inline-flex", alignItems: "center", gap: 6,
    padding: "6px 12px",
    background: "transparent",
    border: "var(--border-width) solid var(--color-border-strong)",
    borderRadius: "var(--radius-pill)",
    color: "var(--color-text-tertiary)", fontSize: 13.5, fontWeight: 500,
    cursor: "pointer", transition: "all 0.15s",
    fontFamily: "inherit",
  },
  reasoningBody: {
    marginTop: 10, padding: "12px 14px",
    background: "var(--color-bg-surface-2)",
    border: "var(--border-width) solid var(--color-border)",
    borderRadius: "var(--radius-md)",
  },
  reasoningLabel: {
    fontSize: 12.5, fontWeight: 700, letterSpacing: "0.05em",
    textTransform: "uppercase", color: "var(--color-text-muted)",
    marginBottom: 6,
  },
  reasoningProse: {
    fontSize: 14, lineHeight: 1.65, color: "var(--color-text-secondary)",
    margin: 0,
  },
  reasoningTables: {
    display: "flex", flexWrap: "wrap", gap: 6, marginTop: 8,
  },
  reasoningTableChip: {
    padding: "3px 9px",
    background: "rgba(var(--color-accent-rgb), 0.08)",
    border: "var(--border-width) solid var(--color-accent-border)",
    borderRadius: "var(--radius-sm)",
    fontSize: 12, color: "var(--color-accent-text)", fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
  },

  // Download bar
  dlBar: {
    marginTop: 14, paddingTop: 12,
    borderTop: "var(--border-width) solid var(--color-border-soft)",
    display: "flex", gap: 8,
  },
  dlBtn: {
    display: "inline-flex", alignItems: "center",
    padding: "7px 14px",
    background: "transparent",
    border: "var(--border-width) solid var(--color-border-strong)",
    borderRadius: "var(--radius-pill)", flexShrink: 0,
    color: "var(--color-text-tertiary)", fontSize: 13, fontWeight: 500,
    cursor: "pointer", transition: "all 0.15s",
    fontFamily: "inherit",
  },
  dlBtnDone: {
    background: "rgba(34,197,94,0.08)",
    borderColor: "rgba(34,197,94,0.2)",
    color: "var(--color-success)",
  },
  dlSpinner: {
    width: 12, height: 12, flexShrink: 0,
    border: "1.5px solid rgba(112,112,138,0.3)",
    borderTopColor: "var(--color-text-tertiary)",
    borderRadius: "50%",
    animation: "spin 0.8s linear infinite",
    display: "inline-block",
  },

  // New message pill
  newMsgPill: {
    position: "absolute", bottom: 88, left: "50%",
    transform: "translateX(-50%)",
    padding: "6px 16px",
    background: "var(--color-bg-elevated)",
    border: "var(--border-width) solid var(--color-border-strong)",
    borderRadius: "var(--radius-pill)", color: "var(--color-text-tertiary)",
    fontSize: 13, fontWeight: 500,
    cursor: "pointer", zIndex: 10,
    fontFamily: "inherit",
  },

  // Input
  inputArea: {
    padding: "10px 20px 14px",
    borderTop: "var(--border-width) solid var(--color-border-soft)",
    background: "var(--color-bg-app)",
    flexShrink: 0,
  },
  persistentChips: {
    display: "flex", flexWrap: "wrap", gap: "var(--space-2)",
    marginBottom: "var(--space-2)",
  },
  persistentChip: {
    display: "inline-flex", alignItems: "center", gap: 6,
    padding: "6px 12px",
    background: "var(--color-bg-surface)",
    border: "var(--border-width) solid var(--color-border)",
    borderRadius: "var(--radius-pill)",
    color: "var(--color-text-tertiary)", fontSize: 14.5,
    cursor: "pointer", transition: "all 0.15s",
    fontFamily: "inherit",
  },
  pillInput: {
    display: "flex", alignItems: "flex-end", gap: 10,
    padding: "10px 10px 10px 18px",
    background: "var(--color-bg-surface-2)",
    border: "var(--border-width) solid var(--color-border-strong)",
    borderRadius: "var(--radius-pill)",
    transition: "border-color 0.2s, box-shadow 0.2s",
  },
  textarea: {
    flex: 1, background: "transparent",
    border: "none", outline: "none",
    color: "var(--color-text-primary)", fontSize: 15, lineHeight: 1.6,
    resize: "none", fontFamily: "inherit",
    padding: "2px 0",
    maxHeight: 140, overflowY: "auto",
  },
  sendBtn: {
    width: 40, height: 40, borderRadius: "50%", flexShrink: 0,
    background: "var(--color-accent-gradient)",
    border: "none", color: "var(--color-on-accent)",
    display: "flex", alignItems: "center", justifyContent: "center",
    cursor: "pointer",
    boxShadow: "0 2px 10px rgba(var(--color-accent-rgb), 0.4)",
    transition: "opacity 0.15s, transform 0.15s",
  },
  sendBtnOff: {
    opacity: 0.28, cursor: "not-allowed",
    boxShadow: "none",
    background: "var(--color-border-strong)",
  },
  sendSpinner: {
    width: 16, height: 16,
    border: "2px solid rgba(255,255,255,0.3)",
    borderTopColor: "var(--color-on-accent)",
    borderRadius: "50%",
    animation: "spin 0.8s linear infinite",
    display: "inline-block",
  },
  hint: {
    fontSize: 12, color: "var(--color-text-disabled)",
    marginTop: 7, textAlign: "center",
  },
  errorBanner: {
    display: "flex", alignItems: "center", gap: 8,
    padding: "9px 12px", marginBottom: 9,
    background: "rgba(var(--color-danger-rgb), 0.07)",
    border: "var(--border-width) solid var(--color-danger-border)",
    borderRadius: "var(--radius-sm)", color: "var(--color-danger)", fontSize: 14,
  },
};
