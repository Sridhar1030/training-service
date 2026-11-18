"""Fail-closed Kueue capability checks shared by discovery and queued submission."""

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import yaml
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException
from urllib3.exceptions import HTTPError

from training_service.errors import ApiError
from training_service.kubernetes_client import TIMEOUT, describe_not_found

GROUP = "kueue.x-k8s.io"


def unavailable() -> ApiError:
    return ApiError(
        503,
        "backend_unavailable",
        "Kueue readiness could not be established. "
        "Contact the platform administrator or retry later.",
    )


@contextmanager
def platform_client() -> Iterator[client.ApiClient]:
    """Never substitute this identity for caller-owned project or queue reads."""
    try:
        configuration = client.Configuration()
        config.load_incluster_config(client_configuration=configuration)
        configuration.retries = 0
        with client.ApiClient(configuration) as api:
            yield api
    except (ApiException, ConfigException, HTTPError, OSError) as error:
        # A service-account denial is a deployment problem, not a caller denial.
        raise unavailable() from error


def matches(selector: Any, labels: Mapping[str, str]) -> bool:
    """Evaluate Kubernetes LabelSelector semantics; reject unknown configuration."""
    if not isinstance(selector, dict) or set(selector) - {"matchLabels", "matchExpressions"}:
        raise unavailable()
    pairs = selector.get("matchLabels", {})
    expressions = selector.get("matchExpressions", [])
    if not isinstance(pairs, dict) or not isinstance(expressions, list):
        raise unavailable()
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in pairs.items()):
        raise unavailable()
    result = all(labels.get(k) == v for k, v in pairs.items())
    for expression in expressions:
        if not isinstance(expression, dict):
            raise unavailable()
        key, op, values = (
            expression.get("key"),
            expression.get("operator"),
            expression.get("values", []),
        )
        if (
            not isinstance(key, str)
            or not isinstance(values, list)
            or not all(isinstance(value, str) for value in values)
        ):
            raise unavailable()
        if op in {"In", "NotIn"} and values:
            match = labels.get(key) in values if op == "In" else labels.get(key) not in values
        elif op in {"Exists", "DoesNotExist"} and not values:
            match = key in labels if op == "Exists" else key not in labels
        else:
            raise unavailable()
        result = result and match
    return result


def served_version(api: client.ApiClient) -> str:
    """Discover preferred served LocalQueue version, not a fixed beta-version chain."""
    groups = client.ApisApi(api).get_api_versions(_request_timeout=TIMEOUT)
    group = next((g for g in groups.groups if g.name == GROUP), None)
    if group is None:
        raise ApiError(409, "kueue_not_installed", "Kueue is not installed on this cluster.")
    versions = [v.version for v in group.versions]
    if group.preferred_version and group.preferred_version.version in versions:
        preferred = group.preferred_version.version
        versions = [preferred] + [v for v in versions if v != preferred]
    for version in versions:
        resources = api.call_api(
            f"/apis/{GROUP}/{version}",
            "GET",
            response_type="object",
            auth_settings=["BearerToken"],
            _return_http_data_only=True,
            _request_timeout=TIMEOUT,
        )
        if not isinstance(resources, dict) or not isinstance(resources.get("resources"), list):
            raise unavailable()
        if any(
            r.get("name") == "localqueues"
            and r.get("namespaced") is True
            and {"get", "list"} <= set(r.get("verbs", []))
            for r in resources["resources"]
        ):
            return str(version)
    # A retained/incompatible API is not proof of an absent installation.
    raise unavailable()


