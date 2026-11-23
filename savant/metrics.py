"""Nightly metrics. Reads reconstructed arrivals and builds per-trip tables,
then the Savant stat lines.

Definitions (see README for the long version):
  on-schedule   subway: terminal arrival no more than 5 min late (MTA standard);
                bus: between 1 min early and 5 min late
  time saved    typical runtime for the same route, pattern and hour, minus
                this trip's runtime (positive = faster than usual)
  TAR           trips above replacement: on-schedule trips minus what a
                replacement-level vehicle (20th percentile on-schedule rate on
                the same line) would have delivered with the same workload
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from . import config

SHRINK_K = 10

SERVICE_TYPE = lambda sid: ("Weekday" if "Weekday" in sid else "Saturday" if "Saturday" in sid
                            else "Sunday" if "Sunday" in sid else sid)


def load_arrivals(kind: str, days: int | None = None) -> pd.DataFrame:
    files = sorted((config.WORK / "arrivals").glob(f"{kind}_*.parquet"))
    if days:
        files = files[-days:]
    if not files:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


# ======================================================================= trips
def subway_static_meta(dates=None) -> pd.DataFrame:
    """Origin, terminal and scheduled times per trip, from the same schedule
    each day was matched against (supplemented copies included)."""
    from . import gtfs_static
    paths = {gtfs_static.subway_schedule_for(dt.date.fromisoformat(d))[1] for d in (dates or [])}
    paths = paths or {config.WORK / "static" / "subway_schedule.parquet"}
    s = pd.concat([pd.read_parquet(p) for p in sorted(paths)]).drop_duplicates(["trip_id", "seq"])
    s = s.sort_values(["trip_id", "seq"])
    g = s.groupby("trip_id")
    return pd.DataFrame({
        "origin": g.station.first(), "terminal": g.station.last(),
        "sched_start": g.dep.first(), "sched_end": g.arr.last(), "n_sched": g.size(),
        "service": g.service_id.first().map(SERVICE_TYPE),
    }).reset_index()


def subway_trips(arr: pd.DataFrame) -> pd.DataFrame:
    a = arr.dropna(subset=["trip_id"]).copy()
    meta = subway_static_meta(sorted(a.date.unique()))
    a = a.merge(meta[["trip_id", "origin", "terminal"]], on="trip_id")
    key = ["date", "rt_trip_id"]
    g = a.groupby(key)
    t = g.agg(route_id=("route_id", "first"), direction_id=("direction_id", "first"),
              trip_id=("trip_id", "first"), n_obs=("station", "size")).reset_index()
    o = a[a.station == a.origin].drop_duplicates(key)[key + ["dep"]].rename(columns={"dep": "act_start"})
    e = a[a.station == a.terminal].drop_duplicates(key)[key + ["arr", "sched_arr"]].rename(
        columns={"arr": "act_end", "sched_arr": "sched_end_obs"})
    t = t.merge(o, on=key, how="left").merge(e, on=key, how="left").merge(meta, on="trip_id", how="left")
    t["complete"] = t.act_start.notna() & t.act_end.notna() & (t.n_obs >= 0.8 * t.n_sched)
    t["runtime"] = t.act_end - t.act_start
    t["end_delay"] = t.act_end - t.sched_end
    t["hour"] = (t.sched_start // 3600).astype(int)
    return _add_trip_scores(t, ["route_id", "direction_id", "origin", "terminal", "service", "hour"], "subway")


def bus_trips(arr: pd.DataFrame) -> pd.DataFrame:
    """One row per (day, bus, trip).

    BusTime usually switches a bus to its next trip only after it has left the
    first stop, so a trip counts as complete when it was seen from within its
    first three stops to the terminal. Time saved compares the runtime between
    the stops actually observed against the schedule for that same stretch.
    """
    a = arr.sort_values(["date", "vehicle", "trip_id", "seq"])
    sched = pd.read_parquet(config.WORK / "static" / "bus_schedule.parquet",
                            columns=["trip_id", "service_id", "seq", "arr"])
    sched = sched[sched.trip_id.isin(a.trip_id.unique())].sort_values(["trip_id", "seq"])
    sched["idx"] = sched.groupby("trip_id").cumcount()
    sg = sched.groupby("trip_id")
    meta = pd.DataFrame({"last_seq": sg.seq.max(), "n_sched": sg.size(),
                         "sched_start": sg.arr.min(), "sched_end": sg.arr.max(),
                         "service": sg.service_id.first().map(SERVICE_TYPE)}).reset_index()
    a = a.merge(sched[["trip_id", "seq", "idx"]], on=["trip_id", "seq"], how="left")
    key = ["date", "vehicle", "trip_id"]
    g = a.groupby(key)
    t = g.agg(route_id=("route_id", "first"), direction_id=("direction_id", "first"),
              n_obs=("seq", "size"), first_idx=("idx", "min"), max_seq=("seq", "max"),
              act_start=("arr", "first"), act_end=("arr", "last"),
              sched_at_start=("sched_arr", "first"), sched_at_end=("sched_arr", "last")).reset_index()
    t = t.merge(meta, on="trip_id")
    t["complete"] = (t.first_idx <= 2) & (t.max_seq == t.last_seq) & (t.n_obs >= 0.8 * t.n_sched)
    t["runtime"] = t.act_end - t.act_start
    t["excess"] = t.runtime - (t.sched_at_end - t.sched_at_start)   # vs schedule, same stretch
    t["end_delay"] = t.act_end - t.sched_end
    t["hour"] = (t.sched_start // 3600).astype(int)
    t["origin"], t["terminal"] = t.first_idx, t.last_seq
    return _add_trip_scores(t, ["route_id", "direction_id", "service", "hour"], "bus", value="excess")


def _add_trip_scores(t: pd.DataFrame, bucket: list[str], mode: str, value: str = "runtime") -> pd.DataFrame:
    """time_saved = typical value for the bucket minus this trip's value
    (value is runtime for the subway, runtime-vs-schedule excess for buses)."""
    c = t[t.complete]
    typical = c.groupby(bucket)[value].median().rename("typical_" + value)
    t = t.merge(typical, left_on=bucket, right_index=True, how="left")
    t["time_saved"] = t["typical_" + value] - t[value]
    early, late = config.ON_SCHED[mode]
    t["on_sched"] = t.complete & t.end_delay.between(-early, late)
    return t


# ======================================================================= savant
def stat_lines(t: pd.DataFrame, entity: str, line_col: str = "route_id", min_trips: int | None = None) -> pd.DataFrame:
    """Per-entity stat line, ranked within its line (the route it ran most)."""
    min_trips = min_trips or config.MIN_TRIPS_FOR_RANK
    c = t[t.complete]
    g = c.groupby(entity)
    s = pd.DataFrame({
        "trips": g.size(),
        "on_sched": g.on_sched.sum(),
        "time_saved": g.time_saved.mean(),
        "median_delay": g.end_delay.median(),
        "line": g[line_col].agg(lambda x: x.value_counts().index[0]),
        "lines": g[line_col].agg(lambda x: " ".join(x.value_counts().index[:4])),
        "days": g.date.nunique(),
    })
    s["on_sched_rate"] = s.on_sched / s.trips
    # Replacement level uses rates shrunk toward the line average, so a vehicle
    # with few trips can't set the bar by luck (empirical Bayes, prior = K trips).
    line_rate = c.groupby(line_col).on_sched.mean()
    lr = s.line.map(line_rate)
    s["shrunk_rate"] = (s.on_sched + SHRINK_K * lr) / (s.trips + SHRINK_K)
    qual = s[s.trips >= min_trips]
    rep = qual.groupby("line").shrunk_rate.quantile(config.REPLACEMENT_PCTL).rename("replacement_rate")
    s = s.join(rep, on="line")
    s["replacement_rate"] = s.replacement_rate.fillna(qual.shrunk_rate.quantile(config.REPLACEMENT_PCTL))
    s["tar"] = s.on_sched - s.trips * s.replacement_rate
    s["qualified"] = s.trips >= min_trips
    q = s[s.qualified].sort_values(["tar", "time_saved"], ascending=False)
    s["rank_line"] = q.groupby("line").cumcount() + 1          # TAR, ties broken by time saved
    s["line_size"] = s.line.map(q.groupby("line").size())
    s["rank_all"] = pd.Series(range(1, len(q) + 1), index=q.index)
    s["pctl_time_saved"] = q.groupby("line").time_saved.rank(pct=True)
    return s.reset_index()


def line_table(t: pd.DataFrame) -> pd.DataFrame:
    c = t[t.complete]
    g = c.groupby("route_id")
    out = pd.DataFrame({"trips": g.size(), "on_sched_rate": g.on_sched.mean(),
                        "median_delay": g.end_delay.median(), "p90_delay": g.end_delay.quantile(0.9),
                        "time_saved": g.time_saved.mean(), "median_runtime": g.runtime.median(),
                        "days": g.date.nunique()})
    t2 = t.assign(daytype=t.service)
    daily = t2.groupby(["route_id", "daytype", "date"]).size().groupby(["route_id", "daytype"]).mean().unstack()
    out = out.join(daily.add_prefix("avg_trips_"))
    out["scheduled"] = t.groupby("route_id").size()
    out["completion"] = g.size() / out.scheduled
    return out.reset_index()


# ======================================================================= segments
def subway_segments(arr: pd.DataFrame) -> pd.DataFrame:
    """Consecutive observed stops within a trip: actual vs typical vs scheduled run time."""
    a = arr.dropna(subset=["sched_arr"]).sort_values(["date", "rt_trip_id", "arr"])
    same = (a.rt_trip_id.values[1:] == a.rt_trip_id.values[:-1]) & (a.date.values[1:] == a.date.values[:-1])
    prev, nxt = a.iloc[:-1][same], a.iloc[1:][same]
    seg = pd.DataFrame({
        "date": nxt.date.values, "route_id": nxt.route_id.values, "direction_id": nxt.direction_id.values,
        "from": prev.station.values, "to": nxt.station.values, "t": prev.arr.values,
        "actual": nxt.arr.values - prev.arr.values,
        "sched": nxt.sched_arr.values - prev.sched_arr.values,
    })
    seg = seg[(seg.actual > 0) & (seg.sched > 0) & (seg.actual < 3 * 3600)]
    seg["hour"] = (seg.t // 3600).astype(int) % 24
    normal = seg.groupby(["route_id", "from", "to", "hour"]).actual.median().rename("normal")
    seg = seg.join(normal, on=["route_id", "from", "to", "hour"])
    seg["excess"] = seg.actual - seg.normal
    return seg
