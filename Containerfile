FROM registry.access.redhat.com/ubi9/python-312@sha256:f43a57e9a6a2243e333008219002b9f2b1661868a94199f5dec4d602e1501482

WORKDIR /opt/app-root/src

COPY pyproject.toml README.md ./
COPY src ./src
COPY docs/api/openapi.yaml ./docs/api/openapi.yaml

# Install the RHOAI 3.6 Ray wheel required by CodeFlare SDK 0.40.1.
# Verify the product-index hash before installing, including after a resumed download.
RUN set -eu; \
    case "$(uname -m)" in \
      x86_64) wheel_arch=x86_64; ray_sha=b68a267d6c5f4c44c89a67f40d9e54b0cec1dc5af5b569728506fdcd1db11f07 ;; \
      aarch64) wheel_arch=aarch64; ray_sha=7a1355c2a04a039f9476be3123e114e6606af89c0f42c8ef3797a9b558d6c43c ;; \
      *) echo "Unsupported architecture for RHOAI Ray wheel" >&2; exit 1 ;; \
    esac; \
    wheel="ray-2.58.0-1-cp312-cp312-linux_${wheel_arch}.whl"; \
    url="https://packages.redhat.com/api/pulp-content/public-rhai/rhoai/3.6/cpu-ubi9/${wheel}"; \
    for attempt in 1 2 3; do \
      if curl -fsSL --retry 3 --retry-all-errors --retry-delay 2 --continue-at - \
        --output "/tmp/${wheel}" "${url}" && \
        printf '%s  %s\n' "${ray_sha}" "/tmp/${wheel}" | sha256sum -c -; then \
        break; \
      fi; \
      rm -f "/tmp/${wheel}"; \
    done; \
    printf '%s  %s\n' "${ray_sha}" "/tmp/${wheel}" | sha256sum -c -; \
    pip install --no-cache-dir \
      "ray[data,default] @ file:///tmp/${wheel}" \
      "colorama @ https://packages.redhat.com/api/pulp-content/public-rhai/rhoai/3.6/cpu-ubi9/colorama-0.4.6-1-py2.py3-none-any.whl#sha256=bde866cb771ec23ed0415c915adc359fc57577b7b07821a71f621aed7fc222bb"; \
    rm -f "/tmp/${wheel}"; \
    pip install --no-cache-dir .

USER 1001
EXPOSE 8080

CMD ["uvicorn", "training_service.main:app", "--host", "0.0.0.0", "--port", "8080"]
