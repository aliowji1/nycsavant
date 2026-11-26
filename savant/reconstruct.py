"""Turn archived realtime snapshots into what actually happened.

Subway: NYCT publishes predictions, not arrivals. For each (trip, stop) we keep
the last prediction seen before that stop drops out of the trip's updates;
once a train is at a platform the feed reports its real arrival, so the last
value is close to the truth. Stops that vanish while still far in the future
(re-routes, skipped stops, trips that disappear) are discarded.

Buses: BusTime reports each bus's position and next stop with its fleet
number (never STOPPED_AT). A stop's passage time comes from GPS distance to
the stop, or the midpoint between the snapshots before and after it.
"""
from __future__ import annotations

import datetime as dt
import gzip
import math
from pathlib import Path

import numpy as np
import pandas as pd
from google.transit import gtfs_realtime_pb2 as rt

from . import config, gtfs_static
from .sim import service_midnight

SUBWAY_FEEDS = list(config.SUBWAY_FEEDS)
DROP_IF_AHEAD = 300   # seconds: stop vanished while its prediction was this far out


def snapshot_files(date: dt.date, feeds: list[str]) -> list[tuple[int, Path]]:
    """All snapshots that may contain trips for service day `date`
    (all of `date` through 06:00 the next calendar day; trips are then
    filtered on their start_date)."""
    nxt = date + dt.timedelta(days=1)
    out = []
    for day, lo, hi in ((date, "000000", "999999"), (nxt, "000000", "060000")):
        for feed in feeds:
            d = config.RAW / str(day) / feed
            if not d.exists():
                continue
            for p in d.glob("*.pb.gz"):
                hhmmss = p.name[:6]
                if lo <= hhmmss < hi:
                    out.append((int(f"{day:%Y%m%d}{hhmmss}"), p))
    out.sort()
    return out


def read_feed(path: Path) -> rt.FeedMessage:
    msg = rt.FeedMessage()
    with gzip.open(path, "rb") as f:
        msg.ParseFromString(f.read())
    return msg


# ======================================================================= subway
def reconstruct_subway(date: dt.date) -> pd.DataFrame:
    sd = date.strftime("%Y%m%d")
    base = service_midnight(date)
    last: dict[tuple[str, str], list] = {}      # (trip, stop) -> [arr, dep, seen_at, first_seen]
    trip_route: dict[str, str] = {}
    for _, path in snapshot_files(date, SUBWAY_FEEDS):
        msg = read_feed(path)
        now = msg.header.timestamp
        for e in msg.entity:
            if not e.HasField("trip_update"):
                continue
            tu = e.trip_update
            if tu.trip.start_date and tu.trip.start_date != sd:
                continue
            tid = tu.trip.trip_id
            trip_route[tid] = tu.trip.route_id
            for stu in tu.stop_time_update:
                a = stu.arrival.time or stu.departure.time
                d = stu.departure.time or a
                if not a:
                    continue
                k = (tid, stu.stop_id)
                v = last.get(k)
                if v is None:
                    last[k] = [a, d, now, now]
                else:
                    v[0], v[1], v[2] = a, d, now
    if not last:
        return pd.DataFrame()
    keys = list(last.keys())
    vals = np.array(list(last.values()), dtype=np.int64)
    df = pd.DataFrame({"rt_trip_id": [k[0] for k in keys], "stop_id": [k[1] for k in keys],
                       "arr": vals[:, 0] - base, "dep": vals[:, 1] - base, "seen": vals[:, 2] - base})
    # keep only stops the train actually reached (prediction ~ when it vanished)
    df = df[df.arr - df.seen <= DROP_IF_AHEAD].copy()
    df["route_id"] = df.rt_trip_id.map(trip_route)
    df["station"] = df.stop_id.str[:-1].where(df.stop_id.str[-1].isin(["N", "S"]), df.stop_id)
    df["direction_id"] = np.where(df.stop_id.str.endswith("N"), "0", "1")
    df, sched = match_subway_schedule(df, date, return_sched=True)
    # terminal of each scheduled trip, for repairing bad terminal times
    term = sched.sort_values("seq").drop_duplicates("trip_id", keep="last").set_index("trip_id")
    _SCHED_CACHE["term"] = term[["station", "seq", "arr", "dep"]]
    del sched
    df = clean_subway(df.drop(columns=["seen"]))
    df["date"] = str(date)
    return df.sort_values(["rt_trip_id", "arr"])


