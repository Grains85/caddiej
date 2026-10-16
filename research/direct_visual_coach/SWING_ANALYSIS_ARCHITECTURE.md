# Swing evidence and coaching

CaddieJ separates evidence production from coaching. The hard boundary is
`SwingEvidencePackage`. Everything above that line produces original-frame
references, motion measurements, stage estimates, and an impact estimate.
Everything below it may interpret those references. It does not go back and
choose frames, rename stages, or invent an impact time.

```
EVIDENCE PRODUCTION
  capture
  motion analysis
  swing stage detection
  impact estimation
  evidence selection
        |
        v
SwingEvidencePackage
        |
        v
COACHING
  visual assessment
  fault and strength classification
  coaching prioritization
  lesson selection
  HUD / TTS
```

The evidence producer does not know which model will read the images, which
faults are in a catalog, which faults a golfer has, or which cue will be
chosen. The coaching side does not control capture timing, frame selection,
motion analysis, stage detection, or impact detection.

A package is immutable after it is written. The raw capture stays where
capture put it. A newer evidence algorithm writes a new versioned directory
and reads the same originals. JSON holds references, not image bytes.

Only the evidence side of this boundary is being built here. Assessment,
the coaching decision, and delivery are specified so the package has somewhere
to go. They are not part of the current work, and the package has no fields
for them.

## What belongs where

| Evidence production | Coaching |
| --- | --- |
| Capture, sync, timestamps, camera identity, `shot_id` | Visual assessment of the selected frames |
| OpenCV motion, scene cuts, temporal activity | Fault and strength classification |
| Stage intervals, uncertainty, unresolved stages | Choosing one coaching focus |
| Impact-region estimate, or an explicit unresolved result | Lesson and cue selection |
| Bounded selection of original frames | Floor HUD and speech |
| The versioned package | History of what was said |

Stage 4 may mark a stage or impact estimate uncertain. It must not write a
replacement timeline. Stage 5 consumes assessment findings. Stage 6 consumes
the coaching decision.

## Owners

| Piece | Owner now | Must not import |
| --- | --- | --- |
| Capture | `caddiej.swing_capture` | assessment, coaching, delivery |
| Motion analysis | `temporal_sampler.py` (frozen) | stages, faults, models |
| Stage detection | `swing_stage_detector.py` | the sampler, the catalog, models |
| Impact estimation | `impact_estimator.py` | stages as an input, faults, models, ProTee contact time |
| Evidence selection and packaging | `evidence_package.py` | the catalog, the classifier, coaching |
| Package contract | `SwingEvidencePackage` in `contracts.py` | fault ids, cue text, advice |
| Assessment, decision, delivery | not implemented on this contract | image selection |

`evidence_package.py` calls the sampler, the stage detector, and the impact
estimator. That is composition. It does not rewrite their algorithms. The
sampler and the stage detector do not import the package.

The dependency direction inside evidence production is downward only:
capture, then motion and stages and impact, then selection, then the package.
Impact estimation does not read stage names. Selection may use an accepted
stage interval or an estimated impact interval, and it falls back to the
sampler when either is unresolved. It does not invent the missing label.

The P1–P9 labels in `caddiej.domain.swing_phase` and the replay-clock aiming
in `caddiej.swing_capture.phases` are a different system. Evidence production
does not read that clock, those labels, a manual contact tick, or a previous
model reply.

## Rules that apply to every result

1. A producer owns its result. A later step must not rewrite it.
2. Results are typed values with `schema_version`, `shot_id`, `status`, and
   `provenance`.
3. `shot_id` is the correlation key. Research captures use the shot directory
   name when the capture metadata has no separate shot id.
4. Package status is `ok`, `partial`, `insufficient_evidence`, or `failed`.
   An impact estimate uses `estimated` or `unresolved`, because "ok" would
   imply contact was established.
5. Frames are references: camera, relative path, capture tick, timestamp,
   optional digest and size. JSON does not contain image bytes.
6. A missing measurement stays missing.
7. Confidence is an uncalibrated heuristic unless a result says, in its
   provenance, that a calibration was performed. None of these results say that.

`provenance` is `stage`, `producer`, `producer_version`, and `created_at` when
the result is written.

## Capture

Input: ProTee VX events and OBS sources.

Output: synchronized original frames for down-the-line and face-on, timestamps,
camera identity, and `shot_id`. The raw directory is the source a later
package is regenerated from.

Owns capture, synchronization, persistence, and metadata. Shot retention is
bounded. The default is 3 directories. `SWING_CAPTURE_DIAGNOSTIC_TIMELINE_SHOTS`
and `SWING_CAPTURE_EVIDENCE_KEEP` can rise together up to 40 so a research
timeline is not deleted by the next shot. The default disk use stays at 3.

Must not classify stages, name faults, or choose coaching.

## Motion analysis

