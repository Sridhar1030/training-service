# Initial structure

The service is intentionally split into generated HTTP plumbing and handwritten
application behavior:

    training-service/
    ├── api/
    │   ├── openapi.yaml                    # public contract
    │   └── openapi-generator-config.yaml  # python-fastapi generation settings
    ├── charts/training-service/            # installable Helm chart
    ├── docs/
    │   ├── architecture.md
    │   └── installation.md
    ├── src/training_service/
    │   ├── main.py                         # application composition and probes
    │   ├── api_impl/                       # implementations called by generated routes
    │   ├── application/                    # job lifecycle use cases
    │   ├── domain/                         # API-facing models and status mapping
    │   └── adapters/
    │       ├── kubernetes/                 # K8s APIs, Kueue, and Trainer resources
    │       └── ray/                        # Ray job submission/status/log adapters
    ├── tests/unit/                         # focused unit tests only
    ├── Containerfile
    ├── Makefile
    └── pyproject.toml

## Endpoint ownership

- Training-job creation and lifecycle operations use the Kubernetes/CodeFlare
  layer for project scope, RBAC, Ray/Trainer resources, and Kueue placement.
- GET /algorithms and GET /projects/{project}/queues use Kubernetes APIs
  directly; they are not Ray SDK operations.
- POST /training-jobs/estimate remains a pure application-domain calculation.
- Ray submission is isolated behind the Ray adapter. The initial seam can use
  ray.job_submission.JobSubmissionClient (the Python wrapper around the Ray
  Jobs REST API), while Kubernetes/CodeFlare remains responsible for cluster
  and scheduling concerns.

The generated package must not contain Kubernetes calls or business rules.
Those belong in the application and adapter layers so the OpenAPI contract can
change without coupling the service to a particular execution backend.

## Explicit non-goals for the initial repository

- No Helm test suite.
- No end-to-end cluster test matrix.
- No demo or feedback workflow.
- No decision to replace the Kubernetes/CodeFlare layer with the Ray Jobs API.
