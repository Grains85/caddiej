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
the same reasoning as the coaching queue. A line already being spoken is allowed
to finish, because a sentence cut off mid-word sounds broken. The exception is
priority: anything more urgent than coaching cuts the current line off
immediately and the speaker is silent before the next one starts. Nothing
produces one of those yet; the mechanism is there for when something does.

Rendered sentences are cached, in memory and as WAV files under
`media/speech`, because coaching repeats itself by design — the same fault gets
the same wording, and rendering it costs about a second of CPU each time. You can
play those files if you want to hear what CaddieJ said.

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
