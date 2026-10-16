# Temporal POC 2: LOCATE as the single source of truth

One paid call: gpt-5-mini, `reasoning_effort=minimal`, no retry. It used the same shot
(`20261008T092949-4-d05a29c4`) and the same 18+18 sheet (`output/sheets/…_T36.jpg`,
unchanged) as POC 1. Only the protocol changed. No production code was touched.

## Decision: C. MIXED

The protocol fix did what it was meant to do. Provenance is now structural: there were no
`phase_unknown`, `phase`, `view` or `frame` rejections (POC 1 had 31), and a Swing Score now
exists. The run was also cheaper and faster.

Localisation, however, got worse, not better. With checkpoints no longer able to cite their own
cell, every LOCATE error becomes a checkpoint read from the wrong moment. CaddieJ cannot detect
that error, and it is now silent: **15 of the 27 accepted checkpoints were scored from a still
that is not their position.** The Swing Score of 76 is produced, but on mislocated evidence.

What remains unresolved is listed at the end. This is not A: DTL P1–P6 came back 5/6 correct, as
in POC 1. It is not B either, because P7–P9 and the whole FO top-to-impact stretch are still
unreliable at minimal effort, and in a different way from POC 1.

## What changed in the protocol (`poc2_vx14b.py`)

- **`locate`:** 9 positional entries (P1..P9). Each is `null` or `[DTL cell|null, FO cell|null,
  confidence 0..100]`.
- **Checkpoint entry:** `[score, confidence, direction|null]`, with no cell.
- **Provenance is derived by CaddieJ.** `_dtl` lists use locate[phase].DTL, `_fo` lists use .FO,
  and `_any` uses DTL, falling back to FO. An unlocated position is refused as `not_located`.
  - The entries are then converted to the vx13 named form and run through the unchanged
    `canonical_reply` and `parse_checkpoints`.
- **JOB 1b holds concise visual P1–P9 definitions** in the repository's semantics: P5 =
  transition, P6 = downswing/"delivery", P8 = release.
  - P3 is "NOT the top".
  - P7 is "prefer the last cell with the ball still in its place; never the first cell after the
    ball only because contact fell between two stills".
  - P8 is "not once the club points up".
  - P9 is "balanced finish".
- **Validator, same as POC 1:** cell camera and existence, chronology per camera (a violation
  drops that whole camera), and at most two adjacent positions sharing a cell.
- **Neutrality check passed.** The model saw no P-labels, calibrated aims or truth.
- **Request:** about 12.6k estimated tokens (system 9,551 chars, evidence 15,805 chars, schema
  5,060 chars, image about 4,916 tokens).

## Localisation against the full-resolution originals

Grading uses the same truth sets and rule as POC 1:
- **correct:** the original is in the truth set;
- **acceptable:** a sheet neighbour of a correct cell;
- **wrong:** anything else.

The originals for the chosen cells were checked visually (`output/poc/originals_swing_side_27-51.jpg`
and `originals_swing_behind_28-51.jpg`).

**The model's DTL answer was out of order** (P8 = DTL_18, then P9 = DTL_15). The validator
therefore dropped the whole DTL camera, and every DTL position is effectively null. The table
also shows the raw DTL answer, graded as if it had been kept (`poc2_raw_grade.py`).

