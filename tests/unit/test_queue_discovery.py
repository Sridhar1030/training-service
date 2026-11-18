"""Kueue LocalQueue discovery tests with mocked Kubernetes boundaries."""

from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from kubernetes.client.exceptions import ApiException

from training_service import queue_discovery
from training_service.auth import CallerIdentity
from training_service.authorization import ProjectPermission
from training_service.main import create_app
from training_service.queue_discovery import (
    _DISCOVERY_TIMEOUT_SECONDS,
    KubernetesQueueDiscoverer,
)
from training_service_api.models.queue_info_response import QueueInfoResponse


class FakeAuthenticator:
    async def authenticate(self, token: str) -> CallerIdentity | None:
        return CallerIdentity("alice", "uid-1", ("team-a",))


class RecordingAuthorizer:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[str, ProjectPermission]] = []

    async def is_allowed(
        self, identity: CallerIdentity, project: str, permission: ProjectPermission
    ) -> bool:
        self.calls.append((project, permission))
        return self.allowed


class FakeDiscoverer:
    def __init__(self, result: QueueInfoResponse) -> None:
        self.result = result
        self.projects: list[str] = []

    async def list_queues(self, request: object, project: str) -> QueueInfoResponse:
        self.projects.append(project)
        return self.result


def mocked_kubernetes_clients(monkeypatch: pytest.MonkeyPatch, core: Mock, custom: Mock) -> None:
    @contextmanager
    def caller(_: object):
        yield Mock()

    monkeypatch.setattr(queue_discovery, "preflight", Mock(return_value="v1beta2"))
    monkeypatch.setattr(queue_discovery, "caller_client", caller)
    monkeypatch.setattr(queue_discovery.client, "CoreV1Api", lambda _: core)
    monkeypatch.setattr(queue_discovery.client, "CustomObjectsApi", lambda _: custom)


def test_kubernetes_queue_discovery_maps_local_queue_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discoverer = KubernetesQueueDiscoverer()
    core = Mock()
    custom = Mock()
    custom.list_namespaced_custom_object.return_value = {
        "items": [
            {"metadata": {"name": "team-alpha"}, "spec": {"clusterQueue": "gpu"}},
            {"metadata": {"name": "unassigned"}, "spec": {}},
        ]
    }
    mocked_kubernetes_clients(monkeypatch, core, custom)

    result = discoverer._list_queues(Mock(), "team-alpha")

    assert result.model_dump(exclude_unset=True) == {
        "kueue_enabled": True,
        "queues": [
            {"name": "team-alpha", "cluster_queue": "gpu"},
            {"name": "unassigned"},
        ],
    }
    queue_discovery.preflight.assert_called_once()
    assert custom.list_namespaced_custom_object.call_args.kwargs == {
        "group": "kueue.x-k8s.io",
        "version": "v1beta2",
        "namespace": "team-alpha",
        "plural": "localqueues",
        "_request_timeout": _DISCOVERY_TIMEOUT_SECONDS,
    }


def test_kubernetes_queue_discovery_handles_empty_and_missing_kueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discoverer = KubernetesQueueDiscoverer()
    core = Mock()
    custom = Mock()
    custom.list_namespaced_custom_object.return_value = {"items": []}
    mocked_kubernetes_clients(monkeypatch, core, custom)
    assert discoverer._list_queues(Mock(), "team-alpha") == QueueInfoResponse(
        kueue_enabled=True, queues=[]
    )

    custom.list_namespaced_custom_object.side_effect = ApiException(status=404)
    with pytest.raises(queue_discovery.QueueDiscoveryUnavailable):
        discoverer._list_queues(Mock(), "team-alpha")


@pytest.mark.anyio
async def test_kubernetes_queue_discovery_maps_kubernetes_failure_to_api_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discoverer = KubernetesQueueDiscoverer()
    core = Mock()
    custom = Mock()
    custom.list_namespaced_custom_object.side_effect = ApiException(status=500)
    mocked_kubernetes_clients(monkeypatch, core, custom)

    with pytest.raises(Exception) as raised:
        await discoverer.list_queues(Mock(), "team-alpha")

    assert getattr(raised.value, "status_code", None) == 503
    assert getattr(raised.value, "code", None) == "backend_unavailable"


