"""Assembling the director from configuration and the services already running.

One switch per channel, in one file, so that adding a fifth output is a case here
and a module beside `channels.py`. The director never learns which ones it got —
only that some of them are available.

The order the channels go in is the order they are begun, and it is chosen: the
floor first, because it is the only channel that cannot fail and the only one
whose latency is a function call rather than a network. If OBS is slow to answer,
the word is already on the projector.
"""

import logging

from caddiej.adapters.obs import ObsClient
from caddiej.effects.channel import EffectChannel
from caddiej.effects.channels import FloorChannel, LightsChannel, SpeechChannel
from caddiej.effects.director import EffectDirector
from caddiej.effects.obs import ClientFactory, ObsChannel
from caddiej.floor.service import FloorService
from caddiej.lighting.service import LightingService
from caddiej.speech.service import SpeechService

logger = logging.getLogger(__name__)


def build_effect_director(
    floor: FloorService,
    speech: SpeechService,
    lighting: LightingService,
    enabled: bool = True,
    use_floor: bool = True,
    use_speech: bool = True,
    use_lights: bool = True,
    use_obs: bool = False,
    obs_client_factory: ClientFactory | None = None,
    obs_scene: str = "",
    obs_source: str = "",
    obs_text_source: str = "",
    max_seconds: float = 8.0,
    channel_timeout_seconds: float = 3.0,
) -> EffectDirector:
    """Wire up whichever outputs are switched on.

    Every channel is constructed whether or not it is switched on, and reports
    its own availability. That is deliberate: a channel that is off still gets
    reset, which is what makes turning one off mid-session safe, and it means
    `/healthz` can distinguish "no OBS configured" from "OBS configured and
    unreachable" without a second flag.
    """

    channels: list[EffectChannel] = [
        FloorChannel(floor, enabled=use_floor),
        SpeechChannel(speech, enabled=use_speech),
        LightsChannel(lighting, enabled=use_lights),
    ]

    if use_obs and obs_client_factory is not None:
        channels.append(
            ObsChannel(
                obs_client_factory,
                scene=obs_scene,
                source=obs_source,
                text_source=obs_text_source,
                enabled=True,
            )
        )
    elif use_obs:
        logger.warning("effects_obs_unconfigured")

    return EffectDirector(
        channels=channels,
        enabled=enabled,
        max_seconds=max_seconds,
        channel_timeout_seconds=channel_timeout_seconds,
    )


def obs_clients(
    host: str, port: int, password: str, connect_timeout: float
) -> ClientFactory:
    """A callable that makes an OBS client, for the effects channel to keep.

    Curried here rather than in the application so the channel takes a plain
    factory and nothing about OBS configuration reaches it. The channel throws a
    client away after a failure, which is why it needs to be able to make
    another.
    """

    def make() -> ObsClient:
        return ObsClient(
            host=host,
            port=port,
            password=password,
            connect_timeout=connect_timeout,
        )

    return make
