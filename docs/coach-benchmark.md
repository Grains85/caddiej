# Coach benchmark

[← Documentation index](README.md)

A ProTee coaching line spends about five seconds in swing capture and about
twenty in one OpenAI request. Neither number explains itself, and changing a
model on the bay and hitting another ball compares two different swings, two
different minutes of network and two different moods of the API. The coach
benchmark makes the comparison the only way that means anything: **one stored
shot, sent again, under each configuration in turn, several times each.**

There are two commands.

| Command | Calls OpenAI | What it prints |
|---|---|---|
| `caddiej --check-coach-request` | no | What the request is made of — prompt, evidence text, schema, each image — and the stored capture's own timing |
| `caddiej --benchmark-coach` | yes | All of the above, then one row per run: latency, tokens, cost, parse result, metrics read, cue and evidence frame |

## No side effects

Both commands read one stored shot directory under `media/swing_shots/` and do
nothing else. There is no coaching service, session, event bus, floor, speech,
database or advice cache involved — not stubbed out, absent. The live path
annotates the capture's `metadata.json` with what it sent; the benchmark does
not, and leaves every file in the evidence directory byte-for-byte as it found
it. The tests check each of these.

The production model, effort and timeouts are never changed. With no options,
both commands describe or replay exactly the request the bay sends today:
`COACHING_MODEL`, `COACHING_REASONING_EFFORT`,
`COACHING_PROTEE_MAX_OUTPUT_TOKENS`, `COACH_OPENAI_TIMEOUT_SECONDS`,
`COACHING_MAX_IMAGES`, `COACHING_SEND_FRAMES`, and swing frames at `low`
detail. The benchmark calls OpenAI directly with `COACHING_API_KEY` and
`COACHING_API_BASE`, whatever `COACHING_PROVIDER` says.

The one deliberate difference from a live request: the session context the
live path adds — the focus being worked on and the lines recently spoken — is
left out, because it depends on what happened earlier in an evening and would
make two replays of the same shot differ. It is a sentence or two of text.

## Examples

PowerShell, from the repository root:

```powershell
# Offline: where do the input tokens go? No API call.
.\.venv\Scripts\python.exe -m caddiej --check-coach-request

# The same, for a named shot, estimated for two models.
.\.venv\Scripts\python.exe -m caddiej --check-coach-request `
  --benchmark-evidence media\swing_shots\20261002T184705-1-0ea68447 `
  --benchmark-models gpt-5-mini,gpt-4.1-mini

# Replay the newest shot once with the production configuration.
.\.venv\Scripts\python.exe -m caddiej --benchmark-coach

# Production against two reasoning efforts, five runs each.
.\.venv\Scripts\python.exe -m caddiej --benchmark-coach `
  --benchmark-efforts minimal,low --benchmark-repeats 5

# A non-reasoning model beside the production one. "none" sends no
# reasoning parameter, which a non-reasoning model requires.
.\.venv\Scripts\python.exe -m caddiej --benchmark-coach `
  --benchmark-models gpt-5-mini,gpt-4.1-mini --benchmark-efforts low,none `
  --benchmark-repeats 3
