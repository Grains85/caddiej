# Temporal POC 3: the vx14 hybrid protocol

One paid call: gpt-5-mini, `reasoning_effort=minimal`, `max_output_tokens` 10000, timeout 30 s,
no retry. It used the same shot (`20261008T092949-4-d05a29c4`) and the same T36 sheet
(byte-identical, 1,129,497 B) as POC 1 and POC 2. Only the protocol changed. No production code
was touched. The pass criteria were written to `output/poc3/criteria.json` (11:54 UTC) before the
call was made.

## Decision: B. HYBRID NEEDS ANOTHER DESIGN ITERATION

| Pre-registered criterion | Result | |
|---|---|---|
| ≤ 1 wrong LOCATE cell | **4 wrong** (P3/P5 DTL, P4/P5 FO) | FAIL |
| P7 within one still of impact on both cameras | **#41 / #41, exact contact on both** | PASS |
| ≥ 40 accepted checkpoints | **24** | FAIL |
| ≤ 3 wrong-position accepted | **9** | FAIL |
| Output ≤ ~1,000 tokens | **976** | PASS |

**The central hypothesis failed.** EVIDENCE equalled LOCATE in **18 of 18** slots. In a compact
single-request protocol at minimal effort, the model does not form a second opinion; it copies.

**Two parts of the design were vindicated:**
- **The club-and-hands P7 semantics** gave exact contact on both cameras, after POC 1 was one still
  late and POC 2 was wildly off.
- **The local validation** nulled only the P3/P4 inversion on each camera and kept every other
  position. Under POC 2's rule, the same input would have dropped both cameras.

That is not enough for multi-shot testing (C), and it is not a failure of temporal evidence
itself (A).

## Full-resolution review of LOCATE

The originals are `output/poc/originals_swing_behind_21-37.jpg`, `originals_swing_side_19-37.jpg`
and the two `*_38-47_box.jpg` tiles. Grading follows POC 1's rule. "Kept" means the position
survived CaddieJ's validation.

