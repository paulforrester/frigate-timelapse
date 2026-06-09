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
4. **Render timelapse** — split segments into parallel chunks (one per CPU core),
   encode each concurrently with ffmpeg (`setpts` + scale + optional watermark),
   then stream-copy join the chunks; see *ffmpeg pipeline* below
5. **Serve download** — stream the finished MP4 to the browser

No Frigate API calls needed for any of these steps.

### Backend — Python / FastAPI

- **FastAPI** with async background tasks for rendering
- Runs on port **8088** inside the container (configurable via `port` option)
- Configuration via `/data/options.json` (HA Supervisor) with fallback to `config.json`

Key routes:

| Route | Purpose |
|-------|---------|
| `GET /cameras` | List cameras (from startup coverage index) |
| `GET /status` | Index build progress: `{indexed, progress}` — UI polls until `indexed=true` |
| `GET /timezone?date=YYYY-MM-DD` | Camera-timezone UTC offset in seconds for that date (handles DST) |
| `GET /coverage?camera=X&date=YYYY-MM-DD` | Return `{hour: [minute,...]}` for available footage; served from in-memory index |
| `GET /thumbnail?camera=X&ts=<unix>` | Extract a JPEG frame from the segment containing `ts`; returns image/jpeg |
| `POST /timelapse` | Start render job; body: `{camera, start_ts, end_ts, speed, name, watermark, encode_preset}`; returns `{job_id}` |
| `GET /timelapse/{job_id}` | Poll job status: `{status, progress, output_file}` |
| `GET /timelapse/{job_id}/download` | Stream finished MP4 to browser |

### ffmpeg pipeline

**Thumbnail extraction** (single frame from a segment file):
```bash
ffmpeg -ss {offset} -i {segment.mp4} -frames:v 1 -q:v 2 -f image2 pipe:1
```

**Timelapse render** — parallel chunk encoding + stream-copy join:

`n_chunks = min(CPU_COUNT, n_segments ÷ 4)`. Falls back to a single ffmpeg process
when fewer than 4 segments per core. Each chunk gets `CPU_COUNT ÷ n_chunks` threads.

```bash
# Step 1: write one concat list per chunk (temp files, deleted after render)
echo "file '...mp4'" > /tmp/frigate_chunk_XXXX.txt

# Step 2: run N ffmpeg processes in parallel (one per chunk)
ffmpeg -y -f concat -safe 0 -i chunk_N.txt \
  -vf "scale=-2:if(gt(ih\,1080)\,1080\,ih),setpts=0.00333*PTS" \
  -an \
  -c:v libx264 -preset fast -crf 28 -threads {CPU÷N} \
  -pix_fmt yuv420p -progress pipe:2 \
  {output_dir}/_chunk_{job_id}_{N}.mp4

# Step 3: stream-copy join — no re-encode, lossless concat
ffmpeg -y -f concat -safe 0 -i join.txt -c:v copy {output.mp4}
```

CRF and max output height are configurable (`OUTPUT_CRF`, `OUTPUT_MAX_HEIGHT`).
The encode preset is chosen per-render in the UI (Quality/Balanced/Speed maps to
ffmpeg `medium`/`fast`/`ultrafast`). Speed multiplier is user-controlled (e.g.
10× = `setpts=0.1*PTS`). Audio is always dropped (`-an`).

**Scale filter `\,` escaping**: within an ffmpeg filtergraph, commas separate
filters. Inside an option value, commas must be escaped as `\,`. In Python source,
write `\\,` so the runtime string contains `\,`, which ffmpeg parses as a literal
comma inside the expression (e.g. `if(gt(ih\\,1080)\\,1080\\,ih)`).

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
  1. Camera dropdown (from `/cameras` index)
  2. Date picker — defaults to today in recording timezone; updates on camera change
  3. Timeline strip: thumbnails for each hour; drag handles to set start/end; double-
     click an hour to snap both handles to a 1-hour selection; two HH:MM text inputs
     below the strip stay in sync with the handles and accept direct edits (validates
     format + start < end; warns if the entered hour has no footage but still applies)
  4. Start/end preview frames update as handles move
  5. Speed multiplier (5×–1200×)
  6. Encode quality: Quality / Balanced / Speed → ffmpeg `medium` / `fast` / `ultrafast`
  7. Watermark toggle — camera name + recording-timezone timestamp burned in
  8. Output filename (auto-generated, editable)
  9. "Build Timelapse" → progress bar polling `/timelapse/{job_id}`
  10. On completion: "Download" button

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
    ├── index.py            # startup coverage index: in-memory + background build/watch + persistence
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

Push the repo to GitHub (must be public). Users add it via:
- The My HA badge link (see README) — one click, opens the repository dialog pre-filled
- Manual: **Settings → Add-ons → Add-on Store** (bottom-right button) **→ ⋮ → Repositories**

HA builds the image locally from the Dockerfile — no registry push needed.

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
- **Job cleanup** — finished job metadata is kept in an in-memory dict; temp concat
  list files and chunk MP4s are deleted in the `finally` block of `_render_sync`
- **Coverage index** — built at startup by walking the entire recordings tree; persisted
  to `/data/coverage_index.json` so restarts are instant; updated live via `watchfiles`
  watching `RECORDINGS_PATH`. The in-memory structure is
  `{camera: {utc_date_str: {utc_hour: set[minute]}}}`. The `/status` endpoint reports
  build progress so the UI can show a loading indicator while the initial scan runs.
- **HA OS host shell has no python3** — all deploy scripting runs locally
- **All HA add-ons share the `hassio` Docker network** — Frigate reachable at
  `http://<frigate-slug>:5000` without extra network config in config.yaml
