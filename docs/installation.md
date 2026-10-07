# Installation and development image workflow

The Training Service is distributed as a Helm chart. The chart installs the
service into an existing OpenShift cluster; it does not install RHOAI, Ray, or
Kueue.

## Prerequisites

- Python 3.11 and `uv` for local checks.
- Podman or Docker for image builds.
- Helm 3 for chart validation and installation.
- `kubectl` or `oc` access to the target cluster.
- Push access to a registry that the target cluster can pull from.

## Build and publish a development image

Run the repository checks before building an image:

```bash
make check
```

Build and push an explicitly tagged image. Do not use `latest` for a shared
development or customer handoff:

```bash
export IMAGE_REPOSITORY=quay.io/<your-user>/training-service
export IMAGE_TAG=dev-$(git rev-parse --short HEAD)
export IMAGE_PLATFORM=linux/amd64

make image-build \
  IMAGE_REPOSITORY="$IMAGE_REPOSITORY" \
  IMAGE_TAG="$IMAGE_TAG" \
  IMAGE_PLATFORM="$IMAGE_PLATFORM"
make image-smoke IMAGE_REPOSITORY="$IMAGE_REPOSITORY" IMAGE_TAG="$IMAGE_TAG"
make image-push IMAGE_REPOSITORY="$IMAGE_REPOSITORY" IMAGE_TAG="$IMAGE_TAG"
```

`make image-smoke` starts the just-built image, verifies `/healthz` and
`/readyz`, and removes the temporary container. The Containerfile packages the
runnable FastAPI service from `src/`, including the generated API scaffold from
RHOAIENG-96348. The scaffold registers the contract routes under `/api/v1`; its
unimplemented operations return `501` until their runtime integrations land.

## Validate the chart

```bash
make helm-lint
make helm-template
```

`make helm-template` validates both the default internal-Service rendering and
an OpenShift Route/TLS rendering. It does not install anything on a cluster.

## Install or upgrade from a checkout

Use the exact image tag that was pushed above:

```bash
helm upgrade --install training-service ./charts/training-service \
  --namespace training-service \
  --create-namespace \
  --set-string image.repository="$IMAGE_REPOSITORY" \
  --set-string image.tag="$IMAGE_TAG"
```

For a private registry, create the pull secret first and pass it through chart
values:

```bash
oc get namespace training-service >/dev/null 2>&1 || \
  oc create namespace training-service

oc -n training-service create secret docker-registry training-service-pull \
  --docker-server=quay.io \
  --docker-username='<username>' \
  --docker-password='<token>'

helm upgrade --install training-service ./charts/training-service \
  --namespace training-service \
  --create-namespace \
  --set-string image.repository="$IMAGE_REPOSITORY" \
  --set-string image.tag="$IMAGE_TAG" \
  --set 'imagePullSecrets[0].name=training-service-pull'
```

Prefer a checked-in, environment-specific values file over a long list of
`--set` arguments when configuring replicas, resources, service settings, or
runtime environment variables:

```yaml
replicaCount: 1

image:
  repository: quay.io/example/training-service
  tag: dev-abcdef0

resources:
  requests:
    cpu: 100m
    memory: 256Mi
  limits:
    cpu: 1
    memory: 1Gi

env:
  - name: EXAMPLE_RUNTIME_SETTING
    value: example
```

The stable authentication, authorization, and SDK runtime variable names are
owned by RHOAIENG-96350 through RHOAIENG-96352. Do not add privileged service
account permissions or embed credentials in values while that design is
pending. Use `envFrom` with project-local Secret or ConfigMap references after
those contracts are defined.

## Verify rollout and health

```bash
kubectl -n training-service rollout status deployment/training-service
kubectl -n training-service get deployment,pod,service

kubectl -n training-service port-forward service/training-service 8080:8080
```

In another terminal:

```bash
curl --fail http://127.0.0.1:8080/healthz
curl --fail http://127.0.0.1:8080/readyz
```

These health checks verify the packaged process. Final delivery verification
should also confirm that the generated API routes are served under `/api/v1`.
Until the backend dependencies are implemented, a `501` response from an API
operation confirms the generated scaffold is present; it does not prove the
operation itself is ready for customer use.

## Optional OpenShift Route and TLS

Route exposure is disabled by default. Do not enable it until caller
authentication and project-scoped authorization are ready for the target
environment.

```yaml
route:
  enabled: true
  host: training-service.apps.example.com
  tls:
    enabled: true
    termination: edge
    insecureEdgeTerminationPolicy: Redirect
```

Install with the values file and inspect the resulting route:

```bash
helm upgrade --install training-service ./charts/training-service \
  --namespace training-service \
  --create-namespace \
  --values values-development.yaml

oc -n training-service get route training-service
```

## Versioned customer handoff

A handoff is a matched pair: a versioned image and a chart whose `appVersion`
identifies that same image version. Package the chart only after the exact image
tag has been built, pushed, and smoke-checked:

```bash
export RELEASE_VERSION=0.1.0

make helm-package \
  CHART_VERSION="$RELEASE_VERSION" \
  APP_VERSION="$RELEASE_VERSION"
```

The handoff record should contain:

- Image reference and immutable digest, for example
  `quay.io/opendatahub/training-service:0.1.0@sha256:<digest>`.
- Chart archive, for example `dist/training-service-0.1.0.tgz`.
- Chart version and `appVersion`.
- Values file used for the verified installation.
- Cluster/RHOAI compatibility and the smoke-check result.

Do not label a pair as customer-ready until the required runtime/security
dependencies are integrated and the exact pair has been verified together.
