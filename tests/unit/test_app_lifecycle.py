"""Boundaries in the application wiring that must hold even when things fail."""

import asyncio
import inspect
import logging
from dataclasses import dataclass

import pytest

from caddiej import app as app_module
from caddiej.adapters.gspro import build_adapter
from caddiej.adapters.hud import ConnectionRegistry, HudServer, create_app, overlay_url
from caddiej.adapters.obs.client import ObsTransportConnectionError
from caddiej.config import Settings
from caddiej.core import AsyncEventBus, Event
from caddiej.domain.shot import Club, ClubKind
from caddiej.domain.speech import Utterance
from caddiej.floor import FloorPoint, FloorService
from caddiej.lighting.modulation import Flourish
from caddiej.runtime import TaskSupervisor


class UnreachableObsClient:
    """An OBS that is not running, which is a normal state for a user's machine."""

    def __init__(self, **_kwargs: object) -> None:
        self.disconnected = False

    async def connect(self) -> None:
        raise ObsTransportConnectionError("Could not establish the OBS connection")

    async def disconnect(self) -> None:
        self.disconnected = True


class RejectingObsClient(UnreachableObsClient):
    """An OBS that connects but refuses the request."""

    async def connect(self) -> None:
        return None

    async def get_version(self) -> object:
        raise ObsTransportConnectionError("connection dropped mid-request")


def settings_for_provisioning() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        OBS_PASSWORD="",
        OBS_PROVISION_FLOOR_SOURCE=True,
    )


async def test_unreachable_obs_does_not_stop_provisioning_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # OBS only hosts the overlay, so its absence must never abort startup.
    created: list[UnreachableObsClient] = []

    def factory(**kwargs: object) -> UnreachableObsClient:
        client = UnreachableObsClient(**kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(app_module, "ObsClient", factory)

    await app_module._provision_floor_source(
        settings_for_provisioning(), "http://127.0.0.1:8730/"
    )

    assert created[0].disconnected, "the OBS connection must still be released"


async def test_obs_rejecting_a_request_does_not_stop_provisioning_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "ObsClient", lambda **kwargs: RejectingObsClient())

    await app_module._provision_floor_source(
        settings_for_provisioning(), "http://127.0.0.1:8730/"
    )


async def test_stop_server_does_not_swallow_cancellation() -> None:
    # Swallowing CancelledError would make a cancelled run() look successful.
    service = FloorService()
    registry = ConnectionRegistry()
    server = HudServer(create_app(service, registry), host="127.0.0.1", port=0)
    never_finishes = asyncio.create_task(asyncio.Event().wait())

    stopping = asyncio.create_task(
        app_module._stop_server(server, never_finishes, timeout=10)
    )
    await asyncio.sleep(0)
    stopping.cancel()

    with pytest.raises(asyncio.CancelledError):
        await stopping

    never_finishes.cancel()
    await asyncio.gather(never_finishes, return_exceptions=True)


async def test_stop_server_reports_a_shutdown_that_overruns_the_budget() -> None:
    service = FloorService()
    registry = ConnectionRegistry()
    server = HudServer(create_app(service, registry), host="127.0.0.1", port=0)
    never_finishes = asyncio.create_task(asyncio.Event().wait())

    await app_module._stop_server(server, never_finishes, timeout=0.05)

    assert never_finishes.cancelled()


def test_the_presenter_is_placed_where_the_operator_configured_the_tee() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        FLOOR_TEE_X_M=0.4,
        FLOOR_TEE_Y_M=0.9,
        FLOOR_TEE_DRIVER_X_M=0.35,
        FLOOR_TEE_DRIVER_Y_M=0.55,
        FLOOR_CLUB_PATH_LENGTH=0.5,
    )
    floor = FloorService()
    presenter = app_module._build_presenter(settings, floor)

    presenter.show_address(None)
    assert floor.state.marker.position == FloorPoint(x=0.4, y=0.9)

    presenter.show_address(Club(kind=ClubKind.DRIVER))
    assert floor.state.marker.position == FloorPoint(x=0.35, y=0.55)


