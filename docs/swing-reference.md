# Swing reference

Ideal Swing 2.9 (`config/ideal_swing.json`, version `2.9-timing-first`) is the
only golf reference the live coach and the saved-swing command use. The
assessment shape lives in `config/swing_reference/assessment.schema.json`.
CaddieJ does not read the external folder those files were copied from.

A captured shot is assessed only when `COACHING_VISION_CALLS=true` and
`COACHING_PROVIDER` can make the call. Until then the shot is not coached.
There is no second coaching mode and no measured-template fallback.

`COACHING_IMAGE_DETAIL` defaults to `high`. That reads hands, shaft, arms,
hips and shoulders more closely than `low`, and it costs more input tokens
per image. It does not change how many images are sent.

`COACHING_EVIDENCE_BUDGET` defaults to 24. `COACHING_EVIDENCE_ALLOCATION`
defaults to `dtl_primary`, which at that budget keeps 20 down-the-line frames
and 4 face-on frames. A different budget scales those weights and the sampler
runs again at the new budget. The live coach sends every frame that selection
kept.

The full reference is about 27,000 tokens. It is sent as a stable prefix,
identical for every shot of this version, and the shot JSON does not repeat
it. OpenAI can cache an identical prefix automatically. This installed SDK
has no separate cache-key parameter, so the prefix order is what makes that
possible.

`COACHING_FEEDBACK` chooses what is said after the session picks a rule.
`exact` speaks the stored correction and makes no second call. `observed`,
the default, may place the assessment's own observation in front of that
sentence when the observation is one sentence, stays on this checkpoint, and
does not add an instruction or an unsupported comparison. `worded` makes a
second, text-only call that may supply that observation. In all three the
technical sentence is the stored correction, unchanged. A draft that rewrites
it, names another checkpoint, or adds a technique is discarded, and the
player hears the stored sentence alone. The floor shows the same text as
speech.

Provisional waits, from the shot result to the first sound, and the same
bands for the floor line: under 2 seconds is excellent, 2–4 seconds is good,
4–6 seconds needs optimization, and over 6 seconds is too slow for normal
coaching. Those are targets. A test that injects a provider delay is
simulated and is not a measurement of GPT.

## Live shots

With `COACHING_VISION_CALLS=true`, a finished swing capture is the same
assessment, validation, ranking and session decision as the saved-swing
command. The approved correction, or the stored positive text, is spoken
unchanged. `observed` and `worded` may place one checked observation in front
of it. A newer shot cancels one still in flight, and a late reply does not
move the session or the floor. Speech counts as delivered only when it
plays through. Each shot logs monotonic timings from the shot result through
the floor line and the start of speech. The log has counts and durations,
not image bytes and not the API key.

With vision calls on, each shot keeps the full down-the-line and face-on
timeline until the evidence package has been built, then drops the frames
that package did not select. A shot requested with
`SWING_CAPTURE_DIAGNOSTIC_TIMELINE_SHOTS` keeps its full timeline. The
package is the only frame set the coach sends: selected image bytes, camera
identity, timestamps, accepted stage labels, stage uncertainty, and the
impact estimate. Club, shot type and handedness stay unknown until a caller
supplies them.

A directory that has neither `evidence_package.json` nor a diagnostic
timeline is not sent. Stages stay unresolved when the pose model is missing,
and an estimated impact is not reported as confirmed contact. A failed
package, a rejected assessment, a duplicate shot or a shot that arrived out
of date produces no line.

The pose file is not downloaded during a shot or at startup. OpenCV 4 and
MediaPipe are the `vision` extra (the `research` extra installs the same
packages). MediaPipe 0.10.35 depends on `opencv-contrib-python` and does not
cap the version, so the install must use the constraint file. That pins one
OpenCV 4 distribution and does not need a later uninstall:

```shell
python -m pip install -e ".[vision]" -c constraints/vision.txt
python scripts/install-pose-model.py
python scripts/verify-vision.py --require-model
```

The model lands at `models/pose_landmarker_full.task`, or at
`CADDIEJ_POSE_MODEL` when that variable is set. Its SHA-256 is
`5134a3aad27a58b93da0088d431f366da362b44e3ccfbe3462b3827a839011b1`.
The file is gitignored. A matching file is left in place. A missing or
corrupted file is an error from the installer and, at runtime, leaves swing
stages unresolved. Motion analysis and impact estimation still run. Neither
failure stops the round.

Windows setup, saved-swing checks, and the live session are in
[Windows validation](windows-validation.md). That guide is the install
procedure. Do not uninstall OpenCV and reinstall another `cv2` package
afterwards.

`COACHING_CALL_LIMIT` caps vision calls for the run. `COACHING_ENABLED=false`
does not assemble coaching.

## Analyze a saved swing

No model call:

```shell
caddiej --analyze-reference-swing \
  --reference-package path/to/evidence_package.json \
  --reference-shot-dir path/to/raw-capture \
  --reference-output /tmp/reference-shot \
  --reference-mock insufficient
```

`--reference-mock` may be `insufficient` or a path to an assessment JSON.
`--reference-call-model` is the explicit approval for one request to
`COACHING_MODEL`, using `COACHING_API_KEY`. Without one of those two flags the
command only writes the image request.

Player context is unknown unless you pass `--reference-club`,
`--reference-shot-type`, `--reference-handedness`, `--reference-distance`,
`--reference-target` and `--reference-evidence-context`. Unknown is not filled
in from the reference's default iron.

The output directory receives `request.json`, `images.json`, `assessment.json`,
`decision.json` and `session_state.json`. Pass the state file back with
`--reference-state` on the next shot, and `--reference-sequence` with a higher
number. A repeated shot id or an older sequence produces no new line.

The command does not speak and does not draw on the floor. The line it selects
is the stored correction, or the stored positive text. Both already name the
swing moment. That line is marked delivered for the next request only when
you pass `--reference-delivered`, after the golfer was actually told.

Session counts of one, two and three successful executions are session policy.
They decide when a focus is reinforced or may advance. They are not golf rules
from `ideal_swing.json`. A successful execution means this swing shows the
requested movement. It is not a claim that the swing improved compared with
an earlier one.
