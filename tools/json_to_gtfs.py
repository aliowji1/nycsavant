"""Sandbox helper: convert the JSON GTFS export from the `mta-gtfs-static` npm
package into standard GTFS .txt files, so the pipeline sees the same files it
would get from the MTA's own zip downloads.

Usage: python tools/json_to_gtfs.py SRC_JSON_DIR DEST_DIR [--routes M15,M15+]
"""
import argparse
import csv
import json
import os

FILES = ["agency", "routes", "stops", "trips", "stop_times", "calendar",
         "calendar_dates", "shapes", "transfers", "feed_info"]


def write(rows, path):
    if not rows:
        return
    cols = list(rows[0].keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dest")
    ap.add_argument("--routes", default="")
    a = ap.parse_args()
    os.makedirs(a.dest, exist_ok=True)
    keep_routes = set(r for r in a.routes.split(",") if r)
    keep_trips, keep_shapes, keep_stops = None, None, None
    order = ["routes", "trips", "stop_times", "stops", "shapes", "agency",
             "calendar", "calendar_dates", "transfers", "feed_info"]
    for name in order:
        p = os.path.join(a.src, name + ".json")
        if not os.path.exists(p):
            continue
        rows = json.load(open(p))
        if keep_routes:
            if name == "routes":
                rows = [r for r in rows if r["route_id"] in keep_routes]
            elif name == "trips":
                rows = [r for r in rows if r["route_id"] in keep_routes]
                keep_trips = {r["trip_id"] for r in rows}
                keep_shapes = {r.get("shape_id") for r in rows}
            elif name == "stop_times":
                rows = [r for r in rows if r["trip_id"] in keep_trips]
                keep_stops = {r["stop_id"] for r in rows}
            elif name == "stops":
                rows = [r for r in rows if r["stop_id"] in keep_stops]
            elif name == "shapes":
                rows = [r for r in rows if r["shape_id"] in keep_shapes]
        write(rows, os.path.join(a.dest, name + ".txt"))
        print(name, len(rows))
        del rows


if __name__ == "__main__":
    main()
