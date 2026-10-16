# POC 4 — vx14c group-owned evidence (final same-shot temporal POC)

**Decision: B. VX14C NEEDS DESIGN CHANGES.**

4 of 6 pre-registered criteria pass, 2 fail:

- **Group cells.** 2 wrong by the pre-registered rule, against a limit of 1. A strict visual review finds 4 wrong.
- **Checkpoints accepted from a wrong position.** 11, against a limit of 3. A strict review finds 13.

The wire and ownership mechanics worked as designed:

- no shape rejections;
- 53 of 53 checkpoints accepted;
- coverage 1.0;
- 901 output tokens;
- P6–P9 correct on both cameras;
- P7 on the exact contact still on both cameras.

What failed is localising the backswing (P3) and transition (P5) on DTL. Both groups were put on top-of-swing stills, and the local validator cannot detect that, because the order and cross-camera timing are still plausible.

## Run

| | |
|---|---|
| Shot / sheet | `20261008T092949-4-d05a29c4`, neutral 18+18 T36 sheet (2048², 1,129,497 B, unchanged) |
| Model | gpt-5-mini, `reasoning_effort=minimal`, `max_output_tokens` 10000, timeout 30 s |
| Paid calls | exactly 1 (no retries) |
| Status | `completed` |
| Script | `poc4_vx14c.py` (`--send` once, then `--analyse` offline) |
| Criteria | `output/poc4/criteria.json`, written by the dry build before the call |

## Per-group review (full-resolution originals)

The "Rule" column uses the pre-registered POC 1 `TRUTH`/`RULE`. The "Strict" column comes from the full-resolution review: a temporally neighbouring still that shows a *different* biomechanical position counts as wrong.

