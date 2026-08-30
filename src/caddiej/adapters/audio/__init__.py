"""Playing audio on the machine CaddieJ is running on.

Nothing outside imports `sounddevice`, and nothing inside CaddieJ depends on it
being installed: the speech subsystem talks to an `AudioSink`, and this is one
implementation of it.
"""

from caddiej.adapters.audio.sink import (
    ABORT_TIMEOUT,
    CHUNK_SECONDS,
    OUTPUT_NAME,
    SoundDeviceSink,
    sdk,
)

__all__ = [
    "ABORT_TIMEOUT",
    "CHUNK_SECONDS",
    "OUTPUT_NAME",
    "SoundDeviceSink",
    "sdk",
]
