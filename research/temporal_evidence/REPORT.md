# Temporal swing evidence — research report

Offline only. No production code changed, no OpenAI call made. Evidence: the
three real shots saved on 2026-10-08 (`20261008T092949-4-d05a29c4`,
`20261008T093010-5-6669cd32`, `20261008T093033-6-819a363f`), each with 51
retained diagnostic stills per body camera.

**Recommendation: MOVE TO TEMPORAL EVIDENCE**, as a superset of today's
calibrated stills: 18 stills per body camera (36 body cells) + IMPACT +
ALL_VALUES on one 2048×2048 sheet, chosen by an impact-anchored dense core
plus time-spread context, with a LOCATE + ANALYSE evolution of vx13 that
keeps the output no larger than today's.

---

## 1. The current pipeline (exact references)

| Stage | Code | What it does |
|---|---|---|
| Burst cadence | `swing_capture/cadence.py` `sample_times` (74), `spacing_floor` (53), `MIN_SPACING = 0.7` (48) | Fixed deadlines from burst zero: 10 fps, 20 fps over burst +3.3..+4.4 s; a still waits at least 0.7 of its period after the previous one. |
| Capture | `swing_capture/service.py` `_capture_frames` (889), `_finish` (1127) | OBS `GetSourceScreenshot` burst per panel. |
| Replay start | `select.py` `replay_start` (249), `_REPLAY_STEP = 1.2` (87), `_REPLAY_HOLD = 0.9` (92); `shared_body_start` (573), `_borrowed` (604) | Largest held step in encoded JPEG size; a camera without one borrows the other's. |
| Phase aims | `phases.py` `PHASE_SECONDS` (140), `phase_plan` (157), `_PRIORITY` (186) | Fixed seconds after replay start per phase (DTL: setup 0.00, takeaway 0.60, backswing 0.88, top 1.64, transition 1.90, downswing 1.95, impact 2.00, release 2.08). |
| Selection | `select.py` `phase_targets` (306), `nearest` (342), `select` (497), `package_quota` (437), `_AIM_TOLERANCE = 0.05` (106) | Nearest still to each aim; 16 selected frames → 8 DTL + 7 FO + impact. |
| Panel selection / result | `service.py` `_select_panels` (1346), `_result` (1414), `_diagnostic_retained` (1459) | Builds the `SwingCapture`; keeps the full burst only when diagnostics are retained. |
| Storage | `store.py` `_write_camera` (459), `_write_diagnostic` (511), `frame_id` (891: `f"{camera_id}_{index:02d}"`) | Selected stills under `<camera>/frame_NNN.jpg`; full burst under `diagnostic/`. |
| Provider order | `stills.py` `provider_frames` (107), `frame_label` (63), `note_provider_images` (233) | The ordered, labelled stills that may travel. |
| Sheet | `sheet.py` `plan_cells` (182), `layout_sheet` (380), `compose_sheet` (561), `EvidenceSheet` (660), `original_for_cell` (875); `MAX_SIDE 2048`, `JPEG_QUALITY 90` | Cell IDs **named after the aimed phase** (`DTL_P5`), caption "~phase @Xs" from `phases.frame_detail` (241). |
| Cell limit | `coaching/service.py` `_protee_cells` (1715) → `plan_cells(capture, self._max_images)` | |
| Request | `coaching/vx.py` `PROTEE_PROMPT_VERSION = "vx13"` (92), `_frames_schema` (501), `reply_schema` (579), `render_evidence` (691), `build_request` (906) | One request, one sheet image. |
| Reply | `vx.py` `decode_wire` (979), `canonical_reply` (946), `parse_frames` (1210), `parse_checkpoints` (1248), `_checkpoint_reading` (1319) | `frames` (cell → phase), positional `metrics`, `checkpoints` lists keyed `<phase>_dtl/_fo/_any` with `[score, cell, confidence, direction]`. |
| Conclusion | `coaching/service.py` `_process_protee` (1494), `_conclude_protee` (1747) | `parse_frames`/`parse_checkpoints` → `score_swing` → mechanics → `infer_faults` → corroborate → `rank_root_cause` → `LessonTracker.step`. |
| Token accounting | `coaching/benchmark.py` 324–431 | gpt-5-mini: fit into 2048², 32-px patches, ×1.2. |

