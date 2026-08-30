"""Letting an effect reach OBS, using the two things OBS can already be asked.

A scene item's visibility and a text source's contents. That is the whole
vocabulary, and it is a deliberately small one: whatever the source *contains* —
a stinger, a graphic, a sound, an animation somebody made — belongs to the person
who set up their own scene, and CaddieJ's job is to turn it on for as long as the
effect lasts and then turn it off. Nothing is created, nothing is downloaded, and
nothing is assumed about what is inside.

**Why this channel holds a connection when nothing else in CaddieJ does.** The
overlay is provisioned once at start-up over a connection that lives for a second
and is thrown away, which is right for a one-off. An effect is not a one-off: it
needs a command at the start and a command a few seconds later, and paying for a
connect and an identify handshake at the front of a celebration would put the
handshake inside the moment it is supposed to mark. So the connection is opened
on first use and kept.

**There is no reconnect loop, on purpose.** A loop would spend the evening
retrying a connection nothing is waiting for; there is nothing to do with an OBS
connection between celebrations. Instead a failed command drops the connection,
and the next effect opens a fresh one. Recovery therefore costs the first command
after OBS comes back, which happens while nobody is looking at OBS, and is free
every other time.
"""

import logging
from collections.abc import Callable

from caddiej.adapters.obs import ObsClient, ObsConnectionError
from caddiej.effects.effect import Effect

logger = logging.getLogger(__name__)

ClientFactory = Callable[[], ObsClient]
"""Makes a fresh client. A factory rather than a client, because recovering from
a failure means throwing one away and building another, and a channel handed a
single instance could not do that."""


class ObsChannel:
    """Shows a source and captions it for the length of an effect."""

    def __init__(
        self,
        client_factory: ClientFactory,
        scene: str,
        source: str,
        text_source: str = "",
        enabled: bool = True,
    ) -> None:
        self._factory = client_factory
        self._scene = scene
        self._source = source
        self._text_source = text_source
        self._enabled = enabled
        self._client: ObsClient | None = None
        # What this channel has actually changed in somebody's OBS and not yet
        # changed back. Tracked separately from the connection because the two
        # come apart in exactly the case that matters: the command that showed
        # the source succeeded, and then the connection died.
        self._showing = False
        self._captioned = False

        self.commands = 0
        self.failures = 0
        self.abandoned = 0

    @property
    def name(self) -> str:
        return "obs"

    @property
    def is_available(self) -> bool:
        """Whether there is somewhere in OBS to put an effect.

        A scene and a source name, both of which have to already exist in
        somebody's OBS — this channel does not create them. Unconfigured is the
        default, so the common case is that this reports False and the director
        never calls it.
        """

        return self._enabled and bool(self._scene) and bool(self._source)

    async def begin(self, effect: Effect) -> None:
        """Caption the effect and show the source."""

        client = await self._connected()
        try:
            if self._text_source:
                # The caption first, so the source is never briefly visible
                # still carrying the last hole's label.
                await client.set_text(self._text_source, effect.label)
                self._captioned = True
            await client.set_scene_item_visibility(
                self._scene, self._source, visible=True
            )
            self._showing = True
        except ObsConnectionError as error:
            await self._forget(error, effect, showing=True)
            raise

        self.commands += 1
        logger.info(
            "effects_obs_shown",
            extra={
                "effect_cue": effect.cue,
                "obs_scene": self._scene,
                "obs_source": self._source,
            },
        )

    async def end(self, effect: Effect) -> None:
        """Hide the source again, and blank the caption.

        Safe to call having never begun, and safe to call twice, which is what
        the director relies on: hiding something already hidden is a command OBS
        accepts and ignores. Blanking the caption matters more than it looks —
        a text source left reading HOLE IN ONE is visible in a recording the next
        time the scene is used, long after anybody remembers why.

        **This is the one place the channel will reconnect to do something.** The
        rule everywhere else is that a dropped connection waits for the next
        effect, because nothing is waiting on it. Here something is: whatever
        this channel turned on is still on in somebody's scene, OBS writes scene
        item visibility to the scene collection file, and so "we lost the
        connection" would otherwise mean a graphic left burning in every
        recording made afterwards. Reconnecting once to undo our own change is
        worth a connect that the ordinary case never pays.
        """

        if not self._showing and not self._captioned:
            # Nothing of ours is on screen. Either this channel never began, or
            # an earlier end already put everything back.
            return

        try:
            client = await self._connected()
            await client.set_scene_item_visibility(
                self._scene, self._source, visible=False
            )
            self._showing = False
            if self._text_source:
                await client.set_text(self._text_source, "")
                self._captioned = False
        except ObsConnectionError as error:
            await self._abandon(error, effect)
            raise

        self.commands += 1

    async def aclose(self) -> None:
        """Drop the connection. Whatever was showing has already been hidden.

        Only the connection: the director always resets its channels before it
        closes them, so by the time this runs the source is hidden and the
        caption is blank. Hiding again here would be a command sent to an OBS
        that may itself be shutting down.
        """

        if self._showing or self._captioned:
            # The reset already tried, and failed, or it never ran. Said plainly
            # because it is the one CaddieJ failure that leaves a mark on
            # somebody else's software after the process has gone.
            logger.error(
                "effects_obs_left_showing",
                extra={"obs_scene": self._scene, "obs_source": self._source},
            )

        client = self._client
        self._client = None
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:
            logger.warning("effects_obs_disconnect_failed", exc_info=True)

    async def _connected(self) -> ObsClient:
        """The live client, connecting if this is the first command since a drop."""

        if self._client is not None:
            return self._client

        client = self._factory()
        await client.connect()
        self._client = client
        logger.info(
            "effects_obs_connected",
            extra={"obs_scene": self._scene, "obs_source": self._source},
        )
        return client

    async def _forget(
        self, error: ObsConnectionError, effect: Effect, showing: bool
    ) -> None:
        """Count a failure and throw the connection away.

        Throwing it away is the whole recovery strategy. A client that failed
        once mid-command is in an unknown state, and the cheapest way to a known
        one is a new client on the next effect — which is also the only moment
        anybody would notice the difference.
        """

        self.failures += 1
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                logger.debug("effects_obs_disconnect_after_error", exc_info=True)

        logger.warning(
            "effects_obs_failed",
            extra={
                "effect_cue": effect.cue,
                "effect_phase": "showing" if showing else "hiding",
                "obs_scene": self._scene,
                "obs_source": self._source,
            },
            exc_info=error,
        )

    async def _abandon(self, error: ObsConnectionError, effect: Effect) -> None:
        """Give up on hiding, having tried a fresh connection to do it.

        Counted separately from an ordinary failure because it is a different
        kind of thing: a command that did not work is a celebration nobody saw,
        whereas this is a change to somebody's OBS that CaddieJ made and could
        not undo.
        """

        self.abandoned += 1
        await self._forget(error, effect, showing=False)
        logger.error(
            "effects_obs_left_showing",
            extra={
                "effect_cue": effect.cue,
                "obs_scene": self._scene,
                "obs_source": self._source,
            },
        )
