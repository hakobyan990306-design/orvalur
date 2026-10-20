"""Armenian news autoposter: RSS + public Telegram channels -> our Telegram channel.

Runs once per invocation (GitHub Actions cron calls it every ~15 min).
State (already-seen items) lives in state/seen.json and is committed back by the workflow.
"""

import hashlib
import html
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
SOURCES_FILE = ROOT / "sources.json"
STATE_FILE = ROOT / "state" / "seen.json"

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "")  # @mychannel or -100123...
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("MODEL", "claude-opus-5-5")
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"
MAX_POSTS = int(os.environ.get("MAX_POSTS_PER_RUN", "3"))
GAP_MIN = float(os.environ.get("MIN_GAP_MINUTES", "3"))  # pause between posts, randomized up to +2 min
CANDS_PER_SOURCE = 10  # per run, only the newest unposted items of each source are considered
MIN_SCORE = int(os.environ.get("MIN_SCORE", "5"))  # importance 1-10; below this is never posted
PENDING_HOURS = 3  # an important item not posted within this time is dropped as stale
LONG_POST = 280  # stories longer than this (chars) show one sentence and fold the rest
ARMENIA = timezone(timedelta(hours=4))
OPEN_WEEKDAY, OPEN_WEEKEND, CLOSE = 8, 9, 23  # posting hours, Yerevan time


def within_hours():
    now = datetime.now(ARMENIA)
    opens = OPEN_WEEKEND if now.weekday() >= 5 else OPEN_WEEKDAY
    return opens <= now.hour < CLOSE
SIGNATURE = os.environ.get("SIGNATURE", "")  # e.g. "👉 @mychannel"

UA = "Mozilla/5.0 (compatible; HyNewsBot/1.0)"
SEEN_LIMIT = 3000
TITLES_LIMIT = 300

SYSTEM_PROMPT = """Դու հայկական լրատվական Telegram կանալի խմբագիր ես։
Քեզ տրվում է լուր ուրիշ աղբյուրից։ Գրիր ՆՈՐ, կարճ գրառում հայերենով՝ քո բառերով, ոչ թե պատճենով։

Կանոններ.
- Առաջին տողը վերնագիրն է՝ կարճ և հստակ։ Սկզբում դիր էմոջի.
  - եթե լուրը շտապ կամ ծանր է, նախ դիր կարևորության նշան՝ 🔴 (վթար, հրդեհ, մահ, ձերբակալություն, արտակարգ դեպք), ⚡️ (շտապ քաղաքական կամ միջազգային լուր), ❗️ (կարևոր հայտարարություն քաղաքացիների համար՝ անջատումներ, նոր կանոններ, գներ),
  - հետո մեկ թեմատիկ էմոջի (⚽ սպորտ, 🏛 քաղաքականություն, 💰 տնտեսություն, 🚗 ճանապարհ, 🌍 աշխարհ, 🎭 մշակույթ և այլն)։
  - Սովորական լուրի դեպքում միայն թեմատիկ էմոջին։ Մի չափազանցիր, առավելագույնը 2 էմոջի վերնագրում։
- Հետո դատարկ տող և 1–4 նախադասություն՝ միայն փաստերը, որոնք կան լրի մեջ։ Ոչինչ մի հորինիր։ Եթե տրված է միայն վերնագիր, գրիր 1 նախադասություն՝ վերնագրի փաստերով։
- Չեզոք տոն, առանց սեփական կարծիքի և առանց կլիքբեյթի։
- Մի գրիր աղբյուրի անունը կամ հղումը, դրանք ավելացվում են ավտոմատ։ Երբեք մի գրիր «ըստ վերնագրի», «ըստ աղբյուրի», «մանրամասները տեսանյութում» և նման արտահայտություններ. գրիր փաստը ուղղակիորեն։
- Վերջում դատարկ տող և 1–2 հեշթեգ միայն հայերեն տառերով, առանց բացատի (#Հայաստան, #Սպորտ, #ԱրտակարգԴեպք)։
- Ընդհանուր երկարությունը՝ մինչև 700 նիշ։
- Եթե տեքստը լուր չէ (գովազդ, մրցույթ, ուղիղ եթերի հայտարարություն, անհասկանալի), պատասխանիր միայն SKIP բառով։
Պատասխանիր միայն գրառման տեքստով, առանց HTML-ի և markdown-ի։"""


# ---------- helpers ----------

