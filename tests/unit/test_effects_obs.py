"""The one channel that has to reach a program which may not be running.

Everything here runs against a stub client, so there is no OBS, no WebSocket and
no scene. What is actually being tested is the recovery story: a failed command
throws the connection away, the next effect opens a fresh one, and nothing about
any of it reaches the golf.
"""

from dataclasses import dataclass, field

import pytest

from caddiej.adapters.obs import ObsRequestError, ObsTransportConnectionError
from caddiej.effects.effect import Effect, EffectPriority
from caddiej.effects.obs import ObsChannel


def an_effect(cue: str = "birdie", label: str = "BIRDIE") -> Effect:
    return Effect(
        cue=cue,
        label=label,
        priority=EffectPriority.NOTABLE,
        intensity=0.4,
        seconds=2.0,
    )


@dataclass
class StubClient:
    """An OBS that records what it was asked and can be made to refuse.

    Deliberately not a mock of the transport: this stands in for `ObsClient`
    itself, because the transport's request names are that class's business and
    already covered by its own tests.
    """

    connect_error: BaseException | None = None
    command_error: BaseException | None = None

    connects: int = 0
    disconnects: int = 0
    texts: list[tuple[str, str]] = field(default_factory=list)
    visibility: list[tuple[str, str, bool]] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    """Every command in order, so a test about sequence is about sequence rather
    than about two separate lists that happen to have the right lengths."""

    async def connect(self) -> None:
        self.connects += 1
        if self.connect_error is not None:
            raise self.connect_error

    async def set_text(self, source_name: str, text: str) -> None:
        if self.command_error is not None:
            raise self.command_error
        self.texts.append((source_name, text))
        self.calls.append("caption" if text else "blank")

    async def set_scene_item_visibility(
        self, scene_name: str, source_name: str, *, visible: bool
    ) -> None:
        if self.command_error is not None:
            raise self.command_error
        self.visibility.append((scene_name, source_name, visible))
        self.calls.append("show" if visible else "hide")

    async def disconnect(self) -> None:
        self.disconnects += 1


class Table:
    """A channel and however many clients it ends up building."""

    def __init__(self, text_source: str = "CaddieJ Caption", **kwargs: object) -> None:
        self.clients: list[StubClient] = []
        self.template = StubClient()
        self.channel = ObsChannel(
            self._make,
            scene="CaddieJ",
            source="Celebration",
            text_source=text_source,
            **kwargs,  # type: ignore[arg-type]
        )

    def _make(self) -> StubClient:
        client = StubClient(
            connect_error=self.template.connect_error,
            command_error=self.template.command_error,
        )
        self.clients.append(client)
        return client  # type: ignore[return-value]

    @property
    def client(self) -> StubClient:
        return self.clients[-1]


class TestShowingAndHiding:
    async def test_it_captions_the_effect_and_reveals_the_source(self) -> None:
        table = Table()

        await table.channel.begin(an_effect("eagle", "EAGLE"))

        assert table.client.texts == [("CaddieJ Caption", "EAGLE")]
        assert table.client.visibility == [("CaddieJ", "Celebration", True)]

    async def test_the_caption_is_set_before_the_source_appears(self) -> None:
        """Otherwise the source is briefly visible still carrying the last
        hole's label, which is worse than a moment of nothing."""

        table = Table()
        await table.channel.begin(an_effect())

        assert table.client.calls == ["caption", "show"]

    async def test_the_source_is_hidden_before_the_caption_is_blanked(self) -> None:
        """The mirror image, for the same reason: blanking first would leave the
        source visible and empty for a frame."""

        table = Table()
        await table.channel.begin(an_effect())
        await table.channel.end(an_effect())

        assert table.client.calls == ["caption", "show", "hide", "blank"]

    async def test_ending_hides_the_source_and_blanks_the_caption(self) -> None:
        """A text source left reading HOLE IN ONE is visible in a recording the
        next time the scene is used, long after anybody remembers why."""

        table = Table()
        await table.channel.begin(an_effect("hole_in_one", "HOLE IN ONE"))
        await table.channel.end(an_effect("hole_in_one", "HOLE IN ONE"))

        assert table.client.visibility[-1] == ("CaddieJ", "Celebration", False)
        assert table.client.texts[-1] == ("CaddieJ Caption", "")

    async def test_a_scene_with_no_caption_source_just_shows_the_source(self) -> None:
        table = Table(text_source="")

        await table.channel.begin(an_effect())
        await table.channel.end(an_effect())

        assert table.client.texts == []
        assert table.client.visibility == [
            ("CaddieJ", "Celebration", True),
            ("CaddieJ", "Celebration", False),
        ]

    async def test_the_connection_is_kept_across_an_effect(self) -> None:
        """One connect for a show and a hide. Paying for a handshake at the front
        of a celebration would put the handshake inside the moment."""

        table = Table()
        await table.channel.begin(an_effect())
        await table.channel.end(an_effect())

        assert len(table.clients) == 1
        assert table.client.connects == 1

    async def test_the_connection_is_kept_across_several_effects(self) -> None:
        table = Table()
        for _ in range(3):
            await table.channel.begin(an_effect())
            await table.channel.end(an_effect())

        assert len(table.clients) == 1


