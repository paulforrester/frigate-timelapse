"""
Filesystem walker and segment-to-timestamp mapper for Frigate recordings.

Recording tree layout:
    {recordings_root}/{YYYY-MM-DD}/{HH}/{camera}/{MM}.{SS}.mp4

Frigate writes directory names in UTC.  All Segment.start / Segment.end
datetimes are UTC-aware (tzinfo=timezone.utc).  Callers that want to query
by a local time range must pass tz-aware datetimes; Python will compare
them correctly across timezones.
"""

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True, order=True)
class Segment:
    start: datetime   # UTC-aware
    end: datetime     # UTC-aware; derived from next segment or +60 s fallback
    path: Path


def _parse_segment_start(path: Path) -> datetime:
    """Extract the segment's start time from its path components (UTC)."""
    # path: .../YYYY-MM-DD/HH/camera/MM.SS.mp4
    parts = path.parts
    # Walk back: filename, camera, HH, YYYY-MM-DD
    date_str   = parts[-4]   # YYYY-MM-DD
    hour_str   = parts[-3]   # HH (UTC)
    stem       = path.stem   # MM.SS
    minute_str, second_str = stem.split(".")

    date = datetime.strptime(date_str, "%Y-%m-%d").date()
    return datetime(
        date.year, date.month, date.day,
        int(hour_str), int(minute_str), int(second_str),
        tzinfo=timezone.utc,   # Frigate paths are always UTC
    )


def _segments_in_dir(cam_dir: Path) -> list[Segment]:
    """Return Segment list for all MP4s in one camera/hour directory, sorted by start."""
    mp4s = sorted(cam_dir.glob("*.mp4"))
    if not mp4s:
        return []

    starts = [_parse_segment_start(p) for p in mp4s]

    segments: list[Segment] = []
    for i, (path, start) in enumerate(zip(mp4s, starts)):
        # End time = start of the next segment in this dir, or start + 60 s as fallback.
        end = starts[i + 1] if i + 1 < len(starts) else start + timedelta(seconds=60)
        segments.append(Segment(start=start, end=end, path=path))

    return segments


def find_segments(
    camera: str,
    start: datetime,
    end: datetime,
    recordings_root: str | Path,
) -> list[Segment]:
    """
    Return all Segments for *camera* whose time window overlaps [start, end).

    *start* and *end* must be tz-aware.  Naive datetimes are assumed UTC.
    Segments are always stored as UTC; callers may pass LA-aware datetimes
    and Python will compare across timezones correctly.
    """
    root = Path(recordings_root)

    def _ensure_aware(dt: datetime) -> datetime:
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    start = _ensure_aware(start)
    end   = _ensure_aware(end)

    result: list[Segment] = []

    for date_dir in sorted(root.iterdir()):
        if not date_dir.is_dir():
            continue
        try:
            dir_date = datetime.strptime(date_dir.name, "%Y-%m-%d").date()
        except ValueError:
            continue

        # Skip entire date dirs that can't overlap (dirs are in UTC).
        day_start = datetime(dir_date.year, dir_date.month, dir_date.day, 0, 0, 0,
                             tzinfo=timezone.utc)
        day_end   = day_start + timedelta(days=1)
        if day_end <= start or day_start >= end:
            continue

        for hour_dir in sorted(date_dir.iterdir()):
            if not hour_dir.is_dir():
                continue
            try:
                hour = int(hour_dir.name)
            except ValueError:
                continue

            # Skip hour dirs that can't overlap (hours are in UTC).
            hour_start = datetime(dir_date.year, dir_date.month, dir_date.day, hour, 0, 0,
                                  tzinfo=timezone.utc)
            hour_end   = hour_start + timedelta(hours=1)
            if hour_end <= start or hour_start >= end:
                continue

            cam_dir = hour_dir / camera
            if not cam_dir.is_dir():
                continue

            for seg in _segments_in_dir(cam_dir):
                if seg.start < end and seg.end > start:
                    result.append(seg)

    return sorted(result)


def list_cameras(recordings_root: str | Path) -> list[str]:
    """Derive camera names from the most recent date directory."""
    root = Path(recordings_root)
    date_dirs = sorted(d for d in root.iterdir() if d.is_dir())
    if not date_dirs:
        return []

    latest   = date_dirs[-1]
    hour_dirs = sorted(h for h in latest.iterdir() if h.is_dir())
    if not hour_dirs:
        return []

    return sorted(cam.name for cam in hour_dirs[0].iterdir() if cam.is_dir())


def segment_at(
    camera: str,
    ts: datetime,
    recordings_root: str | Path,
    search_window: int = 120,
) -> tuple["Segment | None", float]:
    """
    Return (segment, offset_seconds) for *ts*.

    *ts* must be tz-aware (LA or UTC — comparison works either way).
    If *ts* falls in a gap between segments, returns the nearest segment
    within *search_window* seconds with offset=0.
    """
    window_start = ts - timedelta(seconds=search_window)
    window_end   = ts + timedelta(seconds=search_window)
    candidates   = find_segments(camera, window_start, window_end, recordings_root)

    if not candidates:
        return None, 0.0

    for seg in reversed(candidates):
        if seg.start <= ts < seg.end:
            return seg, max(0.0, (ts - seg.start).total_seconds())

    nearest = min(candidates, key=lambda s: abs((s.start - ts).total_seconds()))
    return nearest, 0.0