def test_no_shot_source_is_started_when_gspro_is_off() -> None:
    settings = Settings(_env_file=None, GSPRO_MODE="off")  # type: ignore[call-arg]
    supervisor = TaskSupervisor(shutdown_timeout=1.0, on_failure=lambda: None)

    app_module._start_gspro(settings, supervisor, AsyncEventBus())

    assert supervisor.tasks == frozenset()


@pytest.mark.parametrize(
    ("mode", "task_name"),
    [("interpose", "gspro"), ("simulate", "gspro-simulation")],
)
async def test_each_gspro_mode_starts_its_own_supervised_task(
    mode: str, task_name: str
) -> None:
    # Both must be supervised, so that one dying stops CaddieJ rather than
    # leaving it running with nothing feeding the floor.
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        GSPRO_MODE=mode,
        GSPRO_BIND_PORT=0,
    )
    supervisor = TaskSupervisor(shutdown_timeout=1.0, on_failure=lambda: None)

    app_module._start_gspro(settings, supervisor, AsyncEventBus())

    assert {task.get_name() for task in supervisor.tasks} == {task_name}
    await supervisor.shutdown()


async def test_observe_mode_starts_passive_observers_and_does_not_bind() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        GSPRO_MODE="observe",
    )
    round_view = app_module._RoundObserverHealth()
    protee_view = app_module._ProTeeObserverHealth()
    app_module._attach_observers(settings, _ignore, round_view, protee_view)
    supervisor = TaskSupervisor(shutdown_timeout=1.0, on_failure=lambda: None)

    app_module._start_gspro(
        settings,
        supervisor,
        AsyncEventBus(),
        round_observer=round_view,
        protee_observer=protee_view,
    )

    assert {task.get_name() for task in supervisor.tasks} == {"gspro-observe"}
    assert round_view.adapter is not None
    assert protee_view.adapter is not None
    await supervisor.shutdown()


class TestNothingOptionalStartsBeforeObservation:
    """Passive observation used to be the last thing `run` did.

    It came after a Kokoro model load, an HTTP call to a Hue bridge, binding
    the overlay's port and an awaited OBS round trip — and both of the last
    two are ways for it never to happen at all: `wait_until_serving` raises on
    a port another instance already holds. A session spent unobserved because
    the overlay could not bind is a session of shots that no longer exist,
    since both observers discard whatever is already in their file when they
    first read it.

    Checked against the source of `run` rather than by running it, because
    what is being pinned is an ordering and the failure is a line moving. A
    test that drove the whole lifecycle would need a real port, a real OBS and
    a real model to be able to tell the difference.
    """

    @staticmethod
    def position_of(call: str) -> int:
        source = inspect.getsource(app_module.run)
        index = source.find(call)
        assert index >= 0, f"{call} is no longer in run(); this test needs updating"
        return index

    def test_observers_start_before_the_overlay_is_waited_on(self) -> None:
        assert self.position_of("_start_gspro(") < self.position_of(
            "wait_until_serving("
        )

    def test_observers_start_before_obs_is_spoken_to(self) -> None:
        assert self.position_of("_start_gspro(") < self.position_of(
            "_provision_floor_source("
        )

    def test_observers_start_before_the_assistant_api(self) -> None:
        assert self.position_of("_start_gspro(") < self.position_of(
            "_start_assistant_api("
        )


