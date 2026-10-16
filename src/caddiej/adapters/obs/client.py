"""Async OBS adapter with simpleobsws contained as a transport detail."""

import asyncio
import base64
import binascii
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any, Protocol

import simpleobsws

from caddiej.adapters.obs.floor_reveals import FLOOR_REVEALS, FloorReveals
from caddiej.adapters.obs.screenshot import require_usable_screenshot


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
    supported_image_formats: tuple[str, ...] = field(default=())
    """Which formats `GetSourceScreenshot` will accept on this build.

    Read rather than assumed: the request fails outright on a format OBS does
    not list, and the failure a setup check should report is "this OBS does not
    do jpg" rather than a generic request error.
    """


BROWSER_SOURCE_KIND = "browser_source"

# What a screenshot is asked for as. JPEG because these are photographs of a
# room rather than graphics, and a swing capture writes dozens of them per shot.
SCREENSHOT_FORMAT = "jpg"

# The prefix OBS puts in front of the base64 payload. Documented as
# "Base64-encoded screenshot", delivered as a data URI, so both are accepted.
_DATA_URI_MARKER = ";base64,"

# OBS stores window-capture match priority as an int. 0, the value of a missing
# setting, is "match the window class". A WPF window class is
# HwndWrapper[ProTee Labs;;<guid>], and that guid changes every time ProTee
# starts, so a source saved against the previous process captures nothing and
# reports a 1×1 frame. 1 is "match the window title", which is the stable part
# of the saved window string ("ProTee").
_WINDOW_PRIORITY_TITLE = 1
_WINDOW_CAPTURE_KIND = "window_capture"
_ARM_TIMEOUT_SECONDS = 2.0
_ARM_POLL_SECONDS = 0.05

# The private scene that keeps coach sources showing, and so refreshed, without
# any of them being drawn on the floor. Configured once in OBS; never Program.
CAPTURE_SCENE = "CaddieJ Capture"

