"""Capability checks use platform identity; namespace and queues use caller identity."""

from contextlib import contextmanager
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from training_service import kueue_preflight as k
from training_service.errors import ApiError


@pytest.mark.parametrize("version", ["v1beta1", "v1beta2", "v1beta3"])
def test_served_versions(monkeypatch, version):
    api = Mock()
    api.call_api.return_value = {
        "resources": [{"name": "localqueues", "namespaced": True, "verbs": ["get", "list"]}]
    }
    groups = Mock()
    groups.get_api_versions.return_value = NS(
        groups=[
            NS(name=k.GROUP, versions=[NS(version=version)], preferred_version=NS(version=version))
        ]
    )
    monkeypatch.setattr(client, "ApisApi", lambda _: groups)
    assert k.served_version(api) == version
    assert api.call_api.call_args.args[0] == f"/apis/{k.GROUP}/{version}"


@pytest.fixture
def installation(monkeypatch):
    api = Mock(sanitize_for_serialization=lambda obj: obj)

    @contextmanager
    def platform():
        yield api

    monkeypatch.setattr(k, "platform_client", platform)
    monkeypatch.setattr(k, "served_version", lambda _: "v1beta1")
    dep = NS(
        metadata=NS(generation=1),
        status=NS(
            observed_generation=1,
            available_replicas=1,
            updated_replicas=1,
            ready_replicas=1,
            replicas=1,
        ),
        spec=NS(
            replicas=1,
            template=NS(
                metadata=NS(labels={"app": "kueue"}, annotations={}),
                spec=NS(
                    volumes=[NS(name="config", config_map=NS(name="kueue-manager-config"))],
                    containers=[
                        NS(
                            args=["--config=/etc/controller_manager_config.yaml"],
                            volume_mounts=[NS(name="config", mount_path="/etc", sub_path=None)],
                        )
                    ],
                ),
            ),
        ),
    )
    apps = Mock()
    apps.read_namespaced_deployment.return_value = dep
    core = Mock()
    core.read_namespaced_config_map.return_value = NS(
        data={
            "controller_manager_config.yaml": "integrations:\n  frameworks: [ray.io/rayjob]\n"
            "managedJobsNamespaceSelector:\n  matchLabels:\n"
            '    kueue.openshift.io/managed: "true"\n'
        }
    )
    dep.spec.template.metadata.annotations["configmap/kueue-manager-config"] = k.config_digest(
        core.read_namespaced_config_map.return_value.data
    )
    core.read_namespaced_service.return_value = NS(spec=NS(selector={"app": "kueue"}))
    core.read_namespaced_endpoints.return_value = NS(subsets=[NS(addresses=[NS(ip="127.0.0.1")])])
    webhook = NS(
        client_config=NS(
            service=NS(
                namespace="openshift-kueue-operator",
                name="kueue-webhook-service",
                path="/mutate-ray-io-v1-rayjob",
            )
        ),
        failure_policy="Fail",
        match_conditions=None,
        object_selector=None,
        namespace_selector={},
        rules=[
            NS(
                api_groups=["ray.io"],
                api_versions=["v1"],
                resources=["rayjobs"],
                operations=["CREATE"],
            )
        ],
    )
    admission = Mock()
    admission.read_mutating_webhook_configuration.return_value = NS(webhooks=[webhook])
    monkeypatch.setattr(client, "AppsV1Api", lambda _: apps)
    monkeypatch.setattr(client, "CoreV1Api", lambda _: core)
    monkeypatch.setattr(client, "AdmissionregistrationV1Api", lambda _: admission)
    return dep, core, webhook


def test_separate_installation_without_dsc(installation):
    assert k.capability({"kueue.openshift.io/managed": "true"}) == "v1beta1"


def test_opt_out_with_retained_queues(installation, monkeypatch):
    custom = Mock()
    monkeypatch.setattr(client, "CustomObjectsApi", lambda _: custom)
    installation[1].read_namespace.return_value = NS(metadata=NS(labels={}))
    with pytest.raises(ApiError) as error:
        k.preflight(Mock(), "project", "retained-queue")
    assert (error.value.status_code, error.value.code) == (409, "kueue_project_excluded")
    custom.get_namespaced_custom_object.assert_not_called()


