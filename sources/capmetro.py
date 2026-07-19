"""CapMetro adapter — Legistar InSite calendar page + Legistar Web API.

Two layers, on purpose. Observed 2026-07-18: the public Web API
(webapi.legistar.com/v1/capmetrotx/events) only carries meetings once their
agenda is published, while the InSite calendar page (Calendar.aspx) lists
upcoming meetings earlier — e.g. the 7/27/2026 Board meeting appeared on the
page while the API still returned nothing after 6/22.

Since the whole point is early when/where:
  1. scrape Calendar.aspx for the schedule (dates/times/locations, earliest),
  2. query the API and merge in enrichment (agenda URL, meeting detail page)
     for whichever meetings it already knows about.

Merge key = (body slug, date, start time) — same identity rule as the stable
UID, so an event stays itself as it graduates from "scheduled" to "agenda
posted", and two same-day meetings of one body never collapse into one.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from bs4 import BeautifulSoup

from caltools.model import Event, slugify

SOURCE = "capmetro"
CALENDAR_URL = "https://capmetrotx.legistar.com/Calendar.aspx"
API_URL = "https://webapi.legistar.com/v1/capmetrotx/events"
MEETING_LENGTH = timedelta(hours=2)  # Legistar gives start only; assume 2h.

DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s*[AP]M$", re.IGNORECASE)
# InSite marks cancellations in the time column and/or appends to the name.
CANCEL_RE = re.compile(r"cancell?ed|postponed|deferred|rescheduled", re.IGNORECASE)
NAME_SUFFIX_RE = re.compile(
    r"\s*[-–(]\s*(cancell?ed|postponed|deferred|rescheduled)\)?\s*$", re.IGNORECASE
)


def _parse_time(text: str) -> datetime | None:
    try:
        return datetime.strptime(text.strip().upper(), "%I:%M %p")
    except ValueError:
        return None


def _time_key(ev: Event) -> str:
    return ev.start.strftime("%H%M") if isinstance(ev.start, datetime) else ""


def parse_calendar_html(html: str) -> list[Event]:
    """Pull meeting rows out of the InSite calendar page.

    Deliberately tolerant: rather than binding to Legistar's control IDs, we
    scan every table row for a date-shaped cell, then read name / time /
    location relative to it. Survives cosmetic template changes; the health
    check in build.py catches it if the page changes beyond recognition.
    """
    soup = BeautifulSoup(html, "html.parser")
    events: list[Event] = []
    for row in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
        if len(cells) < 3:
            continue
        date_idx = next((i for i, c in enumerate(cells) if DATE_RE.match(c)), None)
        if date_idx is None or date_idx == 0:
            continue
        raw_name = cells[0]
        if not raw_name:
            continue
        # Strip cancellation markers from the name so the event's identity
        # (UID) survives a rename like "Board of Directors - CANCELLED".
        name = NAME_SUFFIX_RE.sub("", raw_name).strip()
        status = "CANCELLED" if CANCEL_RE.search(raw_name) else "CONFIRMED"

        meeting_date = datetime.strptime(cells[date_idx], "%m/%d/%Y")
        time_idx, tval = next(
            ((i, _parse_time(c)) for i, c in enumerate(cells[date_idx + 1:], date_idx + 1)
             if TIME_RE.match(c.replace("\xa0", " ").strip())),
            (None, None),
        )
        if tval is not None:
            start = meeting_date.replace(hour=tval.hour, minute=tval.minute)
            end = start + MEETING_LENGTH
        else:
            start, end = meeting_date.date(), None
            # No parseable time — check whether the time column says why
            # (InSite puts "Cancelled"/"Deferred" there).
            trailing = " ".join(cells[date_idx + 1:])
            if CANCEL_RE.search(trailing):
                status = "CANCELLED"
        location = ""
        loc_idx = time_idx if time_idx is not None else date_idx + 1
        if loc_idx + 1 < len(cells):
            location = re.sub(r"\s{2,}", " ", cells[loc_idx + 1])[:200]
            if CANCEL_RE.fullmatch(location.strip()):
                location = ""
        detail_url = ""
        link = row.find("a", href=re.compile(r"MeetingDetail", re.IGNORECASE))
        if link and link.get("href"):
            detail_url = "https://capmetrotx.legistar.com/" + link["href"].lstrip("/")
        events.append(
            Event(
                source=SOURCE,
                summary=name,
                start=start,
                end=end,
                location=location,
                url=detail_url or CALENDAR_URL,
                status=status,
            )
        )
    return events


def api_events(session, since: datetime) -> list[dict]:
    params = {
        "$filter": f"EventDate ge datetime'{since.strftime('%Y-%m-%d')}'",
        "$orderby": "EventDate",
    }
    resp = session.get(API_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def merge_api(events: list[Event], api_rows: list[dict]) -> list[Event]:
    """Enrich scraped events with API data; add API-only events.

    Match on (body slug, date, time); if the times disagree but the body has
    exactly one meeting that day, fall back to that one — a reschedule
    shouldn't produce a phantom duplicate.
    """
    by_key: dict[tuple[str, str, str], Event] = {
        (slugify(e.summary), e.start.strftime("%Y%m%d"), _time_key(e)): e
        for e in events
    }

    def _enrich(ev: Event, insite: str, agenda: str) -> None:
        if insite:
            ev.url = insite
        if agenda and agenda not in ev.description:
            ev.description = (ev.description + f"\nAgenda: {agenda}").strip()

    for row in api_rows:
        d = (row.get("EventDate") or "")[:10].replace("-", "")
        slug = slugify(row.get("EventBodyName") or "")
        t = _parse_time(row.get("EventTime") or "")
        tkey = t.strftime("%H%M") if t else ""
        agenda = row.get("EventAgendaFile") or ""
        insite = row.get("EventInSiteURL") or ""

        if (slug, d, tkey) in by_key:
            _enrich(by_key[(slug, d, tkey)], insite, agenda)
            continue
        same_day = [e for (s, dd, _), e in by_key.items() if s == slug and dd == d]
        if len(same_day) == 1:
            _enrich(same_day[0], insite, agenda)
            continue

        try:
            base = datetime.strptime((row.get("EventDate") or "")[:10], "%Y-%m-%d")
        except ValueError:
            continue
        start = base.replace(hour=t.hour, minute=t.minute) if t else base.date()
        ev = Event(
            source=SOURCE,
            summary=row.get("EventBodyName") or "Meeting",
            start=start,
            end=(start + MEETING_LENGTH) if isinstance(start, datetime) else None,
            location=row.get("EventLocation") or "",
            url=insite or CALENDAR_URL,
            description=f"Agenda: {agenda}" if agenda else "",
        )
        by_key[(slug, d, tkey)] = ev
    return list(by_key.values())


def finalize(events: list[Event]) -> list[Event]:
    """Apply display polish AFTER all merging is done.

    The org prefix ("CapMetro - ...") is display-only: the UID is pinned to
    the *raw* body name first, so renaming how events display never changes
    their identity — subscribers' calendars update in place, no duplicates.
    """
    for ev in events:
        ev.uid = ev.stable_uid()  # freeze identity from the raw summary
        if not ev.summary.startswith("CapMetro"):
            ev.summary = f"CapMetro - {ev.summary}"
    return events


def fetch(session) -> list[Event]:
    resp = session.get(CALENDAR_URL, timeout=30)
    resp.raise_for_status()
    events = parse_calendar_html(resp.text)
    since = datetime.now() - timedelta(days=90)
    try:
        rows = api_events(session, since)
    except Exception as exc:  # API enrichment is best-effort
        print(f"[capmetro] WARNING: API enrichment failed: {exc}")
        rows = []
    return finalize(merge_api(events, rows))
