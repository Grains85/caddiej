"""Async OBS adapter with simpleobsws contained as a transport detail."""

import asyncio
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol

import simpleobsws


class ObsConnectionError(RuntimeError):
    """Raised when OBS cannot be reached or a request fails."""


class ObsTransportConnectionError(ObsConnectionError):
    """Raised when the TCP/WebSocket connection to OBS cannot be established."""


class ObsIdentificationError(ObsConnectionError):
    """Raised when OBS rejects or does not complete identification."""


class ObsAuthenticationError(ObsIdentificationError):
    """Raised when OBS rejects the supplied WebSocket password."""


class ObsRequestError(ObsConnectionError):
    """Raised when OBS rejects a CaddieJ operation."""


class ObsResourceNotFoundError(ObsRequestError):
    """Raised when a configured OBS scene or source cannot be found."""


@dataclass(frozen=True, slots=True)
class ObsVersionInfo:
    """Version information reported by OBS WebSocket."""

    obs_version: str
    websocket_version: str
    rpc_version: int | None


BROWSER_SOURCE_KIND = "browser_source"


class _ObsTransport(Protocol):
    async def connect(self, timeout: float) -> None: ...

    async def get_version(self, timeout: float) -> Mapping[str, Any]: ...

    async def set_text(self, source_name: str, text: str, timeout: float) -> None: ...

    async def set_input_settings(
        self,
        source_name: str,
        settings: Mapping[str, Any],
        timeout: float,
    ) -> None: ...

    async def get_input_default_settings(
        self,
        input_kind: str,
        timeout: float,
    ) -> Mapping[str, Any]: ...

    async def create_input(
        self,
        scene_name: str,
        source_name: str,
        input_kind: str,
        settings: Mapping[str, Any],
        timeout: float,
    ) -> None: ...

    async def get_scene_item_id(
        self,
        scene_name: str,
        source_name: str,
        timeout: float,
    ) -> int: ...

    async def set_scene_item_enabled(
        self,
        scene_name: str,
        scene_item_id: int,
        visible: bool,
        timeout: float,
    ) -> None: ...

    async def disconnect(self) -> None: ...


