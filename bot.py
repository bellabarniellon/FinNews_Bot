#!/usr/bin/env python3
"""
Vitis News Bot v3 — новости по трём каналам: 🇰🇿 Vitis KZ · 🇰🇷 Vitis Korea · 🇺🇸 Vitis US

Что умеет:
  • Делит новости по странам и шлёт каждую в свой канал
  • Пропускает только НОВОСТИ про компании и макро (без колонок, мнений, советов, «топ-5 акций»)
  • Переводит на русский: заголовок + суть 2–3 предложения
  • Прикладывает картинку из самой статьи (если есть)
  • Оценка важности 1–5, склейка дублей, 📌 watchlist, тихие часы, дайджесты, календарь

Без внешних библиотек, Python 3.9+.
  python bot.py --once         — одна проверка
  python bot.py --minutes 45   — проверять каждые INTERVAL_SEC в течение 45 минут (GitHub Actions)

Секреты:
  TG_TOKEN                     — токен бота (обязательно)
  TG_CHAT_KZ / TG_CHAT_KR / TG_CHAT_US — каналы (@имя или -100...; можно несколько через запятую)
                                 или темы одной группы: -100XXXXXXXXXX:номер_темы
  TG_CHAT_ID                   — личный чат для служебных сообщений; если канал не задан, его новости идут сюда
  GEMINI_API_KEY               — бесплатный ИИ (или ANTHROPIC_API_KEY — Claude)
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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

# ════════════════════ НАСТРОЙКИ — меняй здесь ════════════════════

WATCHLIST = {
    # Корея
    "Samsung": ["samsung"],
    "SK Hynix": ["sk hynix", "hynix"],
    "Hyundai": ["hyundai", "kia"],
    "KB Financial": ["kb financial"],
    "Shinhan": ["shinhan"],
    "Woori": ["woori"],
    "POSCO": ["posco"],
    "KT / SK Telecom": ["sk telecom", "kt corp"],
    "LG Display": ["lg display"],
    "KEPCO": ["kepco", "korea electric power"],
    "Coupang": ["coupang"],
    # Казахстан
    "Kaspi": ["kaspi"],
    "Kazatomprom": ["kazatomprom"],
    "Halyk": ["halyk"],
    "KazMunayGas": ["kazmunaygas", "kmg"],
    "KEGOC": ["kegoc"],
    "Air Astana": ["air astana"],
    "KazTransOil": ["kaztransoil"],
    "MREK": ["mrek"],
}

MIN_SCORE = int(os.environ.get("MIN_SCORE", "3"))  # с какой оценки присылать сразу
DROP_BELOW = 2                 # ниже — не присылать вообще
QUIET_FROM, QUIET_TO = 23, 8   # тихие часы по Алматы
DIGEST_HOURS = [8, 19]
CALENDAR_REMIND_MIN = 60
SEND_IMAGES = True

# Каналы
REGIONS = {
    "KZ": {"flag": "🇰🇿", "name": "Vitis KZ", "env": "TG_CHAT_KZ"},
    "KR": {"flag": "🇰🇷", "name": "Vitis Korea", "env": "TG_CHAT_KR"},
    "US": {"flag": "🇺🇸", "name": "Vitis US", "env": "TG_CHAT_US"},
}

# Календарь Forex Factory: страна → канал (глобальная макро идёт в US)
CALENDAR_COUNTRIES = {"USD": "US", "EUR": "US", "JPY": "US", "CNY": "US"}

# Свои события: (дата время по Алматы, название, канал)
CUSTOM_EVENTS = [
    ("2026-11-26 06:00", "Банк Кореи: решение по ставке (≈10:00 KST)", "KR"),
    ("2026-12-04 12:00", "НБК: решение по базовой ставке", "KZ"),
    # Графики НБК и Банка Кореи на 2027 год ещё не опубликованы — добавить, когда выйдут
]

MODEL = "claude-haiku-5-5"
# Модели Gemini по очереди: если первая недоступна (404) или исчерпан лимит (429), берётся следующая
GEMINI_MODELS = [m for m in [os.environ.get("GEMINI_MODEL", "")] if m] + [
    "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-flash-lite-latest",
    "gemini-2.5-flash-lite", "gemini-flash-latest"]
LOCAL_TZ = timezone(timedelta(hours=5))

# ═════════════════════════════════════════════════════════════════

TOKEN = os.environ.get("TG_TOKEN", "")
PERSONAL = [c.strip() for c in os.environ.get("TG_CHAT_ID", "").split(",") if c.strip()]
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
AI_ON = bool(GEMINI_KEY or API_KEY)
INTERVAL = int(os.environ.get("INTERVAL_SEC", "300"))
STATE_FILE = os.environ.get("STATE_FILE", "seen.json")
MAX_PER_CYCLE = 150
AI_CHUNK = 10          # маленькие пачки — ответ ИИ не обрезается
MAX_AI_RETRIES = 3     # сколько раз пытаться оценить новость, если ИИ не ответил
DESC_LIMIT = 600


def chats_for(region):
    raw = os.environ.get(REGIONS[region]["env"], "")
    ids = [c.strip() for c in raw.split(",") if c.strip()]
    return ids or PERSONAL


def gnews(query, lang="en", country="US"):
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, "hl": f"{lang}-{country}", "gl": country, "ceid": f"{country}:{lang}"})


# (название, канал по умолчанию, [адреса]). ИИ может перекинуть новость в другой канал,
# если она про другую страну (напр. Reuters про Samsung → Korea).
FEEDS = [
    # ─── 🇺🇸 US / глобальное ───
    ("Reuters", "US", [gnews("site:reuters.com (markets OR business OR economy OR stocks) when:1d")]),
    ("WSJ", "US", [
        "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",
        "https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness",
        "https://feeds.content.dowjones.io/public/rss/RSSWSJD",
        gnews("site:wsj.com (markets OR economy OR business) when:1d"),
    ]),
    ("MarketWatch", "US", ["https://feeds.content.dowjones.io/public/rss/mw_topstories"]),
    ("Yahoo Finance", "US", [
        "https://finance.yahoo.com/rss/topstories",
        "https://finance.yahoo.com/news/rss",
        gnews("site:finance.yahoo.com when:1d"),
    ]),
    ("CNBC", "US", [
        "https://www.cnbc.com/id/20910258/device/rss/rss.html",
        "https://www.cnbc.com/id/15839069/device/rss/rss.html",
        "https://www.cnbc.com/id/10001147/device/rss/rss.html",
        gnews("site:cnbc.com (markets OR economy OR stocks) when:1d"),
    ]),
    ("Investing", "US", ["https://www.investing.com/rss/news.rss"]),
    ("The Economist", "US", [
        "https://www.economist.com/finance-and-economics/rss.xml",
        "https://www.economist.com/business/rss.xml",
    ]),
    # ─── 🇰🇷 Korea ───
    ("Yonhap", "KR", [
        "https://en.yna.co.kr/RSS/economy.xml",
        "https://en.yna.co.kr/RSS/market.xml",
        gnews("site:en.yna.co.kr (economy OR market OR stocks OR company) when:1d"),
    ]),
    ("Korea Herald", "KR", [gnews("site:koreaherald.com (business OR economy OR market) when:1d")]),
    ("KED Global", "KR", [gnews("site:kedglobal.com when:1d")]),
    ("JoongAng Daily", "KR", [gnews("site:koreajoongangdaily.joins.com (business OR economy) when:1d")]),
    ("BusinessKorea", "KR", [gnews("site:businesskorea.co.kr when:1d")]),
    ("SBS Biz", "KR", [gnews("site:biz.sbs.co.kr when:1d", "ko", "KR")]),
    ("Reuters Korea", "KR", [gnews("site:reuters.com (Korea OR KOSPI OR Samsung OR Hynix OR Hyundai) when:1d")]),
    ("CNBC Asia", "KR", ["https://www.cnbc.com/id/19832390/device/rss/rss.html"]),
    # ─── 🇰🇿 Kazakhstan ───
    ("KASE", "KZ", [
        "html:https://kase.kz/ru/information/news/all",
        gnews("site:kase.kz when:2d", "ru", "KZ"),
    ]),
    ("AIX", "KZ", [
        "https://aix.kz/feed/",
        gnews("site:aix.kz when:3d"),
    ]),
    ("Kursiv", "KZ", ["https://kz.kursiv.media/feed/", gnews("site:kz.kursiv.media when:1d", "ru", "KZ")]),
    ("Kapital.kz", "KZ", ["https://kapital.kz/rss", gnews("site:kapital.kz when:1d", "ru", "KZ")]),
    ("Reuters KZ", "KZ", [gnews("site:reuters.com (Kazakhstan OR tenge OR Kazatomprom OR Kaspi) when:2d")]),
]
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Отсекаются ещё до ИИ: рутина бирж и явно не-новости
SKIP_PATTERNS = [
    r"закрылись торги следующими инструментами",
    r"(приостановлены|возобновлены) торги .{0,80}по операциям репо",
    r"специализированные торги по (до)?размещению",
    r"параметры спроса и результаты",
    r"изменени\w+ в регламент\w* торгов",
    r"weekly market update",
    r"\b(\d+|three|five|top) (stocks?|reasons|things) (to|you|that)\b",
    r"\bshould you (buy|sell)\b", r"\bbest (stocks?|etfs?|credit cards?)\b",
    r"\bretire(ment)?\b.*\b(save|savings|401\(k\)|social security)\b",
    r"\bmortgage rates? today\b", r"\bhoroscope\b",
]
SKIP_URL = ["/personal-finance/", "/opinion/", "/opinions/", "/video/", "/videos/", "/sponsored",
            "/lifestyle/", "/sports/", "/podcast", "fool.com", "/live-blog", "/review/"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

SYSTEM_PROMPT = """Ты — редактор новостной ленты для портфельного аналитика инвестфонда в Алматы. \
Она ведёт фонд корейских акций (Vitis Korea Fund) и следит за Казахстаном (KASE, AIX, тенге, \
нефть, уран) и США/глобальными рынками (Fed, CPI, крупный тех).

