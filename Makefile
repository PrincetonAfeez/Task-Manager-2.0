.PHONY: help setup dev test test-fast coverage lint fmt audit check smoke ci \
        seed-demo reset-demo worker scheduler reaper drain \
        migrate makemigrations check-migrations backup restore backup-verify \
        collectstatic docker-build docker-up docker-down clean

PYTHON ?= python
PYTEST ?= $(PYTHON) -m pytest
MANAGE ?= $(PYTHON) manage.py

help:
	@echo "Task Manager - available targets"
	@echo "  setup            Install dependencies and apply migrations"
	@echo "  dev              Run the development server"
	@echo "  worker           Run a durable-queue worker (no broker required)"
	@echo "  scheduler        Run the schedule scanner loop"
	@echo "  reaper           Reclaim expired job leases once"
	@echo "  drain            Execute every runnable job once, then exit"
	@echo "  test             Full pytest suite"
	@echo "  coverage         Tests with a coverage report and an 85% floor"
	@echo "  lint             ruff check"
	@echo "  audit            pip-audit + bandit"
	@echo "  check            Django system checks, deploy-strict"
	@echo "  check-migrations Fail if a model change has no migration"
	@echo "  backup-verify    Back up, restore into a scratch DB, assert it is clean"
	@echo "  ci               Everything CI runs, locally"

setup:
	$(PYTHON) -m pip install -r requirements-dev.txt
	$(MANAGE) migrate

dev:
	$(MANAGE) runserver 127.0.0.1:8000

worker:
	$(MANAGE) run_worker

scheduler:
	$(MANAGE) run_scheduler

reaper:
	$(MANAGE) reap_leases

drain:
	$(MANAGE) run_worker --once

test:
	DJANGO_ENV=test $(PYTEST)

test-fast:
	DJANGO_ENV=test $(PYTEST) -x -q --no-header

coverage:
	DJANGO_ENV=test $(PYTEST) --cov=. --cov-report=term-missing --cov-report=html --cov-fail-under=85

lint:
	$(PYTHON) -m ruff check .

fmt:
	$(PYTHON) -m ruff format .

audit:
	-$(PYTHON) -m pip_audit -r requirements.txt
	$(PYTHON) -m bandit -q -r . -x ./tests,./.venv,./staticfiles --severity-level medium

check:
	$(MANAGE) check
	DJANGO_ENV=production \
	DJANGO_SECRET_KEY=ci-only-not-a-real-secret-000000000000000000 \
	DJANGO_ALLOWED_HOSTS=example.com \
	DB_ENGINE=django.db.backends.postgresql \
	REDIS_URL=redis://localhost:6379/0 \
	$(MANAGE) check --deploy --fail-level WARNING

check-migrations:
	$(MANAGE) makemigrations --check --dry-run

migrate:
	$(MANAGE) migrate

makemigrations:
	$(MANAGE) makemigrations

smoke: check check-migrations test

ci: lint check check-migrations coverage audit

seed-demo:
	$(MANAGE) seed_demo

reset-demo:
	$(MANAGE) flush --no-input
	$(MANAGE) migrate
	$(MANAGE) seed_demo

backup:
	$(MANAGE) backup_db

restore:
	@echo "Usage: $(MANAGE) restore_db <path-to-backup> --confirm"

# An untested backup is not a backup. This performs a real round trip.
backup-verify:
	$(MANAGE) backup_db --verify

collectstatic:
	$(MANAGE) collectstatic --noinput

docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-down:
	docker compose down

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache htmlcov .coverage
