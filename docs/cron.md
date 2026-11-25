# Background Jobs (Cron)

The application uses arq for background job scheduling to periodically update cached data.

## Overview

- **Framework**: arq (Redis-based job queue)
- **Scheduling**: Cron-like syntax for job timing
- **Async**: Asynchronous job execution
- **Persistence**: Jobs survive restarts via Redis

## Job Schedule

| Job | Function | Schedule | Purpose |
|-----|----------|----------|---------|
| Rankings | `rankings_cron` | Every 30 min | Update team rankings |
| Matches | `matches_cron` | Every 5 min | Update match listings |
| Riot Matches | `riot_matches_cron` | Every 5 min at :02, :07, … | Archive every completed game Riot's esports match API lists, then link the unlinked ones to VLR maps (`app/cron/riot_matches.py`); runs when `RIOT_API_KEY` is set, and links only when `LLM_API_KEY` is also set |
| Events | `events_cron` | Every 30 min | Update event listings |
| News | `news_cron` | Every 30 min | Update news articles |
| Standings | `standings_cron` | Daily 00:00 | Update current year standings |
| Team Rankings | `team_rankings_cron` | Every 15 min | Upsert newly completed matches, mirror Riot ids onto them, add settled Riot-only series, and rebuild the Elo ledger (`app/cron/team_rankings.py`) |
| FCM Notifications | `fcm_notification_cron` | Every 15 min | Legacy "match starting soon" alert on the `match-`/`event-`/`team-` topics (`app/cron/legacy_fcm.py`) |
| Live Matches | `live_push_cron` | Every minute | Send APNs/FCM scores for tracked matches (listed live, or already pushed); at most one run at a time (fixed arq `job_id`) |
| Riot Web Schedule | `riot_web_schedule_cron` | Every 10 min at :01, :11, … | Store Riot's web schedule and link each series to its VLR match (`app/cron/riot_web.py`); runs when `RIOT_WEB_API_KEY` is set |
| Riot Web Details | `riot_web_details_cron` | Every 10 min at :04, :14, … | Fetch `getEventDetails` for linked series that are due and have started: at most 30 a run, refreshed hourly, settled 48 hours after the start; an unlinked series is fetched once up to three days before its start, for its tournament id, on every run for six hours after its start (the list shows it live), hourly for the rest of its first day, and again once it is 24 hours old, so its game wins are final |

The team rankings job upserts every completed match that is missing from the ledger,
stored without both teams, a decisive score, or every played map, or contradicted by
the listing's teams and scores. It then replays the whole ledger into the series Elo
(`k48-hnone-m0.3-r0`, no decay), the per-map Elo, and the circuit counts. The replay
is deterministic, so a fetch or write failure only leaves that match for the next
run and can never move a rating out of order. Each upserted match keeps VLR's event
patch, which `/api/v2/rankings/predict` forwards to the external prediction service
as the model's history scope. Historical database seeding and
catch-up are manual deployment steps; import scripts are not included in this
change. Complete them before enabling ongoing ingestion.

The live push cron runs at most once at a time. It fetches every match the listing
shows as live plus every match with stored push state. A tracked match is ended only
by VLR fetch failures while VLR no longer lists it as live: a 404, or a 5xx on three
consecutive runs (`valesports:push:fetch_failures:<match>`, 600-second TTL), using the
last score sent. Parser errors and unreachable-VLR errors are logged without
counting toward the streak. While the broadcast tracker reports a healthy read and
its latest score was delivered, the tracker drives that match's pushes and the cron
only refreshes its metadata; the cron resumes pushing when the tracker errors, goes
quiet, or a delivery fails. The tracker's score endpoint schedules the same push and
refresh work as in-process FastAPI background tasks, so those two paths are not arq
jobs and do not appear in the schedule above.

When the tracker is up but VLR cannot serve it — its score stalls over a match VLR keeps
pushing, or its team codes match no VLR match for 30 minutes — the cron logs an ERROR,
which Sentry turns into an event. See
[Broadcast tracker alerts](live-alerts.md) for both alert paths and the rule runbook.

`RIOT_WEB_API_KEY` is the only switch for the Riot web overlays: **set, every Riot web
feature runs; unset, no Riot web call is made** and the matching responses are VLR-only: the match
list drops its Riot-only entries and every match is shown under its VLR id again, so unsetting it is
the way to turn the feature off (a Riot id still opens its match).
There are no feature flags, and `ENABLE_CACHE` is not needed for them: the cache
wrappers no-op when caching is off, so the jobs still store what they fetch and the
brackets route still answers from Riot.

