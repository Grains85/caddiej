# Synchronized temporal evidence — offline study after POC 4

**Recommendation: B. SYNCHRONIZE T36.**

Keep 18 moments, 36 body images, the 2048² sheet and the current cell geometry. Choose the moments once for both cameras, so that DTL_nn and FO_nn are the same capture tick. Say so truthfully on the sheet and in the evidence text.

I did not adopt reallocation (C). On the three saved swings it moves one or two backswing cells and buys nothing measurable.

There were no OpenAI calls and no production changes, and no prompt was tuned against shot 4. Everything is under `research/temporal_evidence/`.

## 1. Current DTL/FO synchronization error

The diagnostic burst requests both cameras on the same tick, in one `asyncio.gather` in `swing_capture/service.py`. The measured requested-time skew between DTL and FO for the same capture index is **0.000 s in all three shots**. Screenshot latency is a mean of 17–18 ms and a maximum of 23.5 ms per request, issued concurrently.

The visual evidence agrees. At full resolution, every P5–P8 event falls on the **same capture index on both cameras** in all three shots:

| Shot | P5 | P6 | P7 | P8 |
|---|---|---|---|---|
| 4 | #37–39 | #39–40 | #41 | #42–43 |
| 5 | #38–39 | #40 | #41 | #42–43 |
| 6 | #37–38 | #39 | #40 | #42–43 |

In shot 6, the replay froze on the same ticks on both cameras: #41 is a byte-identical repeat of #40, and #51 of #50, on DTL and on FO. That means one replay renderer is driving both views.

The cameras are therefore synchronized per tick. The T36 *sampler* is what desynchronizes them.

**How T36 chooses cells today** (`temporal_sheets.sample_core`, run separately per camera):

- It keeps every still in the replay window [1.75, 2.15] s.
- It keeps 2 stills after that window, including the last.
- It always includes the first still.
- It always includes that camera's own calibrated-aim stills: production's per-camera selector aims for setup, takeaway, backswing and top.
- It fills the remaining budget by widest time gap.

The aims differ per camera. For example, shot 4's "top" aim is DTL #34 but FO #32. So the fill before 1.75 s lands on different ticks per camera.

| Shot | Synchronized ordinal pairs | Not synchronized | Max \|t_DTL − t_FO\| |
|---|---|---|---|
| 4 `092949` | 01, 09–18 (11/18) | 02–08 | 0.204 s (cell 04) |
| 5 `093010` | 01, 09–18 (11/18) | 02–08 | **0.411 s** (cell 06) |
| 6 `093033` | 01, 05, 06, 10–18 (12/18) | 02–04, 07–09 | 0.192 s (cell 04) |

Cell-by-cell timing for the unsynchronized cells (capture index and replay-relative seconds; all other cells are identical on both cameras):

| Cell | Shot 4 DTL | Shot 4 FO | \|Δt\| | Shot 5 DTL | Shot 5 FO | \|Δt\| | Shot 6 DTL | Shot 6 FO | \|Δt\| |
|---|---|---|---|---|---|---|---|---|---|
| 02 | #18 0.282 | #17 0.188 | 0.095 | #18 0.300 | #17 0.204 | 0.096 | #18 0.301 | #17 0.205 | 0.096 |
| 03 | #21 0.581 | #19 0.409 | 0.172 | #21 0.599 | #19 0.397 | 0.203 | #21 0.605 | #19 0.413 | 0.192 |
| 04 | #24 0.880 | #22 0.676 | 0.204 | #24 0.901 | #21 0.599 | 0.302 | #23 0.813 | #22 0.701 | 0.112 |
| 05 | #26 1.069 | #24 0.880 | 0.189 | #26 1.108 | #22 0.710 | 0.398 | #24 | #24 | 0 |
| 06 | #28 1.288 | #27 1.179 | 0.110 | #28 1.312 | #24 0.901 | **0.411** | #26 | #26 | 0 |
| 07 | #30 1.432 | #29 1.384 | 0.048 | #31 1.501 | #27 1.202 | 0.299 | #28 1.301 | #27 1.208 | 0.093 |
| 08 | #34 1.639 | #32 1.517 | 0.121 | #34 1.661 | #31 1.501 | 0.160 | #30 1.458 | #29 1.410 | 0.048 |
| 09 | = | = | 0 | = | = | 0 | #34 1.662 | #32 1.553 | 0.109 |

The full table for all 18 cells is in `output/sync/audit.json`.