def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def strip_html(s):
    s = re.sub(r"(?i)<br\s*/?>", "\n", s or "")
    s = re.sub(r"(?s)<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def norm_words(title):
    words = re.findall(r"\w+", title.lower())
    return {w for w in words if len(w) > 3}


def is_similar(title, recent_titles):
    a = norm_words(title)
    if len(a) < 3:
        return False
    for t in recent_titles:
        b = norm_words(t)
        if b and len(a & b) / len(a | b) >= 0.5:
            return True
    return False


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"seen": [], "titles": [], "bootstrapped": []}


def save_state(state):
    state["seen"] = state["seen"][-SEEN_LIMIT:]
    state["titles"] = state["titles"][-TITLES_LIMIT:]
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- fetchers ----------

def fetch_rss(src):
    raw = http_get(src["url"]).strip()
    end = raw.rfind("</rss>")
    if end != -1:  # some sites (factor.am) append junk after the feed
        raw = raw[: end + len("</rss>")]
    root = ET.fromstring(raw.encode("utf-8"))
    media_ns = "{http://search.yahoo.com/mrss/}"
    content_ns = "{http://purl.org/rss/1.0/modules/content/}"
    items = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link).strip()
        body = it.findtext(content_ns + "encoded") or it.findtext("description") or ""
        image = None
        m = it.find(media_ns + "content")
        if m is not None:
            image = m.get("url")
        enc = it.find("enclosure")
        if not image and enc is not None and (enc.get("type") or "").startswith("image"):
            image = enc.get("url")
        if not image:
            im = re.search(r'<img[^>]+src="([^"]+)"', body)
            image = im.group(1) if im else None
        items.append({
            "id": f'{src["name"]}:{guid}',
            "title": strip_html(it.findtext("title") or ""),
            "text": strip_html(body)[:3000],
            "link": link,
            "image": image,
        })
    return items


