import logging

import httpx
from django.conf import settings
from django.tasks import task

logger = logging.getLogger("apps.realtime")


@task
def publish_to_centrifugo(channel: str, data: dict) -> bool:
    config = settings.CENTRIFUGO
    try:
        response = httpx.post(
            f"{config['API_URL'].rstrip('/')}/api/publish",
            json={"channel": channel, "data": data},
            headers={"X-API-Key": config["API_KEY"]},
            timeout=2.0,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("error"):
            logger.warning("Centrifugo rejected publish to %s: %s", channel, body["error"])
            return False
        return True
    except httpx.HTTPError as exc:
        # Clients poll as a fallback, so a missed publish only delays an update.
        logger.warning("Centrifugo publish to %s failed: %s", channel, exc)
        return False
