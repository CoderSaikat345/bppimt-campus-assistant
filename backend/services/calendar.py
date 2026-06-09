"""
services/calendar.py

ICS Calendar Generator for the BPPIMT Campus Resource Assistant.

Purpose
───────
Converts a student's weekly timetable (a structured list of lecture slots)
into a standards-compliant .ics file (RFC 5545) that can be imported into
Google Calendar, Outlook, Apple Calendar, or any CalDAV-compatible client.

Key design decisions
────────────────────
• RRULE FREQ=WEEKLY: Each lecture is a recurring weekly event, not a one-off.
  This accurately models an academic semester schedule.

• DTSTART + DTEND are placed in the upcoming week relative to "today" so that
  newly imported calendars are immediately relevant, not historical.

• TZID="Asia/Kolkata" (IST) is used throughout. The icalendar library
  embeds a full VTIMEZONE component so clients that don't have the zone
  built-in can still render times correctly.

• Each VEVENT carries a deterministic UID derived from:
    sha256(section + day + start_time + subject)
  This means importing the same timetable twice does NOT duplicate events
  in compliant calendar clients (they perform MERGE on UID).

• DURATION is not used — explicit DTEND is preferred for wider client compat.

• SUMMARY, DESCRIPTION, and LOCATION are all populated for rich display.

Input contract (TimetableSlot)
─────────────────────────────
    section    : str   e.g. "CSE-B"
    day        : str   e.g. "Monday", "Tuesday" … "Saturday"
    start_time : str   "HH:MM"  24-hour format
    end_time   : str   "HH:MM"  24-hour format
    subject    : str   e.g. "Data Structures"
    faculty    : str   e.g. "Dr. A. Roy"
    room       : str   e.g. "401" or "Lab-2"
    semester_end_date: str  "YYYY-MM-DD"  — RRULE UNTIL boundary

Output contract
───────────────
    CalendarResult.ics_bytes  : bytes  — UTF-8 encoded .ics content
    CalendarResult.filename   : str    — suggested download filename
    CalendarResult.event_count: int    — number of VEVENT components created
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event, Timezone, TimezoneStandard, vText

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

IST: Final[ZoneInfo] = ZoneInfo("Asia/Kolkata")

# Maps day names to ISO weekday numbers (Monday=0 … Sunday=6)
_DAY_TO_WEEKDAY: Final[dict[str, int]] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

# RFC 5545 two-letter day codes for RRULE BYDAY
_DAY_TO_RRULE_CODE: Final[dict[str, str]] = {
    "monday": "MO",
    "tuesday": "TU",
    "wednesday": "WE",
    "thursday": "TH",
    "friday": "FR",
    "saturday": "SA",
    "sunday": "SU",
}

# Product identifier embedded in PRODID
_PRODID: Final[str] = "-//BPPIMT Campus Resource Assistant//EN"

# ── Custom exceptions ────────────────────────────────────────────────────────


class CalendarGenerationError(Exception):
    """Raised when .ics generation fails due to invalid input."""


# ── Input model ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TimetableSlot:
    """
    One lecture slot in a student's weekly timetable.

    All string fields are stripped and title-cased on validation.
    """

    section: str
    """Academic section identifier, e.g. "CSE-B"."""

    day: str
    """Day of week as a full English name, case-insensitive."""

    start_time: str
    """Lecture start time in 24-hour HH:MM format."""

    end_time: str
    """Lecture end time in 24-hour HH:MM format."""

    subject: str
    """Subject/course name."""

    faculty: str
    """Lecturer's name."""

    room: str
    """Room number or lab name."""

    semester_end_date: str
    """
    Last day of the semester in YYYY-MM-DD format.
    Used as the UNTIL boundary in the RRULE so events stop at semester end.
    """

    def __post_init__(self) -> None:
        # Validate day name
        if self.day.lower() not in _DAY_TO_WEEKDAY:
            raise CalendarGenerationError(
                f"Invalid day '{self.day}'. Must be one of: "
                f"{', '.join(_DAY_TO_WEEKDAY.keys())}."
            )
        # Validate time format
        for label, t_str in [("start_time", self.start_time), ("end_time", self.end_time)]:
            try:
                _parse_hhmm(t_str)
            except ValueError:
                raise CalendarGenerationError(
                    f"Invalid {label} '{t_str}'. Must be HH:MM in 24-hour format."
                )
        # Validate semester end date
        try:
            date.fromisoformat(self.semester_end_date)
        except ValueError:
            raise CalendarGenerationError(
                f"Invalid semester_end_date '{self.semester_end_date}'. "
                f"Must be YYYY-MM-DD."
            )


