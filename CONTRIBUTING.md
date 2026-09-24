# Contributing

1. Keep api/openapi.yaml as the source of truth for HTTP contract changes.
2. Regenerate the FastAPI server when the contract changes with
   make generate-server.
3. Keep business logic out of generated files.
4. Add focused unit tests for changed behavior.
5. Run make check before opening a pull request.

Changes that require Kubernetes permissions, new Helm values, or a new backend
adapter should document the operational impact in the pull request.
