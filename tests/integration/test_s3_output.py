"""Opt-in check against a real S3-compatible endpoint.

Run with ``pytest tests/integration/test_s3_output.py`` after setting the
TRAINING_S3_E2E_* variables. This is deliberately excluded from the default
unit suite because it writes to an external bucket.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from training_service.ray_entrypoint import _upload_s3


def test_checkpoint_objects_are_uploaded_and_readable(tmp_path: Path) -> None:
    names = (
        "TRAINING_S3_E2E_ENDPOINT",
        "TRAINING_S3_E2E_BUCKET",
        "TRAINING_S3_E2E_ACCESS_KEY",
        "TRAINING_S3_E2E_SECRET_KEY",
    )
    if not all(os.environ.get(name) for name in names):
        pytest.skip("Set the TRAINING_S3_E2E_* endpoint, bucket, and credentials to run.")

    boto3 = pytest.importorskip("boto3")
    endpoint, bucket, access_key, secret_key = (os.environ[name] for name in names)
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "AWS_S3_ENDPOINT").write_text(endpoint)
    (credentials / "AWS_ACCESS_KEY_ID").write_text(access_key)
    (credentials / "AWS_SECRET_ACCESS_KEY").write_text(secret_key)
    if region := os.environ.get("TRAINING_S3_E2E_REGION"):
        (credentials / "AWS_DEFAULT_REGION").write_text(region)
    if session_token := os.environ.get("TRAINING_S3_E2E_SESSION_TOKEN"):
        (credentials / "AWS_SESSION_TOKEN").write_text(session_token)

    output = tmp_path / "output"
    adapter = output / "adapter"
    adapter.mkdir(parents=True)
    content = b"checkpoint-upload-e2e"
    (adapter / "weights.safetensors").write_bytes(content)
    prefix = f"training-api-e2e/{uuid4().hex}/"
    key = prefix + "adapter/weights.safetensors"
    location = {
        "scheme": "s3",
        "credential_dir": str(credentials),
        "bucket": bucket,
        "path": prefix,
    }
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region if region else None,
        aws_session_token=session_token if session_token else None,
    )
    try:
        _upload_s3(location, output)
        objects = client.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents", [])
        assert [item["Key"] for item in objects] == [key]
        assert client.get_object(Bucket=bucket, Key=key)["Body"].read() == content
    finally:
        client.delete_object(Bucket=bucket, Key=key)