Input: chronological original frames and timestamps. The owner is
`research/direct_visual_coach/temporal_sampler.py`. Its algorithm, thresholds,
allocation, and outputs stay as they are.

Output: motion rates, scene cuts, and a chronological selection of original
frames for one camera and one budget. Selection reasons are sampling reasons.
They are not stage names.

Must not name stages, name faults, infer impact, or choose coaching.

## Swing stage detection

Input: the full chronological camera sequence and real timestamps. Not the
selected subset. Not a uniform frame rate.

Output: `SwingStageResult`. Direction changes, accepted intervals, candidates
that did not clear the bar, and unresolved stages.

Must not name faults or treat a hand-height peak as impact. The research
taxonomy is `SETUP`, `BACKSWING`, `TRANSITION`, `DOWNSWING`, `IMPACT_REGION`,
`FOLLOW_THROUGH`, `FINISH`. This detector does not return `ok` and does not
emit `IMPACT_REGION`. Ball contact is a separate estimate.

Failure: no accepted direction change is `insufficient_evidence`. An accepted
direction change with impact unresolved on the stage result is `partial`.

## Impact estimation

Input: chronological frames and their timestamps, after a generic scene cut
has dropped a loading screen. Both cameras are estimated independently.

Output: `ImpactEstimate`. An interval between two frames when a stationary
compact spot changes and stays changed, or `unresolved` with a reason such as
`ball_not_visible`, `no_motion_onset`, or `camera_disagreement`.

The classification `observed_ball_motion_onset` means the spot left. It does
not mean the club was seen touching the ball. `inferred_contact_interval` is
reserved for a later observation that actually shows contact. An optical-flow
peak is not impact.

ProTee `ShotDetected` is not a constraint. It is not an exact contact time on
the replay frame clock.

## Evidence selection

Input: original frames, the sampler's selection, accepted stage intervals when
they exist, and an impact estimate when its status is `estimated`.

Output: a bounded list of original frames inside `SwingEvidencePackage`.
The default budget is 24 images. The default split is the `dtl_primary`
preset: 20 down-the-line (`swing_behind`) and 4 face-on (`swing_side`).
Other presets are `dtl_only` (24 and 0), `balanced` (12 and 12), and
`fo_primary` (8 and 16). A different total budget scales those weights.
An explicit split is also accepted. A preset and an explicit split are not
accepted together. Unknown presets, a non-positive budget, and a split that
does not add up to the budget are rejected.

A camera that is not on disk does not keep its share. That share moves to
the cameras that are present. A preset share of zero stays zero when the
camera is present, so `dtl_only` does not invent face-on frames. If the only
weighted camera is missing, the camera that does exist receives the budget.
The package records the requested split and the applied split on
`allocation`. Those numbers are view weights. They are not fault weights.

The sampler's selection is the base, so coverage and rapid motion survive when
stage or impact detection fails. Accepted stages that contain no selected
frame gain one frame, with reason `stage_coverage`. An estimated ball-departure
interval gains up to two frames per camera inside that interval, with reason
`impact_interval`. An unresolved impact adds none. Duplicate digests are kept
once. Unreadable frames are dropped. Frames before the sampler's scene cut are
not added back. If one camera cannot fill its share with unique frames, the
unused slots move to the other camera. The package is not padded with
duplicates. Original frames are only read.

`fallback` records which of those steps ran: `generic_motion`,
`stage_coverage`, `impact_density`, or `stage_and_impact`.

## SwingEvidencePackage

The package names the cameras, the budget, the allocation, the selected
original frames, the stage estimates it was given, the impact estimate, a
clock comparison between cameras, and the fallback. `raw_capture_preserved`
records that the originals were not rewritten. The package has no coaching
fields.

Camera clocks are compared on `offset_seconds` at shared capture ticks. A
shared tick is not treated as synchronization. When the gap is large, the
package says so and still selects each camera on its own timestamps.

## Visual assessment

GPT, when it is called, reads the current `SwingEvidencePackage` and, after
the first shot, a `PreviousCoachingContext`. It returns a
`VisualAssessmentResult`: supported strengths and weaknesses, evidence
references, an uncalibrated confidence, and `no_proven_finding` when no
weakness is supported. A causal note stays an unverified hypothesis.

The follow-up on that result separates two claims. `current_focus_status`
says whether this swing shows the requested movement, still shows the fault,
is mixed, or cannot be judged. `change_since_previous` says whether this
swing differs from the previous one. Those are different claims. With no
previous frames, the change stays unknown. A missing fault, a lower
confidence, and a ball flight are not a measured improvement.

GPT does not choose the session focus, write the cue, or edit the package.
`request_adapter.py` builds the request from the package and the previous
context. It does not call a model, and it does not replace the frozen
`simple_visual_classifier.py`. The default request attaches no previous
images. Up to two may be attached when a comparison is requested, the camera
matches, and stage correspondence is already reliable. Uncertain stage
correspondence attaches nothing.