```

Every combination of `--benchmark-models`, `--benchmark-efforts` and
`--benchmark-detail` runs, so the last example is four configurations, two of
which (`gpt-4.1-mini` with `low`, `gpt-5-mini` with `none`) are there to show
what a mismatched pairing does. Name only models the configured OpenAI account
can use; one it cannot is reported as a failed row and the rest still run.

| Option | Default | Meaning |
|---|---|---|
| `--benchmark-evidence DIR` | the newest shot | A stored shot directory |
| `--benchmark-models LIST` | `COACHING_MODEL` | Models to compare |
| `--benchmark-efforts LIST` | `COACHING_REASONING_EFFORT` | `none`, `minimal`, `low`, `medium`, `high` |
| `--benchmark-detail LIST` | `low` | Image detail for the swing frames; the All Values still is always `high`, as live |
| `--benchmark-repeats N` | 1 | Runs per configuration, 1–20 |
| `--benchmark-max-output-tokens N` | `COACHING_PROTEE_MAX_OUTPUT_TOKENS` | Output ceiling |
| `--benchmark-timeout SECONDS` | `COACH_OPENAI_TIMEOUT_SECONDS` | Per-request timeout |

Only the three newest shots are kept (`media/swing_shots` is pruned on every
strike). To benchmark a shot later, copy its directory somewhere else first —
for example `.caddiej\benchmark-shots\` — and name the copy with
`--benchmark-evidence`.

## Reading the output

**The header** names the shot, how many images the request carries and the
frame ids they are announced as. Those ids are the only values the reply's
`evidence_frame_id` may take.

**Request size** is computed offline and is an ESTIMATE. Text is characters
divided by four. Image tokens follow OpenAI's published sizing rules, recorded
in `coaching/benchmark.py`:

- *Patch models* (`gpt-5-mini`, `gpt-5-nano`, `gpt-4.1-mini`, `gpt-4.1-nano`,
  `o4-mini`) cut an image into 32 px patches, shrinking it first so that at most
  1536 are needed, and multiply the count by a per-model factor. The published
  rule has no low-detail variant, so a `low` swing frame is estimated at full
  cost.
- *Tile models* (`gpt-5`, `gpt-4o`, `gpt-4.1`, `o3`, …) charge a flat base at
  low detail; at high detail the base plus a cost per 512 px tile after
  fitting the image inside 2048 × 2048 and then shrinking its shortest side to
  768 px.

A real run prints its measured input tokens (`in`) beside this estimate
(`est in`). When they disagree, the run is right and the table is stale.

**Stored capture timing** reads `metadata.json`: each panel's configured delay
and duration, the window end they add up to, the offsets of the stills that
were kept, and which panel's window sets the end of the capture. "Replay
content appears" is a heuristic — the first still in a panel's timeline at
least 1.5× the size of the one before, which is where ProTee's replay replaces
its loading screen. A kept still before that point is very likely a loading
spinner; one marked SENT TO THE MODEL was paid for.

**Runs** has one row per request:

| Column | Meaning |
|---|---|
| `wall s` | Seconds around the provider call, measured by the benchmark |
| `in`, `cached`, `out`, `reason` | Input, cached input, output and reasoning tokens as the API reported them. Reasoning is part of output |
| `est in` | The offline estimate of input tokens for this configuration |
| `usd` | `estimate_cost` from `adapters/openai/pricing.py` — an estimate, never cached-input aware |
| `parsed` | The reply had both a `metrics` object and a `coach` object |
| `metrics` | How many of the 29 All Values readings survived validation |
| `cue`, `frame` | The cue chosen and the evidence frame cited; `NOT SENT` means an id the request did not carry |

Failures are listed underneath with the error, then every coach line, so the
wording of two configurations can be judged side by side. **Per
configuration** gives median, minimum and maximum latency and median tokens.

Runs are sequential, never concurrent, and interleaved: every configuration
once, then every configuration again. API speed drifts over minutes, and
running all of one configuration first would hand that drift to whichever ran
second.

## A/B protocol

The question is always "is B faster than A without being worse", and on one
shot the answer is about that shot. So:

1. **Pick the evidence.** Copy three to five stored shots out of
   `media/swing_shots` — different clubs, at least one with a visible swing
   fault — so the pruning cannot take them. Run `--check-coach-request` on
   each and check that its frames are real swing frames.
2. **Change one thing.** A against B differ in exactly one of model, effort,
   detail or output ceiling. Every combination named runs, so a two-way
   comparison is one list with two items and every other list with one.
3. **Repeat.** At least `--benchmark-repeats 5` per shot. Latency spreads by
   several seconds between identical requests; a single sample proves nothing.
4. **Run it twice, reversed.** Run A,B and then B,A on the same shot, at a
   different time of day if possible. The interleaving shares drift between
   them; reversing the order also cancels any first-request warm-up.
5. **Compare quality before speed.** For every shot, B must parse every time,
   read at least as many metrics as A on every run, choose the same cue as A
   in the majority of runs (or a cue a coach would agree is better), and cite
   a frame that shows what it says. Read the coach lines side by side.
6. **Then compare speed.** Adopt B only if its median latency is meaningfully
   lower — 20% or more — and its maximum is no worse than A's. Note the
   median output and reasoning tokens, which explain most of a difference.
7. **Write it down.** Keep the benchmark output with the shot copies. A
   production change (`COACHING_MODEL`, `COACHING_REASONING_EFFORT`,
   `SWING_CAPTURE_IMAGE_WIDTH`) is then a separate, deliberate edit to `.env`,
   followed by a few real shots to confirm what the benchmark predicted.

Each run of the production configuration costs roughly one US cent at the
listed `gpt-5-mini` price. Twenty runs is twenty cents.
