#!/usr/bin/env python3
"""
Vitis News Bot v4 — новости по каналам: 🇰🇿 Vitis KZ · 🇰🇷 Vitis Korea · 🇺🇸 Vitis US · 🌍 Vitis Macro News

Что умеет:
  • Делит новости по странам и шлёт каждую в свой канал
  • Пропускает только НОВОСТИ про компании и макро (без колонок, мнений, советов, «топ-5 акций»)
  • Переводит на русский: заголовок + суть 2–3 предложения
  • Прикладывает картинку из самой статьи (если есть)
  • Оценка важности 1–5, склейка дублей, 📌 watchlist, тихие часы, дайджесты, календарь

Без внешних библиотек, Python 3.9+.
  python bot.py --once         — одна проверка
  python bot.py --minutes 45   — проверять каждые INTERVAL_SEC в течение 45 минут (GitHub Actions)
  DRY_RUN=1 python bot.py --once — тест без Telegram: посты печатаются в консоль, состояние в seen.dry.json

Секреты:
  TG_TOKEN                     — токен бота (обязательно)
  TG_CHAT_KZ / TG_CHAT_KR / TG_CHAT_US / TG_CHAT_MACRO — каналы (@имя или -100...; можно несколько через запятую)
                                 или темы одной группы: -100XXXXXXXXXX:номер_темы
  TG_CHAT_ID                   — личный чат для служебных сообщений; если канал не задан, его новости идут сюда
  GEMINI_API_KEY               — бесплатный ИИ (или ANTHROPIC_API_KEY — Claude)
"""
import hashlib
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

# С какой оценки присылать сразу — отдельно по каналам (можно переопределить: MIN_SCORE_US=3 и т.п.)
MIN_SCORE = {"US": 4, "KZ": 4, "KR": 3, "MACRO": 4}
MIN_SCORE = {r: int(os.environ.get(f"MIN_SCORE_{r}") or v) for r, v in MIN_SCORE.items()}
DROP_BELOW = 3                 # ниже — не присылать вообще (и в дайджест тоже)
# Потолок постов в час на канал (🔴 5/5 проходят всегда); лишнее уходит в дайджест
MAX_PER_HOUR = {"US": 8, "KR": 8, "KZ": 6, "MACRO": 6}
MAX_AGE_HOURS = 36             # новости старше — не публикуем (старые страницы, всплывшие в лентах)
QUIET_FROM, QUIET_TO = 23, 8   # тихие часы по Алматы
DIGEST_HOURS = [8, 19]
CALENDAR_REMIND_MIN = 60
SEND_IMAGES = True

# Каналы
REGIONS = {
    "KZ": {"flag": "🇰🇿", "name": "Vitis KZ", "env": "TG_CHAT_KZ"},
    "KR": {"flag": "🇰🇷", "name": "Vitis Korea", "env": "TG_CHAT_KR"},
    "US": {"flag": "🇺🇸", "name": "Vitis US", "env": "TG_CHAT_US"},
    "MACRO": {"flag": "🌍", "name": "Vitis Macro News", "env": "TG_CHAT_MACRO"},
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
# Платный Claude выключен: работает только бесплатный Gemini. Чтобы включить Claude Haiku как запасной,
# нужны ОБА условия: секрет ANTHROPIC_API_KEY и переменная USE_CLAUDE=1
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "") if os.environ.get("USE_CLAUDE") == "1" else ""
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
AI_ON = bool(GEMINI_KEY or API_KEY)
INTERVAL = int(os.environ.get("INTERVAL_SEC", "120"))
DRY_RUN = os.environ.get("DRY_RUN", "") not in ("", "0", "false")
STATE_FILE = os.environ.get("STATE_FILE", "seen.dry.json" if DRY_RUN else "seen.json")
SEC_UA = os.environ.get("SEC_UA", "")   # для SEC EDGAR нужен User-Agent вида "Имя email@домен"
GNEWS_EVERY = 3        # ленты через Google News опрашиваем раз в 3 цикла (иначе Google режет запросы)
MAX_PER_CYCLE = 150
AI_CHUNK = 12          # небольшие пачки — ответ ИИ не обрезается
HOLD_MIN = 5           # несрочные новости копим до 5 штук или ~6 минут: меньше запросов к ИИ (лимит Gemini)
MAX_AI_RETRIES = 3     # сколько раз пытаться оценить новость, если ИИ не ответил
DESC_LIMIT = 600


def chats_for(region):
    raw = os.environ.get(REGIONS[region]["env"], "")
    ids = [c.strip() for c in raw.split(",") if c.strip()]
    if DRY_RUN and not ids:
        return [f"«{REGIONS[region]['name']}»"]
    return ids or PERSONAL


def gnews(query, lang="en", country="US"):
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, "hl": f"{lang}-{country}", "gl": country, "ceid": f"{country}:{lang}"})


