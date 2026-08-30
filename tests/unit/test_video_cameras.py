"""Cameras are descriptions, so what they describe is worth pinning down.

These read like assertions about strings because that is exactly what a camera
is here: the arguments FFmpeg will be given. Getting one of them wrong is the
difference between recording and a process that exits immediately, and it is not
the kind of mistake a running system reports clearly.
"""

import pytest

from caddiej.video.cameras import (
    avfoundation_camera,
    build_camera,
    dshow_camera,
    synthetic_camera,
    v4l2_camera,
)


def joined(camera: object) -> str:
    return " ".join(camera.input_arguments)  # type: ignore[attr-defined]


def test_a_synthetic_camera_generates_a_pattern_at_the_asked_for_size() -> None:
    camera = synthetic_camera(fps=60, width=640, height=360)

    assert "testsrc2=size=640x360:rate=60" in joined(camera)
    assert camera.fps == 60


def test_a_synthetic_camera_is_paced_in_real_time() -> None:
    # Without -re a generated source runs as fast as the CPU allows, so an hour
    # of footage would appear in seconds and nothing about a rolling buffer
    # would resemble reality.
    assert synthetic_camera().input_arguments[0] == "-re"


def test_a_real_camera_is_not_paced() -> None:
    # A device delivers frames at its own rate; -re would throttle it and build
    # a backlog that ends in dropped frames.
    assert "-re" not in avfoundation_camera().input_arguments


def test_a_macos_camera_names_the_device_and_the_format() -> None:
    camera = avfoundation_camera(device="1", fps=30, width=1920, height=1080)

    assert joined(camera) == (
        "-f avfoundation -framerate 30 -video_size 1920x1080 -i 1"
    )


def test_a_linux_camera_names_the_device_node() -> None:
    assert "-f v4l2" in joined(v4l2_camera(device="/dev/video2"))
    assert joined(v4l2_camera(device="/dev/video2")).endswith("-i /dev/video2")


def test_a_windows_camera_uses_the_directshow_naming() -> None:
    # DirectShow needs the "video=" prefix; without it FFmpeg looks for a file.
    assert joined(dshow_camera(device="HD Pro Webcam")).endswith(
        "-i video=HD Pro Webcam"
    )


def test_a_frame_rate_that_is_whole_is_written_without_a_decimal() -> None:
    # FFmpeg accepts 30.0, but a fractional rate in the command reads as a
    # mistake when somebody is comparing it to what their camera supports.
    assert "-framerate 30 " in joined(avfoundation_camera(fps=30.0)) + " "
    assert "-framerate 29.97 " in joined(avfoundation_camera(fps=29.97)) + " "


def test_a_camera_is_built_from_configuration() -> None:
    camera = build_camera(
        "avfoundation", device="0", camera_id="face", fps=120, width=800, height=600
    )

    assert camera.camera_id == "face"
    assert camera.fps == 120
    assert "-f avfoundation" in joined(camera)


def test_an_unknown_camera_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="Unknown camera kind"):
        build_camera("thermal")  # type: ignore[arg-type]


def test_a_camera_latency_survives_into_the_spec() -> None:
    # It is the one number that decides whether a clip's impact frame is
    # trustworthy, so it must not be dropped on the way through.
    camera = build_camera("v4l2", latency_seconds=0.08)

    assert camera.latency_seconds == 0.08


@pytest.mark.parametrize(
    ("field", "value"),
    [("fps", 0), ("width", 0), ("height", -1)],
)
def test_a_camera_that_cannot_produce_pictures_is_refused(
    field: str, value: int
) -> None:
    with pytest.raises(ValueError):
        synthetic_camera(**{field: value})  # type: ignore[arg-type]


def test_a_camera_needs_an_id() -> None:
    with pytest.raises(ValueError, match="needs an id"):
        synthetic_camera(camera_id="")
