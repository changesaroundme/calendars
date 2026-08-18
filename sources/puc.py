"""PUC adapter — the Commission's own calendar RSS, plus curated extras.

(Launched as "puct"; renamed feed/org/uids to "puc" on 2026-08-18, the day
after launch, while the subscriber count was ~zero — the one moment a feed
rename is cheap — renaming a published feed later breaks every subscriber.)

The PUC calendar page (agency/calendar/calendar.aspx) publishes a plain
RSS 2.0 feed of ~30 items a year ahead: Open Meetings (with room, and the
occasional off-site venue in the description) and Public Comment Deadlines
(with a Project number). Volume is low enough that everything ships — no
content filter needed at this layer (the filter concern in the KB was
about docket-level scraping, which this is not). SOAH docket hearings are
NOT on this calendar; those stay curated.

The RSS carries dates but not times; each item's AppointmentDetail page
states exact Start/End, so an upgrade-only enrichment pass confirms times
for upcoming items. Identity: the appointment ID in each item's link is
the Commission's own key — UIDs are puc-cal-<id>-<date>, so a curated
entry can pin one to override a scraped copy with richer text (the
2026-08-28 open meeting and Project 58482 deadline both do this).
"""
from __future__ import annotations

import html as htmllib
import json
import pathlib
import re
from datetime import date, datetime, timedelta
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from caltools.model import Event

SOURCE = "puc"
RSS_URL = "https://www.puc.texas.gov/agency/calendar/getcalendarrss/"
CALENDAR_URL = "https://www.puc.texas.gov/agency/calendar/calendar.aspx"
ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
SOS_ARCHIVE = ROOT / "data" / "openmeetings.json"

# Ian's watched cases (2026-08-17). Open meetings whose SOS-filed agenda
# names one of these get kind="hearing" plus a description note — and
# build.py's consolidated feeds carry ONLY non-"regular" puc events, so
# routine open meetings stay on puc.ics without cluttering all.ics.
# Curated entries (kind hearing/comment-window) pass the same gate.
WATCHED_CASES = {
    "59029": "765-kV Longshore Switch - Drill Hole Switch line (Oncor CCN)",
    "59315": "765-kV Dinosaur Switch - Longshore Switch line (Oncor CCN)",
}

HEARING_ROOM = ("Commissioners' Hearing Room 7-100, William B. Travis "
                "Building, 1701 N. Congress Ave., Austin, TX 78701")
DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October"
    r"|November|December)\s+(\d{1,2}),\s+(20\d{2})")
MONTHS = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"])}
ID_RE = re.compile(r"[?&]ID=(\d+)", re.IGNORECASE)
PROJECT_RE = re.compile(r"Project\s*(?:</strong>)?\s*(\d{4,6})")
DETAIL_TIMES_RE = re.compile(
    r"Start:\s*(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2}):\d{2}\s*([AP])M"
    r".*?End:\s*(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2}):\d{2}\s*([AP])M",
    re.IGNORECASE | re.DOTALL)
DETAIL_HORIZON = timedelta(days=45)
DETAIL_FETCH_CAP = 6

_problems: list[str] = []


def health_problems() -> list[str]:
    return list(_problems)


# --- TLS trust for puc.texas.gov -------------------------------------------
# See certs/puc_intermediates.pem for the incident write-up: the server
# doesn't always send its certificate chain, so PUCT requests verify
# against certifi's roots PLUS those pinned intermediates. Built lazily
# into a temp file once per process; falls back to default verification
# if the pinned file ever goes missing.
INTERMEDIATES = ROOT / "certs" / "puc_intermediates.pem"
_verify_path: str | bool | None = None


def _verify() -> str | bool:
    global _verify_path
    if _verify_path is None:
        try:
            import tempfile
            import certifi
            f = tempfile.NamedTemporaryFile("wb", suffix=".pem", delete=False)
            f.write(pathlib.Path(certifi.where()).read_bytes())
            f.write(b"\n")
            f.write(INTERMEDIATES.read_bytes())
            f.close()
            _verify_path = f.name
        except Exception as exc:
            print(f"[{SOURCE}] WARNING: trust-bundle build failed ({exc}); "
                  "using default verification")
            _verify_path = True
    return _verify_path


