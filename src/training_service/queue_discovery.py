"""Project-scoped Kueue LocalQueue discovery through Kubernetes APIs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

import anyio
from fastapi import Request
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from training_service.errors import ApiError
from training_service.kubernetes_client import (
    caller_client,
    validate_names,
)
from training_service.kueue_preflight import preflight
from training_service_api.models.queue_info import QueueInfo
from training_service_api.models.queue_info_response import QueueInfoResponse

_KUEUE_GROUP = "kueue.x-k8s.io"
_LOCAL_QUEUE_PLURAL = "localqueues"
_DISCOVERY_TIMEOUT_SECONDS = 10


class QueueDiscoverer(Protocol):
    """Queue-discovery boundary that unit tests can replace without a cluster."""

    async def list_queues(self, request: Request, project: str) -> QueueInfoResponse:
        """Return Kueue LocalQueues visible in one project."""


class KubernetesQueueDiscoverer:
    """Read Kueue LocalQueues directly from the Kubernetes CustomObjects API."""

    async def list_queues(self, request: Request, project: str) -> QueueInfoResponse:
        """Read queues without blocking the FastAPI event loop."""
        try:
            return await anyio.to_thread.run_sync(self._list_queues, request, project)
        except QueueDiscoveryUnavailable as error:
            raise ApiError(
                503,
                "backend_unavailable",
                "OpenShift queue discovery is temporarily unavailable.",
            ) from error

    def _list_queues(self, request: Request, project: str) -> QueueInfoResponse:
        """List queues with the verified caller credential, not the service account."""
        validate_names(project)
        with caller_client(request) as api:
            version = preflight(api, project)
            try:
                response = client.CustomObjectsApi(api).list_namespaced_custom_object(
                    group=_KUEUE_GROUP,
                    version=version,
                    namespace=project,
                    plural=_LOCAL_QUEUE_PLURAL,
                    _request_timeout=_DISCOVERY_TIMEOUT_SECONDS,
                )
            except ApiException as error:
                if error.status in (401, 403):
                    raise
                raise QueueDiscoveryUnavailable from error

        if not isinstance(response, Mapping):
            raise QueueDiscoveryUnavailable
        if "items" not in response:
            raise QueueDiscoveryUnavailable
        items = response["items"]
        if not isinstance(items, list):
            raise QueueDiscoveryUnavailable

        queues: list[QueueInfo] = []
        for item in items:
            if not isinstance(item, Mapping):
                raise QueueDiscoveryUnavailable
            metadata = item.get("metadata", {})
            spec = item.get("spec", {})
            if not isinstance(metadata, Mapping) or not isinstance(spec, Mapping):
                raise QueueDiscoveryUnavailable
            name = metadata.get("name")
            cluster_queue = spec.get("clusterQueue")
            if not isinstance(name, str):
                raise QueueDiscoveryUnavailable
            if cluster_queue is not None and not isinstance(cluster_queue, str):
                raise QueueDiscoveryUnavailable
            queue = {"name": name}
            if cluster_queue is not None:
                queue["cluster_queue"] = cluster_queue
            queues.append(QueueInfo(**queue))

        return QueueInfoResponse(kueue_enabled=True, queues=queues)


class QueueDiscoveryUnavailable(Exception):
    """Kubernetes cannot provide a reliable queue-discovery result."""
