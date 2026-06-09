"use client";

/**
 * components/chat-interface.tsx
 *
 * The primary chat dashboard component for the BPPIMT Campus Resource Assistant.
 *
 * Features
 * ────────
 * • BPPIMT branded navigation header with logo, user avatar, and sign-out
 * • Dark / light mode toggle via next-themes
 * • Message timeline with smooth auto-scroll and animated entry
 * • Per-message status (streaming cursor, thinking indicator, error state)
 * • Streaming SSE consumer — connects to POST /api/v1/chat/stream via a
 *   Next.js API route proxy (to avoid CORS and hide backend credentials)
 * • Tool call status pills ("Generating calendar…", "Searching docs…")
 * • Suggestion chips on empty state
 * • Accessible textarea with keyboard submit (Enter / Shift+Enter)
 * • Full ARIA labelling throughout
 * • Full dark mode support with WCAG-compliant contrast ratios
 */

import * as React from "react";
import * as Avatar from "@radix-ui/react-avatar";
import Image from "next/image";
import { Send, Loader2, GraduationCap, LogOut, Calendar, Mail, Search, ChevronDown } from "lucide-react";
import { useSession, signOut } from "next-auth/react";
import {
  type Message,
  type ToolInvocation,
  type StreamChunk,
  type ToolName,
  isTokenChunk,
  isToolOutputChunk,
  isDoneChunk,
  isErrorChunk,
} from "@/types/chat";
import { MarkdownRenderer } from "./markdown-renderer";
import { ThemeToggle } from "./theme-toggle";

// ─────────────────────────────────────────────────────────────────────────────
// Constants
// ─────────────────────────────────────────────────────────────────────────────

const SUGGESTION_CHIPS: ReadonlyArray<{ icon: React.ElementType; label: string }> = [
  { icon: Calendar, label: "Download my CSE-B timetable as a calendar" },
  { icon: Search, label: "What are the fee structure details?" },
  { icon: Mail, label: "When is Dr. Roy free on Wednesday?" },
  { icon: GraduationCap, label: "Show me the semester 6 exam schedule" },
];

const TOOL_DISPLAY: Readonly<Record<ToolName, { label: string; icon: React.ElementType; lightColor: string; darkColor: string }>> = {
  rag_query: { label: "Searching documents…", icon: Search, lightColor: "text-violet-600 bg-violet-50 border-violet-200", darkColor: "dark:text-violet-300 dark:bg-violet-950/50 dark:border-violet-800" },
  generate_ics: { label: "Generating calendar…", icon: Calendar, lightColor: "text-brand-600 bg-brand-50 border-brand-200", darkColor: "dark:text-brand-300 dark:bg-brand-950/50 dark:border-brand-800" },
  check_faculty_availability: { label: "Checking schedule…", icon: Mail, lightColor: "text-teal-600 bg-teal-50 border-teal-200", darkColor: "dark:text-teal-300 dark:bg-teal-950/50 dark:border-teal-800" },
};

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────

function generateId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
}

function formatTime(iso: string): string {
  try {
    return new Intl.DateTimeFormat("en-IN", {
      hour: "numeric",
      minute: "2-digit",
      hour12: true,
    }).format(new Date(iso));
  } catch {
    return "";
  }
}

function getUserInitials(name: string | null | undefined): string {
  if (!name) return "?";
  return name
    .split(" ")
    .slice(0, 2)
    .map((n) => n[0]?.toUpperCase() ?? "")
    .join("");
}

// ─────────────────────────────────────────────────────────────────────────────
// Sub-components
// ─────────────────────────────────────────────────────────────────────────────

/** Thinking / loading indicator (three animated dots) */
function ThinkingIndicator(): React.ReactElement {
  return (
    <div
      role="status"
      aria-label="Assistant is thinking"
      className="flex items-center gap-1 px-1"
    >
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          aria-hidden
          className="h-1.5 w-1.5 rounded-full bg-surface-400 dark:bg-surface-500 animate-pulse-dot"
          style={{ animationDelay: `${i * 0.16}s` }}
        />
      ))}
    </div>
  );
}

