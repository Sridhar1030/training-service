"""Credential-free checks for the opt-in remote-storage E2E harness."""

import io
import json
from copy import deepcopy
from pathlib import Path

import pytest
from tests.e2e.support import remote_workloads, s3_path
from tests.e2e.test_remote_storage import verify_s3_adapter

EXAMPLE = Path(__file__).resolve().parents[1] / "e2e/examples/remote-jobs.example.json"


def test_remote_fixture_covers_all_source_combinations():
    cases = remote_workloads(EXAMPLE)
    assert set(cases) == {
        "s3_model_s3_dataset",
        "s3_model_hf_dataset",
        "hf_model_s3_dataset",
        "hf_model_hf_dataset",
    }
    for name, payload in cases.items():
        assert payload["model"]["uri"].startswith(name[:2] + "://")
        assert payload["dataset"]["uri"].startswith(name.split("_model_")[1][:2] + "://")
        assert payload["output"]["destination_uri"].startswith("s3://")
        assert "name" not in payload


@pytest.mark.parametrize(
    "change,error",
    [
        (
            lambda data: data["base_request"]["output"].update(destination_uri="hf://token/o/r"),
            "s3",
        ),
        (
            lambda data: data["models"]["hf"].update(uri="hf://user:password@secret/o/r"),
            "credential-free",
        ),
        (lambda data: data["datasets"]["s3"].update(split="train"), "cannot use"),
        (lambda data: data["models"].pop("s3"), "exactly s3 and hf"),
    ],
)
def test_remote_fixture_rejects_invalid_references(tmp_path, change, error):
    data = deepcopy(json.loads(EXAMPLE.read_text()))
    change(data)
    path = tmp_path / "remote.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match=error):
        remote_workloads(path)


@pytest.mark.parametrize(
    "uri",
    [
        "hf://token/o/r",
        "s3://user:password@connection/bucket/output/",
        "s3://connection/bucket/../output/",
        "s3://connection/bucket/output/?token=secret",
    ],
)
def test_s3_output_parser_rejects_unsafe_uris(uri):
    with pytest.raises(ValueError):
        s3_path(uri)


class _S3:
    def __init__(self, objects):
        self.objects = objects

    def get_paginator(self, operation):
        assert operation == "list_objects_v2"
        return self

    def paginate(self, *, Bucket, Prefix):
        assert Bucket == "bucket"
        yield {
            "Contents": [
                {"Key": key, "Size": len(body)}
                for key, body in self.objects.items()
                if key.startswith(Prefix)
            ]
        }

    def get_object(self, *, Bucket, Key, Range=None):
        assert Bucket == "bucket"
        data = self.objects[Key]
        if Range:
            start, end = map(int, Range.removeprefix("bytes=").split("-"))
            data = data[start : end + 1]
        return {"Body": io.BytesIO(data)}


def test_s3_output_verifier_reads_adapter_config_and_safetensors():
    header = json.dumps(
        {"adapter.weight": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}
    ).encode()
    weights = len(header).to_bytes(8, "little") + header + b"\0" * 4
    prefix = "output/e2e-run/adapter/"
    client = _S3(
        {
            prefix + "adapter_config.json": json.dumps({"peft_type": "LORA", "r": 8}).encode(),
            prefix + "adapter_model.safetensors": weights,
        }
    )
    verify_s3_adapter("s3://connection/bucket/output/e2e-run/", client, "bucket")
    with pytest.raises(AssertionError, match="bucket differs"):
        verify_s3_adapter("s3://connection/bucket/output/e2e-run/", client, "other-bucket")
