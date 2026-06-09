"""
tests/test_services.py

Unit tests for services/calendar.py and services/faculty.py.

All tests are purely unit-level — no network calls, no GCP, no file I/O.
"""

from __future__ import annotations

import base64
import json
from datetime import date

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# calendar.py tests
# ─────────────────────────────────────────────────────────────────────────────

from services.calendar import (
    CalendarGenerationError,
    CalendarResult,
    TimetableSlot,
    _build_uid,
    _next_weekday_from_today,
    generate_ics,
    generate_ics_from_raw,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_slot(**overrides) -> TimetableSlot:
    defaults = dict(
        section="CSE-B",
        day="Monday",
        start_time="09:00",
        end_time="10:00",
        subject="Data Structures",
        faculty="Dr. A. Roy",
        room="401",
        semester_end_date="2025-11-30",
    )
    defaults.update(overrides)
    return TimetableSlot(**defaults)


SAMPLE_SLOTS = [
    _make_slot(day="Monday",    start_time="09:00", end_time="10:00", subject="Data Structures"),
    _make_slot(day="Tuesday",   start_time="10:00", end_time="11:00", subject="DBMS"),
    _make_slot(day="Wednesday", start_time="11:00", end_time="12:00", subject="OS"),
    _make_slot(day="Thursday",  start_time="14:00", end_time="15:00", subject="CN"),
    _make_slot(day="Friday",    start_time="09:00", end_time="10:00", subject="Maths-III"),
]


# ── TimetableSlot validation ──────────────────────────────────────────────────

class TestTimetableSlot:
    def test_valid_slot_creates_successfully(self) -> None:
        slot = _make_slot()
        assert slot.subject == "Data Structures"
        assert slot.section == "CSE-B"

    def test_invalid_day_raises(self) -> None:
        with pytest.raises(CalendarGenerationError, match="Invalid day"):
            _make_slot(day="Funday")

    def test_invalid_start_time_raises(self) -> None:
        with pytest.raises(CalendarGenerationError, match="Invalid start_time"):
            _make_slot(start_time="25:00")

    def test_invalid_end_time_raises(self) -> None:
        with pytest.raises(CalendarGenerationError, match="Invalid end_time"):
            _make_slot(end_time="9am")

    def test_invalid_semester_end_raises(self) -> None:
        with pytest.raises(CalendarGenerationError, match="Invalid semester_end_date"):
            _make_slot(semester_end_date="30-11-2025")  # Wrong format


# ── UID determinism ───────────────────────────────────────────────────────────

class TestUIDGeneration:
    def test_same_slot_gives_same_uid(self) -> None:
        slot = _make_slot()
        assert _build_uid(slot) == _build_uid(slot)

    def test_different_subject_gives_different_uid(self) -> None:
        s1 = _make_slot(subject="Data Structures")
        s2 = _make_slot(subject="DBMS")
        assert _build_uid(s1) != _build_uid(s2)

    def test_uid_ends_with_bppimt_domain(self) -> None:
        assert _build_uid(_make_slot()).endswith("@bppimt.ac.in")


# ── ICS generation ────────────────────────────────────────────────────────────

class TestGenerateICS:
    def test_returns_calendar_result(self) -> None:
        result = generate_ics(SAMPLE_SLOTS)
        assert isinstance(result, CalendarResult)

    def test_event_count_matches_slot_count(self) -> None:
        result = generate_ics(SAMPLE_SLOTS)
        assert result.event_count == len(SAMPLE_SLOTS)

    def test_ics_bytes_are_valid_utf8(self) -> None:
        result = generate_ics(SAMPLE_SLOTS)
        text = result.ics_bytes.decode("utf-8")
        assert "BEGIN:VCALENDAR" in text
        assert "END:VCALENDAR" in text

    def test_ics_contains_vevent_for_each_slot(self) -> None:
        result = generate_ics(SAMPLE_SLOTS)
        text = result.ics_bytes.decode("utf-8")
        assert text.count("BEGIN:VEVENT") == len(SAMPLE_SLOTS)

    def test_ics_contains_rrule_weekly(self) -> None:
        result = generate_ics([_make_slot()])
        text = result.ics_bytes.decode("utf-8")
        assert "FREQ=WEEKLY" in text

    def test_ics_contains_ist_timezone(self) -> None:
        result = generate_ics([_make_slot()])
        text = result.ics_bytes.decode("utf-8")
        assert "Asia/Kolkata" in text or "IST" in text

    def test_ics_contains_correct_summary(self) -> None:
        result = generate_ics([_make_slot(subject="Data Structures", section="CSE-B")])
        text = result.ics_bytes.decode("utf-8")
        assert "Data Structures" in text
        assert "CSE-B" in text

    def test_ics_contains_location(self) -> None:
        result = generate_ics([_make_slot(room="401")])
        text = result.ics_bytes.decode("utf-8")
        assert "Room 401" in text

    def test_filename_contains_section(self) -> None:
        result = generate_ics([_make_slot(section="CSE-B")])
        assert "CSE-B" in result.filename
        assert result.filename.endswith(".ics")

    def test_empty_slots_raises(self) -> None:
        with pytest.raises(CalendarGenerationError, match="empty slot list"):
            generate_ics([])

    def test_saturday_slot_generates_correctly(self) -> None:
        slot = _make_slot(day="Saturday", start_time="10:00", end_time="11:00")
        result = generate_ics([slot])
        text = result.ics_bytes.decode("utf-8")
        assert "BYDAY=SA" in text or "SA" in text


class TestGenerateICSFromRaw:
    def test_raw_dict_slots_parse_correctly(self) -> None:
        raw_slots = [
            {
                "section": "CSE-A",
                "day": "Friday",
                "start_time": "09:00",
                "end_time": "10:00",
                "subject": "Algorithms",
                "faculty": "Dr. B. Das",
                "room": "302",
            }
        ]
        result = generate_ics_from_raw(raw_slots, semester_end_date="2025-11-30")
        assert result.event_count == 1
        assert "CSE-A" in result.section

    def test_missing_required_key_raises(self) -> None:
        raw_slots = [{"section": "CSE-A", "day": "Monday"}]  # Missing fields
        with pytest.raises(CalendarGenerationError, match="missing required fields"):
            generate_ics_from_raw(raw_slots, semester_end_date="2025-11-30")


# ─────────────────────────────────────────────────────────────────────────────
# faculty.py tests
# ─────────────────────────────────────────────────────────────────────────────

from services.faculty import (
    AvailableSlot,
    FacultyServiceError,
    MeetingDraftResult,
    MeetingRequest,
    TeachingBlock,
    _derive_faculty_email,
    _find_free_gaps,
    _minutes_to_hhmm,
    _time_to_minutes,
    check_faculty_availability,
    check_faculty_availability_from_raw,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_request(**overrides) -> MeetingRequest:
    defaults = dict(
        faculty_name="Dr. A. Roy",
        requested_date="2025-11-05",
        window_start="09:00",
        window_end="17:00",
        topic="Project Guidance",
        student_name="Saikat Mukherjee",
        student_email="saikat.m@bppimt.ac.in",
    )
    defaults.update(overrides)
    return MeetingRequest(**defaults)


def _make_block(start: str, end: str, subject: str = "DS", room: str = "401") -> TeachingBlock:
    return TeachingBlock(start_time=start, end_time=end, subject=subject, room=room)


# ── TeachingBlock validation ──────────────────────────────────────────────────

class TestTeachingBlock:
    def test_valid_block_creates(self) -> None:
        block = _make_block("09:00", "10:00")
        assert block.subject == "DS"

    def test_end_before_start_raises(self) -> None:
        with pytest.raises(FacultyServiceError, match="must be after"):
            _make_block("10:00", "09:00")

    def test_same_start_end_raises(self) -> None:
        with pytest.raises(FacultyServiceError):
            _make_block("10:00", "10:00")


# ── MeetingRequest validation ─────────────────────────────────────────────────

class TestMeetingRequest:
    def test_invalid_date_format_raises(self) -> None:
        with pytest.raises(FacultyServiceError, match="Invalid requested_date"):
            _make_request(requested_date="05-11-2025")

    def test_window_end_before_start_raises(self) -> None:
        with pytest.raises(FacultyServiceError, match="window_end must be later"):
            _make_request(window_start="17:00", window_end="09:00")

    def test_min_duration_too_short_raises(self) -> None:
        with pytest.raises(FacultyServiceError, match="min_duration_min"):
            _make_request(min_duration_min=3)


# ── Email derivation ──────────────────────────────────────────────────────────

class TestDeriveEmail:
    def test_dr_prefix_stripped(self) -> None:
        assert _derive_faculty_email("Dr. A. Roy") == "a.roy@bppimt.ac.in"

    def test_prof_prefix_stripped(self) -> None:
        assert _derive_faculty_email("Prof. S. Das") == "s.das@bppimt.ac.in"

    def test_mr_prefix_stripped(self) -> None:
        email = _derive_faculty_email("Mr. P. Sen")
        assert email.endswith("@bppimt.ac.in")
        assert "mr" not in email.split("@")[0]

    def test_no_prefix(self) -> None:
        email = _derive_faculty_email("Anita Roy")
        assert email == "anita.roy@bppimt.ac.in"


# ── Gap detection ─────────────────────────────────────────────────────────────

class TestFindFreeGaps:
    def test_no_blocks_whole_window_is_free(self) -> None:
        gaps = _find_free_gaps([], 9 * 60, 17 * 60, 15)
        assert len(gaps) == 1
        assert gaps[0].start_time == "09:00"
        assert gaps[0].end_time == "17:00"

    def test_single_block_mid_day_creates_two_gaps(self) -> None:
        blocks = [_make_block("11:00", "12:00")]
        gaps = _find_free_gaps(blocks, 9 * 60, 17 * 60, 15)
        assert len(gaps) == 2
        assert gaps[0].start_time == "09:00"
        assert gaps[0].end_time == "11:00"
        assert gaps[1].start_time == "12:00"
        assert gaps[1].end_time == "17:00"

    def test_block_covering_entire_window_leaves_no_gaps(self) -> None:
        blocks = [_make_block("09:00", "17:00")]
        gaps = _find_free_gaps(blocks, 9 * 60, 17 * 60, 15)
        assert gaps == []

    def test_back_to_back_blocks_merged_correctly(self) -> None:
        blocks = [_make_block("09:00", "10:00"), _make_block("10:00", "11:00")]
        gaps = _find_free_gaps(blocks, 9 * 60, 17 * 60, 15)
        assert len(gaps) == 1
        assert gaps[0].start_time == "11:00"

    def test_gap_shorter_than_min_duration_filtered(self) -> None:
        # 10-minute gap between 10:00 and 10:10
        blocks = [_make_block("09:00", "10:00"), _make_block("10:10", "17:00")]
        gaps = _find_free_gaps(blocks, 9 * 60, 17 * 60, 15)
        assert len(gaps) == 0  # 10-min gap < 15-min minimum

    def test_overlapping_blocks_merged(self) -> None:
        # Two overlapping blocks: 09:00–11:00 and 10:00–12:00
        blocks = [_make_block("09:00", "11:00"), _make_block("10:00", "12:00")]
        gaps = _find_free_gaps(blocks, 9 * 60, 17 * 60, 15)
        assert len(gaps) == 1
        assert gaps[0].start_time == "12:00"

    def test_window_intersection_respected(self) -> None:
        # Block 08:00–09:30, student window starts at 09:00
        blocks = [_make_block("08:00", "09:30")]
        gaps = _find_free_gaps(blocks, 9 * 60, 17 * 60, 15)
        # Only 09:30–17:00 is within window AND free
        assert len(gaps) == 1
        assert gaps[0].start_time == "09:30"

    def test_duration_calculated_correctly(self) -> None:
        blocks = [_make_block("10:00", "16:00")]
        gaps = _find_free_gaps(blocks, 9 * 60, 17 * 60, 15)
        assert len(gaps) == 2
        # 09:00–10:00 = 60 min
        assert gaps[0].duration_min == 60
        # 16:00–17:00 = 60 min
        assert gaps[1].duration_min == 60


# ── Full availability check ───────────────────────────────────────────────────

class TestCheckFacultyAvailability:
    def test_returns_meeting_draft_result(self) -> None:
        blocks = [_make_block("09:00", "10:00"), _make_block("14:00", "15:00")]
        req = _make_request()
        result = check_faculty_availability(blocks, req)
        assert isinstance(result, MeetingDraftResult)

    def test_has_availability_true_when_gaps_exist(self) -> None:
        blocks = [_make_block("09:00", "10:00")]
        result = check_faculty_availability(blocks, _make_request())
        assert result.has_availability is True

    def test_has_availability_false_when_full_day(self) -> None:
        blocks = [_make_block("09:00", "17:00")]
        result = check_faculty_availability(blocks, _make_request())
        assert result.has_availability is False

    def test_email_subject_contains_topic(self) -> None:
        result = check_faculty_availability([], _make_request(topic="Thesis Review"))
        assert "Thesis Review" in result.email_subject

    def test_email_body_contains_faculty_name(self) -> None:
        result = check_faculty_availability([], _make_request(faculty_name="Dr. A. Roy"))
        assert "Dr. A. Roy" in result.email_body

    def test_email_body_contains_student_name(self) -> None:
        result = check_faculty_availability(
            [], _make_request(student_name="Saikat Mukherjee")
        )
        assert "Saikat Mukherjee" in result.email_body

    def test_mailto_uri_has_correct_scheme(self) -> None:
        result = check_faculty_availability([], _make_request())
        assert result.mailto_uri.startswith("mailto:")

    def test_mailto_uri_contains_encoded_subject(self) -> None:
        result = check_faculty_availability([], _make_request(topic="Project Guidance"))
        # URL-encoded version of the subject should be present
        assert "Project" in result.mailto_uri or "Project%20" in result.mailto_uri

    def test_faculty_email_derived_when_not_provided(self) -> None:
        result = check_faculty_availability([], _make_request(faculty_name="Dr. A. Roy"))
        assert result.faculty_email.endswith("@bppimt.ac.in")

    def test_explicit_faculty_email_used_when_provided(self) -> None:
        result = check_faculty_availability(
            [],
            _make_request(faculty_email="specific.email@bppimt.ac.in"),
        )
        assert result.faculty_email == "specific.email@bppimt.ac.in"

    def test_available_slots_returned_correctly(self) -> None:
        # Two teaching blocks leave two free windows
        blocks = [
            _make_block("09:00", "11:00"),
            _make_block("14:00", "16:00"),
        ]
        result = check_faculty_availability(blocks, _make_request())
        assert len(result.available_slots) == 2

    def test_no_availability_message_in_body(self) -> None:
        # Full day blocked
        blocks = [_make_block("09:00", "17:00")]
        result = check_faculty_availability(blocks, _make_request())
        assert "no free" in result.email_body.lower() or "full" in result.email_body.lower()


class TestCheckFacultyAvailabilityFromRaw:
    def test_raw_dict_input_parses_correctly(self) -> None:
        raw_blocks = [
            {"start_time": "09:00", "end_time": "10:00", "subject": "DS", "room": "401"}
        ]
        raw_request = {
            "faculty_name": "Dr. A. Roy",
            "requested_date": "2025-11-05",
            "window_start": "09:00",
            "window_end": "17:00",
            "topic": "Project",
            "student_name": "Saikat",
            "student_email": "saikat@bppimt.ac.in",
        }
        result = check_faculty_availability_from_raw(raw_blocks, raw_request)
        assert isinstance(result, MeetingDraftResult)

    def test_missing_block_field_raises(self) -> None:
        raw_blocks = [{"start_time": "09:00", "subject": "DS"}]  # Missing end_time, room
        raw_request = {
            "faculty_name": "Dr. A. Roy",
            "requested_date": "2025-11-05",
            "window_start": "09:00",
            "window_end": "17:00",
            "topic": "Project",
            "student_name": "Saikat",
            "student_email": "saikat@bppimt.ac.in",
        }
        with pytest.raises(FacultyServiceError, match="missing fields"):
            check_faculty_availability_from_raw(raw_blocks, raw_request)

    def test_missing_request_field_raises(self) -> None:
        with pytest.raises(FacultyServiceError, match="missing fields"):
            check_faculty_availability_from_raw([], {"faculty_name": "Dr. Roy"})
