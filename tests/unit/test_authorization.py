"""Focused project-authorization tests without a live OpenShift cluster."""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from urllib3.exceptions import HTTPError

from training_service.auth import CallerIdentity
from training_service.authorization import (
    _SUBJECT_ACCESS_REVIEW_TIMEOUT_SECONDS,
    AuthorizationUnavailable,
    KubernetesProjectAuthorizer,
    ProjectPermission,
)
from training_service.main import create_app
from training_service_api.apis.training_jobs_api_base import BaseTrainingJobsApi


class FakeAuthenticator:
    """Return a fixed verified identity without contacting OpenShift."""

    async def authenticate(self, token: str) -> CallerIdentity | None:
        return CallerIdentity("alice", "uid-1", ("team-a",))


class RecordingAuthorizer:
    """Record requested access and return the configured authorization result."""

    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.calls: list[tuple[CallerIdentity, str, ProjectPermission]] = []

    async def is_allowed(
        self,
        identity: CallerIdentity,
        project: str,
        permission: ProjectPermission,
    ) -> bool:
        self.calls.append((identity, project, permission))
        return self.allowed


class ListBackendProbe:
    """Minimal generated-handler substitute used to prove dispatch behavior."""

    calls = 0

    async def list_training_jobs(self, *args: object, **kwargs: object) -> dict[str, object]:
        type(self).calls += 1
        return {"items": [], "total_count": 0}


def test_denied_project_access_returns_403_before_backend_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorizer = RecordingAuthorizer(allowed=False)
    application = create_app(FakeAuthenticator(), authorizer)
    monkeypatch.setattr(BaseTrainingJobsApi, "subclasses", (ListBackendProbe,))
    ListBackendProbe.calls = 0

    response = TestClient(application).get(
        "/api/v1/projects/team-alpha/training-jobs",
        headers={"Authorization": "Bearer accepted-token"},
    )

    assert response.status_code == 403
    assert response.json() == {
        "code": "forbidden",
        "message": "Caller is not authorized to perform this operation in the requested project.",
    }
    assert ListBackendProbe.calls == 0
    assert authorizer.calls == [
        (
            CallerIdentity("alice", "uid-1", ("team-a",)),
            "team-alpha",
            ProjectPermission(group="ray.io", resource="rayjobs", verb="list"),
        )
    ]


def test_allowed_project_access_reaches_the_backend_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorizer = RecordingAuthorizer(allowed=True)
    application = create_app(FakeAuthenticator(), authorizer)
    monkeypatch.setattr(BaseTrainingJobsApi, "subclasses", (ListBackendProbe,))
    ListBackendProbe.calls = 0

    response = TestClient(application).get(
        "/api/v1/projects/team-alpha/training-jobs",
        headers={"Authorization": "Bearer accepted-token"},
    )

    assert response.status_code == 200
    assert response.json() == {"items": [], "total_count": 0}
    assert ListBackendProbe.calls == 1


def test_subject_access_review_uses_only_verified_identity_and_project_scope() -> None:
    authorizer = KubernetesProjectAuthorizer()
    api = Mock()
    api.create_subject_access_review.return_value = Mock(status=Mock(allowed=True))
    authorizer._api = api
    identity = CallerIdentity("alice", "uid-1", ("team-a",))
    permission = ProjectPermission(
        group="ray.io",
        resource="rayjobs",
        verb="get",
        resource_name="job-1",
    )

    assert authorizer._review(identity, "team-alpha", permission) is True

    review = api.create_subject_access_review.call_args.args[0]
    assert review.spec.user == "alice"
    assert review.spec.uid == "uid-1"
    assert review.spec.groups == ["team-a"]
    assert review.spec.resource_attributes.namespace == "team-alpha"
    assert review.spec.resource_attributes.group == "ray.io"
    assert review.spec.resource_attributes.resource == "rayjobs"
    assert review.spec.resource_attributes.verb == "get"
    assert review.spec.resource_attributes.name == "job-1"
    assert (
        api.create_subject_access_review.call_args.kwargs["_request_timeout"]
        == _SUBJECT_ACCESS_REVIEW_TIMEOUT_SECONDS
    )


def test_subject_access_review_transport_failure_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorizer = KubernetesProjectAuthorizer()

    def raise_transport_error(
        identity: CallerIdentity,
        project: str,
        permission: ProjectPermission,
    ) -> bool:
        raise HTTPError("connection failed")

    monkeypatch.setattr(authorizer, "_review", raise_transport_error)

    with pytest.raises(AuthorizationUnavailable):
        asyncio.run(
            authorizer.is_allowed(
                CallerIdentity("alice", "uid-1", ()),
                "team-alpha",
                ProjectPermission(group="ray.io", resource="rayjobs", verb="get"),
            )
        )


def test_subject_access_review_client_initializes_once_for_concurrent_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorizer = KubernetesProjectAuthorizer()
    api = Mock()
    api.create_subject_access_review.return_value = Mock(status=Mock(allowed=True))
    load_config = Mock(side_effect=lambda: time.sleep(0.01))
    create_api = Mock(return_value=api)
    monkeypatch.setattr("training_service.authorization.config.load_incluster_config", load_config)
    monkeypatch.setattr("training_service.authorization.client.AuthorizationV1Api", create_api)

    identity = CallerIdentity("alice", "uid-1", ("team-a",))
    permission = ProjectPermission(group="ray.io", resource="rayjobs", verb="list")
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(
            executor.map(lambda _: authorizer._review(identity, "team-alpha", permission), range(2))
        )

    load_config.assert_called_once()
    create_api.assert_called_once()
