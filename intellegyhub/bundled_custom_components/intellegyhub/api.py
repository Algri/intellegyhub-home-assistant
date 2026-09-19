from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urljoin

from aiohttp import ClientError, ClientSession, WSMsgType


class IntellegyHubApiError(Exception):
    pass


class IntellegyHubApiClient:
    def __init__(self, session: ClientSession, base_url: str) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/") + "/"

    async def health(self) -> None:
        async with self.session.get(urljoin(self.base_url, "health"), timeout=10) as response:
            if response.status != 200:
                raise IntellegyHubApiError(f"Health check failed with HTTP {response.status}")
            payload = await response.json()
            if payload.get("status") != "ok":
                raise IntellegyHubApiError("Backend health status is not ok")

    async def state(self) -> dict[str, Any]:
        async with self.session.get(urljoin(self.base_url, "api/v1/state"), timeout=10) as response:
            if response.status != 200:
                raise IntellegyHubApiError(f"State request failed with HTTP {response.status}")
            payload = await response.json()
        self._validate_state(payload)
        return payload

    async def set_led(self, on: bool) -> bool:
        async with self.session.put(urljoin(self.base_url, "api/v1/led"), json={"on": on}, timeout=10) as response:
            if response.status != 200:
                raise IntellegyHubApiError(f"LED command failed with HTTP {response.status}")
            payload = await response.json()
        if not isinstance(payload.get("on"), bool):
            raise IntellegyHubApiError("Invalid LED response")
        return payload["on"]

    async def set_xport_mode(self, channel: int, mode: str, revision: int | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"mode": mode}
        if revision is not None:
            body["revision"] = revision
        async with self.session.put(
            urljoin(self.base_url, f"api/v1/xport/channels/{channel}/mode"),
            json=body,
            timeout=10,
        ) as response:
            if response.status != 200:
                raise IntellegyHubApiError(f"X-Port mode command failed with HTTP {response.status}")
            return await response.json()

    async def set_xport_value(self, channel: int, value: float) -> dict[str, Any]:
        async with self.session.put(
            urljoin(self.base_url, f"api/v1/xport/channels/{channel}/value"),
            json={"value": value},
            timeout=10,
        ) as response:
            if response.status != 200:
                raise IntellegyHubApiError(f"X-Port value command failed with HTTP {response.status}")
            return await response.json()

    async def reset_xport_counter(self, channel: int) -> dict[str, Any]:
        async with self.session.post(
            urljoin(self.base_url, f"api/v1/xport/channels/{channel}/counter/reset"),
            timeout=10,
        ) as response:
            if response.status != 200:
                raise IntellegyHubApiError(f"X-Port counter reset failed with HTTP {response.status}")
            return await response.json()

    async def set_extension_power(self, on: bool) -> dict[str, Any]:
        async with self.session.put(
            urljoin(self.base_url, "api/v1/extensions/power"),
            json={"on": on},
            timeout=10,
        ) as response:
            if response.status != 200:
                raise IntellegyHubApiError(f"Extension power command failed with HTTP {response.status}")
            return await response.json()

    async def scan_extensions(self) -> dict[str, Any]:
        async with self.session.post(urljoin(self.base_url, "api/v1/extensions/scan"), timeout=20) as response:
            if response.status != 200:
                raise IntellegyHubApiError(f"Extension scan failed with HTTP {response.status}")
            return await response.json()

    async def set_extension_relay(self, module_id: str, channel: int, on: bool) -> dict[str, Any]:
        async with self.session.put(
            urljoin(self.base_url, f"api/v1/extensions/modules/{module_id}/relays/{channel}"),
            json={"on": on},
            timeout=10,
        ) as response:
            if response.status != 200:
                raise IntellegyHubApiError(f"Extension relay command failed with HTTP {response.status}")
            return await response.json()

    async def delete_extension_module(self, module_id: str) -> dict[str, Any]:
        async with self.session.delete(
            urljoin(self.base_url, f"api/v1/extensions/modules/{module_id}"),
            timeout=10,
        ) as response:
            if response.status != 200:
                raise IntellegyHubApiError(f"Extension module delete failed with HTTP {response.status}")
            return await response.json()

    async def set_onewire_power(self, on: bool) -> dict[str, Any]:
        async with self.session.put(
            urljoin(self.base_url, "api/v1/onewire/power"),
            json={"on": on},
            timeout=10,
        ) as response:
            if response.status != 200:
                raise IntellegyHubApiError(f"1-Wire power command failed with HTTP {response.status}")
            return await response.json()

    async def scan_onewire(self) -> dict[str, Any]:
        async with self.session.post(urljoin(self.base_url, "api/v1/onewire/scan"), timeout=30) as response:
            if response.status != 200:
                raise IntellegyHubApiError(f"1-Wire scan failed with HTTP {response.status}")
            return await response.json()

    async def refresh_onewire(self) -> dict[str, Any]:
        async with self.session.post(urljoin(self.base_url, "api/v1/onewire/refresh"), timeout=30) as response:
            if response.status != 200:
                raise IntellegyHubApiError(f"1-Wire refresh failed with HTTP {response.status}")
            return await response.json()

    async def delete_onewire_sensor(self, sensor_id: str) -> dict[str, Any]:
        async with self.session.delete(
            urljoin(self.base_url, f"api/v1/onewire/sensors/{sensor_id}"),
            timeout=10,
        ) as response:
            if response.status != 200:
                raise IntellegyHubApiError(f"1-Wire sensor delete failed with HTTP {response.status}")
            return await response.json()

    async def listen(self) -> AsyncIterator[dict[str, Any]]:
        ws_url = self.base_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + "ws"
        try:
            async with self.session.ws_connect(ws_url, heartbeat=30) as websocket:
                async for message in websocket:
                    if message.type == WSMsgType.TEXT:
                        payload = message.json()
                        if isinstance(payload, dict):
                            yield payload
                    elif message.type in {WSMsgType.CLOSED, WSMsgType.ERROR}:
                        break
        except ClientError as exc:
            raise IntellegyHubApiError(str(exc)) from exc

    @staticmethod
    def _validate_state(payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise IntellegyHubApiError("Invalid state payload")
        if not isinstance(payload.get("led", {}).get("on"), bool):
            raise IntellegyHubApiError("Invalid LED state payload")
        if not isinstance(payload.get("button", {}).get("pressed"), bool):
            raise IntellegyHubApiError("Invalid button state payload")
        if "xport" in payload and not isinstance(payload["xport"], dict):
            raise IntellegyHubApiError("Invalid X-Port state payload")
        if "extensions" in payload and not isinstance(payload["extensions"], dict):
            raise IntellegyHubApiError("Invalid extensions state payload")
        if "onewire" in payload and not isinstance(payload["onewire"], dict):
            raise IntellegyHubApiError("Invalid 1-Wire state payload")
