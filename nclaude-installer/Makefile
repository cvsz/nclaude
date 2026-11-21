SHELL := /usr/bin/env bash
.DEFAULT_GOAL := all

.PHONY: all lint test feature install uninstall status logs clean ruff bandit shellcheck

all: lint test feature

lint:
	bash -n install.sh
	bash -n uninstall.sh
	python3 -m py_compile scripts/nim_registry.py tests/test_registry.py
	python3 -m json.tool config/models.json >/dev/null

ruff:
	python3 -m ruff check scripts/nim_registry.py tests/test_registry.py ../nclaude.py

bandit:
	python3 -m bandit -r scripts/ tests/ ../nclaude.py -f txt

shellcheck:
	shellcheck install.sh uninstall.sh

test:
	python3 -m unittest discover -s tests -v

feature: lint ruff bandit shellcheck test
	@echo "=== Running benchmark (model latency ranking) ==="
	python3 scripts/nim_registry.py benchmark --registry config/models.json --output /tmp/benchmark-results.json --max-tokens 64 --interval 1 || true

install:
	bash ./install.sh

uninstall:
	bash ./uninstall.sh

status:
	systemctl --user status nclaude.service --no-pager -l

logs:
	journalctl --user -u nclaude.service -f

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name '*.pyc' -delete
