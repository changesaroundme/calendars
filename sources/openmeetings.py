"""Texas SOS open-meetings filings — enrichment source, currently SHADOW MODE.

Reads the UNT Libraries daily mirror of the SOS open-meetings bulletin
(one static HTML file of ~100 recent filings, each a labeled field table
with full agenda text) and filters to WATCH — Ian's org list, nothing
else, to keep the calendar from being overwhelmed.

Why the mirror and not the SOS Appian portal: the portal is a SAIL app
with no API (probed 2026-07-26 — versioned proprietary headers, virtualized
grid, agenda text one request per record). The mirror is one fetch and
includes agendas. Its ~100-record rolling window is narrower than the
portal (~197 that day), but filings arrive 1-3 weeks before their meeting
and a daily fetch records each one in data/openmeetings.json (keyed by TRD)
before it can roll off. If shadow observation shows misses, the portal is
the documented escalation path.

SHADOW MODE means: fetch, filter, archive to data/openmeetings.json, and
LOG what an enrichment pass would have done — but never touch published
events. After a couple of weeks of daily snapshots in git history prove
the matching, the enrichment pass gets built on the evidence. Planned
enrichment (per Ian): upgrade EXISTING events only — exact times onto
all-day placeholders, addresses, Teams links + agenda into descriptions,
Status: Cancelled -> STATUS:CANCELLED. Never creates events.

Filings for watched orgs with no feed yet (CARTS, PUCT, TTI) are archived
for observation but marked unroutable. PUCT will eventually need a
content filter (Ian cares about docket 59475 / the 765kV project, not
every docket) — that's the `include` regex below, dormant until PUCT has
somewhere to route.
"""
from __future__ import annotations

import json
import pathlib
import re
from datetime import date, datetime

from bs4 import BeautifulSoup

MIRROR_URL = "https://texinfo.library.unt.edu/texasregister/openmeetings/OpenMeetings.html"
FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures"
# Chars of agenda text to archive per filing. 8000, not 2000 (2026-08-17):
# PUC open-meeting agendas run 40+ items, and the puct watched-case match
# reads this archive — a watched docket past the cut would be missed.
AGENDA_KEEP = 8000

# (Agency Name pattern, our org key or observation-only placeholder,
#  optional `include` regex the agenda/committee text must match)
# Keys campo/ctrma/lcra/txdot/txdotev route to real feeds; carts/puct/tti
# have no feed yet — archived for observation only.
WATCH = [
    (r"capital area metropolitan planning", "campo", None),
    (r"capital area rural transportation", "carts", None),
    (r"central texas regional mobility", "ctrma", None),
    (r"lower colorado river authority", "lcra", None),
    (r"public utility commission", "puct", None),  # future: r"59475|765\s*.?kV"
    (r"transportation institute", "tti", None),
    (r"texas department of transportation", "txdot", None),  # rerouted below
]
# TxDOT filings split by body: advisory committees live in the txdotev
# feed, the commission in txdot. Committee-name pattern -> key.
TXDOT_COMMITTEE_ROUTES = [
    (r"bicycle and pedestrian", "txdotev"),
    (r"public transportation advisory", "txdotev"),
    (r"transportation commission", "txdot"),
]

FIELD_RE = re.compile(r"^(.*?):\s*$")


def _parse_record(table) -> dict:
    rec: dict[str, str] = {}
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        label = cells[0].get_text(" ", strip=True).rstrip(":").strip()
        value = cells[1].get_text("\n", strip=True)
        if label:
            rec[label] = value
    return rec


def parse_mirror(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    records = []
    for table in soup.find_all("table"):
        rec = _parse_record(table)
        if rec.get("TRD") and rec.get("Agency Name"):
            records.append(rec)
    return records


def _route(rec: dict) -> str:
    """Our org key for a matched filing ('' = watched but unroutable)."""
    agency = rec.get("Agency Name", "").lower()
    for pattern, key, include in WATCH:
        if not re.search(pattern, agency):
            continue
        if include and not re.search(
            include, rec.get("Agenda", "") + " " + rec.get("Committee", ""),
            re.IGNORECASE,
        ):
            return ""
        if key == "txdot":  # split TxDOT bodies across our two feeds
            body = (rec.get("Committee", "") + " " + rec.get("Board", "")).lower()
            for cpat, ckey in TXDOT_COMMITTEE_ROUTES:
                if re.search(cpat, body):
                    return ckey
            return ""  # unrecognized TxDOT body: archive, don't route
        return key
    return ""


def watchlist_records(records: list[dict]) -> list[tuple[dict, str]]:
    """(record, routed key or '') for filings from watched agencies only."""
    out = []
    watch_re = re.compile("|".join(p for p, _, _ in WATCH))
    for rec in records:
        if watch_re.search(rec.get("Agency Name", "").lower()):
            out.append((rec, _route(rec)))
    return out


def _meeting_date(rec: dict) -> date | None:
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", rec.get("Meeting Date", ""))
    try:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else None
    except ValueError:
        return None


def shadow(session, data_dir: pathlib.Path, offline: bool, events_by_key: dict) -> None:
    """Observation pass: archive watched filings + log would-be enrichment."""
    if offline:
        html = (FIXTURES / "openmeetings.html").read_text()
    else:
        resp = session.get(MIRROR_URL, timeout=30)
        resp.raise_for_status()
        html = resp.text

    matched = watchlist_records(parse_mirror(html))
    archive_path = data_dir / "openmeetings.json"
    try:
        archive = json.loads(archive_path.read_text())
    except Exception:
        archive = {}

    today = date.today().isoformat()
    new = 0
    for rec, key in matched:
        trd = rec["TRD"]
        entry = archive.get(trd) or {"first_seen": today}
        new += trd not in archive
        entry.update(
            {f: rec.get(f, "") for f in
             ("Status", "Related TRD", "Submitted Date/Time", "Agency Name",
              "Board", "Committee", "Meeting Date", "Meeting Time", "Address",
              "City", "State", "Additional Information")},
            Agenda=rec.get("Agenda", "")[:AGENDA_KEEP],
            routed=key,
            last_seen=today,
        )
        archive[trd] = entry

        # would-be enrichment log (shadow only — nothing is modified)
        d = _meeting_date(rec)
        who = f"{rec['Agency Name']}" + (
            f" / {rec.get('Committee') or rec.get('Board')}"
            if (rec.get("Committee") or rec.get("Board")) not in (None, "", "N/A")
            else ""
        )
        if not key:
            print(f"[openmeetings] TRD {trd} {who} {rec.get('Meeting Date')}: "
                  "watched, no route (observation only)")
        elif d and any(
            (e.start.date() if isinstance(e.start, datetime) else e.start) == d
            for e in events_by_key.get(key, [])
        ):
            print(f"[openmeetings] TRD {trd} {who} {rec.get('Meeting Date')}: "
                  f"WOULD ENRICH a {key} event "
                  f"({' '.join(rec.get('Meeting Time', '?').split())}, "
                  f"status {rec.get('Status')})")
        else:
            print(f"[openmeetings] TRD {trd} {who} {rec.get('Meeting Date')}: "
                  f"no matching {key} event on that date (enrichment would skip; "
                  "never creates)")

    archive_path.write_text(json.dumps(archive, indent=1, sort_keys=True) + "\n")
    print(f"[openmeetings] shadow: {len(matched)} watchlist filings "
          f"({new} new) archived to {archive_path.name}")
