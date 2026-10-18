"""The command line, which is the first thing anybody meets.

Two failures matter here and neither is about golf. A person who has mistyped a
setting must be told which one and why, in a sentence, without a credential
being printed back at them. And a person who cannot tell whether CaddieJ is
misconfigured or simply switched off must be able to ask.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from caddiej import __version__
from caddiej.__main__ import main
from caddiej.cli import build_parser, describe, explain, resolve_log_level
from caddiej.config import Settings


def a_settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


class TestTheCommandLine:
    def test_help_does_not_start_anything(self, capsys) -> None:
        # Somebody typing --help has not decided to run CaddieJ yet, and starting
        # the application would write a database into whatever directory they
        # happened to be standing in.
        with pytest.raises(SystemExit) as exit_info:
            main(["--help"])

        assert exit_info.value.code == 0
        printed = capsys.readouterr().out
        assert "usage: caddiej" in printed
        assert "--check-config" in printed
        assert "--check-speech" in printed
        assert "--check-video-frames" in printed

    def test_version_is_the_installed_version(self, capsys) -> None:
        with pytest.raises(SystemExit) as exit_info:
            main(["--version"])

        assert exit_info.value.code == 0
        assert capsys.readouterr().out.strip() == f"caddiej {__version__}"

    def test_an_unknown_option_is_refused_rather_than_ignored(self) -> None:
        # It used to start the application and ignore the flag, which reads as
        # the flag having been accepted.
        with pytest.raises(SystemExit) as exit_info:
            build_parser().parse_args(["--projector"])

        assert exit_info.value.code == 2


class TestHowLoudItIs:
    def test_the_flag_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("LOG_LEVEL", "ERROR")

        assert resolve_log_level("DEBUG") == "DEBUG"

    def test_the_environment_is_used_when_no_flag_is_given(self, monkeypatch) -> None:
        monkeypatch.setenv("LOG_LEVEL", "warning")

        assert resolve_log_level(None) == "WARNING"

    def test_nonsense_falls_back_to_info(self, monkeypatch) -> None:
        # A typo in LOG_LEVEL must not be a reason to fail to start: the point of
        # the setting is to help somebody debug, not to become the thing they are
        # debugging.
        monkeypatch.setenv("LOG_LEVEL", "chatty")

        assert resolve_log_level(None) == "INFO"


class TestReportingABadSetting:
    def error_from(self, **environment) -> ValidationError:
        with pytest.raises(ValidationError) as caught:
            Settings(_env_file=None, **environment)
        return caught.value

    def test_it_names_the_variable_and_says_what_was_wrong(self) -> None:
        message = explain(self.error_from(COACHING_PROVIDER="gemini"))

        assert "COACHING_PROVIDER='gemini'" in message
        # Including what it would have accepted, because "invalid" on its own
        # sends somebody to the source.
        assert "'none', 'openai' or 'scripted'" in message
        assert "1 setting is invalid" in message

    def test_every_bad_setting_is_reported_at_once(self) -> None:
        message = explain(self.error_from(COACHING_PROVIDER="gemini", HUD_PORT="99999"))

        assert "2 settings are invalid" in message
        assert "COACHING_PROVIDER" in message
        assert "HUD_PORT" in message

    def test_it_says_where_settings_come_from(self) -> None:
        # The commonest confusion is not knowing that .env in the working
        # directory is read at all.
        message = explain(self.error_from(HUD_PORT="-1"))

        assert ".env" in message
        assert "docs/configuration.md" in message

    def test_a_credential_is_never_printed_back(self) -> None:
        # A mistyped key is still a key, and a terminal keeps scrollback.
        message = explain(self.error_from(COACHING_API_KEY=["sk-not-a-string"]))

        assert "COACHING_API_KEY" in message
        assert "sk-not-a-string" not in message

    def test_the_process_exits_without_a_traceback(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        (tmp_path / ".env").write_text("COACHING_PROVIDER=gemini\n")
        monkeypatch.chdir(tmp_path)

        with pytest.raises(SystemExit) as exit_info:
            main([])

        assert exit_info.value.code == 2
        printed = capsys.readouterr()
        assert "COACHING_PROVIDER" in printed.err
        assert "Traceback" not in printed.err
        assert "pydantic" not in printed.err


class TestCheckingTheConfiguration:
    def test_it_reports_what_is_on_and_what_is_off(self) -> None:
        report = describe(a_settings(GSPRO_MODE="simulate", VIDEO_ENABLED="false"))

        assert "simulated round" in report
        assert "off (VIDEO_ENABLED=false)" in report
        assert "Configuration is valid." in report

    def test_observe_mode_says_it_does_not_listen(self) -> None:
        report = describe(a_settings(GSPRO_MODE="observe"))

        assert "passive observation" in report
        assert "does not listen on 9210" in report

    def test_it_resolves_where_files_will_be_written(self, tmp_path) -> None:
        # "caddiej.sqlite3" does not say where, and the answer depends on which
        # directory CaddieJ was started from.
        report = describe(a_settings(), root=tmp_path)

        assert str(tmp_path / "caddiej.sqlite3") in report

    def test_it_shows_the_shot_type_the_session_will_use(self) -> None:
        assert "shot type unknown" in describe(a_settings())
        assert "shot type stock" in describe(a_settings(COACHING_SHOT_TYPE="stock"))

    def test_it_says_whether_a_credential_is_set_but_never_what_it_is(self) -> None:
        report = describe(
            a_settings(
                COACHING_PROVIDER="openai",
                COACHING_API_KEY="sk-the-actual-key",
                LIGHTING_ENABLED="true",
                LIGHTING_APPLICATION_KEY="the-actual-bridge-key",
                LIGHTING_BRIDGE_HOST="10.0.0.5",
            )
        )

        assert "COACHING_API_KEY set" in report
        assert "LIGHTING_APPLICATION_KEY set" in report
        assert "sk-the-actual-key" not in report
        assert "the-actual-bridge-key" not in report

    def test_a_missing_key_is_called_out_before_anything_starts(self) -> None:
        report = describe(a_settings(COACHING_PROVIDER="openai"))

        assert "COACHING_API_KEY NOT SET" in report

    def test_an_assistant_api_that_will_refuse_to_open_says_so(self) -> None:
        # Enabled with no token is refused at start-up. Finding that out from a
        # check is better than finding it out from a log line after wondering
        # why the port is closed.
        report = describe(a_settings(ASSISTANT_API_ENABLED="true"))

        assert "REFUSED" in report
        assert "ASSISTANT_API_TOKEN is empty" in report

    def test_the_shot_video_delay_says_which_moment_it_counts_from(self) -> None:
        # The same 0.75 means two very different things depending on whether it
        # runs from impact or from GSPro's completed shot, and the difference is
        # several seconds of the player already having walked away.
        report = describe(a_settings(FLOOR_SHOT_VIDEO_ENABLED="true", OBS_PASSWORD="x"))

        assert "0.75s after impact" in report

    def test_a_shot_video_with_no_protee_observer_will_never_fire(self) -> None:
        # Impact comes from the ProTee log. Without that observer the replay is
        # enabled, configured, credentialed, and silent.
        report = describe(
            a_settings(
                FLOOR_SHOT_VIDEO_ENABLED="true",
                OBS_PASSWORD="x",
                PROTEE_OBSERVER_ENABLED="false",
            )
        )

        assert "needs PROTEE_OBSERVER_ENABLED" in report

    def test_it_reports_which_optional_dependencies_are_installed(self) -> None:
        report = describe(a_settings())

        assert "Optional dependencies:" in report
        assert "kokoro" in report
        assert "ffmpeg" in report

    def test_checking_does_not_run_the_application(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        # No port is bound and no database is created: a check is a question.
        monkeypatch.chdir(tmp_path)

        main(["--check-config"])

        assert "configuration check" in capsys.readouterr().out
        assert list(tmp_path.iterdir()) == []


class TestCheckingSpeech:
    def test_it_does_not_start_the_application(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """No HUD, no observers, no database: a check is a question that
        happens to make a noise."""

        async def fake_probe(**_kwargs: object):
            from caddiej.speech.diagnose import SpeechProbe

            return SpeechProbe(ok=True, report="speech check — ok")

        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("SPEECH_ENABLED=true\n")
        monkeypatch.setattr("caddiej.__main__.probe_speech", fake_probe)

        main(["--check-speech"])

        printed = capsys.readouterr()
        assert "speech check — ok" in printed.out
        assert not (tmp_path / "caddiej.sqlite3").exists()

    def test_a_failed_check_is_an_exit_code(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        async def fake_probe(**_kwargs: object):
            from caddiej.speech.diagnose import SpeechProbe

            return SpeechProbe(ok=False, report="FAILED at engine open")

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("caddiej.__main__.probe_speech", fake_probe)

        with pytest.raises(SystemExit) as exit_info:
            main(["--check-speech"])

        assert exit_info.value.code == 1
        assert "FAILED at engine open" in capsys.readouterr().err


class TestCheckingVideoFrames:
    def test_it_does_not_start_the_application(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """No HUD, no observers, no camera, no OpenAI: a check that looks at
        files already on disk."""

        captured: dict[str, object] = {}

        async def fake_probe(**kwargs: object):
            from caddiej.video.diagnose import VideoFramesProbe

            captured.update(kwargs)
            return VideoFramesProbe(ok=True, report="video frames check — ok")

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("VIDEO_ENABLED", "false")
        monkeypatch.setenv("VIDEO_CAMERA_VIEW", "face_on")
        (tmp_path / ".env").write_text(
            "VIDEO_ENABLED=false\nVIDEO_CAMERA_VIEW=face_on\n"
        )
        monkeypatch.setattr("caddiej.__main__.probe_video_frames", fake_probe)
        monkeypatch.setattr(
            "caddiej.__main__.run",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("application started")
            ),
        )

        main(["--check-video-frames"])

        printed = capsys.readouterr()
        assert "video frames check — ok" in printed.out
        assert not (tmp_path / "caddiej.sqlite3").exists()
        assert captured["camera_view"] == "face_on"

    def test_a_failed_check_is_an_exit_code(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        async def fake_probe(**_kwargs: object):
            from caddiej.video.diagnose import VideoFramesProbe

            return VideoFramesProbe(ok=False, report="FAILED at clip: no usable")

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("caddiej.__main__.probe_video_frames", fake_probe)

        with pytest.raises(SystemExit) as exit_info:
            main(["--check-video-frames"])

        assert exit_info.value.code == 1
        assert "FAILED at clip" in capsys.readouterr().err


class TestPathsInAnEnvFile:
    def test_a_leading_tilde_means_home(self) -> None:
        # A shell expands this in `HISTORY_DATABASE_PATH=~/golf/x.sqlite3`, and
        # nothing expands it in .env — so the same line copied from the README
        # used to create a directory called "~".
        settings = a_settings(
            HISTORY_DATABASE_PATH="~/golf/rounds.sqlite3",
            VIDEO_MEDIA_ROOT="~/golf/media",
            SPEECH_CACHE_ROOT="~/golf/media/speech",
        )

        assert settings.history_database_path == Path.home() / "golf/rounds.sqlite3"
        assert settings.video_media_root == Path.home() / "golf/media"
        assert settings.speech_cache_root == Path.home() / "golf/media/speech"

    def test_every_other_path_is_left_alone(self) -> None:
        settings = a_settings(
            HISTORY_DATABASE_PATH="rounds/history.sqlite3",
            VIDEO_MEDIA_ROOT="/Volumes/Swings",
        )

        assert settings.history_database_path == Path("rounds/history.sqlite3")
        assert settings.video_media_root == Path("/Volumes/Swings")
