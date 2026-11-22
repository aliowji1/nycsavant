"""Feed simulator, for testing the pipeline without live MTA access.

Takes the real static schedule, invents what "actually happened" on a given
service day (running-time noise, rush-hour dwell, incidents that hold every
train through a station, cancellations, bus traffic), and writes GTFS-realtime
snapshots in the same layout the collector produces. Predictions in the
snapshots are deliberately imperfect, so reconstruction is tested honestly.

Also writes `truth_*.parquet` (the invented actual times) so reconstruction
accuracy can be measured, and a synthetic hourly ridership table.

Nothing produced here is real data.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import math
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from google.transit import gtfs_realtime_pb2 as rt

from . import config, gtfs_static

NY = ZoneInfo(config.TZ)

FEED_OF_ROUTE = {}
for feed, routes in {"ace": "A C E H FS", "bdfm": "B D F FX M", "g": "G", "jz": "J Z",
                     "nqrw": "N Q R W", "l": "L", "1234567": "1 2 3 4 5 6 6X 7 7X GS", "si": "SI"}.items():
    for r in routes.split():
        FEED_OF_ROUTE[r] = feed

# Relative unreliability by route (purely invented for the simulation).
ROUTE_NOISE = {"A": 1.5, "F": 1.4, "2": 1.35, "5": 1.3, "E": 1.25, "N": 1.2, "D": 1.2, "R": 1.15,
               "4": 1.2, "C": 1.1, "J": 1.05, "Z": 1.05, "M": 1.1, "B": 1.1, "Q": 1.0, "3": 1.0,
               "6": 1.0, "1": 0.95, "G": 0.95, "W": 1.0, "7": 0.7, "L": 0.65, "SI": 0.8, "GS": 0.6}


def service_midnight(date: dt.date) -> int:
    """Unix time of local noon minus 12h: GTFS's definition of service-day 'midnight'."""
    noon = dt.datetime(date.year, date.month, date.day, 12, tzinfo=NY)
    return int(noon.timestamp()) - 12 * 3600


def rush(sec: np.ndarray) -> np.ndarray:
    h = (sec / 3600.0) % 24
    am = np.exp(-((h - 8.2) / 1.0) ** 2)
    pm = np.exp(-((h - 17.6) / 1.2) ** 2)
    return am + pm


