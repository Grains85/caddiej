# Coach V1 — technical specification

[← Back to the docs](README.md)

> **Status: implemented.** Coach V1 is in the production path. What remains is
> real-hardware validation of capture timing and visual-cue quality (§20).

One useful coaching observation after every meaningful shot: one focus, two to
four words on the floor, one or two spoken sentences, optionally one predefined
visual cue, and a memory of the session behind it.

The load-bearing claim of this design is that **CaddieJ already measures what is
measurable, and the only thing a model adds is language and one class of
judgement no number can make.** Everything below follows from keeping those two
apart.

---

## 1. What already exists

Coach V1 is mostly an extension, not a rewrite. The existing pieces:

| Piece | File | State |
| --- | --- | --- |
| Deterministic diagnosis | `coaching/diagnose.py`, `faults.py`, `baseline.py` | Complete. Names the fault from measurements alone, instantly, offline. |
| Session memory | `coaching/session.py` | Complete. Sticky focus, per-focus trends, improvement detection, learned-and-retired. |
| OBS screenshot burst | `swing_capture/service.py` | Works. Photographs whatever `SWING_CAPTURE_PANELS` lists after `ShotDetected`. Delivery is optional. Newest-shot-wins. One source failing does not drop the others. |
| Deterministic frame selection | `swing_capture/select.py` | Complete. Byte-difference activity trimming, one timeline across cameras, no JPEG decoder. |
| Atomic media replacement | `swing_capture/store.py` | Complete. Stage under a hidden name, publish by directory rename. |
| Provider boundary | `coaching/provider.py`, `adapters/openai/provider.py` | Complete. Responses API, strict JSON schema, images as low-detail data URLs. |
| Reply validation | `coaching/validate.py` | Complete for speech. Needs extending for cues. |
| Evidence rendering | `coaching/evidence.py` | Good epistemics already; needs provenance made explicit. |
| Timed OBS reveal | `obs_reveal/timed_source.py` | Complete and **unchanged by this design**. |

The gaps Coach V1 fills: evidence provenance is implicit rather than structured;
there is no cue taxonomy; putting is not separated; and
nothing expresses calibrated confidence.

---

## 2. Event flow

No new events. Two existing events change shape.

```
ProTee log                     GSPro currentRound.dat
    │                                   │
    ▼                                   ▼
ShotDetected(trigger_id)        RoundShotObserved
    │                                   │
    ├─────────────┐                     ▼
    │             │                PlayFlow FIFO join
    ▼             ▼                     │
swing_capture  obs_reveal               ▼
 (burst)       (reveal)         ShotCompleted(shot, trigger_id)
    │                                   │
    ▼                                   ▼
SwingFramesReady(capture)  ────►  CoachingService
                                        │
                        ┌───────────────┼───────────────┐
                        ▼               ▼               ▼
                  ShotDiagnosed   AnalysisStarted   AnalysisCompleted
                   (instant)        (dropped)       (advice + cue)
                        │                               │
                        ▼                               ▼
                    PlayFlow ──► floor            SpeechService
```

The deterministic line is on the floor at `ShotDiagnosed`, which is synchronous
with `ShotCompleted`. Everything after it is enrichment and may fail.

**Changed:** `SwingFramesReady.capture` becomes a multi-panel `CoachShotEvidence`
rather than a two-camera `SwingCapture`. **Changed:** `AnalysisCompleted.advice`
gains `cue`, `confidence` and `confidence_source`.

**Not changed:** the delivery policy table. `swing_capture` stays `DROP` on
`ShotDetected`, `coaching` stays `DROP` on everything, `play` and `speech` stay
`COALESCE` on `AnalysisCompleted`. Coaching is decoration and its queues must be
the ones that shed load.

---

## 3. Shot correlation

Unchanged, and it is already correct. `ShotDetected.trigger_id` is ProTee's own
trigger number. `PlayFlow` joins it FIFO to the GSPro result and carries it on
`ShotCompleted.trigger_id`. The capture is keyed on the same trigger, and
`CoachingService._job_for_trigger` matches them.

The ordering on real hardware, from the measured 2026-09-16 session: GSPro's
result arrives **2.6 to 9.3 seconds** after the trigger, and ProTee has its
replay on screen well before that. So the capture is normally finished before
the shot it explains has been observed at all. That is why `CoachingService`
holds exactly one unclaimed capture in `self._capture` and hands it to the first
job that arrives with a matching trigger. **Keep that.**

One consequence worth stating: a shot with no `trigger_id` — which is every shot
in `interpose` and `simulate` modes — can never be matched to a capture. It
coaches from numbers only. That is correct and not a degradation to fix.

---

## 4. OBS capture mechanism — the decision

**Chosen: screenshot burst over the existing OBS WebSocket, followed by
deterministic frame selection.** Short OBS recording is rejected.

This is a single architecture, not two. "Recording followed by frame selection"
is a superset of "recording" and is rejected with it.

### Why recording is rejected

- **It mutates OBS output state.** `StartRecord`/`StopRecord` hijacks the
  operator's own recording, and the replay buffer is a global OBS resource. OBS
  is master; a decoration subsystem may not take its outputs.
- **It needs a decoder in the coaching path.** The model consumes images, not
  video, so any recording is demuxed back to stills. FFmpeg is already an
  optional dependency for the *owned camera* pipeline; making it mandatory for
  coaching on a bay that has no camera is a real cost for no gain.
- **It cannot be abandoned cheaply.** Newest-shot-wins is the hardest constraint
  in this subsystem. A screenshot burst is cancellable at frame granularity: the
  generation is checked every tick and the loop simply stops. A recording in
  flight must be stopped, flushed, found on disk and deleted — on Windows, while
  something may still hold the handle.
