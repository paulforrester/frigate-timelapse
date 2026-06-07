# Claude Code — Frigate Timelapse Project

## Project goal

Build a self-hosted Home Assistant add-on that generates timelapse videos from Frigate NVR
recordings. The app works **directly with Frigate's recording files on disk** (no Frigate
API auth required), uses ffmpeg to concatenate and speed-up MP4 segments, and serves a
browser UI with a visual timeline scrubber for selecting camera / time range.

**No credentials are stored anywhere in this app.** All access is via bind-mounted
filesystem paths — the app reads files Frigate has already written.

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

- HA OS (supervised)
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
= garage camera, June 7 2026, recording starting at 10:08:30 UTC.

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
- Runs on port **8088** inside the container (configurable via `port` option)
- Configuration via `/data/options.json` (HA Supervisor) with fallback to `config.json`

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

Segment path encodes its start time in **UTC**:
```
recordings/{YYYY-MM-DD}/{HH}/{camera}/{MM}.{SS}.mp4
```

Parse into `datetime(year, month, day, hour=HH, minute=MM, second=SS, tzinfo=timezone.utc)`.
Each segment is approximately 10–60 seconds. Use this to:
- Map a unix timestamp → which segment file contains it (find the segment whose
  start time is ≤ target and whose next segment's start time is > target)
- Calculate the `-ss` offset within that file for thumbnail extraction

### Timezone rules (critical)

- **File paths are always UTC** — Frigate stores recordings using UTC directory names.
  Never convert paths. Parse with `tzinfo=timezone.utc`.
- **Display and watermarks always use the configured camera timezone** (from the
  `timezone` option) via `ZoneInfo`. Never use the system/server timezone.
- **Never call `datetime.now()` without explicit `tz=`** — always pass an explicit
  `ZoneInfo` object.

### Frontend — single HTML file

- Vanilla JS + CSS; no build step, no npm
- Served by FastAPI as a static file
- UI flow:
  1. Camera dropdown (from `/cameras`)
  2. Date picker — grey out dates with no footage
  3. Timeline strip: a row of thumbnail images across the selected day, one per
     available hour-block; clicking/dragging sets start and end handles
  4. Start/end preview frames update as handles move
  5. Speed multiplier selector (5×, 10×, 25×, 60×, 120×, 300×, 600×, 900×, 1200×)
  6. Output filename (auto-generated, editable)
  7. "Build Timelapse" → progress bar polling `/timelapse/{job_id}`
  8. On completion: "Download" button

### HA add-on structure

```
frigate-timelapse/          # add-on subfolder
├── config.yaml             # add-on metadata and options schema
├── build.yaml              # multi-arch build targets
├── Dockerfile              # image build instructions
├── run.sh                  # container entrypoint
├── config.json             # local dev defaults (mirrors config.yaml options)
├── requirements.txt
├── DOCS.md                 # user-facing documentation
└── app/
    ├── main.py             # FastAPI app + all routes
    ├── config.py           # config loading (HA options → config.json → defaults)
    ├── recordings.py       # filesystem walking, segment→timestamp mapping
    ├── render.py           # ffmpeg job queue + timelapse/thumbnail logic
    └── static/
        └── index.html      # entire UI in one file
repository.json             # repo root — required for HA custom repository
```

---

## Output

- Timelapses saved to `/media/frigate/timelapses/{name}.mp4`
- `/media/frigate/` is exposed in HA's Media Browser — the `timelapses/` subdir
  appears there automatically alongside `clips/`, `exports/`, etc.
- Also directly downloadable from the app UI
- Files are automatically deleted after `timelapse_retention_days` (default: 7 days)

---

## Dev / test workflow

### Local development

```bash
# Copy a few hours of recordings for local testing (date/hour/camera structure)
scp -r "root@<ha-host>:/media/frigate/recordings/2026-06-07/08" \
  ./test-data/recordings/2026-06-07/

# Point the app at local test data
RECORDINGS_PATH=./test-data/recordings OUTPUT_PATH=./test-data/output \
  uvicorn app.main:app --reload --port 8088
```

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

### Installing on HA

Push the repo to GitHub (must be public), then add the repo URL as a custom repository
in the HA add-on store. HA builds the image locally — no registry push needed.

---

## Notes / gotchas

- **Segment duration varies** — segments are 10–60 s, not a fixed size; the filename
  gives the start time only. To find the end time of a segment, look at the start time
  of the next segment in the same directory, or probe with ffprobe if needed
- **Segment filename format is `{MM}.{SS}.mp4`** (minute dot second, both zero-padded)
  — parse carefully, e.g. `08.30.mp4` = minute 08, second 30, NOT 8.3 seconds
- **Timezone** — paths encode UTC; display uses the configured camera timezone
- **Large time ranges** — a 24-hour timelapse at 10× is still 8.6 minutes of video
  and ~1440 segments to concatenate; warn users above ~6 hours, cap at 24 hours
- **ffmpeg concat with many files** — write a concat list file rather than passing
  all paths as arguments; avoids shell argument length limits
- **Output directory must exist** before ffmpeg writes to it — create on startup
- **Job cleanup** — keep finished job metadata in memory (dict); optionally delete
  temp concat list files after render completes
- **HA OS host shell has no python3** — all deploy scripting runs locally
- **All HA add-ons share the `hassio` Docker network** — Frigate reachable at
  `http://<frigate-slug>:5000` without extra network config in config.yaml