class _SimpleObsWsTransport:
    """Thin transport wrapper; simpleobsws does not escape this adapter."""

    def __init__(self, url: str, password: str) -> None:
        identification_parameters = simpleobsws.IdentificationParameters(
            ignoreNonFatalRequestChecks=False
        )
        self._client = simpleobsws.WebSocketClient(
            url=url,
            password=password,
            identification_parameters=identification_parameters,
        )

    async def connect(self, timeout: float) -> None:
        try:
            async with asyncio.timeout(timeout):
                await self._client.connect()
        except TimeoutError as error:
            raise ObsTransportConnectionError(
                "Timed out establishing the OBS WebSocket connection"
            ) from error
        except Exception as error:
            raise ObsTransportConnectionError(
                f"Could not establish the OBS WebSocket connection: {error}"
            ) from error

        try:
            identified = await self._client.wait_until_identified(timeout=timeout)
        except Exception as error:
            raise ObsIdentificationError(
                f"OBS WebSocket identification failed: {error}"
            ) from error

        if not identified:
            raise self._identification_failure()

    async def get_version(self, timeout: float) -> Mapping[str, Any]:
        response = await self._client.call(
            simpleobsws.Request("GetVersion"),
            timeout=timeout,
        )
        if not response.ok():
            comment = response.requestStatus.comment or "unknown OBS error"
            raise ObsConnectionError(f"OBS GetVersion request failed: {comment}")
        return response.responseData or {}

    async def set_text(self, source_name: str, text: str, timeout: float) -> None:
        await self._apply_input_settings(
            source_name,
            {"text": text},
            timeout,
            action="Updating OBS text",
        )

    async def set_input_settings(
        self,
        source_name: str,
        settings: Mapping[str, Any],
        timeout: float,
    ) -> None:
        await self._apply_input_settings(
            source_name,
            settings,
            timeout,
            action="Updating OBS input settings",
        )

    async def _apply_input_settings(
        self,
        source_name: str,
        settings: Mapping[str, Any],
        timeout: float,
        *,
        action: str,
    ) -> None:
        response = await self._client.call(
            simpleobsws.Request(
                "SetInputSettings",
                {
                    "inputName": source_name,
                    "inputSettings": dict(settings),
                    "overlay": True,
                },
            ),
            timeout=timeout,
        )
        self._ensure_request_succeeded(
            response,
            action=action,
            not_found_message=f"OBS source '{source_name}' was not found",
        )

    async def get_input_default_settings(
        self,
        input_kind: str,
        timeout: float,
    ) -> Mapping[str, Any]:
        response = await self._client.call(
            simpleobsws.Request("GetInputDefaultSettings", {"inputKind": input_kind}),
            timeout=timeout,
        )
        self._ensure_request_succeeded(
            response,
            action="Reading OBS input defaults",
            not_found_message=f"OBS input kind '{input_kind}' is not available",
        )
        payload = (response.responseData or {}).get("defaultInputSettings", {})
        return payload if isinstance(payload, dict) else {}

    async def create_input(
        self,
        scene_name: str,
        source_name: str,
        input_kind: str,
        settings: Mapping[str, Any],
        timeout: float,
    ) -> None:
        response = await self._client.call(
            simpleobsws.Request(
                "CreateInput",
                {
                    "sceneName": scene_name,
                    "inputName": source_name,
                    "inputKind": input_kind,
                    "inputSettings": dict(settings),
                    "sceneItemEnabled": True,
                },
            ),
            timeout=timeout,
        )
        self._ensure_request_succeeded(
            response,
            action="Creating OBS input",
            not_found_message=f"OBS scene '{scene_name}' was not found",
        )

    async def get_scene_item_id(
        self,
        scene_name: str,
        source_name: str,
        timeout: float,
    ) -> int:
        response = await self._client.call(
            simpleobsws.Request(
                "GetSceneItemId",
                {"sceneName": scene_name, "sourceName": source_name},
            ),
            timeout=timeout,
        )
        self._ensure_request_succeeded(
            response,
            action="Resolving OBS scene item",
            not_found_message=(
                f"OBS source '{source_name}' was not found in scene '{scene_name}'"
            ),
        )
        scene_item_id = (response.responseData or {}).get("sceneItemId")
        if not isinstance(scene_item_id, int):
            raise ObsRequestError("OBS returned an invalid scene item identifier")
        return scene_item_id

    async def set_scene_item_enabled(
        self,
        scene_name: str,
        scene_item_id: int,
        visible: bool,
        timeout: float,
    ) -> None:
        response = await self._client.call(
            simpleobsws.Request(
                "SetSceneItemEnabled",
                {
                    "sceneName": scene_name,
                    "sceneItemId": scene_item_id,
                    "sceneItemEnabled": visible,
                },
            ),
            timeout=timeout,
        )
        self._ensure_request_succeeded(
            response,
            action="Changing OBS scene item visibility",
            not_found_message=f"OBS scene '{scene_name}' was not found",
        )

    async def disconnect(self) -> None:
        await self._client.disconnect()

    @staticmethod
    def _ensure_request_succeeded(
        response: Any,
        *,
        action: str,
        not_found_message: str,
    ) -> None:
        if response.ok():
            return

        status = response.requestStatus
        if status.code == 600:
            raise ObsResourceNotFoundError(not_found_message)

        comment = status.comment or "unknown OBS error"
        raise ObsRequestError(f"{action} failed: {comment} (code {status.code})")

    def _identification_failure(self) -> ObsIdentificationError:
        websocket = self._client.ws
        close_code = getattr(websocket, "close_code", None)
        close_reason = getattr(websocket, "close_reason", None)

        if close_code == 4009 or (
            isinstance(close_reason, str)
            and "authentication" in close_reason.casefold()
        ):
            code_suffix = (
                f" (close code {close_code})" if close_code is not None else ""
            )
            return ObsAuthenticationError(
                f"OBS WebSocket authentication failed{code_suffix}"
            )

        details = []
        if close_code is not None:
            details.append(f"close code {close_code}")
        if close_reason:
            details.append(f"reason: {close_reason}")

        suffix = f" ({', '.join(details)})" if details else ""
        return ObsIdentificationError(
            f"OBS WebSocket identification did not complete{suffix}"
        )


TransportFactory = Callable[[str, str], _ObsTransport]


