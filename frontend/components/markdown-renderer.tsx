"use client";

/**
 * components/markdown-renderer.tsx
 *
 * Real-time markdown renderer for the BPPIMT Campus Resource Assistant.
 *
 * Capabilities:
 * ─────────────
 * 1. STREAMING TEXT: Renders markdown incrementally as tokens arrive.
 *    Text is parsed and rendered without waiting for the full message.
 *
 * 2. CITATION INTERCEPTION: Detects inline `[Source: <doc>, <ref>]` markers
 *    and replaces them with interactive tooltip badges powered by Radix UI.
 *    Example: "[Source: Handbook, p.5]" → a hoverable badge chip.
 *
 * 3. ICS DOWNLOAD CARD: When a `generate_ics` tool payload is present,
 *    renders a prominent "Download Calendar" card with event count and
 *    section info, triggering a client-side base64 → Blob download.
 *
 * 4. EMAIL DRAFT CARD: When a `check_faculty_availability` payload is present,
 *    renders a structured email draft card with:
 *      - Available slot timeline
 *      - Copy-to-clipboard email body button
 *      - Direct mailto: link button
 *
 * All interactive elements carry full ARIA attributes for accessibility.
 * Full dark mode support with WCAG-compliant contrast ratios.
 */

import * as React from "react";
import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import {
  Calendar,
  Mail,
  Download,
  Copy,
  Check,
  Clock,
  ExternalLink,
  BookOpen,
  AlertCircle,
} from "lucide-react";
import {
  type CitationMetadata,
  type FacultyAvailabilityPayload,
  type IcsToolPayload,
  type ToolInvocation,
  isFacultyPayload,
  isIcsPayload,
} from "@/types/chat";

// ─────────────────────────────────────────────────────────────────────────────
// Citation parser
// ─────────────────────────────────────────────────────────────────────────────

const CITATION_REGEX = /\[Source:\s*([^,\]]+?)(?:,\s*([^\]]+?))?\]/g;

function parseCitations(text: string): CitationMetadata[] {
  const citations: CitationMetadata[] = [];
  let match: RegExpExecArray | null;
  CITATION_REGEX.lastIndex = 0;

  while ((match = CITATION_REGEX.exec(text)) !== null) {
    citations.push({
      raw: match[0],
      documentName: match[1].trim(),
      pageRef: match[2]?.trim() ?? null,
      startIndex: match.index,
      endIndex: match.index + match[0].length,
    });
  }
  return citations;
}

/**
 * Splits a text string into segments: plain text interspersed with citation objects.
 */
type TextSegment = { kind: "text"; content: string };
type CitationSegment = { kind: "citation"; citation: CitationMetadata };
type Segment = TextSegment | CitationSegment;

function splitIntoSegments(text: string, citations: CitationMetadata[]): Segment[] {
  if (citations.length === 0) return [{ kind: "text", content: text }];

  const segments: Segment[] = [];
  let cursor = 0;

  for (const citation of citations) {
    if (cursor < citation.startIndex) {
      segments.push({ kind: "text", content: text.slice(cursor, citation.startIndex) });
    }
    segments.push({ kind: "citation", citation });
    cursor = citation.endIndex;
  }

  if (cursor < text.length) {
    segments.push({ kind: "text", content: text.slice(cursor) });
  }

  return segments;
}

// ─────────────────────────────────────────────────────────────────────────────
// Simple markdown → React (streaming-safe, no heavy parser dependency)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Minimal streaming-safe markdown renderer.
 * Handles: **bold**, *italic*, `code`, # headings, - list items, blank lines.
 * Does NOT use remark/rehype to avoid hydration issues with partial token streams.
 */
