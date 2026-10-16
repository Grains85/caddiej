# vx14 hybrid evidence ownership: offline design and replay

No OpenAI call, and no production change. Everything here was derived from the two saved
replies (`output/poc/reply.json`, `output/poc2/reply.json`), the unchanged 18+18 sheet and the
full-resolution originals. The replay is `hybrid_replay.py`; its results are in
`output/hybrid/replay.json`.

## Recommendation: B. KEEP TEMPORAL + HYBRID PROTOCOL

When the saved replies are replayed through the hybrid validator:

- **POC 1's localisation is kept intact** (18/18 LOCATE cells pass).
- **POC 1 goes from 22 accepted / 31 rejected to 44 accepted / 9 rejected.** All 9 rejections
  are cells that really show the wrong moment, and none of the 22 rescued checkpoints is from a
  wrong position. A Swing Score now exists (69, coverage 0.91).
- **POC 2 keeps its good DTL evidence** despite the P8/P9 order failure: DTL P1, P2, P4 and P6
  survive. Wrong-position accepted checkpoints fall from 15 to 9.
- **The 9 POC 2 errors that remain are structural to POC 2's protocol.** POC 2 gave checkpoints
  no cell of their own, so no second opinion existed to catch them. The hybrid protocol asks for
  one.

The design costs about 950 output tokens, the same as POC 2 and 20% below POC 1.

---

## 1. Data flow

```
saved capture -> 18+18 neutral sheet (unchanged) -> ONE call -> reply
reply.locate   (orientation: where each position is seen, per camera)
reply.evidence (ownership: the cell each phase's checkpoints were actually read from, per camera)
reply.checkpoints (score, confidence, direction; no cell)
        |
CaddieJ, deterministic:
  L. validate locate per position:  prior -> local order -> cross-camera agreement
  E. validate evidence against the locate-derived candidate regions
  P. checkpoint gets its cell from evidence[phase][camera]; unchanged vx.parse_checkpoints
  S. score_swing -> faults -> root cause -> lesson cue -> coach validation (unchanged service order)
```

LOCATE never supplies a checkpoint's picture. It only bounds where that picture may come from.
The evidence cell is the picture the model says it scored from, so the score describes that
picture, and CaddieJ checks only that the picture lies in the right part of the swing.

Evidence is owned per phase and camera rather than per checkpoint. In POC 1, every one of the
24 lists cited a single cell for all its checkpoints, so a per-checkpoint cell carried no
information and cost about 220 tokens. The brief's example ("impact_posture → FO_13,
impact_hip_depth → DTL_13") is expressible, because those two checkpoints sit in different camera
lists.

## 2. Compact response schema

```jsonc
{
  "locate":   [[d,f], ...9],   // P1..P9: DTL cell number 1..18, FO cell number 1..18, 0 = not shown
  "evidence": [[d,f], ...9],   // per phase: the cell its _dtl / _fo checkpoints were read from, 0 = none
  "metrics":  [...],           // unchanged vx13
  "checkpoints": {
    "<phase>_dtl": [[score, conf, dir|null] | null, ...],   // read from evidence[phase].d
    "<phase>_fo":  [[score, conf, dir|null] | null, ...],   // read from evidence[phase].f
    "<phase>_any": [cam, [score, conf, dir|null] | null, ...] // cam 1 = DTL, 2 = FO
  },
  "coach": {...}               // unchanged
}
```

**Design choices:**
- **Cells are integers.** The prompt says "DTL_13 is 13 in a d slot". A 1–18 integer is a single
  token; `"DTL_13"` is about four. The list or slot fixes the camera.
- **Both grids are needed.** Locate gives orientation for all nine positions and a complete
  sequence to check for order. Evidence is the independent second opinion. Agreement within a
  region is corroboration; disagreement is a visible refusal, never a silent override.
- **LOCATE confidence is dropped.** In POC 2 it was 70–90 everywhere, and its highest values went
  to both wrong P7 answers. Checkpoint confidence is kept.
- **`_any` lists carry a camera digit.** The model chose FO for `_any` lists while DTL existed in
  POC 1 (release, finish), so a "DTL if present" rule would misattribute the picture.
- **Strict-mode compatible.** The grids are arrays of 9 arrays of 2 integers in 0..18. `_any`
  lists are `n+1` items of anyOf[integer 1..2, entry], which is the same anyOf pattern vx13
  already uses.

