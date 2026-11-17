#!/usr/bin/env python3
"""
Vitis Bonds Bot — облигации KASE и AIX → тема «Bonds Alert».

1) Алерты (каждые ~30 мин):
   🆕 новые выпуски и размещения · 📄 отчётность эмитентов · ⚠️ дефолты, 🔁 выкупы, 🚫 делистинг
   ⭐ рейтинги · 💸 выплаты купонов (собираются в одну сводку за день, чтобы не спамить)
   У каждого алерта пометка биржи: 🏛 KASE / 🌐 AIX
2) Ежедневно после торгов (по умолчанию 18:30 Алматы, Пн–Пт), данные — API KASE (все выпуски):
   графики KZT (к кривой ГЦБ) и USD (к кривой UST), G-спреды, средние по секторам,
   самые широкие/узкие спреды, изменение кривой за день,
   + Excel со ВСЕМИ облигациями KASE, включая выпуски без сделок (купон, срок, объём, bid/offer).

Использует общие функции из bot.py (тот же бот, тот же ключ Gemini).
  python bonds.py --once      — проверка алертов + отчёт по кривой, если пора
  python bonds.py --report    — отправить отчёт по кривой прямо сейчас
Нужны пакеты numpy, matplotlib, openpyxl (ставятся в workflow).

Секреты: TG_TOKEN, TG_CHAT_BONDS (-100XXXXXXXXXX:номер_темы), GEMINI_API_KEY, TG_CHAT_ID (служебные)
"""
import html
import io
import math
import json
import os
import re
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import bot  # общие функции новостного бота
from bot import (LOCAL_TZ, AI_ON, fetch, clean, esc, gnews, parse_feed, parse_html_news,
                 ask_ai, extract_json, send_text, target, similar, local)

# ════════════════════ НАСТРОЙКИ ════════════════════

REPORT_TIME = (18, 30)        # когда отправлять кривую (Алматы), только Пн–Пт
STATE_FILE = os.environ.get("BONDS_STATE_FILE", "bonds_state.json")
CHATS = [c.strip() for c in os.environ.get("TG_CHAT_BONDS", "").split(",") if c.strip()] or bot.PERSONAL


ALERT_FEEDS = [
    ("KASE", "html:https://kase.kz/ru/information/news/all"),
    ("KASE", "html:https://kase.kz/ru/information/news/market"),
    ("KASE", "html:https://kase.kz/ru/information/news/companies"),
    ("KASE", "html:https://kase.kz/ru/information/news/exchange"),
    ("KASE", gnews("site:kase.kz облигации when:2d", "ru", "KZ")),
    ("AIX", "https://aix.kz/feed/"),
    ("AIX", gnews("site:aix.kz (bonds OR notes OR sukuk OR Eurobond) when:3d")),
    ("AIX", gnews('"AIX" Kazakhstan (bonds OR Eurobonds) when:2d')),
    ("Рейтинги", gnews("(Fitch OR Moody's OR \"S&P\") Kazakhstan rating when:2d")),
    ("Рейтинги", gnews("рейтинг (Fitch OR Moody's OR S&P OR \"Эксперт РА\") Казахстан облигации when:2d", "ru", "KZ")),
    ("Облигации KZ", gnews("облигации Казахстан (размещение OR купон OR дефолт OR выкуп) when:1d", "ru", "KZ")),
]

# Предфильтр: в тексте должно быть что-то про долг
BOND_RE = re.compile(
    r"облигац|бонд|купон|еврооблигац|сукук|ноты|коммерческ\w+ облигац|рейтинг|отчетност|отчётност|"
    r"финансов\w+ (отчет|отчёт|результат)|дефолт|выкуп|погашени|размещени|листинг|делистинг|"
    r"\bbonds?\b|eurobond|\bnotes?\b|sukuk|coupon|rating|outlook|default|buyback|repurchase|"
    r"redemption|placement|listing|financial (statements|results)|IFRS|МСФО", re.I)
# Отсекаем то, что к облигациям не относится
NOT_BOND_RE = re.compile(
    r"закрылись торги следующими инструментами рынка иностранных валют|"
    r"(приостановлены|возобновлены) торги .{0,80}по операциям репо|"
    r"KASE Global|ETF|паев\w* инвестиционн", re.I)

CATEGORIES = {
    "placement": "🆕 Размещение",
    "report": "📄 Отчётность",
    "default": "⚠️ Дефолт / невыплата",
    "buyback": "🔁 Выкуп / досрочное погашение",
    "delisting": "🚫 Делистинг",
    "rating": "⭐ Рейтинг",
    "coupon": "💸 Купон",
}

# Сектора по названию эмитента (первое совпадение)
SECTORS = [
    ("Квазигос", r"development bank|банк развития|sustainability fund|фонд устойчив|жилищн|housing|"
                 r"фонд развития промышл|industrial development|baiterek|байтерек|аграрн|agrarian|kazagro|казагро|"
                 r"samruk|самрук|damu|даму|kegoc|kazakhstan temir|темір|қазақстан темір|казмунайгаз|kazmunaygas|"
                 r"казатомпром|kazatomprom|qazaqgaz|казтрансойл|kaztransoil|отбасы|otbasy|"
                 r"социально-предпринимат|social.entrepreneurial|спк |национальн\w+ (компан|управляющ)|"
                 r"national (company|managing)|фонд проблемных|distressed|казахстанская ипотечная|kazakhstan mortgage|"
                 r"центр развития|development fund|qazaqstan investment|kazakh invest|kazakhstan project|"
                 r"инвестиционный фонд казахстана|kegoc|kazpost|казпочта|казахтелеком|kazakhtelecom"),
    ("Банки", r"\bbank\b|банк|halyk|kaspi|каспи|bereke|freedom bank|home credit|nurbank|altyn|shinhan"),
    ("МФО", r"\bmfo\b|мфо|microfinanc|микрофинанс|кредитн\w+ товарищ|ломбард"),
    ("Лизинг", r"leasing|лизинг"),
    ("Финансы прочие", r"finance|capital|invest|инвест|финанс|insurance|страхов|broker|брокер|securities|"
                       r"холдинг|holding|фонд|fund"),
    ("Реальный сектор", r".*"),
]

