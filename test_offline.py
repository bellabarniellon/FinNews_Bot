#!/usr/bin/env python3
"""
Офлайн-тест без Telegram, без интернета и без ключей ИИ:  python test_offline.py
Ленты и ответы ИИ подменены заготовками (заголовки — реальные, из работы бота 23–24.11.2026,
плюс несколько учебных). Проверяет маршрутизацию по каналам, фильтры, защиту от перепутанных
ссылок, «срочную полосу» и алерты по облигациям. Посты печатаются в консоль.
"""
import json, os, re, sys, tempfile, time
from email.utils import formatdate
from html import escape as esc

os.environ.update(DRY_RUN="1", GEMINI_API_KEY="fake", TG_TOKEN="", TG_CHAT_ID="")
tmp = tempfile.mkdtemp()
os.environ["STATE_FILE"] = os.path.join(tmp, "seen.json")
os.environ["BONDS_STATE_FILE"] = os.path.join(tmp, "bonds.json")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tmp)   # боевые seen.json / bonds_state.json тест не трогает и не читает
import bot, bonds

bot.QUIET_FROM = bot.QUIET_TO = 0      # тест не зависит от времени суток
bot.SEND_IMAGES = False
SENT = []
_tg = bot.tg
bot.tg = lambda m, p, retry=True: (SENT.append((p["chat_id"], p.get("text") or p.get("caption"))), _tg(m, p))[1]


def rss(items):
    now = formatdate(time.time())
    return ("<rss><channel>" + "".join(
        f"<item><title>{esc(t)}</title><link>{esc(l)}</link><guid>{esc(l)}</guid><description>{esc(d)}</description>"
        f"<pubDate>{now}</pubDate></item>" for t, l, d in items) + "</channel></rss>").encode()


def kase_page(titles):
    return "".join(f'<a href="/ru/information/news/show/{n}">{t}</a>' for n, t in titles).encode()


FEEDS = {}       # адрес → содержимое
NEWS = {         # источник → [(заголовок, ссылка, описание)]
    "CNBC": [
        ("Nvidia board reinstates Jensen Huang as CEO", "https://cnbc.com/huang", "Nvidia said its board reinstated Huang."),
        ("Fermi COO Jacobo Ortiz Blanes sells $1.99M in company stock", "https://cnbc.com/fermi", ""),
        ("3 Top Dividend Stocks To Watch With Up To 4% Yield", "https://cnbc.com/div", ""),
        ("Mid-size retailer Acme raises full-year outlook", "https://cnbc.com/acme", "Acme lifted guidance."),
        ("Fed cuts rates by 25 basis points, signals pause", "https://cnbc.com/fed", "The FOMC lowered the target range."),
    ],
    "BusinessKorea": [
        ("SK Chairman Chey Offers Full 944 Billion Won in Divorce Asset Division", "https://bk.kr/chey", "Chey Tae-won offered 944 billion won."),
        ("Samsung Electronics posts record Q4 operating profit on HBM demand", "https://bk.kr/sam", "Samsung operating profit hit a record."),
    ],
    "Reuters Korea": [
        ("Russian unmanned aerial vehicle briefly violated Polish airspace", "https://r.com/poland", "Polish armed forces said."),
        ("Top Photos of the Day", "https://r.com/photos", ""),
    ],
    "Kursiv": [
        ("Kyzyl Aray Copper планирует производство 30 тыс. тонн катодной меди в год с 2027 года", "https://kursiv/kac", "Компания заявила о строительстве комплекса."),
        ("Компания «Алатау-Кус» запросит заем в 2,2 млрд тенге у СПК «Алматы» под 0,01%", "https://kursiv/alatau", "Льготное финансирование от акимата."),
        ("Нацбанк Казахстана повысил базовую ставку до 18,5%", "https://kursiv/nbk", "Решение принято на фоне ускорения инфляции."),
        ("Досаев Ерболат Аскарбекович: биография", "https://kursiv/bio", ""),
    ],
    "WSJ": [
        ("Mexico Took Down a Top Drug Kingpin. Can It Dismantle His Cartel?", "https://wsj.com/mexico", "An analysis."),
    ],
    "BBC World": [
        ("Israel launches strikes on Iran oil terminals", "https://bbc.com/iran", "Strikes hit Kharg island export terminal."),
        ("Cho Gue-sung scores sixth goal of season", "https://bbc.com/football", ""),
    ],
    "KASE": [],
}
URL_OF = {"CNBC": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
          "BusinessKorea": bot.gnews("site:businesskorea.co.kr when:1d"),
          "Reuters Korea": bot.gnews("site:reuters.com (Korea OR KOSPI OR Samsung OR Hynix OR Hyundai) when:1d"),
          "Kursiv": "https://kz.kursiv.media/feed/",
          "WSJ": "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",
          "BBC World": "https://feeds.bbci.co.uk/news/world/rss.xml"}
