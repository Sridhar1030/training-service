"""Cheap dataset checks that do not read caller-controlled storage endpoints."""

from __future__ import annotations

from collections.abc import Mapping, Set
from pathlib import PurePosixPath
from typing import Any

from training_service.errors import ApiError
from training_service.ray_entrypoint import SUPPORTED_DATASET_EXTENSIONS, get_backend


def validate_dataset_location(location: Mapping[str, str]) -> None:
    """Reject S3 file types that the selected Training Hub loaders cannot read.

    An S3 prefix is handled separately by the Ray entrypoint, which selects one
    file in a Training Hub-supported format after downloading it. Object
    existence and contents are not checked here: the API must not connect to
    caller-configured endpoints.
    """
    if location["scheme"] != "s3" or location["path"].endswith("/"):
        return
    if PurePosixPath(location["path"].lower()).suffix not in SUPPORTED_DATASET_EXTENSIONS:
        raise ApiError(
            422,
            "validation_error",
            "dataset.uri S3 object must be a JSONL, JSON, CSV, or Parquet file; "
            "S3 prefixes must end with '/'.",
        )


def validate_dataset_columns(
    algorithm: str, parameters: Mapping[str, Any], columns: Set[str] | None
) -> None:
    """Check known HF columns against the chosen Training Hub data path.

    Dataset builder metadata is not always populated. Unknown columns are not
    grounds for rejection, and this check does not claim to validate every row.
    """
    if not columns:
        return
    required = get_backend(algorithm).required_dataset_columns(dict(parameters), set(columns))
    missing = sorted(required - columns)
    if missing:
        raise ApiError(
            422,
            "validation_error",
            f"dataset.uri is missing columns required by {algorithm}: {', '.join(missing)}.",
        )