Нужны ТОЛЬКО НОВОСТИ про компании и макро: события, решения, отчёты, сделки, данные, \
движения рынков. НЕ нужны: колонки и мнения, аналитические эссе и разборы «почему», \
советы инвесторам, «какие акции купить», личные финансы, обзоры продуктов, лайфстайл, \
интервью без новостей, промо, спорт, новинки и рестайлинг моделей авто/гаджетов, \
фото и слухи о продуктах без финансового эффекта для компании.

Для КАЖДОЙ новости верни объект JSON:
- "i": номер
- "kind": "company" (новость компании), "macro" (экономика, центробанки, данные, \
политика с влиянием на экономику), "market" (движения индексов, валют, сырья, облигаций), \
или "other" (всё, что не новость — такое мы НЕ публикуем)
- "region": о какой стране новость (не где опубликована!): "KZ" (Казахстан/Центральная Азия), \
"KR" (Корея и корейские компании), "US" (США и всё глобальное). Казахстанский сайт про Hyundai → "KR"
- "score": важность 1–5:
   5 — двигает рынки сейчас: решение Fed/BoJ/BoK/НБК, неожиданные CPI/NFP, шок на рынке, \
крупное событие по бумаге из watchlist
   4 — важная макро-/отраслевая новость, отчёт крупной компании, заметное движение рынка
   3 — полезная новость по её рынкам; любая заметная новость про крупный тех и ИИ \
