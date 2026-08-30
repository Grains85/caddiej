"""Substitutes for a camera, at two different depths.

They ship inside the subsystem rather than in the test tree because they are how
this code is developed, not merely how it is checked. A swing camera is bolted to
a wall next to a golf mat; nobody writing the clip queue has one to hand.

`synthetic_camera` in `cameras.py` is the shallower fake: FFmpeg really runs and
real files really appear, only the photons are missing. That is the one to reach
for when the question is "does the pipeline work".

What is here is the deeper fake: no FFmpeg at all. `FakeSpawn` pretends to be a
recording and writes whatever segments a test says appeared, which makes the
buffer, the queue and the failure paths testable without a binary, without
waiting, and without a machine that has a camera.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from caddiej.video.ffmpeg import CommandResult, Process


class FakeProcess:
    """A recording that does nothing until a test tells it to end."""

    def __init__(self, exit_code: int = 0) -> None:
        self._finished: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self._exit_code = exit_code
        self.stopped = False
        self.killed = False

    async def wait(self) -> int:
        return await self._finished

    async def stop(self, _grace_seconds: float = 3.0) -> int:
        self.stopped = True
        if not self._finished.done():
            self._finished.set_result(self._exit_code)
        return self._exit_code

    def stderr_tail(self) -> str:
        return ""

    def end(self, exit_code: int = 0) -> None:
        """Make the recording stop, as an unplugged camera would."""

        if not self._finished.done():
            self._finished.set_result(exit_code)


@dataclass
class FakeSpawn:
    """Stands in for starting FFmpeg, and remembers what it was asked to run."""

    fail_with: BaseException | None = None
    commands: list[tuple[str, ...]] = field(default_factory=list)
    processes: list[FakeProcess] = field(default_factory=list)

    async def __call__(self, args: Sequence[str]) -> Process:
        self.commands.append(tuple(args))
        if self.fail_with is not None:
            raise self.fail_with
        process = FakeProcess()
        self.processes.append(process)
        return process

    @property
    def latest(self) -> FakeProcess:
        return self.processes[-1]


def finished_mp4(payload: bytes = b"swing") -> bytes:
    """The smallest thing that reads as an MP4 FFmpeg finished writing.

    Three top-level boxes: a file type, some data, and the `moov` index that
    FFmpeg writes last. The extractor checks for that last box before publishing
    a clip, so a fake that wants to stand in for a successful run has to produce
    one — and a fake that wants to stand in for a killed FFmpeg leaves it out.
    """

    def box(name: bytes, contents: bytes) -> bytes:
        return (len(contents) + 8).to_bytes(4, "big") + name + contents

    return (
        box(b"ftyp", b"isom" + (0x200).to_bytes(4, "big") + b"isom")
        + box(b"mdat", payload)
        + box(b"moov", b"")
    )


@dataclass
class FakeRun:
    """Stands in for a short FFmpeg run, optionally producing an output file.

    Extraction is asserted through the file it leaves behind, so the fake has to
    leave one: the extractor deliberately refuses to publish a clip that FFmpeg
    claimed to write but did not — or wrote only part of.
    """

    returncode: int = 0
    stderr: str = ""
    produce: bool = True
    contents: bytes = field(default_factory=finished_mp4)
    delay: float = 0.0
    raises: BaseException | None = None
    commands: list[tuple[str, ...]] = field(default_factory=list)

    async def __call__(self, args: Sequence[str], _timeout: float) -> CommandResult:
        self.commands.append(tuple(args))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        if self.produce and self.returncode == 0:
            _output_of(args).write_bytes(self.contents)
        return CommandResult(returncode=self.returncode, stderr=self.stderr)


def write_segments(
    directory: Path,
    count: int,
    segment_seconds: float = 1.0,
    first: int = 0,
    start_at: float = 0.0,
    size_bytes: int = 1024,
) -> list[Path]:
    """Write segment files and the index line FFmpeg would have written.

    A test that wants a buffer to exist wants both halves: the files, so they
    can be deleted and measured, and the CSV, because that is the only thing the
    recorder reads.
    """

    directory.mkdir(parents=True, exist_ok=True)
    listing = directory / "segments.csv"
    lines: list[str] = []
    paths: list[Path] = []
    for index in range(count):
        sequence = first + index
        path = directory / f"seg-{sequence:06d}.mkv"
        path.write_bytes(b"x" * size_bytes)
        start = start_at + index * segment_seconds
        lines.append(f"{path.name},{start:.6f},{start + segment_seconds:.6f}\n")
        paths.append(path)
    with listing.open("a", encoding="utf-8") as handle:
        handle.write("".join(lines))
    return paths


def _output_of(args: Sequence[str]) -> Path:
    """The destination in an FFmpeg command: the last argument."""

    return Path(args[-1])