logger = logging.getLogger(__name__)


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

    async def get_source_screenshot(
        self,
        source_name: str,
        image_format: str,
        width: int | None,
        height: int | None,
        quality: int | None,
        timeout: float,
    ) -> str: ...

    async def get_current_program_scene(self, timeout: float) -> str: ...

    async def get_input_settings(
        self, source_name: str, timeout: float
    ) -> Mapping[str, Any]: ...

    async def list_scene_items(
        self, scene_name: str, timeout: float
    ) -> Sequence[Mapping[str, Any]]: ...

    async def get_scene_item_transform(
        self, scene_name: str, scene_item_id: int, timeout: float
    ) -> Mapping[str, Any]: ...

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

    async def get_source_screenshot(
        self,
        source_name: str,
        image_format: str,
        width: int | None,
        height: int | None,
        quality: int | None,
        timeout: float,
    ) -> str:
        """One `GetSourceScreenshot`, returned as OBS wrote it.

        The optional fields are omitted rather than sent as null: OBS treats an
        absent `imageWidth` as "the source's own resolution", and sends back a
        request error for a null.
        """

        payload: dict[str, Any] = {
            "sourceName": source_name,
            "imageFormat": image_format,
        }
        if width is not None:
            payload["imageWidth"] = width
        if height is not None:
            payload["imageHeight"] = height
        if quality is not None:
            payload["imageCompressionQuality"] = quality

        response = await self._client.call(
            simpleobsws.Request("GetSourceScreenshot", payload),
            timeout=timeout,
        )
        self._ensure_request_succeeded(
            response,
            action="Taking an OBS source screenshot",
            not_found_message=f"OBS source '{source_name}' was not found",
        )
        data = (response.responseData or {}).get("imageData")
        if not isinstance(data, str) or not data:
            raise ObsRequestError("OBS returned no screenshot data")
        return data

    async def get_current_program_scene(self, timeout: float) -> str:
        response = await self._client.call(
            simpleobsws.Request("GetSceneList"),
            timeout=timeout,
        )
        self._ensure_request_succeeded(
            response,
            action="Reading the OBS program scene",
            not_found_message="OBS has no program scene",
        )
        scene = (response.responseData or {}).get("currentProgramSceneName")
        if not isinstance(scene, str) or not scene:
            raise ObsRequestError("OBS did not name its program scene")
        return scene

    async def get_input_settings(
        self, source_name: str, timeout: float
    ) -> Mapping[str, Any]:
        response = await self._client.call(
            simpleobsws.Request("GetInputSettings", {"inputName": source_name}),
            timeout=timeout,
        )
        self._ensure_request_succeeded(
            response,
            action="Reading OBS input settings",
            not_found_message=f"OBS source '{source_name}' was not found",
        )
        return response.responseData or {}

    async def list_scene_items(
        self, scene_name: str, timeout: float
    ) -> Sequence[Mapping[str, Any]]:
        response = await self._client.call(
            simpleobsws.Request("GetSceneItemList", {"sceneName": scene_name}),
            timeout=timeout,
        )
        self._ensure_request_succeeded(
            response,
            action="Reading OBS scene items",
            not_found_message=f"OBS scene '{scene_name}' was not found",
        )
        items = (response.responseData or {}).get("sceneItems", [])
        if not isinstance(items, list):
            return ()
        return tuple(item for item in items if isinstance(item, Mapping))

    async def get_source_active(
        self, source_name: str, timeout: float
    ) -> Mapping[str, Any]:
        response = await self._client.call(
            simpleobsws.Request("GetSourceActive", {"sourceName": source_name}),
            timeout=timeout,
        )
        self._ensure_request_succeeded(
            response,
            action="Reading OBS source state",
            not_found_message=f"OBS source '{source_name}' was not found",
        )
        return response.responseData or {}

    async def get_scene_item_transform(
        self, scene_name: str, scene_item_id: int, timeout: float
    ) -> Mapping[str, Any]:
        response = await self._client.call(
            simpleobsws.Request(
                "GetSceneItemTransform",
                {"sceneName": scene_name, "sceneItemId": scene_item_id},
            ),
            timeout=timeout,
        )
        self._ensure_request_succeeded(
            response,
            action="Reading an OBS scene item transform",
            not_found_message=f"OBS scene item {scene_item_id} was not found",
        )
        transform = (response.responseData or {}).get("sceneItemTransform", {})
        return transform if isinstance(transform, Mapping) else {}

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


@dataclass
class _SourceRestore:
    """What one source looked like before a capture borrowed it."""

    scene: str
    item_id: int
    source_name: str
    restore_priority: int | None = None
    wait: bool = False


@dataclass(frozen=True, slots=True)
class CaptureSourceState:
    """Whether one coach source is being refreshed, and where it is visible."""

    source: str
    capture_scene: str
    in_capture_scene: bool
    capture_item_enabled: bool
    rendered: bool | None
    """OBS's `videoShowing`: refreshed on every tick. None if OBS did not say."""
    floor_item_enabled: bool | None
    """The source's item in the program scene. None when it has none there."""

    @property
    def ready(self) -> bool:
        return self.capture_item_enabled and self.rendered is True

    def describe(self) -> str:
        floor = (
            "absent"
            if self.floor_item_enabled is None
            else ("visible" if self.floor_item_enabled else "hidden")
        )
        rendered = "unknown" if self.rendered is None else str(self.rendered).lower()
        return (
            f"capture_scene={self.capture_scene!r} "
            f"in_capture_scene={str(self.in_capture_scene).lower()} "
            f"capture_item_enabled={str(self.capture_item_enabled).lower()} "
            f"rendered={rendered} floor_item={floor}"
        )


