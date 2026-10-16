"""The facts a model is shown, rendered the same way every time.

Deterministic and pure, for two reasons beyond testability. The same diagnosis
must produce the same bytes, or the cache key means nothing. And everything in
here is generated from typed values rather than from free text, which is what
closes off the only realistic route into the prompt for something a device or a
user typed.

What is deliberately absent matters as much as what is present. No player name,
no session label, no file path, no identifier, no raw device payload. None of
them makes a coaching sentence better, and the first two are personal data
leaving the machine for no return.
"""

from collections.abc import Sequence

from caddiej.coaching.session import SessionProgress
from caddiej.domain.coaching import (
    AdviceKind,
    CoachingFocus,
    Diagnosis,
    Observation,
)
from caddiej.domain.cues import PanelView
from caddiej.domain.media import CameraView, ClipRef, SwingCapture, TimeQuality
from caddiej.domain.shot import ClubKind, Handedness

_CLUB_NAMES = {
    ClubKind.DRIVER: "driver",
    ClubKind.WOOD: "fairway wood",
    ClubKind.HYBRID: "hybrid",
    ClubKind.IRON: "iron",
    ClubKind.WEDGE: "wedge",
    ClubKind.PUTTER: "putter",
    ClubKind.UNKNOWN: "unknown club",
}


def render(
    diagnosis: Diagnosis,
    recent_lines: Sequence[str] = (),
    preferred: CoachingFocus | None = None,
    clip: ClipRef | None = None,
    frames: int = 0,
    frame_offsets: Sequence[float] = (),
    progress: SessionProgress | None = None,
    camera_view: CameraView = CameraView.UNKNOWN,
    capture: SwingCapture | None = None,
) -> str:
    """Everything known about one shot, as a compact block of text."""

    lines = [
        f"club: {_CLUB_NAMES.get(diagnosis.club, 'unknown club')}",
        f"handedness: {_handedness(diagnosis.handedness)}",
        f"this line is a: {diagnosis.kind.value}",
        f"diagnosis: {diagnosis.focus.value} (severity "
        f"{diagnosis.severity:.2f} of 1.00)",
        "",
        "measured on this shot (grade=measured), already described from the "
        "player's point of view:",
    ]
    lines.extend(
        f"  {_observation(observation)}" for observation in diagnosis.observations
    )
    if not diagnosis.observations:
        lines.append("  nothing the device measured could be judged")
    if diagnosis.unmeasured:
        lines.append("")
        lines.append(
            "not measured on this shot (grade=absent), do not invent or infer: "
            + ", ".join(diagnosis.unmeasured)
        )

    lines.append("")
    lines.append(_baseline_note(diagnosis))
    if diagnosis.cue is not None:
        lines.append(f"templated cue: {diagnosis.cue.value}")
    if diagnosis.admissible_cues:
        lines.append(
            "cues you may choose from: "
            + ", ".join(cue.value for cue in diagnosis.admissible_cues)
        )

    if diagnosis.candidates:
        ranked = ", ".join(
            f"{candidate.focus.value} {candidate.severity:.2f}"
            for candidate in diagnosis.candidates
        )
        lines.append(f"faults you may choose from: {ranked}")
    if preferred is not None:
        lines.append(f"the player is working on: {preferred.value}")
    lines.extend(_session_notes(diagnosis, progress))
    if recent_lines:
        said = " / ".join(f'"{line}"' for line in recent_lines)
        lines.append(f"already said recently, do not repeat: {said}")

    if frames and clip is not None:
        lines.append("")
        lines.append(_frames_note(clip, frames, frame_offsets, camera_view))
    elif frames and capture is not None:
        lines.append("")
        lines.append(_capture_note(capture, frames))

    return "\n".join(lines)


def _session_notes(diagnosis: Diagnosis, progress: SessionProgress | None) -> list[str]:
    """What has been happening to this focus over the last few swings.

    The numbers rather than a verdict, because "coming down from six degrees to
    one and a half" is the sentence a coach would actually say and a boolean is
    not. `improving` is stated as well and is CaddieJ's own arithmetic: the
    model is not asked to work out whether a trend is a trend.
    """

    notes: list[str] = []
    if diagnosis.focus_shots > 1:
        notes.append(
            f"this has been the focus for the last {diagnosis.focus_shots} shots"
        )
    trend = None if progress is None else progress.trend_for(diagnosis.focus)
    if trend is not None and trend.values:
        readings = ", ".join(f"{value:+.1f}" for value in trend.values)
        notes.append(
            f"{trend.label} on the shots before this one (grade=trend): "
            f"{readings} {trend.unit}"
        )
    if diagnosis.improving:
        notes.append("this is measurably better than the shots before it")
    if diagnosis.kind is AdviceKind.REINFORCEMENT:
        notes.append("nothing needs correcting on this shot: confirm what produced it")
    return notes


def _observation(observation: Observation) -> str:
    """One measurement, its label, and how unusual it is for this player.

    Silent when there is no mean to compare against, rather than saying so. Two
    of these measurements never have one — a strike position has no average to
    keep, and scatter is already a comparison — so a "no baseline yet" would be
    wrong about both, and the cold-start caveat is stated once below anyway.
    """

    text = f"{observation.label}: {observation.value} {observation.unit}"
    if observation.baseline_mean is None:
        return text
    text = f"{text} (usual {observation.baseline_mean} {observation.unit}"
    if observation.deviations is None:
        return f"{text}, spread not yet known)"
    return f"{text}, {abs(observation.deviations):.1f} deviations out)"


