"""Отправка сценарного анализа в Telegram: два альбома (RU, EN) из scenario/latest/.
Если в scenario/latest/meta.json стоит "intro": true — сначала уходит объясняющий пост из scenario/intro/.
Секреты: TG_TOKEN, TG_CHAT_RADAR (формат -100XXXXXXXXXX:номер_темы; несколько через запятую)."""
import json, os, re, sys, time
import requests

TOKEN = os.environ["TG_TOKEN"]
RAW = [c.strip() for c in os.environ.get("TG_CHAT_RADAR", "").split(",") if c.strip()]
DRY = os.environ.get("DRY_RUN", "") not in ("", "0", "false")
HERE = os.path.dirname(os.path.abspath(__file__))


def target(chat):
    m = re.fullmatch(r"(-?\d+|@\w+):(\d+)", chat)
    return {"chat_id": m.group(1), "message_thread_id": m.group(2)} if m else {"chat_id": chat}


def call(method, data, files=None):
    if DRY:
        print("DRY", method, {k: (v if k != "media" else "...") for k, v in data.items()}); return True
    for attempt in (1, 2, 3):
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", data=data, files=files, timeout=60)
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1); continue
        if not r.ok:
            print(f"Telegram {method} error {r.status_code}: {r.text[:300]}", file=sys.stderr); return False
        return True
    return False


def main():
    if not RAW: sys.exit("Не задан секрет TG_CHAT_RADAR")
    meta = json.load(open(f"{HERE}/latest/meta.json"))
    ok = True
    for chat in RAW:
        t = target(chat)
        if meta.get("intro"):
            for name in ("ru_1", "ru_2", "en_1", "en_2"):
                text = open(f"{HERE}/intro/{name}.html", encoding="utf-8").read().strip()
                ok &= call("sendMessage", {**t, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}); time.sleep(1.5)
        for lang in ("ru", "en"):
            cap = open(f"{HERE}/latest/{lang}_caption.txt", encoding="utf-8").read().strip()[:1024]
            names = [f"{lang}_1_table.png", f"{lang}_2_charts.png"]
            media = [{"type": "photo", "media": f"attach://f{i}", **({"caption": cap} if i == 0 else {})} for i in range(len(names))]
            files = {f"f{i}": open(f"{HERE}/latest/{n}", "rb") for i, n in enumerate(names)}
            ok &= call("sendMediaGroup", {**t, "media": json.dumps(media, ensure_ascii=False)}, files); time.sleep(2)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