The important architectural fact: **vx13 already makes the model locate
phases** (`frames` maps each cell to a phase or "unclear", and
`parse_checkpoints` rejects readings whose cell's located phase conflicts).
What leaks the fixed timing into the labels is the evidence itself: the cell
IDs (`DTL_P5`) and captions (`~transition @1.90s`) announce the aimed phase,
and only aimed stills exist on the sheet. When the aim is wrong (shot 6: one
still early, impact lost to a duplicate at 40/41) the model has no correct
still to choose.

## 2. Reusable unchanged

- Cadence, spacing floor, burst capture, OBS adapter, replay-start detection
  and camera borrowing, diagnostic retention (do not undo any of these).
- `select.select` and `phases.py` — kept running; their picks become the
  superset anchors and the `calibrated_aim` diagnostic (never sent to the model).
- `sheet.layout_sheet` and `compose_sheet` — the experiment reused
  `layout_sheet` read-only and it already finds the best grid for 36 cells
  (a 9-column stacked layout was checked by hand and is no better).
- JPEG q90 4:4:4, `MAX_SIDE 2048`, the All Values strip at full width.
- `EvidenceSheet.metadata()` / `original_for_cell` / `frame_path` provenance API.
- `canonical_reply` (cell ID → frame id), `parse_checkpoints`,
  `_checkpoint_reading`, the checkpoint catalogue, `score_swing`, mechanics,
  faults, corroboration, root cause, lesson tracker, cue wording, thresholds —
  all untouched, because the new LOCATE map is inverted back into the exact
  `dict[frame_id, SwingPhase]` that `parse_frames` produces today.

## 3. Needs replacing

1. **Cell planning** (`plan_cells`): neutral chronological IDs `DTL_01..DTL_18`,
   `FO_01..FO_18`, captions `+1.84s` (seconds from that camera's replay start),
   no phase words anywhere.
2. **Which stills travel**: a temporal sampler over the distinct replay stills
   (§9) instead of `provider_frames` over the aimed selection.
3. **What is stored canonically**: the 36 chosen originals must always be
   kept (today only the aimed ones are, unless diagnostics are retained).
4. **vx13 → vx14 protocol** (§10): `frames` map replaced by `locate`; prompt
   text drops aim hints.
5. **Config**: a cells-per-camera setting replaces `SWING_CAPTURE_SELECTED_FRAMES`
   for the sheet path.

## 4. The real evidence

| | DTL (swing_behind) | FO (swing_side) | Impact | All Values |
|---|---|---|---|---|
| Native size | 1002×1480 | 1272×1490 | ≈1350×595 | 5102×435 |
| Golfer | ≈740 px tall (~50% of height), ~320 px wide | ≈890 px tall, ~360 px wide | — | — |
| Never-moving area | bottom ~20–28% (carpet, ProTee controls) | sides and floor | | |

- Replay start detected at capture index 15 on all shots; 14 spinner stills
  precede it. 37 replay stills per camera: ~14 at 10 fps (+0.0..+1.4 s) and
  ~22 at 20 fps (+1.43..+2.47 s).
- **Impact is within one still of replay +2.0 s on all three shots** (impact
  stills at +1.986 / +1.983 / +1.964 s). ProTee's log shows why: every clip is
  a fixed 3.00 s at 120 fps, cut a near-constant ~1.7 s after the strike
  trigger. It is ProTee replay geometry rather than golfer tempo, but it is
  inferred from logs on one ProTee version, not documented — see
  `POC_REPORT.md`. The sampler's dense core uses it only as a broad window.
- The bay runs `duration 4.0 s`, so capture ends at replay ≈ +2.47 s; ProTee's
  replay lasts ≈ 3.05 s. The finish (P9) is barely in frame for this golfer
  and outside the window for most golfers (§12). The config default is 4.5 s;
  restoring it ends the burst at the same moment the static impact pass
  already finishes (burst +5.0 s), so it costs no wall time.
- Downswing sharpness: DTL is crisp enough to see the shaft in delivery
  stills. FO delivery stills (e.g. index 40) have heavy motion blur on shaft
  and hands — camera exposure, present at native resolution, so downscaling
  does not lose it and higher resolution would not recover it.
- Motion (changed-pixel rate): ~0 at address, ~0.1 backswing, a dip at the
  top (+1.45..+1.6 s), 2–4× spikes through the downswing, and it **stays high
  through the follow-through** — so motion peaks alone cannot tell downswing
  from follow-through.
- **How far cells can be downscaled** (judged on 1:1 crops in `output/crops/`):
  - 291×429 (today, ×0.29, golfer ≈215 px): everything legible.
  - 215×317 (T36, ×0.215, golfer ≈160 px DTL, ≈190 px FO): every phase
    identifiable; head, shoulders, hip line, hand blob, knee flex read clearly;
    the shaft reads in backswing, top and DTL delivery; lead-wrist angle and
    the exact shaft-to-plane-line angle are marginal.
  - ~175×260 (22+/camera, golfer ≈130 px): judged too small for posture
    checkpoints; this is the floor.
  - Impact at 272×120 is still a legible strike dot. All Values stays at
    2032×173 in every variant, so its digits read exactly as today.
- Cropping to the motion envelope (T36crop) lifts DTL to ×0.25 (golfer ≈200 px)
  and is visibly better, but CaddieJ's rule is that OBS owns framing. The
  right lever is an operator-side OBS crop of the carpet and ProTee controls,
  later, not in-code cropping.

## 5. Candidate layouts

All via production `layout_sheet`: DTL block and FO block side by side,
chronological left→right, top→bottom, IMPACT in the FO block's free slot,
ALL_VALUES as a full-width strip at the bottom. One header line states
"cells in time order … +s = seconds from that camera's replay start; DTL and
FO are separate clocks". Alternatives considered: stacked DTL-over-FO
(9 columns × 2 rows each) gives 218-px cells — no gain; per-camera separate
images were excluded (one image input only).

## 6. Generated sheets (all three shots give identical geometry)

| Variant | Body cells | Sheet | JPEG | DTL cell | FO cell | Impact | All Values | Image tokens |
|---|---|---|---|---|---|---|---|---|
| Current vx13 | 15 | 2048×1679 | 727–809 KB | 291×429 | 370×433 | 370×163 | 2032×173 | 4071 |
| A: T20 (10/cam) | 20 | 2048×2047 | 0.92 MB | 276×407 | 348×407 | 348×153 | 2032×173 | 4916 |
| B: T28 (14/cam) | 28 | 2048×1707 | 0.92 MB | 217×320 | 275×322 | 275×121 | 2032×173 | 4148 |
| C: T36 (18/cam) | 36 | 2048×2048 | 1.13 MB | 215×317 | 272×318 | 272×120 | 2032×173 | 4916 |
| C-crop (study only) | 36 | 2048×1943–2013 | 1.08–1.11 MB | ~248×271 | ~244×297–311 | ~244×107 | 2032×173 | 4685–4839 |

Cell size against cells per camera (layout only): 8→291, 10→276, 12→244,
14→217, 16→220, 18→215, 20→195, 22→177, 24→176 px DTL width. 16 and 18 cost
the same tokens; 18 is the last count before cells shrink sharply.

## 7. Token budget

Image tokens move from 4071 to 4148 (T28, +77) or 4916 (T36/T20, +845).
vx13 measured 11,634 input / 1,152 output tokens at ~13.5 s. Text: the
evidence section shrinks (one line of offsets per camera replaces per-cell
aim captions) and the schema loses the 15-key `frames` object, so T36 lands at
≈12.3–12.5k input, inside the 11–13k target. Output is expected flat (§10).
Latency is driven by output tokens; the temporal sheet should not move it,
but it will not by itself bring vx13's ~13.5 s down to the preferred ~10 s.

## 8. Recommended frame count

**18 per camera (36 body cells).** Same image cost as 16 or 20 (all hit the
2048 cap), cells still at the readability floor that was checked, and it is
the smallest count at which the sampler holds every labelled position on the
real shots *and* 99/106 positions over the 12 synthetic golfers. **14 per camera
(T28) is the fallback**: it costs only +77 image tokens over today and holds
35/36 real labels, but only 88/106 synthetic positions (it starves the
backswing). 10 per camera is not viable: the dense core consumes the budget.

## 9. Recommended sampling algorithm

Per body camera, over the replay stills (`rel ≥ 0`, blanks < 800 B and
byte-identical repeats removed — deterministic, no pixel decoding):

1. Always keep the first and last replay stills.
2. **Superset**: always keep today's calibrated picks (`select.select`); they
   are recorded as `calibrated_aim` in provenance and never shown to the model.
3. **Dense core**: keep every distinct still with replay offset in
   [+1.75 s, +2.15 s] (≈9 stills at 20 fps). Justified by ProTee anchoring
   impact at +2.00 s regardless of tempo; it is a broad window (top-of-fast-
   transition through release for downswings up to ~0.5 s), not a label.
4. Keep 2 stills after the core (finish side), placed in the widest time gaps.
5. Spend the rest on the backswing side (address → top), widest gap first.
6. Then any leftover anywhere, widest gap first. Output in time order.

Evaluated alternatives (real labels, 36 positions over 3 shots × cameras):

| Sampler | N=10 | N=14 | N=18 |
|---|---|---|---|
| uniform | 26 | 30 | 30 |
| uniform + calibrated | 35 | 35 | 36 |
| variable density (fast-cadence weight 2.5) | 26 | 30 | 32 |
| variable + calibrated | 35 | 36 | 36 |
| motion-weighted (0.4 time + 0.6 change) | 28 | 32 | 35 |
| motion + calibrated | 35 | 36 | 36 |
| **core (recommended, no calibrated)** | 23 | 35 | **36** |
| **core + calibrated (recommended)** | 30 | **36** | **36** |

Uniform at 18 still misses P6/P7 (its ~0.14 s spacing jumps 50-ms positions).
Motion-weighting needs pixel decoding and adds nothing once the core exists;
keep it as a diagnostic. Synthetic generalisation (§12) separates the options:
uniform + calibrated loses P5 for slow downswings because its density is
the calibrated golfer's; the core does not. If the core is quiet (low JPEG
size change across it), log a diagnostic — it means ProTee's replay geometry
changed, not that the golfer did.

## 10. vx14 protocol (compact evolution of vx13)

- **Evidence text**: `DTL_01..DTL_18: +0.00 +0.30 … +2.46` and the same for
  FO; no phase words; header explains the clocks.
- **LOCATE** replaces `frames`:
  `"locate": {"dtl": [cell|null × 9], "fo": [cell|null × 9]}` — index i is
  P(i+1): setup, takeaway, backswing, top, transition, downswing, impact,
  release, finish. `null` = not visible / not captured. ~18 short tokens vs
  vx13's 15 key/value pairs: output does not grow.
- **ANALYSE** unchanged in shape: `checkpoints` lists keyed
  `<phase>_dtl|_fo|_any`, entries `[score, cell, confidence, direction]`,
  null allowed. No return to the V4 schema.
- **Validation** (new, in parsing, before `parse_checkpoints`):
  1. each located cell exists and belongs to that camera;
  2. per camera, located cells are non-decreasing in time;
  3. a cell is located for at most two adjacent phases (e.g. top and
     transition for a golfer with no pause);
  4. a violating camera's whole locate list is dropped (reason
     `locate_order`), its checkpoints fall to null;
  5. every scored checkpoint must cite the cell located for its phase on its
     camera (`_any`: either camera; the two multi-phase checkpoints,
     `transition_hand_direction` and `transition_trail_shoulder`, the
     transition or downswing cell), else rejected `cite_mismatch`.
