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

import copy
import json
import os
import pathlib
import re
import sys
from datetime import date, datetime, timedelta, timezone

import requests

from caltools.ics import CENTRAL, emit
from caltools.model import Event
from sources import (atp, austin, campo, capmetro, ctrma, curated, lcra,
                     legislature, openmeetings, puc, txdot, txdotev)

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
    # 9th org, and the validated palette only has 8 CVD-safe slots. Neutral
    # grey until the palette rethink (categorise by event or agency type)
    # noted on the Calendar Maintenance page happens -- better an obviously
    # unassigned colour than a 9th hue that collides with one of the 8.
    "legislature": ("CAM - Texas Legislature", legislature, "#6E6E6E"),
    # Curated-only substrate (see sources/puc.py) — neutral grey with
    # legislature until the palette rethink.
    "puc": ("CAM - PUC", puc, "#6E6E6E"),
}
# All 8 validated categorical slots are now assigned to orgs; the combined
# feeds get a neutral (they never appear next to org colors in the embed).
ALL_COLOR = "#6E6E6E"

# Kinds that are public-comment windows rather than meetings you attend.
# Widening this set is the only change needed to broaden the engagement
# feed: it is named for the concept, not for today's single kind, so it
# never has to be renamed -- renaming a feed breaks every subscriber.
ENGAGEMENT_KINDS = {"comment-window"}

# Combined feeds derived from all_events by filtering on kind. all.ics is
# deliberately NOT one of these: it keeps carrying everything, so nobody
# already subscribed to it silently loses events.
DERIVED_FEEDS = {
    "engagement": ("CAM - Public Engagement",
                   lambda e: e.kind in ENGAGEMENT_KINDS),
    "meetings": ("CAM - Meetings",
                 lambda e: e.kind not in ENGAGEMENT_KINDS),
}

USER_AGENT = (
    "cam-calendars/1.0 (+https://github.com/changesaroundme/calendars; "
    "ian@changesaroundme.com) public-meeting calendar builder"
)

# Corporation boards that only ever convene INSIDE a City Council meeting
# (the council members reconvening as a different legal entity mid-session:
# 11/11 AHFC and 3/3 Mueller meetings on record share a council day). On
# the CONSOLIDATED feeds (all.ics + derived) they fold into the council
# event; austin.ics keeps every event separate. DISPLAY-ONLY, and a
# DECLARED relationship — body listed here + same day as a council meeting.
# Never inferred from overlapping times (see the dedupe rule in the KB).
NESTED_BODIES = {
    "Austin Housing Finance Corporation": "AHFC",
    "Mueller Local Government Corporation": "Mueller",
    "Austin Industrial Development Corporation": "AIDC",
}
# How each corporation reads in the digest's boards bullet ("- AHFC and
# Mueller Local Government Boards" — Ian's wording, 2026-08-10); the short
# NESTED_BODIES names stay for titles and bullet-dedup matching.
NESTED_BULLETS = {
    "Austin Housing Finance Corporation": "AHFC",
    "Mueller Local Government Corporation": "Mueller Local Government",
    "Austin Industrial Development Corporation": "AIDC",
}


def _hm(dt: datetime) -> str:
    """House time style: `10:30am CDT` / `5pm CST` (minutes only if set)."""
    h12 = dt.hour % 12 or 12
    mins = f":{dt.minute:02d}" if dt.minute else ""
    zone = dt.replace(tzinfo=CENTRAL).tzname()
    return f"{h12}{mins}{'pm' if dt.hour >= 12 else 'am'} {zone}"


def _is_council_container(e: Event) -> bool:
    return (e.source == "austin" and e.summary.startswith("City Council")
            and "Committee" not in e.summary
            and "Work Session" not in e.summary
            and e.status != "CANCELLED")


