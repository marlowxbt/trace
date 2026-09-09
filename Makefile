.PHONY: help test guard watchlist collect dry check desk export cohort callers clean

PY ?= python3

help:
	@echo "TRACE - read-only terminal for Robinhood Chain"
	@echo
	@echo "  make test        142 offline tests. No network, no key, no cost."
	@echo "  make guard       prove no signing or broadcasting call exists"
	@echo "  make watchlist   pick the ten tokens worth paying for"
	@echo "  make dry         every query and the exact bill, without a request"
	@echo "  make check       prove the key works, for one empty call"
	@echo "  make collect     run the collector (spends money)"
	@echo "  make desk        the live desk on http://127.0.0.1:8080"
	@echo "  make export      a public JSON snapshot with posts"
	@echo "  make cohort      the attention-by-age curve"
	@echo "  make callers     who keeps showing up, and how early"
	@echo
	@echo "The collector needs a key in the environment, never in a file:"
	@echo "  export TRACE_X_BEARER='...'"

test:
	$(PY) -m unittest discover -s tests -t . -q

guard:
	$(PY) scripts/no_signer.py

watchlist:
	$(PY) -m trace.watchlist

dry:
	$(PY) -m trace.collector --dry-run

check:
	$(PY) -m trace.collector --check

collect:
	$(PY) -m trace.collector

desk:
	$(PY) -m trace.serve

export:
	$(PY) -m trace.export --with-posts --out web/public.json

cohort:
	$(PY) -m trace.cohort

callers:
	$(PY) -m trace.callers

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	find . -name '*.pyc' -delete
