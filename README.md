# calendars

Subscribable `.ics` calendars of Central Texas public meetings (Austin-area
sources today; statewide bodies on the roadmap), rebuilt daily by GitHub
Actions from official sources and served by GitHub Pages.

**v1 sources**

| Source | Adapter type | Where the data comes from |
|---|---|---|
| CAMPO | ICS ingest | Their published Tribe Events iCal feed |
| CapMetro | HTML scrape + API merge | Legistar `Calendar.aspx` (early dates) + Legistar Web API (agenda links) |
| TxDOT Commission | HTML scrape | Commission meeting-dates page (full year of dates; agendas ~8 days out) |
| LCRA | HTML scrape | Annual schedule tables (dates only → all-day events; times post with agendas) |
| CTRMA | HTML scrape | Upcoming-meeting cards on their board-meetings page (date, time, detail link) |

**Outputs** (in `docs/`, served by Pages)

- `campo.ics`, `capmetro.ics`, `txdot.ics`, `lcra.ics`, `ctrma.ics` — one calendar per organization
- `all.ics` — everything combined
- `index.html` — landing page with subscribe links

## One-time setup

1. Create a new GitHub repository named `calendars` and push this
   folder to its `main` branch:

   ```sh
   git remote add origin https://github.com/changesaroundme/calendars.git
   git push -u origin main
   ```

2. Enable Pages: **Settings → Pages → Deploy from a branch → `main` /docs**.

3. Kick off the first real build: **Actions → Build calendars → Run
   workflow**. This replaces the checked-in snapshot calendars with a fresh
   live fetch. It then runs itself daily at 6:17 AM CDT (5:17 AM CST —
   Actions cron is fixed UTC). The workflow declares its own
   `permissions: contents: write`, so no repository settings change is
   needed for it to commit.

Subscribe URLs will be:

```
webcal://changesaroundme.github.io/calendars/all.ics
webcal://changesaroundme.github.io/calendars/campo.ics
webcal://changesaroundme.github.io/calendars/capmetro.ics
```

Link those from anywhere (e.g. an Obsidian Publish page). Calendar apps
re-poll subscriptions on their own schedule — typically every few hours to
daily, which matches the daily rebuild.

## Day-to-day: pushing changes

The Actions bot commits refreshed calendars after every push and every
morning, so your local clone is almost always slightly behind the remote.
The rhythm that always works:

```sh
git add -A
git commit -m "what changed"
git pull --rebase   # replay your commit on top of the bot's
git push
```

(Committing first matters — rebase refuses to run over uncommitted changes.)

## Local development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python build.py --offline   # build from fixtures/, no network
python build.py             # live fetch (from your machine)
```

`data/*.json` are normalized snapshots committed on every build, so `git log`
doubles as a change history of the underlying schedules — you can see exactly
when a meeting appeared, moved, or was cancelled.

## How it works

```
sources/<org>.py     fetch() → list[Event]        (one adapter per org)
caltools/model.py    Event dataclass + stable UIDs
caltools/ics.py      RFC 5545 emitter (folding, escaping, VTIMEZONE)
build.py             orchestrates, health-checks, writes docs/ + data/
```

Design notes worth keeping in mind:

- **Stable UIDs.** An event's UID is derived from source + body name + date
  + start time, so re-scrapes *update* events in subscribers' calendars
  instead of duplicating them, and two same-day meetings of one body stay
  distinct. Display names (the "CapMetro - " prefix) are applied after the
  UID is frozen, so renames never change identity. CAMPO events keep CAMPO's
  own UIDs (their feed is the system of record).
- **Health checks over silence.** A source yielding zero events, or shrinking
  more than half versus its last snapshot, turns the Actions run red — but
  calendars still publish. Scraper breakage should be loud, stale data
  shouldn't take down what still works.
- **Legistar API lag (observed 2026-07-18).** CapMetro's Web API only lists
  meetings once agendas publish; the Calendar.aspx page shows them earlier.
  Hence the two-layer adapter. Worth rechecking for any future Legistar org.
- **Cancellations.** CAMPO marks them in the title ("… - Cancelled"); the
  adapter also sets `STATUS:CANCELLED` so capable clients render them
  struck-through.

## Adding a source

Write `sources/neworg.py` with a `fetch(session) -> list[Event]`, register it
in `CALENDARS` in `build.py`, add a fixture if practical. Candidate backlog,
roughly easiest-first: Texas SOS / UNT open-meetings snapshot (covers every
TX state + regional body), Texas Senate committee hearings (ephemeral page —
needs faster polling), Austin boards & commissions (static HTML), TxDOT UTP
comment windows (static HTML; the comment-period table is server-rendered —
an earlier "JS-rendered" suspicion was a fetch-truncation artifact).

Fixtures under `fixtures/` are point-in-time dev snapshots reconstructed from
the live sources; the first CI run overwrites all published output with a
fresh fetch.
