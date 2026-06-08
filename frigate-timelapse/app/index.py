"""
In-memory recording index for fast camera/coverage lookups.

Index structure: {camera: {utc_date_str: {utc_hour: set[minute_int]}}}
Frigate recording paths are always UTC-named. Coverage queries convert
UTC entries to local time using the caller-supplied ZoneInfo.

Startup flow:
  1. init()         — load persisted JSON into _index (sync, fast, before first request)
  2. build(root, t) — async background task: incremental scan for changes since t, then save
  3. watch(root)    — async background task: watchfiles watcher for live updates
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone as _utc
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import VERBOSE_LOGGING

log = logging.getLogger(__name__)

# {camera: {utc_date_str: {utc_hour: set[minute]}}}
_index: dict[str, dict[str, dict[int, set[int]]]] = {}
_indexed: bool = False
_progress: float = 0.0
_last_verbose_log: float = 0.0  # monotonic timestamp of the last verbose scan log

# HA add-ons always have /data/ as writable persistent storage.
# Override with INDEX_FILE env var for local dev.
_INDEX_FILE = Path(os.environ.get("INDEX_FILE", "/data/coverage_index.json"))


# ---------------------------------------------------------------------------
# Public read API
# ---------------------------------------------------------------------------

def get_cameras() -> list[str]:
    return sorted(_index.keys())


def get_coverage(camera: str, date_str: str, tz: ZoneInfo) -> dict[int, list[int]]:
    """
    Return {local_hour: [local_minutes]} for *camera* on local calendar date *date_str*.
    Converts UTC index entries to local time using *tz*.
    The local day may span two UTC calendar dates; both are queried.
    """
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    local_midnight = datetime(d.year, d.month, d.day, tzinfo=tz)
    utc_start = local_midnight.astimezone(_utc.utc)
    utc_end   = (local_midnight + timedelta(days=1)).astimezone(_utc.utc)

    cam_data = _index.get(camera, {})
    result: dict[int, set[int]] = {}

    # Collect the UTC calendar dates that overlap with the requested local day.
    utc_date_strs: set[str] = set()
    t = utc_start.replace(minute=0, second=0, microsecond=0)
    while t < utc_end:
        utc_date_strs.add(t.strftime("%Y-%m-%d"))
        t += timedelta(hours=1)

    for utc_date_str in utc_date_strs:
        for utc_hour, minutes in cam_data.get(utc_date_str, {}).items():
            hour_utc = datetime(
                *[int(x) for x in utc_date_str.split("-")],
                utc_hour, 0, 0, tzinfo=_utc.utc,
            )
            if not (utc_start <= hour_utc < utc_end):
                continue
            for minute in minutes:
                seg_local = hour_utc.replace(minute=minute).astimezone(tz)
                result.setdefault(seg_local.hour, set()).add(seg_local.minute)

    return {h: sorted(mins) for h, mins in sorted(result.items())}


def get_status() -> dict:
    return {"indexed": _indexed, "progress": round(_progress)}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _add(camera: str, utc_date_str: str, utc_hour: int, minute: int) -> None:
    _index.setdefault(camera, {}).setdefault(utc_date_str, {}).setdefault(utc_hour, set()).add(minute)


def _parse_segment_path(path: Path, root: Path) -> tuple[str, str, int, int] | None:
    """Return (camera, utc_date_str, utc_hour, minute) or None if not a valid segment path."""
    try:
        parts = path.relative_to(root).parts  # (YYYY-MM-DD, HH, camera, MM.SS.mp4)
        if len(parts) != 4 or not parts[3].endswith(".mp4"):
            return None
        date_str, hour_str, camera, filename = parts
        minute = int(filename.split(".")[0])
        return camera, date_str, int(hour_str), minute
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def init() -> float:
    """
    Load the persisted index synchronously. Returns last_scan_time (0.0 if not found).
    Call before the event loop starts accepting requests so the index is immediately
    available even before the background build finishes.
    """
    global _index
    if not _INDEX_FILE.exists():
        return 0.0
    try:
        data = json.loads(_INDEX_FILE.read_text())
        raw = data.get("index", {})
        _index = {
            cam: {
                date: {int(h): set(mins) for h, mins in hours.items()}
                for date, hours in dates.items()
            }
            for cam, dates in raw.items()
        }
        n_dates = sum(len(d) for d in _index.values())
        log.info("Persisted index loaded: %d cameras, %d date-entries", len(_index), n_dates)
        return float(data.get("last_scan_time", 0.0))
    except Exception as exc:
        log.warning("Could not load persisted index (%s); will do full scan", exc)
        _index = {}
        return 0.0


def _save_sync(last_scan_time: float) -> None:
    """Serialize and write _index to disk. Runs in a thread pool worker."""
    try:
        _INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_scan_time": last_scan_time,
            "index": {
                cam: {
                    date: {str(h): sorted(mins) for h, mins in hours.items()}
                    for date, hours in dates.items()
                }
                for cam, dates in _index.items()
            },
        }
        tmp = _INDEX_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(_INDEX_FILE)
    except OSError as exc:
        log.warning("Could not persist index to %s: %s", _INDEX_FILE, exc)


# ---------------------------------------------------------------------------
# Scanning (thread-safe: results collected in local dict, merged in event loop)
# ---------------------------------------------------------------------------

_VERBOSE_INTERVAL = 30.0  # seconds between verbose scan log lines


def _scan_date_sync(date_dir: Path, verbose: bool = False) -> dict[str, dict[int, set[int]]]:
    """
    Walk one date directory and return {camera: {hour: {minutes}}}.
    Runs entirely in a thread pool worker — never touches module globals except
    _last_verbose_log (written only when verbose=True; acceptable race, see below).
    When verbose=True, logs camera/date/hour at most every _VERBOSE_INTERVAL seconds.
    """
    global _last_verbose_log
    result: dict[str, dict[int, set[int]]] = {}
    date_str = date_dir.name
    for hour_dir in sorted(date_dir.iterdir()):
        if not hour_dir.is_dir():
            continue
        try:
            hour = int(hour_dir.name)
        except ValueError:
            continue
        for camera_dir in sorted(hour_dir.iterdir()):
            if not camera_dir.is_dir():
                continue
            camera = camera_dir.name
            if verbose:
                now = time.monotonic()
                if now - _last_verbose_log >= _VERBOSE_INTERVAL:
                    _last_verbose_log = now
                    log.info(
                        "Index scan [%.0f%%]: date=%s hour=%02d:00 camera=%s",
                        _progress, date_str, hour, camera,
                    )
            for seg in camera_dir.glob("*.mp4"):
                try:
                    minute = int(seg.stem.split(".")[0])
                    result.setdefault(camera, {}).setdefault(hour, set()).add(minute)
                except (ValueError, IndexError):
                    pass
    return result


def _merge_date(date_str: str, partial: dict[str, dict[int, set[int]]]) -> None:
    """Merge a thread-scan result into _index. Called in the event loop."""
    for camera, hours in partial.items():
        date_data = _index.setdefault(camera, {}).setdefault(date_str, {})
        for hour, minutes in hours.items():
            date_data.setdefault(hour, set()).update(minutes)


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def build(recordings_root: Path, last_scan_time: float) -> None:
    """
    Incremental background index build.
    Skips date dirs that are already fully indexed and whose mtime (and all
    hour-subdir mtimes) predate last_scan_time. Scans everything else.
    Sets _indexed=True and persists when done.
    """
    global _indexed, _progress

    if not recordings_root.exists():
        log.warning("Recordings path %s not found; index will be empty", recordings_root)
        _indexed = True
        _progress = 100.0
        return

    try:
        date_dirs = sorted(d for d in recordings_root.iterdir() if d.is_dir())
    except OSError as exc:
        log.error("Cannot list recordings root %s: %s", recordings_root, exc)
        _indexed = True
        _progress = 100.0
        return

    if not date_dirs:
        _indexed = True
        _progress = 100.0
        return

    indexed_dates: set[str] = {date for cam_dates in _index.values() for date in cam_dates}

    if VERBOSE_LOGGING:
        log.info(
            "Index build started: %d date dirs to check (last_scan_time=%.0f)",
            len(date_dirs), last_scan_time,
        )

    for i, date_dir in enumerate(date_dirs):
        date_str = date_dir.name
        try:
            # Rescan if: new date, top-level dir modified, or any hour subdir modified.
            needs_scan = (
                date_str not in indexed_dates
                or date_dir.stat().st_mtime > last_scan_time
                or any(
                    h.stat().st_mtime > last_scan_time
                    for h in date_dir.iterdir()
                    if h.is_dir()
                )
            )
        except OSError:
            needs_scan = True

        if needs_scan:
            if VERBOSE_LOGGING:
                log.info("Index rescan [%.0f%%]: %s", _progress, date_str)
            try:
                partial = await asyncio.to_thread(_scan_date_sync, date_dir, VERBOSE_LOGGING)
                _merge_date(date_str, partial)
            except Exception as exc:
                log.warning("Skipping %s: %s", date_dir.name, exc)
        elif VERBOSE_LOGGING:
            # Throttled heartbeat so the user sees progress even when most dates are cached.
            now = time.monotonic()
            if now - _last_verbose_log >= _VERBOSE_INTERVAL:
                _last_verbose_log = now
                log.info("Index check [%.0f%%]: %s (cached, skipping)", _progress, date_str)

        _progress = (i + 1) / len(date_dirs) * 100.0
        await asyncio.sleep(0)  # yield to event loop between dates

    _indexed = True
    _progress = 100.0
    await asyncio.to_thread(_save_sync, time.time())
    log.info("Index build complete: %d cameras, %d total dates",
             len(_index), sum(len(d) for d in _index.values()))


async def watch(recordings_root: Path) -> None:
    """Watch the recordings directory and update the index as new .mp4 files appear."""
    try:
        from watchfiles import awatch, Change
    except ImportError:
        log.warning("watchfiles not installed; live index updates disabled")
        return

    if not recordings_root.exists():
        log.warning("Recordings path %s not found; watcher not started", recordings_root)
        return

    log.info("Filesystem watcher started on %s", recordings_root)
    try:
        async for changes in awatch(str(recordings_root)):
            updated = False
            for change_type, path_str in changes:
                if change_type not in (Change.added, Change.modified):
                    continue
                path = Path(path_str)
                if path.suffix != ".mp4":
                    continue
                parsed = _parse_segment_path(path, recordings_root)
                if parsed:
                    _add(*parsed)
                    updated = True
            if updated:
                await asyncio.to_thread(_save_sync, time.time())
    except Exception as exc:
        log.error("Filesystem watcher stopped: %s", exc)
