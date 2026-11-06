"""Opt-in real training with every S3/HF input combination and S3 output."""

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.e2e.support import remote_workloads, s3_path
from tests.e2e.test_training_jobs import lifecycle

CASES = (
    "s3_model_s3_dataset",
    "s3_model_hf_dataset",
    "hf_model_s3_dataset",
    "hf_model_hf_dataset",
)


def verify_s3_adapter(uri, client, expected_bucket):
    """Read a real LoRA checkpoint back from the exact unique S3 output prefix."""
    bucket, path = s3_path(uri)
    assert bucket == expected_bucket, "Output bucket differs from the S3 verifier bucket"
    prefix = path.rstrip("/") + "/"
    objects = {}
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            key = item["Key"]
            if key.startswith(prefix):
                objects[key] = item["Size"]
    configs = [key for key in objects if key.endswith("/adapter_config.json")]
    assert configs, "No LoRA adapter config was written to the S3 output prefix"
    for config_key in configs:
        weights_key = config_key.removesuffix("adapter_config.json") + "adapter_model.safetensors"
        if objects.get(weights_key, 0) <= 8:
            continue
        config_body = client.get_object(Bucket=bucket, Key=config_key)["Body"]
        try:
            config_bytes = config_body.read(1024 * 1024 + 1)
        finally:
            config_body.close()
        assert len(config_bytes) <= 1024 * 1024, "LoRA adapter config is unexpectedly large"
        config = json.loads(config_bytes)
        assert config.get("peft_type") == "LORA" and config.get("r", 0) > 0
        header_body = client.get_object(Bucket=bucket, Key=weights_key, Range="bytes=0-7")["Body"]
        try:
            header_bytes = header_body.read()
        finally:
            header_body.close()
        assert len(header_bytes) == 8, "S3 safetensors header is incomplete"
        header_size = int.from_bytes(header_bytes, "little")
        assert 0 < header_size < min(objects[weights_key] - 8, 16 * 1024 * 1024)
        tensors_body = client.get_object(
            Bucket=bucket, Key=weights_key, Range=f"bytes=8-{7 + header_size}"
        )["Body"]
        try:
            tensors = json.loads(tensors_body.read())
        finally:
            tensors_body.close()
        assert isinstance(tensors, dict), "S3 adapter safetensors header is not an object"
        entries = [value for key, value in tensors.items() if key != "__metadata__"]
        assert entries, "S3 adapter has no tensor data"
        for tensor in entries:
            start, end = tensor["data_offsets"]
            assert 0 <= start < end <= objects[weights_key] - 8 - header_size
            assert tensor["dtype"] and isinstance(tensor["shape"], list)
        return
    pytest.fail("No complete LoRA adapter checkpoint was readable from S3", pytrace=False)


def s3_verifier_client():
    import boto3

    kwargs = {
        "endpoint_url": os.environ["TRAINING_S3_E2E_ENDPOINT"],
        "aws_access_key_id": os.environ["TRAINING_S3_E2E_ACCESS_KEY"],
        "aws_secret_access_key": os.environ["TRAINING_S3_E2E_SECRET_KEY"],
        "verify": os.getenv("TRAINING_S3_E2E_CA_BUNDLE") or True,
    }
    if region := os.getenv("TRAINING_S3_E2E_REGION"):
        kwargs["region_name"] = region
    if token := os.getenv("TRAINING_S3_E2E_SESSION_TOKEN"):
        kwargs["aws_session_token"] = token
    return boto3.client("s3", **kwargs)


@pytest.fixture(params=CASES, ids=CASES)
def remote_job(api, pytestconfig, request):
    filename = pytestconfig.getoption("remote_fixtures_file")
    if not filename:
        pytest.skip("Remote training is opt-in: pass --remote-fixtures-file")
    payload = remote_workloads(filename)[request.param]
    job_id = "e2e-" + uuid.uuid4().hex[:16]
    payload["name"] = job_id
    payload["output"]["destination_uri"] += f"{job_id}/"
    api.request("GET", f"{api.jobs}/{job_id}", expected=404)
    print(f"Remote E2E job: {api.project}/{job_id} ({request.param})")
    try:
        yield job_id, payload
    finally:
        retained = False
        if pytestconfig.getoption("keep_failed_job"):
            response = api.request("GET", f"{api.jobs}/{job_id}", expected=(200, 404))
            if response.status_code == 200:
                retained = api.object(response).get("status") in {"FAILED", "STOPPED"}
        if retained:
            print(f"Retained failed remote job: {api.project}/{job_id}")
        else:
            api.cleanup(job_id, pytestconfig.getoption("cleanup_timeout"), payload)


def test_remote_inputs_with_s3_output(api, remote_job, pytestconfig):
    """POST to real API, finish training, then verify persisted S3 adapter after DELETE."""
    with ThreadPoolExecutor(max_workers=1) as executor:
        lifecycle(api, remote_job, pytestconfig, None, executor)
    _, payload = remote_job
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        verify_s3_adapter(
            payload["output"]["destination_uri"],
            s3_verifier_client(),
            os.environ["TRAINING_S3_E2E_BUCKET"],
        )
    except (BotoCoreError, ClientError):
        pytest.fail(
            "S3 output readback failed; check endpoint, credentials and bucket access",
            pytrace=False,
        )