class TestOneObserverCannotTakeTheOtherWithIt:
    """Passive observation outlives every other subsystem, or it is not passive.

    Both observers ran under one bare `asyncio.gather`. `serve()` catches what
    a poll can raise, but not everything above it — a root path that stops
    resolving, a clock that throws — and anything that escaped failed the
    supervised task, which is a shutdown of the whole process. Worse, `gather`
    does not cancel its siblings, so the surviving observer carried on as an
    untracked orphan while CaddieJ shut down around it, still publishing onto
    a bus that was closing.
    """

    class Exploding:
        def __init__(self) -> None:
            self.served = False

        async def serve(self) -> None:
            self.served = True
            raise RuntimeError("the root path stopped resolving")

    class Patient:
        def __init__(self) -> None:
            self.cancelled = False

        async def serve(self) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    async def test_the_survivor_keeps_running(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        broken = self.Exploding()
        working = self.Patient()

        with caplog.at_level(logging.ERROR):
            task = asyncio.create_task(
                app_module._run_observers([broken, working])  # type: ignore[list-item]
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            assert broken.served
            assert not task.done(), "one observer failing ended both"

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert working.cancelled, "the survivor was left behind rather than stopped"
        assert [r.getMessage() for r in caplog.records].count("observer_stopped") == 1

    async def test_the_supervised_task_does_not_fail(self) -> None:
        """A failing supervised task is a reason to stop CaddieJ. An observer
        that dies is a reason to write it down."""

        failures: list[None] = []
        supervisor = TaskSupervisor(
            shutdown_timeout=1.0, on_failure=lambda: failures.append(None)
        )
        broken = self.Exploding()

        supervisor.start(
            app_module._run_observers([broken]),  # type: ignore[list-item]
            name="gspro-observe",
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await supervisor.shutdown()

        assert failures == []


@pytest.mark.parametrize(
    ("history", "capture", "expected"),
    [
        (True, True, True),
        (True, False, False),
        # Nowhere to put them, so holding them would be a memory cost for
        # nothing.
        (False, True, False),
    ],
)
def test_raw_payloads_are_only_captured_when_there_is_somewhere_to_keep_them(
    history: bool, capture: bool, expected: bool
) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        GSPRO_BIND_PORT=0,
        HISTORY_ENABLED=history,
        HISTORY_RECORD_RAW_PAYLOADS=capture,
    )

    adapter = build_adapter(settings, publish=_ignore)

    assert adapter.captures_raw_payloads is expected


async def _ignore(_event: object) -> None:
    return None


def test_history_is_built_from_the_configured_path(tmp_path) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        HISTORY_DATABASE_PATH=str(tmp_path / "rounds.sqlite3"),
        PLAYER_NAME="Johan",
    )

    history = app_module._build_history(settings)

    assert history.health().path == str(tmp_path / "rounds.sqlite3")
    assert history.health().enabled is True


def test_video_is_built_from_the_configured_camera(tmp_path) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        VIDEO_ENABLED=True,
        VIDEO_CAMERA_KIND="v4l2",
        VIDEO_CAMERA_DEVICE="/dev/video3",
        VIDEO_CAMERA_ID="face-on",
        VIDEO_MEDIA_ROOT=str(tmp_path / "media"),
    )

    async def publish(event) -> None:
        return None

    video = app_module._build_video(settings, publish)

    assert video.camera.camera_id == "face-on"
    assert "/dev/video3" in video.camera.input_arguments
    assert video.health().media_root == str(tmp_path / "media")


def test_video_that_is_switched_off_touches_no_disk(tmp_path) -> None:
    # Building it must be harmless: it is constructed on every start, including
    # the overwhelming majority where nobody has a camera.
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        VIDEO_MEDIA_ROOT=str(tmp_path / "media"),
    )

    async def publish(event) -> None:
        return None

    video = app_module._build_video(settings, publish)

    assert video.health().enabled is False
    assert not (tmp_path / "media").exists()


