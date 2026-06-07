# Claude Code — Frigate Timelapse Project

## Project goal

Build a self-hosted web application that generates timelapse videos from Frigate NVR
recordings. The app runs as a Docker container on the HA host, works **directly with
Frigate's recording files on disk** (no Frigate API auth required), uses ffmpeg to
concatenate and speed-up MP4 segments, and serves a browser UI with a visual timeline
scrubber for selecting camera / time range.

**No credentials are stored anywhere in this app.** All access is via bind-mounted
filesystem paths — the app reads files Frigate has already written.

Long-term aspiration: package this as a proper Home Assistant add-on.

---

## Prior art research (done — do not re-investigate)

- **Frigate's built-in export UI** removed timelapse in v0.14; API-only since then,
  and Frigate's API requires authentication which we are deliberately avoiding.
- **kornyhiv/frigatenvr-timelapse-addon** — proof-of-concept, no community adoption,
  incompatible with HA OS, no timeline scrubber. Not worth forking.
- **MrSleeps/FrigateTimelapser** — polls live snapshots, not historical recordings.

**Decision: build from scratch using direct filesystem access + ffmpeg.**

---

## Environment

### Host

- HA OS (supervised), version 2026.5.3
- Host: `ha.flypig.net`
- SSH: `ssh -i ~/.ssh/id_ha root@ha.flypig.net`
- Alpine-based shell — **no python3, no docker CLI, no openssl** on the host shell
- Use `ha apps` (not `ha addons` — deprecated)

### Frigate media layout on host

```
/media/frigate/
├── recordings/         # source — read-only for this app
│   └── {YYYY-MM-DD}/   # date at the TOP level
│       └── {HH}/       # hour (00–23)
│           └── {camera}/
│               └── {MM}.{SS}.mp4   # segment starting at minute:second
├── exports/            # Frigate's own exports (not used by this app)
├── clips/              # event clips
└── snapshots/          # per-event snapshots (not used directly)
```

Example: `/media/frigate/recordings/2026-06-07/10/garage/08.30.mp4`
= garage camera, June 7 2026, recording starting at 10:08:30 local time.

**Note: date is the top-level key, camera is third-level.** This is the opposite of
what might be assumed — do not get this wrong or the path parser will be broken.

The segment files are standard H.264 MP4s. Each is typically 10–60 seconds long.

**Deriving the camera list:** cameras are NOT listed at the top of the tree. To get
the camera list, find the most recent date directory, pick any hour subdir that exists,
and list its contents. Example logic:
```python
latest_date = sorted(os.listdir(RECORDINGS_PATH))[-1]
first_hour = sorted(os.listdir(f"{RECORDINGS_PATH}/{latest_date}"))[0]
cameras = sorted(os.listdir(f"{RECORDINGS_PATH}/{latest_date}/{first_hour}"))
```

### Frigate details (for reference only — API not used for core feature)

- Internal URL: `http://ccab4aaf-frigate-fa:5000` (auth enabled — avoid using)
- Version: 0.17.1-416a9b7
- Add-on slug: `ccab4aaf_frigate-fa`

### Cameras (8 total — directory names under recordings/)

| Directory name | Physical camera |
|----------------|----------------|
| `garage` | Axis M1054 (via Scrypted) |
| `greatroom` | Axis (via Scrypted) |
| `doorbell` | ONVIF doorbell (via Scrypted) |
| `frontdoor` | (via Scrypted) |
| `pooldeck` | VIVOTEK FD8134 (via Scrypted) |
| `poolspa` | (via Scrypted) |
| `backyardeastreo` | Reolink Duo 3 POE — east lens |
| `backyardwestreo` | Reolink Duo 3 POE — west lens |

Always derive the camera list dynamically from the filesystem — the table above is
for reference only.

---

## Application architecture

### Core approach — filesystem + ffmpeg

1. **Discover cameras** — list subdirectories of `RECORDINGS_PATH`
2. **Discover available footage** — walk the date/hour/minute directory tree for a
   given camera and time range to find which segment files exist
