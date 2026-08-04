"""TxDOT engagement events adapter — non-recurring public involvement.

Watches TxDOT public-involvement pages (currently the UTP page; add more
URLs to PAGES as they matter) for two caption-labeled table shapes their
CMS uses:

  1. "... public involvement events" — rows of dated, timed events
     (virtual public meetings, hearings) with recording/presentation links.
  2. "... comment period" — a start date and an end date+time. Emitted as a
     single all-day event SPANNING the window, titled with the closing
     deadline (the part people actually miss), kind=comment-window.

The tables are server-rendered; the comment-period one just sits deep
enough in the page that summarizing fetch tools truncate it — raw HTML has
it (hard-won lesson, see Calendar Maintenance page).

SERVER vs DOM markup (second hard-won lesson, 2026-07-26): the server HTML
has NO <caption> and no <thead> — tables sit inside a
div.table-ultimate-wrap whose <h2 class="tableTitle"> carries the label,
with header cells as plain <td>. TxDOT's client-side table JS then rebuilds
the table, moving the title into a <caption> (what you see in DevTools).
_table_caption() accepts both shapes; header rows self-skip because their
cells never match the date regexes.

Also watches TxDOT advisory-committee pages (COMMITTEES below), which use a
third caption shape: "<year> meeting agendas and materials" with
Date | Time | Location | Agenda | Handout columns. Only current-and-later
year tables are parsed (past years keep the same caption on some pages).
Committee date cells sometimes omit the year ("Jan. 22") — the caption's
year fills it in.
"""
from __future__ import annotations

import pathlib
import re
from datetime import date, datetime, timedelta

from bs4 import BeautifulSoup

from caltools.model import Event, slugify

SOURCE = "txdotev"
PAGES = [
    ("UTP", "https://www.txdot.gov/projects/planning/utp/utp-public-involvement.html"),
]
# (display abbrev, raw body name [frozen into UIDs], page url)
COMMITTEES = [
    ("BPAC", "Bicycle and Pedestrian Advisory Committee",
     "https://www.txdot.gov/about/advisory-committees/"
     "bicycle-pedestrian-advisory-committee.html"),
    ("PTAC", "Public Transportation Advisory Committee",
     "https://www.txdot.gov/about/advisory-committees/"
     "public-transportation-advisory-committee.html"),
]
# Statewide index of every TxDOT hearing, meeting and notice: one
# server-rendered table (Area | Date | Format | Description), each row linking
# to its own event page. Rows here are NOT limited to the pages in PAGES /
# COMMITTEES above -- this is the discovery mechanism for one-off project
# meetings that would otherwise have to be spotted by hand.
#
# NOTE: adding a page to PAGES or COMMITTEES also suppresses matching rows
# from this index (see _index_suppressed) so the same meeting isn't published
# twice by two parsers. The richer parser wins because it knows times.
HEARINGS_INDEX = "https://www.txdot.gov/projects/hearings-meetings.html"

# The district lives in the row's link path, which is the only reliable
# geographic signal: the Area column holds 169 distinct town and county
# names, so a US 281 meeting in the Austin district reads "Blanco".
HM_PATH_RE = re.compile(r"/projects/hearings-meetings/([^/]+)/")
HEARINGS_DISTRICTS = {"austin", "statewide", "transportation-planning",
                      "public-transportation"}
# Everything filed under `austin` is in reach by definition. The other three
# segments are organisational, not geographic, and carry events from all over
# Texas (FTA workshops in Lubbock, aviation hearings in Amarillo) -- for
# those, keep only rows the Area column calls statewide. Preferred over an
# allowlist of Central Texas towns, which would need endless maintenance.
LOCAL_DISTRICTS = {"austin"}
# ...and in those segments, the Area values worth keeping. "Austin" matters
# as well as "Statewide": BPAC/PTAC meetings are filed under
# public-transportation with Area "Austin", and dropping them on area would
# hide them from the suppression logic that is supposed to handle them.
REACHABLE_AREAS = {"statewide", "austin"}

# "Notice of availability of FONSI", "notice and opportunity to comment" and
# friends. Deliberately skipped: the row's single date is of unverified
# meaning (publication? deadline?), and a calendar entry that misstates a
# legal comment deadline is worse than no entry. Doing them properly means
# reading the detail page for a real window -- a later enrichment pass.
INDEX_SKIP_FORMATS = {"notice"}

