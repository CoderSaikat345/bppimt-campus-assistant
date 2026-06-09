"""
services/faculty.py

Faculty Availability & Meeting Request Service for the BPPIMT Campus Resource Assistant.

Purpose
───────
Given a professor's daily teaching schedule and a student's requested meeting
window (date + time range), this module:

  1. Parses all known teaching blocks for the target day.
  2. Runs a gap-detection algorithm to find free intervals between/after classes.
  3. Intersects those free intervals against the requested meeting window.
  4. Returns either:
       (a) A list of AvailableSlot objects (when gaps exist), OR
       (b) A human-readable "no availability" message.
  5. Generates a pre-formatted email draft body AND a ready-to-fire mailto: URI
     for whichever available slot the student selects.

Design decisions
────────────────
• All time arithmetic uses datetime.timedelta — no third-party time libraries.
  This keeps the logic testable without heavy mocking.

• Minimum meeting duration is configurable (default 15 minutes). Gaps shorter
  than this threshold are not surfaced as available.

• The mailto: URI is percent-encoded using urllib.parse.quote so it can be
  opened directly in a browser or embedded in an <a href> element.

• Faculty email is derived as:  first.last@bppimt.ac.in (from the name string).
  In production this would look up the faculty directory; here we use a
  deterministic transform that works for the demo dataset.

Input contract (TeachingBlock)
──────────────────────────────
    start_time: str  "HH:MM" (24-hour)
    end_time:   str  "HH:MM"
    subject:    str  course name
    room:       str  room identifier

Input contract (MeetingRequest)
───────────────────────────────
    faculty_name:    str   e.g. "Dr. A. Roy"
    faculty_email:   str | None   if known; auto-derived if None
    requested_date:  str   "YYYY-MM-DD"
    window_start:    str   "HH:MM"  — student's earliest available time
    window_end:      str   "HH:MM"  — student's latest available time
    topic:           str   reason for meeting / subject to discuss
    student_name:    str   requester's full name
    student_email:   str   requester's email
    min_duration_min: int  minimum meeting length in minutes (default 15)
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Final

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

DEFAULT_MIN_DURATION_MINUTES: Final[int] = 15
COLLEGE_START: Final[time] = time(8, 0)   # Earliest possible slot
COLLEGE_END: Final[time] = time(18, 0)    # Latest possible slot

# ── Exceptions ───────────────────────────────────────────────────────────────


class FacultyServiceError(Exception):
    """Raised when input to the faculty service is invalid."""


# ── Data models ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TeachingBlock:
    """
    One contiguous teaching period for a faculty member.

    Attributes
    ──────────
    start_time : str  "HH:MM" (24-hour)
    end_time   : str  "HH:MM" (24-hour)
    subject    : str  course name
    room       : str  room number or lab name
    """
    start_time: str
    end_time: str
    subject: str
    room: str

    def __post_init__(self) -> None:
        start = _parse_hhmm(self.start_time)
        end = _parse_hhmm(self.end_time)
        if start >= end:
            raise FacultyServiceError(
                f"TeachingBlock end_time '{self.end_time}' must be after "
                f"start_time '{self.start_time}'."
            )


@dataclass(frozen=True, slots=True)
class MeetingRequest:
    """
    Student's request to schedule a meeting with a faculty member.
    """
    faculty_name: str
    requested_date: str            # "YYYY-MM-DD"
    window_start: str              # "HH:MM" — student's earliest free time
    window_end: str                # "HH:MM" — student's latest free time
    topic: str
    student_name: str
    student_email: str
    faculty_email: str | None = None   # Auto-derived if None
    min_duration_min: int = DEFAULT_MIN_DURATION_MINUTES

    def __post_init__(self) -> None:
        try:
            date.fromisoformat(self.requested_date)
        except ValueError:
            raise FacultyServiceError(
                f"Invalid requested_date '{self.requested_date}'. Use YYYY-MM-DD."
            )
        win_start = _parse_hhmm(self.window_start)
        win_end = _parse_hhmm(self.window_end)
        if win_start >= win_end:
            raise FacultyServiceError(
                "window_end must be later than window_start."
            )
        if self.min_duration_min < 5:
            raise FacultyServiceError(
                "min_duration_min must be at least 5 minutes."
            )


@dataclass(frozen=True, slots=True)
class AvailableSlot:
    """
    A free interval in the faculty's schedule that overlaps with the
    student's requested window and meets the minimum duration.
    """
    start_time: str    # "HH:MM"
    end_time: str      # "HH:MM"
    duration_min: int


@dataclass
class MeetingDraftResult:
    """
    Full output of the faculty availability check.
    """
    faculty_name: str
    faculty_email: str
    requested_date: str
    requested_date_human: str       # "Wednesday, 5 June 2024"
    available_slots: list[AvailableSlot]
    has_availability: bool

    # Pre-filled email components — populated even if no slots (for the
    # "no availability" case, subject/body still communicate the request)
    email_subject: str
    email_body: str                 # Plain-text multi-line draft
    mailto_uri: str                 # mailto: URI for browser/frontend use

    teaching_blocks_on_day: list[TeachingBlock]   # For display in the UI


# ── Helpers ──────────────────────────────────────────────────────────────────


def _parse_hhmm(t_str: str) -> time:
    """Parse "HH:MM" string → datetime.time. Raises FacultyServiceError on failure."""
    try:
        return datetime.strptime(t_str.strip(), "%H:%M").time()
    except ValueError:
        raise FacultyServiceError(
            f"Invalid time '{t_str}'. Expected HH:MM in 24-hour format."
        )


def _time_to_minutes(t: time) -> int:
    """Convert a time object to minutes since midnight."""
    return t.hour * 60 + t.minute


def _minutes_to_hhmm(minutes: int) -> str:
    """Convert minutes-since-midnight back to 'HH:MM' string."""
    h, m = divmod(minutes, 60)
    return f"{h:02d}:{m:02d}"


def _derive_faculty_email(faculty_name: str) -> str:
    """
    Derive a bppimt.ac.in email from a faculty name string.

    Rules:
      - Strip honorifics (Dr., Mr., Mrs., Prof., Ms.)
      - Lowercase all parts
      - Join with a dot separator
      - Append @bppimt.ac.in

    Example: "Dr. A. Roy" → "a.roy@bppimt.ac.in"
    """
    honorifics = re.compile(
        r"\b(dr|mr|mrs|ms|prof|er)\b\.?", re.IGNORECASE
    )
    cleaned = honorifics.sub("", faculty_name).strip()
    parts = [p.strip(".").lower() for p in cleaned.split() if p.strip(".")]
    local = ".".join(parts)
    # Replace any characters invalid in email local parts
    local = re.sub(r"[^a-z0-9._+-]", "", local)
    return f"{local}@bppimt.ac.in"


def _human_date(date_str: str) -> str:
    """Format 'YYYY-MM-DD' as 'Wednesday, 5 June 2024'."""
    d = date.fromisoformat(date_str)
    return d.strftime("%A, ") + str(d.day) + d.strftime(" %B %Y")


# ── Core gap-detection algorithm ─────────────────────────────────────────────


def _find_free_gaps(
    blocks: list[TeachingBlock],
    window_start_min: int,
    window_end_min: int,
    min_duration_min: int,
) -> list[AvailableSlot]:
    """
    Find free time gaps within [window_start_min, window_end_min] that are
    not occupied by any teaching block.

    Algorithm
    ─────────
    1. Merge all teaching blocks into a sorted, non-overlapping list of
       occupied intervals (handles the case where two classes are back-to-back
       or overlap due to data errors).
    2. Walk through the gaps between occupied intervals.
    3. Intersect each gap with the requested student window.
    4. Keep only gaps >= min_duration_min.

    Args:
        blocks:            Teaching blocks for the target day.
        window_start_min:  Student's earliest available time (minutes since midnight).
        window_end_min:    Student's latest available time (minutes since midnight).
        min_duration_min:  Minimum meeting length to surface.

    Returns:
        List of AvailableSlot objects (may be empty).
    """
    # ── Step 1: Build sorted list of (start_min, end_min) occupied intervals ──
    occupied: list[tuple[int, int]] = []
    for block in blocks:
        s = _time_to_minutes(_parse_hhmm(block.start_time))
        e = _time_to_minutes(_parse_hhmm(block.end_time))
        occupied.append((s, e))

    occupied.sort(key=lambda x: x[0])

    # Merge overlapping/adjacent intervals
    merged: list[tuple[int, int]] = []
    for start, end in occupied:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    # ── Step 2: Build gaps between merged intervals ───────────────────────────
    # The timeline we care about is [window_start_min, window_end_min].
    # We frame it as: gap_starts = [window_start] + [end of each block]
    #                 gap_ends   = [start of each block] + [window_end]

    cursor = window_start_min
    free_slots: list[AvailableSlot] = []

    for block_start, block_end in merged:
        # Gap: [cursor, block_start]
        gap_start = cursor
        gap_end = min(block_start, window_end_min)

        if gap_end > gap_start:
            # Intersect with student window
            effective_start = max(gap_start, window_start_min)
            effective_end = min(gap_end, window_end_min)
            duration = effective_end - effective_start

            if duration >= min_duration_min:
                free_slots.append(AvailableSlot(
                    start_time=_minutes_to_hhmm(effective_start),
                    end_time=_minutes_to_hhmm(effective_end),
                    duration_min=duration,
                ))

        cursor = max(cursor, block_end)

    # Final gap after the last teaching block
    if cursor < window_end_min:
        effective_start = max(cursor, window_start_min)
        effective_end = window_end_min
        duration = effective_end - effective_start
        if duration >= min_duration_min:
            free_slots.append(AvailableSlot(
                start_time=_minutes_to_hhmm(effective_start),
                end_time=_minutes_to_hhmm(effective_end),
                duration_min=duration,
            ))

    logger.debug(
        "Gap detection: %d blocks → %d free slots (window=%s–%s)",
        len(blocks),
        len(free_slots),
        _minutes_to_hhmm(window_start_min),
        _minutes_to_hhmm(window_end_min),
    )
    return free_slots


# ── Email draft builder ───────────────────────────────────────────────────────


def _build_email_subject(request: MeetingRequest) -> str:
    return (
        f"Meeting Request: {request.topic} — "
        f"{_human_date(request.requested_date)}"
    )


def _build_email_body(
    request: MeetingRequest,
    available_slots: list[AvailableSlot],
    faculty_email: str,
) -> str:
    """
    Build a formal, professionally worded email draft.

    If there are available slots, the body proposes them specifically.
    If no slots are available, the body politely acknowledges the conflict
    and asks the faculty to suggest an alternative.
    """
    date_human = _human_date(request.requested_date)
    salutation = f"Dear {request.faculty_name},"

    intro = (
        f"I hope this message finds you well. I am {request.student_name}, "
        f"a student at B. P. Poddar Institute of Management & Technology. "
        f"I am writing to request a brief meeting with you regarding: "
        f"**{request.topic}**."
    )

    if available_slots:
        slot_lines = "\n".join(
            f"  • {slot.start_time}–{slot.end_time} IST "
            f"({slot.duration_min} minutes available)"
            for slot in available_slots
        )
        timing_block = (
            f"Based on your teaching schedule on {date_human}, "
            f"I have identified the following windows where you appear to be free:\n\n"
            f"{slot_lines}\n\n"
            f"I would be grateful if you could confirm any of the above slots, "
            f"or suggest a time that is more convenient for you."
        )
    else:
        timing_block = (
            f"I understand you have a full teaching schedule on {date_human}. "
            f"Could you please let me know a convenient time (on this date or "
            f"another) when I may meet you briefly? I am flexible and will "
            f"arrange my schedule accordingly."
        )

    closing = (
        f"Thank you for your time and consideration.\n\n"
        f"Warm regards,\n"
        f"{request.student_name}\n"
        f"{request.student_email}"
    )

    return "\n\n".join([salutation, intro, timing_block, closing])


def _build_mailto_uri(
    faculty_email: str,
    subject: str,
    body: str,
) -> str:
    """
    Construct a percent-encoded mailto: URI.

    The URI can be opened directly in a browser to launch the default
    email client with all fields pre-populated, or embedded in the
    frontend as <a href="mailto:...">Send Email</a>.

    RFC 6068 compliance:
      - Subject and body are encoded with urllib.parse.quote.
      - Only unreserved characters and certain delimiters are left unencoded.
    """
    encoded_subject = urllib.parse.quote(subject, safe="")
    encoded_body = urllib.parse.quote(body, safe="")
    return f"mailto:{faculty_email}?subject={encoded_subject}&body={encoded_body}"


# ── Public API ────────────────────────────────────────────────────────────────


def check_faculty_availability(
    teaching_blocks: list[TeachingBlock],
    request: MeetingRequest,
) -> MeetingDraftResult:
    """
    Analyse a faculty member's teaching schedule and generate a meeting draft.

    Args:
        teaching_blocks: All teaching blocks for the faculty on the requested
                         day (may be empty if the faculty has no classes).
        request:         The student's meeting request parameters.

    Returns:
        MeetingDraftResult with available slots, a formatted email body,
        and a ready-to-fire mailto: URI.

    Raises:
        FacultyServiceError: If any input is invalid.
    """
    faculty_email = (
        request.faculty_email
        or _derive_faculty_email(request.faculty_name)
    )

    win_start_min = _time_to_minutes(_parse_hhmm(request.window_start))
    win_end_min = _time_to_minutes(_parse_hhmm(request.window_end))

    free_slots = _find_free_gaps(
        blocks=teaching_blocks,
        window_start_min=win_start_min,
        window_end_min=win_end_min,
        min_duration_min=request.min_duration_min,
    )

    subject = _build_email_subject(request)
    body = _build_email_body(request, free_slots, faculty_email)
    mailto_uri = _build_mailto_uri(faculty_email, subject, body)

    result = MeetingDraftResult(
        faculty_name=request.faculty_name,
        faculty_email=faculty_email,
        requested_date=request.requested_date,
        requested_date_human=_human_date(request.requested_date),
        available_slots=free_slots,
        has_availability=len(free_slots) > 0,
        email_subject=subject,
        email_body=body,
        mailto_uri=mailto_uri,
        teaching_blocks_on_day=teaching_blocks,
    )

    logger.info(
        "Faculty availability check: faculty=%s date=%s free_slots=%d",
        request.faculty_name,
        request.requested_date,
        len(free_slots),
    )

    return result


def check_faculty_availability_from_raw(
    raw_blocks: list[dict],
    raw_request: dict,
) -> MeetingDraftResult:
    """
    Convenience wrapper accepting plain dicts from the LLM tool call payload.

    Args:
        raw_blocks:  List of dicts with keys: start_time, end_time, subject, room.
        raw_request: Dict with keys matching MeetingRequest fields.

    Returns:
        MeetingDraftResult

    Raises:
        FacultyServiceError: On missing keys or invalid values.
    """
    # Parse blocks
    blocks: list[TeachingBlock] = []
    for i, raw in enumerate(raw_blocks):
        missing = {"start_time", "end_time", "subject", "room"} - raw.keys()
        if missing:
            raise FacultyServiceError(
                f"TeachingBlock {i} is missing fields: {missing}"
            )
        blocks.append(TeachingBlock(
            start_time=str(raw["start_time"]),
            end_time=str(raw["end_time"]),
            subject=str(raw["subject"]),
            room=str(raw["room"]),
        ))

    # Parse request
    required_req = {
        "faculty_name", "requested_date", "window_start",
        "window_end", "topic", "student_name", "student_email",
    }
    missing_req = required_req - raw_request.keys()
    if missing_req:
        raise FacultyServiceError(
            f"MeetingRequest is missing fields: {missing_req}"
        )

    req = MeetingRequest(
        faculty_name=str(raw_request["faculty_name"]),
        requested_date=str(raw_request["requested_date"]),
        window_start=str(raw_request["window_start"]),
        window_end=str(raw_request["window_end"]),
        topic=str(raw_request["topic"]),
        student_name=str(raw_request["student_name"]),
        student_email=str(raw_request["student_email"]),
        faculty_email=raw_request.get("faculty_email"),
        min_duration_min=int(raw_request.get("min_duration_min", DEFAULT_MIN_DURATION_MINUTES)),
    )

    return check_faculty_availability(blocks, req)
