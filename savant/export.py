"""Write everything the static site reads into site/data/."""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from . import config, metrics


def _clean(o):
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        return None if math.isnan(o) else round(float(o), 4)
    if isinstance(o, np.bool_):
        return bool(o)
    return o


def dump(obj, name: str):
    p = config.SITE_DATA / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(_clean(obj), separators=(",", ":")))


def records(df: pd.DataFrame, cols: list[str]) -> list[list]:
    return df[cols].astype(object).where(df[cols].notna(), None).values.tolist()


def fmt_time(sec: float) -> str:
    sec = int(sec) % 86400
    h, m = divmod(sec // 60, 60)
    return f"{(h % 12) or 12}:{m:02d}{'a' if h < 12 else 'p'}"


# ======================================================================= network
def network():
    static = config.WORK / "static"
    st = pd.read_parquet(static / "subway_stations.parquet")
    lines = json.loads((static / "subway_lines.json").read_text())
    bus = json.loads((static / "bus_lines.json").read_text()) if (static / "bus_lines.json").exists() else {}
    dump({"stations": records(st, ["station", "name", "lat", "lon", "routes"]),
          "lines": lines, "bus_lines": bus}, "network.json")
    return st, lines


# ======================================================================= marey
def marey(arr: pd.DataFrame, lines: dict, dates: list[str]):
    """One file per day: {route: [[dir, [stationIdx...], [secs...], id, fromStart], ...]}
    where stationIdx indexes the route's canonical southbound (direction 1) order
    and fromStart is 1 if the trip was seen at its scheduled first stop (trips
    already running when collection began are drawn, but not lined up)."""
    from . import gtfs_static
    for date in dates:
        day = arr[arr.date == date]
        _, sched_path = gtfs_static.subway_schedule_for(dt.date.fromisoformat(date))
        sched = pd.read_parquet(sched_path, columns=["trip_id", "station", "seq"])
        origin = sched.sort_values("seq").drop_duplicates("trip_id").set_index("trip_id").station
        first_seen = day.sort_values("arr").drop_duplicates("rt_trip_id").set_index("rt_trip_id")
        from_start = (first_seen.station == first_seen.trip_id.map(origin)).astype(int).to_dict()
        out = {}
        for route, rg in day.groupby("route_id"):
            if route not in lines:
                continue
            order = lines[route]["dirs"].get("1") or next(iter(lines[route]["dirs"].values()))
            idx = {s: i for i, s in enumerate(order["stations"])}
            trips = []
            for tid, g in rg.sort_values("arr").groupby("rt_trip_id", sort=False):
                g = g[g.station.isin(idx)]
                if len(g) < 2:
                    continue
                trips.append([int(g.direction_id.iat[0]), [idx[s] for s in g.station],
                              [int(x) for x in g.arr], tid, from_start.get(tid, 0)])
            out[route] = trips
        dump(out, f"marey/{date}.json")


# ======================================================================= savant
def savant_subway(trips: pd.DataFrame, st_names: dict):
    t = trips.copy()
    slots = metrics.stat_lines(t, "slot", min_trips=5)
    meta = t.drop_duplicates("slot").set_index("slot")
    slots = slots.join(meta[["route_id", "direction_id", "origin", "terminal", "sched_start", "service"]], on="slot")
    slots["label"] = [f"{svc} {fmt_time(s)} from {st_names.get(o, o)}"
                      for svc, s, o in zip(slots.service, slots.sched_start, slots.origin)]
    slots["to"] = slots.terminal.map(st_names)
    cols = ["slot", "label", "route_id", "direction_id", "to", "trips", "on_sched", "on_sched_rate",
            "time_saved", "median_delay", "tar", "rank_line", "line_size", "replacement_rate", "qualified"]
    for route, g in slots.groupby("route_id"):
        dump({"cols": cols, "rows": records(g.sort_values("tar", ascending=False), cols)}, f"slots/{route}.json")
    q = slots[slots.qualified].sort_values("tar", ascending=False)
    dump({"cols": cols, "best": records(q.head(25), cols), "worst": records(q.tail(25).iloc[::-1], cols)},
         "slots/top.json")
    return slots


def savant_buses(trips: pd.DataFrame):
    s = metrics.stat_lines(trips, "vehicle")
    s["fleet"] = s.vehicle.str.replace("MTA NYCT_", "", regex=False).str.replace("MTABC_", "", regex=False)
    daily = (trips[trips.complete].groupby(["vehicle", "date"])
             .agg(n=("on_sched", "size"), on=("on_sched", "sum"), ts=("time_saved", "mean")).reset_index())
    dd = {v: records(g, ["date", "n", "on", "ts"]) for v, g in daily.groupby("vehicle")}
    cols = ["fleet", "line", "lines", "trips", "on_sched", "on_sched_rate", "time_saved", "median_delay",
            "tar", "rank_line", "line_size", "rank_all", "replacement_rate", "days", "qualified"]
    s = s.sort_values("tar", ascending=False)
    dump({"cols": cols, "rows": records(s, cols), "daily": {f.replace("MTA NYCT_", ""): dd.get(f, [])
                                                          for f in s.vehicle}}, "savant_buses.json")
    lt = metrics.line_table(trips)
    dump({"cols": list(lt.columns), "rows": records(lt, list(lt.columns))}, "bus_lines.json")
    return s


def lines_table(trips: pd.DataFrame):
    lt = metrics.line_table(trips)
    # replacement-level line = 20th pct on-schedule rate; TAR per day for lines
    rep = lt.on_sched_rate.quantile(config.REPLACEMENT_PCTL)
    lt["tar_per_day"] = (lt.on_sched_rate - rep) * lt.trips / lt.days
    daily = (trips[trips.complete].groupby(["route_id", "date"]).on_sched.mean().reset_index())
    trend = {r: records(g, ["date", "on_sched"]) for r, g in daily.groupby("route_id")}
    dump({"cols": list(lt.columns), "rows": records(lt, list(lt.columns)), "trend": trend}, "lines.json")


# ======================================================================= segments / delay week
def segments(hourly: pd.DataFrame, bins: pd.DataFrame, dates: list[str]):
    """Worst segments over the window, and the last week's delay strips + map,
    all from the per-day aggregates (never from raw stop events)."""
    key = metrics.SEG_KEY
    days = hourly.date.nunique()
    g = hourly.groupby(key)
    agg = pd.DataFrame({"n": g.n.sum(), "sched": g.sum_sched.sum() / g.n.sum(),
                        "median": g.sum_actual.sum() / g.n.sum(),
                        "lost_min_per_day": g.lost.sum() / 60 / days}).reset_index()
    agg = agg[agg.n >= 20]
    cols = list(agg.columns)
    dump({"cols": cols, "rows": records(agg.sort_values("lost_min_per_day", ascending=False), cols)},
         "segments.json")

    normal = metrics.segment_normals(hourly)
    week = sorted(dates)[-7:]
    day_i = {d: i for i, d in enumerate(week)}
    # per route+direction, 10-minute delay index vs normal for that segment and hour
    b = bins[bins.date.isin(week)].copy()
    b["hour"] = b["bin"] // 6
    b = b.join(normal, on=["route_id", "from", "to", "hour"]).dropna(subset=["normal"])
    b["expected"] = b.n * b.normal
    b["wbin"] = b.date.map(day_i) * 144 + b["bin"]
    rd = b.groupby(["route_id", "direction_id", "wbin"]).agg(a=("sum_actual", "sum"), e=("expected", "sum"))
    rd["rel"] = (rd.a / rd.e - 1) * 100
    series = {}
    for (r, d), gg in rd.groupby(level=[0, 1]):
        arr = np.full(144 * len(week), np.nan)
        arr[gg.index.get_level_values(2)] = gg.rel.values
        series[f"{r}|{d}"] = [None if np.isnan(x) else round(float(x), 1) for x in arr]
    # per segment, hourly index for the map
    h = hourly[hourly.date.isin(week)].join(normal, on=["route_id", "from", "to", "hour"]).dropna(subset=["normal"])
    h["hbin"] = h.date.map(day_i) * 24 + h.hour
    h["rel"] = ((h.sum_actual / (h.n * h.normal) - 1) * 100).round()
    keys = sorted({tuple(k) for k in h[key].itertuples(index=False)})
    kid = {k: i for i, k in enumerate(keys)}
    mat = [[None] * len(keys) for _ in range(24 * len(week))]
    for r, d, f, t, hb, v in h[key + ["hbin", "rel"]].itertuples(index=False):
        mat[hb][kid[(r, d, f, t)]] = int(v)
    return {"days": week, "series": series, "segments": [list(k) for k in keys], "hourly": mat}


# ======================================================================= ridership
def ridership(dates: list[str], delay_week: dict):
    rdir = config.WORK / "ridership"
    p = rdir / "hourly_entries.parquet"
    if not p.exists():
        dump(delay_week, "delay_week.json")
        return
    r = pd.read_parquet(p)
    r["ts"] = pd.to_datetime(r.transit_timestamp)
    r["date"] = r.ts.dt.date.astype(str)
    r["hour"] = r.ts.dt.hour
    days = sorted(r.date.unique())
    di = {d: i for i, d in enumerate(days)}
    sys = r.groupby(["date", "hour"]).ridership.sum()
    sysm = np.zeros((len(days), 24))
    for (d, h), v in sys.items():
        sysm[di[d], h] = v
    exits = pd.read_parquet(rdir / "exit_profile.parquet") if (rdir / "exit_profile.parquet").exists() else None
    wd = pd.to_datetime(pd.Series(days)).dt.weekday.values < 5
    stations = []
    for cid, g in r.groupby("station_complex_id"):
        m = np.zeros((len(days), 24))
        for d, h, v in zip(g.date, g.hour, g.ridership):
            m[di[d], h] += v
        wprof = m[wd].mean(axis=0) if wd.any() else m.mean(axis=0)
        eprof = m[~wd].mean(axis=0) if (~wd).any() else np.zeros(24)
        am, pm = wprof[6:10].sum(), wprof[16:20].sum()
        ratio = am / pm if pm else 1
        ex_wd = ex_we = None
        if exits is not None:
            e = exits[exits.station_complex_id.astype(str) == str(cid)]
            ex_wd = e[e.daytype == "weekday"].set_index("hour").exits.reindex(range(24), fill_value=0).values
            ex_we = e[e.daytype == "weekend"].set_index("hour").exits.reindex(range(24), fill_value=0).values
            if ex_wd.sum():  # scale estimated exits to match entry volume (OD is a modeled estimate)
                ex_wd = ex_wd * wprof.sum() / ex_wd.sum()
            if ex_we.sum():
                ex_we = ex_we * (eprof.sum() or 1) / ex_we.sum()
        kind = "home" if ratio > 1.5 else "work" if ratio < 0.67 else "all-day"
        stations.append({"id": str(cid), "name": g.station_complex.iat[0], "lat": g.latitude.iat[0],
                         "lon": g.longitude.iat[0], "wd": round(wprof.sum()), "we": round(eprof.sum()),
                         "kind": kind, "ratio": round(float(ratio), 2),
                         "m": [int(x) for x in m.flatten()],
                         "xwd": None if ex_wd is None else [int(x) for x in ex_wd],
                         "xwe": None if ex_we is None else [int(x) for x in ex_we],
                         "nwd": [int(x) for x in wprof], "nwe": [int(x) for x in eprof]})
    meta = json.loads((rdir / "meta.json").read_text()) if (rdir / "meta.json").exists() else {}
    dump({"days": days, "system": sysm.astype(int).tolist(), "stations": stations, "meta": meta}, "ridership.json")
    # attach hourly system entries for the delay week
    delay_week["entries"] = [sysm[di[d]].astype(int).tolist() if d in di else None for d in delay_week["days"]]
    dump(delay_week, "delay_week.json")


# ======================================================================= main
def _stale(out: Path, src: Path) -> bool:
    return not out.exists() or out.stat().st_mtime < src.stat().st_mtime


def summarize_day(date: str, lines: dict):
    """Condense one reconstructed day into small files, once. Re-done only if
    the day was reconstructed again. This is the only step that reads a day's
    raw stop events, one day at a time, which keeps memory flat."""
    arr_dir, trips_dir, seg_dir = config.WORK / "arrivals", config.WORK / "trips", config.WORK / "segments"
    trips_dir.mkdir(parents=True, exist_ok=True)
    seg_dir.mkdir(parents=True, exist_ok=True)
    sub_src = arr_dir / f"subway_{date}.parquet"
    if sub_src.exists():
        targets = [trips_dir / f"subway_{date}.parquet", seg_dir / f"hourly_{date}.parquet",
                   seg_dir / f"bins_{date}.parquet", config.SITE_DATA / "marey" / f"{date}.json"]
        if any(_stale(t, sub_src) for t in targets):
            sub = pd.read_parquet(sub_src)
            metrics.subway_trip_rows(sub).to_parquet(targets[0], index=False)
            hourly, bins = metrics.segment_aggregates(sub)
            hourly.to_parquet(targets[1], index=False)
            bins.to_parquet(targets[2], index=False)
            marey(sub, lines, [date])
            del sub
    bus_src = arr_dir / f"bus_{date}.parquet"
    out = trips_dir / f"bus_{date}.parquet"
    if bus_src.exists() and _stale(out, bus_src):
        bt = metrics.bus_trip_rows(pd.read_parquet(bus_src))
        bt[BUS_TRIP_COLS].astype(BUS_TRIP_TYPES).to_parquet(out, index=False)


# Only what scoring and the site use, in compact types: ~50k bus trips a day
# for 28 days has to fit comfortably in a 1 GB server.
BUS_TRIP_COLS = ["date", "vehicle", "route_id", "direction_id", "service", "hour", "complete",
                 "runtime", "excess", "end_delay"]
BUS_TRIP_TYPES = {"hour": "int8", "runtime": "float32", "excess": "float32", "end_delay": "float32"}


def _concat(pattern: str, dates: list[str]) -> pd.DataFrame:
    """Read per-day summaries for the given dates."""
    files = [config.WORK / pattern.format(d) for d in dates]
    files = [f for f in files if f.exists()]
    if not files:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


def run(window_days: int = 28, simulated: bool = False):
    st, lines = network()
    names = dict(zip(st.station, st.name))
    dates = sorted(p.stem.split("_", 1)[1] for p in (config.WORK / "arrivals").glob("subway_*.parquet"))[-window_days:]
    for d in dates:
        summarize_day(d, lines)

    t = metrics.score_subway(_concat("trips/subway_{}.parquet", dates))
    lines_table(t)
    savant_subway(t, names)
    del t
    # 10-minute totals are only drawn for the last week, so only those are loaded
    dw = segments(_concat("segments/hourly_{}.parquet", dates), _concat("segments/bins_{}.parquet", dates[-7:]), dates)
    ridership(dates, dw)
    nbus = 0
    bt = _concat("trips/bus_{}.parquet", dates)
    if len(bt):
        nbus = len(savant_buses(metrics.score_bus(bt)))
    dump({"generated": dt.datetime.now().isoformat(timespec="minutes"), "dates": dates,
          "simulated": simulated, "buses": nbus,
          "definitions": {"on_sched": config.ON_SCHED,
                          "replacement_pctl": config.REPLACEMENT_PCTL, "min_trips": config.MIN_TRIPS_FOR_RANK}},
         "meta.json")
    print(f"exported {len(dates)} days to {config.SITE_DATA}")