# Trailing clause describing how to attend rather than what the event is.
MODALITY_RE = re.compile(
    r"(virtual|in-person|hybrid|online|open house|webinar|public (meeting|hearing))",
    re.IGNORECASE,
)
INDEX_DATE_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2})\s*$")

EVENT_LENGTH = timedelta(hours=2)
STASSNEY = "TxDOT Stassney Campus Auditorium, 6230 E. Stassney Ln., Austin, TX 78744"

NUM_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
LONG_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October"
    r"|November|December)\s+(\d{1,2}),?\s+(20\d{2})"
)
TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\b", re.IGNORECASE)
MONTHS = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"])}


def _time_of(text: str):
    m = TIME_RE.search(text)
    if not m:
        return None
    h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
    return h, int(m.group(2) or 0)


def _long_date(text: str) -> date | None:
    m = LONG_DATE_RE.search(text)
    if not m:
        return None
    try:
        return date(int(m.group(3)), MONTHS[m.group(1)], int(m.group(2)))
    except ValueError:  # "February 30" typo shouldn't kill the source
        return None


# Detail-page tables abbreviate months ("Aug. 6, 2026"); expand to the full
# names LONG_DATE_RE knows. "Sept" before "Sep" so the sub eats all of it.
_MONTH_ABBR = {"Jan": "January", "Feb": "February", "Mar": "March",
               "Apr": "April", "Jun": "June", "Jul": "July", "Aug": "August",
               "Sept": "September", "Sep": "September", "Oct": "October",
               "Nov": "November", "Dec": "December"}
_MONTH_ABBR_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)\.")


def _expand_months(text: str) -> str:
    return _MONTH_ABBR_RE.sub(lambda m: _MONTH_ABBR[m.group(1)], text)


def _table_caption(table) -> str:
    """Label of a TxDOT CMS table, from either markup shape.

    DOM shape (after their client JS runs): <caption> inside the table.
    Server shape (what requests sees): <h2 class="tableTitle"> inside the
    enclosing div.table-ultimate-wrap. Past-year tables on some pages have
    an empty tableTitle — returns "" for those, which no caption regex
    matches.
    """
    cap = table.find("caption")
    if cap and cap.get_text(strip=True):
        return cap.get_text(" ", strip=True)
    wrap = table.find_parent(class_="table-ultimate-wrap")
    if wrap:
        title = wrap.find(class_="tableTitle")
        if title:
            return title.get_text(" ", strip=True)
    return ""


def _fmt_date(d: date) -> str:
    """House date style: `3 Aug 2026` (day, month abbreviation, 4-digit year)."""
    return d.strftime("%-d %b %Y")


def _fmt_deadline(t, end: date, cell_text: str) -> str:
    """`4pm CDT 3 Aug 2026` — time (if posted) + zone + house-style date."""
    if not t:
        return _fmt_date(end)
    h12 = t[0] % 12 or 12
    minutes = f":{t[1]:02d}" if t[1] else ""
    zone = m.group(0) if (m := re.search(r"\bC[DS]T\b", cell_text)) else "CT"
    return f"{h12}{minutes}{'pm' if t[0] >= 12 else 'am'} {zone} {_fmt_date(end)}"


def _kind_of(topic: str) -> str:
    low = topic.lower()
    if "hearing" in low:
        return "hearing"
    return "engagement"


def _split_modality(desc: str) -> tuple[str, str]:
    """Split "Foo Project - virtual public meeting" into (topic, how).

    The trailing clause says how to attend, not what the event is; it reads
    as noise in a calendar title but is worth keeping in the body. Only the
    LAST dash-segment is considered, and only if it looks like modality --
    project names contain dashes too ("I-35", "US 281 - Blanco").
    """
    # Whitespace REQUIRED before the dash. Without it this splits inside
    # "in-person", "I-35" and "US 281 - Blanco", truncating titles mid-word.
    # A space after is optional: TxDOT writes "Boulevard -open house" too.
    parts = re.split(r"\s+[-\u2013]\s*", desc)
    if len(parts) > 1 and MODALITY_RE.search(parts[-1]):
        return " - ".join(parts[:-1]).strip(), parts[-1].strip()
    return desc.strip(), ""


