"""CaddieJ application lifecycle."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict

from caddiej.adapters.api import (
    AssistantServer,
    Guard,
    api_url,
    create_api_app,
    is_loopback_bind,
)
from caddiej.adapters.gspro import (
    GSProConnectAdapter,
    GSProHealth,
    build_adapter,
    run_simulated_round,
)
from caddiej.adapters.hud import ConnectionRegistry, HudServer, create_app, overlay_url
from caddiej.adapters.obs import ObsClient, ObsConnectionError
from caddiej.assistant.commands import CommandFacade
from caddiej.assistant.contract import SubsystemPayload
from caddiej.assistant.reads import ReadFacade, SubsystemProbe, subsystem_probe
from caddiej.coaching import AnalysisService, FrameSource
from caddiej.coaching.factory import build_provider
from caddiej.config import Settings
from caddiej.core import AsyncEventBus
from caddiej.core.events import Event
from caddiej.domain import CoachingFocus, PlayFlow
from caddiej.domain.event_policy import delivery_for, queue_size_for
from caddiej.domain.events import LinkState
from caddiej.effects import EffectDirector, build_effect_director, obs_clients
from caddiej.floor import (
    FloorPresenter,
    FloorService,
    PhysicalPoint,
    RectangularCalibration,
    rectangular_calibration,
    run_demo,
)
from caddiej.lighting import LightingService, build_lighting_service
from caddiej.persistence import HistoryService, build_history_service
from caddiej.persistence.service import app_version
from caddiej.runtime import TaskSupervisor, shutdown_signals
from caddiej.speech import SpeechService, build_speech_service
from caddiej.video import (
    CaptureProfile,
    VideoService,
    build_camera,
    build_video_service,
)

logger = logging.getLogger(__name__)

Publish = Callable[[Event], Awaitable[None]]


async def run(settings: Settings | None = None) -> None:
    """Serve the floor overlay until the process is asked to stop."""

    resolved = settings or Settings()

    event_bus = AsyncEventBus(policy=delivery_for, queue_size=queue_size_for)
    floor = FloorService()
    registry = ConnectionRegistry()
    # The registry is a listener, not an owner: state changes fan out to clients
    # without the service knowing a transport exists.
    floor.add_listener(registry.broadcast)

    # The domain decides what the floor shows; the presenter decides how it looks.
    # Subscribing before anything can publish means no shot can slip past.
    play = PlayFlow(
        floor=_build_presenter(resolved, floor),
        publish=event_bus.publish,
        shot_display_seconds=resolved.floor_shot_display_seconds,
        advice_display_seconds=resolved.coaching_display_seconds,
    )
    play.subscribe(event_bus)

    # Built here so `/healthz` can ask it before anything is listening. `serve()`
    # is not called until the lifecycle starts the ingestion task.
    gspro = _GSProHealth()
    if resolved.gspro_mode == "interpose":
        gspro.attach(build_adapter(resolved, event_bus.publish))

    history = _build_history(resolved)
    # Opening cannot fail loudly: history that will not start disables itself so
    # that a locked file or a full disk never stops someone hitting balls.
    await history.open()
    history.subscribe(event_bus)

    video = _build_video(resolved, event_bus.publish)
    coaching = _build_coaching(resolved, event_bus.publish, history, video)
    speech = _build_speech(resolved, event_bus.publish)
    lighting = _build_lighting(resolved)
    effects = _build_effects(resolved, floor, speech, lighting)

    server = HudServer(
        create_app(
            floor,
            registry,
            {
                "history": lambda: asdict(history.health()),
                "video": lambda: asdict(video.health()),
                "coaching": lambda: asdict(coaching.health()),
                "speech": lambda: asdict(speech.health()),
                "lighting": lambda: asdict(lighting.health()),
                "effects": lambda: asdict(effects.health()),
                "event_bus": lambda: asdict(event_bus.health()),
                "gspro": gspro.snapshot,
            },
        ),
        host=resolved.hud_host,
        port=resolved.hud_port,
    )

    # Built here so that a misconfiguration is reported before anything starts
    # listening, and so that `None` — the normal case — carries no cost at all.
    assistant_api = _build_assistant_api(
        resolved,
        history=history,
        coaching=coaching,
        play=play,
        subsystems={
            "history": subsystem_probe(history.health),
            "video": subsystem_probe(video.health),
            # Not the generic narrowing, because coaching's own `available`
            # answers whether a model will be asked. An assistant asking
            # whether CaddieJ is coaching should be told yes when it is
            # naming faults from templates, which is the default configuration
            # and not a degraded one.
            "coaching": lambda: SubsystemPayload(
                enabled=resolved.coaching_enabled,
                available=coaching.is_coaching,
            ),
            "speech": subsystem_probe(speech.health),
            "lighting": subsystem_probe(lighting.health),
            "effects": subsystem_probe(effects.health),
            "gspro": gspro.subsystem,
        },
    )
    # Everything below is inside the try, including opening the subsystems. An
    # `open` is documented as unable to fail, but "documented" is not "cannot":
    # the bus worker and the history writer are both running by now, and a
    # surprise from any of the opens must still reach the code that stops them.
    try:
        await event_bus.start()
        logger.info("event_bus_started")

        # After the bus, because a camera reports what it is doing through it.
        # Opening cannot fail loudly either: a missing camera or a missing FFmpeg
        # leaves video switched off and changes nothing else.
        await video.open()
        video.subscribe(event_bus)

        # After video, because whether frames can be sent depends on whether a
        # camera actually started. Opening cannot fail loudly either: a missing
        # key or a missing SDK leaves coaching working from its templates.
        await coaching.open()
        coaching.subscribe(event_bus)

        # After coaching, because it is the only subsystem nothing else waits on.
        # Opening loads a speech model and claims the sound card, and failing at
        # either leaves the room quiet and changes nothing else.
        await speech.open()
        speech.subscribe(event_bus)

        # Last of all, because it reacts to speech and so has nothing to say to
        # anything that starts before it. Opening reaches a bridge on the local
        # network, and failing leaves the lights alone.
        await lighting.open()
        lighting.subscribe(event_bus)

        # After the outputs it drives, so that whether a channel is available is
        # already settled by the time the first scoring event could arrive. There
        # is nothing to open: the director holds no resource of its own, and the
        # one channel that holds a connection opens it on first use.
        effects.subscribe(event_bus)

        async with shutdown_signals() as shutdown:
            # A supervised task dying is a reason to stop, exactly like a signal:
            # CaddieJ must not keep running with a dead subsystem.
            supervisor = TaskSupervisor(
                shutdown_timeout=resolved.shutdown_timeout,
                on_failure=shutdown.set,
            )
            server_task = supervisor.start(server.serve(), name="hud-server")
            assistant_task: asyncio.Task[None] | None = None
            # Bound before anything can fail, so that shutdown can always ask
            # about them however far startup got.
            ingestion: asyncio.Task[None] | None = None
            try:
                await server.wait_until_serving(resolved.hud_startup_timeout)
                url = overlay_url(resolved.hud_host, server.port)
                logger.info(
                    "hud_server_listening",
                    extra={
                        "hud_host": resolved.hud_host,
                        "hud_port": server.port,
                        "hud_url": url,
                    },
                )

                floor.set_online(resolved.floor_status_text)

                if resolved.obs_provision_floor_source:
                    await _provision_floor_source(resolved, url)

                # The demo and the domain both write the floor, so exactly one of
                # them owns it. The demo wins only when nothing real can arrive.
                if resolved.floor_demo and resolved.gspro_mode == "off":
                    supervisor.start(
                        run_demo(
                            floor,
                            resolved.floor_demo_step_seconds,
                            calibration=_floor_calibration(resolved),
                        ),
                        name="floor-demo",
                    )
                else:
                    if resolved.floor_demo:
                        logger.warning("floor_demo_ignored_while_gspro_enabled")
                    play.start()

                ingestion = _start_gspro(resolved, supervisor, event_bus, gspro)

                # Last, and on its own terms. Nothing above waits on it, and a
                # failure to start is reported and stepped over: an assistant
                # that cannot be reached is a missing convenience, whereas
                # stopping CaddieJ over it would make the simulator depend on
                # the thing that was supposed to be optional.
                if assistant_api is not None:
                    assistant_task = await _start_assistant_api(
                        resolved, supervisor, assistant_api
                    )

                logger.info("caddiej_ready")
                await shutdown.wait()
                logger.info("shutdown_requested")
            finally:
                # Bounded as a whole, not just step by step. Every step below
                # has its own budget and the sum of them is far longer than the
                # few seconds an operating system allows a process between
                # "please stop" and SIGKILL. Without a cap on the total, the
                # last steps — the ones that close the session row — are the
                # ones that never run on a machine that is actually shutting
                # down.
                try:
                    async with asyncio.timeout(resolved.shutdown_grace):
                        await _close_subsystems(
                            resolved,
                            ingestion=ingestion,
                            event_bus=event_bus,
                            assistant_api=assistant_api,
                            assistant_task=assistant_task,
                            play=play,
                            effects=effects,
                            speech=speech,
                            lighting=lighting,
                            coaching=coaching,
                            video=video,
                        )
                except TimeoutError:
                    logger.error(
                        "shutdown_grace_exceeded",
                        extra={"shutdown_seconds": resolved.shutdown_grace},
                    )
                # Outside the budget, because these two are what makes the
                # difference between a session that ended and a session that
                # stopped being written to, and neither can hang: the server has
                # its own timeout and the supervisor is a cancel.
                floor.set_shutting_down()
                await _stop_server(server, server_task, resolved.shutdown_timeout)
                # A backstop for anything the graceful path left behind.
                await supervisor.shutdown()
                logger.info("hud_server_stopped")

            if supervisor.failure is not None:
                raise supervisor.failure
    finally:
        await event_bus.stop(resolved.shutdown_timeout)
        logger.info("event_bus_stopped")
        # After the bus, so that every shot it still had queued has been offered
        # to history before history is asked to finish writing.
        await history.aclose()


async def _close_subsystems(
    settings: Settings,
    *,
    ingestion: asyncio.Task[None] | None,
    event_bus: AsyncEventBus,
    assistant_api: AssistantServer | None,
    assistant_task: asyncio.Task[None] | None,
    play: PlayFlow,
    effects: EffectDirector,
    speech: SpeechService,
    lighting: LightingService,
    coaching: AnalysisService,
    video: VideoService,
) -> None:
    """Take the subsystems apart in the order that keeps a shot whole."""

    # First of everything: stop new shots arriving. Everything below closes a
    # subsystem that handles them, and a ball struck while that is happening
    # would be ingested normally and then handed to services that have already
    # finished — recorded by history, refused by video, and given a coaching
    # failure it did not earn. There is no partial answer worth having here, so
    # the source is switched off before the consumers are.
    await _stop_ingestion(ingestion, settings.shutdown_timeout)
    # Then let what is already in flight finish, while everything that handles
    # it is still open. This is the whole reason the order above matters: the
    # last shot of the evening gets the same treatment as the first.
    await event_bus.drain(settings.shutdown_timeout)

    # After the bus is quiet, so no outside question can arrive while the
    # subsystems it reads are being taken apart.
    if assistant_api is not None:
        await _stop_assistant_api(assistant_api, assistant_task)
    await play.aclose()
    # Before the outputs it drives, and first of the three, because it is the
    # only thing that can still be putting words on a floor and dips into a
    # room. Returning means every output it touched is back to normal.
    await effects.aclose()
    # Before coaching, so that a line settled on the way out cannot start the
    # room talking while everything else is stopping. Whatever is mid-sentence
    # is given a moment to finish.
    await speech.aclose()
    # After speech, so the last sentence has already released the lights, and
    # before anything that could take time: this is the call that gives somebody
    # their living room back, and it is the one piece of shutdown that is
    # visible from an armchair.
    await lighting.aclose()
    # Before video, because a coaching job may still be waiting on a clip that
    # video is about to stop producing.
    await coaching.aclose()
    # Before the bus stops, because a clip still being written wants to announce
    # itself, and a stopped bus cannot carry the news.
    await video.aclose()


async def _stop_ingestion(ingestion: asyncio.Task[None] | None, timeout: float) -> None:
    """Stop the source of shots, and wait for it to have stopped.

    Cancelled rather than asked politely: the adapter's own shutdown path is a
    cancellation, the supervisor would do exactly this later, and there is
    nothing to drain — a launch monitor connection holds no work of CaddieJ's.
    """

    if ingestion is None or ingestion.done():
        return

    ingestion.cancel()
    done, _pending = await asyncio.wait({ingestion}, timeout=timeout)
    if not done:
        logger.warning("ingestion_shutdown_timed_out")
        return
    logger.info("ingestion_stopped")


def _build_history(settings: Settings) -> HistoryService:
    return build_history_service(
        enabled=settings.history_enabled,
        path=settings.history_database_path,
        player_name=settings.player_name,
        label=settings.session_label,
        queue_size=settings.history_queue_size,
        busy_timeout_seconds=settings.history_busy_timeout,
        drain_timeout_seconds=settings.history_drain_timeout,
    )


def _build_video(settings: Settings, publish: Publish) -> VideoService:
    camera = build_camera(
        settings.video_camera_kind,
        device=settings.video_camera_device,
        camera_id=settings.video_camera_id,
        fps=settings.video_camera_fps,
        width=settings.video_camera_width,
        height=settings.video_camera_height,
        latency_seconds=settings.video_camera_latency_seconds,
    )
    profile = CaptureProfile(
        segment_seconds=settings.video_segment_seconds,
        keyframe_seconds=settings.video_keyframe_seconds,
        buffer_seconds=settings.video_buffer_seconds,
        buffer_max_bytes=settings.video_buffer_max_bytes,
        encoder=settings.video_encoder,
        bitrate=settings.video_bitrate,
    )
    return build_video_service(
        enabled=settings.video_enabled,
        media_root=settings.video_media_root,
        camera=camera,
        publish=publish,
        profile=profile,
        binary=settings.video_ffmpeg_path,
        pre_seconds=settings.video_pre_impact_seconds,
        post_seconds=settings.video_post_impact_seconds,
        shot_report_lag_seconds=settings.video_shot_report_lag_seconds,
        queue_size=settings.video_clip_queue_size,
        restart_limit=settings.video_restart_limit,
        extract_timeout_seconds=settings.video_extract_timeout,
        drain_timeout_seconds=settings.video_drain_timeout,
        frame_width=settings.coaching_frame_width,
    )


def _build_coaching(
    settings: Settings,
    publish: Publish,
    history: HistoryService,
    video: VideoService,
) -> AnalysisService:
    """Assemble coaching, with whichever of its optional inputs are present.

    Both are genuinely optional and behave differently by their absence rather
    than by a flag: with no camera it coaches from the numbers, with no history
    it coaches without keeping a record, and with neither it still names the
    fault on the floor.
    """

    provider = build_provider(
        settings.coaching_provider,
        model=settings.coaching_model,
        api_key=settings.coaching_api_key.get_secret_value(),
        api_base=settings.coaching_api_base,
    )
    frames: FrameSource | None = video if video.is_available else None
    return AnalysisService(
        publish=publish,
        provider=provider,
        frames=frames,
        record=history.record_advice if history.is_available else None,
        enabled=settings.coaching_enabled,
        send_frames=settings.coaching_send_frames,
        preferred_focus=_coaching_focus(settings.coaching_focus),
        severity_fine=settings.coaching_severity_fine,
        baseline_window=settings.coaching_baseline_window,
        queue_size=settings.coaching_queue_size,
        clip_wait_seconds=settings.coaching_clip_wait,
        timeout_seconds=settings.coaching_timeout,
        total_budget_seconds=settings.coaching_budget_seconds,
        retry_limit=settings.coaching_retry_limit,
        min_interval_seconds=settings.coaching_min_interval,
        session_call_limit=settings.coaching_call_limit,
        max_output_tokens=settings.coaching_max_output_tokens,
        drain_timeout_seconds=settings.shutdown_timeout,
    )


def _build_speech(settings: Settings, publish: Publish) -> SpeechService:
    """Assemble the voice, or something that stays quiet in its place.

    Nothing else in the application takes a reference to this, which is the
    point: speech is a listener on the bus and nothing waits on it, so a machine
    with no speaker, no model, or no interest in being spoken to runs exactly
    the same way with it switched off.
    """

    return build_speech_service(
        publish=publish,
        enabled=settings.speech_enabled,
        engine=settings.speech_engine,
        output=settings.speech_output,
        voice=settings.speech_voice,
        speed=settings.speech_speed,
        device=settings.speech_device,
        cache_root=(
            settings.speech_cache_root if settings.speech_cache_enabled else None
        ),
        cache_bytes=settings.speech_cache_bytes,
        cache_files=settings.speech_cache_files,
        queue_size=settings.speech_queue_size,
        max_characters=settings.speech_max_characters,
        render_timeout_seconds=settings.speech_render_timeout,
        open_timeout_seconds=settings.speech_open_timeout,
        drain_timeout_seconds=settings.shutdown_timeout,
    )


def _build_lighting(settings: Settings) -> LightingService:
    """Assemble the lights, or something that leaves them alone in their place.

    Nothing else takes a reference to this either, and here that matters more
    than anywhere: lighting is the one subsystem that changes something outside
    CaddieJ which somebody else set deliberately. It observes speech, it gives
    the room back, and a house with no bridge in it runs identically.
    """

    return build_lighting_service(
        enabled=settings.lighting_enabled,
        output=settings.lighting_output,
        bridge_host=settings.lighting_bridge_host,
        application_key=settings.lighting_application_key.get_secret_value(),
        lights=settings.lighting_lights,
        minimum=settings.lighting_minimum,
        maximum=settings.lighting_maximum,
        max_commands_per_second=settings.lighting_max_commands_per_second,
        transition_scale=settings.lighting_transition_scale,
        failure_limit=settings.lighting_failure_limit,
        cooldown_seconds=settings.lighting_cooldown_seconds,
        command_timeout_seconds=settings.lighting_command_timeout,
    )


def _build_effects(
    settings: Settings,
    floor: FloorService,
    speech: SpeechService,
    lighting: LightingService,
) -> EffectDirector:
    """Assemble the one thing that decides how the room reacts to a round.

    The only subsystem that takes references to three others, and that is the
    whole point of it: what a birdie is worth is one judgement, and it should
    reach the floor, the voice and the lights as one judgement rather than as
    three that agree by coincidence.
    """

    return build_effect_director(
        floor=floor,
        speech=speech,
        lighting=lighting,
        enabled=settings.effects_enabled,
        use_floor=settings.effects_floor,
        use_speech=settings.effects_speech,
        use_lights=settings.effects_lights,
        use_obs=settings.effects_obs,
        obs_client_factory=obs_clients(
            host=settings.obs_host,
            port=settings.obs_port,
            password=settings.obs_password.get_secret_value(),
            connect_timeout=settings.obs_connect_timeout,
        ),
        obs_scene=settings.effects_obs_scene,
        obs_source=settings.effects_obs_source,
        obs_text_source=settings.effects_obs_text_source,
        max_seconds=settings.effects_max_seconds,
        channel_timeout_seconds=settings.effects_channel_timeout,
    )


def _build_assistant_api(
    settings: Settings,
    history: HistoryService,
    coaching: AnalysisService,
    play: PlayFlow,
    subsystems: dict[str, SubsystemProbe],
) -> AssistantServer | None:
    """Assemble the assistant API, or nothing at all.

    Returns None when it is switched off, which is the default, and also when it
    is switched on but configured in a way that would be unsafe. Both refusals
    are total: no port is opened, no façade is constructed, and CaddieJ carries
    on exactly as it does without this milestone.

    Refusing rather than warning is the right shape here because the two
    mistakes it catches — no token, or a bind address that is not loopback —
    both turn a private local surface into something the network can reach.
    Elsewhere CaddieJ degrades; here it declines.
    """

    if not settings.assistant_api_enabled:
        return None

    token = settings.assistant_api_token.get_secret_value().strip()
    if not token:
        logger.error("assistant_api_refused_without_a_token")
        return None
    if not is_loopback_bind(settings.assistant_api_host):
        logger.error(
            "assistant_api_refused_non_loopback_bind",
            extra={"assistant_host": settings.assistant_api_host},
        )
        return None

    reads = ReadFacade(
        history=history,
        coaching=coaching,
        play=play,
        version=app_version(),
        subsystems=subsystems,
        player_name=settings.player_name,
        include_coaching_prose=settings.assistant_api_include_coaching_prose,
        mutation_enabled=settings.assistant_api_allow_mutations,
    )
    # Not constructed at all unless mutation is allowed, so a read-only CaddieJ
    # holds no object capable of changing anything.
    commands = (
        CommandFacade(coaching) if settings.assistant_api_allow_mutations else None
    )
    return AssistantServer(
        create_api_app(reads, Guard(token=token), commands),
        host=settings.assistant_api_host,
        port=settings.assistant_api_port,
    )


async def _start_assistant_api(
    settings: Settings,
    supervisor: TaskSupervisor,
    server: AssistantServer,
) -> asyncio.Task[None]:
    """Start listening, and survive not being able to.

    The task is supervised so that shutdown cancels it like everything else, but
    it is wrapped first: a supervised task that raises brings CaddieJ down, and
    this is the one subsystem for which that would be the wrong answer. The task
    is returned so shutdown can let it finish rather than cancel it mid-way.
    """

    task = supervisor.start(_serve_assistant_api(server), name="assistant-api")
    try:
        await server.wait_until_serving(settings.assistant_api_startup_timeout)
    except Exception:
        logger.error("assistant_api_failed_to_start", exc_info=True)
        return task
    logger.info(
        "assistant_api_listening",
        extra={
            "assistant_host": settings.assistant_api_host,
            "assistant_port": server.port,
            "assistant_url": api_url(settings.assistant_api_host, server.port),
            "assistant_mutations": settings.assistant_api_allow_mutations,
        },
    )
    return task


async def _serve_assistant_api(server: AssistantServer) -> None:
    """Serve until stopped, turning any failure into a log line and a return."""

    try:
        await server.serve()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.error("assistant_api_stopped", exc_info=True)


async def _stop_assistant_api(
    server: AssistantServer,
    task: asyncio.Task[None] | None,
    timeout: float = 2.0,
) -> None:
    """Let uvicorn finish before the supervisor cancels anything.

    Short, and shorter than the HUD's budget, because there is nothing here
    worth draining: a request is a question about golf that has already been
    answered or is about to be. What the wait buys is a quiet shutdown —
    cancelling uvicorn mid-way leaves an exception traceback in the log of every
    clean exit, which is how a log stops being worth reading.
    """

    await server.stop()
    if task is None:
        return
    try:
        await asyncio.wait_for(task, timeout)
    except Exception:
        logger.debug("assistant_api_stopped_untidily", exc_info=True)


def _coaching_focus(name: str) -> CoachingFocus | None:
    """What the player is working on, if they named something recognisable."""

    if not name:
        return None
    try:
        return CoachingFocus(name.strip().lower())
    except ValueError:
        logger.warning("coaching_focus_unknown", extra={"coaching_focus": name})
        return None


def _floor_calibration(settings: Settings) -> RectangularCalibration:
    return rectangular_calibration(settings.floor_width_m, settings.floor_depth_m)


def _build_presenter(settings: Settings, floor: FloorService) -> FloorPresenter:
    return FloorPresenter(
        floor,
        tee=PhysicalPoint(x_m=settings.floor_tee_x_m, y_m=settings.floor_tee_y_m),
        club_path_length=settings.floor_club_path_length,
        calibration=_floor_calibration(settings),
    )


def _disabled_gspro_health() -> GSProHealth:
    return GSProHealth(
        link_state=LinkState.DISABLED,
        shots_ingested=0,
        messages_rejected=0,
        duplicates_ignored=0,
        reused_shot_numbers=0,
        late_data_discarded=0,
        frames_dropped=0,
    )


class _GSProHealth:
    """Holds the adapter so `/healthz` can ask it without owning its lifetime.

    The adapter is the source. When there is none — `GSPRO_MODE=off`, or
    simulate before the fake device has built one — the snapshot is the
    disabled zero-state, not a second set of counters.
    """

    def __init__(self) -> None:
        self.adapter: GSProConnectAdapter | None = None

    def attach(self, adapter: GSProConnectAdapter) -> None:
        self.adapter = adapter

    def snapshot(self) -> dict[str, object]:
        health = (
            self.adapter.health()
            if self.adapter is not None
            else _disabled_gspro_health()
        )
        payload = asdict(health)
        payload["link_state"] = health.link_state.value
        return payload

    def subsystem(self) -> SubsystemPayload:
        adapter = self.adapter
        if adapter is None:
            return SubsystemPayload(enabled=False, available=False)
        state = adapter.link_state
        return SubsystemPayload(
            enabled=True,
            available=state not in {LinkState.DISABLED, LinkState.BIND_FAILED},
        )


def _start_gspro(
    settings: Settings,
    supervisor: TaskSupervisor,
    event_bus: AsyncEventBus,
    gspro: _GSProHealth | None = None,
) -> asyncio.Task[None] | None:
    """Start whichever source of shots was asked for, if any.

    The task is returned as well as supervised, because shutdown has to be able
    to stop it first rather than let the supervisor sweep it up last. The
    adapter itself is built earlier so its health can be on `/healthz` before
    this starts listening.
    """

    if settings.gspro_mode == "interpose":
        adapter = gspro.adapter if gspro is not None else None
        if adapter is None:
            adapter = build_adapter(settings, event_bus.publish)
            if gspro is not None:
                gspro.attach(adapter)
        task = supervisor.start(adapter.serve(), name="gspro")
        logger.info(
            "gspro_interposing",
            extra={
                "gspro_bind": (
                    f"{settings.gspro_bind_host}:{settings.gspro_bind_port}"
                ),
                "gspro_upstream": (
                    f"{settings.gspro_upstream_host}:{settings.gspro_upstream_port}"
                ),
            },
        )
        return task

    if settings.gspro_mode == "simulate":
        on_adapter = gspro.attach if gspro is not None else None
        task = supervisor.start(
            run_simulated_round(settings, event_bus.publish, on_adapter=on_adapter),
            name="gspro-simulation",
        )
        logger.info("gspro_simulating")
        return task

    return None


async def _stop_server(
    server: HudServer,
    server_task: asyncio.Task[None],
    timeout: float,
) -> None:
    """Let uvicorn drain its connections before the supervisor cancels anything."""

    await server.stop()
    try:
        await asyncio.wait_for(server_task, timeout)
    except TimeoutError:
        logger.warning("hud_server_shutdown_timed_out")
    except Exception:
        # Already reported by the supervisor; recorded here so shutdown can finish.
        logger.debug("hud_server_stopped_with_error", exc_info=True)


async def _provision_floor_source(settings: Settings, url: str) -> None:
    """Point an OBS Browser Source at the overlay, on a best-effort basis.

    OBS only hosts the overlay; the overlay serves without it. So a missing,
    unreachable, or misconfigured OBS is reported and abandoned rather than
    allowed to stop the floor from coming online.

    The connection is short-lived: provision, then disconnect. Nothing else in
    this milestone needs OBS.
    """

    obs = ObsClient(
        host=settings.obs_host,
        port=settings.obs_port,
        password=settings.obs_password.get_secret_value(),
        connect_timeout=settings.obs_connect_timeout,
    )
    try:
        await obs.connect()
        version = await obs.get_version()
        logger.info(
            "obs_connection_verified",
            extra={
                "obs_version": version.obs_version,
                "obs_websocket_version": version.websocket_version,
                "obs_rpc_version": version.rpc_version,
            },
        )
        created = await obs.ensure_browser_source(
            settings.obs_floor_scene,
            settings.obs_floor_source,
            url=url,
            width=settings.obs_floor_width,
            height=settings.obs_floor_height,
        )
        logger.info(
            "obs_floor_source_ready",
            extra={
                "obs_scene": settings.obs_floor_scene,
                "obs_source": settings.obs_floor_source,
                "obs_created": created,
                "hud_url": url,
            },
        )
    except ObsConnectionError:
        logger.error(
            "obs_floor_source_provisioning_failed",
            extra={
                "obs_scene": settings.obs_floor_scene,
                "obs_source": settings.obs_floor_source,
            },
            exc_info=True,
        )
    finally:
        await obs.disconnect()
