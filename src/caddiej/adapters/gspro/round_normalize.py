"""A round-file observation, expressed as the shot CaddieJ owns.

Without this, coaching does not happen in the mode CaddieJ actually runs in.
`RoundShotObserved` reaches the floor and stops there, so a passively observed
session produces a result panel and never a diagnosis — which is a gap in the
wiring rather than in the analysis, and it is closed here because unit
conversion of one vendor's file is adapter work.

What is converted is deliberately almost nothing.

`BallSpeed` is metres per second: a real value of 32.8386 was shown by the
simulator as 118 km/h, and 43.04 as 155 km/h. Two captures, one conversion,
consistent both times, and it is the only number in the file whose units are
established well enough to translate.

Everything else is left out, and each omission is a decision:

* **No launch angle and no start line.** The file has neither. A shot with
  zeroes in those fields would be a shot that flew flat and dead straight, and
  coaching would then have two faults to talk about that nothing measured.
* **No carry.** `TotalDistance` is how far the shot *went*, which for anything
  hit off a tee includes run. Putting it in `carry_yards` would quietly wrong
  every club distance and every dispersion figure derived from it. Ball speed
  is enough to tell one strike from another, and it is the truth.
* **No club data at all.** Face, path, attack angle and club speed are not in
  this file, and no arrangement of a ball speed produces them.

So the shot this builds carries one measurement. That is a small thing to hand
to a coach, and it is the honest amount: it supports a baseline, a trend, and
scatter across a club's shots, and it supports nothing about the club that
nothing measured. The club and handedness are attached later by the domain,
which learns both from the launch monitor's own log.
"""

from datetime import datetime
from uuid import UUID, uuid5

from caddiej.domain.observation import ObservedRoundShot
from caddiej.domain.shot import BallLaunch, Shot, ShotSource

# Metres per second to miles per hour. The one verified unit in the file.
_MPH_PER_MPS = 2.236936292054402

# Namespace for turning GSPro's own shot id into CaddieJ's. Fixed, so the same
# round-play shot is the same shot id on every run: the file is re-read at
# startup, and a shot that came back with a new identity would be a second shot
# in history, a second line on the floor and a second call to a model.
_ROUND_SHOT_NAMESPACE = UUID("6f1b5c4e-2a7d-4f8b-9c3e-5d0a1b2c3d4e")


def shot_id_for(observed: ObservedRoundShot) -> UUID:
    """CaddieJ's id for a round-play shot, derived from GSPro's own."""

    return uuid5(_ROUND_SHOT_NAMESPACE, observed.shot_id)


def shot_from_round(
    observed: ObservedRoundShot,
    device_session_id: UUID,
    observed_at: datetime,
    observed_monotonic: float,
) -> Shot | None:
    """The shot as CaddieJ owns it, or None if there is no measurement in it.

    None rather than an empty shot: a round entry with no ball speed is a putt
    conceded, a hole recorded, or a partial write, and inventing a shot for it
    would put a line on the floor about a swing nobody made.
    """

    speed_mps = observed.ball_speed_raw
    if speed_mps is None or speed_mps <= 0:
        return None

    return Shot(
        shot_id=shot_id_for(observed),
        source=ShotSource(
            kind="gspro_round",
            device_session_id=device_session_id,
            observed_at=observed_at,
            observed_monotonic=observed_monotonic,
            device_id=observed.shot_id,
            device_sequence=observed.global_shot_number,
            reported_units="gspro_round_file",
        ),
        ball=BallLaunch(speed_mph=round(speed_mps * _MPH_PER_MPS, 2)),
    )
