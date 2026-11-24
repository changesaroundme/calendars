"""CapMetro adapter — thin wrapper over the shared Legistar machinery.

See sources/legistar.py for the two-layer design (InSite calendar page for
early dates + Web API for agenda enrichment) and the observed API lag that
motivates it (first seen here, 2026-07-18: the 7/27 Board meeting was on
Calendar.aspx while the API still returned nothing after 6/22).

Board and committee meetings also get a "Summary Agenda" digest built from
the API's agenda items — see agenda_digest below.
"""
from __future__ import annotations

import pathlib
import re

from caltools.model import Event
from sources.legistar import AGENDA_TITLE_CHARS, PROCEDURAL_TYPE_RE, Legistar
FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures"

# Bodies whose agenda items get fetched and summarized. Checked against the
# Web API on 2026-11-24: four Board meetings and one meeting of each
# committee, September to November 2026, all came back itemized. Work
# sessions are left out until one has been seen in the API.
# Each meeting of these bodies costs one extra API call per build from the
# day the API lists it until a week after it is held; ITEM_LOOKBACK_DAYS and
# ITEM_FETCH_CAP in sources/legistar.py set that window and the per-build
# ceiling.
AGENDA_BODIES = {
    "Board of Directors",
    "Finance, Audit and Administration Committee",
    "Operations, Planning and Safety Committee",
}

# --- agenda digest ----------------------------------------------------------
# CapMetro lays its agendas out differently from Austin's council, so it has
# its own digest instead of legistar.agenda_block (which reduced the 16 Nov
# 2026 Board agenda to "6 resolution items, 4 memo items, 3 other items...").
# What the Web API returns, as read on 2026-11-24 for six meetings:
#
#   - A section is a row numbered in Roman numerals ("VII. Action Items:").
#     The items under it are numbered from "1." again in every section.
#   - Rows arrive out of order; EventItemAgendaSequence is the agenda order.
#   - Rows with no agenda number are notices (livestream, ADA) or a memo
#     filed in support of the action item above it.
#   - Only its section heading marks a consent item: EventItemConsent is 0
#     on every row, including those under "Consent Items:".
#   - Most action items are one long sentence that opens "Approval of a
#     resolution authorizing the President & CEO, or her designee, to
#     finalize and execute ...", so the subject starts about 100
#     characters in.
#
# The digest keeps the agenda's own sections, in agenda order. Each item is
# reduced to its subject (the lead-ins below are dropped) and capped at the
# same length as Austin's lines. Consent items are counted, not listed, as
# on Austin's council agendas. Standing business is left out: minutes
# approvals, and the two "Committee Updates" sections that open each regular
# Board agenda.
CONSENT_SECTION_RE = re.compile(r"\bconsent\b", re.IGNORECASE)
STANDING_SECTION_RE = re.compile(r"\bcommittee\s+updates\b", re.IGNORECASE)
ITEM_LEAD_INS = (
    # "Approval of a resolution authorizing the President & CEO, or her
    # designee, to finalize and execute a contract with ..." becomes
    # "Contract with ..."; "Approval of a resolution adopting ..." becomes
    # "Adopting ...". Any other wording is left as the agenda has it.
    re.compile(
        r"^Approval of a resolution\s+(?:"
        r"authorizing the President\s*(?:&|and)\s*CEO, "
        r"or (?:her|his|their) designee, to\s+"
        r"(?:finalize and execute\s+(?:an?\s+)?)?"
        r"|(?=\w+ing\b))", re.IGNORECASE),
    # "Memo: ..." and "Memo - ...": the "Memos:" heading already says so.
    re.compile(r"^Memo\s*[:\u2013\u2014-]\s*", re.IGNORECASE),
    # The statute citation that the executive-session heading already gives.
    re.compile(r"^Executive Session under (?:Chapter|Section) 551 of the "
               r"Texas Government Code,\s*", re.IGNORECASE),
)


def _subject(title: str) -> str:
    """One agenda item's title as a digest line: lead-in dropped, capped."""
    for lead_in in ITEM_LEAD_INS:
        title = lead_in.sub("", title, count=1)
    title = title.strip(" .")
    title = title[:1].upper() + title[1:]
    if len(title) > AGENDA_TITLE_CHARS:
        title = title[:AGENDA_TITLE_CHARS - 1].rstrip() + "\u2026"
    return title


def agenda_digest(items: list[dict]) -> str:
    """Summary of one meeting's agenda items for the event description.

    `items` is the Web API's eventitems list for the meeting. Returns the
    digest text, opening with the "Summary Agenda" header every source's
    digest uses, or "" when the agenda has no numbered item outside the
    sections that are left out.
    """
    def _sequence(row: dict) -> int:
        seq = row.get("EventItemAgendaSequence")
        return seq if isinstance(seq, int) else 0

    sections: list[tuple[str, list[str]]] = []   # (heading, item titles)
    for row in sorted((r for r in items if isinstance(r, dict)),
                      key=_sequence):
        number = str(row.get("EventItemAgendaNumber") or "").strip()
        title = " ".join(str(row.get("EventItemTitle") or "").split())
        matter_type = str(row.get("EventItemMatterType") or "")
        if not number or not title:
            continue
        if not re.search(r"\d", number):
            sections.append((title.rstrip(" :"), []))
            continue
        if PROCEDURAL_TYPE_RE.search(matter_type):
            continue
        if not sections:
            sections.append(("", []))    # items listed before any heading
        sections[-1][1].append(title)

    blocks: list[str] = []
    for heading, titles in sections:
        if not titles or STANDING_SECTION_RE.search(heading):
            continue
        if CONSENT_SECTION_RE.search(heading):
            plural = "" if len(titles) == 1 else "s"
            blocks.append(f"- {len(titles)} consent item{plural}")
            continue
        lines = [f"{heading}:"] if heading else []
        blocks.append("\n".join(lines + [f"- {_subject(t)}" for t in titles]))
    return "Summary Agenda\n\n" + "\n\n".join(blocks) if blocks else ""


