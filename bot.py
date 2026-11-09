#!/usr/bin/env python3
"""
FinNews Bot v2 — Investing.com, Reuters, Yahoo Finance → Telegram.

Что умеет:
  1. ИИ (Gemini или Claude) оценивает важность каждой новости (1–5) и пишет суть по-русски
  2. Сразу присылает только важные (оценка ≥ MIN_SCORE), остальное — в дайджест
  3. Склеивает дубли: одна история с разных сайтов приходит один раз
  4. Помечает 📌 новости про бумаги из WATCHLIST
  5. Тихие часы: ночью присылает только 🚨 5/5, остальное — утренним дайджестом
  6. Экономический календарь: обзор недели по понедельникам и напоминание за ~час до важных событий

Без внешних библиотек, Python 3.9+.
  python bot.py          — работает постоянно
  python bot.py --once   — одна проверка (для GitHub Actions)

Секреты (переменные окружения):
  TG_TOKEN, TG_CHAT_ID   — обязательно
  GEMINI_API_KEY         — бесплатный ИИ от Google (aistudio.google.com)
  ANTHROPIC_API_KEY      — или Claude (платно); без ключей — оценка по ключевым словам
Необязательные:
  MIN_SCORE (по умолчанию 3), INTERVAL_SEC (300), STATE_FILE (seen.json)
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

# Бумаги, которые помечаются 📌. Формат: "как показывать": [слова для поиска в тексте]
WATCHLIST = {
    # Корея (ADR в США и локальные)
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
DROP_BELOW = 2          # оценка ниже — выкидывается совсем (даже из дайджеста)
QUIET_FROM, QUIET_TO = 23, 8   # тихие часы по Алматы (с 23:00 до 08:00)
DIGEST_HOURS = [8, 19]  # когда присылать дайджест (по Алматы)
CALENDAR_REMIND_MIN = 60       # за сколько минут напоминать о событии
CALENDAR_COUNTRIES = {"USD", "JPY", "CNY", "EUR"}

# События, которых нет в календаре Forex Factory (НБК, Банк Кореи и т.п.).
# Формат: ("2026-11-28 15:00", "НБК: решение по базовой ставке"). Время — по Алматы.
CUSTOM_EVENTS = [
    ("2026-11-26 06:00", "Банк Кореи: решение по ставке (≈10:00 KST)"),
    ("2026-12-04 12:00", "НБК: решение по базовой ставке"),
    # График НБК и Банка Кореи на 2027 год ещё не опубликован — добавить, когда выйдет
]

MODEL = "claude-haiku-5-5"                # если используется Claude
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")  # бесплатный Gemini
LOCAL_TZ = timezone(timedelta(hours=5))  # Алматы

# ═════════════════════════════════════════════════════════════════

TOKEN = os.environ.get("TG_TOKEN", "")
CHAT_ID = os.environ.get("TG_CHAT_ID", "")
CHAT_IDS = [c.strip() for c in CHAT_ID.split(",") if c.strip()]  # можно несколько через запятую
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
AI_ON = bool(GEMINI_KEY or API_KEY)
INTERVAL = int(os.environ.get("INTERVAL_SEC", "300"))
STATE_FILE = os.environ.get("STATE_FILE", "seen.json")
MAX_PER_CYCLE = 40
DESC_LIMIT = 600

def gnews(query, lang="en", country="US"):
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, "hl": f"{lang}-{country}", "gl": country, "ceid": f"{country}:{lang}"})


# Для каждого источника — несколько адресов: берётся первый, который отдаёт новости
FEEDS = [
    ("Investing", ["https://www.investing.com/rss/news.rss"]),
    # У Reuters нет публичного RSS, берём их статьи через Google News
    ("Reuters", [gnews("site:reuters.com (markets OR business OR economy OR stocks) when:1d")]),
    ("Yahoo Finance", [
        "https://finance.yahoo.com/rss/topstories",
        "https://finance.yahoo.com/news/rss",
        "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%5EGSPC,%5EIXIC,%5EKS11,EWY&region=US&lang=en-US",
        gnews("site:finance.yahoo.com when:1d"),
    ]),
    ("CNBC", [
        "https://www.cnbc.com/id/20910258/device/rss/rss.html",   # Economy
        gnews("site:cnbc.com (markets OR economy OR stocks) when:1d"),
    ]),
    ("CNBC Markets", [
        "https://www.cnbc.com/id/15839069/device/rss/rss.html",
    ]),
    ("CNBC Asia", [
        "https://www.cnbc.com/id/19832390/device/rss/rss.html",
    ]),
    ("The Economist", [
        "https://www.economist.com/finance-and-economics/rss.xml",
        gnews("site:economist.com (finance OR economics OR markets) when:2d"),
    ]),
    ("The Economist Business", [
        "https://www.economist.com/business/rss.xml",
    ]),
    ("The Economist Asia", [
        "https://www.economist.com/asia/rss.xml",
    ]),
    # Корея
    ("Yonhap", [
        "https://en.yna.co.kr/RSS/economy.xml",
        "https://en.yna.co.kr/RSS/market.xml",
        gnews("site:en.yna.co.kr (economy OR market OR stocks) when:1d"),
    ]),
    ("SBS Biz", [
        gnews("site:biz.sbs.co.kr when:1d", "ko", "KR"),
    ]),
    # Казахстан
    ("Kursiv", [
        "https://kz.kursiv.media/feed/",
        gnews("site:kz.kursiv.media when:1d", "ru", "KZ"),
    ]),
    ("Kapital.kz", [
        "https://kapital.kz/rss",
        gnews("site:kapital.kz when:1d", "ru", "KZ"),
    ]),
]
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Запасной вариант оценки, если нет ключа ИИ или он не ответил
KEYWORDS = [
    "fed", "fomc", "powell", "rate cut", "rate hike", "interest rate", "cpi", "ppi",
    "inflation", "payroll", "jobs report", "gdp", "recession", "treasury", "yield",
    "boj", "bank of japan", "ecb", "korea", "kospi", "kazakhstan", "tenge",
    "oil", "opec", "brent", "uranium", "tariff", "sanction", "default", "earnings",
    "guidance", "nvidia",
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

SYSTEM_PROMPT = """Ты — ассистент аналитика инвестфонда в Алматы. Она — портфельный аналитик \
фонда корейских акций (Vitis Korea Fund), также следит за Казахстаном (KASE, тенге, \
нефть, уран), США (Fed, CPI, S&P 500, крупный тех) и Японией (BoJ, иена).

