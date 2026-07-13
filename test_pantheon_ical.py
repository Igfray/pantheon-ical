# SPDX-License-Identifier: Apache-2.0
"""Round-trip + edge cases for the iCal read/write library."""
from __future__ import annotations

from datetime import date

from pantheon_ical import build_ical, parse_ical, parse_ical_slots


def test_roundtrips_through_parse():
    ranges = [(date(2026, 7, 1), date(2026, 7, 5)), (date(2026, 8, 10), date(2026, 8, 12))]
    assert parse_ical(build_ical(ranges, uid_ns="listing-1")) == ranges


def test_build_is_all_day_exclusive_and_crlf():
    ics = build_ical([(date(2026, 7, 1), date(2026, 7, 3))], uid_ns="l1")
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert "DTSTART;VALUE=DATE:20260701" in ics and "DTEND;VALUE=DATE:20260703" in ics
    assert "\n\n" not in ics                              # CRLF, no stray blank lines


def test_build_is_deterministic_and_dedupes():
    r = [(date(2026, 9, 1), date(2026, 9, 3)), (date(2026, 9, 1), date(2026, 9, 3))]
    a = build_ical(r, uid_ns="l"); b = build_ical(r, uid_ns="l")
    assert a == b and a.count("BEGIN:VEVENT") == 1        # byte-stable + duplicate dropped


def test_build_skips_degenerate_ranges():
    ics = build_ical([(date(2026, 7, 5), date(2026, 7, 5)), (date(2026, 7, 9), date(2026, 7, 8))], uid_ns="l")
    assert "BEGIN:VEVENT" not in ics


def test_parse_all_day_and_missing_dtend():
    feed = ("BEGIN:VCALENDAR\r\n"
            "BEGIN:VEVENT\r\nDTSTART;VALUE=DATE:20260701\r\nDTEND;VALUE=DATE:20260704\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT\r\nDTSTART;VALUE=DATE:20260710\r\nEND:VEVENT\r\n"     # no DTEND → one night
            "END:VCALENDAR\r\n")
    assert parse_ical(feed) == [(date(2026, 7, 1), date(2026, 7, 4)), (date(2026, 7, 10), date(2026, 7, 11))]


def test_parse_unfolds_continuation_lines():
    feed = "BEGIN:VEVENT\r\nDTSTART;VALUE=\r\n DATE:20260701\r\nDTEND;VALUE=DATE:20260702\r\nEND:VEVENT\r\n"
    assert parse_ical(feed) == [(date(2026, 7, 1), date(2026, 7, 2))]


def test_recurring_weekly_expands_into_the_future():
    # a weekly rule anchored years ago must still block upcoming occurrences (NOW-anchored horizon)
    feed = ("BEGIN:VEVENT\r\nDTSTART;VALUE=DATE:20240101\r\nDTEND;VALUE=DATE:20240102\r\n"
            "RRULE:FREQ=WEEKLY\r\nEND:VEVENT\r\n")
    ranges = parse_ical(feed)
    assert len(ranges) > 1                                 # not just the historical master


def test_slots_keep_datetimes_and_positive_span():
    feed = ("BEGIN:VEVENT\r\nDTSTART:20260701T090000Z\r\nDTEND:20260701T100000Z\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT\r\nDTSTART:20260701T110000Z\r\nEND:VEVENT\r\n")   # no end → skipped
    slots = parse_ical_slots(feed)
    assert len(slots) == 1
    assert slots[0][0].hour == 9 and slots[0][1].hour == 10


def test_dtstart_plus_duration_blocks_the_full_span():
    # OTAs emit DTSTART + DURATION instead of DTEND; without support this silently collapsed to one night.
    from pantheon_ical import parse_ical
    assert parse_ical("BEGIN:VEVENT\nDTSTART;VALUE=DATE:20260701\nDURATION:P4D\nEND:VEVENT") == \
        [(date(2026, 7, 1), date(2026, 7, 5))]                                  # 4 nights, not 1
    assert parse_ical("BEGIN:VEVENT\nDTSTART;VALUE=DATE:20260701\nDURATION:P1W\nEND:VEVENT") == \
        [(date(2026, 7, 1), date(2026, 7, 8))]                                  # a week
    # an explicit DTEND still wins over a (contradictory) DURATION
    assert parse_ical("BEGIN:VEVENT\nDTSTART;VALUE=DATE:20260701\nDTEND;VALUE=DATE:20260703\n"
                      "DURATION:P9D\nEND:VEVENT") == [(date(2026, 7, 1), date(2026, 7, 3))]


def test_broken_expander_logs_instead_of_silently_underblocking(caplog):
    # if dateutil's expansion raises (broken/missing lib, malformed rule), the safe fallback fires — but it must
    # be LOUD, because in that direction the fallback under-blocks a recurring booking (a double-book).
    import logging

    import pantheon_ical
    with caplog.at_level(logging.WARNING, logger="pantheon_ical"):
        out = pantheon_ical._expand(date(2026, 7, 1), date(2026, 7, 2), "RRULE:FREQ=NONSENSE;X=Y", [])
    assert out == [(date(2026, 7, 1), date(2026, 7, 2))]                        # fell back to the master
    assert any("expansion failed" in r.message for r in caplog.records)        # …and said so
