"""One OpenCV 4 distribution is the supported cv2."""

from __future__ import annotations

import pytest

from caddiej.swing_evidence import images


def test_the_installed_build_is_opencv4() -> None:
    images.require_opencv4()
    assert len(images.opencv_providers()) == 1


def test_a_second_provider_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        images,
        "opencv_providers",
        lambda: ("opencv-contrib-python", "opencv-python-headless"),
    )
    with pytest.raises(RuntimeError, match="more than one"):
        images.require_opencv4()


def test_opencv5_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(images.cv2, "__version__", "5.0.0")
    monkeypatch.setattr(images, "opencv_providers", lambda: ("opencv-contrib-python",))
    with pytest.raises(RuntimeError, match="OpenCV 5"):
        images.require_opencv4()