def _index_suppressed(href: str, desc: str, day: date,
                      owned_urls: set[str], owned_body_days: set) -> bool:
    """True when a richer parser in this module already owns this meeting.

    Two ways that happens, because the index links inconsistently: UTP rows
    point at the very page PAGES scrapes, while BPAC/PTAC rows point at
    their own per-event detail pages and can only be recognised by name.

    The committee test deliberately requires the other parser to have
    ACTUALLY produced an event that day. If a committee page redesigns and
    its parser goes quiet, these rows come through as degraded all-day
    entries instead of vanishing silently -- suppression only ever yields to
    a parser that demonstrably has the event.
    """
    if href in owned_urls:
        return True
    low = desc.lower()
    return any(name.lower() in low and (name.lower(), day) in owned_body_days
               for _, name, _ in COMMITTEES)


def parse_hearings_index(html: str, owned_urls: set[str] | None = None,
                         owned_body_days: set | None = None) -> list[Event]:
    """Rows of the statewide hearings/meetings index, filtered to our patch.

    Emitted all-day: the index carries a date but never a time, and putting
    a made-up start time on a public meeting misleads whoever turns up. A
    later pass can read the detail page and upgrade the event -- which is
    why the UID is frozen from TxDOT's own link path rather than the
    summary. That path is a key they maintain; the description is long
    editable prose that would churn the UID on every copyedit.
    """
    owned_urls = owned_urls or set()
    owned_body_days = owned_body_days or set()
    soup = BeautifulSoup(html, "html.parser")
    events: list[Event] = []
    rows = seen_rows = 0
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
        if len(cells) < 4:
            continue
        area, datetxt, fmt, desc = cells[0], cells[1], cells[2], cells[3]
        m = INDEX_DATE_RE.match(datetxt)
        if not m:
            continue  # header row, or a shape we don't understand
        seen_rows += 1
        link = tr.find("a", href=True)
        if not link:
            continue
        href = link["href"]
        path = HM_PATH_RE.search(href)
        district = path.group(1) if path else "other"
        if district not in HEARINGS_DISTRICTS:
            continue
        if district not in LOCAL_DISTRICTS and area.strip().lower() not in REACHABLE_AREAS:
            continue
        if fmt.strip().lower() in INDEX_SKIP_FORMATS:
            continue
        try:
            day = date(2000 + int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            continue
        if _index_suppressed(href, desc, day, owned_urls, owned_body_days):
            continue
        rows += 1
        topic, how = _split_modality(desc)
        # One page can be many rows -- the I-14 corridor study runs the same
        # meeting in six towns -- so the area disambiguates them. Statewide
        # items gain nothing from it.
        where = "" if area.strip().lower() == "statewide" else f" ({area.strip()})"
        url = href if href.startswith("http") else f"https://www.txdot.gov{href}"
        body = [f"{how}." if how else "",
                "Time and venue are not listed on TxDOT's schedule index — "
                f"see the event page for details: {url}"]
        events.append(Event(
            source=SOURCE,
            summary=f"TxDOT - {topic}{where}",
            start=day,
            location=area.strip(),
            url=url,
            kind=_kind_of(desc),
            description="\n\n".join(x for x in body if x),
            uid=(f"{SOURCE}-hm-"
                 f"{slugify(href.rsplit('/', 1)[-1].removesuffix('.html'))}"
                 f"-{day:%Y%m%d}@calendars.changesaroundme.com"),
        ))
    if seen_rows and not rows:
        _problems.append(
            f"{SOURCE}: hearings index parsed {seen_rows} rows but none "
            f"matched {sorted(HEARINGS_DISTRICTS)} — path segments renamed?"
        )
    return events


# --- hearings-index detail-page enrichment ---------------------------------
# The index gives a date but never a time; each row links to a detail page
# (standard AEM template: h3 schedule headers + a "Meeting details" table)
# that usually does. UIDs were frozen from the link path in anticipation, so
# upgrading all-day -> timed never churns identity. Conservative by rule:
# only an explicit time RANGE upgrades the event ("from 4 to 7 p.m.",
# "4 - 7 p.m."); a lone "by 4 p.m." is a deadline, not a start time, and a
# page whose ranges are ambiguous for the event's date stays all-day.
RANGE_RE = re.compile(
    r"(?:from\s+)?(\d{1,2})(?::(\d{2}))?\s*(?:([ap])\.?\s*m\.?\s*)?"
    r"(?:to|\u2013|\u2014|-)\s*(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\b",
    re.IGNORECASE)
TX_ZIP_RE = re.compile(r"TX\s+\d{5}")
ORDINAL_FIX_RE = re.compile(r"(\d)\s+(st|nd|rd|th)\b")


def _ranges_for_day(soup, day: date) -> list[tuple[int, int, int, int, str]]:
    """Distinct (h1,m1,h2,m2, block_text) ranges in blocks mentioning day."""
    seen: dict[tuple, str] = {}
    for el in soup.find_all(["h2", "h3", "td", "p", "li"]):
        text = el.get_text(" ", strip=True)
        if _long_date(text) != day:
            continue
        m = RANGE_RE.search(text)
        if not m:
            continue
        h2 = int(m.group(4)) % 12 + (12 if m.group(6).lower() == "p" else 0)
        mer1 = (m.group(3) or m.group(6)).lower()  # "4 to 7 p.m.": both p.m.
        h1 = int(m.group(1)) % 12 + (12 if mer1 == "p" else 0)
        if h1 >= h2:  # "11 to 1 p.m." -> the start was a.m.
            h1 = int(m.group(1)) % 12
        key = (h1, int(m.group(2) or 0), h2, int(m.group(5) or 0))
        if key not in seen:
            seen[key] = text
        elif TX_ZIP_RE.search(text) and not TX_ZIP_RE.search(seen[key]):
            # Same range stated twice (h3 header + details cell): keep the
            # block that carries the venue address.
            seen[key] = text
    return [(*k, t) for k, t in seen.items()]


def _venue_from(block_text: str) -> str:
    """Street venue from an in-person block, anchored on a TX zip."""
    z = TX_ZIP_RE.search(block_text)
    if not z:
        return ""
    lead = block_text[:z.end()]
    m = RANGE_RE.search(lead)
    if m:
        lead = lead[m.end():]
    lead = re.sub(r"^[\s.,]*C[DS]?T\b", "", lead)  # drop trailing tz of the range
    venue = ORDINAL_FIX_RE.sub(r"\1\2", lead.strip(" .,;:-"))
    # Sanity: short-ish, contains a street number -> looks like an address.
    return venue if 10 < len(venue) < 140 and re.search(r"\d", venue) else ""


def _when_row(soup, day: date) -> tuple[int, int] | None:
    """(hour, minute) from a "When"-labeled details row naming this day.

    A meeting-details table often states a bare start time ("9 a.m.") that
    the range rule rightly refuses -- out in prose a lone time is usually a
    deadline. Inside a cell whose row label is literally "When" there is no
    such ambiguity. Only a cell that names the event's own date and states
    exactly one time (and no range) qualifies.
    """
    for td in soup.find_all("td"):
        prev = td.find_previous_sibling("td")
        if not prev or prev.get_text(" ", strip=True).lower() != "when":
            continue
        text = _expand_months(td.get_text(" ", strip=True))
        if _long_date(text) != day or RANGE_RE.search(text):
            continue
        times = TIME_RE.findall(text)
        if len(times) == 1:
            h, minute, mer = times[0]
            return int(h) % 12 + (12 if mer.lower() == "p" else 0), int(minute or 0)
    return None


def _where_row(soup) -> str:
    """Venue from a "Where"-labeled details row, joined line-by-line.

    Complements _venue_from (zip-anchored prose): these cells spell the
    state out ("Austin, Texas 78744"), so the zip anchor never fires.
    """
    for td in soup.find_all("td"):
        prev = td.find_previous_sibling("td")
        if prev and prev.get_text(" ", strip=True).lower() == "where":
            venue = ", ".join(s.strip() for s in td.stripped_strings)
            if 10 < len(venue) < 160 and re.search(r"\d", venue):
                return venue
    return ""


def enrich_index_event(ev: Event, html: str) -> None:
    """Upgrade one all-day index event in place from its detail page."""
    soup = BeautifulSoup(html, "html.parser")
    day = _start_day(ev)
    ranges = _ranges_for_day(soup, day)
    deadline = ""
    for td in soup.find_all("td"):
        prev = td.find_previous_sibling("td")
        label = prev.get_text(" ", strip=True).lower() if prev else ""
        if "comment deadline" in label or "comment period" in label:
            row_text = re.sub(r"\s+,", ",", td.get_text(" ", strip=True))  # "<b>Aug. 14<span>, 2026" artifacts
            row_text = _expand_months(row_text)
            # "beginning July 10, 2026 through 5 p.m. on Aug. 10, 2026":
            # the deadline is the LAST date; a time stated after "through"
            # belongs to it.
            dates = [d for m in LONG_DATE_RE.finditer(row_text)
                     if (d := _long_date(m.group(0)))]
            if dates:
                cut = row_text.lower().rfind("through")
                t = _time_of(row_text[cut:]) if cut >= 0 else None
                deadline = _fmt_deadline(t, dates[-1], row_text)
    timed = None
    venue = ""
    if len(ranges) == 1:
        h1, m1, h2, m2, block = ranges[0]
        timed = (datetime(day.year, day.month, day.day, h1, m1),
                 datetime(day.year, day.month, day.day, h2, m2))
        venue = _venue_from(block)
    elif not ranges and (hm := _when_row(soup, day)):
        start = datetime(day.year, day.month, day.day, *hm)
        timed = (start, start + EVENT_LENGTH)  # page states start only
        venue = _where_row(soup)
    if timed:
        ev.start, ev.end = timed
        if venue:
            ev.location = venue
        # The "time and venue not listed" caveat is no longer true.
        ev.description = "\n\n".join(x for x in [
            ev.description.split("\n\n")[0]
            if not ev.description.startswith("Time and venue") else "",
            f"Public comment deadline: {deadline}." if deadline else "",
            f"Details and materials: {ev.url}",
        ] if x)
    elif deadline:
        ev.description += f"\n\nPublic comment deadline: {deadline}."


def enrich_index_events(events: list[Event], get_html) -> None:
    """Enrich every hearings-index event via get_html(url) -> str|None.

    One fetch per distinct URL (a corridor-study page serves several rows);
    any failure leaves that event exactly as the index described it.
    """
    pages: dict[str, str | None] = {}
    for ev in events:
        if "-hm-" not in ev.stable_uid():
            continue
        if ev.url not in pages:
            try:
                pages[ev.url] = get_html(ev.url)
            except Exception as exc:
                print(f"[{SOURCE}] WARNING: detail page fetch failed for "
                      f"{ev.url}: {exc}")
                pages[ev.url] = None
        if pages[ev.url]:
            try:
                enrich_index_event(ev, pages[ev.url])
            except Exception as exc:  # enrichment must never break the feed
                print(f"[{SOURCE}] WARNING: enrichment failed for "
                      f"{ev.url}: {exc}")


def parse_page(html: str, context: str, page_url: str) -> list[Event]:
    soup = BeautifulSoup(html, "html.parser")
    events: list[Event] = []
    matched_tables = 0
    for table in soup.find_all("table"):
        cap = _table_caption(table)

        if re.search(r"comment period", cap, re.IGNORECASE):
            matched_tables += 1
            # One row: start date | end date + time.
            for row in table.find_all("tr"):
                cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
                if len(cells) < 2:
                    continue
                start = _long_date(cells[0])
                end = _long_date(cells[1])
                if not (start and end):
                    continue
                t = _time_of(cells[1])
                events.append(
                    Event(
                        source=SOURCE,
                        summary=f"TxDOT - {cap}",
                        start=start,
                        end=end + timedelta(days=1),  # DTEND exclusive
                        url=page_url,
                        kind="comment-window",
                        description=(
                            f"Public comment window: {_fmt_date(start)} through "
                            f"{_fmt_deadline(t, end, cells[1])}\n\n"
                            f"Details and how to comment: {page_url}"
                        ),
                        uid=(f"{SOURCE}-{slugify(cap)}-{start.strftime('%Y%m%d')}"
                             "@calendars.changesaroundme.com"),
                    )
                )
            continue

        if re.search(r"involvement events", cap, re.IGNORECASE):
            matched_tables += 1
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) < 2:
                    continue
                when = cells[0].get_text(" ", strip=True)
                topic = cells[1].get_text(" ", strip=True)
                dm = NUM_DATE_RE.search(when)
                if not dm or not topic:
                    continue
                try:
                    d = date(int(dm.group(3)), int(dm.group(1)), int(dm.group(2)))
                except ValueError:
                    continue  # impossible date on one row shouldn't kill the source
                t = _time_of(when)
                start = datetime(d.year, d.month, d.day, t[0], t[1]) if t else d
                links = "\n".join(
                    f"{a.get_text(' ', strip=True)}: {a['href']}"
                    for a in (cells[2].find_all("a", href=True) if len(cells) > 2 else [])
                    if a.get_text(strip=True)
                )
                events.append(
                    Event(
                        source=SOURCE,
                        summary=f"TxDOT - {context}: {topic}",
                        start=start,
                        end=(start + EVENT_LENGTH) if isinstance(start, datetime) else None,
                        url=page_url,
                        kind=_kind_of(topic),
                        description=links,
                        uid=(f"{SOURCE}-{slugify(context)}-{slugify(topic)}-"
                             f"{d.strftime('%Y%m%d')}@calendars.changesaroundme.com"),
                    )
                )
    if not matched_tables:
        _problems.append(
            f"txdotev: no involvement/comment tables on {context} page "
            "(page redesign?)"
        )
    return events