def load_subway_schedule(date: dt.date, columns=None) -> pd.DataFrame:
    """Scheduled stops for one service day only (filtered while reading, so the
    rest of the timetable never takes memory)."""
    feed_dir, sched_path = gtfs_static.subway_schedule_for(date)
    services = sorted(gtfs_static.active_services(feed_dir, date))
    filters = [("service_id", "in", services)] if services else None
    columns = columns or ["trip_id", "trip_key", "short_key", "route_id", "service_id",
                          "direction_id", "station", "seq", "arr", "dep"]
    text = ("trip_id", "trip_key", "short_key", "route_id", "service_id", "direction_id", "station")
    df = gtfs_static.read_lean(sched_path, columns, filters, text_cols=text)
    for c in text:                                   # one day is small: plain text for joins
        if c in df.columns:
            df[c] = df[c].astype(str)
    return df


def match_subway_schedule(df: pd.DataFrame, date: dt.date, return_sched: bool = False):
    """Attach the scheduled trip, stop sequence and times. Uses the supplemented
    schedule fetched closest before the day, so planned work is matched too."""
    sched = load_subway_schedule(date, ["trip_id", "trip_key", "short_key", "service_id",
                                        "station", "seq", "arr", "dep"])
    full = sched.drop_duplicates("trip_key").set_index("trip_key").trip_id
    short = sched.drop_duplicates("short_key").set_index("short_key").trip_id
    ids = pd.Series(df.rt_trip_id.unique())
    m = ids.map(full)
    m = m.fillna(ids.map(gtfs_static.short_key).map(short))
    df = df.merge(pd.DataFrame({"rt_trip_id": ids, "trip_id": m}), on="rt_trip_id", how="left")
    s = sched[["trip_id", "station", "seq", "arr", "dep"]].rename(
        columns={"arr": "sched_arr", "dep": "sched_dep", "seq": "sched_seq"})
    s = s.drop_duplicates(["trip_id", "station"])
    out = df.merge(s, on=["trip_id", "station"], how="left")
    return (out, sched) if return_sched else out


MAX_KMH = 100          # faster than any NYC subway train between two stations


def _lis_keep(seq: np.ndarray) -> np.ndarray:
    """Boolean mask of the longest strictly increasing subsequence of seq."""
    n = len(seq)
    tails, tails_i, prev = [], [], np.full(n, -1)
    for i, v in enumerate(seq):
        j = int(np.searchsorted(tails, v, side="left"))
        if j == len(tails):
            tails.append(v); tails_i.append(i)
        else:
            tails[j] = v; tails_i[j] = i
        prev[i] = tails_i[j - 1] if j else -1
    keep = np.zeros(n, bool)
    i = tails_i[-1] if tails_i else -1
    while i >= 0:
        keep[i] = True
        i = prev[i]
    return keep


