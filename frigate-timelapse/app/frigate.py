import os
from typing import Any

import httpx

FRIGATE_URL = os.environ.get("FRIGATE_URL", "http://ccab4aaf-frigate-fa:5000")

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(base_url=FRIGATE_URL, timeout=30.0)
    return _client


async def close() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
    _client = None


async def get_cameras() -> list[str]:
    r = await _get_client().get("/api/config")
    r.raise_for_status()
    config = r.json()
    return sorted(config.get("cameras", {}).keys())


async def get_recordings(camera: str, after: float, before: float) -> list[dict]:
    r = await _get_client().get(
        f"/api/{camera}/recordings",
        params={"after": after, "before": before},
    )
    r.raise_for_status()
    return r.json()


async def create_export(
    camera: str, start_ts: int, end_ts: int, name: str
) -> dict[str, Any]:
    r = await _get_client().post(
        f"/api/export/{camera}/start/{start_ts}/end/{end_ts}",
        json={"playback": "timelapse_25x", "name": name},
    )
    r.raise_for_status()
    return r.json()


async def get_exports() -> list[dict]:
    r = await _get_client().get("/api/exports")
    r.raise_for_status()
    return r.json()


async def get_frame_bytes(camera: str, ts: float) -> tuple[bytes, str]:
    """Return raw JPEG bytes and content-type for a frame at the given timestamp."""
    r = await _get_client().get(f"/api/{camera}/recordings/{ts}/snapshot.jpg")
    r.raise_for_status()
    return r.content, r.headers.get("content-type", "image/jpeg")
