# Coaching

[← Documentation index](README.md)

After each shot CaddieJ works out the one thing most worth fixing and puts it on
the floor beside the numbers — `FACE OPEN`, `HITTING DOWN`, `TOE STRIKE`. This
is on by default and needs no account, no key and no network: it is arithmetic
over the shots you have already hit.

It compares each shot two ways, because either alone gets somebody wrong. A
fixed band catches a fault that is a fault for anybody — six degrees of open
face is six degrees of open face. Your own recent shots with that club catch the
rest: a club speed of ninety-five is a good drive or an alarming nine iron
depending on whose swing it is, and only your own average can say. Whichever
comparison is worse decides. Below a threshold nothing is said at all, because a
coach who finds something wrong with every swing is one nobody listens to.

Everything is stated from where you are standing. A left-hander who leaves the
face open is told the face is open, not closed, and their own average is
mirrored to match.

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

What the model can and cannot do is worth being precise about. It picks one of
the faults CaddieJ already measured — it cannot invent one. It cannot change how
severe that fault was; the instrument decides. The words on the floor are
CaddieJ's own, and the model chooses among them by name rather than writing
them. What it actually contributes is the spoken sentence, which is checked
before use: one instruction, no numbers that could contradict the readout beside
it, and nothing that would not render.

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

Frames from your swing video can be sent too, which is what would let a model
comment on tempo or posture. It is **off by default**, and that is a privacy
decision rather than a cost one: these are pictures of you in your own home.
`COACHING_SEND_FRAMES=true` turns it on. With it off, coaching works from the
measurements alone and no image leaves the machine.

Every line, and every failure to produce one, is written to the shot history
alongside the shot — which model, which prompt, what it cost — so a session can
be reviewed later and the rows still mean something.