# ═══════════════════════════════════════════════════



# ───────────────────────── Telegram ─────────────────────────

def send_photo_file(chats, png_bytes, caption):
    """Отправка картинки из памяти (multipart)."""
    for chat in chats:
        boundary = uuid.uuid4().hex
        fields = {**target(chat), "caption": caption[:1024], "parse_mode": "HTML"}
        body = b""
        for k, v in fields.items():
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="curve.png"\r\n'
                 f"Content-Type: image/png\r\n\r\n").encode() + png_bytes + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{bot.TOKEN}/sendPhoto", data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            urllib.request.urlopen(req, timeout=60).read()
        except urllib.error.HTTPError as e:
            print(f"Telegram sendPhoto error {e.code}: {e.read()[:200]}", file=sys.stderr)
            send_text([chat], caption)
        except Exception as e:
            print(f"Telegram sendPhoto error: {e}", file=sys.stderr)


# ───────────────────────── данные KASE (API) ─────────────────────────

API_CORP = ("https://kase.kz/api/instruments/securities?ticker_category=alternative_commercial_bonds,"
            "alternative_bonds,main_bonds,main_commercial_bonds,private_bonds&category=bond&is_excluded=false&search=")
API_GOV = "https://kase.kz/api/instruments/securities?category=gsec&is_excluded=false&search="
UST_URL = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
           "{y}/all?type=daily_treasury_yield_curve&field_tdr_date_value={y}&page&_format=csv")
BOND_URL = "https://kase.kz/ru/investors/bonds/{code}"
FRESH_DAYS_CORP = 30   # цена считается актуальной, если последняя сделка/котировка не старше
FRESH_DAYS_GOV = 30
MIN_YEARS = 0.25        # бумаги ближе 3 мес. к погашению — в средние и топы не берём (доходность шумная)
ANOMALY_BP = -300       # спред ниже −300 бп к своей кривой — подозрительно (валютная индексация / случайная цена)


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": bot.UA, "Accept": "application/json",
                                               "Referer": "https://kase.kz/ru/markets/corporate-bonds"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def sector_of(name):
    c = name.lower()
    for sec, pat in SECTORS:
        if re.search(pat, c):
            return sec
    return "Реальный сектор"


def norm(x, now):
    t = x.get("ticker") or {}
    last = None
    if x.get("date0"):
        try:
            last = datetime.fromisoformat(x["date0"][:19]).replace(tzinfo=LOCAL_TZ)
        except ValueError:
            pass
    years = x.get("ytm")
    if years is None and x.get("dtm") is not None:
        years = x["dtm"] / 365
    if years is None and t.get("finish_date"):
        try:
            years = (datetime.fromisoformat(t["finish_date"]).replace(tzinfo=LOCAL_TZ) - now).days / 365
        except ValueError:
            pass
    y = x.get("dohod")
    name_ru, name_en = x.get("org_name_ru") or "", x.get("org_name_en") or ""
    return {
        "ticker": x.get("code") or "", "issuer": (x.get("org_short_name_ru") or name_ru).strip(),
        "issuer_full": name_ru, "currency": t.get("currency") or x.get("currency_type") or "?",
        "coupon": t.get("cupon"), "maturity": t.get("finish_date") or "", "years": years,
        "yield": y if (y or 0) > 0 else None, "price": x.get("price"),
        "bid": x.get("best_bid"), "offer": x.get("best_offer"), "last": last,
        "placed": x.get("volume"), "issued": x.get("volume_release"),
        "board": x.get("board_ru") or "", "category": t.get("ticker_category") or "",
        "sector": sector_of(f"{name_ru} {name_en}"),
        "isin": t.get("nin") or "",
    }


def is_fresh(b, now, days):
    return b["yield"] is not None and b["last"] is not None and (now - b["last"]).days <= days


def load_kase(now):
    corp = [norm(x, now) for x in fetch_json(API_CORP)]
    gov_all = [norm(x, now) for x in fetch_json(API_GOV)]
    gov, muni, ust_kase = [], [], []
    for b in gov_all:
        if b["category"] in ("gsecs_mfrk", "gsecs_nbrk") and b["currency"] == "KZT":
            b["sector"] = "ГЦБ"; gov.append(b)
        elif b["category"] == "gsecs_municipal_bonds":
            b["sector"] = "Муниципальные"; muni.append(b)
        elif b["category"] == "gsecs_international_bonds" and re.search(r"США", b["issuer"]):
            ust_kase.append(b)
    print(f"KASE API: корпоративных {len(corp)}, ГЦБ {len(gov)}, муниципальных {len(muni)}, UST {len(ust_kase)}")
    return corp, gov, muni, ust_kase


