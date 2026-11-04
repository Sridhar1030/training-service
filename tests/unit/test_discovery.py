"""Discovery uses caller-scoped Kubernetes clients; no live cluster is needed."""

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from kubernetes.client.exceptions import ApiException

from training_service import discovery
from training_service.auth import CallerIdentity
from training_service.authorization import ProjectPermission
from training_service.contract import load_contract
from training_service.main import create_app

IMAGE = "quay.io/example/ray@sha256:" + "a" * 64
HEADERS = {"Authorization": "Bearer caller-token"}


class Authenticator:
    async def authenticate(self, token: str) -> CallerIdentity | None:
        return CallerIdentity("alice", "uid", ("team",)) if token == "caller-token" else None


class Authorizer:
    allowed = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, ProjectPermission]] = []

    async def is_allowed(self, identity: Any, project: str, permission: ProjectPermission) -> bool:
        assert identity.username == "alice"
        self.calls.append((project, permission))
        return self.allowed


class Kubernetes:
    error: ApiException | None = None
    version = "v1beta2"

    def __init__(self) -> None:
        schemas = load_contract()["components"]["schemas"]["AlgorithmSpec"]["x-parameter-schemas"]
        self.catalog = {
            "image": IMAGE,
            "algorithms": [
                {"name": name, "parameter_schema": deepcopy(schema)}
                for name, schema in schemas.items()
            ],
        }
        self.calls: list[Any] = []
        self.pages = [
            {"items": [{"metadata": {"name": "team-queue"}, "spec": {"clusterQueue": "gpu-pool"}}]}
        ]
        self.available = True

    def factory(self, api: Any) -> "Kubernetes":
        assert api.configuration.api_key["authorization"] == "Bearer caller-token"
        assert api.configuration.verify_ssl is True
        return self

    def read_namespaced_config_map(self, name: str, namespace: str, **kwargs: Any) -> Any:
        self.calls.append(("catalog", name, namespace))
        assert kwargs["_request_timeout"] == discovery.TIMEOUT
        if self.error:
            raise self.error
        return SimpleNamespace(data={"catalog.json": json.dumps(self.catalog)})

    def read_namespace(self, project: str, **kwargs: Any) -> Any:
        self.calls.append(("project", project))
        if self.error:
            raise self.error
        return SimpleNamespace()

    def get_api_versions(self, **kwargs: Any) -> Any:
        self.calls.append("api-discovery")
        return SimpleNamespace(
            groups=[
                SimpleNamespace(
                    name=discovery.KUEUE_GROUP, versions=[SimpleNamespace(version=self.version)]
                )
            ]
            if self.available
            else []
        )

    def get_api_resources(self, group: str, version: str, **kwargs: Any) -> Any:
        self.calls.append(("resources", group, version))
        return SimpleNamespace(
            resources=[SimpleNamespace(name="localqueues", namespaced=True, verbs=["get", "list"])]
        )

    def list_namespaced_custom_object(
        self, group: str, version: str, project: str, plural: str, **kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append(("list", group, version, project, plural, kwargs.get("_continue")))
        assert kwargs["_request_timeout"] == discovery.TIMEOUT
        return self.pages.pop(0)


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Kubernetes, Authorizer]:
    monkeypatch.setenv("TRAINING_RAY_IMAGE", IMAGE)
    monkeypatch.setenv("TRAINING_ALGORITHM_CATALOG_NAMESPACE", "training-service")
    monkeypatch.setenv("TRAINING_ALGORITHM_CATALOG_NAME", "reviewed-runtime")
    monkeypatch.delenv("TRAINING_KUEUE_ENABLED", raising=False)
    kube, authorizer = Kubernetes(), Authorizer()
    for name in ("CoreV1Api", "ApisApi", "CustomObjectsApi"):
        monkeypatch.setattr(discovery.client, name, kube.factory)
    return TestClient(create_app(Authenticator(), authorizer)), kube, authorizer


def test_image_catalog_returns_exact_submission_schemas(setup: Any) -> None:
    http, kube, _ = setup
    response = http.get("/api/v1/algorithms", headers=HEADERS)
    assert response.status_code == 200
    values = response.json()["algorithms"]
    assert [item["name"] for item in values] == ["lora_grpo", "lora_sft", "osft", "sft"]
    for item in values:
        assert item["supported_backends"] == ["ray"]
        assert item["parameter_schema"] == next(
            entry["parameter_schema"]
            for entry in kube.catalog["algorithms"]
            if entry["name"] == item["name"]
        )
    assert kube.calls == [("catalog", "reviewed-runtime", "training-service")]


def test_catalog_is_image_specific_and_does_not_auto_enable_algorithms(setup: Any) -> None:
    http, kube, _ = setup
    kube.catalog["algorithms"] = [kube.catalog["algorithms"][0], {"name": "future_algorithm"}]
    response = http.get("/api/v1/algorithms", headers=HEADERS)
    assert [item["name"] for item in response.json()["algorithms"]] == ["sft"]