# (название, канал по умолчанию, [адреса], {необязательные фильтры}). ИИ может перекинуть новость
# в другой канал, если она про другую страну (напр. Reuters про Samsung → Korea).
# Фильтры: "must" — в заголовке/описании должно встретиться; "url" — в ссылке должно встретиться.
KOREA_RE = r"korea|kospi|kosdaq|samsung|hynix|hyundai|\bkia\b|\blg\b|posco|\bwon\b|seoul|naver|kakao"
KZ_RE = r"kazakh|казахстан|тенге|tenge|kaspi|kazatomprom|казатомпром|kazmunay|halyk|astana|tengiz|kashagan|\bcpc\b|ктк"
GEO_RE = (r"\bwars?\b|warship|warfare|iraq|syria|yemen|saudi|strike|missile|drone|sanction|ceasefire|truce|peace (talks|deal|plan)|troops|invasion|"
          r"invade|nuclear|tariff|trade (war|deal)|export control|embargo|blockade|coup|martial law|"
          r"election|summit|opec|iran|ukrain|russia|kremlin|israel|gaza|hezbollah|houthi|red sea|hormuz|"
          r"taiwan|north korea|pyongyang|nato|\bg7\b|\bg20\b|санкци|войн|перемири|удар")
MEGACAP_RE = (r"nvidia|apple inc|microsoft|alphabet|amazon|meta platforms|tesla|broadcom|"
              r"advanced micro|intel corp|micron|berkshire|jpmorgan|coupang")

FEEDS = [
    # ─── 🇺🇸 US / глобальное: только главные ленты ───
    ("Reuters", "US", [gnews("site:reuters.com (markets OR business OR economy OR stocks) when:1d")]),
    ("WSJ", "US", [
        "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",
        "https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness",
        "https://feeds.content.dowjones.io/public/rss/RSSWSJD",
    ]),
    ("CNBC", "US", [
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",   # Top News
        "https://www.cnbc.com/id/20910258/device/rss/rss.html",
        "https://www.cnbc.com/id/15839069/device/rss/rss.html",
        "https://www.cnbc.com/id/10001147/device/rss/rss.html",
    ]),
    ("Investing", "US", ["https://www.investing.com/rss/news.rss"],
     {"url": r"/news/(economy|economic-indicators|commodities-news|forex-news)"}),
    # первоисточники — отсюда агентства берут новости, так быстрее всего
    ("Fed", "US", ["https://www.federalreserve.gov/feeds/press_all.xml"]),
    ("Nvidia IR", "US", ["https://nvidianews.nvidia.com/releases.xml"]),
    ("SEC 8-K", "US", ["sec:https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb="
                       "&owner=include&count=100&output=atom"], {"must": MEGACAP_RE}),
    # ─── 🇰🇷 Korea ───
    ("Yonhap", "KR", [gnews("site:en.yna.co.kr (economy OR market OR stocks OR company) when:1d")],
     {"must": r"econom|market|stock|share|kospi|won\b|export|trade|rate|bank|chip|semiconductor|samsung|hynix|"
              r"hyundai|\blg\b|\bsk\b|posco|earnings|profit|sales|invest|inflation|gdp|tariff|bok\b"}),
    ("Korea Herald", "KR", [gnews("site:koreaherald.com (business OR economy OR market) when:1d")]),
    ("KED Global", "KR", [gnews("site:kedglobal.com when:1d")]),
    ("BusinessKorea", "KR", [gnews("site:businesskorea.co.kr when:1d")]),
    ("Reuters Korea", "KR", [gnews("site:reuters.com (Korea OR KOSPI OR Samsung OR Hynix OR Hyundai) when:1d")],
     {"must": KOREA_RE}),
    ("CNBC Asia", "KR", ["https://www.cnbc.com/id/19832390/device/rss/rss.html"], {"must": KOREA_RE}),
    # ─── 🇰🇿 Kazakhstan ───
    ("KASE", "KZ", ["html:https://kase.kz/ru/information/news/all"]),
    ("AIX", "KZ", [gnews("site:aix.kz when:3d")]),
    ("Kursiv", "KZ", ["https://kz.kursiv.media/feed/"]),
    ("Kapital.kz", "KZ", [gnews("site:kapital.kz when:1d", "ru", "KZ")]),   # их собственный RSS отвечает 403
    ("Interfax KZ", "KZ", [gnews("site:interfax.kz when:1d", "ru", "KZ")]),
    ("НБК", "KZ", [gnews("site:nationalbank.kz when:2d", "ru", "KZ")]),
    ("Reuters KZ", "KZ", [gnews("site:reuters.com (Kazakhstan OR tenge OR Kazatomprom OR Kaspi) when:2d")],
     {"must": KZ_RE}),
    ("Bloomberg KZ", "KZ", [gnews("site:bloomberg.com (Kazakhstan OR Kazatomprom OR Kaspi OR Tengiz) when:2d")],
     {"must": KZ_RE}),
    # ─── 🌍 Macro / геополитика ───
    ("Reuters World", "MACRO", [gnews("site:reuters.com (war OR sanctions OR ceasefire OR missile OR tariffs OR "
                                      "Iran OR Ukraine OR Russia OR Taiwan OR OPEC) when:1d")], {"must": GEO_RE}),
    ("BBC World", "MACRO", ["https://feeds.bbci.co.uk/news/world/rss.xml"], {"must": GEO_RE}),
    ("Al Jazeera", "MACRO", ["https://www.aljazeera.com/xml/rss/all.xml"], {"must": GEO_RE}),
    ("OFAC", "MACRO", [gnews("(site:home.treasury.gov OR site:ofac.treasury.gov) sanctions when:2d")]),
    ("EU Council", "MACRO", [gnews("site:consilium.europa.eu sanctions when:2d")]),
]
# AP и JoongAng через Google News отдают только страницы-рубрики, а не новости — убраны.
# Убраны как шум: Yahoo Finance (~650 записей в сутки: «цена акции онлайн», penny stocks, графики),
# MarketWatch (личные финансы и мнения), The Economist (эссе, а не новости), SBS Biz (шортсы),
# общий поток Investing (сделки инсайдеров) и дубли тех же сайтов через Google News.
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
    # мусор, который реально проходил в ленту
    r"\b(sells?|buys?|acquires?) \$[\d.,]+ ?[kmb]?(illion)? (in|of) .{0,40}(stock|shares)",
    r"stock price .{0,20}real time", r"\bpenny stocks?\b", r"seasonal returns", r"price of (gold|silver) today",
    r"top photos|photos of the (day|week)", r"\bstocks? to watch\b", r"\bbargain buys?\b",
    r"биография", r"\d+ шаг\w+ к ", r"\[숏폼\]", r"price target|upgrades? .{0,30} to (buy|overweight)",
]
# Биржевая рутина KASE: облигации, купоны, ГЦБ, репо — этим занимается bonds.py (тема Bonds Alert)
KASE_ROUTINE = re.compile(r"облигац|купон|МЕУКАМ|МЕОКАМ|МЕККАМ|МЕУЖКАМ|нот\w* Национального Банка|подписк|"
                          r"специализированные торги|репо|иностранными валютами|своп|паев|ETF|"
                          r"маркет-мейкер|официальн\w+ списк", re.I)
