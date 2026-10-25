VENV ?= .venv
PY ?= $(VENV)/bin/python

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
	$(PY) -m pytest -q
	go test ./...

help:
	@echo "make dev        create .venv, install editable package (dev + dcp extras), build Go"
	@echo "make dev-native dev + build the Rust hot path"
	@echo "make check      repo hygiene + release check + Python suite + Go suite"
	@echo ""
	@echo "Overrides: VENV=<dir> (default .venv), PY=<python> (default \$$(VENV)/bin/python)"
