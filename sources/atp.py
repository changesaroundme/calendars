"""ATP adapter — ingest Austin Transit Partnership's Tribe Events iCal feed.

Same platform and pattern as CAMPO: ATP publishes a subscribable calendar
covering Board meetings and Community Advisory Committee meetings (plus any
other events they post). We re-ingest it, keep their UIDs (their feed is the
system of record), and prefix the display names.

Note (observed 2026-07-25): the feed listed only CAC meetings — the board
meets "third Wednesday, but not every month" per [[Organiations]], so board
entries appear whenever ATP schedules one; nothing extra to do here.

Source: https://www.atptx.org/?post_type=tribe_events&ical=1&eventDisplay=list
"""
from __future__ import annotations

import pathlib
import re
from datetime import date, datetime
from icalendar import Calendar

from caltools.model import Event
from sources.legistar import CANCEL_RE

FEED_URL = "https://www.atptx.org/?post_type=tribe_events&ical=1&eventDisplay=list"
SOURCE = "atp"
FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures"
DEFAULT_LOCATION = "ATP Office, 203 Colorado St, Austin, TX 78701"


def parse_feed(ics_data: bytes | str) -> list[Event]:
    cal = Calendar.from_ical(ics_data)
    events: list[Event] = []
    for component in cal.walk("VEVENT"):
        summary = str(component.get("SUMMARY", "")).strip()
        dtstart = component.get("DTSTART").dt if component.get("DTSTART") else None
        if dtstart is None or not summary:
            continue
        events.append(
            Event(
                source=SOURCE,
                # Org prefix is display-only; identity comes from ATP's UID.
                # Their feed titles some events "ATP Board of Directors
                # Meeting" — strip the leading ATP so the prefix doesn't
                # stutter ("ATP - ATP Board..."; Ian, 2026-08-09).
                summary="ATP - " + re.sub(r"^ATP\s+", "", summary),
                start=dtstart,
                end=component.get("DTEND").dt if component.get("DTEND") else None,
                location=str(component.get("LOCATION", "")).strip() or DEFAULT_LOCATION,
                url=str(component.get("URL", "")).strip(),
                status="CANCELLED" if CANCEL_RE.search(summary) else "CONFIRMED",
                kind="hearing" if "hearing" in summary.lower() else "regular",
                uid=str(component.get("UID", "")).strip(),
            )
        )
    return events


# --- CAC agenda enrichment --------------------------------------------------
# Project Connect posts each CAC meeting's agenda PDF on its committee page;
# the filename carries the meeting date (20260813_CAC_Agenda.pdf), which is
# the declared match key — an event enriches only from an agenda whose
# filename date equals its own date. (Typo'd filenames exist in the wild —
# "202630611" — and simply skip.) The agenda itself is one page of Roman-
# numeral sections with lettered items; per Ian's format (2026-08-12) only
# the Action and Discussion sections are shown, with COA normalized to CoA.
CAC_PAGE = "https://www.projectconnect.com/community-advisory-committee/"
CAC_AGENDA_HREF_RE = re.compile(
    # (?=\D) so nine-digit typos ("202602012_CAC_Agenda.pdf" — real, Feb
    # 2026) can't yield a plausible-but-wrong 8-digit date as a match key.
    r"href=[\"']?(https?://[^\"'\s>]*?/(\d{8})(?=\D)[^\"'\s>]*?CAC[_-]?Agenda"
    r"[^\"'\s>]*?\.pdf)", re.IGNORECASE)
CAC_SECTION_RE = re.compile(r"^([IVX]+)\.\s+(.+)$")
CAC_ITEM_RE = re.compile(r"^([a-z])\.\s+(.+)$")
CAC_WANTED = {"action items": "Action Items:",
              "discussion items": "Discussion Items:"}


def cac_agenda_summary(pdf_bytes: bytes) -> str | None:
    import io
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages[:3])
    section = None
    blocks: dict[str, list[str]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        sm = CAC_SECTION_RE.match(line)
        if sm:
            section = CAC_WANTED.get(sm.group(2).strip().lower())
            continue
        im = CAC_ITEM_RE.match(line)
        if section and im:
            item = re.sub(r"\s+", " ", im.group(2)).strip(" .,")
            item = re.sub(r"\bCOA\b", "CoA", item)  # house abbreviation
            blocks.setdefault(section, []).append(f"- {item}")
        elif section and line and blocks.get(section):
            if len(blocks[section][-1]) < 200:  # wrapped item continues
                blocks[section][-1] += " " + line
    if not blocks:
        return None
    out = ["Summary Agenda"]  # standardized digest header (Ian, 2026-08-09)
    for label in CAC_WANTED.values():
        if label in blocks:
            out.append(label + "\n" + "\n".join(blocks[label]))
    return "\n\n".join(out)


def enrich_cac(events: list[Event], get_html, get_bytes, today: date) -> None:
    """Attach agenda summaries to upcoming CAC events in place."""
    cac = [e for e in events
           if "community advisory committee" in e.summary.lower()
           and e.status != "CANCELLED"
           and today <= (e.start.date() if isinstance(e.start, datetime)
                         else e.start)]
    if not cac:
        return
    try:
        agendas = {m.group(2): m.group(1)
                   for m in CAC_AGENDA_HREF_RE.finditer(get_html(CAC_PAGE))}
    except Exception as exc:  # enrichment must never break the feed
        print(f"[{SOURCE}] WARNING: CAC page fetch failed: {exc}")
        return
    for ev in cac[:3]:
        day = ev.start.date() if isinstance(ev.start, datetime) else ev.start
        url = agendas.get(day.strftime("%Y%m%d"))
        if not url:
            continue
        try:
            block = cac_agenda_summary(get_bytes(url))
        except Exception as exc:
            print(f"[{SOURCE}] WARNING: CAC agenda parse failed ({url}): {exc}")
            continue
        # Standardized shape (Ian, 2026-08-09): summary on top, — rule, link.
        link_line = f"Agenda: {url}"
        addition = f"{block}\n\n—\n\n{link_line}" if block else link_line
        if addition not in ev.description:
            ev.description = (f"{ev.description}\n\n{addition}"
                              if ev.description else addition)


def fetch(session) -> list[Event]:
    resp = session.get(FEED_URL, timeout=30)
    resp.raise_for_status()
    # Bytes, not resp.text: avoids charset-guess mojibake.
    events = parse_feed(resp.content)

    def _got(url, binary=False):
        r = session.get(url, timeout=60)
        r.raise_for_status()
        return r.content if binary else r.text

    try:
        enrich_cac(events, lambda u: _got(u),
                   lambda u: _got(u, binary=True), datetime.now().date())
    except Exception as exc:  # upgrade-only
        print(f"[{SOURCE}] WARNING: CAC enrichment failed: {exc}")
    return events


def fetch_offline() -> list[Event]:
    """Build from fixtures/ (no network) — the --offline contract."""
    events = parse_feed((FIXTURES / "atp.ics").read_bytes())
    # Enrichment chain on real captures (page region + the 13 Aug 2026
    # agenda); today pinned so that meeting is upcoming.
    enrich_cac(
        events,
        lambda u: (FIXTURES / "atp_cac_page.html").read_text(),
        lambda u: (FIXTURES / "atp_agenda_cac_20260813.pdf").read_bytes(),
        today=date(2026, 8, 10),
    )
    return events