def test_stale_crds_unavailable_controller(installation):
    installation[0].status.available_replicas = 0
    with pytest.raises(ApiError) as error:
        k.capability({"kueue.openshift.io/managed": "true"})
    assert error.value.status_code == 503


@pytest.mark.parametrize("status", [401, 403, 404])
def test_selected_queue_checked_with_caller_on_each_post(monkeypatch, status):
    api, core, custom = Mock(), Mock(), Mock()
    core.read_namespace.return_value = NS(metadata=NS(labels={}))
    monkeypatch.setattr(client, "CoreV1Api", lambda passed: core if passed is api else None)
    monkeypatch.setattr(
        client, "CustomObjectsApi", lambda passed: custom if passed is api else None
    )
    monkeypatch.setattr(k, "capability", lambda _: "v1beta1")
    custom.get_namespaced_custom_object.side_effect = ApiException(status=status)
    with pytest.raises(ApiError if status == 404 else ApiException):
        k.preflight(api, "project", "selected")
    assert custom.get_namespaced_custom_object.call_args.args == (
        k.GROUP,
        "v1beta1",
        "project",
        "localqueues",
        "selected",
    )


@pytest.mark.parametrize(
    "op,values,expected",
    [
        ("In", ["a"], True),
        ("NotIn", ["a"], False),
        ("Exists", [], True),
        ("DoesNotExist", [], False),
    ],
)
def test_selector_expressions(op, values, expected):
    assert (
        k.matches(
            {"matchExpressions": [{"key": "team", "operator": op, "values": values}]}, {"team": "a"}
        )
        is expected
    )


def test_absence_requires_successful_discovery(monkeypatch):
    discovery = Mock()
    discovery.get_api_versions.return_value = NS(groups=[])
    monkeypatch.setattr(client, "ApisApi", lambda _: discovery)
    with pytest.raises(ApiError) as error:
        k.served_version(Mock())
    assert error.value.code == "kueue_not_installed"


@pytest.mark.parametrize("status", [403, 500])
def test_platform_check_failure_is_not_disabled(monkeypatch, status):
    monkeypatch.setattr(k.config, "load_incluster_config", lambda **_: None)
    with pytest.raises(ApiError) as error:
        with k.platform_client():
            raise ApiException(status=status, reason="private")
    assert error.value.status_code == 503
    assert "private" not in error.value.message


def test_rhbok_single_file_config_mount_and_empty_object_selector(installation, monkeypatch):
    dep, _, webhook = installation
    manager = dep.spec.template.spec.containers[0]
    manager.args = ["--config=/controller_manager_config.yaml"]
    manager.volume_mounts = [
        client.V1VolumeMount(
            name="config",
            mount_path="/controller_manager_config.yaml",
            sub_path="controller_manager_config.yaml",
        )
    ]
    webhook.object_selector = client.V1LabelSelector()

    @contextmanager
    def platform():
        with client.ApiClient() as api:
            yield api

    monkeypatch.setattr(k, "platform_client", platform)
    assert k.capability({"kueue.openshift.io/managed": "true"}) == "v1beta1"


def test_stale_config_does_not_claim_readiness(installation):
    installation[1].read_namespaced_config_map.return_value.data[
        "controller_manager_config.yaml"
    ] += "\n# edited after rollout\n"
    with pytest.raises(ApiError) as error:
        k.capability({"kueue.openshift.io/managed": "true"})
    assert error.value.status_code == 503


def test_missing_rollout_evidence_fails_closed(installation):
    installation[0].spec.template.metadata.annotations = {}
    with pytest.raises(ApiError) as error:
        k.capability({"kueue.openshift.io/managed": "true"})
    assert error.value.status_code == 503


def test_old_replicas_remaining_fail_closed(installation):
    installation[0].status.replicas = 2
    with pytest.raises(ApiError) as error:
        k.capability({"kueue.openshift.io/managed": "true"})
    assert error.value.status_code == 503