class TestCoachingIsWiredToTheCameraItWasConfiguredWith:
    """The clip-to-model path was unreachable in every configuration.

    `_build_coaching` chose its frame extractor with `video if
    video.is_available else None`, and it runs in the construction pass —
    ninety lines before `await video.open()`, which is what sets
    `is_available`. So it was always false, coaching always got `None`, and
    no camera frame could reach a model however the bay was set up. Every
    test that exercised clip frames handed the extractor to the constructor
    itself, so the wiring was the one part never covered.

    `enabled` is the right question here because it is settled by
    configuration. Whether a camera is filming *now* is a question for the
    moment a clip is wanted, and video answers that one by producing the clip
    or refusing it.
    """

    @staticmethod
    def built(tmp_path, **overrides: object):
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            VIDEO_MEDIA_ROOT=str(tmp_path / "media"),
            SWING_CAPTURE_OUTPUT_DIR=str(tmp_path / "capture"),
            HISTORY_DATABASE_PATH=str(tmp_path / "history.db"),
            COACHING_ENABLED=True,
            COACHING_SEND_FRAMES=True,
            **overrides,  # type: ignore[arg-type]
        )

        async def publish(event) -> None:
            return None

        video = app_module._build_video(settings, publish)
        capture = app_module._build_swing_capture(settings, publish)
        history = app_module._build_history(settings)
        coaching = app_module._build_coaching(
            settings, publish, history, video, capture
        )
        return video, coaching, history

    def test_a_bay_with_a_camera_can_reach_the_model_with_clip_frames(
        self, tmp_path
    ) -> None:
        video, coaching, _history = self.built(tmp_path, VIDEO_ENABLED=True)

        assert video.is_available is False, "nothing has been opened yet"
        assert coaching._frames is video

    def test_a_bay_with_no_camera_is_not_told_to_wait_for_one(self, tmp_path) -> None:
        _video, coaching, _history = self.built(tmp_path, VIDEO_ENABLED=False)

        assert coaching._frames is None

    def test_the_replay_screenshots_are_wired_too(self, tmp_path) -> None:
        _video, coaching, _history = self.built(
            tmp_path,
            SWING_CAPTURE_ENABLED=True,
            SWING_CAPTURE_PANEL_SWING_SOURCE="CaddieJ Swing",
        )

        assert coaching._capture_stills is not None

    def test_the_protee_request_gets_its_own_timeout(self, tmp_path) -> None:
        _video, coaching, _history = self.built(
            tmp_path,
            COACH_OPENAI_TIMEOUT_SECONDS=25,
            COACH_OPENAI_BUDGET_SECONDS=40,
        )

        assert coaching._protee_timeout == 25.0
        assert coaching._protee_budget == 40.0
        assert coaching._timeout == 8.0, "COACHING_TIMEOUT stays the GSPro path's"

    def test_advice_is_wired_to_history_too(self, tmp_path) -> None:
        """The same bug, one line below, and it cost the whole coaching record.

        `record=history.record_advice if history.is_available else None` runs
        in the same construction pass, and `history.open()` is eighty lines
        further down. So every coaching line was published, spoken and
        projected, and none of it was written to the database — leaving a
        session whose shots were all recorded and whose advice was all
        missing, with nothing saying why.
        """

        _video, coaching, history = self.built(tmp_path, HISTORY_ENABLED=True)

        assert history.is_available is False, "nothing has been opened yet"
        assert coaching._record is not None

    def test_history_switched_off_records_nothing(self, tmp_path) -> None:
        _video, coaching, _history = self.built(tmp_path, HISTORY_ENABLED=False)

        assert coaching._record is None


def test_speech_that_is_switched_off_builds_no_engine_at_all(tmp_path) -> None:
    # Building it must be harmless: it is constructed on every start, including
    # the overwhelming majority where nobody wants to be spoken to. Resolving
    # the engine would import Kokoro, which is not installed on most machines.
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        SPEECH_CACHE_ROOT=str(tmp_path / "speech"),
    )

    async def publish(event) -> None:
        return None

    speech = app_module._build_speech(settings, publish)

    health = speech.health()
    assert health.enabled is False
    assert health.engine is None
    assert health.output is None
    assert not (tmp_path / "speech").exists()


