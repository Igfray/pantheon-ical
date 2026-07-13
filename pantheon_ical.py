# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Isaac Teague Frayling
"""Round-trip iCal (RFC 5545) read + write for booking/availability calendars — in under 200 lines.

Just enough of RFC 5545 to keep an availability calendar in sync with the systems that speak it (Airbnb,
Booking.com, Google Calendar, Vrbo, ...):

  * parse_ical(text)         → the busy DATE ranges from a feed's VEVENTs, as half-open [start, end) tuples
                               (checkout day exclusive), with RRULE recurrence expanded (bounded, safe).
  * build_ical(ranges)       → the inverse: serialise busy date ranges into a VCALENDAR feed OTHER systems
                               import. Deterministic (no wall-clock), so an unchanged calendar is byte-stable.
  * parse_ical_slots(text)   → busy DATETIME slots (for appointment/class calendars), timezone-aware.

All-day bookings use `VALUE=DATE` with an EXCLUSIVE `DTEND`, which maps exactly onto the half-open interval
model — a booking of the 1st–5th blocks four nights (`DTSTART:20260701` .. `DTEND:20260705`) and leaves the
5th free for the next arrival.

Safety choices worth knowing (learned running this against real OTA feeds):
  * An unparseable/exotic RRULE falls back to the single master occurrence, so a rule we don't model can never
    silently UNDER-block real availability (which would cause a double-booking).
  * Recurrence is expanded around NOW, not DTSTART — an "every Monday since 2024" rule has a years-old DTSTART,
    so a DTSTART-anchored horizon would expand only historical dates and leave the live window unblocked.

Extracted from PANTHEON (a multi-tenant AI substrate), where it drives two-way calendar sync for the
holiday-let / appointment businesses it hosts.

    from pantheon_ical import parse_ical, build_ical
    busy = parse_ical(requests.get(airbnb_ics_url).text)   # [(date(2026,7,1), date(2026,7,5)), ...]
    feed = build_ical(busy, uid_ns="my-listing")           # hand this URL to Booking.com → the block propagates
"""
from __future__ import annotations

import logging
import math
import re
from datetime import UTC, date, datetime, timedelta

log = logging.getLogger("pantheon_ical")

_DT_RE = re.compile(r"^(DTSTART|DTEND)(?:;[^:]*)?:(.+)$", re.IGNORECASE)
_DUR_RE = re.compile(r"^DURATION(?:;[^:]*)?:(.+)$", re.IGNORECASE)
# RFC 5545 §3.3.6 duration: [+-]P(nW | nD? (T nH? nM? nS?)?)
_DURATION_VALUE_RE = re.compile(
    r"^(?P<sign>[+-])?P(?:(?P<w>\d+)W)?(?:(?P<d>\d+)D)?(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?)?$",
    re.IGNORECASE)


def _parse_duration(value: str) -> timedelta | None:
    """An RFC 5545 DURATION value (`P4D`, `P1W`, `P1DT12H`) → timedelta, or None if unparseable / non-positive.
    Used when a VEVENT carries DTSTART + DURATION instead of DTEND (which OTA feeds emit) — without this the event
    would silently collapse to a single night, UNDER-blocking the calendar (the double-booking this guards against)."""
    m = _DURATION_VALUE_RE.match((value or "").strip())
    if not m or not any(m.group(g) for g in ("w", "d", "h", "m", "s")):
        return None
    total = timedelta(weeks=int(m["w"] or 0), days=int(m["d"] or 0), hours=int(m["h"] or 0),
                      minutes=int(m["m"] or 0), seconds=int(m["s"] or 0))
    if total <= timedelta(0):                              # zero / negative duration is not a bookable span
        return None
    return -total if m["sign"] == "-" else total