# ======================================================================= subway
def simulate_subway_truth(date: dt.date, rng: np.random.Generator, weekday: bool) -> pd.DataFrame:
    sdir = config.STATIC / "subway"
    services = gtfs_static.active_services(sdir, date)
    sched = pd.read_parquet(config.WORK / "static" / "subway_schedule.parquet")
    sched = sched[sched.service_id.isin(services)].sort_values(["trip_id", "seq"])

    # incidents: (station, direction, start, end, severity)
    stations = sched.station.unique()
    n_inc = rng.poisson(9 if weekday else 5)
    incidents = []
    for _ in range(n_inc):
        st = rng.choice(stations)
        start = rng.uniform(5.5, 23) * 3600
        dur = rng.gamma(2.0, 9) * 60
        incidents.append((st, str(rng.integers(0, 2)), start, start + dur, rng.uniform(0.3, 0.9)))
    inc_df = pd.DataFrame(incidents, columns=["station", "dir", "t0", "t1", "sev"])

    rows = []
    for tid, g in sched.groupby("trip_id", sort=False):
        route = g.route_id.iat[0]
        if rng.random() < 0.015:          # cancelled: never appears in the feed
            continue
        noise = ROUTE_NOISE.get(route, 1.0)
        arr_s, dep_s = g.arr.to_numpy(float), g.dep.to_numpy(float)
        n = len(g)
        run = np.diff(arr_s, prepend=arr_s[0])
        r = rush(arr_s) * (1.0 if weekday else 0.3)
        # scheduled run times already include normal dwell. Trains run a little
        # faster than the padded schedule off-peak and lose time in the rush.
        trip_f = rng.normal(1.0, 0.025 * noise)
        factor = (0.965 + 0.07 * r + rng.normal(0, 0.05 * noise, n)) * trip_f
        dwell = 15 * r * rng.gamma(2, 0.5, n) * noise      # crowding: extra dwell only
        trip_bias = rng.normal(0, 25 * noise)
        inc_stations = set(inc_df.station)
        actual_arr = np.empty(n)
        actual_dep = np.empty(n)
        inc_hits = []                     # (stop idx, time the hit occurred, seconds)
        t = dep_s[0] + max(-30, rng.normal(30, 40 * noise)) + trip_bias
        stations_arr = g.station.to_numpy()
        dirc = g.direction_id.iat[0]
        for i in range(n):
            if i > 0:
                t = actual_dep[i - 1] + max(30, run[i] * factor[i])
            actual_arr[i] = t
            hold = 0.0
            if stations_arr[i] in inc_stations:
                m = (inc_df.station.values == stations_arr[i]) & (inc_df.dir.values == dirc) & \
                    (inc_df.t0.values <= t) & (inc_df.t1.values > t)
                if m.any():
                    k = np.flatnonzero(m)[0]
                    hold = (inc_df.t1.values[k] - t) * inc_df.sev.values[k] + rng.uniform(30, 90)
                    inc_hits.append((i, t, hold))
            d = 0 if i == n - 1 else dwell[i]
            actual_dep[i] = t + d + hold
        rows.append(pd.DataFrame({
            "trip_id": tid, "trip_key": g.trip_key.iat[0], "route_id": route, "direction_id": dirc,
            "station": stations_arr, "seq": g.seq.to_numpy(), "sched_arr": arr_s,
            "arr": actual_arr.round(), "dep": actual_dep.round(),
            "inc_delay": _cum_hits(n, inc_hits), "inc_time": _hit_times(n, inc_hits)}))
    return pd.concat(rows, ignore_index=True)


def _cum_hits(n, hits):
    out = np.zeros(n)
    for i, _, d in hits:
        out[i + 1:] += d                  # hold at stop i delays arrival at later stops
    return out


def _hit_times(n, hits):
    """For each stop, the time of the latest incident hit that delays it (0 if none)."""
    out = np.zeros(n)
    for i, t, _ in hits:
        out[i + 1:] = np.maximum(out[i + 1:], t)
    return out


