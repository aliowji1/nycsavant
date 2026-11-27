# NYC Savant

Stat lines for every NYC bus and every scheduled subway trip, plus an interactive look at how the subway actually runs.
A New York take on [MBTA Savant](https://mbtasavant.com) and [Visualizing MBTA Data](https://mbtaviz.github.io).

**Pages**

| Page | What it shows | Inspired by |
|---|---|---|
| Savant: Buses | Leaderboard + Savant-style percentile card for every bus by fleet number: trips, on-schedule rate, time saved per trip, TAR, rank on its route | MBTA Savant |
| Savant: Subway trips | Every row of the timetable (e.g. "Weekday 8:14a Q from 96 St") ranked the same way | MBTA Savant (adapted, see below) |
| Savant: Lines | Each line's on-schedule rate, delay distribution, completion, TAR/day, daily trend | |
| Trains | Marey diagram per line and day with a live position strip; aligned trips; trips per day | mbtaviz §1 |
| Your commute | Pick two stations: trip time and platform gap across the day, middle-80% bands and medians | mbtaviz §4 |
| Delays | A week of hourly entries above per-line speed-vs-normal strips, linked to a segment map; worst segments by minutes lost | mbtaviz §3 |
| Riders | Hourly entries heatmap; station table + map; home/work/all-day stations; per-station entry/exit profiles | mbtaviz §2 |
| Method | Definitions, sources, limits | |

## Why the subway is ranked by trip, not by car

MBTA Savant ranks individual train cars because the MBTA's feed says which cars are on each train. The MTA's NYC subway
feed does not: the vehicle descriptor is unused, and the internal train ID changes whenever a train is re-routed.
Buses are fine: BusTime reports fleet numbers. So buses get per-vehicle stat lines and the subway gets per-scheduled-trip
and per-line stat lines. If the MTA ever publishes consists, add the car ID as the `entity` in `metrics.stat_lines`.

## How it works

```
 MTA GTFS-rt (8 subway feeds) ─┐
 BusTime GTFS-rt (vehicles)  ──┼─> collector (every 30 s) ─> data/raw/<date>/<feed>/*.pb.gz
                               │
 MTA static GTFS (weekly) ─────┼─> gtfs_static ─> schedule, station orders, shapes
                               │
 nightly ──────────────────────┴─> reconstruct ─> data/work/arrivals/*.parquet   (actual arrival per trip+stop)
                                   metrics     ─> trips, stat lines, segments
 NY Open Data (ridership) ───────> ridership   ─> hourly entries, exit profiles
                                   export      ─> site/data/*.json ─> static D3 site (site/)
```

* `savant/collector.py` polls every feed concurrently, skips duplicate snapshots, and writes gzipped protobuf.
* `savant/reconstruct.py` keeps the last prediction for each (trip, stop) before the stop drops off the train's list and
  discards stops that vanished while still more than 5 minutes out. Trips are matched to the timetable by
  `origin-time_route..direction+shape` (falling back to the short form without the shape suffix), using the
  **supplemented** schedule (planned work included) fetched on or before that day; without it about half of weekend
  trips don't match. Stops reported out of scheduled order or at impossible speed (over 100 km/h) are dropped; a bad
  terminal time is replaced by the previous stop plus scheduled running time and flagged `estimated`.
* Buses: BusTime never reports "stopped at", so a stop's passage time comes from GPS (first snapshot within 40 m,
  100 m for the first and last stop), else the midpoint between snapshots. A trip counts as complete when seen from
  within its first three stops to the terminal.
* `savant/metrics.py` holds every definition (on-schedule, time saved, replacement level, TAR). Thresholds are in `savant/config.py`.
* The site is plain HTML + D3 with no build step. Marey data is one JSON file per day, loaded on demand.

## Running it for real

You need an always-on machine: a $5/month VPS, a Raspberry Pi, or any box that stays up. Plan on roughly
0.45 GB/day of raw snapshots (measured on a real Sunday: about 0.27 GB subway, 0.17 GB citywide buses); `nightly` prunes raw days older than 3 days once
they're reconstructed, so steady-state disk use is about 2 to 3 GB plus about 6 MB/day of reconstructed arrivals.

**Quickest:** on a fresh Ubuntu 24.04 server, as root, run `ops/setup_server.sh` (it installs everything, asks for
your BusTime key, starts the collector as a service and schedules the nightly job):

```bash
curl -fsSL https://raw.githubusercontent.com/aliowji1/nycsavant/main/ops/setup_server.sh -o setup.sh && bash setup.sh
```