- **It loses CaddieJ's clock.** Each screenshot is stamped on CaddieJ's own
  monotonic clock at request time. A recording's timestamps are OBS's, and
  relating them to `ShotDetected` means demuxing before knowing anything.
- **Frame selection already works without a decoder.** `select.py` compares
  encoded bytes. A recording would force pixel decoding into the selection path.

### How the burst is pointed at ProTee's presentation

CaddieJ asks OBS for a screenshot of a **named panel**. A panel is an OBS source
*or scene* name — `GetSourceScreenshot` accepts either. That is the whole
mechanism, and it is what keeps OBS master of layout:

> **The operator defines each panel as a scene in OBS that crops the ProTee
> window however they like. CaddieJ never crops, scales, or positions anything.
> It names panels and asks for pictures of them.**

A typical bay configures three panels against one ProTee Window Capture:

| Panel | What the operator crops it to | Kind |
| --- | --- | --- |
| `swing` | the swing replay area (one or both camera views) | dynamic |
| `impact` | the Top Impact / face-strike graphic | static |
| `delivery` | the club path / impact visualisation and shot info | static |

Panel names are CaddieJ's; the OBS scene names behind them are the operator's.
ProTee is kept at a stable screen and resolution, as required — CaddieJ neither
knows nor cares which display it is on, but the operator's crops do, which is
why the constraint exists and why it belongs in the runbook rather than in code.

---

## 5. Capture timing

The ~3 second hypothesis is close, but a single window is the wrong shape,
because the panels have different temporal behaviour. The replay moves; the
Top Impact graphic is drawn once and then sits there.

**A capture is a plan, not a window.** Each panel declares a delay, a duration
and a rate. One task, one generation, one atomic publish — the existing
structure, with a per-panel schedule instead of one global one.

| Panel kind | Delay | Duration | Rate | Stills | Why |
| --- | --- | --- | --- | --- | --- |
| dynamic (`swing`) | 0.5 s | **3.0 s** | 10 fps | ~30 | Covers ProTee's ~2.5 s replay with margin at both ends. The existing 4.0 s default is trimmed to 3.0 s to make room for the static pass. |
| static (`impact`, `delivery`) | 3.5 s | 1.5 s | 2 fps | 3 each | After the replay has finished drawing. Three stills is enough to tell "settled" from "still rendering", and the dedup below collapses them to one. |

**Total wall time: ~5.0 s from `ShotDetected`.** That is the number that matters,
because it must fit inside the coaching worker's wait. With
`COACHING_CLIP_WAIT=8.0` there is three seconds of headroom, and GSPro's result
typically has not even arrived yet. `COACHING_CLIP_WAIT` does not change.

These remain configuration with a first guess for a default, and every capture
logs what it actually did, exactly as today. **The timing has still not been
measured against a real replay**, and the hardware validation in §17 is how it
gets corrected.

---

## 6. Frame selection

`select.py` is reused unchanged for dynamic panels: trim leading and trailing
stills that are byte-identical to their neighbour, spread targets across the
active interval, take the nearest still per target, drop repeats. The fallback —
a uniform spread across everything captured — stays the normal case rather than
the error case.

**One addition for static panels: `settled_frame()`.** A static panel wants the
opposite question. Walk backwards from the last still and return the first one
that is byte-identical to its predecessor; that is the panel after it stopped
rendering. If no two stills match, return the last one and mark it unsettled.
Deterministic, no decoder, three lines of the same byte comparison.

Budget per capture, enforced before anything reaches a model:

| Panel kind | Frames kept |
| --- | --- |
| dynamic | 4 |
| static | 1 each |

Six images maximum per shot. At OpenAI's `low` detail that is a fixed, small
token cost, and a swing replay does not become more legible at eight frames than
at four.

---

## 7. Evidence model

`CoachShotEvidence` is what the coaching worker assembles and what
`evidence.py` renders. Its purpose is to make provenance **structural rather
than prose**, so that no consumer — model or human — can mistake one grade for
another.

```python
class EvidenceGrade(StrEnum):
    MEASURED = "measured"  # a device reported this number
    DERIVED = "derived"  # CaddieJ's arithmetic over measured numbers
    VISUAL = "visual"  # only the pictures support this, and never a number
    TREND = "trend"  # arithmetic across this session's shots
    ABSENT = "absent"  # explicitly not available on this shot
```

```python
@dataclass(frozen=True, slots=True)
class CoachShotEvidence:
    shot_id: UUID
    trigger_id: str | None

    # MEASURED / DERIVED — the existing types, unchanged.
    observations: tuple[Observation, ...]
    candidates: tuple[FocusCandidate, ...]
    club: ClubKind
    handedness: Handedness
    baseline_shots: int

    # TREND
    progress: SessionProgress
    focus_shots: int
    improving: bool
    recent_lines: tuple[str, ...]
    previous_focus: CoachingFocus | None

    # VISUAL
    panels: tuple[EvidencePanel, ...]

    # ABSENT — the anti-hallucination device. See below.
    unmeasured: tuple[str, ...]

    # Provenance of the pictures themselves.
    capture_id: UUID | None
    capture_started_at: datetime | None
    capture_complete: bool
```

```python
@dataclass(frozen=True, slots=True)
class EvidencePanel:
    panel_id: str  # "swing" | "impact" | "delivery"
    kind: PanelKind  # DYNAMIC | STATIC
    supports: frozenset[VisualClaim]
    frames: tuple[SwingFrame, ...]
    settled: bool  # static panels only
    interval: ActiveInterval | None  # dynamic panels only
```

### The two rules that make this worth having

**`unmeasured` is populated, not omitted.** Every fault the device did *not*
report on this shot is listed by name. A bay in `observe` mode gets ball data
and no club delivery, so `unmeasured` reads `club path, face angle, angle of
attack, impact position`. The prompt states them. A model told what is missing
does not invent it; a model told nothing fills the gap plausibly and wrongly.
This is the single highest-value addition in the evidence model.