3. **Extract thumbnail frames** — use ffmpeg to pull a single JPEG from a segment
   file at a given offset; used for the timeline scrubber and start/end preview
4. **Render timelapse** — concatenate the relevant segments with ffmpeg and apply a
   speed-up filter (`setpts`, `atempo` or just drop audio); save to output directory
5. **Serve download** — stream the finished MP4 to the browser

No Frigate API calls needed for any of these steps.

### Backend — Python / FastAPI

- **FastAPI** with async background tasks for rendering
- Runs on port **8088** inside the container
- Two environment variables (set via docker-compose, never hardcoded):
  - `RECORDINGS_PATH` — bind-mount path to `/media/frigate/recordings/` (default: `/recordings`)
  - `OUTPUT_PATH` — where finished timelapses are written (default: `/output`)

Key routes:

| Route | Purpose |
|-------|---------|
| `GET /cameras` | List subdirs of `RECORDINGS_PATH` |
| `GET /coverage?camera=X&date=YYYY-MM-DD` | Return hour/minute slots that have footage for the given camera+date; used to grey out unavailable times in the UI |
| `GET /thumbnail?camera=X&ts=<unix>` | Extract a JPEG frame from the segment containing `ts`; returns image/jpeg |
| `POST /timelapse` | Start render job; body: `{camera, start_ts, end_ts, speed, name}`; returns `{job_id}` |
| `GET /timelapse/{job_id}` | Poll job status: `{status, progress, output_file}` |
| `GET /timelapse/{job_id}/download` | Stream finished MP4 to browser |

### ffmpeg pipeline

**Thumbnail extraction** (single frame from a segment file):
```bash
ffmpeg -ss {offset} -i {segment.mp4} -frames:v 1 -q:v 2 {out.jpg}
```

**Timelapse render** (concatenate segments then speed up):
```bash
# Step 1: write a concat list
echo "file '/recordings/camera/...mp4'" > /tmp/concat.txt
...

# Step 2: concatenate + speed up in one pass
ffmpeg -f concat -safe 0 -i /tmp/concat.txt \
  -vf "setpts={1/speed}*PTS" \
  -an \
  -c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p \
  {output.mp4}
```

Speed is a user-controlled multiplier (e.g. 10× = `setpts=0.1*PTS`). Default: 10×.
Drop audio (`-an`) — timelapse audio is meaningless.

### Segment file → timestamp mapping

Segment path encodes its start time:
```
recordings/{YYYY-MM-DD}/{HH}/{camera}/{MM}.{SS}.mp4
```

