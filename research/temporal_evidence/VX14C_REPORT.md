# vx14c: group-owned evidence (offline design and replay)

No OpenAI call and no production change. Everything here is derived from the three saved replies,
the checkpoint catalogue, the unchanged 18+18 T36 sheet and the full-resolution originals.

Code: `vx14c_replay.py` (validator and replays), `vx14c_size.py` (output size),
`vx14c_catalogue.py` (catalogue audit). Results: `output/vx14c/replay.json`, `size.json` and
`vx14c_payload_example.json`.

## Recommendation: A. GROUP-OWNED EVIDENCE (variant A: no LOCATE)

One phase/camera group writes its evidence cell and its scores together; nothing else carries a
cell.

- **On POC 1, the only reply with independently chosen cells,** vx14c-A accepts 49 of 53
  checkpoints with coverage 1.0, compared with 22 accepted when POC 1 was first validated.
  - 4 of those accepts are from a wrong position, and all 4 come from two one-still errors (FO
    top, FO impact). These are exactly the errors the corrected P4 and P7 semantics target.
- **On POC 2 and POC 3, vx14c-A behaves like the hybrid validator,** because their cells are
  per phase and camera anyway. It also removes the gap-filling rule that admitted 4 wrong cells
  in POC 3.
- **LOCATE is dropped.** Variant B did measurably remove the 4 wrong accepts on POC 1. But that
  benefit needs LOCATE to be an independent second opinion, POC 3 showed a parallel grid gets
  copied, and B adds about 46 tokens and an anchoring risk. See section 2.

One vx14c paid POC is justified (section 12).

---

## 1. Data flow

```
saved capture -> 18+18 neutral T36 sheet (unchanged, one 2048² image) -> ONE call (gpt-5-mini, minimal)
reply.phases[<phase>] = { d: DTL cell, f: FO cell, a: camera of the _any checkpoints,
                          s: [score, confidence] per checkpoint, x: direction per checkpoint }
reply.metrics, reply.coach                     (unchanged vx13)
        |
CaddieJ, deterministic:
  G. validate the group cells per camera (prior, local order, cross-camera); a failing group is null
  P. each checkpoint's picture = its group's cell (_dtl -> d, _fo -> f, _any -> the camera in a)
  V. unchanged vx.parse_checkpoints with frame_phases = {group cell: phase}
  S. score_swing -> faults -> root cause -> lesson cue -> coach validation (unchanged service order)
```

Each group answers one combined visual question: *which cell best shows this position on this
camera, and what do this group's checkpoints score in it?* Location and mechanics are generated
together, in one place, as POC 1's per-list cells were.

## 2. LOCATE: remove it (variant A)

Replayed on POC 1, the only reply where a LOCATE and independently chosen cells coexist:

| POC 1 replay | A: no LOCATE | B: POC 1's LOCATE as a region check only |
|---|---|---|
| Accepted / rejected | **49 / 4** | 40 / 13 |
| Wrong-position accepts | 4 (`top_centre` at FO_06; `impact_fo` ×3 at FO_11) | **0** |
| Coverage | **1.00** (full) | 0.91 |
| Swing Score | 69 | 69 |
| Release phase | 70 (5/6) | none (0/6; all refused) |
| Extra output | 0 | about 46 tokens |

