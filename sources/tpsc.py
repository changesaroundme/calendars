"""Texas Pedestrian Safety Coalition (TPSC) adapter — added 2026-09-04.

texaspedsafety.org is a WordPress site hosted by TTI. There is no events
plugin and no ICS; what exists is:

  1. The home-page blog, where each upcoming meeting, webinar and the
     annual Forum gets an announcement post ("Upcoming Pedestrian Safety
     Coalition Meeting – Thursday, August 20, 2026, 10:00 – 11:30",
     "Upcoming Webinar: ...", "2026 Texas Pedestrian Safety Forum – May 6
     in Austin"). Read through the WP REST API (/wp-json/wp/v2/posts) —
     structured JSON rather than theme HTML, so a restyle can't break it.
  2. One WP page per coalition meeting ("August 2026 Texas Pedestrian
     Safety Coalition Meeting") with the agenda and, afterwards, the
     recording / presentation / notes links. Sometimes published with the
     announcement (Dec 2025), sometimes on the day (Aug 2026). Read via
     /wp-json/wp/v2/pages?search=... and merged onto the matching
     meeting event as a Summary Agenda + links.

Lead time is whatever the coalition gives (observed: 1 day to 4 weeks for
meetings, days for webinars, ~4 months for the Forum), and posts are
sporadic — SPORADIC tells build.py an empty-future fetch is normal.

Identity: explicit uids from kind + date (tpsc-coalition-meeting-20260820),
so retitles never fork an event. Virtual events carry no location (the
Teams link is the venue); the Forum gets its venue/city from the post.
"""
from __future__ import annotations

import html
import json
import pathlib
import re
from datetime import date, datetime, timedelta

from bs4 import BeautifulSoup

from caltools.model import Event

SOURCE = "tpsc"
SPORADIC = True
FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures"
SITE = "https://www.texaspedsafety.org"
POSTS_API = (f"{SITE}/wp-json/wp/v2/posts?per_page=30"
             "&_fields=id,date,link,title,content")
PAGES_API = (f"{SITE}/wp-json/wp/v2/pages?per_page=20"
             "&search=Pedestrian+Safety+Coalition+Meeting"
             "&_fields=id,date,modified,link,title,content")

MEETING_LENGTH = timedelta(minutes=90)
WEBINAR_LENGTH = timedelta(hours=1)

MONTHS = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])}
# "Thursday, August 20, 2026" / "Wednesday, May 6" (year optional — the
# post date supplies it: first such date on/after the post).
DATE_RE = re.compile(
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
    r"(January|February|March|April|May|June|July|August|September|October"
    r"|November|December)\s+(\d{1,2})(?:,?\s+(20\d{2}))?")
# "10:00am to 11:30am", "10:00 – 11:30", "1:30p.m. – 2:30p.m.",
# "2:00 p.m. – 3:00 p.m."
TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2})\s*([ap])?\.?\s*m?\.?\s*(?:to|–|-|—)\s*"
    r"(\d{1,2}):(\d{2})\s*([ap])?\.?\s*m?\.?", re.IGNORECASE)
WEBINAR_TITLE_RE = re.compile(r"^\s*Upcoming Webinar:\s*(.+?)\s*$", re.IGNORECASE)
MEETING_TITLE_RE = re.compile(r"Coalition Meeting", re.IGNORECASE)
FORUM_TITLE_RE = re.compile(r"Pedestrian Safety Forum", re.IGNORECASE)
# "at the AT&T Center in Austin Texas" -> venue, city
FORUM_VENUE_RE = re.compile(
    r"\bat (?:the )?(.+?) in ([A-Z][A-Za-z .]+?)(?:,)? Texas\b")
# Meeting-page agenda lines: "10:20 – 10:40: Topic" (times bolded).
AGENDA_LINE_RE = re.compile(
    r"^(\d{1,2}:\d{2})(?:\s*[–-]\s*\d{1,2}:\d{2})?:\s*(.+?)\s*$")
