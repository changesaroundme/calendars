"""CTRMA adapter — Central Texas Regional Mobility Authority board meetings.

Their WordPress site renders upcoming meetings as cards on /board-meetings/
(.board-meeting-card: month / day / year divs, a "9:00 am" caption, and a
link to the meeting's detail page). The board-meeting REST endpoint exists
but exposes no meeting metadata, so the cards are the source of truth.

Meetings are consistently at their offices (confirmed on detail pages);
per-meeting exceptions would surface on the linked detail page. Past
meetings drop off the card grid, so this feed carries upcoming meetings
only — the committed data/ snapshots keep the historical record.

CTRMA is a regional mobility authority, so its notices also hit the Texas
SOS open-meetings portal ~72h ahead — a future SOS adapter can serve as a
cross-check.

Source: /board-meetings/upcoming/ — the full forward schedule (through
year-end), not /board-meetings/, which only teases the next two meetings.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from bs4 import BeautifulSoup

from caltools.model import Event

SOURCE = "ctrma"
PAGE_URL = "https://www.mobilityauthority.com/board-meetings/upcoming/"
LOCATION = "3300 N. IH 35, Suite 300, Austin, TX 78705"
MEETING_LENGTH = timedelta(hours=2)


def _parse_time(text: str) -> datetime | None:
    for fmt in ("%I:%M %p", "%I %p"):
        try:
            return datetime.strptime(text.strip().upper(), fmt)
        except ValueError:
            continue
    return None


def parse_page(html: str) -> list[Event]:
    soup = BeautifulSoup(html, "html.parser")
    events: list[Event] = []
    for card in soup.select(".board-meeting-card"):
        date_div = card.select_one(".date")
        if date_div is None:
            continue
        parts = [d.get_text(" ", strip=True) for d in date_div.find_all("div")]
        try:
            month_abbr, day, year = parts[0], int(parts[1]), int(parts[2])
            month = datetime.strptime(month_abbr[:3], "%b").month
        except (IndexError, ValueError):
            continue
        caption = card.select_one(".caption")
        tval = _parse_time(caption.get_text(" ", strip=True)) if caption else None
        link = card.select_one("h3 a") or card.find("a")
        title = link.get_text(" ", strip=True) if link else "Board Meeting"
        if tval is not None:
            start = datetime(year, month, day, tval.hour, tval.minute)
            end = start + MEETING_LENGTH
        else:
            start, end = datetime(year, month, day).date(), None
        events.append(
            Event(
                source=SOURCE,
                summary=title,
                start=start,
                end=end,
                location=LOCATION,
                url=(link.get("href") or PAGE_URL) if link else PAGE_URL,
            )
        )
    return events


def finalize(events: list[Event]) -> list[Event]:
    """Freeze UIDs from the raw title, then apply the display prefix."""
    for ev in events:
        ev.uid = ev.stable_uid()
        if not ev.summary.startswith("CTRMA"):
            ev.summary = f"CTRMA - {ev.summary}"
    return events


def fetch(session) -> list[Event]:
    resp = session.get(PAGE_URL, timeout=30)
    resp.raise_for_status()
    return finalize(parse_page(resp.text))