Тебе дают список новостей (на английском, корейском или русском). Для КАЖДОЙ верни объект JSON:
- "i": номер новости
- "title_ru": перевод заголовка на русский, ТОЛЬКО если он на корейском; иначе ""
- "score": важность для неё от 1 до 5:
   5 — двигает рынки прямо сейчас: решение Fed/BoJ/BoK/НБК, неожиданный CPI/NFP, \
шок на рынке, крупное событие по бумаге из watchlist
   4 — важная макро- или отраслевая новость, отчёт крупной компании, заметное движение индексов/валют/нефти
   3 — полезный контекст по её рынкам и секторам
   2 — второстепенное, слабо связано с её рынками
   1 — шум: кликбейт, личные финансы, советы «какие 3 акции купить», реклама, лайфстайл
- "summary": суть по-русски, 1–2 коротких предложения, только факты и цифры из текста
- "impact": на что влияет, коротко (напр. "KOSPI, USD/KRW" или "нефть, KMG"), или ""
- "watch": названия из watchlist, которых касается новость, массивом (или [])
- "dup": true, если это та же история, что уже была в списке «недавно отправлено» \
или выше в этом же списке (тот же факт другими словами)

Watchlist: {watchlist}

Ответь ТОЛЬКО массивом JSON, без пояснений."""


# ───────────────────────── сеть ─────────────────────────

def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def send(text):
    """Отправляет сообщение во все чаты из TG_CHAT_ID (через запятую)."""
    if len(text) > 4000:
        text = text[:3990] + "…"
    for chat in CHAT_IDS:
        send_one(chat, text)


def send_one(chat, text, retry=True):
    data = urllib.parse.urlencode({
        "chat_id": chat, "text": text, "parse_mode": "HTML",
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
            return send_one(chat, text, retry=False)
        print(f"Telegram error {e.code} для {chat}: {body}", file=sys.stderr)
    except Exception as e:
        print(f"Telegram error для {chat}: {e}", file=sys.stderr)


def ask_ai(system, user, max_tokens=4000):
    """Gemini (бесплатно), если задан GEMINI_API_KEY, иначе Claude."""
    if GEMINI_KEY:
        name = "Gemini"
        body = json.dumps({
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.2},
        }).encode()
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            data=body, headers={"x-goog-api-key": GEMINI_KEY, "content-type": "application/json"})
        extract = lambda resp: "".join(
            p.get("text", "") for c in resp.get("candidates", [])[:1]
            for p in c.get("content", {}).get("parts", []))
    else:
        name = "Claude"
        body = json.dumps({
            "model": MODEL, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": user}],
        }).encode()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
            "x-api-key": API_KEY, "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        })
        extract = lambda resp: "".join(b.get("text", "") for b in resp.get("content", []))
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                resp = json.load(r)
            return extract(resp)
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "ignore")
            print(f"{name} error {e.code}: {msg[:300]}", file=sys.stderr)
            if e.code in (429, 500, 502, 503, 529) and attempt < 2:
                time.sleep(10 * (attempt + 1))
                continue
            return None
        except Exception as e:
            print(f"{name} error: {e}", file=sys.stderr)
            return None


# ───────────────────────── разбор лент ─────────────────────────

def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def child(el, *names):
    for c in el:
        if c.tag.split("}")[-1] in names:
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


def parse_feed(data):
    root = ET.fromstring(data)
    items = []
    for it in root.iter():
        if it.tag.split("}")[-1] not in ("item", "entry"):
            continue
        t = child(it, "title")
        title = clean(t.text if t is not None else "")
        l = child(it, "link")
        link = (l.text or l.get("href") or "").strip() if l is not None else ""
        d = child(it, "description", "summary")
        desc = clean(d.text if d is not None else "")
        g = child(it, "guid", "id")
        uid = (g.text.strip() if g is not None and g.text else "") or link or title
        p = child(it, "pubDate", "published", "updated")
        date = parse_date(p.text if p is not None else "")
        if title and link:
            items.append({"id": uid, "title": title, "link": link, "desc": desc,
                          "ts": int(date.timestamp()) if date else None})
    return items


def page_description(url):
    if "news.google.com" in url:
        return ""
    try:
        page = fetch(url, timeout=8)[:400000].decode("utf-8", "ignore")
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


def tidy(item):
    title = item["title"]
    via_google = "news.google.com" in item["link"]
    if via_google:  # Google News дописывает " - Reuters" / " - Yahoo Finance"
        title = re.sub(r"\s+-\s+[^-]{2,40}$", "", title)
    desc = item["desc"]
    if desc and (via_google or desc.lower().startswith(title[:40].lower())):
        desc = ""
    if not desc:
        desc = page_description(item["link"])
    item["title"], item["desc"] = title, desc[:DESC_LIMIT]
    return item


# ───────────────────────── дубли ─────────────────────────

STOP = set("a an the of to in on for and or at by with from as is are was be after over amid "
           "its it this that new says said us".split())


def words(title):
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if w not in STOP and len(w) > 1}


def similar(a, b):
    wa, wb = words(a), words(b)
    if len(wa) < 3 or len(wb) < 3:
        return False
    return len(wa & wb) / len(wa | wb) >= 0.5


# ───────────────────────── оценка ─────────────────────────

def watch_hits(text):
    t = f" {text.lower()} "
    return [name for name, keys in WATCHLIST.items()
            if any(re.search(r"\b" + re.escape(k) + r"\b", t) for k in keys)]


def keyword_score(item):
    text = f" {item['title']} {item['desc']} ".lower()
    if watch_hits(text):
        return 4
    return 3 if any(re.search(r"\b" + re.escape(k) + r"\b", text) for k in KEYWORDS) else 2


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
    """Добавляет каждой новости score, summary, impact, watch, dup."""
    for it in items:  # значения по умолчанию = запасной режим
        it.update(score=keyword_score(it), summary=it["desc"][:350], impact="",
                  watch=watch_hits(f"{it['title']} {it['desc']}"), dup=False, ai=False)
    if not AI_ON or not items:
        return items

    lines = [f"[{i}] ({it['source']}) {it['title']}\n{it['desc'][:DESC_LIMIT]}"
             for i, it in enumerate(items)]
    recent = "\n".join(f"- {t}" for t in recent_titles[-40:]) or "(пусто)"
    user = f"Недавно отправлено:\n{recent}\n\nНовые новости:\n\n" + "\n\n".join(lines)
    system = SYSTEM_PROMPT.replace("{watchlist}", ", ".join(WATCHLIST))
    result = extract_json(ask_ai(system, user, max_tokens=150 * len(items) + 500))
    if not isinstance(result, list):
        print("ИИ не вернул JSON — использую ключевые слова", file=sys.stderr)
        return items
    for r in result:
        try:
            it = items[int(r["i"])]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        it["score"] = max(1, min(5, int(r.get("score", it["score"]))))
        it["summary"] = str(r.get("summary") or it["summary"])
        it["impact"] = str(r.get("impact") or "")
        w = r.get("watch") or []
        it["watch"] = sorted(set(it["watch"]) | {x for x in w if x in WATCHLIST})
        it["dup"] = bool(r.get("dup"))
        if r.get("title_ru"):
            it["title"] = str(r["title_ru"])
        it["ai"] = True
    return items


# ───────────────────────── сообщения ─────────────────────────

def esc(s):
    return html.escape(s or "", quote=False)


def local(ts):
    return datetime.fromtimestamp(ts, LOCAL_TZ)


def stars(score):
    return "⭐" * score + "☆" * (5 - score)


def format_news(it, night=False):
    head = f"{'🚨 ' if it['score'] == 5 else ''}{stars(it['score'])} {it['score']}/5"
    if it["watch"]:
        head += "  📌 " + ", ".join(it["watch"])
    parts = [head, f"<b>{esc(it['title'])}</b>"]
    if it["summary"]:
        parts.append(esc(it["summary"]))
    if it["impact"]:
        parts.append(f"📈 {esc(it['impact'])}")
    meta = it["source"] + (" · " + local(it["ts"]).strftime("%d.%m %H:%M") if it["ts"] else "")
    parts.append(f'<i>{esc(meta)}</i> · <a href="{html.escape(it["link"])}">Открыть</a>')
    return "\n".join(parts)


def in_quiet(now):
    h = now.hour
    return h >= QUIET_FROM or h < QUIET_TO if QUIET_FROM > QUIET_TO else QUIET_FROM <= h < QUIET_TO


# ───────────────────────── дайджест ─────────────────────────

def calendar_section(state, now):
    """Утром — события на сегодня, вечером — на завтра (в т.ч. ночные)."""
    try:
        events = calendar_events(state, int(now.timestamp()))
    except Exception:
        return ""
    morning = now.hour < 12
    day = now.date() if morning else (now + timedelta(days=1)).date()
    todays = [e for e in events if local(e["t"]).date() == day and e["t"] > now.timestamp()]
    if not todays:
        return ""
    return f"<b>🗓 {'Сегодня' if morning else 'Завтра'} в календаре:</b>\n" + "\n".join(ev_line(e) for e in todays)


def send_digest(state, now):
    queue = state["queue"]
    cal = calendar_section(state, now)
    if not queue:
        if cal:
            send(cal)
        return
    queue.sort(key=lambda x: (-x["score"], -(x["ts"] or 0)))
    important = [q for q in queue if q["score"] >= MIN_SCORE][:15]
    minor = [q for q in queue if q["score"] < MIN_SCORE][:20]

    title = "🌅 Утренний дайджест" if now.hour < 12 else "🌆 Вечерний дайджест"
    parts = [f"<b>{title}</b> · {len(queue)} новостей"]

    if AI_ON:
        lst = "\n".join(f"- ({q['score']}/5) {q['title']}: {q['summary']}" for q in queue[:40])
        brief = ask_ai(
            "Ты — ассистент аналитика корейских и казахстанских акций. Напиши по-русски "
            "3–5 коротких пунктов «главное», каждый с новой строки и начиная с «• ». "
            "Только факты из списка, без вступления.", lst, max_tokens=600)
        if brief:
            parts.append(esc(brief.strip()))

    def line(q):
        pin = "📌 " if q.get("watch") else ""
        return f'{pin}<a href="{html.escape(q["link"])}">{esc(q["title"])}</a> <i>({q["score"]}/5)</i>'

    if important:
        parts.append("<b>Важное:</b>\n" + "\n".join(line(q) for q in important))
    if minor:
        parts.append("<b>Остальное:</b>\n" + "\n".join(line(q) for q in minor))
    if cal:
        parts.append(cal)
    send("\n\n".join(parts))
    state["queue"] = []


def maybe_digest(state, now):
    for h in DIGEST_HOURS:
        key = f"{now:%Y-%m-%d}-{h}"
        if h <= now.hour < h + 3 and key not in state["digests"]:
            send_digest(state, now)
            state["digests"] = (state["digests"] + [key])[-20:]


# ───────────────────────── календарь ─────────────────────────

def calendar_events(state, now_ts):
    cal = state["calendar"]
    if now_ts - cal.get("fetched", 0) > 6 * 3600:
        try:
            raw = json.loads(fetch(CALENDAR_URL))
            cal["events"] = [
                {"t": int(d.timestamp()), "name": f"{e['country']}: {e['title']}",
                 "fc": e.get("forecast", ""), "prev": e.get("previous", "")}
                for e in raw
                if e.get("impact") == "High" and e.get("country") in CALENDAR_COUNTRIES
                and (d := parse_date(e.get("date")))
            ]
            cal["fetched"] = now_ts
        except Exception as e:
            print(f"Календарь не загрузился: {e}", file=sys.stderr)
    events = list(cal.get("events", []))
    for when, name in CUSTOM_EVENTS:
        try:
            d = datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=LOCAL_TZ)
            events.append({"t": int(d.timestamp()), "name": name, "fc": "", "prev": ""})
        except ValueError:
            print(f"Не понял дату в CUSTOM_EVENTS: {when}", file=sys.stderr)
    return sorted(events, key=lambda e: e["t"])


def ev_line(e):
    d = local(e["t"])
    s = f"{'Пн Вт Ср Чт Пт Сб Вс'.split()[d.weekday()]} {d:%d.%m %H:%M} — {esc(e['name'])}"
    extra = ", ".join(x for x in (f"прогноз {e['fc']}" if e["fc"] else "",
                                  f"пред. {e['prev']}" if e["prev"] else "") if x)
    return s + (f" <i>({esc(extra)})</i>" if extra else "")


def check_calendar(state, now):
    now_ts = int(now.timestamp())
    events = calendar_events(state, now_ts)
    cal = state["calendar"]

    week_key = f"{now:%G-W%V}"
    if now.weekday() == 0 and now.hour >= QUIET_TO and cal.get("week") != week_key:
        upcoming = [e for e in events if 0 <= e["t"] - now_ts <= 7 * 86400]
        if upcoming:
            send("<b>🗓 Календарь недели (важное)</b>\n\n" + "\n".join(ev_line(e) for e in upcoming))
        cal["week"] = week_key

    notified = set(cal.get("notified", []))
    for e in events:
        key = f"{e['t']}|{e['name']}"
        if 0 <= e["t"] - now_ts <= CALENDAR_REMIND_MIN * 60 and key not in notified:
            mins = max(1, round((e["t"] - now_ts) / 60))
            if not in_quiet(now):
                send(f"⏰ <b>Через {mins} мин</b>\n{ev_line(e)}")
            notified.add(key)
    cal["notified"] = sorted(notified)[-200:]


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
    if len(state["seen"]) > 5000:
        state["seen"] = dict(sorted(state["seen"].items(), key=lambda kv: kv[1])[-5000:])
    cutoff = int(time.time()) - 48 * 3600
    state["recent"] = [r for r in state["recent"] if r["ts"] > cutoff][-300:]
    state["queue"] = state["queue"][-150:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


# ───────────────────────── основной цикл ─────────────────────────

def check_news(state, now):
    now_ts = int(now.timestamp())
    fresh = []
    for source, urls in FEEDS:
        items, errors = None, []
        for url in urls:
            try:
                items = parse_feed(fetch(url))
                if items:
                    break
                errors.append(f"{url[:60]}: пусто")
            except Exception as e:
                errors.append(f"{url[:60]}: {e}")
        if not items:
            print(f"[{source}] не загрузился: {'; '.join(errors)}", file=sys.stderr)
            continue
        first_time = source not in state["feeds"]
        for it in items:
            key = f"{source}|{it['id']}"
            if key in state["seen"]:
                continue
            state["seen"][key] = now_ts
            if not first_time:
                it["source"] = source
                fresh.append(it)
        if first_time:
            state["feeds"].append(source)
            state["recent"] += [{"title": it["title"], "ts": now_ts} for it in items[:30]]
            print(f"[{source}] подключён, в ленте {len(items)} новостей")

    fresh.sort(key=lambda x: x["ts"] or 0)
    fresh = fresh[-MAX_PER_CYCLE:]
    if not fresh:
        print(f"{now:%H:%M} новых нет")
        return

    with ThreadPoolExecutor(8) as ex:
        fresh = list(ex.map(tidy, fresh))

    # 1) дубли по заголовкам — без ИИ
    recent_titles = [r["title"] for r in state["recent"]]
    unique = []
    for it in fresh:
        if any(similar(it["title"], t) for t in recent_titles + [u["title"] for u in unique]):
            continue
        unique.append(it)

    # 2) оценка ИИ (+ он же ловит дубли, сформулированные иначе)
    scored = score_items(unique, recent_titles)

    quiet = in_quiet(now)
    sent = queued = dropped = 0
    log = []
    for it in fresh:
        if it not in unique:
            log.append(("   ", "ДУБЛЬ (заголовок)", it))
    for it in scored:
        state["recent"].append({"title": it["title"], "ts": now_ts})
        if it["dup"] or it["score"] < DROP_BELOW:
            dropped += 1
            log.append((f"{it['score']}/5", "ДУБЛЬ (ИИ)" if it["dup"] else "отброшено", it))
            continue
        if it["score"] >= MIN_SCORE and (not quiet or it["score"] == 5):
            send(format_news(it))
            sent += 1
            log.append((f"{it['score']}/5", "→ ОТПРАВЛЕНО", it))
            time.sleep(1.1)
        else:
            state["queue"].append({k: it[k] for k in
                                   ("title", "link", "score", "summary", "watch", "ts", "source")})
            queued += 1
            log.append((f"{it['score']}/5", "в дайджест", it))
    print("─" * 60)
    for sc, what, it in sorted(log, key=lambda x: x[0], reverse=True):
        print(f"{sc}  {what:<18} [{it['source']}] {it['title'][:90]}")
    print("─" * 60)
    if scored and not scored[0].get("ai"):
        print("⚠️ Оценка по ключевым словам (ИИ не ответил или нет ключа)")
    print(f"{now:%H:%M} новых {len(fresh)}, дублей {len(fresh) - len(unique)}, "
          f"отправлено {sent}, в дайджест {queued}, отброшено {dropped}")


def run_once(state):
    now = datetime.now(LOCAL_TZ)
    for step in (check_news, check_calendar, maybe_digest):
        try:
            step(state, now)
        except Exception as e:
            print(f"Ошибка в {step.__name__}: {e!r}", file=sys.stderr)
        save_state(state)


def main():
    if not TOKEN or not CHAT_ID:
        sys.exit("Укажите TG_TOKEN и TG_CHAT_ID")
    if not AI_ON:
        print("Ключ ИИ не задан — оценка по ключевым словам", file=sys.stderr)
    state = load_state()
    if not state.get("v2"):
        send("✅ <b>FinNews Bot v2 запущен</b>\n"
             f"Сразу присылаю новости с оценкой {MIN_SCORE}/5 и выше, остальное — в дайджесте "
             f"в {', '.join(f'{h}:00' for h in DIGEST_HOURS)}. "
             f"Тихие часы {QUIET_FROM}:00–{QUIET_TO:02d}:00 (кроме 🚨 5/5)."
             + ("" if AI_ON else "\n⚠️ Ключ ИИ не задан — оцениваю по ключевым словам."))
        state["v2"] = True
    while True:
        run_once(state)
        if "--once" in sys.argv:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
