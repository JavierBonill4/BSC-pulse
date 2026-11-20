.PHONY: help setup lint format test check probe run metrics up down logs notify telegram-test rules-test alert-test
PY := .venv/bin/python

help:            ## Show this list
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/ -/'

setup:           ## Create .venv and install dependencies
	@python3 -m venv .venv
	@$(PY) -m pip install --quiet --upgrade pip
	@$(PY) -m pip install --quiet -r requirements-dev.txt

lint:            ## Lint and format check (same as CI)
	@$(PY) -m ruff check .
	@$(PY) -m ruff format --check .

format:          ## Auto-format and apply safe lint fixes
	@$(PY) -m ruff format .
	@$(PY) -m ruff check --fix .

test:            ## Run the unit tests (no network needed)
	@$(PY) -m pytest -q

check: lint test ## Everything CI runs

probe:           ## One live sweep of every endpoint in config/endpoints.yaml
	@$(PY) -m bsc_pulse.probe

run:             ## Run the watchdog locally; metrics on http://127.0.0.1:9105/metrics
	@$(PY) -m bsc_pulse.main

metrics:         ## Show the current bsc_* metrics from a running watchdog
	@curl -s http://127.0.0.1:9105/metrics | grep -E '^bsc_(endpoint_(up|lag_blocks|head_age)|network)'

up:              ## Start the Docker Compose stack
	docker compose up -d --build

down:            ## Stop the stack (data volumes are kept)
	docker compose down

logs:            ## Follow the watchdog's log
	docker compose logs -f bsc-pulse

notify:          ## Run the notifier locally (dry run unless TELEGRAM_* are set)
	@$(PY) -m bsc_pulse.notifier

telegram-test:   ## Send one sample incident note to Telegram (needs TELEGRAM_* set)
	@$(PY) -m bsc_pulse.notifier --sample

PROMTOOL := docker run --rm -v "$(CURDIR)/deploy/prometheus:/etc/prometheus:ro" --entrypoint promtool prom/prometheus:v3.5.0

rules-test:      ## Check the alert rules and run their unit tests (uses Docker)
	$(PROMTOOL) check config /etc/prometheus/prometheus.yml
	$(PROMTOOL) test rules /etc/prometheus/tests/bsc_test.yml

alert-test:      ## Post a fake alert to a running notifier (make up first)
	@curl -s -X POST http://127.0.0.1:9106/alert -H 'Content-Type: application/json' \
	  --data '{"alerts":[{"status":"firing","labels":{"alertname":"BscEndpointLagging","severity":"warning","network":"mainnet","endpoint":"bsc-dataseed.bnbchain.org"},"annotations":{"summary":"TEST: manual alert from make alert-test"}}]}'; echo
