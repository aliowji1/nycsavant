"""Paths, feed URLs and tunable constants. Override paths with SAVANT_DATA."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path):
    """Read KEY=value lines from .env into the environment (real env vars win)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")

DATA = Path(os.environ.get("SAVANT_DATA", ROOT / "data"))
RAW = DATA / "raw"            # raw feed snapshots: raw/<date>/<feed>/<HHMMSS>.pb.gz
STATIC = DATA / "static"      # GTFS static: static/subway, static/bus
WORK = DATA / "work"          # parquet intermediates
SITE_DATA = Path(os.environ.get("SAVANT_SITE_DATA", ROOT / "site" / "data"))

TZ = "America/New_York"

# MTA subway GTFS-realtime feeds (no API key needed since 2023).
SUBWAY_FEEDS = {
    "ace": "nyct%2Fgtfs-ace",
    "bdfm": "nyct%2Fgtfs-bdfm",
    "g": "nyct%2Fgtfs-g",
    "jz": "nyct%2Fgtfs-jz",
    "nqrw": "nyct%2Fgtfs-nqrw",
    "l": "nyct%2Fgtfs-l",
    "1234567": "nyct%2Fgtfs",
    "si": "nyct%2Fgtfs-si",
}
SUBWAY_FEED_BASE = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/"

# Bus GTFS-realtime (needs a free BusTime key: https://register.developer.obanyc.com/)
BUS_FEEDS = {
    "bus_vehicles": "https://gtfsrt.prod.obanyc.com/vehiclePositions?key={key}",
}
BUSTIME_KEY = os.environ.get("BUSTIME_KEY", "")

# Static GTFS downloads (refreshed weekly by `savant static`)
STATIC_URLS = {
    "subway": "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip",
    # Regular schedule plus planned service changes (weekend work etc.) for the
    # next ~7 days. Fetched daily and kept per date, see gtfs_static.fetch_supplemented.
    "subway_supplemented": "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_supplemented.zip",
    "bus_m": "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_m.zip",
    "bus_bx": "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_bx.zip",
    "bus_b": "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_b.zip",
    "bus_q": "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_q.zip",
    "bus_si": "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_si.zip",
    "bus_busco": "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_busco.zip",
}

POLL_SECONDS = 30
SUPPLEMENTED_KEEP_DAYS = 14

# Ridership (NY Open Data, Socrata). Hourly entries by station complex, and
# origin-destination estimates (used to estimate exits).
RIDERSHIP_HOURLY = "https://data.ny.gov/resource/5wq4-mkjj.json"
RIDERSHIP_OD = "https://data.ny.gov/resource/y2qv-fytt.json"

# ---- metric definitions ----
# "On schedule" at the terminal, following MTA's published standards:
#   subway (terminal on-time performance): no more than 5 min late; early is fine
#   bus (on-time performance): no more than 1 min early or 5 min late
ON_SCHED = {"subway": (10 ** 6, 300), "bus": (60, 300)}   # (max early s, max late s)
# Replacement level: the on-schedule rate of the vehicle at this percentile of
# its line (among vehicles with enough trips).
REPLACEMENT_PCTL = 0.20
MIN_TRIPS_FOR_RANK = 20
