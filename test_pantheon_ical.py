# SPDX-License-Identifier: Apache-2.0
"""Round-trip + edge cases for the iCal read/write library."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from pantheon_ical import ICalTooLarge, build_ical, parse_ical, parse_ical_slots


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


# ── truncation must be visible, 2026-09-08 ────────────────────────────────────────────────────────
# Same defect class an external reviewer found in the sibling components: a bound that silently
# drops data. `parse_ical(max_events=730)` returns a plain list, so a caller cannot distinguish
# "this calendar has 700 busy periods" from "this calendar had 5,000 and you are seeing 730".
#
# For an availability feed that is not a cosmetic difference. Unseen busy periods read as FREE, so
# the failure mode is a double-booking: the system confidently offers a date the owner has sold.
# A fetch failure already aborts the whole sync (calendar_feeds.py) -- silent truncation is a
# quieter version of the same event and should be at least as loud.

class TestTruncationIsVisibleToTheCaller:

    @staticmethod
    def _cal(n: int) -> str:
        ev = "".join(
            f"BEGIN:VEVENT\r\nUID:u{i}\r\nDTSTART;VALUE=DATE:2026{(i % 12) + 1:02d}{(i % 28) + 1:02d}\r\n"
            f"DTEND;VALUE=DATE:2026{(i % 12) + 1:02d}{(i % 28) + 2:02d}\r\nEND:VEVENT\r\n"
            for i in range(n))
        return "BEGIN:VCALENDAR\r\n" + ev + "END:VCALENDAR\r\n"

    def test_a_calendar_within_the_cap_parses_normally(self):
        out = parse_ical(self._cal(50))
        assert out and len(out) <= 50

    def test_a_calendar_over_the_cap_raises_rather_than_silently_dropping(self):
        """Refuse, don't truncate: a partial busy-list is indistinguishable from a complete one,
        and the wrong direction of error is 'we think you're free'."""
        with pytest.raises(ICalTooLarge) as exc:
            parse_ical(self._cal(3000), max_events=100)
        assert "100" in str(exc.value)

    def test_the_exception_is_a_valueerror_for_existing_handlers(self):
        assert issubclass(ICalTooLarge, ValueError)

    def test_a_caller_that_prefers_a_partial_answer_must_ask_for_it(self):
        """The unsafe behaviour is still available, but only as an explicit decision."""
        out = parse_ical(self._cal(3000), max_events=100, on_overflow="truncate")
        assert len(out) == 100

    def test_slots_parsing_has_the_same_contract(self):
        big = "BEGIN:VCALENDAR\r\n" + "".join(
            f"BEGIN:VEVENT\r\nUID:s{i}\r\nDTSTART:2026010{(i % 9) + 1}T090000Z\r\n"
            f"DTEND:2026010{(i % 9) + 1}T100000Z\r\nEND:VEVENT\r\n" for i in range(3000)
        ) + "END:VCALENDAR\r\n"
        with pytest.raises(ICalTooLarge):
            parse_ical_slots(big, max_events=100)

    def test_the_cap_still_bounds_work_when_truncation_is_requested(self):
        """The DoS bound must survive the escape hatch -- 'truncate' still stops at the cap."""
        out = parse_ical(self._cal(20000), max_events=50, on_overflow="truncate")
        assert len(out) == 50

    def test_an_rrule_that_expands_past_the_cap_is_also_refused(self):
        """An 'every day forever' rule is the cheap way to overflow a calendar."""
        rec = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:r1\r\n"
               "DTSTART;VALUE=DATE:20260101\r\nDTEND;VALUE=DATE:20260102\r\n"
               "RRULE:FREQ=DAILY;COUNT=400\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        with pytest.raises(ICalTooLarge):
            parse_ical(rec, max_events=10)


