#!/usr/bin/env sh
set -e

# Read port from HA options (/data/options.json), fall back to 8088.
PORT=$(python3 -c "
import json, sys
try:
    with open('/data/options.json') as f:
        print(json.load(f).get('port', 8088))
except Exception:
    print(8088)
")

# Ensure timelapse output directory exists.
mkdir -p "${OUTPUT_PATH:-/media/frigate/timelapses}"

exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT}"
