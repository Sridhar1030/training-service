"""FastAPI application entry point."""

from fastapi import FastAPI


def create_app() -> FastAPI:
    """Create the HTTP application."""
    application = FastAPI(
        title="OpenDataHub Training Service",
        version="0.1.0",
        description="API for submitting and managing distributed training jobs.",
    )

    @application.get("/healthz", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/readyz", include_in_schema=False)
    def ready() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()


def run() -> None:
    """Run the service locally."""
    import uvicorn

    uvicorn.run("training_service.main:app", host="0.0.0.0", port=8080)