def clean_subway(df: pd.DataFrame) -> pd.DataFrame:
    """Drop stop times that can't be real, and repair a bad terminal time.

    Two failure modes seen in real NYCT data:
      * stops reported out of the scheduled order (stale predictions after a
        re-route): keep the longest run that follows the schedule's sequence;
      * stops a train 'reached' at impossible speed, typically the terminal,
        whose last prediction equals the previous stop's time when the trip
        drops out of the feed: drop them, and for the terminal estimate the
        arrival from the previous stop plus the scheduled running time
        (flagged in `estimated`).
    """
    st = pd.read_parquet(config.WORK / "static" / "subway_stations.parquet").set_index("station")
    df = df.join(st[["lat", "lon"]], on="station").sort_values(["rt_trip_id", "arr"])
    df["estimated"] = False
    out = []
    for _, g in df.groupby("rt_trip_id", sort=False):
        g = g.copy()
        matched = g.sched_seq.notna()
        if matched.sum() >= 2:
            k = np.ones(len(g), bool)
            idx = np.flatnonzero(matched.values)
            k[idx] = _lis_keep(g.sched_seq.values[idx])
            g = g[k]
        lat, lon, t = g.lat.values, g.lon.values, g.arr.values
        keep = np.ones(len(g), bool)
        last = 0
        for i in range(1, len(g)):
            km = math.hypot((lat[i] - lat[last]) * 111.0, (lon[i] - lon[last]) * 84.0) if lat[i] == lat[i] else 0
            dt_ = t[i] - t[last]
            if km > 0.05 and (dt_ <= 0 or km / dt_ * 3600 > MAX_KMH):
                keep[i] = False
            else:
                last = i
        dropped_last = not keep[-1]
        g = g[keep].copy()
        if dropped_last and len(g) and g.sched_arr.notna().iat[-1]:
            out.append(g)
            out.append(_estimate_terminal(g, df))
            continue
        out.append(g)
    res = pd.concat(out, ignore_index=True) if out else df
    return res.drop(columns=["lat", "lon"])


_SCHED_CACHE: dict = {}


def _estimate_terminal(g: pd.DataFrame, _df) -> pd.DataFrame:
    """Terminal arrival = last good stop + scheduled running time to the terminal."""
    term = _SCHED_CACHE.get("term")
    tid = g.trip_id.iat[-1]
    if term is None or tid not in term.index:
        return g.iloc[0:0]
    last = term.loc[tid]
    if last.station == g.station.iat[-1]:
        return g.iloc[0:0]
    prev_sched = g.sched_arr.iat[-1]
    row = g.iloc[[-1]].copy()
    row["station"] = last.station
    row["stop_id"] = last.station + ("N" if g.direction_id.iat[-1] == "0" else "S")
    row["sched_seq"], row["sched_arr"], row["sched_dep"] = last.seq, last.arr, last.dep
    row["arr"] = row["dep"] = g.arr.iat[-1] + (last.arr - prev_sched)
    row["estimated"] = True
    return row


# ======================================================================= bus
NEAR_STOP_M = 40       # a bus within this distance of a stop is "at" it
NEAR_END_M = 100       # looser for the first and last stop (layover areas)


def _load_bus_schedule(date: dt.date):
    """Compact arrays for the day's scheduled bus stops, sorted by trip then
    sequence, plus trip_id -> (start, end) row ranges. Only services running
    that day are read, and IDs stay as integer codes (the citywide timetable
    is ~7 million stop rows; as plain strings it would need ~3 GB)."""
    services = sorted(gtfs_static.active_services(config.STATIC / "bus", date))
    filters = [("service_id", "in", services)] if services else None
    sched = gtfs_static.read_lean(config.WORK / "static" / "bus_schedule.parquet",
                                  ["trip_id", "direction_id", "block_id", "stop_id", "seq", "arr"],
                                  filters, text_cols=("trip_id", "direction_id", "block_id", "stop_id"))
    trip_codes = sched.trip_id.cat.codes.to_numpy()
    order = np.lexsort((sched.seq.to_numpy(), trip_codes)).astype(np.int32)
    trip_codes = trip_codes[order]
    starts = np.flatnonzero(np.r_[True, trip_codes[1:] != trip_codes[:-1]]) if len(order) else np.array([], int)
    ends = np.r_[starts[1:], len(order)]
    trip_cats = sched.trip_id.cat.categories.to_numpy()
    ranges = {trip_cats[trip_codes[a]]: (int(a), int(b)) for a, b in zip(starts, ends)}
    stop_cats = sched.stop_id.cat.categories
    stops = (pd.read_parquet(config.WORK / "static" / "bus_stops.parquet")
             .drop_duplicates("stop_id").set_index("stop_id"))   # boroughs share some stops
    cat_ll = stops.reindex(stop_cats)[["lat", "lon"]].to_numpy(np.float32)
    stop_codes = sched.stop_id.cat.codes.to_numpy()[order]
    arrays = {
        "stop_codes": stop_codes, "stop_cats": stop_cats.to_numpy(),
        "seq": sched.seq.to_numpy(np.int32)[order], "arr": sched.arr.to_numpy(np.int32)[order],
        "dir": sched.direction_id.cat.codes.to_numpy()[order], "dir_cats": sched.direction_id.cat.categories,
        "block": sched.block_id.cat.codes.to_numpy()[order], "block_cats": sched.block_id.cat.categories,
        "lat": cat_ll[stop_codes, 0], "lon": cat_ll[stop_codes, 1],
    }
    del sched
    return ranges, arrays