An example built from the POC 1 reply is in `output/hybrid/hybrid_payload_example.json`.

## 3. Deterministic validator rules

**Applied to LOCATE**, each rule nulling single slots only:

| Rule | Check | On failure |
|---|---|---|
| L1 | Number in 0..18 and a real cell of that camera | null slot (`camera`) |
| L2 | Replay prior (section 4): P7 inside R7; P1–P6 before R7's end; P8–P9 after R7's start | null slot (`prior`) |
| L3 | Local order (section 5) | null only the conflicting slots (`order`) |
| L4 | Cross-camera agreement for *moving* positions: \|t_DTL − t_FO\| ≤ τ, with τ = 0.30 s for P2/P3 (sparse stills) and 0.15 s for P5–P8 (dense stills). P1, P4 and P9 are exempt because they are holds, and a pause or held finish may be sampled at different ends. | Null the side that falls outside the other camera's bracket (the times of its kept neighbours). Null both sides if neither or both can be told apart. |

L4 needs only the two replays to be synchronised, which they are: on shot 4 contact is capture
#41 on both cameras, at +1.986 s on both clocks. It does not depend on the golfer's tempo.

**Applied to evidence and checkpoints:**

| Rule | Check | On failure |
|---|---|---|
| E1 | evidence[φ][cam] lies **strictly between** the cells LOCATE kept for the neighbouring positions on that camera, or equals LOCATE's own kept cell. It is then clipped by L2 for φ. Multi-phase checkpoints (`transition_hand_direction`, `transition_trail_shoulder`) may use the union of their phases' regions. | that phase/camera's checkpoints are refused `outside_region` |
| E2 | If LOCATE(φ, cam) was nulled, the region is the bracket alone, accepted only if it is at most 4 cells wide, and marked unanchored. Neither replay exercised this rule. | `unanchored_wide` |
| E3 | `_any` camera digit 1 or 2, and that evidence slot valid | `not_located` |
| E4 | Accepted entries go through the unchanged `vx.parse_checkpoints` with `frame_phases = {evidence frame: φ}`, so view, confidence, direction and score checks are all production's own | production reasons |

E1 is what stops "an arbitrary frame from another part of the swing". A checkpoint can never use
the cell LOCATE assigned to a neighbouring position, nor anything beyond it. It also does not
require equality with LOCATE: anywhere inside the gap is accepted.

## 4. P7 search prior, and priors for the other positions

**The prior is ProTee's, not the golfer's.** ProTee's replay is a fixed 3.00 s / 120 fps clip
cut around its own strike trigger. Measured impact sits at +1.964, +1.983 and +1.986 s after the
detected replay start. Tempo cannot move it; ProTee's pre-roll and trigger can.

- **R7 = mean of the calibration shots ± 0.25 s.** This replay used leave-one-out, built from
  shots 5 and 6 only (shot 4 is the test shot): [1.724, 2.224]. That is 8 of each camera's 18
  cells (DTL/FO_09..16) and more than 10 times the observed spread of 22 ms.
- **The ±0.25 s is broad on purpose.** It cannot tell delivery from contact: POC 2's FO_11 at
  +1.89 s passes. That job belongs to the visual semantics. The prior removes only gross errors
  such as POC 2's DTL_17 at +2.29 s.
- **The centre should be calibrated per installation,** from the logged anchor (`replay_anchor.py`)
  and recomputed when ProTee changes.
- **If R7 is wrong,** impact evidence is refused rather than mislabelled. That fails safe, and it
  should be logged as a `prior` miss.

| Positions | Useful constraint | Golfer-safe? |
|---|---|---|
| P1–P3 | Order only, and before R7's end. **No window.** The clip covers only about 1.98 s before impact, so a slow backswing may put address, or even takeaway, before the first still; those must be null, never forced. | yes |
| P4 | Order only; exempt from cross-camera because of pauses. No duration bound: a pause or a slow backswing must stay valid. | yes |
| P5–P6 | Before R7's end, after P4, cross-camera τ 0.15 s. I evaluated generous windows relative to R7 (P5 at most 0.8 s, P6 at most 0.5 s before it). They caught nothing in either reply and add risk for unusual downswings, so **I don't recommend them.** | yes as stated |
| P7 | Inside R7 (ProTee property). | yes, if R7 is calibrated per installation |
| P8–P9 | After R7's start; P8 cross-camera τ 0.15 s. **No upper bound on P9.** The sheet ends about 0.49 s after impact, so a slow finish may lie beyond the sheet and must be null. | yes |