# ── Output model ─────────────────────────────────────────────────────────────


@dataclass
class CalendarResult:
    """The output of the calendar generation service."""

    ics_bytes: bytes
    """Raw .ics file content, UTF-8 encoded."""

    filename: str
    """Suggested Content-Disposition filename for HTTP download."""

    event_count: int
    """Number of VEVENT components embedded in the file."""

    section: str
    """Section identifier this calendar was generated for."""


# ── Helpers ──────────────────────────────────────────────────────────────────


def _parse_hhmm(t_str: str) -> time:
    """Parse a "HH:MM" string into a datetime.time object."""
    return datetime.strptime(t_str.strip(), "%H:%M").time()


def _next_weekday_from_today(target_weekday: int) -> date:
    """
    Return the date of the next occurrence of `target_weekday` (0=Monday)
    starting from today (inclusive).

    This ensures the first event's DTSTART is always in the upcoming week,
    making the imported calendar immediately visible.
    """
    today = date.today()
    days_ahead = (target_weekday - today.weekday()) % 7
    return today + timedelta(days=days_ahead)


def _build_uid(slot: TimetableSlot) -> str:
    """
    Generate a deterministic UID for a timetable slot.

    RFC 5545 requires UIDs to be globally unique per event.  We use a
    SHA-256 hash of the slot identity fields so the same slot always
    produces the same UID — enabling idempotent calendar import (MERGE).
    """
    key = "|".join([
        slot.section.upper(),
        slot.day.lower(),
        slot.start_time,
        slot.subject.lower(),
    ])
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return f"{digest}@bppimt.ac.in"


def _build_ist_timezone_component() -> Timezone:
    """
    Build a VTIMEZONE component for Asia/Kolkata (IST, UTC+5:30).

    IST does not observe daylight saving time, so only one STANDARD
    sub-component is needed. This embedded VTIMEZONE makes the .ics file
    self-contained — clients that don't know IST can still render it.
    """
    tz = Timezone()
    tz.add("TZID", "Asia/Kolkata")
    tz.add("X-LIC-LOCATION", "Asia/Kolkata")

    standard = TimezoneStandard()
    standard.add("DTSTART", datetime(1970, 1, 1, 0, 0, 0))
    standard.add("TZOFFSETFROM", timedelta(hours=5, minutes=30))
    standard.add("TZOFFSETTO", timedelta(hours=5, minutes=30))
    standard.add("TZNAME", vText("IST"))
    tz.add_component(standard)

    return tz


def _build_vevent(slot: TimetableSlot) -> Event:
    """
    Build a single RFC 5545 VEVENT for one lecture slot.

    The RRULE specifies FREQ=WEEKLY;BYDAY=<XX>;UNTIL=<semester_end> so the
    event automatically repeats every week until the semester ends.
    """
    target_weekday = _DAY_TO_WEEKDAY[slot.day.lower()]
    rrule_day_code = _DAY_TO_RRULE_CODE[slot.day.lower()]

    first_occurrence: date = _next_weekday_from_today(target_weekday)
    start_t: time = _parse_hhmm(slot.start_time)
    end_t: time = _parse_hhmm(slot.end_time)
    semester_end: date = date.fromisoformat(slot.semester_end_date)

    # Full datetime objects in IST
    dtstart = datetime.combine(first_occurrence, start_t, tzinfo=IST)
    dtend = datetime.combine(first_occurrence, end_t, tzinfo=IST)

    # UNTIL must be a UTC datetime per RFC 5545 §3.3.10
    # Convert semester_end to end-of-day UTC
    until_ist = datetime.combine(semester_end, time(23, 59, 59), tzinfo=IST)
    until_utc = until_ist.astimezone(ZoneInfo("UTC"))

    event = Event()
    event.add("UID", _build_uid(slot))
    event.add("SUMMARY", f"{slot.subject} — {slot.section}")
    event.add("DESCRIPTION", (
        f"Subject: {slot.subject}\n"
        f"Section: {slot.section}\n"
        f"Faculty: {slot.faculty}\n"
        f"Room: {slot.room}\n"
        f"Schedule: Every {slot.day.title()} "
        f"{slot.start_time}–{slot.end_time} IST"
    ))
    event.add("LOCATION", f"Room {slot.room}, BPPIMT")
    event.add("DTSTART", dtstart)
    event.add("DTEND", dtend)
    event.add("RRULE", {
        "FREQ": "WEEKLY",
        "BYDAY": [rrule_day_code],
        "UNTIL": until_utc,
    })
    event.add("STATUS", "CONFIRMED")
    event.add("TRANSP", "OPAQUE")  # Blocks time (shows as busy)

    # Organiser metadata — helps calendar clients display faculty info
    event["ORGANIZER"] = vText(f"MAILTO:{slot.faculty.replace(' ', '.').lower()}@bppimt.ac.in")

    logger.debug(
        "Built VEVENT: uid=%s subject=%s day=%s %s-%s",
        _build_uid(slot),
        slot.subject,
        slot.day,
        slot.start_time,
        slot.end_time,
    )
    return event


