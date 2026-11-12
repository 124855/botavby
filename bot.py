"""
Telegram-бот: следит за новыми объявлениями на cars.av.by,
фильтрует по вашим условиям и присылает ссылку + краткий обзор от нейронки (Gemini).

Команды в Telegram:
  /status - жив ли бот, когда была последняя проверка, почему отсеивались объявления
  /test   - прислать одно реальное объявление с обзором (проверка всей цепочки)
  /scan   - проверить сайт прямо сейчас
  /debug  - технические данные (что вернул av.by), если что-то не работает
"""
import json
import os
import re
import time
import html
import logging
from collections import Counter
from pathlib import Path

import requests

# ---------- настройки из .env / переменных окружения ----------


def load_env_file(path=".env"):
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env_file()

TG_TOKEN = os.environ["TG_TOKEN"]
TG_CHAT_ID = str(os.environ["TG_CHAT_ID"])
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "600"))  # секунды
PAGES = int(os.getenv("PAGES", "3"))  # сколько страниц выдачи смотреть (по ~25 объявлений)
DATA_DIR = Path(os.getenv("DATA_DIR", "."))

# ---------- ваши фильтры ----------
PRICE_MIN = 2700
PRICE_MAX = 4500
MAX_ENGINE_L = 1.8
ONLY_MANUAL = True
ONLY_PRIVATE = True
EXCLUDED_BRANDS = ["opel", "mazda", "kia", "bmw", "audi", "mercedes"]
# Подстроки, которые должны встретиться в названии места. "гродн" = Гродно и вся Гродненская обл.
# Если хотите только ближние города, замените на нужные, например ["гродно", "скидель", "свислочь", "сопоцкин"]
LOCATION_KEYWORDS = [
    "гродн", "скидел", "сопоцк", "поречь", "свислоч", "волковыск", "мосты",
    "лид", "щучин", "берестовиц", "вороново", "кореличи", "новогрудок", "зельв",
]

API_URL = "https://api.av.by/offer-types/cars/filters/main/apply"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
    "Origin": "https://cars.av.by",
    "Referer": "https://cars.av.by/",
}

TG = f"https://api.telegram.org/bot{TG_TOKEN}"
DATA_DIR.mkdir(parents=True, exist_ok=True)
SEEN_FILE = DATA_DIR / "seen.json"
STATE_FILE = DATA_DIR / "state.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("avby-bot")

state = {
    "last_scan": None,
    "last_fetched": 0,
    "last_new": 0,
    "last_sent": 0,
    "last_rejects": Counter(),
    "last_error": None,
    "fail_streak": 0,
}

# ---------- Telegram ----------


