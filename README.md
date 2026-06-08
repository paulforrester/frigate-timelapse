# Frigate Timelapse — HA Add-on

A Home Assistant add-on that generates timelapse videos directly from
[Frigate NVR](https://frigate.video) recording files on disk.

No Frigate API credentials required — the add-on reads the segment files
Frigate has already written to `/media/frigate/recordings/`.

---

## Features

- **Visual timeline scrubber** — browse thumbnails across any day; drag handles to
  set start/end points; double-click an hour to snap to a 1-hour selection
- **Direct time entry** — HH:MM inputs below the timeline stay in sync with the handles
  for precise range selection
- **Speed control** — 5×, 10×, 25×, 60×, 120×, 300×, 600×, 900×, or 1200×
- **Encode quality** — Quality (smaller file), Balanced (default), or Speed (fastest
  render) — chosen per timelapse in the UI
- **Output size controls** — configurable CRF and maximum output height; defaults to
  CRF 28 scaled to ≤1080p, keeping files reasonable without visible quality loss
- **Parallel rendering** — encodes across all available CPU cores for faster renders
- **Watermark** — optional camera name + timestamp burned into the video in your
  recording timezone
- **HA Media Browser** — finished timelapses land in `/media/frigate/timelapses/`
  and appear automatically alongside Frigate's own clips and exports
- **Ingress** — available as a sidebar panel or via direct port access

---

## Installation

This is a custom add-on repository. To install:

1. In HA, go to **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
2. Add this repo's URL: `https://github.com/paulforrester/frigate-timelapse`
3. Find **Frigate Timelapse** in the store and install it
4. Configure options (see below) and start the add-on

---

## Configuration

| Option | Default | Description |
|--------|---------|-------------|
| `timezone` | `America/Los_Angeles` | IANA timezone of the camera location — used for watermarks and the date picker |
| `port` | `8088` | Host port for direct browser access |
| `timelapse_retention_days` | `7` | Days to keep finished timelapse files before automatic deletion |
| `verbose_logging` | `false` | Log index build/scan progress at 30-second intervals |
| `output_crf` | `28` | libx264 quality (18 = near-lossless, 28 = good, 35 = small). Lower = larger file |
| `output_max_height` | `1080` | Scale down output if source is taller than this. Set to `0` to disable |

---

## How it works

Frigate writes H.264 MP4 segments to:
```
/media/frigate/recordings/{YYYY-MM-DD}/{HH}/{camera}/{MM}.{SS}.mp4
```

On startup the add-on indexes the entire recordings tree and caches it in memory
(persisted to `/data/` so subsequent restarts are fast). The UI loads immediately
from the cache; the index updates in the background and live as new segments arrive.

When you click **Build Timelapse**, the add-on:
1. Finds all segments for the selected camera and time range
2. Splits the segment list across CPU cores and encodes each chunk in parallel
3. Stream-copies the encoded chunks into a single output MP4
4. Saves to `/media/frigate/timelapses/` and makes it available for download

---

## Accessing the UI

- **Sidebar:** click *Timelapse* after enabling ingress in the add-on settings
- **Direct:** `http://<ha-host>:8088`

---

## Requirements

- Home Assistant OS or Supervised
- Frigate NVR add-on with recordings enabled
- Recordings stored at the standard path (`/media/frigate/recordings/`)