SKIP_URL = ["/personal-finance/", "/opinion/", "/opinions/", "/video/", "/videos/", "/sponsored",
            "/lifestyle/", "/sports/", "/podcast", "fool.com", "/live-blog", "/review/"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

SYSTEM_PROMPT = """Ты — редактор новостной ленты для портфельного аналитика инвестфонда в Алматы. \
Она ведёт фонд корейских акций (Vitis Korea Fund) и следит за Казахстаном (KASE, AIX, тенге, \
нефть, уран), США/глобальными рынками (Fed, CPI, крупный тех) и геополитикой (канал Macro).

Нужны ТОЛЬКО НОВОСТИ про компании и макро: события, решения, отчёты, сделки, данные, \
движения рынков. НЕ нужны: колонки и мнения, аналитические эссе и разборы «почему», \
советы инвесторам, «какие акции купить», личные финансы, обзоры продуктов, лайфстайл, \
интервью без новостей, промо, спорт, новинки и рестайлинг моделей авто/гаджетов, \
фото и слухи о продуктах без финансового эффекта для компании.

Для КАЖДОЙ новости верни объект JSON:
- "i": идентификатор новости — ровно тот, что в квадратных скобках перед ней (напр. "k7q")
- "kind": "company" (новость компании), "macro" (экономика, центробанки, данные, \
политика с влиянием на экономику), "market" (движения индексов, валют, сырья, облигаций), \
"geo" (геополитика: войны, удары, перемирия, санкции, торговые войны, перевороты, выборы), \
или "other" (всё, что не новость — такое мы НЕ публикуем)
- "region": в какой канал (по содержанию, не по сайту!): \
"MACRO" — геополитика и международная политика: войны и удары (Иран, Израиль, Россия/Украина, \
Тайвань, КНДР), перемирия и переговоры, санкции и экспортный контроль, пошлины и торговые войны, \
перевороты, выборы и смена власти в крупных странах, политические решения ОПЕК+; \
"KZ" — Казахстан/Центральная Азия; "KR" — Корея и корейские компании; "US" — США и остальное \
глобальное (ФРС, данные, компании). Казахстанский сайт про Hyundai → "KR"; санкции против РФ → "MACRO"
- "score": важность 1–5. Оценивай СТРОГО: 4–5 получают считаные новости в день, большинство — 1–2.
   5 — двигает рынки прямо сейчас: решение Fed/BoJ/BoK/НБК, неожиданные CPI/NFP, шок на рынке, \
смена CEO или крупная сделка в мегакапе, начало/конец войны, удар по нефтяной инфраструктуре
   4 — крупное событие масштаба страны или сектора
   3 — заметная, но не срочная новость по её рынкам
   2 — второстепенное, локальное, мелкие компании
   1 — шум
  Масштаб по каналам:
   US: 4+ только ФРС и ключевая статистика, отчёты и прогнозы мегакапов и крупного ИИ/чип-сектора \
(Nvidia, AMD, TSMC, Apple, Microsoft, OpenAI, Anthropic…), смена CEO в них, M&A от $10 млрд, \
движение индексов >1,5%. Средние и малые компании, рейтинги аналитиков, целевые цены, \
продажи акций инсайдерами, «акции на заметку» — 1–2.
   KZ: 4+ только масштаб страны: НБК и базовая ставка, инфляция, ВВП, бюджет, налоги, Нацфонд, тенге, \
нефть/ОПЕК+/КТК/Тенгиз/Кашаган, уран, суверенный рейтинг, нацкомпании и бумаги watchlist \
(отчёты, дивиденды, IPO/SPO), топ-5 банков, сделки и проекты от ~$100 млн (≈50 млрд ₸), \
санкционные риски для Казахстана. Проекты и займы небольших компаний (меньше ~50 млрд ₸), \
планы «построим к 2027», акиматы и регионы, льготные кредиты, биографии, криминал, назначения \
ниже министра — 1–2.
   KR: 4+ — Банк Кореи, фактические данные по экспорту/ВВП/инфляции (прогнозы банков и ведомств — 3), вона, отчёты и крупные решения Samsung, SK Hynix, \
Hyundai и других тяжеловесов KOSPI, чипы, потоки иностранцев, реформы рынка (Value-up, налоги). \
Личные дела владельцев чеболей — 3 и только если затрагивают контроль или пакет акций. \
Общество, спорт, внутренняя политика без влияния на рынок — 1.
   MACRO: 4+ — эскалация или деэскалация с эффектом для нефти, валют, рынков: новые удары и фронты, \
перемирие, новые пакеты санкций, пошлины. Рутинные сводки с фронта, заявления без действий — 2.
- "title_ru": заголовок по-русски, коротко и по делу, как в новостном агентстве (до 110 знаков)
- "summary": суть по-русски, 2–3 коротких предложения: что произошло, ключевые цифры, контекст. \
Только факты из текста, без воды
- "why": ПОЧЕМУ ЭТО ВАЖНО — одно короткое предложение по-русски: какой механизм и для каких \
активов, В КОНТЕКСТЕ РЫНКА САМОЙ НОВОСТИ (новость про США — последствия для американских \
компаний, сектора, индексов, ставок; про Корею — для корейских; про Казахстан — для казахстанских; \
геополитика — для нефти, золота, доллара, рисковых активов). \
Упоминай другие страны и компании, только если связь прямая и названа в тексте \
(напр. в новости сказано, что Samsung поставляет чипы). Без общих фраз вроде «это важно для рынка»
- "impact": на что влияет, коротко, с ▲/▼ если ясно направление (напр. "KOSPI ▲, USD/KRW ▼"), или ""
- "tags": 1–3 тега латиницей или кириллицей без пробелов (тикеры, компании, темы: "Samsung", "ФРС", "нефть")
- "watch": названия из watchlist, которые ПРЯМО упомянуты в новости, массивом; \
косвенная связь (тот же сектор, «может повлиять») — НЕ считается; иначе []
- "dup": true, если это ТО ЖЕ СОБЫТИЕ, что в «недавно отправлено» или выше в этом списке, — даже если \
издание другое и слова другие (напр. «KOSPI превысил 5000» и «Kospi подскочил выше 5000 на ралли чипов» — \
одно событие; пересказ, реакция рынка и «что это значит» про уже отправленное событие — тоже dup). \
Из нескольких версий одного события оставь одну — самую полную, остальным ставь dup

Watchlist: {watchlist}

Ответь ТОЛЬКО массивом JSON."""

KIND_LABEL = {"company": "🏢 Компании", "macro": "🏛 Макро", "market": "📊 Рынки", "geo": "🌍 Геополитика"}


# ───────────────────────── сеть ─────────────────────────

def fetch(url, timeout=20):
    ua = SEC_UA if "sec.gov" in url and SEC_UA else UA
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def tg(method, params, retry=True):
    if DRY_RUN:  # тест без Telegram: печатаем то, что ушло бы в канал
        body = params.get("text") or params.get("caption") or ""
        body = html.unescape(re.sub(r"<[^>]+>", "", re.sub(r'<a href="([^"]+)">([^<]*)</a>', r"\2 <\1>", body)))
        print(f"\n┌── {method} → {params.get('chat_id')}"
              + (f" (тема {params['message_thread_id']})" if params.get("message_thread_id") else "")
              + (f"  [фото: {params['photo'][:60]}]" if params.get("photo") else "")
              + "\n" + "\n".join("│ " + l for l in body.split("\n")) + "\n└──")
        return {"ok": True}
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


# Как просить модель «не размышлять» (размышления съедают лимит ответа): у Gemini 3.x это
# thinkingLevel, у 2.x — thinkingBudget; если модель не принимает ни то ни другое — без параметра.
THINK_MODES = [{"thinkingLevel": "minimal"}, {"thinkingBudget": 0}, None]
_gemini_state = {"model": 0, "mode": {}}


def ask_gemini(system, user, max_tokens, as_json):
    for _ in range(len(GEMINI_MODELS) * 5):
        if _gemini_state["model"] >= len(GEMINI_MODELS):
            _gemini_state["model"] = 0
            return None
        model = GEMINI_MODELS[_gemini_state["model"]]
        mode_i = _gemini_state["mode"].setdefault(model, 1 if "-2." in model else 0)
        think = THINK_MODES[mode_i]
        # без явного отключения размышлений даём запас по длине ответа
        cfg = {"maxOutputTokens": max_tokens if think else max_tokens * 3, "temperature": 0.2}
        if as_json:
            cfg["responseMimeType"] = "application/json"
        if think:
            cfg["thinkingConfig"] = think
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
            print(f"Gemini ({model}, {json.dumps(think)}) error {e.code}: {msg[:250]}", file=sys.stderr)
            if e.code == 400 and "model" not in msg.lower() and mode_i + 1 < len(THINK_MODES):
                _gemini_state["mode"][model] = mode_i + 1   # «invalid argument» — пробуем другой способ
                continue
            if e.code in (400, 404, 429):
                _gemini_state["model"] += 1  # следующая модель
                continue
            if e.code in (500, 502, 503, 504):
                if _gemini_state.setdefault("busy", 0) < 1:
                    _gemini_state["busy"] += 1
                    time.sleep(5)
                    continue
                _gemini_state["busy"] = 0
                _gemini_state["model"] += 1
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
    """Gemini (бесплатно), если задан GEMINI_API_KEY; если он не ответил (лимит) — Claude, если есть ключ."""
    out = ask_gemini(system, user, max_tokens, as_json) if GEMINI_KEY else None
    if not out and API_KEY:
        out = ask_claude(system, user, max_tokens)
    return out


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


BREAK_EVENT = re.compile(
    r"\b(resign\w*|steps? down|stepping down|oust\w*|fired|reinstat\w*|returns? as (ceo|chief)|named (ceo|chief)|"
    r"new ceo|appoint\w* (as )?(ceo|chief)|to acquire|agrees? to (buy|acquire)|merger|takeover|bankruptcy|"
    r"chapter 11|halts? trading|trading halt\w*|circuit breaker|emergency (rate|meeting)|rate (cut|hike)|"
    r"(cuts?|raises?|hikes?|holds?) (interest )?rates?|defaults?|invad\w+|invasion|declares? war|ceasefire|"
    r"truce|missile|air ?strikes?|strikes? on|sanctions?|martial law|coup|assassinat\w+|plunge\w*|crash\w*|"
    r"soars?|tariffs?)\b|отставк|уволен|назначен\w* (глав|председател)|дефолт|девальвац|базов\w+ ставк|"
    r"санкци|перемири|удар\w* по|военное положение", re.I)
BREAK_ENTITY = re.compile(
    r"\b(nvidia|apple|microsoft|alphabet|google|amazon|meta|tesla|tsmc|openai|anthropic|broadcom|amd|intel|"
    r"samsung|hynix|hyundai|fed|fomc|powell|ecb|boj|bank of korea|bok|kospi|s&p 500|nasdaq|iran|israel|"
    r"russia|ukraine|china|taiwan|north korea|opec|trump|white house|kaspi|halyk|kazatomprom|kazmunaygas)\b|"
    r"нбк|нацбанк|казатомпром|казмунайгаз|тенге|иран|росси|украин|кита|опек", re.I)


HARD_EVENT = re.compile(
    r"\b(resign\w*|steps? down|stepping down|oust\w*|fired|reinstat\w*|returns? as (ceo|chief)|named (ceo|chief)|"
    r"new ceo|appoint\w* (as )?(ceo|chief)|to acquire|agrees? to (buy|acquire)|bankruptcy|chapter 11|"
    r"trading halt\w*|circuit breaker|emergency (rate|meeting)|(cuts?|raises?|hikes?) (interest )?rates?|"
    r"invad\w+|invasion|declares? war|ceasefire|martial law|coup|assassinat\w+)\b|"
    r"отставк|дефолт|девальвац|(повысил|снизил|сохранил)\w* базов\w+ ставк|перемири|военное положение", re.I)


def is_hard_breaking(it):
    """Узкий список событий, которые публикуем даже без ИИ (оригинальным заголовком)."""
    return bool(HARD_EVENT.search(it["title"]) and BREAK_ENTITY.search(it["title"])) and not is_junk(it)


def is_breaking(it):
    """Похоже на срочное: такие новости оцениваются первыми и уходят, не дожидаясь остальных."""
    text = f"{it['title']} {it.get('desc', '')[:200]}"
    return bool(BREAK_EVENT.search(text) and BREAK_ENTITY.search(text))


def _overlap(it, out):
    """Сколько слов ответа ИИ (латиница, числа, русские основы) встречается в исходной новости."""
    src = f"{it['title']} {it['desc']}".lower().replace(",", ".")
    toks = set(re.findall(r"[a-z]{3,}|\d+(?:\.\d+)?", out))
    stems = {w[:5] for w in re.findall(r"[а-яё]{5,}", out)}
    return sum(t in src for t in toks) + len(stems & {w[:5] for w in re.findall(r"[а-яё]{5,}", src)})


def plausible(it, r, others=()):
    """Ответ ИИ действительно про эту новость? Вторая защита от чужих ссылок (первая — коды вместо номеров).
    Бракуем, только если с этой новостью общих слов нет, а с другой из той же пачки — явно есть:
    у перевода с английского общих слов с оригиналом может не быть совсем, это не ошибка."""
    if not r.get("title_ru"):
        return True
    out = f"{r.get('title_ru')} {r.get('summary') or ''}".lower().replace(",", ".")
    if _overlap(it, out) > 0:
        return True
    if len({w[:5] for w in re.findall(r"[а-яё]{5,}", f"{it['title']} {it['desc']}".lower())}) >= 3:
        return False   # русский источник и ни одного общего слова
    return not any(_overlap(o, out) >= 2 for o in others if o is not it)


def score_chunk(items, recent_titles):
    for it in items:  # запасной режим без ИИ
        it.update(score=keyword_score(it), summary=it["desc"][:300], impact="", why="", kind="",
                  tags=[], title_ru="", watch=watch_hits(f"{it['title']} {it['desc']}"),
                  dup=False, ai=False)
    if not AI_ON or not items:
        return items
    # у каждой новости свой короткий код, а не порядковый номер: если ИИ собьётся со счёта,
    # ответ не «переедет» на соседнюю новость
    by_id = {}
    for n, it in enumerate(items):
        code = hashlib.sha1(it["link"].encode()).hexdigest()[:4]
        by_id[code if code not in by_id else f"{code}{n}"] = it
    lines = [f"[{code}] ({it['source']}) {it['title']}\n{it['desc'][:DESC_LIMIT]}" for code, it in by_id.items()]
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
        if not isinstance(r, dict):
            continue
        it = by_id.get(str(r.get("i", "")).strip("[] "))
        if it is None:
            continue
        if not plausible(it, r, items):
            print(f"ИИ перепутал новости — «{str(r.get('title_ru'))[:50]}» не про «{it['title'][:50]}»; повтор",
                  file=sys.stderr)
            continue
        try:
            it["score"] = max(1, min(5, int(r.get("score", it["score"]))))
        except (ValueError, TypeError):
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
    meta = ["⚡️ Срочно · без перевода" if it.get("raw") else importance(it["score"])]
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
    tags = " ".join(dict.fromkeys(filter(None, (hashtag(t) for t in it.get("tags", []) + it["watch"]))))
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
             for q in queue[:15]]
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