With the key set, the live push cron also reads Riot's `getLiveDetails` list to find
matches VLR's listing has not caught up to. The cron runs once a minute, as long as Riot's
own CDN window, so the answer is not cached. Riot is additive only: it decides which VLR
pages the cron reads, and VLR's detail still decides every push, ending and final. A
Riot-only id with no stored push state whose VLR page is not live is skipped and not kept
as a live copy: Riot's list lingers about 11 minutes after a series ends, and would
otherwise resend the Android FINAL every minute. That linger can also defer the
fetch-failure path, so it does so only for a five-minute grace window starting at the first
consecutive failed read, and never for a 404; after that VLR's failure decides. Riot never ends a match: its list omits
whole leagues, so only VLR can end one.

With the key unset, live discovery stops, `map_vods` is empty and brackets serve empty
`sections`; the rest of each response is unchanged.

The Riot web schedule job runs whenever `RIOT_WEB_API_KEY` is set. It walks
`getSchedule` by `pageToken` (never `page=`), at most five pages and stopping at the
three-day lookback edge, then upserts `riot_web_matches` and
links a series only when the normalised team-name pair and the start time agree within two
hours (six hours only when exactly one candidate fits). Two or more candidates are logged
and left unlinked. A link is written once and never rewritten, so a wrong link stays until
it is cleared by hand:

```sql
UPDATE riot_web_matches SET vlr_match_id = NULL, vlr_event_id = NULL
WHERE riot_id = '<riot id>';
```

Also clear the mirror on the VLR ledger row with
`UPDATE matches SET riot_id = NULL WHERE riot_id = '<riot id>' AND id <> riot_id;` (the team rankings cron
clears a mirror its link no longer backs anyway). A row whose `id` is the Riot id is the match clients
know by that id: if the wrong VLR match adopted it, also set its `vlr_id` to NULL so the right one can.

The next run may link it again from scratch. If a run logs `Riot and VLR times disagree for
every name match`, at least three name pairs were found and none was inside two hours: the
host's `TIMEZONE` no longer matches the zone VLR's schedule renders in, so check that
before suspecting the alias table.

The details job runs whenever `RIOT_WEB_API_KEY` is set, every 10
minutes at :04, :14, …. It fetches `getEventDetails` for at most 30 linked series that are
due and have started — never fetched, or fetched over an hour ago while the series started less
than 48 hours ago — and stores each series' per-map VOD offsets and `tournament_id`. A series that
has not started is never polled, so a match two weeks out is not fetched every hour until kickoff.
After the 48 hours a series is settled and never fetched again. `event: null` for most 2021–2023
ids is expected and leaves `tournament_id` NULL, and a fetched series with no readable games keeps
its previously stored offsets. Every id is fetched before any write, so no transaction spans
network I/O; a rejected key or a `429` ends the run after storing what was already fetched.

The Riot web brackets route fetches `getStandings?tournamentId=…` on demand for every
tournament a linked series names, all at once with a 5-second timeout each. A rejected key
alerts like the other jobs and leaves the response `unavailable`; a `429` or another failure
makes it `stale` when another tournament answered. There is no brackets cron and no stored
layout: only a complete converted response is cached in Redis for 60 seconds, so the next
request retries a tournament that failed.

**Rotating `RIOT_WEB_API_KEY`.** Riot rotates the site's frontend key without notice. A
Sentry ERROR `Riot web rejected RIOT_WEB_API_KEY` (throttled to one an hour) means it was
rejected. Open https://valorantesports.com with devtools → Network, filter `esports-api`,
copy the `x-api-key` request header, check it with

```sh
curl -s -o /dev/null -w '%{http_code}\n' -H "x-api-key: $KEY" \
  'https://esports-api.service.valorantesports.com/persisted/val/getLiveDetails?hl=en-US&sport=val'
```

(a valid key prints `200`; a rejected one prints `403`), then set `RIOT_WEB_API_KEY` in
`~/val-esports-backend/.env` and restart the service. Removing `RIOT_WEB_API_KEY` disables
the jobs and leaves every Riot-web field empty, and VLR responses are unaffected.

### Riot-only matches in the ledger

