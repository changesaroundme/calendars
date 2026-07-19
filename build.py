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
from sources import campo, capmetro

ROOT = pathlib.Path(__file__).parent
DOCS = ROOT / "docs"
DATA = ROOT / "data"

CALENDARS = {
    "campo": ("CAM - CAMPO", campo),
    "capmetro": ("CAM - CapMetro", capmetro),
}

USER_AGENT = (
    "cam-calendars/1.0 (+https://github.com/changesaroundme/calendars; "
    "ian@changesaroundme.com) public-meeting calendar builder"
)


def load_fixture(key: str):
    if key == "campo":
        text = (ROOT / "fixtures" / "campo.ics").read_text()
        return campo.parse_feed(text)
    if key == "capmetro":
        rows = json.loads((ROOT / "fixtures" / "capmetro.json").read_text())
        events = capmetro.parse_calendar_html(
            (ROOT / "fixtures" / "capmetro_calendar.html").read_text()
        )
        return capmetro.finalize(capmetro.merge_api(events, rows))
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

    for key, (calname, module) in CALENDARS.items():
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
        problems = []
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
                emit(events, calname, now), newline=""
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
            emit(all_events, "CAM - All", now), newline=""
        )
        print(f"[all] {len(all_events)} events")

    if unhealthy:
        print("BUILD UNHEALTHY:\n  - " + "\n  - ".join(unhealthy))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