| Phase | Pos | Cam | Cell | Orig # | Actual visual position | Rule | Strict |
|---|---|---|---|---|---|---|---|
| setup | P1 | DTL | DTL_01 | 15 | address | correct | correct |
| takeaway | P2 | DTL | DTL_03 | 21 | shaft parallel to ground, going back | correct | correct |
| backswing | P3 | DTL | DTL_06 | 28 | **top of swing** (hands high, club behind head) | **wrong** | **wrong** |
| top | P4 | DTL | DTL_07 | 30 | top | correct | correct |
| transition | P5 | DTL | DTL_08 | 34 | **still at top** (hands not yet lowering) | **wrong** | **wrong** |
| downswing | P6 | DTL | DTL_11 | 39 | delivery; lead arm below horizontal, shaft still ~45° up (DTL_12 #40 is closer to shaft-parallel) | correct | acceptable |
| impact | P7 | DTL | DTL_13 | 41 | contact (shaft and lead arm in one line) | correct | correct |
| release | P8 | DTL | DTL_15 | 43 | release, shaft parallel past impact | correct | correct |
| finish | P9 | DTL | DTL_18 | 51 | finish | correct | correct |
| setup | P1 | FO | FO_01 | 15 | address | correct | correct |
| takeaway | P2 | FO | FO_03 | 19 | shaft parallel going back | correct | correct |
| backswing | P3 | FO | FO_06 | 27 | late backswing near top, lead arm well above horizontal (FO_05 #24 was available) | acceptable | **wrong** |
| top | P4 | FO | FO_07 | 29 | top | acceptable | correct |
| transition | P5 | FO | FO_08 | 32 | **top** (FO_10 #38 shows P5) | acceptable | **wrong** |
| downswing | P6 | FO | FO_11 | 39 | shaft parallel, hands at trail hip | correct | correct |
| impact | P7 | FO | FO_13 | 41 | contact | correct | correct |
| release | P8 | FO | FO_15 | 43 | shaft parallel toward target | correct | correct |
| finish | P9 | FO | FO_18 | 51 | finish | correct | correct |

Group counts:

- **Rule:** 13 correct, 3 acceptable, 2 wrong.
- **Strict:** 13 correct, 1 acceptable, 4 wrong.

All 18 groups were returned with a non-zero cell, so none were unavailable, and none were rejected by the validator.

### Observations

- **The model returned the same cell number on both cameras for every phase (9 of 9).**
  - In POC 1 and POC 3 the LOCATE pairs were also identical; only POC 2 differed.
  - From cell 09 onwards both cameras sample the same captures, so number-pairing is harmless there.
  - In cells 02–08 the cameras sample *different* captures (DTL #18/21/24/26/28/30/34, FO #17/19/22/24/27/29/32). Here a shared number is not a shared moment.
  - DTL P3 at #28 against FO P3 at #27 is consistent with picking the number on one camera and copying it to the other.
- **DTL P3 has been DTL_06 (#28, top) in all four POCs,** whatever the protocol: checkpoint-owned, LOCATE-owned, hybrid and group-owned. This is protocol-invariant, so it is not something a further prompt or wire change on this shot is likely to fix.
- **P5 on both cameras was placed at the last sparse cell (08), which still shows the top.**
  - The relational wording ("after the top, hands lowering, lead arm near horizontal") was not honoured.
  - The correct P5 stills (DTL_10/FO_10, #38) sit in the dense block.
- **The validator kept every group, which is what its specification says it should do:**
  - P3 #28 < P4 #30 < P5 #34 is strictly ordered.
  - The DTL/FO P3 gap is 0.11 s, inside τ = 0.30 s.
  - The P5 gap is 0.12 s, inside τ = 0.15 s.
  - P7 is inside the R7 prior.

  Local chronology and cross-camera agreement cannot detect "a top still labelled as P3/P5" when the error is consistent across both cameras.

## Metrics

| | |
|---|---|
| Groups returned / kept / rejected | 18 / 18 / 0 (0 wire/schema rejections) |
| Checkpoints returned / accepted / rejected | 53 / 53 / 0 (59 scoreable; 6 null scores by the model) |
| Rejection reasons | none |
| Accepted from a biomechanically wrong position | 11 by rule; 13 strict (adds `backswing_centre`@FO_06 and `transition_pressure_shift`@FO_08) |
| Coverage | 1.00 (full) |
| Swing Score (mechanics) | 73 |
| Faults / root cause | none / none |
| Model cue | correction `PATH_IN_TO_OUT`, from `ALL_VALUES` (launch data, not frames) |
| Final cue after reconciliation | CaddieJ's lesson `FACE_CONTROL_GOOD` |
| Tokens in / out / reasoning | 13,333 / 901 / 0 |
| OpenAI wall time | 10.25 s |
| Validation | 0.001 s |
| Strike-to-verdict | ~15.3 s (capture 4.53 + render 0.55 + call 10.25) |
| Cost | $0.0051 |

The 11 rule-wrong accepts are every checkpoint on the two wrong DTL groups:

- **backswing@DTL_06:** `hand_depth`, `hand_height`, `hip_depth`, `posture`, `arm_structure`, `body_turn`.
- **transition@DTL_08:** `arms_lowering`, `hand_direction`, `trail_shoulder`, `posture_space`, `pelvis_initiation`.

As a result, the backswing score (74) and transition score (68) are mostly read from top-of-swing images.

Phase scores (score, confidence, scored/total):

| setup | takeaway | backswing | top | transition | downswing | impact | release | finish |
|---|---|---|---|---|---|---|---|---|
| 83, 1.00, 7/7 | 72, 0.80, 5/6 | 74, 0.88, 7/8 | 77, 0.94, 7/8 | 68, 0.88, 6/7 | 68, 0.88, 6/7 | 72, 1.00, 7/7 | 73, 0.91, 5/6 | 77, 1.00, 3/3 |

## Pre-registered criteria (unchanged)

| Criterion | Result | Verdict |
|---|---|---|
| ≤ 1 wrong phase/view group cell | 2 (rule), 4 (strict) | **FAIL** |
| P7 within one still of contact, both cameras | DTL #41, FO #41 (exact) | PASS |
| ≥ 40 accepted checkpoints | 53 | PASS |
| ≤ 3 accepted from a wrong position | 11 (rule), 13 (strict) | **FAIL** |
| 0 groups rejected for wire/schema shape | 0 | PASS |
| Output ≤ ~1,000 tokens | 901 | PASS |

## POC 1–4 comparison (same shot, same sheet, same model and effort)

| | POC 1 checkpoint-owned | POC 2 LOCATE-owned | POC 3 LOCATE + EVIDENCE | POC 4 vx14c group-owned |
|---|---|---|---|---|
| Position cells by rule (correct / acceptable / wrong) | 10 / 7 / 1 | 7 / 2 / 8 (+1 missed) | 12 / 2 / 4 | 13 / 3 / 2 |
| P3–P5 correct (of 6) | 3 | 3 | 0 | 1 |
| P6–P9 correct (of 8) | 4 | 3 | 8 | 8 |
| P7 DTL / FO | #42 / #42 | #47 / #39 | #41 / #41 | #41 / #41 |
| Accepted / rejected checkpoints | 22 / 31 | 27 / 26 | 24 / 29 | 53 / 0 |
| Wire/schema rejections | — | — | 28 | 0 |
| Wrong-position accepts (contamination) | 2 | 15 | 9 | 11 |
| Coverage | 0.37 | 0.57 | 0.69 | 1.00 |
| Swing Score | none | 76 | 67 | 73 |
| Tokens in / out | 13,093 / 1,183 | 12,788 / 950 | 12,940 / 976 | 13,333 / 901 |
| OpenAI wall time | 13.9 s | 7.9 s | 10.7 s | 10.3 s |
| Strike-to-verdict | ~19.0 s | ~13.0 s | ~15.8 s | ~15.3 s |
| Cost | $0.0056 | $0.0051 | $0.0052 | $0.0051 |

What each POC showed:

- **POC 4 is the best on wire reliability, coverage, output size and the downswing-to-finish half** (P6–P9 8/8, P7 exact, as in POC 3).
- **It still has contamination: 11 accepts from a wrong position, compared with 2 in POC 1.**
  - POC 1 had less contamination only because it rejected 31 checkpoints, giving 0.37 coverage and no Swing Score.
  - In vx14c a wrong group cell contaminates every checkpoint that the group owns. One wrong DTL group costs 5–7 accepts.
- **Latency and cost are flat across POC 2–4** (about $0.005, 10 s call). Input grew by about 400 tokens because of the typed schema.

## Why B and not A or C

- **Not C.** Two pre-registered criteria fail even under the lenient rule. The contamination (11) is far above the limit (3).
- **Not A.** The parts vx14c set out to fix all work:
  - the strict typed wire, with 0 shape rejections where POC 3 had 28;
  - group ownership with no borrowing;
  - full coverage;
  - output size;
  - downswing, impact, release and finish localisation.

  The failure is concentrated in two DTL backswing/transition groups.

What needs to change (design only; not tested here):

1. **Sheet sampling in the sparse backswing segment.** Sample the *same* capture indices on both cameras, so a shared cell number means a shared moment. Consider denser sampling between takeaway and the start of the dense block. The model pairs numbers across cameras (9 of 9 here), and DTL P3 = DTL_06 in 4 of 4 protocols. Both point at the evidence layout rather than the wire.
2. **Contamination containment per group.** Because a whole group inherits its cell, P3/P5 groups need a check that does not rely on order alone. One example is to null P3 or P5 when it is within one sheet cell of the P4 cell on that camera, or when it is a sparse cell next to P4. Any such rule must be judged against fresh swings, not tuned on this shot.
3. **Re-test only on new material.** This shot has now been used for four protocols, and the remaining errors are stable on it. Further same-shot iterations risk overfitting. Any next run of a revised design should use multiple fresh real swings, then a different golfer with a meaningfully different tempo.

Do not take this into production on the strength of this saved swing.

## Files

All files are under `research/temporal_evidence/`.

- `poc4_vx14c.py`: the POC 4 protocol (prompt edits, typed schema, decode, R1, analysis).
- `output/poc4/criteria.json`: pre-registered criteria, written before the call.
- `output/poc4/request_system.txt`, `request_evidence.txt`, `request_schema.json`: the exact request texts.
- `output/poc4/reply.json`: raw reply, usage and wall time.
- `output/poc4/analysis.json`: groups, validation, checkpoint assessment, summary and criteria verdicts.
- `output/poc/originals_swing_behind_24-40_box.jpg`, `output/poc/originals_swing_side_22-39_box.jpg`: full-resolution review tiles.
- `POC4_REPORT.md`: this report.

No production code was modified.
