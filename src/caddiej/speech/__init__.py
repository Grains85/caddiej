"""Saying things out loud, locally.

Text arrives as an `Utterance`, a queue decides whether and when it deserves the
room, an engine turns it into samples and a sink plays them. Nothing in here can
delay a shot, and nothing outside it needs to know that any of it exists.
"""

from caddiej.speech.audio import Audio, from_wav, silence, to_wav
from caddiej.speech.cache import AudioCache, voice_key
from caddiej.speech.diagnose import CHECK_PHRASE, SpeechProbe, probe_speech
from caddiej.speech.envelope import DEFAULT_FRAME_RATE, envelope_of
from caddiej.speech.factory import build_sink, build_synthesizer
from caddiej.speech.service import (
    DEFAULT_VOICE,
    SpeechHealth,
    SpeechService,
    build_speech_service,
)
from caddiej.speech.sink import (
    AudioSink,
    NullSink,
    PlaybackError,
    PlaybackUnavailableError,
)
from caddiej.speech.synth import (
    SynthesisError,
    SynthesisFailedError,
    SynthesisUnavailableError,
    Synthesizer,
)

__all__ = [
    "CHECK_PHRASE",
    "DEFAULT_FRAME_RATE",
    "DEFAULT_VOICE",
    "Audio",
    "AudioCache",
    "AudioSink",
    "NullSink",
    "PlaybackError",
    "PlaybackUnavailableError",
    "SpeechHealth",
    "SpeechProbe",
    "SpeechService",
    "SynthesisError",
    "SynthesisFailedError",
    "SynthesisUnavailableError",
    "Synthesizer",
    "build_sink",
    "build_speech_service",
    "build_synthesizer",
    "probe_speech",
    "envelope_of",
    "from_wav",
    "silence",
    "to_wav",
    "voice_key",
]
