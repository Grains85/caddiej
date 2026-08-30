"""Not rendering the same sentence twice.

Worth having here in a way it would not be for most caches, because coaching
repeats itself by design. The advice cache upstream hands back the same wording
for the same fault, the templated lines are a fixed table, and a player working
on one thing hears one sentence all evening. Rendering it is a second of CPU
every time, in the second after a shot, on the machine also running the
simulator.

Two layers, for two different reasons.

**Memory, bounded by bytes rather than by entries.** A sentence is a hundred and
fifty kilobytes and a word is two, so counting entries would cap the interesting
resource at somewhere between three megabytes and three hundred. Bytes is the
thing actually being spent, so bytes is what is counted.

**Disk, optional, as plain WAV files.** It survives a restart, which matters
because the first coaching line after starting CaddieJ is the one somebody is
waiting for. It is also debuggable in the only way audio really can be: the
files are in a directory and can be played.

Everything touching the disk goes through a thread. A cache that blocked the
event loop to save a second of CPU would be a poor trade.
"""

import asyncio
import hashlib
import logging
from collections import OrderedDict
from pathlib import Path

from caddiej.speech.audio import Audio, from_wav, to_wav

logger = logging.getLogger(__name__)

# Part of every key. Bumping it retires every cached file, which is what a
# change to the sample format or the PCM layout requires.
CACHE_VERSION = "1"


def voice_key(text: str, voice: str, speed: float, engine: str) -> str:
    """What makes two requests the same sound.

    Everything that changes the samples, and nothing that does not. The shot the
    line was about is absent on purpose: the same sentence about two different
    swings is the same sentence.
    """

    material = "|".join(
        (
            CACHE_VERSION,
            engine,
            voice,
            f"{speed:.3f}",
            " ".join(text.split()).lower(),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


class AudioCache:
    """Rendered speech, kept in memory and optionally on disk."""

    def __init__(
        self,
        max_bytes: int = 32 * 1024 * 1024,
        directory: Path | None = None,
        max_files: int = 512,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must hold at least one byte")
        if max_files < 1:
            raise ValueError("max_files must allow at least one file")
        self._max_bytes = max_bytes
        self._directory = directory
        self._max_files = max_files
        self._entries: OrderedDict[str, Audio] = OrderedDict()
        self._bytes = 0
        self._files = 0

        self.hits = 0
        self.misses = 0
        self.writes = 0

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def held_bytes(self) -> int:
        return self._bytes

    async def open(self) -> None:
        """Count what a previous run left behind, and prune it to size.

        A cache directory is the one part of speech that outlives the process,
        so it is the one part that has to be brought back under its limit at
        start-up rather than trusted to have been left tidy.
        """

        if self._directory is None:
            return
        try:
            self._files = await asyncio.to_thread(self._sweep)
        except OSError as error:
            # A cache that cannot be read is a cache that is not used. It is an
            # optimisation, and no optimisation is worth refusing to speak over.
            logger.warning("speech_cache_unreadable", exc_info=error)
            self._directory = None

    async def get(self, key: str) -> Audio | None:
        """The rendered line, from memory or from disk, or None."""

        cached = self._entries.get(key)
        if cached is not None:
            self._entries.move_to_end(key)
            self.hits += 1
            return cached

        stored = await self._read(key)
        if stored is None:
            self.misses += 1
            return None

        self.hits += 1
        self._remember(key, stored)
        return stored

    async def put(self, key: str, audio: Audio) -> None:
        """Keep it, in memory and on disk, evicting whatever no longer fits."""

        if audio.is_empty:
            return
        self._remember(key, audio)
        await self._write(key, audio)

    def clear(self) -> None:
        self._entries.clear()
        self._bytes = 0

    # --- memory ------------------------------------------------------------

    def _remember(self, key: str, audio: Audio) -> None:
        existing = self._entries.pop(key, None)
        if existing is not None:
            self._bytes -= len(existing.data)
        self._entries[key] = audio
        self._bytes += len(audio.data)
        while self._bytes > self._max_bytes and len(self._entries) > 1:
            _, evicted = self._entries.popitem(last=False)
            self._bytes -= len(evicted.data)

    # --- disk --------------------------------------------------------------

    def _path(self, key: str) -> Path | None:
        return None if self._directory is None else self._directory / f"{key}.wav"

    async def _read(self, key: str) -> Audio | None:
        path = self._path(key)
        if path is None:
            return None
        try:
            return await asyncio.to_thread(self._load, path)
        except OSError as error:
            logger.warning("speech_cache_read_failed", exc_info=error)
            return None

    def _load(self, path: Path) -> Audio | None:
        if not path.is_file():
            return None
        try:
            return from_wav(path.read_bytes())
        except ValueError:
            # Half-written by a run that was killed, or written by a version
            # that framed the samples differently. Either way it is not audio
            # this process should play, and deleting it makes the next miss the
            # last one for this line.
            logger.warning(
                "speech_cache_entry_discarded", extra={"clip_path": str(path)}
            )
            path.unlink(missing_ok=True)
            return None

    async def _write(self, key: str, audio: Audio) -> None:
        path = self._path(key)
        if path is None:
            return
        try:
            await asyncio.to_thread(self._store, path, audio)
        except OSError as error:
            logger.warning("speech_cache_write_failed", exc_info=error)
            return
        self.writes += 1
        self._files += 1
        if self._files > self._max_files:
            try:
                self._files = await asyncio.to_thread(self._sweep)
            except OSError as error:
                logger.warning("speech_cache_prune_failed", exc_info=error)

    def _store(self, path: Path, audio: Audio) -> None:
        """Write the WAV under a temporary name, then rename it into place.

        The rename is atomic within a directory, so a reader either finds a
        whole file or finds nothing. Without it, a process killed mid-write
        leaves something that parses as a WAV and plays as a burst of noise.
        """

        path.parent.mkdir(parents=True, exist_ok=True)
        working = path.with_name(f".tmp-{path.name}")
        working.write_bytes(to_wav(audio))
        working.replace(path)

    def _sweep(self) -> int:
        """Delete the least recently written files until the limit is met."""

        assert self._directory is not None
        self._directory.mkdir(parents=True, exist_ok=True)
        files = sorted(
            self._directory.glob("*.wav"), key=lambda path: path.stat().st_mtime
        )
        for path in files[: max(0, len(files) - self._max_files)]:
            path.unlink(missing_ok=True)
        return min(len(files), self._max_files)
