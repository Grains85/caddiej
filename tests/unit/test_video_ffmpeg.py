"""The two FFmpeg commands, checked flag by flag.

This is the request-mapping test for this subsystem. An FFmpeg command that is
subtly wrong does not fail loudly: it records at a variable frame rate, or writes
segments that cannot be joined, and the damage is discovered a week later in a
clip that will not line up.
"""

import asyncio
import sys
from pathlib import Path

import pytest

from caddiej.video.cameras import synthetic_camera
from caddiej.video.ffmpeg import (
    CaptureProfile,
    capture_command,
    concat_command,
    concat_file_contents,
    encoder_arguments,
    run,
)

CAMERA = synthetic_camera(fps=30, width=1280, height=720)


def capture(profile: CaptureProfile | None = None, camera=CAMERA) -> tuple[str, ...]:
    return capture_command(
        camera,
        profile or CaptureProfile(),
        Path("/media/buffer/tee/seg-%06d.mkv"),
        Path("/media/buffer/tee/segments.csv"),
    )


def value_after(args: tuple[str, ...], flag: str) -> str:
    return args[args.index(flag) + 1]


def output_value_after(args: tuple[str, ...], flag: str) -> str:
    """The last occurrence of a flag, which is the one applying to the output.

    `-f` appears twice in a capture command: once naming the input format and
    once naming the output muxer.
    """

    return args[len(args) - 1 - tuple(reversed(args)).index(flag) + 1]


# --- capture --------------------------------------------------------------


def test_capture_records_at_a_constant_frame_rate() -> None:
    # Not a preference. Frame index has to convert to time by arithmetic, and a
    # camera left to itself delivers a variable rate that makes that impossible.
    args = capture()

    assert value_after(args, "-fps_mode") == "cfr"
    assert value_after(args, "-r") == "30"


def test_capture_writes_segments_with_an_index_beside_them() -> None:
    args = capture()

    assert output_value_after(args, "-f") == "segment"
    assert value_after(args, "-segment_format") == "matroska"
    assert value_after(args, "-segment_list").endswith("segments.csv")
    assert value_after(args, "-segment_list_type") == "csv"
    # Without +live the index is only written when FFmpeg exits, which for a
    # recorder that runs all evening means never.
    assert value_after(args, "-segment_list_flags") == "+live"
    assert args[-1].endswith("seg-%06d.mkv")


def test_each_segment_starts_its_own_timeline() -> None:
    # The concat demuxer expects it, and joining segments is the whole point.
    assert value_after(capture(), "-reset_timestamps") == "1"


def test_segments_are_matroska_rather_than_mp4() -> None:
    # A Matroska file interrupted by a crash is still readable; an MP4 whose
    # index was never written is not, and the interrupted segment is exactly the
    # one covering the last shot.
    assert value_after(capture(), "-segment_format") == "matroska"


def test_the_keyframe_interval_is_expressed_in_frames() -> None:
    args = capture(CaptureProfile(keyframe_seconds=0.5))

    # 30 fps, a keyframe every half second.
    assert value_after(args, "-g") == "15"


def test_a_keyframe_interval_never_rounds_down_to_zero() -> None:
    # A GOP of zero is rejected by encoders, and a slow camera with a short
    # keyframe interval is exactly how you would get there.
    args = capture(CaptureProfile(keyframe_seconds=0.1), camera=synthetic_camera(fps=5))

    assert value_after(args, "-g") == "1"


def test_capture_records_no_audio() -> None:
    # Nothing reads it yet. When something does, the impact transient will be
    # the cheapest clock alignment available.
    assert "-an" in capture()


def test_capture_does_not_compete_for_the_terminal() -> None:
    # FFmpeg reads stdin for interactive commands unless told not to, and it is
    # sharing a terminal with CaddieJ.
    assert "-nostdin" in capture()


def test_the_software_encoder_is_told_not_to_spend_the_cpu() -> None:
    # Left at its defaults libx264 will happily out-bid the golf simulator for
    # CPU on a machine that is also driving a projector.
    assert encoder_arguments("libx264") == (
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
    )


def test_a_hardware_encoder_is_left_alone() -> None:
    # Its flags are not x264's, and it is already cheap.
    assert encoder_arguments("h264_videotoolbox") == ()


def test_the_chosen_encoder_reaches_the_command() -> None:
    args = capture(CaptureProfile(encoder="h264_videotoolbox", bitrate="12M"))

    assert value_after(args, "-c:v") == "h264_videotoolbox"
    assert value_after(args, "-b:v") == "12M"
    assert "-preset" not in args