def condense_council(events: list[Event]) -> list[Event]:
    """Fold nested corporation boards into their council meeting.

    Returns a new list of (copied) events for the consolidated feeds: each
    NESTED_BODIES meeting sharing a day with a council meeting disappears
    as a standalone entry; the council copy's title gains `+ AHFC` (with a
    trailing " Meeting" dropped first — it's obviously a meeting) and its
    description gains each board's time and agenda link. Uids and the
    original Event objects are untouched, so the org feeds and snapshots
    never see this. A board meeting with no council container that day
    passes through unchanged.
    """
    containers: dict[date, Event] = {}
    for e in events:
        if _is_council_container(e):
            d = start_day(e)
            if d not in containers or e.start < containers[d].start:
                containers[d] = e
    nested: dict[date, list[Event]] = {}
    out: list[Event] = []
    for e in events:
        if (e.source == "austin" and e.summary in NESTED_BODIES
                and start_day(e) in containers):
            nested.setdefault(start_day(e), []).append(e)
        else:
            out.append(e)
    for d, boards in nested.items():
        boards.sort(key=lambda e: (str(e.start), e.summary))
        original = containers[d]
        cont = copy.copy(original)
        base = re.sub(r"\s+Meeting$", "", cont.summary)
        cont.summary = base + "".join(
            f" + {NESTED_BODIES[b.summary]}" for b in boards)
        entries = []
        for b in boards:
            when = (_hm(b.start) if isinstance(b.start, datetime) else "time TBD")
            cancelled = " (CANCELLED)" if b.status == "CANCELLED" else ""
            link = f": {b.url}" if b.url else ""
            entries.append(f"{b.summary}{cancelled}, {when}{link}")
        # Blank line between entries: with wrapped MeetingDetail URLs they
        # were unreadable run together (Ian, 8/12).
        also = "Also convening within this meeting:\n" + "\n\n".join(entries)
        d0 = original.description or ""
        if d0.startswith("Summary Agenda"):
            # Digest stays on top, links below the — rule (Ian, 2026-08-10).
            # The corporations appear ONCE in the digest: their per-corp
            # matter-type bullets ("- 1 ahfc meeting item") collapse into a
            # single boards bullet; full names/times/links live below.
            head, _, tail = d0.partition("\n\n—\n\n")
            tokens = [NESTED_BODIES[b.summary].lower() for b in boards]
            lines = [ln for ln in head.splitlines()
                     if not (ln.startswith("- ")
                             and any(t in ln.lower() for t in tokens))]
            names = [NESTED_BULLETS[b.summary] for b in boards]
            joined = (" and ".join(names) if len(names) <= 2
                      else ", ".join(names[:-1]) + f", and {names[-1]}")
            lines.append(f"- {joined} Board{'' if len(names) == 1 else 's'}")
            cont.description = ("\n".join(lines) + "\n\n—\n\n"
                                + "\n\n".join(x for x in [tail, also] if x))
        else:
            cont.description = also + (f"\n\n{d0}" if d0 else "")
        out[out.index(original)] = cont
    return out


def snapshot_events(path: pathlib.Path) -> list[Event]:
    """Last-good events from a data/*.json snapshot, or [] if unreadable."""
    try:
        return [Event.from_json(d) for d in json.loads(path.read_text())]
    except Exception:
        return []


# How much past a published feed carries. Sources supply their own history
# now -- Legistar's "All Years" view reaches back to 2024, CTRMA's past
# accordions to 2003 -- and subscribers re-download the whole .ics on every
# poll, so cap what actually ships. data/*.json keeps whatever was scraped.
PUBLISH_HISTORY = timedelta(days=365)


def start_day(e: Event) -> date:
    """The calendar day an event begins on."""
    return e.start.date() if isinstance(e.start, datetime) else e.start


def event_day(e: Event) -> date:
    """The last day an event touches. Uses end so a comment window counts."""
    latest = e.end or e.start
    return latest.date() if isinstance(latest, datetime) else latest


