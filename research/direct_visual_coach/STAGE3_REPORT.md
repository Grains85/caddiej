# Stage 3 feasibility report

Development examples only. Five swings, one golfer, one down-the-line
camera. The same detector and the same thresholds were used on every shot.
Nothing here was tuned to make these five look correct.

Architecture and contracts: `SWING_ANALYSIS_ARCHITECTURE.md`,
`contracts.py`. Detector: `swing_stage_detector.py`. Results:
`results/swing_stage_detector/`.

The temporal sampler, the Visual Fault Catalog, the simplified classifier,
production capture, and `.env` were left unchanged. No OpenAI call was made.

## What the detector does

It reads every original `swing_behind` diagnostic frame and the capture
clock `diagnostic_timeline.offset_seconds`. It does not read strike offsets,
replay-start offsets, phase labels, or the 24-frame sample.

MediaPipe Pose Landmarker (`pose_landmarker_full`, float16, video mode,
one pose, default confidence 0.5) tracks shoulders, elbows, wrists, and
hips in normalized image coordinates. A missing landmark stays missing.

The stage signal is torso-normalized hand elevation: how far the visible
wrist midpoint sits above the hip midpoint, divided by the shoulder-to-hip
distance. A local maximum is accepted as a transition candidate only when
the hands rise at least 0.75 torso lengths, then fall at least 0.50, with
a velocity reversal. The transition interval is the time the hands stay
within 12 percent of that rise of the peak. The accept bar is 0.60 on an
uncalibrated heuristic. Child stages inherit that score. They are not
separate measurements.

`IMPACT_REGION` is never emitted. A low hand position is the end of the
descent, which is not ball contact.

## MediaPipe

Compatible with this Python 3.12 venv at mediapipe 0.10.35 with numpy 2.
Version 0.10.21 was not compatible: it pinned numpy 1.26 and broke the
existing OpenCV headless pin. The research extra now asks for
`mediapipe>=0.10.35,<0.11`. The model is downloaded on first use
(SHA-256 `5134a3aa…b1`, about 9.4 MB) and is gitignored.

Video tracking finds the golfer once the person is in frame. A single-image
call at the default 0.5 confidence did not detect the golfer on a
top-of-swing frame, including after square padding. The tracker depends on
continuity from earlier frames. A clip that starts at the top can fail.

On these frames:

- Shoulders and hips are continuous while the pose is present, and stable
  enough to scale the hand path. The drawn joint centers sit high of the
  real joints (shoulders toward the head, hips toward the thigh).
- One wrist, the person's right wrist in these captures, tracks the hands
  through the swing. The other wrist is below the visibility floor for most
  of the backswing and again after the hands come down.
- Elbows are recorded and are not used. The left elbow is intermittent.
  The right elbow is continuous and still not on the elbow.
- Visibility is present. It is not positional accuracy.
- MediaPipe warns that this portrait frame (1002×1480) is not the square
  region its projection step expects. Coordinates are still usable for the
  vertical hand signal. They are not survey measurements.

Optical flow was not needed to find the direction change, and it was not
used as an impact detector.

## Results

All five returned `partial`. Transition and downswing were accepted.
Impact is unsupported on every shot. Confidence is the uncalibrated
heuristic, not a probability.

Pose detection is about two thirds of the frames because the timeline
starts on the replay loading screen. The first frame with a pose is the
frame after the golfer is already visible, so at least one address frame
is missed.

| Shot | Pose | Usable hand frames | Both wrists | Transition (s) | Downswing (s) | Setup | Impact |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `…93738c0a` | 35/51 | 35 | 14 | 2.811–3.098, ticks 24–27 | 3.192–3.952, ticks 28–42 | unresolved | unresolved |
| `…09132997` | 34/51 | 32 | 14 | 2.914–3.214, ticks 25–28 | 3.310–3.952, ticks 29–42 | unresolved | unresolved |
| `…694d0a52` | 34/51 | 33 | 12 | 2.909–3.463, ticks 25–32 | 3.511–3.813, ticks 33–39 | 2.210–2.500 | unresolved |
| `…345eaca1` | 35/51 | 35 | 14 | 2.914–3.465, ticks 25–32 | 3.514–3.909, ticks 33–41 | 2.107–2.407 | unresolved |
| `…5b29909f` | 34/51 | 34 | 14 | 2.805–3.105, ticks 24–27 | 3.201–3.898, ticks 28–41 | unresolved | unresolved |

Heuristic confidence was 0.84, 0.86, 0.88, 0.87, and 0.84. The peak rise
was 1.54–1.87 torso lengths and the fall was 1.36–1.80. Each peak used
one wrist above the visibility floor.

Right-wrist continuity, among frames where that wrist cleared 0.5, was
0.85–1.00. Left-wrist continuity on its usable frames was 0.79–1.00, but
those usable frames are a short run around the descent. The left wrist is
missing from the start of the clip through the transition (about 27–30
frames, ending between 3.10 s and 3.35 s) and missing again through most
of the follow-through.

