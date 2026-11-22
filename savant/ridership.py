"""Ridership from NY Open Data (Socrata).

- Hourly entries by station complex (OMNY + MetroCard taps), aggregated over
  payment methods. Writes work/ridership/hourly_entries.parquet.
- Estimated exits by hour: the origin-destination estimate summed by
  destination, giving a typical weekday/weekend exit profile per complex.
  Writes work/ridership/exit_profile.parquet.

Set SOCRATA_APP_TOKEN to avoid throttling (free at data.ny.gov).
"""
from __future__ import annotations

import datetime as dt
import os

import pandas as pd
import requests

from . import config

PAGE = 50000


def _get(url: str, params: dict) -> list[dict]:
    headers = {}
    if os.environ.get("SOCRATA_APP_TOKEN"):
        headers["X-App-Token"] = os.environ["SOCRATA_APP_TOKEN"]
    rows, offset = [], 0
    while True:
        p = dict(params, **{"$limit": PAGE, "$offset": offset})
        r = requests.get(url, params=p, headers=headers, timeout=120)
        r.raise_for_status()
        batch = r.json()
        rows += batch
        if len(batch) < PAGE:
            return rows
        offset += PAGE


def fetch_hourly(start: dt.date, end: dt.date):
    rows = _get(config.RIDERSHIP_HOURLY, {
        "$select": "station_complex_id, station_complex, latitude, longitude, transit_timestamp, "
                   "sum(ridership) as ridership",
        "$where": f"transit_mode='subway' AND transit_timestamp >= '{start}T00:00:00' "
                  f"AND transit_timestamp < '{end + dt.timedelta(days=1)}T00:00:00'",
        "$group": "station_complex_id, station_complex, latitude, longitude, transit_timestamp",
        "$order": "transit_timestamp",
    })
    df = pd.DataFrame(rows)
    df["ridership"] = df.ridership.astype(float).round()
    df["latitude"], df["longitude"] = df.latitude.astype(float), df.longitude.astype(float)
    out = config.WORK / "ridership"
    out.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out / "hourly_entries.parquet", index=False)
    return df


def fetch_exit_profile():
    rows = _get(config.RIDERSHIP_OD, {
        "$select": "destination_station_complex_id, day_of_week, hour_of_day, "
                   "sum(estimated_average_ridership) as exits",
        "$group": "destination_station_complex_id, day_of_week, hour_of_day",
    })
    df = pd.DataFrame(rows)
    df["exits"] = df.exits.astype(float)
    df["daytype"] = df.day_of_week.isin(["Saturday", "Sunday"]).map({True: "weekend", False: "weekday"})
    prof = (df.groupby(["destination_station_complex_id", "daytype", "hour_of_day"]).exits.mean()
            .reset_index().rename(columns={"destination_station_complex_id": "station_complex_id",
                                           "hour_of_day": "hour"}))
    prof["hour"] = prof.hour.astype(int)
    prof.to_parquet(config.WORK / "ridership" / "exit_profile.parquet", index=False)
    return prof