PROCEDURAL_RE = re.compile(r"^(welcome|introductions?|adjourn|call to order)",
                           re.IGNORECASE)
MATERIAL_LINK_RE = re.compile(
    r"^(?:Meeting\s+)?(Recording|Presentation|Slides|Notes|Minutes|Transcript)$",
    re.IGNORECASE)

_problems: list[str] = []


def health_problems() -> list[str]:
    return list(_problems)


def _text(rendered: str) -> str:
    return " ".join(BeautifulSoup(rendered, "html.parser")
                    .get_text(" ").split())


def _hour(h: str, m: str, ap: str | None, default_pm: bool = False) -> tuple[int, int]:
    hh = int(h) % 12
    if ap:
        hh += 12 if ap.lower() == "p" else 0
    elif default_pm or int(h) < 8:  # "1:30 – 2:30" with no marker: afternoon
        hh += 12
    return hh, int(m)


def _times(text: str):
    m = TIME_RE.search(text)
    if not m:
        return None
    h1, m1, ap1, h2, m2, ap2 = m.groups()
    # "1:30p.m. – 2:30p.m." / "10:00am to 11:30am" / "10:00 – 11:30"
    end_pm = (ap2 or ap1 or "").lower() == "p"
    start = _hour(h1, m1, ap1 or ap2, default_pm=end_pm and int(h1) < 12
                  and int(h1) <= int(h2))
    end = _hour(h2, m2, ap2 or ap1, default_pm=end_pm)
    return start, end


def _date_in(text: str, posted: date) -> date | None:
    m = DATE_RE.search(text)
    if not m:
        return None
    month, day, year = MONTHS[m.group(1)], int(m.group(2)), m.group(3)
    if year:
        try:
            return date(int(year), month, day)
        except ValueError:
            return None
    for y in (posted.year, posted.year + 1):  # first on/after the post
        try:
            d = date(y, month, day)
        except ValueError:
            return None
        if d >= posted - timedelta(days=1):
            return d
    return None


def _join_link(soup) -> tuple[str, str]:
    """(label, url) for the post's action link, or ('', '')."""
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "preview=true" in href:  # editor preview leak (Dec 2025 post)
            href = href.split("?")[0]
        if "teams.microsoft.com" in href or "zoom.us" in href:
            return "Join (Teams)" if "teams" in href else "Join", href
        if href.startswith(SITE) and "wp-content" not in href:
            return "Details", href
    return "", ""


def parse_posts(posts: list[dict]) -> list[Event]:
    events: list[Event] = []
    for p in posts:
        title = html.unescape(_text(p["title"]["rendered"]))
        body_html = p["content"]["rendered"]
        body = html.unescape(_text(body_html))
        posted = datetime.fromisoformat(p["date"]).date()
        soup = BeautifulSoup(body_html, "html.parser")
        label, link = _join_link(soup)

        if (m := WEBINAR_TITLE_RE.match(title)):
            day = _date_in(body, posted) or _date_in(title, posted)
            if not day:
                continue
            t = _times(body)
            start = (datetime.combine(day, datetime.min.time())
                     .replace(hour=t[0][0], minute=t[0][1]) if t else day)
            end = (start.replace(hour=t[1][0], minute=t[1][1])
                   if t else None)
            desc = ["Webinar (virtual)"]
            events.append(Event(
                source=SOURCE, summary=f"TPSC: Webinar – {m.group(1)}",
                start=start, end=end, url=p["link"], kind="regular",
                description="\n\n—\n\n".join(
                    s for s in ["\n".join(desc),
                                f"{label}: {link}" if link else ""] if s),
                uid=f"tpsc-webinar-{day:%Y%m%d}@calendars.changesaroundme.com",
            ))
        elif MEETING_TITLE_RE.search(title):
            day = _date_in(title, posted) or _date_in(body, posted)
            if not day:
                continue
            t = _times(title) or _times(body)
            start = (datetime.combine(day, datetime.min.time())
                     .replace(hour=t[0][0], minute=t[0][1]) if t else day)
            end = (start.replace(hour=t[1][0], minute=t[1][1])
                   if t else None)
            events.append(Event(
                source=SOURCE, summary="TPSC: Coalition Meeting",
                start=start, end=end, url=p["link"], kind="regular",
                description="\n\n—\n\n".join(
                    s for s in ["Virtual meeting (Microsoft Teams)",
                                f"{label}: {link}" if link else ""] if s),
                uid=(f"tpsc-coalition-meeting-{day:%Y%m%d}"
                     "@calendars.changesaroundme.com"),
            ))
        elif FORUM_TITLE_RE.search(title) and "will be held" in body:
            day = _date_in(body, posted) or _date_in(title, posted)
            if not day:
                continue
            venue = FORUM_VENUE_RE.search(body)
            location = (f"{venue.group(1)}, {venue.group(2)}, TX"
                        if venue else "")
            events.append(Event(
                source=SOURCE, summary="TPSC: Texas Pedestrian Safety Forum",
                start=day, end=None, url=p["link"], location=location,
                kind="regular",
                description="Annual statewide forum. Registration and "
                            "program are posted on the event link.",
                uid=f"tpsc-forum-{day:%Y%m%d}@calendars.changesaroundme.com",
            ))
    return events


