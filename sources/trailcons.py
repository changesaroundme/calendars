"""The Trail Conservancy adapter — added 2026-10-01.

The Trail Conservancy ("TTC" on its own site; "Trail Cons." here, because
TTC already means the Texas Transportation Commission on these calendars)
is the nonprofit that runs the Ann and Roy Butler Hike-and-Bike Trail with
Austin Parks and Recreation. Its site runs WordPress with The Events
Calendar plugin, whose REST API lists every event as JSON:

    /wp-json/tribe/events/v1/events?categories=community-engagement,project-celebrations

Most of what TTC posts is programming (volunteer workdays, bird walks,
Music on the Trail, fundraisers). Ian picked the two categories that are
about projects (2026-10-01): "Community Engagement" (open houses and design
workshops, e.g. Holly Shores / Eastside Family Garden) and "Project
Celebrations" (ribbon cuttings and openings, e.g. the Wishbone Bridge).
CATEGORIES below is the whole filter; one request asks for both (the API
takes a comma-separated list and returns the union). Volume is low (7
events from April 2025 to October 2026), so the registry row says
`expect: sporadic`.

Times are Austin wall time with the plugin's own timezone field. The
description is the event page's HTML, which repeats the page chrome
("« All Events", the date/time block, "Add to Calendar"); those are
dropped and the rest is kept as plain text, trimmed at a paragraph.

Identity: the plugin's post id (trailcons-<id>@…), so a retitled event
updates in place. The `trailcons-` prefix is what the embed's org filter
matches.
"""
from __future__ import annotations

import html
import json
import pathlib
import re
from datetime import date, datetime, timedelta

from bs4 import BeautifulSoup

from caltools.model import Event

SOURCE = "trailcons"
PREFIX = "Trail Cons. - "
API = "https://thetrailconservancy.org/wp-json/tribe/events/v1/events"
# category slug -> event kind. Add a category here (and to the registry
# row's URL) to carry it.
CATEGORIES = {
    "community-engagement": "engagement",
    "project-celebrations": "special",
}
FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures"
LOOKBACK = timedelta(days=365)
DESC_MAX = 900

CANCEL_RE = re.compile(r"\b(cancell?ed|postponed)\b", re.IGNORECASE)
# The event page's own date/time block, repeated inside the description:
# "April 12 @ 9:00 am – 11:00 am" (split over <br>s in the HTML).
DATE_BLOCK_RE = re.compile(
    r"^(January|February|March|April|May|June|July|August|September|October"
    r"|November|December)\s+\d{1,2}\s*@", re.IGNORECASE)
CHROME_RE = re.compile(r"^(« All Events|Add to Calendar|Learn More)$", re.IGNORECASE)
COORDS_RE = re.compile(r"^-?\d{1,3}\.\d+,\s*-?\d{1,3}\.\d+$")

_problems: list[str] = []


def health_problems() -> list[str]:
    return list(_problems)


def _clean(s: str) -> str:
    s = re.sub(r"\s+", " ", html.unescape(s or "")).strip()
    return re.sub(r"\s+([.,;:!?])", r"\1", s)   # "Rialto Studio ." from inline links


def _description(raw: str, url: str) -> str:
    """Event-page HTML -> a few plain-text paragraphs plus the link."""
    soup = BeautifulSoup(raw or "", "html.parser")
    for tag in soup.find_all(["img", "iframe", "nav", "svg"]):
        tag.decompose()                     # nav = the previous/next-event links
    for a in soup.find_all("a"):
        href, label = a.get("href", ""), _clean(a.get_text(" "))
        if "calendar.google.com" in href:
            a.decompose()
        elif label.lower().startswith("rsvp"):
            a.replace_with(f"RSVP: {href}")
    blocks: list[str] = []
    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
        if el.name in ("p", "li") and el.find_parent("li") and el.name == "p":
            continue
        text = _clean(el.get_text(" "))
        if not text or CHROME_RE.match(text) or DATE_BLOCK_RE.match(text):
            continue
        if el.name == "h1":                 # the title again
            continue
        links = el.find_all("a")
        if el.name == "li" and len(links) == 1 and "maps.app.goo.gl" in links[0].get("href", "") \
                and _clean(links[0].get_text(" ")) == text:
            continue                         # the venue as a map link; LOCATION has it
        blocks.append(("- " if el.name == "li" else "") + text)
    out: list[str] = []
    size = 0
    for b in blocks:
        if size + len(b) > DESC_MAX and out:
            out.append("…")
            break
        out.append(b)
        size += len(b)
    body = "\n".join(out)
    return (body + "\n\n" if body else "") + f"Details: {url}"


def _kind(e: dict) -> str:
    slugs = [c.get("slug", "") for c in e.get("categories", [])]
    for slug, kind in CATEGORIES.items():   # engagement wins if both
        if slug in slugs:
            return kind
    return "engagement"


def _wall(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def parse_events(payload: dict) -> list[Event]:
    events: list[Event] = []
    for e in payload.get("events", []):
        if e.get("status", "publish") != "publish":
            continue
        title = _clean(e.get("title", ""))
        if not title or not e.get("start_date"):
            continue
        url = e.get("url", "")
        venue = e.get("venue") if isinstance(e.get("venue"), dict) else {}
        name = _clean(venue.get("venue", ""))
        addr = _clean(venue.get("address", ""))
        city = _clean(venue.get("city", ""))
        if addr and city and city not in addr:
            addr = f"{addr}, {city}, TX {venue.get('zip') or ''}".strip()
        if COORDS_RE.match(addr):           # a map pin, not a street address
            location = f"{name} ({addr})" if name else addr
        elif addr and name.startswith(addr.split(",")[0]):
            location = addr                 # the venue IS the address (Fishing Pier)
        else:
            location = ", ".join(p for p in (name, addr) if p)
        if e.get("is_virtual") and not location:
            location = "Virtual"
        if e.get("all_day"):
            start = _wall(e["start_date"]).date()
            end = _wall(e["end_date"]).date() if e.get("end_date") else None
        else:
            start = _wall(e["start_date"])
            end = _wall(e["end_date"]) if e.get("end_date") else None
        events.append(Event(
            source=SOURCE,
            summary=PREFIX + title,
            start=start,
            end=end,
            location=location,
            url=url,
            status="CANCELLED" if CANCEL_RE.search(title) else "CONFIRMED",
            kind=_kind(e),
            uid=f"trailcons-{e['id']}@calendars.changesaroundme.com",
            description=_description(e.get("description", ""), url),
        ))
    return events


def fetch(session) -> list[Event]:
    _problems.clear()
    since = (date.today() - LOOKBACK).isoformat()
    events: list[Event] = []
    page = 1
    while True:
        # Categories go in the URL by hand: `params=` would encode the comma
        # as %2C and the registry's prefix match would miss the page.
        resp = session.get(f"{API}?categories={','.join(CATEGORIES)}",
                           params={"start_date": since, "per_page": 50,
                                   "page": page}, timeout=30)
        # No matches is a 200 with "events": [] (checked 2026-10-01) — a
        # quiet stretch, not a failure; anything else non-2xx raises.
        resp.raise_for_status()
        payload = resp.json()
        events += parse_events(payload)
        if page >= int(payload.get("total_pages") or 1):
            break
        page += 1
    return events


def fetch_offline() -> list[Event]:
    """Build from fixtures/ (no network) — the --offline contract."""
    path = FIXTURES / "trailcons_events.json"
    return parse_events(json.loads(path.read_text())) if path.exists() else []