def test_the_binary_can_be_somewhere_else() -> None:
    args = capture_command(
        CAMERA,
        CaptureProfile(),
        Path("seg-%06d.mkv"),
        Path("segments.csv"),
        binary="/opt/homebrew/bin/ffmpeg",
    )

    assert args[0] == "/opt/homebrew/bin/ffmpeg"


# --- profile ---------------------------------------------------------------


def test_a_buffer_shorter_than_a_segment_is_refused() -> None:
    # It could hold nothing, and would report as "not recording" while
    # recording perfectly well.
    with pytest.raises(ValueError, match="at least one segment"):
        CaptureProfile(segment_seconds=2.0, buffer_seconds=1.0)


@pytest.mark.parametrize("field", ["segment_seconds", "keyframe_seconds"])
def test_a_profile_needs_positive_intervals(field: str) -> None:
    with pytest.raises(ValueError):
        CaptureProfile(**{field: 0})  # type: ignore[arg-type]


# --- extraction ------------------------------------------------------------


def test_a_clip_is_copied_rather_than_re_encoded() -> None:
    # A stream copy is the most robust thing FFmpeg can be asked for, and the
    # clip's metadata carries the precision that a cut would have provided.
    args = concat_command(Path("/tmp/list.txt"), Path("/media/clips/x.mp4"))

    assert value_after(args, "-c") == "copy"
    assert value_after(args, "-f") == "concat"
    assert args[-1] == "/media/clips/x.mp4"


def test_the_concat_demuxer_is_allowed_absolute_paths() -> None:
    # It refuses them by default, and every path in the list is absolute.
    assert value_after(concat_command(Path("l.txt"), Path("o.mp4")), "-safe") == "0"


def test_a_clip_is_written_for_playing_rather_than_for_streaming() -> None:
    assert "+faststart" in concat_command(Path("l.txt"), Path("o.mp4"))


def test_the_concat_list_names_one_file_per_line() -> None:
    contents = concat_file_contents([Path("/media/a.mkv"), Path("/media/b.mkv")])

    assert contents == "file '/media/a.mkv'\nfile '/media/b.mkv'\n"


def test_a_quote_in_a_path_cannot_break_out_of_the_concat_list() -> None:
    contents = concat_file_contents([Path("/media/it's here.mkv")])

    assert contents == "file '/media/it'\\''s here.mkv'\n"


def test_concat_paths_never_contain_backslashes() -> None:
    # FFmpeg's concat demuxer treats `\` as an escape. A Windows path written
    # with os.sep would be misread; forward slashes are accepted everywhere.
    contents = concat_file_contents([Path("media") / "buffer" / "seg-000001.mkv"])

    assert "\\" not in contents
    assert contents == "file 'media/buffer/seg-000001.mkv'\n"


# --- running one -----------------------------------------------------------
#
# The only tests here that start a process, and they start the interpreter
# rather than FFmpeg: what is being checked is that CaddieJ does not leave a
# child behind, which has nothing to do with what the child was.


async def watched(
    monkeypatch: pytest.MonkeyPatch,
) -> list[asyncio.subprocess.Process]:
    """Collect whatever `run` starts, so a test can ask what became of it."""

    started: list[asyncio.subprocess.Process] = []
    create = asyncio.create_subprocess_exec

    async def spy(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        process = await create(*args, **kwargs)  # type: ignore[arg-type]
        started.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    return started


def sleeping(seconds: int = 30) -> tuple[str, ...]:
    return (sys.executable, "-c", f"import time; time.sleep({seconds})")


async def test_a_cancelled_run_takes_the_process_with_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown cancels the clip worker part way through an extraction. Unwinding
    without this leaves FFmpeg running with CaddieJ gone — nothing left to reap
    it, and a half-written file in the media directory that it goes on writing.
    """

    started = await watched(monkeypatch)
    extracting = asyncio.create_task(run(sleeping(), timeout=30.0))
    await asyncio.sleep(0.2)
    assert started, "the process never started"

    extracting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await extracting

    assert started[0].returncode is not None, "the process was left running"


async def test_a_run_that_overstays_is_killed_and_collected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = await watched(monkeypatch)

    with pytest.raises(TimeoutError):
        await run(sleeping(), timeout=0.2)

    assert started[0].returncode is not None
