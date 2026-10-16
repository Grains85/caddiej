"""Show an existing OBS source for a while after a shot, and never cost a shot.

It waits on impact, not on the result. `ShotDetected` is the launch monitor's
own trigger; `RoundShotObserved` is GSPro's completed shot, and on real
hardware that arrives three to six seconds later because the ball has to finish
flying first. A replay of the swing that starts after the ball has stopped is a
replay nobody watches, so the delay below is measured from the strike. The
floor's result panel still waits for GSPro, because that is when the numbers
exist.

**Nothing here is ever on the floor HUD's path, and that is the whole design
constraint.** A shot result is a number a player can see, and it must be on the
projector the moment CaddieJ knows it; a revealed source is decoration. So
three rules:

* the event handler starts a task and returns, so the bus worker never waits;
* start-up does the same, so an OBS that is missing or slow to answer cannot
  hold up the floor state machine or the observers behind it;
* every OBS command is bounded, so a half-open WebSocket cannot pin the one
  lock an instance holds and strand every later reveal behind it.

Best effort throughout: a missing source, a down WebSocket, or a failed command
is logged and abandoned. Golf continues.

Instances are independent. Each holds its own connection, its own lock and its
own generation counter, so a feature whose source does not exist cannot stop
another feature's from being shown — which is what lets Shot Video and Top
Impact be configured, and fail, separately.

Log events are named for the feature (`shot_video_replay_scheduled`,
`top_impact_replay_scheduled`), while the structured fields are shared and
carry `obs_feature`. That way one grep finds every reveal in a session and a
narrower one finds a single feature's.

**A reveal works on the floor scene, never on the capture scene.** Swing
capture keeps its own panels enabled in a scene nobody watches, so it can take
screenshots without the projector ever showing a coaching camera. The same
OBS source can therefore have two scene items: a hidden one in the floor scene
that this class toggles, and an enabled one in the capture scene that capture
reads. They share a source name and nothing else. A reveal pointed at the
capture scene would hide the item capture depends on and then show it to
nobody, so it refuses at start-up instead and says why.

Capture also watches the floor scene, and warns if a source it reads changes
there while it is reading. A reveal is exactly such a change, made on purpose,
so every toggle here is declared to `FloorReveals` first. That is what lets the
warning stay loud for a swing camera that reaches the projector without going
off on every shot that shows Top Impact.

A source that cannot be found is the most common misconfiguration and the
quietest: the panel simply never appears. So it is not logged once and
forgotten. It is repeated, rate-limited, with a count, and it is visible in
`health()` for `/healthz`, where somebody setting up a bay will look.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from caddiej.adapters.obs import (
    FLOOR_REVEALS,
    FloorReveals,
    ObsConnectionError,
    ObsResourceNotFoundError,
)
from caddiej.core.event_bus import EventBus
from caddiej.domain.events import ShotDetected
from caddiej.obs_reveal.features import RevealFeature

logger = logging.getLogger(__name__)


class VisibilityClient(Protocol):
    """The slice of `ObsClient` a reveal uses."""

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def scene_item_id(self, scene_name: str, source_name: str) -> int: ...

    async def set_scene_item_visibility(
        self, scene_name: str, source_name: str, *, visible: bool
    ) -> None: ...


ClientFactory = Callable[[], VisibilityClient]
Sleep = Callable[[float], Awaitable[None]]
Monotonic = Callable[[], float]

RevealState = Literal[
    "disabled", "refused", "unchecked", "resolved", "missing", "obs_unavailable"
]

# Long enough not to fill a log during a session, short enough that somebody
# reading the last few minutes of it still sees the source is missing.
MISSING_WARNING_INTERVAL_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class RevealHealth:
    """What a reveal knows about its source, for `/healthz`.

    `state` is the last thing OBS said, not a fresh probe: a health endpoint
    that opened a WebSocket per request would be a way to load OBS from a
    browser tab. `detail` is the remediation, in the words of the OBS UI.
    """

    feature: str
    enabled: bool
    scene: str
    source: str
    state: RevealState
    reveals_shown: int
    missing_attempts: int
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "feature": self.feature,
            "enabled": self.enabled,
            "scene": self.scene,
            "source": self.source,
            "state": self.state,
            "reveals_shown": self.reveals_shown,
            "missing_attempts": self.missing_attempts,
            "detail": self.detail,
        }


class TimedObsSource:
    """Show one OBS source briefly after each detected shot."""

    def __init__(
        self,
        client_factory: ClientFactory,
        scene: str,
        source: str,
        *,
        feature: RevealFeature,
        enabled: bool = False,
        delay_seconds: float = 0.75,
        duration_seconds: float = 4.0,
        command_timeout: float = 10.0,
        shutdown_timeout: float = 5.0,
        sleep: Sleep | None = None,
        capture_scene: str | None = None,
        monotonic: Monotonic | None = None,
        missing_warning_interval: float = MISSING_WARNING_INTERVAL_SECONDS,
        floor_reveals: FloorReveals = FLOOR_REVEALS,
    ) -> None:
        if delay_seconds < 0:
            raise ValueError("delay_seconds must be >= 0")
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be > 0")
        if command_timeout <= 0:
            raise ValueError("command_timeout must be > 0")
        if shutdown_timeout <= 0:
            raise ValueError("shutdown_timeout must be > 0")
        if missing_warning_interval <= 0:
            raise ValueError("missing_warning_interval must be > 0")

        self._factory = client_factory
        # One name, used three ways: the log event prefix, the `obs_feature`
        # field, and the bus subscriber whose delivery policy `event_policy`
        # declares. They were three constructor arguments and could disagree.
        self._feature = feature
        self._scene = scene
        self._source = source
        self._capture_scene = (capture_scene or "").strip()
        self._refused = bool(
            enabled and self._capture_scene and scene.strip() == self._capture_scene
        )
        self._enabled = enabled and not self._refused
        self._delay = delay_seconds
        self._duration = duration_seconds
        self._command_timeout = command_timeout
        self._shutdown_timeout = shutdown_timeout
        self._sleep: Sleep = sleep if sleep is not None else asyncio.sleep
        self._floor_reveals = floor_reveals

        self._client: VisibilityClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._startup: asyncio.Task[None] | None = None
        self._generation = 0
        self._lock = asyncio.Lock()
        self._last_trigger_id: str | None = None
        self._monotonic: Monotonic = (
            monotonic if monotonic is not None else time.monotonic
        )
        self._missing_interval = missing_warning_interval
        self._missing_count = 0
        self._missing_warned_at: float | None = None
        self._state: RevealState = (
            "refused" if self._refused else "unchecked" if enabled else "disabled"
        )
        self._shown = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def feature(self) -> RevealFeature:
        return self._feature

    def health(self) -> RevealHealth:
        return RevealHealth(
            feature=self._feature.value,
            enabled=self._enabled,
            scene=self._scene,
            source=self._source,
            state=self._state,
            reveals_shown=self._shown,
            missing_attempts=self._missing_count,
            detail=self._remediation(),
        )

    def _remediation(self) -> str:
        if self._state == "refused":
            return (
                f"The floor scene '{self._scene}' is the swing capture scene. "
                "Point OBS_FLOOR_SCENE at the scene the projector shows; the "
                "capture scene keeps its own enabled items and is never toggled."
            )
        if self._state == "missing":
            return (
                f"Scene '{self._scene}' has no item for source '{self._source}'. "
                f"In OBS select scene '{self._scene}', Sources > + > Add Existing "
                f"> '{self._source}', move it above the CaddieJ Browser Source "
                "and leave it hidden; CaddieJ shows it after each strike."
            )
        if self._state == "obs_unavailable":
            return (
                "OBS WebSocket did not answer. Check Tools > WebSocket Server "
                "Settings and OBS_HOST/OBS_PORT/OBS_PASSWORD."
            )
        return ""

    def subscribe(self, bus: EventBus) -> None:
        if not self._enabled:
            return
        bus.subscribe(ShotDetected, self.on_shot_detected, name=self._feature.value)

    def start(self) -> asyncio.Task[None] | None:
        """Say what is configured, then hide the source on a task of its own.

        Synchronous, and that matters: this is called during start-up, before
        the floor state machine and the observers are running. Awaiting an OBS
        connection here would mean an OBS that is unreachable delays the first
        shot of the session by however long the connect takes. The task is
        returned for anything that wants to wait for it; the application does
        not.
        """

        if self._refused:
            self._log(
                "refused_capture_scene",
                {**self._names(), "obs_capture_scene": self._capture_scene},
                level=logging.ERROR,
            )
            return None
        if not self._enabled:
            return None
        self._log("feature_enabled", self._configuration())
        self._startup = asyncio.create_task(
            self._prepare(), name=f"{self._feature}-startup"
        )
        return self._startup

    async def on_shot_detected(self, event: ShotDetected) -> None:
        """Schedule a reveal. Returns before any delay: the bus must not wait."""

        if not self._enabled:
            return
        trigger_id = event.trigger_id
        if trigger_id and trigger_id == self._last_trigger_id:
            # A log the observer had to re-read from the start replays the
            # detections in it. One reveal per strike.
            self._log(
                "replay_ignored_duplicate",
                {"obs_feature": self._feature, "shot_trigger_id": trigger_id},
            )
            return
        self._last_trigger_id = trigger_id or None
        self._replace(trigger_id)

    async def aclose(self) -> None:
        """Cancel anything pending and try to leave the source hidden.

        The hide is worth one attempt, because OBS remembers visibility across
        restarts and a source left showing sits on somebody's projector until
        they notice. It is not worth a shutdown: an OBS that has stopped
        answering would otherwise spend the whole grace period here, and the
        steps after this one are the ones that close the session.
        """

        if not self._enabled:
            return
        self._generation += 1
        for task in (self._startup, self._task):
            await self._abandon(task)
        self._startup = None
        self._task = None
        try:
            async with asyncio.timeout(self._shutdown_timeout):
                await self._set_visible(False, self._generation)
        except TimeoutError:
            self._log("left_showing", self._names(), level=logging.ERROR)
        await self._drop_client()

    # --- lifecycle --------------------------------------------------------

    async def _abandon(self, task: asyncio.Task[None] | None) -> None:
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            # Expected: this is what cancelling it looks like.
            pass
        except Exception:
            logger.warning(
                f"{self._feature}_shutdown_failed", extra=self._names(), exc_info=True
            )

    async def _prepare(self) -> None:
        """Resolve the source once, then hide it.

        Resolution is logged separately from the hide because the two failures
        mean different things to somebody setting up a bay: a name that does not
        match anything in the scene, versus an OBS that cannot be reached.
        """

        try:
            # Under the lock, because this is the one OBS conversation that
            # was not. A shot struck while start-up is still waiting on the
            # connect — ordinary, since an unreachable OBS waits the whole
            # command timeout here — has `_set_visible` call `_connected` at
            # the same time as this does. Both find no client, both build one,
            # and the loser is overwritten in `self._client` while still
            # connected: a socket nothing will ever close, for the length of
            # the session. Released before the hide below, which takes it.
            async with self._lock:
                async with asyncio.timeout(self._command_timeout):
                    client = await self._connected()
                    scene_item_id = await client.scene_item_id(
                        self._scene, self._source
                    )
        except ObsResourceNotFoundError:
            self._note_missing(self._names())
            await self._drop_client()
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            self._state = "obs_unavailable"
            logger.warning(
                f"{self._feature}_obs_unavailable", extra=self._names(), exc_info=True
            )
            await self._drop_client()
            return

        self._state = "resolved"
        self._log(
            "source_resolved", {**self._names(), "obs_scene_item_id": scene_item_id}
        )
        # OBS writes scene item visibility into the scene collection, so it
        # remembers whatever was showing when it last stopped.
        await self._set_visible(False, self._generation)

    def _replace(self, trigger_id: str) -> None:
        """Newest strike owns the source. The previous lifecycle is abandoned."""

        self._generation += 1
        generation = self._generation
        previous = self._task
        if previous is not None and not previous.done():
            # Not awaited: this runs on a bus worker. The cancelled task's own
            # generation is now stale, so nothing it had left to do can land.
            previous.cancel()
        self._log(
            "replay_scheduled",
            {
                "obs_feature": self._feature,
                "shot_trigger_id": trigger_id,
                "obs_reveal_delay_seconds": self._delay,
            },
        )
        self._task = asyncio.create_task(
            self._run_reveal(generation, trigger_id),
            name=f"{self._feature}-reveal",
        )

    async def _run_reveal(self, generation: int, trigger_id: str) -> None:
        try:
            # Hidden through the wait, so a reveal that replaced another cannot
            # leave the previous shot's panel on the floor.
            await self._set_visible(False, generation, trigger_id)
            await self._sleep(self._delay)
            await self._set_visible(True, generation, trigger_id)
            await self._sleep(self._duration)
            await self._set_visible(False, generation, trigger_id)
        except asyncio.CancelledError:
            self._log(
                "replay_cancelled",
                {"obs_feature": self._feature, "shot_trigger_id": trigger_id},
            )
            raise
        except Exception:
            logger.exception(
                f"{self._feature}_replay_failed",
                extra={"obs_feature": self._feature, "shot_trigger_id": trigger_id},
            )

    async def _set_visible(
        self,
        visible: bool,
        generation: int,
        trigger_id: str | None = None,
    ) -> None:
        extra = {**self._names(), "shot_trigger_id": trigger_id or ""}
        async with self._lock:
            if generation != self._generation:
                # A newer shot owns the source now. An older lifecycle's hide
                # arriving here would black out the newer reveal.
                return
            try:
                # Bounded as a whole: OBS can accept a connection and then stop
                # answering, and this call holds the lock every reveal needs.
                async with asyncio.timeout(self._command_timeout):
                    client = await self._connected()
                    with self._floor_reveals.changing(self._scene, self._source):
                        await client.set_scene_item_visibility(
                            self._scene, self._source, visible=visible
                        )
            except ObsResourceNotFoundError:
                self._note_missing(extra)
                await self._drop_client()
                return
            except ObsConnectionError:
                self._state = "obs_unavailable"
                logger.warning(
                    f"{self._feature}_obs_unavailable", extra=extra, exc_info=True
                )
                await self._drop_client()
                return
            except TimeoutError:
                self._log("obs_timeout", extra, level=logging.WARNING)
                await self._drop_client()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(f"{self._feature}_obs_failed", extra=extra)
                await self._drop_client()
                return

            if self._missing_count:
                self._log(
                    "source_recovered",
                    {**extra, "obs_reveal_missing_count": self._missing_count},
                )
            self._missing_count = 0
            self._missing_warned_at = None
            self._state = "resolved"
            if visible:
                self._shown += 1
            self._log("source_visible" if visible else "source_hidden", extra)

    def _note_missing(self, extra: dict[str, Any]) -> None:
        """Count a miss, and say so again only every so often.

        Not once per shot, which buries everything else in the log; not once
        per session, which is a line at start-up nobody at the bay reads.
        """

        self._state = "missing"
        self._missing_count += 1
        now = self._monotonic()
        last = self._missing_warned_at
        if last is not None and now - last < self._missing_interval:
            return
        self._missing_warned_at = now
        self._log(
            "source_missing",
            {**extra, "obs_reveal_missing_count": self._missing_count},
            level=logging.WARNING,
        )

    async def _connected(self) -> VisibilityClient:
        if self._client is not None:
            return self._client
        client = self._factory()
        await client.connect()
        self._client = client
        self._log("obs_connected", self._names())
        return client

    async def _drop_client(self) -> None:
        """Throw the connection away. The next command opens a fresh one."""

        client = self._client
        self._client = None
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:
            logger.debug(f"{self._feature}_disconnect_failed", exc_info=True)

    # --- logging ----------------------------------------------------------

    def _names(self) -> dict[str, Any]:
        return {
            "obs_feature": self._feature,
            "obs_scene": self._scene,
            "obs_source": self._source,
        }

    def _configuration(self) -> dict[str, Any]:
        return {
            **self._names(),
            "obs_reveal_delay_seconds": self._delay,
            "obs_reveal_duration_seconds": self._duration,
        }

    def _log(
        self, suffix: str, extra: dict[str, Any], *, level: int = logging.INFO
    ) -> None:
        """One event name per feature, one set of field names for all of them.

        The name is built here rather than passed in so the two halves cannot
        disagree: every event this class writes is `<feature>_<suffix>`, and
        `obs_feature` on the record says the same thing again in a field that
        can be filtered on.
        """

        logger.log(level, f"{self._feature}_{suffix}", extra=extra)
