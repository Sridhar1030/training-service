"""Check Hugging Face references before creating a RayJob, without downloading data."""

from __future__ import annotations

from datasets import ReadInstruction, load_dataset_builder
from datasets.exceptions import DatasetNotFoundError
from huggingface_hub import HfApi
from huggingface_hub.errors import GatedRepoError, HfHubHTTPError, RepositoryNotFoundError

from training_service.errors import ApiError


def _hub_error(error: Exception, field: str) -> ApiError:
    """Give actionable, credential-safe errors for metadata requests."""
    if isinstance(error, GatedRepoError):
        return ApiError(
            422,
            "validation_error",
            f"{field} points to a gated Hugging Face repository; "
            "grant the Secret token access first.",
        )
    if isinstance(error, RepositoryNotFoundError):
        return ApiError(
            422,
            "validation_error",
            f"{field} Hugging Face repository was not found or is inaccessible; "
            "check the repository ID and token permissions.",
        )
    if isinstance(error, HfHubHTTPError) and error.response.status_code in {400, 401, 403, 404}:
        return ApiError(
            422,
            "validation_error",
            f"{field} Hugging Face repository or Secret token was rejected; "
            "check the repository ID and token permissions.",
        )
    return ApiError(
        503,
        "submission_unavailable",
        f"{field} could not be checked with Hugging Face; try again later.",
    )


def validate_hf_model(repository: str, token: str) -> None:
    """Confirm the configured token can see the model repository."""
    try:
        HfApi().model_info(repository, token=token, timeout=10)
    except Exception as error:
        raise _hub_error(error, "model.uri") from error


def validate_hf_dataset(
    repository: str,
    config: str | None,
    split: str,
    token: str,
    *,
    remote_loader: bool,
) -> set[str] | None:
    """Check repository access, config/split and return known column metadata."""
    if remote_loader:
        # GRPO passes the repository ID to its own loader, not datasets.load_dataset.
        try:
            HfApi().dataset_info(repository, token=token, timeout=10)
        except Exception as error:
            raise _hub_error(error, "dataset.uri") from error
        return None

    try:
        builder = load_dataset_builder(repository, name=config, token=token)
    except DatasetNotFoundError as error:
        raise ApiError(
            422,
            "validation_error",
            "dataset.uri Hugging Face repository was not found or cannot be loaded by datasets; "
            "check the repository ID and Secret token permissions.",
        ) from error
    except ValueError as error:
        reason = str(error)
        if config is None and "Config name is missing" in reason:
            message = (
                "Hugging Face dataset configuration could not be selected; "
                "provide dataset.config when the dataset has multiple configurations."
            )
        elif config is not None and "BuilderConfig" in reason and "not found" in reason:
            message = "dataset.config is not valid for this Hugging Face dataset."
        else:
            message = (
                "dataset.uri could not be loaded by Hugging Face datasets; "
                "check the repository files and dataset configuration."
            )
        raise ApiError(422, "validation_error", message) from error
    except Exception as error:
        raise _hub_error(error, "dataset.uri") from error

    try:
        instruction = ReadInstruction.from_spec(split)
        if builder.info.splits:
            lengths = {name: info.num_examples for name, info in builder.info.splits.items()}
            if all(isinstance(length, int) for length in lengths.values()):
                instruction.to_absolute(lengths)
    except ValueError as error:
        raise ApiError(
            422,
            "validation_error",
            "dataset.split is not valid for this Hugging Face dataset; "
            "check the available split names and slice syntax.",
        ) from error
    features = getattr(builder.info, "features", None)
    return set(features) if features is not None else None