KASE_GOV_PAGE = "https://kase.kz/ru/markets/government-securities"


def load_kase_curve():
    """Официальная спот-кривая ГЦБ KASE со страницы ГЦБ (лежит в HTML) →
    [(дата, [сроки, лет], [доходность, % — непрерывное начисление])], от старой к новой."""
    page = fetch(KASE_GOV_PAGE, timeout=40).decode("utf-8", "ignore")
    i = page.find('"spot":[')
    if i < 0:
        i = page.find('&q;spot&q;:[')  # на случай экранированного JSON
        if i < 0:
            raise RuntimeError("на странице ГЦБ не найден блок spot")
        page = page.replace("&q;", '"')
        i = page.find('"spot":[')
    data, _ = json.JSONDecoder().raw_decode(page[i + len('"spot":'):])
    out = []
    for d in data:
        pts = sorted((float(p["x"]), float(p["y"])) for p in d.get("dataset", []) if p.get("x") is not None)
        if len(pts) > 10:
            out.append((d.get("calc_date", ""), [p[0] for p in pts], [p[1] for p in pts]))
    if not out:
        raise RuntimeError("в блоке spot нет точек")
    return sorted(out, key=lambda t: t[0])


def load_ust(now):
    """Официальная кривая UST (treasury.gov) → [(лет, доходность)]."""
    tenors = {"1 Mo": 1/12, "2 Mo": 2/12, "3 Mo": 0.25, "4 Mo": 1/3, "6 Mo": 0.5, "1 Yr": 1, "2 Yr": 2,
              "3 Yr": 3, "5 Yr": 5, "7 Yr": 7, "10 Yr": 10, "20 Yr": 20, "30 Yr": 30}
    for y in (now.year, now.year - 1):
        try:
            text = fetch(UST_URL.format(y=y), timeout=40).decode("utf-8", "ignore").splitlines()
            head = [h.strip().strip('"') for h in text[0].split(",")]
            row = [c.strip().strip('"') for c in text[1].split(",")]  # первая строка — последняя дата
            pts = [(tenors[h], float(v)) for h, v in zip(head, row) if h in tenors and v]
            if len(pts) >= 5:
                print(f"UST: {row[0]}, {len(pts)} сроков")
                return pts, row[0]
        except Exception as e:
            print(f"UST {y} недоступна: {e}", file=sys.stderr)
    return None, None


# ───────────────────────── кривая Nelson-Siegel ─────────────────────────

def ns_yield(m, b0, b1, b2, tau):
    import numpy as np
    m = np.maximum(np.asarray(m, dtype=float), 1e-6)
    x = m / tau
    f = (1 - np.exp(-x)) / x
    return b0 + b1 * f + b2 * (f - np.exp(-x))


def fit_ns(years, yields):
    import numpy as np
    m, y = np.asarray(years, float), np.asarray(yields, float)
    best = None
    for tau in np.linspace(0.076, 5, 200):  # диапазон τ как в методике KASE — без «загиба» на длинном конце
        x = m / tau
        f = (1 - np.exp(-x)) / x
        X = np.column_stack([np.ones_like(m), f, f - np.exp(-x)])
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        sse = float(((X @ beta - y) ** 2).sum())
        if best is None or sse < best[0]:
            best = (sse, *beta, tau)
    _, b0, b1, b2, tau = best
    return (float(b0), float(b1), float(b2), float(tau)), (best[0] / len(m)) ** 0.5


def robust_fit(points):
    """Подгонка с отсевом выбросов (точки дальше 2,5 п.п. от первой подгонки)."""
    import numpy as np
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    params, rmse = fit_ns(xs, ys)
    keep = [p for p in points if abs(p[1] - float(ns_yield([p[0]], *params)[0])) < 2.5]
    if 5 <= len(keep) < len(points):
        params, rmse = fit_ns([p[0] for p in keep], [p[1] for p in keep])
    return params, rmse, len(keep)


# ───────────────────────── отчёт ─────────────────────────

TENORS = [0.5, 1, 2, 3, 5, 7, 10]


