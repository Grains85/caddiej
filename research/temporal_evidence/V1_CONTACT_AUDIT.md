# V1 contact audit (post hoc)

Recorded 2026-10-08, before the Temporal Evidence V2 freeze. Research note only.

The immutable V1 evaluation reported M3=10/10 using the original frozen
human labels. A post-hoc audit found shot
20261008T142305-23-3d49f492 was labelled contact_tick=44, while the clarified
V2 contact rule identifies tick 42. The V1 files/results remain unchanged.
Under the clarified rule, that V1 P7 selection at tick 44 would be two ticks
late in both cameras, making the equivalent M3 result 8/10.

## Rule applied

`v2_grade.CONTACT_RULE`: "contact_tick is the capture tick where the clubhead
is at the ball and the struck ball is still on/at the mat. A later frame where
the ball has already departed is post-impact and must not be labelled contact."

## Evidence

Canonical full-resolution originals, `fresh/shots/20261008T142305-23-3d49f492/diagnostic/`.
Crops: `output/v2/contact/dtl_40..44.jpg`, `fo_40..44.jpg`, `fo_low_41..43.jpg`.

| Tick | DTL (swing_behind) | FO (swing_side) |
|---|---|---|
| 40 | club high, both balls on the mat: pre-impact | hands out, club approaching: pre-impact |
| 41 | club blurred, approaching, not at the ball; both balls on the mat: pre-impact | hands low, club in front of the legs, descending: pre-impact |
| 42 | clubhead at the struck ball, both balls still on the mat: **contact** | hands at the bottom, club through the hitting zone: **contact** |
| 43 | struck ball gone from the mat, club past: post-impact | hands and club past the body: post-impact |
| 44 | ball gone, screen bulging where it landed: post-impact | follow-through: post-impact |

The struck ball is not visible on FO (framing and the replay scrubber cover
the mat). FO is judged from the club and body alone; DTL and FO share capture
ticks, and DTL shows the ball present at 42 and gone at 43.

## What did not change

- `fresh/runs/20261008T142305-23-3d49f492/grades.json` still reads
  `contact_tick` `{"dtl": 44, "fo": 44}`.
- `fresh/evaluation.json`, `fresh/ledger.jsonl`, `fresh/criteria.json`,
  `fresh/config.json` and every reply are untouched.
- The V1 result stands as frozen: M3 10/10 PASS, overall FAIL. The 8/10 figure
  is a post-hoc equivalent under the V2 rule and is not a V1 result.
