"""LCRA adapter — board & committee meeting schedule page.

LCRA publishes a year of meeting *dates* (no times) as two HTML tables on
one WordPress page: one for LCRA proper (committee day + board day per
month) and one for LCRA Transmission Services Corporation. Times and
detailed locations only appear later in agenda PDFs and the SOS open-
meetings filing, so v1 emits all-day events; a future SOS adapter can
upgrade them to timed events.

Table semantics (verified against live markup 2026-07-19):
- 4-cell row:  Month | committee day | board day | city
- 3-cell row with colspan=2 on the middle cell: one combined day for board
  and committees (e.g. "September | 23 | Austin")
- "No meeting" rows and single-cell year rows ("2026") in between.

Source: https://www.lcra.org/about/leadership/board-meeting-schedule/

Also watches LCRA's standing public-comment page (COMMENT_URL): three
fixed sections (water rules / pending water sale contracts / Dredge &
Fill permits), each either an explicit empty-state sentence ("There are
no ... at this time") or free prose describing an open item. The prose
varies, but the calendar-relevant kernel is regular: "Comments may be
submitted through [Tuesday,] Sept. 1". Each parsed deadline becomes one
all-day comment-window event ON the deadline day (the part people miss;
the window's start date is never stated). Conservative by rule: a section
with content but NO parseable deadline trips a health problem instead of
guessing — the red build names the section, and the fallback workflow is
a curated.yaml entry (or a parser fix if LCRA changed their phrasing).
"""
from __future__ import annotations

import pathlib
import re
from datetime import date, datetime, timedelta

from bs4 import BeautifulSoup, NavigableString, Tag

from caltools.model import Event

SOURCE = "lcra"
FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures"
SCHEDULE_URL = "https://www.lcra.org/about/leadership/board-meeting-schedule/"
COMMENT_URL = ("https://www.lcra.org/water/"
               "water-related-rules-and-regulations-for-public-comment/")
# The page's three standing sections: (heading fragment, short display name).
COMMENT_SECTIONS = [
    ("Water-related rules and regulations", "Water rules"),
    ("water sale contract", "Water sale contract"),
    ("Dredge and Fill", "Dredge & Fill permit"),
]
EMPTY_RE = re.compile(r"There are no\b", re.IGNORECASE)
DEADLINE_RE = re.compile(
    r"submitted\s+through\s+(?:(Mon|Tues?|Wednes|Thurs?|Fri|Satur|Sun)[a-z]*day,?\s+)?"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})"
    r"(?:,?\s+(20\d{2}))?",
    re.IGNORECASE,
)
MONTH3 = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
     "nov", "dec"])}
WEEKDAYS = {"mon": 0, "tue": 1, "tues": 1, "wednes": 2, "thu": 3, "thur": 3,
            "thurs": 3, "fri": 4, "satur": 5, "sun": 6}
AGENDAS_URL = "https://www.lcra.org/about/leadership/board-agendas/"
NOTE = (
    "All-day placeholder: LCRA publishes dates a year out; the exact time "
    f"and room post with the agenda (~1 week ahead): {AGENDAS_URL}"
)

MONTHS = {
    m.lower(): i + 1
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"]
    )
}
YEAR_RE = re.compile(r"^(20\d{2})$")
DAY_RE = re.compile(r"\d{1,2}")


def _days(cell_text: str) -> list[int]:
    """All day-numbers in a cell ('18', '17-18', '18, 19' all work)."""
    return [int(d) for d in DAY_RE.findall(cell_text) if 1 <= int(d) <= 31]


def _mk(summary: str, year: int, month: int, day: int, city: str) -> Event | None:
    try:
        d = date(year, month, day)
    except ValueError:
        return None
    # Full venue per KB Organizations/Overview; the schedule table only
    # says "Austin".
    if city == "Austin":
        location = "Board Room, Hancock Building, 3700 Lake Austin Blvd, Austin, TX 78703"
    else:
        location = f"{city}, TX" if city and city != "–" else ""
    return Event(
        source=SOURCE,
        summary=summary,
        start=d,
        location=location,
        url=AGENDAS_URL,
        description=NOTE,
    )


