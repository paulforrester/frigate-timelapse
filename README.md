# Frigate Timelapse — HA Add-on

A Home Assistant add-on that generates timelapse videos directly from
[Frigate NVR](https://frigate.video) recording files on disk.

No Frigate API credentials required — the add-on reads the segment files
Frigate has already written to `/media/frigate/recordings/`.

---

## Features

- **Visual timeline scrubber** — browse thumbnails across any day, drag handles to
  select start and end points
- **Speed control** — 5×, 10×, 25×, 60×, 120×, 300×, 600×, 900×, or 1200×
- **Watermark** — optional camera name + timestamp burned into the video in your
  local timezone
- **HA Media Browser** — finished timelapses land in `/media/frigate/timelapses/`
  and appear automatically alongside Frigate's own clips and exports
- **Ingress** — available as a sidebar panel or via direct port access

---

## Installation

This is a custom add-on repository. To install:

1. In HA, go to **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
2. Add this repo's URL
3. Find **Frigate Timelapse** in the store and install it
4. Configure options (see below) and start the add-on

---

## Configuration

| Option | Default | Description |
|--------|---------|-------------|
| `timezone` | `America/Los_Angeles` | IANA timezone of the camera location — used for watermarks and the date picker |
| `port` | `8088` | Host port for direct browser access |
| `timelapse_retention_days` | `7` | Days to keep finished timelapse files before automatic deletion |

---

## How it works

Frigate writes H.264 MP4 segments to:
```
/media/frigate/recordings/{YYYY-MM-DD}/{HH}/{camera}/{MM}.{SS}.mp4
```

The add-on walks this tree for the selected camera and time range, writes a concat
list, and runs ffmpeg to stitch the segments together and apply the speed multiplier.
Finished files are saved to `/media/frigate/timelapses/`.

---

## Accessing the UI

- **Sidebar:** click *Timelapse* after enabling ingress in the add-on settings
- **Direct:** `http://<ha-host>:8088`

---

## Requirements

- Home Assistant OS or Supervised
- Frigate NVR add-on with recordings enabled
- Recordings stored at the standard path (`/media/frigate/recordings/`)
