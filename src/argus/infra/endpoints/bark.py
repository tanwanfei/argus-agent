import base64
import json
import logging
import secrets

import httpx2
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from argus.channels.push import PushError, PushLevel

logger = logging.getLogger(__name__)

_SERVER = "https://api.day.app"
_PUSH_PATH = "/push"
_TIMEOUT = httpx2.Timeout(10.0)
_KEY_BYTES = 32
_IV_RANDOM_BYTES = 9

_BARK_LEVELS = {
    PushLevel.NORMAL: "active",
    PushLevel.TIME_SENSITIVE: "timeSensitive",
}


class BarkPush:
    def __init__(
        self,
        device_key: str,
        encryption_key: str,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        key_bytes = encryption_key.encode("utf-8")
        if len(key_bytes) != _KEY_BYTES:
            raise ValueError(f"Bark 的 Key 要正好 {_KEY_BYTES} 个字符（AES-256）")
        self._device_key = device_key
        self._cipher = AESGCM(key_bytes)
        self._client = httpx2.AsyncClient(
            base_url=_SERVER,
            trust_env=False,
            timeout=_TIMEOUT,
            transport=transport,
        )

    async def push(
        self, title: str, body: str, *, url: str | None = None, level: PushLevel
    ) -> None:
        content = {"title": title, "body": body, "level": _BARK_LEVELS[level]}
        if url is not None:
            content["url"] = url
        iv = secrets.token_urlsafe(_IV_RANDOM_BYTES)
        plaintext = json.dumps(content, ensure_ascii=False).encode("utf-8")
        sealed = self._cipher.encrypt(iv.encode("utf-8"), plaintext, None)
        request_body = {
            "device_key": self._device_key,
            "ciphertext": base64.b64encode(sealed).decode("ascii"),
            "iv": iv,
        }
        try:
            response = await self._client.post(_PUSH_PATH, json=request_body)
        except httpx2.TimeoutException as error:
            logger.warning("等 Bark 服务器回复超时：%s", type(error).__name__)
            raise PushError("Bark 服务器没有及时回复") from error
        except httpx2.RequestError as error:
            logger.warning("连不上 Bark 服务器：%s", type(error).__name__)
            raise PushError("连不上 Bark 服务器，检查主机的网络") from error

        if response.status_code == 400:
            logger.warning("Bark 服务器返回 HTTP 400")
            raise PushError("Bark 服务器不认这台手机（HTTP 400），对一下 .env 的 BARK_DEVICE_KEY")
        if not response.is_success:
            logger.warning("Bark 服务器返回 HTTP %d", response.status_code)
            raise PushError(f"Bark 服务器没推出去，HTTP 状态码 {response.status_code}")
        logger.info("Bark 推送发出了（%s）", level)

    async def aclose(self) -> None:
        await self._client.aclose()
