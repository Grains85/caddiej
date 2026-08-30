"""Assembling the GSPro adapter from configuration.

Kept apart from the adapter so that tests construct it with fakes directly and
never have to satisfy configuration to exercise behaviour.
"""

from functools import partial

from caddiej.adapters.gspro.adapter import GSProConnectAdapter, Publish
from caddiej.adapters.gspro.interposer import (
    FrameObserver,
    Interposer,
    LinkObserver,
    TcpMonitorListener,
    connect_to_simulator,
)
from caddiej.config import Settings
from caddiej.runtime.backoff import Backoff


def build_adapter(
    settings: Settings,
    publish: Publish,
    listener: TcpMonitorListener | None = None,
) -> GSProConnectAdapter:
    """Build the adapter that listens for a launch monitor and bridges to GSPro.

    A listener may be supplied so a caller that needs to know which port was
    actually bound — the simulated round, which then connects to it — can keep
    hold of it.
    """

    listener = listener or TcpMonitorListener(
        settings.gspro_bind_host, settings.gspro_bind_port
    )

    def make_interposer(on_frame: FrameObserver, on_link: LinkObserver) -> Interposer:
        return Interposer(
            connect_simulator=partial(
                connect_to_simulator,
                settings.gspro_upstream_host,
                settings.gspro_upstream_port,
                settings.gspro_connect_timeout,
            ),
            on_frame=on_frame,
            on_link=on_link,
            max_message_bytes=settings.gspro_max_message_bytes,
            backoff=Backoff(settings.gspro_backoff_initial, settings.gspro_backoff_max),
            idle_timeout=settings.gspro_idle_timeout,
        )

    return GSProConnectAdapter(
        listener=listener,
        interposer_factory=make_interposer,
        publish=publish,
        assembly_window_seconds=settings.gspro_shot_assembly_window,
        ball_present_debounce_seconds=settings.gspro_ball_present_debounce,
        ball_absent_debounce_seconds=settings.gspro_ball_absent_debounce,
        # Only worth capturing if there is somewhere to keep it.
        raw_payload_max_bytes=(
            settings.history_raw_payload_max_bytes
            if settings.history_enabled and settings.history_record_raw_payloads
            else 0
        ),
    )
