# Training Hub parameters and discovery

The API supports **reviewed execution profiles**, not arbitrary Python kwargs or
every algorithm installed in Training Hub. New algorithms require an adapter,
an OpenAPI parameter schema, focused tests, and an administrator-approved image
catalog entry. No algorithm becomes available just because a library is upgraded.

## Supported execution profiles

| API name | Training Hub backend class selected through `create_algorithm` | Topology |
| --- | --- | --- |
| `sft` | `instructlab-training` | One Ray head; local torchrun processes derived from `gpus_per_node`. |
| `osft` | `mini-trainer` | One Ray head; local torchrun processes derived from `gpus_per_node`. |
| `lora_sft` | `unsloth` | One Ray head and one GPU, or explicit `enable_model_splitting=true` for multiple GPUs. No distributed worker launcher. |
| `lora_grpo` | `verl` | GPU-free head plus GPU workers; `n_gpus` and `nnodes` derived from resources. Shared RWX output PVC required. |

SFT, OSFT, and LoRA SFT reject `num_nodes > 1` with `422`. Creating idle Ray
workers is not distributed training. GRPO's `num_nodes` includes the head.
Algorithm parameters cannot override platform topology or backend selection.

The authoritative accepted parameter lists and value constraints are
`components.schemas.AlgorithmSpec.x-parameter-schemas` in
[`api/openapi.yaml`](api/openapi.yaml). `GET /api/v1/algorithms` returns those
same schemas for profiles enabled by the deployment's image catalog. A schema
catalog mismatch is an error, not a reason to advertise parameters POST rejects.

For example, inspect OSFT without guessing which parameters are supported:

```bash
curl --cacert "$ROUTE_CA" --fail --silent --show-error \
  -H "Authorization: Bearer $TOKEN" "$BASE_URL/api/v1/algorithms" |
  jq '.algorithms[] | select(.name == "osft") | .parameter_schema'
```

