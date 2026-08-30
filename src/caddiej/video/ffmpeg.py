"""The only place in CaddieJ where an FFmpeg argument vector exists.

FFmpeg is an external binary rather than a Python dependency, so there is nothing
to import and nothing to pin — and no wrapper library either. An argument vector
is a list of strings; building it explicitly costs a few lines and makes the
flags that actually matter assertable in a unit test, which a wrapper would hide.

Three commands, and only three. One runs for as long as CaddieJ does and produces
the rolling buffer; one runs for a second at a time and turns part of that buffer
into a clip; the third pulls a single still out of a clip for something else to
look at.
"""

import asyncio
import logging
import shutil
import subprocess
import sys
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from caddiej.video.cameras import CameraSpec

logger = logging.getLogger(__name__)

# Enough stderr to explain a failure, not enough to hold a round's worth of
# warnings in memory.
_STDERR_LINES = 40


@dataclass(frozen=True, slots=True)
class CaptureProfile:
    """How the rolling buffer is written.

    Segments are Matroska because a Matroska file that was being written when
    the process died is still readable, while an MP4 whose index was never
    written is not — and the segment interrupted by a crash is exactly the one
    covering the last shot. Clips are MP4 because that is what everything else
    expects to be handed.
    """

    segment_seconds: float = 1.0
    keyframe_seconds: float = 0.5
    buffer_seconds: float = 30.0
    buffer_max_bytes: int = 2 * 1024**3
    encoder: str = "libx264"
    bitrate: str = "8M"

    def __post_init__(self) -> None:
        if self.segment_seconds <= 0:
            raise ValueError("segment_seconds must be positive")
        if self.keyframe_seconds <= 0:
            raise ValueError("keyframe_seconds must be positive")
        if self.buffer_seconds < self.segment_seconds:
            raise ValueError("buffer_seconds must hold at least one segment")


def encoder_arguments(encoder: str) -> tuple[str, ...]:
    """Extra flags a particular encoder needs to be usable here.

    Only the software encoder needs any: left at its defaults, `libx264` will
    happily spend more CPU on a nicer picture than the machine can spare while
    also driving a projector. Hardware encoders are already cheap, so they get
    nothing added and stay whatever the user asked for.
    """

    if encoder == "libx264":
        return ("-preset", "ultrafast", "-tune", "zerolatency")
    return ()


def capture_command(
    camera: CameraSpec,
    profile: CaptureProfile,
    segment_pattern: Path,
    segment_list: Path,
    binary: str = "ffmpeg",
) -> tuple[str, ...]:
    """Record continuously, in short segments, with an index beside them."""

    # A keyframe every half second bounds how far a cut can land from where it
    # was asked for, and every segment necessarily begins on one.
    keyframe_interval = max(1, round(camera.fps * profile.keyframe_seconds))
    return (
        binary,
        "-hide_banner",
        # Without this FFmpeg competes with CaddieJ for the terminal's input.
        "-nostdin",
        "-loglevel",
        "warning",
        *camera.input_arguments,
        # No audio in this milestone. It would be nearly free and the impact
        # transient is the best clock-alignment signal there is, but nothing
        # reads it yet.
        "-an",
        "-c:v",
        profile.encoder,
        *encoder_arguments(profile.encoder),
        "-b:v",
        profile.bitrate,
        # Constant frame rate is not a preference. Frame index has to be
        # convertible to time by arithmetic, and a webcam left to its own
        # devices delivers a variable rate that makes that impossible.
        "-fps_mode",
        "cfr",
        "-r",
        _rate(camera.fps),
        "-g",
        str(keyframe_interval),
        "-f",
        "segment",
        "-segment_time",
        _seconds(profile.segment_seconds),
        "-segment_format",
        "matroska",
        # Each segment starts its own timeline, which is what the concat
        # demuxer expects to be given.
        "-reset_timestamps",
        "1",
        # FFmpeg's own index of what it wrote and when: one line per segment,
        # flushed as each one closes. Cheaper and far more reliable than
        # watching a directory and guessing at durations.
        "-segment_list",
        _ffmpeg_path(segment_list),
        "-segment_list_type",
        "csv",
        "-segment_list_flags",
        "+live",
        "-y",
        _ffmpeg_path(segment_pattern),
    )


def concat_command(
    concat_file: Path,
    destination: Path,
    binary: str = "ffmpeg",
) -> tuple[str, ...]:
    """Join whole segments into one clip without re-encoding anything.

    A stream copy is the most robust thing FFmpeg can be asked to do, and it is
    all that is needed: the clip is a whole number of segments and its metadata
    says exactly where impact falls inside it. Trimming to the requested window
    would cost precision in the metadata to gain tidiness in the file, and the
    metadata is the part something else has to rely on.
    """

    return (
        binary,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-f",
        "concat",
        # The list holds absolute paths, which the demuxer refuses by default.
        "-safe",
        "0",
        "-i",
        _ffmpeg_path(concat_file),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-y",
        _ffmpeg_path(destination),
    )