/** Tool status pill shown while a tool is running */
interface ToolPillProps {
  tool: ToolName;
  status: "running" | "completed" | "error";
}

function ToolPill({ tool, status }: ToolPillProps): React.ReactElement {
  const config = TOOL_DISPLAY[tool];
  const Icon = config.icon;

  return (
    <div
      role="status"
      aria-label={status === "running" ? config.label : `${tool} completed`}
      className={[
        "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1",
        "text-[11px] font-medium border",
        config.lightColor,
        config.darkColor,
        "transition-all duration-300",
      ].join(" ")}
    >
      {status === "running" ? (
        <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
      ) : (
        <Icon className="h-3 w-3" aria-hidden />
      )}
      <span>{status === "running" ? config.label : tool.replace(/_/g, " ")}</span>
    </div>
  );
}

/** User message bubble */
interface UserBubbleProps {
  message: Message;
}

function UserBubble({ message }: UserBubbleProps): React.ReactElement {
  return (
    <div
      className="flex justify-end animate-fade-in"
      role="listitem"
      aria-label={`You: ${message.content}`}
    >
      <div className="max-w-[75%] min-w-0">
        <div className={[
          "rounded-2xl rounded-tr-sm px-4 py-3",
          "bg-brand-600 text-white text-sm leading-relaxed shadow-card",
        ].join(" ")}>
          <p className="whitespace-pre-wrap break-words">{message.content}</p>
        </div>
        <p className="mt-1 text-right text-[10px] text-surface-400 dark:text-surface-500">
          {formatTime(message.createdAt)}
        </p>
      </div>
    </div>
  );
}

/** Assistant message bubble */
interface AssistantBubbleProps {
  message: Message;
  userInitials: string;
}

function AssistantBubble({ message, userInitials: _ }: AssistantBubbleProps): React.ReactElement {
  const isThinking = message.status === "pending" && message.content === "";

  return (
    <div
      className="flex items-start gap-3 animate-fade-in"
      role="listitem"
      aria-label={
        message.status === "streaming"
          ? "Assistant is responding"
          : `Assistant: ${message.content || "thinking"}`
      }
      aria-live={message.status === "streaming" ? "polite" : undefined}
      aria-atomic={false}
    >
      {/* Assistant avatar */}
      <div
        aria-hidden
        className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-brand-500 to-brand-700 shadow-glow-brand mt-0.5"
      >
        <GraduationCap className="h-4 w-4 text-white" />
      </div>

      <div className="min-w-0 flex-1">
        {/* Tool status pills */}
        {message.toolCalls.length > 0 && (
          <div
            className="flex flex-wrap gap-1.5 mb-2"
            role="list"
            aria-label="Active tool calls"
          >
            {message.toolCalls.map((tc, i) => (
              <ToolPill key={i} tool={tc.tool} status={tc.status} />
            ))}
          </div>
        )}

        {/* Message bubble */}
        <div className={[
          "rounded-2xl rounded-tl-sm px-4 py-3",
          "bg-white dark:bg-surface-900 border border-surface-100 dark:border-surface-800 shadow-card dark:shadow-none",
          message.status === "error"
            ? "border-red-100 bg-red-50 dark:border-red-900/50 dark:bg-red-950/30"
            : "",
        ].join(" ")}>
          {isThinking ? (
            <ThinkingIndicator />
          ) : message.status === "error" ? (
            <p className="text-sm text-red-600 dark:text-red-400 flex items-center gap-2">
              <span aria-hidden className="inline-block h-1.5 w-1.5 rounded-full bg-red-500 dark:bg-red-400 shrink-0" />
              {message.errorDetail ?? "Something went wrong. Please try again."}
            </p>
          ) : (
            <MarkdownRenderer
              content={message.content}
              toolCalls={message.toolCalls}
              isStreaming={message.status === "streaming"}
            />
          )}
        </div>

        {message.status !== "pending" && message.status !== "streaming" && message.content && (
          <p className="mt-1 text-[10px] text-surface-400 dark:text-surface-500">
            {formatTime(message.createdAt)}
          </p>
        )}
      </div>
    </div>
  );
}

