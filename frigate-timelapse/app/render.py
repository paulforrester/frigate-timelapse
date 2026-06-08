"""
ffmpeg-based thumbnail extraction and timelapse rendering.

Parallel rendering splits the segment list across CPU_COUNT worker ffmpeg processes,
then joins their outputs via a stream-copy concatenation pass (no re-encode).
Each chunk starts encoding immediately — the pipelining is across cores, not time.

Progress is tracked per-chunk via ffmpeg's -progress output; job.progress is the
duration-weighted average across all running chunks.
"""

import asyncio
import logging
import os
import re
import subprocess
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable

from app.config import CAMERA_TZ, OUTPUT_CRF, OUTPUT_MAX_HEIGHT, TIMELAPSE_RETENTION_SECONDS
from app.recordings import Segment, find_segments

FFMPEG = "ffmpeg"
log = logging.getLogger(__name__)

CPU_COUNT = os.cpu_count() or 1
_MIN_SEGS_PER_CHUNK = 4      # minimum segments per chunk; below this, single-process is faster

_FONT_SIZES = {"small": 18, "medium": 28, "large": 42}
_MARGIN = 40

# UI label → ffmpeg preset string
_PRESET_MAP = {
    "quality":  "medium",     # slower encode, better compression, smaller file
    "balanced": "fast",       # default: good tradeoff
    "speed":    "ultrafast",  # fastest encode, largest file
}


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
    id:            str
    camera:        str
    start:         datetime
    end:           datetime
    speed:         float
    output_path:   Path
    watermark:     WatermarkConfig = None   # type: ignore[assignment]
    encode_preset: str             = "fast"
    status:        Status          = Status.PENDING
    progress:      float           = 0.0
    error:         str             = ""

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
# Shared ffmpeg helpers
# ---------------------------------------------------------------------------

def _total_duration(segments: list[Segment]) -> float:
    return sum((s.end - s.start).total_seconds() for s in segments)


def _build_vf(job: Job, chunk_start_ts: int) -> str:
    """Build the complete -vf filter chain for one chunk."""
    parts: list[str] = []
    if job.watermark and job.watermark.enabled:
        parts.extend(_watermark_filters(job.watermark, job.camera, chunk_start_ts))
    if OUTPUT_MAX_HEIGHT > 0:
        parts.append(
            f"scale=-2:if(gt(ih\\,{OUTPUT_MAX_HEIGHT})\\,{OUTPUT_MAX_HEIGHT}\\,ih)"
        )
    parts.append(f"setpts={1.0 / job.speed:.6f}*PTS")
    return ",".join(parts)


def _run_encode(
    cmd: list[str],
    chunk_duration: float,
    speed: float,
    chunk_progress: list[float],
    chunk_idx: int,
    on_progress: Callable[[], None],
) -> None:
    """Run ffmpeg, parsing out_time_us to update chunk_progress[chunk_idx]."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
    )
    for line in proc.stderr:  # type: ignore[union-attr]
        m = re.match(r"out_time_us=(\d+)", line.strip())
        if m and chunk_duration > 0:
            # out_time_us is output PTS — multiply by speed to recover input time.
            elapsed_s = int(m.group(1)) / 1_000_000 * speed
            chunk_progress[chunk_idx] = min(elapsed_s / chunk_duration, 0.99)
            on_progress()
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exited with code {proc.returncode}")


# ---------------------------------------------------------------------------
# Per-chunk encoding
# ---------------------------------------------------------------------------

def _encode_chunk(
    segments: list[Segment],
    output_path: Path,
    job: Job,
    thread_count: int,
    chunk_duration: float,
    chunk_progress: list[float],
    chunk_idx: int,
    on_progress: Callable[[], None],
) -> None:
    """Render one list of segments to output_path."""
    chunk_start_ts = int(segments[0].start.timestamp())
    vf = _build_vf(job, chunk_start_ts)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", prefix="frigate_chunk_", delete=False
    ) as f:
        concat_path = Path(f.name)
        for seg in segments:
            f.write(f"file '{seg.path.absolute()}'\n")

    try:
        cmd = [
            FFMPEG, "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_path),
            "-vf", vf,
            "-an",
            "-c:v", "libx264",
            "-preset", job.encode_preset,
            "-crf", str(OUTPUT_CRF),
            "-threads", str(thread_count),
            "-pix_fmt", "yuv420p",
            "-progress", "pipe:2",
            str(output_path),
        ]
        _run_encode(cmd, chunk_duration, job.speed, chunk_progress, chunk_idx, on_progress)
    finally:
        concat_path.unlink(missing_ok=True)


def _concat_chunks(chunk_paths: list[Path], output_path: Path) -> None:
    """Stream-copy pre-encoded chunks into one final MP4 (no re-encode)."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", prefix="frigate_join_", delete=False
    ) as f:
        concat_path = Path(f.name)
        for p in chunk_paths:
            f.write(f"file '{p.absolute()}'\n")
    try:
        cmd = [
            FFMPEG, "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_path),
            "-c:v", "copy",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"chunk join failed: {result.stderr[-500:]}")
    finally:
        concat_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Timelapse rendering (orchestrator)