**B removes the 4 wrong accepts on POC 1.** It also refuses release evidence that was really the
contact frame (#41), which was a fair refusal. So B has a measurable safety benefit, but it holds
only on this one reply.

**I still prefer A:**
1. **B's benefit depends on LOCATE being an independent second opinion.** That was true in POC 1
   (17 of 24 lists disagreed) and false in POC 3 (18 of 18 copied). For POC 2 and POC 3, B is
   identical to A because their only cells *are* their LOCATE.
2. **Asking LOCATE first is the anchoring that produced POC 3's copying.** Including it risks
   turning group cells back into LOCATE copies.
3. **The 4 errors B caught are one-still errors in P4 and P7.** The relational P4 rule and the
   proven P7 rule (section 4) address them in the picture itself, at no token cost.
4. **It costs about 46 tokens on a budget that is already tight.**

If the vx14c paid POC still shows more than 3 wrong-position accepts from one-still errors,
revisit B with a pre-registered measurement of LOCATE-versus-group disagreement.

## 3. Compact response schema (strict)

```jsonc
"phases": {                         // required, additionalProperties: false
  "<phase>": {                      // setup, takeaway, backswing, top, transition, downswing, impact, release, finish
    "d": 0..18,                     // integer: DTL evidence cell (DTL_13 -> 13); 0 = no trustworthy DTL cell
    "f": 0..18,                     // integer: FO evidence cell; 0 = none
    "a": "d" | "f",                 // enum: camera the phase's _any checkpoints were judged on
    "s": [ null | [score 0..100, confidence 0..100], ...n ],   // one per checkpoint of the phase
    "x": [ "none" | "<direction>", ...n ]                     // one per checkpoint, same order as s
  }
},
"metrics": [...],                   // unchanged
"coach": {...}                      // unchanged
```

- **Order within a phase** is the catalogue order: the phase's `_dtl` checkpoints, then `_fo`, then
  `_any`. `n` is fixed per phase by `minItems`/`maxItems`: 7, 6, 8, 8, 7, 7, 7, 6 and 3.
- **The camera and cell come from the group, never from an entry.**

## 4. Visual semantics for P1–P9

These are relational, follow swing order, and use no timings. They follow the repository's
meanings: P5 = transition, P6 = downswing/"delivery", P8 = release. They were checked against
the full-resolution originals: P3 at DTL #24 / FO #22–24; P4 at DTL #28–36 / FO #30–35; P5 at
DTL #38–39 / FO #37–39; P6 at #39–40; P7 at #41; P8 at #42–43.

> **P1 setup:** address, club behind the ball, before the club moves.
> **P2 takeaway:** going back, shaft about parallel to the ground, hands about hip high.
> **P3 backswing:** before the top. Lead arm about horizontal going back; hands still rising,
> about chest high; shaft pointing up, not yet behind the head.
> **P4 top:** the hands at their highest and furthest back. The cell before shows them still
> rising; the cell after shows them level or lower. During a pause, any cell of the pause. Never a
> cell where the hands are still clearly rising.
> **P5 transition:** after the top. Lead arm about horizontal again coming down; hands clearly
> lower than at the top and moving toward the ball; shaft still above the hands.
> P3 and P5 can look alike in one picture: the order decides. P3 comes before the top, P5 after it.
> **P6 downswing (delivery):** after P5, before impact. Shaft about horizontal, hands near the
> trail hip, clubhead trailing the hands.
> **P7 impact (POC 3, unchanged):** the clubhead at the ball, lead arm and shaft one straight line
> from the shoulder to the clubhead, hands at or just ahead of the ball. Before it the clubhead
> trails the hands; after it the clubhead is past the hands. Judge by club and hands, not by
> whether a ball is visible: other balls may lie on the mat. If contact fell between two cells,
> give the one whose clubhead is nearer the ball, and if tied the earlier. Search hint only: on
> these replays impact is usually among the cells from about +1.7 s to +2.2 s; the picture decides.
> **P8 release:** after impact. Shaft about horizontal again, pointing at the target, both arms
> straight, clubhead well past the hands. Not the contact cell; once the shaft is vertical it is
> past.
> **P9 finish:** the balanced finish, turned to the target, club over the shoulder.
> A group whose position no cell plainly shows has cell 0 and all its entries null.

This is about 330 words, compared with about 300 in POC 3's JOB 1b. The text that asked for
LOCATE and EVIDENCE is gone, so the prompt gets shorter overall.

## 5. Evidence ownership rules

1. **One group = one phase × one camera.**
   - Its cell is the only picture for its `_dtl` (or `_fo`) checkpoints, plus the phase's `_any`
     checkpoints when `a` names that camera.
   - Multi-phase checkpoints (`transition_hand_direction`, `transition_trail_shoulder`) belong to
     the transition group.
2. **Cell 0 makes the whole group unavailable.** Any non-null entry in it is refused
   (`no_evidence`). There is no gap filling and no borrowing from neighbours, ever.
3. **A group that fails validation (section 6) is unavailable as a whole,** with the reason
   recorded.
4. **Address reference (proposed, R1).** Checkpoints judged against address need that camera's
   setup group to be valid. Explicit: `takeaway_posture`, `backswing_posture`,
   `backswing_hip_depth`, `top_posture`, `delivery_hip_depth`, `impact_hip_depth`,
   `impact_posture`. Implicit: `transition_posture_space`, `release_posture`, `backswing_centre`,
   `top_centre`, `transition_pressure_shift`. In the replays it would bind only on POC 2, whose
   FO P1 was null; there it would also have refused the wrong `top_centre` at FO_06.

## 6. Local chronological validator

Run per camera, on the group cells. Every failure nulls one group, never a camera.

| Rule | Check |
|---|---|
| G1 | Cell 1..18 is a real cell (the schema already bounds it); 0 = unavailable |
| G2 | Broad replay-impact prior, experimental and not biomechanical truth. Impact cell inside R7 = calibration mean ± 0.25 s ([1.724, 2.224] leave-one-out); P1–P6 cells before R7's end; P8–P9 cells after R7's start. |
| G3 | Swing order P1 < … < P9 where present. Keep the groups present in **every** longest strictly time-ordered chain (one cell may serve two *adjacent* positions); null the rest. A tie nulls every group it touches. Nothing is reordered. |
| G4 | Cross-camera agreement for moving positions (P2/P3 τ 0.30 s, P5–P8 τ 0.15 s; P1, P4 and P9 exempt as holds). On disagreement, null the side outside the other camera's bracket; null both if they can't be told apart. |

G3 on POC 2's DTL nulls only P8/P9, after G2 has removed P7. On POC 3, G3 nulls only the P3/P4
inversion on each camera. The neighbours stay, and no gap is filled.

## 7. Strict wire typing

Each POC 3 malformation is made impossible by the schema itself:

| POC 3 malformation | Why it cannot happen in vx14c |
|---|---|
| `0` as "no direction" | `x` items are a string enum with an explicit `"none"`; integers are not admitted. |
| Camera digit inside a checkpoint tuple (`[1,80,80]`) | `s` items are exactly two integers; the camera is the typed field `a` (enum `"d"`/`"f"`). |
| Stray integers appended to a list | Fixed `minItems`/`maxItems`, and `s` items admit only null or a 2-integer array. |
| `[null,null,null]` | `s` items are null or a 2-integer array; there is no third slot. |

What strictness cannot prevent: a direction that is valid but belongs to a different checkpoint
than the one at that position. Production's own `deviation` check refuses it whenever that
checkpoint lacks the word.

## 8. POC 1 replayed through vx14c

POC 1's per-list cells were mapped into groups: the camera list's cell, with the `_any` list
joining the camera it cited. No cell was invented.

- **14 of 16 used groups already had one common cell.** Every list was internally uniform.
- **Two groups split** between the camera list and the `_any` list on the same camera:
  backswing DTL (05 versus 06) and downswing DTL (10 versus 11).
  - Both splits are adjacent cells, and the catalogue defines both lists at the same moment, so
    the split is model noise, not a mechanical need.
  - The replay keeps the camera list's cell and refuses the 4 `_any` entries as
    `needs_second_cell`. Two of those were the wrong DTL_06 backswing reads.

**Group cells against the originals (under the POC 1 rule):**
- **DTL:** P1 correct, P2 acceptable, P3 DTL_05 acceptable, P4 correct, P5 acceptable, P6
  acceptable, P7 DTL_12 acceptable, P8 DTL_13 acceptable (this is the contact frame).
- **FO:** P1 correct, P2 acceptable, P3 correct, **P4 FO_06 wrong** (still rising), P5
  acceptable, **P7 FO_11 wrong** (delivery), P8 FO_13 acceptable (contact), P9 acceptable.
- **No group failed G1–G4.**

**vx14c-A results:**
- **Accepted / rejected:** 49 / 4.
- **Accepted, graded against truth:** 14 correct, 31 acceptable, **4 wrong**.
- **Coverage:** 1.00. **Swing Score:** 69.
- **Faults:** EARLY_EXTENSION (0.47) and BODY_STALL; root cause EARLY_EXTENSION; CaddieJ's visual
  lesson EARLY_EXTENSION.

**Phase scores:**

| Phase | Score | Coverage | Assessed |
|---|---|---|---|
| setup | 84 | 1.00 | 7/7 |
| takeaway | 78 | 0.80 | 5/6 |
| backswing | 74 | 0.59 | 5/8 |
| top | 78 | 0.94 | 7/8 |
| transition | 64 | 0.88 | 6/7 |
| delivery | 60 | 0.62 | 4/7 |
| impact | 58 | 1.00 | 7/7 |
| release | 70 | 0.91 | 5/6 |
| finish | 77 | 1.00 | 3/3 |

**Caveat:** 31 of the 45 non-wrong accepts are neighbour cells. The "acceptable" rule is lenient:
release read at #41, the contact frame, counts as acceptable. POC 1's group cells drift about
one still early, and the EARLY_EXTENSION lesson rests on impact read at #40.

**POC 2 and POC 3 through vx14c-A.** Their only cells are per phase and camera; nothing was
invented.

| | POC 2 | POC 3 (camera digit read where written) |
|---|---|---|
| Groups nulled | DTL P3, P5, P7, P8, P9 and FO P5 | P3 and P4 on both cameras (the inversion) |
| Accepted / rejected | 39 / 14 | 29 / 23 |
| Wrong-position accepts | 9 (FO P4, P7, P9) | **6** (transition at the top cells DTL_07 / FO_07), compared with 12 under the hybrid's gap-filling |
| Coverage / Swing Score | 0.82 / 75 | 0.65 / 68 |

The errors left in both are wrong cells that pass every deterministic rule. That points to the
P4/P5 semantics, not the ownership model.

## 9. Is one evidence cell per phase and view enough?

**Yes, against the catalogue (`vx14c_catalogue.py`):**
- **Every checkpoint is a single-moment criterion.** Several name their own moment, and it
  matches the group's position definition: `takeaway_clubhead_outside_hands` "with the shaft near
  parallel", `backswing_hand_depth` and `backswing_shaft_pitch` "with the lead arm near parallel",
  `delivery_shaft_plane` "with the shaft near parallel coming down".
- **All `impact_dtl` checkpoints** (hip depth, posture) and all `impact_fo` checkpoints (chest
  rotation, hand position, head) are properties of the contact frame. The `_any` impact
  checkpoints (pelvis rotation, lead arm in line with the shaft) are too.
- **No checkpoint needs a different frame from the rest of its group.**

**Two real nuances, neither needing per-checkpoint evidence:**
- **Address comparison.** About 12 checkpoints are judged *relative to address*. They need the
  same camera's setup picture as a reference, not a second evidence cell; rule R1 covers this.
- **One camera per phase for `_any`.** Some `_any` checkpoints read best from a particular camera
  (`top_shoulder_turn` from DTL, `release_lead_arm_structure` from FO), but all are defined as
  readable from either. The cost is a coarser camera choice, not untruthful evidence.

## 10. Estimated output tokens

The estimate uses a token proxy calibrated on all three real replies, within 2% (1,183 → 1,185;
950 → 966; 976 → 958). It is filled with POC 1's content.

| Wire variant | Phases | Total (+ metrics 215, coach 52) |
|---|---|---|
| **Recommended: `s` + `x` for every checkpoint** | 706 | **≈ 986** |
| `x` only for checkpoints that have directions | 667 | ≈ 947 |
| One pattern-typed string per entry (`"80 80 none"`) | 489 | ≈ 768 |
| Variant B (adds coarse LOCATE) | | +46 |

**The ~900 target is not met with proven typing.**
- **The pattern variant would reach about 770,** but production never uses `pattern`, so its
  acceptance under strict mode is unproven here. A schema rejection would waste the single call.
- **The `x`-subset variant saves about 40 tokens** but makes the model align directions to a
  subset of positions.
- **I recommend the robust variant (≈ 986) for the POC.** The pattern form should be proven
  separately later.

For reference, POC 2 produced 950 tokens and POC 3 976, so vx14c lands near both and is
structurally simpler than POC 3: one layer instead of three.

## 11. Remaining risks

1. **One-still errors that pass every rule** (POC 1's FO_06 top and FO_11 impact; POC 3's P5 =
   top on both cameras). Only the visual semantics address these, and the relational P3–P5
   wording is untested.
2. **Consistent cross-camera errors** are invisible to G4 (POC 3's P5).
3. **Lenient grading.** "Acceptable" includes reading release at the contact frame. Under an
   exact-still rule POC 1-A would show many more misses.
4. **Positional alignment of `s`/`x`** within a phase cannot be verified when a misplaced
   direction happens to be valid for the receiving checkpoint.
5. **One `_any` camera per phase** is coarser than POC 1's per-list choice.
6. **Token budget:** about 986 with proven typing, above the 900 target.
7. **Prior calibration:** a ProTee update or replay-start mis-detection shifts R7. Impact
   evidence would be lost (safe), and it must be logged.
8. **Evidence base:** one shot, one golfer, one ProTee version. Nothing here speaks to
   generalisation.

## 12. Is one vx14c paid POC justified?

**Yes, one, on the same shot and the same sheet, with the model and effort unchanged.** The
protocol under test is group ownership, the relational semantics and strict typing together.
These cannot be separated without more calls, and they are all "the protocol".

Pre-registered criteria, graded with `vx14c_replay.py`:
- ≤ 1 wrong group cell (of those returned);
- P7 within one still of contact (#41) on both cameras;
- ≥ 40 accepted checkpoints;
- ≤ 3 wrong-position accepts;
- no group refused for wire shape;
- output ≤ about 1,000 tokens.

If it passes, the next validation is several swings, then a different golfer with a different
tempo. Neither is performed here.

## Files (all under `research/temporal_evidence/`)

- `VX14C_REPORT.md`: this report.
- `vx14c_replay.py`: the group validator, the POC 1/2/3 mappings, variants A and B.
- `vx14c_size.py`: output-size variants. `vx14c_catalogue.py`: catalogue audit.
- `output/vx14c/`: `replay.json`, `size.json`, `vx14c_payload_example.json`.
