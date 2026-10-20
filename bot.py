"""Armenian news autoposter: RSS + public Telegram channels -> our Telegram channel.

Runs once per invocation (GitHub Actions cron calls it every ~15 min).
State (already-seen items) lives in state/seen.json and is committed back by the workflow.
"""

import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).parent
SOURCES_FILE = ROOT / "sources.json"
STATE_FILE = ROOT / "state" / "seen.json"

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "")  # @mychannel or -100123...
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("MODEL", "claude-opus-5-5")
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"
MAX_POSTS = int(os.environ.get("MAX_POSTS_PER_RUN", "4"))
SIGNATURE = os.environ.get("SIGNATURE", "")  # e.g. "👉 @mychannel"

UA = "Mozilla/5.0 (compatible; HyNewsBot/1.0)"
SEEN_LIMIT = 3000
TITLES_LIMIT = 300

SYSTEM_PROMPT = """Դու հայկական լրատվական Telegram կանալի խմբագիր ես։
Քեզ տրվում է լուր ուրիշ աղբյուրից։ Գրիր ՆՈՐ, կարճ գրառում հայերենով՝ քո բառերով, ոչ թե պատճենով։

Կանոններ.
- Առաջին տողը՝ մեկ համապատասխան էմոջի և կարճ, հստակ վերնագիր։
- Հետո դատարկ տող և 2–4 նախադասություն՝ միայն փաստերը, որոնք կան լրի մեջ։ Ոչինչ մի հորինիր։
- Չեզոք տոն, առանց սեփական կարծիքի և առանց կլիքբեյթի։
- Մի գրիր աղբյուրի անունը կամ հղումը, դրանք ավելացվում են ավտոմատ։
- Վերջում 1–2 հեշթեգ (#Հայաստան, #Սպորտ, #Երևան և այլն)։
- Ընդհանուր երկարությունը՝ մինչև 700 նիշ։
- Եթե տեքստը լուր չէ (գովազդ, մրցույթ, ուղիղ եթերի հայտարարություն, դատարկ կամ անհասկանալի), պատասխանիր միայն SKIP բառով։
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
        items.append({
            "id": f"tg:{post.group(1)}",
            "title": text.split("\n", 1)[0][:200],
            "text": text[:3000],
            "link": f"https://t.me/{post.group(1)}",
            "image": im.group(1) if im else None,
        })
    return items[::-1]  # the page lists oldest first; keep newest first like RSS


FETCHERS = {"rss": fetch_rss, "telegram": fetch_telegram}


# ---------- rewrite ----------

_client = None


def rewrite(item, source_name):
    """Returns post text, or None to skip. Without an API key, falls back to title + excerpt."""
    if not ANTHROPIC_KEY:
        title = item["title"]
        body = re.sub(r"https?://\S+", "", item["text"]).strip()
        if body.startswith(title):
            body = body[len(title):].strip()
        if len(body) > 400:
            body = body[:400].rsplit(" ", 1)[0] + "…"
        return f"📰 {title}" + (f"\n\n{body}" if body else "")

    global _client
    import anthropic

    if _client is None:
        _client = anthropic.Anthropic()

    kwargs = dict(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        output_config={"effort": "low"},
        messages=[{
            "role": "user",
            "content": f'Աղբյուր՝ {source_name}\nՎերնագիր՝ {item["title"]}\n\nՏեքստ՝\n{item["text"]}',
        }],
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
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not text or text.upper().startswith("SKIP"):
        return None
    return text


# ---------- telegram ----------

def tg_call(method, params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}")


def publish(text, item, source_name):
    footer = f'\n\n🔗 <a href="{html.escape(item["link"])}">{html.escape(source_name)}</a>'
    if SIGNATURE:
        footer += f"\n{html.escape(SIGNATURE)}"
    body = html.escape(text)

    if DRY_RUN:
        print("----- DRY RUN -----")
        print(text + f'\n\n🔗 {source_name}: {item["link"]}' + (f'\n🖼 {item["image"]}' if item["image"] else ""))
        return True

    if item["image"]:
        caption = body[: 1024 - len(footer)] + footer
        r = tg_call("sendPhoto", {"chat_id": CHANNEL, "photo": item["image"], "caption": caption, "parse_mode": "HTML"})
        if r.get("ok"):
            return True
        print(f'  ! sendPhoto failed ({r.get("description")}), sending text only')

    r = tg_call("sendMessage", {
        "chat_id": CHANNEL,
        "text": body[: 4096 - len(footer)] + footer,
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
        if src["name"] not in state["bootstrapped"] and not DRY_RUN:
            state["bootstrapped"].append(src["name"])
            state["seen"].extend(it["id"] for it in new)
            seen.update(it["id"] for it in new)
            continue
        per_source.append([(src, it) for it in new])  # newest first

    # Round-robin across sources so one busy source doesn't fill the whole run.
    queue = []
    while any(per_source):
        for lst in per_source:
            if lst:
                queue.append(lst.pop(0))

    posted = 0
    for src, item in queue:
        state["seen"].append(item["id"])
        if posted >= MAX_POSTS:
            continue  # drop the backlog so the channel never posts stale news
        if is_similar(item["title"], state["titles"]):
            print(f'  = duplicate: {item["title"][:70]}')
            continue
        print(f'  > {src["name"]}: {item["title"][:70]}')
        text = rewrite(item, src["name"])
        if not text:
            print("    skipped")
            continue
        if publish(text, item, src["name"]):
            state["titles"].append(item["title"])
            posted += 1
            time.sleep(3)

    if not DRY_RUN:
        save_state(state)
    print(f"done: {posted} posted, {len(queue) - posted} skipped")


if __name__ == "__main__":
    main()
