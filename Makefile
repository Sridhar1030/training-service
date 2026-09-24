SHELL := /bin/bash

UV ?= uv
OPENAPI_GENERATOR_VERSION ?= 7.25.0
CONTAINER_RUNTIME ?= $(shell command -v podman 2>/dev/null || command -v docker 2>/dev/null)
IMAGE ?= quay.io/opendatahub/training-service:latest

.PHONY: install test lint format-check typecheck check run generate-server helm-lint

install:
	$(UV) sync

test:
	$(UV) run pytest

lint:
	$(UV) run ruff check .

format-check:
	$(UV) run ruff format --check .

typecheck:
	$(UV) run mypy src

check: lint format-check typecheck test

run:
	$(UV) run training-service

generate-server:
	@test -n "$(CONTAINER_RUNTIME)" || (echo "podman or docker is required" >&2; exit 1)
	$(CONTAINER_RUNTIME) run --rm -v "$$(pwd):/local" \
		openapitools/openapi-generator-cli:v$(OPENAPI_GENERATOR_VERSION) generate \
		-i /local/api/openapi.yaml \
		-g python-fastapi \
		-o /local/generated/server \
		-c /local/api/openapi-generator-config.yaml

helm-lint:
	helm lint charts/training-service
