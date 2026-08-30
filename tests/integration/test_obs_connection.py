import os
from contextlib import suppress

import pytest

from caddiej.adapters.obs import ObsClient
from caddiej.config import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("CADDIEJ_RUN_INTEGRATION") != "1",
        reason="set CADDIEJ_RUN_INTEGRATION=1 to test against a real OBS instance",
    ),
]


async def test_real_obs_connection_reports_versions() -> None:
    settings = Settings()
    client = ObsClient(
        host=settings.obs_host,
        port=settings.obs_port,
        password=settings.obs_password.get_secret_value(),
        connect_timeout=settings.obs_connect_timeout,
    )

    try:
        await client.connect()
        version = await client.get_version()
    finally:
        await client.disconnect()

    assert version.obs_version != "unknown"
    assert version.websocket_version != "unknown"


async def test_real_obs_status_source_control() -> None:
    settings = Settings()
    client = ObsClient(
        host=settings.obs_host,
        port=settings.obs_port,
        password=settings.obs_password.get_secret_value(),
        connect_timeout=settings.obs_connect_timeout,
    )

    connected = False
    try:
        await client.connect()
        connected = True
        await client.set_text(
            settings.obs_test_status_source,
            "CADDIEJ INTEGRATION TEST",
        )
        await client.set_scene_item_visibility(
            settings.obs_test_scene,
            settings.obs_test_status_source,
            visible=False,
        )
        await client.set_scene_item_visibility(
            settings.obs_test_scene,
            settings.obs_test_status_source,
            visible=True,
        )
        await client.set_text(
            settings.obs_test_status_source,
            "CADDIEJ READY",
        )
    finally:
        if connected:
            with suppress(Exception):
                await client.set_scene_item_visibility(
                    settings.obs_test_scene,
                    settings.obs_test_status_source,
                    visible=True,
                )
        await client.disconnect()