def fetch_telegram(src):
    """Public channels expose a web preview at t.me/s/<channel> - no Telegram account needed."""
    channel = src["channel"].lstrip("@")
    page = http_get(f"https://t.me/s/{channel}")
    items = []
    for block in page.split('<div class="tgme_widget_message_wrap')[1:]:
        post = re.search(r'data-post="([^"]+)"', block)
        if not post:
            continue
        tm = re.search(r'(?s)<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block)
        text = strip_html(tm.group(1)) if tm else ""
        if len(text) < 40:  # photo-only / service messages
            continue
        im = re.search(r"tgme_widget_message_photo_wrap[^>]+background-image:url\('([^']+)'\)", block)
        # Small videos come with a direct file URL; big ones ("Media is too big") only with a thumbnail.
        vid = re.search(r'<video[^>]+src="([^"]+)"', block)
        thumb = re.search(r"tgme_widget_message_video_thumb[^>]+background-image:url\('([^']+)'\)", block)
        dur = re.search(r'message_video_duration[^>]*>([^<]+)<', block)
        items.append({
            "id": f"tg:{post.group(1)}",
            "title": text.split("\n", 1)[0][:200],
            "text": text[:3000],
            "link": f"https://t.me/{post.group(1)}",
            "image": im.group(1) if im else (thumb.group(1) if thumb else None),
            "video": html.unescape(vid.group(1)) if vid else None,
            "video_duration": dur.group(1).strip() if (thumb or vid) and dur else None,
        })
    return items[::-1]  # the page lists oldest first; keep newest first like RSS


def fetch_veolia(src):
    """Water outage notices: the page has one accordion per day, each with several <strong>-titled notices."""
    page = http_get(src["url"])
    items = []
    for day in page.split('<div class="accordion-item"')[1:3]:  # the two newest days
        body = day.split('<div class="wysiwyg">', 1)[-1]
        for chunk in re.split(r"<p>\s*<strong>", body)[1:]:
            title = strip_html(chunk.split("</strong>", 1)[0])
            text = strip_html(chunk.split("</strong>", 1)[-1])
            if not title or "ջրանջատում" not in title.lower():
                continue
            hours = re.search(r"ժամը\s*([\d:]+\s*[-–]\s*[\d:]+)", text)
            where = re.search(r"կդադարեցվի\s+(.+?)\s+ջրամատակարարում", text)
            items.append({
                "id": "veolia:" + hashlib.md5((title + text[:200]).encode()).hexdigest()[:12],
                "title": title,
                "text": text,
                "link": src["url"],
                "image": None,
                "hours": hours.group(1).replace("-", "–").replace(" ", "") if hours else "",
                "where": where.group(1) if where else "",
            })
    return items


def _date_of(text):
    m = re.search(r"(" + "|".join(MONTHS) + r")\s+(\d{1,2})", text)
    return f"{m.group(1)} {m.group(2)}-ին" if m else ""


def fetch_ena(src):
    """Electricity outages (ENA): planned notices separated by ****,
    with region headings followed by "11:00–15:00 addresses" lines."""
    page = http_get(src["url"])
    items = []
    # Only the planned section: the emergency table on that page is not kept up to date.
    planned = page.split('ContentPlaceHolder1_attenbody"', 1)[-1].split("titleVtarayin", 1)[0]
    for section in [planned]:
        text = strip_html(re.sub(r"(?i)</p>", "\n", section))
        for notice in re.split(r"\*{5,}", text):
            date = _date_of(notice)
            region, lines = None, {}
            for line in (l.strip(" ,;") for l in notice.split("\n")):
                if not line:
                    continue
                if re.match(r"^\d{1,2}[:.]\d{2}\s*[–-]", line):
                    if region:
                        lines.setdefault(region, []).append(line)
                elif len(line) < 70 and ("մարզ" in line or "վարչական շրջան" in line):
                    region = line.rstrip("՝: ")
            for region, ls in lines.items():
                where = "; ".join(ls)
                items.append({
                    "id": "ena:" + hashlib.md5((date + region + where[:200]).encode()).hexdigest()[:12],
                    "title": f"{region}, {date}".strip(", "),
                    "text": where, "link": src["url"], "image": None,
                    "hours": "", "where": where[:250] + ("…" if len(where) > 250 else ""),
                })
    return items


def fetch_gas(src):
    """Gas interruptions (Gazprom Armenia): planned and emergency pages, one sentence per notice."""
    items = []
    for url in src["urls"]:
        page = http_get(url)
        body = page.split('class="page_text_cont"', 1)[-1].split('<div id="footer"', 1)[0]
        text = strip_html(re.sub(r"(?i)<br\s*/?>|</div>", "\n", body))
        for notice in re.findall(r"(?s)նպատակով[՝,]?\s*(.+?գազամատակարարումը)", text):
            notice = " ".join(notice.split())
            when = re.match(r"(.+?ժամը\s+[\d:]+[^,]*?)(?:,|\s+կդադարեցվի)", notice)
            where = re.search(r"կդադարեցվի\s+(.+?)\s+(?:գազասպառողների|գազամատակարարումը)", notice)
            items.append({
                "id": "gas:" + hashlib.md5(notice[:300].encode()).hexdigest()[:12],
                "title": (where.group(1) if where else notice[:80]) + ", " + _date_of(notice),
                "text": notice, "link": url, "image": None,
                "hours": when.group(1).strip() if when else "",
                "where": "",
            })
    return items


MONTHS = ["հունվարի", "փետրվարի", "մարտի", "ապրիլի", "մայիսի", "հունիսի",
          "հուլիսի", "օգոստոսի", "սեպտեմբերի", "հոկտեմբերի", "նոյեմբերի", "դեկտեմբերի"]


def outage_is_current(title):
    """True if the notice's date ("հոկտեմբերի 21-ին") is today or later, Yerevan time."""
    m = re.search(r"(" + "|".join(MONTHS) + r")\s+(\d{1,2})", title)
    if not m:
        return True
    today = datetime.now(ARMENIA).date()
    month, day = MONTHS.index(m.group(1)) + 1, int(m.group(2))
    year = today.year + (1 if month < today.month - 6 else 0)  # December notice for January
    try:
        return datetime(year, month, day).date() >= today
    except ValueError:
        return True


KINDS = {  # emoji, headline, "X will be off"
    "water": ("💧", "Ջրանջատումներ", "Ջուր չի լինի"),
    "power": ("💡", "Լույսի անջատումներ", "Լույս չի լինի"),
    "gas": ("🔥", "Գազի անջատումներ", "Գազ չի լինի"),
}


def place_of(text):
    """("Աջափնյակ", True) for a Yerevan district, ("Հրազդան", False) for a town, ("Կոտայքի մարզ", False) else."""
    m = re.search(r"Երևան\S*\s+(?:քաղաքի\s+)?(.+?)\s+վարչական", text)
    if m:
        return m.group(1), True
    m = (re.search(r"մարզի\s+(.+?)\s+(?:քաղաք|գյուղ|համայնք|բնակավայր)", text) or re.search(r"(\S+\s+մարզ)", text))
    return (m.group(1), False) if m else ("", False)


def make_digest(src, new):
    """Several outage notices -> one post, so the channel isn't flooded. None if nothing is current."""
    all_new = new
    current = [it for it in new if outage_is_current(it["title"])]
    if not current:
        return None
    current.sort(key=lambda it: "Երևան" not in it["title"])  # Yerevan first, otherwise page order
    new = current
    lines, yerevan, others = [], [], []
    for it in new[:15]:
        if sum(map(len, lines)) > 3000:  # stay well under Telegram's 4096-char message limit
            break
        t = re.sub(r"^(Վթարային\s+)?ջրանջատում\s*", "", it["title"], flags=re.I).strip()[:140]
        line = f"• <b>{html.escape(t)}</b>" + (f", ժ. {html.escape(it['hours'])}" if it["hours"] else "")
        if it["where"]:
            line += f"\n{html.escape(it['where'][:250])}"
        lines.append(line)
        p, is_yerevan = place_of(t)
        bucket = yerevan if is_yerevan else others
        if p and p not in bucket:
            bucket.append(p)
    emoji, headline, off = KINDS[src.get("kind", "water")]
    where = "; ".join(filter(None, [("Երևան՝ " + ", ".join(yerevan)) if yerevan else "", ", ".join(others[:6])]))
    summary = (f"{off}՝ {where}" if where else f"{headline}՝ {len(new)}") + "։\n👇 Հասցեներն ու ժամերը՝ ներքևում"
    return {
        "id": "digest:" + new[0]["id"],
        "parts": [it["id"] for it in all_new],
        "title": f"{headline}՝ " + "; ".join(it["title"] for it in new[:3]),
        "text": "\n".join(lines),
        "post": f"❗️ {emoji} {headline}\n\n" + summary,  # plain version for logs
        # Short summary visible; the full list is an expandable quote that opens right in the channel.
        "post_html": f"<b>❗️ {emoji} {headline}</b>\n\n{html.escape(summary)}\n\n"
                     f"<blockquote expandable>" + "\n\n".join(lines) + "</blockquote>",
        "link": src.get("url") or new[0]["link"],
        "image": None,
        "score": 10,
        "urgent": True,
    }


FETCHERS = {"rss": fetch_rss, "telegram": fetch_telegram, "veolia": fetch_veolia, "ena": fetch_ena, "gas": fetch_gas}


# ---------- rewrite ----------

_client = None


def ask_claude(system, user, max_tokens=4000):
    """One Claude call; returns the text, or None on error/refusal."""
    global _client
    import anthropic

    if _client is None:
        _client = anthropic.Anthropic()

    kwargs = dict(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": user}],
    )
    try:
        if MODEL.startswith("claude-haiku"):
            resp = _client.messages.create(**kwargs)
        else:
            # Server-side fallback: if the model declines, the API retries on a suitable model in the same call.
            resp = _client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
            )
    except anthropic.APIStatusError as e:
        print(f"  ! Claude API error {e.status_code}: {e.message}")
        return None
    except anthropic.APIConnectionError as e:
        print(f"  ! Claude connection error: {e}")
        return None

    if resp.stop_reason == "refusal":
        return None
    return "".join(b.text for b in resp.content if b.type == "text").strip()