def publish_horizon(today: date) -> date:
    """Far edge of the published window: the end of NEXT calendar year.

    A year boundary rather than a rolling count, because sources publish
    their annual schedules late in the preceding year -- a rolling window
    would clip a freshly-posted schedule at exactly the moment it appears,
    which is the moment it is most useful.

    Anything past it is either a data-entry artifact in the source (Austin's
    Legistar carried a lone "City Council" row dated 1/14/2030 when this
    shipped) or a parser that produced a wild year. Neither belongs in a
    subscriber's calendar; both stay in data/*.json.
    """
    return date(today.year + 1, 12, 31)


def has_future_events(events: list[Event], today: date) -> bool:
    """True if any event is still relevant (ends, or starts, today or later)."""
    return any(event_day(e) >= today for e in events)


def main() -> int:
    offline = "--offline" in sys.argv
    now = datetime.now(timezone.utc)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    # Transient-flake armor for EVERY source (added 2026-08-14 after three
    # single-fetch flakes turned three builds red in five days: senate 8/9,
    # txdot.gov 8/10, campotexas.org 8/12). Two retries with a pause cover
    # connection refusals, drops, and 5xx; a source that stays down through
    # all three attempts is a real signal and still fails loud.
    retry = requests.adapters.Retry(
        total=2, connect=2, read=2, status=2, backoff_factor=13,
        status_forcelist=[500, 502, 503, 504, 429],
        allowed_methods=["GET", "POST"])
    session.mount("https://", requests.adapters.HTTPAdapter(max_retries=retry))
    session.mount("http://", requests.adapters.HTTPAdapter(max_retries=retry))

    docs = (ROOT / "dist-offline" / "docs") if offline else DOCS
    data = (ROOT / "dist-offline" / "data") if offline else DATA
    docs.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)

    unhealthy: list[str] = []
    all_events = []
    events_by_key: dict[str, list[Event]] = {}

    # The publish window, shared by the normal path AND the snapshot
    # backfills below — raw snapshot events must pass the same bounds, or
    # a red day ships unbounded history to combined-feed subscribers.
    def in_window(e: Event) -> bool:
        return (event_day(e) >= today - PUBLISH_HISTORY
                and start_day(e) <= publish_horizon(today))

    # Central, not UTC. `now` stays UTC because DTSTAMP requires it, but
    # "is this meeting still upcoming" is a question about local wall-clock
    # days: now.date() flips over at 7pm Central, so an evening build
    # counted a meeting held earlier that same day as already past.
    today = now.astimezone(CENTRAL).date()

    # Hand-curated one-offs (events/curated.yaml) merge into org feeds below.
    curated.load(set(CALENDARS))
    unhealthy.extend(curated.health_problems())

    for key, (calname, module, color) in CALENDARS.items():
        snapshot_path = data / f"{key}.json"
        try:
            events = module.fetch_offline() if offline else module.fetch(session)
            hand = curated.events_for(key)
            # Same start + same org but a DIFFERENT uid: probably the same
            # meeting entered twice (scraper + yaml). Detection only — never
            # auto-merged: two committees genuinely can meet at the same
            # time, and a wrong merge silently deletes a real meeting. The
            # human resolves it by pinning `uid:` in curated.yaml (adopting
            # the scraped identity) or removing the entry.
            fetched_uids = {e.stable_uid() for e in events}
            fetched_starts = {e.start for e in events}
            for ev in hand:
                if ev.start in fetched_starts and ev.stable_uid() not in fetched_uids:
                    print(f"[{key}] WARNING: curated entry {ev.summary!r} shares "
                          "a start time with a scraped event but not its uid — "
                          "possible duplicate; pin `uid:` in curated.yaml or "
                          "remove the entry")
            # Curated first: on a uid tie (a pinned `uid:`), emit()'s
            # first-wins dedupe lets the hand-written entry override the
            # scraped copy — deliberate human data beats generic scrape.
            events = hand + events
            # SOS open-meetings enrichment (times/venues/cancellations onto
            # existing events, from LAST build's archive — see the module
            # docstring for the graduation story and the one-build lag).
            try:
                openmeetings.enrich_from_archive(events, key, today, data)
            except Exception as exc:  # upgrade-only: never sink a source
                print(f"[{key}] WARNING: SOS enrichment failed: {exc}")
            # --- past-event retention (append-only, keyed by uid) ---
            # Sources shed their own history: Legistar's "All Years" view
            # self-bounds at ~100 rows (run #72 dropped eight Oct/Nov-2025
            # council meetings the day the August rows appeared), committee
            # tables roll over, CTRMA lists upcoming only. Any PAST event
            # in the last snapshot whose uid this fetch no longer has is
            # carried forward verbatim. Future events are never carried —
            # a source delisting an upcoming meeting is a revision, not
            # history. No identity matching, ever: uid equality only.
            # (Emptiness health check below uses fresh_empty — a total
            # fetch collapse must not hide behind carried history.)
            fresh_empty = not events
            have = {e.stable_uid() for e in events}
            snap = snapshot_events(snapshot_path)
            carried = [e for e in snap
                       if event_day(e) < today and e.stable_uid() not in have]
            if carried:
                print(f"[{key}] retained {len(carried)} past events the "
                      "source no longer lists")
                events = events + carried
            # A fresh copy of a PAST event can be BARER than its archived
            # self: enrichment only runs for upcoming meetings, so the
            # morning after a meeting the scrape hands back the default
            # body and would overwrite the snapshot's agenda digest (the
            # CapMetro 90-day decay, run #72; the 24 Aug 2026 Design
            # Commission digest vanished the same way — Ian, 2026-08-25).
            # Field-wise repair, uid equality only, past events only:
            # keep the archived description when the fresh one lost its
            # "Summary Agenda" digest, and the archived url/location when
            # the fresh ones are empty. Future events: fresh always wins.
            snap_by_uid = {e.stable_uid(): e for e in snap}
            healed = 0
            for ev in events:
                if event_day(ev) >= today:
                    continue
                old_ev = snap_by_uid.get(ev.stable_uid())
                if old_ev is None:
                    continue
                kept = False
                if ("Summary Agenda" in old_ev.description
                        and "Summary Agenda" not in ev.description):
                    ev.description = old_ev.description
                    kept = True
                if not ev.url and old_ev.url:
                    ev.url = old_ev.url
                    kept = True
                if not ev.location and old_ev.location:
                    ev.location = old_ev.location
                    kept = True
                healed += kept
            if healed:
                print(f"[{key}] kept archived enrichment on {healed} "
                      "past events (fresh copies were barer)")
            events_by_key[key] = events
        except Exception as exc:
            print(f"[{key}] ERROR: fetch failed: {exc}")
            unhealthy.append(f"{key}: fetch failed ({exc})")
            # Per-org .ics goes stale-but-present (never rewritten); give
            # all.ics the same courtesy by backfilling from the last-good
            # snapshot, so combined-feed subscribers don't lose the org.
            stale = [e for e in snapshot_events(snapshot_path) if in_window(e)]
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
        # SPORADIC sources (legislature: hearings come in bursts) are
        # legitimately empty — or past-only — between events; alarming
        # there would guarantee red builds in every quiet stretch.
        sporadic = getattr(module, "SPORADIC", False) or getattr(
            module, "CURATED_ONLY", False)
        if fresh_empty and not sporadic:
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
            # Sporadic sources skip this too — past-only is their normal
            # state whenever the retained history outlives a hearing lull.
            if not offline and not sporadic and not has_future_events(events, today):
                problems.append(
                    f"{key}: no future events (all {len(events)} are in the past)"
                )
        unhealthy.extend(problems)

        # --- write outputs ---
        # Publish a bounded window; the snapshot below keeps everything the
        # source gave us, so git history stays complete either way.
        # Bounded at both ends: not finished too long ago, not starting too
        # far out. event_day for the near edge so an in-progress comment
        # window still counts; start_day for the far edge so one that opens
        # inside the horizon isn't dropped for ending just past it.
        cutoff, horizon = today - PUBLISH_HISTORY, publish_horizon(today)
        publishable = [e for e in events if in_window(e)]
        if publishable:
            # Always publish what we got (stale beats absent)...
            (docs / f"{key}.ics").write_text(
                emit(publishable, calname, now, color=color), newline=""
            )
            all_events.extend(publishable)
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
            # Nothing inside the publish window (parsed to zero — flagged
            # unhealthy above — or a sporadic source whose events all fell
            # outside it): keep the org present in all.ics from the
            # last-good snapshot, same window applied.
            stale = [e for e in snapshot_events(snapshot_path) if in_window(e)]
            if stale:
                print(f"[{key}] backfilling all.ics with {len(stale)} snapshot events")
                all_events.extend(stale)
        old_n = sum(1 for e in events if event_day(e) < cutoff)
        far_n = sum(1 for e in events if start_day(e) > horizon)
        notes = ([f"{old_n} older than the window"] if old_n else []) + \
                ([f"{far_n} beyond {horizon.year}"] if far_n else [])
        print(f"[{key}] {len(events)} events"
              + (f" ({', '.join(notes)})" if notes else ""))

    if all_events:
        # Consolidated feeds get the condensed council view (display-only;
        # the per-org feeds above were emitted from the untouched originals).
        # PUCT publishes ~30 routine open meetings a year; the consolidated
        # feeds carry only the ones that matter here — watched-case
        # meetings (kind flipped to "hearing" by puc.mark_watched_cases)
        # and comment windows / curated entries. puc.ics keeps everything.
        # Content-based (kind), so the gate survives snapshot round-trips.
        consolidated = [e for e in all_events
                        if not (e.source == "puc" and e.kind == "regular")]
        gated = len(all_events) - len(consolidated)
        if gated:
            print(f"[all] {gated} routine PUC open meetings stay on "
                  "puc.ics only (watched-case gate)")
        combined = condense_council(consolidated)
        folded = len(consolidated) - len(combined)
        if folded:
            print(f"[all] folded {folded} corporation-board meetings into "
                  "their council events")
        (docs / "all.ics").write_text(
            emit(combined, "CAM - All", now, color=ALL_COLOR), newline=""
        )
        print(f"[all] {len(combined)} events")
        for name, (calname, keep) in DERIVED_FEEDS.items():
            subset = [e for e in combined if keep(e)]
            (docs / f"{name}.ics").write_text(
                emit(subset, calname, now, color=ALL_COLOR), newline=""
            )
            print(f"[{name}] {len(subset)} events")

    # --- SOS open-meetings, SHADOW MODE: archive + log, never publish. ---
    # Non-fatal during observation; promote to a health check when the
    # enrichment pass ships.
    try:
        openmeetings.shadow(session, data, offline, events_by_key)
    except Exception as exc:
        print(f"[openmeetings] shadow error (non-fatal during observation): {exc}")

    if unhealthy:
        print("BUILD UNHEALTHY:\n  - " + "\n  - ".join(unhealthy))
        # Surface the list where a red run is actually READ (Ian,
        # 2026-08-25: the failure email said only "see build log above",
        # so every soft flake looked like a crisis): one GitHub annotation
        # per problem (top of the run page), the run's summary panel, and
        # unhealthy.txt for the workflow's failure step to echo. All three are
        # no-ops outside Actions.
        for prob in unhealthy:
            print("::error title=Unhealthy source::"
                  + prob.replace("%", "%25").replace("\n", " "))
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a") as f:
                f.write("### Unhealthy sources (feeds still published)\n\n"
                        + "\n".join(f"- {p}" for p in unhealthy) + "\n")
        (ROOT / "unhealthy.txt").write_text("\n".join(unhealthy) + "\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
