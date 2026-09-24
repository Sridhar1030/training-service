# API contract

openapi.yaml is the source of truth for the public Training Service HTTP
contract.

The repository uses the OpenAPI Generator python-fastapi server generator.
Generated output is deliberately kept separate from handwritten orchestration
and adapters. Regenerate it with:

    make generate-server

The generator is currently beta. Review generated changes as part of the pull
request that changes the API contract.