SCORE_PROMPT = """Դու Հայաստանում ապրող լսարանի համար լրատվական կանալի գլխավոր խմբագիր ես։
Քեզ տրվում է համարակալված լուրերի ցուցակ։ Յուրաքանչյուրին տուր կարևորության գնահատական 1-ից 10.
10–9՝ մեծ, շտապ լուր, որը հետաքրքրում է գրեթե բոլորին (մահ կամ զոհեր, խոշոր վթար կամ աղետ, սահման և պատերազմ, կառավարության որոշում, որը վերաբերում է բոլորին, մեծ սկանդալ)
8–7՝ կարևոր ազգային լուր (քաղաքականություն, տնտեսություն, գներ, անջատումներ Երևանում, խոշոր ձերբակալություն, հայկական սպորտի մեծ հաջողություն)
6–5՝ հետաքրքիր լուր, բայց ոչ շտապ (միջազգային կարևոր լուր, մարզային դեպք, մշակույթ)
4–3՝ սովորական կամ նեղ լուր (առօրյա պաշտոնական հանդիպումներ, փոքր տեղական դեպք, արտասահմանյան սպորտի սովորական արդյունք)
2–1՝ անկարևոր (գովազդ, մրցույթ, ուղիղ եթերի հայտարարություն, կրկնություն)
Պարտադիր կանոններ.
- Հայաստանում ցանկացած վթար, հրդեհ, ավտովթար կամ բռնության դեպք, որում կա տուժած, վիրավոր կամ զոհ, ստանում է առնվազն 6, իսկ զոհերի դեպքում՝ առնվազն 8։
- Երևանում կամ ամբողջ Հայաստանում ջրի, լույսի կամ գազի անջատումները ստանում են առնվազն 6։
- Եթե երկու լուր նույն իրադարձության մասին են, երկրորդին տուր 1։
Պատասխանիր միայն տողերով՝ «համար: գնահատական», օրինակ՝
1: 7
2: 3"""