def build_report(now, prev):
    import numpy as np
    corp, gov, muni, ust_kase = load_kase(now)
    # индексируемые на инфляцию ГЦБ (МЕИКАМ/МУИКАМ: тикеры MTC/MTM/MUJ…, купон 1–3%) дают другую доходность —
    # в номинальную кривую их не берём
    indexed = re.compile(r"^(MTC|MTM|MUJ|MKJ|MEJ)", re.I)
    gov_pts = [(b["years"], b["yield"]) for b in gov
               if is_fresh(b, now, FRESH_DAYS_GOV) and b["years"] and 0.05 < b["years"] < 31
               and not indexed.match(b["ticker"]) and not (b["coupon"] is not None and 0 < b["coupon"] < 3)]
    if len(gov_pts) < 5:
        raise RuntimeError(f"мало ГЦБ с актуальной доходностью ({len(gov_pts)}) — кривую не построить")
    kzt, kzt_rmse, kzt_n = robust_fit(gov_pts)
    own = lambda m: float(ns_yield([max(m, 0.02)], *kzt)[0])

    # Основная кривая — официальная KASE (спот, непрерывное начисление) → переводим в годовую
    # доходность exp(z)−1, чтобы сравнивать с доходностями облигаций (они годовые)
    official = None
    try:
        days = load_kase_curve()
        d, xs, ys = days[-1]
        prev_d = days[-2] if len(days) > 1 else None
        cont = lambda m, xs=xs, ys=ys: float(np.interp(m, xs, ys))
        official = {"date": d, "cont": cont,
                    "prev_date": prev_d[0] if prev_d else None,
                    "prev_cont": (lambda m, xs=prev_d[1], ys=prev_d[2]: float(np.interp(m, xs, ys))) if prev_d else None}
        print(f"Официальная кривая KASE: {d}, точек {len(xs)}; 1Y={cont(1):.2f}% (непр.)")
    except Exception as e:
        print(f"Официальная кривая KASE недоступна ({e}) — строю свою по сделкам", file=sys.stderr)
    to_annual = lambda z: (math.exp(z / 100) - 1) * 100
    if official:
        curve = lambda m: to_annual(official["cont"](m))
        prev_curve = (lambda m: to_annual(official["prev_cont"](m))) if official["prev_cont"] else None
    else:
        curve, prev_curve = own, None

    ust_pts, ust_date = load_ust(now)
    if not ust_pts:  # запасной вариант — UST, торгующиеся на KASE
        ust_pts = [(b["years"], b["yield"]) for b in ust_kase if is_fresh(b, now, 90) and b["years"]]
        ust_date = "KASE"
    usd = robust_fit(ust_pts)[0] if ust_pts and len(ust_pts) >= 5 else None
    ucurve = (lambda m: float(ns_yield([max(m, 0.02)], *usd)[0])) if usd else None

    universe = corp + muni
    for b in universe:
        b["fresh"] = is_fresh(b, now, FRESH_DAYS_CORP)
        b["g"] = b["cg"] = None
        if not b["years"] or b["years"] <= 0:
            continue
        base = curve if b["currency"] == "KZT" else ucurve if b["currency"] == "USD" else None
        if base:
            if b["yield"] is not None:
                b["g"] = (b["yield"] - base(b["years"])) * 100
            if b["coupon"]:
                b["cg"] = (b["coupon"] - base(b["years"])) * 100   # купон минус кривая — для бумаг без цены

    def sector_table(bonds):
        groups = {}
        for b in bonds:
            groups.setdefault(b["sector"], []).append(b)
        return sorted(((s, len(v), float(np.mean([x["yield"] for x in v])), float(np.median([x["g"] for x in v])))
                       for s, v in groups.items()), key=lambda r: -r[3])

    def flag(b):
        if b["years"] is not None and b["years"] < MIN_YEARS:
            return "до погашения < 3 мес."
        if b["g"] is not None and b["g"] < ANOMALY_BP:
            return "доходность намного ниже кривой — проверить (валютная индексация / случайная цена)"
        if b["g"] is not None and b["g"] > 3000:
            return "аномально высокая доходность"
        return ""
    for b in universe:
        b["flag"] = flag(b) if b["fresh"] and b["g"] is not None else ""

    def clean_set(cur):
        return [b for b in universe if b["currency"] == cur and b["fresh"] and b["g"] is not None
                and not b["flag"] and b["years"] < 31]
    kzt_live, usd_live = clean_set("KZT"), clean_set("USD")
    anomalies = [b for b in universe if b["flag"] and b["flag"] != "до погашения < 3 мес."]
    no_deals = [b for b in universe if not b["fresh"]]
    return {"kzt": kzt, "kzt_rmse": kzt_rmse, "kzt_n": kzt_n, "usd": usd, "ust_date": ust_date,
            "curve": curve, "prev_curve": prev_curve, "official": official,
            "ust_pts": ust_pts or [], "gov": gov_pts, "points": {t: curve(t) for t in TENORS},
            "kzt_live": kzt_live, "usd_live": usd_live, "no_deals": no_deals, "anomalies": anomalies, "all": universe, "gov_all": gov,
            "kzt_sectors": sector_table(kzt_live), "usd_sectors": sector_table(usd_live), "prev": prev}


SECTOR_COLORS = {"Квазигос": "#2a9d8f", "Банки": "#457b9d", "МФО": "#e76f51", "Лизинг": "#f4a261",
                 "Финансы прочие": "#9b5de5", "Реальный сектор": "#e9c46a", "Муниципальные": "#90be6d"}


