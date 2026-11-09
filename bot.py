#!/usr/bin/env python3
"""
Новостной Telegram-бот: Investing.com, Reuters, Yahoo Finance.
Присылает каждую новую новость: заголовок, краткое описание, источник, время, ссылку.
Без внешних библиотек, нужен только Python 3.9+.

Запуск:
  python bot.py          — работает постоянно, проверяет каждые INTERVAL_SEC секунд
  python bot.py --once   — одна проверка и выход (для GitHub Actions / cron)

Переменные окружения:
  TG_TOKEN       — токен бота от @BotFather (обязательно)
  TG_CHAT_ID     — ваш chat_id (обязательно)
  INTERVAL_SEC   — интервал проверки, по умолчанию 300 (5 минут)
  ONLY_KEYWORDS  — "1", чтобы присылать только новости с ключевыми словами
  STATE_FILE     — файл с уже отправленными новостями, по умолчанию seen.json
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

TOKEN = os.environ.get("TG_TOKEN", "")
CHAT_ID = os.environ.get("TG_CHAT_ID", "")
INTERVAL = int(os.environ.get("INTERVAL_SEC", "300"))
ONLY_KEYWORDS = os.environ.get("ONLY_KEYWORDS", "") == "1"
STATE_FILE = os.environ.get("STATE_FILE", "seen.json")
LOCAL_TZ = timezone(timedelta(hours=5))  # Алматы
MAX_PER_CYCLE = 30   # защита от спама, если источник выдаст сразу много
DESC_LIMIT = 350     # длина описания в символах

REUTERS_QUERY = "site:reuters.com (markets OR business OR economy OR stocks) when:1d"
FEEDS = [
    ("Investing", "https://www.investing.com/rss/news.rss"),
    # У Reuters нет публичного RSS с 2020 года, поэтому берём их статьи через Google News
    ("Reuters", "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": REUTERS_QUERY, "hl": "en-US", "gl": "US", "ceid": "US:en"})),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
]

# Новость с этими словами помечается 🔴 (и только такие приходят, если ONLY_KEYWORDS=1)
KEYWORDS = [
    "fed", "fomc", "powell", "rate cut", "rate hike", "interest rate", "cpi", "ppi",
    "inflation", "payroll", "jobs report", "gdp", "recession", "treasury", "yield",
    "boj", "bank of japan", "ecb", "korea", "kospi", "samsung", "sk hynix", "hyundai",
    "kazakhstan", "tenge", "kaspi", "kazatomprom", "oil", "opec", "brent", "uranium",
    "tariff", "sanction", "default", "earnings", "guidance", "nvidia",
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def child(el, *names):
    for c in el:
        if c.tag.split("}")[-1] in names:
            return c
    return None


def parse_date(s):
    if not s:
        return None
    try:
        return parsedate_to_datetime(s)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def parse_feed(data):
    root = ET.fromstring(data)
    items = []
    for it in root.iter():
        if it.tag.split("}")[-1] not in ("item", "entry"):
            continue
        t = child(it, "title")
        title = clean(t.text if t is not None else "")
        l = child(it, "link")
        link = ""
        if l is not None:
            link = (l.text or l.get("href") or "").strip()
        d = child(it, "description", "summary")
        desc = clean(d.text if d is not None else "")
        g = child(it, "guid", "id")
        uid = (g.text.strip() if g is not None and g.text else "") or link or title
        p = child(it, "pubDate", "published", "updated")
        date = parse_date(p.text if p is not None else "")
        if title and link:
            items.append({"id": uid, "title": title, "link": link, "desc": desc, "date": date})
    return items


def page_description(url):
    """Если в RSS нет описания — берём og:description со страницы статьи."""
    if "news.google.com" in url:
        return ""
    try:
        page = fetch(url, timeout=10)[:400000].decode("utf-8", "ignore")
    except Exception:
        return ""
    for pat in (
        r'<meta[^>]+(?:property|name)=["\'](?:og:description|description)["\'][^>]*content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:og:description|description)',
    ):
        m = re.search(pat, page, re.I)
        if m:
            return clean(m.group(1))
    return ""


def tidy(source, item):
    title = item["title"]
    if source == "Reuters":
        title = re.sub(r"\s+-\s+Reuters$", "", title)
    desc = item["desc"]
    # В Google News описание — это просто заголовок ещё раз, оно бесполезно
    if desc and (desc.lower().startswith(title[:40].lower()) or source == "Reuters"):
        desc = ""
    if not desc:
        desc = page_description(item["link"])
    if len(desc) > DESC_LIMIT:
        desc = desc[:DESC_LIMIT].rsplit(" ", 1)[0] + "…"
    item["title"], item["desc"] = title, desc
    return item


def is_important(item):
    text = f" {item['title']} {item['desc']} ".lower()
    return any(re.search(r"\b" + re.escape(k) + r"\b", text) for k in KEYWORDS)


def esc(s):
    return html.escape(s, quote=False)


def format_message(source, item, important):
    parts = [("🔴 " if important else "") + f"<b>{esc(item['title'])}</b>"]
    if item["desc"]:
        parts.append(esc(item["desc"]))
    meta = source
    if item["date"]:
        d = item["date"]
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        meta += " · " + d.astimezone(LOCAL_TZ).strftime("%d.%m %H:%M")
    parts.append(f"<i>{esc(meta)}</i>")
    parts.append(f'<a href="{html.escape(item["link"])}">Открыть статью</a>')
    return "\n\n".join(parts)


def send(text, retry=True):
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        if e.code == 429 and retry:
            wait = json.loads(body).get("parameters", {}).get("retry_after", 5)
            time.sleep(wait + 1)
            return send(text, retry=False)
        print(f"Telegram error {e.code}: {body}", file=sys.stderr)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seen": {}, "feeds": []}


def save_state(state):
    seen = state["seen"]
    if len(seen) > 5000:  # храним только последние 5000
        state["seen"] = dict(sorted(seen.items(), key=lambda kv: kv[1])[-5000:])
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def check(state):
    now = int(time.time())
    queue = []
    for source, url in FEEDS:
        try:
            items = parse_feed(fetch(url))
        except Exception as e:
            print(f"[{source}] не удалось загрузить: {e}", file=sys.stderr)
            continue
        first_time = source not in state["feeds"]
        for it in items:
            key = f"{source}|{it['id']}"
            if key in state["seen"]:
                continue
            state["seen"][key] = now
            if not first_time:  # при первом запуске не заваливаем старыми новостями
                queue.append((source, it))
        if first_time:
            state["feeds"].append(source)
            print(f"[{source}] подключён, в ленте {len(items)} новостей")

    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    queue.sort(key=lambda x: x[1]["date"] or epoch)
    sent = 0
    for source, it in queue[-MAX_PER_CYCLE:]:
        it = tidy(source, it)
        important = is_important(it)
        if ONLY_KEYWORDS and not important:
            continue
        send(format_message(source, it, important))
        sent += 1
        time.sleep(1.1)
    print(f"{datetime.now(LOCAL_TZ):%H:%M} новых: {len(queue)}, отправлено: {sent}")


def main():
    if not TOKEN or not CHAT_ID:
        sys.exit("Укажите TG_TOKEN и TG_CHAT_ID в переменных окружения")
    state = load_state()
    if not state["feeds"]:
        send("✅ Новостной бот запущен: Investing, Reuters, Yahoo Finance. "
             "Буду присылать новые новости по мере выхода.")
    while True:
        check(state)
        save_state(state)
        if "--once" in sys.argv:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
