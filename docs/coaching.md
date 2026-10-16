# Coaching

[← Documentation index](README.md)

After each shot CaddieJ works out the one thing most worth saying and puts it on
the floor beside the numbers — `FACE OPEN`, `HITTING DOWN`, `TOE STRIKE`, or
`GOOD FACE` when there is nothing to fix. This is on by default and needs no
account, no key and no network: it is arithmetic over the shots you have already
hit.

It compares each shot two ways, because either alone gets somebody wrong. A
fixed band catches a fault that is a fault for anybody — six degrees of open
face is six degrees of open face. Your own recent shots with that club catch the
rest: a club speed of ninety-five is a good drive or an alarming nine iron
depending on whose swing it is, and only your own average can say. Whichever
comparison is worse decides.

Everything is stated from where you are standing. A left-hander who leaves the
face open is told the face is open, not closed, and their own average is
mirrored to match.

## One thing per shot, and the same thing for a while

Two rules do most of the work, and they are what separate a lesson from a list
of faults.

**One idea.** Every shot gets exactly one: the worst thing measured about it, or
the confirmation that the thing being worked on has stopped being a problem.
Never two. A player told about the face, then the path, then the tempo has been
given three things to think about and will take away none of them.

**And it sticks.** A focus adopted when a fault is corrected is kept — through
the shots that are fine as well as the ones that are not — until either another
fault is markedly worse, or the same focus has come back clean three times in a
row, at which point it has been learned and coaching moves on. This is what
`coaching/session.py` holds, and it is memory rather than history: a handful of
numbers per focus, in this process, gone when you stop playing.

Because the trend is kept per focus rather than per shot, "the face has been
coming down from six degrees to one and a half" is a fact CaddieJ can state. It
is also how improvement is recognised: a shot materially better than the ones
behind it is told so — `GOOD FACE`, "much better face control, do that again" —
rather than being mined for the next flaw.

## Every shot, and the two kinds of line

Every shot with something measurable about it gets a line, and it is one of two
kinds. A **correction** when a fault is above the threshold
(`COACHING_SEVERITY_FINE`). A **reinforcement** when everything measured is
inside its band, which names what worked and asks for it again — because a good
shot is useful exactly for the movement that produced it, and "great shot" tells
a player nothing to repeat.

Silence is left for one case: nothing about the shot could be judged. No club
data, no baseline yet, nothing outside a band. Then CaddieJ says nothing,
because the alternative is a sentence it made up.

## What is actually measured, by path

This decides what coaching can say, and it is much narrower on real passive
hardware than the fault table suggests.

| Path | What reaches coaching |
| --- | --- |
| ProTee log (`PROTEE_OBSERVER_ENABLED`) | Club, handedness, readiness, ball placement, shot trigger, GSPro's remaining distance. **No ball or club measurements at all.** |
| GSPro `currentRound.dat` (`GSPRO_MODE=observe`) | Ball speed, and distances that include roll. |
| Open Connect interpose (`GSPRO_MODE=interpose`, experimental) | Whatever ProTee sends on the wire, which is where face, path, attack and spin would come from — unverified. |
| Swing video frames (`COACHING_SEND_FRAMES`) | Posture, tempo, sway, standing up through the ball, qualitatively. |

So in the mode CaddieJ actually runs in, the one measurable thing is how
repeatable a club's strike is, and it takes about six shots with that club
before scatter means anything. Coaching about the *swing* on this path comes
from the camera, not from the launch monitor. Nothing estimates a club number
from a ball number to fill the gap: `TotalDistance` is not recorded as carry,
and a missing launch angle stays missing rather than becoming zero.

This is also why `AdviceKind` exists rather than a severity of zero meaning
"good". A shot nobody can judge and a shot with nothing wrong with it are
different things, and a number cannot hold that difference without one of them
being a lie.

## Letting a model choose the words

