# Frigate Timelapse — Add-on Documentation

Generates timelapse videos directly from Frigate NVR recording files on disk.
No Frigate API authentication required.

## How it works

The add-on reads the segment files Frigate writes to `/media/frigate/recordings/`
and uses ffmpeg to concatenate and speed them up.  Finished timelapses are saved
to `/media/frigate/timelapses/`, which appears automatically in HA's Media Browser
alongside Frigate's own clips and exports.

## Configuration options

| Option | Default | Description |
|--------|---------|-------------|
| `timezone` | `America/Los_Angeles` | IANA timezone of the camera location, used for watermarks and display |
| `port` | `8088` | Host port for direct browser access (ingress is also available) |
| `timelapse_retention_days` | `7` | Days to keep finished timelapse files before automatic deletion |
| `verbose_logging` | `false` | Log index build/scan progress at 30-second intervals including camera, date, and hour |

## Access

- **Sidebar (ingress):** click *Timelapse* in the HA sidebar after enabling the add-on
- **Direct:** `http://<ha-host>:8088`

## Frigate network access

All HA add-ons share the internal `hassio` Docker network.  The Frigate add-on is
reachable inside the container at `http://ccab4aaf-frigate-fa:5000` without any
additional network configuration.  This add-on currently uses direct file access
only; the Frigate API client (`app/frigate.py`) is wired up for future use.

## Media paths (fixed)

| Path in container | HA host path |
|-------------------|--------------|
| `/media/frigate/recordings` | `/media/frigate/recordings` (read) |
| `/media/frigate/timelapses` | `/media/frigate/timelapses` (write) |
