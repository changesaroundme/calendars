"""Shared machinery for orgs on Granicus Legistar (InSite + Web API).

Observed on both CapMetro and Austin: the public Web API
(webapi.legistar.com/v1/{client}/events) only lists meetings once their
agenda is published, while the InSite calendar page (Calendar.aspx) shows
scheduled meetings earlier. Since early when/where is the point:

  1. scrape Calendar.aspx for the schedule (earliest dates/times/locations),
  2. query the API and merge in enrichment (agenda URL, detail page)
     for whichever meetings it already knows about.

Merge key = (body slug, date, start time) — the same identity rule as the
stable UID, so an event stays itself as it graduates from "scheduled" to
"agenda posted", and two same-day meetings of one body never collapse.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from bs4 import BeautifulSoup

from caltools.model import Event, slugify

DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s*[AP]M$", re.IGNORECASE)
# InSite marks cancellations in the time column and/or appends to the name.
CANCEL_RE = re.compile(r"cancell?ed|postponed|deferred|rescheduled", re.IGNORECASE)
NAME_SUFFIX_RE = re.compile(
    r"\s*[-–(]\s*(cancell?ed|postponed|deferred|rescheduled)\)?\s*$", re.IGNORECASE
)


def parse_time(text: str) -> datetime | None:
    try:
        return datetime.strptime(text.strip().upper(), "%I:%M %p")
    except ValueError:
        return None


def _time_key(ev: Event) -> str:
    return ev.start.strftime("%H%M") if isinstance(ev.start, datetime) else ""


@dataclass
class Legistar:
    source: str          # adapter key, e.g. "capmetro"
    client: str          # Web API client slug, e.g. "capmetrotx"
    host: str            # InSite host, e.g. "capmetrotx.legistar.com"
    prefix: str          # display prefix, e.g. "CapMetro"; "" = no prefix
    meeting_length: timedelta = timedelta(hours=2)
    # Display renames applied AFTER the UID is frozen (raw body name is the
    # identity; these are presentation only), e.g.
    # {"Budget Meeting of the Austin City Council": "City Council - Budget Meeting"}
    display_names: dict = None
    # Location cleanups: if a location starts with a key, replace it with the
    # canonical value (sources often publish just a room name or junk-suffixed
    # venue), e.g. {"Rosa Parks Boardroom": "Rosa Parks Boardroom, ..."}
    location_fixes: dict = None

    @property
    def calendar_url(self) -> str:
        return f"https://{self.host}/Calendar.aspx"

    @property
    def api_url(self) -> str:
        return f"https://webapi.legistar.com/v1/{self.client}/events"

    def parse_calendar_html(self, html: str) -> list[Event]:
        """Pull meeting rows out of the InSite calendar page.

        Deliberately tolerant: rather than binding to Legistar's control IDs,
        we scan every table row for a date-shaped cell, then read name / time
        / location relative to it. Survives cosmetic template changes; the
        health check in build.py catches a redesign beyond recognition.
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
                ((i, parse_time(c)) for i, c in enumerate(cells[date_idx + 1:], date_idx + 1)
                 if TIME_RE.match(c.replace("\xa0", " ").strip())),
                (None, None),
            )
            if tval is not None:
                start = meeting_date.replace(hour=tval.hour, minute=tval.minute)
                end = start + self.meeting_length
            else:
                start, end = meeting_date.date(), None
                # No parseable time — the time column may say why.
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
                detail_url = f"https://{self.host}/" + link["href"].lstrip("/")
            events.append(
                Event(
                    source=self.source,
                    summary=name,
                    start=start,
                    end=end,
                    location=location,
                    url=detail_url or self.calendar_url,
                    status=status,
                )
            )
        return events

    def api_events(self, session, since: datetime) -> list[dict]:
        params = {
            "$filter": f"EventDate ge datetime'{since.strftime('%Y-%m-%d')}'",
            "$orderby": "EventDate",
        }
        resp = session.get(self.api_url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def merge_api(self, events: list[Event], api_rows: list[dict]) -> list[Event]:
        """Enrich scraped events with API data; add API-only events.

        Match on (body slug, date, time); if the times disagree but the body
        has exactly one meeting that day, fall back to that one — a
        reschedule shouldn't produce a phantom duplicate.
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
            t = parse_time(row.get("EventTime") or "")
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
                source=self.source,
                summary=row.get("EventBodyName") or "Meeting",
                start=start,
                end=(start + self.meeting_length) if isinstance(start, datetime) else None,
                location=row.get("EventLocation") or "",
                url=insite or self.calendar_url,
                description=f"Agenda: {agenda}" if agenda else "",
            )
            by_key[(slug, d, tkey)] = ev
        return list(by_key.values())

    def finalize(self, events: list[Event]) -> list[Event]:
        """Freeze UIDs from the raw body name, then apply display polish."""
        for ev in events:
            ev.uid = ev.stable_uid()
            low = ev.summary.lower()
            if "work session" in low:
                ev.kind = "work-session"
            elif "budget" in low:
                ev.kind = "budget"
            if self.display_names:
                ev.summary = self.display_names.get(ev.summary, ev.summary)
            if self.prefix and not ev.summary.startswith(self.prefix):
                ev.summary = f"{self.prefix} - {ev.summary}"
            if self.location_fixes:
                for pfx, canonical in self.location_fixes.items():
                    if ev.location.startswith(pfx):
                        ev.location = canonical
                        break
        return events

    def fetch(self, session) -> list[Event]:
        resp = session.get(self.calendar_url, timeout=30)
        resp.raise_for_status()
        events = self.parse_calendar_html(resp.text)
        since = datetime.now() - timedelta(days=90)
        try:
            rows = self.api_events(session, since)
        except Exception as exc:  # API enrichment is best-effort
            print(f"[{self.source}] WARNING: API enrichment failed: {exc}")
            rows = []
        return self.finalize(self.merge_api(events, rows))
