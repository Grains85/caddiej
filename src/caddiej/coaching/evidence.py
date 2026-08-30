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

from caddiej.domain.coaching import CoachingFocus, Diagnosis, Observation
from caddiej.domain.media import ClipRef, TimeQuality
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
) -> str:
    """Everything known about one shot, as a compact block of text."""

    lines = [
        f"club: {_CLUB_NAMES.get(diagnosis.club, 'unknown club')}",
        f"handedness: {_handedness(diagnosis.handedness)}",
        f"diagnosis: {diagnosis.focus.value} (severity "
        f"{diagnosis.severity:.2f} of 1.00)",
        "",
        "measured on this shot, already described from the player's point of view:",
    ]
    lines.extend(
        f"  {_observation(observation)}" for observation in diagnosis.observations
    )
    if not diagnosis.observations:
        lines.append("  nothing the device measured could be judged")

    lines.append("")
    lines.append(_baseline_note(diagnosis))

    if diagnosis.candidates:
        ranked = ", ".join(
            f"{candidate.focus.value} {candidate.severity:.2f}"
            for candidate in diagnosis.candidates
        )
        lines.append(f"faults you may choose from: {ranked}")
    if preferred is not None:
        lines.append(f"the player is working on: {preferred.value}")
    if recent_lines:
        said = " / ".join(f'"{line}"' for line in recent_lines)
        lines.append(f"already said recently, do not repeat: {said}")

    if frames and clip is not None:
        lines.append("")
        lines.append(_frames_note(clip, frames, frame_offsets))

    return "\n".join(lines)


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


def _frames_note(clip: ClipRef, frames: int, offsets: Sequence[float]) -> str:
    """What the pictures are, and how much to trust when they were taken.

    The frame rate and the timing quality are stated because they bound what can
    honestly be read from the images. Ten frames across a downswing, timed by
    estimate, does not support a claim about the exact moment of impact.
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
        f"Impact timing is {trust}."
    )
