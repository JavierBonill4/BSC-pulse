.PHONY: help setup lint format test check probe run metrics up down logs
PY := .venv/bin/python

help:            ## Show this list
	grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/ -/'

setup:           ## Create .venv and install dependencies
	python3 -m venv .venv
	$(PY) -m pip install --quiet --upgrade pip
	$(PY) -m pip install --quiet -r requirements-dev.txt

lint:            ## Lint and format check (same as CI)
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

format:          ## Auto-format and apply safe lint fixes
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix .

test:            ## Run the unit tests (no network needed)
	$(PY) -m pytest -q

check: lint test ## Everything CI runs

probe:           ## One live sweep of every endpoint in config/endpoints.yaml
	$(PY) -m bsc_pulse.probe

run:             ## Run the watchdog locally; metrics on http://127.0.0.1:9105/metrics
	@$(PY) -m bsc_pulse.main

metrics:         ## Show the current bsc_* metrics from a running watchdog
	@curl -s http://127.0.0.1:9105/metrics | grep -E '^bsc_(endpoint_(up|lag_blocks|head_age)|network)'

up:              ## Start the Docker Compose stack (watchdog + Prometheus)
	docker compose up -d --build

down:            ## Stop the stack (data volumes are kept)
	docker compose down

logs:            ## Follow the watchdog's log
	docker compose logs -f bsc-pulse
