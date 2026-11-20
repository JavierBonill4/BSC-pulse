.PHONY: help setup lint format test check probe run metrics up down logs notify telegram-test rules-test alert-test dashboard fake-rpc demo demo-break demo-heal demo-status tf-init tf-plan tf-apply tf-destroy ansible-deps ping deploy deploy-demo tunnel ssh
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

# ---- Deploying to AWS (Terraform creates the server, Ansible configures it) ----

TF := terraform -chdir=infra/terraform
# Load .env into the environment so Ansible can read the secrets from it.
LOAD_ENV := set -a; [ -f .env ] && . ./.env; set +a;
INVENTORY := infra/ansible/inventory/hosts.ini
SERVER = $$(sed -n 's/.*ansible_host=\([^ ]*\).*/\1/p' $(INVENTORY) | head -1)

tf-init:         ## Terraform: download providers (once)
	$(TF) init

tf-plan:         ## Terraform: show what would be created
	$(TF) plan

tf-apply:        ## Terraform: create the AWS server (costs money while it runs)
	$(TF) apply

tf-destroy:      ## Terraform: DELETE the server. Run this when you are done!
	$(TF) destroy

ansible-deps:    ## Install the Ansible collections the playbook needs (once)
	cd infra/ansible && ansible-galaxy collection install -r requirements.yml

ping:            ## Can Ansible reach the server?
	cd infra/ansible && ansible all -m ping

deploy:          ## Harden the server and deploy the stack (secrets come from .env)
	@$(LOAD_ENV) cd infra/ansible && ansible-playbook site.yml

deploy-demo:     ## Same, plus the fake endpoints, for a live demo on the server
	@$(LOAD_ENV) cd infra/ansible && ansible-playbook site.yml -e bsc_pulse_demo=true

tunnel:          ## Forward the server's UIs to this machine (leave running, Ctrl+C to stop)
	@echo "Grafana http://127.0.0.1:3000  Prometheus :9090  Alertmanager :9093  (Ctrl+C to stop)"
	ssh -N -L 3000:127.0.0.1:3000 -L 9090:127.0.0.1:9090 -L 9093:127.0.0.1:9093 \
	  -L 18547:127.0.0.1:18547 ubuntu@$(SERVER)

ssh:             ## Open a shell on the server (the stack is in /opt/bsc-pulse)
	ssh ubuntu@$(SERVER)
