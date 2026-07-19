"""Emit RFC 5545 iCalendar files from normalized Events.

Hand-rolled on purpose: the format is simple text, and owning the emitter
means we control folding, escaping, timezone handling, and UID stability
without fighting a library's opinions.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Iterable
from zoneinfo import ZoneInfo

from .model import Event

CRLF = "\r\n"
CENTRAL = ZoneInfo("America/Chicago")

# America/Chicago with US DST rules (2nd Sunday March / 1st Sunday November).
VTIMEZONE = """BEGIN:VTIMEZONE
TZID:America/Chicago
BEGIN:DAYLIGHT
TZOFFSETFROM:-0600
TZOFFSETTO:-0500
TZNAME:CDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0500
TZOFFSETTO:-0600
TZNAME:CST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE"""


def escape(value: str) -> str:
    """Escape text per RFC 5545 (backslash, semicolon, comma, newline)."""
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def fold(line: str) -> str:
    """Fold long content lines at 75 octets (continuation lines start with a space)."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    parts = []
    limit = 75  # continuation lines get a leading space, so cap them at 74
    while encoded:
        cut = min(limit, len(encoded))
        # Never cut in the middle of a UTF-8 multibyte sequence.
        while cut < len(encoded) and (encoded[cut] & 0xC0) == 0x80:
            cut -= 1
        parts.append(encoded[:cut].decode("utf-8"))
        encoded = encoded[cut:]
        limit = 74
    return (CRLF + " ").join(parts)


def _dt(value: datetime | date, prop: str) -> str:
    if isinstance(value, datetime):
        # A tz-aware datetime (e.g. UTC from a source feed) must be converted
        # to Central wall time before being labeled TZID=America/Chicago;
        # naive datetimes are already Central wall time by our convention.
        if value.tzinfo is not None:
            value = value.astimezone(CENTRAL)
        return f"{prop};TZID=America/Chicago:{value.strftime('%Y%m%dT%H%M%S')}"
    return f"{prop};VALUE=DATE:{value.strftime('%Y%m%d')}"


def event_block(ev: Event, dtstamp: str) -> list[str]:
    lines = ["BEGIN:VEVENT"]
    lines.append(f"UID:{ev.stable_uid()}")
    lines.append(f"DTSTAMP:{dtstamp}")
    lines.append(_dt(ev.start, "DTSTART"))
    if ev.end is not None:
        lines.append(_dt(ev.end, "DTEND"))
    lines.append(f"SUMMARY:{escape(ev.summary)}")
    if ev.location:
        lines.append(f"LOCATION:{escape(ev.location)}")
    if ev.url:
        lines.append(f"URL:{ev.url}")
    if ev.description:
        lines.append(f"DESCRIPTION:{escape(ev.description)}")
    if ev.status and ev.status != "CONFIRMED":
        lines.append(f"STATUS:{ev.status}")
    lines.append("END:VEVENT")
    return lines


def emit(
    events: Iterable[Event],
    calname: str,
    generated_at: datetime,
    color: str | None = None,
) -> str:
    """Render a complete .ics document. generated_at must be UTC.

    color: optional "#RRGGBB" — Apple clients use it as the calendar's
    default color at subscribe time; others ignore it harmlessly.
    """
    dtstamp = generated_at.strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//changesaroundme//calendars//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape(calname)}",
        "X-WR-TIMEZONE:America/Chicago",
        # Suggested re-poll cadence; honored by some clients (e.g. Outlook),
        # ignored by others (Apple/Google poll on their own schedule).
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
        "X-PUBLISHED-TTL:PT12H",
    ]
    if color:
        lines.append(f"X-APPLE-CALENDAR-COLOR:{color}")
    lines.extend(VTIMEZONE.split("\n"))
    seen = set()
    for ev in sorted(events, key=lambda e: e.start.isoformat()):
        uid = ev.stable_uid()
        if uid in seen:  # merge safety: last writer does NOT win; first does
            continue
        seen.add(uid)
        lines.extend(event_block(ev, dtstamp))
    lines.append("END:VCALENDAR")
    return CRLF.join(fold(l) for l in lines) + CRLF
