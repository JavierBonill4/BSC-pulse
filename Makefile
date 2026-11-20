.PHONY: help setup lint format test check probe run metrics up down logs notify telegram-test rules-test alert-test dashboard fake-rpc demo demo-break demo-heal demo-status
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

dashboard:       ## Rebuild the Grafana dashboard JSON from deploy/grafana/build_dashboard.py
	@$(PY) deploy/grafana/build_dashboard.py

fake-rpc:        ## Run the fake endpoints locally on ports 18545-18547 (no Docker)
	@$(PY) -m bsc_pulse.fake_rpc

DEMO_CONFIG := config/endpoints.yaml,config/endpoints.demo.yaml

demo:            ## Start the stack plus fake endpoints you can break (network "demo")
	BSC_PULSE_CONFIG=$(DEMO_CONFIG) docker compose --profile demo up -d --build

MODE ?= stall
demo-break:      ## Break the third fake endpoint: make demo-break MODE=stall|lag|slow|error|ratelimit|fork|wrong_chain|syncing|timeout
	@curl -s "http://127.0.0.1:18547/control?mode=$(MODE)" | python3 -c 'import json,sys; s=json.load(sys.stdin); print("fake-rpc:18547 is now", s["mode"])'

demo-heal:       ## Make every fake endpoint healthy again
	@for port in 18545 18546 18547; do curl -s -o /dev/null "http://127.0.0.1:$$port/control?mode=healthy"; done; echo "all fake endpoints healthy"

demo-status:     ## Show what each fake endpoint is doing
	@for port in 18545 18546 18547; do printf "fake-rpc:%s  " $$port; curl -s "http://127.0.0.1:$$port/control" | python3 -c 'import json,sys; print(json.load(sys.stdin)["mode"])'; done