def tg_send(text):
    try:
        r = requests.post(
            f"{TG}/sendMessage",
            json={
                "chat_id": TG_CHAT_ID,
                "text": text[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=30,
        )
        if not r.ok:
            log.error("Telegram: %s %s", r.status_code, r.text[:300])
    except Exception as e:
        log.error("Telegram send error: %s", e)


def tg_updates(offset, timeout=20):
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(f"{TG}/getUpdates", params=params, timeout=timeout + 20)
    r.raise_for_status()
    return r.json().get("result", [])


# ---------- av.by ----------


def fetch_adverts():
    out = []
    for page in range(1, PAGES + 1):
        payload = {
            "page": page,
            "properties": [
                {"modified": True, "name": "price_currency", "value": 2},
                {"name": "price_usd", "value": {"min": PRICE_MIN, "max": PRICE_MAX}},
            ],
            "sorting": 4,  # сначала новые
        }
        r = requests.post(API_URL, json=payload, headers=HEADERS, timeout=30)
        r.raise_for_status()
        ads = r.json().get("adverts", [])
        if not ads:
            break
        out.extend(ads)
        time.sleep(1)
    return out


def props(ad):
    return {
        p.get("name"): p.get("value")
        for p in ad.get("properties", [])
        if isinstance(p, dict)
    }


def price_usd(ad):
    try:
        return float(ad["price"]["usd"]["amount"])
    except Exception:
        return None


def brand_of(ad):
    b = props(ad).get("brand")
    if b:
        return str(b).lower()
    url = ad.get("publicUrl", "")
    parts = url.split("/")
    return parts[3].lower() if len(parts) > 3 else ""


def engine_l(ad):
    v = props(ad).get("engine_capacity")
    try:
        x = float(str(v).replace(",", ".").split()[0])
    except Exception:
        return None
    return x / 1000 if x > 50 else x


def location_of(ad):
    return " ".join(
        str(ad.get(k) or "") for k in ("locationName", "shortLocationName")
    ).lower()


COMPANY_RE = re.compile(
    r"\b(ооо|оао|зао|уп|чуп|тюп|ип|чтуп|автосалон|автоцентр|автомобили|motors|auto|group)\b",
    re.IGNORECASE,
)


def is_company(ad):
    for k in ("isCompany", "company", "shop", "dealer", "autoSalon", "companyId"):
        if ad.get(k):
            return True
    return bool(COMPANY_RE.search(str(ad.get("sellerName") or "")))


def reject_reason(ad):
    """None, если объявление подходит; иначе короткая причина."""
    p = price_usd(ad)
    if p is None:
        return "нет цены в USD"
    if not (PRICE_MIN <= p <= PRICE_MAX):
        return "цена"
    b = brand_of(ad)
    title = (ad.get("publicUrl", "") + " " + b).lower()
    if any(x in title for x in EXCLUDED_BRANDS):
        return "марка"
    if ONLY_MANUAL:
        tr = str(props(ad).get("transmission_type", "")).lower()
        if not tr:
            return "КПП неизвестна"
        if "мех" not in tr:
            return "не механика"
    e = engine_l(ad)
    if e is not None and e > MAX_ENGINE_L:
        return "объём двигателя"
    if ONLY_PRIVATE and is_company(ad):
        return "компания"
    loc = location_of(ad)
    if not loc.strip():
        return "место неизвестно"
    if not any(k in loc for k in LOCATION_KEYWORDS):
        return "регион"
    return None


# ---------- описание + нейронка ----------


def ad_title(ad):
    pr = props(ad)
    parts = [str(pr.get("brand", "")), str(pr.get("model", "")), str(pr.get("generation", ""))]
    t = " ".join(x for x in parts if x and x != "None").strip()
    return t or ad.get("publicUrl", "объявление")


def ad_facts(ad):
    pr = props(ad)
    lines = [
        f"Машина: {ad_title(ad)}",
        f"Год: {pr.get('year', '?')}",
        f"Пробег, км: {pr.get('mileage_km', '?')}",
        f"Двигатель: {pr.get('engine_capacity', '?')} л, {pr.get('engine_type', '?')}",
        f"КПП: {pr.get('transmission_type', '?')}",
        f"Кузов: {pr.get('body_type', '?')}",
        f"Привод: {pr.get('drive_type', '?')}",
        f"Цена: {price_usd(ad)} USD",
        f"Место: {ad.get('locationName') or ad.get('shortLocationName') or '?'}",
        f"Продавец: {ad.get('sellerName', '?')}",
    ]
    desc = (ad.get("description") or "").strip()
    lines.append(f"Описание продавца: {desc[:1500] if desc else '(пусто)'}")
    return "\n".join(lines)


PROMPT = """Ты помогаешь человеку, который совсем не разбирается в автомобилях, выбрать подержанную машину в Беларуси.
Ниже данные из объявления. Дай короткий обзор по-русски простыми словами, без жаргона.

Формат (без markdown, без звёздочек):
Вердикт: одно из «Стоит посмотреть» / «Смотреть с осторожностью» / «Лучше пропустить» + одно предложение почему.
Цена: дорого / нормально / дёшево для такого года и пробега (если не уверен, так и скажи).
Плюсы: 1-3 пункта.
Минусы и риски: 1-3 пункта (учти красные флаги в описании: ДТП, покраска, такси, не на ходу, срочно, скрутка пробега, плохо растаможена и т.п.).
Что проверить на осмотре: 2-3 конкретных пункта для этой модели.

Правила: не выдумывай факты, которых нет в данных. Если данных мало, скажи это. Ты оцениваешь только по тексту объявления, не по реальному состоянию машины. Уложись в 900 символов.

Данные:
"""


def ai_review(ad):
    if not GEMINI_API_KEY:
        return None
    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": PROMPT + ad_facts(ad)}]}]},
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        log.error("Gemini error: %s", e)
        return f"(обзор от нейронки не получился: {type(e).__name__})"


def format_message(ad, header="🚗 Новое объявление"):
    pr = props(ad)
    url = ad.get("publicUrl", "")
    head = (
        f"<b>{header}</b>\n"
        f"{html.escape(ad_title(ad))}, {pr.get('year', '?')} г. · "
        f"{price_usd(ad):.0f} $ · {pr.get('mileage_km', '?')} км\n"
        f"{html.escape(str(ad.get('locationName') or ad.get('shortLocationName') or ''))}\n"
        f"{url}"
    )
    review = ai_review(ad)
    if review:
        head += "\n\n🤖 <b>Обзор нейронки</b>\n" + html.escape(review)
    return head


# ---------- хранилище просмотренных ----------


def load_seen():
    if SEEN_FILE.exists():
        try:
            return list(json.loads(SEEN_FILE.read_text()))
        except Exception:
            pass
    return []


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(seen[-5000:]))


# ---------- сканирование ----------


def scan(seen, silent=False):
    rejects = Counter()
    try:
        ads = fetch_adverts()
    except Exception as e:
        state["fail_streak"] += 1
        state["last_error"] = f"{type(e).__name__}: {e}"
        log.error("fetch error: %s", e)
        if state["fail_streak"] == 3:
            tg_send(
                "⚠️ av.by не отдаёт данные уже 3 проверки подряд.\n"
                f"Ошибка: {html.escape(state['last_error'][:300])}\n"
                "Возможно, сайт изменил запросы или блокирует этот сервер."
            )
        return
    state["fail_streak"] = 0
    state["last_error"] = None

    seen_set = set(seen)
    new = sent = 0
    for ad in reversed(ads):  # от старых к новым
        ad_id = str(ad.get("id"))
        if ad_id in seen_set:
            continue
        seen.append(ad_id)
        seen_set.add(ad_id)
        new += 1
        reason = reject_reason(ad)
        if reason:
            rejects[reason] += 1
            continue
        if not silent:
            tg_send(format_message(ad))
            sent += 1
            time.sleep(1)
    save_seen(seen)

    state.update(
        last_scan=time.strftime("%H:%M:%S"),
        last_fetched=len(ads),
        last_new=new,
        last_sent=sent,
        last_rejects=rejects,
    )
    log.info("scan: получено %s, новых %s, отправлено %s, отсеяно %s", len(ads), new, sent, dict(rejects))


# ---------- команды ----------


def cmd_status():
    rej = ", ".join(f"{k}: {v}" for k, v in state["last_rejects"].items()) or "-"
    err = f"\n⚠️ Последняя ошибка: {html.escape(state['last_error'])}" if state["last_error"] else ""
    tg_send(
        "✅ Бот работает\n"
        f"Проверка каждые {CHECK_INTERVAL // 60} мин\n"
        f"Последняя проверка: {state['last_scan'] or 'ещё не было'}\n"
        f"Получено с сайта: {state['last_fetched']}, новых: {state['last_new']}, "
        f"отправлено вам: {state['last_sent']}\n"
        f"Отсеяно по причинам: {rej}\n"
        f"Нейронка: {'подключена' if GEMINI_API_KEY else 'не подключена (нет GEMINI_API_KEY)'}"
        + err
    )


def cmd_test():
    tg_send("Ищу тестовое объявление...")
    try:
        ads = fetch_adverts()
    except Exception as e:
        tg_send(f"❌ Не смог получить данные с av.by: {html.escape(str(e)[:300])}")
        return
    if not ads:
        tg_send("❌ av.by вернул пустой список. Формат ответа мог измениться, нажмите /debug")
        return
    for ad in ads:
        if reject_reason(ad) is None:
            tg_send(format_message(ad, "🧪 ТЕСТ (подходит под ваши фильтры)"))
            return
    ad = ads[0]
    tg_send(
        "Сейчас ни одно из последних объявлений не проходит фильтры. "
        "Показываю первое попавшееся, чтобы проверить связку с нейронкой."
    )
    tg_send(format_message(ad, f"🧪 ТЕСТ (НЕ проходит фильтры: {reject_reason(ad)})"))


def cmd_debug():
    try:
        ads = fetch_adverts()
    except Exception as e:
        tg_send(f"❌ Ошибка запроса: {html.escape(str(e)[:500])}")
        return
    if not ads:
        tg_send("Пустой ответ (0 объявлений).")
        return
    rej = Counter(reject_reason(a) or "ПОДХОДИТ" for a in ads)
    ad = ads[0]
    sample = {k: (v if not isinstance(v, (dict, list)) else "...") for k, v in ad.items()}
    sample["_props"] = props(ad)
    sample["description"] = str(sample.get("description"))[:80]
    txt = (
        f"Получено: {len(ads)}\nПричины: {dict(rej)}\n\nПервое объявление:\n"
        + json.dumps(sample, ensure_ascii=False, indent=1)
    )
    tg_send("<pre>" + html.escape(txt[:3800]) + "</pre>")


# ---------- главный цикл ----------


def handle_command(text, seen, once):
    """Возвращает True, если была внеплановая проверка."""
    if text in ("/start", "/status"):
        cmd_status()
    elif text == "/test":
        cmd_test()
    elif text == "/debug":
        cmd_debug()
    elif text == "/scan":
        if once:
            tg_send("В облачном режиме проверка идёт сама по расписанию (раз в ~10 минут). Вот статус:")
            cmd_status()
        else:
            tg_send("Проверяю...")
            scan(seen)
            cmd_status()
            return True
    return False


def process_updates(offset, seen, once, timeout):
    rescanned = False
    for u in tg_updates(offset, timeout):
        offset = u["update_id"] + 1
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != TG_CHAT_ID:
            continue
        text = (msg.get("text") or "").strip().split("@")[0]
        if handle_command(text, seen, once):
            rescanned = True
    return offset, rescanned


def load_state_file():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def run_once():
    """Режим для GitHub Actions: одна проверка + ответы на накопившиеся команды, затем выход."""
    saved = load_state_file()
    state["fail_streak"] = saved.get("fail_streak", 0)
    first_run = not SEEN_FILE.exists()
    seen = load_seen()
    scan(seen, silent=first_run)
    if first_run and SEEN_FILE.exists():
        tg_send(
            "🤖 Бот запущен в облаке. Запомнил текущие объявления (%d), буду присылать только новые.\n"
            "Команды: /status /test /debug (ответ приходит при ближайшей проверке, до ~10 минут)." % len(seen)
        )
    offset = saved.get("offset")
    try:
        offset, _ = process_updates(offset, seen, once=True, timeout=0)
    except Exception as e:
        log.error("updates error: %s", e)
    STATE_FILE.write_text(json.dumps({"offset": offset, "fail_streak": state["fail_streak"]}))


def run_forever():
    first_run = not SEEN_FILE.exists()
    seen = load_seen()
    tg_send(
        "🤖 Бот запущен. Команды: /status /test /scan /debug\n"
        + ("Первый запуск: запоминаю текущие объявления, присылать буду только новые." if first_run else "")
    )
    last_scan = 0
    offset = None
    while True:
        if time.time() - last_scan >= CHECK_INTERVAL:
            scan(seen, silent=first_run)
            if first_run:
                first_run = False
                tg_send(f"Запомнил {len(seen)} текущих объявлений. Теперь жду новые.")
            last_scan = time.time()
        try:
            offset, rescanned = process_updates(offset, seen, once=False, timeout=20)
            if rescanned:
                last_scan = time.time()
        except Exception as e:
            log.error("updates error: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    import sys

    if "--once" in sys.argv:
        run_once()
    else:
        run_forever()