def _bus_passages(ob, a, b, S, base):
    """Passage time at each scheduled stop of one (bus, trip), or None."""
    idx = {s: i for i, s in enumerate(S["stop_cats"][S["stop_codes"][a:b]])}
    ob.sort()
    t = np.array([o[0] for o in ob], dtype=float) - base
    pos = np.array([idx.get(o[1], -1) for o in ob])
    olat = np.array([o[2] for o in ob]); olon = np.array([o[3] for o in ob])
    keep = pos >= 0
    t, pos, olat, olon = t[keep], pos[keep], olat[keep], olon[keep]
    if len(t) < 3:
        return None
    pos = np.maximum.accumulate(pos)              # next-stop index never goes backwards
    n = b - a
    # distance (m) from every snapshot to every stop on the trip
    dist = np.hypot((olat[:, None] - S["lat"][None, a:b].astype(np.float64)) * 111_000,
                    (olon[:, None] - S["lon"][None, a:b].astype(np.float64)) * 84_000)
    passage = np.full(n, np.nan)
    for k in range(n):
        r = NEAR_END_M if k in (0, n - 1) else NEAR_STOP_M
        near = np.flatnonzero((dist[:, k] < r) & ((pos == k) | (pos == k + 1)))
        if len(near):
            passage[k] = t[near[-1]] if k == 0 else t[near[0]]
            continue
        if k == n - 1:
            continue                              # never seen at the terminal: no end time
        after = np.flatnonzero(pos > k)
        if len(after) and after[0] > 0:
            i = after[0]
            passage[k] = (t[i - 1] + t[i]) / 2
    return passage


_LAST_BUS_TRIPS: dict = {}


def _bus_trip_table(done, S, date) -> pd.DataFrame:
    """One row per (bus, trip), built from the schedule already in memory, so the
    summary step doesn't have to load the citywide timetable a second time."""
    rows = []
    for veh, tid, route, a, b, p in done:
        ok = np.flatnonzero(np.isfinite(p))
        f, l = ok[0], ok[-1]
        rows.append((veh, tid, route, S["dir_cats"][S["dir"][a]], len(ok), f, S["seq"][a + l],
                     p[f], p[l], S["arr"][a + f], S["arr"][a + l], S["seq"][b - 1], b - a, S["arr"][a], S["arr"][b - 1]))
    t = pd.DataFrame(rows, columns=["vehicle", "trip_id", "route_id", "direction_id", "n_obs", "first_idx", "max_seq",
                                    "act_start", "act_end", "sched_at_start", "sched_at_end", "last_seq", "n_sched",
                                    "sched_start", "sched_end"])
    t.insert(0, "date", str(date))
    from .metrics import finish_bus_trip_rows
    return finish_bus_trip_rows(t)


FINISHED_AFTER = 1800  # s: a (bus, trip) not seen for this long is done; process and free it