After its VLR upserts, `team_rankings_cron` runs `attach_riot_ids`, which clears a
`matches.riot_id` mirror its link no longer backs, folds a VLR row into the Riot-only row of the same match when a series is linked after both were stored
(the Riot row keeps its id and takes over VLR's values and maps, and the VLR row is deleted), and
copies each linked series id onto its VLR row. It then stores every series the details cron fetched that no
VLR match claimed (unlinked, 24 hours to three days after its start, no result stored yet) in its
Riot-only `matches` row, whose id is the Riot series id. The schedule cron creates that row, empty,
when the match list starts to show the series: an unlinked series within three days of now with two
named teams (also on VLR's side: an entry VLR still shows with an undecided team holds the series
back, so the match is not listed twice and then renamed) and a known Riot tournament (the row's event is that tournament's one VLR event, else the
Riot-created event), unless a listed VLR match no other series is linked to has the same two teams within six
hours. A result is stored
only when both teams are mirrored to distinct VLR teams by Riot name (partner teams come from
`scripts.mirror_team_logos`; any other Riot name, such as a Challengers team at a LAN, is first paired
with the one unpaired VLR team whose name folds to the same letters and digits, and left alone when
there are several), its stored game score is
decisive and fetched after the series settled (an early fetch can hold a partial score), its
tournament id is 11 to 20 digits, and no VLR match has the same two teams within a day. When VLR later lists the match without a stored link, `ingest_match` adopts the one Riot-only row of the same two teams that started within six hours of it and stores the missing link. A
tournament that names exactly one VLR event ledgers the match under it; one that names several is
skipped; one that names none is ledgered under a Riot-created event whose id is the tournament id
(see [database.md](database.md)), written in the same transaction as its first match.
`adopt_riot_events` runs in the `attach_riot_ids` transaction and moves a Riot-created
event's matches to the VLR event its tournament has since been linked to. Anything missing is
retried on the next run until the series leaves the window. Such a
row has no maps and is rated on its series outcome only. When VLR lists the series later, the
row is adopted: it keeps its id, gains `vlr_id`, and VLR's values and maps replace Riot's, so
the series is rated once.

## Riot esports match archive

`riot_matches_cron` is a separate integration on Riot's official esports match API and is
gated only on `RIOT_API_KEY`, not on `RIOT_WEB_API_KEY`. It archives every completed game
Riot lists, whether or not VLR has a map for it yet. Linking an archived game to its VLR
map asks the model, so it runs only when `LLM_API_KEY` is also set; without it the archive
still runs and only the linking step is skipped. See the job table above for its schedule.

The run has a four-minute budget and stops early when Riot answers `429`; whatever was
found so far is still stored. A game already archived and linked is skipped, and a game
the model turned down, or one that competes for a map another game also claims, is
remembered in Redis for 13 hours and not asked about again.

## Implementation

### Job Functions (`app/cron/jobs.py`, `app/cron/legacy_fcm.py`, `app/cron/live_push.py`, and `app/cron/team_rankings.py`)

Each job function:
1. Takes a `ctx` dict (Redis connection, etc.)
2. Performs scraping/parsing
3. Updates Redis cache with fresh data
4. Logs execution

Example:
```python
async def rankings_cron(ctx: dict) -> None:
    get_current_scope().set_transaction_name("Rankings Cron")
    result = await rankings.ranking_list()
    await ctx["redis"].set("rankings", json.dumps([...]), ex=3600)
```

### Worker Setup

`app/cron/worker.py` schedules the jobs:

```python
cron_jobs = [
    cron("app.cron.jobs.rankings_cron", hour=None, minute={0, 30}),
    # ... other jobs
]

worker = create_worker({"cron_jobs": cron_jobs})
```

## Configuration

Jobs are configured in `ArqWorker.start()` method. Requires Redis connection.

## Monitoring

- **Logs**: Each job logs start/completion
- **Redis**: Job status visible in Redis
- **Metrics**: Job execution times and success rates

## Error Handling

- **Retries**: arq handles job retries on failure
- **Logging**: Failed jobs logged with stack traces
- **Isolation**: Job failures don't affect main app

## Development

To run jobs locally:
```bash
# Jobs run automatically with the FastAPI server
uv run fastapi dev
```

To test jobs manually:
```python
from app.cron.jobs import rankings_cron

await rankings_cron({"redis": redis_client})
```
