PY ?= python3

.PHONY: install static collect nightly export serve demo clean-demo

install:
	$(PY) -m pip install -r requirements.txt

static:            ## download MTA static GTFS and build lookup tables
	$(PY) -m savant static

collect:           ## poll realtime feeds forever (Ctrl-C to stop)
	$(PY) -m savant collect

nightly:           ## reconstruct yesterday, refresh ridership, rebuild site data
	$(PY) -m savant nightly

export:
	$(PY) -m savant export

serve:             ## preview the site at http://localhost:8000
	cd site && $(PY) -m http.server 8000

demo:              ## simulated two weeks, end to end, no network needed after `static`
	$(PY) tools/simulate.py 2026-11-07 14 2
	$(PY) -m savant reconstruct --start 2026-11-07 --days 14
	$(PY) -m savant export --days 14 --simulated

clean-demo:
	rm -rf data/raw data/work/arrivals data/work/truth data/work/ridership site/data