## 5. Local order validation

```
per camera:
  seq = [(position, cell, t) for kept slots]
  chains = all maximum subsets of seq whose times strictly increase
           (one cell may repeat only for two adjacent positions)
  keep the positions present in EVERY maximum chain; null the rest ("order")
```

This is the longest monotonic subsequence, with ties resolved by nulling every position the tie
touches, never by guessing. At most 9 slots means 512 subsets, so brute force is trivial and
deterministic. Nothing is reordered.

On POC 2's DTL, L2 first removed P7 (DTL_17 @ +2.29, outside R7). P8 = DTL_18 and P9 = DTL_15
then tie, so both are nulled and P1–P6 are untouched. That is exactly the "keep P1–P7, null the
conflicting tail" behaviour the brief asked for. Under POC 2's rule, the same input dropped the
entire camera.

## 6. Visual semantics, from the full-resolution originals

The tiles are `output/poc/originals_swing_behind_38-47_box.jpg`, `originals_swing_side_38-47_box.jpg`,
`originals_swing_behind_21-37.jpg` and `originals_swing_side_19-37.jpg`.

**Delivery, contact, post-impact and release (capture index, both cameras):**

| Still | DTL | FO | Distinguishing relationship |
|---|---|---|---|
| #39–#40 delivery | hands at the trail hip/thigh, shaft angled up behind the hands | shaft about horizontal pointing away from the target, hands at the trail hip | **clubhead trails the hands**; wrists still hinged |
| #41 contact | clubhead at the ball; lead arm and shaft one straight line down to it | hands at the lead thigh, club and lead arm vertical in one line | **clubhead in line under the hands, at the ball** |
| #42 just after | hands past the lead thigh, club low and pointing toward the target | arms crossing, club past the hands | **clubhead has passed the hands** |
| #43 release (P8) | both arms extended across | both arms and shaft about horizontal toward the target | **shaft about horizontal toward the target, arms straight** |
| #44 onwards | club vertical or higher | club vertical, hands at the shoulder | past release |

**The ball is not a usable cue on DTL.** A second white object stays on the mat in every DTL
frame from #38 to #47, which is the likely trap POC 2's "ball still in its place" rule fell into.

**Proposed P7 wording (about 60 words):**

> P7 impact: the clubhead is at the ball, with the lead arm and shaft one straight line from the
> shoulder to the clubhead and the hands at or just ahead of the ball. Before it the clubhead
> trails the hands; after it the clubhead is past the hands. Judge by club and hands, not by
> whether a ball is visible: other balls may lie on the mat. If contact fell between two cells,
> give the one whose clubhead is nearer the ball, and if tied the earlier.

The wording says "at the ball", not "on the ground", so it also holds for a teed driver.

**The other positions, briefly:**
- **P3:** lead arm about horizontal going back, hands about chest high, shaft pointing up and
  *not yet behind the head*. DTL #24, FO #22. The top (#28) has the shaft behind the head.
- **P4:** the hands at their highest and the shaft behind the head; the next cells show the hands
  lowering. During a pause any cell of the pause is valid. FO #27 is still rising, so it is not
  P4.