def draw_chart(rep, date_str):
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    kzt_label = ("Кривая ГЦБ KASE (официальная)" if rep["official"]
                 else "Кривая ГЦБ (по сделкам)")
    panels = [("KZT", rep["curve"], rep["gov"], rep["kzt_live"], kzt_label)]
    if rep["usd"] and rep["usd_live"]:
        panels.append(("USD", lambda m: float(ns_yield([m], *rep["usd"])[0]), rep["ust_pts"], rep["usd_live"], "Кривая UST"))
    fig, axes = plt.subplots(1, len(panels), figsize=(7.2 * len(panels), 6), dpi=150, squeeze=False)
    fig.patch.set_facecolor("#0f1720")
    for ax, (cur, fn, base_pts, bonds, label) in zip(axes[0], panels):
        ax.set_facecolor("#0f1720")
        maxm = min(15, max([p[0] for p in base_pts if p[0] <= 15] + [b["years"] for b in bonds] + [5]) + 0.5)
        last = maxm if (cur == "KZT" and rep["official"]) else min(maxm, max(p[0] for p in base_pts) + 1)
        xs = np.linspace(0.05, last, 300)
        ax.plot(xs, [fn(x) for x in xs], color="#ffffff", lw=2.4, label=label, zorder=3)
        if cur == "KZT" and rep["prev_curve"]:
            ax.plot(xs, [rep["prev_curve"](x) for x in xs], color="#8a99a8", lw=1.2, ls="--",
                    label=f"Предыдущий день ({'.'.join(reversed(rep['official']['prev_date'].split('-')))})", zorder=2)
        ax.scatter([p[0] for p in base_pts if p[0] <= maxm], [p[1] for p in base_pts if p[0] <= maxm],
                   s=16, color="#cfd8e3", alpha=0.6, label="Сделки ГЦБ" if cur == "KZT" else "UST", zorder=4)
        for s, color in SECTOR_COLORS.items():
            pts = [b for b in bonds if b["sector"] == s and b["years"] <= maxm]
            if pts:
                ax.scatter([b["years"] for b in pts], [b["yield"] for b in pts], s=30, color=color,
                           edgecolor="#0f1720", lw=0.5, label=f"{s} ({len(pts)})", zorder=5)
        ys = [b["yield"] for b in bonds if b["years"] <= maxm] + [p[1] for p in base_pts if p[0] <= maxm]
        if ys:
            ax.set_ylim(np.percentile(ys, 2) - 1.5, np.percentile(ys, 97) + 1.5)
        ax.set_xlim(0, maxm)
        for sp in ax.spines.values():
            sp.set_color("#2b3a4a")
        ax.tick_params(colors="#cfd8e3")
        ax.grid(color="#1e2b38", lw=0.8)
        ax.set_xlabel("Срок до погашения, лет", color="#cfd8e3")
        ax.set_ylabel("Доходность, % годовых", color="#cfd8e3")
        ax.set_title(f"{cur}: {label} и облигации KASE", color="#ffffff", fontsize=11.5, loc="left")
        leg = ax.legend(loc="upper right", fontsize=8, facecolor="#16212c", edgecolor="#2b3a4a")
        for t in leg.get_texts():
            t.set_color("#e6edf3")
    fig.suptitle(f"Облигации KASE · {date_str}", color="#ffffff", fontsize=13, x=0.01, ha="left")
    kz_src = ("кривая ГЦБ — официальная KASE, переведена в годовую доходность" if rep["official"]
              else "кривая ГЦБ — Nelson-Siegel по сделкам")
    fig.text(0.01, 0.01, f"Источник: KASE (бумаги с ценой не старше 30 дней); {kz_src}; "
             f"UST — treasury.gov {rep['ust_date'] or '—'}", color="#6d7f8f", fontsize=7.5)
    buf = io.BytesIO()
    fig.tight_layout(rect=(0, 0.03, 1, 0.95))
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()


def build_excel(rep, date_str):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    cols = [("Тикер", 12), ("Эмитент", 30), ("Сектор", 16), ("Площадка", 14), ("Валюта", 8), ("Купон, %", 9),
            ("Погашение", 11), ("Лет до погаш.", 10), ("Доходность, %", 12), ("G-спред, бп", 11),
            ("Купон − кривая, бп", 14), ("Цена, %", 9), ("Bid", 9), ("Offer", 9), ("Посл. цена", 11),
            ("Размещено, ₸", 16), ("Объём выпуска, ₸", 16), ("ISIN", 14), ("Примечание", 40), ("Карточка KASE", 40)]
    sheets = [
        ("KZT с ценой", [b for b in rep["all"] if b["currency"] == "KZT" and b["fresh"]], "g"),
        ("KZT без сделок", [b for b in rep["all"] if b["currency"] == "KZT" and not b["fresh"]], "cg"),
        ("USD", [b for b in rep["all"] if b["currency"] == "USD"], "g"),
        ("Другие валюты", [b for b in rep["all"] if b["currency"] not in ("KZT", "USD")], None),
        ("ГЦБ", rep["gov_all"], None),
    ]
    head_fill = PatternFill("solid", fgColor="1F3247")
    for idx, (name, bonds, sort_key) in enumerate(sheets):
        ws = wb.active if idx == 0 else wb.create_sheet()
        ws.title = name
        for i, (h, w) in enumerate(cols, 1):
            c = ws.cell(row=1, column=i, value=h)
            c.font, c.fill = Font(bold=True, color="FFFFFF"), head_fill
            c.alignment = Alignment(wrap_text=True, vertical="center")
            ws.column_dimensions[get_column_letter(i)].width = w
        if sort_key:
            bonds = sorted(bonds, key=lambda b: -(b.get(sort_key) if b.get(sort_key) is not None else -1e9))
        for r, b in enumerate(bonds, 2):
            vals = [b["ticker"], b["issuer"], b["sector"], b["board"], b["currency"], b["coupon"],
                    b["maturity"], round(b["years"], 2) if b["years"] else None,
                    round(b["yield"], 2) if b["yield"] is not None else None,
                    round(b["g"]) if b.get("g") is not None else None,
                    round(b["cg"]) if b.get("cg") is not None else None,
                    b["price"], b["bid"], b["offer"], b["last"].strftime("%d.%m.%Y") if b["last"] else None,
                    b["placed"], b["issued"], b["isin"], b.get("flag", ""), BOND_URL.format(code=b["ticker"])]
            for i, v in enumerate(vals, 1):
                c = ws.cell(row=r, column=i, value=v)
                if i in (16, 17):
                    c.number_format = "#,##0"
            ws.cell(row=r, column=20).hyperlink = BOND_URL.format(code=b["ticker"])
        ws.freeze_panes = "C2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{max(2, len(bonds) + 1)}"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def fmt_pct(x):
    return f"{x:.2f}".replace(".", ",") + "%"


