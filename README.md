# pantheon-ical

**Round-trip iCal (RFC 5545) read + write for booking / availability calendars — in under 200 lines.**

> The guarantee: import busy dates from the systems that speak iCal (Airbnb, Booking.com, Google Calendar, Vrbo, …), **and** export your own busy dates back as a feed they import — with the same half-open date model on both sides, so a block on any channel propagates to the others without double-booking.

Extracted from [PANTHEON](https://pantheonlabs.co.uk), where it drives two-way calendar sync for the holiday-let and appointment businesses it hosts.

Most iCal libraries are large, general-purpose, and read-only. This is the small, opinionated subset you actually need to keep an availability calendar in sync — and it does the **export** side too, which is the half most libraries skip.

## What it does

```python
from pantheon_ical import parse_ical, build_ical, parse_ical_slots

# 1. Import: an OTA feed → busy DATE ranges (half-open, checkout day exclusive)
busy = parse_ical(text)          # [(date(2026,7,1), date(2026,7,5)), ...]  — the 1st–5th = 4 nights, 5th free

# 2. Export: your busy ranges → a VCALENDAR feed OTHER systems import
feed = build_ical(busy, uid_ns="my-listing")
#   → hand this URL to Booking.com/Airbnb "Import calendar" and your blocks propagate outward

# 3. Appointments: DATETIME slots, timezone-aware (for class/booking calendars)
slots = parse_ical_slots(text)   # [(datetime, datetime), ...]
```

## Design choices that matter (learned against real OTA feeds)

- **Half-open intervals, checkout-day exclusive.** `DTSTART:20260701 .. DTEND:20260705` blocks four nights and leaves the 5th free for the next arrival — matching how every booking system models a stay.
- **RRULE recurrence is expanded around _now_, not DTSTART.** An "every Monday since 2024" rule has a years-old start date; a naive expander would only produce historical dates and leave the live window bookable. This anchors the horizon to the present.
- **Unparseable rules fall back to the master occurrence.** A recurrence syntax we don't model can never silently _under_-block real availability (which is what causes a double-booking) — the safe failure direction.
- **`build_ical` is deterministic** (no wall-clock) and emits only busy dates + a fixed generic summary — so a public feed leaks no guest data, and an unchanged calendar is byte-identical (ETag/cache friendly).

## Install

```bash
pip install pantheon-ical      # or copy the single pantheon_ical.py file
```

Depends only on `python-dateutil` (for RRULE expansion).

## License

Apache-2.0. See `LICENSE`.
