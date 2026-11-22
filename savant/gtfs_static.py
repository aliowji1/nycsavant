"""Static GTFS: download, load, and turn into the lookup tables the pipeline needs.

Outputs (in data/work/static/):
  subway_stations.parquet   parent station id, name, lat, lon, routes
  subway_schedule.parquet   one row per scheduled stop: trip key, route, dir, stop, sched secs
  subway_lines.json         per route: color, canonical station order (both dirs), shape
  bus_stops.parquet, bus_schedule.parquet, bus_lines.json   same idea for buses
"""
from __future__ import annotations

import datetime as dt
import io
import json
import math
import zipfile
from collections import Counter
from pathlib import Path

import pandas as pd

from . import config


# ---------------------------------------------------------------- loading
def download(name: str, url: str, dest: Path):
    import requests
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    dest.mkdir(parents=True, exist_ok=True)
    zipfile.ZipFile(io.BytesIO(r.content)).extractall(dest)


def download_all():
    """Fetch current static GTFS from the MTA. Bus zips are merged into one dir."""
    download("subway", config.STATIC_URLS["subway"], config.STATIC / "subway")
    frames: dict[str, list[pd.DataFrame]] = {}
    for k, url in config.STATIC_URLS.items():
        if not k.startswith("bus_"):
            continue
        tmp = config.STATIC / "_tmp" / k
        download(k, url, tmp)
        for f in tmp.glob("*.txt"):
            frames.setdefault(f.stem, []).append(pd.read_csv(f, dtype=str))
    out = config.STATIC / "bus"
    out.mkdir(parents=True, exist_ok=True)
    for stem, dfs in frames.items():
        pd.concat(dfs).drop_duplicates().to_csv(out / f"{stem}.txt", index=False)


def read(feed_dir: Path, name: str, **kw) -> pd.DataFrame:
    p = Path(feed_dir) / f"{name}.txt"
    if not p.exists():
        return pd.DataFrame()
    return pd.read_csv(p, dtype=str, keep_default_na=False, **kw)


def hms_to_sec(s: pd.Series) -> pd.Series:
    parts = s.str.split(":", expand=True).astype(int)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def active_services(feed_dir: Path, date: dt.date) -> set[str]:
    cal = read(feed_dir, "calendar")
    cd = read(feed_dir, "calendar_dates")
    ds = date.strftime("%Y%m%d")
    dow = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][date.weekday()]
    on = set()
    if len(cal):
        m = (cal[dow] == "1") & (cal.start_date <= ds) & (cal.end_date >= ds)
        on = set(cal.loc[m, "service_id"])
    if len(cd):
        day = cd[cd.date == ds]
        on |= set(day.loc[day.exception_type == "1", "service_id"])
        on -= set(day.loc[day.exception_type == "2", "service_id"])
    return on


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def simplify(points, tol=0.00015):
    """Douglas-Peucker on [lon, lat] pairs (tolerance in degrees, ~15 m)."""
    if len(points) < 3:
        return points
    (x1, y1), (x2, y2) = points[0], points[-1]
    dx, dy = x2 - x1, y2 - y1
    norm = math.hypot(dx, dy) or 1e-12
    best, idx = -1.0, 0
    for i in range(1, len(points) - 1):
        x0, y0 = points[i]
        d = abs(dy * x0 - dx * y0 + x2 * y1 - y2 * x1) / norm
        if d > best:
            best, idx = d, i
    if best <= tol:
        return [points[0], points[-1]]
    return simplify(points[: idx + 1], tol)[:-1] + simplify(points[idx:], tol)


# ---------------------------------------------------------------- subway
def subway_trip_key(trip_id: str) -> str:
    """Static 'AFA26GEN-1038-Sunday-00_000600_1..S03R' -> '000600_1..S03R'.
    Realtime trip_ids look like '000600_1..S03R' (sometimes without the shape suffix)."""
    parts = trip_id.split("_", 1)
    return parts[1] if len(parts) == 2 else trip_id


def short_key(key: str) -> str:
    """'000600_1..S03R' -> '000600_1..S' (origin time, route, direction)."""
    origin, _, rest = key.partition("_")
    route, _, tail = rest.partition("..")
    return f"{origin}_{route}..{tail[:1]}"


def canonical_order(patterns: Counter) -> list[str]:
    """Pick the stop pattern that covers the most stop-visits for a route/direction."""
    best = max(patterns.items(), key=lambda kv: kv[1] * len(kv[0]))
    return list(best[0])


