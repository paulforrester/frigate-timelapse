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
    "verbose_logging": False,
    "output_crf": 28,
    "output_max_height": 1080,
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

# When True, index build logs progress at 30-second intervals with camera/date/hour detail.
VERBOSE_LOGGING: bool = bool(_cfg.get("verbose_logging", False))

# libx264 CRF value for timelapse output (18=lossless-ish, 28=good, 35=small/lower quality).
OUTPUT_CRF: int = int(_cfg.get("output_crf", 28))

# Maximum output height in pixels; source is scaled down if taller. 0 = no limit.
OUTPUT_MAX_HEIGHT: int = int(_cfg.get("output_max_height", 1080))
