# Swing Score

[← Documentation](README.md)

A number from 0 to 100 for one swing, shown beside the coaching line on the
ProTee-only path, with a score for each phase of the swing and one lesson.
**Swing Score is the technical quality of the golf swing.** Version 4 builds
it from concrete swing checkpoints, the way a coach walks a swing from address
to finish, and every number after the checkpoints is CaddieJ's.

**The model scores checkpoints and nothing else.** It reads ProTee's All
Values grid into typed metrics, says which phase each picture shows, and
scores the checkpoints it can see, 0 to 100, each from one named picture.
CaddieJ computes the phase scores, the Swing Score, the faults, which fault
caused which, the lesson, and whether the player fixed it. The reply schema
has no field any of those could arrive in, and keys a reply adds anyway are
never read.

Code:

- `src/caddiej/coaching/checkpoints.py`: the checkpoint catalogue.
- `src/caddiej/coaching/mechanics.py`: phases and the Swing Score.
- `src/caddiej/coaching/swing_faults.py`: the fault library, ProTee
  corroboration and the root cause.
- `src/caddiej/coaching/lesson.py`: the session lesson.
- `src/caddiej/coaching/score.py`: the Swing Score with ProTee delivery
  beside it.
- Types: `domain/swing_checkpoints.py`, `domain/swing_score.py`,
  `domain/swing_review.py`.

Rubric version: `swing-score-v4`. Prompt version: `vx13`.

**The reply carries facts, not prose.** No checkpoint or metric is named,
because a position is its identity. What the model has to look at is written
as a word:

- `frames` maps each camera cell, by its printed ID (`DTL_P5`, `FO_P3`), to
  the phase the model sees in it: `setup`..`finish`, or `unclear`. Only the
  DTL_ and FO_ cells are keys; IMPACT and ALL_VALUES are not. The printed P is
  only the moment the cell was aimed at; what the model says it shows decides.
- `metrics` is one entry per registry metric, in registry order: `null`
  (unreadable) or `[value, unit, direction]`, where `unit` is that metric's
  1-based unit code and `direction` its 1-based direction code, `0` for none.
  Attack angle and impact height are printed signed with no word, and are
  written signed with direction `0`.
- `checkpoints` is one list per phase and camera, keyed `<phase>_dtl`,
  `<phase>_fo` or `<phase>_any`. Each holds, in catalogue order, the
  scoreable checkpoints of that phase that camera may show: `null` (not
  assessed) or `[score, "CELL_ID", confidence, direction]`. `score` is 0..100
  and not quantised; the cell is a printed ID; `confidence` is a percent;
  `direction` is one of that checkpoint's direction words, or `null`.

The schema restricts what can be written. A `_dtl` list can cite only DTL_
cells, a `_fo` list only FO_ cells, and `_any` either. No list can cite
IMPACT or ALL_VALUES. The parser still checks everything the schema allows.

CaddieJ decodes this back into named checkpoints before anything reads it, so
every later step is unchanged. Nothing is repaired in decoding:

| What arrived | Refused as |
| --- | --- |
| a list of the wrong length, or missing | `length`, for every position in it |
| an entry that is not two integers around a cell ID, then a word or null | `shape` |
| an unknown cell | `frame` |
| IMPACT | `view_unknown` |
| a cell from the wrong camera | `view` |
| a confidence over 100 | `confidence` |
| a direction the checkpoint does not have | `deviation` |
| a cited cell that `frames` calls `unclear`, or leaves out | `phase_unknown` |
| a cited cell that `frames` says shows another phase | `phase` |

A wrong-length list refuses every position because once one is missing, none
can be trusted to be the checkpoint it sits at. A wrong-length `metrics` list
reads no metric.

**A signed metric keeps ProTee's sign.** For attack angle and impact height
the kept reading is the signed number with no word. A word that agrees with
the sign is redundant: `-6.3 down` and `6.3 down` are both `-6.3`. A word that
contradicts it, such as `-6.3 up`, is refused rather than settled either way.
Every other directional metric is still a magnitude and a word, and a
negative number beside a word is refused.