def write_subway_snapshots(truth: pd.DataFrame, date: dt.date, rng: np.random.Generator, interval: int):
    base = service_midnight(date)
    truth = truth.assign(z=rng.normal(0, 1, len(truth)))
    trips = []
    for tid, g in truth.groupby("trip_id", sort=False):
        key = g.trip_key.iat[0]
        if rng.random() < 0.3:            # many realtime ids omit the shape suffix
            key = gtfs_static.short_key(key)
        dirc = "N" if g.direction_id.iat[0] == "0" else "S"
        trips.append(dict(
            key=key, route=g.route_id.iat[0], feed=FEED_OF_ROUTE.get(g.route_id.iat[0], "1234567"),
            stop_ids=[s + dirc for s in g.station], arr=g.arr.to_numpy(), dep=g.dep.to_numpy(),
            inc=g.inc_delay.to_numpy(), inct=g.inc_time.to_numpy(), z=g.z.to_numpy(),
            start=g.dep.iat[0] - 600, end=g.arr.iat[-1] + 20))
    starts = np.array([t["start"] for t in trips])
    ends = np.array([t["end"] for t in trips])
    feeds = np.array([t["feed"] for t in trips])
    start_date = date.strftime("%Y%m%d")
    t = 0                                 # service day runs 00:00 -> 27:30
    while t < 27.5 * 3600:
        now = base + t
        active = np.flatnonzero((starts <= t) & (ends >= t))
        stamp = dt.datetime.fromtimestamp(now, NY)
        for feed in set(feeds[active]):
            msg = rt.FeedMessage()
            msg.header.gtfs_realtime_version = "1.0"
            msg.header.timestamp = now
            for idx in active[feeds[active] == feed]:
                tr = trips[idx]
                remaining = np.flatnonzero(tr["dep"] >= t)
                if not len(remaining):
                    continue
                e = msg.entity.add()
                e.id = f"{idx}"
                tu = e.trip_update
                tu.trip.trip_id = tr["key"]
                tu.trip.route_id = tr["route"]
                tu.trip.start_date = start_date
                for j in remaining:
                    a = tr["arr"][j]
                    if a > t:
                        lead = a - t
                        unknown = tr["inc"][j] if tr["inct"][j] > t else 0.0
                        a = a - unknown + tr["z"][j] * (4 + 0.08 * lead)
                    stu = tu.stop_time_update.add()
                    stu.stop_id = tr["stop_ids"][j]
                    stu.arrival.time = int(base + a)
                    stu.departure.time = int(base + max(a, tr["dep"][j] - (tr["inc"][j] if tr["inct"][j] > t else 0)))
                v = msg.entity.add()
                v.id = f"{idx}v"
                v.vehicle.trip.CopyFrom(tu.trip)
                j0 = remaining[0]
                v.vehicle.stop_id = tr["stop_ids"][j0]
                v.vehicle.current_status = (rt.VehiclePosition.STOPPED_AT if tr["arr"][j0] <= t
                                            else rt.VehiclePosition.IN_TRANSIT_TO)
                v.vehicle.timestamp = now
            d = config.RAW / stamp.strftime("%Y-%m-%d") / feed
            d.mkdir(parents=True, exist_ok=True)
            with gzip.open(d / f"{stamp:%H%M%S}-s{date:%d}.pb.gz", "wb", compresslevel=6) as f:
                f.write(msg.SerializeToString())
        t += interval


# ======================================================================= bus
DEPOT_FLEETS = {  # invented fleet-number ranges per route group (sim only)
    "M15": range(5650, 5790), "M15+": range(5250, 5330), "M14A+": range(7680, 7760),
    "M14D+": range(7680, 7760), "M86+": range(5330, 5390), "M101": range(5790, 5900),
    "M4": range(4800, 4890), "M34+": range(5390, 5440), "M79+": range(4890, 4950), "M60+": range(6000, 6060),
}


def bus_fleet(rng_seed=7):
    rng = np.random.default_rng(rng_seed)
    nums = sorted({n for r in DEPOT_FLEETS.values() for n in r})
    # each physical bus gets a persistent reliability: >1 means more erratic running
    # and more breakdowns (think older buses, flaky doors, kneeling faults)
    return {n: float(rng.lognormal(0, 0.35)) for n in nums}


def simulate_bus_truth(date: dt.date, rng: np.random.Generator, weekday: bool) -> pd.DataFrame:
    bdir = config.STATIC / "bus"
    services = gtfs_static.active_services(bdir, date)
    sched = pd.read_parquet(config.WORK / "static" / "bus_schedule.parquet")
    sched = sched[sched.service_id.isin(services)].sort_values(["trip_id", "seq"])
    quality = bus_fleet()
    # assign a physical bus to each block for the day
    blocks = sched.groupby("block_id").route_id.first()
    assign = {}
    for blk, route in blocks.items():
        pool = list(DEPOT_FLEETS.get(route, range(9000, 9100)))
        assign[blk] = int(rng.choice(pool))
    weather = rng.normal(1.0, 0.04)       # day-level traffic factor
    rows = []
    for tid, g in sched.groupby("trip_id", sort=False):
        if rng.random() < 0.02:
            continue
        blk = g.block_id.iat[0]
        bus = assign.get(blk, 9999)
        q = quality.get(bus, 1.0)
        s = g.arr.to_numpy(float)
        n = len(s)
        run = np.diff(s, prepend=s[0])
        r = rush(s) * (1.0 if weekday else 0.35)
        trip_f = rng.normal(1.0 + 0.01 * (q - 1), 0.03 * q)
        factor = (0.97 + 0.14 * r + rng.normal(0, 0.08 * q, n)) * weather * trip_f
        breakdown = rng.random() < 0.004 * q ** 2
        t = s[0] + max(-60, rng.normal(60, 90))
        act = np.empty(n)
        for i in range(n):
            if i > 0:
                t = act[i - 1] + max(10, run[i] * factor[i])
                if breakdown and i == n // 2:
                    t += rng.uniform(600, 1500)
            act[i] = t
        rows.append(pd.DataFrame({"trip_id": tid, "route_id": g.route_id.iat[0],
                                  "direction_id": g.direction_id.iat[0], "block_id": blk,
                                  "vehicle": f"MTA NYCT_{bus}", "stop_id": g.stop_id.to_numpy(),
                                  "seq": g.seq.to_numpy(), "sched_arr": s, "arr": act.round()}))
    return pd.concat(rows, ignore_index=True)