def score_items(cands):
    """Asks Claude to rate each (src, item) 1-10. Returns a list of scores, or None without AI."""
    if not ANTHROPIC_KEY or not cands:
        return None
    lines = [f'{i}. [{src["name"]}] {it["title"]}' for i, (src, it) in enumerate(cands, 1)]
    text = ask_claude(SCORE_PROMPT, "\n".join(lines), max_tokens=2000)
    if not text:
        return None
    scores = [0] * len(cands)
    for n, s in re.findall(r"(?m)^\s*(\d+)\s*[:.)-]\s*(\d+)", text):
        if 1 <= int(n) <= len(cands):
            scores[int(n) - 1] = min(int(s), 10)
    return scores


def rewrite(item, source_name):
    """Returns post text, or None to skip. Without an API key, falls back to title + excerpt."""
    if not ANTHROPIC_KEY:
        title = item["title"]
        body = re.sub(r"https?://\S+", "", item["text"])
        body = re.sub(r"(?m)^.*(Կարդալ|Մանրամասն|→|👉).*$", "", body).strip()  # "read more" lines
        if body.startswith(title):
            body = body[len(title):].strip()
        if len(body) > 400:
            body = body[:400].rsplit(" ", 1)[0] + "…"
        return f"📰 {title}" + (f"\n\n{body}" if body else "")

    text = ask_claude(SYSTEM_PROMPT, f'Աղբյուր՝ {source_name}\nՎերնագիր՝ {item["title"]}\n\nՏեքստ՝\n{item["text"]}')
    if not text or text.upper().startswith("SKIP"):
        return None
    # Keep at most 2 hashtags, skipping very long ones.
    tags = iter([t for t in re.findall(r"#\w+", text) if len(t) <= 20][:2])
    return re.sub(r"#\w+", lambda m: next(tags, ""), text).rstrip(" \n")


# ---------- telegram ----------

def tg_call(method, params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}")


def tg_upload_photo(photo_bytes, params):
    boundary = "----orvalur" + os.urandom(8).hex()
    parts = []
    for k, v in params.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="photo.jpg"\r\n'
                 f"Content-Type: image/jpeg\r\n\r\n".encode() + photo_bytes + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto", data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}")


def framed_photo(item, source_name):
    """Our branded version of the item's photo, or None if it can't be made."""
    if not item["image"]:
        return None
    try:
        import frame
        is_video = item.get("video_duration") is not None and not item.get("video")
        return frame.render_url(item["image"], source_name, item["video_duration"] if is_video else None)
    except Exception as e:
        print(f"  ! frame failed: {e}")
        return None