The view is the cited cell's camera and the phase is what `frames` says that
cell shows, so neither is written per checkpoint. Older replies are still
read:

- vx10 wrote the view and the phase per checkpoint, plus a sentence.
- vx11 used named objects.
- vx12 used numeric phase, cell and direction codes.

A phase or view an older reply states is still checked.

## What a number means

Every checkpoint score is a distance from the range tour players functionally
occupy for that checkpoint, not a distance from one model swing:

| Score | Meaning |
| ---: | --- |
| 90–100 | exemplary, tour quality |
| 75–89 | strong |
| 60–74 | functional but improvable |
| 40–59 | a meaningful problem |
| 20–39 | a major problem |
| 0–19 | a severe deviation |
| null | not assessable |

**Null is never zero and never good.** A checkpoint the pictures do not plainly
show is absent: it leaves every average it would have been part of and lowers
coverage instead. Tour players differ: a flatter or more upright backswing, a
stronger or weaker turn, are all used on tour, and the prompt tells the model
to score against the functional range rather than mark a difference down.

## The checkpoint catalogue

P1 to P9, 64 checkpoints, 59 of them scoreable on this bay. Each declares the
phases it may be read from, the views that can show it, whether the bay can
assess it at all, whether a tour-quality 90–100 is distinguishable, its
minimum confidence, its weight within its phase, which directions a miss can
take, and which faults it triggers or supports.

| Phase | Weight | Scoreable checkpoints | Tour-capable | Not scoreable |
| --- | ---: | ---: | --- | --- |
| P1 setup | 8 | 7 | hip hinge, knee flex, spine, arm hang | foot orientation, body alignment |
| P2 takeaway | 9 | 6 | — | clubface |
| P3 backswing (lead arm parallel) | 10 | 8 | body turn | — |
| P4 top | 11 | 8 | shoulder turn, centre | clubface |
| P5 transition | 18 | 7 | — | — |
| P6 delivery | 17 | 7 | — | — |
| P7 impact | 13 | 7 | pelvis rotation | shaft lean |
| P8 release | 9 | 6 | body rotation | — |
| P9 finish | 5 | 3 | balance, rotation | — |

**Down-the-line and face-on are not interchangeable.** The trail shoulder
moving out, hand depth and hip depth are down-the-line checkpoints. Sway,
centre and hand position at impact are face-on. A score cited from the wrong
camera is refused.

**Tour quality needs evidence.** A checkpoint marked not tour-capable is
clamped to 89 (`NON_TOUR_CEILING`) when a reply scores it higher, and the clamp
is recorded. Down the line, the camera is an oblique rear quarter with a
picture-in-picture and ProTee's plane line across it, so hand depth at 96 is a
claim the picture cannot hold.

**What the cameras cannot see is not asked for.** Clubface at the takeaway and
at the top, and shaft lean at impact, are in the table with `scoreable=False`.
They are always null, and they are where a better camera would start.

### Steep, shallow, steep

The club's own motion is five down-the-line checkpoints, each with a still
aimed at its moment:

- `backswing_shaft_pitch`
- `top_shaft_position`
- `transition_shallowing`
- `delivery_shaft_plane`
- `release_club_exit`

The down-the-line source shows the whole golfer and the whole club through the
swing, so all five can be assessed. Each still needs the whole club plainly in
that picture. A club hidden behind the head at the top, blurred through the
ball, or a picture of the wrong moment, is null, and then nothing is claimed
about the plane. From the top to impact is three stills at 10 a second, so the
delivery still is whichever one lands nearest P6, and the shaft-plane
checkpoint is null when the model places that still at the transition instead.
`coach_club_motion` logs them as `score:direction` or `null`.

## Provenance: what a checkpoint score has to prove

A score is accepted only if all of the following hold. Each failed rule is a
reason to have no score, and is logged as one:

- the key is a catalogue checkpoint (`unknown`) and the bay can assess it
  (`not_scoreable`);
- the score is a number from 0 to 100 (`score`);
- the confidence is between 0 and 1 (`confidence`), quantised to 0.05 and
  capped at 0.90, and at least the checkpoint's own minimum
  (`low_confidence`);