- The inverted locate map becomes today's `dict[frame_id, SwingPhase]`, so
  `parse_checkpoints` and everything after it run unchanged.

## 11. Provenance

Each cell in `evidence_sheet.cells` (metadata.json) gains: `cell_id`,
`camera_id`, `capture_index`, `frame_id`, `file` (canonical original),
`sha256`, `offset_from_replay_start_seconds`, `burst_offset_seconds`,
`calibrated_aim` (or null), `crop` (always null in CaddieJ). The 36 chosen
originals are written as the camera's canonical frames (independent of
diagnostic retention); `frame_id` should derive from `capture_index` so it is
stable whether or not the full burst is retained. `original_for_cell` keeps
its signature. The experiment writes exactly this table next to each sheet
(`output/sheets/<shot>_<variant>.json`).

## 12. Generalisation (synthetic timelines on the real cadence, impact at +2.00 s)

18 per camera, core + calibrated. "Held" = a kept still within ±75 ms of
P2/P3, ±30 ms of P5–P7, ±40 ms of P8, or inside the address / top-with-pause
/ finish interval.

| Golfer | Backswing / pause / downswing (s) | Missed on bay window (4.0 s) | Missed with 4.5 s window |
|---|---|---|---|
| 1 fast backswing | 0.65 / 0.05 / 0.25 | P4; P9 not captured | P4 |
| 2 very slow backswing | 1.40 / 0.10 / 0.33 | P2; P9 not captured | P2 |
| 3 pause at top | 0.90 / 0.60 / 0.30 | P3; P9 not captured | P3 |
| 4 short backswing | 0.60 / 0.05 / 0.24 | P4 | P4 |
| 5 long backswing | 1.10 / 0.10 / 0.30 | P9 not captured | — |
| 6 very fast transition | 0.90 / 0.00 / 0.20 | P9 not captured | — |
| 7 beginner, slow, cast | 1.20 / 0.20 / 0.45 | P9 not captured | — |
| 8 partial swing | 0.55 / 0.00 / 0.30 | P4 (no P3 exists) | P4 |
| 9 driver | 0.95 / 0.10 / 0.30 | P9 not captured | — |
| 10 iron | 0.85 / 0.05 / 0.28 | P2; P9 not captured | P2 |
| extreme 2.2 s backswing + pause | 1.70 / 0.20 / 0.35 | P1 before replay; P2; P9 | P1 before replay; P2 |