@pytest.mark.parametrize("invalid", ["image", "schema", "duplicate", "malformed"])
def test_catalog_drift_fails_closed(setup: Any, invalid: str) -> None:
    http, kube, _ = setup
    if invalid == "image":
        kube.catalog["image"] = "another-image"
    elif invalid == "schema":
        kube.catalog["algorithms"][0]["parameter_schema"] = {"type": "object"}
    elif invalid == "duplicate":
        kube.catalog["algorithms"].append(kube.catalog["algorithms"][0])
    else:
        kube.catalog = []
    response = http.get("/api/v1/algorithms", headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["code"] == "backend_unavailable"


def test_missing_catalog_configuration_does_not_read_kubernetes(
    setup: Any, monkeypatch: Any
) -> None:
    http, kube, _ = setup
    monkeypatch.delenv("TRAINING_ALGORITHM_CATALOG_NAME")
    assert http.get("/api/v1/algorithms", headers=HEADERS).status_code == 503
    assert kube.calls == []


@pytest.mark.parametrize("status,expected", [(401, 401), (403, 403), (404, 503), (500, 503)])
def test_catalog_errors_preserve_rbac_and_sanitize_bodies(
    setup: Any, status: int, expected: int
) -> None:
    http, kube, _ = setup
    kube.error = ApiException(status=status, reason="private upstream response")
    response = http.get("/api/v1/algorithms", headers=HEADERS)
    assert response.status_code == expected
    assert "private" not in response.text
    if status == 401:
        assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("path", ["/algorithms", "/projects/team-alpha/queues"])
def test_discovery_requires_authentication(setup: Any, path: str) -> None:
    http, kube, _ = setup
    assert http.get("/api/v1" + path).status_code == 401
    assert kube.calls == []


@pytest.mark.parametrize("version", ["v1beta1", "v1beta2"])
def test_queues_are_project_scoped_paginated_and_authorized(setup: Any, version: str) -> None:
    http, kube, authorizer = setup
    kube.version = version
    kube.pages = [{"items": [], "metadata": {"continue": "page-2"}}, *kube.pages]
    response = http.get("/api/v1/projects/team-alpha/queues", headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {
        "kueue_enabled": True,
        "queues": [{"name": "team-queue", "cluster_queue": "gpu-pool"}],
    }
    assert authorizer.calls == [
        ("team-alpha", ProjectPermission(discovery.KUEUE_GROUP, "localqueues", "list"))
    ]
    assert kube.calls[-1] == (
        "list",
        discovery.KUEUE_GROUP,
        version,
        "team-alpha",
        "localqueues",
        "page-2",
    )


@pytest.mark.parametrize("disabled", [True, False])
def test_absent_or_explicitly_disabled_kueue_returns_empty_after_project_check(
    setup: Any, monkeypatch: Any, disabled: bool
) -> None:
    http, kube, authorizer = setup
    kube.available = False
    if disabled:
        monkeypatch.setenv("TRAINING_KUEUE_ENABLED", "false")
    response = http.get("/api/v1/projects/team-alpha/queues", headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"kueue_enabled": False, "queues": []}
    assert kube.calls[0] == ("project", "team-alpha")
    assert not authorizer.calls
    assert not any(isinstance(item, tuple) and item[0] == "list" for item in kube.calls)


@pytest.mark.parametrize("status", [401, 403, 404])
def test_queue_project_errors_are_not_disguised_as_kueue_absence(setup: Any, status: int) -> None:
    http, kube, _ = setup
    kube.available = False
    kube.error = ApiException(status=status)
    assert http.get("/api/v1/projects/team-alpha/queues", headers=HEADERS).status_code == status


def test_queue_list_denial_does_not_list_resources(setup: Any) -> None:
    http, kube, authorizer = setup
    authorizer.allowed = False
    assert http.get("/api/v1/projects/team-alpha/queues", headers=HEADERS).status_code == 403
    assert not any(isinstance(item, tuple) and item[0] == "list" for item in kube.calls)


def test_unknown_kueue_version_fails_closed(setup: Any) -> None:
    http, kube, _ = setup
    kube.version = "v99"
    assert http.get("/api/v1/projects/team-alpha/queues", headers=HEADERS).status_code == 503


@pytest.mark.parametrize(
    "method", ["get_api_versions", "get_api_resources", "list_namespaced_custom_object"]
)
@pytest.mark.parametrize("status", [401, 403, 500])
def test_kueue_transport_failures_never_look_disabled(
    setup: Any, monkeypatch: Any, method: str, status: int
) -> None:
    http, kube, _ = setup

    def fail(*args: Any, **kwargs: Any) -> None:
        raise ApiException(status=status, reason="private response")

    monkeypatch.setattr(kube, method, fail)
    response = http.get("/api/v1/projects/team-alpha/queues", headers=HEADERS)
    assert response.status_code == (status if status in {401, 403} else 503)
    assert "kueue_enabled" not in response.json()
    assert "private" not in response.text


def test_repeated_queue_continuation_fails_instead_of_looping(setup: Any) -> None:
    http, kube, _ = setup
    kube.pages = [{"items": [], "metadata": {"continue": "same"}}] * 2
    assert http.get("/api/v1/projects/team-alpha/queues", headers=HEADERS).status_code == 503


def test_submission_enforces_the_same_live_catalog_as_discovery(setup: Any) -> None:
    http, kube, _ = setup
    kube.catalog["algorithms"] = [
        item for item in kube.catalog["algorithms"] if item["name"] != "lora_sft"
    ]
    listing = http.get("/api/v1/algorithms", headers=HEADERS)
    assert "lora_sft" not in {item["name"] for item in listing.json()["algorithms"]}
    examples = load_contract()["paths"]["/projects/{project}/training-jobs"]["post"]["requestBody"][
        "content"
    ]["application/json"]["examples"]
    response = http.post(
        "/api/v1/projects/team-alpha/training-jobs",
        headers=HEADERS,
        json=examples["pvc_inputs_output"]["value"],
    )
    assert response.status_code == 422
    assert "not enabled" in response.json()["message"]
    assert kube.calls == [("catalog", "reviewed-runtime", "training-service")] * 2


def test_discovery_openapi_does_not_advertise_scaffold_responses(setup: Any) -> None:
    http, _, _ = setup
    document = http.get("/openapi.json").json()
    for path in ("/algorithms", "/projects/{project}/queues"):
        responses = document["paths"][path]["get"]["responses"]
        assert "501" not in responses
        assert responses["503"]["$ref"] == "#/components/responses/503DiscoveryUnavailable"


@pytest.mark.parametrize(
    "page", [[], {"items": {}}, {"items": None}, {"items": [], "metadata": []}, {"items": [{}]}]
)
def test_malformed_queue_pages_fail_closed(setup: Any, page: Any) -> None:
    http, kube, _ = setup
    kube.pages = [page]
    response = http.get("/api/v1/projects/team-alpha/queues", headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["code"] == "backend_unavailable"


@pytest.mark.parametrize(
    "method,reply",
    [
        ("get_api_versions", SimpleNamespace(groups=None)),
        (
            "get_api_versions",
            SimpleNamespace(groups=[SimpleNamespace(name=discovery.KUEUE_GROUP, versions=None)]),
        ),
        ("get_api_resources", SimpleNamespace(resources=None)),
    ],
)
def test_malformed_kueue_discovery_fails_closed(
    setup: Any, monkeypatch: Any, method: str, reply: Any
) -> None:
    http, kube, _ = setup
    monkeypatch.setattr(kube, method, lambda *args, **kwargs: reply)
    response = http.get("/api/v1/projects/team-alpha/queues", headers=HEADERS)
    assert response.status_code == 503
    assert "kueue_enabled" not in response.json()


@pytest.mark.parametrize(
    "case",
    ["namespace", "oversized", "description", "kueue-setting", "queue-name", "cluster-queue"],
)
def test_invalid_discovery_configuration_and_payloads_are_sanitized(
    setup: Any, monkeypatch: Any, case: str
) -> None:
    http, kube, _ = setup
    path = "/api/v1/algorithms"
    if case == "namespace":
        monkeypatch.setenv("TRAINING_ALGORITHM_CATALOG_NAMESPACE", "../other-project")
    elif case == "oversized":
        monkeypatch.setattr(
            kube,
            "read_namespaced_config_map",
            lambda *args, **kw: SimpleNamespace(data={"catalog.json": " " * 262145}),
        )
    elif case == "description":
        kube.catalog["algorithms"][0]["description"] = {"private": "not a string"}
    else:
        path = "/api/v1/projects/team-alpha/queues"
        if case == "kueue-setting":
            monkeypatch.setenv("TRAINING_KUEUE_ENABLED", "invalid")
        elif case == "queue-name":
            kube.pages[0]["items"][0]["metadata"]["name"] = 123
        else:
            kube.pages[0]["items"][0]["spec"]["clusterQueue"] = None
    response = http.get(path, headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["code"] == "backend_unavailable"
    assert "private" not in response.text


@pytest.mark.parametrize("beta2_localqueue", [True, False])
def test_queue_discovery_prefers_beta2_and_falls_back_to_beta1_only_when_unserved(
    setup: Any, monkeypatch: Any, beta2_localqueue: bool
) -> None:
    http, kube, _ = setup
    monkeypatch.setattr(
        kube,
        "get_api_versions",
        lambda **kw: SimpleNamespace(
            groups=[
                SimpleNamespace(
                    name=discovery.KUEUE_GROUP,
                    versions=[
                        SimpleNamespace(version="v1beta1"),
                        SimpleNamespace(version="v1beta2"),
                    ],
                )
            ]
        ),
    )
    original = kube.get_api_resources

    def resources(group: str, version: str, **kw: Any) -> Any:
        if version == "v1beta2" and not beta2_localqueue:
            return SimpleNamespace(resources=[])
        return original(group, version, **kw)

    monkeypatch.setattr(kube, "get_api_resources", resources)
    response = http.get("/api/v1/projects/team-alpha/queues", headers=HEADERS)
    assert response.status_code == 200
    assert kube.calls[-1][2] == ("v1beta2" if beta2_localqueue else "v1beta1")
