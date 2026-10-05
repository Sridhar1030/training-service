# Server scaffold structure

```text
training-service/
├── docs/api/
│   ├── openapi.yaml                   # reviewed contract, unchanged
│   ├── openapi-generator-config.yaml # python-fastapi settings
│   ├── format_generated.py           # value-preserving string wrapping
│   └── templates/python-fastapi/     # reproducible generator overrides
├── src/training_service_api/          # generated, committed; do not edit
├── src/training_service/
│   ├── main.py                       # generated routers, Swagger, probes
│   ├── api_impl/                     # future handwritten implementations
│   ├── contract.py                   # canonical model validation and Swagger
│   ├── contract_types.py             # scalar URI generator workaround
│   └── errors.py                     # API error envelope
├── tests/unit/                       # startup/routes, models, formatting
└── Containerfile                     # packages both Python namespaces
```

Only one FastAPI application runs. Generated routers and models form a
library imported by the handwritten entry point; the wheel and container
bundle both namespaces. Generation never overwrites handwritten source.

The application registers the contract's eight operations under `/api/v1`.
Authentication and all API operations are unimplemented, fail closed with
`501`, and perform no backend calls. `/healthz`, `/readyz`, `/docs`, and
`/openapi.json` are available without a cluster.

## Ownership boundary

Routes, request/response models, credential interfaces, and formatting are
generated. Canonical validation fills generator gaps without changing the
contract. Authentication, project authorization, Kubernetes discovery,
CodeFlare/Ray calls, and Helm deployment behavior belong to separate tasks
and remain outside generated code.

The runtime Swagger copy changes only the server URL and documents the
scaffold's `501` responses. The reviewed YAML remains the source of truth.
