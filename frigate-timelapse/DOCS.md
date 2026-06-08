# Frigate Timelapse — Add-on Documentation

Generates timelapse videos directly from Frigate NVR recording files on disk.
No Frigate API authentication required.

## How it works

The add-on indexes the segment files Frigate writes to `/media/frigate/recordings/`
at startup and keeps the index in memory.  The UI loads immediately from the cached
index; new recordings are picked up live as Frigate writes them.

When you build a timelapse the add-on splits the segment list across CPU cores,
encodes each chunk in parallel with ffmpeg, then joins them into a single MP4.
Finished timelapses are saved to `/media/frigate/timelapses/`, which appears
automatically in HA's Media Browser alongside Frigate's own clips and exports.

## Building a timelapse

1. Select a camera and date — the timeline loads thumbnails for every available hour
2. Drag the handles to set your start/end range, **double-click an hour** to snap to
   a 1-hour selection, or type directly in the **Start / End time** fields
3. Choose a speed multiplier (10× is a good starting point; 300× for a full day)
4. Choose an encode quality — **Balanced** is the default; **Quality** gives a smaller
   file at the cost of slower encoding; **Speed** is fastest but produces larger files
5. Optionally enable the **Watermark** (camera name + recording-timezone timestamp)
6. Click **Build Timelapse** and watch the progress bar
7. Download the finished file or find it in the HA Media Browser under `timelapses/`

## Configuration options

| Option | Default | Description |
|--------|---------|-------------|
| `timezone` | `America/Los_Angeles` | IANA timezone of the camera location, used for watermarks and display |
| `port` | `8088` | Host port for direct browser access (ingress is also available) |
| `timelapse_retention_days` | `7` | Days to keep finished timelapse files before automatic deletion |
| `verbose_logging` | `false` | Log index build/scan progress at 30-second intervals including camera, date, and hour |
| `output_crf` | `28` | libx264 quality level (18 = near-lossless, 28 = good quality, 35 = small file). Lower = larger file |
| `output_max_height` | `1080` | Scale down output if source is taller than this many pixels. Set to `0` to disable scaling |

## Access

- **Sidebar (ingress):** click *Timelapse* in the HA sidebar after enabling the add-on
- **Direct:** `http://<ha-host>:8088`

## Media paths (fixed)

| Path in container | Purpose |
|-------------------|---------|
| `/media/frigate/recordings` | Source recordings (read) |
| `/media/frigate/timelapses` | Output timelapses (write) |
