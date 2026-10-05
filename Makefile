SHELL := /bin/bash

UV ?= uv
OPENAPI_GENERATOR_VERSION ?= 7.25.0
CONTAINER_RUNTIME ?= $(shell command -v podman 2>/dev/null || command -v docker 2>/dev/null)
IMAGE ?= quay.io/opendatahub/training-service:latest

.PHONY: install test lint format format-check typecheck check run generate-server helm-lint

install: generate-server
	$(UV) sync

test:
	$(UV) run pytest

lint:
	$(UV) run ruff check .

format:
	$(UV) run ruff check --fix --select I .
	$(UV) run ruff format .

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
		-i /local/docs/api/openapi.yaml \
		-g python-fastapi \
		-o /local \
		-c /local/docs/api/openapi-generator-config.yaml \
		-t /local/docs/api/templates/python-fastapi \
		--schema-mappings TrainingInputURI=TrainingInputURI \
		--import-mappings 'TrainingInputURI=from training_service.contract_types import TrainingInputURI' \
		--global-property 'apis,models,supportingFiles=security_api.py:extra_models.py:__init__.py,apiTests=false,modelTests=false,apiDocs=false,modelDocs=false'
	$(UV) run --frozen python docs/api/format_generated.py src/training_service_api
	$(UV) run --frozen ruff check --fix \
		--select I,F401,UP src/training_service_api
	$(UV) run --frozen ruff format src/training_service_api
	$(UV) run --frozen ruff check src/training_service_api

helm-lint:
	helm lint charts/training-service