Or by hand:

```bash
git clone <this repo> /opt/nycsavant && cd /opt/nycsavant
pip install -r requirements.txt
cp .env.example .env            # add BUSTIME_KEY (free: https://register.developer.obanyc.com/)
python -m savant static         # download static GTFS for subway + all bus boroughs, build tables
python -m savant collect        # leave this running (or use the systemd unit / Docker below)
```

Then every night (see `ops/crontab`):

```bash
python -m savant nightly        # reconstruct yesterday, refresh ridership, export site/data, prune raw
./ops/deploy.sh                 # publish site/ (GitHub Pages, Cloudflare Pages, or rsync)
```

**Publishing to GitHub Pages.** Set `DEPLOY_TARGET=gh-pages` in `.env` and give the server a deploy key
(`ssh-keygen -t ed25519 -N "" -f ~/.ssh/nycsavant_deploy` as the `savant` user, then add the `.pub` file under the
repo's Settings → Deploy keys with write access). `ops/deploy.sh` keeps a clone of the `gh-pages` branch in
`build/gh-pages` and pushes only changed files, so a typical night uploads a few MB (well inside the e2-micro's
1 GB/month of free outbound data). On the 1st of each month it squashes the branch to a single commit. Turn on
Settings → Pages → Deploy from branch → `gh-pages` / root.

* systemd: `ops/savant-collector.service`
* Docker: `docker build -t nycsavant .` then `docker run -d --restart=always -v $PWD/data:/app/data --env-file .env nycsavant collect`
* Preview: `make serve` and open http://localhost:8000

**Memory.** The pipeline is built to fit a 1 GB server (e.g. Google Cloud's free e2-micro) with 2 GB of swap,
which `ops/setup_server.sh` adds. The nightly job reads one day of raw data at a time: buses are processed as
each trip ends, schedules are read for that day's services only with IDs dictionary-encoded, and every day is
condensed once into small per-trip and per-segment summaries (`data/work/trips/`, `data/work/segments/`). The site
export reads only those summaries, so it doesn't grow with the 28-day window. Measured peaks: ~0.6 GB for
`reconstruct` (bus timetable for a weekday is ~5 million stop rows) and ~0.65 GB for `export`.

Rankings need volume. A bus needs 20 complete trips to qualify (about a week on a busy route); a scheduled subway trip
needs 5 runs. Leaderboards get meaningful after 2 to 4 weeks. `export` uses a rolling 28-day window (`--days`).

GitHub Actions is fine for the nightly build and deploy, but not for the collector: it can't poll every 30 seconds
around the clock. If you want zero servers, an alternative is a Cloudflare Worker on a 1-minute cron writing
snapshots to R2, with the nightly job pulling them down. 1-minute polling roughly doubles reconstruction error (to ~30 s).

## Demo mode (no live data)

`savant/sim.py` takes the real static schedule and invents what "actually happened": running-time noise, rush-hour
crowding, incidents that hold every train through a station for a while, cancellations, per-bus quality and breakdowns,
and imperfect predictions. It writes snapshots in the collector's format, so the real pipeline runs unchanged.
It also writes the invented truth to `data/work/truth/`, which is how reconstruction accuracy was checked:
on a simulated weekday, 95% of reconstructed subway arrivals were within 8 seconds of the truth and 99.9% of stop events were recovered; bus stop passages were within 19 seconds 95% of the time.

```bash
python -m savant static          # or tools/json_to_gtfs.py if the MTA is unreachable
make demo                        # 14 simulated days -> reconstruct -> export --simulated
make serve
```

Simulated builds show a yellow banner on every page. Nothing in them describes real trains or buses.

## Data sources and terms

* MTA realtime and static GTFS: https://www.mta.info/developers (use is subject to the MTA's developer terms)
* MTA BusTime: https://bustime.mta.info/wiki/Developers/Index
* Hourly subway ridership: https://data.ny.gov/d/5wq4-mkjj
* Origin-destination estimates (for exits): https://data.ny.gov/d/y2qv-fytt (swap the ID in `config.py` when a newer year is published)

## Layout

```
savant/        python package: config, collector, gtfs_static, reconstruct, metrics, ridership, export, sim, __main__
site/          static site (index.html, style.css, js/, vendor/d3.min.js); site/data/ is generated
ops/           systemd unit, crontab, deploy script
tools/         simulate.py (parallel demo days), json_to_gtfs.py (sandbox static loader)
```
