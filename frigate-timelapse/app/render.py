"""
ffmpeg-based thumbnail extraction and timelapse rendering.

Jobs run in a thread pool (asyncio.to_thread) so they don't block the event loop.
Progress is tracked by parsing ffmpeg's -progress output against total input duration.
"""

import asyncio
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from app.config import CAMERA_TZ, OUTPUT_CRF, OUTPUT_MAX_HEIGHT, TIMELAPSE_RETENTION_SECONDS
from app.recordings import Segment, find_segments

FFMPEG = "ffmpeg"

_FONT_SIZES = {"small": 18, "medium": 28, "large": 42}
_MARGIN = 40


class Status(str, Enum):
    PENDING  = "pending"
    RUNNING  = "running"
    COMPLETE = "complete"
    ERROR    = "error"


@dataclass
class WatermarkConfig:
    enabled:  bool = False
    content:  str  = "both"          # "time" | "camera" | "both"
    position: str  = "bottom-right"  # top-left | top-right | bottom-left | bottom-right
    size:     str  = "medium"        # small | medium | large


@dataclass
class Job:
    id: str
    camera: str
    start: datetime
    end: datetime
    speed: float
    output_path: Path
    watermark: WatermarkConfig = None   # type: ignore[assignment]
    status: Status = Status.PENDING
    progress: float = 0.0
    error: str = ""

    def __post_init__(self) -> None:
        if self.watermark is None:
            self.watermark = WatermarkConfig()


# Module-level job store — survives for the lifetime of the process.
_jobs: dict[str, Job] = {}


def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def list_jobs() -> list[Job]:
    return list(_jobs.values())


# ---------------------------------------------------------------------------
# Thumbnail extraction
# ---------------------------------------------------------------------------

def extract_thumbnail_sync(segment: Segment, offset: float) -> bytes:
    """
    Pull a single JPEG frame from *segment* at *offset* seconds into the file.
    Returns raw JPEG bytes. Runs synchronously — call via asyncio.to_thread.

    Frigate segments have gaps between them; the actual file duration is often
    shorter than (next_segment.start - this_segment.start). If the requested
    offset falls past EOF ffmpeg returns 0 bytes — we retry at offset 0.
    """
    for ss in [max(0.0, offset), 0.0]:
        cmd = [
            FFMPEG, "-y",
            "-ss", str(ss),
            "-i", str(segment.path),
            "-frames:v", "1",
            "-q:v", "2",
            "-f", "image2",
            "pipe:1",
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=15)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg thumbnail failed: {result.stderr.decode()[-500:]}")
        if result.stdout:
            return result.stdout
    raise RuntimeError(f"ffmpeg produced no output for {segment.path}")


async def extract_thumbnail(segment: Segment, offset: float) -> bytes:
    return await asyncio.to_thread(extract_thumbnail_sync, segment, offset)


# ---------------------------------------------------------------------------
# Watermark filter building
# ---------------------------------------------------------------------------

