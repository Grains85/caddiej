"""The video subsystem's boundaries, enforced rather than documented.

Two claims are made about this subsystem elsewhere, and both are the kind that
quietly stops being true. FFmpeg lives in one file, so that replacing it — or
adding a second camera, or a different container — is a change in one place. And
the domain does not know cameras exist, so a clip is a fact about a shot rather
than an artefact of a subsystem.
"""

import ast
import inspect
from pathlib import Path

import pytest

import caddiej.domain.shot as domain_shot

SOURCE_ROOT = Path(inspect.getfile(domain_shot)).parents[1]
# The one file allowed to know what an FFmpeg argument looks like, and the two
# that legitimately hold a command they were handed.
FFMPEG_HOME = "video/ffmpeg.py"


def python_files(relative: str) -> list[Path]:
    return sorted((SOURCE_ROOT / relative).rglob("*.py"))


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


@pytest.mark.parametrize("path", python_files("domain"))
def test_the_domain_does_not_know_cameras_exist(path: Path) -> None:
    # A clip is a fact about a shot. If the domain had to import the video
    # subsystem to describe one, every consumer of a shot would inherit FFmpeg.
    leaks = {module for module in imported_modules(path) if "caddiej.video" in module}

    assert not leaks, f"{path.name} imports {leaks}"


@pytest.mark.parametrize("path", python_files("."))
def test_only_one_file_builds_ffmpeg_arguments(path: Path) -> None:
    relative = path.relative_to(SOURCE_ROOT).as_posix()
    if relative == FFMPEG_HOME:
        return

    source = path.read_text(encoding="utf-8")
    # Any literal that looks like an FFmpeg flag. Not exhaustive, and not meant
    # to be: it catches the realistic way this leaks, which is somebody adding
    # "just one flag" where they happen to be working.
    offenders = [
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith(("-c:v", "-segment_", "-movflags", "-fps_mode"))
    ]

    assert not offenders, f"{relative} builds FFmpeg arguments: {offenders}"


def test_nothing_outside_the_subsystem_runs_a_subprocess_for_video() -> None:
    # Spawning FFmpeg from anywhere else would put an unsupervised process
    # outside the one place that knows how to stop it.
    offenders = []
    for path in python_files("."):
        relative = path.relative_to(SOURCE_ROOT).as_posix()
        if relative.startswith("video/"):
            continue
        if "asyncio.create_subprocess_exec" in path.read_text(encoding="utf-8"):
            offenders.append(relative)

    assert not offenders


def test_the_application_never_reaches_past_the_service() -> None:
    # Wiring imports the subsystem's front door and nothing behind it: the
    # recorder, the extractor and the segment index are its own business.
    internals = ("video.recorder", "video.segments", "video.clips", "video.ffmpeg")

    leaks = {
        module
        for module in imported_modules(SOURCE_ROOT / "app.py")
        if module.endswith(internals)
    }

    assert not leaks
