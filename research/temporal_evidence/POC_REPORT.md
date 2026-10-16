# Temporal POC — one vx14-draft call on one saved shot

Exactly one paid OpenAI call was made (2026-10-08, gpt-5-mini, production
settings). No production code was changed. Recommendation: **B. TEMPORAL
PROMISING.**

## 1. Is "impact at replay +2.00 s" a ProTee property or a golfer property?

A. **Golfer tempo** decides how long before impact the takeaway and top come.
B. **ProTee's replay timeline** decides where impact sits inside the clip
CaddieJ photographs. It depends only on how ProTee cuts its camera buffer
around its trigger, and that trigger is ProTee's own detection of the ball at
the strike.

Evidence for B:

1. ProTee's log (`LogFile-2026-10-08.txt`, ProTee Labs v1.11.15): all 25 triggers
   today produced, per camera, `Video duration 00:00:03 - FPS: 120`. The
   container metadata of the clips still on disk (four files, including shot 6)
   reads `Duration: 00:00:03.00, 120 fps`.
2. The first clip is created a near-constant time after the trigger:
   1.63–1.76 s on 23 of 25 triggers (two outliers 1.87 and 1.93 s); 1.732,
   1.723 and 1.719 s for the three saved shots. A fixed-length clip that ends a
   fixed time after the trigger starts a fixed time before it, however the
   golfer swung.
3. CaddieJ's own capture: replay start detected at burst +1.918 / +1.901 /
   +1.902 s, and the impact still (by eye, full resolution) at **+1.986 /
   +1.983 / +1.964 s** from it, within one 20 fps still of +2.0 s. The OBS load
   delay varies (1.83–2.06 s after the strike on earlier shots), but CaddieJ
   measures from the detected replay start, so that jitter cancels.

Not proven:

- The three shots are one golfer with a steady tempo. On their own they
  cannot separate A from B. The separation rests on the mechanism in points 1
  and 2, which is inferred from the log, not documented by ProTee.
- The 2.0 s before / 1.0 s after split is measured, not read from ProTee. The
  clip bitstream is not standard MPEG-4 (no start codes in `mdat`), so the
  impact frame could not be found in ProTee's own 120 fps video.
- One ProTee version and one camera configuration (120 fps, 25-image trigger
  buffer). Precision is ±50 ms.
- My earlier report quoted 2.002 / 2.001 / 2.006 s. Those figures do not
  reproduce; the measured values are above.

**Verdict:** a well-supported property of ProTee's replay on this bay, not a
proven invariant. It may set sampling density (a broad ±0.25 s window), never
a label. It was not given to the model in the POC. If it is used, CaddieJ
should check it at runtime, for example by logging the model-located impact
time against +2.0 s and ProTee's `Video duration` line.

## 2. The call

- Shot `20261008T092949-4-d05a29c4` (trigger 4, iron). It was chosen because
  both cameras have P1–P9 ground truth fixed before the call.
- Sheet `output/sheets/…_T36.jpg`: 2048×2048, 1.13 MB. 18 DTL + 18 FO neutral
  cells, IMPACT, ALL_VALUES, and `+s.ss` captions only.
- Prompt: vx13's SYSTEM with JOB 1b replaced by LOCATE, aim/`~moment`
  paragraphs neutralised, and the cell list given as `DTL_07 +1.43s`.
  Schema: `frames` replaced by `locate: {dtl:[9], fo:[9]}`; checkpoints
  unchanged. An automated check confirmed no `~phase`, `DTL_P…`, `@time`,
  calibrated aim or ground truth in the request.
- Reading: CaddieJ's own `decode_wire` → `canonical_reply` → `parse_checkpoints`.
  LOCATE is inverted into the `frames` mapping, plus a chronology / shared-cell
  validator; the rest follows `_conclude_protee`'s order on a fresh session.

| | POC (vx14 draft, T36) | vx13 reference (3 shots, 2026-10-07; same shot not available) |
|---|---|---|
| Input tokens | 13,093 | 11,695–11,752 |
| Image tokens | 4,916 (gpt-5-mini 2048² rule, measured 2026-10-07) | 4,071 |
| Output tokens | 1,183 | 1,091–1,150 |
| Reasoning tokens | 0 (effort `minimal`) | — |
| OpenAI wall clock | 13.88 s | 10.2–13.2 s |
| Strike → verdict | ≈19.0 s: capture 4.53 + sheet render 0.55 + call 13.88 + conclusion <0.01 | comparable capture + call |
| Cost | $0.0056 | ≈ $0.005 |
| Checkpoints accepted | 22 of 59 scoreable (53 non-null entries, 31 rejected) | 46–49 |
| Swing Score | none (coverage too thin) | 74–77 |

The three saved 2026-10-08 shots were diagnostic dry runs, so none has a vx13
result. A second call to make one was not allowed.

## 3. Localisation against the full-resolution originals

The draft schema had no per-position confidence. The rule was fixed before the
call: **correct** = the original is in the by-eye accepted set; **acceptable**
= the sheet neighbour of a correct cell; **wrong** otherwise. The visual notes
come from `output/poc/originals_*.jpg`.

