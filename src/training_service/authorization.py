"""Project-scoped OpenShift authorization for Training API operations."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Protocol

import anyio
from fastapi import Request
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException
from urllib3.exceptions import HTTPError

from training_service.auth import CallerIdentity
from training_service.errors import ApiError

_LOG = logging.getLogger(__name__)
_SUBJECT_ACCESS_REVIEW_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class ProjectPermission:
    """The namespaced Kubernetes resource action required by an API operation."""

    group: str
    resource: str
    verb: str
    resource_name: str | None = None


class OpenShiftProjectAuthorizer(Protocol):
    """Authorization boundary that unit tests can replace without a cluster."""

    async def is_allowed(
        self,
        identity: CallerIdentity,
        project: str,
        permission: ProjectPermission,
    ) -> bool:
        """Return whether the verified caller may perform the namespaced action."""


class KubernetesProjectAuthorizer:
    """Check caller permissions through the Kubernetes SubjectAccessReview API."""

    def __init__(self) -> None:
        self._api: client.AuthorizationV1Api | None = None
        self._api_lock = threading.Lock()
        self._thread_limiter = anyio.CapacityLimiter(10)

    async def is_allowed(
        self,
        identity: CallerIdentity,
        project: str,
        permission: ProjectPermission,
    ) -> bool:
        """Check authorization without blocking FastAPI's event loop."""
        try:
            return await anyio.to_thread.run_sync(
                self._review, identity, project, permission, limiter=self._thread_limiter
            )
        except (ApiException, ConfigException, HTTPError) as error:
            _LOG.warning("OpenShift SubjectAccessReview is unavailable (%s)", type(error).__name__)
            raise AuthorizationUnavailable from error

    def _review(
        self,
        identity: CallerIdentity,
        project: str,
        permission: ProjectPermission,
    ) -> bool:
        if self._api is None:
            # Multiple requests use worker threads. Initialize the shared client once.
            with self._api_lock:
                if self._api is None:
                    config.load_incluster_config()
                    self._api = client.AuthorizationV1Api()

        review = client.V1SubjectAccessReview(
            spec=client.V1SubjectAccessReviewSpec(
                user=identity.username,
                uid=identity.uid,
                groups=list(identity.groups),
                resource_attributes=client.V1ResourceAttributes(
                    namespace=project,
                    group=permission.group,
                    resource=permission.resource,
                    verb=permission.verb,
                    name=permission.resource_name,
                ),
            )
        )
        result = self._api.create_subject_access_review(
            review,
            _request_timeout=_SUBJECT_ACCESS_REVIEW_TIMEOUT_SECONDS,
        )
        return bool(result.status and result.status.allowed)


class AuthorizationUnavailable(Exception):
    """The service cannot reach its SubjectAccessReview boundary."""


def _forbidden() -> ApiError:
    """Return a sanitized failure for denied or unavailable authorization."""
    return ApiError(
        403,
        "forbidden",
        "Caller is not authorized to perform this operation in the requested project.",
    )


async def authorize_project_request(
    request: Request,
    project: str,
    *,
    group: str,
    resource: str,
    verb: str,
    resource_name: str | None = None,
) -> None:
    """Fail closed unless the verified caller can perform a project-scoped action."""
    identity = getattr(request.state, "caller_identity", None)
    if not isinstance(identity, CallerIdentity):
        raise _forbidden()

    authorizer: OpenShiftProjectAuthorizer = request.app.state.project_authorizer
    try:
        allowed = await authorizer.is_allowed(
            identity,
            project,
            ProjectPermission(
                group=group,
                resource=resource,
                verb=verb,
                resource_name=resource_name,
            ),
        )
    except AuthorizationUnavailable as error:
        raise _forbidden() from error

    if not allowed:
        raise _forbidden()