def _agenda(page_html: str) -> tuple[str, list[str]]:
    """(Summary Agenda block or '', ['Recording: url', ...]) from a page."""
    soup = BeautifulSoup(page_html, "html.parser")
    items: list[str] = []
    links: list[str] = []
    in_plain_list = False
    for el in soup.find_all(["p", "li", "strong", "b"]):
        if el.name in ("strong", "b"):
            continue
        text = html.unescape(" ".join(el.get_text(" ").split())).strip("​ ")
        if not text:
            continue
        a = el.find("a", href=True) if el.name == "p" else None
        if a and (lm := MATERIAL_LINK_RE.match(
                " ".join(a.get_text(" ").split()))):
            links.append(f"{lm.group(1).capitalize()}: {a['href']}")
            continue
        if el.name == "p":
            in_plain_list = bool(re.match(r"^(Meeting\s+)?Agenda:?$", text, re.I))
            if (m := AGENDA_LINE_RE.match(text)):
                topic = m.group(2)
                if not PROCEDURAL_RE.match(topic):
                    items.append(f"{m.group(1)} {topic}")
            continue
        # <li>: a speaker under the last timed item, or a plain agenda row
        if items and not in_plain_list and AGENDA_LINE_RE.match(
                items[-1].split(" ", 1)[0] + ": x") and el.find_parent("ul"):
            if not items[-1].endswith(")"):
                items[-1] += f" ({text})"
        elif in_plain_list and not PROCEDURAL_RE.match(text):
            items.append(f"- {text}")
    block = "Summary Agenda\n\n" + "\n".join(items) if items else ""
    return block, links


def enrich_from_pages(events: list[Event], pages: list[dict]) -> int:
    """Merge each meeting page's agenda/materials onto its event by
    (month, year) in the page title. Returns events enriched."""
    by_month: dict[tuple[int, int], dict] = {}
    for pg in pages:
        t = html.unescape(_text(pg["title"]["rendered"]))
        m = re.match(r"^(January|February|March|April|May|June|July|August"
                     r"|September|October|November|December)\s+(20\d{2})\s+"
                     r"Texas Pedestrian Safety Coalition Meeting", t)
        if m:
            by_month[(int(m.group(2)), MONTHS[m.group(1)])] = pg
    n = 0
    for ev in events:
        if not ev.stable_uid().startswith("tpsc-coalition-meeting-"):
            continue
        d = ev.start.date() if isinstance(ev.start, datetime) else ev.start
        pg = by_month.get((d.year, d.month))
        if not pg:
            continue
        block, links = _agenda(pg["content"]["rendered"])
        if not block and not links:
            continue
        head, _, tail = ev.description.partition("\n\n—\n\n")
        ev.url = pg["link"]
        # A "Details" link that just repeats the (new) event URL is noise.
        tail = "\n".join(l for l in tail.split("\n")
                         if l != f"Details: {ev.url}")
        parts = [s for s in [head, block, "\n".join([tail] + links).strip()] if s]
        ev.description = "\n\n—\n\n".join(parts)
        n += 1
    return n


