"""Caller-authorized discovery from an image-bound catalog and Kueue APIs."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Any

from fastapi import Request
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from training_service.contract import load_contract
from training_service.errors import ApiError
from training_service.kubernetes_client import (
    TIMEOUT,
    caller_client,
    describe_not_found,
    validate_names,
)

KUEUE_GROUP = "kueue.x-k8s.io"


def _unavailable(message: str) -> ApiError:
    return ApiError(503, "backend_unavailable", message)


def read_algorithm_catalog(api: client.CoreV1Api, image: str) -> dict[str, dict[str, Any]]:
    """Only advertise reviewed adapters whose image catalog matches this API contract.

    The administrator publishes catalog.json in a ConfigMap for an exact runtime
    image. It is read with the caller's client on every operation, not a shared
    service identity or a cache that could bypass the caller's permissions.
    """
    namespace = os.getenv("TRAINING_ALGORITHM_CATALOG_NAMESPACE", "")
    name = os.getenv("TRAINING_ALGORITHM_CATALOG_NAME", "")
    if not image or not namespace or not name:
        raise _unavailable("A Ray image and image-specific algorithm catalog must be configured.")
    try:
        validate_names(namespace, name)
    except ApiError:
        raise _unavailable("Algorithm catalog configuration is invalid.") from None
    try:
        catalog = api.read_namespaced_config_map(name, namespace, _request_timeout=TIMEOUT)
    except ApiException as error:
        if error.status == 404:
            raise _unavailable("The configured algorithm catalog was not found.") from None
        raise
    schemas = load_contract()["components"]["schemas"]["AlgorithmSpec"]["x-parameter-schemas"]
    try:
        raw = (catalog.data or {})["catalog.json"]
        if not isinstance(raw, str) or len(raw.encode()) > 262144:
            raise ValueError
        document = json.loads(raw)
        if document["image"] != image or not isinstance(document["algorithms"], list):
            raise ValueError
        result: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()
        for item in document["algorithms"]:
            algorithm = item["name"]
            if not isinstance(algorithm, str) or algorithm in seen:
                raise ValueError
            seen.add(algorithm)
            if algorithm not in schemas:
                # Installing a new TH algorithm does not automatically enable it.
                continue
            if item["parameter_schema"] != schemas[algorithm]:
                raise ValueError
            descriptor = {
                "name": algorithm,
                "supported_backends": ["ray"],
                "parameter_schema": deepcopy(schemas[algorithm]),
            }
            if "description" in item:
                if not isinstance(item["description"], str):
                    raise ValueError
                descriptor["description"] = item["description"]
            result[algorithm] = descriptor
        return dict(sorted(result.items()))
    except (KeyError, TypeError, ValueError):
        raise _unavailable(
            "Algorithm catalog is malformed or incompatible with the configured image/API."
        ) from None


def list_algorithms(request: Request) -> dict[str, Any]:
    with caller_client(request) as api:
        catalog = read_algorithm_catalog(client.CoreV1Api(api), os.getenv("TRAINING_RAY_IMAGE", ""))
    return {"algorithms": list(catalog.values())}


def queue_api_version(request: Request, project: str) -> str | None:
    """Check project access before distinguishing absent Kueue from denied access."""
    validate_names(project)
    with caller_client(request) as api:
        with describe_not_found("Project", project):
            client.CoreV1Api(api).read_namespace(project, _request_timeout=TIMEOUT)
        enabled = os.getenv("TRAINING_KUEUE_ENABLED", "auto").lower()
        if enabled not in {"auto", "true", "false"}:
            raise _unavailable("TRAINING_KUEUE_ENABLED must be auto, true, or false.")
        if enabled == "false":
            return None
        groups = client.ApisApi(api).get_api_versions(_request_timeout=TIMEOUT).groups
        if not isinstance(groups, list):
            raise _unavailable("Kubernetes returned invalid API discovery metadata.")
        group = next((item for item in groups if item.name == KUEUE_GROUP), None)
        if group is None:
            return None
        if not isinstance(group.versions, list):
            raise _unavailable("Kueue returned invalid API version metadata.")
        served = {item.version for item in group.versions}
        custom = client.CustomObjectsApi(api)
        for version in ("v1beta2", "v1beta1"):
            if version not in served:
                continue
            resources = custom.get_api_resources(KUEUE_GROUP, version, _request_timeout=TIMEOUT)
            if not isinstance(resources.resources, list):
                raise _unavailable("Kueue returned invalid resource discovery metadata.")
            if any(
                item.name == "localqueues" and item.namespaced and "list" in (item.verbs or [])
                for item in resources.resources
            ):
                return version
        raise _unavailable("Kueue does not expose a supported LocalQueue API version.")


def list_queues(request: Request, project: str, version: str) -> dict[str, Any]:
    queues = []
    with caller_client(request) as api:
        custom = client.CustomObjectsApi(api)
        continuation = ""
        seen: set[str] = set()
        while True:
            with describe_not_found("Project", project):
                page = custom.list_namespaced_custom_object(
                    KUEUE_GROUP,
                    version,
                    project,
                    "localqueues",
                    limit=500,
                    _continue=continuation,
                    _request_timeout=TIMEOUT,
                )
            try:
                if not isinstance(page["items"], list):
                    raise ValueError
                for item in page["items"]:
                    name, cluster_queue = item["metadata"]["name"], item["spec"]["clusterQueue"]
                    if not isinstance(name, str) or not isinstance(cluster_queue, str):
                        raise ValueError
                    queues.append({"name": name, "cluster_queue": cluster_queue})
                metadata = page.get("metadata", {})
                if not isinstance(metadata, dict):
                    raise ValueError
                continuation = metadata.get("continue", "")
                if not continuation:
                    break
                if not isinstance(continuation, str) or continuation in seen:
                    raise ValueError
                seen.add(continuation)
            except (KeyError, TypeError, ValueError):
                raise _unavailable("Kueue returned an invalid LocalQueue response.") from None
    return {"kueue_enabled": True, "queues": sorted(queues, key=lambda item: item["name"])}