def test_speech_is_built_from_the_configured_voice_and_output(tmp_path) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        SPEECH_ENABLED=True,
        SPEECH_ENGINE="fake",
        SPEECH_OUTPUT="none",
        SPEECH_VOICE="bf_emma",
        SPEECH_CACHE_ROOT=str(tmp_path / "speech"),
    )

    async def publish(event) -> None:
        return None

    speech = app_module._build_speech(settings, publish)

    health = speech.health()
    assert health.voice == "bf_emma"
    assert health.engine == "fake"
    assert health.output == "none"


async def test_speech_switched_on_with_fakes_opens_and_closes_cleanly(
    tmp_path,
) -> None:
    # The one path that proves the wiring, rather than the construction: a
    # service that opens, reports itself available, says something, and lets go
    # of everything on the way out.
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        SPEECH_ENABLED=True,
        SPEECH_ENGINE="fake",
        SPEECH_OUTPUT="none",
        SPEECH_CACHE_ROOT=str(tmp_path / "speech"),
    )

    async def publish(event) -> None:
        return None

    speech = app_module._build_speech(settings, publish)
    await speech.open()
    try:
        assert speech.is_available is True
        assert await speech.say(Utterance(text="Square the face."))
    finally:
        await speech.aclose()

    assert speech.is_available is False
    assert speech.is_idle
    # The cache root is created only once there is something to put in it.
    assert (tmp_path / "speech").exists()


def test_lighting_that_is_switched_off_builds_no_bridge_at_all() -> None:
    # Building it must be harmless: it is constructed on every start, including
    # the overwhelming majority where nobody wants their lights touched.
    # Resolving the output would import an HTTP client that need not be present.
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    lighting = app_module._build_lighting(settings)

    health = lighting.health()
    assert health.enabled is False
    assert health.available is False
    # Nothing was constructed, so what it reports holding is the do-nothing port
    # rather than a Hue adapter waiting for an address.
    assert health.output == "none"


def test_lighting_is_built_from_the_configured_range_and_output() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        LIGHTING_ENABLED=True,
        LIGHTING_OUTPUT="none",
        LIGHTING_MINIMUM=0.5,
        LIGHTING_MAXIMUM=0.9,
    )

    lighting = app_module._build_lighting(settings)

    assert lighting.health().output == "none"
    assert lighting._modulation.minimum == 0.5  # noqa: SLF001
    assert lighting._modulation.maximum == 0.9  # noqa: SLF001


def test_a_hue_output_is_built_without_reaching_the_network() -> None:
    # Constructing the adapter must not open a socket, so that a start-up with a
    # wrong address still gets as far as reporting it.
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        LIGHTING_ENABLED=True,
        LIGHTING_OUTPUT="hue",
        LIGHTING_BRIDGE_HOST="10.0.0.5",
        LIGHTING_APPLICATION_KEY="a-key",
        LIGHTING_LIGHTS="Sitting room",
    )

    lighting = app_module._build_lighting(settings)

    assert lighting.health().output == "hue"
    assert lighting.health().available is False, "nothing is open until open()"


async def test_lighting_switched_on_with_a_null_output_opens_and_closes_cleanly() -> (
    None
):
    # The one path that proves the wiring rather than the construction: a service
    # that opens, reports itself available, plays a cue, and lets go of
    # everything on the way out.
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        LIGHTING_ENABLED=True,
        LIGHTING_OUTPUT="none",
    )

    lighting = app_module._build_lighting(settings)
    await lighting.open()
    try:
        assert lighting.is_available is True
        # A do-nothing port reports an empty room, so there is nothing to dim and
        # the cue finds nothing to do — which is exactly what it should report.
        assert await lighting.play(Flourish(target=0.7, seconds=0.5)) is True
    finally:
        await lighting.aclose()

    assert lighting.is_available is False
    assert lighting.is_idle


class Closing:
    """A subsystem that records the order it was closed in, and nothing else."""

    def __init__(self, name: str, order: list[str]) -> None:
        self._name = name
        self._order = order
        self.closed = False

    async def aclose(self) -> None:
        # A real close awaits something — a worker draining, a bridge answering
        # — so this one does too. Ordering bugs hide behind the first await.
        await asyncio.sleep(0)
        self.closed = True
        self._order.append(self._name)


