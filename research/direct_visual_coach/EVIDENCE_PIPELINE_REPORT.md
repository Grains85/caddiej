# Evidence pipeline report

Development examples only. Five swings, one golfer, one indoor bay.
The same code and the same thresholds ran on every shot. Nothing was
adjusted to make a shot succeed.

Packages are in `research/direct_visual_coach/results/evidence_pipeline/v1/`.
Original JPEGs were not modified.

## Impact

The estimator looks for a small spot on the ground that sits still and then
changes while the area around it does not. The result is an interval between
two frames. The classification is `observed_ball_motion_onset`. The club
meeting the ball was not observed, so nothing here is an inferred contact.

| Shot | Fused status | DTL | Face-on | Interval |
| --- | --- | --- | --- | --- |
| `…93738c0a` | estimated, 0.65 | ticks 41–42, 3.903–3.952 s | `ball_not_visible` | spot near (0.77, 0.65) |
| `…09132997` | estimated, 0.65 | ticks 40–41, 3.857–3.904 s | `ball_not_visible` | spot near (0.77, 0.66) |
| `…694d0a52` | unresolved | `unstable_before_motion` at (0.75, 0.65) | `ball_not_visible` | none |
| `…345eaca1` | unresolved | spot at (0.77, 0.65) unstable; no persistent change | `ball_not_visible` | none |
| `…5b29909f` | unresolved | spot at (0.76, 0.65) unstable; a neighbor never moved | `ball_not_visible` | none |

Confidence is the uncalibrated heuristic, capped at 0.90. 0.65 is not a
probability.

On the two estimated shots the bracket is about 50 ms, while the club is low.
The review sheet draws the spot on the mat near the ball. A look at those
frames does not show the ball clearly leaving. The appearance of that patch
changed and stayed changed. That can be the ball, the club, or the mat. It is
not a confirmed strike.

Face-on found no compact stationary spot on any of the five. In these frames
the ball sits on the bottom edge and is hidden by the golfer for much of the
swing. The selector did not invent a face-on contact frame.

Rejected candidates that did not become estimates: a bright corner at the
bottom left that never moved, and on one shot a mid-frame spot that never
moved. No shot produced two disagreeing estimates. ProTee `ShotDetected` was
not read.

On these five shots the stored `offset_seconds` for a shared capture tick
match exactly (median and max delta 0). That is the capture request clock.
It does not show that the two replay pictures are frame-locked inside ProTee.

## Selection

Default budget 24, split 12 and 12. Every shot returned 24 original frames,
12 down-the-line and 12 face-on, in timestamp order. No unreadable frames.
Duplicate digests in the raw timeline (one pair on shot 2, two on shot 5)
were kept once.

The sampler is the base. Accepted stage intervals that had no selected frame
gained a `stage_coverage` frame. The two estimated brackets each added two
`impact_interval` frames. The three unresolved shots added none
(`stage_coverage` fallback). High-motion regions kept about 6 or 7 of their
9–15 hot ticks per camera. A 12-frame share cannot hold every hot frame.

Stage names on the package are the existing detector's accepted intervals,
including a face-on run of the same hand-elevation method. Those face-on
labels were not separately checked. `IMPACT_REGION` stayed unresolved on
every stage result. The impact field is the ball estimator, not the stage
detector.

## Runtime

About 2.8–3.2 s per shot on this machine. Pose on both cameras was about
1.5–1.9 s. The ball estimator was about 0.22 s. Working set during a shot
was about 116–143 MiB. The process peak climbed to about 250 MiB by the
fifth shot because the peak is for the whole process.

## Retention

`SWING_CAPTURE_DIAGNOSTIC_TIMELINE_SHOTS=20` is now accepted. The default
keep is still 3 directories. The hard cap is 40. A timeline count above the
keep raises retention to that count, so 20 timelines keep 20 shot
directories. Diagnostic files stay inside the shot directory, and startup
pruning uses the same limit. `.env` was not edited.

Twenty full diagnostic shots are about 261 MB. Twenty shots of selected
frames only are about 57 MB. The default of 3 does not change production
disk use.

## Next evidence experiment

Leave these thresholds frozen. The open question is whether a camera that
actually shows the ball, at a higher rate through the strike, can turn
ball-departure into a bracket a person can verify. These five face-on views
cannot. Do not lower the stability gate to accept the three misses. Do not
feed the two estimated brackets to a coach as contact.
