"""Smoke test: render a short timelapse and extract a thumbnail."""

import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.recordings import find_segments
from app.render import create_job, extract_thumbnail, Status

TZ = ZoneInfo("America/Los_Angeles")
RECORDINGS = "./test-data/recordings"
OUTPUT = Path("./test-data/output")
OUTPUT.mkdir(parents=True, exist_ok=True)


async def main() -> None:
    # --- Thumbnail ---
    start = datetime(2026, 6, 7, 10, 5, 0, tzinfo=TZ)
    end   = datetime(2026, 6, 7, 10, 5, 30, tzinfo=TZ)
    segs  = find_segments("frontdoor", start, end, RECORDINGS)
    if segs:
        seg = segs[0]
        jpeg = await extract_thumbnail(seg, offset=0.0)
        out_jpg = OUTPUT / "thumb_test.jpg"
        out_jpg.write_bytes(jpeg)
        print(f"Thumbnail: {len(jpeg)} bytes → {out_jpg}")
    else:
        print("No segments found for thumbnail test")

    # --- Timelapse (5 minutes at 10×) ---
    job = create_job(
        camera="frontdoor",
        start=datetime(2026, 6, 7, 10, 0, 0, tzinfo=TZ),
        end=datetime(2026, 6, 7, 10, 5, 0, tzinfo=TZ),
        speed=10.0,
        output_dir=OUTPUT,
        recordings_root=RECORDINGS,
        name="frontdoor_test",
    )
    print(f"\nJob {job.id} created → {job.output_path.name}")

    while job.status not in (Status.COMPLETE, Status.ERROR):
        await asyncio.sleep(0.5)
        print(f"  progress: {job.progress:.0%}  status: {job.status}", end="\r")

    print()
    if job.status == Status.COMPLETE:
        size_mb = job.output_path.stat().st_size / 1_048_576
        print(f"Done. Output: {job.output_path}  ({size_mb:.1f} MB)")
    else:
        print(f"Error: {job.error}")


asyncio.run(main())