CAPTION_YEAR_RE = re.compile(r"(20\d{2})\s+meeting agendas", re.IGNORECASE)
# "Jan. 30, 2026" / "April 24, 2026" / "Jan. 22" / "August 13"
COMMITTEE_DATE_RE = re.compile(r"([A-Za-z]+)\.?\s+(\d{1,2})(?:,?\s+(20\d{2}))?")
MONTHS3 = {name[:3].lower(): num for name, num in MONTHS.items()}

_problems: list[str] = []


def health_problems() -> list[str]:
    return list(_problems)


def _committee_location(raw: str) -> str:
    """Normalize the messy Location cell to something map-appable.

    Observed cell texts: "Hybrid 6230 E Stassney Lane, Austin, TX 78744",
    "6230 E. Stassney Lane, Austin, Tx Auditorium, Teams virtual meeting",
    "Teams virtual meeting has surpassed", "Virtual",
    "Teams virtual link will be provided".
    """
    low = raw.lower()
    if "stassney" in low:
        virtual_too = "teams" in low or "virtual" in low or "hybrid" in low
        return STASSNEY + (" (Teams option available)" if virtual_too else "")
    if "teams" in low or "virtual" in low:
        return "Virtual (Teams)"
    return re.sub(r"\s+", " ", raw).strip()


