"""Command-line entry point for CaddieJ."""

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from caddiej.adapters.openai.provider import IMAGE_DETAIL
from caddiej.app import run
from caddiej.cli import (
    build_parser,
    describe,
    explain,
    load_settings,
    resolve_log_level,
)
from caddiej.coaching.benchmark import (
    benchmark_coach,
    coach_request_report,
    configurations,
    openai_factory,
)
from caddiej.coaching.factory import build_provider
from caddiej.coaching.probe import probe_coach, probe_coach_cues, probe_protee_coach
from caddiej.config import Settings
from caddiej.obs_reveal import RevealFeature, probe_obs_source
from caddiej.runtime.logging import JsonFormatter as _JsonFormatter
from caddiej.runtime.logging import LoggingRuntime, configure_logging, shutdown_logging
from caddiej.speech import probe_speech
from caddiej.swing_capture import probe_swing_capture
from caddiej.swing_capture.store import load_capture, newest_capture
from caddiej.video.diagnose import probe_video_frames

# Existing tests import the formatter from this module.
__all__ = ["_JsonFormatter", "main"]


def _configure_logging(level: str = "INFO") -> LoggingRuntime:
    return configure_logging(level)


def _survive_a_legacy_console() -> None:
    """Never fail to say something because of one character.

    A Windows console — or a redirected stream on Windows — is often cp1252,
    which cannot encode the arrows and dashes in CaddieJ's reports. The default
    is to raise, which turns `--check-config` into a traceback at exactly the
    moment somebody is trying to find out why nothing works. Substituting a
    question mark for a character is a better answer than no report at all.
    """

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (OSError, ValueError):
            # A stream that will not be reconfigured is one we write to as it is.
            pass


def _coach_benchmark(options: argparse.Namespace, settings: Settings) -> None:
    """`--check-coach-request` and `--benchmark-coach`, from one set of defaults.

    Every default is what production would send today, so either command run
    bare describes or replays the live request and changes nothing about it.
    """

    models = options.benchmark_models or (settings.coaching_model,)
    details = options.benchmark_detail or (IMAGE_DETAIL,)
    common: dict[str, Any] = {
        "evidence_dir": options.benchmark_evidence,
        "latest_dir": Path(settings.swing_capture_output_dir),
        "max_images": settings.coaching_max_images,
        "send_frames": settings.coaching_send_frames,
        "visual_cues": settings.coaching_visual_cues,
    }
    if options.benchmark_coach:
        efforts = (
            tuple(
                None if effort == "omit" else effort
                for effort in options.benchmark_efforts
            )
            if options.benchmark_efforts
            else (settings.coaching_reasoning_effort or None,)
        )
        result = asyncio.run(
            benchmark_coach(
                configs=configurations(
                    models=models,
                    efforts=efforts,
                    details=details,
                    max_output_tokens=(
                        options.benchmark_max_output_tokens
                        or settings.coaching_protee_max_output_tokens
                    ),
                    timeout_seconds=(
                        options.benchmark_timeout or settings.coach_openai_timeout
                    ),
                ),
                repeats=options.benchmark_repeats,
                make_provider=openai_factory(
                    api_key=settings.coaching_api_key.get_secret_value(),
                    base_url=settings.coaching_api_base or None,
                ),
                **common,
            )
        )
    else:
        result = asyncio.run(
            coach_request_report(models=models, details=details, **common)
        )
    print(result.report, file=sys.stdout if result.ok else sys.stderr)
    if not result.ok:
        raise SystemExit(1)


