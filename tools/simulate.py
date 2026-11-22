"""Simulate N service days of feeds in parallel: python tools/simulate.py 2026-11-07 14"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from concurrent.futures import ProcessPoolExecutor

from savant import sim


def one(args):
    start, k = args
    sim.run(start + dt.timedelta(days=k), 1, seed=100 + k)


if __name__ == "__main__":
    start = dt.date.fromisoformat(sys.argv[1])
    days = int(sys.argv[2])
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    with ProcessPoolExecutor(workers) as ex:
        list(ex.map(one, [(start, k) for k in range(days)]))
    sim.simulate_ridership([start + dt.timedelta(days=k) for k in range(days)])
    print("done")
