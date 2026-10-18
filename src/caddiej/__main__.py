"""Command-line entry point for CaddieJ."""

import asyncio
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from caddiej.app import run
from caddiej.cli import (
    build_parser,
    describe,
    explain,
    load_settings,
    resolve_log_level,
)
from caddiej.obs_reveal import RevealFeature, probe_obs_source
from caddiej.runtime.logging import JsonFormatter as _JsonFormatter
from caddiej.runtime.logging import LoggingRuntime, configure_logging, shutdown_logging
from caddiej.speech import probe_speech
from caddiej.swing_capture import probe_swing_capture
from caddiej.swing_reference.command import run_reference_analysis
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

        if options.export_diagnostics:
            from caddiej.diagnostics.export import export_session

            try:
                exported = export_session(
                    settings.diagnostics_dir,
                    options.diagnostics_output,
                    session_id=options.diagnostics_session,
                    include_images=options.diagnostics_include_images,
                )
            except FileNotFoundError as error:
                print(str(error), file=sys.stderr)
                raise SystemExit(1) from None
            print(exported)
            return

        if options.analyze_reference_swing:
            if options.reference_package is None or options.reference_shot_dir is None:
                print(
                    "--analyze-reference-swing needs --reference-package and "
                    "--reference-shot-dir"
                )
                raise SystemExit(2)
            raise SystemExit(run_reference_analysis(options, settings))

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