def build_ical(ranges: list[tuple[date, date]], *, uid_ns: str = "", cal_name: str = "Availability",
               prodid: str = "-//pantheon-ical//EN", summary: str = "Unavailable") -> str:
    """Serialise half-open [start, end) BUSY date ranges into an RFC 5545 VCALENDAR — the inverse of
    `parse_ical`. Each range is one all-day VEVENT (`DTSTART;VALUE=DATE` .. `DTEND;VALUE=DATE`, end EXCLUSIVE),
    so a consumer re-imports the same ranges.

    Emits ONLY the busy dates + a fixed generic SUMMARY — no free-text that could leak guest data if the feed
    is public. Deterministic (no wall-clock) → an unchanged calendar serialises byte-identically (ETag/cache
    friendly + testable). Invalid ranges (end <= start, non-dates) and exact duplicates are skipped. `uid_ns`
    (e.g. a listing id) only keeps UIDs globally unique across feeds."""
    def _d(x: date) -> str:
        return f"{x.year:04d}{x.month:02d}{x.day:02d}"

    ns = re.sub(r"[^A-Za-z0-9-]", "", uid_ns or "")[:64] or "cal"
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{prodid}", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             f"X-WR-CALNAME:{cal_name}"]
    seen: set[tuple[date, date]] = set()
    for start, end in ranges:
        if not (isinstance(start, date) and isinstance(end, date)) or end <= start or (start, end) in seen:
            continue
        seen.add((start, end))
        lines += ["BEGIN:VEVENT", f"UID:{_d(start)}-{_d(end)}-{ns}@pantheon-ical",
                  f"DTSTAMP:{_d(start)}T000000Z", f"DTSTART;VALUE=DATE:{_d(start)}",
                  f"DTEND;VALUE=DATE:{_d(end)}", f"SUMMARY:{summary}", "TRANSP:OPAQUE", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"                                # RFC 5545 §3.1 CRLF line breaks


def _parse_date(value: str) -> date | None:
    """A DATE (YYYYMMDD) or DATE-TIME (YYYYMMDDTHHMMSS[Z]) value → its date. None if unparseable."""
    m = re.match(r"^\s*(\d{4})(\d{2})(\d{2})", value or "")
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def parse_ical(text: str, *, max_events: int = 730) -> list[tuple[date, date]]:
    """Parse a feed into half-open [start, end) blocked DATE ranges from its VEVENTs. End is taken from DTEND, else
    DTSTART+DURATION (OTAs emit this), else a single night. RRULE recurrence is expanded (bounded). Caps at
    max_events."""
    text = re.sub(r"\r?\n[ \t]", "", text or "")            # unfold continuation lines (RFC 5545 §3.1)
    ranges: list[tuple[date, date]] = []
    in_event = False
    start: date | None = None
    end: date | None = None
    duration: timedelta | None = None
    rrule: str | None = None
    exdates: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        upper = line.upper()
        if upper == "BEGIN:VEVENT":
            in_event, start, end, duration, rrule, exdates = True, None, None, None, None, []
        elif upper == "END:VEVENT":
            if in_event and start is not None:
                # end precedence: an explicit DTEND, else DTSTART+DURATION (OTAs emit this), else a single night.
                if end is None and duration is not None:
                    end = start + timedelta(days=max(1, math.ceil(duration.total_seconds() / 86400)))
                e = end if (end is not None and end > start) else start + timedelta(days=1)
                for os_, oe_ in _expand(start, e, rrule, exdates):
                    ranges.append((os_, oe_))
                    if len(ranges) >= max_events:
                        break
            in_event = False
            if len(ranges) >= max_events:
                break
        elif in_event:
            m = _DT_RE.match(line)
            if m:
                d = _parse_date(m.group(2))
                if d is not None:
                    if m.group(1).upper() == "DTSTART":
                        start = d
                    else:
                        end = d
            elif upper.startswith("DURATION"):
                md = _DUR_RE.match(line)
                if md:
                    duration = _parse_duration(md.group(1))
            elif upper.startswith("RRULE:"):
                rrule = line
            elif upper.startswith("EXDATE"):
                exdates.append(line)
    return ranges


def _parse_datetime(value: str, tzid: str | None = None) -> datetime | None:
    """A DATE-TIME (YYYYMMDDTHHMMSS[Z]) value → datetime; DATE-only → midnight. A trailing Z → UTC-aware; an
    explicit TZID → aware in THAT zone; otherwise naive wall-clock. Convert any aware value to your local zone."""
    m = re.match(r"^\s*(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2}))?", value or "")
    if not m:
        return None
    try:
        dt = datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4] or 0), int(m[5] or 0), int(m[6] or 0))
    except ValueError:
        return None
    if (value or "").strip().endswith("Z"):
        return dt.replace(tzinfo=UTC)
    if tzid:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            return dt.replace(tzinfo=ZoneInfo(tzid))
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            return dt                                       # unresolvable (e.g. a Windows zone) → naive/local
    return dt