class TestResettingIsAlwaysSafe:
    async def test_ending_without_beginning_does_nothing_at_all(self) -> None:
        """The director resets unconditionally, so this is the common case — and
        opening a connection purely to hide something nobody showed would be the
        one reconnect that benefits nobody."""

        table = Table()

        await table.channel.end(an_effect())

        assert table.clients == []

    async def test_ending_twice_is_harmless(self) -> None:
        """And the second one costs nothing: the source is already hidden, and
        the channel knows it is because it is the thing that hid it."""

        table = Table()
        await table.channel.begin(an_effect())
        await table.channel.end(an_effect())
        await table.channel.end(an_effect())

        assert table.client.visibility == [
            ("CaddieJ", "Celebration", True),
            ("CaddieJ", "Celebration", False),
        ]


class TestRecovery:
    async def test_an_obs_that_is_not_running_is_reported_and_abandoned(self) -> None:
        table = Table()
        table.template.connect_error = ObsTransportConnectionError("nothing there")

        with pytest.raises(ObsTransportConnectionError):
            await table.channel.begin(an_effect())

        assert table.channel.failures == 0, "connecting is not a command"

    async def test_a_failed_command_throws_the_connection_away(self) -> None:
        """A client that failed mid-command is in an unknown state, and the
        cheapest way to a known one is a new client on the next effect."""

        table = Table()
        table.template.command_error = ObsRequestError("no such source")

        with pytest.raises(ObsRequestError):
            await table.channel.begin(an_effect())

        assert table.channel.failures == 1
        assert table.client.disconnects == 1

    async def test_the_next_effect_connects_again(self) -> None:
        """Recovery costs the first command after OBS comes back, which happens
        while nobody is looking at OBS, and is free every other time."""

        table = Table()
        table.template.command_error = ObsRequestError("no such source")
        with pytest.raises(ObsRequestError):
            await table.channel.begin(an_effect())

        table.template.command_error = None
        await table.channel.begin(an_effect())

        assert len(table.clients) == 2
        assert table.client.visibility == [("CaddieJ", "Celebration", True)]

    async def test_a_failure_while_hiding_is_reported_too(self) -> None:
        table = Table()
        await table.channel.begin(an_effect())
        table.client.command_error = ObsRequestError("gone")

        with pytest.raises(ObsRequestError):
            await table.channel.end(an_effect())

        assert table.channel.failures == 1

    async def test_nothing_is_retried_inside_the_channel(self) -> None:
        """A celebration is a moment. Retrying inside it would spend the moment
        on a connection nobody is waiting for."""

        table = Table()
        table.template.command_error = ObsRequestError("no")

        with pytest.raises(ObsRequestError):
            await table.channel.begin(an_effect())

        assert len(table.clients) == 1