class Subsystems:
    """The set of things `_close_subsystems` takes apart, as recorders."""

    def __init__(self) -> None:
        self.order: list[str] = []
        self.play = Closing("play", self.order)
        self.shot_video = Closing("shot_video", self.order)
        self.top_impact = Closing("top_impact", self.order)
        self.swing_capture = Closing("swing_capture", self.order)
        self.effects = Closing("effects", self.order)
        self.speech = Closing("speech", self.order)
        self.lighting = Closing("lighting", self.order)
        self.coaching = Closing("coaching", self.order)
        self.video = Closing("video", self.order)

    async def close(self, event_bus: AsyncEventBus, ingestion) -> None:
        await app_module._close_subsystems(
            Settings(_env_file=None),  # type: ignore[call-arg]
            ingestion=ingestion,
            event_bus=event_bus,
            assistant_api=None,
            assistant_task=None,
            play=self.play,  # type: ignore[arg-type]
            shot_video=self.shot_video,  # type: ignore[arg-type]
            top_impact=self.top_impact,  # type: ignore[arg-type]
            swing_capture=self.swing_capture,  # type: ignore[arg-type]
            effects=self.effects,  # type: ignore[arg-type]
            speech=self.speech,  # type: ignore[arg-type]
            lighting=self.lighting,  # type: ignore[arg-type]
            coaching=self.coaching,  # type: ignore[arg-type]
            video=self.video,  # type: ignore[arg-type]
        )


class TestShutdownOrder:
    """Ingestion stops first. Everything else follows from that.

    The order below is not a preference. Every subsystem in it handles shots, so
    a ball struck while they are being closed would be ingested normally and
    then handed to services that have already finished — which is a shot that is
    half recorded, and worse than a shot that arrived a second too late.
    """

    async def test_ingestion_is_stopped_before_anything_that_handles_a_shot(
        self,
    ) -> None:
        bus = AsyncEventBus()
        await bus.start()
        subsystems = Subsystems()
        stopped = asyncio.Event()

        async def ingest() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                stopped.set()
                raise

        ingestion = asyncio.create_task(ingest())
        await asyncio.sleep(0)

        try:
            await subsystems.close(bus, ingestion)
        finally:
            await bus.stop(timeout=1.0)

        assert stopped.is_set(), "ingestion must be stopped, not merely asked"
        assert subsystems.order == [
            "play",
            "shot_video",
            "top_impact",
            "swing_capture",
            "effects",
            "speech",
            "lighting",
            "coaching",
            "video",
        ]

    async def test_the_last_shot_is_handled_by_subsystems_that_are_still_open(
        self,
    ) -> None:
        """The point of draining the bus before closing anything.

        A shot that arrived a moment before Ctrl-C is somewhere in the bus queue
        when shutdown begins. Without the drain it is dispatched into subsystems
        that are already closing, and the handler finds a service that has let
        go of the thing it needs.
        """

        bus = AsyncEventBus()
        subsystems = Subsystems()
        seen: list[bool] = []

        async def handle(_event: SampleShot) -> None:
            # Slow on purpose: a handler that returns without awaiting would be
            # finished before the first close either way, and would prove
            # nothing about whether shutdown waited for it.
            await asyncio.sleep(0.05)
            seen.append(subsystems.video.closed)

        bus.subscribe(SampleShot, handle)
        await bus.start()

        async def ingest() -> None:
            await bus.publish(SampleShot())
            await asyncio.Event().wait()

        ingestion = asyncio.create_task(ingest())
        await asyncio.sleep(0)

        try:
            await subsystems.close(bus, ingestion)
        finally:
            await bus.stop(timeout=1.0)

        assert seen == [False], "the shot was handled by a subsystem already closing"

    async def test_shutdown_survives_ingestion_that_ignores_cancellation(self) -> None:
        """Otherwise a bridge that will not let go takes the session row with it:
        nothing below the wait would run."""

        bus = AsyncEventBus()
        await bus.start()
        subsystems = Subsystems()

        async def stubborn() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(30)

        ingestion = asyncio.create_task(stubborn())
        await asyncio.sleep(0)

        try:
            await asyncio.wait_for(
                app_module._stop_ingestion(ingestion, timeout=0.05), timeout=2.0
            )
        finally:
            ingestion.cancel()
            await asyncio.gather(ingestion, return_exceptions=True)
            await bus.stop(timeout=1.0)

        assert subsystems.order == []

    async def test_the_sequence_can_be_cut_short_at_a_subsystem_that_hangs(
        self,
    ) -> None:
        """What makes a budget for the whole of shutdown possible.

        Each step already has its own timeout and the sum of them is far longer
        than the grace an operating system gives a process between "please stop"
        and SIGKILL. `run` therefore caps the total — which only works if this
        can be abandoned part way through, leaving the two steps that matter
        most, stopping the bus and closing the session row, to the caller.
        """

        bus = AsyncEventBus()
        await bus.start()
        subsystems = Subsystems()

        async def never_returns() -> None:
            await asyncio.sleep(30)

        subsystems.speech.aclose = never_returns  # type: ignore[method-assign]

        try:
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.05):
                    await subsystems.close(bus, None)
        finally:
            await asyncio.wait_for(bus.stop(timeout=1.0), timeout=2.0)

        assert subsystems.order == [
            "play",
            "shot_video",
            "top_impact",
            "swing_capture",
            "effects",
        ]
        assert subsystems.lighting.closed is False

    async def test_there_is_nothing_to_stop_when_no_source_was_started(self) -> None:
        # GSPRO_MODE=off, which is how the floor demo runs.
        await app_module._stop_ingestion(None, timeout=1.0)

    async def test_a_source_that_already_finished_is_left_alone(self) -> None:
        finished = asyncio.create_task(asyncio.sleep(0))
        await finished

        await app_module._stop_ingestion(finished, timeout=1.0)

        assert not finished.cancelled()