Cross-check the selected runtime's tagged Training Hub source, including the
**backend implementation**, not just its convenience-function signature:
[SFT](https://github.com/Red-Hat-AI-Innovation-Team/training_hub/blob/v0.11.2/src/training_hub/algorithms/sft.py),
[OSFT](https://github.com/Red-Hat-AI-Innovation-Team/training_hub/blob/v0.11.2/src/training_hub/algorithms/osft.py),
[LoRA SFT](https://github.com/Red-Hat-AI-Innovation-Team/training_hub/blob/v0.11.2/src/training_hub/algorithms/lora.py),
[LoRA GRPO](https://github.com/Red-Hat-AI-Innovation-Team/training_hub/blob/v0.11.2/src/training_hub/algorithms/lora_grpo_verl.py).

### Why some Training Hub arguments are not algorithm parameters

| Category | API handling |
| --- | --- |
| `model_path`, `data_path`, `ckpt_output_dir` | Resolved from the authorized model, dataset and output URI fields. |
| `data_output_dir` | Derived beneath the job output for SFT/OSFT. |
| `nproc_per_node`, `nnodes`, rank/master/rendezvous settings | Owned by the execution adapter and `resources`, not caller kwargs. Multi-node SFT/OSFT/LoRA is not implemented. |
| Tracking endpoints, W&B/MLflow names, TensorBoard paths | Not in this parameter contract; endpoint/credential and writable-path configuration needs separate support. They return `422`, not silent pass-through. |
| `trust_remote_code`, Python callbacks/reward/rollout functions | Not exposed; require a deliberate remote-code/security contract and cannot be accepted as arbitrary JSON code. |
| `enable_jit_checkpoint`, `checkpoint_storage`, `eval_data_path` | Not exposed by this profile; require additional runtime/version and authorized storage/input mapping. |
| LoRA `assistant_only_loss`, `flash_attention`, `tf32` | The reviewed backend does not consume these as configurable overrides. Do not advertise ignored kwargs. |
| LoRA `eval_steps` | Requires a separately configured evaluation dataset, which this request contract does not provide. |
| GRPO `tasks_per_iteration`, `temperature`, `concurrency`, `target_modules`, `max_grad_norm`, `max_lora_rank` | Not consumed as effective overrides by the reviewed verl execution path. `prompt_batch_size` controls prompts per step. |

These are explicit exclusions, **not full Training Hub feature parity**.
The schemas were reviewed against TH 0.8.1 and 0.11.2; this does not establish
runtime compatibility between their images and the API's pinned Ray/CodeFlare.

## Configure an image-bound release catalog

Discovery reads a ConfigMap through the caller's Kubernetes client. It does not
run containers to inspect images, trust an arbitrary image tag, or use the API
service account to bypass RBAC. The administrator must review the selected image
and publish its enabled profiles. Prefer an immutable image digest.

The chart can create the catalog ConfigMap and a narrowly scoped reader Role
and RoleBinding. With `algorithmCatalog.create=true`, it also sets the catalog
namespace/name variables and derives `TRAINING_RAY_IMAGE` from the catalog.
An existing `TRAINING_RAY_IMAGE` entry in `env` is allowed only if it matches
the catalog exactly; do not duplicate the namespace/name variables.

If an administrator manages the ConfigMap and RBAC outside Helm instead, set
these variables through the chart's `env` list:

```yaml
env:
  - name: TRAINING_RAY_IMAGE
    value: "<approved-compatible-image>@sha256:<digest>"
  - name: TRAINING_ALGORITHM_CATALOG_NAMESPACE
    value: training-service
  - name: TRAINING_ALGORITHM_CATALOG_NAME
    value: training-algorithm-catalog
```

After reviewing the image, generate its catalog from the API's reviewed schemas.
Explicitly choose profiles; this example enables three, **not GRPO**:

```bash
export TRAINING_RAY_IMAGE='<approved-compatible-image>@sha256:<digest>'
uv run python - <<'PY' > /tmp/training-algorithm-catalog.json
import json, os
from training_service.contract import load_contract
schemas = load_contract()["components"]["schemas"]["AlgorithmSpec"]["x-parameter-schemas"]
names = ["sft", "osft", "lora_sft"]  # Profiles validated for this exact image.
print(json.dumps({"image": os.environ["TRAINING_RAY_IMAGE"], "algorithms": [
    {"name": name, "parameter_schema": schemas[name]} for name in names
]}))
PY
```

For the chart-managed path, add this to your deployment values file, replacing
`training-users` with a group whose members are allowed to submit jobs:

```yaml
algorithmCatalog:
  create: true
  readerSubjects:
    - kind: Group
      name: training-users
      apiGroup: rbac.authorization.k8s.io
```

Install or upgrade the service with that values file and the generated catalog:

```bash
helm upgrade --install training-service charts/training-service \
  --namespace training-service --create-namespace \
  -f /path/to/values.yaml \
  --set-file algorithmCatalog.json=/tmp/training-algorithm-catalog.json
```

The catalog's `image` becomes `TRAINING_RAY_IMAGE`; it must name the approved
runtime image. The catalog is non-secret, but its Role grants only `get` on the
named ConfigMap to the chosen subjects. Callers outside those subjects receive
`403` with a catalog-specific message. If chart-managed creation is disabled,
the administrator must create the ConfigMap and caller RBAC before submitting.
For example, after setting the three `env` values above:

```bash
oc create configmap training-algorithm-catalog -n training-service \
  --from-file=catalog.json=/tmp/training-algorithm-catalog.json \
  --dry-run=client -o yaml | oc apply -f -
oc create role training-algorithm-catalog-reader -n training-service \
  --verb=get --resource=configmaps \
  --resource-name=training-algorithm-catalog \
  --dry-run=client -o yaml | oc apply -f -
oc create rolebinding training-algorithm-catalog-reader -n training-service \
  --role=training-algorithm-catalog-reader --group=training-users \
  --dry-run=client -o yaml | oc apply -f -
```

Replace `training-users` with an approved caller group. The chart-managed
path is preferred because Helm then owns and updates these resources together.

Callers need `get` permission on this **named ConfigMap** in its configured
namespace, in addition to existing project/workload permissions. No
cluster-admin or Secret access is needed for discovery.
Do not put credentials in the catalog. The service account retains only its
TokenReview/SubjectAccessReview permissions.

Both GET discovery and POST submission read this catalog with the caller's
identity. Unconfigured/missing catalogs, image mismatches and schema drift return
`503`; denied catalog reads return `403`; a profile not enabled in the catalog
returns `422` on POST. New upstream algorithms are filtered out until an API
adapter and schema are implemented. Update catalog schemas together with the API
release; do not reuse an old catalog after changing its parameter contract.

## Queue discovery

`GET /api/v1/projects/{project}/queues` checks project access, discovers served
Kueue API versions, authorizes LocalQueue listing, and returns only queue names
and their referenced ClusterQueue names. It does not read ClusterQueue quotas.
Both `v1beta1` and `v1beta2` are supported, preferring `v1beta2` when served.

An absent Kueue API returns `200` with `kueue_enabled: false` and `queues: []`
when `TRAINING_KUEUE_ENABLED=auto` (the default). `false` explicitly disables
this helper; `true` requires a served Kueue API and returns `503` if absent.
A denied/failed Kubernetes call is never reported as Kueue absence.
This setting does not change submission admission behavior. For an omitted
`queue_name`, the API resolves the default LocalQueue through the caller's
served Kueue API version and passes it explicitly to CodeFlare. Lookup errors
fail the submission instead of silently dropping queue management. If no
default queue exists, the job is submitted without a queue label, subject to
platform admission policy. Explicit queue submissions do not require a
LocalQueue list permission merely to resolve a default.

## Verification boundary

Focused tests mock Kubernetes and Training Hub and cover dispatch, parameter
preservation, catalog/schema/image agreement, caller identity, authorization,
queue pagination, Kueue absence, and documented error responses. They do not
require a live cluster. A `202` confirms RayJob creation, not completed training.
See the repository README for current live-runtime results and the unresolved
verl/TRL import failure in the TH 0.11.2 combined image. Do not enable GRPO in a
customer catalog until its selected runtime has been validated.
