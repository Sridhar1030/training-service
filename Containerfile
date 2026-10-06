FROM registry.access.redhat.com/ubi9/python-311

WORKDIR /opt/app-root/src

COPY pyproject.toml README.md ./
COPY src ./src
COPY docs/api/openapi.yaml ./docs/api/openapi.yaml

RUN pip install --no-cache-dir .

USER 1001
EXPOSE 8080

CMD ["uvicorn", "training_service.main:app", "--host", "0.0.0.0", "--port", "8080"]
