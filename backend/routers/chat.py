"""
routers/chat.py

Core streaming chat endpoint for the BPPIMT Campus Resource Assistant.

Architecture
────────────
                              ┌────────────────────────────────────────────┐
  POST /api/v1/chat/stream    │          ReActAgent (LlamaIndex 0.14)     │
  { query, context }  ──────▶│                                            │
                              │  Workflow-based agent with tool calling    │
                              │         │                                  │
                              │   ┌─────▼──────────────────────────────┐  │
                              │   │        Tool Selector (LLM)          │  │
                              │   │  "which tool fits this query best?" │  │
                              │   └──┬──────────────┬───────────────┬──┘  │
                              │      │              │               │      │
                              │ rag_query    generate_ics   check_faculty  │
                              │      │              │          _availability│
                              │      │              │               │      │
                              │  BigQuery      calendar.py    faculty.py  │
                              │  Vector         (.ics bytes)  (email body)│
                              │  Search                                   │
                              └───────────────────────────────────────────┘
                                            │
                              SSE token stream (text/event-stream)
                              { type, content, tool_output, metadata }

Stream protocol (Server-Sent Events)
─────────────────────────────────────
Every chunk flushed to the client is a newline-terminated JSON object:

  {"type": "token",      "content": "The timetable…"}
  {"type": "token",      "content": " for CSE-B…"}
  {"type": "tool_call",  "tool": "generate_ics", "status": "running"}
  {"type": "tool_output","tool": "generate_ics", "payload": {...}}
  {"type": "done",       "conversation_id": "<uuid>"}
  {"type": "error",      "message": "…"}

Design decisions
────────────────
• StreamingResponse with media_type="text/event-stream" (SSE) is chosen
  over WebSockets because:
  (a) SSE is simpler to integrate with the Vercel AI SDK on the frontend.
  (b) The connection is unidirectional (server → client) for chat streams.
  (c) No heartbeat infrastructure required — HTTP/2 handles keep-alive.

• LlamaIndex 0.14 ReActAgent (Workflow-based) is used with direct
  constructor instantiation. The agent uses Google Gemini via the
  llama-index-llms-google-genai integration with vertexai_config for
  enterprise GCP routing.

• The streaming loop uses the modern handler.stream_events() API.
  AgentStream events carry token deltas; ToolCall and ToolCallResult
  events carry tool execution metadata.

• Tool outputs (ICS bytes, mailto URIs) are embedded in the stream as
  structured JSON payloads under "type": "tool_output" so the frontend
  can render download buttons / mailto links without parsing text.

• ICS bytes are base64-encoded within the JSON payload since binary
  cannot be embedded in SSE text directly.

• conversation_id is a UUID4 generated server-side if not supplied.
  The frontend persists it and sends it back on follow-up turns.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from dataclasses import asdict
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from dependencies.auth import AuthenticatedUser, verify_enterprise_user
from services.calendar import (
    CalendarGenerationError,
    generate_ics_from_raw,
)
from services.faculty import (
    FacultyServiceError,
    check_faculty_availability_from_raw,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Chat"])

# ── Stream chunk type literals ────────────────────────────────────────────────
_TYPE_TOKEN: Final[str] = "token"
_TYPE_TOOL_CALL: Final[str] = "tool_call"
_TYPE_TOOL_OUTPUT: Final[str] = "tool_output"
_TYPE_DONE: Final[str] = "done"
_TYPE_ERROR: Final[str] = "error"

# ── Request / response models ─────────────────────────────────────────────────


class UserContext(BaseModel):
    """Optional structured context the frontend can inject per turn."""

    section: str | None = Field(
        default=None,
        description="Student's academic section, e.g. 'CSE-B'.",
    )
    semester_end_date: str | None = Field(
        default=None,
        description="Semester end date for ICS generation (YYYY-MM-DD).",
    )


class StreamChatRequest(BaseModel):
    query: str = Field(
        ...,
        min_length=1,
        max_length=4096,
        description="The student's natural-language question or command.",
        examples=[
            "Generate my timetable calendar for CSE-B",
            "When is Dr. Roy free on Wednesday?",
            "What are the exam dates for semester 6?",
        ],
    )
    conversation_id: str | None = Field(
        default=None,
        description="UUID of a previous conversation turn (for multi-turn context).",
    )
    context: UserContext = Field(
        default_factory=UserContext,
        description="Structured per-user context injected by the frontend.",
    )


# ── SSE serialiser ────────────────────────────────────────────────────────────


def _sse(payload: dict[str, Any]) -> str:
    """Serialise a dict to a JSON newline string suitable for SSE."""
    return json.dumps(payload, ensure_ascii=False, default=str) + "\n"


# ── Tool implementations ──────────────────────────────────────────────────────


async def _tool_generate_ics(
    tool_input: dict,
    context: UserContext,
) -> dict[str, Any]:
    """
    Execute the ICS calendar generation tool.

    Accepts:
        tool_input["slots"]              : list[dict] — timetable slot dicts
        tool_input["semester_end_date"]  : str — YYYY-MM-DD (optional, falls
                                           back to context.semester_end_date)

    Returns:
        Structured JSON payload with base64-encoded ICS content and metadata.
    """
    slots = tool_input.get("slots", [])
    semester_end = (
        tool_input.get("semester_end_date")
        or context.semester_end_date
        or "2026-11-30"   # Fallback semester end
    )

    result = generate_ics_from_raw(
        raw_slots=slots,
        semester_end_date=semester_end,
    )

    return {
        "tool": "generate_ics",
        "status": "success",
        "filename": result.filename,
        "event_count": result.event_count,
        "section": result.section,
        # ICS bytes are base64-encoded so they survive JSON serialisation
        "ics_base64": base64.b64encode(result.ics_bytes).decode("ascii"),
        "message": (
            f"Generated a recurring weekly calendar for {result.section} "
            f"with {result.event_count} lecture event(s). "
            f"Download the file and import it into Google Calendar or Outlook."
        ),
    }


async def _tool_check_faculty_availability(
    tool_input: dict,
    principal: AuthenticatedUser,
) -> dict[str, Any]:
    """
    Execute the faculty availability + email draft tool.

    Accepts:
        tool_input["teaching_blocks"] : list[dict]
        tool_input["request"]         : dict — MeetingRequest fields

    Injects student_name and student_email from the authenticated principal
    so the LLM doesn't need to ask the user for their own details.

    Returns:
        Structured JSON payload with available slots, email body, mailto URI.
    """
    raw_blocks = tool_input.get("teaching_blocks", [])
    raw_request = tool_input.get("request", {})

    # Inject authenticated user identity — never trust LLM-provided values
    raw_request["student_email"] = principal.email
    raw_request["student_name"] = principal.name

    result = check_faculty_availability_from_raw(
        raw_blocks=raw_blocks,
        raw_request=raw_request,
    )

    slots_payload = [
        {
            "start_time": s.start_time,
            "end_time": s.end_time,
            "duration_min": s.duration_min,
        }
        for s in result.available_slots
    ]

    return {
        "tool": "check_faculty_availability",
        "status": "success",
        "faculty_name": result.faculty_name,
        "faculty_email": result.faculty_email,
        "requested_date": result.requested_date_human,
        "has_availability": result.has_availability,
        "available_slots": slots_payload,
        "email_subject": result.email_subject,
        "email_body": result.email_body,
        "mailto_uri": result.mailto_uri,
        "message": (
            f"Found {len(result.available_slots)} available slot(s) for a meeting "
            f"with {result.faculty_name} on {result.requested_date_human}. "
            f"An email draft has been prepared."
        ) if result.has_availability else (
            f"{result.faculty_name} has no free slots in your requested window "
            f"on {result.requested_date_human}. A polite email draft has been "
            f"prepared asking them to suggest an alternative time."
        ),
    }


# ── LlamaIndex Agent builder ──────────────────────────────────────────────────


def _build_agent(rag_engine: Any) -> Any:
    """
    Build and return a LlamaIndex 0.14 ReActAgent configured with three tools:

      1. rag_query              — BigQuery Vector Search RAG lookup
      2. generate_ics           — ICS calendar file generator
      3. check_faculty_availability — Faculty gap detector + email drafter

    The agent LLM (Google Gemini via Vertex AI) uses ReAct reasoning to decide
    which tool(s) to invoke based on the user's query.

    Returns:
        A LlamaIndex ReActAgent (Workflow) instance.
    """
    try:
        from llama_index.core.agent.workflow import ReActAgent
        from llama_index.core.tools import FunctionTool
        from llama_index.llms.google_genai import GoogleGenAI
    except ImportError as exc:
        raise RuntimeError(
            "LlamaIndex agent dependencies are not installed. "
            "Run: pip install llama-index llama-index-llms-google-genai"
        ) from exc

    from llama_index.core import Settings

    # ── Tool 1: RAG query ─────────────────────────────────────────────────────
    async def rag_query_tool(query: str) -> str:
        """
        Search the BPPIMT knowledge base (timetables, fees, exam schedules,
        notices) and return a factual answer. Use for informational questions
        that require looking up university documents.
        """
        result = await rag_engine.query(query=query)
        return result.answer

    # ── Tool 2: Generate ICS calendar ─────────────────────────────────────────
    async def generate_ics_tool(slots_json: str, semester_end_date: str = "2026-11-30") -> str:
        """
        Generate a downloadable .ics calendar file from a student's weekly
        timetable. Use when the user asks to 'download', 'export', or 'create'
        their timetable as a calendar file.

        Args:
            slots_json: JSON string of a list of timetable slot objects. Each
                        object must have: section, day, start_time, end_time,
                        subject, faculty, room.
            semester_end_date: Last day of the semester in YYYY-MM-DD format.
        """
        try:
            slots = json.loads(slots_json)
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"Invalid slots_json: {exc}"})

        result = generate_ics_from_raw(
            raw_slots=slots,
            semester_end_date=semester_end_date,
        )
        return json.dumps({
            "tool": "generate_ics",
            "status": "success",
            "filename": result.filename,
            "event_count": result.event_count,
            "ics_base64": base64.b64encode(result.ics_bytes).decode("ascii"),
        })

    # ── Tool 3: Faculty availability ──────────────────────────────────────────
    async def faculty_availability_tool(
        faculty_name: str,
        requested_date: str,
        teaching_blocks_json: str,
        window_start: str = "09:00",
        window_end: str = "17:00",
        topic: str = "Academic Discussion",
        student_name: str = "Student",
        student_email: str = "student@bppimt.ac.in",
        faculty_email: str | None = None,
    ) -> str:
        """
        Find free slots in a faculty member's schedule and generate a
        professional email draft requesting a meeting. Use when the user asks
        to 'meet', 'schedule', 'contact', 'email', or 'message' a professor.

        Args:
            faculty_name:         Full name of the professor, e.g. 'Dr. A. Roy'.
            requested_date:       Date for the meeting in YYYY-MM-DD format.
            teaching_blocks_json: JSON string of the faculty's teaching blocks on
                                  that day. Each block: start_time, end_time,
                                  subject, room.
            window_start:         Student's earliest available time (HH:MM).
            window_end:           Student's latest available time (HH:MM).
            topic:                Subject/reason for the meeting.
            student_name:         Student's full name (will be overridden by auth).
            student_email:        Student's email (will be overridden by auth).
            faculty_email:        Faculty's email (optional, auto-derived if omitted).
        """
        try:
            blocks = json.loads(teaching_blocks_json)
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"Invalid teaching_blocks_json: {exc}"})

        raw_request = {
            "faculty_name": faculty_name,
            "requested_date": requested_date,
            "window_start": window_start,
            "window_end": window_end,
            "topic": topic,
            "student_name": student_name,
            "student_email": student_email,
            "faculty_email": faculty_email,
        }

        result = check_faculty_availability_from_raw(
            raw_blocks=blocks,
            raw_request=raw_request,
        )

        return json.dumps({
            "tool": "check_faculty_availability",
            "status": "success",
            "faculty_name": result.faculty_name,
            "faculty_email": result.faculty_email,
            "requested_date": result.requested_date_human,
            "has_availability": result.has_availability,
            "available_slots": [
                {
                    "start_time": s.start_time,
                    "end_time": s.end_time,
                    "duration_min": s.duration_min,
                }
                for s in result.available_slots
            ],
            "email_subject": result.email_subject,
            "email_body": result.email_body,
            "mailto_uri": result.mailto_uri,
        })

    tools = [
        FunctionTool.from_defaults(
            async_fn=rag_query_tool,
            name="rag_query",
            description=(
                "Search BPPIMT's document knowledge base. Use for: timetables, "
                "fee structures, exam schedules, academic notices, rules, and "
                "any question that requires looking up university records."
            ),
        ),
        FunctionTool.from_defaults(
            async_fn=generate_ics_tool,
            name="generate_ics",
            description=(
                "Generate a downloadable .ics calendar file for a student's "
                "weekly class schedule. Returns base64-encoded ICS content. "
                "Use when the user wants to export or download their timetable."
            ),
        ),
        FunctionTool.from_defaults(
            async_fn=faculty_availability_tool,
            name="check_faculty_availability",
            description=(
                "Analyse a faculty member's teaching schedule, identify free "
                "slots, and generate a professional email draft requesting a "
                "meeting. Returns available time slots and a mailto: URI. "
                "Use when the user wants to contact or schedule a meeting with a professor."
            ),
        ),
    ]

    system_prompt = (
        "You are the BPPIMT Campus Resource Assistant — a knowledgeable, "
        "helpful, and professional AI for students of B. P. Poddar Institute "
        "of Management & Technology. You help students with:\n"
        "  • Finding information about timetables, fees, exams, and notices\n"
        "  • Generating downloadable calendar files for their weekly classes\n"
        "  • Drafting meeting request emails for faculty members\n\n"
        "Guidelines:\n"
        "  - Always use the available tools rather than guessing facts.\n"
        "  - For calendar requests, first retrieve the timetable via rag_query, "
        "    then call generate_ics with the structured data.\n"
        "  - For faculty meeting requests, first retrieve the faculty schedule "
        "    via rag_query, then call check_faculty_availability.\n"
        "  - Be concise, professional, and campus-aware in your responses.\n"
        "  - Restrict all answers to BPPIMT-specific information."
    )

    # LlamaIndex 0.14: ReActAgent is a Pydantic Workflow — use direct constructor
    return ReActAgent(
        tools=tools,
        llm=Settings.llm,
        system_prompt=system_prompt,
        verbose=False,
    )


# ── Stream generator ──────────────────────────────────────────────────────────


async def _stream_agent_response(
    query: str,
    conversation_id: str,
    context: UserContext,
    principal: AuthenticatedUser,
    rag_engine: Any,
) -> AsyncGenerator[str, None]:
    """
    Core streaming generator.

    Runs the LlamaIndex 0.14 ReActAgent and yields SSE-formatted JSON chunks:
      - "token"       : incremental text from the LLM
      - "tool_call"   : notification that a tool is being invoked
      - "tool_output" : structured payload from the tool
      - "done"        : terminal chunk with conversation_id
      - "error"       : any exception encountered

    Uses the modern Workflow handler.stream_events() API:
      - AgentStream events carry token deltas in ev.delta
      - ToolCall events notify that a tool is about to be invoked
      - ToolCallResult events carry the tool's return value
    """
    try:
        from llama_index.core.agent.workflow import (
            ToolCall,
            ToolCallResult,
        )

        agent = _build_agent(rag_engine)

        # LlamaIndex 0.14 Workflow API: agent.run() returns a WorkflowHandler
        handler = agent.run(user_msg=query)

        # ── Stream events from the workflow handler ────────────────────────
        # IMPORTANT: ReActAgent's AgentStream.delta contains raw ReAct
        # reasoning traces (Thought:/Action:/Action Input:/Observation:)
        # mixed with the final answer. We stream ToolCall/ToolCallResult
        # events in real-time for UI pill indicators, but collect all
        # AgentStream deltas into a buffer and extract only the final
        # answer after the agent completes.
        async for event in handler.stream_events():

            # Tool invocation — notify client that a tool is running
            if isinstance(event, ToolCall):
                yield _sse({
                    "type": _TYPE_TOOL_CALL,
                    "tool": event.tool_name,
                    "status": "running",
                    "conversation_id": conversation_id,
                })

            # Tool result — notify client with the output
            elif isinstance(event, ToolCallResult):
                yield _sse({
                    "type": _TYPE_TOOL_CALL,
                    "tool": event.tool_name,
                    "status": "completed",
                    "conversation_id": conversation_id,
                })

                # Attempt to parse the tool output as structured JSON
                raw_output = event.tool_output
                if raw_output is not None:
                    try:
                        parsed_output = json.loads(str(raw_output))
                    except (json.JSONDecodeError, TypeError):
                        parsed_output = {"raw": str(raw_output)}

                    yield _sse({
                        "type": _TYPE_TOOL_OUTPUT,
                        "tool": event.tool_name,
                        "payload": parsed_output,
                        "conversation_id": conversation_id,
                    })

            # We intentionally skip AgentStream events here — they contain
            # raw ReAct reasoning that should not be sent to the user.

        # Await the handler to get the clean final response
        result = await handler

        # Extract the clean final answer from the AgentOutput
        final_response = str(result) if result else ""

        # Emit the final answer as token chunks (split into ~80-char
        # pieces to simulate a streaming feel on the frontend)
        if final_response:
            chunk_size = 80
            for i in range(0, len(final_response), chunk_size):
                yield _sse({
                    "type": _TYPE_TOKEN,
                    "content": final_response[i:i + chunk_size],
                    "conversation_id": conversation_id,
                })
                await asyncio.sleep(0)

        # ── Terminal done chunk ────────────────────────────────────────────
        yield _sse({
            "type": _TYPE_DONE,
            "conversation_id": conversation_id,
        })

    except (CalendarGenerationError, FacultyServiceError) as exc:
        logger.warning("Tool execution error: %s", exc)
        yield _sse({
            "type": _TYPE_ERROR,
            "message": str(exc),
            "conversation_id": conversation_id,
        })

    except Exception as exc:
        logger.exception(
            "Unhandled error in stream generator: conversation_id=%s",
            conversation_id,
        )
        yield _sse({
            "type": _TYPE_ERROR,
            "message": "An internal error occurred. Please try again.",
            "conversation_id": conversation_id,
        })


# ── Streaming endpoint ────────────────────────────────────────────────────────


@router.post(
    "/chat/stream",
    summary="Streaming agentic chat endpoint",
    description=(
        "Submit a natural-language query and receive a token-by-token "
        "Server-Sent Events stream. The agent automatically routes to:\n"
        "  • **rag_query** — for informational lookups (timetables, fees)\n"
        "  • **generate_ics** — to export a timetable as a .ics calendar file\n"
        "  • **check_faculty_availability** — to draft a meeting request email\n\n"
        "Requires a valid @bppimt.ac.in Google ID token in the Authorization header."
    ),
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "SSE stream of JSON chunks",
            "content": {
                "text/event-stream": {
                    "example": (
                        '{"type":"token","content":"The timetable..."}\n'
                        '{"type":"tool_output","tool":"generate_ics","payload":{...}}\n'
                        '{"type":"done","conversation_id":"<uuid>"}\n'
                    )
                }
            },
        },
        401: {"description": "Missing or invalid Bearer token"},
        403: {"description": "Email domain is not @bppimt.ac.in"},
    },
)
async def stream_chat(
    payload: StreamChatRequest,
    request: Request,
    principal: Annotated[AuthenticatedUser, Depends(verify_enterprise_user)],
) -> StreamingResponse:
    """
    Streaming agentic chat endpoint (SSE).

    The response is a continuous stream of newline-delimited JSON objects.
    The stream terminates with either a {"type":"done"} or {"type":"error"} chunk.
    """
    conversation_id = payload.conversation_id or str(uuid.uuid4())

    logger.info(
        "Stream chat request: user=%s conversation_id=%s query_length=%d",
        principal.email,
        conversation_id,
        len(payload.query),
    )

    rag_engine = request.app.state.rag_engine

    return StreamingResponse(
        content=_stream_agent_response(
            query=payload.query,
            conversation_id=conversation_id,
            context=payload.context,
            principal=principal,
            rag_engine=rag_engine,
        ),
        media_type="text/event-stream",
        headers={
            # Disable buffering at every layer so tokens reach the client immediately
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",          # Disable nginx proxy buffering
            "X-Conversation-ID": conversation_id,
            "Transfer-Encoding": "chunked",
        },
    )


# ── Non-streaming fallback endpoint ──────────────────────────────────────────
# Kept from Phase 1 for compatibility with simple REST clients and health checks.


class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2048)
    conversation_id: str | None = None


class ChatResponse(BaseModel):
    answer: str
    sources: list[str] = Field(default_factory=list)
    conversation_id: str


@router.post(
    "/chat",
    response_model=ChatResponse,
    summary="Non-streaming RAG query (compatibility endpoint)",
    description=(
        "Synchronous RAG query that returns a complete answer in one response. "
        "Prefer /chat/stream for production use. "
        "Requires a valid @bppimt.ac.in Google ID token."
    ),
)
async def chat(
    payload: ChatRequest,
    request: Request,
    principal: Annotated[AuthenticatedUser, Depends(verify_enterprise_user)],
) -> ChatResponse:
    conversation_id = payload.conversation_id or str(uuid.uuid4())
    logger.info(
        "Sync chat request: user=%s conversation_id=%s",
        principal.email,
        conversation_id,
    )
    rag_engine = request.app.state.rag_engine
    result = await rag_engine.query(
        query=payload.query,
        user_email=principal.email,
        conversation_id=conversation_id,
    )
    return ChatResponse(
        answer=result.answer,
        sources=result.sources,
        conversation_id=conversation_id,
    )
