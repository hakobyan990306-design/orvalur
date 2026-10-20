"""Puts a source photo into the channel frame: black bar with our logo and photo credit,
plus an orange play button when the photo is a video frame."""

import io
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parent
LOGO_FILE = ROOT / "brand" / "orvalur-logo-dark.png"
FONT_FILES = [  # DejaVu ships with Ubuntu runners and has Armenian glyphs
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
]

WIDTH = 1280
INK = (17, 17, 17)
ORANGE = (242, 168, 0)
BAR = ORANGE
CREDIT = (70, 45, 0)
UA = "Mozilla/5.0 (compatible; HyNewsBot/1.0)"

_logo = None


def _font(size):
    for f in FONT_FILES:
        if Path(f).exists():
            return ImageFont.truetype(f, size)
    return ImageFont.load_default(size)


def _load_logo():
    global _logo
    if _logo is None:
        img = Image.open(LOGO_FILE).convert("RGBA")
        img = img.crop(img.getbbox())  # the PNG has wide transparent margins
        # The orange dot would vanish on the orange bar, so make it white.
        px = img.load()
        for y in range(img.height):
            for x in range(img.width):
                r, g, b, a = px[x, y]
                if a and r > 150 and r - b > 100:
                    px[x, y] = (255, 255, 255, a)
        _logo = img
    return _logo


def _fit(img):
    """Scale to WIDTH and center-crop extreme shapes to between 4:5 (tall) and 16:9 (wide)."""
    w, h = img.size
    ratio = w / h
    if ratio < 4 / 5:
        nh = int(w * 5 / 4)
        top = (h - nh) // 2
        img = img.crop((0, top, w, top + nh))
    elif ratio > 16 / 9:
        nw = int(h * 16 / 9)
        left = (w - nw) // 2
        img = img.crop((left, 0, left + nw, h))
    w, h = img.size
    return img.resize((WIDTH, round(h * WIDTH / w)), Image.LANCZOS)


def _play_button(draw, cx, cy, r):
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=ORANGE)
    t = r * 0.42
    draw.polygon([(cx - t * 0.75, cy - t), (cx - t * 0.75, cy + t), (cx + t * 1.05, cy)], fill=INK)


def render(photo_bytes, credit, video_duration=None):
    """Returns JPEG bytes of the framed photo."""
    photo = _fit(Image.open(io.BytesIO(photo_bytes)).convert("RGB"))
    w, h = photo.size
    bar_h = 120
    out = Image.new("RGB", (w, h + bar_h), BAR)
    out.paste(photo, (0, 0))
    draw = ImageDraw.Draw(out)

    if video_duration is not None:
        _play_button(draw, w // 2, h // 2, 70)
        if video_duration:
            f = _font(30)
            tw = draw.textlength(video_duration, font=f)
            x1, y1 = w - 24, h - 24
            draw.rounded_rectangle((x1 - tw - 28, y1 - 48, x1, y1), radius=10, fill=INK)
            draw.text((x1 - tw - 14, y1 - 42), video_duration, font=f, fill=(255, 255, 255))

    logo = _load_logo()
    lh = 54
    logo = logo.resize((round(logo.width * lh / logo.height), lh), Image.LANCZOS)
    out.paste(logo, (36, h + (bar_h - lh) // 2 + 4), logo)

    if credit:
        f = _font(26)
        text = f"Լուսանկար՝ {credit}"
        tw = draw.textlength(text, font=f)
        draw.text((w - 36 - tw, h + bar_h // 2), text, font=f, fill=CREDIT, anchor="lm")

    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=88, optimize=True)
    return buf.getvalue()


def render_url(url, credit, video_duration=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        return render(r.read(), credit, video_duration)
