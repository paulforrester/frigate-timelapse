import asyncio
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import index
from app.config import CAMERA_TZ
from app.recordings import find_segments, segment_at
from app.render import CPU_COUNT, Status, WatermarkConfig, create_job, extract_thumbnail, get_job

log = logging.getLogger(__name__)

TZ = CAMERA_TZ

RECORDINGS_PATH = Path(os.environ.get("RECORDINGS_PATH", "/media/frigate/recordings"))
OUTPUT_PATH     = Path(os.environ.get("OUTPUT_PATH",     "/media/frigate/timelapses"))

app = FastAPI(title="Frigate Timelapse")


@app.on_event("startup")
async def _startup() -> None:
    log.info("CPU cores available: %d", CPU_COUNT)
    OUTPUT_PATH.mkdir(parents=True, exist_ok=True)
    last_scan_time = index.init()
    asyncio.create_task(index.build(RECORDINGS_PATH, last_scan_time))
    asyncio.create_task(index.watch(RECORDINGS_PATH))


# ---------------------------------------------------------------------------
# Cameras
# ---------------------------------------------------------------------------

@app.get("/cameras")
async def cameras() -> list[str]:
    return index.get_cameras()


@app.get("/status")
async def status() -> dict:
    return index.get_status()


@app.get("/timezone")
async def timezone_info(date: str = "") -> dict:
    """Return the UTC offset (seconds) for the recording timezone on a given date.
    The browser uses this to build correct unix timestamps regardless of its own timezone."""
    try:
        d = datetime.strptime(date, "%Y-%m-%d").date() if date else datetime.now(TZ).date()
    except ValueError:
        d = datetime.now(TZ).date()
    dt = datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=TZ)
    return {"offset_seconds": int(dt.utcoffset().total_seconds())}


# ---------------------------------------------------------------------------
# Coverage — which hours/minutes have footage for a camera+date
# ---------------------------------------------------------------------------

@app.get("/coverage")
async def coverage(camera: str, date: str) -> dict[int, list[int]]:
    """
    Returns {hour: [minute, ...]} for every segment start on *date* (in camera local time).
    Served from the in-memory index — no filesystem walking at request time.
    """
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
    return index.get_coverage(camera, date, TZ)


# ---------------------------------------------------------------------------
# Thumbnail — single JPEG frame at a given unix timestamp
# ---------------------------------------------------------------------------

@app.get("/thumbnail")
async def thumbnail(camera: str, ts: float) -> Response:
    dt          = datetime.fromtimestamp(ts, tz=TZ)
    seg, offset = segment_at(camera, dt, RECORDINGS_PATH)
    if seg is None:
        raise HTTPException(status_code=404, detail="No footage at that timestamp")

    jpeg = await extract_thumbnail(seg, offset)
    return Response(content=jpeg, media_type="image/jpeg")


# ---------------------------------------------------------------------------
# Timelapse — create job, poll status, download result
# ---------------------------------------------------------------------------

class WatermarkOptions(BaseModel):
    enabled:  bool = False
    content:  str  = "both"
    position: str  = "bottom-right"
    size:     str  = "medium"


class TimelapseRequest(BaseModel):
    camera:        str
    start_ts:      int                           # unix seconds
    end_ts:        int                           # unix seconds
    speed:         float          = 10.0         # playback multiplier
    name:          str            = ""
    watermark:     WatermarkOptions = WatermarkOptions()
    encode_preset: str            = "balanced"   # "quality" | "balanced" | "speed"


@app.post("/timelapse", status_code=202)
async def start_timelapse(req: TimelapseRequest) -> dict:
    start = datetime.fromtimestamp(req.start_ts, tz=TZ)
    end   = datetime.fromtimestamp(req.end_ts,   tz=TZ)

    if end <= start:
        raise HTTPException(status_code=422, detail="end_ts must be after start_ts")
    if (end - start) > timedelta(hours=24):
        raise HTTPException(status_code=422, detail="Time range capped at 24 hours")
    if req.speed <= 0:
        raise HTTPException(status_code=422, detail="speed must be positive")

    job = create_job(
        camera=req.camera,
        start=start,
        end=end,
        speed=req.speed,
        output_dir=OUTPUT_PATH,
        recordings_root=RECORDINGS_PATH,
        name=req.name,
        watermark=WatermarkConfig(
            enabled=req.watermark.enabled,
            content=req.watermark.content,
            position=req.watermark.position,
            size=req.watermark.size,
        ),
        encode_preset=req.encode_preset,
    )
    return {"job_id": job.id}


@app.get("/timelapse/{job_id}")
async def timelapse_status(job_id: str) -> dict:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    resp: dict = {
        "job_id":   job.id,
        "status":   job.status,
        "progress": round(job.progress, 3),
        "camera":   job.camera,
        "start":    job.start.isoformat(),
        "end":      job.end.isoformat(),
        "speed":    job.speed,
    }
    if job.status == Status.COMPLETE:
        resp["output_file"]   = job.output_path.name
        resp["download_url"]  = f"/timelapse/{job_id}/download"
    if job.status == Status.ERROR:
        resp["error"] = job.error

    return resp


@app.get("/timelapse/{job_id}/download")
async def timelapse_download(job_id: str) -> FileResponse:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != Status.COMPLETE:
        raise HTTPException(status_code=409, detail="Job not complete yet")
    if not job.output_path.exists():
        raise HTTPException(status_code=404, detail="Output file missing")

    return FileResponse(
        path=job.output_path,
        media_type="video/mp4",
        filename=job.output_path.name,
    )


# ---------------------------------------------------------------------------
# Static UI — must be last so API routes take precedence
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
