# Developer workflow. Every target is a thin wrapper around docker compose or uv, so the stack works
# just as well without make: `make help` lists the targets, `make -n <target>` prints the command.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help
MAKEFLAGS += --no-print-directory

-include .env
HTTP_PORT ?= 8080
COMPOSE ?= docker compose
UV ?= uv
ALL_PROFILES := --profile observability --profile load

.PHONY: help up down destroy ps logs observe load smoke drill audit-broker ingest-token \
	test test-unit lint typecheck fmt web-dev

help: ## Show this list
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / {printf "  \033[1m%-13s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

up: ## Build and start the stack; returns once every service is healthy
	$(COMPOSE) up -d --build --wait

down: ## Stop everything, keep data and secrets
	$(COMPOSE) $(ALL_PROFILES) down --remove-orphans

destroy: ## Stop everything and delete all volumes: data, broker store and secrets
	$(COMPOSE) $(ALL_PROFILES) down --volumes --remove-orphans

ps: ## Containers and their health
	$(COMPOSE) $(ALL_PROFILES) ps --all

logs: ## Follow logs (SERVICE=api to narrow it down)
	$(COMPOSE) $(ALL_PROFILES) logs --follow --tail=200 $(SERVICE)

observe: ## Start with Prometheus (:9090) and Jaeger (:16686); api and engine export traces
	OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318 $(COMPOSE) --profile observability up -d --build --wait

load: ## Drive 10,000 simulated devices through the edge (ARGS="--devices 20000 --transport ws")
	$(COMPOSE) --profile load run --rm generator $(ARGS)

smoke: ## End-to-end check through the edge: sign-in, zone, ingest, live alert and position
	$(COMPOSE) exec -T api cat /run/secrets/ingest_token \
		| $(UV) run python scripts/smoke.py --base-url http://127.0.0.1:$(HTTP_PORT) --ingest-token-file -

drill: ## Kill a replica under load, then prove no loss or duplicates (KILL=engine|api|none)
	$(UV) run python scripts/drill.py --kill $(or $(KILL),engine) $(ARGS)

audit-broker: ## Fail if the broker refused anything: any "Violation" in its log
	@log="$$($(COMPOSE) logs --no-color nats)"; \
	if grep -F "Violation" <<<"$$log"; then exit 1; fi; \
	echo "no permission or authorization violations in the broker log"

ingest-token: ## Copy the device ingest token to .secrets/ingest_token (git-ignored)
	@mkdir -p .secrets && umask 077 \
		&& $(COMPOSE) exec -T api cat /run/secrets/ingest_token > .secrets/ingest_token
	@echo ".secrets/ingest_token (uv run generator.py --token-file .secrets/ingest_token)"

test: ## All Python tests; integration tests start PostGIS and NATS containers
	$(UV) run pytest $(ARGS)

test-unit: ## Unit tests only (no Docker needed)
	$(UV) run pytest tests/unit $(ARGS)

lint: ## Ruff lint and format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

typecheck: ## mypy, strict
	$(UV) run mypy

fmt: ## Format and apply safe fixes
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

web-dev: ## Dashboard with hot reload (Vite) against the running stack
	cd web && { [ -d node_modules ] || npm ci; } && npm run dev