def fmt_bp(x):
    return f"{x:+.0f} бп"


def report_texts(rep, date_str, coupons):
    off = rep["official"]
    lines = []
    if off:
        for t in TENORS:
            z = off["cont"](t)
            d = f"{round((z - off['prev_cont'](t)) * 100):+d}" if off["prev_cont"] else " —"
            label = f"{t:g}Y" if t >= 1 else f"{int(t * 12)}M"
            lines.append(f"{label:<4}{fmt_pct(z):>8}{fmt_pct(rep['points'][t]):>9}{d:>5}")
        table = "<pre>Срок   KASE   Годовая  Δ,бп\n" + "\n".join(lines) + "</pre>\n"
        dd = "-".join(reversed(off["date"].split("-"))).replace("-", ".")
        head = (f"<b>Кривая ГЦБ KASE на {esc(dd)}</b>\n<i>«KASE» — как на сайте (спот, непрерывное "
                "начисление); «Годовая» — та же ставка в годовой доходности, к ней считаются спреды</i>\n")
    else:
        prev = rep["prev"].get("points", {})
        for t in TENORS:
            y = rep["points"][t]
            p = prev.get(str(t))
            d = f"{(y - p) * 100:+.0f}" if p is not None else "  —"
            label = f"{t:g}Y" if t >= 1 else f"{int(t * 12)}M"
            lines.append(f"{label:<5}{fmt_pct(y):>8}{d:>7}")
        table = "<pre>Срок  Доходн.  Δ,бп\n" + "\n".join(lines) + "</pre>\n"
        head = "<b>Кривая ГЦБ (по сделкам — официальная KASE недоступна)</b>\n"
    slope = (rep["points"][10] - rep["points"][1]) * 100
    caption = (f"📉 <b>Облигации KASE · {date_str}</b>\n\n" + head + table +
               f"Наклон 10Y–1Y: <b>{fmt_bp(slope)}</b> "
               f"({'нормальная' if slope > 25 else 'инвертированная' if slope < -25 else 'плоская'})")

    def sec_block(title, rows):
        out = [f"<pre>{'Сектор':<15}{'N':>4}{'YTM':>8}{'G-спред':>11}"]
        for s, n, y, g in rows:
            out.append(f"{s[:15]:<15}{n:>4}{fmt_pct(y):>8}{g:>+8.0f} бп")
        return title + "\n" + "\n".join(out) + "</pre>"

    def row(b):
        return (f"▫️ <b>{esc(b['ticker'])}</b> · {esc(b['issuer'][:26])} — "
                f"{fmt_pct(b['yield'])}, {b['years']:.1f} г., <b>{fmt_bp(b['g'])}</b>")
    parts = [sec_block("🇰🇿 <b>KZT: средние по секторам</b> <i>(медианный G-спред к ГЦБ)</i>", rep["kzt_sectors"])]
    kz = sorted(rep["kzt_live"], key=lambda b: -b["g"])
    if kz:
        parts.append("📈 <b>Самые широкие спреды (KZT)</b>\n" + "\n".join(row(b) for b in kz[:5]))
        parts.append("📉 <b>Самые узкие спреды (KZT)</b>\n" + "\n".join(row(b) for b in kz[-5:][::-1]))
    if rep["usd_sectors"]:
        parts.append(sec_block(f"🇺🇸 <b>USD: средние по секторам</b> <i>(G-спред к UST, {esc(rep['ust_date'] or '')})</i>",
                               rep["usd_sectors"]))
        us = sorted(rep["usd_live"], key=lambda b: -b["g"])
        parts.append("📈 <b>Самые широкие спреды (USD)</b>\n" + "\n".join(row(b) for b in us[:5]))
    nd = rep["no_deals"]
    by_cur = {}
    for b in nd:
        by_cur[b["currency"]] = by_cur.get(b["currency"], 0) + 1
    parts.append(f"🔎 <b>Выпусков без сделок за {FRESH_DAYS_CORP} дней: {len(nd)}</b> "
                 f"({', '.join(f'{k} {v}' for k, v in sorted(by_cur.items(), key=lambda kv: -kv[1]))})\n"
                 "<i>Полный список с купонами, сроками, объёмами и bid/offer — в Excel ниже "
                 "(лист «KZT без сделок» отсортирован по спреду купона к кривой).</i>")
    if coupons:
        parts.append(f"💸 <b>Купоны за день</b> ({len(coupons)})\n" + "\n".join(
            f'▫️ <a href="{html.escape(c["link"])}">{esc(c["title"])}</a>' for c in coupons[:20]))
    if rep["anomalies"]:
        parts.append(f"⚠️ <b>Исключены как подозрительные: {len(rep['anomalies'])}</b> — " + ", ".join(
            f"{esc(b['ticker'])} ({fmt_pct(b['yield'])})" for b in rep["anomalies"][:8])
            + ("…" if len(rep["anomalies"]) > 8 else "") + "\n<i>Скорее всего валютная индексация или случайная "
            "цена сделки. В Excel помечены в колонке «Примечание».</i>")
    src = ("G-спред — к официальной кривой ГЦБ KASE (в годовой доходности)" if rep["official"] else
           f"Кривая ГЦБ по {rep['kzt_n']} выпускам со сделками за {FRESH_DAYS_GOV} дней, "
           f"точность ±{rep['kzt_rmse'] * 100:.0f} бп")
    parts.append(f"<i>{src}. В средних — бумаги с ценой не старше {FRESH_DAYS_CORP} дней и сроком от 3 мес.</i>")
    return caption, "\n\n".join(parts)


