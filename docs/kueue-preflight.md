# Kueue discovery and queued submission

GET queues and POST with an explicit `queue_name` share a fail-closed preflight.
Authentication and project authorization run first. Namespace and LocalQueue reads
use the caller credential. Platform readiness reads use the service account and
only the named installation resources configured in `kueuePreflight` Helm values.
There are no DSC reads or dependencies, so an independently installed RHBoK is supported.

## Contract change

* 200: readiness and project eligibility passed; `kueue_enabled: true`.
  An eligible project with no queues returns `queues: []`.
* 409 `kueue_not_installed`: successful API discovery did not advertise Kueue.
* 409 `kueue_project_excluded`: effective configured selectors exclude the project.
* 404 `not_found`: caller project or explicitly selected LocalQueue is missing.
* 401/403: caller authentication/permission failure, including downstream failures.
* 503 `backend_unavailable`: incomplete/unavailable capability checks, unhealthy
  controller, unsupported configuration, or platform service-account permission failure.

This replaces the old absence response (`200`, `kueue_enabled: false`).
POST repeats preflight without requiring a preceding GET. Creation acknowledges
only the RayJob resource: admission remains asynchronous and may wait for quota,
GPUs or admission checks. The preflight is not an atomic guarantee: configuration
can change after any check; Kueue admission remains authoritative.

## Supported configuration and installation

Set the namespace, Deployment, ConfigMap, mutating webhook configuration, and
webhook Service names in the chart to match the actual installation. The chart
creates named-resource read-only permissions in that namespace, plus get on the
named webhook and Kueue discovery URLs. Callers need no platform/admin reads.
If Kueue is absent, disable `kueuePreflight.createRBAC` until its namespace exists
and provision platform discovery permissions separately as needed.

Checks require a current, available controller Deployment mounting the named
ConfigMap with `--config` referencing `controller_manager_config.yaml`, an explicit
`managedJobsNamespaceSelector`, and `ray.io/rayjob` in `integrations.frameworks`.
The RayJob CREATE mutating webhook must point to the configured Service, fail
closed, have ready endpoints, and select the controller's pod labels. Its namespace
selector is evaluated as well. Arbitrary CEL match conditions and object selectors
are unsupported and fail closed instead of claiming readiness. ConfigMap contents must match the RHBoK operator-generated
`configmap/<name>` pod-template digest, and the observed Deployment must have all
replicas updated and ready with no old replicas remaining. A changed ConfigMap,
missing digest, or incomplete rollout returns 503. This binds the inspected data
to the completed rollout instead of assuming edited data was loaded. Arbitrary
non-RHBoK installations without this rollout evidence need an adapter and fail closed.
No DSC integration is required. The hash follows the operator
[implementation](https://github.com/openshift/kueue-operator/blob/main/pkg/operator/target_config_reconciler.go):
SHA256 of Go JSON-encoded ConfigMap data.

## Pinned SDK verification

Inspected the Red Hat `codeflare_sdk-0.40.1-1-py3-none-any.whl` pinned in
`pyproject.toml` (SHA256 `2edca4173a6db7169e4708d26395982a4e91c2aa2bcdf58c8e24737959cde96d`).
The source path `codeflare_sdk/ray/rayjobs/rayjob.py`, `RayJob.submit()` calls
`_build_rayjob_cr()`. With a managed cluster and explicit `local_queue`, that
method directly sets `kueue.x-k8s.io/queue-name`. It does not call
`local_queue_exists`, validate the namespace selector, or inspect controller health.
It relies on mutation to suspend the job. Separate cluster/queue SDK helpers are
not evidence of checks on this path. Discovery uses Kubernetes directly, not SDK helpers.

## Read-only development-cluster evidence

On 2026-11-17, exercised discovery with actual `oc` reads from
`api.dev.aip-ft.rh-ods.com`. The platform client boundary was replaced with the
logged-in user's read-only `oc` transport for this check; this is not evidence of
service-account RBAC or a deployed HTTP endpoint.

* `training-service`: retained `queue-discovery-smoke` LocalQueue, but missing
  the installed selector's enrollment label; returned 409 `kueue_project_excluded`.
* `test-efazal`: already eligible; returned enabled and LocalQueue `pr11-review`
  backed by `default` through discovered `v1beta2`.
* The RHBoK controller mounts its config using a single-file `subPath`; this
  layout and the API's empty webhook object selector have regression coverage.

No cluster objects were changed and no RayJobs were submitted. Deployed HTTP,
service-account permissions, queued POST and actual admission still need end-to-end
validation. Two controller Deployments were observed, in `kueue-system` and
`openshift-kueue-operator`; the RayJob webhook targets the latter installation.

## Deployed HTTP canary verification (2026-11-17)

Subsequently deployed `kueue-review-check-training-service` in `training-service`
using the chart's service account and named-resource RBAC. The existing compatible
service image (CodeFlare 0.40.1, Ray 2.58.0) loaded this branch's complete source and
OpenAPI files from a read-only ConfigMap overlay. This validates deployed code/RBAC,
not a newly built release image. HTTPS calls validated the ingress certificate;
credentials were held in memory, not written into evidence.

| Check | Actual response |
| --- | --- |
| POST excluded project before any GET | 409 `kueue_project_excluded` |
| POST eligible project, missing LocalQueue, before GET | 404 `not_found`, LocalQueue message |
| POST existing eligible queue, intentionally missing PVC | 404 `not_found`, PVC message: queue preflight passed |
| GET excluded project retaining a LocalQueue | 409 `kueue_project_excluded` |
| GET eligible `test-efazal` | 200, enabled, `pr11-review` / `default` |
| Invalid bearer token | 401 `unauthorized` |
| Authenticated canary service account without project permission | 403 `forbidden` |

All expected statuses passed after fixing missing error-enum entries discovered by
the initial HTTP run. Regression tests now exercise serialization through the HTTP
error handler. The actual operator-generated ConfigMap digest matched the current
configuration; stale/missing digests and incomplete rollouts have regression tests.
Final local validation: 865 tests, lint, formatting, mypy and Helm lint passed.

No RayJobs were created by these negative/preflight tests; neither successful 202
submission nor actual Kueue admission/GPU execution was tested here. They are not
claimed by the evidence above. Temporary canary resources and its RBAC are cleaned
up after verification; existing services, queues and Kueue policy are unchanged.