> **Not yet run against a real OpenAI account.** The provider is developed
> against a stub SDK and a scripted provider. See
> [Project status](../README.md#project-status).

Optionally, the fault CaddieJ measured can be handed to a language model to be
worded more like a person and less like a readout. The model's job is only the
wording:

```shell
pip install -e '.[ai]'
COACHING_PROVIDER=openai COACHING_API_KEY=sk-... caddiej
```

The key is read from the environment or a `.env` file, which is gitignored. It
is never written to a log, and never committed.

What the model can and cannot do is worth being precise about. It picks one
**cue** from a list CaddieJ already computed as admissible — it cannot invent a
lesson, a measurement or a display. The floor words come from that cue. The
model writes the spoken sentence, which is checked before use: one instruction,
no numbers that could contradict the readout beside it, and nothing that would
not render. Visual cues need pictures and are confidence-capped; a visual
confidence can never out-rank a derived one.

## Coach the cause, not the result

A cue is admitted when its evidence exists. Which of the admitted cues is
actually said is `authority_cue`, and the order is a coach's rather than a
launch monitor's:

1. a **seen fault** that is admissible on this shot and that the model was sure
   enough about — because it is a cause and the player can change it;
2. the **measured** lesson, when it is a correction at or above
   `SEVERITY_FAULT` — a badly open face is more urgent than a plane somebody
   thinks they can see;
3. the **reinforcement**, which is worth saying and is worth saying last.

This is the one real behaviour change from Coach V1, and it came from a shot
that scored 94 and was told GOOD STRIKE while the replay showed the club coming
over the top. Two things had gone wrong, and both are fixed here.

**A good reading no longer silences a picture.** `admissible_cues` used to run
the advice-kind gate before the visual branch, so a reinforcement diagnosis
dropped every visual correction before it was considered: an excellent delivery
took every visible swing fault off the table with it. The visual branch now runs
first. A measurement at the ball and a movement in the backswing are facts about
different things, and one being fine is not evidence about the other.

**There is now a cue for the move itself.** `OVER_THE_TOP` — observed
`OUT-TO-IN PATH`, instruction `DIP TRAIL SHOULDER`. The measured `PATH_OUT_TO_IN`
cue was correctly silent on that shot: the path was 1.7° out-to-in, inside every
band. There was simply no cue for a visible over-the-top move, which is the gap.
Each of the four rated parts of the swing now has exactly one cue to promote to,
so the score and the coaching cannot disagree about what was seen.

A promoted cue needs a `fault` rating at 0.45 confidence or better, and is
sorted by confidence then by a fixed order (plane, posture, transition, balance)
so the answer never depends on the order the reply listed them in.

**One focus and one instruction, unchanged.** Nothing here widens what may be
said: the cue vocabulary is closed, the sentence is reviewed by the same
validator, and exactly one of each reaches the player. What changed is which one.

**Still one request.** The swing rating is a `swing` block in the same
`protee_vx_coach` schema as the metrics and the coaching line — prompt version
`vx6`. There is no second call. A rating must cite a frame id that was actually
sent, and the All Values grid is excluded from the ids a rating may cite:
nobody's swing plane is visible in a picture of numbers.

Prove the production path without a golf shot:

```shell
caddiej --check-coach-cues
caddiej --check-coach
```

`--check-coach` runs `diagnose()`, evidence rendering, the OpenAI request (if
configured), validation, cue selection and the analysis worker. It does not
bypass that code.

It is given a short, deterministic account of the session along with the shot:
which focus is being worked on, how many shots it has been the focus for, the
last few readings of it, and whether this shot is an improvement on them. No
history is queried and nothing is sent that a swing did not produce — no name,
no file path, no identifier.

If any of that fails — no key, a timeout, a refusal, a reply that breaks a rule,
or an answer that arrives after you have hit the next ball — the line CaddieJ
worked out itself is already on the floor and simply stays there. `/healthz`
says what happened and how many calls have been made.

`COACHING_API_BASE` points the same adapter at any endpoint that implements
OpenAI's **Responses** API with structured JSON output, including a local one.
Servers that offer only chat completions need a new provider rather than a base
URL; the port they would implement is `coaching/provider.py`, which is one method
and five neutral error types.

## Seeing it work without an account

```shell
COACHING_PROVIDER=scripted GSPRO_MODE=simulate caddiej
```

That plays a scripted round and coaches it end to end, using a provider that
answers from inside the process. It exercises the whole path — prompt,
validation, floor, database — on a machine with no credentials and no internet.

## What it costs, and what it sends

Five separate limits keep an evening cheap, and all of them are configurable: a
ceiling on calls per run (`COACHING_CALL_LIMIT`), a minimum gap between them
(`COACHING_MIN_INTERVAL`), a tight cap on the answer's length, a cache so the
same swing is never paid for twice, and a short queue that throws away advice
for a swing you have moved on from rather than showing it late.

Frames from your swing video can be sent too, which is what lets a model comment
on tempo or posture. It is **off by default**, and that is a privacy decision
rather than a cost one: these are pictures of you in your own home.
`COACHING_SEND_FRAMES=true` turns it on. With it off, coaching works from the
measurements alone and no image leaves the machine.

## What the model may read from a picture

Four stills per swing, chosen by time and not by looking at them — spaced across
roughly the second and a half either side of impact, widened when the camera's
latency has never been measured. That bound is the whole of it: four, every
time, whatever else is happening. Prove the extraction path with
`caddiej --check-video-frames` before setting `COACHING_SEND_FRAMES=true`.

With frames supplied the model may name something only a picture shows —
posture, tempo, standing up through the ball, swaying off it — instead of a
measured fault. It may not contradict or restate a measurement from a picture,
and it may not use a number it read off one.

Three limits are stated to it rather than left to be guessed, because a model
not told them will fill the gap in plausibly and wrongly:

- **Where the camera is.** `VIDEO_CAMERA_VIEW` — nothing can work this out from
  the footage. Face-on shows sway and posture and cannot show swing plane;
  down-the-line is the reverse. Left `unknown`, which is the default, the model
  is told the angle is unknown and to keep off anything that depends on it.
- **When each still was taken**, as a signed offset from impact, and whether
  that impact time was measured or estimated.
- **That no swing position has been detected.** Nothing here finds address or
  the top of a backswing, so a frame is offered as "0.45 seconds before impact"
  and never as "the top". Labelling a seek offset as a swing position is how a
  model ends up describing a position it was never shown.

Every line, and every failure to produce one, is written to the shot history
alongside the shot — which model, which prompt, what it cost — so a session can
be reviewed later and the rows still mean something.