**A value visible on screen is `VISUAL`, never `MEASURED`.** ProTee draws
numbers in its own panels. Those numbers are pictures. Where CaddieJ has the
same value structurally — from the ProTee log or GSPro — the structural one is
authoritative and the picture adds nothing. Where CaddieJ does *not* have it
structurally, **it is still not a metric**: it is not read, not transcribed, and
not inferred from a swing picture. The swing frames are never read for text.

The one exception is deliberate and labelled: on the ProTee-only path (section
23) ProTee's own **All Values** grid is photographed once per strike and the
model reads it, in the same request, into a typed and individually nullable
`metrics` object. That is a visual reading of ProTee's display, stored with
provenance `protee_vx_all_values_visual`, and never confused with a structural
measurement.

---

## 8. Cue taxonomy

A **focus** is a category to work on (the existing `CoachingFocus`). A **cue** is
one specific, nameable lesson with a stable identity, a picture, and rules about
when it may be used. Many cues map to one focus.

```python
@dataclass(frozen=True, slots=True)
class CoachCue:
    cue_id: CueId
    focus: CoachingFocus
    title: str  # "Open face"
    display: str  # "CLOSE THE FACE" — 2-4 words, ≤18 chars
    explanation: str  # one paragraph, for the HUD and docs
    grade: EvidenceGrade  # MEASURED or VISUAL. Nothing else.
    requires: EvidenceRequirement
    contraindicated_by: tuple[EvidenceRequirement, ...]
    clubs: frozenset[ClubKind]
    asset: str | None  # instructional image id, optional
```

### Admissibility is computed by CaddieJ, not chosen by the model

This is the existing `permitted_focuses` / `legal_displays` pattern extended one
level. Before any request is built, CaddieJ evaluates every cue against the
evidence and produces the **admissible set**. The model's JSON schema enumerates
exactly that set. A cue whose requirement is unmet cannot be named, because it is
not in the schema.

- `requires` is satisfied only by evidence of the cue's own grade. A `MEASURED`
  cue needs its metric present, with the right sign, above tolerance. A `VISUAL`
  cue needs a panel whose `supports` set contains the claim.
- `contraindicated_by` removes a cue that the numbers rule out. `EARLY_EXTENSION`
  is contraindicated by a centred, clean strike; `SWAY_OFF_BALL` is
  contraindicated by a strike pattern that did not move.
- `clubs` is the putting gate. See §9.

### The V1 cue set

**Measured cues** — entailed by a metric, admissible only when that metric was
reported. Confidence is CaddieJ's arithmetic.

| Cue | Focus | Requires |
| --- | --- | --- |
| `FACE_OPEN` | `face_control` | `face_to_target` above tolerance, player-oriented |
| `FACE_CLOSED` | `face_control` | `face_to_target` below tolerance |
| `PATH_OUT_TO_IN` | `path` | `club_path` negative beyond tolerance |
| `PATH_IN_TO_OUT` | `path` | `club_path` positive beyond tolerance |
| `STRIKE_TOE` | `strike` | `impact_horizontal_mm` toward the toe |
| `STRIKE_HEEL` | `strike` | `impact_horizontal_mm` toward the heel |
| `TOO_STEEP` | `low_point` | `angle_of_attack` below the club's ideal |
| `TOO_SHALLOW` | `low_point` | `angle_of_attack` above the club's ideal |
| `START_PUSHED` | `start_line` | `launch_direction` beyond tolerance |
| `START_PULLED` | `start_line` | `launch_direction` beyond tolerance |
| `CURVE_SLICE` | `curve` | `spin_axis` beyond tolerance |
| `CURVE_HOOK` | `curve` | `spin_axis` beyond tolerance |
| `SPEED_RUSHED` | `speed` | `club_speed` above the player's own mean |
| `SCATTER` | `consistency` | ≥5 shots with this club, spread above band |

**Visual cues** — admissible only when frames were sent and a panel supports the
claim. Confidence comes from the model and is capped.

| Cue | Focus | Panel must support | Contraindicated by |
| --- | --- | --- | --- |
| `EARLY_EXTENSION` | `early_extension` | `BODY_THROUGH_IMPACT` | centred strike + clean low point |
| `SWAY_OFF_BALL` | `sway` | `BODY_IN_BACKSWING` | strike position unchanged from baseline |
| `REVERSE_PIVOT` | `sway` | `BODY_IN_BACKSWING` | — |
| `POSTURE_LOST` | `posture` | `WHOLE_BODY` | — |
| `BALANCE_FINISH` | `posture` | `FINISH_POSITION` | — |
| `TEMPO_RUSHED` | `tempo` | `SEQUENCE` (dynamic panel only) | — |

**Reinforcement cues** — one per focus, the existing `REINFORCEMENT_DISPLAYS`
table promoted into cues with `kind = REINFORCEMENT`. A good shot gets a named
thing to repeat, which is what makes it worth talking about.

### What is deliberately not in the taxonomy, and why

The candidate list mixes three kinds of thing. These are rejected:

- **`OVER_THE_TOP`** — folded into `PATH_OUT_TO_IN`, which measures it. As a
  *visual* cue it is rejected: judging the plane needs a known camera angle, and
  CaddieJ does not know where ProTee's cameras are standing.
- **`CHICKEN_WING`, `LEAD_WRIST`, `TRAIL_SHOULDER_DOWN`** — each needs a
  specific body part at a specific moment. The capture has no frame-to-position
  mapping; `evidence.py` already tells the model "no swing position has been
  detected, so do not treat one as address, the top, or impact." A cue that
  requires a position the evidence cannot locate is a cue that will be guessed.