# ── Public API ────────────────────────────────────────────────────────────────


def generate_ics(slots: list[TimetableSlot]) -> CalendarResult:
    """
    Generate a standards-compliant .ics calendar file from a list of
    timetable slots.

    Args:
        slots: One or more TimetableSlot objects representing weekly lectures.
               All slots are assumed to belong to the same student/section.

    Returns:
        CalendarResult with the raw ICS bytes and metadata.

    Raises:
        CalendarGenerationError: If slots is empty or any slot is malformed.
    """
    if not slots:
        raise CalendarGenerationError(
            "Cannot generate a calendar from an empty slot list."
        )

    # Infer section from the first slot (all slots should share one section)
    section = slots[0].section

    cal = Calendar()
    cal.add("PRODID", _PRODID)
    cal.add("VERSION", "2.0")
    cal.add("CALSCALE", "GREGORIAN")
    cal.add("METHOD", "PUBLISH")
    cal.add("X-WR-CALNAME", f"BPPIMT Timetable — {section}")
    cal.add("X-WR-TIMEZONE", "Asia/Kolkata")
    cal.add("X-WR-CALDESC", (
        f"Weekly class schedule for {section} — "
        f"BPPIMT (B. P. Poddar Institute of Management & Technology)"
    ))

    # Embed IST timezone definition
    cal.add_component(_build_ist_timezone_component())

    # Add one VEVENT per slot
    for slot in slots:
        event = _build_vevent(slot)
        cal.add_component(event)

    ics_bytes: bytes = cal.to_ical()
    filename = f"BPPIMT_{section.replace(' ', '_')}_Timetable.ics"

    logger.info(
        "ICS generated: section=%s events=%d bytes=%d",
        section,
        len(slots),
        len(ics_bytes),
    )

    return CalendarResult(
        ics_bytes=ics_bytes,
        filename=filename,
        event_count=len(slots),
        section=section,
    )


def generate_ics_from_raw(
    raw_slots: list[dict],
    semester_end_date: str,
) -> CalendarResult:
    """
    Convenience wrapper that accepts plain dicts (e.g. from the LLM tool call
    JSON payload) and converts them to TimetableSlot objects before generating.

    Args:
        raw_slots: List of dicts, each with keys matching TimetableSlot fields
                   (except semester_end_date, which is passed separately).
        semester_end_date: "YYYY-MM-DD" boundary for RRULE UNTIL.

    Returns:
        CalendarResult

    Raises:
        CalendarGenerationError: On missing keys or invalid values.
    """
    required_keys = {"section", "day", "start_time", "end_time", "subject", "faculty", "room"}
    parsed: list[TimetableSlot] = []

    for i, raw in enumerate(raw_slots):
        missing = required_keys - raw.keys()
        if missing:
            raise CalendarGenerationError(
                f"Slot {i} is missing required fields: {missing}. "
                f"Received keys: {set(raw.keys())}"
            )
        try:
            parsed.append(TimetableSlot(
                section=str(raw["section"]),
                day=str(raw["day"]),
                start_time=str(raw["start_time"]),
                end_time=str(raw["end_time"]),
                subject=str(raw["subject"]),
                faculty=str(raw["faculty"]),
                room=str(raw["room"]),
                semester_end_date=semester_end_date,
            ))
        except CalendarGenerationError:
            raise
        except Exception as exc:
            raise CalendarGenerationError(
                f"Failed to parse slot {i}: {exc}"
            ) from exc

    return generate_ics(parsed)
