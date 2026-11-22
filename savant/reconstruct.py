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
    df["date"] = str(date)
    return df.sort_values(["rt_trip_id", "arr"]).drop(columns=["seen"])


def match_subway_schedule(df: pd.DataFrame, date: dt.date) -> pd.DataFrame:
    services = gtfs_static.active_services(config.STATIC / "subway", date)
    sched = pd.read_parquet(config.WORK / "static" / "subway_schedule.parquet")
    sched = sched[sched.service_id.isin(services)]
    full = sched.drop_duplicates("trip_key").set_index("trip_key").trip_id
    short = sched.drop_duplicates("short_key").set_index("short_key").trip_id
    ids = pd.Series(df.rt_trip_id.unique())
    m = ids.map(full)
    m = m.fillna(ids.map(gtfs_static.short_key).map(short))
    df = df.merge(pd.DataFrame({"rt_trip_id": ids, "trip_id": m}), on="rt_trip_id", how="left")
    s = sched[["trip_id", "station", "arr", "dep"]].rename(columns={"arr": "sched_arr", "dep": "sched_dep"})
    s = s.drop_duplicates(["trip_id", "station"])
    return df.merge(s, on=["trip_id", "station"], how="left")


# ======================================================================= bus
def reconstruct_bus(date: dt.date) -> pd.DataFrame:
    sd = date.strftime("%Y%m%d")
    base = service_midnight(date)
    obs = defaultdict(list)                       # (vehicle, trip) -> [(t, stop, status)]
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
                           v.current_status == rt.VehiclePosition.STOPPED_AT))
    if not obs:
        return pd.DataFrame()
    sched = pd.read_parquet(config.WORK / "static" / "bus_schedule.parquet")
    sched = sched[sched.trip_id.isin({k[1] for k in obs})]
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
        stopped = np.array([o[2] for o in ob])
        keep = pos >= 0
        t, pos, stopped = t[keep], pos[keep], stopped[keep]
        if len(t) < 3:
            continue
        pos = np.maximum.accumulate(pos)          # next-stop index never goes backwards
        n = len(g)
        passage = np.full(n, np.nan)
        for k in range(n):
            at = np.flatnonzero((pos == k) & stopped)
            if len(at):
                passage[k] = t[at[0]]
                continue
            after = np.flatnonzero(pos > k)
            if len(after) and after[0] > 0:
                i = after[0]
                passage[k] = (t[i - 1] + t[i]) / 2
        if k == n - 1 and np.isnan(passage[-1]) and pos[-1] == n - 1:
            passage[-1] = t[-1]                   # last stop: last time we saw it heading there
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