KASE_NEWS = [  # первые 5 — реальные заголовки KASE за 23–24.11.2026, остальные — по шаблонам KASE
    (1, 'Облигации KZ2P00015886 (MFAFb13) ТОО "Микрофинансовая организация аФинанс" исключены из официального списка KASE в связи с истечением срока обращения'),
    (2, 'Сегодня на KASE проводится подписка на облигации KZ2P00016173 (MFRFb25) ТОО "Микрофинансовая организация "R-Finance""'),
    (3, 'Сегодня на KASE проводятся специализированные торги по размещению 50,0 млн облигаций KZ2C00020915 (IDFRb17) АО "Фонд развития промышленности"'),
    (4, 'Сегодня на KASE проводятся специализированные торги по размещению 20,0 млн облигаций KZ2C00020394 (IDFRb15) АО "Фонд развития промышленности"'),
    (5, 'Сегодня на KASE проводятся повторные специализированные торги по размещению МЕУКАМ KZKD00001269 (MUM120_0020) с расчетами T+2'),
    (6, 'АО "АК Алтыналмас" выкупило на KASE 23 ноября 3 846 своих облигаций KZ2C00012953 (ALMSb1) на сумму $389,5 тыс.'),
    (7, 'С 24 ноября на KASE открываются торги облигациями KZ2P00020951 (MFCRb2) ТОО "МФО CapitalCredit"'),
    (8, 'Облигации KZ2C00021111 (TESTb1) АО "Тестовый эмитент" включены в официальный список KASE по категории "облигации"'),
    (9, 'ТОО "МФО CapitalCredit" сообщило о выплате третьего купонного вознаграждения по облигациям KZ2P00020951 (MFCRb2)'),
    (10, 'АО "АК Алтыналмас" предоставило KASE консолидированную финансовую отчетность за январь–сентябрь 2026 года'),
    (11, 'АО "Компания без облигаций" предоставило KASE финансовую отчетность за январь–сентябрь 2026 года'),
    (12, 'ТОО "Тестовая МФО" допустило дефолт по выплате купонного вознаграждения по облигациям KZ2P00099999 (TMFOb1)'),
    (13, '23 ноября значение Индекса KASE снизилось на 0,39% до 7 749,11'),
    (14, 'Результаты торгов иностранными валютами на KASE на 17:00'),
    (15, 'Министерство финансов Казахстана привлекло на KASE 45,2 млрд тенге, разместив МЕУКАМ KZKD00001269 (MUM120_0020)'),
]


def fake_fetch(url, timeout=20):
    if url in FEEDS:
        return FEEDS[url]
    if "kase.kz/ru/information/news/" in url and "/show/" not in url:
        return FEEDS.get("kase", b"")
    raise OSError("нет сети (тест)")


AI_CALLS = {"n": 0}
RULES = [  # (что в заголовке, канал, оценка, тип, заголовок_ru)
    ("reinstates Jensen Huang", "US", 5, "company", "Совет директоров Nvidia вернул Дженсена Хуанга на пост CEO"),
    ("Fed cuts rates", "US", 5, "macro", "ФРС снизила ставку на 25 б.п. и намекнула на паузу"),
    ("Acme", "US", 2, "company", "Ритейлер Acme повысил прогноз на год"),
    ("Chey", "KR", 3, "company", "Глава SK Group Чхве Тхэ Вон выплатит 944 млрд вон по делу о разводе"),
    ("Samsung Electronics posts", "KR", 4, "company", "Samsung Electronics получила рекордную прибыль в IV квартале"),
    ("Kyzyl Aray", "KZ", 2, "company", "Kyzyl Aray Copper планирует производство 30 тыс. тонн катодной меди"),
    ("Алатау-Кус", "KZ", 2, "company", "«Алатау-Кус» запросит заем в 2,2 млрд тенге у СПК «Алматы»"),
    ("базовую ставку", "KZ", 5, "macro", "Нацбанк Казахстана повысил базовую ставку до 18,5%"),
    ("Drug Kingpin", "US", 1, "other", "Мексика задержала наркобарона"),
    ("strikes on Iran", "MACRO", 5, "geo", "Израиль нанёс удары по нефтяным терминалам Ирана"),
]


