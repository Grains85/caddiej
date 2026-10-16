# Speech

[← Documentation index](README.md)

> **Not yet run against a real Kokoro model or a real sound card.** The queue,
> the priorities and the cache are developed against fakes. See
> [Project status](../README.md#project-status).

The coaching line can be said out loud, in a British voice, by a model running on
your own machine. Nothing is sent anywhere and no account is involved.

It is **off by default**, for two reasons worth stating plainly: it makes noise in
your house, and switching it on the first time downloads a few hundred megabytes
of speech model — on top of the machine learning framework the extra installs,
which is by far the largest dependency in the project.

```shell
pip install -e '.[tts]'
SPEECH_ENABLED=true caddiej
```

The default voice is `bm_george`. Any Kokoro voice works, and the first letter
picks the language, so `bf_emma` is also British and `af_heart` is American:

```shell
SPEECH_ENABLED=true SPEECH_VOICE=bf_emma caddiej
```

Prove the voice and speaker without a golf shot, GSPro, or a coaching model:

```shell
caddiej --check-speech
```

That loads the same Kokoro engine, `SPEECH_VOICE` and sounddevice output a normal run would use, says "Caddie J is ready.", waits for the line to finish, and exits. It does not start the HUD, the observers or coaching. A failure names the step — engine open, synthesis, playback — so a quiet room after a real shot can be told apart from a voice that never loaded.

Playback goes through PortAudio, which the `sounddevice` wheel usually bundles.
If importing it fails, install the library itself: `brew install portaudio` on
macOS, `apt install libportaudio2` on Debian and Ubuntu. Kokoro falls back to
`espeak-ng` for words outside its dictionary, so installing that too is worth it
if you hear an odd pronunciation.

## What it will and will not say

Only the coached sentence. The line on the floor is already there and reads in
less time than it takes to say, so speaking every one of them would mean CaddieJ
talking after every swing. That also means speech needs a coaching provider: with
`COACHING_PROVIDER=none`, which is the default, the room stays quiet. To hear it
with nothing installed and nothing spent, use the scripted provider:

```shell
SPEECH_ENABLED=true COACHING_PROVIDER=scripted GSPRO_MODE=simulate caddiej
```

Only one thing is ever said at a time. If you hit again before the last line has
been spoken, the waiting line is thrown away rather than said two swings late —
the same reasoning as the coaching queue. That happens two ways, and it needs
both: a newer coaching line replaces an older one, and a **new shot** throws away
anything unsaid about an earlier one. The second covers the case the first
cannot, which is the next shot producing no line at all — nothing measurable
about it, or the model failing on it — leaving the old sentence to be spoken to
somebody who has already hit again.

A line already being spoken is allowed to finish, because a sentence cut off
mid-word sounds broken. The exception is priority: anything more urgent than
coaching cuts the current line off immediately and the speaker is silent before
the next one starts. Nothing produces one of those yet; the mechanism is there
for when something does.

## The other voice in the room

The ProTee software says "ready" out loud when the launch monitor finds the ball.
**That is not CaddieJ**, and CaddieJ cannot turn it off: nothing in this project
speaks a readiness line, and the only place the word appears is a log line
CaddieJ *reads* — `VX status changed: READY FOR SHOT` — which is what puts
`READY` on the floor as a status. Whether it can be silenced is a question about
ProTee's own settings, not about anything here.

CaddieJ does not add a readiness line of its own either, and the reason is the
floor: the ball-lock pulse and the brief tick already say the same thing without
a word, and a room that speaks on every ball as well as after every shot is a
room nobody wants to practise in.

Rendered sentences are cached, in memory and as WAV files under
`media/speech`, because coaching repeats itself by design — the same fault gets
the same wording, and rendering it costs about a second of CPU each time. You can
play those files if you want to hear what CaddieJ said.

## How loud it is

Kokoro is quiet, and quieter than it looks: measured across the cached
`bm_george` lines on the bay, the loudest sample in a sentence sits between
0.33 and 0.47 of full scale and the average sits around 0.06 — roughly 24 dB
below the rail. That is headroom nothing was using. A room where the Windows
mixer is already at maximum has no way to recover it, which is why
`SPEECH_GAIN` exists.

It is one constant multiplier, applied to every sentence equally, at the
speaker. Three consequences follow from where it sits, and all three are the
point:

- **The WAV cache is the engine's own output.** Changing the gain retires no
  file and re-renders nothing.
- **The lights are unaffected.** The envelope published with `SpeechStarted` is
  measured before the gain, so the lamps keep reacting to the shape of the
  words rather than to how loud the room wants them.
- **Nothing is normalised per sentence.** A fixed multiplier is predictable; a
  per-utterance normaliser would make a whisper and a shout arrive at the same
  level, and would make the same line play back differently depending on what
  else was in it.

Peaks are clipped at the rail rather than limited, and the clipped samples are
counted and logged (`speech_gain_clipped`) rather than swallowed. A soft limiter
would hide a gain that is too high by quietly reshaping every peak; a number in
the log says so out loud, which is what lets the setting be chosen from evidence.

`caddiej --check-speech` applies the configured gain and reports the clipped
share, so the bay can be turned up and verified without guessing. Given the
measured peaks above, a gain of 2.0 is clean on every sentence observed and is
what the bay runs; the shipped default is 1.0, because that is the only value
that is right on hardware nobody has measured. The ceiling is 4.0, above which
this stops being a volume control.

If the voice is still too quiet at a gain that does not clip, the limit is
outside CaddieJ: the Windows output device level, the per-application volume in
the Windows mixer, or the amplifier. CaddieJ cannot read or change any of them.

## When it cannot speak

No model, no weights, no sound card, no PortAudio, a render that hangs, or a
speaker unplugged mid-sentence: all of them mean silence and nothing else. The
shot still reaches the floor, the history and the database. `/healthz` says what
went wrong, what has been said, and what was dropped. Loading the voice is
bounded by `SPEECH_OPEN_TIMEOUT`, so a first-run download that stalls leaves the
room quiet rather than holding up start-up for ever.

To run the whole pipeline without making a sound — on a headless machine, or to
check the events without waking anybody up:

```shell
SPEECH_ENABLED=true SPEECH_OUTPUT=none caddiej
```