Parse into `datetime(year, month, day, hour=HH, minute=MM, second=SS)` in local time
(`America/Los_Angeles`). Each segment is approximately 10–60 seconds. Use this to:
- Map a unix timestamp → which segment file contains it (find the segment whose
  start time is ≤ target and whose next segment's start time is > target)
- Calculate the `-ss` offset within that file for thumbnail extraction

### Frontend — single HTML file

- Vanilla JS + CSS; no build step, no npm
- Served by FastAPI as a static file
- UI flow:
  1. Camera dropdown (from `/cameras`)
  2. Date picker — grey out dates with no footage
  3. Timeline strip: a row of thumbnail images across the selected day, one per
     available hour-block; clicking/dragging sets start and end handles
  4. Start/end preview frames update as handles move
  5. Speed multiplier selector (5×, 10×, 25×, 60×)
  6. Output filename (auto-generated, editable)
  7. "Build Timelapse" → progress bar polling `/timelapse/{job_id}`
  8. On completion: "Download" button

### Docker

- Base image: `python:3.12-slim` + ffmpeg via apt (`ffmpeg` package)
- Bind mounts (read-only for recordings, read-write for output):
  ```yaml
  volumes:
    - /media/frigate/recordings:/recordings:ro
    - /media/frigate/timelapses:/output:rw
  ```
- Output directory `/media/frigate/timelapses/` sits alongside Frigate's own
  `exports/` and is visible in HA's Media Browser automatically
- No network dependency on Frigate container — no shared Docker network needed
- No authentication — LAN-only

---

## File layout

```
frigate-timelapse/
├── CLAUDE.md
├── README.md
├── Dockerfile
├── docker-compose.yml
├── app/
│   ├── main.py          # FastAPI app + all routes
│   ├── recordings.py    # filesystem walking, segment→timestamp mapping
│   ├── render.py        # ffmpeg job queue + timelapse/thumbnail logic
│   └── static/
│       └── index.html   # entire UI in one file
└── deploy.sh            # SSH deploy helper
```

---

## Output

- Timelapses saved to `/media/frigate/timelapses/{camera}_{date}_{time}.mp4`
- `/media/frigate/` is exposed in HA's Media Browser — the `timelapses/` subdir
  appears there automatically alongside `clips/`, `exports/`, etc.
- Also directly downloadable from the app UI

---

## Dev / test workflow

### Local development

The app needs access to the recording files. Use SSHFS to mount them locally, or
(simpler) use a small sample of real segment files copied from the host for initial
development, and test the full flow once deployed.

**Recommended local dev approach:**

```bash
# Copy a few hours of recordings for local testing (date/hour/camera structure)
scp -i ~/.ssh/id_ha -r \
  "root@ha.flypig.net:/media/frigate/recordings/2026-06-07/08" \
  ./test-data/recordings/2026-06-07/

# Point the app at local test data
RECORDINGS_PATH=./test-data/recordings OUTPUT_PATH=./test-data/output \
  uvicorn app.main:app --reload --port 8088
```

This tests all functionality except rendering a full multi-hour timelapse — but that
is just a scale difference, not a code difference.

**What works locally with test data:**
- Camera list
- Coverage map / timeline scrubber
- Thumbnail extraction
- Short timelapse renders
- Download

**What to test on the deployed version:**
- Full multi-hour renders
- Correct segment discovery across day/hour boundaries
- Download of large files

### Deploying to the HA host

```bash
# Copy compose file to host and restart
scp -i ~/.ssh/id_ha docker-compose.yml root@ha.flypig.net:/root/frigate-timelapse/
ssh -i ~/.ssh/id_ha root@ha.flypig.net \
  'cd /root/frigate-timelapse && docker compose up -d --pull always'
```

App available at `http://ha.flypig.net:8088`.

First deploy: create the output directory on the host first:
```bash
ssh -i ~/.ssh/id_ha root@ha.flypig.net 'mkdir -p /media/frigate/timelapses'
```

---

## HA add-on path (future)

- Add-on config in `ha-addon/config.yaml`
- Needs `media: rw` in `map:` to read recordings and write timelapses
- Does NOT need to join Frigate's Docker network (no API dependency)
- `RECORDINGS_PATH=/media/frigate/recordings`, `OUTPUT_PATH=/media/frigate/timelapses`

References:
- https://github.com/hassio-addons/addon-example
- https://developers.home-assistant.io/docs/add-ons/configuration

---

## Notes / gotchas

- **Segment duration varies** — segments are 10–60 s, not a fixed size; the filename
  gives the start time only. To find the end time of a segment, look at the start time
  of the next segment in the same directory, or probe with ffprobe if needed
- **Segment filename format is `{MM}.{SS}.mp4`** (minute dot second, both zero-padded)
  — parse carefully, e.g. `08.30.mp4` = minute 08, second 30, NOT 8.3 seconds
- **Timezone** — the path encodes local time (`America/Los_Angeles`); be consistent
  about tz-aware vs naive datetimes throughout
- **Large time ranges** — a 24-hour timelapse at 10× is still 8.6 minutes of video
  and ~1440 segments to concatenate; warn users above ~6 hours, cap at 24 hours
- **ffmpeg concat with many files** — write a concat list file rather than passing
  all paths as arguments; avoids shell argument length limits
- **Output directory must exist** before ffmpeg writes to it — create on startup
- **Job cleanup** — keep finished job metadata in memory (dict); optionally delete
  temp concat list files after render completes
- **HA OS host shell has no python3** — all deploy scripting runs locally and is
  pushed via SSH