def write_bus_snapshots(truth: pd.DataFrame, date: dt.date, interval: int):
    base = service_midnight(date)
    stops = pd.read_parquet(config.WORK / "static" / "bus_stops.parquet").set_index("stop_id")
    trips = []
    for tid, g in truth.groupby("trip_id", sort=False):
        ll = stops.loc[g.stop_id, ["lat", "lon"]].to_numpy()
        trips.append(dict(tid=tid, route=g.route_id.iat[0], veh=g.vehicle.iat[0],
                          stop_ids=g.stop_id.to_numpy(), arr=g.arr.to_numpy(), ll=ll,
                          start=g.arr.iat[0] - 120, end=g.arr.iat[-1] + 10))
    starts = np.array([t["start"] for t in trips])
    ends = np.array([t["end"] for t in trips])
    start_date = date.strftime("%Y%m%d")
    t = 0
    while t < 27.5 * 3600:
        now = base + t
        stamp = dt.datetime.fromtimestamp(now, NY)
        msg = rt.FeedMessage()
        msg.header.gtfs_realtime_version = "1.0"
        msg.header.timestamp = now
        seen = set()
        for idx in np.flatnonzero((starts <= t) & (ends >= t)):
            tr = trips[idx]
            if tr["veh"] in seen:        # a bus can only be on one trip
                continue
            seen.add(tr["veh"])
            j = int(np.searchsorted(tr["arr"], t))
            j = min(j, len(tr["arr"]) - 1)
            if j == 0:
                lat, lon = tr["ll"][0]
            else:
                a0, a1 = tr["arr"][j - 1], tr["arr"][j]
                f = 0 if a1 == a0 else min(1, max(0, (t - a0) / (a1 - a0)))
                lat, lon = tr["ll"][j - 1] + f * (tr["ll"][j] - tr["ll"][j - 1])
            e = msg.entity.add()
            e.id = tr["veh"]
            v = e.vehicle
            v.trip.trip_id = tr["tid"]
            v.trip.route_id = tr["route"]
            v.trip.start_date = start_date
            v.vehicle.id = tr["veh"]
            v.position.latitude, v.position.longitude = float(lat), float(lon)
            v.stop_id = tr["stop_ids"][j]
            # like real BusTime: no current_status, just position and next stop
            v.timestamp = now
        d = config.RAW / stamp.strftime("%Y-%m-%d") / "bus_vehicles"
        d.mkdir(parents=True, exist_ok=True)
        with gzip.open(d / f"{stamp:%H%M%S}-s{date:%d}.pb.gz", "wb", compresslevel=6) as f:
            f.write(msg.SerializeToString())
        t += interval