def _hm(h: str, mm: str, mer: str) -> tuple[int, int]:
    return int(h) % 12 + (12 if mer.upper() == "P" else 0), int(mm)


def parse_rss(xml_text: str) -> list[Event]:
    events: list[Event] = []
    root = ElementTree.fromstring(xml_text)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc_html = htmllib.unescape(item.findtext("description") or "")
        dm = DATE_RE.search(item.findtext("pubDate") or "") \
            or DATE_RE.search(title)
        idm = ID_RE.search(link)
        if not (dm and idm and title):
            continue
        try:
            d = date(int(dm.group(3)), MONTHS[dm.group(1)], int(dm.group(2)))
        except ValueError:
            continue
        kind_part = title.split(" - ")[0].strip()
        desc_text = BeautifulSoup(desc_html, "html.parser") \
            .get_text(" ", strip=True)
        pm = PROJECT_RE.search(desc_html)
        # Off-site venue rides in the description AFTER the boilerplate
        # ("( Open To Public ) AT&T Hotel and Conference Center (...)");
        # the default is the Commissioners' Hearing Room.
        tail = re.sub(r"^.*?\(\s*Open To Public\s*\)\s*", "", desc_text)
        tail = re.sub(r"^(NA|Project\s*\d+)\s*", "", tail).strip()
        location = tail if len(tail) > 10 else HEARING_ROOM
        if kind_part.lower().startswith("public comment deadline"):
            summary = ("PUC - Project " + pm.group(1) + " comments close"
                       if pm else "PUC - Public comment deadline")
            kind = "comment-window"
            body = ("Comments are filed via the PUC Interchange: "
                    "https://interchange.puc.texas.gov/filer"
                    + (f"\n\nProject filings: https://interchange.puc.texas"
                       f".gov/search/filings/?UtilityType=A&ControlNumber="
                       f"{pm.group(1)}" if pm else ""))
            location = ""
        else:
            summary = f"PUC - {kind_part}"
            kind = "regular"
            # Same shape as senate hearings: meetings you can't attend in
            # person are still watchable — live and archived.
            body = ("Watch live or archived: "
                    "https://www.puc.texas.gov/agency/calendar/broadcasts/")
        events.append(Event(
            source=SOURCE,
            summary=summary,
            start=d,   # all-day until the detail page confirms the time
            location=location,
            url=link,
            kind=kind,
            description=body,
            # The Commission's own appointment id + the day it names.
            uid=(f"{SOURCE}-cal-{idm.group(1)}-{d:%Y%m%d}"
                 "@calendars.changesaroundme.com"),
        ))
    if not events:
        _problems.append("puc: calendar RSS parsed to zero items "
                         "(feed moved or reshaped?)")
    return events


def enrich_details(events: list[Event], get_html, today: date) -> None:
    """Confirm Start/End times from each upcoming item's detail page."""
    horizon = today + DETAIL_HORIZON
    spent = 0
    for ev in events:
        if ev.kind == "comment-window" or isinstance(ev.start, datetime):
            continue  # deadlines stay all-day; already-timed stays put
        d = ev.start
        if not today <= d <= horizon or spent >= DETAIL_FETCH_CAP:
            continue
        spent += 1
        try:
            page = get_html(ev.url)
            if not page:
                continue  # offline: no fixture for this item
            text = BeautifulSoup(page, "html.parser").get_text(" ", strip=True)
        except Exception as exc:  # enrichment must never break the feed
            print(f"[{SOURCE}] WARNING: detail fetch failed ({ev.url}): {exc}")
            continue
        m = DETAIL_TIMES_RE.search(text)
        if not m:
            continue
        sd = (int(m.group(3)), int(m.group(1)), int(m.group(2)))
        if date(*sd) != d:
            continue  # page names a different day than the feed item
        ev.start = datetime(*sd, *_hm(m.group(4), m.group(5), m.group(6)))
        end_d = (int(m.group(9)), int(m.group(7)), int(m.group(8)))
        ev.end = datetime(*end_d, *_hm(m.group(10), m.group(11), m.group(12)))


