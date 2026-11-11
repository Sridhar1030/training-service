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
    core.read_namespace.assert_called_once_with("team-alpha", _request_timeout=(5, 15))
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
    assert discoverer._list_queues(Mock(), "team-alpha") == QueueInfoResponse(
        kueue_enabled=False, queues=[]
    )


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
