"""Small, credential-free workloads for every supported Training Hub backend."""

from copy import deepcopy

ALGORITHMS = ("sft", "osft", "lora_sft", "lora_grpo")

DATASETS = {
    "sft": "tiny-chat.jsonl",
    "osft": "tiny-chat.jsonl",
    "lora_sft": "tiny-chat.jsonl",
    "lora_grpo": "tiny-math.jsonl",
}

PARAMETERS = {
    "sft": {
        "num_epochs": 1,
        "effective_batch_size": 1,
        "max_seq_len": 128,
        "max_tokens_per_gpu": 128,
        "learning_rate": 0.00001,
        "checkpoint_at_epoch": True,
    },
    "osft": {
        "unfreeze_rank_ratio": 0.25,
        "num_epochs": 1,
        "effective_batch_size": 1,
        "max_seq_len": 128,
        "max_tokens_per_gpu": 128,
        "learning_rate": 0.000005,
        "save_final_checkpoint": True,
    },
    "lora_sft": {
        "dataset_type": "chat_template",
        "field_messages": "messages",
        "num_epochs": 1,
        "effective_batch_size": 1,
        "max_seq_len": 128,
        "learning_rate": 0.0001,
        "logging_steps": 1,
    },
    "lora_grpo": {
        "reward_type": "math",
        "n_train": 8,
        "n_val": 4,
        "num_iterations": 1,
        "group_size": 4,
        "prompt_batch_size": 4,
    },
}


def algorithm_workload(base: dict, algorithm: str) -> dict:
    """Adapt the shared PVC fixture without changing the caller's template."""
    if algorithm not in ALGORITHMS:
        raise ValueError(f"Unsupported E2E algorithm: {algorithm}")
    payload = deepcopy(base)
    payload["algorithm"] = {"name": algorithm, "parameters": deepcopy(PARAMETERS[algorithm])}
    dataset = payload["dataset"]["uri"]
    if not dataset.startswith("pvc://"):
        raise ValueError("Algorithm matrix requires a PVC dataset under /datasets/")
    claim, separator, path = dataset.partition("/datasets/")
    if not separator or not path:
        raise ValueError("Algorithm matrix requires a PVC dataset under /datasets/")
    payload["dataset"]["uri"] = f"{claim}/datasets/{DATASETS[algorithm]}"
    resources = payload.get("resources")
    if not isinstance(resources, dict):
        raise ValueError("Algorithm matrix requires resources in the job template")
    if algorithm == "lora_grpo":
        if not payload["output"]["destination_uri"].startswith("pvc://"):
            raise ValueError("lora_grpo matrix training requires a shared PVC output")
        resources["num_nodes"] = 2
        resources["gpus_per_node"] = 1
    else:
        resources["num_nodes"] = 1
        resources["gpus_per_node"] = 1
    return payload