def send_document(chats, data, filename, caption=""):
    for chat in chats:
        boundary = uuid.uuid4().hex
        fields = {**target(chat), "caption": caption[:1024], "parse_mode": "HTML"}
        body = b""
        for k, v in fields.items():
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\n'
                 "Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n").encode()
        body += data + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{bot.TOKEN}/sendDocument", data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            urllib.request.urlopen(req, timeout=60).read()
        except Exception as e:
            print(f"Telegram sendDocument error: {e}", file=sys.stderr)


def send_report(state, now):
    rep = build_report(now, state.get("curve", {}))
    date_str = f"{now:%d.%m.%Y}"
    caption, text = report_texts(rep, date_str, state.get("coupons", []))
    send_photo_file(CHATS, draw_chart(rep, date_str), caption)
    send_text(CHATS, text)
    send_document(CHATS, build_excel(rep, date_str), f"KASE_bonds_{now:%Y-%m-%d}.xlsx",
                  f"📎 Все облигации KASE на {date_str}: с ценой, без сделок, USD, ГЦБ")
    state["curve"] = {"date": f"{now:%d.%m}",
                      "points": {str(t): v for t, v in rep["points"].items()}}
    state["coupons"] = []
    print(f"Отчёт отправлен: KZT с ценой {len(rep['kzt_live'])}, USD {len(rep['usd_live'])}, "
          f"без сделок {len(rep['no_deals'])}")


# ───────────────────────── алерты ─────────────────────────

ALERT_PROMPT = """Ты — аналитик долгового рынка Казахстана. Тебе дают новости. Для КАЖДОЙ верни JSON:
- "i": номер
- "category": "placement" (новый выпуск, регистрация, листинг, аукцион/подписка, итоги размещения),
  "report" (финансовая отчётность эмитента облигаций: квартальная, годовая, МСФО),
  "coupon" (плановая выплата купона), "default" (невыплата, просрочка, дефолт, реструктуризация),
  "buyback" (выкуп, досрочное погашение, оферта), "delisting" (исключение из листинга),
  "rating" (присвоение/изменение рейтинга или прогноза: Fitch, S&P, Moody's, Эксперт РА и т.п.),
  "other" — всё прочее (акции, ETF, валюта, репо, общие новости биржи)
- "title_ru": короткий заголовок по-русски (до 100 знаков)
- "summary": суть по-русски, 1–2 предложения, с цифрами
- "issuer": эмитент или ""
- "ticker": тикер выпуска или ""
- "amount": объём (напр. "100 млрд ₸", "$500 млн") или ""
- "yield": доходность (напр. "15,60%") или ""
- "coupon": купон или ""
- "maturity": срок/погашение или ""
- "rating": для рейтингов — что было → что стало и прогноз (напр. "BB+ → BBB−, прогноз «Стабильный»") или ""
- "why": одно предложение — почему это важно для инвестора в облигации (риск, спред, ликвидность, сигнал для сектора)
- "exchange": где обращаются бумаги: "KASE", "AIX", "KASE, AIX" или "" (если не ясно)
- "dup": true, если это та же новость, что выше в списке или в «уже было»

Ответь ТОЛЬКО массивом JSON."""


def load_alerts(state, now_ts):
    fresh = []
    for source, url in ALERT_FEEDS:
        try:
            items = (parse_html_news(fetch(url[5:]), url[5:]) if url.startswith("html:")
                     else parse_feed(fetch(url)))
        except Exception as e:
            print(f"[{source}] {url[:60]} не загрузился: {e}", file=sys.stderr)
            continue
        key0 = f"{source}|{url[:80]}"
        first = key0 not in state["feeds"]
        for it in items:
            key = f"{source}|{it['link']}"
            if key in state["seen"]:
                continue
            state["seen"][key] = now_ts
            if not first:
                it["source"] = source
                if "news.google.com" in it["link"]:
                    it["title"] = re.sub(r"\s+-\s+[^-]{2,40}$", "", it["title"])
                fresh.append(it)
        if first:
            state["feeds"].append(key0)
            print(f"[{source}] подключён: {url[:70]} ({len(items)})")
    return fresh


EXCH_ICON = {"KASE": "🏛 KASE", "AIX": "🌐 AIX"}


def exchange_label(a):
    ex = str(a.get("exchange") or "").upper()
    found = [k for k in ("KASE", "AIX") if k in ex]
    if not found and a["source"] in EXCH_ICON:  # по источнику
        found = [a["source"]]
    if not found:
        text = f"{a['title']} {a.get('desc', '')}"
        found = [k for k in ("KASE", "AIX") if re.search(k, text, re.I)]
    return " · ".join(EXCH_ICON[k] for k in found)


