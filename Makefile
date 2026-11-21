SHELL := /usr/bin/env bash
.DEFAULT_GOAL := all

.PHONY: all lint test feature install uninstall status logs clean ruff bandit shellcheck

all: lint test feature

lint test feature install uninstall status logs clean ruff bandit shellcheck:
	$(MAKE) -C nclaude-installer $@
