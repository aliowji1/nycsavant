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

import numpy as np
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
    """Fetch current static GTFS from the MTA.

    Bus feeds come one zip per borough. They are kept separate and processed
    one at a time (never concatenated: citywide stop_times is ~7M rows), and
    only the small calendar files are merged into static/bus/."""
    import shutil
    download("subway", config.STATIC_URLS["subway"], config.STATIC / "subway")
    feeds = config.STATIC / "bus_feeds"
    shutil.rmtree(feeds, ignore_errors=True)
    shutil.rmtree(config.STATIC / "_tmp", ignore_errors=True)
    for k, url in config.STATIC_URLS.items():
        if k.startswith("bus_"):
            download(k, url, feeds / k)
    out = config.STATIC / "bus"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    for name in ("calendar", "calendar_dates"):
        parts = [read(d, name) for d in sorted(feeds.iterdir())]
        parts = [p for p in parts if len(p)]
        if parts:
            pd.concat(parts).drop_duplicates().to_csv(out / f"{name}.txt", index=False)


def fetch_supplemented(today: dt.date | None = None):
    """Download today's supplemented subway schedule and build its tables.

    The supplemented feed reflects planned service changes, so the schedule a
    day actually ran on is only available around that day. Each fetch is kept
    under static/supplemented/<date>/ and work/static/supplemented/<date>/;
    reconstruction uses the newest copy fetched on or before the service day.
    """
    import shutil
    today = today or dt.date.today()
    feed_dir = config.STATIC / "supplemented" / str(today)
    download("subway_supplemented", config.STATIC_URLS["subway_supplemented"], feed_dir)
    build_subway(feed_dir, config.WORK / "static" / "supplemented" / str(today))
    # only the service calendar is read again later; drop the ~130 MB of other files
    for f in feed_dir.glob("*.txt"):
        if f.name not in ("calendar.txt", "calendar_dates.txt"):
            f.unlink()
    cutoff = today - dt.timedelta(days=config.SUPPLEMENTED_KEEP_DAYS)
    for root in (config.STATIC / "supplemented", config.WORK / "static" / "supplemented"):
        for d in root.glob("????-??-??"):
            if dt.date.fromisoformat(d.name) < cutoff:
                shutil.rmtree(d)


def subway_schedule_for(date: dt.date) -> tuple[Path, Path]:
    """(feed dir, schedule parquet) to use for a service day: the newest
    supplemented copy fetched on or before that day, else the regular schedule."""
    root = config.WORK / "static" / "supplemented"
    best = None
    for d in sorted(root.glob("????-??-??")) if root.exists() else []:
        if dt.date.fromisoformat(d.name) <= date and (d / "subway_schedule.parquet").exists():
            best = d.name
    if best:
        return config.STATIC / "supplemented" / best, root / best / "subway_schedule.parquet"
    return config.STATIC / "subway", config.WORK / "static" / "subway_schedule.parquet"


def read_lean(path: Path, columns: list[str], filters=None, text_cols=()) -> pd.DataFrame:
    """Read a parquet table with repeated text columns as categoricals
    (dictionary-encoded), which keeps millions of trip/stop IDs small."""
    import pyarrow.parquet as pq
    # filter columns must be compact too, or they get read as millions of strings
    filter_cols = [f[0] for f in (filters or [])]
    dict_cols = sorted({c for c in text_cols if c in columns} | set(filter_cols))
    t = pq.read_table(path, columns=columns, filters=filters, read_dictionary=dict_cols)
    # self_destruct frees Arrow's buffers as columns convert, so the data isn't held twice
    return t.to_pandas(self_destruct=True, split_blocks=True)


def read(feed_dir: Path, name: str, **kw) -> pd.DataFrame:
    p = Path(feed_dir) / f"{name}.txt"
    if not p.exists():
        return pd.DataFrame()
    return pd.read_csv(p, dtype=str, keep_default_na=False, **kw)