def publish(text, item, source_name):
    video = item.get("video")
    watch = ""
    if item.get("video_duration") and not video:  # too big to re-post: show the frame and link to it
        watch = f'\n\n▶️ <a href="{html.escape(item["link"])}">Դիտել տեսանյութը ({html.escape(item["video_duration"])})</a>'
    footer = watch + f'\n\n🔗 <a href="{html.escape(item["link"])}">{html.escape(source_name)}</a>'
    if SIGNATURE:
        footer += f"\n{html.escape(SIGNATURE)}"
    head, _, rest = text.strip().partition("\n")
    rest = re.sub(r"\n{3,}", "\n\n", rest.strip())
    plain = f"<b>{html.escape(head.strip())}</b>" + (f"\n\n{html.escape(rest)}" if rest else "")
    body = item.get("post_html") or plain
    tags = " ".join(re.findall(r"#\w+", rest))
    story = re.sub(r"#\w+", "", rest).strip()
    first = re.match(r"(?s)(.+?[։.!?])\s+(.+)", story)
    if not item.get("post_html") and len(story) > LONG_POST and first:
        # Long story: first sentence visible, the rest folds into an expandable quote right in the channel.
        body = (f"<b>{html.escape(head.strip())}</b>\n\n{html.escape(first.group(1))}\n"
                f"<blockquote expandable>{html.escape(first.group(2))}</blockquote>" + (f"\n\n{tags}" if tags else ""))

    def fit(limit):  # never cut inside the HTML tags; fall back to the plain layout when too long
        return body if len(body) + len(footer) <= limit else plain[: limit - len(footer)]

    if DRY_RUN:
        print("----- DRY RUN -----")
        print(text + re.sub(r"<[^>]+>", "", watch) + f'\n\n🔗 {source_name}: {item["link"]}'
              + (f"\n🎬 {video}" if video else "") + (f'\n🖼 {item["image"]}' if item["image"] else ""))
        return True

    if video:
        caption = fit(1024) + footer
        r = tg_call("sendVideo", {"chat_id": CHANNEL, "video": video, "caption": caption,
                                  "parse_mode": "HTML", "supports_streaming": "true"})
        if r.get("ok"):
            return True
        print(f'  ! sendVideo failed ({r.get("description")}), sending the frame instead')

    photo = framed_photo(item, source_name)
    if photo:
        caption = fit(1024) + footer
        r = tg_upload_photo(photo, {"chat_id": CHANNEL, "caption": caption, "parse_mode": "HTML"})
        if r.get("ok"):
            return True
        print(f'  ! framed sendPhoto failed ({r.get("description")}), trying the original photo')

    if item["image"]:
        caption = fit(1024) + footer
        r = tg_call("sendPhoto", {"chat_id": CHANNEL, "photo": item["image"], "caption": caption, "parse_mode": "HTML"})
        if r.get("ok"):
            return True
        print(f'  ! sendPhoto failed ({r.get("description")}), sending text only')

    r = tg_call("sendMessage", {
        "chat_id": CHANNEL,
        "text": fit(4096) + footer,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    })
    if not r.get("ok"):
        print(f'  ! sendMessage failed: {r.get("description")}')
    return bool(r.get("ok"))


# ---------- main ----------