def _find_font() -> str:
    """Return a usable TTF/TTC font path for ffmpeg drawtext, or '' for built-in."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",   # Debian / Docker
        "/usr/share/fonts/TTF/DejaVuSans.ttf",                # Alpine
        "/System/Library/Fonts/Supplemental/Arial.ttf",       # macOS
        "/System/Library/Fonts/Helvetica.ttc",                # macOS fallback
    ]
    for p in candidates:
        if Path(p).exists():
            return p
    try:
        r = subprocess.run(
            ["fc-match", "--format=%{file}", "sans"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return ""


def _watermark_filters(wm: WatermarkConfig, camera: str, start_ts: int) -> list[str]:
    """
    Build a list of ffmpeg drawtext filter strings for the watermark.
    Applied BEFORE setpts so %{pts} reflects the original recording timeline.
    """
    fontsize = _FONT_SIZES.get(wm.size, 28)
    font     = _find_font()
    ff       = f"fontfile={font}:" if font else ""
    x        = str(_MARGIN) if "left" in wm.position else f"w-tw-{_MARGIN}"

    def y_for_line(line: int, total: int) -> str:
        if wm.position.startswith("top"):
            return str(_MARGIN + line * (fontsize + 4))
        # bottom: last line at margin from bottom, earlier lines stack upward
        offset = (total - 1 - line) * (fontsize + 4)
        return f"h-th-{_MARGIN + offset}"

    # Escape camera name for ffmpeg drawtext option value
    cam = camera.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")

    # start_ts is a true UTC unix timestamp (recordings.py parses file paths as UTC).
    # Shift by the camera timezone offset so gmtime(adj_epoch + pts) shows local wall time.
    from datetime import datetime as _dt
    _cam_offset = int(_dt.fromtimestamp(start_ts, tz=CAMERA_TZ).utcoffset().total_seconds())
    adj_epoch   = start_ts + _cam_offset
    # %Y-%m-%d %T has no bare colons in the format string, avoiding ffmpeg's argument-separator bug.
    datetime_expr = f"%{{pts\\:gmtime\\:{adj_epoch}\\:%Y-%m-%d %T}}"

    lines: list[str] = []
    if wm.content in ("camera", "both"):
        lines.append(cam)
    if wm.content in ("time", "both"):
        lines.append(datetime_expr)

    filters: list[str] = []
    for i, text in enumerate(lines):
        y = y_for_line(i, len(lines))
        filters.append(
            f"drawtext={ff}text='{text}':"
            f"x={x}:y={y}:"
            f"fontsize={fontsize}:"
            f"fontcolor=white@0.9:"
            f"shadowx=1:shadowy=1:shadowcolor=black@0.8"
        )
    return filters


# ---------------------------------------------------------------------------
# Timelapse rendering
# ---------------------------------------------------------------------------

def _total_duration(segments: list[Segment]) -> float:
    return sum((s.end - s.start).total_seconds() for s in segments)


def _render_sync(job: Job, segments: list[Segment]) -> None:
    """
    Concatenate *segments*, optionally burn in a watermark, then speed up.
    Writes to job.output_path. Updates job.progress while running.
    """
    job.status = Status.RUNNING
    total_duration = _total_duration(segments)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", prefix="frigate_concat_", delete=False
    ) as f:
        concat_path = Path(f.name)
        for seg in segments:
            f.write(f"file '{seg.path.absolute()}'\n")

    try:
        # Build the -vf filter chain.
        # Watermark drawtext runs first (sees original PTS = recording offset),
        # then setpts compresses the timeline for the speed-up.
        vf_parts: list[str] = []
        if job.watermark and job.watermark.enabled:
            vf_parts.extend(
                _watermark_filters(job.watermark, job.camera, int(job.start.timestamp()))
            )
        if OUTPUT_MAX_HEIGHT > 0:
            # Scale down if source is taller than max height; never upscale.
            # \, escapes the comma so ffmpeg doesn't treat it as a filter separator.
            vf_parts.append(
                f"scale=-2:if(gt(ih\\,{OUTPUT_MAX_HEIGHT})\\,{OUTPUT_MAX_HEIGHT}\\,ih)"
            )
        vf_parts.append(f"setpts={1.0 / job.speed:.6f}*PTS")
        vf = ",".join(vf_parts)

        cmd = [
            FFMPEG, "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_path),
            "-vf", vf,
            "-an",
            "-c:v", "libx264", "-preset", "fast", "-crf", str(OUTPUT_CRF),
            "-pix_fmt", "yuv420p",
            "-progress", "pipe:2",
            str(job.output_path),
        ]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        for line in proc.stderr:  # type: ignore[union-attr]
            line = line.strip()
            m = re.match(r"out_time_us=(\d+)", line)
            if m and total_duration > 0:
                # out_time_us is output PTS — multiply by speed to recover input time.
                elapsed_s = int(m.group(1)) / 1_000_000 * job.speed
                job.progress = min(elapsed_s / total_duration, 0.99)

        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg exited with code {proc.returncode}")

        job.progress = 1.0
        job.status = Status.COMPLETE

    except Exception as exc:
        job.status = Status.ERROR
        job.error = str(exc)
        raise

    finally:
        concat_path.unlink(missing_ok=True)


_PURGE_DELAY = TIMELAPSE_RETENTION_SECONDS


async def _purge_after(path: Path) -> None:
    await asyncio.sleep(_PURGE_DELAY)
    path.unlink(missing_ok=True)


async def _render_async(job: Job, segments: list[Segment]) -> None:
    try:
        await asyncio.to_thread(_render_sync, job, segments)
    except Exception:
        pass  # status/error already set in _render_sync
    if job.status == Status.COMPLETE:
        asyncio.create_task(_purge_after(job.output_path))


def create_job(
    camera: str,
    start: datetime,
    end: datetime,
    speed: float,
    output_dir: Path,
    recordings_root: str | Path,
    name: str = "",
    watermark: WatermarkConfig | None = None,
) -> Job:
    """
    Create a timelapse render job and schedule it as a background task.
    Returns the Job immediately; caller can poll job.status / job.progress.
    """
    job_id    = str(uuid.uuid4())
    safe_name = name.strip() or f"{camera}_{start.strftime('%Y%m%d_%H%M%S')}"
    output_path = output_dir / f"{safe_name}.mp4"

    job = Job(
        id=job_id,
        camera=camera,
        start=start,
        end=end,
        speed=speed,
        output_path=output_path,
        watermark=watermark or WatermarkConfig(),
    )
    _jobs[job_id] = job

    segments = find_segments(camera, start, end, recordings_root)
    if not segments:
        job.status = Status.ERROR
        job.error = f"No footage found for {camera} between {start} and {end}"
        return job

    asyncio.create_task(_render_async(job, segments))
    return job