def read_stop_times(feed_dir: Path, with_departure: bool = False) -> pd.DataFrame:
    """stop_times with IDs and times as categoricals (millions of rows, few
    distinct values), times converted to seconds via the categories only."""
    cols = ["trip_id", "stop_id", "arrival_time", "stop_sequence"] + (["departure_time"] if with_departure else [])
    st = pd.read_csv(Path(feed_dir) / "stop_times.txt", usecols=cols,
                     dtype={c: "category" for c in cols if c != "stop_sequence"} | {"stop_sequence": "int32"})
    for c, out in (("arrival_time", "arr"), ("departure_time", "dep")):
        if c in st.columns:
            secs = hms_to_sec(pd.Series(st[c].cat.categories.astype(str))).to_numpy("int32")
            st[out] = secs[st[c].cat.codes.to_numpy()]
            st = st.drop(columns=c)
    return st.rename(columns={"stop_sequence": "seq"})


def attach_trips(st: pd.DataFrame, trips: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Add trip-level columns to stop rows by category code (no big merge)."""
    t = trips.drop_duplicates("trip_id").set_index("trip_id").reindex(st.trip_id.cat.categories)
    codes = st.trip_id.cat.codes.to_numpy()
    for c in cols:
        vals = pd.Categorical(t[c].fillna(""))
        st[c] = pd.Categorical.from_codes(vals.codes[codes], vals.categories)
    return st[st.route_id != ""] if "route_id" in cols else st


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
    st = read_stop_times(feed_dir, with_departure=True)
    # station = stop_id without its N/S platform suffix, computed on the categories
    sc = pd.Series(st.stop_id.cat.categories.astype(str))
    stripped = sc.where(~sc.str[-1].isin(["N", "S"]), sc.str[:-1]).to_numpy()
    uniq, inv = np.unique(stripped, return_inverse=True)
    st["station"] = pd.Categorical.from_codes(inv[st.stop_id.cat.codes.to_numpy()], uniq)
    st = attach_trips(st, trips, ["route_id", "service_id", "direction_id", "shape_id"])
    tc = pd.Series(st.trip_id.cat.categories.astype(str))
    tk = tc.map(subway_trip_key)
    codes = st.trip_id.cat.codes.to_numpy()
    st["trip_key"] = pd.Categorical.from_codes(codes, tk) if not tk.duplicated().any() else tk.to_numpy()[codes]
    sk = tk.map(short_key).to_numpy()[codes]
    st["short_key"] = pd.Categorical(sk)
    st = st.sort_values(["trip_id", "seq"])

    sched = st[["trip_id", "trip_key", "short_key", "route_id", "service_id", "direction_id",
                "station", "seq", "arr", "dep"]]
    _to_parquet_plain(sched, out / "subway_schedule.parquet")

    # stations
    parents = stops[stops.location_type == "1"].copy()
    parents["lat"] = parents.stop_lat.astype(float)
    parents["lon"] = parents.stop_lon.astype(float)
    served = st.groupby("station", observed=True).route_id.agg(lambda s: "".join(sorted(set(s.astype(str)))))
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
    for rid, rg in st.groupby("route_id", observed=True):
        r = routes[routes.route_id == rid].iloc[0]
        entry = {"route": rid, "name": r.route_long_name, "color": "#" + (r.route_color or "808183"),
                 "text": "#" + (r.route_text_color or "FFFFFF"), "dirs": {}}
        for d, dg in rg.groupby("direction_id", observed=True):
            pats = Counter(tuple(g.station.astype(str)) for _, g in dg.groupby("trip_id", sort=False, observed=True))
            order = canonical_order(pats)
            dist = [0.0]
            for a, b in zip(order, order[1:]):
                pa, pb = pos.get(a), pos.get(b)
                dist.append(dist[-1] + (haversine_m(pa["lat"], pa["lon"], pb["lat"], pb["lon"]) if pa and pb else 800))
            top_shape = dg.groupby("shape_id", observed=True).trip_id.nunique().idxmax() if dg.shape_id.ne("").any() else None
            entry["dirs"][d] = {"stations": order, "dist_m": [round(x) for x in dist],
                                "shape": simplify(shape_pts.get(top_shape, []))}
        lines[rid] = entry
    (out / "subway_lines.json").write_text(json.dumps(lines))
    print(f"subway: {len(lines)} routes, {len(parents)} stations, {st.trip_id.nunique()} trips")


# ---------------------------------------------------------------- bus
def _to_parquet_plain(df: pd.DataFrame, path: Path, **kw):
    """Write categoricals straight from their codes (no per-row strings), as
    dictionary-encoded text that readers get back as ordinary strings."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    arrays = {}
    for c in df.columns:
        col = df[c]
        if isinstance(col.dtype, pd.CategoricalDtype):
            arrays[c] = pa.DictionaryArray.from_arrays(
                pa.array(col.cat.codes.to_numpy(np.int32)), pa.array(col.cat.categories.astype(str).to_numpy(), pa.string()))
        else:
            arrays[c] = pa.array(col.to_numpy())
    pq.write_table(pa.table(arrays), path, store_schema=False, **kw)


def bus_feed_dirs() -> list[Path]:
    """Per-borough bus feeds (production), or a single merged dir (tests)."""
    single = config.STATIC / "bus"
    if (single / "stop_times.txt").exists():
        return [single]
    feeds = config.STATIC / "bus_feeds"
    return sorted(d for d in feeds.iterdir() if (d / "stop_times.txt").exists()) if feeds.exists() else []


def build_bus(feed_dirs: list[Path] | None = None, out: Path | None = None):
    """Bus tables, one borough feed at a time so memory stays at one feed's
    worth. The schedule is written as row groups sorted by service, compact types."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    feed_dirs = feed_dirs or bus_feed_dirs()
    out = Path(out or config.WORK / "static")
    out.mkdir(parents=True, exist_ok=True)
    cols = ["trip_id", "route_id", "service_id", "direction_id", "block_id", "stop_id", "seq", "arr"]
    text = pa.dictionary(pa.int32(), pa.string())
    schema = pa.schema([(c, pa.int32() if c in ("seq", "arr") else text) for c in cols])
    # store_schema=False: readers get plain strings back, not dictionary columns
    writer = pq.ParquetWriter(out / "bus_schedule.parquet", schema, store_schema=False)
    stops_parts, lines, n_trips = [], {}, 0
    for fd in feed_dirs:
        trips = read(fd, "trips")
        routes = read(fd, "routes")
        st = read_stop_times(fd)
        st = attach_trips(st, trips, ["route_id", "service_id", "direction_id", "block_id", "shape_id"])
        n_trips += st.trip_id.nunique()
        bs = st[cols].sort_values(["service_id", "trip_id", "seq"])
        # hand Parquet the category codes directly (no per-row strings)
        table = pa.table({c: (pa.array(bs[c].to_numpy(np.int32)) if c in ("seq", "arr") else
                              pa.DictionaryArray.from_arrays(pa.array(bs[c].cat.codes.to_numpy(np.int32)),
                                                             pa.array(bs[c].cat.categories.astype(str).to_numpy(), pa.string())))
                          for c in cols}, schema=schema)
        writer.write_table(table, row_group_size=200_000)
        del bs, table
        stops = read(fd, "stops")
        stops_parts.append(stops[["stop_id", "stop_name", "stop_lat", "stop_lon"]])
        shapes = (pd.read_csv(Path(fd) / "shapes.txt", dtype={"shape_id": "category"})
                  if (Path(fd) / "shapes.txt").exists() else pd.DataFrame())
        if len(shapes):
            shapes = shapes.sort_values(["shape_id", "shape_pt_sequence"])
        for rid, rg in st.groupby("route_id", observed=True):
            if rid in lines or not (routes.route_id == rid).any():
                continue
            r = routes[routes.route_id == rid].iloc[0]
            top_shape = rg.groupby("shape_id", observed=True).trip_id.nunique().idxmax()
            pts = (shapes[shapes.shape_id == top_shape][["shape_pt_lon", "shape_pt_lat"]].astype(float).values.tolist()
                   if len(shapes) else [])
            lines[rid] = {"route": rid, "name": r.route_long_name,
                          "color": "#" + (r.get("route_color") or "0039A6"), "shape": simplify(pts)}
        del st, shapes
    writer.close()
    s = pd.concat(stops_parts).drop_duplicates("stop_id")      # boroughs share some stops
    s["lat"], s["lon"] = s.stop_lat.astype(float), s.stop_lon.astype(float)
    s[["stop_id", "stop_name", "lat", "lon"]].to_parquet(out / "bus_stops.parquet", index=False)
    (out / "bus_lines.json").write_text(json.dumps(lines))
    print(f"bus: {len(lines)} routes, {n_trips} trips")


def build_all():
    build_subway()
    if bus_feed_dirs():
        build_bus()
