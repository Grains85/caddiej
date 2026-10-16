"""Replay one stored ProTee shot against candidate models, and say where the time goes.

A ProTee coaching line takes about five seconds of capture and about twenty of
OpenAI, and neither number explains itself. Changing a model, an effort or an
image setting on the bay and hitting another ball compares two different
swings, two different network minutes and two different moods of the API, and
calls the difference a result. This module exists so that a comparison is made
the only way that means anything: the same stored bytes, sent again, under each
configuration in turn, several times each.

Four rules shape it.

**Nothing happens but the request.** There is no `AnalysisService`, no session,
no event bus, no floor, no speech, no database and no advice cache anywhere in
this file — not stubbed out, absent. A benchmark run reads a capture directory,
builds requests with the production `vx` functions and hands them straight to a
provider. The evidence directory is opened for reading only; `note_provider_images`,
which the live path uses to annotate it, is deliberately not called. A diagnostic
that could change what the bay remembers would make every run a small incident.

**The same bytes for everyone.** The stills are read once, into an
`EvidencePackage`, and every configuration's request is built from that one
package. Only what a configuration names — model, reasoning effort, output
ceiling, timeout, swing-frame detail — differs between requests. The session
context the live path adds (a focus being worked on, lines recently spoken) is
left out on purpose: it is a sentence or two, it depends on what happened
earlier in an evening, and it would make two runs of the same shot differ.

**Measure before optimising.** The offline breakdown says what the request is
made of — prompt, evidence, schema and each image — with no API call at all, so
a proposal to shorten the request can be checked against where its tokens
actually are. Image tokens are an ESTIMATE from OpenAI's published sizing rules
and are labelled as one; the measured input tokens of a real run are the
arbiter, and the two are printed side by side.

**A failure is a row, not an exit.** A model the account cannot use, a key that
is wrong, a timeout: each is recorded against the configuration that caused it
and the next configuration still runs. A benchmark that stopped at the first
refusal would answer only the question nobody asked.
"""

from __future__ import annotations

import json
import math
import re
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from itertools import product
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from caddiej.adapters.openai.pricing import estimate_cost
from caddiej.adapters.openai.provider import (
    IMAGE_DETAIL,
    OpenAIAdviceProvider,
    _announce,
    sdk,
)
from caddiej.coaching import vx
from caddiej.coaching.cues import CUES_BY_ID, admissible_cues
from caddiej.coaching.provider import (
    AdviceProvider,
    AdviceRequest,
    ImageInput,
    ProviderReply,
)
from caddiej.domain.coaching import (
    SEVERITY_FINE,
    AdviceKind,
    CoachingFocus,
    Diagnosis,
)
from caddiej.domain.cues import CueId, EvidenceGrade, PanelView
from caddiej.domain.media import SwingCapture
from caddiej.domain.shot import ClubKind, Handedness
from caddiej.domain.swing_phase import SwingPhase
from caddiej.domain.vx import VX_METRICS
from caddiej.swing_capture.select import (
    ActiveInterval,
    CapturedFrame,
    select,
)
from caddiej.swing_capture.stills import read_labeled_stills, read_metrics_still
from caddiej.swing_capture.store import (
    METADATA_NAME,
    METRICS_FRAME_ID,
    load_capture,
    newest_capture,
)
from caddiej.video.jpeg_size import jpeg_size

IMAGE_DETAILS = ("low", "high", "auto")
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high")
"""Every value a model might accept, not every value any one model accepts:
gpt-5-mini refuses `none` and gpt-5.4-mini refuses `minimal`. The API is the
authority on which, and a refusal is reported as the failure it is rather than
quietly retried with something else."""
METRICS_DETAIL = "high"
"""The All Values still is always sent at high detail, as the live path sends
it: it is read digit by digit, and the benchmark varies the swing frames."""

_TEXT_CHARS_PER_TOKEN = 4.0
_CONTENT_STEP = 1.5
"""A still at least this many times larger than the one before it, in one
panel's timeline, is where ProTee's replay replaced its loading screen. Only a
cross-check now: the selector's own replay start is the reported answer."""


# --- configurations ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    """One way of asking. Everything that differs between two requests."""

    model: str
    reasoning_effort: str | None
    max_output_tokens: int
    timeout_seconds: float
    swing_detail: str = IMAGE_DETAIL

    @property
    def label(self) -> str:
        return (
            f"{self.model} effort={self.reasoning_effort or 'none'} "
            f"detail={self.swing_detail} out={self.max_output_tokens} "
            f"timeout={self.timeout_seconds:g}s"
        )


def configurations(
    *,
    models: Sequence[str],
    efforts: Sequence[str | None],
    details: Sequence[str],
    max_output_tokens: int,
    timeout_seconds: float,
) -> tuple[BenchmarkConfig, ...]:
    """Every combination named, in the order named, each once."""

    chosen = (
        BenchmarkConfig(
            model=model,
            reasoning_effort=effort,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
            swing_detail=detail,
        )
        for model, effort, detail in product(models, efforts, details)
    )
    return tuple(dict.fromkeys(chosen))


# --- the evidence -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvidencePackage:
    """One stored shot, read once, as every configuration will be shown it.

    `swing` carries no detail of its own; a configuration supplies it. The
    All Values still keeps the detail the live path gives it.
    """

    capture: SwingCapture
    swing: tuple[ImageInput, ...]
    metrics: ImageInput | None
    cues: tuple[CueId, ...]
    club: ClubKind
    handedness: Handedness

    @property
    def frame_ids(self) -> tuple[str, ...]:
        return vx.sent_frame_ids(self.images(IMAGE_DETAIL))

    def images(self, swing_detail: str) -> tuple[ImageInput, ...]:
        swing = tuple(replace(image, detail=swing_detail) for image in self.swing)
        return swing if self.metrics is None else (*swing, self.metrics)


def select_capture(
    evidence_dir: Path | None, latest_dir: Path
) -> tuple[SwingCapture | None, str]:
    """The capture to replay, and a sentence saying which and why."""

    if evidence_dir is not None:
        capture = load_capture(evidence_dir)
        if capture is None:
            return None, f"{evidence_dir} has no readable {METADATA_NAME}"
        return capture, f"{evidence_dir} (named on the command line)"
    capture = newest_capture(latest_dir)
    if capture is None:
        return None, (
            f"no stored capture under {latest_dir.parent} — hit one ball with "
            "swing capture on, or name a directory with --benchmark-evidence"
        )
    return capture, f"{capture.directory} (newest stored shot)"