# ---------------------------------------------------------------------------

def _split_chunks(segments: list[Segment], n: int) -> list[list[Segment]]:
    """Split segments into at most n roughly equal-sized chunks."""
    if n <= 1:
        return [segments]
    chunk_size = max(_MIN_SEGS_PER_CHUNK, len(segments) // n)
    chunks = [segments[i:i + chunk_size] for i in range(0, len(segments), chunk_size)]
    # Merge a tiny tail chunk into the previous one to avoid wasted ffmpeg startup.
    if len(chunks) > 1 and len(chunks[-1]) < _MIN_SEGS_PER_CHUNK:
        chunks[-2].extend(chunks.pop())
    return chunks


def _render_sync(job: Job, segments: list[Segment]) -> None:
    """
    Concatenate segments, optionally burn watermark, then speed up.
    Writes to job.output_path. Updates job.progress while running.
    Uses parallel chunks when segment count justifies it.
    """
    job.status = Status.RUNNING
    total_duration = _total_duration(segments)
    n_chunks = min(CPU_COUNT, max(1, len(segments) // _MIN_SEGS_PER_CHUNK))

    try:
        if n_chunks <= 1:
            log.info(
                "Render %s: %d segs, %.0fs input → single ffmpeg, %d threads",
                job.id[:8], len(segments), total_duration, CPU_COUNT,
            )
            chunk_progress = [0.0]

            def _update_single() -> None:
                job.progress = chunk_progress[0]

            _encode_chunk(
                segments, job.output_path, job,
                thread_count=CPU_COUNT,
                chunk_duration=total_duration,
                chunk_progress=chunk_progress,
                chunk_idx=0,
                on_progress=_update_single,
            )

        else:
            chunks = _split_chunks(segments, n_chunks)
            actual_n = len(chunks)
            threads_per_chunk = max(1, CPU_COUNT // actual_n)
            chunk_durations = [_total_duration(c) for c in chunks]
            chunk_progress = [0.0] * actual_n
            chunk_paths = [
                job.output_path.parent / f"_chunk_{job.id}_{i}.mp4"
                for i in range(actual_n)
            ]

            log.info(
                "Render %s: %d segs, %.0fs input → %d parallel chunks, %d threads each",
                job.id[:8], len(segments), total_duration, actual_n, threads_per_chunk,
            )

            def _update_parallel() -> None:
                if total_duration > 0:
                    job.progress = min(
                        sum(
                            chunk_progress[i] * chunk_durations[i]
                            for i in range(actual_n)
                        ) / total_duration,
                        0.99,
                    )

            try:
                with ThreadPoolExecutor(max_workers=actual_n) as pool:
                    futures = {
                        pool.submit(
                            _encode_chunk,
                            chunk, chunk_paths[i], job, threads_per_chunk,
                            chunk_durations[i], chunk_progress, i, _update_parallel,
                        ): i
                        for i, chunk in enumerate(chunks)
                    }
                    errors: list[Exception] = []
                    for fut in as_completed(futures):
                        try:
                            fut.result()
                        except Exception as exc:
                            errors.append(exc)

                if errors:
                    raise errors[0]

                log.info("Render %s: all chunks done, joining", job.id[:8])
                _concat_chunks(chunk_paths, job.output_path)

            finally:
                for p in chunk_paths:
                    p.unlink(missing_ok=True)

        job.progress = 1.0
        job.status = Status.COMPLETE

    except Exception as exc:
        job.status = Status.ERROR
        job.error = str(exc)
        raise


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
    encode_preset: str = "balanced",
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
        encode_preset=_PRESET_MAP.get(encode_preset, "fast"),
    )
    _jobs[job_id] = job

    segments = find_segments(camera, start, end, recordings_root)
    if not segments:
        job.status = Status.ERROR
        job.error = f"No footage found for {camera} between {start} and {end}"
        return job

    asyncio.create_task(_render_async(job, segments))
    return job