- it cites a frame sent in this request (`frame`) that is not the All Values
  grid (`metrics_frame`);
- the frame came from a body camera (`view_unknown`), any view the entry
  states is that camera (`view_mismatch`), and the checkpoint may be read
  from it (`view`);
- the model placed that picture (`phase_unknown`), any phase the entry
  states agrees with that placement (`phase_conflict`), and the placement is
  one of the checkpoint's moments (`phase`);
- the direction, if any, is one the checkpoint has (`deviation`).

The model identifies every picture before scoring anything because CaddieJ
aims its cameras with a stopwatch, and was wrong twice in one shot. The
model's identification is authoritative, and it is the only source of a
checkpoint's phase: a picture it calls `unclear` supports no checkpoint.

## Phase scores

A phase is scored when at least two of its checkpoints were assessed and they
carry at least 40 % of the phase's scoreable weight:

```
mean       = Σ(weight × score) / Σ(weight)           over assessed checkpoints
value      = round(0.65 × mean + 0.35 × worst)
coverage   = assessed weight / scoreable weight
confidence = weighted mean confidence × min(1, coverage / 0.75)
```

Pulling the score towards the worst checkpoint stops one major problem
averaging away among good checkpoints. Three equally weighted transition
checkpoints of 85, 85 and 30 make a transition of 54, not 67. Missing
checkpoints leave the denominator; they are not zeros. A phase is `full` at
75 % coverage, `limited` between 40 and 75 %, and `insufficient` below that,
with no score.

## Swing Score V4

```
raw   = Σ(phase weight × phase value) / Σ(phase weight)   over scored phases
score = min(raw, ceilings)
```

| Phase weight | | Phase weight | |
| --- | ---: | --- | ---: |
| setup | 8 | delivery | 17 |
| takeaway | 9 | impact | 13 |
| backswing | 10 | release | 9 |
| top | 11 | finish | 5 |
| transition | 18 | | |

Transition, delivery and impact carry 48 of the 100 points because that is
where a golf swing is decided. The finish carries 5 because a finish is a
consequence. Phase weights are declared, not summed from checkpoints, so the
setup's seven easy-to-see checkpoints cannot dominate by count.

The binding ceiling is recorded with the score:

| Ceiling | Value | Why |
| --- | --- | --- |
| worst core phase | its value + 25 | a bad transition cannot be averaged away by a good setup and finish |
| worst core checkpoint below 40 | its score + 40 | a major deviation materially lowers the number |
| limited coverage (< 70 %) | 85 | a partly seen swing cannot claim the top of the scale |

The core phases are backswing through release. Setup and finish problems cost
what their weight says and do not cap.

**Coverage** is the share of phase weight that was scored. Below 45 %, or with
none of transition, delivery and impact scored, there is **no Swing Score**,
which is not a low one.

**ProTee delivery is separate.** `delivery_value` is still the five measured
ramps (face, face to path, path past its window, strike, efficiency), stored
beside the Swing Score and not blended into it. An excellent strike cannot
carry a swing with a transition fault, and a poor strike does not mark down a
sound swing.

**Limit, stated plainly.** Only eleven checkpoints are tour-capable, so on
this bay a swing scored 97 everywhere comes out at about 90. That is a fact
about the cameras, and it is held by a test so that better evidence shows up.

## Faults

25 faults, each with:

- triggering checkpoints, and the direction each must miss in;
- supporting checkpoints;
- a minimum number of triggers;
- a minimum confidence;
- an importance;
- the upstream faults that commonly cause it;
- the ProTee patterns that can corroborate it;
- one reviewed cue: a headline, one physical action and one spoken line, in
  lead/trail words.

The model never names a fault.

**Inference.** A trigger fires when its checkpoint scored below 60, in the
fault's direction, with confidence at least the larger of the fault's and the
checkpoint's minimum. A low score with no direction is read as the
checkpoint's only direction when it has exactly one; otherwise a directional
fault cannot be inferred from it. A 60–74 checkpoint is something to improve,
not a fault.

