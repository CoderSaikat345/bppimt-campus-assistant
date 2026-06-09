/**
 * types/chat.ts
 *
 * Strict TypeScript interfaces for the BPPIMT Campus Resource Assistant chat layer.
 *
 * These types model the Server-Sent Events (SSE) stream protocol defined in
 * routers/chat.py on the FastAPI backend:
 *
 *   {"type": "token",      "content": "…",  "conversation_id": "…"}
 *   {"type": "tool_call",  "tool": "…",     "status": "running|completed"}
 *   {"type": "tool_output","tool": "…",     "payload": ToolOutputPayload}
 *   {"type": "done",       "conversation_id": "…"}
 *   {"type": "error",      "message": "…"}
 *
 * Zero `any` declarations — every discriminated union member is fully typed.
 */

// ─────────────────────────────────────────────────────────────────────────────
// SSE stream chunk types (backend → frontend wire format)
// ─────────────────────────────────────────────────────────────────────────────

/** A single text token emitted by the LLM. */
export interface TokenChunk {
  readonly type: "token";
  readonly content: string;
  readonly conversation_id: string;
}

/** Notification that a tool has been invoked. */
export interface ToolCallChunk {
  readonly type: "tool_call";
  readonly tool: ToolName;
  readonly status: "running" | "completed";
  readonly conversation_id: string;
}

/** Structured payload returned from a tool execution. */
export interface ToolOutputChunk {
  readonly type: "tool_output";
  readonly tool: ToolName;
  readonly payload: ToolOutputPayload;
  readonly conversation_id: string;
}

/** Terminal chunk — stream is complete. */
export interface DoneChunk {
  readonly type: "done";
  readonly conversation_id: string;
}

/** Error chunk — something went wrong server-side. */
export interface ErrorChunk {
  readonly type: "error";
  readonly message: string;
  readonly conversation_id?: string;
}

/** Discriminated union of all possible SSE stream chunks. */
export type StreamChunk =
  | TokenChunk
  | ToolCallChunk
  | ToolOutputChunk
  | DoneChunk
  | ErrorChunk;

// ─────────────────────────────────────────────────────────────────────────────
// Tool names (must stay in sync with routers/chat.py tool registrations)
// ─────────────────────────────────────────────────────────────────────────────

export type ToolName =
  | "rag_query"
  | "generate_ics"
  | "check_faculty_availability";

// ─────────────────────────────────────────────────────────────────────────────
// Tool output payload variants (discriminated by `tool` field)
// ─────────────────────────────────────────────────────────────────────────────

/** Payload returned by the `generate_ics` tool. */
export interface IcsToolPayload {
  readonly tool: "generate_ics";
  readonly status: "success" | "error";
  readonly filename: string;
  /** Base64-encoded .ics file content. */
  readonly ics_base64: string;
  readonly event_count: number;
  readonly section: string;
  readonly message: string;
}

/** A single available meeting slot within the `check_faculty_availability` payload. */
export interface AvailableSlot {
  readonly start_time: string;
  readonly end_time: string;
  readonly duration_min: number;
}

/** Payload returned by the `check_faculty_availability` tool. */
export interface FacultyAvailabilityPayload {
  readonly tool: "check_faculty_availability";
  readonly status: "success" | "error";
  readonly faculty_name: string;
  readonly faculty_email: string;
  readonly requested_date: string;
  readonly has_availability: boolean;
  readonly available_slots: ReadonlyArray<AvailableSlot>;
  readonly email_subject: string;
  readonly email_body: string;
  /** RFC 6068 mailto: URI ready to open in an email client. */
  readonly mailto_uri: string;
  readonly message: string;
}

/** Payload returned by the `rag_query` tool (raw text answer). */
export interface RagQueryPayload {
  readonly tool: "rag_query";
  readonly status: "success" | "error";
  readonly answer: string;
}

