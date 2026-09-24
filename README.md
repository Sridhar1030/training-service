# OpenDataHub Training Service

OpenDataHub Training Service exposes a single API for submitting and managing
distributed training jobs in an OpenShift AI environment.

The service is Python-based and uses FastAPI. The public contract is maintained
in api/openapi.yaml; the server implementation is generated with OpenAPI
Generator's python-fastapi generator and kept separate from handwritten
application and backend adapters.

## Current repository state

This initial commit provides the runnable service shell, health probes, API
contract, generation command, unit-test setup, and installable Helm chart.
Training-job behavior will be added behind the generated routes.

The planned backend boundary is:

- Kubernetes/CodeFlare for project scope, permissions, Ray/Trainer resources,
  and Kueue placement.
- Kubernetes APIs directly for algorithm discovery and Kueue queue discovery.
- Ray Jobs submission, through its Python client or equivalent REST adapter, for
  submitting work to an existing Ray cluster once that integration is finalized.

See docs/architecture.md for the structure and ownership rules, and
docs/installation.md for Helm installation.

## Local development

Requirements: Python 3.11+ and uv.

    make install
    make check
    make run

The local service listens on http://localhost:8080. The health endpoint is
available at http://localhost:8080/healthz.
