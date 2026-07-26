#!/usr/bin/env python3
"""Build all calendars: fetch each source, write per-org .ics + all.ics.

Usage:
  python build.py             # live fetch (normal mode; what CI runs)
  python build.py --offline   # build from fixtures/ (no network; for dev)

Outputs land in docs/ (served by GitHub Pages) plus JSON snapshots in data/
so every change to the underlying schedules shows up in git history as a
readable diff.

Health checks: a source that yields zero events, or that shrinks by more
than half versus its last snapshot, marks the build unhealthy (exit 1) —
the calendars still get written, but CI goes red so a silent page redesign
can't quietly starve the feeds.
"""
from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timezone

import requests

from caltools.ics import emit
from sources import atp, austin, campo, capmetro, ctrma, lcra, txdot, txdotev

ROOT = pathlib.Path(__file__).parent
DOCS = ROOT / "docs"
DATA = ROOT / "data"

# key -> (display name, adapter module, default color for Apple clients)
CALENDARS = {
    "campo": ("CAM - CAMPO", campo, "#2A78D6"),      # blue (slot 1)
    "capmetro": ("CAM - CapMetro", capmetro, "#008300"),  # green (slot 2)
    "txdot": ("CAM - TTC", txdot, "#E87BA4"),  # magenta (slot 3)
    "lcra": ("CAM - LCRA", lcra, "#EDA100"),  # yellow (slot 4)
    "ctrma": ("CAM - CTRMA", ctrma, "#1BAF7A"),  # aqua (slot 5)
    "austin": ("CAM - City of Austin", austin, "#EB6834"),  # orange (slot 6)
    "atp": ("CAM - ATP", atp, "#4A3AA7"),  # violet (slot 7)
    "txdotev": ("CAM - TxDOT Events", txdotev, "#E34948"),  # red (slot 8)
}
# All 8 validated categorical slots are now assigned to orgs; the combined
# feed gets a neutral (it never appears next to org colors in the embed).
ALL_COLOR = "#6E6E6E"

USER_AGENT = (
    "cam-calendars/1.0 (+https://github.com/changesaroundme/calendars; "
    "ian@changesaroundme.com) public-meeting calendar builder"
)


def load_fixture(key: str):
    if key == "campo":
        text = (ROOT / "fixtures" / "campo.ics").read_text()
        return campo.parse_feed(text)
    if key == "atp":
        return atp.parse_feed((ROOT / "fixtures" / "atp.ics").read_bytes())
    if key == "txdotev":
        events = txdotev.parse_page(
            (ROOT / "fixtures" / "txdotev_utp.html").read_text(),
            "UTP",
            "https://www.txdot.gov/projects/planning/utp/utp-public-involvement.html",
        )
        for abbrev, name, url in txdotev.COMMITTEES:
            fixture = ROOT / "fixtures" / f"txdotev_{abbrev.lower()}.html"
            events.extend(
                txdotev.parse_committee_page(
                    fixture.read_text(), abbrev, name, url, min_year=2026
                )
            )
        return events
    if key == "capmetro":
        rows = json.loads((ROOT / "fixtures" / "capmetro.json").read_text())
        events = capmetro.ADAPTER.parse_calendar_html(
            (ROOT / "fixtures" / "capmetro_calendar.html").read_text()
        )
        return capmetro.ADAPTER.finalize(capmetro.ADAPTER.merge_api(events, rows))
    if key == "txdot":
        return txdot.finalize(
            txdot.parse_page((ROOT / "fixtures" / "txdot.html").read_text())
        )
    if key == "lcra":
        return lcra.finalize(
            lcra.parse_page(
                (ROOT / "fixtures" / "lcra_schedule.html").read_text(),
                default_year=2026,
            )
        )
    if key == "ctrma":
        return ctrma.finalize(
            ctrma.parse_page((ROOT / "fixtures" / "ctrma_meetings.html").read_text())
        )
    if key == "austin":
        council = austin.COUNCIL.finalize(
            austin.COUNCIL.merge_api(
                austin.COUNCIL.parse_calendar_html(
                    (ROOT / "fixtures" / "austin_legistar.html").read_text()
                ),
                json.loads((ROOT / "fixtures" / "austin_api.json").read_text()),
            )
        )
        boards = austin.parse_board_page(
            (ROOT / "fixtures" / "austin_board.html").read_text(),
            "Planning Commission",
            "https://www.austintexas.gov/boards-commissions/meetings/40_1",
            # no fallbacks: exercises the Meeting Details auto-extraction
        )
        records = austin.parse_council_pdf(austin.ANNUAL_PDF_FIXTURE.read_bytes())
        council = austin.apply_budget_flags(council, records)
        annual = austin.annual_council_events(records, council)
        return council + annual + boards
    raise KeyError(key)


def main() -> int:
    offline = "--offline" in sys.argv
    now = datetime.now(timezone.utc)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    DOCS.mkdir(exist_ok=True)
    DATA.mkdir(exist_ok=True)

    unhealthy: list[str] = []
    all_events = []

    for key, (calname, module, color) in CALENDARS.items():
        try:
            events = load_fixture(key) if offline else module.fetch(session)
        except Exception as exc:
            print(f"[{key}] ERROR: fetch failed: {exc}")
            unhealthy.append(f"{key}: fetch failed ({exc})")
            continue

        # --- health checks ---
        snapshot_path = DATA / f"{key}.json"
        previous_count = None
        if snapshot_path.exists():
            try:
                previous_count = len(json.loads(snapshot_path.read_text()))
            except Exception:
                pass
        problems = list(getattr(module, "health_problems", lambda: [])())
        if not events:
            problems.append(f"{key}: 0 events parsed")
        elif previous_count and len(events) < previous_count / 2:
            problems.append(
                f"{key}: event count fell from {previous_count} to {len(events)}"
            )
        unhealthy.extend(problems)

        # --- write outputs ---
        if events:
            # Always publish what we got (stale beats absent)...
            (DOCS / f"{key}.ics").write_text(
                emit(events, calname, now, color=color), newline=""
            )
            all_events.extend(events)
            # ...but only advance the snapshot baseline when healthy, so a
            # shrink alarm keeps firing until the data actually recovers
            # (otherwise the shrunken count becomes tomorrow's baseline and
            # the alarm silences itself after one red run).
            if not problems:
                snapshot = sorted(
                    (e.to_json() for e in events), key=lambda d: d["start"]
                )
                snapshot_path.write_text(json.dumps(snapshot, indent=1) + "\n")
        print(f"[{key}] {len(events)} events")

    if all_events:
        (DOCS / "all.ics").write_text(
            emit(all_events, "CAM - All", now, color=ALL_COLOR), newline=""
        )
        print(f"[all] {len(all_events)} events")

    if unhealthy:
        print("BUILD UNHEALTHY:\n  - " + "\n  - ".join(unhealthy))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