def _handedness(handedness: Handedness) -> str:
    if handedness is Handedness.UNKNOWN:
        return "unknown, assume right"
    return handedness.value


def _baseline_note(diagnosis: Diagnosis) -> str:
    """How much of the player's own normal is known.

    Said out loud because it changes how confidently a line should be phrased:
    the third shot of an evening is being judged against a table of reference
    numbers, not against the person holding the club.
    """

    if diagnosis.baseline_shots == 0:
        return (
            "baseline: none yet, so this is judged against general reference "
            "numbers rather than against this player"
        )
    return (
        f"baseline: {diagnosis.baseline_shots} shot(s) with this club "
        "so far this session"
    )


_CAMERA_VIEWS = {
    CameraView.FACE_ON: (
        "The camera is face-on, square to the player. It shows sway, posture, "
        "how the body turns and whether they stand up through the ball. It does "
        "not show swing plane, shaft position or club path"
    ),
    CameraView.DOWN_THE_LINE: (
        "The camera is down the line, behind the player. It shows swing plane, "
        "shaft position and where the club goes. It does not show sway towards "
        "or away from the target reliably"
    ),
    CameraView.UNKNOWN: (
        "Nobody has told CaddieJ where the camera is standing, so do not read "
        "anything from these stills that depends on the angle. Keep to what is "
        "plain from any angle, or choose a measured fault instead"
    ),
}


def _frames_note(
    clip: ClipRef,
    frames: int,
    offsets: Sequence[float],
    camera_view: CameraView,
) -> str:
    """What the pictures are, and how much to trust when they were taken.

    Three limits, all stated rather than left to be inferred: where the camera
    is, how fast it was filming, and how well the impact time is known. Each of
    them rules something out — a plane fault cannot be seen face-on, a position
    cannot be read from a frame rate that never sampled it, and an estimated
    impact does not support a claim about the moment of the strike. A model that
    is not told them will fill the gaps in, plausibly and wrongly.
    """

    relative = ", ".join(
        f"{offset - clip.impact_offset_seconds:+.2f}s" for offset in offsets
    )
    trust = (
        "measured"
        if clip.time_quality is TimeQuality.CALIBRATED
        else "estimated, so impact may be a fraction of a second either side"
    )
    return (
        f"frames: {frames} stills from one camera at {relative} relative to "
        f"impact, in that order. Recorded at {clip.fps:g} frames per second. "
        f"Impact timing is {trust}. {_CAMERA_VIEWS[camera_view]}. The times are "
        "all these stills are: no swing position has been detected, so do not "
        "treat one as address, the top, or impact."
    )


def _capture_note(capture: SwingCapture, frames: int) -> str:
    """The other kind of picture, and the smaller claim it supports.

    These are screenshots of the launch monitor's own replay window, not
    footage from a camera CaddieJ placed. Nothing here knows where those
    cameras are, what they were filming at, or where impact falls in the
    replay — the replay is not played back in real time and does not start at
    the strike. Saying so is the whole point of a separate note: a model given
    the clip's wording would read a timeline into pictures that have none.
    """

    shown = [camera for camera in capture.cameras if camera.frames]
    ids = {camera.camera_id for camera in shown}
    views = {camera.view for camera in shown}
    labels: list[str] = []
    if PanelView.BEHIND in views or "swing_behind" in ids:
        labels.append(
            "SWING_BEHIND / DOWN_THE_LINE is the camera behind the player: "
            "plane and steep or shallow movement, not lateral sway."
        )
    if PanelView.SIDE in views or "swing_side" in ids:
        labels.append(
            "SWING_SIDE / FACE_ON is the camera to the player's side: posture, "
            "lateral sway, weight shift, arms, extension and release."
        )
    if PanelView.BEHIND in views and PanelView.SIDE in views:
        labels.append(
            "Do not state a side-view fact from a behind image, or a "
            "behind-view fact from a side image."
        )
    if "impact" in ids:
        labels.append("IMPACT is the strike graphic, not a body camera.")
    if "delivery" in ids:
        labels.append("DELIVERY is the analysis graphic, not a body camera.")
    panels = "; ".join(
        (
            f"{camera.camera_id} view={camera.view.value} "
            f"source={camera.source!r} ({camera.kind.value}, "
            f"{'settled' if camera.settled else 'unsettled'}, "
            f"supports={','.join(sorted(claim.value for claim in camera.supports)) or 'none'})"
        )
        for camera in shown
    )
    described = " ".join(labels)
    return (
        f"frames: {frames} labeled stills (grade=visual), in this order. "
        f"Each image is preceded by its label. "
        f"{described + ' ' if described else ''}"
        f"Do not read any text, number, graph, dial or club measurement from "
        f"any image. Club numbers come only from the measured lines above. "
        f"The pictures are screenshots of a replay, not footage at real speed, "
        f"so do not name a swing position or a moment."
        f"{f' Panels: {panels}.' if panels else ''}"
    )
