#!/usr/bin/env python3
"""
Vitis Bonds Bot — облигации KASE и AIX → тема «Bonds Alert».

1) Алерты (каждые ~10 мин), БЕЗ ИИ — новости KASE шаблонные, разбираются правилами (ничего не теряется,
   когда у Gemini кончился лимит):
   сразу, отдельным постом: 🆕 листинг и открытие торгов · 💰 итоги размещения · ⚠️ дефолты ·
     🔁 выкупы · 🚫 делистинг · ⏸ приостановка торгов · ⭐ рейтинги
   списком за проверку: 💸 выплаты купонов · 📄 отчётность эмитентов облигаций (с пометкой периода) ·
     ✅ погашения · 📣 размещения и подписки сегодня · 🏛 аукционы Минфина
2) Ежедневно после торгов (по умолчанию 18:30 Алматы, Пн–Пт), данные — API KASE (все выпуски):
   графики KZT (к кривой ГЦБ) и USD (к кривой UST), G-спреды, средние по секторам,
   самые широкие/узкие спреды, изменение кривой за день,
   + Excel со ВСЕМИ облигациями KASE, включая выпуски без сделок (купон, срок, объём, bid/offer).

Использует общие функции из bot.py (тот же бот, тот же ключ Gemini).
  python bonds.py --once      — проверка алертов + отчёт по кривой, если пора
  python bonds.py --report    — отправить отчёт по кривой прямо сейчас
  DRY_RUN=1 python bonds.py --once --sample — тест без Telegram на всём, что сейчас в лентах
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
from bot import (LOCAL_TZ, DRY_RUN, fetch, clean, esc, gnews, parse_feed, parse_html_news,
                 send_text, target, similar, local)

# ════════════════════ НАСТРОЙКИ ════════════════════

REPORT_TIME = (18, 30)        # когда отправлять кривую (Алматы), только Пн–Пт
STATE_FILE = os.environ.get("BONDS_STATE_FILE", "bonds_state.dry.json" if DRY_RUN else "bonds_state.json")
CHATS = ([c.strip() for c in os.environ.get("TG_CHAT_BONDS", "").split(",") if c.strip()] or bot.PERSONAL
         or (["«Bonds Alert»"] if DRY_RUN else []))


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
    if DRY_RUN:
        with open("dry_curve.png", "wb") as f:
            f.write(png_bytes)
        print("🧪 график сохранён в dry_curve.png")
        return send_text(chats, caption)
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
                try:
                    d = datetime.strptime(row[0], "%m/%d/%Y").strftime("%d.%m.%Y")
                except ValueError:
                    d = row[0]
                print(f"UST: {d}, {len(pts)} сроков")
                return pts, d
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
    # UST публикуются в bond-equivalent (полугодовое начисление) → переводим в годовую эффективную,
    # как у доходностей KASE; между точками — линейная интерполяция (точнее, чем сглаживание Nelson-Siegel)
    usd = ucurve = None
    if ust_pts and len(ust_pts) >= 5:
        if ust_date != "KASE":
            ust_pts = [(m, ((1 + y / 200) ** 2 - 1) * 100) for m, y in ust_pts]
        ust_pts = sorted(ust_pts)
        ux, uy = [p[0] for p in ust_pts], [p[1] for p in ust_pts]
        usd = ucurve = lambda m: float(np.interp(m, ux, uy))

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
        return sorted(((s, len(v), float(np.median([x["yield"] for x in v])), float(np.median([x["g"] for x in v])))
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
        panels.append(("USD", rep["usd"], rep["ust_pts"], rep["usd_live"], "Кривая UST"))
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
        low = [b for b in rep["anomalies"] if b["g"] < 0]
        high = [b for b in rep["anomalies"] if b["g"] >= 0]
        names = lambda bs: ", ".join(f"{esc(b['ticker'])} ({fmt_pct(b['yield'])})" for b in bs[:8]) + ("…" if len(bs) > 8 else "")
        txt = f"⚠️ <b>Исключены из средних как подозрительные: {len(rep['anomalies'])}</b>"
        if low:
            txt += f"\n▫️ намного ниже кривой: {names(low)} — <i>скорее всего валютная индексация или случайная цена</i>"
        if high:
            txt += f"\n▫️ аномально высокая доходность: {names(high)} — <i>дистресс или сделка по случайной цене, проверить</i>"
        parts.append(txt + "\n<i>В Excel помечены в колонке «Примечание».</i>")
    src = ("G-спред — к официальной кривой ГЦБ KASE (в годовой доходности)" if rep["official"] else
           f"Кривая ГЦБ по {rep['kzt_n']} выпускам со сделками за {FRESH_DAYS_GOV} дней, "
           f"точность ±{rep['kzt_rmse'] * 100:.0f} бп")
    parts.append(f"<i>{src}. В средних — бумаги с ценой не старше {FRESH_DAYS_CORP} дней и сроком от 3 мес.</i>")
    return caption, "\n\n".join(parts)


def send_document(chats, data, filename, caption=""):
    if DRY_RUN:
        with open("dry_" + filename, "wb") as f:
            f.write(data)
        print(f"🧪 файл сохранён: dry_{filename}")
        return
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
    caption, text = report_texts(rep, date_str, [])
    send_photo_file(CHATS, draw_chart(rep, date_str), caption)
    send_text(CHATS, text)
    send_document(CHATS, build_excel(rep, date_str), f"KASE_bonds_{now:%Y-%m-%d}.xlsx",
                  f"📎 Все облигации KASE на {date_str}: с ценой, без сделок, USD, ГЦБ")
    state["curve"] = {"date": f"{now:%d.%m}",
                      "points": {str(t): v for t, v in rep["points"].items()}}
    state["coupons"] = []
    print(f"Отчёт отправлен: KZT с ценой {len(rep['kzt_live'])}, USD {len(rep['usd_live'])}, "
          f"без сделок {len(rep['no_deals'])}")


# ───────────────────────── алерты (правила, без ИИ) ─────────────────────────

DEBT_RE = re.compile(r"облигац|бонд|купон|еврооблигац|сукук|\bнот[ыа]?\b|МЕУКАМ|МЕОКАМ|МЕККАМ|МЕУЖКАМ|"
                     r"\bbonds?\b|eurobonds?|\bnotes\b|sukuk|coupon", re.I)
GOV_RE = re.compile(r"МЕУКАМ|МЕОКАМ|МЕККАМ|МЕУЖКАМ|МУИКАМ|МЕИКАМ|нот\w* Национального Банка|Министерств\w+ финансов|"
                    r"Минфин", re.I)
AGENCY_RE = re.compile(r"fitch|moody|s&p|s&amp;p|standard & poor|эксперт ра|acra|акра", re.I)
KZ_CTX_RE = re.compile(r"kazakh|казахстан|kase|aix|astana|almaty|тенге", re.I)
NOISE_RE = re.compile(r"репо|иностранными валютами|своп|KASE Global|ETF|паев|маркет-мейкер|Индекс\w* KASE|"
                      r"результаты торгов|итоги торгов акци", re.I)

# категория → (значок и название, отдельным постом?)
CATEGORIES = {
    "default":   ("⚠️ Дефолт / невыплата", True),
    "halt":      ("⏸ Торги приостановлены", True),
    "listing":   ("🆕 Новый выпуск", True),
    "result":    ("💰 Итоги размещения", True),
    "buyback":   ("🔁 Выкуп / досрочное погашение", True),
    "delisting": ("🚫 Делистинг", True),
    "rating":    ("⭐ Рейтинг", True),
    "auction":   ("📣 Размещения и подписки сегодня", False),
    "gov":       ("🏛 Аукционы Минфина и НБК", False),
    "coupon":    ("💸 Выплаты купонов", False),
    "matured":   ("✅ Погашения", False),
    "report":    ("📄 Отчётность эмитентов облигаций", False),
}


def classify(title, source="KASE"):
    """Категория новости по заголовку или None. Порядок проверок важен: сначала плохие новости."""
    t = title
    low = t.lower()
    if NOISE_RE.search(t):
        return None
    debt = bool(DEBT_RE.search(t))
    if re.search(r"дефолт|не выплати|невыплат|не исполни|неисполнен|просроч|задерж\w+ (выплат|исполнен)|"
                 r"несвоевременн|реструктуризац|\bdefault|missed (coupon|payment)|restructur", low) and \
            (debt or source != "KASE"):
        return "default"
    if AGENCY_RE.search(t) and re.search(r"рейтинг|rating|outlook|прогноз|affirm|upgrade|downgrade|"
                                         r"подтверд|повыс|пониз|присво", low):
        return "rating" if (source == "KASE" or KZ_CTX_RE.search(t)) else None
    if re.search(r"финансов\w+ отч[её]тност|аудиторск\w+ отч[её]т|годово\w+ отч[её]т|financial statements|"
                 r"отч[её]тност\w* за |финансов\w+ результат", low):
        return "report"
    if not debt:
        return None
    if re.search(r"приостановлен\w* торг|торги .{0,60}приостановлен|trading .{0,30}suspend", low):
        return "halt"
    if re.search(r"исключен\w* из официального списка|delist", low):
        return "matured" if re.search(r"истечени\w+ срока обращения|погашени", low) else "delisting"
    if re.search(r"купон|coupon", low) and re.search(r"выплат|paid|payment", low):
        return "coupon"
    if re.search(r"выкуп|досрочн\w+ погаш|оферт|buyback|repurchase|early redemption|tender offer", low):
        return "buyback"
    if re.search(r"погасил|погашен\w* облигац|выплат\w+ основно\w+ долг|redeem|redemption", low):
        return "matured"
    if re.search(r"привлек\w* на kase|привлек\w* .{0,40}размести|итоги размещени|результаты размещени|"
                 r"размести\w* .{0,80}облигац|raises? .{0,40}(bonds?|notes|sukuk)|priced", low):
        return "gov" if GOV_RE.search(t) else "result"
    if re.search(r"специализированные торги|подписк\w* на|проводится подписка|аукцион|book.?build", low):
        return "gov" if GOV_RE.search(t) else "auction"
    if re.search(r"включен\w* в официальный список|прош\w+ процедуру листинга|открываются торги|"
                 r"открыты торги|зарегистрирован\w* выпуск|регистраци\w+ выпуска|"
                 r"admitted|admission|list(s|ed|ing) .{0,60}(bonds?|notes|sukuk)|new (bond|sukuk|notes?) ", low):
        return "listing"
    return None


def report_period(title):
    low = title.lower()
    m = re.search(r"(январ\w*\s*[–-]\s*(март|июн|сентябр|декабр)\w*|[1-4i]{1,3}\s*квартал|первое полугодие|"
                  r"полугоди\w*|(3|6|9|три|шесть|девять) месяц\w*)[^,.;]{0,12}?(20\d\d)?", low)
    if m:
        return "квартальная · " + m.group(0).strip()
    m = re.search(r"за (20\d\d) год", low)
    return f"годовая · {m.group(1)}" if m else ""


TICKER_RE = re.compile(r"\(([A-Za-z0-9_]{4,14})\)")
ISIN_RE = re.compile(r"\b(KZ[0-9A-Z]{10})\b")


def name_key(s):
    s = re.sub(r"\b(ао|тоо|jsc|llp|нао|plc|ltd|limited|company|компания|микрофинансовая организация|мфо)\b", " ",
               s.lower())
    return re.sub(r"[^a-zа-яё0-9]", "", s)


_api = {}


def kase_bonds():
    """Все корпоративные облигации KASE (один запрос за запуск): тикер → бумага, + имена эмитентов."""
    if "by" not in _api:
        try:
            raw = fetch_json(API_CORP)
            now = datetime.now(LOCAL_TZ)
            _api["by"] = {(x.get("code") or "").upper(): norm(x, now) for x in raw}
            names = set()
            for x in raw:
                for k in ("org_short_name_ru", "org_name_ru", "org_name_en"):
                    n = name_key(x.get(k) or "")
                    if len(n) >= 4:
                        names.add(n)
            _api["issuers"] = names
        except Exception as e:
            print(f"API KASE недоступен ({e}) — алерты без параметров выпуска", file=sys.stderr)
            _api["by"], _api["issuers"] = {}, None
    return _api["by"], _api["issuers"]


def is_bond_issuer(title):
    """Отчётность интересна только по эмитентам, у которых есть облигации на KASE."""
    _, issuers = kase_bonds()
    if issuers is None:
        return True   # список не получили — лучше показать лишнее, чем потерять
    key = name_key(title)
    return any(n in key for n in issuers)


def kase_body(link):
    """Первый абзац новости KASE (там параметры выпуска: объём, срок, купон)."""
    if "kase.kz" not in link:
        return ""
    try:
        text = clean(fetch(link, timeout=15).decode("utf-8", "ignore"))
        m = re.search(r"/KASE,\s*\d\d\.\d\d\.\d\d/\s*[–—-]\s*(.+?)\s*\[\d{4}-\d\d-\d\d\]", text)
        return m.group(1)[:700] if m else ""
    except Exception:
        return ""


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
            known = key in state["seen"]
            state["seen"][key] = now_ts
            if (known or first) and "--sample" not in sys.argv:   # --sample: тест на всём, что сейчас в лентах
                continue
            it["source"] = source
            if "news.google.com" in it["link"]:
                it["title"] = re.sub(r"\s+-\s+[^-]{2,40}$", "", it["title"])
            fresh.append(it)
        if first:
            state["feeds"].append(key0)
            print(f"[{source}] подключён: {url[:70]} ({len(items)})")
    return fresh


def short(title, n=150):
    t = re.sub(r"^Сегодня на KASE (проводятся|проводится)\s+", "", title).strip()
    t = t[:1].upper() + t[1:]
    return t if len(t) <= n else t[:n - 1].rsplit(" ", 1)[0] + "…"


def fmt_alert(a):
    label = CATEGORIES[a["category"]][0]
    ex = a["source"] if a["source"] in ("KASE", "AIX") else ""
    parts = [f"{label}{' · ' + ('🏛 KASE' if ex == 'KASE' else '🌐 AIX') if ex else ''}\n<b>{esc(a['title'])}</b>"]
    body = kase_body(a["link"]) or a.get("desc", "")[:400]
    if body and not body.lower().startswith(a["title"][:40].lower()):
        parts.append(f"<blockquote>{esc(body)}</blockquote>")
    m = TICKER_RE.search(a["title"])
    b = kase_bonds()[0].get(m.group(1).upper()) if m else None
    if b:
        bits = [f"Тикер: <b>{esc(b['ticker'])}</b>", f"сектор: {esc(b['sector'])}"]
        if b["coupon"]:
            bits.append(f"купон: <b>{fmt_pct(b['coupon'])}</b>")
        if b["maturity"]:
            bits.append(f"погашение: <b>{esc(str(b['maturity'])[:10])}</b>")
        if b["yield"]:
            bits.append(f"доходность: <b>{fmt_pct(b['yield'])}</b>")
        parts.append("▫️ " + " · ".join(bits) + f'\n▫️ <a href="{BOND_URL.format(code=b["ticker"])}">Карточка выпуска на KASE</a>')
    when = f" · {local(a['ts']):%d.%m %H:%M}" if a.get("ts") else ""
    parts.append(f'🔗 <a href="{html.escape(a["link"])}">{esc(a["source"])}</a><i>{when}</i>')
    return "\n\n".join(parts)


def fmt_list(cat, items):
    label = CATEGORIES[cat][0]
    lines = []
    for a in items[:40]:
        extra = f" <i>({esc(a['period'])})</i>" if a.get("period") else ""
        lines.append(f'▫️ <a href="{html.escape(a["link"])}">{esc(short(a["title"]))}</a>{extra}')
    more = f"\n<i>…и ещё {len(items) - 40}</i>" if len(items) > 40 else ""
    return f"{label} <b>({len(items)})</b>\n\n" + "\n".join(lines) + more


def check_alerts(state, now):
    now_ts = int(now.timestamp())
    fresh = load_alerts(state, now_ts)
    recent = [r["title"] for r in state["recent"]]
    single, lists, skipped = [], {}, 0
    for it in fresh:
        cat = classify(it["title"], it["source"])
        if cat == "report" and not is_bond_issuer(it["title"]):
            cat = None
        # у KASE одинаковые по шаблону заголовки — это разные выпуски, дубли там только по ссылке;
        # для пересказов из СМИ сравниваем заголовки
        if cat and it["source"] != "KASE" and any(similar(it["title"], t) for t in recent):
            cat = None
        print(f"   {cat or '—':<10} [{it['source']}] {it['title'][:90]}")
        if not cat:
            skipped += 1
            continue
        it["category"] = cat
        recent.append(it["title"])
        state["recent"].append({"title": it["title"], "ts": now_ts})
        if cat == "report":
            it["period"] = report_period(it["title"])
        if CATEGORIES[cat][1]:
            single.append(it)
        else:
            lists.setdefault(cat, []).append(it)
    for a in single:
        send_text(CHATS, fmt_alert(a))
        time.sleep(0 if DRY_RUN else 1.1)
    for cat in CATEGORIES:
        if lists.get(cat):
            send_text(CHATS, fmt_list(cat, lists[cat]))
            time.sleep(0 if DRY_RUN else 1.1)
    print(f"{now:%H:%M} алерты: новых {len(fresh)}, отдельных постов {len(single)}, "
          f"в списках {sum(len(v) for v in lists.values())}, не про облигации {skipped}")


# ───────────────────────── состояние и запуск ─────────────────────────

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    if DRY_RUN and not st:   # тест стартует с боевого состояния
        try:
            with open("bonds_state.json", encoding="utf-8") as f:
                st = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
    for k, v in (("seen", {}), ("feeds", []), ("recent", []), ("coupons", []), ("curve", {})):
        st.setdefault(k, v)
    st.pop("pending", None)   # осталось от старой версии с ИИ
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
    if (not bot.TOKEN or not CHATS) and not DRY_RUN:
        sys.exit("Укажите TG_TOKEN и TG_CHAT_BONDS")
    state = load_state()
    now = datetime.now(LOCAL_TZ)
    if not state.get("started"):
        send_text(CHATS, "💼 <b>Bonds Alert подключён</b>\n\nСюда будут приходить: новые выпуски и размещения, "
                  "купоны, отчётность эмитентов, дефолты и выкупы, рейтинги (KASE и AIX). "
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