def parse_committee_page(
    html: str, abbrev: str, name: str, page_url: str, min_year: int | None = None
) -> list[Event]:
    if min_year is None:
        min_year = date.today().year
    soup = BeautifulSoup(html, "html.parser")
    events: list[Event] = []
    saw_table = False
    current_tables = 0
    for table in soup.find_all("table"):
        cap = _table_caption(table)
        ym = CAPTION_YEAR_RE.search(cap)
        if not ym:
            continue
        saw_table = True
        year = int(ym.group(1))
        if year < min_year:
            continue  # archive table for a past year
        current_tables += 1
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 3:
                continue
            texts = [c.get_text(" ", strip=True).replace("\xa0", " ") for c in cells]
            dm = COMMITTEE_DATE_RE.search(texts[0])
            month = MONTHS3.get(dm.group(1)[:3].lower()) if dm else None
            if not month:
                continue
            try:
                d = date(int(dm.group(3) or year), month, int(dm.group(2)))
            except ValueError:
                continue  # impossible date on one row shouldn't kill the source
            t = _time_of(texts[1])
            start = datetime(d.year, d.month, d.day, t[0], t[1]) if t else d
            links = []
            for a in row.find_all("a", href=True):
                label = (a.get("title") or a.get_text(" ", strip=True)).strip()
                href = a["href"]
                if href.startswith("/"):
                    href = "https://www.txdot.gov" + href
                if label and href.startswith("http"):
                    links.append(f"{label}: {href}")
            desc = [f"Posted location: {re.sub(r'  +', ' ', texts[2])}"] if texts[2] else []
            events.append(
                Event(
                    source=SOURCE,
                    summary=f"TxDOT {abbrev} - Meeting",
                    start=start,
                    end=(start + EVENT_LENGTH) if isinstance(start, datetime) else None,
                    location=_committee_location(texts[2]),
                    url=page_url,
                    status=("CANCELLED" if "cancel" in " ".join(texts).lower()
                            else "CONFIRMED"),
                    kind="regular",
                    description="\n".join(desc + links),
                    uid=(f"{SOURCE}-{slugify(name)}-"
                         f"{start.strftime('%Y%m%dT%H%M') if isinstance(start, datetime) else start.strftime('%Y%m%d')}"
                         "@calendars.changesaroundme.com"),
                )
            )
    if not saw_table:
        _problems.append(
            f"txdotev: no '<year> meeting agendas' table on {abbrev} page "
            "(page redesign?)"
        )
    elif current_tables and not events:
        # A current-or-later-year table exists but nothing in it parsed —
        # likely a row-format change. (All tables being past-year is NOT
        # flagged: that's the normal early-January state before the new
        # year's table posts.)
        _problems.append(
            f"txdotev: {abbrev} current-year table has no parseable rows"
        )
    return events