- **P5:** lead arm about horizontal again *coming down*, with the shaft still pointing up above
  the hands (FO #38).
- **P6:** hands at the trail hip, shaft about horizontal and trailing the hands, wrists hinged
  (FO #39, DTL #40).
- **P8:** shaft about horizontal pointing *at the target*, both arms straight (#43). Once the
  shaft is vertical (#44) it is past.

## 7. Estimated output size

The estimate uses a token proxy calibrated on the two real replies. It reproduces POC 1 as 1,174
against 1,183 actual and POC 2 as 957 against 950 actual, so it is within 1%.

| Reply | locate | evidence | metrics | checkpoints | coach | **total** |
|---|---|---|---|---|---|---|
| POC 1 actual | 90 | — | 213 | 804 | 51 | **1,183** |
| POC 2 actual | 105 | — | 213 | 564 | 59 | **950** |
| Hybrid wire, filled with POC 1's content | 41 | 41 | 213 | 581 | 51 | **≈ 947** |

## 8. Offline re-grade of POC 1

| | As validated in POC 1 | Under hybrid |
|---|---|---|
| LOCATE kept | 18/18 | 18/18; no rule fired (10 correct, 7 acceptable, 1 wrong) |
| Accepted / rejected | 22 / 31 (27 `phase_unknown`, 4 `phase`) | **44 / 9** (all `outside_region`) |
| Accepted, graded against truth | 15 correct, 5 acceptable, 2 wrong | 16 correct, 26 acceptable, **2 wrong** |
| Coverage | 0.37, insufficient | **0.91, full** |
| Swing Score | none | **69** |
| Faults / root / cue | none / none / FACE_CONTROL_GOOD | EARLY_EXTENSION (0.47) and BODY_STALL; root EARLY_EXTENSION; CaddieJ's visual lesson EARLY_EXTENSION |

**Phase scores under hybrid** (score, coverage, assessed/scoreable):

| Phase | Score | Coverage | Assessed |
|---|---|---|---|
| setup | 84 | 1.00 | 7/7 |
| takeaway | 78 | 0.80 | 5/6 |
| backswing | 76 | 0.88 | 7/8 |
| top | 78 | 0.77 | 6/8 |
| transition | 64 | 0.88 | 6/7 |
| delivery | 60 | 0.88 | 6/7 |
| impact | 58 | 0.61 | 4/7 |
| release | — | 0.00 | 0/6 |
| finish | 77 | 1.00 | 3/3 |

**All 9 hybrid rejections were correct rejections:**
- `top_centre` at FO_06 (#27), which is still the backswing;
- `impact_fo` ×3 at FO_11 (#39), which is delivery;
- release ×5 at DTL_13 / FO_13 (#41), which is contact.

**None of the 22 rescued checkpoints is from a wrong position.** The only 2 wrong accepts were
already accepted in POC 1: `backswing_*` at DTL_06. There LOCATE *and* the checkpoint both
called the top P3, a correlated error.

**Caveat:** 26 of the 44 accepted cells are neighbours of the true still, not the still itself.
In the dense segment a neighbour is 50 ms away; in the sparse segment it is 0.2–0.3 s. The
EARLY_EXTENSION lesson rests partly on impact checkpoints read at DTL_12 (#40), one still before
contact. The error is bounded but real.

## 9. Offline re-grade of POC 2

POC 2 asked checkpoints for no cell, so the replay can only give each checkpoint its
hybrid-validated LOCATE cell (`_any` uses DTL if kept, else FO, as POC 2 did). This replay
therefore tests the LOCATE rules only, not evidence ownership.

**What the LOCATE rules did:**
- DTL P7 (DTL_17) was nulled by the prior.
- DTL P8 and P9 were nulled by local order (a tie).
- DTL P3 (DTL_06, the top) was nulled by cross-camera agreement, while FO_04 (correct) was kept.
- P5 was nulled on both cameras by cross-camera agreement (DTL_11 was correct and lost as
  collateral; FO_07 was wrong).

| | As validated in POC 2 | Under hybrid |
|---|---|---|
| DTL LOCATE kept | 0 (whole camera dropped) | **P1, P2, P4, P6, all correct** |
| FO LOCATE kept | 8 | 7 (P5 removed) |
| Accepted / rejected | 27 / 26 `not_located` | **39 / 14** `not_located` |
| Accepted, graded against truth | 7 correct, 5 acceptable, **15 wrong** | **29 correct**, 1 acceptable, **9 wrong** |
| Coverage | 0.57, limited | 0.82, full |
| Swing Score | 76 | 75 |
| Faults / root / cue | none / none / FACE_CONTROL_GOOD | unchanged |

The 9 wrong checkpoints left are `top_centre` at FO_06, impact ×5 at FO_11 and finish ×3 at
FO_16. Each FO cell is in the prior, in order, and has no conflicting DTL partner: P4 and P9 are
exempt holds, and DTL P7 had already been nulled. No deterministic rule can see these errors. In
the hybrid protocol, the evidence grid is the second opinion that would have to disagree. In
POC 1's data it did exactly that for impact, citing FO_11 when LOCATE said FO_14. There
it was the checkpoint side that was wrong, and E1 refused it.

**Answers to the four questions:**

| Question | Answer |
|---|---|
| Retain POC 1's good localisation? | **Yes.** No LOCATE rule fired on POC 1. |
| Avoid POC 1's 31 provenance contradictions? | **Mostly.** 31 → 9, and the 9 left are true wrong-moment cells, refused visibly. |
| Stop POC 2's wrong LOCATE cells contaminating whole phases? | **Partly from LOCATE rules alone** (15 → 9 wrong). The rest needs the evidence grid, which only a new call can test. |
| Preserve good DTL despite the P8/P9 failure? | **Yes.** DTL P1, P2, P4 and P6 are kept; only the conflicting tail and the cross-camera conflicts are nulled. |

## 10. Generalisation

| Swing | Effect on the hybrid |
|---|---|
| Very slow backswing | P1–P4 stretch earlier; no window constrains them. Address may precede the clip and must be null (allowed). |
| Very fast backswing | P2/P3 may share a cell or fall between stills: adjacent sharing is allowed, and null is allowed. |
| Pause at top | Any pause cell is P4; P4 is exempt from cross-camera agreement; the region rule allows any cell between P3 and P5. |
| Short swing | P3 ≈ P4 (shared cell allowed); P8 may be brief; the definitions are geometric, not durations. |
| Long swing / overswing | Top earlier relative to impact; no bound affected. |
| Beginner | Positions may be ill-formed (casting puts P6 early), so null is allowed. Mis-triggers or topped shots may move ProTee's anchor, and then the prior refuses impact evidence rather than mislabelling it. |
| Driver | Teed ball: P7 says "at the ball", not "on the ground". Longer arc but the same ProTee-anchored timing. |
| Iron | Reference case (this shot). |

No rule encodes this golfer's phase timing. The only absolute times are ProTee's (R7) and the
sampling geometry (τ). Every other constraint is ordering.

## 11. Remaining failure modes

1. **Correlated errors.** If LOCATE and evidence agree on the wrong cell (POC 1's DTL P3), the
   hybrid accepts it. Whether the evidence grid stays independent of LOCATE inside one reply can
   only be measured with a call.
2. **One- or two-still errors inside the dense window** that respect order and the prior (POC 2's
   FO_11 as P7). Only the visual semantics or a disagreeing second opinion catch these.
3. **Holds are unguarded.** P4 and P9 are exempt from cross-camera agreement (POC 2's FO_06 and
   FO_16).
4. **Collateral nulls.** A tie or cross-camera disagreement nulls good cells too (POC 2's
   DTL_11). That costs coverage, not correctness.
5. **Systematically early evidence.** POC 1's release evidence was one still early and all of it
   was refused, so release coverage was 0. The hybrid makes this visible but cannot fix it.
6. **Neighbour-accuracy evidence.** 26 of 44 POC 1 accepts were neighbours. Scores and lessons
   may describe a moment 50 ms (dense) to 0.3 s (sparse) off.
7. **Prior calibration.** A ProTee update or a replay-start mis-detection shifts R7, and impact
   evidence is then lost (safe, but silent unless logged).
8. **Evidence base.** One shot, one golfer, one ProTee version, two calls.

## 12. Is another paid POC justified?

**Yes, exactly one, on the same shot and the same sheet.** The central unknown can't be settled
offline: does a separately asked evidence grid stay an independent second opinion, as POC 1's
cell citations were (they differed from LOCATE in 17 of 24 lists), when asked for in the compact
integer form?

That call necessarily changes two things at once: the protocol, and the P3–P8 definitions with
the new club-and-hands P7 wording. The definitions are part of the protocol under test.

Pre-registered pass criteria, all graded with this replay code:
- LOCATE at least as good as POC 1 (at most 1 wrong);
- P7 within one still on both cameras;
- at least 40 checkpoints accepted;
- at most 3 accepted from a wrong position;
- evidence differs from LOCATE in at least a few phases (otherwise independence is unproven);
- output at most about 1,000 tokens.

A second shot should follow before any production work.

## Files (all under `research/temporal_evidence/`)

- `HYBRID_REPORT.md`: this report.
- `hybrid_replay.py`: the hybrid validator, the replay of both saved replies, and the output-size
  calibration.
- `hybrid_explore.py`: list shapes and POC 1's per-list cell use.
- `output/hybrid/replay.json`: the full replay results. `output/hybrid/hybrid_payload_example.json`:
  the proposed wire filled with POC 1's content.
- `output/poc/originals_swing_behind_38-47_box.jpg`, `originals_swing_side_38-47_box.jpg`,
  `originals_swing_behind_21-37.jpg`, `originals_swing_side_19-37.jpg`: the originals behind the
  visual semantics.
