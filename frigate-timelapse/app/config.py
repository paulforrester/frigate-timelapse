"""
Server configuration.

Load order:
  1. /data/options.json  — HA add-on runtime (written by Supervisor from config.yaml options)
  2. config.json         — local dev fallback (relative to cwd, or CONFIG_PATH env var)
  3. built-in defaults

Key names match config.yaml schema: timezone, port, timelapse_retention_days.
"""

import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

_DEFAULTS: dict = {
    "timezone": "America/Los_Angeles",
    "port": 8088,
    "timelapse_retention_days": 7,
}


def _load() -> dict:
    # HA runtime options take priority.
    ha_options = Path("/data/options.json")
    if ha_options.exists():
        with ha_options.open() as f:
            return {**_DEFAULTS, **json.load(f)}

    # Local dev fallback.
    dev_config = Path(os.environ.get("CONFIG_PATH", "config.json"))
    if dev_config.exists():
        with dev_config.open() as f:
            return {**_DEFAULTS, **json.load(f)}

    return dict(_DEFAULTS)


_cfg = _load()

# The IANA timezone name for the physical camera location.
# Used for watermark display and all camera-local↔UTC conversions.
CAMERA_TZ: ZoneInfo = ZoneInfo(_cfg["timezone"])

# Seconds after a render completes before the output file is deleted.
TIMELAPSE_RETENTION_SECONDS: int = int(_cfg["timelapse_retention_days"]) * 86400
