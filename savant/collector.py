"""Poll MTA realtime feeds and archive every snapshot.

Layout: data/raw/<YYYY-MM-DD>/<feed>/<HHMMSS>.pb.gz  (date/time in New York time,
taken from the feed header timestamp so identical snapshots are skipped).

Roughly 0.45 GB/day gzipped at 30 s (about 0.27 GB subway, 0.17 GB citywide buses). The
nightly job only needs the last ~2 days of raw data; prune older days with
`savant prune` once they have been reconstructed.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import gzip
import logging
import time
from zoneinfo import ZoneInfo

import requests
from google.transit import gtfs_realtime_pb2

from . import config

log = logging.getLogger("collector")
NY = ZoneInfo(config.TZ)


def feed_urls() -> dict[str, str]:
    urls = {k: config.SUBWAY_FEED_BASE + v for k, v in config.SUBWAY_FEEDS.items()}
    if config.BUSTIME_KEY:
        urls.update({k: u.format(key=config.BUSTIME_KEY) for k, u in config.BUS_FEEDS.items()})
    else:
        log.warning("BUSTIME_KEY not set: bus feeds will not be collected")
    return urls


class Collector:
    def __init__(self):
        self.session = requests.Session()
        self.last_ts: dict[str, int] = {}

    def fetch(self, name: str, url: str) -> str:
        r = self.session.get(url, timeout=20)
        r.raise_for_status()
        msg = gtfs_realtime_pb2.FeedMessage()
        msg.ParseFromString(r.content)            # validate before saving
        ts = msg.header.timestamp or int(time.time())
        if self.last_ts.get(name) == ts:
            return "dup"
        self.last_ts[name] = ts
        t = dt.datetime.fromtimestamp(ts, NY)
        d = config.RAW / t.strftime("%Y-%m-%d") / name
        d.mkdir(parents=True, exist_ok=True)
        with gzip.open(d / f"{t:%H%M%S}.pb.gz", "wb", compresslevel=6) as f:
            f.write(r.content)
        return "ok"

    def poll_once(self, urls: dict[str, str]):
        with cf.ThreadPoolExecutor(max_workers=len(urls)) as ex:
            futs = {ex.submit(self.fetch, n, u): n for n, u in urls.items()}
            for fut in cf.as_completed(futs):
                n = futs[fut]
                try:
                    fut.result()
                except Exception as e:  # keep going; one bad feed shouldn't stop the rest
                    msg = str(e)
                    if config.BUSTIME_KEY:
                        msg = msg.replace(config.BUSTIME_KEY, "***")   # keep the key out of logs
                    log.warning("%s: %s", n, msg)

    def run(self, interval: int = config.POLL_SECONDS):
        urls = feed_urls()
        log.info("polling %d feeds every %ss into %s", len(urls), interval, config.RAW)
        while True:
            start = time.monotonic()
            self.poll_once(urls)
            time.sleep(max(0.0, interval - (time.monotonic() - start)))


def prune(keep_days: int = 3):
    """Delete raw days older than keep_days that already have reconstructed output."""
    import shutil
    cutoff = dt.date.today() - dt.timedelta(days=keep_days)
    for d in sorted(config.RAW.glob("????-??-??")):
        day = dt.date.fromisoformat(d.name)
        done = (config.WORK / "arrivals" / f"subway_{d.name}.parquet").exists()
        if day < cutoff and done:
            shutil.rmtree(d)
            log.info("pruned %s", d)