**Severity** is `(80 − worst trigger) / 60`, clamped to 0–1. Each weak
supporting checkpoint (69 or below) adds 0.05, up to 0.15. A fault seen with
less than 0.60 confidence is capped at 0.5.

`OVER_THE_TOP` needs two of its four places to agree: the trail shoulder out,
the hands out, the delivery hands outside, or the shaft steep. Any one of them
alone is a single oblique still.

## Root cause

```
priority  = severity × confidence × importance
symptom   : an inferred, coachable ancestor (through any chain of causes)
            has priority ≥ 0.5 × this fault's
effective = priority + 0.5 × Σ priority of the faults it explains   (roots only)
coachable : severity ≥ 0.25 and confidence ≥ 0.55
winner    : highest effective coachable root; ties to earliest phase, then
            catalogue order
sticky    : the session lesson stays while it is a coachable root with
            effective ≥ 0.67 × the winner's
```

A credible earlier cause wins: trail shoulder out, a steep transition and an
over-the-top delivery all seen, and the player is coached on the shoulder. A
tiny early imperfection does not: a shoulder turn of 58 at 0.6 confidence has
less than half the priority of a trail shoulder at 20, so it explains nothing
and the shoulder stays the root. None is a real answer: a sound swing has no
mechanical correction in it.

## ProTee's role

ProTee does four things, and only these:

1. **Corroboration.** A fault already inferred from the pictures gains 0.05
   confidence for each matching delivery pattern, up to 0.10 and never above
   0.95. ProTee cannot create a fault or change a fault's severity.
2. **Delivery quality**, as `delivery_value`.
3. **The lesson when no credible mechanical cause was seen.** The measured
   coaching line from the reply, checked against the numbers as before.
4. **Patterns, read player-relative against the club's functional window.**
   A path is out-to-in or in-to-out only outside the window, so +3.7° on an
   iron is healthy and corroborates nothing. A club without a window has no
   path pattern. Face-to-path of ±4° and attack ±4° from the club's ideal
   are the other patterns. The player's own baseline plays no part.

## The session lesson

The lesson's score is the mean of its fault's triggering checkpoints on this
shot. It is compared with the previous shot it was assessed on, and with the
shot it was adopted on:

| Verdict | Rule |
| --- | --- |
| `worse` | down 12 or more |
| `better` | up 12 on the previous shot, or 15 on the baseline until first solved |
| `much_better` | better, and solved: at least 75 with the fault no longer inferred |
| `held` | solved, without a further jump |
| `same` | anything else: 61 to 64 is the same swing |
| `unassessed` | none of its checkpoints seen; nothing changes, nothing counts |

What happens next:

- **Retired** after two solved shots in a row. The next root, if there is
  one, becomes the lesson.
- **Advance** on `much_better` or `held` while a different root exists.
- **Reinforce** when improved and nothing else outranks it. The same cue is
  said as a reinforcement under the progress headline.
- **New lesson** when a different root outranks it and it did not improve.
- **Continue** otherwise.

An unassessed lesson is not said as if it had been seen; that shot is
coached on whatever was seen.

Headlines come from a fixed set: `MUCH BETTER <PHASE>`, `BETTER <PHASE>` and
`<PHASE> HELD`. The phase is written the way the floor writes it, so P6 is
`DELIVERY`.

## The words belong to the winning cue

Once CaddieJ has chosen the lesson, that cue owns the display, the observation
and the spoken line, all reviewed together in the registry. The model's
sentence does not survive. Every fault cue starts with an action verb, does not
repeat its own observation, fits 18 characters, and says lead and trail rather
than left and right. Tests check each of these.

## Worked example

The shot that started this work: a driver, right-handed. ProTee read face
0.7° open, path 1.7° out-to-in, face to path 2.4° open, strike 8.75 mm toe,
smash 1.49. Every reading was inside its band; v1 scored it 94 and said GOOD
STRIKE.