- **`HIP_ROTATION`** — same reason, plus it is a degree claim by nature.
- **`HEAD_MOVEMENT`** — merged into `SWAY_OFF_BALL`. Same evidence, same
  instruction, and two cues competing for one observation is how a taxonomy
  starts producing arbitrary choices.
- **`WEIGHT_TRANSFER`** — not directly visible; it is inferred from pivot and
  finish, which are `REVERSE_PIVOT` and `BALANCE_FINISH`. Kept as the two things
  a picture can actually show.
- **`BALL_POSITION`** — a setup fact. The capture begins after the strike and
  the replay may not include address at all. Nothing in V1 can see it.

Fifteen admissible corrections is already more than a player will meet in an
evening. A taxonomy is judged by what it refuses.

---

## 9. Putting

**Putting is in Coach V1, as a separate and much smaller thing.**

Today this is a live hazard rather than a gap. Most faults in `faults.py` have
`ideals=None`, so `ideal_for()` returns `0.0` for *every* club including the
putter, and a putt with a few degrees of spin axis will currently be diagnosed
`CURVE_SLICE`. Only `low_point` is safe, because `IDEAL_ATTACK` has no `PUTTER`
key and an absent key means "not ranked".

The fix is the cue's `clubs` field, applied as a hard gate:

- Every full-swing cue declares `clubs` that **excludes** `ClubKind.PUTTER`.
- Every visual cue excludes the putter too, and `COACHING_SEND_FRAMES` is
  skipped entirely for putts. A replay screenshot of a putting stroke does not
  support any claim worth a token, and not asking is free.
- A putt therefore reaches a three-cue set and nothing else.

**Putting cues:**

| Cue | Focus | Requires |
| --- | --- | --- |
| `PUTT_FACE_OPEN` / `PUTT_FACE_CLOSED` | `face_control` | face angle at impact, tighter tolerance than full swing |
| `PUTT_STRIKE_TOE` / `PUTT_STRIKE_HEEL` | `strike` | impact position, tighter tolerance |
| `PUTT_PACE_LONG` / `PUTT_PACE_SHORT` | `speed` | ball speed against the target distance GSPro reported |

Face angle dominates a putt's start direction, so a tighter tolerance on it is
the whole of the useful coaching. Pace is the second thing and needs the target
distance, which the ProTee observer already publishes as
`TargetDistanceObserved`.

**Detecting a putt:** club selection, not GSPro's flag. `floor/putting.py`
documents that `isPutt` was false on all 196 shots of the real capture, and
`ClubSelected` → `ClubKind.PUTTER` is what the floor already trusts. Coach V1
uses the same signal, from `Shot.club`.

**Floor:** the putting lane and putt path are already drawn. The advice line sits
at the existing advice anchor and does not move. No cue graphic is drawn during
putting.

---

## 10. OpenAI request

The provider (`adapters/openai/provider.py`) is **unchanged**. Responses API,
strict `json_schema`, images as base64 data URLs at `detail: "low"`, SDK retries
off. All changes are in `prompt.py`.

### Request shape

```
instructions:  SYSTEM (revised, see below)
input[0].content:
  input_text:  INSTRUCTIONS
  input_text:  rendered CoachShotEvidence
  input_image: ×0-6, low detail
text.format:   json_schema, strict, narrowed per shot
max_output_tokens: 120
timeout: COACHING_TIMEOUT (8.0)
```

`max_output_tokens` rises from 60 to 120 because the reply now carries a cue id
and a confidence alongside the sentence. Still small enough that an incomplete
response means something is genuinely wrong.

### What the system prompt gains

Three paragraphs, all narrowing:

1. **Cue selection, not cue invention.** "You are choosing one lesson from a list
   CaddieJ has already decided is supported by the evidence. You may not name a
   lesson that is not in the list, and the list is not negotiable."
2. **The picture ban.** "Do not read any text, number, graph, dial or readout in
   the images. They are pictures of a screen and anything written on it is
   already known to CaddieJ from a better source. Describe only body shapes and
   movement."
3. **Absence is information.** "You are told which measurements were not taken.
   Do not reason about them, refer to them, or choose a lesson that depends on
   them."

The existing rules stay verbatim: one idea, no numbers, no left or right, never
mention the camera or the data, reinforcement names what to repeat.

### Cost and latency

Unchanged in shape. Six low-detail images is a fixed token cost; the evidence
block is a few hundred tokens; the reply is under a hundred. Existing guards all
apply and none change: `COACHING_MIN_INTERVAL=8.0`, `COACHING_CALL_LIMIT=60`,
`COACHING_BUDGET_SECONDS=20.0`, `COACHING_RETRY_LIMIT=1`.

The cache stays and gets one more key part. `fingerprint_parts` currently keys on
prompt version, model, club, focus, band, display, frames-yes-no and kind. It
gains `cue` and loses `display` (which becomes a property of the cue). A cached
line is still a line for the same lesson at the same severity band with the same
kind of evidence behind it.

---

## 11. Structured response

### The schema

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["cue", "speech"],
  "properties": {
    "cue":    { "type": "string", "enum": ["<admissible cue ids for this shot>"] },
    "speech": { "type": "string", "maxLength": 90 },
    "visual_confidence": { "type": "number", "minimum": 0, "maximum": 1 }
  }
}
```

`visual_confidence` is present in the schema **only when the admissible set
contains at least one visual cue**. A model that was sent no pictures is never
offered the field.

### What the model does not get to decide

This is the part that prevents unsupported certainty, and it is mostly
subtraction:

- **`display` is removed from the reply entirely.** It is a property of the cue,
  and the cue comes from a closed set, so the display is a lookup. This deletes
  `legal_displays`, the whole `_display` validation branch, and one failure mode.
- **`focus` is removed.** It is the chosen cue's focus.
- **`kind`** (correction or reinforcement) stays CaddieJ's, derived from severity
  against `SEVERITY_FINE`. A model able to write one word that turns an open face
  into praise is a model that will eventually write it.
- **`severity`** stays CaddieJ's for any measured cue. For a visual cue, severity
  is quantized as it is today (`_OBSERVED_STEPS`).

### Confidence

```python
class ConfidenceSource(StrEnum):
    DERIVED = "derived"  # CaddieJ's arithmetic over measured evidence
    MODEL_CAPPED = "model_capped"  # the model's, quantized and ceilinged
