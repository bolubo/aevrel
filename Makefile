VENV ?= .venv
PY ?= $(VENV)/bin/python

# The coverage tripwire, in one place so `make check` and CI cannot drift
# apart. 70 is a catastrophe line, not a quality bar — see CONTRIBUTING.
# `make check COV=` skips it entirely for a fast local loop.
COV ?= --cov=aevrel --cov-report=term-missing --cov-fail-under=70

.PHONY: dev dev-native check help

## dev: local toolchain in one command (venv + editable install + Go build)
dev:
	@test -x $(PY) || python3 -m venv $(VENV)
	$(PY) -m pip install -U pip
	$(PY) -m pip install -e ".[dev,dcp]"
	go build ./...
	@echo "ready: . $(VENV)/bin/activate && aevrel demo --dcp"

## dev-native: also build the Rust hot path (enables native/aevrel-fast tests)
dev-native: dev
	cargo build --release --manifest-path native/aevrel-fast/Cargo.toml

## check: the same gates CI runs, runnable locally before you push
check:
	@test -x $(PY) || { echo "make check: no interpreter at $(PY) -- run 'make dev' first, or pass PY=<path>"; exit 1; }
	$(PY) scripts/check_repo.py
	$(PY) scripts/check_release.py --json
	$(PY) tests/acceptance/verify_line2_acceptance.py
	$(PY) tests/acceptance/verify_line3_acceptance.py
	$(PY) -m pytest -q -n auto --dist loadfile $(COV)
	go test ./...

help:
	@echo "make dev        create .venv, install editable package (dev + dcp extras), build Go"
	@echo "make dev-native dev + build the Rust hot path"
	@echo "make check      repo hygiene + release check + Python suite (with the coverage floor) + Go suite"
	@echo ""
	@echo "Overrides: VENV=<dir> (default .venv), PY=<python> (default \$$(VENV)/bin/python),"
	@echo "           COV= (empty to skip coverage, e.g. make check COV=)"