ADAPTER = Legistar(
    source="capmetro",
    client="capmetrotx",
    host="capmetrotx.legistar.com",
    prefix="CapMetro",
    # Their Legistar publishes only the room name; give subscribers a real
    # address (CapMetro HQ boardroom).
    location_fixes={
        "Rosa Parks Boardroom":
            "Rosa Parks Boardroom, CapMetro HQ, 2910 E. 5th St., Austin, TX 78702",
    },
    agenda_detail=lambda name: name in AGENDA_BODIES,
    agenda_digest=agenda_digest,
)

fetch = ADAPTER.fetch  # build.py calls module.fetch; everything else via ADAPTER


def fetch_offline() -> list[Event]:
    """Build from fixtures/ (no network) — the --offline contract."""
    import json

    rows = json.loads((FIXTURES / "capmetro.json").read_text())
    events = ADAPTER.parse_calendar_html(
        (FIXTURES / "capmetro_calendar.html").read_text()
    )
    # Agenda items as the Web API returned them on 2026-11-24 for two Board
    # meetings, in the API's order and cut down to the fields the digest
    # reads: 28 Sep 2026 (EventId 3059) and 16 Nov 2026 (EventId 3061). They
    # are served through the same gate as the live fetch, with its date
    # floor pinned a week before the 16 Nov meeting.
    saved = json.loads((FIXTURES / "capmetro_eventitems.json").read_text())

    def items_for(event_id: int) -> list[dict]:
        return [i for i in saved if i["EventItemEventId"] == event_id]

    def fetcher(row: dict):
        if not ADAPTER.wants_items(row, "2026-11-09"):
            return None
        return items_for(row["EventId"])

    events = ADAPTER.finalize(
        ADAPTER.merge_api(events, rows, item_fetcher=fetcher))
    _selftest_digest(events, september_items=items_for(3059))
    return events


def _selftest_digest(events: list[Event], september_items: list[dict]) -> None:
    """The saved agendas must keep producing the digest described above.

    Runs on every --offline build and raises AssertionError, so a change
    that breaks the digest fails there instead of reaching subscribers.
    """
    # Which bodies ask for agenda items at all.
    gate = lambda name: ADAPTER.wants_items(
        {"EventBodyName": name, "EventDate": "2026-11-16T00:00:00",
         "EventId": 1}, "2026-11-09")
    assert all(gate(name) for name in AGENDA_BODIES), \
        "a body in AGENDA_BODIES is not asking for its agenda items"
    assert not gate("Board of Directors Work Session"), \
        "work sessions are asking for agenda items"

    # 16 Nov 2026 Board meeting, through the whole merge: the only fixture
    # meeting on or after the date floor, so the only one with a digest.
    dated = [f"{e.start:%Y-%m-%d}" for e in events
             if "Summary Agenda" in e.description]
    assert dated == ["2026-11-16"], \
        f"expected one digest, on the 16 Nov 2026 Board meeting; got {dated}"
    text = next(e.description for e in events
                if "Summary Agenda" in e.description)
    # The digest opens with the first section that has business in it: the
    # committee updates and the minutes approval ahead of it are left out,
    # and the first item's "Approval of a resolution ..." lead-in is gone.
    assert text.startswith(
        "Summary Agenda\n\nAction Items:\n"
        "- Contract with Doggett Freightliner of South Texas, LLC,"), \
        f"16 Nov digest does not open with its first action item:\n{text}"
    # The lead-in is what pushed this item's subject past the length cap.
    assert ("- Construction contract with Jay-Reese Contractors, Inc. for the "
            "construction of the Plaza Saltillo Rail Station expansion, "
            "double tracking") in text, \
        f"16 Nov digest lost the Plaza Saltillo contract's subject:\n{text}"
    # The API returned the four memos as 4, 3, 1, 2; the agenda's order is
    # restored and the "Memo:" label dropped.
    assert ("Memos:\n- Fiscal Year 2026 Performance Update - November 9, "
            "2026\n- Bus Stop and Transit Amenity Standard") in text, \
        f"16 Nov digest does not list the memos in agenda order:\n{text}"
    # The agenda link that was already there stays below the rule.
    assert text.endswith("\n\n\u2014\n\nAgenda: https://capmetrotx.legistar1."
                         "com/capmetrotx/meetings/2026/11/3061_A_Board_of_"
                         "Directors_26-11-16_Agenda.pdf"), \
        f"16 Nov description does not end with its agenda link:\n{text}"

    # 28 Sep 2026 Board meeting, the digest alone: a consent agenda of ten
    # items, one of them the minutes, is counted and not listed.
    text = agenda_digest(september_items)
    assert text.startswith("Summary Agenda\n\n- 9 consent items\n\n"
                           "Action Items:\n- Adopting the Fiscal Year 2027 "
                           "Operating and Capital Budget"), \
        f"28 Sep digest does not open with its consent count:\n{text}"
    # Neither committee-updates section is listed, nor the unnumbered memo
    # about a Community Advisory Committee filed under the budget item.
    assert "Committee" not in text, \
        f"28 Sep digest lists a row that should be left out:\n{text}"
    # No line outgrows the cap ("- " plus the capped subject).
    assert all(len(line) <= AGENDA_TITLE_CHARS + 2
               for line in text.splitlines()), \
        f"28 Sep digest has a line over the length cap:\n{text}"