def _expand(start, end, rrule_line: str | None, exdate_lines: list[str],
            *, max_occ: int = 400, horizon_days: int = 400) -> list:
    """Expand a recurring VEVENT (RRULE) into concrete (start, end) occurrences via python-dateutil — bounded to
    `max_occ`/`horizon_days` so an unbounded 'every Monday forever' can't blow up. Works for date OR datetime
    start/end. ANY parse issue → the single master occurrence, so a rule we don't model never under-blocks."""
    if not rrule_line:
        return [(start, end)]
    is_date = not isinstance(start, datetime)
    s_dt = datetime(start.year, start.month, start.day) if is_date else start
    e_dt = datetime(end.year, end.month, end.day) if is_date else end
    try:
        from dateutil.rrule import rrulestr
        spec = "\n".join([rrule_line, *exdate_lines])
        if s_dt.tzinfo is None:                             # dateutil rejects a naive DTSTART paired with a UTC (…Z) UNTIL;
            spec = re.sub(r"(UNTIL=\d{8}(?:T\d{6})?)Z", r"\1", spec)   # make UNTIL floating to match, else the fallback
        rule = rrulestr(spec, dtstart=s_dt, forceset=True)  # would collapse to the master (a silent under-block → double-book)
        dur = e_dt - s_dt
        # Anchor to NOW, not DTSTART: an established recurrence has a DTSTART years in the past, so a
        # DTSTART-anchored horizon would expand only historical occurrences and leave live dates unblocked.
        now = datetime.now(s_dt.tzinfo) if s_dt.tzinfo else datetime.now()   # noqa: DTZ005
        window = rule.between(now - timedelta(days=2), now + timedelta(days=horizon_days), inc=True)[:max_occ]
        return [(occ.date(), (occ + dur).date()) if is_date else (occ, occ + dur) for occ in window]
        # empty = no current/future occurrences → block nothing (an expired/EXDATE'd rule must not re-block its master)
    except Exception:                                       # noqa: BLE001 — safe single-occurrence fallback
        # The fallback is safe for a rule we don't model (it OVER-blocks via the master). But it ALSO catches a
        # broken/missing dateutil or a malformed rule — and there it UNDER-blocks a recurring booking (the
        # double-book this guards against). Log it LOUDLY so a broken expander is visible, not silent. [audit]
        log.warning("RRULE expansion failed for %r — falling back to the single master occurrence; a recurring "
                    "block may under-block. Check python-dateutil is installed and the rule is valid.",
                    rrule_line, exc_info=True)
        return [(start, end)]


def parse_ical_slots(text: str, *, max_events: int = 2000) -> list[tuple[datetime, datetime]]:
    """Parse a feed into [start, end) DATETIME slots from its VEVENTs (for appointment/class calendars). Each
    VEVENT needs both DTSTART and DTEND with a positive span; events with no usable end are skipped."""
    text = re.sub(r"\r?\n[ \t]", "", text or "")
    slots: list[tuple[datetime, datetime]] = []
    in_event = False
    start: datetime | None = None
    end: datetime | None = None
    rrule: str | None = None
    exdates: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        upper = line.upper()
        if upper == "BEGIN:VEVENT":
            in_event, start, end, rrule, exdates = True, None, None, None, []
        elif upper == "END:VEVENT":
            try:
                if in_event and start is not None and end is not None and end > start:
                    for os_, oe_ in _expand(start, end, rrule, exdates):
                        slots.append((os_, oe_))
                        if len(slots) >= max_events:
                            break
            except (TypeError, ValueError):                 # mixed tz-aware/naive DTSTART vs DTEND → skip the event
                pass
            in_event = False
            if len(slots) >= max_events:
                break
        elif in_event:
            m = _DT_RE.match(line)
            if m:
                tzid_m = re.search(r"TZID=([^:;]+)", line)
                d = _parse_datetime(m.group(2), tzid_m.group(1) if tzid_m else None)
                if d is not None:
                    if m.group(1).upper() == "DTSTART":
                        start = d
                    else:
                        end = d
            elif upper.startswith("RRULE:"):
                rrule = line
            elif upper.startswith("EXDATE"):
                exdates.append(line)
    return slots