def test_queue_endpoint_authorizes_then_returns_discovery_response() -> None:
    authorizer = RecordingAuthorizer()
    discoverer = FakeDiscoverer(
        QueueInfoResponse(
            kueue_enabled=True,
            queues=[{"name": "team-alpha", "cluster_queue": "gpu"}],
        )
    )
    client = TestClient(create_app(FakeAuthenticator(), authorizer, queue_discoverer=discoverer))

    response = client.get(
        "/api/v1/projects/team-alpha/queues", headers={"Authorization": "Bearer accepted"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "kueue_enabled": True,
        "queues": [{"name": "team-alpha", "cluster_queue": "gpu"}],
    }
    assert discoverer.projects == ["team-alpha"]
    assert authorizer.calls == [
        ("team-alpha", ProjectPermission("kueue.x-k8s.io", "localqueues", "list"))
    ]


def test_queue_endpoint_does_not_discover_when_authorization_is_denied() -> None:
    authorizer = RecordingAuthorizer(allowed=False)
    discoverer = FakeDiscoverer(QueueInfoResponse(kueue_enabled=True, queues=[]))
    client = TestClient(create_app(FakeAuthenticator(), authorizer, queue_discoverer=discoverer))

    response = client.get(
        "/api/v1/projects/team-alpha/queues", headers={"Authorization": "Bearer accepted"}
    )

    assert response.status_code == 403
    assert discoverer.projects == []


def test_kubernetes_queue_discovery_rejects_missing_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discoverer = KubernetesQueueDiscoverer()
    core = Mock()
    custom = Mock()
    custom.list_namespaced_custom_object.return_value = {}
    mocked_kubernetes_clients(monkeypatch, core, custom)

    with pytest.raises(queue_discovery.QueueDiscoveryUnavailable):
        discoverer._list_queues(Mock(), "team-alpha")


@pytest.mark.parametrize("status", [401, 403])
def test_list_preserves_downstream_authentication_errors(monkeypatch, status):
    from fastapi import Request

    from training_service import kubernetes_client

    api = Mock()
    api.__enter__ = Mock(return_value=api)
    api.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(kubernetes_client.client, "ApiClient", lambda _: api)
    monkeypatch.setattr(kubernetes_client, "caller_configuration", lambda _: Mock())
    monkeypatch.setattr(queue_discovery, "preflight", lambda *_: "v1beta1")
    custom = Mock()
    custom.list_namespaced_custom_object.side_effect = ApiException(status=status, reason="private")
    monkeypatch.setattr(queue_discovery.client, "CustomObjectsApi", lambda _: custom)
    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer test")]})
    request.state.caller_identity = CallerIdentity("alice", None, ())
    request.state.caller_bearer_token = "test"
    from training_service.errors import ApiError

    with pytest.raises(ApiError) as raised:
        KubernetesQueueDiscoverer()._list_queues(request, "project")
    assert raised.value.status_code == status
    assert "private" not in raised.value.message


@pytest.mark.parametrize("code", ["kueue_not_installed", "kueue_project_excluded"])
def test_kueue_error_is_serialized_by_http_contract(code):
    from training_service.errors import ApiError

    class UnavailableDiscoverer:
        async def list_queues(self, request, project):
            raise ApiError(409, code, "Kueue cannot manage this project.")

    client = TestClient(
        create_app(
            FakeAuthenticator(), RecordingAuthorizer(), queue_discoverer=UnavailableDiscoverer()
        )
    )
    response = client.get(
        "/api/v1/projects/team-alpha/queues", headers={"Authorization": "Bearer test"}
    )
    assert response.status_code == 409
    assert response.json()["code"] == code


@pytest.mark.parametrize("header", ["Bearer verified-token", "Bearer  verified-token"])
def test_caller_client_uses_verified_token_not_raw_header(monkeypatch, header):
    from fastapi import Request
    from kubernetes import client

    from training_service import kubernetes_client as kc

    request = Request({"type": "http", "headers": [(b"authorization", header.encode())]})
    request.state.caller_identity = CallerIdentity("alice", None, ())
    request.state.caller_bearer_token = "verified-token"
    with kc.caller_client(request) as api:
        assert api.configuration.api_key["authorization"] == "Bearer verified-token"
    del request.state.caller_bearer_token
    create = Mock(side_effect=AssertionError("Unverified request reached Kubernetes"))
    monkeypatch.setattr(client, "ApiClient", create)
    from training_service.errors import ApiError

    with pytest.raises(ApiError) as error:
        with kc.caller_client(request):
            pass
    assert error.value.status_code == 403
    create.assert_not_called()


def test_invalid_queue_project_has_documented_400():
    from training_service.contract import load_contract

    client = TestClient(create_app(FakeAuthenticator(), RecordingAuthorizer()))
    response = client.get(
        "/api/v1/projects/Team-Alpha/queues", headers={"Authorization": "Bearer test"}
    )
    assert response.status_code == 400
    assert response.json()["code"] == "bad_request"
    assert "400" in load_contract()["paths"]["/projects/{project}/queues"]["get"]["responses"]