def fmt_alert(a):
    cat = CATEGORIES.get(a["category"], "🔔 Облигации")
    ex = exchange_label(a)
    parts = [f"{cat}{' · ' + ex if ex else ''}\n<b>{esc(a.get('title_ru') or a['title'])}</b>"]
    if a.get("summary"):
        parts.append(f"<blockquote>{esc(a['summary'])}</blockquote>")
    params = [(k, a.get(f)) for k, f in (("Эмитент", "issuer"), ("Тикер", "ticker"), ("Объём", "amount"),
              ("Доходность", "yield"), ("Купон", "coupon"), ("Срок", "maturity"), ("Рейтинг", "rating")) if a.get(f)]
    if params:
        parts.append("\n".join(f"▫️ {k}: <b>{esc(str(v))}</b>" for k, v in params))
    if a.get("why"):
        parts.append(f"💡 <b>Почему важно:</b> {esc(a['why'])}")
    when = f" · {local(a['ts']):%d.%m %H:%M}" if a.get("ts") else ""
    parts.append(f'🔗 <a href="{html.escape(a["link"])}">{esc(a["source"])}</a><i>{when}</i>')
    return "\n\n".join(parts)


def check_alerts(state, now):
    now_ts = int(now.timestamp())
    fresh = load_alerts(state, now_ts)
    cand = [it for it in fresh if BOND_RE.search(f"{it['title']} {it['desc'][:300]}")
            and not NOT_BOND_RE.search(it["title"])]
    pending = state.pop("pending", [])
    recent = [r["title"] for r in state["recent"]]
    uniq = []
    for it in cand:
        if any(similar(it["title"], t) for t in recent + [u["title"] for u in uniq]):
            continue
        uniq.append(it)
    items = pending + uniq
    print(f"{now:%H:%M} алерты: новых {len(fresh)}, про облигации {len(cand)}, к оценке {len(items)}")
    if not items:
        return
    if not AI_ON:
        print("⚠️ Нет ключа ИИ — алерты не классифицируются", file=sys.stderr)
        return
    sent = 0
    for i in range(0, len(items), 8):
        part = items[i:i + 8]
        user = ("Уже было:\n" + "\n".join(f"- {t}" for t in recent[-30:]) + "\n\nНовости:\n\n" +
                "\n\n".join(f"[{j}] ({x['source']}) {x['title']}\n{x['desc'][:500]}" for j, x in enumerate(part)))
        res = extract_json(ask_ai(ALERT_PROMPT, user, max_tokens=500 * len(part) + 500, as_json=True))
        if not isinstance(res, list):
            for x in part:  # повторим в следующий раз (до 3 попыток)
                x["tries"] = x.get("tries", 0) + 1
                if x["tries"] < 3:
                    state.setdefault("pending", []).append(x)
            print("ИИ не ответил по пачке алертов — повтор позже", file=sys.stderr)
            continue
        for r in res:
            try:
                a = dict(part[int(r["i"])], **{k: v for k, v in r.items() if k != "i"})
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            state["recent"].append({"title": a["title"], "ts": now_ts})
            cat = a.get("category")
            print(f"   {cat or '?':<10} {'дубль' if a.get('dup') else ''} [{a['source']}] {a['title'][:80]}")
            if a.get("dup") or cat not in CATEGORIES:
                continue
            if cat == "coupon":  # плановые купоны — в вечернюю сводку
                state.setdefault("coupons", []).append({"title": a.get("title_ru") or a["title"], "link": a["link"]})
                continue
            send_text(CHATS, fmt_alert(a))
            sent += 1
            time.sleep(1.1)
    print(f"   отправлено алертов: {sent}")


# ───────────────────────── состояние и запуск ─────────────────────────

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    for k, v in (("seen", {}), ("feeds", []), ("recent", []), ("coupons", []), ("curve", {})):
        st.setdefault(k, v)
    return st


def save_state(st):
    if len(st["seen"]) > 6000:
        st["seen"] = dict(sorted(st["seen"].items(), key=lambda kv: kv[1])[-6000:])
    cutoff = int(time.time()) - 3 * 86400
    st["recent"] = [r for r in st["recent"] if r["ts"] > cutoff][-300:]
    st["coupons"] = st["coupons"][-60:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)


def report_due(state, now):
    return (now.weekday() < 5 and (now.hour, now.minute) >= REPORT_TIME and now.hour < 23
            and state.get("report_date") != f"{now:%Y-%m-%d}")


def main():
    if not bot.TOKEN or not CHATS:
        sys.exit("Укажите TG_TOKEN и TG_CHAT_BONDS")
    state = load_state()
    now = datetime.now(LOCAL_TZ)
    if not state.get("started"):
        send_text(CHATS, "💼 <b>Bonds Alert подключён</b>\n\nСюда будут приходить: новые выпуски и размещения, "
                  "отчётность эмитентов, дефолты и выкупы, рейтинги (KASE и AIX). "
                  f"По будням в {REPORT_TIME[0]}:{REPORT_TIME[1]:02d} — кривая ГЦБ, G-спреды и средние по секторам.")
        state["started"] = True
    try:
        check_alerts(state, now)
    except Exception as e:
        print(f"Ошибка в алертах: {e!r}", file=sys.stderr)
    save_state(state)
    if "--report" in sys.argv or report_due(state, now):
        try:
            send_report(state, now)
            state["report_date"] = f"{now:%Y-%m-%d}"
        except Exception as e:
            print(f"Ошибка в отчёте по кривой: {e!r}", file=sys.stderr)
            if "--report" in sys.argv or now.hour >= 21:  # к вечеру так и не вышло — сообщаем
                bot.send(f"⚠️ Отчёт по кривой не построен: {esc(str(e))[:300]}")
                state["report_date"] = f"{now:%Y-%m-%d}"
        save_state(state)


if __name__ == "__main__":
    main()
