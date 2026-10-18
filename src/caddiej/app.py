"""CaddieJ application lifecycle."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from uuid import uuid4

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
    GSProRoundFileAdapter,
    GSProRoundObserverHealth,
    RoundObserverStatus,
    build_adapter,
    run_simulated_round,
)
from caddiej.adapters.hud import ConnectionRegistry, HudServer, create_app, overlay_url
from caddiej.adapters.hud.evidence import EvidenceFrames
from caddiej.adapters.obs import ObsClient, ObsConnectionError
from caddiej.adapters.protee import (
    ProTeeLogAdapter,
    ProTeeObserverHealth,
    ProTeeObserverStatus,
)
from caddiej.assistant.commands import CommandFacade
from caddiej.assistant.contract import SubsystemPayload
from caddiej.assistant.reads import ReadFacade, SubsystemProbe, subsystem_probe
from caddiej.coaching.factory import build_provider
from caddiej.config import Settings
from caddiej.core import AsyncEventBus
from caddiej.core.events import Event
from caddiej.diagnostics.journal import DiagnosticJournal
from caddiej.domain import (
    ClubDistanceProfile,
    ClubDistanceProvider,
    PlayFlow,
    StaticClubDistances,
)
from caddiej.domain.event_policy import delivery_for, queue_size_for
from caddiej.domain.events import LinkState
from caddiej.effects import EffectDirector, build_effect_director, obs_clients
from caddiej.floor import (
    FloorLayout,
    FloorPresenter,
    FloorService,
    PhysicalOffset,
    PhysicalRect,
    PlacementMode,
    RectangularCalibration,
    rectangular_calibration,
    run_demo,
)
from caddiej.lighting import LightingService, build_lighting_service
from caddiej.obs_reveal import RevealFeature, TimedObsSource
from caddiej.persistence import HistoryService, build_history_service
from caddiej.persistence.service import app_version
from caddiej.runtime import TaskSupervisor, shutdown_signals
from caddiej.speech import SpeechService, build_speech_service
from caddiej.swing_capture import (
    SwingCaptureService,
    build_swing_capture_service,
)
from caddiej.swing_evidence.ready import start_evidence_warmup
from caddiej.swing_reference.live import ReferenceLiveCoach
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
    diagnostics = DiagnosticJournal(
        resolved.diagnostics_dir,
        session_id=str(uuid4()),
        enabled=resolved.diagnostics_enabled,
        keep_sessions=resolved.diagnostics_keep_sessions,
        keep_shots=resolved.diagnostics_keep_shots,
        retain_payloads=resolved.diagnostics_retain_payloads,
        payload_max_bytes=resolved.diagnostics_payload_max_bytes,
        auto_export=resolved.diagnostics_auto_export_latest,
        include_images=resolved.diagnostics_include_images,
    )

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
        shot_result_min_duration=resolved.floor_shot_result_min_duration,
        shot_result_max_duration=resolved.floor_shot_result_max_duration,
        club_distances=_build_club_distances(resolved),
        progress_max_seconds=_progress_ceiling(resolved),
        diagnostics=diagnostics,
    )
    play.subscribe(event_bus)

    # Built here so `/healthz` can ask it before anything is listening. `serve()`
    # is not called until the lifecycle starts the ingestion task.
    gspro = _GSProHealth()
    if resolved.gspro_mode == "interpose":
        gspro.attach(build_adapter(resolved, event_bus.publish))

    round_observer = _RoundObserverHealth()
    protee_observer = _ProTeeObserverHealth()
    if resolved.gspro_mode == "observe":
        _attach_observers(resolved, event_bus.publish, round_observer, protee_observer)

    history = _build_history(resolved)
    # Subscribing is registration and nothing else; no event reaches a handler
    # until the bus starts, which happens after `open` inside the try below.
    history.subscribe(event_bus)

    video = _build_video(resolved, event_bus.publish)
    # Before coaching, because it is one of the two things that can put a
    # picture of a swing in front of a model and coaching has to be told which
    # of them exist.
    swing_capture = _build_swing_capture(
        resolved, event_bus.publish, diagnostics=diagnostics
    )
    coaching = _build_coaching(
        resolved,
        event_bus.publish,
        history,
        video,
        swing_capture,
        diagnostics=diagnostics,
    )
    speech = _build_speech(resolved, event_bus.publish, diagnostics=diagnostics)
    lighting = _build_lighting(resolved)
    effects = _build_effects(resolved, floor, speech, lighting)
    shot_video = _build_reveal(
        resolved,
        RevealFeature.SHOT_VIDEO,
        source=resolved.obs_shot_video_source,
        enabled=resolved.floor_shot_video_enabled,
        delay_seconds=resolved.floor_shot_video_delay_seconds,
        duration_seconds=resolved.floor_shot_video_duration_seconds,
    )
    top_impact = _build_reveal(
        resolved,
        RevealFeature.TOP_IMPACT,
        source=resolved.obs_top_impact_source,
        enabled=resolved.floor_top_impact_enabled,
        delay_seconds=resolved.floor_top_impact_delay_seconds,
        duration_seconds=resolved.floor_top_impact_duration_seconds,
    )

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
                "swing_capture": lambda: asdict(swing_capture.health()),
                "event_bus": lambda: asdict(event_bus.health()),
                "gspro": gspro.snapshot,
                "gspro_round_observer": round_observer.snapshot,
                "protee_observer": protee_observer.snapshot,
                "shot_video": lambda: shot_video.health().as_dict(),
                "top_impact": lambda: top_impact.health().as_dict(),
            },
            evidence=EvidenceFrames(swing_capture.evidence_root).resolve,
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
                enabled=resolved.coaching_enabled and resolved.coaching_vision_calls,
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
        # First, and inside the try. This used to sit above it, next to the
        # construction, which left the SQLite thread and its file handle alive
        # with nothing to close them if any later `_build_*` raised — and one
        # of them validates an OBS source name, a camera view and an output
        # directory. Opening still cannot fail loudly: history that will not
        # start disables itself so a locked file never stops someone hitting
        # balls.
        await history.open()
        if history.session_id is not None:
            diagnostics.bind_session(str(history.session_id))

        await event_bus.start()
        logger.info("event_bus_started")

        # After the bus, because a camera reports what it is doing through it.
        # Opening cannot fail loudly either: a missing camera or a missing FFmpeg
        # leaves video switched off and changes nothing else.
        await video.open()
        video.subscribe(event_bus)

        # After video. Opening cannot fail loudly: a missing key leaves the
        # coach unable to call a model, and evidence warmup runs beside it
        # without holding startup. A warmup failure is logged and does not
        # stop the round.
        await coaching.open()
        if resolved.coaching_enabled and resolved.coaching_vision_calls:
            start_evidence_warmup()
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
        # Same OBS client factory as effects: visibility of an existing source,
        # nothing more. Off by default, and a failed hide never stops golf.
        shot_video.subscribe(event_bus)
        top_impact.subscribe(event_bus)
        # And the same again for reading stills back out of two existing
        # sources. Off by default; an OBS that cannot answer costs the stills
        # of one swing and nothing else.
        swing_capture.subscribe(event_bus)

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
                # Before the HUD is listening and before OBS is spoken to.
                # Everything passive observation needs is already true — the
                # bus is running and every subscriber is registered — and
                # everything below this line is a way for it not to happen:
                # `wait_until_serving` raises on a port somebody else holds,
                # and provisioning waits on an OBS round trip. Neither has
                # anything to do with reading two files, and a session spent
                # unobserved because the overlay could not bind is a session
                # of shots that no longer exist. Observation is the one thing
                # here with nothing above it.
                ingestion = _start_gspro(
                    resolved,
                    supervisor,
                    event_bus,
                    gspro,
                    round_observer=round_observer,
                    protee_observer=protee_observer,
                )

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

                # Before shots can arrive: OBS may have left Shot Video visible
                # from the last session, and that must not sit on the floor.
                # Not awaited, and not supervised: it reaches OBS, and neither a
                # slow answer nor a failure may delay the floor or the observers
                # below it. See `caddiej.obs_reveal`.
                shot_video.start()
                top_impact.start()
                # Says what it is configured to do and sweeps any staging
                # directory a previous run left behind. No OBS connection is
                # opened here either: the first shot of the session opens one.
                swing_capture.start()

                # The demo and the domain both write the floor, so exactly one of
                # them owns it. The demo wins only when nothing real can arrive.
                if resolved.floor_demo and resolved.gspro_mode == "off":
                    supervisor.start(
                        run_demo(
                            floor,
                            resolved.floor_demo_step_seconds,
                            calibration=_floor_calibration(resolved),
                            scale=resolved.ball_status_chamfer_scale,
                        ),
                        name="floor-demo",
                    )
                else:
                    if resolved.floor_demo:
                        logger.warning("floor_demo_ignored_while_gspro_enabled")
                    play.start()

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
                            shot_video=shot_video,
                            top_impact=top_impact,
                            swing_capture=swing_capture,
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
        diagnostics.close()
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
    shot_video: TimedObsSource,
    top_impact: TimedObsSource,
    swing_capture: SwingCaptureService,
    effects: EffectDirector,
    speech: SpeechService,
    lighting: LightingService,
    coaching: ReferenceLiveCoach,
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
    # After play, because the floor HUD should keep updating until it has
    # stopped; before effects, because this is the same kind of OBS visibility
    # as a celebration and should be off the projector first. Best effort: a
    # missing OBS does not hold shutdown.
    await shot_video.aclose()
    # Beside it, and separately, because that is the whole arrangement: an OBS
    # that has stopped answering Shot Video must not also cost Top Impact its
    # hide, and each has its own budget.
    await top_impact.aclose()
    # Beside it, and for the same reason: this is the other subsystem holding
    # an OBS connection on behalf of a shot. A capture in flight is cancelled
    # rather than waited for — the swing it was taking pictures of is the last
    # one of the session, and nobody is going to look at it.
    await swing_capture.aclose()
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
        clip_retention_days=settings.video_clip_retention_days,
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
    swing_capture: SwingCaptureService,
    diagnostics: DiagnosticJournal | None = None,
) -> ReferenceLiveCoach:
    """The one live coach: Ideal Swing 2.9 on the captured DTL and FO stills.

    Vision calls stay off until `COACHING_VISION_CALLS` is set. History, video
    and swing capture stay in the signature because the application still owns
    them; this coach reads the capture the swing service already published.
    """

    del history, video, swing_capture
    return ReferenceLiveCoach(
        publish,
        build_provider(
            settings.coaching_provider,
            model=settings.coaching_model,
            api_key=settings.coaching_api_key.get_secret_value(),
            api_base=settings.coaching_api_base,
        ),
        enabled=settings.coaching_enabled,
        vision_calls=settings.coaching_vision_calls,
        image_detail=settings.coaching_image_detail,
        feedback=settings.coaching_feedback,
        evidence_budget=settings.coaching_evidence_budget,
        evidence_allocation=settings.coaching_evidence_allocation,
        timeout_seconds=settings.coach_openai_timeout,
        call_limit=settings.coaching_call_limit,
        diagnostics=diagnostics,
    )


def _build_speech(
    settings: Settings,
    publish: Publish,
    diagnostics: DiagnosticJournal | None = None,
) -> SpeechService:
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
        gain=settings.speech_gain,
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
        diagnostics=diagnostics,
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


def _build_reveal(
    settings: Settings,
    feature: RevealFeature,
    *,
    source: str,
    enabled: bool,
    delay_seconds: float,
    duration_seconds: float,
) -> TimedObsSource:
    """Toggle an existing OBS capture of one ProTee view.

    The scene is the floor scene already configured; CaddieJ never switches
    away from it, and never creates, moves, crops or reorders the source.

    Each feature gets its own instance and its own connection — the client
    factory hands out a new one per call — so a bay with one of the two views
    configured and not the other is normal and one failing cannot cost the
    other.
    """

    return TimedObsSource(
        obs_clients(
            host=settings.obs_host,
            port=settings.obs_port,
            password=settings.obs_password.get_secret_value(),
            connect_timeout=settings.obs_connect_timeout,
        ),
        settings.obs_floor_scene,
        source,
        feature=feature,
        enabled=enabled,
        delay_seconds=delay_seconds,
        duration_seconds=duration_seconds,
        command_timeout=settings.obs_connect_timeout,
        shutdown_timeout=settings.shutdown_timeout,
        capture_scene=(
            settings.swing_capture_scene if settings.swing_capture_enabled else None
        ),
    )


def _progress_ceiling(settings: Settings) -> float:
    """How long a coaching stage may stay on the floor with no word after it.

    The capture window plus the vision budget, plus a margin: a job still
    running past that has already been abandoned, and its bar would otherwise
    outlive it.
    """

    wall = max(
        settings.swing_capture_initial_delay_seconds
        + settings.swing_capture_duration_seconds,
        settings.swing_capture_static_delay_seconds
        + settings.swing_capture_static_duration_seconds,
    )
    return wall + settings.coach_openai_budget + 5.0


def _build_swing_capture(
    settings: Settings,
    publish: Publish,
    diagnostics: DiagnosticJournal | None = None,
) -> SwingCaptureService:
    """Assemble swing capture, pointed at the configured OBS sources.

    The third user of the same `ObsClient` factory. A hidden window capture is
    shown for the length of the burst so OBS has a frame to photograph, then
    its previous visibility is restored. The scene collection is not rewritten.
    """

    return build_swing_capture_service(
        obs_clients(
            host=settings.obs_host,
            port=settings.obs_port,
            password=settings.obs_password.get_secret_value(),
            connect_timeout=settings.obs_connect_timeout,
        ),
        publish,
        enabled=settings.swing_capture_enabled,
        panels=settings.capture_panels(),
        output_dir=settings.swing_capture_output_dir,
        initial_delay_seconds=settings.swing_capture_initial_delay_seconds,
        duration_seconds=settings.swing_capture_duration_seconds,
        fps=settings.swing_capture_fps,
        downswing_fps=settings.swing_capture_downswing_fps,
        downswing_delay_seconds=settings.swing_capture_downswing_delay_seconds,
        downswing_duration_seconds=settings.swing_capture_downswing_duration_seconds,
        selected_frames=settings.swing_capture_selected_frames,
        image_width=settings.swing_capture_image_width,
        static_delay_seconds=settings.swing_capture_static_delay_seconds,
        static_duration_seconds=settings.swing_capture_static_duration_seconds,
        static_fps=settings.swing_capture_static_fps,
        debug_keep_shots=settings.swing_capture_debug_keep_shots,
        evidence_keep=settings.swing_capture_evidence_keep,
        metrics_source=settings.coach_metrics_source_name(),
        metrics_delay_seconds=settings.coach_metrics_capture_delay_seconds,
        capture_scene=settings.swing_capture_scene,
        command_timeout=settings.obs_connect_timeout,
        shutdown_timeout=settings.shutdown_timeout,
        announce_progress=settings.coaching_enabled and settings.coaching_vision_calls,
        diagnostic_timeline_shots=settings.swing_capture_diagnostic_timeline_shots,
        evidence_timeline=(
            settings.coaching_enabled and settings.coaching_vision_calls
        ),
        diagnostics=diagnostics,
    )


def _build_assistant_api(
    settings: Settings,
    history: HistoryService,
    coaching: ReferenceLiveCoach,
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


def _floor_calibration(settings: Settings) -> RectangularCalibration:
    return rectangular_calibration(settings.floor_width_m, settings.floor_depth_m)


def _build_club_distances(settings: Settings) -> ClubDistanceProvider:
    """The configured bag, as the one thing the domain is allowed to ask.

    A provider rather than the profile itself, so that replacing configuration
    with carries learned from shot history is a different object constructed
    here and no change at all to `PlayFlow` or the floor.
    """

    profile = ClubDistanceProfile.from_carries(settings.club_carries())
    logger.info(
        "club_distances_configured",
        extra={"club_carry_count": len(profile.carries)},
    )
    return StaticClubDistances(carries=profile)


def _build_presenter(settings: Settings, floor: FloorService) -> FloorPresenter:
    """The strip, and everything golf-related placed from it.

    The offsets reaching here have already absorbed any absolute `FLOOR_TEE_*_M`
    an older `.env` still sets, so there is one coordinate model from this line
    down: an anchor somebody measured, and offsets that survive moving it.
    """

    strip = PhysicalRect(
        x_m=settings.floor_hitting_strip_x_m,
        y_m=settings.floor_hitting_strip_y_m,
        width_m=settings.floor_hitting_strip_width_m,
        depth_m=settings.floor_hitting_strip_depth_m,
    )
    layout = FloorLayout(
        strip=strip,
        ball={
            PlacementMode.IRON: PhysicalOffset(
                dx_m=settings.floor_ball_iron_dx_m,
                dy_m=settings.floor_ball_iron_dy_m,
            ),
            PlacementMode.DRIVER: PhysicalOffset(
                dx_m=settings.floor_ball_driver_dx_m,
                dy_m=settings.floor_ball_driver_dy_m,
            ),
            PlacementMode.PUTTER: PhysicalOffset(
                dx_m=settings.floor_ball_putter_dx_m,
                dy_m=settings.floor_ball_putter_dy_m,
            ),
        },
        information=PhysicalOffset(
            dx_m=settings.floor_information_dx_m,
            dy_m=settings.floor_information_dy_m,
        ),
        strip_filled=settings.floor_hitting_strip_fill,
        putting_lane=settings.floor_putting_lane,
        putting_lane_length_m=settings.floor_putting_lane_length_m,
        putting_lane_width_m=settings.floor_putting_lane_width_m,
    )
    # The anchor and the three ball positions in one line, because "the marker
    # is in the wrong place" is answered by comparing these to a tape measure
    # and nothing else in the log carries them.
    iron = layout.ball_position(PlacementMode.IRON)
    driver = layout.ball_position(PlacementMode.DRIVER)
    putter = layout.ball_position(PlacementMode.PUTTER)
    logger.info(
        "floor_layout_configured",
        extra={
            "floor_anchor_x_m": strip.x_m,
            "floor_anchor_y_m": strip.y_m,
            "floor_ball_iron_x_m": iron.x_m,
            "floor_ball_iron_y_m": iron.y_m,
            "floor_ball_driver_x_m": driver.x_m,
            "floor_ball_driver_y_m": driver.y_m,
            "floor_ball_putter_x_m": putter.x_m,
            "floor_ball_putter_y_m": putter.y_m,
        },
    )
    return FloorPresenter(
        floor,
        layout,
        club_path_length=settings.floor_club_path_length,
        calibration=_floor_calibration(settings),
        ball_status_scale=settings.ball_status_chamfer_scale,
    )


def _disabled_round_observer_health() -> GSProRoundObserverHealth:
    return GSProRoundObserverHealth(
        status=RoundObserverStatus.DISABLED,
        file=None,
        last_change_at=None,
        last_shot_id=None,
        shots_observed=0,
        parse_failures=0,
        last_read_at=None,
        read_failures=0,
        shots_deferred=0,
    )


def _disabled_protee_observer_health() -> ProTeeObserverHealth:
    return ProTeeObserverHealth(
        status=ProTeeObserverStatus.DISABLED,
        current_log=None,
        last_event_at=None,
        last_read_at=None,
        read_failures=0,
        events_dropped=0,
    )


class _RoundObserverHealth:
    def __init__(self) -> None:
        self.adapter: GSProRoundFileAdapter | None = None

    def attach(self, adapter: GSProRoundFileAdapter) -> None:
        self.adapter = adapter

    def snapshot(self) -> dict[str, object]:
        health = (
            self.adapter.health()
            if self.adapter is not None
            else _disabled_round_observer_health()
        )
        payload = asdict(health)
        payload["status"] = health.status.value
        return payload


class _ProTeeObserverHealth:
    def __init__(self) -> None:
        self.adapter: ProTeeLogAdapter | None = None

    def attach(self, adapter: ProTeeLogAdapter) -> None:
        self.adapter = adapter

    def snapshot(self) -> dict[str, object]:
        health = (
            self.adapter.health()
            if self.adapter is not None
            else _disabled_protee_observer_health()
        )
        payload = asdict(health)
        payload["status"] = health.status.value
        return payload


def _attach_observers(
    settings: Settings,
    publish: Publish,
    round_observer: _RoundObserverHealth,
    protee_observer: _ProTeeObserverHealth,
) -> None:
    if settings.gspro_round_observer_enabled:
        round_observer.attach(
            GSProRoundFileAdapter(
                settings.gspro_round_file_path,
                publish,
                poll_interval=settings.gspro_round_poll_interval,
            )
        )
    if settings.protee_observer_enabled:
        protee_observer.attach(
            ProTeeLogAdapter(
                settings.protee_root_path,
                publish,
                poll_interval=settings.protee_log_poll_interval,
            )
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
    round_observer: _RoundObserverHealth | None = None,
    protee_observer: _ProTeeObserverHealth | None = None,
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

    if settings.gspro_mode == "observe":
        adapters: list[GSProRoundFileAdapter | ProTeeLogAdapter] = []
        if round_observer is not None and round_observer.adapter is not None:
            adapters.append(round_observer.adapter)
        if protee_observer is not None and protee_observer.adapter is not None:
            adapters.append(protee_observer.adapter)
        if not adapters:
            logger.info("gspro_observe_idle")
            return None
        task = supervisor.start(_run_observers(adapters), name="gspro-observe")
        logger.info("gspro_observing")
        return task

    return None


async def _run_observers(
    adapters: list[GSProRoundFileAdapter | ProTeeLogAdapter],
) -> None:
    """Run every passive observer until cancelled.

    One supervised task so shutdown still stops ingestion first. A missing
    file is not a failure; each adapter waits.

    Each observer is contained. `serve()` already catches what a poll can
    raise, but anything above it — a root path that stops resolving, a clock
    that throws — used to escape a bare `gather`, fail this supervised task,
    and take the whole process down. `gather` does not cancel its siblings
    when one raises, so the other observer carried on as an untracked orphan
    while CaddieJ shut down around it. Passive observation is the one thing
    here that has to outlive every other subsystem.
    """

    await asyncio.gather(*(_run_observer(adapter) for adapter in adapters))


async def _run_observer(adapter: GSProRoundFileAdapter | ProTeeLogAdapter) -> None:
    try:
        await adapter.serve()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "observer_stopped",
            extra={
                "observer_name": type(adapter).__name__,
                "observer_status": "stopped",
            },
        )


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