P5–P8 are held for every golfer. Remaining misses are backswing positions
falling between 10 fps stills (nearest kept still ~100–150 ms away — the model
should locate the nearest or return null), and two capture limits no sampler
can fix: ProTee's ~2 s pre-roll hides address for very slow swings, and the
bay's 4.0 s window cuts the finish for 8 of 11 golfers.

## 13. Risks

- Smaller cells (215 vs 291 px): wrist/shaft-angle checkpoints may lose
  confidence; the benchmark must compare checkpoint coverage, not only locate.
- FO delivery blur is in the source; temporal context helps locate but not read it.
- Dependence on ProTee replay geometry (impact at +2.00 s); a ProTee update
  could move it — mitigated by the quiet-core diagnostic and the broad window.
- Very slow backswings lose P1 to ProTee's pre-roll.
- 4.0 s bay window loses P9 — recommend restoring 4.5 s (config only).
- Model may mis-order or over-locate; the monotonic validator turns that into
  nulls rather than wrong labels.
- ~2.4× more stills stored per shot.
- Output-token growth if the prompt invites commentary on more cells — the
  schema must stay positional.
- Three shots, one golfer: the real-label results are a sanity check, not a
  validation; the synthetic study covers tempo, not body type or clothing.

## 14. Minimal paid benchmark (not run)