## 2. Does equal cell numbering misrepresent time?

**Yes, exactly where P3 and P4 live.**

- **Shot 5 cell 06:** FO_06 is #24, which is P3 (lead arm horizontal going back). DTL_06 is #28, which is the top.
- **Shot 4 cell 06:** FO_06 is #27 (late backswing) while DTL_06 is #28 (the top).

Three things push the model toward pairing numbers:

- The current layout puts DTL_nn and FO_nn in **identical positions** in two side-by-side 4-column blocks.
- From cell 09 onwards the numbers really are the same moment.
- The model paired numbers in 9 of 9 phases in POC 4.

Meanwhile the vx14c text says the opposite of the capture's reality:

- The production SYSTEM says the views "are separate recordings and are NOT synchronised. The same time … is a different moment".
- The evidence line says "DTL and FO are separate clocks".

So the sheet geometry, the numbering and the text contradict each other. In the backswing, following the geometry gives the wrong answer.

The repeated DTL P3 = DTL_06 failure is *consistent with* this mechanism: find P3 on face-on (FO_06 in POC 1, 3 and 4), then reuse the number on DTL. It is not proven to be the sole cause, because POC 2 chose FO_04 and still chose DTL_06.

## 3. Proposed synchronized sampling algorithm

This is deterministic, and only replay and capture mechanics enter it (`sync_sampling.py`, `moments` and `sample_sync`).

1. **Moment line.** Use every capture tick at or after the detected replay start, at its requested replay-relative time. Both cameras share these ticks.
2. **Per-camera still for a moment.** This is that camera's still of the same tick, if it is distinct (not blank, not a byte-identical repeat) and within **τ = ½ × the smallest measured tick gap**.
   - Measured τ is 10.4, 9.2 and 10.5 ms for shots 4, 5 and 6 (minimum gaps 20.8, 18.4 and 19.9 ms).
   - Any other still is nearer a different moment, so it is never borrowed. The camera's cell for that moment is marked **unavailable**, and frames are never duplicated to fake sync.
   - None of the three shots has a one-sided gap, because the duplicate ticks are duplicated on both cameras.
   - A moment is a candidate if at least one camera has a still.
3. **Choose 18 moments once, for both cameras,** with today's core rule applied to the shared line, without per-camera aims:
   - every moment in the ProTee replay impact window [1.75, 2.15] s (replay geometry, not golfer timing);
   - 2 moments after it, the last included;
   - the first moment;
   - the rest by widest time gap.
4. **Naming.** DTL_kk and FO_kk are moment kk. The wire IDs stay exactly as vx14c expects.

The skew within one tick comes from screenshot latency, at most about 23 ms. That is half a fast tick (48 ms), and none was visible.

## 4. Evidence density

| Sampler (18 moments) | P3–P8 covered, both cameras, 3 shots | Cells P3..P5 | Max gap P2→P5 |
|---|---|---|---|
| A current T36 (per camera) | 36/36 | 7–9 | 0.21–0.30 s |
| **B synchronized** | 36/36 | 7–8 | 0.25–0.30 s |
| C synchronized + motion reallocation | 36/36 | 7–8 | 0.25–0.28 s |

Three conclusions follow:

- **The evidence for P3, P4 and P5 was already on every sheet, on both cameras, in all three swings.** POC 1–4 did not fail for want of a P3 or P5 still. It failed in choosing between stills.
- **Reallocation (C)** weights the pre-impact budget by time × (0.4 + 0.6 × normalised joint motion). It shifts one or two cells from takeaway towards P3, in shots 4 and 6. Coverage and gaps are unchanged within 0.05 s. It would matter for a long pause at the top, which these swings don't have. That stays a hypothesis, so C is not recommended now.
- **More moments.** At the same 2048² sheet, 24 moments means body images of h ≈ 251–258 px instead of 318, and golfer height of 121/142 px instead of 149/175 (DTL/FO), about −19% linear. The extra moments would only add backswing ticks that are already covered. That is not justified by this evidence.

## 5. Sheet layout

I generated three layouts on every shot for B and C (`output/sync/*_{current,columns,pairs}.jpg`) and inspected them:

