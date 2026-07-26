#!/usr/bin/env python3
"""Build all calendars: fetch each source, write per-org .ics + all.ics.

Usage:
  python build.py             # live fetch (normal mode; what CI runs)
  python build.py --offline   # build from fixtures/ (no network; for dev
                              # and the CI parser-regression check)

Live outputs land in docs/ (served by GitHub Pages) plus JSON snapshots in
data/ so every change to the underlying schedules shows up in git history
as a readable diff. Offline outputs land in dist-offline/ (gitignored) —
an offline run NEVER touches the published docs/ or the data/ baselines,
so a dev build can't accidentally ship fixture data to subscribers.

Each source module implements fetch(session) for live and fetch_offline()
for fixture builds; the fixture wiring lives with the parser it exercises.

Health checks: a source that yields zero events, or that shrinks by more
than half versus its last snapshot, marks the build unhealthy (exit 1) —
the calendars still get written, but CI goes red so a silent page redesign
can't quietly starve the feeds.
"""
from __future__ import annotations

import json
import pathlib
import sys
from datetime import date, datetime, timezone

import requests

from caltools.ics import emit
from caltools.model import Event
from sources import (atp, austin, campo, capmetro, ctrma, curated, lcra,
                     txdot, txdotev)

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


def snapshot_events(path: pathlib.Path) -> list[Event]:
    """Last-good events from a data/*.json snapshot, or [] if unreadable."""
    try:
        return [Event.from_json(d) for d in json.loads(path.read_text())]
    except Exception:
        return []


def has_future_events(events: list[Event], today: date) -> bool:
    """True if any event is still relevant (ends, or starts, today or later).

    Uses end when present so an in-progress comment window counts.
    """
    for e in events:
        latest = e.end or e.start
        d = latest.date() if isinstance(latest, datetime) else latest
        if d >= today:
            return True
    return False


def main() -> int:
    offline = "--offline" in sys.argv
    now = datetime.now(timezone.utc)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    docs = (ROOT / "dist-offline" / "docs") if offline else DOCS
    data = (ROOT / "dist-offline" / "data") if offline else DATA
    docs.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)

    unhealthy: list[str] = []
    all_events = []

    today = now.date()

    # Hand-curated one-offs (events/curated.yaml) merge into org feeds below.
    curated.load(set(CALENDARS))
    unhealthy.extend(curated.health_problems())

    for key, (calname, module, color) in CALENDARS.items():
        snapshot_path = data / f"{key}.json"
        try:
            events = module.fetch_offline() if offline else module.fetch(session)
            events = events + curated.events_for(key)
        except Exception as exc:
            print(f"[{key}] ERROR: fetch failed: {exc}")
            unhealthy.append(f"{key}: fetch failed ({exc})")
            # Per-org .ics goes stale-but-present (never rewritten); give
            # all.ics the same courtesy by backfilling from the last-good
            # snapshot, so combined-feed subscribers don't lose the org.
            stale = snapshot_events(snapshot_path)
            if stale:
                print(f"[{key}] backfilling all.ics with {len(stale)} snapshot events")
                all_events.extend(stale)
            continue

        # --- health checks ---
        previous_count = None
        if snapshot_path.exists():
            try:
                previous_count = len(json.loads(snapshot_path.read_text()))
            except Exception:
                pass
        problems = list(getattr(module, "health_problems", lambda: [])())
        if not events:
            problems.append(f"{key}: 0 events parsed")
        else:
            if previous_count and len(events) < previous_count / 2:
                problems.append(
                    f"{key}: event count fell from {previous_count} to {len(events)}"
                )
            # Catches the "count looks fine but everything is past" class:
            # stale annual PDFs, prior-year tables, default-year drift.
            # Live-only: fixtures are point-in-time snapshots whose dates
            # age past naturally, and that's not a parser regression.
            if not offline and not has_future_events(events, today):
                problems.append(
                    f"{key}: no future events (all {len(events)} are in the past)"
                )
        unhealthy.extend(problems)

        # --- write outputs ---
        if events:
            # Always publish what we got (stale beats absent)...
            (docs / f"{key}.ics").write_text(
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
        else:
            # Parsed to zero (already flagged unhealthy above): keep the org
            # present in all.ics from the last-good snapshot.
            stale = snapshot_events(snapshot_path)
            if stale:
                print(f"[{key}] backfilling all.ics with {len(stale)} snapshot events")
                all_events.extend(stale)
        print(f"[{key}] {len(events)} events")

    if all_events:
        (docs / "all.ics").write_text(
            emit(all_events, "CAM - All", now, color=ALL_COLOR), newline=""
        )
        print(f"[all] {len(all_events)} events")

    if unhealthy:
        print("BUILD UNHEALTHY:\n  - " + "\n  - ".join(unhealthy))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
