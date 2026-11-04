# Shortcuts for the docker-compose stack. `make help` lists them.

.DEFAULT_GOAL := help
.PHONY: help up down clean logs ps smoke traffic test lint

help: ## list the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  make %-8s %s\n", $$1, $$2}'

up: ## build and start the whole stack, wait until healthy
	docker compose up -d --build --wait
	@echo
	@echo "  gateway     http://localhost:8000   (X-API-Key: gw_dev_local_only)"
	@echo "  grafana     http://localhost:3000   (dashboard, no login)"
	@echo "  prometheus  http://localhost:9090"

down: ## stop the stack
	docker compose down

clean: ## stop the stack and delete its images and volumes
	docker compose down --volumes --rmi local

logs: ## follow the gateway logs
	docker compose logs -f gateway

ps: ## show what is running and healthy
	docker compose ps

smoke: ## end-to-end check of the running stack
	./scripts/smoke_test.sh

traffic: ## 2 minutes of mixed traffic, to fill the dashboard
	python3 scripts/traffic.py --seconds 120

test: ## unit tests
	pytest -q

lint: ## ruff
	ruff check . && ruff format --check .