def main(argv: Sequence[str] | None = None) -> None:
    """Run CaddieJ from either the module or console entry point.

    Configuration is read here rather than inside the application, so that a
    mistyped setting is a sentence on stderr and an exit code, instead of a
    traceback wrapped in a log line that nobody can read at the moment they most
    need to.
    """

    options = build_parser().parse_args(argv)
    _survive_a_legacy_console()
    runtime = _configure_logging(resolve_log_level(options.log_level))
    logger = logging.getLogger(__name__)

    try:
        try:
            settings = load_settings()
        except ValidationError as error:
            print(explain(error), file=sys.stderr)
            raise SystemExit(2) from None

        if options.check_config:
            print(describe(settings))
            return

        if options.check_obs_shot_video:
            probe = asyncio.run(
                probe_obs_source(
                    feature=RevealFeature.SHOT_VIDEO,
                    host=settings.obs_host,
                    port=settings.obs_port,
                    password=settings.obs_password.get_secret_value(),
                    connect_timeout=settings.obs_connect_timeout,
                    scene=settings.obs_floor_scene,
                    source=settings.obs_shot_video_source,
                )
            )
            print(probe.report, file=sys.stdout if probe.ok else sys.stderr)
            if not probe.ok:
                raise SystemExit(1)
            return

        if options.check_obs_top_impact:
            probe = asyncio.run(
                probe_obs_source(
                    feature=RevealFeature.TOP_IMPACT,
                    host=settings.obs_host,
                    port=settings.obs_port,
                    password=settings.obs_password.get_secret_value(),
                    connect_timeout=settings.obs_connect_timeout,
                    scene=settings.obs_floor_scene,
                    source=settings.obs_top_impact_source,
                )
            )
            print(probe.report, file=sys.stdout if probe.ok else sys.stderr)
            if not probe.ok:
                raise SystemExit(1)
            return

        if options.check_coach_cues:
            cues = probe_coach_cues()
            print(cues.report, file=sys.stdout if cues.ok else sys.stderr)
            if not cues.ok:
                raise SystemExit(1)
            return

        if options.check_coach:
            provider = build_provider(
                settings.coaching_provider,
                model=settings.coaching_model,
                api_key=settings.coaching_api_key.get_secret_value(),
                api_base=settings.coaching_api_base,
            )
            trigger = settings.resolved_coaching_trigger()
            if trigger == "protee":
                coach = asyncio.run(
                    probe_protee_coach(
                        provider_name=settings.coaching_provider,
                        provider=provider,
                        capture=(
                            newest_capture(settings.swing_capture_output_dir)
                            if options.benchmark_evidence is None
                            else load_capture(options.benchmark_evidence)
                        ),
                        send_frames=settings.coaching_send_frames,
                        visual_cues=settings.coaching_visual_cues,
                        max_images=settings.coaching_max_images,
                        timeout_seconds=settings.coach_openai_timeout,
                        budget_seconds=settings.coach_openai_budget,
                        max_output_tokens=settings.coaching_protee_max_output_tokens,
                        reasoning_effort=settings.coaching_reasoning_effort or None,
                    )
                )
            else:
                coach = asyncio.run(
                    probe_coach(
                        provider_name=settings.coaching_provider,
                        provider=provider,
                        send_frames=settings.coaching_send_frames,
                        visual_cues=settings.coaching_visual_cues,
                        max_images=settings.coaching_max_images,
                    )
                )
            panels = settings.capture_panels()
            key_state = (
                "present" if settings.coaching_api_key.get_secret_value() else "absent"
            )
            panel_names = (
                ", ".join(f"{panel.panel_id}={panel.source}" for panel in panels)
                or "(none)"
            )
            header = "\n".join(
                (
                    "Effective Coach configuration",
                    f"  coaching_enabled     {settings.coaching_enabled}",
                    f"  trigger              {trigger} "
                    f"(COACHING_TRIGGER={settings.coaching_trigger})",
                    f"  protee_only          {'yes' if trigger == 'protee' else 'no'}",
                    f"  gspro_required       {'no' if trigger == 'protee' else 'yes'}",
                    "  metrics_source       "
                    + (
                        settings.coach_metrics_source_name()
                        or "(off: COACH_METRICS_CAPTURE_ENABLED=false)"
                    ),
                    "  metrics_delay        "
                    f"{settings.coach_metrics_capture_delay_seconds}s after the strike",
                    f"  max_images           {settings.coaching_max_images} swing "
                    f"+ {1 if settings.coach_metrics_source_name() else 0} All Values",
                    (
                        "  timeout_seconds      "
                        f"{settings.coach_openai_timeout} per attempt, "
                        f"{settings.coach_openai_budget} per shot "
                        "(COACH_OPENAI_TIMEOUT_SECONDS, COACH_OPENAI_BUDGET_SECONDS)"
                        if trigger == "protee"
                        else "  timeout_seconds      "
                        f"{settings.coaching_timeout} per attempt, "
                        f"{settings.coaching_budget_seconds} per shot"
                    ),
                    f"  provider             {settings.coaching_provider}",
                    f"  model                {settings.coaching_model}",
                    f"  api_key              {key_state}",
                    f"  api_base             {settings.coaching_api_base or '(default)'}",
                    f"  send_frames          {settings.coaching_send_frames}",
                    f"  visual_cues          {settings.coaching_visual_cues}",
                    f"  floor_cue_image      {settings.floor_coach_cue_image}",
                    f"  clip_wait_seconds    {settings.coaching_clip_wait}",
                    f"  severity_fine        {settings.coaching_severity_fine}",
                    f"  speech_enabled       {settings.speech_enabled}",
                    f"  speech_engine        {settings.speech_engine}",
                    f"  speech_output        {settings.speech_output}",
                    f"  speech_voice         {settings.speech_voice}",
                    f"  swing_capture        {settings.swing_capture_enabled}",
                    f"  capture_panels       {panel_names}",
                    "",
                )
            )
            print(header + coach.report, file=sys.stdout if coach.ok else sys.stderr)
            if not coach.ok:
                raise SystemExit(1)
            return

        if options.check_coach_request or options.benchmark_coach:
            _coach_benchmark(options, settings)
            return

        if options.check_swing_capture:
            swing = asyncio.run(
                probe_swing_capture(
                    host=settings.obs_host,
                    port=settings.obs_port,
                    password=settings.obs_password.get_secret_value(),
                    connect_timeout=settings.obs_connect_timeout,
                    panels=settings.capture_panels(),
                    absent=settings.absent_capture_roles(),
                    image_width=settings.swing_capture_image_width,
                    metrics_source=settings.coach_metrics_source_name(),
                    capture_scene=settings.swing_capture_scene,
                )
            )
            env_file = Path.cwd() / ".env"
            origin = (
                str(env_file.resolve())
                if env_file.is_file()
                else "environment only (no .env file in the working directory)"
            )
            print(
                f"Settings read from   {origin}\n{swing.report}",
                file=sys.stdout if swing.ok else sys.stderr,
            )
            if not swing.ok:
                raise SystemExit(1)
            return

        if options.check_speech:
            try:
                speech = asyncio.run(
                    probe_speech(
                        enabled=settings.speech_enabled,
                        engine=settings.speech_engine,
                        output=settings.speech_output,
                        voice=settings.speech_voice,
                        speed=settings.speech_speed,
                        device=settings.speech_device,
                        gain=settings.speech_gain,
                        open_timeout_seconds=settings.speech_open_timeout,
                        render_timeout_seconds=settings.speech_render_timeout,
                    )
                )
            except KeyboardInterrupt:
                logger.info("shutdown_requested")
                raise SystemExit(130) from None
            print(speech.report, file=sys.stdout if speech.ok else sys.stderr)
            if not speech.ok:
                raise SystemExit(1)
            return

        if options.check_video_frames:
            try:
                video = asyncio.run(
                    probe_video_frames(
                        media_root=settings.video_media_root,
                        camera_view=settings.video_camera_view,
                        ffmpeg_path=settings.video_ffmpeg_path,
                        frame_width=settings.coaching_frame_width,
                    )
                )
            except KeyboardInterrupt:
                logger.info("shutdown_requested")
                raise SystemExit(130) from None
            print(video.report, file=sys.stdout if video.ok else sys.stderr)
            if not video.ok:
                raise SystemExit(1)
            return

        try:
            asyncio.run(run(settings))
        except KeyboardInterrupt:
            logger.info("shutdown_requested")
            raise SystemExit(130) from None
        except Exception:
            logger.exception("application_failed")
            raise SystemExit(1) from None
    finally:
        shutdown_logging(runtime)


if __name__ == "__main__":
    main()