async def load_evidence(
    capture: SwingCapture,
    *,
    max_images: int,
    send_frames: bool = True,
    visual_cues: bool = True,
    club: ClubKind = ClubKind.UNKNOWN,
    handedness: Handedness = Handedness.UNKNOWN,
    fine: float = SEVERITY_FINE,
) -> EvidencePackage:
    """Read the stills the live path would send, the way it would choose them.

    The same reader, the same frame budget and the same cue set as
    `AnalysisService._process_protee`, with club and handedness unknown as they
    are on a ProTee-only bay. Read-only: nothing is written beside the stills.
    """

    swing: tuple[ImageInput, ...] = ()
    if send_frames and club is not ClubKind.PUTTER and max_images > 0:
        raw = await read_labeled_stills(capture, max_images)
        swing = tuple(
            ImageInput(
                data=data,
                label=label,
                frame_id=identifier,
                **({"detail": detail} if detail else {}),
            )
            for data, label, _panel, identifier, detail in raw[:max_images]
        )
    data = await read_metrics_still(capture)
    metrics = (
        None
        if data is None
        else ImageInput(
            data=data,
            label=vx.METRICS_LABEL,
            detail=METRICS_DETAIL,
            frame_id=METRICS_FRAME_ID,
        )
    )
    visual: tuple[CueId, ...] = ()
    if swing:
        probe = Diagnosis(
            shot_id=capture.coaching_id,
            focus=CoachingFocus.NONE,
            severity=0.0,
            display="",
            speech="",
            club=club,
            handedness=handedness,
            baseline_shots=0,
            kind=AdviceKind.CORRECTION,
        )
        visual = tuple(
            cue
            for cue in admissible_cues(
                probe,
                has_frames=True,
                capture=capture,
                visual_cues=visual_cues,
                fine=fine,
            )
            if CUES_BY_ID[cue].grade is EvidenceGrade.VISUAL
        )
    cues = vx.possible_cues(club, has_metrics=metrics is not None, visual=visual)
    return EvidencePackage(
        capture=capture,
        swing=swing,
        metrics=metrics,
        cues=cues,
        club=club,
        handedness=handedness,
    )


def build_benchmark_request(
    package: EvidencePackage, config: BenchmarkConfig
) -> AdviceRequest:
    """The production request for this shot, under this configuration."""

    images = package.images(config.swing_detail)
    return vx.build_request(
        evidence=vx.render_evidence(
            club=package.club,
            handedness=package.handedness,
            has_metrics=package.metrics is not None,
            swing_images=tuple(image.label for image in package.swing),
            cues=package.cues,
            session_focus=None,
            focus_shots=0,
            frames=tuple(
                (image.frame_id, image.label) for image in images if image.frame_id
            ),
        ),
        images=images,
        cues=package.cues,
        max_output_tokens=config.max_output_tokens,
        timeout_seconds=config.timeout_seconds,
        reasoning_effort=config.reasoning_effort,
    )


# --- image tokens: an estimate ----------------------------------------------