```

**For a measured cue, confidence is derived and the model is not asked.**

```
base                         0.50
+0.20  baseline_shots >= 10        (judged against this player, not a table)
+0.20  severity >= SEVERITY_FAULT  (clearly outside the band, not marginal)
+0.10  the metric was reported directly rather than derived
clamp to [0.30, 0.95]
```

**For a visual cue, the model's value is quantized to (0.30, 0.45, 0.60) and
capped at `VISUAL_CONFIDENCE_CEILING = 0.60`.** Nothing read from an
uncalibrated screenshot of a replay, whose camera position is unknown and whose
frames map to no swing position, deserves more. A visual cue that quantizes below
0.30 is **rejected**, and the best measured cue is used instead.

The ceiling is the schema-level answer to "prevent unsupported certainty": a
visual claim can never out-rank a measured one, because it structurally cannot
reach the same number.

### Resulting advice

```json
{
  "kind": "correction",
  "cue": "FACE_OPEN",
  "focus": "face_control",
  "display": "CLOSE THE FACE",
  "speech": "Good path. Get the knuckles down a little earlier.",
  "severity": 0.71,
  "severity_source": "measured",
  "confidence": 0.90,
  "confidence_source": "derived",
  "evidence": [ /* graded, see §7 */ ]
}
```

Speech validation stays strict: ≤90 characters, ≤18 words, ≤2 sentences,
exactly one instruction, no vocabulary from another focus, no digits, no left or
right, no semicolons. See §23 for how a restating second sentence is counted.
A refused reply leaves the templated line standing, which was already on the
floor.

---

## 12. Session intelligence

`coaching/session.py` needs **no structural change**. It already does what is
asked: one sticky focus, adopted on correction and kept until another fault is
markedly worse (`PREFERENCE_TOLERANCE = 0.15`); per-focus trends over a five-shot
window; improvement called only with three samples and a 0.15 margin; a focus
retired after three clean shots; and the last three spoken lines fed back so the
same sentence is not said twice.

Three small additions:

1. **Cue-level repetition.** The session remembers the last cue per focus. The
   same cue twice running is allowed — that is a lesson being taught — but the
   third consecutive identical cue moves the *speech* to a variant. The cue does
   not change; the wording does. A coach repeats the lesson, not the sentence.
2. **Visual cues do not adopt a focus.** Only a measured correction sets the
   sticky focus. A single visual observation is the weakest evidence in the
   system and must not be able to redirect a session.
3. **Putting is a separate track.** Putts do not enter the full-swing focus or
   trends, and full-swing shots do not enter the putting track. Mixing them would
   let an evening of putting retire a face-control focus nobody worked on.

"One poor swing should not constantly change coaching focus" is already
guaranteed by `PREFERENCE_TOLERANCE` plus the fact that severity is measured
against both the player's own mean and an absolute band.

---

## 13. Persistence

Schema **v3**. One migration, additive, in the existing ordered `MIGRATIONS`
list with its own transaction.

`shot_advice` gains:

| Column | Content |
| --- | --- |
| `cue_id` | The chosen cue, or null for a templated line with no cue |
| `confidence` | 0–1 |
| `confidence_source` | `derived` / `model_capped` |
| `evidence_grades` | Compact string, e.g. `measured=4,derived=2,trend=1,visual=6,absent=3` |
| `capture_id` | The capture the pictures came from, or null |
| `panels_used` | Comma-separated panel ids |

**No image paths are stored, deliberately.** `media/latest_swing` holds exactly
one capture and is replaced whole on the next shot, so a stored path would be a
dangling reference within seconds. `capture_id` plus panel ids is enough to
correlate a row with the log lines of the capture that produced it, which is what
a session review actually needs. This is different from `clip_path`, which points
at a retained file from the owned-camera pipeline and stays.

Every diagnosed shot still gets a row, including good ones and failed ones.

Schema **v5** adds the Swing Score to `coach_results`; see
[Swing Score](swing-score.md).

---

## 14. Media lifecycle

Unchanged in principle: **one capture on disk, replaced whole, no archive.**

```
media/latest_swing/
  metadata.json
  swing/     frame_001.jpg … frame_004.jpg
  impact/    frame_001.jpg
  delivery/  frame_001.jpg