def _start_day(e: Event) -> date:
    return e.start.date() if isinstance(e.start, datetime) else e.start


def fetch(session) -> list[Event]:
    _problems.clear()
    events: list[Event] = []
    owned_urls = {u for _, u in PAGES} | {u for _, _, u in COMMITTEES}
    owned_body_days: set[tuple[str, date]] = set()
    for context, url in PAGES:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        events.extend(parse_page(resp.text, context, url))
    for abbrev, name, url in COMMITTEES:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        found = parse_committee_page(resp.text, abbrev, name, url)
        events.extend(found)
        owned_body_days |= {(name.lower(), _start_day(e)) for e in found}
    # Discovery pass last: it needs to know what the parsers above already
    # produced so the same meeting isn't published twice.
    resp = session.get(HEARINGS_INDEX, timeout=30)
    resp.raise_for_status()
    index_events = parse_hearings_index(resp.text, owned_urls, owned_body_days)

    def _get(url):
        r = session.get(url, timeout=30)
        r.raise_for_status()
        return r.text

    enrich_index_events(index_events, _get)
    events.extend(index_events)
    return events


FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures"
# Committee fixtures were captured in 2026; pin min_year so they stay stable.
FIXTURE_MIN_YEAR = 2026


def fetch_offline() -> list[Event]:
    """Build from fixtures/ (no network) — the --offline contract."""
    _problems.clear()
    events = parse_page(
        (FIXTURES / "txdotev_utp.html").read_text(),
        "UTP",
        "https://www.txdot.gov/projects/planning/utp/utp-public-involvement.html",
    )
    for abbrev, name, url in COMMITTEES:
        events.extend(
            parse_committee_page(
                (FIXTURES / f"txdotev_{abbrev.lower()}.html").read_text(),
                abbrev, name, url, min_year=FIXTURE_MIN_YEAR,
            )
        )
    owned_urls = {u for _, u in PAGES} | {u for _, _, u in COMMITTEES}
    owned_body_days = {(name.lower(), _start_day(e))
                       for _, name, _ in COMMITTEES for e in events
                       if name.lower() in e.summary.lower()
                       or slugify(name) in e.stable_uid()}
    index_events = parse_hearings_index(
        (FIXTURES / "txdotev_hearings_index.html").read_text(),
        owned_urls, owned_body_days,
    )
    detail_fixtures = {
        "https://www.txdot.gov/projects/hearings-meetings/austin/2026/"
        "us281-073026.html": FIXTURES / "txdotev_hm_us281.html",
        # "When"/"Where" details-table shape (bare start time, no range).
        "https://www.txdot.gov/projects/hearings-meetings/"
        "public-transportation/2026/public-hearing-public-transportation"
        ".html": FIXTURES / "txdotev_hm_pt_chapter31.html",
    }
    enrich_index_events(
        index_events,
        lambda u: detail_fixtures[u].read_text() if u in detail_fixtures else None,
    )
    events.extend(index_events)
    return events