| Pos | DTL cell (orig) | Actually shows | Verdict | Kept | FO cell (orig) | Actually shows | Verdict | Kept |
|---|---|---|---|---|---|---|---|---|
| P1 | DTL_01 (#15) | address | correct | yes | FO_01 (#15) | address | correct | yes |
| P2 | DTL_03 (#21) | shaft parallel going back | correct | yes | FO_03 (#19) | shaft parallel going back | correct | yes |
| P3 | DTL_06 (#28) | **the top** | **wrong** | no (order) | FO_06 (#27) | late backswing, nearly top | acceptable | no (order) |
| P4 | DTL_05 (#26) | nearly top, hands at the shoulder | acceptable | no (order) | FO_05 (#24) | lead arm just past horizontal (P3) | **wrong** | no (order) |
| P5 | DTL_07 (#30) | **the top** | **wrong** | yes | FO_07 (#29) | **the top** | **wrong** | yes |
| P6 | DTL_11 (#39) | hands at the waist, coming down | correct | yes | FO_11 (#39) | hands at the trail hip, shaft horizontal | correct | yes |
| P7 | DTL_13 (#41) | **contact** | correct | yes | FO_13 (#41) | **contact** | correct | yes |
| P8 | DTL_15 (#43) | release, arms extended | correct | yes | FO_15 (#43) | release, shaft toward the target | correct | yes |
| P9 | DTL_18 (#51) | finish | correct | yes | FO_18 (#51) | finish | correct | yes |

The raw totals are 12 correct, 2 acceptable, 4 wrong and none missed.
- **From the downswing to the finish (P6–P9) it is 8/8 correct, the best of the three POCs.** P7
  answers the question POC 2 left open: the model picked DTL_13 and FO_13, neither a
  post-impact nor a pre-impact cell.
- **The backswing-to-transition stretch (P3–P5) is the worst of the three POCs.** The model put
  P3 *after* P4 (an order violation on both cameras) and labelled the real top as P5. The true P5,
  lead arm horizontal coming down at #37–39 (cells 09–11), was skipped.
- **The cross-camera check could not catch P5.** Both cameras made the *same* error (DTL_07 at
  +1.43 s, FO_07 at +1.38 s), so they agree.

## Full-resolution review of EVIDENCE

| | Count |
|---|---|
| EVIDENCE entries (non-zero) | 18 |
| **Equal to LOCATE** | **18** |
| Different from LOCATE and valid | 0 |
| Different from LOCATE and invalid | 0 |

- **No entry was independently chosen.** Each evidence cell is exactly as appropriate as its
  LOCATE cell: 12 correct, 2 acceptable, 4 wrong.
- **What the validator accepted:**
  - Raw evidence passed the region check everywhere.
  - Once CaddieJ had nulled LOCATE P3/P4, the backswing and top evidence was accepted through
    the unanchored rule (a 3-cell gap between P2 and P5). That admitted DTL_06 (the top) as
    backswing evidence and FO_05 as top evidence, both wrong.
  - Transition evidence (DTL_07 / FO_07) was anchored on the wrongly kept P5, so it passed
    as well.

EVIDENCE therefore behaved purely as a mirror: it added 41 output tokens and no information.

## Downstream results

**Rejections** (29 in total):

| Reason | Count | Cause |
|---|---|---|
| `length` | 20 | All 9 `_any` lists refused. The model wrote the camera digit *inside* the first entry (`[1,80,80]`) and appended stray integers at the end of some lists (`…, 2]`). |
| `deviation` | 8 | The model wrote `0` instead of `null` for "no direction". |
| `shape` | 1 | `[null,null,null]` for `release_club_exit`. |

**Both encoding failures were mine.** My schema let any slot be an integer and let a camera digit
sit anywhere in an `_any` list. POC 2's entries used the same anyOf and were written correctly,
so the `_any` prefix is what confused the model.

**Results:**
- **Accepted:** 24, graded 12 correct, 3 acceptable, 9 wrong.
  - The 9 wrong are `backswing_*` ×3 at DTL_06, `top_centre` at FO_05, `transition_*` ×4 at
    DTL_07 and `transition_pressure_shift` at FO_07.
- **Coverage:** 0.69 (limited).
- **Mechanics / Swing Score:** 67.
- **Faults:** none. **Root cause:** none.
- **Cue:** the model proposed PATH_IN_TO_OUT, whose instruction contradicts its own cue, and
  CaddieJ reconciled it to FACE_CONTROL_GOOD.

**Phase scores** (score, coverage, assessed/scoreable):

| Phase | Score | Coverage | Assessed |
|---|---|---|---|
| setup | — | 0.17 | 1/7 |
| takeaway | — | 0.30 | 2/6 |
| backswing | 77 | 0.53 | 4/8 |
| top | 72 | 0.47 | 3/8 |
| transition | 62 | 0.71 | 5/7 |
| delivery | 64 | 0.62 | 4/7 |
| impact | 66 | 0.69 | 4/7 |
| release | — | 0.09 | 1/6 |
| finish | — | 0.00 | 0/3 |

**Diagnostic only, not used for the verdict (`poc3_salvage.py`).** I also decoded the reply
leniently: direction `0` read as null, and the `_any` camera read from inside the first entry.
That gives 38 accepted, 12 of them from a wrong position, coverage 0.86 and Swing Score 69. The
reply would **still fail** both checkpoint criteria. The encoding cost coverage; the copied
evidence and the P3–P5 errors decide the outcome.

**Cost and timing:**
- **Tokens:** 12,940 in, 976 out, 0 reasoning.
- **OpenAI wall time:** 10.73 s.
- **Validation:** 0.001 s.
- **Strike to verdict:** about 15.8 s (capture 4.53 + render 0.55 + call 10.73).
- **Cost:** $0.0052.

## Comparison of the three POCs

| | POC 1: checkpoint-owned | POC 2: LOCATE-owned | POC 3: hybrid |
|---|---|---|---|
| LOCATE raw (correct / acceptable / wrong / missed) | 10 / 7 / 1 / 0 | 7 / 2 / 8 / 1 | 12 / 2 / 4 / 0 |
| P3–P5 correct of 6 | 3 | 3 | 0 |
| P6–P9 correct of 8 | 4 | 3 | **8** |
| P7 DTL / FO (truth #41) | #42 / #42 | #47 / #39 | **#41 / #41** |
| Accepted / rejected, as validated in that POC | 22 / 31 | 27 / 26 | 24 / 29 |
| Wrong-position accepted | 2 | 15 | 9 |
| Same reply through the hybrid validator (offline) | 44 / 9, 2 wrong | 39 / 14, 9 wrong | 24 / 29, 9 wrong |
| Evidence differs from LOCATE | 17 of 24 lists | n/a (none asked for) | **0 of 18** |
| Coverage | 0.37 | 0.57 | 0.69 |
| Swing Score | none | 76 | 67 |
| Tokens in / out / reasoning | 13,093 / 1,183 / 0 | 12,788 / 950 / 0 | 12,940 / 976 / 0 |
| OpenAI wall time | 13.9 s | 7.9 s | 10.7 s |
| Strike to verdict | ~19.0 s | ~13.0 s | ~15.8 s |
| Cost | $0.0056 | $0.0051 | $0.0052 |

**Lessons across the three calls:**
1. **Precise visual semantics move the error rather than remove it** (P7 fixed, P3–P5 broken).
   With n = 1 per protocol, prompt changes cannot be separated from run-to-run variation.
2. **Independence came only from placement.** It appeared when the cell was written inside each
   checkpoint list, away from LOCATE (POC 1). A parallel grid next to LOCATE was copied verbatim.
3. **The validator parts generalise.** Local order degradation, the impact prior and the
   neighbour-bracket region behaved as designed on all three replies.
4. **Some errors no deterministic rule can catch:** errors that are consistent across both
   cameras and in order (P5 = top on both cameras).

## What the next design iteration must address

These are offline design items, not performed here.

- **Evidence placement.** Either put a compact integer cell back at the head of each phase/camera
  list, where POC 1 showed real disagreement with LOCATE, or drop the second-opinion idea and
  accept LOCATE ownership with the hybrid validator. The second option is POC 2 under hybrid
  rules: 9 wrong out of 39.
- **Wire typing.** Give direction its own enum, with an explicit `"none"` and no integer allowed
  in that slot. Give `_any` its camera as a separate typed field (for example a `<phase>_any_cam`
  integer key) rather than a positional prefix in a mixed array.
- **The unanchored rule (E2).** It admitted 4 wrong cells here. Unanchored evidence should be
  refused, or limited to one cell.
- **P3–P5 robustness.** The top is being mistaken for P3 or P5. A relational rule such as "P5
  shows the hands lower than at P4", checked against the same camera, may help. It is untested.

## Recommendation for what follows

This was the authorised final single-shot call. I recommend no multi-shot testing of this
protocol as it stands. Fix the four items above offline first, and check them against the three
saved replies with `hybrid_replay.py`.

Only then is a multi-swing test worthwhile, followed by a different golfer with a different
tempo. A single shot cannot establish golfer generalisation in any case.

## Files (all under `research/temporal_evidence/`)

- `poc3_hybrid.py`: build, neutrality check, the pre-registered criteria, the one `--send`, and
  `--analyse`.
- `poc3_salvage.py`: the diagnostic lenient decode (not used for the verdict).
- `output/poc3/`: `criteria.json` (written before the call), `reply.json`, `analysis.json`,
  `salvage.json`, `request_system.txt`, `request_evidence.txt`, `request_schema.json`.
- `hybrid_replay.py`: the hybrid validator, reused unchanged.
