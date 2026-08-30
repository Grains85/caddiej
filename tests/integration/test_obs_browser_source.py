"""Opt-in provisioning of the floor Browser Source in a real OBS instance.

Unlike the other OBS integration tests, this one intentionally leaves the source
behind: creating it is the point, and provisioning is idempotent. The scene named
by OBS_FLOOR_SCENE must already exist.
"""

import os

import pytest

from caddiej.adapters.hud import overlay_url
from caddiej.adapters.obs import ObsClient
from caddiej.config import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("CADDIEJ_RUN_INTEGRATION") != "1",
        reason="set CADDIEJ_RUN_INTEGRATION=1 to test against a real OBS instance",
    ),
]


async def _client(settings: Settings) -> ObsClient:
    return ObsClient(
        host=settings.obs_host,
        port=settings.obs_port,
        password=settings.obs_password.get_secret_value(),
        connect_timeout=settings.obs_connect_timeout,
    )


async def test_browser_source_is_provisioned_idempotently() -> None:
    settings = Settings()
    url = overlay_url(settings.hud_host, settings.hud_port)
    client = await _client(settings)

    try:
        await client.connect()

        # First call creates the source or reconciles an existing one; the second
        # must always take the reconcile path.
        await client.ensure_browser_source(
            settings.obs_floor_scene,
            settings.obs_floor_source,
            url=url,
            width=settings.obs_floor_width,
            height=settings.obs_floor_height,
        )
        created_again = await client.ensure_browser_source(
            settings.obs_floor_scene,
            settings.obs_floor_source,
            url=url,
            width=settings.obs_floor_width,
            height=settings.obs_floor_height,
        )

        assert created_again is False

        # Prove OBS really knows the source by resolving it as a scene item.
        await client.set_scene_item_visibility(
            settings.obs_floor_scene,
            settings.obs_floor_source,
            visible=True,
        )
    finally:
        await client.disconnect()
