"""
Server configuration loaded from config.json at startup.

The file is looked up relative to the working directory, or at the path
given by the CONFIG_PATH environment variable.  Missing keys fall back to
the defaults defined here.
"""

import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

_DEFAULTS: dict = {
    "camera_timezone": "America/Los_Angeles",
}


def _load() -> dict:
    path = Path(os.environ.get("CONFIG_PATH", "config.json"))
    if path.exists():
        with path.open() as f:
            return {**_DEFAULTS, **json.load(f)}
    return dict(_DEFAULTS)


_cfg = _load()

# The IANA timezone name for the physical camera location.
# Used for watermark display and all LA↔UTC conversions.
CAMERA_TZ: ZoneInfo = ZoneInfo(_cfg["camera_timezone"])