| Layout | Body cell DTL / FO | Golfer px DTL / FO | Moment grouping | Per-camera continuity |
|---|---|---|---|---|
| **current geometry** (production `layout_sheet`, two 4-wide blocks) | 215×317 / 272×318 | ~149 / ~175 | implicit: same grid position in each block (now true) | contiguous per camera |
| pairs (one labelled tile per moment, DTL \| FO side by side, 4 per row) | 215×318 / 271×318 | ~149 / ~175 | explicit, one label per moment | broken: DTL frames interleave with FO |
| columns (moment column, DTL above FO, 3 bands of 7) | 184×273 / 233×273 | ~128 / ~150 | explicit, vertical | contiguous strips of 7 |

Golfer pixel heights use golfer shares of the native frame measured by eye: 0.47 for DTL and 0.55 for FO.

- **The columns layout** makes the pairing unmistakable. In shot 5, at moment 05 (#24), FO plainly shows the lead arm horizontal and the DTL frame above it is the same instant (`output/sync/crop_columns_m04-07.png`). But it costs 14% linear resolution, and a band break can fall inside the top.
- **The pairs layout** keeps resolution, but separates each camera's consecutive frames, which weakens reading the direction of motion.

**Proposal:** keep the current geometry, and change the header and evidence text to state that DTL_nn and FO_nn are the same moment. Once synchronized, the grid alignment the model already relies on becomes true, and nothing else in the evidence changes. Pairs is the resolution-neutral fallback if fresh swings still show cross-camera confusion. Columns is not recommended without evidence that it helps.

## 6. P3/P5 temporal-neighbour strategy

**Should direction-sensitive positions use previous/current/next cells instead of one still?** The neighbours are already on the sheet as the adjacent moments: about 0.2 s apart in the backswing, where capture is 10 fps and the sheet keeps about every second tick, and about 0.05 s apart from 1.75 s. In the current geometry, a camera's neighbours sit side by side in its block.

**Is single-still P3/P5 the wrong abstraction?** On these swings, P3 and P5 are not static twins. P3 is followed by the top run and P5 follows it, with the club set upright at P3 and lagging behind at P5. The observed failures were confusing P3 or P5 with the *top*, not with each other.

The useful change is synchronization itself. Lead-arm-horizontal is plain on FO and foreshortened on DTL. With one moment line, a P3 or P5 read on the clearer camera is automatically correct on the other.

**Can neighbours be represented without per-checkpoint evidence or more output?** Yes:

- Keep one moment number per group. The validator can derive the neighbours deterministically (kk−1, kk+1) for grading and review.
- Optionally, the visual definitions can be stated relationally: "P3: lead arm horizontal and the next moment shows the hands higher; P5: lead arm horizontal and the previous moment shows the hands higher".

Explicit triplets on the wire would triple the evidence fields, so I don't recommend them.

With synchronization, the cross-camera validator rule can become a structural check: **|d − f| ≤ 1 moment** for P2–P8, with P1, P4 and P9 exempt as today. This replaces τ in seconds, is golfer-independent, and needs no wire change.

## 7–9. Frame count, resolution, tokens

| | Current T36 | Proposed synchronized T36 |
|---|---|---|
| Moments / body images | 18 per camera, unaligned / 36 | 18 shared / 36 |
| Other cells | IMPACT, ALL_VALUES | same |
| Sheet | 2048×2048, ~1.13 MB | 2048×2048, ~1.13 MB |
| Body cells (DTL / FO) | 215×317 / 272×318 | 215×317 / 272×318 |
| Golfer height (DTL / FO) | ~149 / ~175 px | same |
| Image tokens (gpt-5-mini estimate) | ~4,916 | ~4,916 |

## 10. Offline coverage on all available real swings

Moment numbers on the synchronized (B) sheet that show each position, judged at full resolution (`output/sync/tiles_*`):

| Shot | P3 | P4 | P5 | P6 | P7 | P8 | Both cameras trustworthy? |
|---|---|---|---|---|---|---|---|
| 4 | M04 #22, M05 #24 | M07 #28, M08 #32 (FO: M08 only) | M10 #38, M11 #39 (FO also M09 #37) | M11, M12 | M13 #41 | M14, M15 | yes; P3 is clearest on FO |
| 5 | M05 #24 | M07 #28, M08 #31, M09 #36 | M11 #38, M12 #39 | M13 #40 | M14 #41 | M15, M16 | yes; P3 on a single moment |
| 6 | M06 #24 | M08 #28, M09 #31 | M11 #37, M12 #38 | M13 #39 | M14 #40 | M15, M16 | yes; P3 on a single moment |

**Caveat:** this is too few independent swings to conclude anything about tempo. It is three swings by one golfer, 44 s apart, in one session. Shots 5 and 6 were labelled for this study, and their P3 sets are mine, at about ±1 tick. What the data *does* establish is mechanical and golfer-independent: the cameras share ticks, and today's sampler unaligns them.

## 11. Expected effect on P6–P9

None on the evidence:

- **Shots 4 and 5:** the synchronized sampler keeps exactly the same stills under exactly the same cell numbers for cells 09–18.
- **Shot 6:** the same holds for cells 10–18.

The impact core is chosen by the same replay-time window as today, and was already synchronized. POC 4's correct delivery, exact impact, release and finish evidence is therefore untouched. The only changes are cells 02–08 (02–09 in shot 6), the header, and the clock sentences.

## 12. Generalisation risks

Synchronization itself is a property of capture, not of the golfer. The sampler uses only tick times and the ProTee replay geometry; no P-position timestamp enters it.

| Case | Effect | Why the policy still exposes the motion, or the risk |
|---|---|---|
| Fast backswing | P3 may last under one 10 fps tick, and the sheet keeps about every second tick | P3 may fall between moments. It must then be null, never forced. **Main risk**: the remedy is capture cadence in the backswing, not sheet layout. |
| Slow backswing | Address or takeaway may precede the replay's first tick | First moment mid-takeaway; P1/P2 null; P3–P5 better resolved |
| Pause at top | Many similar top moments under time-uniform fill | Order and the top run stay visible; C's motion weighting would thin the pause (untested here) |
| No pause | Top on one moment | Adjacent sharing allowed, as today |
| Short backswing | P3 ≈ P4 | Shared or adjacent moments allowed; geometric definitions |
| Long backswing | Top later, near the 20 fps region (from ~1.43 s) | Denser capture helps; transition is in the core window |
| Fast transition | P4→P5 in under 0.1 s | Core keeps every tick from 1.75 s at ~48 ms |
| Slow downswing / beginner | P5 can start before 1.75 s | P5 sampled by the gap fill at ~0.1–0.2 s, coarser than today's shots. A risk to measure on a slower golfer. |
| Beginner sequencing (arms first, casting) | Geometry differs from the textbook | Definitions are geometric; synchronized views help separate casting from delivery |
| Replay-start or clip change | The [1.75, 2.15] s window assumes impact at ~+2.0 s in the ProTee clip | Measured on 3 shots only (+1.96–1.99 s). If ProTee or the detection changes, the window must be re-derived. |

## 13. Exact next experiment

1. Capture **≥ 5 fresh real swings** with diagnostic timelines retained. Before any call, label P1–P9 per capture index on both cameras at full resolution, and pre-register the criteria. Use the POC 4 criteria per swing, plus: zero P3/P5 group cells on a top still, and no regression on P6–P9.
2. For each swing, build two sheets offline:
   - **A:** current T36.
   - **B:** synchronized T36, same geometry. The header reads "DTL_nn and FO_nn are the SAME moment from two cameras".
3. Run vx14c unchanged, except for the evidence-description sentences that are false under B. These are the SYSTEM "NOT synchronised" paragraph and the "separate clocks" evidence line, both overridden in research code; production is untouched. The B validator uses |d − f| ≤ 1 moment.

   That is one call per sheet per swing: **2 × N paid calls**, for the user to authorise.
4. Primary metric: wrong P3/P5 group cells, A vs B, across swings. Secondary metrics: wrong-position accepts and P6–P9 regressions.
5. Only if B holds up, repeat with **a different golfer with a meaningfully different tempo.** Consider the pairs layout or C only if confusion persists.

## Files

All files are under `research/temporal_evidence/`.

- `sync_sampling.py`: audit, tick skew, synchronized moments with τ, samplers B and C, evaluation against by-eye labels, three layouts, size plans.
- `sync_tiles.py`: full-resolution tiles of any saved shot, used to label shots 5 and 6 on both cameras.
- `output/sync/audit.json`: current T36 timing per ordinal cell, plus tick skew and cadence.
- `output/sync/sampling.json`: moments chosen per sampler, coverage, gaps, tolerance, unavailable ticks.
- `output/sync/layouts.json`: layout plans (18/21/24/27 moments) and render sizes.
- `output/sync/<shot>_{B_sync,C_realloc}_{current,columns,pairs}.jpg`: 18 candidate sheets.
- `output/sync/tiles_*.jpg`, `crop_columns_m04-07.png`, `crop_pairs_m05-08.png`: review images.
- `SYNC_REPORT.md`: this report.