class ObsClient:
    """CaddieJ-facing async client for the limited OBS operations in scope."""

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        connect_timeout: float,
        *,
        transport_factory: TransportFactory = _SimpleObsWsTransport,
    ) -> None:
        self._url = f"ws://{host}:{port}"
        self._connect_timeout = connect_timeout
        self._transport = transport_factory(self._url, password)
        self._connected = False
        self._disconnect_needed = False

    async def connect(self) -> None:
        self._disconnect_needed = True
        try:
            await self._transport.connect(self._connect_timeout)
        except ObsConnectionError:
            await self._disconnect_after_error()
            raise
        except Exception as error:
            await self._disconnect_after_error()
            raise ObsTransportConnectionError(
                f"Could not connect to OBS at {self._url}: {error}"
            ) from error

        self._connected = True

    async def get_version(self) -> ObsVersionInfo:
        self._ensure_connected()

        try:
            data = await self._transport.get_version(self._connect_timeout)
        except ObsConnectionError:
            raise
        except Exception as error:
            raise ObsConnectionError(
                f"Could not query OBS version information: {error}"
            ) from error

        rpc_version = data.get("rpcVersion")
        return ObsVersionInfo(
            obs_version=str(data.get("obsVersion", "unknown")),
            websocket_version=str(data.get("obsWebSocketVersion", "unknown")),
            rpc_version=rpc_version if isinstance(rpc_version, int) else None,
        )

    async def set_text(self, source_name: str, text: str) -> None:
        """Set text without exposing OBS transport request details."""

        self._ensure_connected()
        try:
            await self._transport.set_text(
                source_name,
                text,
                self._connect_timeout,
            )
        except ObsConnectionError:
            raise
        except Exception as error:
            raise ObsRequestError(f"Could not update OBS text: {error}") from error

    async def set_scene_item_visibility(
        self,
        scene_name: str,
        source_name: str,
        *,
        visible: bool,
    ) -> None:
        """Show or hide a named source within a named scene."""

        self._ensure_connected()
        try:
            scene_item_id = await self._transport.get_scene_item_id(
                scene_name,
                source_name,
                self._connect_timeout,
            )
            await self._transport.set_scene_item_enabled(
                scene_name,
                scene_item_id,
                visible,
                self._connect_timeout,
            )
        except ObsConnectionError:
            raise
        except Exception as error:
            raise ObsRequestError(
                f"Could not change OBS scene item visibility: {error}"
            ) from error

    async def ensure_browser_source(
        self,
        scene_name: str,
        source_name: str,
        *,
        url: str,
        width: int,
        height: int,
    ) -> bool:
        """Create or reconcile a browser source, returning True if it was created.

        Only the URL, size, and the two lifecycle toggles are managed, so a user's
        own tweaks to other settings survive. Lifecycle toggles are forced off:
        the overlay must stay loaded and keep its WebSocket rather than being
        suspended or refreshed when scenes change.
        """

        self._ensure_connected()
        settings: dict[str, Any] = {
            "url": url,
            "width": width,
            "height": height,
            "shutdown": False,
            "restart_when_active": False,
        }

        defaults = await self._transport.get_input_default_settings(
            BROWSER_SOURCE_KIND,
            self._connect_timeout,
        )
        # Guard against an OBS release renaming a key: writing an unknown
        # setting is silently ignored by OBS, which would look like our bug.
        unknown = sorted(set(settings) - set(defaults))
        if unknown:
            raise ObsRequestError(
                f"OBS does not recognise browser source settings: {', '.join(unknown)}"
            )

        try:
            await self._transport.get_scene_item_id(
                scene_name,
                source_name,
                self._connect_timeout,
            )
        except ObsResourceNotFoundError:
            await self._transport.create_input(
                scene_name,
                source_name,
                BROWSER_SOURCE_KIND,
                settings,
                self._connect_timeout,
            )
            return True

        await self._transport.set_input_settings(
            source_name,
            settings,
            self._connect_timeout,
        )
        return False

    async def disconnect(self) -> None:
        if not self._disconnect_needed:
            return
        try:
            await self._transport.disconnect()
        finally:
            self._connected = False
            self._disconnect_needed = False

    async def _disconnect_after_error(self) -> None:
        with suppress(Exception):
            await self._transport.disconnect()
        self._connected = False
        self._disconnect_needed = False

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise ObsConnectionError("OBS client is not connected")
