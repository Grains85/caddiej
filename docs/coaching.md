# Coaching

[← Documentation index](README.md)

CaddieJ has one golf coach: Ideal Swing 2.9. The technical text lives in
`config/ideal_swing.json`. A model does not rewrite the approved correction.

A live shot is assessed only when `COACHING_VISION_CALLS=true` and
`COACHING_PROVIDER` can make the call. Until then the shot is not coached, and
nothing else is spoken in its place.

The path is:

1. Swing capture writes the down-the-line and face-on timeline.
2. The evidence pipeline reads those frames once, then runs motion analysis,
   pose stages, and impact estimation.
3. Frame selection keeps a budget of images (24 by default, 20 down-the-line
   and 4 face-on) and records why each frame was kept.
4. The vision model assesses that package against Ideal Swing 2.9.
5. The assessment is validated. Ranking and the session choose one focus.
6. The stored correction is what the floor and the voice say. `observed`, the
   default, may place one checked observation in front of that sentence.

One focus per shot. A newer shot cancels one still in flight. A failed model
call or a failed evidence package does not stop the round.

Details, including the pose-model install, are in
[Swing reference](swing-reference.md). Settings are in
[Configuration](configuration.md).

`COACHING_EVIDENCE_BUDGET` is the image budget. Capture still has its own
`SWING_CAPTURE_SELECTED_FRAMES` count for the packaged stills it writes beside
the timeline. That count is not the set sent to the model.