def build_subway(feed_dir: Path | None = None, out: Path | None = None):
    feed_dir = Path(feed_dir or config.STATIC / "subway")
    out = Path(out or config.WORK / "static")
    out.mkdir(parents=True, exist_ok=True)

    stops = read(feed_dir, "stops")
    routes = read(feed_dir, "routes")
    trips = read(feed_dir, "trips")
    st = read(feed_dir, "stop_times", usecols=["trip_id", "stop_id", "arrival_time", "departure_time", "stop_sequence"])

    st["station"] = st.stop_id.str[:-1].where(st.stop_id.str[-1].isin(["N", "S"]), st.stop_id)
    st["arr"] = hms_to_sec(st.arrival_time)
    st["dep"] = hms_to_sec(st.departure_time)
    st["seq"] = st.stop_sequence.astype(int)
    st = st.merge(trips[["trip_id", "route_id", "service_id", "direction_id", "shape_id"]], on="trip_id")
    st["trip_key"] = st.trip_id.map(subway_trip_key)
    st["short_key"] = st.trip_key.map(short_key)
    st = st.sort_values(["trip_id", "seq"])

    sched = st[["trip_id", "trip_key", "short_key", "route_id", "service_id", "direction_id",
                "station", "seq", "arr", "dep"]]
    sched.to_parquet(out / "subway_schedule.parquet", index=False)

    # stations
    parents = stops[stops.location_type == "1"].copy()
    parents["lat"] = parents.stop_lat.astype(float)
    parents["lon"] = parents.stop_lon.astype(float)
    served = st.groupby("station").route_id.agg(lambda s: "".join(sorted(set(s))))
    parents["routes"] = parents.stop_id.map(served).fillna("")
    parents = parents[parents.routes != ""]
    parents[["stop_id", "stop_name", "lat", "lon", "routes"]].rename(
        columns={"stop_id": "station", "stop_name": "name"}).to_parquet(out / "subway_stations.parquet", index=False)
    pos = parents.set_index("stop_id")[["lat", "lon"]].to_dict("index")

    shapes = read(feed_dir, "shapes")
    shapes["seq"] = shapes.shape_pt_sequence.astype(int)
    shapes = shapes.sort_values(["shape_id", "seq"])
    shape_pts = {sid: g[["shape_pt_lon", "shape_pt_lat"]].astype(float).values.tolist()
                 for sid, g in shapes.groupby("shape_id")}

    lines = {}
    for rid, rg in st.groupby("route_id"):
        r = routes[routes.route_id == rid].iloc[0]
        entry = {"route": rid, "name": r.route_long_name, "color": "#" + (r.route_color or "808183"),
                 "text": "#" + (r.route_text_color or "FFFFFF"), "dirs": {}}
        for d, dg in rg.groupby("direction_id"):
            pats = Counter(tuple(g.station) for _, g in dg.groupby("trip_id", sort=False))
            order = canonical_order(pats)
            dist = [0.0]
            for a, b in zip(order, order[1:]):
                pa, pb = pos.get(a), pos.get(b)
                dist.append(dist[-1] + (haversine_m(pa["lat"], pa["lon"], pb["lat"], pb["lon"]) if pa and pb else 800))
            top_shape = dg.groupby("shape_id").trip_id.nunique().idxmax() if dg.shape_id.ne("").any() else None
            entry["dirs"][d] = {"stations": order, "dist_m": [round(x) for x in dist],
                                "shape": simplify(shape_pts.get(top_shape, []))}
        lines[rid] = entry
    (out / "subway_lines.json").write_text(json.dumps(lines))
    print(f"subway: {len(lines)} routes, {len(parents)} stations, {sched.trip_id.nunique()} trips")


# ---------------------------------------------------------------- bus
def build_bus(feed_dir: Path | None = None, out: Path | None = None):
    feed_dir = Path(feed_dir or config.STATIC / "bus")
    out = Path(out or config.WORK / "static")
    out.mkdir(parents=True, exist_ok=True)
    stops = read(feed_dir, "stops")
    routes = read(feed_dir, "routes")
    trips = read(feed_dir, "trips")
    st = read(feed_dir, "stop_times", usecols=["trip_id", "stop_id", "arrival_time", "departure_time", "stop_sequence"])
    st["arr"] = hms_to_sec(st.arrival_time)
    st["seq"] = st.stop_sequence.astype(int)
    st = st.merge(trips[["trip_id", "route_id", "service_id", "direction_id", "block_id", "shape_id"]], on="trip_id")
    st = st.sort_values(["trip_id", "seq"])
    st[["trip_id", "route_id", "service_id", "direction_id", "block_id", "stop_id", "seq", "arr"]].to_parquet(
        out / "bus_schedule.parquet", index=False)
    s = stops[["stop_id", "stop_name", "stop_lat", "stop_lon"]].copy()
    s["lat"], s["lon"] = s.stop_lat.astype(float), s.stop_lon.astype(float)
    s[["stop_id", "stop_name", "lat", "lon"]].to_parquet(out / "bus_stops.parquet", index=False)

    shapes = read(feed_dir, "shapes")
    if len(shapes):
        shapes["seq"] = shapes.shape_pt_sequence.astype(int)
        shapes = shapes.sort_values(["shape_id", "seq"])
    lines = {}
    for rid, rg in st.groupby("route_id"):
        r = routes[routes.route_id == rid].iloc[0]
        top_shape = rg.groupby("shape_id").trip_id.nunique().idxmax()
        pts = shapes[shapes.shape_id == top_shape][["shape_pt_lon", "shape_pt_lat"]].astype(float).values.tolist() if len(shapes) else []
        lines[rid] = {"route": rid, "name": r.route_long_name,
                      "color": "#" + (r.get("route_color") or "0039A6"), "shape": simplify(pts)}
    (out / "bus_lines.json").write_text(json.dumps(lines))
    print(f"bus: {len(lines)} routes, {st.trip_id.nunique()} trips")


def build_all():
    build_subway()
    if (config.STATIC / "bus" / "stop_times.txt").exists():
        build_bus()