def skey(source, uid):
    """Короткий ключ «уже видели» (хэш вместо длинной ссылки — файл состояния в разы меньше)."""
    return hashlib.sha1(f"{source}|{uid}".encode()).hexdigest()[:14]


def load_state():
    st = {}
    for path in ([STATE_FILE, "seen.json"] if DRY_RUN else [STATE_FILE]):  # тест стартует с боевого состояния
        try:
            with open(path, encoding="utf-8") as f:
                st = json.load(f)
            break
        except (FileNotFoundError, json.JSONDecodeError):
            pass
    for k, v in (("seen", {}), ("feeds", []), ("recent", []), ("queue", []),
                 ("digests", []), ("calendar", {}), ("sent_log", [])):
        st.setdefault(k, v)
    if any(not re.fullmatch(r"[0-9a-f]{14}", k) for k in list(st["seen"])[:50]):  # старый формат ключей
        st["seen"] = {(k if re.fullmatch(r"[0-9a-f]{14}", k) else hashlib.sha1(k.encode()).hexdigest()[:14]): v
                      for k, v in st["seen"].items()}
    return st


def save_state(state):
    # вытесняем по времени, когда запись ПОСЛЕДНИЙ раз была в ленте (см. check_news), — иначе старые
    # статьи, которые ещё висят в ленте, через пару дней считались новыми и публиковались повторно
    if len(state["seen"]) > 20000:
        state["seen"] = dict(sorted(state["seen"].items(), key=lambda kv: kv[1])[-20000:])
    cutoff = int(time.time()) - 48 * 3600
    state["recent"] = [r for r in state["recent"] if r["ts"] > cutoff][-400:]
    state["queue"] = state["queue"][-300:]
    state["sent_log"] = [x for x in state["sent_log"] if x[0] > time.time() - 3600]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


