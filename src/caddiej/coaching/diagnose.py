"""Naming the fault. Pure arithmetic, and the load-bearing part of coaching.

Nothing in this file can fail, wait, or cost money. Given a shot and the shots
before it, it always produces the same answer, and that answer is what the floor
shows the instant the ball is struck. A language model is asked afterwards to say
it better, and if it never answers, this is what the player read anyway.

Three rules it applies, in the order they matter:

* a fault is only worth naming if it is outside this player's own normal or
  outside a fixed band that everybody's swing is judged against;
* one fault per shot, because a player can act on one idea;
* if the worst fault is not bad enough, the answer is that there is nothing to
  say. A coach who finds something wrong with every swing is a coach nobody
  listens to.
"""

from collections.abc import Sequence

from caddiej.coaching.baseline import ClubBaseline
from caddiej.coaching.faults import (
    FAULTS,
    clamp,
    is_positive,
    observe,
)
from caddiej.coaching.templates import display_for, speech_for
from caddiej.domain.coaching import (
    SEVERITY_FINE,
    CoachingFocus,
    Diagnosis,
    FocusCandidate,
    Observation,
)
from caddiej.domain.shot import Shot
from caddiej.domain.statistics import club_of

# How much worse another fault has to be before it overrides what the player
# came to work on. Without this, a session becomes a heat map of whatever went
# wrong last, which is the opposite of a lesson.
PREFERENCE_TOLERANCE = 0.15

# Scatter needs a few shots to be scatter rather than variety.
_CONSISTENCY_MINIMUM_SHOTS = 5
# Spread as a fraction of the mean: four per cent is a tight group, fourteen is
# somebody finding a different swing every time.
_CONSISTENCY_FINE = 0.04
_CONSISTENCY_SPAN = 0.14


def consistency_of(baseline: ClubBaseline) -> tuple[Observation, float] | None:
    """How scattered the recent shots with this club were.

    The one fault that is not about the shot in hand, so it is measured here
    rather than in the fault table. Carry when the device reports it, because
    that is what a player judges a group by, and ball speed when it does not.
    """

    if baseline.shots < _CONSISTENCY_MINIMUM_SHOTS:
        return None

    summary = baseline.metrics.get("carry") or baseline.metrics.get("ball_speed")
    if summary is None or summary.standard_deviation is None or summary.mean <= 0:
        return None

    variation = summary.standard_deviation / summary.mean
    severity = clamp(
        (variation - _CONSISTENCY_FINE) / (_CONSISTENCY_SPAN - _CONSISTENCY_FINE)
    )
    # No baseline mean: this observation is already a comparison against the
    # player's own shots, and the mean it came from is a carry in yards. Putting
    # that in a field the evidence renders with this observation's unit would
    # read as "usual scatter 184 per cent".
    observation = Observation(
        focus=CoachingFocus.CONSISTENCY,
        label="shots scattered",
        value=round(variation * 100, 1),
        unit="%",
    )
    return observation, severity


def diagnose(
    shot: Shot,
    baseline: ClubBaseline,
    preferred: CoachingFocus | None = None,
    fine: float = SEVERITY_FINE,
) -> Diagnosis:
    """Say what was wrong with one shot, in the player's own terms."""

    club = club_of(shot)
    observations: list[Observation] = []
    candidates: list[FocusCandidate] = []

    for fault in FAULTS:
        result = observe(fault, shot, club, baseline.metrics)
        if result is None:
            continue
        observation, severity = result
        observations.append(observation)
        candidates.append(FocusCandidate(focus=fault.focus, severity=severity))

    scatter = consistency_of(baseline)
    if scatter is not None:
        observation, severity = scatter
        observations.append(observation)
        candidates.append(
            FocusCandidate(focus=CoachingFocus.CONSISTENCY, severity=severity)
        )

    # Worst first, then alphabetically, so two equally bad faults are always
    # broken the same way and a diagnosis never depends on table order.
    candidates.sort(key=lambda candidate: (-candidate.severity, candidate.focus.value))
    # The observations follow, so that anything reading them — a person, a
    # prompt — meets the worst one first rather than whichever the table lists
    # first.
    ranked = _in_candidate_order(observations, candidates)
    chosen = _choose(candidates, preferred)

    if chosen is None or chosen.severity < fine:
        return Diagnosis(
            shot_id=shot.shot_id,
            focus=CoachingFocus.NONE,
            # The best-known severity even though nothing is being said, so a
            # stored row records how close the shot came to being worth a word.
            severity=0.0 if chosen is None else chosen.severity,
            display="",
            speech="",
            club=club,
            handedness=shot.handedness,
            baseline_shots=baseline.shots,
            candidates=tuple(candidates),
            observations=ranked,
        )

    matched = _observation_for(observations, chosen.focus)
    label = None if matched is None else matched.label
    positive = True if label is None else is_positive(chosen.focus, label)
    return Diagnosis(
        shot_id=shot.shot_id,
        focus=chosen.focus,
        severity=chosen.severity,
        display=display_for(chosen.focus, label),
        speech=speech_for(chosen.focus, positive),
        club=club,
        handedness=shot.handedness,
        baseline_shots=baseline.shots,
        candidates=tuple(candidates),
        observations=ranked,
    )


def _in_candidate_order(
    observations: Sequence[Observation], candidates: Sequence[FocusCandidate]
) -> tuple[Observation, ...]:
    by_focus = {observation.focus: observation for observation in observations}
    return tuple(
        by_focus[candidate.focus]
        for candidate in candidates
        if candidate.focus in by_focus
    )


def _choose(
    candidates: Sequence[FocusCandidate], preferred: CoachingFocus | None
) -> FocusCandidate | None:
    """The worst fault, unless what the player is working on is nearly as bad."""

    if not candidates:
        return None
    worst = candidates[0]
    if preferred is None:
        return worst
    for candidate in candidates:
        if (
            candidate.focus is preferred
            and candidate.severity >= worst.severity - PREFERENCE_TOLERANCE
        ):
            return candidate
    return worst


def _observation_for(
    observations: Sequence[Observation], focus: CoachingFocus
) -> Observation | None:
    for observation in observations:
        if observation.focus is focus:
            return observation
    return None