```

The store's staging-and-rename publication is reused verbatim; the only change is
that the per-camera loop becomes a per-panel loop. `prepare()` still sweeps
`.latest_swing.staging-*` and `.latest_swing.discard-*` left by a crash.

`metadata.json` gains `panel_id`, `kind`, `settled` and `supports` per panel, and
keeps the existing rule that **no shot measurements are copied into it** — those
belong to history and a second copy is a second place to be wrong.

Retention is not needed and is not added. The directory does not grow.

---

## 15. Failure and degradation

The tiers, worst outcome first, and every one of them keeps golf running:

| Failure | Result |
| --- | --- |
| No provider configured | Templated line. Floor and speech unaffected. |
| Provider times out, rate-limits, errors, or is over budget | Templated line, `AnalysisFailed` (DROP), row stored with the reason. |
| Reply invalid, speech rejected, or visual confidence below floor | Templated line, or the best measured cue. |
| OBS down / panel name wrong | Capture is barren. Coaching proceeds from numbers. After three barren captures the subsystem stands down for ten shots, then tries again. |
| Capture incomplete (some panels empty) | `capture_complete=False`. The panels that worked are sent; the missing ones go in `unmeasured`. |
| Capture arrives after the coaching worker gave up | Ignored. The shot is already coached. |
| No club delivery data (`observe` mode) | Ball-only cues. Club cues never enter the admissible set. |
| No trigger id (`interpose`, `simulate`) | Numbers only, no pictures. |
| Putter | Three putting cues, no pictures, no model call if nothing is outside tolerance. |

The invariant behind all of them: **the deterministic line is on the floor before
any of this can happen**, so every failure above is invisible to the player.

---

## 16. Cancellation and newest-shot-wins

Unchanged, and it is already right in three places that each need it:

- **Capture** (`swing_capture/service.py`): a generation counter bumped on every
  `ShotDetected`, checked every tick of the burst and again inside the publishing
  lock, so a capture cancelled a moment too late cannot overwrite a newer swing.
- **Coaching** (`coaching/service.py`): `_is_stale(job)` after the footage wait,
  failing the job as `SUPERSEDED`. Late coaching is worse than none.
- **Speech** (`speech/service.py`): `ShotCompleted` clears queued perishable
  coaching and cancels a render that has not begun speaking. A line already being
  spoken is allowed to finish.

One new rule: **a capture may not be published for a trigger the coaching service
has already settled.** Today an orphan capture simply sits in the one-slot hold
and is overwritten. That stays, and the only addition is that the hold is cleared
when its trigger is settled, so a stale capture cannot be handed to a later job
that happens to reuse a trigger id after a ProTee restart.

---

## 17. Floor, speech, diagnostics

### Floor

The advice line is unchanged: 2–4 words, ≤18 characters, coloured by
`AdviceBand`, at the existing advice anchor, for `COACHING_DISPLAY_SECONDS`
(9.0). The display now comes from the cue table rather than from the model.

**The cue graphic is specified but not enabled in V1.** `CoachCue.asset` exists
and is stored, and the HUD gains an optional cue image slot at the advice anchor,
but it ships **off** behind `FLOOR_COACH_CUE_IMAGE=false`. Reason: the floor is
projected onto a hitting mat and read at a glance from standing height by
somebody about to swing. A diagram there is a change to the thing a player looks
at while addressing a ball, and it should be turned on deliberately after the
text-only version has been lived with.

`FloorAdvice.pending` already exists in the state, the protocol and the CSS, and
nothing sets it. Coach V1 sets it: `AnalysisStarted` gains a `play` subscriber
that shows the pending state, so the floor says a better line is coming rather
than looking finished. This is a two-line change to an unused affordance.

### Speech

Unchanged. `AnalysisCompleted` → `SpeechService.say` at `COACHING` priority,
perishable, superseded by the next `ShotCompleted`. The validated ≤90 character
line fits inside the 300 character queue limit with room to spare.

One gap worth naming: **a templated line is never spoken today**, because speech
listens to `AnalysisCompleted` and a template-only path publishes only
`ShotDiagnosed`. On a bay with no provider, CaddieJ is silent. That is arguably
correct — but it should be a decision, not an accident. **Recommendation: leave
it.** A synthesised voice reading a canned sentence after every shot wears out
faster than silence does, and the floor already carries the line.

### Diagnostics

Extending the existing `--check-*` family, which is how every other subsystem is
validated:

- `--check-swing-capture` gains per-panel reporting: resolved or missing, stills
  returned, bytes, whether the panel settled, and the wall time of the plan.
- `--check-coach-cues` is new and offline: renders the cue table, and for a
  synthetic shot prints the admissible set with the reason each cue was admitted
  or excluded. This is the tool that answers "why did it say that", which is the
  question a cue taxonomy exists to make answerable.
- `/healthz` gains `coaching.cues_admitted`, `coaching.visual_cues_chosen` and
  `swing_capture.panels`.

---

## 18. Configuration

Replacing the two camera settings with a panel list:

| Setting | Default | Purpose |
| --- | --- | --- |
| `SWING_CAPTURE_PANELS` | `swing_behind,swing_side,impact,delivery` | Which panels to capture, in order. The list is authoritative. A panel left out, including `delivery`, is not captured. `swing` aliases to `swing_behind`. |
| `SWING_CAPTURE_PANEL_<ID>_SOURCE` | — | The OBS source or scene name for a panel. |
| `SWING_CAPTURE_PANEL_<ID>_KIND` | `dynamic` | `dynamic` or `static`. |
| `SWING_CAPTURE_PANEL_<ID>_SUPPORTS` | — | Comma-separated visual claims this panel can support. |
| `SWING_CAPTURE_DYNAMIC_DELAY_SECONDS` | `0.5` | Delay before a dynamic burst. |
| `SWING_CAPTURE_DYNAMIC_DURATION_SECONDS` | `3.0` | Dynamic burst length. Was 4.0. |
| `SWING_CAPTURE_STATIC_DELAY_SECONDS` | `3.5` | Delay before the static pass. |
| `SWING_CAPTURE_STATIC_DURATION_SECONDS` | `1.5` | Static pass length. |
| `SWING_CAPTURE_STATIC_FPS` | `2.0` | Static sample rate. |
| `COACHING_MAX_IMAGES` | `6` | Hard ceiling on images per request. |
| `COACHING_VISUAL_CUES` | `true` | Allow visual cues at all. |
| `FLOOR_COACH_CUE_IMAGE` | `false` | Draw the cue graphic on the floor. |

`SWING_CAPTURE_FPS` still names the dynamic screenshot rate.
`SWING_CAPTURE_SELECTED_FRAMES` is the total stills kept for one shot, not a
count per panel. `SWING_CAPTURE_CAMERA_1_SOURCE` and
`SWING_CAPTURE_CAMERA_2_SOURCE` are not read. `SWING_CAPTURE_PANEL_SWING_SOURCE`
remains a deprecated alias for the behind view, used only when the behind
source is empty.

`test_configuration_docs.py` already enforces that every setting appears in
`docs/configuration.md`, so the documentation is not optional.

---

## 19. Testing

The existing suite's weakness for this feature is that **every test that
exercises clip frames hands the extractor to the constructor**, which is exactly
how the dead `is_available` wiring survived. The new tests below are chosen to
cover what green tests currently do not.

**Deterministic, no hardware:**

- Cue admissibility is a pure function. A table-driven test per cue: the evidence
  that admits it, the evidence that excludes it, and the contraindication that
  removes it. This is the highest-value test in the feature.
- Every full-swing cue excludes `ClubKind.PUTTER`, asserted over the whole cue
  table rather than per cue, so a new cue cannot forget.
- A putt produces only putting cues, and sends no images.
- `unmeasured` names every fault the shot did not measure — asserted against an
  `observe`-mode shot with ball data only.
- Visual confidence quantizes and caps; below floor falls back to the measured
  cue; a visual cue never out-ranks a measured one.
- `settled_frame()` on a static panel: settled, unsettled, and single-frame.
- The capture plan's total wall time is under `COACHING_CLIP_WAIT`, asserted from
  the configuration rather than hard-coded, so raising a duration fails the test
  that says coaching will wait for it.
- Wiring, through the real `_build_coaching` and `_build_swing_capture`, that a
  configured panel reaches the coaching service. Same shape as the wiring test
  added after the `is_available` bug.

**Integration, in-process:**

- A full `ShotDetected → capture → ShotCompleted → AnalysisCompleted` run against
  a fake OBS and a scripted provider, asserting the floor line, the spoken line,
  the stored row and the cue.
- A second shot mid-capture cancels the first and the older capture never
  reaches the disk.
- OBS returns nothing for one panel of three: the other two are sent and the
  missing one is in `unmeasured`.

---

## 20. Real-hardware validation

Five things this design assumes and cannot verify in a test. Each needs one
session on the real bay, and each has a log line that answers it.

1. **When ProTee draws the replay, and for how long.** The entire timing table in
   §5 is a first guess. `swing_replay_interval_detected` with its start and end
   offsets answers it in one shot. If the interval consistently starts late,
   raise the dynamic delay; if it runs past the window, extend the duration.
2. **Whether the static panels have settled by 3.5 s.** `settled=false` on a
   static panel means the pass is too early.
3. **Whether `GetSourceScreenshot` on a cropped scene costs the OBS instance that
   draws the floor anything measurable.** Thirty screenshots in three seconds is
   the heaviest thing CaddieJ asks of OBS. Watch for floor frame drops during a
   capture; if there are any, the dynamic rate comes down before anything else
   does.
4. **Whether the capture reliably beats the GSPro result.** The whole design
   assumes it does, on the measured 2.6–9.3 s latency. A capture arriving after
   the coaching worker's wait shows as `coaching_footage_not_ready`.
5. **Whether visual cues are ever right.** The one thing no amount of
   architecture settles. Run a session with `COACHING_VISUAL_CUES=true` and
   review the stored rows against what actually happened. If the visual cues are
   noise, the ceiling comes down or the feature goes off — and the measured cues
   carry the product regardless, which is why they are the ones the floor has
   always shown.

---

## 21. Reuse, change, delete

**Reuse unchanged.**
`swing_capture/select.py` · `swing_capture/store.py` (per-panel loop aside) ·
`coaching/session.py` · `coaching/baseline.py` · `coaching/faults.py` measurement
math · `coaching/cache.py` · `coaching/validate.py` speech rules ·
`adapters/openai/provider.py` · `obs_reveal/timed_source.py` ·
`domain/event_policy.py` · the whole speech and floor delivery path.

**Change.**

| File | Change |
| --- | --- |
| `swing_capture/service.py` | Sources become panels with per-panel schedule, kind and supported claims. One task, one generation, unchanged cancellation. |
| `swing_capture/select.py` | Add `settled_frame()` for static panels. |
| `domain/coaching.py` | Add `CoachCue`, `CueId`, `EvidenceGrade`, `ConfidenceSource`; `Advice` gains `cue`, `confidence`, `confidence_source`. |
| `domain/media.py` | `SwingCapture` → `CoachShotEvidence` with panels. |
| `coaching/cues.py` | **New.** The cue table and admissibility. The only new file of consequence. |
| `coaching/diagnose.py` | Emit admissible cues alongside candidates; gate by club. |
| `coaching/evidence.py` | Render graded evidence and the `unmeasured` list. |
| `coaching/prompt.py` | Cue enum in the schema; drop `display` and `focus` from the reply; add `visual_confidence`; `PROMPT_VERSION` → `3`. |
| `coaching/validate.py` | Validate cue and confidence; delete the display branch. |
| `coaching/service.py` | Assemble `CoachShotEvidence`; skip images for putts. |
| `persistence/schema.py`, `records.py` | Migration to v3. |
| `config.py`, `.env.example`, `docs/configuration.md` | Panel settings. |
| `floor/state.py`, HUD | Set `FloorAdvice.pending` from `AnalysisStarted`. |

**Delete.**

| Thing | Why |
| --- | --- |
| `templates.OBSERVED_DISPLAYS` | A cue owns its display. |
| `templates.REINFORCEMENT_DISPLAYS` | Becomes the reinforcement cues. |
| `templates.legal_displays()` | No display in the reply, nothing to police. |
| `validate._display()` and its tests | Same. |
| `SWING_CAPTURE_CAMERA_1_SOURCE` / `_2_SOURCE` | Replaced by the panel list. |
| `coaching/service.CAPTURE_STILL_LIMIT` | Replaced by the total still budget. |
| `prompt.permitted_focuses()` | Subsumed by cue admissibility. |

`templates.SPEECH` and `templates.REINFORCEMENT` **stay**. They are the offline
path — the words on the floor when there is no model — and that is the whole
reason the architecture survives a missing, slow, or wrong provider.

---

## 22. What this design refuses

Stated plainly so that a later reader knows these were decisions:

- **No separate OCR.** No OCR library and no second OCR request. The only
  numbers read off a screen are ProTee's All Values grid, by the coaching model,
  in the one coaching request, and they are stored as visual readings
  (section 23). Swing frames are never read for numbers or club names.
- **No pixel decoding.** No JPEG decoder enters the dependency set. Selection is
  byte comparison, and that is enough for the question it asks.
- **No OBS output control.** No recording, no replay buffer, no scene switching.
  CaddieJ reads OBS; it does not drive it.
- **No frame-to-swing-position mapping.** CaddieJ does not claim a still is
  address, the top, or impact, and no cue may depend on one that does.
- **No visual claim above 0.60 confidence.** Structurally, not by convention.
- **No coaching on the critical path.** Every clause of this document sits behind
  a deterministic line that is already on the floor.

---

## 23. ProTee-only production path

`COACHING_TRIGGER=protee` (or `auto` with swing capture on) coaches from ProTee
VX and OBS alone. GSPro is optional enrichment for history and the floor; it is
never waited for, and its absence is not a failure.

1. **Strike.** ProTee's `ShotDetected` is the strike identity
   (`coaching_strike_id`). It is the only event that makes earlier advice and
   unsaid speech stale. BALL READY, a ball placed, and GSPro writing the same
   strike's result late do not.
2. **Swing capture.** Unchanged: the timed burst across `swing_behind`,
   `swing_side` and `impact`, `SWING_CAPTURE_SELECTED_FRAMES` in total.
3. **All Values.** With `COACH_METRICS_CAPTURE_ENABLED=true`, one screenshot of
   `COACH_METRICS_SOURCE` is taken `COACH_METRICS_CAPTURE_DELAY_SECONDS` after
   the strike, at the source's own resolution, concurrently with the burst and
   inside the same source hold. It is outside the frame budget, written to
   `metrics/all_values.jpg`, and recorded in `metadata.json` as
   `coach_metrics` with `counts_against_selected_frames: false`. A 1×1, blank or
   byte-identical-to-last-shot still is refused (one retry after 1 s).
4. **One request.** Swing frames (at most `COACHING_MAX_IMAGES`) plus the All
   Values still (`detail: high`), strict schema `protee_vx_coach`:
   `{metrics: {<29 metrics>: null | {value, unit, direction}}, coach: {kind,
   cue, instruction, reinforcement, visual_confidence}}` (prompt `vx3`). Each
   attempt gets
   `COACH_OPENAI_TIMEOUT_SECONDS` (30), passed to the OpenAI SDK, within
   `COACH_OPENAI_BUDGET_SECONDS` (45) for the shot. A timed-out attempt is
   retried only when a full attempt still fits. Quick failures such as 503 or
   429 are retried within `COACHING_RETRY_LIMIT`. The next strike cancels a
   request still in flight, and only a strike does that.
5. **Validation.** Each metric alone: finite, plausible, allowed unit, allowed
   direction word, no negative number with a word. Failures are nulled and
   listed. Directions stay ProTee's words in storage; `shot_from_metrics` is
   the only place that turns them into CaddieJ's sign (target frame, positive
   right; open and in-to-out are player-relative and mirrored for left-handers).
   A directional number with no word is kept and not ranked.
6. **Judgement.** The readings become a never-persisted `Shot`, diagnosed by the
   same `diagnose()` with the session baseline and sticky focus. The model's cue
   must be in the registry, in the schema it was offered, and admissible for
   that diagnosis. Otherwise the floor gets CaddieJ's own template line (no
   speech), or nothing, and the log says why.
7. **Speech contract.** One cue is one focus and one instruction.
   `instruction` is one verb-led sentence about the chosen cue's focus;
   `reinforcement` is null or one sentence that only repeats it. The two are
   joined and validated as one line. A verb-led sentence counts as a
   restatement, not a second instruction, only when every word after the verb
   is a neutral back-reference ("Keep that same feeling on the next swing."),
   and only as the last sentence. "Keep that lead arm straight" is still an
   instruction. Words owned by another focus (weight, shoulders, path, tempo,
   strike, face …) are refused unless the cue's focus owns them. A correction
   must contain a real instruction.
8. **Spoken fallback.** If only the wording is refused and the cue is still
   registered and admissible, George speaks that cue's reviewed sentence from
   `CUE_SPEECH` in `cues.py`, which is the existing template line for exactly
   that focus and direction (FACE_CLOSED: "Let the face stay open a fraction
   longer."). The model's words are never spoken. Logged as
   `coaching_speech_fallback`; stored with status `template`. A cue with no
   reviewed sentence (the visual corrections) stays floor-only.
9. **Outputs.** Floor cue, Kokoro speech for an accepted model line or the
   cue's reviewed sentence, and a `coach_results` row with metrics JSON (nulls
   included), provenance, cue, focus, status and the strike id.

`caddiej --check-swing-capture` photographs All Values beside the panels.
`caddiej --check-coach` shows the resolved trigger, that GSPro is not required,
the All Values settings, provider, timeouts and speech. In ProTee mode it
replays the newest real capture through the real provider.