def fetch(session) -> list[Event]:
    _problems.clear()
    resp = session.get(POSTS_API, timeout=30)
    resp.raise_for_status()
    events = parse_posts(resp.json())
    try:
        pr = session.get(PAGES_API, timeout=30)
        pr.raise_for_status()
        enrich_from_pages(events, pr.json())
    except Exception as exc:  # enrichment is best-effort
        print(f"[{SOURCE}] WARNING: meeting pages fetch failed: {exc}")
    return events


def fetch_offline() -> list[Event]:
    """Build from fixtures/ (no network) — the --offline contract."""
    _problems.clear()
    data = json.loads((FIXTURES / "tpsc_wp.json").read_text())
    events = parse_posts(data["posts"])
    by_uid = {e.stable_uid(): e for e in events}
    # Announcement shapes: meeting (title carries date+time), webinar
    # (body "Wednesday, August 26, 2026 | 1:30p.m. – 2:30p.m." and the
    # year-less "Tuesday, September 23 | 2:00 p.m. – 3:00 p.m. (CDT)"),
    # Forum (year-less date, venue + city). Non-event posts are skipped.
    m = by_uid["tpsc-coalition-meeting-20260820@calendars.changesaroundme.com"]
    assert (m.start, m.end) == (datetime(2026, 8, 20, 10, 0),
                                datetime(2026, 8, 20, 11, 30)), (m.start, m.end)
    assert "Join (Teams): https://teams.microsoft.com/meet/276795173889779" in m.description
    w = by_uid["tpsc-webinar-20260826@calendars.changesaroundme.com"]
    assert (w.start, w.end) == (datetime(2026, 8, 26, 13, 30),
                                datetime(2026, 8, 26, 14, 30)), (w.start, w.end)
    assert w.summary == "TPSC: Webinar – What does Texas’ New Sidewalk User Law Really Mean?", w.summary
    w2 = by_uid["tpsc-webinar-20250923@calendars.changesaroundme.com"]
    assert w2.start == datetime(2025, 9, 23, 14, 0), w2.start
    f = by_uid["tpsc-forum-20260506@calendars.changesaroundme.com"]
    assert f.location == "AT&T Center, Austin, TX", f.location
    assert not m.location and not w.location
    assert len(events) == 6, [e.summary for e in events]
    # Meeting pages: timed agenda with speakers (Aug 2026) and the plain
    # bulleted shape (Dec 2025); procedural rows dropped; materials linked.
    assert enrich_from_pages(events, data["pages"]) == 2
    assert m.description.startswith(
        "Virtual meeting (Microsoft Teams)\n\n—\n\nSummary Agenda\n\n"
        "10:10 2026 Texas Pedestrian Safety Forum Recap\n"
        "10:20 Statewide Update on Youth Pedestrian Crashes (Gaby Kolodzy, "
        "Assistant Research Scientist, Texas A&M Transportation Institute "
        "(TTI))\n"), m.description
    assert "Recording: https://youtu.be/XyxoZdyEi9k" in m.description
    assert m.url.endswith("/august-2026-texas-pedestrian-safety-coalition-meeting/")
    dec = by_uid["tpsc-coalition-meeting-20251211@calendars.changesaroundme.com"]
    assert "- 2026 Texas Pedestrian Safety Forum\n- 2026 Pedestrian Safety Task Force" in dec.description, dec.description
    assert "Welcome" not in dec.description
    assert "Transcript: https://" in dec.description
    return events