def fake_ai(system, user, max_tokens=4000, as_json=False):
    AI_CALLS["n"] += 1
    blocks = re.findall(r"\[([0-9a-f]{4,6})\] \((.*?)\) (.*)", user.split("Новые новости:")[1])
    if MODE["v"] == "down":
        return None
    out = []
    for code, src, title in blocks:
        for key, reg, sc, kind, ru in RULES:
            if key in title:
                out.append({"i": code, "kind": kind, "region": reg, "score": sc, "title_ru": ru,
                            "summary": ru + ".", "why": "тест", "impact": "", "tags": [], "watch": [], "dup": False})
    if MODE["v"] == "shuffle" and len(out) > 1:   # ИИ «съехал» на одну новость — раньше так появлялись чужие ссылки
        ids = [o["i"] for o in out]
        for o, i in zip(out, ids[1:] + ids[:1]):
            o["i"] = i
    return json.dumps(out, ensure_ascii=False)


MODE = {"v": "ok"}
bot.fetch = bonds.fetch = fake_fetch
bot.ask_ai = fake_ai
bonds.fetch_json = lambda url: [
    {"code": "ALMSb1", "org_short_name_ru": 'АО "АК Алтыналмас"', "org_name_ru": 'АО "АК Алтыналмас"', "dohod": 9.1,
     "ticker": {"currency": "USD", "cupon": 9.5, "finish_date": "2028-06-01"}},
    {"code": "MFCRb2", "org_short_name_ru": 'ТОО "МФО CapitalCredit"', "org_name_ru": 'ТОО "МФО CapitalCredit"',
     "ticker": {"currency": "KZT", "cupon": 22.0, "finish_date": "2027-11-24"}},
]


def publish(only=None):
    for src, items in NEWS.items():
        if src in URL_OF:
            FEEDS[URL_OF[src]] = rss(items if only is None else items[:only])


def run(title):
    print(f"\n{'═' * 30} {title} {'═' * 30}")
    SENT.clear()
    bot._cycle["n"] = 0
    state = bot.load_state()
    bot.check_news(state, bot.datetime.now(bot.LOCAL_TZ))
    bot.save_state(state)
    return list(SENT), state


def chan(sent, name):
    return [t for c, t in sent if name in c]


fails = []
def check(cond, what):
    print(("  ✅ " if cond else "  ❌ ") + what)
    if not cond:
        fails.append(what)


# 1) первое подключение лент — ничего не шлём
publish(only=0)
for u in list(FEEDS):
    FEEDS[u] = rss([("Старая новость для подключения ленты номер один", u + "/old", "")])
sent, _ = run("1. Подключение лент")
check(not sent, "при подключении новых лент в каналы ничего не уходит")

# 2) обычный цикл, ИИ отвечает
publish()
sent, st = run("2. Новые новости, ИИ отвечает")
us, kr, kz, mc = (chan(sent, n) for n in ("Vitis US", "Vitis Korea", "Vitis KZ", "Vitis Macro"))
check(any("Хуанга" in t for t in us) and any("ФРС" in t for t in us), "US: Хуанг и ФРС отправлены")
check(len(us) == 2, f"US: только 2 поста из 5 (инсайдер, дивидендные «акции на заметку», мелкий ритейлер — нет): {len(us)}")
check(any("Samsung" in t for t in kr) and any("Чхве" in t for t in kr), "KR: Samsung (4) и развод главы SK (3) отправлены")
check(not any("Polish" in t or "Photos" in t for _, t in sent), "KR: польский дрон и «фото дня» из Reuters Korea отсечены")
check(len(kz) == 1 and "базовую ставку" in kz[0], "KZ: ушла только ставка НБК; медь 30 тыс. т, заём 2,2 млрд ₸ и биография — нет")
check(len(mc) == 1 and "Ирана" in mc[0], "MACRO: удар по Ирану ушёл в Vitis Macro News, футбол — нет")
check(all(("kursiv" in t) == ("Нацбанк" in t) for t in kz), "ссылка поста KZ ведёт на свою статью")
check(not any(q["score"] < 3 for q in st["queue"]), "в дайджест не попадает «фон» 2/5")

# 3) повтор — дублей нет
sent, _ = run("3. Тот же набор ещё раз")
check(not sent, "повторно ничего не отправлено")