function renderMarkdownLine(line: string, key: number): React.ReactElement {
  // Headings
  const h3 = line.match(/^### (.+)/);
  if (h3) return <h3 key={key} className="text-sm font-semibold text-surface-900 dark:text-surface-100 mt-3 mb-1">{renderInline(h3[1])}</h3>;
  const h2 = line.match(/^## (.+)/);
  if (h2) return <h2 key={key} className="text-base font-semibold text-surface-900 dark:text-surface-100 mt-4 mb-1">{renderInline(h2[1])}</h2>;
  const h1 = line.match(/^# (.+)/);
  if (h1) return <h1 key={key} className="text-lg font-bold text-surface-900 dark:text-surface-100 mt-4 mb-2">{renderInline(h1[1])}</h1>;

  // List items
  const li = line.match(/^[-*+] (.+)/);
  if (li) {
    return (
      <li key={key} className="flex gap-2 items-start text-sm text-surface-700 dark:text-surface-300 leading-relaxed">
        <span aria-hidden className="mt-1.5 h-1.5 w-1.5 rounded-full bg-brand-400 dark:bg-brand-500 shrink-0" />
        <span>{renderInline(li[1])}</span>
      </li>
    );
  }

  // Blank line
  if (line.trim() === "") return <div key={key} className="h-2" aria-hidden />;

  // Default paragraph
  return <p key={key} className="text-sm text-surface-700 dark:text-surface-300 leading-relaxed">{renderInline(line)}</p>;
}

function renderInline(text: string): React.ReactNode {
  // Split on bold, italic, and inline code patterns
  const parts = text.split(/(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)/g);
  return parts.map((part, i) => {
    if (part.startsWith("**") && part.endsWith("**"))
      return <strong key={i} className="font-semibold text-surface-900 dark:text-surface-100">{part.slice(2, -2)}</strong>;
    if (part.startsWith("*") && part.endsWith("*"))
      return <em key={i} className="italic">{part.slice(1, -1)}</em>;
    if (part.startsWith("`") && part.endsWith("`"))
      return (
        <code key={i} className="px-1.5 py-0.5 rounded bg-surface-100 dark:bg-surface-800 font-mono text-xs text-brand-700 dark:text-brand-300 border border-surface-200 dark:border-surface-700">
          {part.slice(1, -1)}
        </code>
      );
    return part;
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Citation tooltip badge
// ─────────────────────────────────────────────────────────────────────────────

interface CitationBadgeProps {
  citation: CitationMetadata;
  index: number;
}

function CitationBadge({ citation, index }: CitationBadgeProps): React.ReactElement {
  return (
    <TooltipPrimitive.Provider delayDuration={150}>
      <TooltipPrimitive.Root>
        <TooltipPrimitive.Trigger asChild>
          <button
            type="button"
            aria-label={`Citation ${index + 1}: ${citation.documentName}${citation.pageRef ? `, ${citation.pageRef}` : ""}`}
            aria-describedby={`citation-tooltip-${index}`}
            className={[
              "inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full",
              "bg-brand-50 dark:bg-brand-950/50 border border-brand-200 dark:border-brand-800 text-brand-700 dark:text-brand-300",
              "text-[10px] font-medium leading-none cursor-pointer",
              "transition-colors hover:bg-brand-100 dark:hover:bg-brand-900/50 hover:border-brand-300 dark:hover:border-brand-700",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500",
            ].join(" ")}
          >
            <BookOpen className="h-2.5 w-2.5" aria-hidden />
            <span>{index + 1}</span>
          </button>
        </TooltipPrimitive.Trigger>

        <TooltipPrimitive.Portal>
          <TooltipPrimitive.Content
            id={`citation-tooltip-${index}`}
            role="tooltip"
            side="top"
            sideOffset={6}
            className={[
              "z-50 max-w-xs rounded-xl border",
              "border-surface-200 dark:border-surface-700",
              "bg-white dark:bg-surface-800",
              "px-3 py-2.5 shadow-card-md dark:shadow-none",
              "text-xs leading-relaxed animate-fade-in",
            ].join(" ")}
          >
            <div className="flex items-start gap-2">
              <BookOpen className="h-3.5 w-3.5 text-brand-500 mt-0.5 shrink-0" aria-hidden />
              <div>
                <p className="font-semibold text-surface-900 dark:text-surface-100">{citation.documentName}</p>
                {citation.pageRef && (
                  <p className="text-surface-500 dark:text-surface-400 mt-0.5">{citation.pageRef}</p>
                )}
              </div>
            </div>
            <TooltipPrimitive.Arrow className="fill-white dark:fill-surface-800 drop-shadow-sm" />
          </TooltipPrimitive.Content>
        </TooltipPrimitive.Portal>
      </TooltipPrimitive.Root>
    </TooltipPrimitive.Provider>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// ICS Download Card
// ─────────────────────────────────────────────────────────────────────────────

interface IcsCardProps {
  payload: IcsToolPayload;
}

function IcsCard({ payload }: IcsCardProps): React.ReactElement {
  const [downloaded, setDownloaded] = React.useState(false);

  const handleDownload = React.useCallback(() => {
    try {
      const bytes = Uint8Array.from(atob(payload.ics_base64), (c) => c.charCodeAt(0));
      const blob = new Blob([bytes], { type: "text/calendar;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = payload.filename;
      anchor.click();
      URL.revokeObjectURL(url);
      setDownloaded(true);
      setTimeout(() => setDownloaded(false), 3000);
    } catch {
      // Silently ignore — browser may block programmatic downloads in some contexts
    }
  }, [payload.ics_base64, payload.filename]);

  return (
    <div
      role="region"
      aria-label="Downloadable calendar file"
      className={[
        "mt-3 rounded-2xl border border-brand-100 dark:border-brand-800/50",
        "bg-gradient-to-br from-brand-50 to-white dark:from-brand-950/30 dark:to-surface-900",
        "p-4 shadow-card dark:shadow-none",
        "animate-slide-up",
      ].join(" ")}
    >
      <div className="flex items-start gap-3">
        <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-brand-500 shadow-glow-brand shrink-0">
          <Calendar className="h-5 w-5 text-white" aria-hidden />
        </div>

        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold text-surface-900 dark:text-surface-100">
            Calendar Ready — {payload.section}
          </p>
          <p className="mt-0.5 text-xs text-surface-500 dark:text-surface-400">
            {payload.event_count} recurring weekly lecture{payload.event_count !== 1 ? "s" : ""}
            {" · "}
            <span className="font-mono">{payload.filename}</span>
          </p>
          <p className="mt-1.5 text-xs text-surface-600 dark:text-surface-400 leading-relaxed">{payload.message}</p>
        </div>
      </div>

      <div className="mt-3 pt-3 border-t border-brand-100 dark:border-brand-800/50">
        <button
          type="button"
          onClick={handleDownload}
          aria-label={`Download ${payload.filename} — ${payload.event_count} calendar events`}
          aria-pressed={downloaded}
          className={[
            "inline-flex w-full items-center justify-center gap-2 rounded-xl px-4 py-2.5",
            "text-sm font-medium transition-all duration-200",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 dark:focus-visible:ring-offset-surface-900",
            downloaded
              ? "bg-emerald-500 text-white cursor-default"
              : "bg-brand-600 text-white hover:bg-brand-700 active:scale-[0.98] shadow-glow-brand",
          ].join(" ")}
        >
          {downloaded ? (
            <>
              <Check className="h-4 w-4" aria-hidden />
              <span>Downloaded!</span>
            </>
          ) : (
            <>
              <Download className="h-4 w-4" aria-hidden />
              <span>Download .ics Calendar</span>
            </>
          )}
        </button>

        <p className="mt-2 text-center text-[10px] text-surface-400 dark:text-surface-500">
          Import into Google Calendar, Outlook, or Apple Calendar
        </p>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Faculty / Email Draft Card
// ─────────────────────────────────────────────────────────────────────────────

interface FacultyCardProps {
  payload: FacultyAvailabilityPayload;
}

function FacultyCard({ payload }: FacultyCardProps): React.ReactElement {
  const [copied, setCopied] = React.useState(false);
  const bodyRef = React.useRef<HTMLDivElement>(null);

  const handleCopy = React.useCallback(async () => {
    try {
      await navigator.clipboard.writeText(payload.email_body);
      setCopied(true);
      setTimeout(() => setCopied(false), 2500);
    } catch {
      // Fallback: select text for manual copy
      if (bodyRef.current) {
        const selection = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(bodyRef.current);
        selection?.removeAllRanges();
        selection?.addRange(range);
      }
    }
  }, [payload.email_body]);

  return (
    <div
      role="region"
      aria-label={`Meeting request draft for ${payload.faculty_name}`}
      className="mt-3 rounded-2xl border border-surface-200 dark:border-surface-700 bg-white dark:bg-surface-900 shadow-card dark:shadow-none overflow-hidden animate-slide-up"
    >
      {/* Header */}
      <div className="flex items-center gap-3 bg-gradient-to-r from-surface-50 to-white dark:from-surface-800 dark:to-surface-900 px-4 py-3 border-b border-surface-100 dark:border-surface-800">
        <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-surface-800 dark:bg-surface-700 shrink-0">
          <Mail className="h-4 w-4 text-white" aria-hidden />
        </div>
        <div className="min-w-0">
          <p className="text-sm font-semibold text-surface-900 dark:text-surface-100 truncate">
            Meeting Request — {payload.faculty_name}
          </p>
          <p className="text-xs text-surface-500 dark:text-surface-400">{payload.requested_date}</p>
        </div>
        {payload.has_availability ? (
          <span
            role="status"
            aria-label={`${payload.available_slots.length} slots available`}
            className="ml-auto shrink-0 inline-flex items-center gap-1 rounded-full bg-emerald-50 dark:bg-emerald-950/40 border border-emerald-200 dark:border-emerald-800 px-2 py-0.5 text-[10px] font-medium text-emerald-700 dark:text-emerald-400"
          >
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" aria-hidden />
            {payload.available_slots.length} slot{payload.available_slots.length !== 1 ? "s" : ""} free
          </span>
        ) : (
          <span
            role="status"
            aria-label="No availability found"
            className="ml-auto shrink-0 inline-flex items-center gap-1 rounded-full bg-amber-50 dark:bg-amber-950/40 border border-amber-200 dark:border-amber-800 px-2 py-0.5 text-[10px] font-medium text-amber-700 dark:text-amber-400"
          >
            <AlertCircle className="h-3 w-3" aria-hidden />
            No gaps
          </span>
        )}
      </div>

      {/* Available slots */}
      {payload.has_availability && payload.available_slots.length > 0 && (
        <div className="px-4 py-3 border-b border-surface-100 dark:border-surface-800">
          <p className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-surface-400 dark:text-surface-500">
            Available Windows
          </p>
          <div className="flex flex-wrap gap-2" role="list" aria-label="Available meeting slots">
            {payload.available_slots.map((slot, i) => (
              <div
                key={i}
                role="listitem"
                className="inline-flex items-center gap-1.5 rounded-lg bg-surface-50 dark:bg-surface-800 border border-surface-200 dark:border-surface-700 px-2.5 py-1.5"
              >
                <Clock className="h-3 w-3 text-surface-400 dark:text-surface-500" aria-hidden />
                <span className="text-xs font-medium text-surface-700 dark:text-surface-300">
                  {slot.start_time}–{slot.end_time}
                </span>
                <span className="text-[10px] text-surface-400 dark:text-surface-500">
                  ({slot.duration_min}m)
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Email subject */}
      <div className="px-4 pt-3 pb-0">
        <p className="text-[10px] font-semibold uppercase tracking-wider text-surface-400 dark:text-surface-500 mb-1">
          Subject
        </p>
        <p className="text-xs text-surface-700 dark:text-surface-300 font-medium truncate">{payload.email_subject}</p>
      </div>

      {/* Email body */}
      <div className="px-4 pt-3">
        <p className="text-[10px] font-semibold uppercase tracking-wider text-surface-400 dark:text-surface-500 mb-1.5">
          Email Draft
        </p>
        <div
          ref={bodyRef}
          role="document"
          aria-label="Email draft body"
          className={[
            "max-h-44 overflow-y-auto rounded-xl",
            "bg-surface-50 dark:bg-surface-800",
            "border border-surface-100 dark:border-surface-700",
            "px-3 py-2.5 text-xs text-surface-700 dark:text-surface-300 leading-relaxed whitespace-pre-wrap",
            "font-mono selection:bg-brand-100 dark:selection:bg-brand-900",
            "scrollbar-thin scrollbar-thumb-surface-200",
          ].join(" ")}
          tabIndex={0}
        >
          {payload.email_body}
        </div>
      </div>

      {/* Action buttons */}
      <div className="flex gap-2 px-4 py-3">
        <button
          type="button"
          onClick={handleCopy}
          aria-label={copied ? "Email draft copied to clipboard" : "Copy email draft to clipboard"}
          aria-pressed={copied}
          className={[
            "flex-1 inline-flex items-center justify-center gap-2 rounded-xl",
            "px-3 py-2 text-xs font-medium transition-all duration-200",
            "border focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 dark:focus-visible:ring-offset-surface-900",
            copied
              ? "bg-emerald-50 dark:bg-emerald-950/40 border-emerald-200 dark:border-emerald-800 text-emerald-700 dark:text-emerald-400"
              : "bg-surface-50 dark:bg-surface-800 border-surface-200 dark:border-surface-700 text-surface-700 dark:text-surface-300 hover:bg-surface-100 dark:hover:bg-surface-700",
          ].join(" ")}
        >
          {copied ? (
            <><Check className="h-3.5 w-3.5" aria-hidden /><span>Copied!</span></>
          ) : (
            <><Copy className="h-3.5 w-3.5" aria-hidden /><span>Copy Draft</span></>
          )}
        </button>

        <a
          href={payload.mailto_uri}
          aria-label={`Open email to ${payload.faculty_email} in your mail client`}
          rel="noopener noreferrer"
          className={[
            "flex-1 inline-flex items-center justify-center gap-2 rounded-xl",
            "px-3 py-2 text-xs font-medium",
            "bg-brand-600 text-white border border-brand-600",
            "hover:bg-brand-700 transition-colors",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 dark:focus-visible:ring-offset-surface-900",
          ].join(" ")}
        >
          <ExternalLink className="h-3.5 w-3.5" aria-hidden />
          <span>Open in Mail</span>
        </a>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Main MarkdownRenderer component
// ─────────────────────────────────────────────────────────────────────────────

interface MarkdownRendererProps {
  content: string;
  toolCalls: ToolInvocation[];
  isStreaming?: boolean;
}

export function MarkdownRenderer({
  content,
  toolCalls,
  isStreaming = false,
}: MarkdownRendererProps): React.ReactElement {
  const citations = React.useMemo(() => parseCitations(content), [content]);
  const segments = React.useMemo(
    () => splitIntoSegments(content, citations),
    [content, citations]
  );

  // Collect completed tool outputs
  const completedTools = toolCalls.filter(
    (t) => t.status === "completed" && t.payload !== null
  );

  const icsPayload = completedTools.find(
    (t) => t.payload !== null && isIcsPayload(t.payload)
  )?.payload as IcsToolPayload | undefined;

  const facultyPayload = completedTools.find(
    (t) => t.payload !== null && isFacultyPayload(t.payload)
  )?.payload as FacultyAvailabilityPayload | undefined;

  // Build rendered line elements from segments
  const lineElements = React.useMemo(() => {
    // Join all text segments and split by newlines for line-by-line rendering
    const fullText = segments
      .map((s) => (s.kind === "text" ? s.content : s.kind === "citation" ? "" : ""))
      .join("");

    const lines = fullText.split("\n");
    let citationCounter = 0;

    // Re-walk segments to interleave citation badges at correct positions
    return lines.map((line, lineIdx) => {
      const rendered = renderMarkdownLine(line, lineIdx);

      // Find citation badges that fall within this line's approximate offset
      // (simplified: place badges after the line they appear on)
      const lineCitations: CitationMetadata[] = [];
      if (citations.length > 0 && content.split("\n")[lineIdx]?.includes("[Source:")) {
        const lineContent = content.split("\n")[lineIdx];
        CITATION_REGEX.lastIndex = 0;
        let m: RegExpExecArray | null;
        while ((m = CITATION_REGEX.exec(lineContent ?? "")) !== null) {
          lineCitations.push({
            raw: m[0],
            documentName: m[1].trim(),
            pageRef: m[2]?.trim() ?? null,
            startIndex: m.index,
            endIndex: m.index + m[0].length,
          });
        }
      }

      if (lineCitations.length === 0) return rendered;

      return (
        <React.Fragment key={lineIdx}>
          {rendered}
          <span className="inline-flex gap-0.5 ml-1" aria-label="Citations">
            {lineCitations.map((cit, i) => (
              <CitationBadge key={i} citation={cit} index={citationCounter++} />
            ))}
          </span>
        </React.Fragment>
      );
    });
  }, [segments, citations, content]);

  // Detect if content has a list (to wrap in <ul>)
  const hasListItems = content.split("\n").some((l) => /^[-*+] /.test(l));

  return (
    <div className="flex flex-col gap-0.5">
      {/* Rendered markdown content */}
      <div className={hasListItems ? "space-y-1" : "space-y-1"}>
        {hasListItems ? (
          <ul className="space-y-1.5 list-none" role="list">
            {lineElements}
          </ul>
        ) : (
          <div className="space-y-1">{lineElements}</div>
        )}
      </div>

      {/* Streaming cursor */}
      {isStreaming && (
        <span
          aria-hidden
          className="inline-block h-4 w-0.5 bg-brand-500 rounded-full animate-pulse ml-0.5"
        />
      )}

      {/* Tool output cards — rendered below message text */}
      {icsPayload && <IcsCard payload={icsPayload} />}
      {facultyPayload && <FacultyCard payload={facultyPayload} />}
    </div>
  );
}