## Session coaching

`session_coach/` is research code. Production coaching, the floor HUD, and
speech are not wired to it.

The coaching engine reads an assessment, a versioned rulebook, and
`CoachingSessionState`. It returns one `SessionCoachingDecision` and the next
state. Decision types are `START_FOCUS`, `CONTINUE_FOCUS`,
`REINFORCE_PROGRESS`, `ADVANCE_FOCUS`, `REASSESS_FOCUS`, and `NO_COACHING`.
One decision has one active focus.

Priority comes from `priority_rank` in the rulebook. Confidence is only an
eligibility floor, and it is not a probability. Equal ranks break toward the
alphabetically earlier finding id, and the decision records that the rank was
provisional. A possible cause stays unverified and is not copied into the
decision as something that was seen.

The engine keeps the active focus while the assessment still supports that
fault, or while the follow-up is uncertain. A visible requested movement on
the current frames can reinforce that same focus. The decision then says the
movement is visible. It does not say the swing is better than the previous
one. A comparative claim is used only when the follow-up includes previous
frames and reliable stage correspondence. A supported strength can be noted
on the decision and does not replace the focus.

Counts still live on the rule. An explicit current-swing execution and a
supported comparison both add toward `IMPROVEMENT_OBSERVED`,
`IMPROVEMENT_CONFIRMED`, and `FOCUS_MASTERED`. The engine does not advance
because a fixed number of shots has passed. A legacy assessment that sets
`comparison` to `improved`, and has no follow-up, still counts. That mark is
the assessor's explicit statement from the first session contract.

A failed assessment, a duplicate shot, an out-of-order shot, or a different
rulebook version returns `NO_COACHING` with status `failed` and the previous
state object. The session is not rewritten.

Delivery copies the rule's HUD line (2 to 5 words) and speech (1 or 2
sentences). It does not write new advice. After a presented delivery, the
next `PreviousCoachingContext` carries the active focus, that approved cue,
and the supported observation from the assessment. If delivery did not
happen, the cue is omitted. If no delivery result is available, the status
is `unknown` and the cue is still omitted.

`replay.py` reads saved assessments and one rulebook. Each step records the
previous context, the evidence package identity when one was supplied, the
decision, the next state, and the context the following assessment would
receive. The same assessments and the same rulebook produce the same
decisions. Another rulebook version can be replayed from an empty session.
A session already stamped with a rulebook version refuses a different one.

## Coach-video knowledge, not built as a pipeline

A later knowledge base can store a video reference, coach, timestamps, the
original words, an extracted observation, an unverified possible cause, a
suggested correction, a drill, stages, uncertainty, review status, and
contradictions. Review status starts unreviewed. Unreviewed and rejected
records cannot become rules. An approved record can become a draft note, and
a person still has to edit the versioned rulebook. Contradictions are stored,
not merged. No video is downloaded by this work. See
`session_coach/COACH_VIDEO_KNOWLEDGE.md`.

## What this repository contains now

Implemented, research only unless noted:

- `contracts.py` — evidence and assessment result types. The package is the boundary.
- `temporal_sampler.py` — frozen motion sampler.
- `swing_stage_detector.py` — stage estimates from pose. No impact.
- `impact_estimator.py` — ball-departure interval, or unresolved.
- `evidence_package.py` — selection, allocation presets, and package writer.
- `session_coach/` — rulebook, session state, previous-shot context, assessment
  request, priority, progress, engine, delivery brief, knowledge records, and
  offline replay.
- `SWING_ANALYSIS_ARCHITECTURE.md` — this boundary.
- `STAGE3_REPORT.md` — the earlier five-swing stage result.
- `EVIDENCE_PIPELINE_REPORT.md` — impact and package results on the same five swings.
- Retention: `EVIDENCE_KEEP` stays 3. The cap is 40. A timeline setting of 20
  is accepted and retains 20 shot directories. `.env` is not edited by this work.

The fault catalog and the GPT classifier are unchanged and are not inputs to
the package or the session engine. Saved evidence packages under
`results/evidence_pipeline/v1` were built with an even 12 and 12 split. A new
run of `evidence_package.py` uses `dtl_primary` unless `--preset` says
otherwise. This work does not regenerate those five shots.

## Limitations

The synthetic rulebook is a fixture. Real cues from coaching video are a
separate project: a person edits a new rulebook version after review. This
engine does not measure a swing. A follow-up that says the movement is
visible is a statement about the current frames. A follow-up that says the
swing changed needs previous frames, which stay off unless a later caller
asks for them. Confidence is an eligibility floor, not a chance the fault is
real. Face-on stage labels from the down-the-line pose detector were not
validated as coaching evidence. Production HUD and speech are unchanged, and
this loop does not call a model.

