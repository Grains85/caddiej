"""The review bundle must not depend on a Unix-only zip(1) install.

Git for Windows bash is a supported way to produce the archive, but it does not
ship `zip`. The packager has to pick a backend that preserves repository-relative
paths and the same exclusions as collect_packaged_files — otherwise a Windows
review zip either fails to build or quietly contains .env.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "package-project.sh"


def bash_executable() -> str | None:
    if sys.platform == "win32":
        for candidate in (
            Path(r"C:\Program Files\Git\bin\bash.exe"),
            Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
        ):
            if candidate.exists():
                return str(candidate)
    return shutil.which("bash")


def bash_path(path: Path) -> str:
    posix = path.resolve().as_posix()
    if sys.platform == "win32" and len(posix) >= 2 and posix[1] == ":":
        return f"/{posix[0].lower()}{posix[2:]}"
    return posix


BASH = bash_executable()
needs_bash = pytest.mark.skipif(
    BASH is None, reason="bash is required to exercise package-project.sh"
)


def run_bash(
    script: str, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        [BASH, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=merged,
    )


def write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@needs_bash
class TestArchiveBackendSelection:
    def test_unix_zip_is_preferred_when_it_is_on_path(self, tmp_path: Path) -> None:
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        write_executable(fake_bin / "zip", "#!/bin/sh\nexit 0\n")
        write_executable(fake_bin / "unzip", "#!/bin/sh\nexit 0\n")

        result = run_bash(
            f"""
            set -euo pipefail
            PATH="{bash_path(fake_bin)}:$PATH"
            unset CADDIEJ_ZIP_BACKEND CADDIEJ_UNZIP_BACKEND
            # shellcheck disable=SC1091
            source "{bash_path(SCRIPT)}"
            select_zip_backend
            select_unzip_backend
            printf 'zip=%s unzip=%s\\n' "$ZIP_BACKEND" "$UNZIP_BACKEND"
            """,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "zip=zip unzip=unzip"

    def test_windows_falls_back_to_powershell_when_zip_is_missing(
        self, tmp_path: Path
    ) -> None:
        empty_bin = tmp_path / "empty"
        empty_bin.mkdir()

        result = run_bash(
            f"""
            set -euo pipefail
            PATH="{bash_path(empty_bin)}:/usr/bin:/bin"
            unset CADDIEJ_ZIP_BACKEND CADDIEJ_UNZIP_BACKEND
            # shellcheck disable=SC1091
            source "{bash_path(SCRIPT)}"
            select_zip_backend
            select_unzip_backend
            printf 'windows=%s zip_present=%s unzip_present=%s zip=%s unzip=%s powershell=%s\\n' \\
              "$(is_windows_bash && echo yes || echo no)" \\
              "$(command -v zip >/dev/null && echo yes || echo no)" \\
              "$(command -v unzip >/dev/null && echo yes || echo no)" \\
              "$ZIP_BACKEND" "$UNZIP_BACKEND" "$POWERSHELL_CMD"
            """,
        )

        assert result.returncode == 0, result.stderr
        parts = _parse_backend_line(result.stdout)
        if parts["windows"] == "yes" and parts["zip_present"] == "no":
            assert parts["zip"] == "powershell"
            assert parts["powershell"]
        elif parts["zip_present"] == "yes":
            assert parts["zip"] == "zip"
        else:
            assert parts["zip"] == "missing"

        if parts["unzip_present"] == "yes":
            assert parts["unzip"] == "unzip"
        elif parts["windows"] == "yes":
            assert parts["unzip"] == "powershell"
        else:
            assert parts["unzip"] == "missing"


def _parse_backend_line(stdout: str) -> dict[str, str]:
    line = stdout.strip().splitlines()[-1]
    return dict(item.split("=", 1) for item in line.split())


def _seed_fixture(root: Path) -> None:
    (root / "src" / "nested").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / ".git").mkdir()
    (root / ".venv" / "lib").mkdir(parents=True)
    (root / "media").mkdir()
    (root / "dist").mkdir()

    (root / "src" / "hello.py").write_text("print('hello')\n", encoding="utf-8")
    (root / "src" / "nested" / "a file.py").write_text(
        "print('space')\n", encoding="utf-8"
    )
    (root / "tests" / "test_hello.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'fixture'\n", encoding="utf-8"
    )
    (root / "ARCHITECTURE.md").write_text("# Architecture\n", encoding="utf-8")
    (root / ".env.example").write_text("OPENAI_API_KEY=\n", encoding="utf-8")

    (root / ".env").write_text("OPENAI_API_KEY=sk-secret\n", encoding="utf-8")
    (root / ".env.local").write_text("OBS_PASSWORD=secret\n", encoding="utf-8")
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (root / ".venv" / "lib" / "site.py").write_text("# venv\n", encoding="utf-8")
    (root / "media" / "clip.mp4").write_text("not-a-video", encoding="utf-8")
    (root / ".caddiej" / "diagnostics" / "video-frames" / "stamp").mkdir(parents=True)
    (
        root / ".caddiej" / "diagnostics" / "video-frames" / "stamp" / "frame-01.jpg"
    ).write_bytes(b"not-a-real-frame")
    (root / "dist" / "old.zip").write_text("old", encoding="utf-8")
    (root / "caddiej.sqlite3").write_text("db", encoding="utf-8")


def _assert_packaged_entries(listing: Path) -> set[str]:
    entries = {
        line.strip().replace("\\", "/")
        for line in listing.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }

    assert "src/hello.py" in entries
    assert "src/nested/a file.py" in entries
    assert "tests/test_hello.py" in entries
    assert "pyproject.toml" in entries
    assert "ARCHITECTURE.md" in entries
    assert ".env.example" in entries

    assert not any(entry == ".env" or entry.endswith("/.env") for entry in entries)
    assert not any(entry.startswith(".git/") for entry in entries)
    assert not any(entry.startswith(".venv/") for entry in entries)
    assert not any(entry.startswith("media/") for entry in entries)
    assert not any(entry.startswith(".caddiej/") for entry in entries)
    assert not any(entry.startswith("dist/") for entry in entries)
    assert "caddiej.sqlite3" not in entries
    assert ".env.local" not in entries
    return entries


def _package_fixture(
    tmp_path: Path, extra_commands: str = ""
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    fixture = tmp_path / "repo"
    fixture.mkdir()
    _seed_fixture(fixture)
    archive = tmp_path / "review.zip"
    listing = tmp_path / "listing.txt"
    text_bundle = tmp_path / "review.txt"

    result = run_bash(
        f"""
        set -euo pipefail
        {extra_commands}
        # shellcheck disable=SC1091
        source "{bash_path(SCRIPT)}"
        REPO_ROOT="{bash_path(fixture)}"
        ARCHIVE_PATH="{bash_path(archive)}"
        TEXT_BUNDLE_PATH="{bash_path(text_bundle)}"
        ensure_archive_backends
        create_archive
        list_archive_entries > "{bash_path(listing)}"
        printf 'create=%s list=%s\\n' "$ZIP_BACKEND" "$UNZIP_BACKEND"
        """,
    )
    return result, archive, listing


@needs_bash
class TestArchiveSemantics:
    def test_selected_backend_keeps_relative_paths_and_exclusions(
        self, tmp_path: Path
    ) -> None:
        result, archive, listing = _package_fixture(
            tmp_path, "unset CADDIEJ_ZIP_BACKEND CADDIEJ_UNZIP_BACKEND"
        )
        if result.returncode != 0 and "neither zip nor Windows PowerShell" in (
            result.stderr + result.stdout
        ):
            pytest.skip("no ZIP backend available on this machine")

        assert result.returncode == 0, result.stderr + result.stdout
        assert archive.is_file()
        _assert_packaged_entries(listing)

        backends = result.stdout.strip().splitlines()[-1]
        create_backend = dict(item.split("=", 1) for item in backends.split())["create"]
        if sys.platform == "win32":
            assert create_backend in {"zip", "powershell"}
        else:
            assert create_backend == "zip"

    @pytest.mark.skipif(
        sys.platform != "win32", reason="PowerShell ZIP fallback is Windows-specific"
    )
    def test_powershell_create_and_list_keep_the_same_exclusions(
        self, tmp_path: Path
    ) -> None:
        result, archive, listing = _package_fixture(
            tmp_path,
            "export CADDIEJ_ZIP_BACKEND=powershell CADDIEJ_UNZIP_BACKEND=powershell",
        )

        assert result.returncode == 0, result.stderr + result.stdout
        assert archive.is_file()
        _assert_packaged_entries(listing)
        backends = dict(
            item.split("=", 1)
            for item in result.stdout.strip().splitlines()[-1].split()
        )
        assert backends["create"] == "powershell"
        assert backends["list"] == "powershell"
