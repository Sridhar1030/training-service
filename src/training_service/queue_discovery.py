"""Project-scoped Kueue LocalQueue discovery through Kubernetes APIs."""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from typing import Protocol

import anyio
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException
from urllib3.exceptions import HTTPError

from training_service.errors import ApiError
from training_service_api.models.queue_info import QueueInfo
from training_service_api.models.queue_info_response import QueueInfoResponse

_LOG = logging.getLogger(__name__)
_KUEUE_GROUP = "kueue.x-k8s.io"
_KUEUE_VERSION = "v1beta2"
_LOCAL_QUEUE_PLURAL = "localqueues"
_DISCOVERY_TIMEOUT_SECONDS = 10


class QueueDiscoverer(Protocol):
    """Queue-discovery boundary that unit tests can replace without a cluster."""

    async def list_queues(self, project: str) -> QueueInfoResponse:
        """Return Kueue LocalQueues visible in one project."""


class KubernetesQueueDiscoverer:
    """Read Kueue LocalQueues directly from the Kubernetes CustomObjects API."""

    def __init__(self) -> None:
        self._api: client.CustomObjectsApi | None = None
        self._api_lock = threading.Lock()
        self._thread_limiter = anyio.CapacityLimiter(10)

    async def list_queues(self, project: str) -> QueueInfoResponse:
        """Read queues without blocking the FastAPI event loop."""
        try:
            return await anyio.to_thread.run_sync(
                self._list_queues, project, limiter=self._thread_limiter
            )
        except QueueDiscoveryUnavailable as error:
            raise ApiError(
                503,
                "backend_unavailable",
                "OpenShift queue discovery is temporarily unavailable.",
            ) from error

    def _list_queues(self, project: str) -> QueueInfoResponse:
        if self._api is None:
            # The Kubernetes client is shared by concurrent request worker threads.
            with self._api_lock:
                if self._api is None:
                    try:
                        config.load_incluster_config()
                        self._api = client.CustomObjectsApi()
                    except (ConfigException, HTTPError) as error:
                        raise QueueDiscoveryUnavailable from error

        try:
            response = self._api.list_namespaced_custom_object(
                group=_KUEUE_GROUP,
                version=_KUEUE_VERSION,
                namespace=project,
                plural=_LOCAL_QUEUE_PLURAL,
                _request_timeout=_DISCOVERY_TIMEOUT_SECONDS,
            )
        except ApiException as error:
            # A missing Kueue API is an optional-feature condition, not a failure.
            if error.status == 404:
                return QueueInfoResponse(kueue_enabled=False, queues=[])
            _LOG.warning("Kueue LocalQueue discovery is unavailable (%s)", error.status)
            raise QueueDiscoveryUnavailable from error
        except (ConfigException, HTTPError) as error:
            _LOG.warning(
                "Kueue LocalQueue discovery transport is unavailable (%s)", type(error).__name__
            )
            raise QueueDiscoveryUnavailable from error

        if not isinstance(response, Mapping):
            raise QueueDiscoveryUnavailable
        items = response.get("items", [])
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