def still_command(
    source: Path,
    offset_seconds: float,
    destination: Path,
    width: int = 512,
    binary: str = "ffmpeg",
) -> tuple[str, ...]:
    """Decode one frame from a clip and write it as a JPEG.

    `-ss` goes before `-i` so FFmpeg seeks the container rather than decoding
    from the start and discarding: on a two-second clip the difference is
    unimportant, but the flag order is the difference between a seek and a scan
    and is worth being deliberate about.

    Scaled down here rather than by whoever consumes it. A frame is being
    prepared to be looked at, and a 1280-pixel-wide still costs several times
    what a 512-pixel one does to send while showing the same swing.

    `-2` for the height keeps the aspect ratio and rounds to an even number,
    which JPEG's chroma subsampling requires.
    """

    return (
        binary,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-ss",
        _seconds(offset_seconds),
        "-i",
        _ffmpeg_path(source),
        "-frames:v",
        "1",
        "-vf",
        f"scale={int(width)}:-2",
        "-q:v",
        "4",
        "-y",
        _ffmpeg_path(destination),
    )


def concat_file_contents(paths: Sequence[Path]) -> str:
    """The concat demuxer's playlist format."""

    return "".join(f"file '{_escape(path)}'\n" for path in paths)


def is_available(binary: str = "ffmpeg") -> bool:
    """Whether FFmpeg can be found at all.

    Absence is a supported way to run CaddieJ, so this is asked once at startup
    and answered with a log line rather than an exception.
    """

    return shutil.which(binary) is not None


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stderr: str


class Process(Protocol):
    """A running FFmpeg, as the recorder needs to see one."""

    async def wait(self) -> int: ...

    async def stop(self, grace_seconds: float = 3.0) -> int: ...

    def stderr_tail(self) -> str: ...


Spawn = Callable[[Sequence[str]], Awaitable[Process]]
Run = Callable[[Sequence[str], float], Awaitable[CommandResult]]


class FFmpegProcess:
    """A long-running FFmpeg, and the pipe that has to keep being emptied.

    The draining task is not optional. A subprocess whose stderr is piped and
    never read blocks as soon as the pipe buffer fills, which for a recorder
    left running for an evening means capture silently stopping.
    """

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._lines: deque[str] = deque(maxlen=_STDERR_LINES)
        self._drain = asyncio.create_task(self._read_stderr(), name="ffmpeg-stderr")

    @property
    def pid(self) -> int:
        return self._process.pid

    async def _read_stderr(self) -> None:
        stream = self._process.stderr
        if stream is None:
            return
        with suppress(Exception):
            while line := await stream.readline():
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    self._lines.append(text)

    def stderr_tail(self) -> str:
        return "\n".join(self._lines)

    async def wait(self) -> int:
        """Wait for the exit code. Safe to call from more than one place.

        Which it is: the recorder watches the process while `stop` also waits
        for it to go. So this must not do anything that a second caller would
        find already done — hence `asyncio.wait` on the reader rather than
        `wait_for`, which raises if the task it is given has been cancelled.
        """

        code = await self._process.wait()
        if not self._drain.done():
            # Let the reader finish what is already buffered, so an exit can be
            # explained by the lines that preceded it.
            done, _pending = await asyncio.wait({self._drain}, timeout=1.0)
            if not done:
                self._drain.cancel()
        return code

    async def stop(self, grace_seconds: float = 3.0) -> int:
        """Ask FFmpeg to finish, then insist.

        SIGTERM first, because it gives FFmpeg the chance to close the segment
        it is writing rather than leaving a torn file behind.
        """

        if self._process.returncode is not None:
            return self._process.returncode

        with suppress(ProcessLookupError):
            self._process.terminate()
        try:
            return await asyncio.wait_for(self.wait(), grace_seconds)
        except TimeoutError:
            logger.warning("ffmpeg_kill_after_grace", extra={"ffmpeg_exit": "timeout"})
            with suppress(ProcessLookupError):
                self._process.kill()
            return await self.wait()


async def spawn(args: Sequence[str]) -> Process:
    """Start a long-running FFmpeg."""

    return FFmpegProcess(await _start_ffmpeg(args))


async def run(args: Sequence[str], timeout: float = 30.0) -> CommandResult:
    """Run a short FFmpeg to completion, killing it if it overstays.

    Used for clip extraction. The timeout is what stops one unreadable segment
    from leaving a process behind for the rest of the evening.

    Cancellation is handled the same way as the timeout, and for the same
    reason. Shutdown cancels the clip worker part way through an extraction, and
    without this the coroutine would unwind while leaving FFmpeg running — with
    CaddieJ gone, nothing left to reap it, and a half-written file in the media
    directory that it goes on writing.
    """

    process = await _start_ffmpeg(args)
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        with suppress(ProcessLookupError):
            process.kill()
        # Awaited, not fired and forgotten: this is what collects the child.
        # Safe during cancellation because the `CancelledError` has already been
        # caught, so this await is not immediately cancelled again.
        with suppress(Exception):
            await process.wait()
        raise
    return CommandResult(
        returncode=process.returncode or 0,
        stderr=stderr.decode("utf-8", "replace").strip(),
    )


def _ffmpeg_path(path: Path) -> str:
    """A path FFmpeg will not misread on Windows.

    The concat demuxer treats backslash as an escape, so a native Windows path
    written into a playlist or an `-i` argument would lose separators. Forward
    slashes are accepted on every platform FFmpeg runs on.
    """

    return path.as_posix()


def _escape(path: Path) -> str:
    return _ffmpeg_path(path).replace("'", r"'\''")


async def _start_ffmpeg(args: Sequence[str]) -> asyncio.subprocess.Process:
    """Start FFmpeg without a shell, and without a console window on Windows."""

    if sys.platform == "win32":
        return await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    return await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )


def _rate(fps: float) -> str:
    return str(int(fps)) if float(fps).is_integer() else str(fps)


def _seconds(value: float) -> str:
    return f"{value:g}"
