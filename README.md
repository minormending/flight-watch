# flight-watch

Watches Google Flights for cheap **flexible-date** trips and pushes an alert when
the price is unusually low *for this route*, judged against its own history.

Built for the case where you know how long you want to be somewhere but not when:
"4 nights in San Juan for 2 adults and a child, sometime in the next six
months, tell me when it's cheap."

- **Source:** [`fast-flights`](https://github.com/AWeirdDev/flights) — free, no API
  key. It rebuilds the base64-protobuf `tfs` parameter Google Flights uses
  internally, so one call is one real Google search.
- **Cadence:** every 10 hours via `launchd`.
- **Alerting:** percentile against trailing history, not a fixed threshold.
- **Dashboard:** static page in `docs/`, publishable with GitHub Pages.

## How it decides something is cheap

A fixed "alert me under $300" is wrong on day one and wronger later — you can't
know a fare is good without having watched it. So instead:

1. Each scan sweeps every departure date in the window (default 21–180 days out),
   pairing each with its return date `stay_nights` later, and records the
   cheapest round trip for each pair. Prices are the **total for the whole
   party**, not per person.
2. The best price in the scan is compared against the **20th percentile of every
   price seen on this route in the last 30 days**.
3. It fires only if it's at or below that threshold, below your optional hard
   ceiling, and not inside the cooldown from the last alert.

The pool is pooled across date pairs — with no fixed dates, every 4-night trip
is interchangeable — but never across **search signatures**. The signature covers
the route, stay length, party, cabin and bags, because a 2-adult premium-economy
fare and a 1-adult economy fare are different products whose prices must not
share a baseline.

Change `FW_ADULTS`, `FW_SEAT_CLASS`, `FW_CARRY_ON_BAGS` or `FW_STAY_NIGHTS` and
you start a clean history automatically: earlier observations stay in the
database but no longer match, so they drop out of the baseline rather than
corrupting it. Expect the warmup gate to re-arm after such a change.

Two gates stop it being annoying:

- **Warmup** — no alerts until there are 300 observations spanning 3 days.
  Without this, day one alerts on everything, because every price is its own
  percentile.
- **Cooldown** — 20 hours of silence after an alert, unless the price drops
  another 10%, so one volatile date pair can't spam you.

### Why 10 hours

10 doesn't divide 24, so the scan time precesses through the clock:
00:00 → 10:00 → 20:00 → 06:00 → 16:00 → 02:00. Every part of the day gets
sampled within about three days. A 12- or 24-hour cadence would only ever show
you the same point in the daily price cycle.

At this cadence a full 160-date sweep is roughly 380 requests/day, which measured
comfortable (~1.1s/request, no rate limiting) without any request-budget tricks.

## Setup

```bash
cd flight-watch
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
```

Then edit `.env`. The only thing you must set is a notification channel:

```bash
FW_NTFY_TOPIC=flightwatch-sju-<something-unguessable>
```

Install [ntfy](https://ntfy.sh) on your phone and subscribe to that exact topic.
There's no account — **the topic name is the only secret**, so make it long and
random. Anyone who guesses it can read your alerts.

Check it works:

```bash
.venv/bin/python -m flight_watch.main notify-test
```

Gmail is supported as a second channel (`GMAIL_USERNAME`, `GMAIL_APP_PASSWORD`,
`ALERT_TO_EMAIL`) if you'd rather have email too.

## Usage

```bash
# A quick 5-date smoke test that never notifies
.venv/bin/python -m flight_watch.main scan --limit 5 --dry-run

# A real sweep (~8 minutes for 160 dates), refreshing the dashboard
.venv/bin/python -m flight_watch.main scan --export docs

# What do we know so far?
.venv/bin/python -m flight_watch.main report

# Rebuild docs/data.json from the database
.venv/bin/python -m flight_watch.main export --out docs
```

Data lives in `~/.flight_watch/flight_watch.db` (SQLite), logs alongside it.
Nothing is stored in the repo except the dashboard snapshot.

## Running it every 10 hours

```bash
cp scripts/com.kevinramdath.flightwatch.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.kevinramdath.flightwatch.plist
```

Check on it with `launchctl list | grep flightwatch`, and unload with
`launchctl unload ~/Library/LaunchAgents/com.kevinramdath.flightwatch.plist`.

Run it on this Mac rather than GitHub Actions: datacenter IPs get blocked by
Google far more aggressively than a residential one.

## Dashboard

`docs/` is a static page that reads `docs/data.json` — no build step. Preview it
locally:

```bash
python3 -m http.server 8765 --directory docs
```

To publish: enable GitHub Pages on the repo (Settings → Pages → branch `main`,
folder `/docs`), then commit `docs/data.json` after a scan. Setting
`FW_AUTO_PUBLISH=1` in the plist makes `run-scan.sh` commit and push it on every
scan; it's off by default so you opt into publishing deliberately.

## Tuning

| Variable | Default | What it does |
|---|---|---|
| `FW_STAY_NIGHTS` | `4` | Nights at the destination |
| `FW_ADULTS` / `FW_CHILDREN` | `2` / `1` | Who's flying |
| `FW_SEAT_CLASS` | `economy` | `economy`, `premium-economy`, `business`, `first` |
| `FW_EXCLUDE_BASIC_ECONOMY` | `true` | Drop Basic Economy — no carry-on, no seat choice, no changes |
| `FW_CARRY_ON_BAGS` | `1` | Carry-ons **per passenger** whose fees are priced in |
| `FW_CHECKED_BAGS` | `0` | Checked bags per passenger |
| `FW_WINDOW_START_DAYS` / `FW_WINDOW_END_DAYS` | `21` / `180` | How far ahead to look |
| `FW_ALERT_PERCENTILE` | `20` | Lower = pickier. `10` is roughly "only real deals" |
| `FW_BASELINE_DAYS` | `30` | Trailing window the percentile is computed over |
| `FW_PRICE_CEILING` | unset | Never alert above this, however good the percentile |
| `FW_COOLDOWN_HOURS` | `20` | Silence after an alert |
| `FW_REQUEST_DELAY` | `2.5` | Seconds between requests (jittered ±50%) |

`FW_ORIGIN=NYC` is a metro code covering JFK, LGA and EWR in a single query.

## Known limits

- **Southwest never appears in Google Flights.** It serves SJU. Check it
  separately before booking.
- **Bag counts are per passenger and behave as a price adjustment, not a
  filter.** Google adds estimated fees for that many bags each; setting more
  than 1 carry-on changes nothing. Measured on this route: 1 adult economy
  $291 → $307 with a carry-on; 2 adults + 1 child $871 → $922. Premium economy
  is unaffected, since a carry-on is already in the fare.
- **Premium economy has far thinner coverage** — roughly 4 priced itineraries
  per date versus 10 in economy, and some dates may return nothing at all.
  A higher `failed` count in the scan log is expected, not necessarily a fault.
- **Prices are indicative.** They're what Google showed at scan time; fares can
  vanish between the alert and your click.
- **Google returns two result lists and the library only reads one.**
  `payload[2][0]` is "Top departing flights" — where the cheapest fares live —
  and `payload[3][0]` is "Other departing flights". `fast_flights.get_flights()`
  parses only the second, so it reported $1,211 on NYC→SJU when $1,001 was on
  the page. `GoogleFlightsSource._fetch_all` splices both lists into the slot
  the library's parser reads. **Do not replace it with `get_flights()`** — the
  scan would silently start missing every cheap fare. Covered by
  `test_both_result_lists_are_parsed`.
- **Premium economy parses badly on this route.** The scraper returned prices
  Google's own UI contradicted (a claimed $1,319 against a stated cheapest of
  $1,624). Economy, with or without Basic, matches the UI exactly. If you
  switch cabins, re-verify against a `tfs` link before trusting the numbers.
- **This is a scraper.** Google can change the response shape at any time and
  the scan will start returning nothing. `scan` exits non-zero and logs
  `Scan produced no quotes at all` when that happens — worth noticing.
- **`fast-flights` 3.1.0 doesn't declare its `typing_extensions` dependency**, so
  it's pinned explicitly in `pyproject.toml`. Don't remove it.
- For a round trip, Google prices *outbound* options at the full round-trip
  total, so the recorded price is the whole trip. The airline and duration
  recorded are the outbound leg's.
- **Alert links are protobuf `tfs` deep links**, built from the same query used
  to fetch the price. Do not "simplify" them to a `?q=Flights from...` URL:
  Google silently ignores that form and lands on a blank 1-adult economy
  search, so the link would disagree with the price in the alert.

If the scraper becomes unreliable, `src/flight_watch/sources/` has a
`PriceSource` protocol and a stubbed SerpApi backend. Note that SerpApi's Google
Flights *Deals* endpoint takes a date range plus a `trip_length`, so it can
replace this entire per-date loop with one request — worth restructuring the
scan loop for rather than porting it date-by-date.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

Covers the percentile maths and the alert gates (warmup, ceiling, cooldown,
re-arm), which is where the logic actually lives.