def mark_watched_cases(events: list[Event],
                       archive_path: pathlib.Path = SOS_ARCHIVE) -> None:
    """Flag open meetings whose SOS-filed agenda names a watched case.

    The SOS open-meetings shadow (sources/openmeetings.py) already archives
    every PUCT filing with its agenda text; a filing whose meeting date
    equals an event's date is that meeting's agenda — the match key is
    declared twice over (agency + date), no inference. Agendas post ~a week
    ahead, so a meeting flips from org-feed-only to consolidated exactly
    when a watched case lands on its posted agenda. The archive is last
    build's snapshot (shadow runs after sources) — one run of lag, well
    inside the posting lead time.
    """
    try:
        archive = {k: v for k, v in
                   json.loads(archive_path.read_text()).items()
                   if "Utility Commission" in v.get("Agency Name", "")}
    except Exception:
        return  # no archive yet (first run) — nothing to mark
    for ev in events:
        if ev.kind != "regular":
            continue
        d = ev.start.date() if isinstance(ev.start, datetime) else ev.start
        hits = []
        for v in archive.values():
            m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})",
                         v.get("Meeting Date", ""))
            if not m or date(int(m.group(3)), int(m.group(1)),
                             int(m.group(2))) != d:
                continue
            agenda = v.get("Agenda", "")
            hits = [(n, label) for n, label in WATCHED_CASES.items()
                    if n in agenda]
            if hits:
                break
        if hits:
            ev.kind = "hearing"  # the consolidated-feed gate (see WATCHED_CASES)
            block = "Watched cases on the posted agenda:\n" + "\n".join(
                f"- Docket {n}: {label}" for n, label in hits)
            ev.description = (f"{block}\n\n{ev.description}"
                              if ev.description else block)


def fetch(session) -> list[Event]:
    _problems.clear()
    # Tripwire: the pinned anchor and PUC's whole chain expire 2028-12-31
    # (see certs/puc_intermediates.pem). Start failing loud two months out
    # so the re-chain lands before the cliff.
    if date.today() >= date(2028, 11, 1):
        _problems.append("puc: pinned trust anchors expire 2028-12-31 — "
                         "refresh certs/puc_intermediates.pem (see header)")
    resp = session.get(RSS_URL, timeout=30, verify=_verify())
    resp.raise_for_status()
    events = parse_rss(resp.text)
    mark_watched_cases(events)

    def _got(url):
        r = session.get(url, timeout=30, verify=_verify())
        r.raise_for_status()
        return r.text

    try:
        enrich_details(events, _got, datetime.now().date())
    except Exception as exc:  # upgrade-only
        print(f"[{SOURCE}] WARNING: detail enrichment failed: {exc}")
    return events


def fetch_offline() -> list[Event]:
    """Build from fixtures/ (no network) — the --offline contract."""
    _problems.clear()
    events = parse_rss((FIXTURES / "puc_rss.xml").read_text())
    # Detail chain on the real ID=1806 capture (28 Aug open meeting,
    # 9:30 AM - 5:00 PM); other items have no fixture and skip.
    details = {"https://www.puc.texas.gov/agency/calendar/"
               "AppointmentDetail.aspx?ID=1806":
               FIXTURES / "puc_detail_1806.html"}

    enrich_details(events,
                   lambda u: details[u].read_text() if u in details else None,
                   today=date(2026, 8, 17))
    # Watched-case marking against a pinned SOS-archive extract (the real
    # 14 Aug 2026 filing, agenda naming both 765-kV dockets).
    mark_watched_cases(events, FIXTURES / "puc_sos_openmeetings.json")
    return events