def main():
    if not DRY_RUN and not (BOT_TOKEN and CHANNEL):
        sys.exit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL are required (or set DRY_RUN=1)")

    sources = [s for s in json.loads(SOURCES_FILE.read_text(encoding="utf-8")) if s.get("enabled", True)]

    if os.environ.get("TEST_AI") == "1":  # rewrite each source's newest item and print it; posts nothing
        print(f"model: {MODEL}, key set: {bool(ANTHROPIC_KEY)}")
        fetched = {}
        for src in sources:
            try:
                fetched[src["name"]] = FETCHERS[src["type"]](src)
            except Exception as e:
                print(f'! {src["name"]}: {e}')
        sources = [s for s in sources if fetched.get(s["name"])]
        FETCHERS["rss"] = FETCHERS["telegram"] = lambda s: fetched[s["name"]]  # reuse below
        cands = [(src, it) for src in sources for it in fetched[src["name"]][:5]]
        scores = score_items(cands) or [0] * len(cands)
        print("\n===== importance ranking (newest 5 per source)")
        for sc, (src, it) in sorted(zip(scores, cands), key=lambda x: -x[0]):
            print(f'[{sc:>2}] {"POST" if sc >= MIN_SCORE else "drop"}  {src["name"]}: {it["title"][:80]}')
        for src in sources:
            items = FETCHERS[src["type"]](src)
            if src.get("digest"):
                print(f'\n===== {src["name"]} (digest of {len(items)})')
                d = make_digest(src, items)
                print(d["post_html"] if d else "(nothing current)")
                continue
            print(f'\n===== {src["name"]}: {items[0]["title"][:80]}')
            print(rewrite(items[0], src["name"]) or "(skipped)")
        return

    if not DRY_RUN and not within_hours():
        print("outside posting hours, nothing to do")
        return

    state = load_state()
    seen = set(state["seen"])
    per_source = []

    for src in sources:
        try:
            items = FETCHERS[src["type"]](src)
        except Exception as e:  # one broken source must not stop the others
            print(f'! {src["name"]}: {e}')
            continue
        new = [it for it in items if it["id"] not in seen]
        print(f'{src["name"]}: {len(items)} items, {len(new)} new')

        # First time we see a source: remember its current items without posting, so the channel isn't flooded.
        if src["name"] not in state["bootstrapped"] and not DRY_RUN and not src.get("digest"):
            state["bootstrapped"].append(src["name"])
            state["seen"].extend(it["id"] for it in new)
            seen.update(it["id"] for it in new)
            continue
        if src.get("digest") and new:
            digest = make_digest(src, new)
            if not digest:  # only past notices
                state["seen"].extend(it["id"] for it in new)
            new = [digest] if digest else []
        # Newest first; anything older than the newest few is dropped so we never post stale news.
        for it in new[CANDS_PER_SOURCE:]:
            state["seen"].append(it["id"])
        per_source.append([(src, it) for it in new[:CANDS_PER_SOURCE]])

    # Round-robin across sources so one busy source doesn't fill the whole run.
    queue = []
    while any(per_source):
        for lst in per_source:
            if lst:
                queue.append(lst.pop(0))

    # Rank by importance. Each item is scored once; a good one may wait a few runs for its turn,
    # weak or stale ones are dropped.
    def ids(it):  # a digest stands for several source items
        return [it["id"], *it.get("parts", [])]

    pending = state.setdefault("pending", {})
    now = time.time()
    fresh = [(s, it) for s, it in queue if it["id"] not in pending]
    scores = score_items(fresh)
    if scores is not None:
        for (s, it), sc in zip(fresh, scores):
            pending[it["id"]] = {"score": it.get("score", sc), "since": now}
        ranked = []
        for s, it in queue:
            p = pending[it["id"]]
            if p["score"] < MIN_SCORE or now - p["since"] > PENDING_HOURS * 3600:
                state["seen"].extend(ids(it))
                pending.pop(it["id"])
            else:
                ranked.append((s, it))
        ranked.sort(key=lambda c: -pending[c[1]["id"]]["score"])  # stable: ties keep round-robin order
        for s, it in ranked[:8]:
            print(f'  [{pending[it["id"]]["score"]}] {s["name"]}: {it["title"][:70]}')
        queue = ranked
    else:
        for lst_item in queue[MAX_POSTS * 3:]:  # no AI: plain round-robin of the newest
            state["seen"].extend(ids(lst_item[1]))
        queue = queue[: MAX_POSTS * 3]

    posted = 0
    per_src_count = {}
    for src, item in queue:
        if posted >= MAX_POSTS:
            break  # left unseen: competes again next run
        if per_src_count.get(src["name"], 0) >= 2:
            continue  # variety: at most 2 posts per source per run
        state["seen"].extend(ids(item))
        pending.pop(item["id"], None)
        if "post" not in item and is_similar(item["title"], state["titles"]):
            print(f'  = duplicate: {item["title"][:70]}')
            continue
        print(f'  > {src["name"]}: {item["title"][:70]}')
        text = item.get("post") or rewrite(item, src["name"])
        if not text:
            print("    skipped")
            continue
        if not DRY_RUN and not item.get("urgent"):  # urgent (water outages) goes out right away
            # Space posts out, also across runs, so the channel doesn't get a burst.
            wait = GAP_MIN * 60 + random.uniform(0, 120) - (time.time() - state.get("last_post", 0))
            if wait > 0:
                print(f"    waiting {wait / 60:.1f} min")
                time.sleep(wait)
            if not within_hours():  # the wait may have crossed closing time
                for x in ids(item):  # keep it for tomorrow morning
                    state["seen"].remove(x)
                break
        if publish(text, item, src["name"]):
            state["titles"].append(item["title"])
            state["last_post"] = time.time()
            posted += 1
            per_src_count[src["name"]] = per_src_count.get(src["name"], 0) + 1
            if not DRY_RUN:
                save_state(state)  # so a cancelled run never re-posts

    seen_now = set(state["seen"])
    for k in [k for k, p in pending.items() if k in seen_now or now - p["since"] > PENDING_HOURS * 3600]:
        pending.pop(k)
    if not DRY_RUN:
        save_state(state)
    print(f"done: {posted} posted, {len(pending)} waiting")


if __name__ == "__main__":
    main()