| Pos | DTL raw cell (orig) | Actually shows | Verdict | FO cell (orig) | Actually shows | Verdict | Conf |
|---|---|---|---|---|---|---|---|
| P1 | DTL_01 (#15) | address | correct (dropped) | null | — | missed | 90 |
| P2 | DTL_03 (#21) | takeaway | correct (dropped) | FO_02 (#17) | just moving away | acceptable | 80 |
| P3 | DTL_06 (#28) | top | **wrong** (dropped) | FO_04 (#22) | lead arm ~parallel | **correct** | 70 |
| P4 | DTL_07 (#30) | top | correct (dropped) | FO_06 (#27) | late backswing, nearly top | **wrong** | 85 |
| P5 | DTL_11 (#39) | lead arm ~parallel down | correct (dropped) | FO_07 (#29) | the top | **wrong** | 75 |
| P6 | DTL_12 (#40) | shaft ~parallel down | correct (dropped) | FO_10 (#38) | lead arm parallel down (P5) | acceptable | 80 |
| P7 | DTL_17 (#47) | high follow-through | **wrong** (dropped) | FO_11 (#39) | shaft parallel, before impact (P6) | **wrong** | 90 |
| P8 | DTL_18 (#51) | finish | **wrong** (dropped) | FO_14 (#42) | release | **correct** | 80 |
| P9 | DTL_15 (#43) | release | **wrong** (dropped) | FO_16 (#44) | club past vertical | **wrong** | 70 |

**Totals**

| | POC 1 | POC 2 as used | POC 2 raw |
|---|---|---|---|
| Correct | 10 | 2 | 7 |
| Acceptable | 7 | 2 | 2 |
| Wrong | 1 | 4 | 8 |
| Missed | 0 | 10 (9 of them from the DTL drop) | 1 |

**P3–P8 compared with POC 1:**
- **DTL:**
  - P3 is still the top (#28), the same error as POC 1.
  - P5 improved from acceptable to correct.
  - P4 and P6 are unchanged and correct.
  - P7 and P8 are much worse: #47 and #51, against #42 and #44 in POC 1.
- **FO:**
  - P3 improved from acceptable to correct.
  - P8 improved from late to correct.
  - P4, P5, P6 and P7 are each **one position early**. The "top" is the late backswing, the
    "transition" is the top, the "delivery" is P5, and the "impact" is the delivery.

**P7, explicitly: the model chose neither DTL_13 nor FO_13.** It also no longer chose the
immediately post-impact cells (POC 1's DTL_14 / FO_14).
- On FO, the P7 rule over-corrected: FO_11 (#39) is shaft parallel and *before* impact, two
  stills early. FO_13 (#41) shows contact.
- On DTL, it picked DTL_17 (#47), a high follow-through frame, five stills late.
- A likely cause, not proven: the DTL originals from #43 onwards show a white ball-like object
  still lying on the mat. "The last cell with the ball still in its place" then points to the
  last DTL cells. That pushed P8 to DTL_18 and left P9 with nothing later, which broke
  chronology.

**LOCATE confidence did not discriminate.** It was 70–90 everywhere. The highest values (90)
were on DTL P1, which was right, and on both P7 answers, which were wrong. Nothing was nulled
except FO P1.

## Downstream: POC 2 against POC 1

| | POC 1 | POC 2 |
|---|---|---|
| Input / output / reasoning tokens | 13,093 / 1,183 / 0 | **12,788 / 950 / 0** |
| OpenAI wall time | 13.88 s | **7.93 s** |
| Strike to verdict (capture 4.53 + render 0.55 + call + parse) | ~19.0 s | **~13.0 s** (parse 0.007 s) |
| Cost | $0.0056 | **$0.0051** |
| Checkpoint entries returned | 53 | 53 (of 59 scoreable) |
| Accepted | 22 | **27** |
| Rejected | 31 (27 `phase_unknown`, 4 `phase`) | 26, all `not_located` (the dropped DTL camera) |
| Accepted checkpoints read from the wrong position | (cells cited freely) | **15 of 27** (top, transition, impact, finish) |
| Mechanics coverage | too thin | 0.57 (limited), confidence 0.38 |
| Swing Score | none | **76** (mechanics 76, no ceiling; ProTee delivery 91) |
| Metrics | 22 read, 7 nulled | 24 read; rejected spin_axis, impact_width, side_spin, off_line, curve |
| Faults / root cause | — | none / none |
| Cue | model TOO_STEEP, reconciled to FACE_CONTROL_GOOD | model PATH_IN_TO_OUT (correction), reconciled to FACE_CONTROL_GOOD (CaddieJ's reinforcement); not spoken |

The vx13 reference rows from other shots had 46–49 assessed checkpoints and Swing Scores of 74–77.

**Phase scores** (value / coverage / assessed of scoreable). All of them come from FO cells only.

| Phase | Score | Coverage | Assessed | Picture actually shows |
|---|---|---|---|---|
| setup | — | 0.00 | 0/7 | — |
| takeaway | 76 | 0.50 | 3/6 | near takeaway |
| backswing | 73 | 0.41 | 3/8 | P3, correct |
| top | 79 | 0.65 | 5/8 | late backswing |
| transition | — | 0.29 | 2/7 | the top |
| delivery | — | 0.25 | 2/7 | P5 |
| impact | 77 | 0.61 | 5/7 | P6, before impact |
| release | 73 | 0.82 | 4/6 | P8, correct |
| finish | 77 | 1.00 | 3/3 | past vertical, not the finish |

The checkpoint scores were 70–85 and the confidences 0.6–0.8 throughout, so the number carries
little information about this swing.

## What remains unresolved

1. **Localising P7–P9 at minimal effort.** Two calls on the same sheet gave two different
   failure patterns: POC 1 was one still late, POC 2 was two early on FO and five late on DTL.
   Rewording a definition moves the error rather than removing it, so this cannot be settled by
   more prompt text.
2. **Run-to-run stability is unknown.** n = 1 per protocol, so model variance and wording
   effects cannot be separated. The two POCs differ by the protocol *and* by a sample.
3. **The single-source design amplifies errors.** One wrong LOCATE entry silently mislabels a
   whole phase's checkpoints, and CaddieJ has no independent check on locate. This design needs
   such a check before it is trusted, for example a soft timing plausibility on P7 or a
   cross-camera agreement test. That is a design question, not tested here.
4. **The validator is too harsh.** Dropping the whole camera on one order violation discarded
   five correct DTL positions and 26 checkpoints. A tail-only drop would have kept them.
5. **LOCATE confidence is uncalibrated.** It cannot be used to null or gate positions as it
   stands.

## Files (all under `research/temporal_evidence/`)

- `poc2_vx14b.py`: request build (dry), the one `--send`, and `--analyse`. Imports `poc_vx14.py`
  unchanged.
- `poc2_raw_grade.py`: grades the raw locate before validation (no call).
- `output/poc2/`: `reply.json` (the call record and payload), `analysis.json`,
  `request_system.txt`, `request_evidence.txt`, `request_schema.json`.
- `output/poc/originals_swing_side_27-51.jpg` and `originals_swing_behind_28-51.jpg`: the
  originals behind the chosen cells.