/** Empty state with suggestion chips */
interface EmptyStateProps {
  onSuggestion: (text: string) => void;
}

function EmptyState({ onSuggestion }: EmptyStateProps): React.ReactElement {
  return (
    <div className="flex flex-col items-center justify-center h-full px-6 py-12 text-center animate-fade-in">
      {/* Logo mark */}
      <Image
        src="/bppimt-logo.webp"
        alt="B. P. Poddar Logo"
        width={72}
        height={72}
        priority={true}
        className="mb-6 rounded-full bg-white p-1 shadow-md"
      />

      <h2 className="text-xl font-bold text-surface-900 dark:text-surface-100 mb-2">
        SPARK: BPPIMT Campus Resource Assistant
      </h2>
      <p className="text-sm text-surface-500 dark:text-surface-400 max-w-sm leading-relaxed mb-8">
        Ask me anything about timetables, fees, exam schedules, or request
        a meeting with your faculty. I can also export your class schedule
        directly to your calendar.
      </p>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 w-full max-w-lg" role="list" aria-label="Suggested questions">
        {SUGGESTION_CHIPS.map(({ icon: Icon, label }) => (
          <button
            key={label}
            type="button"
            role="listitem"
            onClick={() => onSuggestion(label)}
            className={[
              "flex items-center gap-3 rounded-xl border",
              "border-surface-200 dark:border-surface-700",
              "bg-white dark:bg-surface-900",
              "px-4 py-3 text-left text-sm",
              "text-surface-700 dark:text-surface-300",
              "hover:border-brand-300 dark:hover:border-brand-600",
              "hover:bg-brand-50 dark:hover:bg-brand-950/40",
              "hover:text-brand-700 dark:hover:text-brand-300",
              "transition-all duration-150 shadow-card dark:shadow-none",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 dark:focus-visible:ring-offset-surface-950",
              "active:scale-[0.98]",
            ].join(" ")}
          >
            <Icon className="h-4 w-4 shrink-0 text-brand-400 dark:text-brand-500" aria-hidden />
            <span className="leading-snug">{label}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Scroll-to-bottom button
// ─────────────────────────────────────────────────────────────────────────────

interface ScrollToBottomButtonProps {
  visible: boolean;
  onClick: () => void;
}

function ScrollToBottomButton({ visible, onClick }: ScrollToBottomButtonProps): React.ReactElement | null {
  if (!visible) return null;
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label="Scroll to latest message"
      className={[
        "absolute bottom-4 right-4 z-10 flex h-8 w-8 items-center justify-center",
        "rounded-full bg-white dark:bg-surface-800 border border-surface-200 dark:border-surface-700 shadow-card-md dark:shadow-none",
        "text-surface-500 dark:text-surface-400 hover:text-surface-900 dark:hover:text-surface-100 hover:border-surface-300 dark:hover:border-surface-600",
        "transition-all duration-200 animate-fade-in",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500",
      ].join(" ")}
    >
      <ChevronDown className="h-4 w-4" aria-hidden />
    </button>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Main ChatInterface component
// ─────────────────────────────────────────────────────────────────────────────

export function ChatInterface(): React.ReactElement {
  const { data: session } = useSession();

  const [messages, setMessages] = React.useState<Message[]>([]);
  const [input, setInput] = React.useState("");
  const [isSubmitting, setIsSubmitting] = React.useState(false);
  const [backendConversationId, setBackendConversationId] = React.useState<string | null>(null);
  const [showScrollButton, setShowScrollButton] = React.useState(false);

  const bottomRef = React.useRef<HTMLDivElement>(null);
  const scrollRef = React.useRef<HTMLDivElement>(null);
  const textareaRef = React.useRef<HTMLTextAreaElement>(null);
  const abortRef = React.useRef<AbortController | null>(null);

  // ── Auto-resize textarea ────────────────────────────────────────────────
  const adjustTextarea = React.useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, []);

  React.useEffect(() => {
    adjustTextarea();
  }, [input, adjustTextarea]);

  // ── Auto-scroll to bottom ───────────────────────────────────────────────
  const scrollToBottom = React.useCallback((smooth = true) => {
    bottomRef.current?.scrollIntoView({ behavior: smooth ? "smooth" : "instant" });
  }, []);

  React.useEffect(() => {
    scrollToBottom();
  }, [messages.length, scrollToBottom]);

  const handleScroll = React.useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setShowScrollButton(distFromBottom > 150);
  }, []);

  // ── SSE stream consumer ─────────────────────────────────────────────────
  const submitQuery = React.useCallback(async (query: string) => {
    if (!query.trim() || isSubmitting) return;

    // Abort any previous stream
    abortRef.current?.abort();
    const abortController = new AbortController();
    abortRef.current = abortController;

    const userMessageId = generateId();
    const assistantMessageId = generateId();
    const now = new Date().toISOString();

    const userMessage: Message = {
      id: userMessageId,
      role: "user",
      content: query.trim(),
      createdAt: now,
      status: "complete",
      citations: [],
      toolCalls: [],
      errorDetail: null,
    };

    const assistantMessage: Message = {
      id: assistantMessageId,
      role: "assistant",
      content: "",
      createdAt: now,
      status: "pending",
      citations: [],
      toolCalls: [],
      errorDetail: null,
    };

    setMessages((prev) => [...prev, userMessage, assistantMessage]);
    setInput("");
    setIsSubmitting(true);

    // Scroll instantly to show the pending bubble
    setTimeout(() => scrollToBottom(false), 50);

    try {
      const response = await fetch("/api/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query: query.trim(),
          conversation_id: backendConversationId,
          context: { section: null, semester_end_date: null },
        }),
        signal: abortController.signal,
      });

      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(`HTTP ${response.status}: ${errorText}`);
      }

      if (!response.body) throw new Error("No response body");

      const reader = response.body.getReader();
      const decoder = new TextDecoder();

      // Transition to streaming state
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantMessageId ? { ...m, status: "streaming" } : m
        )
      );

      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        // Process all complete JSON lines in the buffer
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";

        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed) continue;

          let chunk: StreamChunk;
          try {
            chunk = JSON.parse(trimmed) as StreamChunk;
          } catch {
            continue; // Skip malformed lines
          }

          if (isTokenChunk(chunk)) {
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantMessageId
                  ? { ...m, content: m.content + chunk.content }
                  : m
              )
            );
          } else if (chunk.type === "tool_call") {
            const toolInvocation: ToolInvocation = {
              tool: chunk.tool,
              status: chunk.status === "running" ? "running" : "completed",
              payload: null,
            };
            setMessages((prev) =>
              prev.map((m) => {
                if (m.id !== assistantMessageId) return m;
                // Update existing invocation or add new
                const existingIdx = m.toolCalls.findIndex((tc) => tc.tool === chunk.tool);
                if (existingIdx >= 0) {
                  const updated = [...m.toolCalls];
                  updated[existingIdx] = { ...updated[existingIdx], status: toolInvocation.status };
                  return { ...m, toolCalls: updated };
                }
                return { ...m, toolCalls: [...m.toolCalls, toolInvocation] };
              })
            );
          } else if (isToolOutputChunk(chunk)) {
            setMessages((prev) =>
              prev.map((m) => {
                if (m.id !== assistantMessageId) return m;
                const existingIdx = m.toolCalls.findIndex((tc) => tc.tool === chunk.tool);
                if (existingIdx >= 0) {
                  const updated = [...m.toolCalls];
                  updated[existingIdx] = {
                    ...updated[existingIdx],
                    status: "completed",
                    payload: chunk.payload,
                  };
                  return { ...m, toolCalls: updated };
                }
                return {
                  ...m,
                  toolCalls: [...m.toolCalls, { tool: chunk.tool, status: "completed", payload: chunk.payload }],
                };
              })
            );
          } else if (isDoneChunk(chunk)) {
            setBackendConversationId(chunk.conversation_id);
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantMessageId ? { ...m, status: "complete" } : m
              )
            );
          } else if (isErrorChunk(chunk)) {
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantMessageId
                  ? { ...m, status: "error", errorDetail: chunk.message }
                  : m
              )
            );
          }
        }
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;

      const errorMessage =
        err instanceof Error ? err.message : "An unexpected error occurred.";

      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantMessageId
            ? { ...m, status: "error", errorDetail: errorMessage }
            : m
        )
      );
    } finally {
      setIsSubmitting(false);
      textareaRef.current?.focus();
    }
  }, [isSubmitting, backendConversationId, scrollToBottom]);

  // ── Keyboard handler ────────────────────────────────────────────────────
  const handleKeyDown = React.useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        void submitQuery(input);
      }
    },
    [input, submitQuery]
  );

  const handleSubmit = React.useCallback(
    (e: React.FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      void submitQuery(input);
    },
    [input, submitQuery]
  );

  const isEmpty = messages.length === 0;

  return (
    <div className="flex flex-col h-screen bg-surface-50 dark:bg-surface-950 font-sans">

      {/* ── Navigation header ─────────────────────────────────────────────── */}
      <header
        role="banner"
        className={[
          "flex items-center justify-between px-4 sm:px-6 py-3",
          "bg-white/80 dark:bg-surface-900/80 backdrop-blur-md",
          "border-b border-surface-100 dark:border-surface-800",
          "sticky top-0 z-20 h-14",
        ].join(" ")}
      >
        {/* Brand mark */}
        <div className="flex items-center gap-3" aria-label="SPARK: BPPIMT Campus Resource Assistant">
          <Image
            src="/bppimt-logo.webp"
            alt="B. P. Poddar Logo"
            width={40}
            height={40}
            className="rounded-full bg-white p-0.5 shadow-sm drop-shadow-sm"
          />
          <div className="hidden sm:block">
            <p className="text-lg font-extrabold tracking-wide uppercase text-surface-900 dark:text-surface-100 leading-tight">SPARK✨</p>
            <p className="text-[10px] text-surface-400 dark:text-surface-500 leading-tight">BPPIMT Campus Resource Assistant</p>
          </div>
        </div>

        {/* User controls */}
        {session?.user && (
          <div className="flex items-center gap-3">
            <div className="hidden sm:block text-right">
              <p className="text-xs font-medium text-surface-800 dark:text-surface-200 leading-tight">
                {session.user.name}
              </p>
              <p className="text-[10px] text-surface-400 dark:text-surface-500 leading-tight truncate max-w-[180px]">
                {session.user.email}
              </p>
            </div>

            <Avatar.Root aria-label={`User avatar for ${session.user.name ?? "user"}`}>
              <Avatar.Image
                src={session.user.image ?? undefined}
                alt={session.user.name ?? "User avatar"}
                className="h-8 w-8 rounded-xl object-cover ring-2 ring-surface-100 dark:ring-surface-800"
              />
              <Avatar.Fallback
                className="flex h-8 w-8 items-center justify-center rounded-xl bg-brand-100 dark:bg-brand-900 text-xs font-semibold text-brand-700 dark:text-brand-300 ring-2 ring-surface-100 dark:ring-surface-800"
                delayMs={300}
              >
                {getUserInitials(session.user.name)}
              </Avatar.Fallback>
            </Avatar.Root>

            {/* Theme toggle */}
            <ThemeToggle />

            <button
              type="button"
              onClick={() => void signOut({ callbackUrl: "/login" })}
              aria-label="Sign out"
              className={[
                "flex h-8 w-8 items-center justify-center rounded-xl",
                "text-surface-400 dark:text-surface-500",
                "hover:text-surface-700 dark:hover:text-surface-200",
                "hover:bg-surface-100 dark:hover:bg-surface-800",
                "transition-colors",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500",
              ].join(" ")}
            >
              <LogOut className="h-4 w-4" aria-hidden />
            </button>
          </div>
        )}
      </header>

      {/* ── Message timeline ──────────────────────────────────────────────── */}
      <main
        role="main"
        aria-label="Chat conversation"
        className="relative flex-1 min-h-0"
      >
        <div
          ref={scrollRef}
          onScroll={handleScroll}
          className="h-full overflow-y-auto"
        >
          {isEmpty ? (
            <EmptyState onSuggestion={(text) => void submitQuery(text)} />
          ) : (
            <div
              role="list"
              aria-label="Messages"
              aria-live="polite"
              aria-relevant="additions"
              className="flex flex-col gap-4 px-4 sm:px-6 py-6 max-w-3xl mx-auto w-full pb-2"
            >
              {messages.map((message) =>
                message.role === "user" ? (
                  <UserBubble key={message.id} message={message} />
                ) : (
                  <AssistantBubble
                    key={message.id}
                    message={message}
                    userInitials={getUserInitials(session?.user?.name)}
                  />
                )
              )}
              <div ref={bottomRef} aria-hidden />
            </div>
          )}
        </div>

        <ScrollToBottomButton
          visible={showScrollButton}
          onClick={() => scrollToBottom()}
        />
      </main>

      {/* ── Input area ───────────────────────────────────────────────────── */}
      <footer
        role="contentinfo"
        aria-label="Message input area"
        className="sticky bottom-0 z-20 bg-white/80 dark:bg-surface-900/80 backdrop-blur-md border-t border-surface-100 dark:border-surface-800 px-4 sm:px-6 py-3"
      >
        <form
          onSubmit={handleSubmit}
          className="flex items-end gap-3 max-w-3xl mx-auto"
          aria-label="Send a message"
        >
          <div className={[
            "flex-1 flex items-end gap-2 rounded-2xl border px-4 py-3",
            "bg-white dark:bg-surface-900",
            "transition-shadow duration-200",
            isSubmitting
              ? "border-surface-200 dark:border-surface-700 opacity-70"
              : "border-surface-200 dark:border-surface-700 focus-within:border-brand-300 dark:focus-within:border-brand-600 focus-within:shadow-[0_0_0_3px_rgba(59,103,243,0.12)] dark:focus-within:shadow-[0_0_0_3px_rgba(59,103,243,0.2)]",
          ].join(" ")}>
            <label htmlFor="chat-input" className="sr-only">
              Type a message. Press Enter to send, Shift+Enter for a new line.
            </label>
            <textarea
              id="chat-input"
              ref={textareaRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Ask about timetables, fees, faculty, or request a calendar…"
              aria-label="Chat message input"
              aria-multiline
              aria-disabled={isSubmitting}
              disabled={isSubmitting}
              rows={1}
              className={[
                "flex-1 resize-none bg-transparent text-sm",
                "text-surface-900 dark:text-surface-100",
                "placeholder:text-surface-400 dark:placeholder:text-surface-500",
                "leading-relaxed",
                "focus:outline-none",
                "disabled:cursor-not-allowed",
                "max-h-40 overflow-y-auto",
              ].join(" ")}
              style={{ height: "24px" }}
            />
          </div>

          <button
            type="submit"
            disabled={isSubmitting || !input.trim()}
            aria-label={isSubmitting ? "Sending message" : "Send message"}
            aria-disabled={isSubmitting || !input.trim()}
            className={[
              "flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl",
              "transition-all duration-200",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 dark:focus-visible:ring-offset-surface-950",
              isSubmitting || !input.trim()
                ? "bg-surface-100 dark:bg-surface-800 text-surface-300 dark:text-surface-600 cursor-not-allowed"
                : "bg-brand-600 text-white hover:bg-brand-700 active:scale-95 shadow-glow-brand",
            ].join(" ")}
          >
            {isSubmitting ? (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
            ) : (
              <Send className="h-4 w-4" aria-hidden />
            )}
          </button>
        </form>

        <p className="mt-2 text-center text-[10px] text-surface-300 dark:text-surface-600 max-w-3xl mx-auto">
          Restricted to{" "}
          <span className="font-medium text-surface-400 dark:text-surface-500">@bppimt.ac.in</span> accounts.
          AI responses may contain errors — verify with official sources.
        </p>
      </footer>
    </div>
  );
}