# ───────────────────────── основной цикл ─────────────────────────

_cycle = {"n": 0}
def _sample_arg():
    """--sample N для bot.py; у bonds.py флаг --sample идёт без числа."""
    if "--sample" not in sys.argv:
        return 0
    nxt = sys.argv[sys.argv.index("--sample") + 1:][:1]
    return int(nxt[0]) if nxt and nxt[0].isdigit() else 5


SAMPLE = _sample_arg()


def load_source(urls, with_gnews=True):
    """→ (новости, ошибки, сколько адресов опрошено)."""
    items, errors, links, titles, tried = [], [], set(), set(), 0
    for url in urls:
        if "news.google.com" in url and not with_gnews:
            continue
        if url.startswith("sec:"):
            if not SEC_UA:
                continue
            url = url[4:]
        tried += 1
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
    return items, errors, tried


def check_news(state, now):
    now_ts = int(now.timestamp())
    _cycle["n"] += 1
    with_gnews = _cycle["n"] % GNEWS_EVERY == 1 or GNEWS_EVERY <= 1
    with ThreadPoolExecutor(12) as ex:  # все ленты параллельно — цикл занимает секунды, а не минуты
        loaded = list(ex.map(lambda f: load_source(f[2], with_gnews), FEEDS))
    fresh, log = [], []
    for (source, region, urls, *rest), (items, errors, tried) in zip(FEEDS, loaded):
        opts = rest[0] if rest else {}
        if errors:
            print(f"[{source}] часть адресов не ответила: {'; '.join(errors)}", file=sys.stderr)
        if not items:
            if tried:
                print(f"[{source}] не загрузился", file=sys.stderr)
            continue
        first_time = source not in state["feeds"]
        if SAMPLE:  # тестовый режим: берём N свежих из каждой ленты, даже если уже видели
            items = sorted(items, key=lambda x: x["ts"] or 0)[-SAMPLE:]
        for it in items:
            key = skey(source, it["id"])
            known = key in state["seen"]
            state["seen"][key] = now_ts          # обновляем метку, пока запись есть в ленте
            if (known or first_time) and not SAMPLE:
                continue
            it["source"], it["region"] = source, region
            text = f"{it['title']} {it['desc'][:300]}"
            if it["ts"] and now_ts - it["ts"] > MAX_AGE_HOURS * 3600:
                log.append(("   ", "старая (фильтр)", it))
            elif len(it["title"]) < 15:
                pass
            elif opts.get("must") and not re.search(opts["must"], text, re.I):
                log.append(("   ", "не по теме ленты", it))
            elif opts.get("url") and not re.search(opts["url"], it["link"], re.I):
                log.append(("   ", "не тот раздел", it))
            elif source == "KASE" and KASE_ROUTINE.search(it["title"]):
                log.append(("   ", "→ Bonds (bonds.py)", it))
            else:
                fresh.append(it)
        if first_time and not SAMPLE:
            state["feeds"].append(source)
            state["recent"] += [{"title": it["title"], "ts": now_ts} for it in items[:30]]
            print(f"[{source}] подключён, в ленте {len(items)} новостей")

    fresh.sort(key=lambda x: x["ts"] or 0)
    fresh = fresh[-MAX_PER_CYCLE:]
    if not fresh and not state.get("pending"):
        print(f"{now:%H:%M} новых нет")
        return

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
    todo = [it for it in state.pop("pending", []) if not is_junk(it)] + unique
    for it in todo:
        it["prio"] = is_breaking(it)
    todo.sort(key=lambda x: (not x["prio"], x.get("ts") or 0))   # срочные — первыми
    if (AI_ON and todo and not SAMPLE and not any(it["prio"] for it in todo)
            and len(todo) < HOLD_MIN and _cycle["n"] % GNEWS_EVERY != 0):
        state["pending"] = [{k: it.get(k) for k in ("id", "title", "link", "desc", "image", "ts", "source",
                                                     "region", "tries")} for it in todo]
        print(f"{now:%H:%M} несрочных {len(todo)} — подождут следующей пачки")
        return
    quiet = in_quiet(now)
    sent = {r: 0 for r in REGIONS}
    all_ai = True

    def queue(it, summary=None):
        state["queue"].append({"title": it.get("title_ru") or it["title"], "link": it["link"],
                               "score": it["score"], "summary": it["summary"] if summary is None else summary,
                               "watch": it["watch"], "ts": it["ts"], "source": it["source"], "region": it["region"]})

    def route(it):
        """Куда новость: вернуть True — отправить сейчас."""
        nonlocal all_ai
        if AI_ON and not it.get("ai"):
            all_ai = False
            if is_hard_breaking(it):   # срочное не ждёт ИИ: шлём оригинальный заголовок
                it.update(raw=True, score=4)
                state["recent"].append({"title": it["title"], "ts": now_ts})
                log.append(("4/5", f"⚡️ без ИИ → {it['region']}", it))
                return True
            it["tries"] = (it.get("tries") or 0) + 1
            if it["tries"] < MAX_AI_RETRIES:
                state.setdefault("pending", []).append(
                    {k: it.get(k) for k in ("id", "title", "link", "desc", "image", "ts", "source", "region", "tries")})
                log.append(("   ", "ждёт ИИ (повтор)", it))
            else:
                log.append(("   ", "без ИИ — отброшено", it))
            return False
        state["recent"].append({"title": it["title"], "ts": now_ts})
        if it.get("title_ru"):
            state["recent"].append({"title": it["title_ru"], "ts": now_ts})
        sc, reg = f"{it['score']}/5", it["region"]
        if it["dup"]:
            log.append((sc, "ДУБЛЬ (ИИ)", it)); return False
        if it.get("kind") == "other":
            log.append((sc, "не новость (ИИ)", it)); return False
        if it["score"] < DROP_BELOW:
            log.append((sc, "отброшено", it)); return False
        hour = sum(1 for t, r in state["sent_log"] if r == reg and t > now_ts - 3600)
        ok = it["score"] >= MIN_SCORE[reg] and (not quiet or it["score"] == 5 or (it["prio"] and it["score"] >= 4))
        if ok and it["score"] < 5 and hour >= MAX_PER_HOUR[reg]:
            queue(it); log.append((sc, f"лимит в час → дайджест {reg}", it)); return False
        if ok:
            state["sent_log"].append([now_ts, reg]); sent[reg] += 1
            log.append((sc, f"→ {reg}", it)); return True
        queue(it); log.append((sc, f"в дайджест {reg}", it))
        return False

    def add_image(it):
        if not it.get("image"):
            it["image"] = page_meta(it["link"])[1]
        return it

    # оцениваем пачками и СРАЗУ отправляем каждую пачку — срочное не ждёт, пока ИИ разберёт всё остальное
    for i in range(0, len(todo), AI_CHUNK):
        chunk = score_chunk(todo[i:i + AI_CHUNK], recent_titles)
        to_send = [it for it in chunk if route(it)]
        recent_titles += [t for it in chunk if it.get("ai") and not it["dup"]
                          for t in (it["title"], it.get("title_ru")) if t]
        if SEND_IMAGES and to_send:
            with ThreadPoolExecutor(8) as ex:
                to_send = list(ex.map(add_image, to_send))
        for it in to_send:
            send_post(chats_for(it["region"]), format_post(it), it.get("image"))
            time.sleep(0 if DRY_RUN else 1.1)
        save_state(state)

    print("─" * 70)
    for sc, what, it in sorted(log, key=lambda x: x[0], reverse=True):
        print(f"{sc}  {what:<26} [{it['source']}] {(it.get('title_ru') or it['title'])[:85]}")
    print("─" * 70)
    if AI_ON and not all_ai:
        print("⚠️ ИИ ответил не на все новости — они попробуются ещё раз (см. ошибки Gemini выше)")
    elif not AI_ON:
        print("⚠️ Ключ ИИ не задан — оценка по ключевым словам")
    print(f"{now:%H:%M} новых {len(todo) + len(junk)}, отправлено " + ", ".join(f"{r} {n}" for r, n in sent.items()))


