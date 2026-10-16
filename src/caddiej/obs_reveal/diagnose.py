"""Proving, against the real OBS, that CaddieJ can toggle one source.

There are two very different reasons a revealed source can fail to appear on a
floor: CaddieJ cannot control that source at all, or it can and the shot never
reached the reveal. From the bay those look identical, so this walks the OBS
half on demand — connect, resolve, show, hide — and says which step stopped
working.

Impact has a second life, and it is the one that hides the first. Swing
capture screenshots ProTee's Impact view from a private capture scene where its
item stays enabled and nobody watches; the floor reveal shows the same source
from a hidden item in the projector's scene. Two scene items, one source name.
Setting up the first does not set up the second, and a bay that has only the
capture item has a coach that sees Impact and a floor that never shows it. So
for Top Impact this reports both roles: the floor item is shown and hidden,
the capture item is only resolved, and its visibility is never changed.

Prose rather than JSON, and no event loop of its own, because it is read by a
person at a terminal in the same session as `--check-config`. It is a
diagnostic: it holds the source visible for a moment and puts it back.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from caddiej.adapters.obs import ObsClient, ObsResourceNotFoundError, ObsVersionInfo
from caddiej.obs_reveal.features import RevealFeature


@dataclass(frozen=True, slots=True)
class ObsSourceProbe:
    """What happened, and whether the operator has to fix OBS."""

    ok: bool
    report: str


class ProbeClient(Protocol):
    """What this diagnostic asks of OBS. Four requests, one of them a read."""

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def get_version(self) -> ObsVersionInfo: ...

    async def scene_item_id(self, scene_name: str, source_name: str) -> int: ...

    async def set_scene_item_visibility(
        self, scene_name: str, source_name: str, *, visible: bool
    ) -> None: ...


async def probe_obs_source(
    *,
    feature: RevealFeature,
    host: str,
    port: int,
    password: str,
    connect_timeout: float,
    scene: str,
    source: str,
    hold_seconds: float = 2.0,
    client_factory: Callable[[], ProbeClient] | None = None,
    capture_scene: str | None = None,
    capture_source: str | None = None,
) -> ObsSourceProbe:
    """Show the configured source for a moment, then hide it again.

    The feature names itself in the heading and names the line to look for
    when OBS turns out to be fine, so an operator running this for Top Impact
    is not sent to grep for Shot Video.

    Never prints the password, and never changes anything but visibility: no
    scene switch, no crop, no transform, no reordering. In the capture scene
    it does not change even that.
    """

    if (
        capture_scene is None
        and capture_source is None
        and client_factory is None
        and feature is RevealFeature.TOP_IMPACT
    ):
        capture_scene, capture_source = _configured_capture_role()
    capture = (
        (capture_scene, capture_source) if capture_scene and capture_source else None
    )

    lines = [
        f"OBS {feature.label} check — ws://{host}:{port}",
        f"  Scene                {scene}",
        f"  Source               {source}",
    ]
    if capture is not None:
        lines.extend(
            [
                f"  Capture scene        {capture[0]}",
                f"  Capture source       {capture[1]}",
            ]
        )
    lines.append("")

    if capture is not None and scene.strip() == capture[0].strip():
        lines.extend(
            [
                f"  REFUSED              {scene!r} is the swing capture scene",
                "",
                "The floor reveal toggles its item; swing capture needs its item",
                "left enabled in a scene nobody watches. Point OBS_FLOOR_SCENE at",
                "the scene the projector shows.",
            ]
        )
        return ObsSourceProbe(ok=False, report="\n".join(lines))

    client: ProbeClient = (
        client_factory()
        if client_factory is not None
        else ObsClient(
            host=host,
            port=port,
            password=password,
            connect_timeout=connect_timeout,
        )
    )

    try:
        try:
            await client.connect()
        except Exception as error:
            return _failed(
                lines,
                "connect",
                error,
                "Check that OBS is running, that Tools / WebSocket Server "
                "Settings has the server enabled on this port, and that "
                "OBS_PASSWORD matches the password shown there.",
            )
        version = await client.get_version()
        lines.append(
            f"  connected            OBS {version.obs_version}, "
            f"WebSocket {version.websocket_version}"
        )

        if capture is not None:
            lines.extend(["", "  Floor presentation — shown after each strike"])
        floor_ok = await _floor_role(client, lines, scene, source, hold_seconds)
        capture_ok = True
        if capture is not None:
            lines.extend(
                ["", "  Coach evidence — screenshotted, never shown on the floor"]
            )
            capture_ok = await _capture_role(client, lines, *capture)
            if capture[1] == source:
                lines.extend(
                    [
                        "",
                        f"{source!r} is used in both roles. That is intended: one",
                        f"hidden item in {scene!r} for the floor, one enabled item",
                        f"in {capture[0]!r} for capture. They share a source and",
                        "CaddieJ never toggles the capture one.",
                    ]
                )
    finally:
        try:
            await client.disconnect()
        except Exception:
            # Nothing is waiting on this socket, and the visibility change has
            # already landed or already failed.
            pass

    if not (floor_ok and capture_ok):
        return ObsSourceProbe(ok=False, report="\n".join(lines))
    lines.extend(
        [
            "",
            "CaddieJ can control this source. If a real shot still produces no",
            "reveal, the problem is the shot event or the reveal lifecycle rather",
            f"than OBS: look for {feature.scheduled_event} in the log.",
        ]
    )
    return ObsSourceProbe(ok=True, report="\n".join(lines))


async def _floor_role(
    client: ProbeClient,
    lines: list[str],
    scene: str,
    source: str,
    hold_seconds: float,
) -> bool:
    try:
        scene_item_id = await client.scene_item_id(scene, source)
    except ObsResourceNotFoundError as error:
        _note_failure(
            lines,
            "GetSceneItemId",
            error,
            f"OBS has no source named {source!r} in scene {scene!r}. The "
            "names are compared exactly, spaces included. CaddieJ does not "
            f"create this source. In OBS select scene {scene!r}, Sources > + > "
            f"Add Existing > {source!r}, move it above the CaddieJ Browser "
            "Source and leave it hidden.",
        )
        return False
    except Exception as error:
        _note_failure(lines, "GetSceneItemId", error, "")
        return False
    lines.append(f"  resolved             scene item id {scene_item_id}")

    try:
        await client.set_scene_item_visibility(scene, source, visible=True)
    except Exception as error:
        _note_failure(lines, "SetSceneItemEnabled(true)", error, "")
        return False
    lines.append(f"  shown                for {hold_seconds:g}s — look at OBS now")

    await asyncio.sleep(hold_seconds)

    try:
        await client.set_scene_item_visibility(scene, source, visible=False)
    except Exception as error:
        _note_failure(
            lines,
            "SetSceneItemEnabled(false)",
            error,
            f"{source!r} may have been left visible in OBS. Hide it by hand.",
        )
        return False
    lines.append("  hidden               back to normal")
    return True


async def _capture_role(
    client: ProbeClient, lines: list[str], scene: str, source: str
) -> bool:
    try:
        scene_item_id = await client.scene_item_id(scene, source)
    except ObsResourceNotFoundError as error:
        _note_failure(
            lines,
            "GetSceneItemId (capture)",
            error,
            f"Swing capture cannot screenshot {source!r} from {scene!r}. In "
            f"OBS select scene {scene!r}, Sources > + > Add Existing > "
            f"{source!r} and leave it enabled. That scene is never shown.",
        )
        return False
    except Exception as error:
        _note_failure(lines, "GetSceneItemId (capture)", error, "")
        return False
    lines.append(
        f"  resolved             scene item id {scene_item_id}, visibility "
        "left as it is"
    )
    return True


def _configured_capture_role() -> tuple[str | None, str | None]:
    """Where swing capture reads Impact from, if settings can say.

    The CLI passes the floor scene and source and nothing else. Reading the
    capture half here keeps that call unchanged; a configuration that cannot
    load just means the capture role is not reported.
    """

    try:
        from caddiej.config import Settings

        settings = Settings()
    except Exception:
        return None, None
    if not settings.swing_capture_enabled:
        return None, None
    return (
        settings.swing_capture_scene,
        settings.swing_capture_panel_impact_source,
    )


def _note_failure(lines: list[str], step: str, error: Exception, advice: str) -> None:
    lines.append(f"  FAILED at {step}: {error}")
    if advice:
        lines.extend(["", advice])


def _failed(
    lines: list[str], step: str, error: Exception, advice: str
) -> ObsSourceProbe:
    """A failure named by the OBS request that produced it.

    The exception's own message is included because it carries the OBS status
    comment, which is the useful part. It cannot carry the password: nothing on
    this path is given it except the client's constructor.
    """

    _note_failure(lines, step, error, advice)
    return ObsSourceProbe(ok=False, report="\n".join(lines))