def _parse_table(table, is_tsc: bool, default_year: int) -> list[Event]:
    events: list[Event] = []
    year = default_year
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        texts = [c.get_text(" ", strip=True) for c in cells]
        if len(texts) == 1:
            m = YEAR_RE.match(texts[0])
            if m:
                year = int(m.group(1))
            continue
        if not texts or texts[0].lower() not in MONTHS:
            continue  # header rows, titles
        month = MONTHS[texts[0].lower()]
        body_cells = cells[1:]
        city = texts[-1] if len(texts) > 1 else ""
        # No explicit "No meeting" skip: a cell saying "No meeting" has no
        # digits, so _days() yields nothing for it — but the OTHER cells in
        # the row still parse (committees can skip a month the board meets).
        if is_tsc:
            for day in _days(texts[1] if len(texts) > 1 else ""):
                ev = _mk("Transmission Services Corp. Board", year, month, day, city)
                if ev:
                    events.append(ev)
            continue
        # LCRA table: combined day if the day cell spans both columns
        if len(body_cells) >= 1 and body_cells[0].get("colspan"):
            for day in _days(texts[1]):
                ev = _mk("Board & Committee Meetings", year, month, day, city)
                if ev:
                    events.append(ev)
        elif len(texts) >= 4:
            for day in _days(texts[1]):
                ev = _mk("Board Committees", year, month, day, city)
                if ev:
                    events.append(ev)
            for day in _days(texts[2]):
                ev = _mk("Board of Directors", year, month, day, city)
                if ev:
                    events.append(ev)
    return events


def parse_page(html: str, default_year: int | None = None) -> list[Event]:
    if default_year is None:
        default_year = datetime.now().year
    soup = BeautifulSoup(html, "html.parser")
    events: list[Event] = []
    for table in soup.find_all("table"):
        title = table.find("td")
        title_text = title.get_text(" ", strip=True).lower() if title else ""
        is_tsc = "transmission" in title_text
        events.extend(_parse_table(table, is_tsc, default_year))
    return events


def finalize(events: list[Event]) -> list[Event]:
    """Freeze UIDs from the raw body name, then apply the display prefix."""
    for ev in events:
        ev.uid = ev.stable_uid()
        if not ev.summary.startswith("LCRA"):
            ev.summary = f"LCRA - {ev.summary}"
    return events


_problems: list[str] = []


def health_problems() -> list[str]:
    return list(_problems)


def _deadline_date(m: re.Match, today: date) -> date | None:
    """Resolve a DEADLINE_RE match to a date; None if it can't be trusted.

    The page usually omits the year: assume this year, roll to next if
    that's well past (an open window's deadline is never far behind
    today), and when a weekday word is present, require it to agree —
    a mismatch means the year guess (or the page) is wrong.
    """
    month, day = MONTH3[m.group(2).lower()[:3]], int(m.group(3))
    for year in ([int(m.group(4))] if m.group(4) else
                 [today.year, today.year + 1]):
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        if not m.group(4) and d < today - timedelta(days=14):
            continue  # long past: try next year
        if m.group(1) and WEEKDAYS.get(m.group(1).lower()) != d.weekday():
            continue  # stated weekday disagrees: wrong year, or a typo
        return d
    return None


def parse_comment_page(html: str, today: date | None = None) -> list[Event]:
    if today is None:
        today = date.today()
    soup = BeautifulSoup(html, "html.parser")
    events: list[Event] = []
    headings = soup.find_all(["h2", "h3"])
    matched_any = False
    for frag, label in COMMENT_SECTIONS:
        head = next((h for h in headings
                     if frag.lower() in h.get_text(" ", strip=True).lower()), None)
        if head is None:
            continue
        matched_any = True
        # Section text = every text node until the next section heading.
        # A string walk, not an element list: Divi pages put copy in
        # arbitrary containers (the deadline sentence lives in a bare
        # <div class="et_pb_text_inner"><strong>), so filtering by tag
        # name silently drops content.
        texts: list[str] = []
        submit = ""
        for node in head.next_elements:
            if isinstance(node, Tag) and node.name in ("h2", "h3"):
                txt = node.get_text(" ", strip=True)
                if any(f.lower() in txt.lower() for f, _ in COMMENT_SECTIONS) \
                        or txt in ("Social Media", "Follow us"):
                    break
                continue
            if isinstance(node, Tag) and node.name == "a" and not submit:
                if "SUBMIT" in node.get_text(" ", strip=True).upper():
                    submit = node.get("href", "")
            if isinstance(node, NavigableString) and node.parent.name not in (
                    "script", "style"):
                frag = str(node).strip()
                if frag:
                    texts.append(frag)
        text = re.sub(r"\s+", " ", " ".join(texts)).strip()
        if not text or EMPTY_RE.search(text[:200]):
            continue  # standing empty state: nothing open in this section
        dm = DEADLINE_RE.search(text)
        d = _deadline_date(dm, today) if dm else None
        if d is None:
            _problems.append(
                f"lcra: comment page section '{label}' has content but no "
                "parseable deadline — add via curated.yaml or fix the parser"
            )
            continue
        snippet = text[:420].rstrip() + ("…" if len(text) > 420 else "")
        events.append(Event(
            source=SOURCE,
            summary=f"{label} public comments close",
            start=d,
            url=COMMENT_URL,
            kind="comment-window",
            description="\n\n".join(x for x in [
                snippet,
                f"Submit comments: {submit}" if submit else "",
                f"Details: {COMMENT_URL}",
            ] if x),
        ))
    if not matched_any:
        _problems.append(
            "lcra: no known sections on the public-comment page "
            "(page redesign?)"
        )
    return events


