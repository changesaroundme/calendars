"""Curated one-off events — the hand-maintained layer.

Scraper adapters cover pages that outlive the event cycle (annual
schedules, standing committees). One-off meetings — an open house, a
project-specific hearing, a pop-up comment session — usually live on pages
that die with the event, which is below the adapter threshold. Those go in
events/curated.yaml by hand (directly, or via a link dropped in
events/inbox.md for a Claude session to extract; either way the git diff
is the review step).

Design, deliberately minimal:
- No ninth feed. Each entry names an existing org calendar via `org:` and
  is merged into that calendar (and all.ics) by build.py.
- The YAML file IS the source of truth — read in both live and offline
  builds, so `python build.py --offline` validates your edit without
  touching the network or the published output.
- Fail loud, publish anyway: a malformed entry becomes a health problem
  (red CI run naming the entry); the valid entries still ship.

Entry fields are documented in events/curated.yaml itself.
"""
from __future__ import annotations

import pathlib
import re
from datetime import date, datetime, timedelta

import yaml

from caltools.model import Event, slugify

CURATED_PATH = pathlib.Path(__file__).resolve().parent.parent / "events" / "curated.yaml"
DEFAULT_LENGTH = timedelta(hours=2)
KINDS = {"regular", "work-session", "special", "budget", "hearing",
         "comment-window", "engagement"}

_events: dict[str, list[Event]] = {}
_problems: list[str] = []


def _parse_when(value):
    """date / datetime / ISO-ish string -> date|datetime, or raise ValueError.

    PyYAML already yields date for `2026-09-15` and datetime for
    `2026-09-15 17:30:00`, but `2026-09-15 17:30` (no seconds) stays a
    string — fromisoformat handles that form.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        s = value.strip()
        try:
            return date.fromisoformat(s)  # matches pure dates only
        except ValueError:
            return datetime.fromisoformat(s)
    raise ValueError(f"can't interpret {value!r} as a date or datetime")


def load(valid_orgs: set[str], path: pathlib.Path | None = None) -> None:
    """Read + validate the curated file; call once per build."""
    global _events, _problems
    _events, _problems = {}, []
    path = path or CURATED_PATH
    if not path.exists():
        return
    try:
        entries = yaml.safe_load(path.read_text()) or []
    except yaml.YAMLError as exc:
        _problems.append(f"curated.yaml: file does not parse ({exc})".replace("\n", " "))
        return
    if not isinstance(entries, list):
        _problems.append("curated.yaml: top level must be a list of entries")
        return

    for i, entry in enumerate(entries, 1):
        label = f"curated.yaml entry {i}"
        if not isinstance(entry, dict):
            _problems.append(f"{label}: not a mapping (check indentation)")
            continue
        if entry.get("summary"):
            label += f" ({entry['summary']!r})"
        missing = [k for k in ("summary", "start", "org") if not entry.get(k)]
        if missing:
            _problems.append(f"{label}: missing required field(s) {', '.join(missing)}")
            continue
        org = str(entry["org"])
        if org not in valid_orgs:
            _problems.append(
                f"{label}: unknown org '{org}' (valid: {', '.join(sorted(valid_orgs))})"
            )
            continue
        try:
            start = _parse_when(entry["start"])
            end = _parse_when(entry["end"]) if entry.get("end") else None
        except ValueError as exc:
            _problems.append(f"{label}: bad date ({exc})")
            continue
        timed = isinstance(start, datetime)
        if end is None:
            end = start + DEFAULT_LENGTH if timed else None
        elif not timed and not isinstance(end, datetime):
            end = end + timedelta(days=1)  # YAML end is the last day, inclusive
        if timed != isinstance(end, datetime) and end is not None:
            _problems.append(f"{label}: start and end must both have times, or neither")
            continue
        kind = str(entry.get("kind", "regular"))
        if kind not in KINDS:
            _problems.append(
                f"{label}: unknown kind '{kind}' (valid: {', '.join(sorted(KINDS))})"
            )
            continue
        ev = Event(
            source=org,  # lands in that org's feed; UID gets the org prefix
            summary=str(entry["summary"]),
            start=start,
            end=end,
            location=str(entry.get("location", "")),
            url=str(entry.get("url", "")),
            status="CANCELLED" if str(entry.get("status", "")).lower().startswith("cancel")
            else "CONFIRMED",
            kind=kind,
            uid=str(entry.get("uid", "")),  # optional override; else stable_uid
            # Collapse the YAML source's line wraps: a single newline inside
            # a paragraph is an artifact of editing width and renders as a
            # mid-sentence break in calendar apps; blank lines (paragraph
            # breaks) survive.
            description=re.sub(
                r"(?<!\n)\n(?!\n)", " ",
                str(entry.get("description", "")).strip()),
        )
        ev.uid = ev.stable_uid()
        _events.setdefault(org, []).append(ev)


def events_for(org: str) -> list[Event]:
    return list(_events.get(org, []))


def health_problems() -> list[str]:
    return list(_problems)