Review sheets (`stage_review_sheet.jpg`) show the original frames around
each accepted boundary, plus neighboring frames, with landmark dots drawn
only on those copies. The original JPEGs were not modified.

On all five sheets the transition row is the top of the swing: hands and
club up. The backswing row is the hands rising. The follow-through row is
the hands rising again after the low point. That matches the elevation
plots.

The limitation that matters for the original failure: the downswing
interval starts on the first frame after the peak band, and those first
frames still show the hands near the top. The later frames in the same
interval show the hands and club coming down. Calling the whole interval
"delivery" would repeat the top-versus-delivery confusion. The transition
interval itself is the top. `FINISH` stayed unresolved because the capture
ends while the hands are still moving. Setup was accepted only on the two
swings where a short stable prefix existed before the rise. On the other
three, pose tracking began during the takeaway.

Shots 3 and 4 hold the hands near the peak for about 0.55 s (eight
frames). The other three hold for about 0.29 s (four frames). That
difference came out of the same 12 percent band. It was not adjusted.

## Performance

Measured in one process on these 51-frame clips, after the memory API
call was given the correct Windows signature.

| | Init | Mean / max per frame | Inference | Total, with plots | Working set | Peak |
| --- | --- | --- | --- | --- | --- | --- |
| Range | 55–70 ms | 7.0 ms / 12.7–13.9 ms | 0.355–0.363 s | 1.06–1.56 s | 108–112 MiB | 208–217 MiB |

A separate cold-process probe created the landmarker in 0.47 s the first
time. Speed is not the reason to reject the approach. A production run
would still have to stay off the gameplay path. Correctness is the open
question, and these five swings do not settle it.

## Generalization

These results do not transfer on the evidence we have.

- One golfer. Body size, tempo, and clothing are one sample.
- Handedness is unknown. Left and right here are the pose model's image
  sides. Lead and trail were not assigned. A left-handed golfer was not
  tested. The vertical signal does not require a handedness label, and
  that is untested.
- One indoor down-the-line camera, portrait, with a loading screen, a
  wooden rail, and a green alignment overlay. Face-on is untested.
  Camera roll would break an image-vertical elevation signal.
- One wrist carries the reversal. Clothing or a camera angle that hides
  that wrist removes the signal. The detector will not invent the other
  wrist.
- The pose model misses the first visible address frame and fails in
  single-image mode at the top. Different lighting or a partial body can
  drop the track.
- Joint centers are biased. Elbow and shoulder angles are not ready for
  a fault decision.
- Swing speed and proportions were not varied. The gates are in torso
  lengths and seconds, which avoids a fixed frame count, and five similar
  swings cannot show that those gates survive a different tempo.

## Next experiment

Keep this detector frozen, including the 12 percent band and the 0.60
bar.

Give the simplified visual classifier the accepted stage intervals as
read-only orientation. Allow it to mark an interval uncertain. Do not
allow it to invent a replacement timeline. Score whether delivery faults
(`HAND_PATH_OUT`, `STEEP_DELIVERY`, `OUT_TO_IN_DELIVERY`) still land on
frames inside the transition interval.

Treat only the transition interval as "hands at the top." Do not treat
the whole downswing interval as "at the ball." Keep impact unresolved
until some observation is actually contact.

Do that on swings that are not these five, and include another golfer
before any claim that the orientation is general. Do not integrate it
into the live coach as part of that experiment.

## Retention

`SWING_CAPTURE_DIAGNOSTIC_TIMELINE_SHOTS=20` is rejected because
`Settings.swing_capture_diagnostic_timeline_shots` has `le=3`, and
`caddiej.swing_capture.store.EVIDENCE_KEEP` is 3. The diagnostic JPEGs
live inside the shot directory that prune deletes. `prepare()` prunes on
startup. The keep is the newest N, so a later shot evicts the oldest.

`SWING_CAPTURE_DEBUG_KEEP_SHOTS` does not hold the diagnostic timeline.
It copies `latest_swing`, and the settings cap of 3 stops a 20-shot
timeline from being written in the first place.

One archived shot here is about 13.0 MB, of which the diagnostic JPEGs
are about 10.1 MB and the rest is about 2.8 MB. Twenty complete
diagnostic swings are about 261 MB. Twenty shots kept as selected frames
only are about 57 MB. The current three full diagnostic shots are about
39 MB. Disk is the smaller constraint. The store comment treats the
directory as private home-golf video, which is why the keep is small.

The smallest change that would actually retain 20 complete research
swings is to raise both `EVIDENCE_KEEP` and the settings maximum from 3
to 20, together with the test and the docs that hardcode 3. Changing
`.env` alone does nothing. That change was not made.