# 4) ИИ путает номера → чужой текст не должен уйти с чужой ссылкой
MODE["v"] = "shuffle"
NEWS["CNBC"].append(("Apple names new chief financial officer", "https://cnbc.com/apple", "Apple said."))
NEWS["Kursiv"].append(("Казатомпром увеличил добычу урана на 12% за девять месяцев", "https://kursiv/kap", "Добыча выросла."))
RULES += [("Apple names", "US", 4, "company", "Apple назначила нового финансового директора"),
          ("Казатомпром", "KZ", 4, "company", "Казатомпром увеличил добычу урана на 12%")]
publish()
sent, st = run("4. ИИ перепутал новости местами")
check(not any(("Apple" in t and "kursiv" in t) or ("Казатомпром" in t and "cnbc" in t) for _, t in sent),
      "перепутанный ответ не опубликован с чужой ссылкой")
check(len(st.get("pending", [])) == 2, "обе новости оставлены на повторную оценку")
MODE["v"] = "ok"
sent, _ = run("4б. Повторная оценка")
check(any("Apple" in t and "cnbc.com/apple" in t for _, t in sent) and any("Казатомпром" in t and "kursiv/kap" in t for _, t in sent),
      "после повтора каждая новость ушла со своей ссылкой")

# 5) ИИ недоступен, а новость срочная
MODE["v"] = "down"
NEWS["CNBC"].append(("Tesla CEO Elon Musk resigns, board names successor", "https://cnbc.com/tesla", ""))
NEWS["CNBC"].append(("Coffee chain opens 12 new stores in Ohio", "https://cnbc.com/coffee", ""))
publish()
sent, st = run("5. ИИ не отвечает + срочная новость")
check(any("Musk resigns" in t and "Срочно" in t for t in chan(sent, "Vitis US")), "срочная новость ушла сразу, без ИИ (⚡️, оригинальный заголовок)")
check(not any("Coffee" in t for _, t in sent), "несрочная ждёт ИИ, сырой не публикуется")
MODE["v"] = "ok"

# 6) Bonds Alert
print(f"\n{'═' * 30} 6. Bonds Alert {'═' * 30}")
FEEDS["kase"] = kase_page([(0, "Старая новость KASE про облигации для подключения ленты")])
stb = bonds.load_state(); bonds.check_alerts(stb, bot.datetime.now(bot.LOCAL_TZ)); bonds.save_state(stb)
SENT.clear()
FEEDS["kase"] = kase_page(KASE_NEWS)
calls = AI_CALLS["n"]
stb = bonds.load_state(); bonds.check_alerts(stb, bot.datetime.now(bot.LOCAL_TZ)); bonds.save_state(stb)
b = [t for _, t in SENT]
has = lambda label, frag: any(label in t and frag in t for t in b)
check(AI_CALLS["n"] == calls, "алерты по облигациям не используют ИИ")
check(has("Новый выпуск", "MFCRb2") and has("Новый выпуск", "TESTb1"), "🆕 листинг и открытие торгов — отдельными постами")
check(has("Выкуп", "ALMSb1") and has("Дефолт", "TMFOb1"), "🔁 выкуп и ⚠️ дефолт — отдельными постами")
check(has("Выплаты купонов", "MFCRb2"), "💸 купон попал в список")
check(has("Отчётность", "Алтыналмас") and has("Отчётность", "квартальная"), "📄 отчётность эмитента облигаций, помечена как квартальная")
check(not any("Компания без облигаций" in t for t in b), "отчётность компании без облигаций отсеяна")
check(has("Погашения", "MFAFb13"), "✅ исключение по истечении срока — в погашениях, а не как тревожный делистинг")
check(has("Размещения и подписки", "IDFRb17") and has("Размещения и подписки", "IDFRb15"),
      "📣 два размещения одного эмитента не склеились как «дубли» (раньше терялись)")
check(has("Минфина", "MUM120_0020"), "🏛 аукционы Минфина — отдельным списком")
check(not any("Индекс" in t or "иностранными валютами" in t for t in b), "индекс и валютные торги в Bonds не идут")
ALMS = next(t for t in b if "ALMSb1" in t and "Выкуп" in t)
check("купон" in ALMS and "9,50%" in ALMS, "к алерту подтянуты параметры выпуска из KASE (купон, погашение)")

print("\n" + ("❌ ПРОВАЛЕНО: " + "; ".join(fails) if fails else f"✅ Все проверки пройдены"))
sys.exit(1 if fails else 0)