# OpenAI publishes two sizing rules and says which models use which. The
# numbers below are theirs as published at the time of writing, and they are
# exactly the kind of number that goes quietly wrong: when the estimate and a
# real run's input tokens disagree, the run is right and this table is stale.
#
# Patch models cut the image into 32 px patches, shrinking it first so that at
# most 1536 patches are needed, and multiply the count by a per-model factor.
# The published rule has no low-detail variant for them, so `detail` is not
# modelled: a "low" swing frame is estimated at full patch cost, and a measured
# input count close to that estimate is evidence that the setting saved nothing.
#
# gpt-5-mini is the exception, because it was measured rather than read
# (2026-10-07, real requests, input tokens differenced against a text-only
# request): it fits the image inside 2048 x 2048 first, then costs 1.2 tokens
# per 32 px patch with no 1536 cap -- a 2048 square cost 4916 tokens, a 3072
# square the same, and `detail` low or high made no difference.
_PATCH_SIZE = 32
_PATCH_CAP = 1536
_PATCH_MULTIPLIERS: Mapping[str, float] = {
    "gpt-5-mini": 1.2,
    "gpt-5-nano": 2.46,
    "gpt-4.1-mini": 1.62,
    "gpt-4.1-nano": 2.46,
    "o4-mini": 1.72,
}
_PATCH_FIT: Mapping[str, int] = {"gpt-5-mini": 2048}
"""Models measured to fit inside a square before patching, and uncapped after."""
# Tile models: low detail is a flat base cost; high detail is the base plus a
# cost per 512 px tile after fitting the image inside 2048 x 2048 and then
# shrinking it until its shortest side is 768 px. Neither step enlarges.
_TILE_SIZE = 512
_TILE_FIT = 2048
_TILE_SHORT_SIDE = 768
_TILE_COSTS: Mapping[str, tuple[int, int]] = {
    "gpt-5": (70, 140),
    "gpt-5-chat-latest": (70, 140),
    "gpt-4o": (85, 170),
    "gpt-4.1": (85, 170),
    "gpt-4.5-preview": (85, 170),
    "gpt-4o-mini": (2833, 5667),
    "o1": (75, 150),
    "o1-pro": (75, 150),
    "o3": (75, 150),
}
_ASSUMED_TILE_COST = (85, 170)
_SNAPSHOT = re.compile(r"-\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True, slots=True)
class ImageTokenEstimate:
    """What one image probably costs in input tokens, and by which rule."""

    tokens: int
    rule: str
    """`patch`, `tile`, or `tile (assumed)` for a model in neither table."""
    units: int
    """Patches for the patch rule, tiles for a high-detail tile estimate."""
    detail_applied: bool

    def describe(self) -> str:
        if self.rule == "patch":
            return f"{self.units} patches"
        if self.units == 0:
            return f"{self.rule}, flat"
        return f"{self.rule}, {self.units} tiles"


def _up(value: float) -> int:
    """Ceiling that forgives floating-point dust on an exact multiple."""

    return math.ceil(value - 1e-9)


def tile_image_tokens(
    width: int,
    height: int,
    *,
    detail: str,
    base: int = 85,
    per_tile: int = 170,
) -> tuple[int, int]:
    """Tokens and tiles under the tile rule. `auto` is costed as `high`,
    because the API is free to choose it and an estimate should not be low."""

    if detail == "low" or width <= 0 or height <= 0:
        return base, 0
    scaled_width, scaled_height = float(width), float(height)
    longest = max(scaled_width, scaled_height)
    if longest > _TILE_FIT:
        scaled_width *= _TILE_FIT / longest
        scaled_height *= _TILE_FIT / longest
    shortest = min(scaled_width, scaled_height)
    if shortest > _TILE_SHORT_SIDE:
        scaled_width *= _TILE_SHORT_SIDE / shortest
        scaled_height *= _TILE_SHORT_SIDE / shortest
    tiles = _up(scaled_width / _TILE_SIZE) * _up(scaled_height / _TILE_SIZE)
    return base + per_tile * tiles, tiles


def patch_image_tokens(
    width: int, height: int, *, multiplier: float, fit: int | None = None
) -> tuple[int, int]:
    """Tokens and patches under the patch rule, as OpenAI describes it, or,
    with `fit`, as measured: fitted inside that square and never capped."""

    if width <= 0 or height <= 0:
        return 0, 0
    if fit is not None:
        shrink = min(1.0, fit / max(width, height))
        patches = _up(width * shrink / _PATCH_SIZE) * _up(height * shrink / _PATCH_SIZE)
        return _up(patches * multiplier), patches
    patches = _up(width / _PATCH_SIZE) * _up(height / _PATCH_SIZE)
    if patches > _PATCH_CAP:
        shrink = math.sqrt(_PATCH_SIZE**2 * _PATCH_CAP / (width * height))
        across = width * shrink / _PATCH_SIZE
        down = height * shrink / _PATCH_SIZE
        shrink *= min(
            max(1, math.floor(across)) / across, max(1, math.floor(down)) / down
        )
        patches = min(
            _PATCH_CAP,
            _up(width * shrink / _PATCH_SIZE) * _up(height * shrink / _PATCH_SIZE),
        )
    return _up(patches * multiplier), patches


def estimate_image_tokens(
    width: int, height: int, *, detail: str, model: str
) -> ImageTokenEstimate:
    """The input tokens one image probably costs this model. An ESTIMATE."""

    name = _SNAPSHOT.sub("", model.strip().lower())
    multiplier = _PATCH_MULTIPLIERS.get(name)
    if multiplier is not None:
        tokens, patches = patch_image_tokens(
            width, height, multiplier=multiplier, fit=_PATCH_FIT.get(name)
        )
        return ImageTokenEstimate(
            tokens=tokens, rule="patch", units=patches, detail_applied=False
        )
    known = _TILE_COSTS.get(name)
    base, per_tile = known or _ASSUMED_TILE_COST
    tokens, tiles = tile_image_tokens(
        width, height, detail=detail, base=base, per_tile=per_tile
    )
    return ImageTokenEstimate(
        tokens=tokens,
        rule="tile" if known else "tile (assumed)",
        units=tiles,
        detail_applied=True,
    )


def image_rule(model: str) -> str:
    """One line saying which rule a model's images are estimated by."""

    name = _SNAPSHOT.sub("", model.strip().lower())
    if name in _PATCH_FIT:
        return (
            f"measured: fit {_PATCH_FIT[name]} px, 32 px patches, "
            f"x{_PATCH_MULTIPLIERS[name]:g}, no cap; detail is ignored"
        )
    if name in _PATCH_MULTIPLIERS:
        return (
            f"32 px patches, cap {_PATCH_CAP}, x{_PATCH_MULTIPLIERS[name]:g}; "
            "detail is not part of this rule"
        )
    if name in _TILE_COSTS:
        base, per_tile = _TILE_COSTS[name]
        return f"tiles: low {base} flat, high {base} + {per_tile} per 512 px tile"
    return "unknown model: tile rule at gpt-4o rates assumed"


# --- the request, measured offline ------------------------------------------


@dataclass(frozen=True, slots=True)
class TextSize:
    part: str
    characters: int

    @property
    def approx_tokens(self) -> int:
        return _up(self.characters / _TEXT_CHARS_PER_TOKEN)


@dataclass(frozen=True, slots=True)
class ImageSize:
    frame_id: str
    label: str
    size_bytes: int
    width: int
    height: int
    detail: str
    estimate: ImageTokenEstimate


@dataclass(frozen=True, slots=True)
class RequestBreakdown:
    """What one request is made of, measured without sending it."""

    model: str
    texts: tuple[TextSize, ...]
    images: tuple[ImageSize, ...]

    @property
    def text_tokens(self) -> int:
        return sum(text.approx_tokens for text in self.texts)

    @property
    def image_tokens(self) -> int:
        return sum(image.estimate.tokens for image in self.images)

    @property
    def total_tokens(self) -> int:
        return self.text_tokens + self.image_tokens


def request_breakdown(
    request: AdviceRequest, *, model: str, default_detail: str = IMAGE_DETAIL
) -> RequestBreakdown:
    """Sizes of every part the provider sends. Pure: no network, no clock.

    The schema is measured as compact JSON. How the API serialises it into the
    prompt is not published, so its share is the least certain number here.
    """

    announcements = sum(len(_announce(image)) for image in request.images)
    texts = (
        TextSize("system prompt", len(request.system)),
        TextSize("instructions", len(request.instructions)),
        TextSize("evidence text", len(request.evidence)),
        TextSize("image announcements", announcements),
        TextSize(
            "JSON schema",
            len(json.dumps(dict(request.schema), separators=(",", ":"))),
        ),
    )
    images = []
    for image in request.images:
        width, height = jpeg_size(image.data) or (0, 0)
        detail = image.detail or default_detail
        images.append(
            ImageSize(
                frame_id=image.frame_id or "(none)",
                label=image.label,
                size_bytes=len(image.data),
                width=width,
                height=height,
                detail=detail,
                estimate=estimate_image_tokens(
                    width, height, detail=detail, model=model
                ),
            )
        )
    return RequestBreakdown(model=model, texts=texts, images=tuple(images))


def breakdown_report(breakdown: RequestBreakdown) -> str:
    total = max(1, breakdown.total_tokens)
    lines = [
        f"Request size for {breakdown.model} — offline, no API call (ESTIMATE)",
        f"  image rule            {image_rule(breakdown.model)}",
        f"  {'part':<22}{'chars':>9}{'~tokens':>10}{'share':>8}",
    ]
    for text in breakdown.texts:
        lines.append(
            f"  {text.part:<22}{text.characters:>9}{text.approx_tokens:>10}"
            f"{text.approx_tokens / total:>8.0%}"
        )
    lines.append(
        f"  {'text subtotal':<22}{'':>9}{breakdown.text_tokens:>10}"
        f"{breakdown.text_tokens / total:>8.0%}"
    )
    lines.append("")
    lines.append(
        f"  {'image':<18}{'bytes':>9}  {'pixels':<11}{'detail':<8}"
        f"{'~tokens':>8}{'share':>7}  rule"
    )
    for image in breakdown.images:
        lines.append(
            f"  {image.frame_id:<18}{image.size_bytes:>9}  "
            f"{f'{image.width}x{image.height}':<11}{image.detail:<8}"
            f"{image.estimate.tokens:>8}{image.estimate.tokens / total:>7.0%}"
            f"  {image.estimate.describe()}"
        )
    lines.append(
        f"  {f'images ({len(breakdown.images)})':<18}"
        f"{sum(image.size_bytes for image in breakdown.images):>9}  "
        f"{'':<11}{'':<8}{breakdown.image_tokens:>8}"
        f"{breakdown.image_tokens / total:>7.0%}"
    )
    lines.extend(
        [
            "",
            f"  estimated input total {breakdown.total_tokens} tokens",
            "  Text is characters / 4. Images follow OpenAI's published sizing",
            "  rules as this file records them. Both are estimates: a real run's",
            "  measured input tokens are the answer, and are printed beside them.",
        ]
    )
    return "\n".join(lines)


# --- the capture's own clock --------------------------------------------------


@dataclass(frozen=True, slots=True)
class PanelWindow:
    panel_id: str
    kind: str
    delay_seconds: float
    duration_seconds: float | None
    fps: float | None
    captured: int
    kept_offsets: tuple[float, ...]
    content_from: float | None
    """Where a panel's stills first jump in size: ProTee's replay replacing
    its loading screen. A heuristic, and labelled as one where it is shown."""

    interval: ActiveInterval | None = None
    """This panel's interval as the selector finds it today, from the stored
    timeline. None for a static panel or one with no timeline."""

    reselected: tuple[float, ...] = ()
    """The offsets the selector would keep today, as many as were kept."""

    reselected_phases: tuple[SwingPhase, ...] = ()
    """Which swing moment each of those offsets was aimed at.

    The point of the whole report, after the first hardware session: the old
    selector spread its moments evenly and the audit could only say *when* it
    kept a still, which made it impossible to see from the numbers that one of
    three down-the-line stills was the player walking away. Empty for a panel
    with no phase calibration, which is still an even spread."""

    @property
    def end_seconds(self) -> float:
        return self.delay_seconds + (self.duration_seconds or 0.0)


@dataclass(frozen=True, slots=True)
class CaptureTiming:
    windows: tuple[PanelWindow, ...]
    elapsed_seconds: float | None
    detection_lag_seconds: float | None
    selection: tuple[float, float, bool] | None
    metrics_offset_seconds: float | None
    selector: ActiveInterval | None = None
    """The interval both body panels share, as the selector finds it today."""

    @property
    def defining(self) -> PanelWindow | None:
        if not self.windows:
            return None
        return max(self.windows, key=lambda window: window.end_seconds)


def capture_timing(directory: Path) -> CaptureTiming | None:
    """The windows a capture was configured with and how long it really took.

    Read straight from `metadata.json`, which keeps the configuration the
    capture ran under and every screenshot's offset and size. None when there
    is no such file.
    """

    try:
        payload = json.loads((directory / METADATA_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    configuration = payload.get("configuration")
    configuration = configuration if isinstance(configuration, dict) else {}
    windows: list[PanelWindow] = []
    for camera in payload.get("cameras") or ():
        if not isinstance(camera, dict):
            continue
        static = camera.get("kind") == "static"
        prefix = "static_" if static else ""
        delay = _number(
            configuration.get(f"{prefix}delay_seconds")
            if static
            else configuration.get("initial_delay_seconds")
        )
        windows.append(
            PanelWindow(
                panel_id=str(camera.get("camera_id", "?")),
                kind=str(camera.get("kind", "?")),
                delay_seconds=delay or 0.0,
                duration_seconds=_number(
                    configuration.get(f"{prefix}duration_seconds")
                ),
                fps=_number(configuration.get(f"{prefix}fps")),
                captured=int(_number(camera.get("captured_frames")) or 0),
                kept_offsets=tuple(
                    float(frame.get("offset_seconds", 0.0))
                    for frame in camera.get("frames") or ()
                    if isinstance(frame, dict)
                ),
                content_from=None if static else _content_from(camera),
            )
        )
    metrics = payload.get("coach_metrics")
    if isinstance(metrics, dict):
        delay = _number(metrics.get("delay_seconds"))
        if delay is not None:
            windows.append(
                PanelWindow(
                    panel_id="coach_metrics",
                    kind="still",
                    delay_seconds=delay,
                    duration_seconds=None,
                    fps=None,
                    captured=1 if metrics.get("ok") else 0,
                    kept_offsets=(),
                    content_from=None,
                )
            )
    windows, selector = _reselect(payload.get("cameras") or (), windows)
    selection = payload.get("selection_interval")
    return CaptureTiming(
        selector=selector,
        windows=tuple(windows),
        elapsed_seconds=_between(
            payload.get("started_at"), payload.get("completed_at")
        ),
        detection_lag_seconds=_between(
            payload.get("detected_at"), payload.get("started_at")
        ),
        selection=(
            (
                float(selection.get("start_seconds", 0.0)),
                float(selection.get("end_seconds", 0.0)),
                bool(selection.get("detected")),
            )
            if isinstance(selection, dict)
            else None
        ),
        metrics_offset_seconds=(
            _number(metrics.get("offset_seconds"))
            if isinstance(metrics, dict)
            else None
        ),
    )


def _reselect(
    cameras: Sequence[object], windows: list[PanelWindow]
) -> tuple[list[PanelWindow], ActiveInterval | None]:
    """Run today's selector over the body panels' stored timelines.

    The timeline keeps each screenshot's offset, size and digest, and those
    are the only things the selector reads, so a stand-in of the same length
    whose bytes are equal exactly when the digests are equal is selected
    exactly as the original was. Nothing is decoded and nothing is written.
    """

    tracks: dict[str, list[CapturedFrame]] = {}
    views: dict[str, PanelView] = {}
    for camera in cameras:
        if not isinstance(camera, dict) or camera.get("kind") == "static":
            continue
        frames = _stand_ins(camera.get("timeline") or ())
        if not frames:
            continue
        panel_id = str(camera.get("camera_id", "?"))
        tracks[panel_id] = frames
        try:
            views[panel_id] = PanelView(str(camera.get("view", PanelView.NONE.value)))
        except ValueError:
            views[panel_id] = PanelView.NONE
    if not tracks:
        return windows, None
    kept = {
        window.panel_id: len(window.kept_offsets)
        for window in windows
        if window.panel_id in tracks
    }
    chosen, intervals, combined, aimed = select(tracks, kept, views)
    return [
        replace(
            window,
            interval=intervals[window.panel_id],
            reselected=tuple(
                tracks[window.panel_id][index].offset_seconds
                for index in chosen[window.panel_id]
            ),
            reselected_phases=aimed.get(window.panel_id, ()),
        )
        if window.panel_id in tracks
        else window
        for window in windows
    ], combined


def _stand_ins(timeline: Sequence[object]) -> list[CapturedFrame]:
    frames: list[CapturedFrame] = []
    identity = b""
    for position, trace in enumerate(timeline):
        if not isinstance(trace, dict):
            continue
        size = _number(trace.get("bytes"))
        offset = _number(trace.get("offset_seconds"))
        if size is None or offset is None:
            continue
        digest = trace.get("sha256")
        if digest:
            identity = str(digest).encode()
        elif not identity or trace.get("changed") is not False:
            identity = f"#{position}".encode()
        head = b"\xff\xd8" + identity
        frames.append(
            CapturedFrame(offset, head + bytes(max(0, int(size) - len(head))))
        )
    return frames


def _content_from(camera: Mapping[str, Any]) -> float | None:
    previous: float | None = None
    for trace in camera.get("timeline") or ():
        if not isinstance(trace, dict):
            continue
        size = _number(trace.get("bytes"))
        if size is None:
            continue
        if previous is not None and previous > 0 and size >= previous * _CONTENT_STEP:
            return _number(trace.get("offset_seconds"))
        previous = size
    return None


def _number(raw: object) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    return float(raw)


def _between(start: object, end: object) -> float | None:
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        return (
            datetime.fromisoformat(end) - datetime.fromisoformat(start)
        ).total_seconds()
    except ValueError:
        return None


def timing_report(timing: CaptureTiming, sent: Sequence[str] = ()) -> str:
    lines = ["Stored capture timing (metadata.json; offsets from the strike)"]
    if timing.detection_lag_seconds is not None:
        lines.append(f"  detected -> started   {timing.detection_lag_seconds:.3f} s")
    if timing.elapsed_seconds is not None:
        lines.append(
            f"  started -> completed  {timing.elapsed_seconds:.3f} s (measured wall time)"
        )
    if timing.selection is not None:
        start, end, detected = timing.selection
        lines.append(
            f"  selection interval    {start:g}-{end:g} s "
            f"({'detected from motion' if detected else 'NOT detected; whole window used'})"
        )
    lines.append(
        f"  {'panel':<15}{'kind':<9}{'delay':>6}{'dur':>6}{'fps':>6}{'end':>7}"
        f"{'shots':>7}  kept offsets (s)"
    )
    for window in timing.windows:
        duration = (
            "-" if window.duration_seconds is None else f"{window.duration_seconds:g}"
        )
        fps = "-" if window.fps is None else f"{window.fps:g}"
        kept = ", ".join(f"{offset:g}" for offset in window.kept_offsets) or "-"
        if (
            window.panel_id == "coach_metrics"
            and timing.metrics_offset_seconds is not None
        ):
            kept = f"captured at {timing.metrics_offset_seconds:g}"
        lines.append(
            f"  {window.panel_id:<15}{window.kind:<9}{window.delay_seconds:>6g}"
            f"{duration:>6}{fps:>6}{window.end_seconds:>7g}{window.captured:>7}  {kept}"
        )
    defining = timing.defining
    if defining is not None:
        lines.append(
            f"  capture end is set by {defining.panel_id} ({defining.kind}): "
            f"{defining.delay_seconds:g} + {defining.duration_seconds or 0:g} = "
            f"{defining.end_seconds:g} s"
        )
    lines.extend(_selector_report(timing, sent))
    replay = [window for window in timing.windows if window.content_from is not None]
    if replay:
        lines.append("  cross-check only (HEURISTIC: first >=1.5x jump in still size):")
        for window in replay:
            content_from = window.content_from
            assert content_from is not None
            early = [
                (f"{window.panel_id}_{index:02d}", offset)
                for index, offset in enumerate(window.kept_offsets, start=1)
                if offset < content_from
            ]
            named = ", ".join(f"{name} at {offset:g} s" for name, offset in early)
            sent_early = [name for name, _offset in early if name in sent]
            lines.append(
                f"    {window.panel_id:<15}from {content_from:g} s; kept before it: "
                f"{named or 'none'}"
                + (
                    f"; SENT TO THE MODEL: {', '.join(sent_early)}"
                    if sent_early
                    else ""
                )
            )
    return "\n".join(lines)


def _selector_report(timing: CaptureTiming, sent: Sequence[str]) -> list[str]:
    """The selector's replay start, and every kept still measured against it."""

    combined = timing.selector
    if combined is None:
        return []
    lines = [
        "  replay start (the selector, re-run on the stored timeline; authoritative):",
        f"    {'selection':<15}"
        + (
            f"{combined.start_seconds:g}-{combined.end_seconds:g} s, after "
            f"{combined.pre_replay_frames} pre-replay stills"
            if combined.detected
            else f"{combined.start_seconds:g}-{combined.end_seconds:g} s, "
            "NOT detected; whole window used"
        ),
    ]
    for window in timing.windows:
        interval = window.interval
        if interval is None:
            continue
        replay_from = interval.start_seconds if interval.pre_replay_frames > 0 else None
        lines.append(
            f"    {window.panel_id:<15}"
            + (
                f"replay from {replay_from:g} s after "
                f"{interval.pre_replay_frames} pre-replay stills"
                if replay_from is not None
                else "no replay start found"
            )
        )
        kept = []
        for position, offset in enumerate(window.kept_offsets, start=1):
            name = f"{window.panel_id}_{position:02d}"
            notes = [
                note
                for note, applies in (
                    ("PRE-REPLAY", replay_from is not None and offset < replay_from),
                    ("SENT TO THE MODEL", name in sent),
                )
                if applies
            ]
            kept.append(
                f"{name} {offset:g} s" + (f" [{', '.join(notes)}]" if notes else "")
            )
        lines.append(f"    {'':<15}kept: {'; '.join(kept) or 'none'}")
        lines.append(
            f"    {'':<15}the selector keeps today: "
            + (
                ", ".join(
                    f"{offset:g}" + (f" {phase.value}" if phase else "")
                    for offset, phase in zip(
                        window.reselected,
                        window.reselected_phases or (None,) * len(window.reselected),
                        strict=False,
                    )
                )
                or "none"
            )
        )
    return lines


# --- running it ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts as the API reported them, whether or not the reply parsed."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_tokens: int | None = None
    status: str | None = None


@runtime_checkable
class ReportsUsage(Protocol):
    """A provider that can say what its last call cost, including cached input,
    which `ProviderReply` does not carry."""

    def usage(self) -> Usage | None: ...


ProviderFactory = Callable[[BenchmarkConfig], AdviceProvider]


@dataclass(frozen=True, slots=True)
class RunResult:
    config_index: int
    config: BenchmarkConfig
    repeat: int
    ok: bool
    wall_seconds: float | None
    error: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_tokens: int | None = None
    cost_usd: float | None = None
    parsed: bool = False
    metrics_read: int = 0
    kind: str | None = None
    cue: str | None = None
    evidence_frame_id: str | None = None
    frame_id_sent: bool | None = None
    speech: str | None = None
    estimated_input_tokens: int | None = None


async def run_benchmark(
    package: EvidencePackage,
    configs: Sequence[BenchmarkConfig],
    *,
    repeats: int,
    make_provider: ProviderFactory,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[RunResult, ...]:
    """Every configuration, `repeats` times, one request each, never two at once.

    Sequential, because concurrent requests would share a connection and a
    rate limit and each latency would then be partly the others'. Interleaved
    — every configuration once, then every configuration again — because the
    API's own speed drifts over minutes, and running all of one configuration
    before all of the next would hand that drift to whichever ran second.
    """

    estimates: dict[int, int] = {}
    providers: dict[int, AdviceProvider] = {}
    unbuilt: dict[int, str] = {}
    for index, config in enumerate(configs, start=1):
        estimates[index] = request_breakdown(
            build_benchmark_request(package, config), model=config.model
        ).total_tokens
        try:
            providers[index] = make_provider(config)
        except Exception as error:
            unbuilt[index] = _describe(error)
    results: list[RunResult] = []
    try:
        for repeat in range(1, repeats + 1):
            for index, config in enumerate(configs, start=1):
                provider = providers.get(index)
                if provider is None:
                    results.append(
                        RunResult(
                            config_index=index,
                            config=config,
                            repeat=repeat,
                            ok=False,
                            wall_seconds=None,
                            error=unbuilt.get(index, "no provider"),
                            estimated_input_tokens=estimates[index],
                        )
                    )
                    continue
                results.append(
                    await _run_once(
                        package,
                        config,
                        index,
                        repeat,
                        provider,
                        clock,
                        estimates[index],
                    )
                )
    finally:
        for provider in providers.values():
            try:
                await provider.aclose()
            except Exception:
                pass
    results.sort(key=lambda result: (result.config_index, result.repeat))
    return tuple(results)


async def _run_once(
    package: EvidencePackage,
    config: BenchmarkConfig,
    index: int,
    repeat: int,
    provider: AdviceProvider,
    clock: Callable[[], float],
    estimate: int,
) -> RunResult:
    base = RunResult(
        config_index=index,
        config=config,
        repeat=repeat,
        ok=False,
        wall_seconds=None,
        estimated_input_tokens=estimate,
    )
    if not provider.is_configured:
        return replace(
            base,
            error="provider not configured (COACHING_API_KEY empty or openai not installed)",
        )
    request = build_benchmark_request(package, config)
    started = clock()
    try:
        reply = await provider.complete(request)
    except Exception as error:
        usage = provider.usage() if isinstance(provider, ReportsUsage) else None
        return _with_usage(
            replace(base, wall_seconds=clock() - started, error=_describe(error)),
            usage,
            config.model,
        )
    wall = clock() - started
    usage = provider.usage() if isinstance(provider, ReportsUsage) else None
    return _judge(replace(base, ok=True, wall_seconds=wall), reply, usage, package)


def _with_usage(result: RunResult, usage: Usage | None, model: str) -> RunResult:
    if usage is None:
        return result
    return replace(
        result,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        cached_tokens=usage.cached_tokens,
        cost_usd=estimate_cost(model, usage.input_tokens, usage.output_tokens),
    )


def _judge(
    result: RunResult,
    reply: ProviderReply,
    usage: Usage | None,
    package: EvidencePackage,
) -> RunResult:
    """What came back, read the way the live path reads it, and never trusted."""

    payload = reply.payload
    raw_metrics = payload.get("metrics")
    metrics = vx.parse_metrics(raw_metrics if package.metrics is not None else None)
    coach = vx.coach_payload(payload)
    cue = None if coach is None else _text(coach.get("cue"))
    frame = None if coach is None else _text(coach.get("evidence_frame_id"))
    input_tokens = reply.input_tokens
    output_tokens = reply.output_tokens
    reasoning = reply.reasoning_tokens
    cached = None
    if usage is not None:
        input_tokens = (
            usage.input_tokens if usage.input_tokens is not None else input_tokens
        )
        output_tokens = (
            usage.output_tokens if usage.output_tokens is not None else output_tokens
        )
        reasoning = (
            usage.reasoning_tokens if usage.reasoning_tokens is not None else reasoning
        )
        cached = usage.cached_tokens
    return replace(
        result,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning,
        cached_tokens=cached,
        cost_usd=estimate_cost(reply.model, input_tokens, output_tokens),
        parsed=coach is not None and isinstance(raw_metrics, Mapping),
        metrics_read=len(metrics.available),
        kind=None if coach is None else _text(coach.get("kind")),
        cue=cue,
        evidence_frame_id=frame,
        frame_id_sent=None if frame is None else frame in package.frame_ids,
        speech=None if coach is None else _text(coach.get("speech")),
    )


def _text(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    return raw.strip() or None


def _describe(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"[:200]


# --- reading the results ------------------------------------------------------


def results_report(results: Sequence[RunResult]) -> str:
    total = len(VX_METRICS)
    lines = [
        "Runs (wall = seconds around provider.complete; tokens as the API reported)",
        f"  {'cfg':>3}{'rep':>4}  {'status':<7}{'wall s':>7}{'in':>7}{'est in':>8}"
        f"{'cached':>7}{'out':>6}{'reason':>7}{'usd':>9}  {'parsed':<7}"
        f"{'metrics':<8}{'cue':<22}frame",
    ]
    for result in results:
        frame = result.evidence_frame_id or "-"
        if result.frame_id_sent is False:
            frame += " (NOT SENT)"
        lines.append(
            f"  {result.config_index:>3}{result.repeat:>4}  "
            f"{'ok' if result.ok else 'FAILED':<7}{_cell(result.wall_seconds, 2):>7}"
            f"{_cell(result.input_tokens):>7}{_cell(result.estimated_input_tokens):>8}"
            f"{_cell(result.cached_tokens):>7}{_cell(result.output_tokens):>6}"
            f"{_cell(result.reasoning_tokens):>7}{_cell(result.cost_usd, 5):>9}  "
            f"{('yes' if result.parsed else 'no') if result.ok else '-':<7}"
            f"{f'{result.metrics_read}/{total}' if result.ok else '-':<8}"
            f"{(result.cue or result.kind or '-') if result.ok else '-':<22}"
            f"{frame if result.ok else '-'}"
        )
    failures = [result for result in results if result.error]
    if failures:
        lines.append("")
        lines.append("Failures")
        for result in failures:
            lines.append(
                f"  cfg {result.config_index} rep {result.repeat}: {result.error}"
            )
    spoken = [result for result in results if result.speech]
    if spoken:
        lines.append("")
        lines.append("Coach lines (for judging quality side by side)")
        for result in spoken:
            lines.append(
                f"  cfg {result.config_index} rep {result.repeat}: "
                f"[{result.cue or result.kind}] {result.speech}"
            )
    return "\n".join(lines)


def summary_report(
    configs: Sequence[BenchmarkConfig], results: Sequence[RunResult]
) -> str:
    lines = ["Per configuration"]
    for index, config in enumerate(configs, start=1):
        mine = [result for result in results if result.config_index == index]
        good = [
            result for result in mine if result.ok and result.wall_seconds is not None
        ]
        lines.append(f"  cfg {index}: {config.label}")
        if not good:
            lines.append(f"    ok 0/{len(mine)}; no latency to report")
            continue
        walls = [
            result.wall_seconds for result in good if result.wall_seconds is not None
        ]
        lines.append(
            f"    ok {len(good)}/{len(mine)}; wall median {statistics.median(walls):.2f} s "
            f"(min {min(walls):.2f}, max {max(walls):.2f})"
        )
        lines.append(
            "    median tokens: in "
            + _median([result.input_tokens for result in good])
            + ", out "
            + _median([result.output_tokens for result in good])
            + ", reasoning "
            + _median([result.reasoning_tokens for result in good])
            + "; median cost "
            + _median([result.cost_usd for result in good], 5)
            + " USD (estimate)"
        )
        lines.append(
            f"    parsed {sum(1 for result in good if result.parsed)}/{len(good)}; "
            "cues "
            + (", ".join(sorted({result.cue or result.kind or "-" for result in good})))
        )
    return "\n".join(lines)


def _cell(value: float | int | None, places: int = 0) -> str:
    if value is None:
        return "-"
    if places == 0:
        return str(int(value))
    return f"{value:.{places}f}"


def _median(values: Sequence[float | int | None], places: int = 0) -> str:
    known = [value for value in values if value is not None]
    if not known:
        return "-"
    return _cell(statistics.median(known), places)


# --- the two commands -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    ok: bool
    report: str
    results: tuple[RunResult, ...] = ()


def _package_header(package: EvidencePackage, origin: str) -> list[str]:
    still = package.capture.metrics
    return [
        f"  evidence              {origin}",
        f"  strike_id             {package.capture.strike_id}",
        f"  club / handedness     {package.club.value} / {package.handedness.value} "
        "(as on a ProTee-only bay)",
        f"  images                {len(package.images(IMAGE_DETAIL))} "
        f"({len(package.swing)} swing + {0 if package.metrics is None else 1} All Values)",
        f"  frame ids             {', '.join(package.frame_ids) or '(none)'}",
        "  All Values still      "
        + (
            "not captured"
            if package.metrics is None or still is None
            else f"{still.width}x{still.height} at {METRICS_DETAIL} detail"
        ),
        f"  cues offered          {len(package.cues)}",
        f"  prompt                {vx.PROTEE_PROMPT_VERSION}",
    ]


async def _prepare(
    *,
    evidence_dir: Path | None,
    latest_dir: Path,
    max_images: int,
    send_frames: bool,
    visual_cues: bool,
) -> tuple[EvidencePackage | None, str]:
    capture, origin = select_capture(evidence_dir, latest_dir)
    if capture is None:
        return None, origin
    package = await load_evidence(
        capture,
        max_images=max_images,
        send_frames=send_frames,
        visual_cues=visual_cues,
    )
    return package, origin


def _offline_sections(
    package: EvidencePackage, pairs: Sequence[tuple[str, str]]
) -> list[str]:
    sections: list[str] = []
    probe = BenchmarkConfig(
        model="", reasoning_effort=None, max_output_tokens=1, timeout_seconds=1
    )
    for model, detail in dict.fromkeys(pairs):
        request = build_benchmark_request(
            package, replace(probe, model=model, swing_detail=detail)
        )
        sections.extend(["", breakdown_report(request_breakdown(request, model=model))])
    timing = capture_timing(package.capture.directory)
    if timing is not None:
        sections.extend(["", timing_report(timing, package.frame_ids)])
    return sections


async def coach_request_report(
    *,
    evidence_dir: Path | None,
    latest_dir: Path,
    models: Sequence[str],
    details: Sequence[str],
    max_images: int,
    send_frames: bool = True,
    visual_cues: bool = True,
) -> BenchmarkReport:
    """The offline half alone: what the request is made of, and the capture's clock."""

    lines = ["Coach request breakdown — stored evidence, no API call, no side effects"]
    package, origin = await _prepare(
        evidence_dir=evidence_dir,
        latest_dir=latest_dir,
        max_images=max_images,
        send_frames=send_frames,
        visual_cues=visual_cues,
    )
    if package is None:
        lines.append(f"  evidence              {origin}")
        return BenchmarkReport(ok=False, report="\n".join(lines))
    lines.extend(_package_header(package, origin))
    lines.extend(_offline_sections(package, list(product(models, details))))
    return BenchmarkReport(ok=True, report="\n".join(lines))


async def benchmark_coach(
    *,
    evidence_dir: Path | None,
    latest_dir: Path,
    configs: Sequence[BenchmarkConfig],
    repeats: int,
    max_images: int,
    make_provider: ProviderFactory,
    send_frames: bool = True,
    visual_cues: bool = True,
) -> BenchmarkReport:
    """Replay one stored shot under every configuration. No other side effect."""

    lines = [
        "Coach benchmark — one stored shot replayed; no session, floor, speech, "
        "database or cache is touched"
    ]
    package, origin = await _prepare(
        evidence_dir=evidence_dir,
        latest_dir=latest_dir,
        max_images=max_images,
        send_frames=send_frames,
        visual_cues=visual_cues,
    )
    if package is None:
        lines.append(f"  evidence              {origin}")
        return BenchmarkReport(ok=False, report="\n".join(lines))
    lines.extend(_package_header(package, origin))
    lines.append(f"  repeats               {repeats}")
    lines.append("  configurations")
    for index, config in enumerate(configs, start=1):
        lines.append(f"    {index}: {config.label}")
    lines.extend(
        _offline_sections(
            package, [(config.model, config.swing_detail) for config in configs]
        )
    )
    results = await run_benchmark(
        package, configs, repeats=repeats, make_provider=make_provider
    )
    lines.extend(["", results_report(results), "", summary_report(configs, results)])
    return BenchmarkReport(
        ok=any(result.ok for result in results),
        report="\n".join(lines),
        results=results,
    )


# --- the real provider, with its usage visible --------------------------------


class _ResponsesTap:
    def __init__(self, responses: Any, tap: _SdkTap) -> None:
        self._responses = responses
        self._tap = tap

    async def create(self, **kwargs: Any) -> Any:
        response = await self._responses.create(**kwargs)
        self._tap.last = response
        return response


class _ClientTap:
    def __init__(self, client: Any, tap: _SdkTap) -> None:
        self._client = client
        self.responses = _ResponsesTap(client.responses, tap)

    async def close(self) -> None:
        await self._client.close()


class _SdkTap:
    """Stands in for the `openai` module so the raw response is seen too.

    `OpenAIAdviceProvider` takes its SDK module as an argument, which is the
    seam its tests use. Here it lets the benchmark read what `ProviderReply`
    does not carry — cached input tokens — and the usage of a reply the
    provider then refused as incomplete, without changing the provider.
    Everything else, exceptions included, is the real module's.
    """

    def __init__(self, module: Any) -> None:
        self._module = module
        self.last: Any = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._module, name)

    def AsyncOpenAI(self, **kwargs: Any) -> _ClientTap:
        return _ClientTap(self._module.AsyncOpenAI(**kwargs), self)


class MeteredOpenAIProvider:
    """The production OpenAI provider, plus the usage of its last call."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        module: Any | None = None,
    ) -> None:
        real = module if module is not None else sdk()
        self._tap = None if real is None else _SdkTap(real)
        self._inner = OpenAIAdviceProvider(
            api_key=api_key, model=model, base_url=base_url, module=self._tap
        )

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def is_configured(self) -> bool:
        return self._tap is not None and self._inner.is_configured

    async def complete(self, request: AdviceRequest) -> ProviderReply:
        if self._tap is not None:
            self._tap.last = None
        return await self._inner.complete(request)

    def usage(self) -> Usage | None:
        response = None if self._tap is None else self._tap.last
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        return Usage(
            input_tokens=_count(getattr(usage, "input_tokens", None)),
            output_tokens=_count(getattr(usage, "output_tokens", None)),
            reasoning_tokens=_count(
                getattr(
                    getattr(usage, "output_tokens_details", None),
                    "reasoning_tokens",
                    None,
                )
            ),
            cached_tokens=_count(
                getattr(
                    getattr(usage, "input_tokens_details", None), "cached_tokens", None
                )
            ),
            status=str(getattr(response, "status", None) or "") or None,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


def _count(raw: object) -> int | None:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


def openai_factory(*, api_key: str, base_url: str | None = None) -> ProviderFactory:
    """A fresh metered provider per configuration, so no client is shared."""

    def make(config: BenchmarkConfig) -> AdviceProvider:
        return MeteredOpenAIProvider(
            api_key=api_key, model=config.model, base_url=base_url or None
        )

    return make
