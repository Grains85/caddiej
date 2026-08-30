"""Choosing a voice and a speaker from configuration, and nothing else.

Two switches, in one file, so that adding an engine is a case here and a module
beside `adapters/kokoro`. The service never learns which one it got.

Each vendor import is inside its own branch on purpose: selecting `fake`, or
`none`, must not require a neural speech model — or the several hundred
megabytes of machine learning framework underneath it — to be installed.
"""

import logging

from caddiej.speech.sink import AudioSink, NullSink
from caddiej.speech.synth import Synthesizer

logger = logging.getLogger(__name__)


def build_synthesizer(kind: str, voice: str, speed: float = 1.0) -> Synthesizer | None:
    """The engine named, or None to leave the room quiet.

    None is a supported answer rather than a failure: speech is off by default
    and a machine with no model installed is a machine CaddieJ runs on.
    """

    if kind == "none":
        return None

    if kind == "kokoro":
        from caddiej.adapters.kokoro import KokoroSynthesizer

        return KokoroSynthesizer(voice=voice, speed=speed)

    if kind == "fake":
        from caddiej.speech.fakes import FakeSynthesizer

        logger.warning("speech_fake_engine")
        return FakeSynthesizer(voice_name=voice)

    logger.error("speech_engine_unknown", extra={"speech_engine": kind})
    return None


def build_sink(kind: str, device: str = "") -> AudioSink | None:
    """Where the sound goes, or None to disable speech entirely.

    `none` is distinct from a missing sink: it renders every line, publishes
    every event and plays nothing, which is what a headless machine wants.
    """

    if kind == "none":
        return NullSink()

    if kind == "sounddevice":
        from caddiej.adapters.audio import SoundDeviceSink

        return SoundDeviceSink(device=device or None)

    logger.error("speech_output_unknown", extra={"speech_output": kind})
    return None