def _source_edge(transform: Mapping[str, Any]) -> tuple[float, float]:
    width = transform.get("sourceWidth") or 0
    height = transform.get("sourceHeight") or 0
    try:
        return float(width), float(height)
    except (TypeError, ValueError):
        return 0.0, 0.0


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
        floor_reveals: FloorReveals = FLOOR_REVEALS,
    ) -> None:
        self._url = f"ws://{host}:{port}"
        self._connect_timeout = connect_timeout
        self._floor_reveals = floor_reveals
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
        formats = data.get("supportedImageFormats")
        return ObsVersionInfo(
            obs_version=str(data.get("obsVersion", "unknown")),
            websocket_version=str(data.get("obsWebSocketVersion", "unknown")),
            rpc_version=rpc_version if isinstance(rpc_version, int) else None,
            supported_image_formats=(
                tuple(str(item) for item in formats)
                if isinstance(formats, Sequence) and not isinstance(formats, str)
                else ()
            ),
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

    async def scene_item_id(self, scene_name: str, source_name: str) -> int:
        """The numeric id OBS uses to address a source inside a scene.

        Public because resolution is the half of a visibility change that can
        fail for a reason worth reporting on its own: a scene or source that is
        not there. `ObsResourceNotFoundError` says which.
        """

        self._ensure_connected()
        try:
            return await self._transport.get_scene_item_id(
                scene_name,
                source_name,
                self._connect_timeout,
            )
        except ObsConnectionError:
            raise
        except Exception as error:
            raise ObsRequestError(
                f"Could not resolve OBS scene item: {error}"
            ) from error

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
            scene_item_id = await self.scene_item_id(scene_name, source_name)
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

    async def source_screenshot(
        self,
        source_name: str,
        *,
        image_format: str = SCREENSHOT_FORMAT,
        width: int | None = None,
        height: int | None = None,
        quality: int | None = None,
    ) -> bytes:
        """A still of one source, as encoded image bytes.

        Bytes rather than the base64 OBS sends, so nothing above this adapter
        has to know how the protocol carries an image. Named sources only: OBS
        renders inputs and scenes alike here, which is what lets a cropped
        region be a scene somebody built rather than a transform CaddieJ has to
        compute.

        `width` and `height` are OBS's "scale to inner": the aspect ratio of the
        source is kept, so passing one of them bounds the still without
        distorting it.
        """

        self._ensure_connected()
        try:
            encoded = await self._transport.get_source_screenshot(
                source_name,
                image_format,
                width,
                height,
                quality,
                self._connect_timeout,
            )
        except ObsConnectionError:
            raise
        except Exception as error:
            raise ObsRequestError(
                f"Could not take an OBS source screenshot: {error}"
            ) from error

        data = _decode_screenshot(source_name, encoded)
        require_usable_screenshot(source_name, data)
        return data

    async def source_showing(self, source_name: str) -> bool | None:
        """Whether OBS is rendering this source anywhere. None if unknown."""

        self._ensure_connected()
        query = getattr(self._transport, "get_source_active", None)
        if query is None:
            return None
        try:
            state = await query(source_name, self._connect_timeout)
        except Exception:
            return None
        showing = state.get("videoShowing")
        return showing if isinstance(showing, bool) else None

    @asynccontextmanager
    async def hold_sources(
        self,
        source_names: Sequence[str],
        *,
        capture_scene: str = CAPTURE_SCENE,
    ) -> AsyncIterator[tuple[CaptureSourceState, ...]]:
        """Prepare window captures for a burst without touching any visibility.

        OBS ticks a window capture only while it is showing somewhere. A hidden
        one keeps the last texture it grabbed, and `GetSourceScreenshot`
        renders that texture again on every request: real-sized,
        byte-identical, and reported as success. Showing it in the program
        scene fixes that and puts it on the floor projector, so this never
        does. The sources are kept showing by `capture_scene`, a private scene
        configured once in OBS and rendered transparently, and this only reads
        and reports whether that is so.

        No scene is switched and no scene item is enabled or disabled, here or
        in the restore, so neither an error nor a cancellation can leave a
        coach source on the floor. The one change made is a window capture's
        match priority, when a stale WPF class guid leaves it 1×1, and that is
        restored afterwards.

        A floor reveal may legitimately show or hide one of these sources in
        the program scene while this is held; `FloorReveals` is how the check
        afterwards tells that from a leak.
        """

        self._ensure_connected()
        reveals_mark = self._floor_reveals.mark()
        program = await self._program_scene()
        floor_before = await self._enabled_items(program) if program else {}
        restorations, states = await self._arm_sources(
            source_names, capture_scene, program, floor_before
        )
        try:
            yield states
        finally:
            await self._restore_sources(restorations)
            if program:
                await self._check_floor_unchanged(
                    program,
                    source_names,
                    floor_before,
                    self._floor_reveals.since(reveals_mark, program),
                )

    async def _program_scene(self) -> str | None:
        try:
            return await self._transport.get_current_program_scene(
                self._connect_timeout
            )
        except ObsConnectionError as error:
            logger.warning(
                "obs_screenshot_scene_unavailable",
                extra={"swing_capture_detail": str(error)},
            )
            return None

    async def _enabled_items(self, scene: str) -> dict[str, bool]:
        """Every source in `scene` and whether its item is enabled. Read only."""

        try:
            items = await self._transport.list_scene_items(scene, self._connect_timeout)
        except ObsConnectionError as error:
            logger.warning(
                "obs_screenshot_scene_unavailable",
                extra={"obs_scene": scene, "swing_capture_detail": str(error)},
            )
            return {}
        return {
            str(item.get("sourceName")): bool(item.get("sceneItemEnabled"))
            for item in items
            if item.get("sourceName")
        }

    async def _arm_sources(
        self,
        source_names: Sequence[str],
        capture_scene: str,
        program: str | None,
        floor: Mapping[str, bool],
    ) -> tuple[list[_SourceRestore], tuple[CaptureSourceState, ...]]:
        restorations: list[_SourceRestore] = []
        items: Sequence[Mapping[str, Any]] = ()
        if program is not None and capture_scene == program:
            # Rendering the capture scene by putting it on Program would show
            # every coach source on the floor. Nothing is prepared.
            logger.error(
                "obs_capture_scene_is_program",
                extra={
                    "obs_scene": capture_scene,
                    "swing_capture_detail": (
                        "the capture scene is the program scene; it must stay "
                        "off Program and be nested, transparent, in the floor scene"
                    ),
                },
            )
        else:
            try:
                items = await self._transport.list_scene_items(
                    capture_scene, self._connect_timeout
                )
            except ObsConnectionError as error:
                logger.warning(
                    "obs_capture_scene_missing",
                    extra={
                        "obs_scene": capture_scene,
                        "swing_capture_detail": str(error),
                    },
                )
        by_name = {
            str(item.get("sourceName")): item
            for item in items
            if item.get("sourceName")
        }
        for source_name in source_names:
            item = by_name.get(source_name)
            if item is None or not bool(item.get("sceneItemEnabled")):
                continue
            item_id = item.get("sceneItemId")
            if not isinstance(item_id, int):
                continue
            restore = _SourceRestore(
                scene=capture_scene, item_id=item_id, source_name=source_name
            )
            try:
                await self._retarget_if_unsized(restore)
            except ObsConnectionError as error:
                logger.warning(
                    "obs_screenshot_arm_failed",
                    extra={
                        "swing_capture_source": source_name,
                        "swing_capture_detail": str(error),
                    },
                )
            restorations.append(restore)
        await self._wait_until_sized(capture_scene, restorations)

        states: list[CaptureSourceState] = []
        for source_name in source_names:
            item = by_name.get(source_name)
            state = CaptureSourceState(
                source=source_name,
                capture_scene=capture_scene,
                in_capture_scene=item is not None,
                capture_item_enabled=item is not None
                and bool(item.get("sceneItemEnabled")),
                rendered=await self.source_showing(source_name),
                floor_item_enabled=floor.get(source_name),
            )
            states.append(state)
            self._log_capture_state(state)
        return restorations, tuple(states)

    @staticmethod
    def _log_capture_state(state: CaptureSourceState) -> None:
        extra = {
            "swing_capture_source": state.source,
            "obs_scene": state.capture_scene,
            "swing_capture_detail": state.describe(),
        }
        if state.ready:
            logger.info("obs_capture_source_state", extra=extra)
            return
        logger.warning(
            "obs_capture_source_not_rendering",
            extra={
                **extra,
                "swing_capture_detail": (
                    f"{state.describe()}; add the source, visible, to the "
                    f"{state.capture_scene!r} scene and nest that scene, visible "
                    "with an opacity 0 Color Correction filter, in the floor scene"
                ),
            },
        )

    async def _check_floor_unchanged(
        self,
        program: str,
        source_names: Sequence[str],
        before: Mapping[str, bool],
        revealed: frozenset[str],
    ) -> None:
        after = await self._enabled_items(program)
        changed = [
            name
            for name in source_names
            if name in before and after.get(name, before[name]) != before[name]
        ]
        leaked = [name for name in changed if name not in revealed]
        scheduled = [name for name in changed if name in revealed]
        if leaked:
            detail = "floor visibility changed during capture"
            if scheduled:
                detail += f"; also revealed as scheduled: {', '.join(scheduled)}"
            logger.warning(
                "obs_capture_floor_changed",
                extra={
                    "obs_scene": program,
                    "swing_capture_sources": ", ".join(leaked),
                    "swing_capture_detail": detail,
                },
            )
            return
        if scheduled:
            logger.info(
                "obs_capture_floor_revealed",
                extra={
                    "obs_scene": program,
                    "swing_capture_sources": ", ".join(scheduled),
                    "swing_capture_detail": (
                        "only scheduled floor reveals changed during capture"
                    ),
                },
            )
            return
        logger.info(
            "obs_capture_floor_unchanged",
            extra={
                "obs_scene": program,
                "swing_capture_detail": "floor visibility unchanged",
            },
        )

    async def _retarget_if_unsized(self, restore: _SourceRestore) -> None:
        transform = await self._transport.get_scene_item_transform(
            restore.scene, restore.item_id, self._connect_timeout
        )
        width, height = _source_edge(transform)
        if width >= 2 and height >= 2:
            return
        source_name = restore.source_name
        settings = await self._transport.get_input_settings(
            source_name, self._connect_timeout
        )
        kind = settings.get("inputKind")
        raw = settings.get("inputSettings")
        current = raw if isinstance(raw, Mapping) else {}
        priority = int(current.get("priority") or 0)
        if kind == _WINDOW_CAPTURE_KIND and priority != _WINDOW_PRIORITY_TITLE:
            await self._transport.set_input_settings(
                source_name,
                {"priority": _WINDOW_PRIORITY_TITLE},
                self._connect_timeout,
            )
            restore.restore_priority = priority
            logger.info(
                "obs_window_capture_retargeted",
                extra={
                    "swing_capture_source": source_name,
                    "swing_capture_detail": (
                        "window match priority set to title while capturing; "
                        "the saved class guid was not producing a frame"
                    ),
                },
            )
        restore.wait = True

    async def _wait_until_sized(
        self, scene: str, restorations: list[_SourceRestore]
    ) -> None:
        pending = [item for item in restorations if item.wait]
        if not pending:
            return
        deadline = time.monotonic() + _ARM_TIMEOUT_SECONDS
        while pending:
            still_waiting: list[_SourceRestore] = []
            for item in pending:
                transform = await self._transport.get_scene_item_transform(
                    scene, item.item_id, self._connect_timeout
                )
                width, height = _source_edge(transform)
                if width < 2 or height < 2:
                    still_waiting.append(item)
            pending = still_waiting
            if not pending or time.monotonic() >= deadline:
                return
            await asyncio.sleep(_ARM_POLL_SECONDS)

    async def _restore_sources(self, restorations: list[_SourceRestore]) -> None:
        for item in reversed(restorations):
            try:
                if item.restore_priority is not None:
                    await self._transport.set_input_settings(
                        item.source_name,
                        {"priority": item.restore_priority},
                        self._connect_timeout,
                    )
            except ObsConnectionError as error:
                logger.warning(
                    "obs_screenshot_restore_failed",
                    extra={
                        "swing_capture_source": item.source_name,
                        "swing_capture_detail": str(error),
                    },
                )

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


def _decode_screenshot(source_name: str, encoded: str) -> bytes:
    """Turn what OBS sent into image bytes, or say it was not an image."""

    marker = encoded.find(_DATA_URI_MARKER)
    payload = encoded[marker + len(_DATA_URI_MARKER) :] if marker >= 0 else encoded
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ObsRequestError(
            f"OBS returned an undecodable screenshot for '{source_name}'"
        ) from error
    if not data:
        raise ObsRequestError(f"OBS returned an empty screenshot for '{source_name}'")
    return data
