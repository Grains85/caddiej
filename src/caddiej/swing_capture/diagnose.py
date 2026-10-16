"""Proving, without hitting a ball, that OBS can hand over both swing panels.

Swing capture has exactly one hardware prerequisite and it is entirely outside
CaddieJ: two OBS sources, each showing one of ProTee's two Swing Video panels,
each named in the configuration. Everything else in the subsystem is timing,
and timing cannot be judged until that part works.

From the bay, "the sources are wrong" and "the replay had not started yet" look
identical — both produce a `latest_swing` that is useless. So this asks OBS for
one still from each configured source, writes them where they can be opened,
and reports their dimensions. Nothing is captured on a schedule, nothing is
published, and no golf shot is required.

Prose rather than JSON, and no event loop of its own, because it is read by a
person at a terminal in the same session as `--check-config`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from caddiej.adapters.obs import (
    CAPTURE_SCENE,
    SCREENSHOT_FORMAT,
    ObsClient,
    ObsResourceNotFoundError,
    ObsVersionInfo,
)
from caddiej.adapters.obs.screenshot import (
    UnusableScreenshot,
    require_usable_screenshot,
)
from caddiej.domain.cues import CapturePanel, PanelKind

_TITLES = {
    "swing_behind": "Swing Behind",
    "swing_side": "Swing Side",
    "impact": "Impact",
    "delivery": "Delivery",
    "coach_metrics": "All Values",
}


@dataclass(frozen=True, slots=True)
class SwingCaptureProbe:
    """What happened, and whether the operator has to fix OBS."""

    ok: bool
    report: str


class ProbeClient(Protocol):
    """What this diagnostic asks of OBS. Two requests, both reads."""

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def get_version(self) -> ObsVersionInfo: ...

    async def source_screenshot(
        self, source_name: str, *, width: int | None = None
    ) -> bytes: ...


async def probe_swing_capture(
    *,
    host: str,
    port: int,
    password: str,
    connect_timeout: float,
    sources: tuple[str, ...] = (),
    panels: tuple[CapturePanel, ...] | None = None,
    absent: tuple[tuple[str, str], ...] = (),
    image_width: int = 0,
    metrics_source: str = "",
    capture_scene: str = CAPTURE_SCENE,
    output_root: Path | str | None = None,
    now: Callable[[], datetime] | None = None,
    client_factory: Callable[[], ProbeClient] | None = None,
) -> SwingCaptureProbe:
    """Take one still from each configured source and say what came back.

    Never prints the password, and changes nothing in OBS: no scene switch, no
    visibility change, no crop, no transform. A screenshot is a read. The
    All Values still, when configured, is checked the same way at the source's
    own resolution, which is how the coach receives it.
    """

    stamp = (now or (lambda: datetime.now(UTC)))()
    destination = _destination(output_root, stamp)
    configured_panels = panels if panels is not None else _panels_from_sources(sources)
    metrics_panel = (
        CapturePanel(
            panel_id="coach_metrics",
            source=metrics_source.strip(),
            kind=PanelKind.STATIC,
            setting="COACH_METRICS_SOURCE",
        )
        if metrics_source.strip()
        else None
    )
    named = (
        ", ".join(f"{panel.panel_id}={panel.source!r}" for panel in configured_panels)
        or "(none)"
    )
    lines = [
        f"OBS swing capture check — ws://{host}:{port}",
        f"  Format               {SCREENSHOT_FORMAT}",
        f"  Width                {image_width or 'source resolution'}",
        f"  Output               {destination}",
        f"  Effective panels     {named}",
        f"  Capture scene        {capture_scene!r} (never Program)",
        "  All Values           "
        + (
            "(not configured; COACH_METRICS_CAPTURE_ENABLED is off)"
            if metrics_panel is None
            else f"{metrics_panel.source!r}, source resolution, not in the frame budget"
        ),
        "",
    ]
    if not configured_panels:
        lines.append("  FAILED at configuration: no swing capture source is named")
        lines.extend(["", *_absent_lines(absent), "", _MISSING])
        return SwingCaptureProbe(ok=False, report="\n".join(lines))

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
        formats = version.supported_image_formats
        if formats and SCREENSHOT_FORMAT not in formats:
            return _failed(
                lines,
                "GetVersion",
                f"this OBS does not list {SCREENSHOT_FORMAT!r} as a screenshot format",
                f"It offers: {', '.join(formats)}. Swing capture asks for "
                f"{SCREENSHOT_FORMAT!r} and cannot be reconfigured.",
            )
        lines.append("")

        failures: list[str] = []
        not_rendering: list[str] = []
        names = tuple(panel.source for panel in configured_panels)
        if metrics_panel is not None and metrics_panel.source not in names:
            names += (metrics_panel.source,)
        hold = getattr(client, "hold_sources", None)
        hold_context = (
            hold(names, capture_scene=capture_scene) if hold is not None else _no_hold()
        )
        async with hold_context as states:
            for state in states or ():
                lines.append(
                    f"  {state.source!r:<20} rendered="
                    f"{'unknown' if state.rendered is None else str(state.rendered).lower()}"
                    f", in {capture_scene!r}: "
                    f"{'visible' if state.capture_item_enabled else 'missing or hidden'}"
                    f", floor item: {_floor(state.floor_item_enabled)}"
                )
                if not state.ready:
                    not_rendering.append(state.source)
            if states:
                lines.append("")
            failed = await _photograph(
                client, configured_panels, image_width, destination, lines, failures
            )
            if failed is not None:
                return failed
            if metrics_panel is not None:
                failed = await _photograph(
                    client, (metrics_panel,), 0, destination, lines, failures
                )
                if failed is not None:
                    return failed
        lines.extend(_absent_lines(absent))
    finally:
        try:
            await client.disconnect()
        except Exception:
            # Nothing is waiting on this socket, and the screenshots have
            # already arrived or already failed.
            pass

    if failures:
        lines.extend(
            [
                "",
                f"  FAILED at GetSourceScreenshot: {', '.join(repr(f) for f in failures)}",
                "",
                _ADVICE,
            ]
        )
        return SwingCaptureProbe(ok=False, report="\n".join(lines))

    if not_rendering:
        lines.extend(
            [
                "",
                "  FAILED at capture scene: not refreshed: "
                + ", ".join(repr(name) for name in not_rendering),
                "",
                _capture_scene_advice(capture_scene),
            ]
        )
        return SwingCaptureProbe(ok=False, report="\n".join(lines))

    lines.extend(
        [
            "",
            "Open each file. It should show the panel named above.",
            "Black means that source was not rendering.",
            "",
            "No shot was taken and nothing was captured on a schedule. This says",
            "the sources work, not that the timing does.",
        ]
    )
    return SwingCaptureProbe(ok=True, report="\n".join(lines))


_ADVICE = (
    "OBS compares these names exactly, spaces included, and CaddieJ does not\n"
    "create them. A source that exists but is in no active scene may also\n"
    "return black or a frozen picture: OBS refreshes a window capture only\n"
    "while something is showing it. See the capture scene setup in\n"
    "docs/swing-capture.md."
)


def _capture_scene_advice(scene: str) -> str:
    return (
        f"OBS refreshes a window capture only while it is showing. In OBS:\n"
        f"  1. Scene {scene!r}: add each source above (Add > Existing), eye on.\n"
        f"  2. In the floor scene, add {scene!r} as a scene source, eye on.\n"
        f"  3. On that nested scene: Filters > Effect Filters > Color Correction,\n"
        f"     Opacity 0. It renders, refreshes, and draws nothing on the floor.\n"
        f"Never switch {scene!r} to Program. CaddieJ never toggles a source."
    )


def _floor(enabled: bool | None) -> str:
    if enabled is None:
        return "absent"
    return "VISIBLE" if enabled else "hidden"


@asynccontextmanager
async def _no_hold() -> AsyncIterator[None]:
    yield


async def _photograph(
    client: ProbeClient,
    configured_panels: tuple[CapturePanel, ...],
    image_width: int,
    destination: Path,
    lines: list[str],
    failures: list[str],
) -> SwingCaptureProbe | None:
    """One still per panel. A 1×1 JPEG is a failed capture, not a success."""

    for panel in configured_panels:
        title = _TITLES.get(panel.panel_id, panel.panel_id)
        source = panel.source
        lines.append(f"{title}:")
        lines.append("  configured: yes")
        if panel.setting:
            lines.append(f"  setting: {panel.setting}")
        lines.append(f"  OBS source: {source!r}")
        try:
            data = await client.source_screenshot(source, width=image_width or None)
            size = require_usable_screenshot(source, data)
        except UnusableScreenshot as error:
            failures.append(source)
            lines.append("  OBS source resolved: yes")
            lines.append(f"  capture: FAILED ({error})")
            lines.append("")
            continue
        except ObsResourceNotFoundError as error:
            failures.append(source)
            lines.append("  OBS source resolved: no")
            lines.append(f"  capture: FAILED ({error})")
            lines.append(
                f"  missing source {source!r} from {panel.setting or panel.panel_id}"
            )
            lines.append("")
            continue
        except Exception as error:
            failures.append(source)
            lines.append("  OBS source resolved: no")
            lines.append(f"  capture: FAILED ({error})")
            lines.append("")
            continue

        path = destination / f"{panel.panel_id}.{SCREENSHOT_FORMAT}"
        try:
            destination.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError as error:
            return _failed(lines, "output", error, "")
        lines.append("  OBS source resolved: yes")
        lines.append("  capture: OK")
        lines.append(f"  size: {size[0]} × {size[1]}, {len(data):,} bytes")
        lines.append(f"  file: {path}")
        lines.append("")
    return None


def _panels_from_sources(sources: tuple[str, ...]) -> tuple[CapturePanel, ...]:
    return tuple(
        CapturePanel(
            panel_id=f"camera_{position}",
            source=source,
            kind=PanelKind.DYNAMIC,
        )
        for position, source in enumerate(
            (item for item in sources if item.strip()), start=1
        )
    )


def _absent_lines(absent: tuple[tuple[str, str], ...]) -> list[str]:
    lines: list[str] = []
    for title, setting in absent:
        lines.extend(
            [
                f"{title}:",
                "  configured: no",
                "  OBS source resolved: no",
                "  capture: not configured",
                f"  setting: {setting}",
                "",
            ]
        )
    return lines


_MISSING = (
    "Set SWING_CAPTURE_PANEL_SWING_BEHIND_SOURCE, "
    "SWING_CAPTURE_PANEL_SWING_SIDE_SOURCE, "
    "SWING_CAPTURE_PANEL_IMPACT_SOURCE or "
    "SWING_CAPTURE_PANEL_DELIVERY_SOURCE. "
    "SWING_CAPTURE_PANEL_SWING_SOURCE is a deprecated alias for the behind "
    "view and is used only when the behind source is empty. "
    "CaddieJ does not create the OBS sources."
)


def _destination(output_root: Path | str | None, when: datetime) -> Path:
    root = Path(output_root) if output_root is not None else Path(".caddiej")
    stamp = when.strftime("%Y%m%d-%H%M%S")
    return (root / "diagnostics" / "swing-capture" / stamp).resolve()


def _failed(
    lines: list[str], step: str, error: object, advice: str
) -> SwingCaptureProbe:
    """A failure named by the OBS request that produced it.

    The exception's own message is included because it carries the OBS status
    comment, which is the useful part. It cannot carry the password: nothing on
    this path is given it except the client's constructor.
    """

    lines.append(f"  FAILED at {step}: {error}")
    if advice:
        lines.extend(["", advice])
    return SwingCaptureProbe(ok=False, report="\n".join(lines))