/** Discriminated union of all tool output payloads. */
export type ToolOutputPayload =
  | IcsToolPayload
  | FacultyAvailabilityPayload
  | RagQueryPayload;

// ─────────────────────────────────────────────────────────────────────────────
// Citation metadata (parsed from inline [Source: …] markers in message text)
// ─────────────────────────────────────────────────────────────────────────────

export interface CitationMetadata {
  /** The full raw citation string as it appears in the text, e.g. "[Source: Handbook, p.5]" */
  readonly raw: string;
  /** Human-readable document name extracted from the citation. */
  readonly documentName: string;
  /** Optional page reference, e.g. "p.5" or "Sheet: Monday". */
  readonly pageRef: string | null;
  /** Zero-based character offset where this citation starts in the message text. */
  readonly startIndex: number;
  /** Zero-based character offset where this citation ends (exclusive). */
  readonly endIndex: number;
}

// ─────────────────────────────────────────────────────────────────────────────
// Message model (UI-level)
// ─────────────────────────────────────────────────────────────────────────────

export type MessageRole = "user" | "assistant" | "system";

export type MessageStatus =
  | "pending"     // User message submitted, awaiting first token
  | "streaming"   // Receiving tokens
  | "complete"    // Stream ended cleanly
  | "error";      // Stream ended with an error chunk

/** An active or resolved tool invocation attached to an assistant message. */
export interface ToolInvocation {
  readonly tool: ToolName;
  readonly status: "running" | "completed" | "error";
  readonly payload: ToolOutputPayload | null;
}

/**
 * A fully typed chat message as stored in the UI state.
 *
 * - `content`     : Accumulated raw text (may contain [Source: …] markers).
 * - `citations`   : Parsed CitationMetadata objects derived from content.
 * - `toolCalls`   : Tool invocations triggered during this message's generation.
 * - `status`      : Current lifecycle state of the message.
 */
export interface Message {
  readonly id: string;
  readonly role: MessageRole;
  /** Accumulated plain text from all received `token` chunks. */
  content: string;
  /** ISO-8601 timestamp of message creation. */
  readonly createdAt: string;
  status: MessageStatus;
  citations: CitationMetadata[];
  toolCalls: ToolInvocation[];
  /** Error detail string if status === "error". */
  errorDetail: string | null;
}

// ─────────────────────────────────────────────────────────────────────────────
// Chat request (frontend → backend wire format)
// ─────────────────────────────────────────────────────────────────────────────

export interface UserContext {
  section: string | null;
  semester_end_date: string | null;
}

export interface ChatRequest {
  readonly query: string;
  readonly conversation_id: string | null;
  readonly context: UserContext;
}

// ─────────────────────────────────────────────────────────────────────────────
// Conversation (collection of messages)
// ─────────────────────────────────────────────────────────────────────────────

export interface Conversation {
  readonly id: string;
  messages: Message[];
  /** The active conversation_id from the backend (set after first response). */
  backendConversationId: string | null;
}

// ─────────────────────────────────────────────────────────────────────────────
// Type guards (discriminated union narrowing helpers)
// ─────────────────────────────────────────────────────────────────────────────

export function isIcsPayload(p: ToolOutputPayload): p is IcsToolPayload {
  return p.tool === "generate_ics";
}

export function isFacultyPayload(p: ToolOutputPayload): p is FacultyAvailabilityPayload {
  return p.tool === "check_faculty_availability";
}

export function isRagPayload(p: ToolOutputPayload): p is RagQueryPayload {
  return p.tool === "rag_query";
}

export function isTokenChunk(c: StreamChunk): c is TokenChunk {
  return c.type === "token";
}

export function isToolOutputChunk(c: StreamChunk): c is ToolOutputChunk {
  return c.type === "tool_output";
}

export function isDoneChunk(c: StreamChunk): c is DoneChunk {
  return c.type === "done";
}

export function isErrorChunk(c: StreamChunk): c is ErrorChunk {
  return c.type === "error";
}
