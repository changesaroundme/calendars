# calendars

> [!WARNING]
> **Vibe-coded — take with a grain of salt.** This project was built with
> heavy AI assistance and light human review, and it aggregates public
> meeting information on a best-effort basis. Entries can be wrong, stale,
> or missing entirely. Always confirm details against the official source
> (every event links to its origin) before making plans around it, and do
> not rely on these calendars for anything safety-critical, legal-deadline,
> or financial.

Subscribable `.ics` calendars of Central Texas public meetings, rebuilt
daily by GitHub Actions from official sources and served by GitHub Pages.
The build runs every morning at 6:17 AM Central and on every push.

**Subscribe: <https://changesaroundme.com/calendar>** — the canonical feed
directory. The data itself is served from
<https://changesaroundme.github.io/calendars/>, including the browsable
combined calendar at
[`embed.html`](https://changesaroundme.github.io/calendars/embed.html).

**Sources**

| Source | Adapter type | Where the data comes from |
|---|---|---|
| CAMPO | ICS ingest | Their published Tribe Events iCal feed |
| CapMetro | HTML scrape + API merge | Legistar `Calendar.aspx` (early dates) + Legistar Web API (agenda links) |
| TxDOT Commission | HTML scrape | Commission meeting-dates page (full year of dates; agendas ~8 days out) |
| LCRA | HTML scrape | Annual schedule tables (dates only → all-day events; times post with agendas) |
| CTRMA | HTML scrape | Upcoming-meeting cards on their board-meetings page (date, time, detail link) |
| City of Austin | Legistar (shared module) + annual PDF + board pages | Council/committees via `austintexas` Legistar; year schedule from the EDIMS PDF; boards & commissions plus the Bicycle and Pedestrian Advisory Councils from full-year date lists on austintexas.gov |
| ATP | ICS ingest | Austin Transit Partnership's published Tribe iCal feed (Board + Community Advisory Committee) |
| TxDOT Events | HTML tables scrape | Public-involvement pages (UTP): dated events + comment windows; advisory committees (BPAC, PTAC) |
| Texas Legislature | *Curated only* | No scraper: committee notices come down at end of day, so a daily build can't catch same-day postings. `sources/legislature.py` exists so `events/curated.yaml` entries have a feed to land in |
| One-off events | Hand-curated | `events/curated.yaml` — see below |
| SOS open meetings | *Shadow mode* | UNT mirror of Texas SOS filings, watchlist-filtered and archived to `data/openmeetings.json`; observation only until the enrichment pass ships |

**Outputs** (in `docs/`, served by Pages)

- `campo.ics`, `capmetro.ics`, `txdot.ics`, `lcra.ics`, `ctrma.ics`, `austin.ics`, `atp.ics`, `txdotev.ics`, `legislature.ics` — one calendar per organization
- `all.ics` — everything combined (never filtered; existing subscribers rely on it)
- `meetings.ics` — everything except `ENGAGEMENT_KINDS`; `engagement.ics` — only those.
  The two are disjoint and their union is `all.ics`. Widen the set in
  `build.py` to reclassify; never rename a feed, it breaks subscribers.
- `index.html` — stub pointing to the canonical directory on changesaroundme.com; `embed.html` — filterable month/list view for embedding

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

An `--offline` build writes to `dist-offline/` (gitignored) and never
touches the published `docs/` or the `data/` health baselines — so a dev
run can't accidentally commit fixture data over the live calendars. CI
runs the offline build after every publish as a parser-regression check.

`data/*.json` are normalized snapshots committed on every build, so `git log`
doubles as a change history of the underlying schedules — you can see exactly
when a meeting appeared, moved, or was cancelled.

## How it works

```
sources/<org>.py     fetch(session) + fetch_offline() → list[Event]
sources/curated.py   loads events/curated.yaml into org feeds
sources/openmeetings.py  SOS filings watcher (shadow mode)
caltools/model.py    Event dataclass + stable UIDs
caltools/ics.py      RFC 5545 emitter (folding, escaping, VTIMEZONE)
build.py             orchestrates, health-checks, writes docs/ + data/
```

Every adapter exposes two entry points: `fetch(session)` does the live
scrape; `fetch_offline()` builds the same output from `fixtures/`
(point-in-time snapshots of real markup), keeping each parser's fixture
wiring next to the parser it exercises.

Design notes worth keeping in mind:

- **Stable UIDs.** An event's UID is derived from source + body name + date
  + start time, so re-scrapes *update* events in subscribers' calendars
  instead of duplicating them, and two same-day meetings of one body stay
  distinct. Display names (the "CapMetro - " prefix) are applied after the
  UID is frozen, so renames never change identity. CAMPO and ATP events
  keep their feeds' own UIDs (those feeds are the system of record).
- **Health checks over silence.** A source yielding zero events, shrinking
  more than half versus its last snapshot, or containing no future events
  turns the Actions run red — but calendars still publish, with `all.ics`
  backfilled from the last-good snapshot for any source that failed
  outright. Scraper breakage should be loud; stale data shouldn't take
  down what still works.
- **Cancellations.** Marked in titles by some sources ("… - Cancelled"),
  in a status field by others; adapters strip the marker from the name
  (identity survives) and set `STATUS:CANCELLED` so capable clients render
  the event struck-through. Cancelled events are kept, never deleted.
- **Legistar shows one month by default (observed 2026-07-28).** `Calendar.aspx`
  opens on a "This Month" period filter, so a plain GET structurally cannot see
  next month's meetings. CapMetro turned the build red with "no future events"
  the day after its 7/27 board meeting while 8/24 through 12/14 were already
  scheduled and simply hidden. The adapter now replays the page as an ASP.NET
  postback with the period set to "All Years", and falls back to the default
  view if the postback fails or comes back with fewer rows. Legistar caps the
  grid at the 100 most recent rows, so "All Years" self-bounds — measured on
  capmetrotx as 100 rows spanning Feb 2024 to Dec 2026. "This Year" would have
  emptied out every 1 January and taken the year's history with it; letting
  each source carry its own past is why this project keeps no archive of its
  own. Published feeds are bounded at both ends in `build.py`: back to
  `PUBLISH_HISTORY` (12 months), since subscribers re-download the whole
  `.ics` on every poll, and forward to the end of *next* calendar year. The
  far edge is a year boundary rather than a rolling count so a newly-posted
  annual schedule is never clipped at the moment it appears; it also drops
  source data-entry artifacts, e.g. the lone Austin "City Council" row dated
  1/14/2030. `data/*.json` keeps everything either way.
- **Legistar API lag (observed 2026-07-18).** CapMetro's Web API only lists
  meetings once agendas publish; the Calendar.aspx page shows them earlier.
  Hence the two-layer adapter. Worth rechecking for any future Legistar org.
- **Server markup ≠ DevTools markup (observed 2026-07-26).** TxDOT's CMS
  builds table captions client-side; fixtures must be captured from a raw
  same-origin fetch, not the rendered DOM.

## One-off events (events/curated.yaml)

Not every meeting deserves a scraper. The rule of thumb: **adapter** when
the page outlives the event cycle with stable structure (annual schedules,
standing committees); **curated entry** otherwise (project open houses,
one-off hearings, pop-up comment windows — pages that die with the event).

Add entries to `events/curated.yaml` by hand (field docs are in the file),
or drop links in `events/inbox.md` for a Claude session to extract, then
review the git diff and push. Each entry names an existing org calendar
via `org:` and merges into it — no separate feed. `python build.py
--offline` validates the file locally; a malformed entry turns CI red
with a message naming it, while valid entries still publish.

## Adding a source

Write `sources/neworg.py` with `fetch(session) -> list[Event]` and a
`fetch_offline() -> list[Event]` that builds from a checked-in fixture,
then register the module in `CALENDARS` in `build.py`. Current backlog,
roughly in order: promote the SOS open-meetings watcher from shadow mode
to enrichment (times/addresses/agendas/cancellations onto existing
events), un-park the ERCOT adapter (branch `add-ercot`, awaiting a
committee filter), Texas Senate committee hearings (ephemeral page —
needs faster-than-daily polling), PUCT (needs a content filter first),
Speak Up Austin engagement events, and a custom domain
(`calendars.changesaroundme.com`) before the URLs spread widely.
