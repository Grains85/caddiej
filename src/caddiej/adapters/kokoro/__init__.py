"""Kokoro speech synthesis, confined to this package.

Nothing outside imports `kokoro`, and nothing inside CaddieJ depends on this
package being installed: the speech subsystem talks to a `Synthesizer`, and this
is one implementation of it.
"""

from caddiej.adapters.kokoro.synthesizer import (
    DEFAULT_VOICE,
    ENGINE_NAME,
    LANGUAGES,
    SAMPLE_RATE,
    KokoroSynthesizer,
    language_of,
    sdk,
)

__all__ = [
    "DEFAULT_VOICE",
    "ENGINE_NAME",
    "LANGUAGES",
    "SAMPLE_RATE",
    "KokoroSynthesizer",
    "language_of",
    "sdk",
]