# ======================================================================= ridership
def simulate_ridership(dates: list[dt.date], seed: int = 3):
    """Synthetic hourly entries per station complex, shaped like the real
    data.ny.gov table (station_complex_id, station_complex, lat, lon, hour, ridership)."""
    rng = np.random.default_rng(seed)
    st = pd.read_parquet(config.WORK / "static" / "subway_stations.parquet")
    # crude 'complex' = parent station
    cbd = (st.lat < 40.765) & (st.lat > 40.70) & (st.lon > -74.02) & (st.lon < -73.97)
    nr = st.routes.str.len()
    size = (1500 + 2600 * nr ** 1.4) * np.where(cbd, 3.0, 1.0) * rng.lognormal(0, 0.35, len(st))
    kind = np.where(cbd, "work", np.where(rng.random(len(st)) < 0.15, "mixed", "home"))
    h = np.arange(24)
    prof = {
        "home": np.exp(-((h - 7.8) / 1.3) ** 2) * 1.0 + np.exp(-((h - 17.5) / 2.5) ** 2) * 0.35 + 0.05,
        "work": np.exp(-((h - 8.3) / 1.0) ** 2) * 0.3 + np.exp(-((h - 17.4) / 1.4) ** 2) * 1.0 + 0.06,
        "mixed": np.exp(-((h - 8) / 1.5) ** 2) * 0.6 + np.exp(-((h - 17.5) / 2) ** 2) * 0.7 + 0.12,
    }
    weekend = (np.exp(-((h - 14.5) / 4.5) ** 2) * 0.5 + 0.04) * 0.6
    night = (h < 5)
    rows = []
    mid = [d for d in dates[len(dates) // 2:] if d.weekday() < 5]
    storm = mid[0] if mid else None        # a rainy weekday: fewer riders
    for d in dates:
        we = d.weekday() >= 5
        day_f = 0.55 if d == storm else 1.0
        for i, s in st.iterrows():
            p = weekend if we else prof[kind[i]]
            p = np.where(night, p * 0.4, p)
            vals = size[i] * p * day_f * rng.lognormal(0, 0.12, 24) / 6.5
            for hh in range(24):
                rows.append((s.station, s["name"], s.lat, s.lon, f"{d}T{hh:02d}:00:00", round(vals[hh])))
    out = pd.DataFrame(rows, columns=["station_complex_id", "station_complex", "latitude", "longitude",
                                      "transit_timestamp", "ridership"])
    # OD-style exit profile: invert the entry profile (people return home)
    ex = []
    for i, s in st.iterrows():
        k = kind[i]
        inv = {"home": "work", "work": "home", "mixed": "mixed"}[k]
        for hh in range(24):
            ex.append((s.station, "weekday", hh, round(size[i] * prof[inv][hh] / 6.5)))
            ex.append((s.station, "weekend", hh, round(size[i] * weekend[hh] / 6.5)))
    (config.WORK / "ridership").mkdir(parents=True, exist_ok=True)
    out.to_parquet(config.WORK / "ridership" / "hourly_entries.parquet", index=False)
    pd.DataFrame(ex, columns=["station_complex_id", "daytype", "hour", "exits"]).to_parquet(
        config.WORK / "ridership" / "exit_profile.parquet", index=False)
    json.dump({"simulated": True, "storm_day": str(storm)}, open(config.WORK / "ridership" / "meta.json", "w"))


# ======================================================================= driver
def run(start: dt.date, days: int, interval: int = 30, seed: int = 1):
    (config.WORK / "truth").mkdir(parents=True, exist_ok=True)
    for k in range(days):
        date = start + dt.timedelta(days=k)
        rng = np.random.default_rng(seed + k)
        weekday = date.weekday() < 5 and date != dt.date(2026, 11, 26)
        truth = simulate_subway_truth(date, rng, weekday)
        truth.to_parquet(config.WORK / "truth" / f"subway_{date}.parquet", index=False)
        write_subway_snapshots(truth, date, rng, interval)
        if (config.WORK / "static" / "bus_schedule.parquet").exists():
            bt = simulate_bus_truth(date, rng, weekday)
            bt.to_parquet(config.WORK / "truth" / f"bus_{date}.parquet", index=False)
            write_bus_snapshots(bt, date, interval)
        print(f"simulated {date}: {truth.trip_id.nunique()} subway trips")
