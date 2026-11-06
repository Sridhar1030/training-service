"""Validate Training Hub overrides against the service's algorithm catalog."""

from __future__ import annotations

import math
import re
from functools import lru_cache
from importlib.resources import files
from typing import Any

import yaml
from jsonschema import Draft4Validator

from training_service.errors import ApiError
from training_service.ray_entrypoint import get_backend


@lru_cache(maxsize=1)
def load_algorithm_parameter_schemas() -> dict[str, dict[str, Any]]:
    """Keep backend-specific validation independent of the public OpenAPI contract.

    A future algorithm-discovery provider can replace this catalog without
    changing the request model or the OpenAPI document.
    """
    source = files("training_service").joinpath("algorithm_parameter_schemas.yaml")
    schemas = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(schemas, dict) or not all(
        isinstance(name, str) and isinstance(schema, dict) for name, schema in schemas.items()
    ):
        raise RuntimeError("Algorithm parameter catalog is invalid.")
    for schema in schemas.values():
        Draft4Validator.check_schema(schema)
    return schemas


def _finite(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite(item) for item in value.values())
    if isinstance(value, list):
        return all(_finite(item) for item in value)
    return True


def validate_algorithm_parameters(name: str, parameters: dict[str, Any] | None) -> dict[str, Any]:
    """Return a validated copy; never pass unrecognized kwargs to Training Hub."""
    schemas = load_algorithm_parameter_schemas()
    schema = schemas.get(name)
    if schema is None:
        raise ApiError(422, "validation_error", "Unsupported Training Hub algorithm.")
    values = dict(parameters or {})
    if not _finite(values):
        raise ApiError(422, "validation_error", "Algorithm parameters must be finite.")
    error = next(iter(Draft4Validator(schema).iter_errors(values)), None)
    if error is not None:
        if error.validator == "additionalProperties":
            unknown = next((key for key in values if key not in schema["properties"]), None)
            if isinstance(unknown, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", unknown):
                message = f"Unsupported {name} parameter: {unknown}."
            else:
                message = f"Unsupported {name} parameter."
        elif error.validator == "required":
            missing = next((key for key in schema["required"] if key not in values), None)
            message = f"Missing required {name} parameter: {missing}."
        else:
            field = str(error.path[0]) if error.path else "parameters"
            label = f"{name}.{field}"
            if error.validator == "type":
                expected = error.validator_value
                article = "an" if expected in {"integer", "object", "array"} else "a"
                message = f"{label} must be {article} {expected}."
            elif error.validator in {"minimum", "maximum"}:
                direction = "greater" if error.validator == "minimum" else "less"
                qualifier = (
                    " than"
                    if error.schema.get("exclusive" + error.validator.title())
                    else " than or equal to"
                )
                message = f"{label} must be {direction}{qualifier} {error.validator_value}."
            elif error.validator == "enum":
                message = f"{label} must be one of: {', '.join(map(str, error.validator_value))}."
            else:
                message = f"Invalid {name} parameter: {field}."
        raise ApiError(422, "validation_error", message)
    try:
        return get_backend(name).validate_parameters(values)
    except ValueError as error:
        raise ApiError(422, "validation_error", str(error)) from error