| P | DTL cell (orig.) | Visual | Verdict | FO cell (orig.) | Visual | Verdict |
|---|---|---|---|---|---|---|
| P1 setup | DTL_01 (#15, +0.00) | address | correct | FO_01 (#15) | address | correct |
| P2 takeaway | DTL_03 (#21, +0.58) | shaft parallel going back | correct | FO_03 (#19, +0.41) | early takeaway | correct |
| P3 backswing | DTL_06 (#28, +1.29) | hands already at the top; P3 is DTL_04 (#24) | **wrong** | FO_06 (#27, +1.18) | near the top; P3 is FO_05 (#24) | acceptable |
| **P4 top** | DTL_07 (#30, +1.43) | top | **correct** | FO_07 (#29, +1.38) | top reached | acceptable (correct in substance) |
| **P5 transition** | DTL_09 (#37, +1.80) | first move down, hands at shoulder | acceptable (#38/39 better) | FO_09 (#37) | start down | **correct** |
| **P6 delivery** | DTL_11 (#39, +1.89) | hands at chest, coming down | **correct** | FO_11 (#39) | hands at hip | **correct** |
| **P7 impact** | DTL_14 (#42, +2.03) | first still after the ball; impact is DTL_13 (#41) | acceptable, **one still late** | FO_14 (#42) | after the ball; impact is FO_13 (#41) | acceptable, **one still late** |
| **P8 release** | DTL_16 (#44, +2.14) | club vertical in follow-through; release is DTL_15 (#43) | acceptable, **late** | FO_16 (#44) | arms up; release is FO_15 (#43) | acceptable, **late** |
| P9 finish | DTL_18 (#51) | finish | correct | FO_18 (#51) | finish | correct |

- **Totals:** 10 correct, 7 acceptable, 1 wrong. Every pick is in time order
  on both cameras, so the validator raised nothing.
- **Critical positions P4–P8:** 5 correct and 5 acceptable. The exact impact
  still was missed on **both** cameras, and impact and release were
  consistently one still late.
- **Citations vs LOCATE:** the checkpoint citations disagree with the model's
  own LOCATE answer. Impact checkpoints cite DTL_12 / FO_11 (one to two stills
  *early*), release cites DTL_13 / FO_13, and takeaway, backswing, delivery and
  finish all cite the neighbour of the located cell. 27 entries were refused
  as `phase_unknown` (cited a cell it never located) and 4 as `phase`. That
  is why coverage collapsed. Both answers bracket the truth within one still;
  neither is exact.

## 4. What CaddieJ concluded

- **Accepted checkpoints by phase:** setup 7, backswing 2, top 6,
  transition 5, delivery 2. None for takeaway, impact, release or finish.
- **Swing Score:** none (mechanics coverage too thin). ProTee delivery value 91.
- **Faults inferred:** none. **Root cause:** none. **Lesson:** none.
- **Metrics:** 22 read, 7 nulled. Impact height was read as "in" where the
  grid shows mm. That is an All Values read issue; the strip is the same size
  as vx13's.
- **Coach line:** the model proposed TOO_STEEP ("Your attack angle was steep
  at impact. Shallow the club slightly in the downswing."). It is not
  admissible on these numbers, so CaddieJ reconciles to its own
  **FACE_CONTROL_GOOD** reinforcement. The guardrails worked; the cue choice
  is unrelated to the temporal evidence.

## 5. Decision: B. TEMPORAL PROMISING

The architecture works at all. With no phase labels, the model placed all
nine positions in order on both cameras, never more than one still from the
truth except DTL P3, and got the top and delivery right. It is not ready for a
broader benchmark as drafted, for three reasons:

- Impact is one still late on both views.
- Its citations contradict its own LOCATE answer, which costs more than half
  the checkpoints and the Swing Score.
- P3 drifted to the top on DTL.

Refinements to make before a multi-shot benchmark (none implemented):

1. Remove the cell from single-phase checkpoint entries. Take it from LOCATE
   instead, so a reply cannot contradict itself; this also shortens the output.
2. Define impact as a visible test (club at the ball, ball still on the mat)
   and release as shaft parallel past the ball.
3. Add a per-position confidence to LOCATE.
4. Benchmark reasoning effort `low` against `minimal`: 0 reasoning tokens were
   spent locating 18 positions in 36 cells.

## Files added for the POC (all under `research/temporal_evidence/`)

- `replay_anchor.py`: ProTee log and capture-clock evidence for §1.
- `protee_clips/`: shot 6's two ProTee clips, copied before ProTee rotated
  them out.
- `_deps/`: a throwaway `imageio-ffmpeg` binary used only to read the clip
  metadata.
- `db_peek.py`, `db_vx13.py`: read-only database lookups.
- `poc_vx14.py`: request build, the one call, and the analysis.
- `poc_citations.py`, `poc_originals.py`: citation audit and full-resolution
  tiles.
- `output/poc/`: request text and schema, `reply.json`, `analysis.json`, and
  `originals_*.jpg`.
- `POC_REPORT.md`: this report.
- Edited: `REPORT.md` (impact-offset bullet) and the `generalisation.py`
  docstring.