def run_once(state):
    now = datetime.now(LOCAL_TZ)
    for step in (check_news, check_calendar, maybe_digest):
        try:
            step(state, now)
        except Exception as e:
            print(f"Ошибка в {step.__name__}: {e!r}", file=sys.stderr)
        save_state(state)


def main():
    if not TOKEN and not DRY_RUN:
        sys.exit("Укажите TG_TOKEN")
    if DRY_RUN:
        print(f"🧪 DRY_RUN: в Telegram ничего не уходит, состояние пишется в {STATE_FILE}")
    missing = [r for r in REGIONS if not os.environ.get(REGIONS[r]["env"])]
    if missing and not PERSONAL and not DRY_RUN:
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
    if not state.get("v4"):
        state["queue"] = [q for q in state["queue"] if q.get("score", 0) >= DROP_BELOW]  # старый «фон» не нужен
        state.pop("v2", None)
    if not state.get("v4") and not DRY_RUN:
        if os.environ.get("TG_CHAT_MACRO"):
            send_text(chats_for("MACRO"), "🌍 <b>Vitis Macro News</b> подключён\n\nСюда будет приходить геополитика: "
                      "войны и удары, перемирия, санкции, пошлины, смена власти — то, что двигает нефть, валюты и рынки.")
        else:
            send("⚠️ Не задан секрет TG_CHAT_MACRO — геополитика пока идёт сюда, в личку.")
        state["v4"] = True
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