# ── recurrence overflow, and the boundary that rejected its own limit [2026-09-10] ───────────────
# Astra, third review. The v0.2.0 fix made an oversized calendar refuse rather than truncate --
# but only counted EVENTS. A single VEVENT carrying RRULE:FREQ=HOURLY;COUNT=500 expands inside
# _expand(), which slices to max_occ=400 and returns. The outer check counts what came back, so
# it cannot see the 100 occurrences that were dropped: the overflow test sits OUTSIDE the function
# that discards the data.
#
# For an availability feed, a dropped busy period reads as FREE. This is the double-booking the
# whole v0.2.0 change existed to prevent, arriving by the one path that change did not cover.
#
# _expand's own comments reason carefully about under-blocking in the fallback path, then slice
# silently two lines above. The care and the defect are in the same function.

def _hourly_calendar(count: int) -> str:
    """One VEVENT, `count` half-hour occurrences, starting tomorrow — all inside the horizon."""
    start = datetime.now(timezone.utc) + timedelta(days=1)
    return (
        "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:rec-1\n"
        f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}\n"
        f"DTEND:{(start + timedelta(minutes=30)).strftime('%Y%m%dT%H%M%SZ')}\n"
        f"RRULE:FREQ=HOURLY;COUNT={count}\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )


class TestARecurringEventCannotBeSilentlyClipped:

    def test_recurrence_overflow_raises_instead_of_returning_a_short_list(self):
        """Astra's reproduction: 500 requested, 400 returned, no exception."""
        with pytest.raises(ICalTooLarge):
            parse_ical_slots(_hourly_calendar(500), max_events=5000)

    def test_the_message_names_recurrence_so_the_cause_is_findable(self):
        with pytest.raises(ICalTooLarge) as e:
            parse_ical_slots(_hourly_calendar(500), max_events=5000)
        assert "recurr" in str(e.value).lower(), (
            "an operator reading this needs to know it was ONE rule that overflowed, "
            f"not a calendar with thousands of events: {e.value}")

    def test_a_recurrence_inside_the_cap_is_returned_whole(self):
        # The guard must not refuse legitimate recurring events -- the failure mode that would
        # replace a double-booking with a feed that never syncs at all.
        out = parse_ical_slots(_hourly_calendar(50), max_events=5000)
        assert len(out) == 50, f"a 50-occurrence rule must survive intact, got {len(out)}"

    def test_truncate_still_bounds_the_work_when_explicitly_asked(self):
        out = parse_ical_slots(_hourly_calendar(500), max_events=5000, on_overflow="truncate")
        assert 0 < len(out) <= 500

    def test_the_same_hole_in_parse_ical(self):
        # sibling path: the busy-period parser expands the same way
        with pytest.raises(ICalTooLarge):
            parse_ical(_hourly_calendar(500), max_events=5000)


class TestTheLimitIsTheLimitNotOneBelowIt:
    """`>=` rejected a calendar sitting exactly ON its advertised limit.

    Cosmetic-looking, but it means the documented number is a lie by one, and a caller who sizes
    max_events to their real feed gets a refusal on a calendar that fits.
    """

    def _one_event(self) -> str:
        start = datetime.now(timezone.utc) + timedelta(days=1)
        return ("BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:single\n"
                f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}\n"
                f"DTEND:{(start + timedelta(minutes=30)).strftime('%Y%m%dT%H%M%SZ')}\n"
                "END:VEVENT\nEND:VCALENDAR\n")

    def test_exactly_at_the_limit_is_accepted_slots(self):
        out = parse_ical_slots(self._one_event(), max_events=1)
        assert len(out) == 1

    def test_exactly_at_the_limit_is_accepted_ranges(self):
        out = parse_ical(self._one_event(), max_events=1)
        assert len(out) == 1

    def test_one_over_the_limit_still_refuses(self):
        # the off-by-one fix must not become "no limit at all"
        with pytest.raises(ICalTooLarge):
            parse_ical_slots(_hourly_calendar(3), max_events=2)