(Nvidia, AMD, TSMC, Apple, Microsoft, OpenAI, Anthropic…) — минимум 3; \
KASE/AIX: IPO, дивиденды, индекс, отчёты эмитентов, остановка торгов бумагой — минимум 3
   2 — второстепенное
   1 — шум
- "title_ru": заголовок по-русски, коротко и по делу, как в новостном агентстве (до 110 знаков)
- "summary": суть по-русски, 2–3 коротких предложения: что произошло, ключевые цифры, контекст. \
Только факты из текста, без воды
- "why": ПОЧЕМУ ЭТО ВАЖНО — одно короткое предложение по-русски: какой механизм и для каких \
активов, В КОНТЕКСТЕ РЫНКА САМОЙ НОВОСТИ (новость про США — последствия для американских \
компаний, сектора, индексов, ставок; про Корею — для корейских; про Казахстан — для казахстанских). \
Упоминай другие страны и компании, только если связь прямая и названа в тексте \
(напр. в новости сказано, что Samsung поставляет чипы). Без общих фраз вроде «это важно для рынка»
- "impact": на что влияет, коротко, с ▲/▼ если ясно направление (напр. "KOSPI ▲, USD/KRW ▼"), или ""
- "tags": 1–3 тега латиницей или кириллицей без пробелов (тикеры, компании, темы: "Samsung", "ФРС", "нефть")
- "watch": названия из watchlist, которые ПРЯМО упомянуты в новости, массивом; \
косвенная связь (тот же сектор, «может повлиять») — НЕ считается; иначе []
- "dup": true, если это та же история, что в «недавно отправлено» или выше в этом списке

Watchlist: {watchlist}