async def test_a_subsystem_that_fails_to_open_still_lets_go_of_everything(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opening a subsystem is documented as unable to fail, and "documented" is
    not "cannot". By the time the first `open` runs, the bus worker and the
    history writer are both going — so a surprise from any of them has to reach
    the code that stops them, rather than leaving two threads and a task behind
    while the exception goes to the top."""

    async def explode(_self: object) -> None:
        raise RuntimeError("the camera driver took the process with it")

    monkeypatch.setattr(app_module.VideoService, "open", explode)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        GSPRO_MODE="off",
        VIDEO_ENABLED=True,
        VIDEO_MEDIA_ROOT=str(tmp_path / "media"),
        HISTORY_DATABASE_PATH=str(tmp_path / "history.sqlite3"),
    )

    with pytest.raises(RuntimeError, match="camera driver"):
        await app_module.run(settings)

    lingering = {
        task.get_name()
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
    }
    assert "caddiej-event-bus" not in lingering
    assert "history-writer" not in lingering


@dataclass(frozen=True, slots=True)
class SampleShot(Event):
    """Stands in for a shot on the bus, without pulling in a whole domain."""


def test_overlay_url_uses_a_host_a_browser_can_reach() -> None:
    assert overlay_url("127.0.0.1", 8730) == "http://127.0.0.1:8730/"
    assert overlay_url("localhost", 9000) == "http://localhost:9000/"
    # A wildcard bind is not connectable, so it is reported as loopback.
    assert overlay_url("0.0.0.0", 8730) == "http://127.0.0.1:8730/"  # noqa: S104
    assert overlay_url("::", 8730) == "http://127.0.0.1:8730/"
    assert overlay_url("::1", 8730) == "http://[::1]:8730/"
