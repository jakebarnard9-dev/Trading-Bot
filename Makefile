.PHONY: install test replay doctor preview run
install:
	python3 -m venv .venv && . .venv/bin/activate && pip install -U pip && pip install -r requirements.txt && pip install -e .
test:
	. .venv/bin/activate && python -m pytest -q
replay:
	. .venv/bin/activate && python -m earnings_bot replay
doctor:
	. .venv/bin/activate && python -m earnings_bot doctor
preview:
	. .venv/bin/activate && python -m earnings_bot preview --deep --write THIS_WEEK_LIVE.md
run:
	. .venv/bin/activate && python -m earnings_bot run
