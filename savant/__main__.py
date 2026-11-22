"""Command line: python -m savant <command>

  static        download current MTA static GTFS and rebuild lookup tables
  build-static  rebuild lookup tables from data/static (no download)
  collect       poll realtime feeds forever (run as a service)
  reconstruct   turn raw snapshots into arrivals   [--date YYYY-MM-DD, default yesterday]
  ridership     fetch hourly ridership + exit profile for the export window
  export        compute metrics and write site/data
  nightly       reconstruct yesterday, refresh ridership, export, prune old raw data
  simulate      generate simulated feeds          --start YYYY-MM-DD --days N
"""
import argparse
import datetime as dt
import logging
from zoneinfo import ZoneInfo

from . import config


def yesterday() -> dt.date:
    return dt.datetime.now(ZoneInfo(config.TZ)).date() - dt.timedelta(days=1)


def main():
    ap = argparse.ArgumentParser(prog="savant")
    ap.add_argument("cmd")
    ap.add_argument("--date")
    ap.add_argument("--start")
    ap.add_argument("--days", type=int, default=28)
    ap.add_argument("--simulated", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    if a.cmd == "static":
        from . import gtfs_static
        gtfs_static.download_all()
        gtfs_static.build_all()
    elif a.cmd == "build-static":
        from . import gtfs_static
        gtfs_static.build_all()
    elif a.cmd == "collect":
        from .collector import Collector
        Collector().run()
    elif a.cmd == "reconstruct":
        from . import reconstruct
        if a.start:
            s = dt.date.fromisoformat(a.start)
            for k in range(a.days):
                reconstruct.run(s + dt.timedelta(days=k))
        else:
            reconstruct.run(dt.date.fromisoformat(a.date) if a.date else yesterday())
    elif a.cmd == "ridership":
        from . import ridership
        end = yesterday()
        ridership.fetch_hourly(end - dt.timedelta(days=a.days), end)
        ridership.fetch_exit_profile()
    elif a.cmd == "export":
        from . import export
        export.run(a.days, simulated=a.simulated)
    elif a.cmd == "nightly":
        from . import collector, export, reconstruct, ridership
        d = yesterday()
        reconstruct.run(d)
        try:
            # hourly ridership is published with a lag; fetch whatever exists
            ridership.fetch_hourly(d - dt.timedelta(days=a.days), d)
            if not (config.WORK / "ridership" / "exit_profile.parquet").exists():
                ridership.fetch_exit_profile()
        except Exception as e:
            logging.warning("ridership fetch failed: %s", e)
        export.run(a.days)
        collector.prune()
    elif a.cmd == "simulate":
        from . import sim
        start = dt.date.fromisoformat(a.start)
        sim.run(start, a.days)
        sim.simulate_ridership([start + dt.timedelta(days=k) for k in range(a.days)])
    else:
        ap.error(f"unknown command {a.cmd}")


if __name__ == "__main__":
    main()
