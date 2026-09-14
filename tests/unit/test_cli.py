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
