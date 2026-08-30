"""Configuration and wiring for the assistant boundary.

Three things worth pinning. That every switch is off by default, so a CaddieJ
nobody configured serves nothing and opens no port. That the two
misconfigurations which would turn a private local surface into a networked one
are refused outright rather than warned about — this is the one subsystem where
CaddieJ declines instead of degrading. And that the whole thing assembles from a
`Settings` without any of it being reachable.
"""

import asyncio
import logging
import socket
from pathlib import Path

import pytest
from assistant_support import boundary, probe

from caddiej.__main__ import _JsonFormatter
from caddiej.adapters.api.server import AssistantServer
from caddiej.app import (
    _build_assistant_api,
    _serve_assistant_api,
    _start_assistant_api,
)
from caddiej.config import Settings
from caddiej.runtime import TaskSupervisor


def a_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


async def built(tmp_path: Path, **overrides: object) -> AssistantServer | None:
    """Run the real application builder over a real set of services."""

    ready = await boundary(tmp_path)
    try:
        return _build_assistant_api(
            a_settings(**overrides),
            history=ready.history,
            coaching=ready.coaching,
            play=ready.play,
            subsystems={"history": probe(True, True)},
        )
    finally:
        await ready.coaching.aclose()
        await ready.play.aclose()
        await ready.history.aclose()


class TestTheDefaults:
    def test_the_whole_boundary_is_off(self) -> None:
        """Not conservatism for its own sake.

        Switching it on opens a port that answers questions about what somebody
        did in their own house. That is a decision an operator should make
        rather than inherit, and CaddieJ is complete without it.
        """

        settings = a_settings()

        assert settings.assistant_api_enabled is False

    def test_there_is_no_token_and_none_is_invented(self) -> None:
        # A token generated at start-up would have to be printed or written
        # somewhere for the MCP server to find, and every mechanism for doing
        # that is worse than asking the operator for one.
        assert a_settings().assistant_api_token.get_secret_value() == ""

    def test_reading_and_writing_are_two_decisions(self) -> None:
        # So the first thing anybody runs is a read-only surface.
        assert a_settings().assistant_api_allow_mutations is False

    def test_coaching_prose_does_not_cross_the_boundary(self) -> None:
        """Off for the same reason camera frames are.

        The named fault and its severity are facts about a golf swing; the
        sentences are a record of somebody being told what they are doing wrong
        in their own home.
        """

        assert a_settings().assistant_api_include_coaching_prose is False

    def test_it_binds_to_loopback_on_its_own_port(self) -> None:
        settings = a_settings()

        assert settings.assistant_api_host == "127.0.0.1"
        assert settings.assistant_api_port != settings.hud_port


class TestWhatIsRefused:
    async def test_nothing_is_built_when_it_is_switched_off(
        self, tmp_path: Path
    ) -> None:
        # The default path, and it must cost nothing: no façade, no port, no
        # object capable of reading or writing anything.
        assert await built(tmp_path) is None

    async def test_it_is_refused_without_a_token(self, tmp_path: Path) -> None:
        """Refused rather than served unauthenticated.

        An unauthenticated local API is readable by any web page the operator
        visits, so this is not a lesser version of the feature.
        """

        server = await built(tmp_path, assistant_api_enabled=True)

        assert server is None

    async def test_it_is_refused_when_the_token_is_only_whitespace(
        self, tmp_path: Path
    ) -> None:
        server = await built(
            tmp_path, assistant_api_enabled=True, assistant_api_token="   "
        )

        assert server is None

    @pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10"])
    async def test_a_bind_the_network_could_reach_is_refused(
        self, tmp_path: Path, host: str
    ) -> None:
        """Unlike `HUD_HOST`, which is passed straight to uvicorn.

        The HUD serves static files and a read-only feed. This has a bearer
        token and a mutation route, so the same mistake is worth refusing
        outright rather than warning about.
        """

        server = await built(
            tmp_path,
            assistant_api_enabled=True,
            assistant_api_token="a-token",
            assistant_api_host=host,
        )

        assert server is None

    async def test_a_refusal_says_why_without_quoting_the_token(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.ERROR):
            await built(
                tmp_path,
                assistant_api_enabled=True,
                assistant_api_token="hunter2",
                assistant_api_host="0.0.0.0",
            )

        assert "assistant_api_refused_non_loopback_bind" in caplog.text
        assert "hunter2" not in caplog.text