def capability(labels: Mapping[str, str]) -> str:
    """Read explicitly configured installation resources, independently of DSC."""
    namespace = os.getenv("TRAINING_KUEUE_NAMESPACE", "openshift-kueue-operator")
    deployment = os.getenv("TRAINING_KUEUE_DEPLOYMENT", "kueue-controller-manager")
    configmap = os.getenv("TRAINING_KUEUE_CONFIGMAP", "kueue-manager-config")
    webhook = os.getenv("TRAINING_KUEUE_WEBHOOK", "kueue-mutating-webhook-configuration")
    with platform_client() as api:
        version = served_version(api)
        dep = client.AppsV1Api(api).read_namespaced_deployment(
            deployment, namespace, _request_timeout=TIMEOUT
        )
        if not dep.status or (dep.status.available_replicas or 0) < 1:
            raise unavailable()
        cm = client.CoreV1Api(api).read_namespaced_config_map(
            configmap, namespace, _request_timeout=TIMEOUT
        )
        try:
            settings = yaml.safe_load((cm.data or {})["controller_manager_config.yaml"])
        except (KeyError, yaml.YAMLError) as error:
            raise unavailable() from error
        integrations = settings.get("integrations") if isinstance(settings, dict) else None
        if not isinstance(integrations, dict) or "ray.io/rayjob" not in (
            integrations.get("frameworks") or []
        ):
            raise unavailable()
        # Require explicit configuration instead of guessing version-dependent defaults.
        selector = settings.get("managedJobsNamespaceSelector")
        eligible = matches(selector, labels)
        configuration = client.AdmissionregistrationV1Api(api).read_mutating_webhook_configuration(
            webhook, _request_timeout=TIMEOUT
        )
        candidates = [
            w
            for w in configuration.webhooks or []
            if w.client_config
            and w.client_config.service
            and w.client_config.service.namespace == namespace
            and w.client_config.service.path == "/mutate-ray-io-v1-rayjob"
        ]
        if len(candidates) != 1:
            raise unavailable()
        admission = candidates[0]
        object_selector = api.sanitize_for_serialization(admission.object_selector) or {}
        if (
            admission.failure_policy != "Fail"
            or admission.match_conditions
            or not isinstance(object_selector, dict)
            or set(object_selector) - {"matchLabels", "matchExpressions"}
            or object_selector.get("matchLabels")
            or object_selector.get("matchExpressions")
        ):
            raise unavailable()
        if not any(
            "ray.io" in (r.api_groups or [])
            and "v1" in (r.api_versions or [])
            and "rayjobs" in (r.resources or [])
            and "CREATE" in (r.operations or [])
            for r in admission.rules or []
        ):
            raise unavailable()
        service_name = os.getenv("TRAINING_KUEUE_SERVICE", "kueue-webhook-service")
        if admission.client_config.service.name != service_name:
            raise unavailable()
        service = client.CoreV1Api(api).read_namespaced_service(
            service_name, namespace, _request_timeout=TIMEOUT
        )
        if not service.spec.selector or not all(
            dep.spec.template.metadata.labels.get(k) == v for k, v in service.spec.selector.items()
        ):
            raise unavailable()
        endpoints = client.CoreV1Api(api).read_namespaced_endpoints(
            service_name, namespace, _request_timeout=TIMEOUT
        )
        if not any(subset.addresses for subset in endpoints.subsets or []):
            raise unavailable()
        webhook_selector = api.sanitize_for_serialization(admission.namespace_selector) or {}
        eligible = matches(webhook_selector, labels) and eligible
        if not eligible:
            raise ApiError(
                409,
                "kueue_project_excluded",
                "The project is excluded by the installed Kueue namespace policy.",
            )
        return version


def preflight(api: client.ApiClient, project: str, queue: str | None = None) -> str:
    """Caller project access, platform capability, then optional caller queue lookup."""
    with describe_not_found("Project", project):
        namespace = client.CoreV1Api(api).read_namespace(project, _request_timeout=TIMEOUT)
    if namespace.metadata is None:
        raise unavailable()
    version = capability(namespace.metadata.labels or {})
    if queue is not None:
        with describe_not_found("LocalQueue", queue, project=project):
            client.CustomObjectsApi(api).get_namespaced_custom_object(
                GROUP,
                version,
                project,
                "localqueues",
                queue,
                _request_timeout=TIMEOUT,
            )
    return version