- Shots: the three saved shots. Arms: (A) current vx13 sheet + vx13 prompt
  (control), (B) T36 core sheet + vx14 draft, (C) T28 core sheet + vx14 draft.
  3 repeats each = 27 gpt-5-mini calls, same model, same timeout. The harness
  lives under `research/`, reusing `build_request`-style assembly read-only.
- Measure per call: locate correctness against the by-eye labels in
  `temporal_sheets.py` (`TRUTH`), validator rejections by reason, scored
  checkpoint count and citation validity, checkpoint score agreement with arm A,
  input/output tokens, latency, cost.
- Pass for B: ≥ 8/9 phases located inside the accepted sets per camera on all
  shots, no order violations on ≥ 8/9 calls, scored checkpoints ≥ arm A,
  input ≤ 13k, output ≤ arm A + 10%, p50 latency ≤ arm A and max < 15 s.
  Prefer C if it matches B's checkpoint coverage.

## 15. Recommendation

**MOVE TO TEMPORAL EVIDENCE** — 18 per camera, core + calibrated superset,
vx14 LOCATE + ANALYSE, after the benchmark above passes. Restore the 4.5 s
capture window at the same time.

---

## Files created (all under `research/temporal_evidence/`)

- `inspect_shots.py` — prints configuration, sheet cells, per-camera timelines.
- `analyse_frames.py` — motion occupancy maps, motion rate, sharpness →
  `output/frame_analysis.json`, `output/motion_maps/*.png` (6).
- `contact.py` — numbered contact sheets for labelling → `output/contact/*.jpg` (18).
- `temporal_sheets.py` — pool, samplers (uniform, variable, motion, core),
  by-eye labels, coverage, sheet composition and provenance →
  `output/sheets/*.jpg` + `*.json` (24), `output/crops/*.png` (48),
  `output/report.json`, `output/temporal_sheets.log`.
- `generalisation.py` — layout cell-size sweep and synthetic-golfer sampler study.
- `REPORT.md` — this report.