# --- meeting-materials enrichment ------------------------------------------
# The board-agendas page groups each meeting's documents under a dated
# heading ("Aug. 19, 2026, meeting materials"): top-level list items are a
# body label with a nested list of /download/ links. The heading's own date
# is the declared match key. TSC-labeled sections attach to the TSC event;
# everything else attaches to the LCRA-side event on that date. Loose
# monthly boilerplate links (schedule, notices) are skipped. Upgrade-only.
MATERIALS_HEAD_RE = re.compile(
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
    r"(\d{1,2}),\s+(20\d{2}),?\s+meeting materials", re.IGNORECASE)
AGENDA_TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\b",
                            re.IGNORECASE)
MATERIALS_HORIZON = timedelta(days=45)
MATERIALS_PDF_CAP = 3       # agenda PDFs per run, for the time upgrade
MEETING_HOURS = 3


def parse_materials_page(html: str) -> dict[date, list[tuple[str, str, str]]]:
    """date -> [(section_label, link_text, abs_url)] per dated block."""
    soup = BeautifulSoup(html, "html.parser")
    out: dict[date, list[tuple[str, str, str]]] = {}
    for strong in soup.find_all("strong"):
        m = MATERIALS_HEAD_RE.search(strong.get_text(" ", strip=True))
        if not m:
            continue
        try:
            d = date(int(m.group(3)), MONTH3[m.group(1).lower()[:3]],
                     int(m.group(2)))
        except (ValueError, KeyError):
            continue
        container = strong.find_parent(["div", "section"])
        ul = container.find("ul") if container else None
        if ul is None:
            continue
        for li in ul.find_all("li", recursive=False):
            sub = li.find("ul")
            if sub is None:
                continue  # loose boilerplate link (schedule, notices)
            # The label is the li's leading text node (sometimes wrapped in
            # a span, sometimes bare — both shapes are live).
            label = next((s.strip() for s in li.stripped_strings), "")
            for a in sub.find_all("a", href=True):
                url = a["href"]
                if "/download/" not in url:
                    continue
                if url.startswith("/"):
                    url = "https://www.lcra.org" + url
                out.setdefault(d, []).append(
                    (label, a.get_text(" ", strip=True), url))
    return out


def _agenda_start_time(pdf_bytes: bytes, day: date):
    """((hour, minute), earliest_flag) if page 1 names this day and states
    exactly one distinct time — counting the word "noon", which is the
    ONLY time on real TSC agendas ("Earliest start time: noon", verified
    against the 19 Aug 2026 document). earliest_flag is True when the
    header uses that "earliest start time" phrasing: the board convenes
    after preceding meetings adjourn, so the time is a floor, not a
    promise — worth one line of commentary (real uncertainty)."""
    import io
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = (pdf.pages[0].extract_text() or "") if pdf.pages else ""
    daystr = f"{day.day}, {day.year}"
    if day.strftime("%B").lower()[:3] not in text.lower() or daystr not in text:
        return None
    times = {(int(h) % 12 + (12 if mer.lower() == "p" else 0), int(mn or 0))
             for h, mn, mer in AGENDA_TIME_RE.findall(text)}
    if re.search(r"\bnoon\b", text, re.IGNORECASE):
        times.add((12, 0))
    if len(times) != 1:
        return None
    earliest = bool(re.search(r"earliest start time", text, re.IGNORECASE))
    return times.pop(), earliest


