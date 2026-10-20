"""Renders a few framed samples from live sources into brand/preview/ to check the design."""

import json
from pathlib import Path

import bot
import frame

out = Path(__file__).parent / "brand" / "preview"
out.mkdir(parents=True, exist_ok=True)
sources = {s["name"]: s for s in json.loads(bot.SOURCES_FILE.read_text(encoding="utf-8"))}

samples = []
for name in ["Shamshyan.com", "News.am", "Hetq", "SportToday (FastSport)"]:
    src = sources[name]
    items = [it for it in bot.FETCHERS[src["type"]](src) if it["image"]]
    videos = [it for it in items if it.get("video_duration")]
    photos = [it for it in items if not it.get("video_duration")]
    samples += [(name, it) for it in photos[:1] + videos[:1]]

for i, (name, it) in enumerate(samples, 1):
    is_video = it.get("video_duration") is not None
    try:
        data = frame.render_url(it["image"], name, it["video_duration"] if is_video else None)
    except Exception as e:
        print(f"{name}: {e}")
        continue
    p = out / f"{i}-{'video' if is_video else 'photo'}.jpg"
    p.write_bytes(data)
    print(p.name, name, it["title"][:60])
