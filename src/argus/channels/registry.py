import logging

from argus.channels.endpoint import Endpoint

logger = logging.getLogger(__name__)


class EndpointRegistry:
    def __init__(self) -> None:
        self._online_by_id: dict[str, Endpoint] = {}

    def register(self, endpoint: Endpoint) -> None:
        replaced = self._online_by_id.pop(endpoint.id, None)
        self._online_by_id[endpoint.id] = endpoint
        if replaced is None:
            logger.info("端点 %s 上线", endpoint.id)
        else:
            logger.info("端点 %s 重新连上，顶替了之前的连接", endpoint.id)

    def unregister(self, endpoint: Endpoint) -> None:
        if self._online_by_id.get(endpoint.id) is endpoint:
            del self._online_by_id[endpoint.id]
            logger.info("端点 %s 下线", endpoint.id)

    def get(self, endpoint_id: str) -> Endpoint | None:
        return self._online_by_id.get(endpoint_id)

    def online(self) -> tuple[Endpoint, ...]:
        return tuple(self._online_by_id.values())