class TestWhatIsBuilt:
    async def test_a_configured_boundary_assembles(self, tmp_path: Path) -> None:
        server = await built(
            tmp_path,
            assistant_api_enabled=True,
            assistant_api_token="a-token",
            assistant_api_port=0,
        )

        assert server is not None

    async def test_it_starts_and_stops_on_a_real_port(self, tmp_path: Path) -> None:
        server = await built(
            tmp_path,
            assistant_api_enabled=True,
            assistant_api_token="a-token",
            assistant_api_port=0,
        )
        assert server is not None

        serving = asyncio.create_task(server.serve())
        try:
            await server.wait_until_serving(5.0)
            assert server.port > 0
        finally:
            await server.stop()
            await asyncio.wait_for(serving, 5.0)


class TestFailureIsolation:
    async def test_a_failing_api_does_not_raise_out_of_its_task(self) -> None:
        """The one subsystem a supervised failure must not stop CaddieJ for.

        A supervised task that raises brings the process down, which is right
        for the HUD and wrong here: an assistant that cannot be reached is a
        missing convenience, and stopping the simulator over it would make the
        simulator depend on the thing that was supposed to be optional.
        """

        class Broken(AssistantServer):
            def __init__(self) -> None:
                pass

            async def serve(self) -> None:
                raise OSError(48, "address already in use")

        # Returns rather than raises, so the supervisor sees a completed task
        # and never calls `on_failure`.
        await _serve_assistant_api(Broken())

    async def test_a_port_already_in_use_is_stepped_over(self, tmp_path: Path) -> None:
        """How this actually fails in somebody's golf room.

        Something else is on the port — a stale CaddieJ, an unrelated program,
        a typo that collided with the HUD. uvicorn reports it by calling
        `sys.exit`, and `SystemExit` is the one exception class that walks past
        every `except Exception` between it and the event loop. So this is
        checked against a genuinely occupied socket rather than a raised
        `OSError`, because the raised version passes whether the real path
        works or not.
        """

        occupied = socket.socket()
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        taken = occupied.getsockname()[1]

        failures: list[str] = []
        supervisor = TaskSupervisor(
            shutdown_timeout=2.0, on_failure=lambda: failures.append("stopped")
        )
        try:
            server = await built(
                tmp_path,
                assistant_api_enabled=True,
                assistant_api_token="a-token",
                assistant_api_port=taken,
            )
            assert server is not None

            await _start_assistant_api(
                a_settings(assistant_api_startup_timeout=5.0), supervisor, server
            )

            # CaddieJ carries on: the supervisor was never told to shut down.
            assert failures == []
        finally:
            await supervisor.shutdown()
            occupied.close()

    async def test_a_cancelled_api_task_stays_cancelled(self) -> None:
        # Shutdown must not be swallowed along with the failures.
        class Hanging(AssistantServer):
            def __init__(self) -> None:
                pass

            async def serve(self) -> None:
                await asyncio.Event().wait()

        task = asyncio.create_task(_serve_assistant_api(Hanging()))
        await asyncio.sleep(0)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task


def test_every_assistant_log_field_is_allowed_by_the_formatter() -> None:
    """The formatter fails closed, so an unlisted field is dropped silently.

    Listed here as well as in `test_logging_fields` because these are the newest
    ones and because the list is what stops a credential reaching a log.
    """

    allowed = set(_JsonFormatter._CONTEXT_FIELDS)

    assert {
        "assistant_tool",
        "assistant_refusal",
        "assistant_changed",
        "assistant_host",
        "assistant_port",
        "assistant_url",
        "assistant_mutations",
    } <= allowed


def test_no_log_field_could_carry_a_token_or_an_answer() -> None:
    # A log of what an assistant asked and what it was told would be both a
    # credential risk and a transcript of somebody's practice session.
    fields = [
        name for name in _JsonFormatter._CONTEXT_FIELDS if name.startswith("assistant_")
    ]

    for name in fields:
        assert "token" not in name
        assert "header" not in name
        assert "body" not in name
        assert "answer" not in name
