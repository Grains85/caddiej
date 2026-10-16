"""Proving, against the real voice and speaker, that CaddieJ can talk.

There are two very different reasons the room stays quiet after a shot: coaching
never produced a line, or speech itself cannot load, render or play. From the
bay those look identical, so this walks the speech half on demand — open the
configured engine, render one sentence, play it through the configured output —
and says which step stopped working.

It uses the same factory, synthesizer and sink that a normal run does. A
diagnostic that invented its own playback path would pass while the voice the
player hears stayed broken.

Prose rather than JSON, and no event loop of its own, because it is read by a
person at a terminal in the same session as `--check-config`. It is a
diagnostic: it says one short line and exits.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from caddiej.speech.audio import amplify
from caddiej.speech.factory import build_sink, build_synthesizer
from caddiej.speech.sink import AudioSink, PlaybackError, PlaybackUnavailableError
from caddiej.speech.synth import SynthesisError, SynthesisUnavailableError, Synthesizer

logger = logging.getLogger(__name__)

# Spoken, not written as the product name: Kokoro reads "Caddie J" as two words
# and "CaddieJ" as a jumble. The line is short enough to hear and specific
# enough that a working check cannot be mistaken for a coaching sentence.
CHECK_PHRASE = "Caddie J is ready."


@dataclass(frozen=True, slots=True)
class SpeechProbe:
    """What happened, and whether the operator has to fix the voice."""

    ok: bool
    report: str


async def probe_speech(
    *,
    enabled: bool,
    engine: str,
    output: str,
    voice: str,
    speed: float = 1.0,
    device: str = "",
    gain: float = 1.0,
    open_timeout_seconds: float = 90.0,
    render_timeout_seconds: float = 20.0,
    synthesizer_factory: Callable[[], Synthesizer | None] | None = None,
    sink_factory: Callable[[], AudioSink | None] | None = None,
) -> SpeechProbe:
    """Speak one configured line through the production speech path.

    Never logs audio bytes. The phrase is a public constant, not a secret.
    """

    lines = [
        "Speech check — one line through the configured voice and speaker",
        f"  Engine               {engine}",
        f"  Voice                {voice}",
        f"  Output               {output}",
        "",
    ]
    logger.info(
        "speech_check_started",
        extra={
            "speech_engine": engine,
            "speech_voice": voice,
            "speech_output": output,
        },
    )

    if not enabled:
        return _failed(
            lines,
            "enabled",
            "SPEECH_ENABLED is false",
            "This check exists to prove the room can hear CaddieJ. Set "
            "SPEECH_ENABLED=true in .env, then run it again.",
        )
    if engine == "none":
        return _failed(
            lines,
            "engine",
            "SPEECH_ENGINE is none",
            "There is no voice to load. Set SPEECH_ENGINE=kokoro.",
        )
    if output == "none":
        return _failed(
            lines,
            "output",
            "SPEECH_OUTPUT is none",
            "That setting renders every line and plays nothing, which is right "
            "for a headless machine and useless as a speaker check. Set "
            "SPEECH_OUTPUT=sounddevice.",
        )

    synthesizer = (
        synthesizer_factory()
        if synthesizer_factory is not None
        else build_synthesizer(engine, voice=voice, speed=speed)
    )
    if synthesizer is None:
        return _failed(
            lines,
            "engine",
            f"unknown engine {engine!r}",
            "SPEECH_ENGINE must be kokoro (or fake, for a machine with no model).",
        )
    if not synthesizer.is_configured:
        return _failed(
            lines,
            "engine",
            f"{synthesizer.name} is not installed",
            "Install the speech extra: pip install -e '.[tts]'.",
        )

    sink = (
        sink_factory()
        if sink_factory is not None
        else build_sink(output, device=device)
    )
    if sink is None or not sink.is_configured:
        try:
            await synthesizer.aclose()
        except Exception:
            logger.warning("speech_check_close_failed", exc_info=True)
        if sink is None:
            return _failed(
                lines,
                "output",
                f"unknown output {output!r}",
                "SPEECH_OUTPUT must be sounddevice.",
            )
        return _failed(
            lines,
            "output",
            f"{sink.name} is not installed",
            "Install the speech extra: pip install -e '.[tts]'. On Linux that "
            "also needs the PortAudio library (apt install libportaudio2).",
        )

    try:
        return await _run(
            lines,
            synthesizer,
            sink,
            voice=voice,
            gain=gain,
            open_timeout_seconds=open_timeout_seconds,
            render_timeout_seconds=render_timeout_seconds,
        )
    finally:
        for closeable in (sink, synthesizer):
            try:
                await closeable.aclose()
            except Exception:
                logger.warning("speech_check_close_failed", exc_info=True)


async def _run(
    lines: list[str],
    synthesizer: Synthesizer,
    sink: AudioSink,
    *,
    voice: str,
    gain: float,
    open_timeout_seconds: float,
    render_timeout_seconds: float,
) -> SpeechProbe:
    try:
        async with asyncio.timeout(open_timeout_seconds):
            await synthesizer.open()
    except TimeoutError:
        return _failed(
            lines,
            "engine open",
            f"the voice did not load within {open_timeout_seconds:g}s",
            "A first run downloads the model. Give it a clearer network, "
            "or raise SPEECH_OPEN_TIMEOUT.",
        )
    except SynthesisUnavailableError as error:
        return _failed(
            lines,
            "engine open",
            error,
            "The voice could not be loaded. Check that the [tts] extra is "
            "installed and that the machine can reach Hugging Face for the "
            "first-run download.",
        )
    except Exception as error:
        return _failed(lines, "engine open", error, "")
    lines.append(f"  engine ready         {synthesizer.name} voice {voice}")
    logger.info(
        "speech_check_engine_ready",
        extra={"speech_engine": synthesizer.name, "speech_voice": voice},
    )

    try:
        await sink.open()
    except PlaybackUnavailableError as error:
        return _failed(
            lines,
            "output open",
            error,
            "No usable speaker. Check that a playback device is connected "
            "and that SPEECH_DEVICE, if set, names one PortAudio knows.",
        )
    except Exception as error:
        return _failed(lines, "output open", error, "")
    lines.append(f"  output ready         {sink.name}")
    logger.info("speech_check_output_ready", extra={"speech_output": sink.name})

    try:
        async with asyncio.timeout(render_timeout_seconds):
            audio = await synthesizer.render(CHECK_PHRASE, voice)
    except TimeoutError:
        return _failed(
            lines,
            "synthesis",
            f"the voice took longer than {render_timeout_seconds:g}s",
            "The engine is loaded but a sentence did not come back. Try "
            "again, or raise SPEECH_RENDER_TIMEOUT.",
        )
    except SynthesisError as error:
        return _failed(lines, "synthesis", error, "")
    except Exception as error:
        return _failed(lines, "synthesis", error, "")
    if audio.is_empty:
        return _failed(
            lines,
            "synthesis",
            "the engine returned no audio",
            "A silent render is not a working voice.",
        )
    # The configured gain, so the check answers "is this loud enough" rather
    # than "did a sentence come out". A check quieter than the bay is no check.
    audio, clipped = amplify(audio, gain)
    duration = round(audio.duration_seconds, 2)
    lines.append(f"  synthesised          {duration:g}s of audio")
    if gain != 1.0:
        share = clipped / max(1, audio.frame_count)
        lines.append(f"  gain                 x{gain:g}, {share:.2%} clipped")
        logger.info(
            "speech_check_gain",
            extra={"speech_gain": gain, "speech_clipped_samples": clipped},
        )
    logger.info(
        "speech_check_synthesized",
        extra={"speech_voice": voice, "speech_seconds": duration},
    )

    logger.info(
        "speech_check_playback_started",
        extra={"speech_output": sink.name, "speech_seconds": duration},
    )
    lines.append("  playing              listen now")
    try:
        await sink.play(audio)
    except PlaybackError as error:
        return _failed(
            lines,
            "playback",
            error,
            "The voice rendered but the speaker failed mid-sentence.",
        )
    except Exception as error:
        return _failed(lines, "playback", error, "")
    lines.append("  played               finished")
    logger.info(
        "speech_check_playback_completed",
        extra={"speech_output": sink.name, "speech_seconds": duration},
    )

    lines.extend(
        [
            "",
            "CaddieJ can speak. If a real shot still produces no voice, the",
            "problem is coaching or the speech queue rather than the engine:",
            "look for speech_queued and speech_started in the log.",
        ]
    )
    return SpeechProbe(ok=True, report="\n".join(lines))


def _failed(lines: list[str], step: str, error: object, advice: str) -> SpeechProbe:
    """A failure named by the speech step that produced it.

    The error's own message is included because it names the missing package,
    the missing device, or the timeout. It cannot carry audio: nothing on this
    path is given the samples except the sink, and the sink is not asked to
    describe them.
    """

    text = str(error)
    lines.append(f"  FAILED at {step}: {text}")
    if advice:
        lines.extend(["", advice])
    logger.warning(
        "speech_check_failed", extra={"speech_detail": f"{step}: {text}"[:300]}
    )
    return SpeechProbe(ok=False, report="\n".join(lines))
