"""Turn archived realtime snapshots into what actually happened.

Subway: NYCT publishes predictions, not arrivals. For each (trip, stop) we keep
the last prediction seen before that stop drops out of the trip's updates;
once a train is at a platform the feed reports its real arrival, so the last
value is close to the truth. Stops that vanish while still far in the future
(re-routes, skipped stops, trips that disappear) are discarded.

Buses: BusTime reports each bus's position and next stop with its fleet
number. A stop's passage time is when the bus is seen STOPPED_AT it, or else
the midpoint between the last snapshot before it and the first one after.
"""
from __future__ import annotations

import datetime as dt
import gzip
import math
from collections import defaultdict
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
    df = match_subway_schedule(df, date)
    _, sched_path = gtfs_static.subway_schedule_for(date)
    _SCHED_CACHE["s"] = pd.read_parquet(sched_path).groupby("trip_id")
    df = clean_subway(df.drop(columns=["seen"]))
    df["date"] = str(date)
    return df.sort_values(["rt_trip_id", "arr"])


def match_subway_schedule(df: pd.DataFrame, date: dt.date) -> pd.DataFrame:
    """Attach the scheduled trip, stop sequence and times. Uses the supplemented
    schedule fetched closest before the day, so planned work is matched too."""
    feed_dir, sched_path = gtfs_static.subway_schedule_for(date)
    services = gtfs_static.active_services(feed_dir, date)
    sched = pd.read_parquet(sched_path)
    sched = sched[sched.service_id.isin(services)]
    full = sched.drop_duplicates("trip_key").set_index("trip_key").trip_id
    short = sched.drop_duplicates("short_key").set_index("short_key").trip_id
    ids = pd.Series(df.rt_trip_id.unique())
    m = ids.map(full)
    m = m.fillna(ids.map(gtfs_static.short_key).map(short))
    df = df.merge(pd.DataFrame({"rt_trip_id": ids, "trip_id": m}), on="rt_trip_id", how="left")
    s = sched[["trip_id", "station", "seq", "arr", "dep"]].rename(
        columns={"arr": "sched_arr", "dep": "sched_dep", "seq": "sched_seq"})
    s = s.drop_duplicates(["trip_id", "station"])
    return df.merge(s, on=["trip_id", "station"], how="left")


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
    sched = _SCHED_CACHE.get("s")
    tid = g.trip_id.iat[-1]
    if sched is None or tid not in sched.groups:
        return g.iloc[0:0]
    s = sched.get_group(tid).sort_values("seq")
    last = s.iloc[-1]
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


def reconstruct_bus(date: dt.date) -> pd.DataFrame:
    """BusTime reports position and next stop but never STOPPED_AT, so a stop's
    passage time comes from GPS: the first snapshot within NEAR_STOP_M of it
    (for the origin: the last snapshot near it, i.e. departure). Stops passed
    between two snapshots get the midpoint. The terminal needs a snapshot near
    it; otherwise the trip has no end time (it is not guessed from layover)."""
    sd = date.strftime("%Y%m%d")
    base = service_midnight(date)
    obs = defaultdict(list)                       # (vehicle, trip) -> [(t, stop, lat, lon)]
    route_of = {}
    for _, path in snapshot_files(date, ["bus_vehicles"]):
        msg = read_feed(path)
        for e in msg.entity:
            if not e.HasField("vehicle"):
                continue
            v = e.vehicle
            if v.trip.start_date and v.trip.start_date != sd:
                continue
            k = (v.vehicle.id, v.trip.trip_id)
            route_of[k] = v.trip.route_id
            obs[k].append((v.timestamp or msg.header.timestamp, v.stop_id,
                           v.position.latitude, v.position.longitude))
    if not obs:
        return pd.DataFrame()
    sched = pd.read_parquet(config.WORK / "static" / "bus_schedule.parquet")
    sched = sched[sched.trip_id.isin({k[1] for k in obs})]
    # the borough feeds share some stops, so a stop_id can appear more than once
    stops = (pd.read_parquet(config.WORK / "static" / "bus_stops.parquet")
             .drop_duplicates("stop_id").set_index("stop_id"))
    by_trip = {tid: g.sort_values("seq") for tid, g in sched.groupby("trip_id")}
    rows = []
    for (veh, tid), ob in obs.items():
        g = by_trip.get(tid)
        if g is None:
            continue
        idx = {s: i for i, s in enumerate(g.stop_id)}
        ob.sort()
        t = np.array([o[0] for o in ob], dtype=float) - base
        pos = np.array([idx.get(o[1], -1) for o in ob])
        olat = np.array([o[2] for o in ob]); olon = np.array([o[3] for o in ob])
        keep = pos >= 0
        t, pos, olat, olon = t[keep], pos[keep], olat[keep], olon[keep]
        if len(t) < 3:
            continue
        pos = np.maximum.accumulate(pos)          # next-stop index never goes backwards
        n = len(g)
        sll = stops.reindex(g.stop_id)[["lat", "lon"]].to_numpy()
        # distance (m) from every snapshot to every stop on the trip
        dist = np.hypot((olat[:, None] - sll[None, :, 0]) * 111_000,
                        (olon[:, None] - sll[None, :, 1]) * 84_000)
        passage = np.full(n, np.nan)
        for k in range(n):
            r = NEAR_END_M if k in (0, n - 1) else NEAR_STOP_M
            near = np.flatnonzero((dist[:, k] < r) & ((pos == k) | (pos == k + 1)))
            if len(near):
                passage[k] = t[near[-1]] if k == 0 else t[near[0]]
                continue
            if k == n - 1:
                continue                          # never seen at the terminal: no end time
            after = np.flatnonzero(pos > k)
            if len(after) and after[0] > 0:
                i = after[0]
                passage[k] = (t[i - 1] + t[i]) / 2
        rows.append(pd.DataFrame({"vehicle": veh, "trip_id": tid, "route_id": route_of[(veh, tid)],
                                  "direction_id": g.direction_id.iat[0], "block_id": g.block_id.iat[0],
                                  "stop_id": g.stop_id.to_numpy(), "seq": g.seq.to_numpy(),
                                  "sched_arr": g.arr.to_numpy(), "arr": passage}))
    out = pd.concat(rows, ignore_index=True).dropna(subset=["arr"])
    out["date"] = str(date)
    return out


def run(date: dt.date):
    out = config.WORK / "arrivals"
    out.mkdir(parents=True, exist_ok=True)
    s = reconstruct_subway(date)
    if len(s):
        s.to_parquet(out / f"subway_{date}.parquet", index=False)
    b = reconstruct_bus(date)
    if len(b):
        b.to_parquet(out / f"bus_{date}.parquet", index=False)
    print(f"reconstructed {date}: {len(s)} subway stop events, {len(b)} bus stop events")
