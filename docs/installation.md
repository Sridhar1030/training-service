# Installation

The service is distributed as a Helm chart.

## Install from a checkout

    helm upgrade --install training-service ./charts/training-service \
      --namespace training-service \
      --create-namespace

To use a private image or a release tag:

    helm upgrade --install training-service ./charts/training-service \
      --namespace training-service \
      --set image.repository=quay.io/opendatahub/training-service \
      --set image.tag=<tag>

Check the deployment:

    kubectl -n training-service rollout status deployment/training-service
    kubectl -n training-service get service training-service

The chart exposes /healthz and /readyz on port 8080. Kubernetes access
permissions and backend configuration will be added together with the
corresponding API implementation; the initial chart intentionally does not
grant broad cluster permissions.