class TestLeavingNothingBehind:
    """The one thing this channel changes that outlives the process.

    OBS writes scene item visibility into the scene collection file. So a source
    CaddieJ showed and did not hide stays visible after CaddieJ has gone, and
    turns up in every recording made afterwards — long after anybody remembers
    why there is a HOLE IN ONE graphic in the corner. That makes hiding worth a
    reconnect, which nothing else in this channel is.
    """

    async def test_a_dropped_connection_is_reopened_to_hide_the_source(self) -> None:
        table = Table()
        await table.channel.begin(an_effect())
        # What a failed command does: the client is thrown away, so `end` finds
        # no connection — which is the case that used to leave the graphic up.
        table.client.command_error = ObsRequestError("dropped")
        with pytest.raises(ObsRequestError):
            await table.channel.end(an_effect())

        table.template.command_error = None
        await table.channel.end(an_effect())

        assert len(table.clients) == 2, "a second connection was opened to hide it"
        assert table.client.visibility == [("CaddieJ", "Celebration", False)]
        assert table.client.texts == [("CaddieJ Caption", "")]

    async def test_a_source_hidden_on_the_second_attempt_is_not_still_owed(
        self,
    ) -> None:
        table = Table()
        await table.channel.begin(an_effect())
        table.client.command_error = ObsRequestError("dropped")
        with pytest.raises(ObsRequestError):
            await table.channel.end(an_effect())
        table.template.command_error = None
        await table.channel.end(an_effect())

        await table.channel.end(an_effect())

        assert len(table.clients) == 2, "there was nothing left to hide"

    async def test_a_caption_set_before_a_failure_is_still_blanked(self) -> None:
        """The two commands are separate: the caption goes first so the source is
        never briefly visible carrying the last hole's label. Which means a
        failure between them leaves a caption set and nothing showing, and the
        caption is the half that is visible next time the scene is used."""

        table = Table()
        table.channel._captioned = True  # noqa: SLF001

        await table.channel.end(an_effect())

        assert table.client.texts == [("CaddieJ Caption", "")]

    async def test_an_obs_that_will_not_come_back_is_reported_not_hidden(self) -> None:
        table = Table()
        await table.channel.begin(an_effect())
        table.client.command_error = ObsRequestError("dropped")
        with pytest.raises(ObsRequestError):
            await table.channel.end(an_effect())
        assert table.channel.abandoned == 1

        table.template.connect_error = ObsTransportConnectionError("nothing there")
        with pytest.raises(ObsTransportConnectionError):
            await table.channel.end(an_effect())

        assert table.channel.abandoned == 2, "every attempt that failed is counted"

    async def test_ending_something_that_never_showed_opens_nothing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The director resets every channel after every effect, including ones
        an effect never touched. Reconnecting for those would be the one
        reconnect that benefits nobody."""

        table = Table()
        table.template.connect_error = ObsTransportConnectionError("nothing there")
        with pytest.raises(ObsTransportConnectionError):
            await table.channel.begin(an_effect())
        connects = len(table.clients)

        await table.channel.end(an_effect())
        await table.channel.aclose()

        assert len(table.clients) == connects
        assert "effects_obs_left_showing" not in caplog.text


class TestAvailability:
    def test_an_unconfigured_scene_or_source_is_not_available(self) -> None:
        """The default. This shows a source in a scene somebody built, and
        CaddieJ does not create either — so with nothing there to show, the
        honest answer is not to try."""

        assert (
            ObsChannel(StubClient, scene="", source="Celebration").is_available is False
        )
        assert ObsChannel(StubClient, scene="CaddieJ", source="").is_available is False

    def test_switching_it_off_makes_it_unavailable(self) -> None:
        channel = ObsChannel(
            StubClient, scene="CaddieJ", source="Celebration", enabled=False
        )

        assert channel.is_available is False

    def test_a_configured_scene_and_source_is_available(self) -> None:
        assert (
            ObsChannel(StubClient, scene="CaddieJ", source="Celebration").is_available
            is True
        )


class TestClosing:
    async def test_closing_drops_the_connection_without_hiding_again(self) -> None:
        """The director always resets before it closes, so by now the source is
        hidden. Hiding again would be a command sent to an OBS that may itself be
        shutting down."""

        table = Table()
        await table.channel.begin(an_effect())
        await table.channel.end(an_effect())
        hides = table.client.visibility.count(("CaddieJ", "Celebration", False))

        await table.channel.aclose()

        assert table.client.disconnects == 1
        assert table.client.visibility.count(("CaddieJ", "Celebration", False)) == hides

    async def test_closing_without_ever_connecting_is_harmless(self) -> None:
        table = Table()

        await table.channel.aclose()

        assert table.clients == []

    async def test_a_disconnect_that_fails_is_swallowed(self) -> None:
        """Nothing useful happens after this point, so a failure here can only
        make shutdown worse."""

        table = Table()
        await table.channel.begin(an_effect())

        async def refuse() -> None:
            raise OSError("the socket had already gone")

        table.client.disconnect = refuse  # type: ignore[method-assign]
        await table.channel.aclose()
