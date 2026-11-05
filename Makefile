# Короткие команды для разработки и запуска.
# Windows: используйте python напрямую (make не входит в поставку).

PYTHON ?= python

.PHONY: help install dev check test run once docker-build docker-up docker-logs docker-down

help:
	@echo "make install      — установить зависимости"
	@echo "make dev          — установить зависимости и тесты"
	@echo "make check        — проверить настройки, почту, базу и модель"
	@echo "make test         — запустить автотесты"
	@echo "make run          — запустить службу"
	@echo "make once         — один проход по ящику"
	@echo "make docker-build — собрать образ"
	@echo "make docker-up    — поднять контейнер"
	@echo "make docker-logs  — логи контейнера"
	@echo "make docker-down  — остановить контейнер"

install:
	$(PYTHON) -m pip install -r requirements.txt

dev:
	$(PYTHON) -m pip install -r requirements-dev.txt

check:
	$(PYTHON) main.py --check

test:
	$(PYTHON) -m pytest

run:
	$(PYTHON) main.py

once:
	$(PYTHON) main.py --once

docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-logs:
	docker compose logs -f

docker-down:
	docker compose down
