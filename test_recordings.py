"""Quick smoke test for recordings.py against real test data."""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.recordings import find_segments, list_cameras

TZ = ZoneInfo("America/Los_Angeles")
RECORDINGS = "./test-data/recordings"

cameras = list_cameras(RECORDINGS)
print(f"Cameras found: {cameras}")

start = datetime(2026, 6, 7, 10, 0, 0, tzinfo=TZ)
end   = datetime(2026, 6, 7, 10, 30, 0, tzinfo=TZ)

segments = find_segments("frontdoor", start, end, RECORDINGS)
print(f"\nSegments for frontdoor 10:00–10:30 ({len(segments)} total):")
for seg in segments:
    print(f"  {seg.start.strftime('%H:%M:%S')} → {seg.end.strftime('%H:%M:%S')}  {seg.path.name}")