**Frames to checkpoints.** The checkpoints are scored from the swing pictures
only. The trail shoulder scored 35, direction out, at 0.75 confidence, from a
down-the-line picture the model identified as the transition. Hand direction
scored 40 out, and the delivery hand path 45 outside. Most other checkpoints
were 85. The club was blurred coming down, so the shallowing and shaft plane
checkpoints are null. The model identified the still aimed at the release as
the finish, so every release checkpoint is null rather than read from the
wrong moment.

**Phases.**

| Phase | Value | Coverage | Phase | Value | Coverage |
| --- | ---: | ---: | --- | ---: | ---: |
| setup | 85 | 1.00 | delivery | 65 | 0.88 |
| takeaway | 85 | 1.00 | impact | 85 | 1.00 |
| backswing | 85 | 1.00 | release | — | 0.00 |
| top | 85 | 0.94 | finish | 85 | 1.00 |
| transition | 57 | 0.88 | | | |

**Score.** The raw weighted mean is 76. The trail shoulder at 35 caps the score
at 75, so the Swing Score is **75**, with 91 % coverage (`full`). Delivery is a
separate 94.

**Faults.**

| Fault | Severity | Confidence | Priority |
| --- | ---: | ---: | ---: |
| `TRAIL_SHOULDER_OUT` | 0.75 | 0.75 | 0.62 |
| `OVER_THE_TOP` | 0.75 | 0.72 | 0.54 |
| `STEEP_TRANSITION` | 0.72 | 0.70 | 0.50 |

**Root.** The trail shoulder is upstream of both other faults and has more than
half their priority. It explains them, its effective priority is 1.14, and
**the lesson is DIP TRAIL SHOULDER**.

**ProTee.** A 1.7° out-to-in driver path is inside the −2 to +6 window, so it
is no pattern and corroborates nothing.

**Floor.** `75`, the phases above, the observation `UPPER BODY OUT` and the cue
`DIP TRAIL SHOULDER`.

**The next shot.** The trail shoulder scored 78, the hands 74 and 72, and no
fault was inferred. The Swing Score is **83**. The lesson moved from 35 to 78:
up 43, at least 75, and the fault is gone, so the verdict is `much_better`.
Nothing else outranks it, so the lesson is reinforced, and the floor reads
**TRANSITION 78 +43 MUCH BETTER TRANSITION** beside DIP TRAIL SHOULDER.

**The shot after.** The trail shoulder scored 80, which is solved for a second
time, so the verdict is `held` and the lesson is retired (`TRANSITION HELD`).
The hips moved in towards the ball, scoring 42 at delivery and 48 at impact,
so `EARLY_EXTENSION` is the new root and **KEEP HIPS BACK** is the next lesson.

## Where it goes

- **The floor** (data contract only; no layout change). `CoachingResult`
  carries `swing_score` and `phases` (label and value per scored phase). It
  also carries `progress`: label, value, delta, headline, verdict and mode.
  The same fields go into `FloorAdvice.phases` / `FloorAdvice.lesson` and the
  HUD `AdvicePayload`.
- **The logs**, each bounded:
  - `coach_checkpoints_assessed`: scored count, `id=score:direction` pairs,
    rejections with reasons, and clamps.
  - `coach_phase_scores`: `phase=value:coverage:confidence`, club motion,
    coverage, confidence and ceiling.
  - `coach_faults_inferred`: up to eight faults, with ProTee patterns.
  - `coach_root_cause_selected`: the root, the reason and the ranking.
  - `coach_lesson_progress`: fault, cue, verdict, mode, score, baseline,
    delta and next.

  These logs contain IDs and numbers only, never prose.
- **History.** `coach_results.swing_score_json` holds the delivery components
  as before. It also holds a V4 `mechanics` block (every checkpoint, every
  phase, coverage, confidence, ceiling and clamps) and a `review` block
  (faults, ranking, root, lesson progress, deltas, ProTee patterns, lesson
  source and the final cue). This needed no schema migration.

## Versioning

`swing-score-v1` to `-v4` are different rubrics, and history holds all four.
Rows are never rescored. A v3 row's mechanics are kept as their original JSON
(`legacy_mechanics_json`) rather than translated into checkpoints that were
never scored. Changing any threshold or weight here is a new rubric version.