Ответь ТОЛЬКО массивом JSON."""

KIND_LABEL = {"company": "🏢 Компании", "macro": "🏛 Макро", "market": "📊 Рынки"}


# ───────────────────────── сеть ─────────────────────────

def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def tg(method, params, retry=True):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        if e.code == 429 and retry:
            wait = json.loads(body).get("parameters", {}).get("retry_after", 5)
            time.sleep(wait + 1)
            return tg(method, params, retry=False)
        print(f"Telegram {method} error {e.code} для {params.get('chat_id')}: {body[:200]}", file=sys.stderr)
    except Exception as e:
        print(f"Telegram {method} error для {params.get('chat_id')}: {e}", file=sys.stderr)
    return None


def target(chat):
    """'-100123:5' → chat_id -100123 + тема (topic) 5 в группе с темами."""
    m = re.fullmatch(r"(-?\d+|@\w+):(\d+)", chat)
    return {"chat_id": m.group(1), "message_thread_id": m.group(2)} if m else {"chat_id": chat}


def send_text(chats, text):
    if len(text) > 4000:
        text = text[:3990] + "…"
    for chat in chats:
        tg("sendMessage", {**target(chat), "text": text, "parse_mode": "HTML",
                           "disable_web_page_preview": "true"})


def send_post(chats, text, image=None):
    """С картинкой — sendPhoto (подпись до 1024 знаков); если не вышло — обычный текст."""
    for chat in chats:
        if image and SEND_IMAGES and len(text) <= 1024:
            r = tg("sendPhoto", {**target(chat), "photo": image, "caption": text, "parse_mode": "HTML"})
            if r and r.get("ok"):
                continue
        send_text([chat], text)


def send(text):  # служебные сообщения — в личку
    send_text(PERSONAL, text)


_gemini_state = {"model": 0, "thinking": True}


def ask_gemini(system, user, max_tokens, as_json):
    for _ in range(len(GEMINI_MODELS) * 2):
        if _gemini_state["model"] >= len(GEMINI_MODELS):
            _gemini_state["model"] = 0
            return None
        model = GEMINI_MODELS[_gemini_state["model"]]
        cfg = {"maxOutputTokens": max_tokens, "temperature": 0.2}
        if as_json:
            cfg["responseMimeType"] = "application/json"
        if _gemini_state["thinking"]:
            cfg["thinkingConfig"] = {"thinkingBudget": 0}  # без «размышлений»: они съедают лимит ответа
        body = json.dumps({
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": cfg,
        }).encode()
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            data=body, headers={"x-goog-api-key": GEMINI_KEY, "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                resp = json.load(r)
            cand = (resp.get("candidates") or [{}])[0]
            text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
            if cand.get("finishReason") == "MAX_TOKENS":
                print(f"Gemini ({model}): ответ обрезан по длине", file=sys.stderr)
            return text
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "ignore")
            print(f"Gemini ({model}) error {e.code}: {msg[:250]}", file=sys.stderr)
            if e.code == 400 and _gemini_state["thinking"] and "think" in msg.lower():
                _gemini_state["thinking"] = False
                continue
            if e.code in (404, 429) or (e.code == 400 and "model" in msg.lower()):
                _gemini_state["model"] += 1  # следующая модель
                continue
            if e.code in (500, 502, 503, 504):
                time.sleep(5)
                continue
            return None
        except Exception as e:
            print(f"Gemini ({model}) error: {e}", file=sys.stderr)
            return None
    return None


def ask_claude(system, user, max_tokens):
    body = json.dumps({"model": MODEL, "max_tokens": max_tokens, "system": system,
                       "messages": [{"role": "user", "content": user}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return "".join(b.get("text", "") for b in json.load(r).get("content", []))
        except urllib.error.HTTPError as e:
            print(f"Claude error {e.code}: {e.read().decode('utf-8', 'ignore')[:250]}", file=sys.stderr)
            if e.code in (429, 500, 502, 503, 529) and attempt < 2:
                time.sleep(10 * (attempt + 1))
                continue
            return None
        except Exception as e:
            print(f"Claude error: {e}", file=sys.stderr)
            return None


def ask_ai(system, user, max_tokens=4000, as_json=False):
    """Gemini (бесплатно), если задан GEMINI_API_KEY, иначе Claude."""
    if GEMINI_KEY:
        return ask_gemini(system, user, max_tokens, as_json)
    return ask_claude(system, user, max_tokens)


# ───────────────────────── разбор лент ─────────────────────────

def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def tag(el):
    return el.tag.split("}")[-1]


def child(el, *names):
    for c in el:
        if tag(c) in names:
            return c
    return None


def parse_date(s):
    if not s:
        return None
    for fn in (parsedate_to_datetime, lambda x: datetime.fromisoformat(x.replace("Z", "+00:00"))):
        try:
            d = fn(s.strip())
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def rss_image(it, desc_html):
    for c in it.iter():
        t = tag(c)
        if t in ("content", "thumbnail") and c.get("url") and (
                c.get("medium") == "image" or (c.get("type") or "").startswith("image")
                or re.search(r"\.(jpe?g|png|webp)", c.get("url"), re.I) or t == "thumbnail"):
            return c.get("url")
        if t == "enclosure" and (c.get("type") or "").startswith("image") and c.get("url"):
            return c.get("url")
    m = re.search(r'<img[^>]+src=["\']([^"\']+)', desc_html or "", re.I)
    return m.group(1) if m else None


def parse_feed(data):
    root = ET.fromstring(data)
    items = []
    for it in root.iter():
        if tag(it) not in ("item", "entry"):
            continue
        t = child(it, "title")
        title = clean(t.text if t is not None else "")
        l = child(it, "link")
        link = (l.text or l.get("href") or "").strip() if l is not None else ""
        d = child(it, "description", "summary")
        desc_html = d.text if d is not None else ""
        g = child(it, "guid", "id")
        uid = (g.text.strip() if g is not None and g.text else "") or link or title
        p = child(it, "pubDate", "published", "updated")
        date = parse_date(p.text if p is not None else "")
        if title and link:
            items.append({"id": uid, "title": title, "link": link, "desc": clean(desc_html),
                          "image": rss_image(it, desc_html),
                          "ts": int(date.timestamp()) if date else None})
    return items


def parse_html_news(data, base):
    """Страница новостей сайта: ссылки вида .../news/show/123 с текстом."""
    page = data.decode("utf-8", "ignore")
    items, seen = [], set()
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']*/news/show/\d+[^"\']*)["\'][^>]*>(.*?)</a>',
                         page, re.S | re.I):
        link = urllib.parse.urljoin(base, html.unescape(m.group(1)))
        title = clean(m.group(2))
        if link in seen or len(title) < 15:
            continue
        seen.add(link)
        items.append({"id": link, "title": title[:250], "link": link, "desc": "", "image": None, "ts": None})
    return items


_meta_cache = {}


def page_meta(url):
    """(описание, картинка) из og-тегов статьи."""
    if "news.google.com" in url:
        return "", None
    if url in _meta_cache:
        return _meta_cache[url]
    desc, img = "", None
    try:
        page = fetch(url, timeout=8)[:500000].decode("utf-8", "ignore")

        def meta(names):
            for n in names:
                for pat in (rf'<meta[^>]+(?:property|name)=["\']{n}["\'][^>]*content=["\']([^"\']+)',
                            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']{n}["\']'):
                    m = re.search(pat, page, re.I)
                    if m:
                        return html.unescape(m.group(1)).strip()
            return ""
        desc = clean(meta(["og:description", "description", "twitter:description"]))
        img = meta(["og:image", "og:image:url", "twitter:image", "twitter:image:src"]) or None
        if img:
            img = urllib.parse.urljoin(url, img)
            if re.search(r"logo|default|placeholder|favicon", img, re.I):
                img = None
    except Exception:
        pass
    _meta_cache[url] = (desc, img)
    return desc, img


def tidy(item):
    title = item["title"]
    via_google = "news.google.com" in item["link"]
    if via_google:  # Google News дописывает " - Reuters"
        title = re.sub(r"\s+-\s+[^-]{2,40}$", "", title)
    desc = item["desc"]
    if desc and (via_google or desc.lower().startswith(title[:40].lower())):
        desc = ""
    if not desc:
        desc, img = page_meta(item["link"])
        item["image"] = item.get("image") or img
    item["title"], item["desc"] = title, desc[:DESC_LIMIT]
    return item


# ───────────────────────── дубли и фильтры ─────────────────────────

STOP = set("a an the of to in on for and or at by with from as is are was be after over amid "
           "its it this that new says said us".split())


def words(title):
    return {w for w in re.findall(r"[a-zа-яё0-9]+", title.lower()) if w not in STOP and len(w) > 1}


def similar(a, b):
    wa, wb = words(a), words(b)
    if len(wa) < 3 or len(wb) < 3:
        return False
    return len(wa & wb) / len(wa | wb) >= 0.5


SKIP_RE = re.compile("|".join(SKIP_PATTERNS), re.I)


def is_junk(it):
    if any(p in it["link"].lower() for p in SKIP_URL):
        return True
    return bool(SKIP_RE.search(f"{it['title']} {it['desc'][:200]}"))


def watch_hits(text):
    t = f" {text.lower()} "
    return [name for name, keys in WATCHLIST.items()
            if any(re.search(r"\b" + re.escape(k) + r"\b", t) for k in keys)]


KEYWORDS = ["fed", "fomc", "powell", "rate cut", "rate hike", "interest rate", "cpi", "ppi",
            "inflation", "payroll", "gdp", "recession", "treasury", "yield", "boj", "ecb", "kospi",
            "tenge", "oil", "opec", "brent", "uranium", "tariff", "sanction", "earnings", "guidance",
            "nvidia", "ставк", "инфляц", "нефт", "тенге", "дивиденд", "ipo"]


def keyword_score(item):
    text = f" {item['title']} {item['desc']} ".lower()
    if watch_hits(text):
        return 4
    return 3 if any(k in text for k in KEYWORDS) else 2


def extract_json(text):
    if not text:
        return None
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def score_items(items, recent_titles):
    out = []
    for i in range(0, len(items), AI_CHUNK):
        out += score_chunk(items[i:i + AI_CHUNK], recent_titles + [x["title"] for x in out])
    return out


def score_chunk(items, recent_titles):
    for it in items:  # запасной режим без ИИ
        it.update(score=keyword_score(it), summary=it["desc"][:300], impact="", why="", kind="",
                  tags=[], title_ru="", watch=watch_hits(f"{it['title']} {it['desc']}"),
                  dup=False, ai=False)
    if not AI_ON or not items:
        return items
    lines = [f"[{i}] ({it['source']}) {it['title']}\n{it['desc'][:DESC_LIMIT]}" for i, it in enumerate(items)]
    recent = "\n".join(f"- {t}" for t in recent_titles[-40:]) or "(пусто)"
    user = f"Недавно отправлено:\n{recent}\n\nНовые новости:\n\n" + "\n\n".join(lines)
    system = SYSTEM_PROMPT.replace("{watchlist}", ", ".join(WATCHLIST))
    result = extract_json(ask_ai(system, user, max_tokens=400 * len(items) + 800, as_json=True))
    if not isinstance(result, list):
        if len(items) > 1:  # пробуем половинками
            mid = len(items) // 2
            return score_chunk(items[:mid], recent_titles) + score_chunk(items[mid:], recent_titles)
        print(f"ИИ не оценил: {items[0]['title'][:60]}", file=sys.stderr)
        return items
    for r in result:
        try:
            it = items[int(r["i"])]
            it["score"] = max(1, min(5, int(r.get("score", it["score"]))))
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        it["kind"] = str(r.get("kind") or "")
        if r.get("region") in REGIONS:
            it["region"] = r["region"]
        it["title_ru"] = str(r.get("title_ru") or "")
        it["summary"] = str(r.get("summary") or it["summary"])
        it["impact"] = str(r.get("impact") or "")
        it["why"] = str(r.get("why") or "")
        it["tags"] = [str(t) for t in (r.get("tags") or [])][:3]
        text = f"{it['title']} {it['desc']} {it['summary']} {it['title_ru']}"
        it["watch"] = watch_hits(text)  # 📌 только если компания реально названа в тексте
        it["dup"] = bool(r.get("dup"))
        it["ai"] = True
    return items


# ───────────────────────── оформление ─────────────────────────

def esc(s):
    return html.escape(s or "", quote=False)


def local(ts):
    return datetime.fromtimestamp(ts, LOCAL_TZ)


def hashtag(t):
    t = re.sub(r"[^\wА-Яа-яЁё]", "", str(t).replace(" ", "_").replace("&", "and"))
    return f"#{t}" if t and not t[0].isdigit() else ""


def importance(score):
    return {5: "🔴 Срочно", 4: "🟠 Важно", 3: "🟡 Заметно"}.get(score, "⚪️ Фон")


def format_post(it):
    title = it.get("title_ru") or it["title"]
    head = f"{REGIONS[it['region']]['flag']} <b>{esc(title)}</b>"
    meta = [importance(it["score"])]
    if KIND_LABEL.get(it.get("kind")):
        meta.append(KIND_LABEL[it["kind"]])
    if it["watch"]:
        meta.append("📌 " + ", ".join(it["watch"]))
    parts = [head + "\n" + " · ".join(meta)]
    if it.get("summary"):
        parts.append(f"<blockquote>{esc(it['summary'])}</blockquote>")
    if it.get("why"):
        parts.append(f"💡 <b>Почему важно:</b> {esc(it['why'])}")
    if it.get("impact"):
        parts.append(f"📈 <i>Влияние:</i> <b>{esc(it['impact'])}</b>")
    when = " · " + local(it["ts"]).strftime("%d.%m %H:%M") if it.get("ts") else ""
    src = f'🔗 <a href="{html.escape(it["link"])}">{esc(it["source"])}</a><i>{when}</i>'
    tags = " ".join(filter(None, (hashtag(t) for t in it.get("tags", []) + it["watch"])))
    parts.append(src + (f"\n{tags}" if tags else ""))
    text = "\n\n".join(parts)
    if len(text) > 1024 and it.get("summary"):  # влезть в подпись к фото
        cut = max(80, len(it["summary"]) - (len(text) - 1020))
        it2 = dict(it, summary=it["summary"][:cut].rsplit(" ", 1)[0] + "…")
        return format_post(it2) if cut < len(it["summary"]) else text
    return text


def in_quiet(now):
    h = now.hour
    return h >= QUIET_FROM or h < QUIET_TO if QUIET_FROM > QUIET_TO else QUIET_FROM <= h < QUIET_TO


# ───────────────────────── календарь ─────────────────────────

def calendar_events(state, now_ts):
    cal = state["calendar"]
    if now_ts - cal.get("fetched", 0) > 6 * 3600:
        try:
            raw = json.loads(fetch(CALENDAR_URL))
            cal["events"] = [
                {"t": int(d.timestamp()), "name": f"{e['country']}: {e['title']}",
                 "fc": e.get("forecast", ""), "prev": e.get("previous", ""),
                 "region": CALENDAR_COUNTRIES[e["country"]]}
                for e in raw
                if e.get("impact") == "High" and e.get("country") in CALENDAR_COUNTRIES
                and (d := parse_date(e.get("date")))
            ]
            cal["fetched"] = now_ts
        except Exception as e:
            print(f"Календарь не загрузился: {e}", file=sys.stderr)
    events = [dict(e, region=e.get("region", "US")) for e in cal.get("events", [])]
    for when, name, region in CUSTOM_EVENTS:
        try:
            d = datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=LOCAL_TZ)
            events.append({"t": int(d.timestamp()), "name": name, "fc": "", "prev": "", "region": region})
        except ValueError:
            print(f"Не понял дату в CUSTOM_EVENTS: {when}", file=sys.stderr)
    return sorted(events, key=lambda e: e["t"])


def ev_line(e):
    d = local(e["t"])
    s = f"▫️ <b>{'Пн Вт Ср Чт Пт Сб Вс'.split()[d.weekday()]} {d:%d.%m %H:%M}</b> — {esc(e['name'])}"
    extra = ", ".join(x for x in (f"прогноз {e['fc']}" if e["fc"] else "",
                                  f"пред. {e['prev']}" if e["prev"] else "") if x)
    return s + (f" <i>({esc(extra)})</i>" if extra else "")


def check_calendar(state, now):
    now_ts = int(now.timestamp())
    events = calendar_events(state, now_ts)
    cal = state["calendar"]
    week_key = f"{now:%G-W%V}"
    if now.weekday() == 0 and now.hour >= QUIET_TO and cal.get("week") != week_key:
        for reg in REGIONS:
            upcoming = [e for e in events if e["region"] == reg and 0 <= e["t"] - now_ts <= 7 * 86400]
            if upcoming:
                send_text(chats_for(reg), f"🗓 <b>Календарь недели</b> {REGIONS[reg]['flag']}\n\n"
                          + "\n".join(ev_line(e) for e in upcoming))
        cal["week"] = week_key
    notified = set(cal.get("notified", []))
    for e in events:
        key = f"{e['t']}|{e['name']}"
        if 0 <= e["t"] - now_ts <= CALENDAR_REMIND_MIN * 60 and key not in notified:
            mins = max(1, round((e["t"] - now_ts) / 60))
            if not in_quiet(now):
                send_text(chats_for(e["region"]), f"⏰ <b>Через {mins} мин</b>\n\n{ev_line(e)}")
            notified.add(key)
    cal["notified"] = sorted(notified)[-200:]


def calendar_section(state, now, region):
    try:
        events = calendar_events(state, int(now.timestamp()))
    except Exception:
        return ""
    morning = now.hour < 12
    day = now.date() if morning else (now + timedelta(days=1)).date()
    sel = [e for e in events if e["region"] == region and local(e["t"]).date() == day
           and e["t"] > now.timestamp()]
    if not sel:
        return ""
    return f"🗓 <b>{'Сегодня' if morning else 'Завтра'} в календаре</b>\n" + "\n".join(ev_line(e) for e in sel)


# ───────────────────────── дайджест ─────────────────────────

def send_digest(state, now, region):
    queue = [q for q in state["queue"] if q.get("region", "US") == region]
    state["queue"] = [q for q in state["queue"] if q.get("region", "US") != region]
    cal = calendar_section(state, now, region)
    chats = chats_for(region)
    if not queue:
        if cal:
            send_text(chats, cal)
        return
    queue.sort(key=lambda x: (-x["score"], -(x["ts"] or 0)))
    title = "🌅 Утренний дайджест" if now.hour < 12 else "🌆 Вечерний дайджест"
    parts = [f"{REGIONS[region]['flag']} <b>{title}</b>\n<i>Не вошло в ленту: {len(queue)}</i>"]
    if AI_ON:
        lst = "\n".join(f"- ({q['score']}/5) {q['title']}: {q['summary']}" for q in queue[:40])
        brief = ask_ai("Ты — редактор новостей для инвест-аналитика. Напиши по-русски 3–5 "
                       "коротких пунктов «главное», каждый с новой строки и начиная с «• ». "
                       "Только факты из списка, без вступления.", lst, max_tokens=600)
        if brief:
            parts.append("<b>Главное</b>\n<blockquote>" + esc(brief.strip()) + "</blockquote>")
    lines = [f'{"📌 " if q.get("watch") else "▫️ "}<a href="{html.escape(q["link"])}">{esc(q["title"])}</a>'
             for q in queue[:25]]
    parts.append("<b>Все новости</b>\n" + "\n".join(lines))
    if cal:
        parts.append(cal)
    send_text(chats, "\n\n".join(parts))


def maybe_digest(state, now):
    for h in DIGEST_HOURS:
        key = f"{now:%Y-%m-%d}-{h}"
        if h <= now.hour < h + 3 and key not in state["digests"]:
            for reg in REGIONS:
                send_digest(state, now, reg)
            state["digests"] = (state["digests"] + [key])[-20:]


# ───────────────────────── состояние ─────────────────────────

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    for k, v in (("seen", {}), ("feeds", []), ("recent", []), ("queue", []),
                 ("digests", []), ("calendar", {})):
        st.setdefault(k, v)
    return st


def save_state(state):
    if len(state["seen"]) > 8000:
        state["seen"] = dict(sorted(state["seen"].items(), key=lambda kv: kv[1])[-8000:])
    cutoff = int(time.time()) - 48 * 3600
    state["recent"] = [r for r in state["recent"] if r["ts"] > cutoff][-400:]
    state["queue"] = state["queue"][-300:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


# ───────────────────────── основной цикл ─────────────────────────

def load_source(urls):
    items, errors, links, titles = [], [], set(), set()
    for url in urls:
        try:
            got = (parse_html_news(fetch(url[5:]), url[5:]) if url.startswith("html:")
                   else parse_feed(fetch(url)))
        except Exception as e:
            errors.append(f"{url[:60]}: {e}")
            continue
        for it in got:
            tkey = re.sub(r"\s+-\s+[^-]{2,40}$", "", it["title"]).lower()
            if it["link"] in links or tkey in titles:
                continue
            links.add(it["link"]); titles.add(tkey)
            items.append(it)
    return items, errors


def check_news(state, now):
    now_ts = int(now.timestamp())
    fresh = []
    for source, region, urls in FEEDS:
        items, errors = load_source(urls)
        if errors:
            print(f"[{source}] часть адресов не ответила: {'; '.join(errors)}", file=sys.stderr)
        if not items:
            print(f"[{source}] не загрузился", file=sys.stderr)
            continue
        first_time = source not in state["feeds"]
        for it in items:
            key = f"{source}|{it['id']}"
            if key in state["seen"]:
                continue
            state["seen"][key] = now_ts
            if not first_time:
                it["source"], it["region"] = source, region
                fresh.append(it)
        if first_time:
            state["feeds"].append(source)
            state["recent"] += [{"title": it["title"], "ts": now_ts} for it in items[:30]]
            print(f"[{source}] подключён, в ленте {len(items)} новостей")

    fresh.sort(key=lambda x: x["ts"] or 0)
    fresh = fresh[-MAX_PER_CYCLE:]
    if not fresh and not state.get("pending"):
        print(f"{now:%H:%M} новых нет")
        return

    log = []
    junk = [it for it in fresh if is_junk(it)]
    fresh = [it for it in fresh if it not in junk]
    log += [("   ", "не новость (фильтр)", it) for it in junk]

    with ThreadPoolExecutor(8) as ex:
        fresh = list(ex.map(tidy, fresh))

    recent_titles = [r["title"] for r in state["recent"]]
    unique = []
    for it in fresh:
        if any(similar(it["title"], t) for t in recent_titles + [u["title"] for u in unique]):
            log.append(("   ", "ДУБЛЬ (заголовок)", it))
            continue
        unique.append(it)

    # новости, которые ИИ не смог оценить в прошлый раз, — ещё одна попытка
    pending = state.pop("pending", [])
    unique = pending + unique
    scored = score_items(unique, recent_titles)
    to_send = []
    quiet = in_quiet(now)
    for it in scored:
        if AI_ON and not it.get("ai"):
            it["tries"] = it.get("tries", 0) + 1
            if it["tries"] < MAX_AI_RETRIES:
                state.setdefault("pending", []).append(
                    {k: it[k] for k in ("id", "title", "link", "desc", "image", "ts", "source", "region", "tries")})
                log.append(("   ", "ждёт ИИ (повтор)", it))
                continue
            # ИИ так и не ответил — не публикуем «сырой» пост, только в дайджест
            state["queue"].append({"title": it["title"], "link": it["link"], "score": it["score"],
                                   "summary": "", "watch": it["watch"], "ts": it["ts"],
                                   "source": it["source"], "region": it["region"]})
            log.append(("   ", "без ИИ → дайджест", it))
            continue
        state["recent"].append({"title": it["title"], "ts": now_ts})
        if it.get("title_ru"):
            state["recent"].append({"title": it["title_ru"], "ts": now_ts})
        sc = f"{it['score']}/5"
        if it["dup"]:
            log.append((sc, "ДУБЛЬ (ИИ)", it)); continue
        if it.get("kind") == "other":
            log.append((sc, "не новость (ИИ)", it)); continue
        if it["score"] < DROP_BELOW:
            log.append((sc, "отброшено", it)); continue
        if it["score"] >= MIN_SCORE and (not quiet or it["score"] == 5):
            to_send.append(it)
            log.append((sc, f"→ {it['region']}", it))
        else:
            state["queue"].append({"title": it.get("title_ru") or it["title"], "link": it["link"],
                                   "score": it["score"], "summary": it["summary"], "watch": it["watch"],
                                   "ts": it["ts"], "source": it["source"], "region": it["region"]})
            log.append((sc, f"в дайджест {it['region']}", it))

    # картинки для тех, что отправляем
    def add_image(it):
        if not it.get("image"):
            it["image"] = page_meta(it["link"])[1]
        return it
    if SEND_IMAGES:
        with ThreadPoolExecutor(8) as ex:
            to_send = list(ex.map(add_image, to_send))
    for it in to_send:
        send_post(chats_for(it["region"]), format_post(it), it.get("image"))
        time.sleep(1.1)

    print("─" * 70)
    for sc, what, it in sorted(log, key=lambda x: x[0], reverse=True):
        print(f"{sc}  {what:<20} [{it['source']}] {(it.get('title_ru') or it['title'])[:85]}")
    print("─" * 70)
    if AI_ON and scored and not all(x.get("ai") for x in scored):
        print("⚠️ ИИ ответил не на все новости — они попробуются ещё раз (см. ошибки Gemini выше)")
    elif not AI_ON:
        print("⚠️ Ключ ИИ не задан — оценка по ключевым словам")
    by_reg = {r: sum(1 for it in to_send if it["region"] == r) for r in REGIONS}
    print(f"{now:%H:%M} новых {len(fresh) + len(junk)}, отправлено "
          + ", ".join(f"{r} {n}" for r, n in by_reg.items()))


def run_once(state):
    now = datetime.now(LOCAL_TZ)
    for step in (check_news, check_calendar, maybe_digest):
        try:
            step(state, now)
        except Exception as e:
            print(f"Ошибка в {step.__name__}: {e!r}", file=sys.stderr)
        save_state(state)


def main():
    if not TOKEN:
        sys.exit("Укажите TG_TOKEN")
    missing = [r for r in REGIONS if not os.environ.get(REGIONS[r]["env"])]
    if missing and not PERSONAL:
        sys.exit("Укажите каналы TG_CHAT_KZ/KR/US или хотя бы TG_CHAT_ID")
    if not AI_ON:
        print("Ключ ИИ не задан — оценка по ключевым словам, без перевода", file=sys.stderr)
    state = load_state()
    if not state.get("v3"):
        state["queue"] = []  # старая очередь без разбивки по странам
        for reg, cfg in REGIONS.items():
            if os.environ.get(cfg["env"]):
                send_text(chats_for(reg), f"{cfg['flag']} <b>{cfg['name']}</b> подключён\n\n"
                          "Сюда будут приходить новости компаний и макро на русском. "
                          f"Дайджест — в {', '.join(f'{h}:00' for h in DIGEST_HOURS)}.")
        send("✅ <b>Vitis News Bot v3 запущен</b>"
             + (f"\n⚠️ Не заданы каналы: {', '.join(missing)} — их новости идут сюда, в личку."
                if missing else "")
             + ("" if AI_ON else "\n⚠️ Ключ ИИ не задан — без перевода и оценки."))
        state["v3"] = True
    minutes = None
    if "--minutes" in sys.argv:
        minutes = int(sys.argv[sys.argv.index("--minutes") + 1])
    stop_at = time.time() + (minutes or 0) * 60
    while True:
        run_once(state)
        if "--once" in sys.argv or (minutes is not None and time.time() + INTERVAL > stop_at):
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