def enrich_materials(events: list[Event], get_html, today: date,
                     get_bytes=None) -> None:
    """Attach posted meeting materials (and confirm times) in place."""
    horizon = today + MATERIALS_HORIZON
    upcoming = [e for e in events
                if e.url == AGENDAS_URL and e.status != "CANCELLED"
                and today <= (e.start.date() if isinstance(e.start, datetime)
                              else e.start) <= horizon]
    if not upcoming:
        return
    try:
        blocks = parse_materials_page(get_html(AGENDAS_URL))
    except Exception as exc:  # enrichment must never break the feed
        print(f"[{SOURCE}] WARNING: materials page fetch failed: {exc}")
        return
    spent = 0
    for ev in upcoming:
        d = ev.start.date() if isinstance(ev.start, datetime) else ev.start
        entries = blocks.get(d)
        if not entries:
            continue
        tsc = "Transmission" in ev.summary
        keep = [(lbl, txt, url) for lbl, txt, url in entries
                if ("transmission" in lbl.lower()) == tsc]
        if not keep:
            continue
        # Time upgrade from the day's FIRST posted agenda (blocks list the
        # bodies in convening order; the combined event starts when the
        # first committee does). Conservative: page 1 must name the day
        # and state exactly one distinct time, else the event stays
        # all-day and says where to look.
        confirmed = earliest = False
        main = next(((lbl, txt, url) for lbl, txt, url in keep
                     if txt.lower().endswith("meeting agenda")), None)
        if get_bytes is not None and main and spent < MATERIALS_PDF_CAP:
            spent += 1
            try:
                data = get_bytes(main[2])
                t = _agenda_start_time(data, d) if data else None
            except Exception as exc:
                print(f"[{SOURCE}] WARNING: agenda time parse failed "
                      f"({main[2]}): {exc}")
                t = None
            if t is not None:
                (h, m), earliest = t
                ev.start = datetime(d.year, d.month, d.day, h, m)
                ev.end = ev.start + timedelta(hours=MEETING_HOURS)
                confirmed = True
        lines = [f"- {txt}: {url}" for _, txt, url in keep]
        parts = ["Meeting materials:\n" + "\n".join(lines)]
        if earliest:
            # The agenda's own phrasing: a floor, not a promise — the one
            # kind of start-time commentary that stays on a confirmed event.
            parts.append("Per the agenda this is the earliest start time — "
                         "the board convenes after preceding meetings "
                         "adjourn.")
        elif not confirmed:
            # Commentary only where uncertainty is real (house rule).
            parts.append("Start time: see the posted agenda.")
        ev.description = "\n\n".join(parts)


def fetch(session) -> list[Event]:
    _problems.clear()
    resp = session.get(SCHEDULE_URL, timeout=30)
    resp.raise_for_status()
    events = finalize(parse_page(resp.text))

    def _got(url, binary=False):
        r = session.get(url, timeout=60)
        r.raise_for_status()
        return r.content if binary else r.text

    try:
        enrich_materials(events, lambda u: _got(u), datetime.now().date(),
                         get_bytes=lambda u: _got(u, binary=True))
    except Exception as exc:  # upgrade-only: never sink the feed
        print(f"[{SOURCE}] WARNING: materials enrichment failed: {exc}")
    resp = session.get(COMMENT_URL, timeout=30)
    resp.raise_for_status()
    return events + finalize(parse_comment_page(resp.text))


def fetch_offline() -> list[Event]:
    """Build from fixtures/ (no network) — the --offline contract."""
    _problems.clear()
    # Fixtures captured in 2026; pin dates so they stay stable.
    events = finalize(
        parse_page((FIXTURES / "lcra_schedule.html").read_text(), default_year=2026)
    )
    # Materials enrichment on the real Aug-19 captures; today pinned so
    # that meeting is upcoming. The TSC agenda PDF (real document, 26pp,
    # "Earliest start time: noon") exercises the noon/earliest time
    # upgrade; other agendas have no fixture and return None, so the
    # LCRA-side event exercises the all-day + pointer path.
    enrich_materials(
        events,
        lambda u: (FIXTURES / "lcra_materials.html").read_text(),
        today=date(2026, 8, 14),
        get_bytes=lambda u: (
            (FIXTURES / "lcra_agenda_tsc_20260819.pdf").read_bytes()
            if "tsc-board-agenda" in u else None),
    )
    return events + finalize(
        parse_comment_page((FIXTURES / "lcra_comments.html").read_text(),
                           today=date(2026, 8, 4))
    )
