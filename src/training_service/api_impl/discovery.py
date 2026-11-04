"""Infrastructure discovery handlers; no SDK or privileged fallback."""

import anyio
from fastapi import Request

from training_service import discovery
from training_service.authorization import authorize_project_request
from training_service_api.apis.infrastructure_discovery_api_base import (
    BaseInfrastructureDiscoveryApi,
)
from training_service_api.models.extra_models import TokenModel
from training_service_api.models.list_algorithms200_response import ListAlgorithms200Response
from training_service_api.models.queue_info_response import QueueInfoResponse


class InfrastructureDiscoveryApi(BaseInfrastructureDiscoveryApi):
    async def list_algorithms(
        self, token_OpenShiftBearer: TokenModel, request: Request
    ) -> ListAlgorithms200Response:
        result = await anyio.to_thread.run_sync(discovery.list_algorithms, request)
        return ListAlgorithms200Response.model_validate(result)

    async def list_queues(
        self, project: str, token_OpenShiftBearer: TokenModel, request: Request
    ) -> QueueInfoResponse:
        version = await anyio.to_thread.run_sync(discovery.queue_api_version, request, project)
        if version is None:
            return QueueInfoResponse(kueue_enabled=False, queues=[])
        await authorize_project_request(
            request, project, group=discovery.KUEUE_GROUP, resource="localqueues", verb="list"
        )
        result = await anyio.to_thread.run_sync(discovery.list_queues, request, project, version)
        return QueueInfoResponse.model_validate(result)