def reconstruct_bus(date: dt.date) -> pd.DataFrame:
    """BusTime reports position and next stop but never STOPPED_AT, so a stop's
    passage time comes from GPS: the first snapshot within NEAR_STOP_M of it
    (for the origin: the last snapshot near it, i.e. departure). Stops passed
    between two snapshots get the midpoint. The terminal needs a snapshot near
    it; otherwise the trip has no end time (it is not guessed from layover).

    Memory: snapshots are read in time order and each (bus, trip) is processed
    as soon as the bus has moved on, so only trips in progress are held."""
    sd = date.strftime("%Y%m%d")
    base = service_midnight(date)
    ranges, S = _load_bus_schedule(date)
    active: dict = {}                              # (vehicle, trip) -> [obs...]
    last_seen: dict = {}
    route_of: dict = {}
    done = []                                      # (vehicle, trip, route, a, b, passages float32)

    def finish(key):
        ob = active.pop(key)
        last_seen.pop(key, None)
        rng = ranges.get(key[1])
        if rng is None:
            return
        p = _bus_passages(ob, rng[0], rng[1], S, base)
        if p is not None and np.isfinite(p).any():
            done.append((key[0], key[1], route_of.pop(key, ""), rng[0], rng[1], p.astype(np.float32)))
        else:
            route_of.pop(key, None)

    next_sweep = 0
    for _, path in snapshot_files(date, ["bus_vehicles"]):
        msg = read_feed(path)
        now = msg.header.timestamp
        for e in msg.entity:
            if not e.HasField("vehicle"):
                continue
            v = e.vehicle
            if v.trip.start_date and v.trip.start_date != sd:
                continue
            if v.trip.trip_id not in ranges:
                continue
            k = (v.vehicle.id, v.trip.trip_id)
            if k not in active:
                active[k] = []
                route_of[k] = v.trip.route_id
            active[k].append((v.timestamp or now, v.stop_id, v.position.latitude, v.position.longitude))
            last_seen[k] = now
        if now >= next_sweep:                      # every 10 minutes, finish trips that ended
            for k in [k for k, ts in last_seen.items() if ts < now - FINISHED_AFTER]:
                finish(k)
            next_sweep = now + 600
    for k in list(active):
        finish(k)
    if not done:
        return pd.DataFrame()
    _LAST_BUS_TRIPS["trips"] = _bus_trip_table(done, S, date)
    lens = np.array([d[4] - d[3] for d in done])
    idx = np.concatenate([np.arange(d[3], d[4]) for d in done])
    cat = lambda vals: pd.Categorical(np.repeat(np.array(vals, dtype=object), lens))
    out = pd.DataFrame({
        "vehicle": cat([d[0] for d in done]), "trip_id": cat([d[1] for d in done]),
        "route_id": cat([d[2] for d in done]),
        "direction_id": pd.Categorical.from_codes(S["dir"][idx], S["dir_cats"]),
        "block_id": pd.Categorical.from_codes(S["block"][idx], S["block_cats"]),
        "stop_id": pd.Categorical.from_codes(S["stop_codes"][idx], S["stop_cats"]), "seq": S["seq"][idx],
        "sched_arr": S["arr"][idx], "arr": np.concatenate([d[5] for d in done]),
    })
    out = out[np.isfinite(out.arr.to_numpy())].reset_index(drop=True)
    for c in ("vehicle", "trip_id", "route_id", "direction_id", "block_id", "stop_id"):
        out[c] = out[c].astype(str)                # plain strings on disk, as before
    out["date"] = str(date)
    return out


def run(date: dt.date):
    out = config.WORK / "arrivals"
    out.mkdir(parents=True, exist_ok=True)
    s = reconstruct_subway(date)
    if len(s):
        s.to_parquet(out / f"subway_{date}.parquet", index=False)
    n_sub = len(s)
    del s                                  # free before the bus step so peaks don't stack
    b = reconstruct_bus(date)
    if len(b):
        b.to_parquet(out / f"bus_{date}.parquet", index=False)
        from .export import BUS_TRIP_COLS, BUS_TRIP_TYPES
        (config.WORK / "trips").mkdir(parents=True, exist_ok=True)
        trips = _LAST_BUS_TRIPS.pop("trips")
        trips[BUS_TRIP_COLS].astype(BUS_TRIP_TYPES).to_parquet(
            config.WORK / "trips" / f"bus_{date}.parquet", index=False)   # written after arrivals: not stale
    print(f"reconstructed {date}: {n_sub} subway stop events, {len(b)} bus stop events")
