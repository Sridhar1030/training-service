"""OpenShift bearer-token authentication for handwritten service behavior."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import anyio
from fastapi import Request
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException
from urllib3.exceptions import HTTPError

from training_service.errors import ApiError
from training_service_api.models.extra_models import TokenModel

_LOG = logging.getLogger(__name__)
_TOKEN_REVIEW_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class CallerIdentity:
    """Identity derived only from a successful OpenShift TokenReview."""

    username: str
    uid: str | None
    groups: tuple[str, ...]


class OpenShiftTokenAuthenticator(Protocol):
    """Authentication boundary that unit tests can replace without a cluster."""

    async def authenticate(self, token: str) -> CallerIdentity | None:
        """Return a verified caller, or ``None`` when the token is not accepted."""


class KubernetesTokenAuthenticator:
    """Validate caller tokens through the in-cluster Kubernetes TokenReview API."""

    def __init__(self) -> None:
        self._api: client.AuthenticationV1Api | None = None

    async def authenticate(self, token: str) -> CallerIdentity | None:
        """Validate a token without blocking FastAPI's event loop."""
        try:
            return await anyio.to_thread.run_sync(self._review, token)
        except (ApiException, ConfigException, HTTPError) as error:
            # Do not log the request token or an API exception body that could contain it.
            _LOG.warning("OpenShift TokenReview is unavailable (%s)", type(error).__name__)
            raise AuthenticationUnavailable from error

    def _review(self, token: str) -> CallerIdentity | None:
        if self._api is None:
            config.load_incluster_config()
            self._api = client.AuthenticationV1Api()

        review = client.V1TokenReview(spec=client.V1TokenReviewSpec(token=token))
        result = self._api.create_token_review(
            review,
            _request_timeout=_TOKEN_REVIEW_TIMEOUT_SECONDS,
        )
        status = result.status
        if status is None or not status.authenticated or status.user is None:
            return None

        user = status.user
        if not user.username:
            return None
        return CallerIdentity(
            username=user.username,
            uid=user.uid,
            groups=tuple(user.groups or ()),
        )


class AuthenticationUnavailable(Exception):
    """The service cannot reach its TokenReview authentication boundary."""


def _unauthorized() -> ApiError:
    return ApiError(
        401,
        "unauthorized",
        "A valid OpenShift bearer token is required.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _extract_bearer_token(request: Request) -> str:
    authorization = request.headers.get("Authorization")
    if authorization is None:
        raise _unauthorized()

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise _unauthorized()
    return parts[1]


async def authenticate_request(request: Request) -> TokenModel:
    """Authenticate a request and expose only verified identity to handlers."""
    token = _extract_bearer_token(request)
    authenticator: OpenShiftTokenAuthenticator = request.app.state.token_authenticator

    try:
        identity = await authenticator.authenticate(token)
    except AuthenticationUnavailable as error:
        raise ApiError(
            503,
            "authentication_unavailable",
            "OpenShift token validation is temporarily unavailable.",
        ) from error

    if identity is None:
        raise _unauthorized()

    request.state.caller_identity = identity
    return TokenModel(username=identity.username, uid=identity.uid, groups=list(identity.groups))
