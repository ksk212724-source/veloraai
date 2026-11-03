"""Velora: единый сервер (мини-приложение + Telegram-бот + оплата ЮKassa и Telegram Stars).

Запуск на Render:   gunicorn server:app --workers 1 --threads 16 --timeout 900
  (воркер строго один: сессии и задачи хранятся в памяти; timeout не меньше 300, игры и видео делаются долго)
Файлы рядом: server.py, index.html (приложение), persona.md (необязательно), requirements.txt.

ОБЯЗАТЕЛЬНЫЕ переменные
  BOT_TOKEN                 токен бота от @BotFather
  WEBAPP_URL                https-адрес сервера (на Render подхватится сам из RENDER_EXTERNAL_URL)
  YOOKASSA_SHOP_ID          номер магазина ЮKassa (1513722)
  YOOKASSA_SECRET_KEY       секретный ключ ЮKassa. Только в переменных окружения, не в коде
  и ключ нейросети: GEMINI_API_KEY или OPENROUTER_API_KEY (или LLM_API_URL / LLM_API_KEY / LLM_MODEL)

ЦЕНЫ: таблица PRICES в этом файле (сейчас везде 1 ₽ и 1 ⭐). Переменные в Render для цен не нужны.

ХРАНЕНИЕ (на Render без диска всё стирается при деплое: подписки пропадут)
  DB_PATH=/var/data/velora.db   MEDIA_DIR=/var/data/media   (Disk с Mount Path /var/data)

ОСТАЛЬНОЕ
  YOOKASSA_RECEIPT_EMAIL (только для ИП/ООО с чеками), YOOKASSA_RETURN_URL,
  LLM_MAX_TOKENS (по умолчанию 60000), LLM_CONTINUES (авто-продолжение длинных ответов, по умолчанию 12), UNLIMITED (1 = бета без лимитов), FREE_LIMIT, BETA_PASSWORD, BETA_GATE,
  POLLINATIONS_KEY, OR_*_MODEL, GEMINI_*, CF_ACCOUNT_ID / CF_API_TOKEN (голос), TTS_VOICE,
  USE_POLLING, MOCK, ALLOW_BROWSER, MEDIA_TTL_HOURS.
В кабинете ЮKassa: Интеграция → HTTP-уведомления → WEBAPP_URL/api/yookassa/webhook, событие payment.succeeded.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

import requests
from flask import Flask, abort, jsonify, request, send_from_directory

BASE = Path(__file__).parent
MEDIA = Path(os.getenv("MEDIA_DIR") or BASE / "media")
MEDIA.mkdir(parents=True, exist_ok=True)
DB_PATH = os.getenv("DB_PATH") or str(BASE / "velora.db")
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
TG_API = os.getenv("TG_API", "https://api.telegram.org")
MEDIA_TTL = int(os.getenv("MEDIA_TTL_HOURS", "24")) * 3600
LLM_MAX = int(os.getenv("LLM_MAX_TOKENS", "60000"))
LLM_CONT = int(os.getenv("LLM_CONTINUES", "12"))  # сколько раз автоматически «продолжить», если ответ упёрся в лимит
LLM_DEADLINE = int(os.getenv("LLM_DEADLINE", "600"))  # общий предел одного ответа, секунд
CONT_ASK = ("Продолжи ровно с того места, где оборвался ответ. Не повторяй уже написанное, без вступлений, "
            "не начинай новый блок кода и не повторяй открывающие ```: просто допиши текст и код до конца.")


def glue(a: str, b: str) -> str:
    """Склеивает продолжение: убирает повторное открытие блока кода, если мы уже внутри него."""
    if a.count("```") % 2 == 1:
        b = re.sub(r"^\s*```[a-zA-Z]*\s*\n", "", b, count=1)
    return a + b

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = (os.getenv("WEBAPP_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
POLL_KEY = os.getenv("POLLINATIONS_KEY", "")
MOCK = os.getenv("MOCK") == "1"
ALLOW_BROWSER = os.getenv("ALLOW_BROWSER") == "1" or MOCK

# OpenRouter
OR_KEY = os.getenv("OPENROUTER_API_KEY", "")
OR_BASE = "https://openrouter.ai/api/v1"
OR_CHAT_MODEL = os.getenv("OR_CHAT_MODEL", "google/gemini-2.5-flash")
OR_IMAGE_MODEL = os.getenv("OR_IMAGE_MODEL", "bytedance-seed/seedream-4.5")
OR_EDIT_MODEL = os.getenv("OR_EDIT_MODEL", "") or OR_IMAGE_MODEL
OR_VIDEO_MODEL = os.getenv("OR_VIDEO_MODEL", "google/veo-3.1-fast")

LLM_API_URL = os.getenv("LLM_API_URL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")

# Google Gemini
GEMINI_KEY = os.getenv("GEMINI_API_KEY") or (LLM_API_KEY if "googleapis.com" in LLM_API_URL else "")
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")

# Голос: распознавание через Cloudflare Workers AI (Whisper), озвучка через edge-tts
CF_ACCOUNT = os.getenv("CF_ACCOUNT_ID", "")
CF_TOKEN = os.getenv("CF_API_TOKEN", "")
TTS_VOICE = os.getenv("TTS_VOICE", "ru-RU-SvetlanaNeural")  # мужской: ru-RU-DmitryNeural

FREE_LLM_URL = os.getenv("FREE_LLM_URL", "https://gen.pollinations.ai/v1/chat/completions")
FREE_LLM_MODEL = os.getenv("FREE_LLM_MODEL", "openai")
PERSONA = (BASE / "persona.md").read_text(encoding="utf-8") if (BASE / "persona.md").exists() \
    else "Ты Velora, дружелюбная ИИ-помощница. Отвечай по-русски, коротко и по делу."

BETA_PASSWORD = os.getenv("BETA_PASSWORD", "5050")
BETA_GATE = os.getenv("BETA_GATE", "1") == "1"
FREE = int(os.getenv("FREE_LIMIT", "10"))
UNLIMITED = os.getenv("UNLIMITED", "1") == "1"
WINDOW = 3 * 3600


# ===================== ЦЕНЫ: меняются прямо здесь, в Render ничего вписывать не нужно =====================
#            рубли  звёзды
PRICES = {"plus":      (1, 1),
          "pro":       (1, 1),
          "ultra":     (1, 1),
          "pro_trial": (1, 1)}   # пробный Pro на 3 дня
# ===========================================================================================================

PLANS = {
    "plus": {"days": 30, "grant": "plus", "rub": PRICES["plus"][0], "stars": PRICES["plus"][1],
             "title": "Velora Plus", "desc": "600K токенов в день, фото без лимита, голос и проекты"},
    "pro": {"days": 30, "grant": "pro", "rub": PRICES["pro"][0], "stars": PRICES["pro"][1],
            "title": "Velora Pro", "desc": "1,5M токенов в день, видео из кода, глубокое исследование"},
    "ultra": {"days": 30, "grant": "ultra", "rub": PRICES["ultra"][0], "stars": PRICES["ultra"][1],
              "title": "Velora Ultra", "desc": "3M токенов в день, всё без ограничений"},
    "pro_trial": {"days": 3, "grant": "pro", "rub": PRICES["pro_trial"][0], "stars": PRICES["pro_trial"][1],
                  "title": "Velora Pro · 3 дня", "desc": "Пробный Pro на 3 дня"},
}

_INVIS = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00a0\u202f\u2009\u200a"), None)
_QUOTES = {ord(c): '"' for c in "\u201c\u201d\u201e\u00ab\u00bb"}
_QUOTES.update({ord(c): "'" for c in "\u2018\u2019\u201a"})
_QUOTES.update({ord(c): "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2212"})


def _env_clean(v: str) -> str:
    """Чистим значение из Render: невидимые символы, умные кавычки, пробелы и кавычки по краям."""
    return (v or "").translate(_INVIS).translate(_QUOTES).strip().strip("\"'").strip()


def _bad_chars(name: str, v: str) -> str:
    import unicodedata
    for i, ch in enumerate(v):
        if ord(ch) > 126 or ord(ch) < 33:
            return (f"В {name} на позиции {i + 1} недопустимый символ «{ch}» ({unicodedata.name(ch, 'без имени')}). "
                    "Скорее всего, русская буква, похожая на латинскую, или лишний знак. "
                    "Удалите значение в Render и вставьте ключ заново прямо из кабинета ЮKassa (кнопка «Скопировать»).")
    return ""


YK_API = os.getenv("YK_API", "https://api.yookassa.ru/v3").strip().rstrip("/")
YK_SHOP = _env_clean(os.getenv("YOOKASSA_SHOP_ID", "1513722"))
YK_KEY = _env_clean(os.getenv("YOOKASSA_SECRET_KEY") or os.getenv("YOOKASSA_SECRET", ""))
YK_RETURN = (os.getenv("YOOKASSA_RETURN_URL", "") or "").strip()
YK_EMAIL = (os.getenv("YOOKASSA_RECEIPT_EMAIL") or os.getenv("YK_RECEIPT_EMAIL", "")).strip()
YK_TAX = (os.getenv("YOOKASSA_TAX_SYSTEM", "") or "").strip()  # 1..6, только если у магазина несколько систем налогообложения

GEN_URL = "https://gen.pollinations.ai/image/"
OLD_IMG_URL = "https://image.pollinations.ai/prompt/"
STYLES = {
    "none": "",
    "cinematic": ", cinematic lighting, film still, dramatic, high detail",
    "photo": ", ultra realistic photo, 35mm, natural light, sharp focus",
    "anime": ", anime style, clean lineart, vibrant, studio quality",
    "3d": ", 3D render, soft studio light, octane, glossy materials",
    "neon": ", neon noir, dark background, glowing accents, moody",
    "minimal": ", minimalist, clean composition, soft gradients, elegant",
}
IMAGE_SIZES = {"1:1": (1024, 1024), "3:4": (768, 1024), "16:9": (1280, 720), "9:16": (720, 1280)}

app = Flask(__name__, static_folder=None)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.executescript("""
create table if not exists users(uid text primary key, used int default 0, until real default 0);
create table if not exists pays(id text primary key, uid text, status text, t real);
create table if not exists beta(uid text primary key);
""")
for _sql in ("alter table users add column lk real default 0",
             "alter table users add column plan text default ''",
             "alter table pays add column plan text default ''",
             "alter table pays add column amount int default 0",
             "alter table pays add column method text default ''"):
    try:
        db.execute(_sql)
        db.commit()
    except sqlite3.OperationalError:
        pass  # колонка уже есть

tickets: dict = {}
jobs: dict = {}
hits: dict = {}
sessions: dict = {}  # sid -> (uid, до какого времени)
SESSION_TTL = int(os.getenv("SESSION_TTL", str(6 * 3600)))
APP_MSG_TTL = int(os.getenv("APP_MSG_TTL", "45"))
open_msgs: dict = {}
lock = threading.Lock()


# ---------- защита ----------
def valid_init_data(raw: str) -> bool:
    try:
        pairs = dict(urllib.parse.parse_qsl(raw, keep_blank_values=True))
        got = pairs.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        fresh = time.time() - int(pairs.get("auth_date", "0")) < 86400
        return hmac.compare_digest(calc, got) and fresh
    except Exception:
        return False


def is_beta(u: str) -> bool:
    if not BETA_GATE:
        return True
    with lock:
        return db.execute("select 1 from beta where uid=?", (u,)).fetchone() is not None


def add_beta(u: str):
    with lock:
        db.execute("insert or ignore into beta(uid) values(?)", (u,))
        db.commit()


def uid() -> str:
    raw = request.headers.get("X-Init-Data", "")
    if ALLOW_BROWSER and not raw:
        return "web:" + (request.headers.get("X-Anon-Id") or request.remote_addr or "x")[:60]
    try:
        return "tg:" + str(json.loads(dict(urllib.parse.parse_qsl(raw)).get("user", "{}"))["id"])
    except Exception:
        abort(403)


def check_access(gate=True):
    if ALLOW_BROWSER:
        return
    if not BOT_TOKEN or not valid_init_data(request.headers.get("X-Init-Data", "")):
        abort(403)
    if gate and BETA_GATE:  # при каждом открытии мини-приложения пароль вводится заново
        s = sessions.get(request.headers.get("X-Sess", ""))
        if not s or s[1] < time.time() or s[0] != uid():
            abort(401)


def client_ip() -> str:
    """За прокси Render настоящий адрес добавляет сам Render, он последний в цепочке (первые можно подделать)."""
    xff = request.headers.get("X-Forwarded-For", "")
    return (xff.split(",")[-1].strip() if xff else request.remote_addr) or "x"


def rate_limit(key: str, limit=20, per=3600):
    now = time.time()
    with lock:
        arr = [t for t in hits.get(key, []) if now - t < per]
        if len(arr) >= limit:
            hits[key] = arr
            abort(429)
        arr.append(now)
        hits[key] = arr


# ---------- лимиты и оплата ----------
def row(u: str):
    """Возвращает (used, until, lk) и при необходимости запускает/снимает паузу."""
    with lock:
        db.execute("insert or ignore into users(uid) values(?)", (u,))
        db.commit()
        used, until, lk = db.execute("select used, until, lk from users where uid=?", (u,)).fetchone()
        now = time.time()
        if used >= FREE and not lk:  # лимит кончился: пауза в 3 часа
            lk = now + WINDOW
            db.execute("update users set lk=? where uid=?", (lk, u))
            db.commit()
        if lk and now >= lk:  # пауза прошла: новые запросы
            db.execute("update users set used=0, lk=0 where uid=?", (u,))
            db.commit()
            used, lk = 0, 0
    return used, until, lk


def spend(u: str) -> bool:
    if UNLIMITED:
        return True
    used, until, _ = row(u)
    if until > time.time():
        return True
    if used >= FREE:
        return False
    with lock:
        db.execute("update users set used=used+1, lk=case when used+1>=? then ? else lk end where uid=?",
                   (FREE, time.time() + WINDOW, u))
        db.commit()
    return True


def paywall():
    return jsonify(error="Бесплатные запросы закончились", paywall=True), 402


def yk_creds_problem() -> str:
    return _bad_chars("YOOKASSA_SHOP_ID", YK_SHOP) or _bad_chars("YOOKASSA_SECRET_KEY", YK_KEY)


def yk(method: str, path: str, body=None):
    bad = yk_creds_problem()
    if bad:
        raise RuntimeError(bad)
    r = requests.request(method, YK_API + path, json=body, auth=(YK_SHOP, YK_KEY),
                         headers={"Idempotence-Key": uuid.uuid4().hex}, timeout=30)
    if r.status_code >= 400:
        print("ЮKassa", method, path, r.status_code, r.text[:300])
    r.raise_for_status()
    return r.json()


_yk_me = {"t": 0.0, "d": None}


def yk_me(force: bool = False) -> dict:
    """Настройки магазина у ЮKassa (GET /me): включены ли чеки, тестовый ли магазин."""
    if not force and _yk_me["d"] is not None and time.time() - _yk_me["t"] < 600:
        return _yk_me["d"]
    d = yk("GET", "/me")
    _yk_me.update(t=time.time(), d=d)
    return d


def yk_fiscal() -> bool:
    """True, если в магазине включён сервис чеков ЮKassa: тогда receipt в платеже обязателен."""
    try:
        d = yk_me()
    except Exception:  # noqa: BLE001
        return bool(YK_EMAIL)
    f = d.get("fiscalization")
    if isinstance(f, dict) and "enabled" in f:
        return bool(f["enabled"])
    return bool(d.get("fiscalization_enabled"))


def yk_err(e: "requests.HTTPError") -> tuple:
    code = desc = par = ""
    try:
        j = e.response.json()
        code, desc, par = j.get("code", ""), j.get("description", ""), j.get("parameter", "")
    except Exception:  # noqa: BLE001
        desc = (e.response.text or "")[:150]
    return e.response.status_code, code, desc, par


def yk_create(u: str, plan: str, ret: str) -> dict:
    """Создаёт платёж. Чек добавляется, только если его требует магазин; при ошибке про чек пробуем наоборот."""
    p = PLANS[plan]
    val = f"{p['rub']}.00"
    base = {"amount": {"value": val, "currency": "RUB"}, "capture": True,
            "confirmation": {"type": "redirect", "return_url": ret},
            "description": f"{p['title']} · {p['days']} дн.", "metadata": {"uid": u, "plan": plan}}

    def body(with_receipt: bool) -> dict:
        b = dict(base)
        if with_receipt:
            rc = {"customer": {"email": YK_EMAIL}, "items": [{
                "description": p["title"][:128], "quantity": "1",
                "amount": {"value": val, "currency": "RUB"},
                "vat_code": int(os.getenv("YOOKASSA_VAT_CODE", "1")),
                "payment_subject": "service", "payment_mode": "full_payment"}]}
            if YK_TAX.isdigit():
                rc["tax_system_code"] = int(YK_TAX)
            b["receipt"] = rc
        return b

    first = yk_fiscal() and bool(YK_EMAIL)
    try:
        return yk("POST", "/payments", body(first))
    except requests.HTTPError as e:
        st, code, desc, par = yk_err(e)
        about_receipt = "receipt" in (desc + par).lower() or "чек" in (desc + par).lower()
        if st == 400 and about_receipt:
            if not first and YK_EMAIL:
                return yk("POST", "/payments", body(True))
            if first:
                return yk("POST", "/payments", body(False))
            raise RuntimeError("Магазину нужен чек: добавьте в Render переменную YOOKASSA_RECEIPT_EMAIL (любая ваша почта)")
        raise


def sub_of(u: str) -> dict:
    """Настоящая подписка пользователя (без учёта беты)."""
    with lock:
        r = db.execute("select until, plan from users where uid=?", (u,)).fetchone()
    until, plan = (r[0] or 0, r[1] or "") if r else (0, "")
    if until > time.time():
        return {"active": True, "plan": plan or "plus", "sub_until": int(until)}
    return {"active": False, "plan": "free", "sub_until": 0}


def grant(pid: str, u: str, plan: str, amount: int = 0, method: str = "") -> bool:
    """Выдаёт подписку один раз на платёж pid. True, если выдана именно сейчас."""
    p = PLANS[plan]
    with lock:
        r = db.execute("select status from pays where id=?", (pid,)).fetchone()
        if r and r[0] == "paid":
            return False
        if r:
            db.execute("update pays set status='paid', plan=?, amount=?, method=? where id=?", (plan, amount, method, pid))
        else:
            db.execute("insert into pays(id,uid,status,t,plan,amount,method) values(?,?,?,?,?,?,?)",
                       (pid, u, "paid", time.time(), plan, amount, method))
        db.execute("insert or ignore into users(uid) values(?)", (u,))
        until = db.execute("select until from users where uid=?", (u,)).fetchone()[0] or 0
        db.execute("update users set until=?, plan=? where uid=?", (max(time.time(), until) + p["days"] * 86400, p["grant"], u))
        db.commit()
    return True


def trial_blocked(u: str) -> str:
    if sub_of(u)["active"]:
        return "Пробный период доступен, пока нет активной подписки"
    with lock:
        used = db.execute("select 1 from pays where uid=? and plan='pro_trial' and status='paid'", (u,)).fetchone()
    return "Пробный период уже использован" if used else ""


def sync(pid: str):
    """Спрашиваем у ЮKassa настоящий статус платежа: вебхук подделать нельзя, мы ему не верим."""
    with lock:
        r = db.execute("select uid, plan, status from pays where id=?", (pid,)).fetchone()
    if not r or MOCK or r[2] == "paid" or r[1] not in PLANS:
        return
    p = yk("GET", "/payments/" + pid)
    if p.get("status") == "succeeded" and p.get("paid"):
        am = p.get("amount") or {}
        if am.get("currency") != "RUB" or float(am.get("value", 0)) + 0.001 < PLANS[r[1]]["rub"]:
            print("ЮKassa: сумма не совпадает с тарифом, подписка не выдана:", pid, am)
            return
        grant(pid, r[0], r[1], int(float(am["value"])), "yookassa")
    elif p.get("status") == "canceled":
        with lock:
            db.execute("update pays set status='canceled' where id=?", (pid,))
            db.commit()


_bot_name = [""]


def bot_username() -> str:
    if not _bot_name[0] and BOT_TOKEN:
        try:
            _bot_name[0] = (tg("getMe").get("result") or {}).get("username", "")
        except Exception:  # noqa: BLE001
            pass
    return _bot_name[0]


# ---------- Telegram Stars ----------
ALLOWED = ["message", "pre_checkout_query"]


def stars_invoice(u: str, plan: str) -> str:
    p = PLANS[plan]
    res = tg("createInvoiceLink", title=p["title"], description=p["desc"],
             payload=json.dumps({"u": u, "p": plan}), provider_token="", currency="XTR",
             prices=[{"label": p["title"], "amount": p["stars"]}])
    if not res.get("ok"):
        raise RuntimeError(res.get("description") or "нет ответа")
    return res["result"]


def stars_payload(raw):
    """(uid, plan) из payload нашего счёта или None."""
    try:
        pl = json.loads(raw)
        if pl.get("p") in PLANS and str(pl.get("u", "")).startswith("tg:"):
            return str(pl["u"]), pl["p"]
    except Exception:  # noqa: BLE001
        pass
    return None


def is_payment(upd: dict) -> bool:
    return "pre_checkout_query" in upd or bool((upd.get("message") or {}).get("successful_payment"))


def handle_payment(upd: dict):
    """Платёжные события обрабатываются сразу и честно: при ошибке исключение, и Telegram пришлёт событие ещё раз."""
    q = upd.get("pre_checkout_query")
    if q:
        pl = stars_payload(q.get("invoice_payload", ""))
        ok = bool(pl) and q.get("currency") == "XTR" and q.get("total_amount") == PLANS[pl[1]]["stars"]
        a = {"pre_checkout_query_id": q["id"], "ok": ok}
        if not ok:
            a["error_message"] = "Не удалось проверить счёт, попробуйте ещё раз"
        res = tg("answerPreCheckoutQuery", **a)
        if not res.get("ok"):
            print("answerPreCheckoutQuery:", res)
        return
    m = upd.get("message") or {}
    sp = m.get("successful_payment")
    if not sp or sp.get("currency") != "XTR":
        return
    pl = stars_payload(sp.get("invoice_payload", ""))
    if not pl:
        print("Stars: платёж с чужим или битым payload, пропущен:", str(sp)[:300])
        return
    u, plan = pl
    paid = int(sp.get("total_amount", 0))
    if paid < PLANS[plan]["stars"]:
        print(f"Stars: оплачено {paid} вместо {PLANS[plan]['stars']} за {plan}, user {u}, подписка НЕ выдана:",
              sp.get("telegram_payment_charge_id"))
        return
    if grant("tg:" + (sp.get("telegram_payment_charge_id") or uuid.uuid4().hex), u, plan, paid, "stars"):
        try:
            tg("sendMessage", chat_id=(m.get("chat") or {}).get("id") or u[3:],
               text="Оплата прошла ⭐ Подписка подключена. Вернитесь в приложение.")
        except Exception:  # noqa: BLE001
            pass


# ---------- нейросеть чата ----------
def gemini_chat(msgs, system, max_tokens) -> str:
    model = LLM_MODEL if "gemini" in LLM_MODEL else "gemini-2.5-flash"
    contents = []
    for m in msgs:
        c = m.get("content")
        parts = [{"text": c}] if isinstance(c, str) else []
        if not isinstance(c, str):
            for it in c or []:
                if it.get("type") == "text":
                    parts.append({"text": it.get("text", "")})
                elif it.get("type") == "image_url":
                    u = (it.get("image_url") or {}).get("url", "")
                    if u.startswith("data:") and "," in u:
                        head, b64 = u.split(",", 1)
                        parts.append({"inline_data": {"mime_type": head[5:].split(";")[0] or "image/jpeg",
                                                      "data": b64}})
        if parts:
            contents.append({"role": "model" if m.get("role") == "assistant" else "user", "parts": parts})
    gen = {"temperature": 0.8, "maxOutputTokens": min(max(max_tokens, 400), 65000)}
    if "flash" in model:
        gen["thinkingConfig"] = {"thinkingBudget": 0}
    out, t0 = "", time.time()
    for _ in range(LLM_CONT + 1):
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY}, timeout=280,
            json={"systemInstruction": {"parts": [{"text": system}]}, "contents": contents,
                  "generationConfig": gen})
        if r.status_code != 200:
            if out:
                break
            raise RuntimeError(f"Google код {r.status_code}: {r.text[:200]}")
        cand = (r.json().get("candidates") or [{}])[0]
        txt = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts") or [])
        if not txt.strip() and not out:
            raise RuntimeError("Google вернул пустой ответ")
        out = glue(out, txt) if out else txt
        if cand.get("finishReason") != "MAX_TOKENS" or time.time() - t0 > LLM_DEADLINE:
            break
        contents = contents + [{"role": "model", "parts": [{"text": txt}]},
                               {"role": "user", "parts": [{"text": CONT_ASK}]}]
    return out.strip()


def ask_llm(msgs, system=None, max_tokens=700) -> str:
    """Сначала Google (если есть ключ), затем OpenRouter, ваш API, бесплатный Pollinations."""
    if GEMINI_KEY:
        try:
            return gemini_chat(msgs, system or PERSONA, max_tokens)
        except Exception as e:  # noqa: BLE001
            print("LLM Google:", type(e).__name__, str(e)[:300])
    providers = []
    if OR_KEY:
        providers.append((OR_BASE + "/chat/completions", OR_KEY, OR_CHAT_MODEL))
    if LLM_API_URL:
        providers.append((LLM_API_URL, LLM_API_KEY, LLM_MODEL))
    providers.append((FREE_LLM_URL, POLL_KEY, FREE_LLM_MODEL))
    last = None
    for url, key, model in providers:
        try:
            h = {"Content-Type": "application/json"}
            if key:
                h["Authorization"] = f"Bearer {key}"
            sysmsg = system or PERSONA

            def call(messages, url=url, h=h, model=model):
                return requests.post(url, headers=h, timeout=280, json={
                    "model": model, "temperature": 0.8, "max_tokens": min(max_tokens, 16000), "messages": messages})

            sent = [{"role": "system", "content": sysmsg}] + msgs
            r = call(sent)
            if r.status_code == 400:  # некоторые модели не принимают system: вкладываем в первое сообщение
                folded = [dict(m) for m in msgs]
                for m in folded:
                    if m.get("role") == "user":
                        c = m["content"]
                        if isinstance(c, str):
                            m["content"] = sysmsg + "\n\n---\n\n" + c
                        else:
                            m["content"] = [{"type": "text", "text": sysmsg + "\n\n---\n\n"}] + c
                        break
                sent = folded
                r = call(folded)
            if r.status_code != 200:
                print("LLM", url.split("/")[2], r.status_code, r.text[:300])
                last = f"{url.split('/')[2]} код {r.status_code}"
                continue
            ch = r.json()["choices"][0]
            txt = ch["message"]["content"] or ""
            t0 = time.time()
            for _ in range(LLM_CONT):
                if ch.get("finish_reason") != "length" or not txt or time.time() - t0 > LLM_DEADLINE:
                    break
                r2 = call(sent + [{"role": "assistant", "content": txt}, {"role": "user", "content": CONT_ASK}])
                if r2.status_code != 200:
                    break
                ch = r2.json()["choices"][0]
                txt = glue(txt, ch["message"]["content"] or "")
            txt = txt.strip()
            if txt:
                return txt
        except Exception as e:  # noqa: BLE001
            print("LLM", url.split("/")[2], type(e).__name__, str(e)[:200])
            last = f"{url.split('/')[2]} {type(e).__name__}"
    raise RuntimeError(str(last) if last else "пустой ответ")


# ---------- генерация ----------
def auth_headers():
    return {"Authorization": f"Bearer {POLL_KEY}"} if POLL_KEY else {}


def mock_make(kind: str, path: Path, prompt: str):
    time.sleep(3)
    if kind == "image":
        from PIL import Image, ImageDraw
        h = int(hashlib.md5(prompt.encode()).hexdigest()[:6], 16)
        im = Image.new("RGB", (768, 768), (8, 8, 12))
        d = ImageDraw.Draw(im)
        for i in range(6):
            c = ((h >> (i * 2)) & 255, (h >> (i * 3)) & 255, 255 - ((h >> i) & 255))
            x, y = 120 + (h >> i) % 400, 120 + (h >> (i + 3)) % 400
            d.ellipse([x - 160, y - 160, x + 160, y + 160], fill=tuple(v // 3 for v in c))
        im.save(path, "PNG")
    else:
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=480x854:rate=24:duration=3",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", str(path)],
            check=True, capture_output=True)


def or_headers():
    return {"Authorization": f"Bearer {OR_KEY}", "Content-Type": "application/json"}


def or_ok(r):
    if r.status_code >= 400:
        raise RuntimeError(f"код {r.status_code}: {r.text[:200]}")


def or_image(prompt: str, aspect: str, src: str = "") -> tuple:
    body = {"model": OR_EDIT_MODEL if src else OR_IMAGE_MODEL, "prompt": prompt, "n": 1}
    if aspect in ("1:1", "3:4", "16:9", "9:16"):
        body["aspect_ratio"] = aspect
    if src:  # правка: исходное фото передаём ссылкой
        body["input_references"] = [{"type": "image_url", "image_url": {"url": src}}]
    r = requests.post(OR_BASE + "/images", headers=or_headers(), json=body, timeout=240)
    or_ok(r)
    item = r.json()["data"][0]
    return base64.b64decode(item["b64_json"]), item.get("media_type") or "image/png"


def or_video(prompt: str, aspect: str, dur: int) -> tuple:
    body = {"model": OR_VIDEO_MODEL, "prompt": prompt, "duration": dur,
            "aspect_ratio": "9:16" if aspect == "9:16" else "16:9"}
    r = requests.post(OR_BASE + "/videos", headers=or_headers(), json=body, timeout=60)
    or_ok(r)
    job = r.json()
    poll = urllib.parse.urljoin("https://openrouter.ai", job["polling_url"])
    deadline = time.time() + 540
    while time.time() < deadline:
        time.sleep(6)
        j = requests.get(poll, headers=or_headers(), timeout=30).json()
        st = j.get("status")
        if st == "completed":
            for u in j.get("unsigned_urls") or []:
                v = requests.get(u, headers=or_headers(), timeout=300)
                if v.status_code == 200 and len(v.content) > 1000:
                    return v.content, "video/mp4"
            v = requests.get(f"{OR_BASE}/videos/{job['id']}/content", headers=or_headers(), timeout=300)
            v.raise_for_status()
            return v.content, "video/mp4"
        if st in ("failed", "cancelled", "expired"):
            raise RuntimeError("видео не получилось: " + str(j.get("error", st))[:120])
    raise RuntimeError("видео делается слишком долго")


def google_image(prompt: str, aspect: str, src_url: str = "") -> tuple:
    """Картинка через Google Gemini. Правка: исходник читаем из папки media."""
    parts = [{"text": prompt}]
    if src_url:
        f = MEDIA / Path(urllib.parse.urlparse(src_url).path).name
        parts.append({"inline_data": {"mime_type": "image/jpeg",
                                      "data": base64.b64encode(f.read_bytes()).decode()}})
    gen = {"responseModalities": ["IMAGE"]}
    if not src_url and aspect in ("1:1", "3:4", "16:9", "9:16"):
        gen["imageConfig"] = {"aspectRatio": aspect}
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_IMAGE_MODEL}:generateContent",
        headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY},
        json={"contents": [{"parts": parts}], "generationConfig": gen}, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"Google код {r.status_code}: {r.text[:200]}")
    for c in r.json().get("candidates") or []:
        for pt in (c.get("content") or {}).get("parts") or []:
            d = pt.get("inlineData") or pt.get("inline_data")
            if d and d.get("data"):
                return base64.b64decode(d["data"]), d.get("mimeType") or d.get("mime_type") or "image/png"
    raise RuntimeError("Google не вернул картинку: " + r.text[:200])


def fetch(kind: str, prompt: str, opts: dict) -> tuple:
    if GEMINI_KEY and not MOCK and kind in ("image", "edit"):  # сначала Google, при ошибке дальше
        try:
            style = STYLES.get(opts.get("style", "none"), "")
            full = prompt.strip()[:1500] if kind == "edit" else (prompt.strip() + style)[:1500]
            return google_image(full, opts.get("aspect", "1:1"), opts.get("src", "") if kind == "edit" else "")
        except Exception as e:  # noqa: BLE001
            print("Google image:", type(e).__name__, str(e)[:300])
    if OR_KEY and not MOCK:  # затем OpenRouter, при ошибке бесплатный Pollinations
        try:
            style = STYLES.get(opts.get("style", "none"), "")
            full = (prompt.strip() + style)[:1500]
            aspect = opts.get("aspect", "1:1")
            if kind == "video":
                return or_video(full, aspect, int(opts.get("duration", 4)))
            return or_image(prompt.strip()[:1500] if kind == "edit" else full, aspect,
                            opts.get("src", "") if kind == "edit" else "")
        except Exception as e:  # noqa: BLE001
            print("OpenRouter:", type(e).__name__, str(e)[:200])
    return fetch_free(kind, prompt, opts)


def fetch_free(kind: str, prompt: str, opts: dict) -> tuple:
    style = STYLES.get(opts.get("style", "none"), "")
    full = (prompt.strip() + style)[:1500]
    aspect = opts.get("aspect", "1:1")
    seed = opts.get("seed") or int(time.time()) % 1_000_000
    if kind in ("image", "edit"):
        w, h = IMAGE_SIZES.get(aspect, (1024, 1024))
        params = {"model": "flux", "width": w, "height": h, "nologo": "true", "seed": seed}
        if kind == "edit":  # картинка -> картинка: исходник отдаём по публичной ссылке
            params = {"model": "kontext", "image": opts.get("src", ""), "nologo": "true", "seed": seed}
        attempts = [(GEN_URL, auth_headers()), (OLD_IMG_URL, {})]
    else:
        dur = int(opts.get("duration", 4))
        params = {"model": os.getenv("VIDEO_MODEL", "seedance"), "duration": dur,
                  "aspectRatio": "9:16" if aspect == "9:16" else "16:9"}
        attempts = [(GEN_URL, auth_headers())]
    last = "нет ответа"
    for base, headers in attempts:
        try:
            r = requests.get(base + urllib.parse.quote(full, safe=""), params=params,
                             headers=headers, timeout=300)
            ct = r.headers.get("content-type", "").split(";")[0]
            if r.status_code == 200 and ct.startswith(("image/", "video/")):
                return r.content, ct
            last = f"сервис ответил {r.status_code}"
            if r.status_code in (401, 402, 403):
                last += " — для этого нужен ключ"
        except requests.RequestException as e:
            last = f"сеть: {type(e).__name__}"
    raise RuntimeError(last)


def run_job(jid: str, kind: str, prompt: str, opts: dict):
    try:
        if MOCK:
            ext = "mp4" if kind == "video" else "png"
            path = MEDIA / f"{jid}.{ext}"
            mock_make("video" if kind == "video" else "image", path, prompt)
        else:
            data, ct = fetch(kind, prompt, opts)
            ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
                   "video/mp4": "mp4", "video/webm": "webm"}.get(ct, "bin")
            path = MEDIA / f"{jid}.{ext}"
            path.write_bytes(data)
        jobs[jid].update(status="done", url=f"/media/{path.name}",
                         kind="video" if kind == "video" else "image")
    except Exception as e:  # noqa: BLE001
        jobs[jid].update(status="error", error=str(e)[:200])


# ---------- маршруты ----------
@app.get("/")
def index():
    folder = BASE if (BASE / "index.html").exists() else BASE / "static"
    return send_from_directory(folder, "index.html")


@app.get("/api/config")
def config():
    return jsonify(styles=list(STYLES), mock=MOCK)


@app.post("/api/generate")
def generate():
    check_access()
    rate_limit(client_ip())
    body = request.get_json(silent=True) or {}
    kind = body.get("kind")
    prompt = (body.get("prompt") or "").strip()
    if kind not in ("image", "video", "edit") or not (2 <= len(prompt) <= 1500):
        return jsonify(error="Опишите, что создать (2–1500 символов)"), 400
    raw_img = None
    if kind == "edit":
        src = body.get("image") or ""
        mm = re.match(r"data:image/(?:jpeg|png|webp);base64,(.+)$", src, re.S)
        if not mm or len(src) > 4_000_000:
            return jsonify(error="Добавьте фото, которое нужно изменить"), 400
        try:
            raw_img = base64.b64decode(mm.group(1))
        except Exception:  # noqa: BLE001
            return jsonify(error="Не удалось прочитать фото"), 400
    u = uid()
    tk = tickets.pop(body.get("ticket"), None)  # картинка, которую Velora рисует сама по ходу чата
    if not (tk and tk[0] == u and tk[1] > time.time()) and not spend(u):
        return paywall()
    jid = uuid.uuid4().hex[:12]
    jobs[jid] = {"status": "working", "t": time.time()}
    opts = {k: body.get(k) for k in ("style", "aspect", "duration", "seed")}
    if raw_img:
        sname = f"src_{jid}.jpg"
        (MEDIA / sname).write_bytes(raw_img)
        opts["src"] = (WEBAPP_URL or request.host_url.rstrip("/")) + "/media/" + sname
    threading.Thread(target=run_job, args=(jid, kind, prompt, opts), daemon=True).start()
    return jsonify(id=jid)


def mock_reply(last: str, photo=False) -> str:
    low = last.lower()
    if photo:
        return "На фото спокойная сцена: мягкий свет, чистая композиция, главный объект в центре."
    if any(w in low for w in ("нарису", "фото", "картин")):
        return ("Хорошо, сейчас создам: мягкий свет, глубина, чистые цвета. "
                "[[image: a misty morning over a calm sea, soft painterly watercolor look, muted colors]]")
    if "видео" in low:
        return ("Сделаю медленный кадр, пока поднимается рассвет. "
                "[[video: slow camera glide over calm water at dawn, thin mist rising, painterly look]]")
    if "кто ты" in low or "умеешь" in low:
        return "Я Velora, ИИ-помощница этой студии. Разговариваю, придумываю идеи и рисую фото и видео."
    return "Я здесь. Расскажи, что хочешь создать, или просто поговорим."


def stt_cloudflare(audio_b64: str) -> str:
    r = requests.post(
        f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/ai/run/@cf/openai/whisper-large-v3-turbo",
        headers={"Authorization": f"Bearer {CF_TOKEN}", "Content-Type": "application/json"},
        json={"audio": audio_b64, "language": "ru"}, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Cloudflare код {r.status_code}: {r.text[:200]}")
    return ((r.json().get("result") or {}).get("text") or "").strip()


def stt_gemini(audio_b64: str, mime: str) -> str:
    model = os.getenv("GEMINI_STT_MODEL", "gemini-2.5-flash")
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY},
        json={"contents": [{"parts": [
            {"text": "Дословно запиши речь из аудио на том языке, на котором она звучит. "
                     "Верни только текст. Если речи нет, верни пустую строку."},
            {"inline_data": {"mime_type": mime, "data": audio_b64}}]}]}, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Google код {r.status_code}: {r.text[:200]}")
    parts = (r.json().get("candidates") or [{}])[0].get("content", {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts).strip()


@app.post("/api/stt")
def stt():
    """Голос в текст для режима разговора: Cloudflare Whisper (русский), запасной вариант Google."""
    check_access()
    rate_limit("stt:" + client_ip(), limit=60)
    body = request.get_json(silent=True) or {}
    audio = body.get("audio") or ""
    mime = (body.get("mime") or "audio/webm").split(";")[0]
    if not (CF_ACCOUNT and CF_TOKEN) and not GEMINI_KEY:
        return jsonify(error="Для голоса добавьте CF_ACCOUNT_ID и CF_API_TOKEN"), 503
    if not audio or len(audio) > 6_000_000:
        return jsonify(error="Запись слишком длинная или пустая"), 400
    attempts = []
    if CF_ACCOUNT and CF_TOKEN:
        attempts.append(("Cloudflare", lambda: stt_cloudflare(audio)))
    if GEMINI_KEY:
        attempts.append(("Google", lambda: stt_gemini(audio, mime)))
    for name, fn in attempts:
        try:
            return jsonify(text=fn())
        except Exception as e:  # noqa: BLE001
            print("STT", name, type(e).__name__, str(e)[:300])
    return jsonify(error="Не удалось распознать речь"), 502


@app.post("/api/tts")
def tts():
    """Текст в голос (русский) через edge-tts. Возвращает mp3."""
    check_access()
    rate_limit("tts:" + client_ip(), limit=200)
    text = re.sub(r"[*_`#>\[\]]", "", str((request.get_json(silent=True) or {}).get("text", ""))).strip()[:1500]
    if not text:
        return jsonify(error="Пустой текст"), 400
    voice = TTS_VOICE if re.search(r"[а-яё]", text, re.I) else os.getenv("TTS_VOICE_EN", "en-US-AriaNeural")
    try:
        import asyncio
        import edge_tts

        async def run():
            out = b""
            async for ch in edge_tts.Communicate(text, voice).stream():
                if ch["type"] == "audio":
                    out += ch["data"]
            return out

        data = asyncio.run(run())
        if not data:
            raise RuntimeError("пустой звук")
        return app.response_class(data, mimetype="audio/mpeg")
    except Exception as e:  # noqa: BLE001
        print("TTS", type(e).__name__, str(e)[:200])
        return jsonify(error="Озвучка недоступна"), 502


@app.post("/api/chat")
def chat():
    check_access()
    rate_limit("chat:" + client_ip(), limit=120)
    body = request.get_json(silent=True) or {}
    msgs = [m for m in (body.get("messages") or [])
            if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)]
    if not msgs or msgs[-1]["role"] != "user":
        return jsonify(error="Пустое сообщение"), 400
    msgs = [{"role": m["role"], "content": m["content"][:300000]} for m in msgs[-24:]]
    while msgs and msgs[0]["role"] != "user":  # некоторые нейросети требуют начинать с пользователя
        msgs.pop(0)
    u = uid()
    if not spend(u):
        return paywall()
    text0 = msgs[-1]["content"]
    image = body.get("image")
    if isinstance(image, str) and image.startswith("data:image/") and len(image) < 4_000_000:
        msgs[-1]["content"] = [{"type": "text", "text": text0},
                               {"type": "image_url", "image_url": {"url": image}}]
    else:
        image = None

    def done(reply):
        out = {"reply": reply}
        if re.search(r"\[\[(image|video):", reply, re.I):
            t = uuid.uuid4().hex
            tickets[t] = (u, time.time() + 600)
            out["ticket"] = t
        return jsonify(out)

    if MOCK:
        time.sleep(1.2)
        return done(mock_reply(text0, bool(image)))
    try:
        prof = str(body.get("profile") or "")[:30000].strip()
        system = PERSONA + ("\n\nПожелания пользователя (учитывай, если они не противоречат правилам выше): "
                            + prof if prof else "")
        return done(ask_llm(msgs, system=system, max_tokens=LLM_MAX))
    except Exception as e:  # noqa: BLE001
        return jsonify(error=f"Нейросеть не ответила ({e})"), 502


@app.post("/api/unlock")
def unlock():
    check_access(gate=False)
    if not BETA_GATE:
        return jsonify(sid="open")
    u = uid()
    rate_limit("unlock:" + u, limit=8, per=600)
    pw = str((request.get_json(silent=True) or {}).get("password", ""))
    if not hmac.compare_digest(pw.encode(), BETA_PASSWORD.encode()):
        return jsonify(error="Неверный пароль"), 401
    now = time.time()
    with lock:
        for k in [k for k, v in sessions.items() if v[1] < now]:
            sessions.pop(k, None)
        sid = secrets.token_urlsafe(24)
        sessions[sid] = (u, now + SESSION_TTL)
    return jsonify(sid=sid)


@app.get("/api/me")
def me():
    check_access()
    u = uid()
    with lock:
        pend = db.execute("select id from pays where uid=? and status='pending' and method='yookassa' and t>?",
                          (u, time.time() - 7200)).fetchall()
    for (pid,) in pend:
        try:
            sync(pid)
        except Exception as e:  # noqa: BLE001
            print("sync:", type(e).__name__, str(e)[:200])
    used, _, lk = row(u)
    s = sub_of(u)
    return jsonify(free_total=FREE, used=min(used, FREE), beta=UNLIMITED, active=UNLIMITED or s["active"],
                   paid=s["active"], plan=s["plan"], sub_until=s["sub_until"], price=PLANS["plus"]["rub"], reset=lk,
                   prices={k: {"rub": v["rub"], "stars": v["stars"]} for k, v in PLANS.items()})


def stars_pay(u: str, plan: str):
    if not BOT_TOKEN:
        return jsonify(error="На сервере не задан BOT_TOKEN"), 503
    if not u.startswith("tg:"):
        return jsonify(error="Звёзды работают только внутри Telegram"), 400
    why = trial_blocked(u) if plan == "pro_trial" else ""
    if why:
        return jsonify(error=why), 400
    try:
        return jsonify(invoice=stars_invoice(u, plan), plan=plan)
    except Exception as e:  # noqa: BLE001
        print("Stars invoice:", e)
        return jsonify(error=f"Telegram не создал счёт: {str(e)[:150]}"), 502


def _plan_of(body: dict) -> str:
    return body.get("plan") if body.get("plan") in PLANS else "plus"


@app.post("/api/stars/invoice")
def stars_invoice_route():
    check_access()
    return stars_pay(uid(), _plan_of(request.get_json(silent=True) or {}))


@app.get("/api/stars/me")
def stars_me():
    check_access()
    return jsonify(sub_of(uid()))


@app.post("/api/pay")
def pay():
    check_access()
    u = uid()
    body = request.get_json(silent=True) or {}
    plan = _plan_of(body)
    if body.get("method") == "stars":
        return stars_pay(u, plan)
    p = PLANS[plan]
    if plan == "pro_trial" and trial_blocked(u):
        return jsonify(error=trial_blocked(u)), 400
    if MOCK:
        pid = "mock" + uuid.uuid4().hex[:8]
        url = request.host_url + "api/mock-pay/" + pid
        method = "mock"
    else:
        if not (YK_SHOP and YK_KEY):
            return jsonify(error="Оплата ещё не подключена"), 503
        ret = YK_RETURN or (f"https://t.me/{bot_username()}" if bot_username() else WEBAPP_URL) or "https://t.me"
        try:
            pr = yk_create(u, plan, ret)
        except requests.HTTPError as e:
            st, code, desc, par = yk_err(e)
            hint = {401: " Проверьте YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY: ключ должен быть от этого же магазина",
                    403: " Магазин не активирован или у ключа нет прав на платежи"}.get(st, "")
            return jsonify(error=f"ЮKassa отклонила платёж ({st}) {code}: {desc} {par}.{hint}"[:300]), 502
        except RuntimeError as e:
            return jsonify(error=str(e)), 502
        except Exception as e:  # noqa: BLE001
            return jsonify(error=f"ЮKassa не ответила ({type(e).__name__}), попробуйте ещё раз"), 502
        pid, url, method = pr["id"], pr["confirmation"]["confirmation_url"], "yookassa"
    with lock:
        db.execute("insert into pays(id,uid,status,t,plan,amount,method) values(?,?,?,?,?,?,?)",
                   (pid, u, "pending", time.time(), plan, p["rub"], method))
        db.commit()
    return jsonify(url=url, id=pid, plan=plan)


@app.get("/api/mock-pay/<pid>")
def mock_pay(pid):
    with lock:
        r = db.execute("select uid, plan from pays where id=?", (pid,)).fetchone()
    if not MOCK or not r:
        abort(404)
    grant(pid, r[0], r[1] if r[1] in PLANS else "plus", 0, "mock")
    return ("<meta name=viewport content='width=device-width'><body style='background:#09090b;color:#eee;"
            "font-family:sans-serif;display:grid;place-items:center;height:100vh;text-align:center'>"
            "Тестовая оплата прошла. Вернитесь в приложение.")


@app.post("/api/yookassa/webhook")  # в кабинете ЮKassa: Интеграция → HTTP-уведомления
@app.post("/yookassa/webhook")
def hook():
    j = request.get_json(silent=True) or {}
    pid = (j.get("object") or {}).get("id") if j.get("event") == "payment.succeeded" else None
    if pid:
        try:
            sync(pid)
        except Exception:  # noqa: BLE001
            abort(502)
    return "", 200


@app.get("/api/job/<jid>")
def job(jid):
    j = jobs.get(jid)
    if not j:
        abort(404)
    return jsonify({k: v for k, v in j.items() if k != "t"})


@app.get("/media/<name>")
def media(name):
    return send_from_directory(MEDIA, name, max_age=3600)


def cleaner():
    while True:
        time.sleep(1800)
        cutoff = time.time() - MEDIA_TTL
        for f in MEDIA.iterdir():
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
        for k in [k for k, v in jobs.items() if v.get("t", 0) < cutoff]:
            jobs.pop(k, None)


@app.get("/api/yk-check")
def yk_check():
    """Открыть в браузере: WEBAPP_URL/api/yk-check. Секретов не показывает."""
    out = {"shop_id": YK_SHOP, "key_set": bool(YK_KEY),
           "key_kind": "test_" if YK_KEY.startswith("test_") else "live_" if YK_KEY.startswith("live_") else "неизвестный формат",
           "receipt_email_set": bool(YK_EMAIL), "mock": MOCK}
    if not (YK_SHOP and YK_KEY):
        out["problem"] = "Не заданы YOOKASSA_SHOP_ID или YOOKASSA_SECRET_KEY"
        return jsonify(out)
    if yk_creds_problem():
        out.update(auth="не проверялась", problem=yk_creds_problem())
        return jsonify(out)
    try:
        d = yk_me(force=True)
        out.update(auth="ok", account_id=d.get("account_id"), test_shop=d.get("test"), status=d.get("status"),
                   receipts_required=yk_fiscal(), methods=d.get("payment_methods"))
        if str(d.get("account_id")) != YK_SHOP:
            out["problem"] = f"Ключ относится к магазину {d.get('account_id')}, а в YOOKASSA_SHOP_ID указан {YK_SHOP}"
        elif yk_fiscal() and not YK_EMAIL:
            out["problem"] = "В магазине включены чеки ЮKassa: добавьте YOOKASSA_RECEIPT_EMAIL"
        elif d.get("status") != "enabled":
            out["problem"] = "Магазин в статусе " + str(d.get("status")) + ": оплата ещё не включена в кабинете ЮKassa"
    except requests.HTTPError as e:
        st, code, desc, par = yk_err(e)
        out.update(auth="ошибка", http=st, code=code, description=desc,
                   problem="Неверные shopId или секретный ключ" if st == 401 else desc)
    except Exception as e:  # noqa: BLE001
        out.update(auth="нет связи", problem=f"{type(e).__name__}: {str(e)[:150]}")
    return jsonify(out)


@app.get("/health")
def health():
    return jsonify(ok=True, yookassa=bool(YK_SHOP and YK_KEY), stars=bool(BOT_TOKEN),
                   llm=bool(GEMINI_KEY or OR_KEY or LLM_API_URL), bot=bool(BOT_TOKEN and WEBAPP_URL))


# ---------- бот ----------
def tg(method: str, **payload):
    r = requests.post(f"{TG_API}/bot{BOT_TOKEN}/{method}", json=payload, timeout=40)
    return r.json()


def tg_upload(method: str, field: str, data: bytes, filename: str, **form):
    r = requests.post(f"{TG_API}/bot{BOT_TOKEN}/{method}", data=form,
                      files={field: (filename, data)}, timeout=180)
    try:
        return r.json()
    except ValueError:
        return {"ok": False}


@app.post("/api/send")
def send_to_chat():
    """Кнопка «Сохранить»: бот присылает фото/видео в личный чат."""
    check_access()
    u = uid()
    if not u.startswith("tg:") or not BOT_TOKEN:
        return jsonify(error="Доступно только внутри Telegram"), 400
    rate_limit("send:" + u, limit=30)
    name = Path((request.get_json(silent=True) or {}).get("name", "")).name
    path = MEDIA / name
    if not name or not path.is_file():
        abort(404)
    video = path.suffix.lower() in (".mp4", ".webm")
    data = path.read_bytes()
    r = tg_upload("sendVideo" if video else "sendPhoto", "video" if video else "photo", data, name,
                  chat_id=u[3:], caption="Velora")
    if not r.get("ok") and not video:  # слишком большое/необычное фото: шлём файлом
        r = tg_upload("sendDocument", "document", data, name, chat_id=u[3:], caption="Velora")
    return (jsonify(ok=True), 200) if r.get("ok") else (jsonify(error="Не удалось отправить."), 502)


WH_SECRET = hashlib.sha256(("velora-wh:" + BOT_TOKEN).encode()).hexdigest()[:40]
USE_WEBHOOK = WEBAPP_URL.startswith("https://") and os.getenv("USE_POLLING") != "1"
bot_hist: dict = {}
HELP = ("Я Velora: говорю, придумываю, рисую фото и видео и разбираю снимки.\n\n"
        "Что можно прямо в чате:\n"
        "• просто напишите вопрос\n"
        "• пришлите фото: расскажу, что на нём (подпись к фото = ваш вопрос)\n"
        "• фото с подписью /edit и описанием: изменю снимок\n"
        "• /img описание: нарисую фото\n"
        "• /video описание: сниму короткое видео\n"
        "• /new: начать разговор заново\n\n"
        "Кнопка ниже открывает приложение: камера, стили, форматы.")


def app_button():
    return {"inline_keyboard": [[{"text": "Открыть Velora", "web_app": {"url": WEBAPP_URL}}]]}


def strip_tags(s: str) -> str:
    return re.sub(r"\[\[(image|video):[\s\S]*?\]\]", "", s, flags=re.I).strip()


def send_media(chat_id, kind: str, prompt: str, opts=None):
    data, _ = fetch(kind, prompt, opts or {})
    if kind == "video":
        tg_upload("sendVideo", "video", data, "velora.mp4", chat_id=chat_id)
        return
    r = tg_upload("sendPhoto", "photo", data, "velora.jpg", chat_id=chat_id, caption=prompt[:200])
    if not r.get("ok"):
        tg_upload("sendDocument", "document", data, "velora.jpg", chat_id=chat_id, caption=prompt[:200])


def handle_update(u: dict):
    if is_payment(u):
        return handle_payment(u)
    msg = u.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id, frm = chat.get("id"), (msg.get("from") or {}).get("id")
    if not chat_id or not frm or chat.get("type") != "private":
        return
    who = f"tg:{frm}"
    text = (msg.get("text") or msg.get("caption") or "").strip()
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower() if cmd.startswith("/") else ""
    arg = arg.strip() if cmd else text

    def say(t, **kw):
        res = tg("sendMessage", chat_id=chat_id, text=(t or "…")[:4000], **kw)
        if "web_app" in json.dumps(kw.get("reply_markup") or {}):  # сообщение с кнопкой удаляем через время
            mid = ((res or {}).get("result") or {}).get("message_id")
            if mid:
                open_msgs.setdefault(chat_id, []).append(mid)
                threading.Timer(APP_MSG_TTL, lambda: tg("deleteMessage", chat_id=chat_id,
                                                        message_id=mid)).start()

    def need_pay():
        say("Бесплатные запросы закончились. Они обновятся через 3 часа, а подписка в Velora снимает лимит.",
            reply_markup=app_button())

    try:
        for mid in open_msgs.pop(chat_id, []):  # прошлые кнопки открытия убираем сразу
            try:
                tg("deleteMessage", chat_id=chat_id, message_id=mid)
            except Exception:  # noqa: BLE001
                pass
        if cmd in ("/start", "/help"):
            say(HELP, reply_markup=app_button())
            return
        if cmd == "/new":
            bot_hist.pop(chat_id, None)
            say("Начали заново.")
            return
        photos = msg.get("photo")
        if photos:
            if cmd == "/edit" and not arg:
                say("Добавьте описание: что изменить на фото.")
                return
            if not spend(who):
                need_pay()
                return
            pick = next((p for p in reversed(photos) if p.get("file_size", 0) <= 3_000_000), photos[0])
            tg("sendChatAction", chat_id=chat_id, action="upload_photo" if cmd == "/edit" else "typing")
            path = tg("getFile", file_id=pick["file_id"])["result"]["file_path"]
            raw = requests.get(f"{TG_API}/file/bot{BOT_TOKEN}/{path}", timeout=60).content
            if cmd == "/edit":
                name = f"src_{uuid.uuid4().hex[:12]}.jpg"
                (MEDIA / name).write_bytes(raw)
                send_media(chat_id, "edit", arg[:600], {"src": f"{WEBAPP_URL}/media/{name}"})
                return
            q = (arg or "Что на фото? Опиши подробно и по делу.")[:2000]
            durl = "data:image/jpeg;base64," + base64.b64encode(raw).decode()
            reply = ask_llm([{"role": "user", "content": [{"type": "text", "text": q},
                                                          {"type": "image_url", "image_url": {"url": durl}}]}])
            say(strip_tags(reply))
            return
        if cmd in ("/img", "/image", "/video"):
            if len(arg) < 2:
                say("Добавьте описание, например: /img рассвет над горами")
                return
            if not spend(who):
                need_pay()
                return
            kind = "video" if cmd == "/video" else "image"
            tg("sendChatAction", chat_id=chat_id, action="upload_video" if kind == "video" else "upload_photo")
            send_media(chat_id, kind, arg[:600])
            return
        if cmd == "/edit":
            say("Пришлите фото и в подписи напишите /edit и что изменить.")
            return
        if cmd or not text:
            if cmd:
                say(HELP, reply_markup=app_button())
            return
        if not spend(who):
            need_pay()
            return
        tg("sendChatAction", chat_id=chat_id, action="typing")
        h = bot_hist.setdefault(chat_id, [])
        h.append({"role": "user", "content": text[:2000]})
        del h[:-10]
        while h and h[0]["role"] != "user":
            h.pop(0)
        try:
            reply = ask_llm(h)
        except Exception:
            h.pop()
            raise
        clean = strip_tags(reply)
        h.append({"role": "assistant", "content": clean[:2000] or "…"})
        if clean:
            say(clean)
        m = re.search(r"\[\[(image|video):\s*([\s\S]+?)\]\]", reply, re.I)
        if m:
            send_media(chat_id, m.group(1).lower(), m.group(2).strip()[:600])
    except Exception as e:  # noqa: BLE001
        say(f"Не вышло: {str(e)[:160]}")


@app.post("/tg/<secret>")
def tg_hook(secret):
    if not BOT_TOKEN or not hmac.compare_digest(secret, WH_SECRET):
        abort(404)
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token", "") != WH_SECRET:
        abort(403)
    upd = request.get_json(silent=True) or {}
    if is_payment(upd):
        handle_payment(upd)  # сразу, не в потоке: при сбое вернём 500, и Telegram повторит событие
    else:
        threading.Thread(target=handle_update, args=(upd,), daemon=True).start()
    return "", 200


def setup_bot():
    # бот открыт для всех, пароль спрашивает само мини-приложение (/api/unlock)
    tg("setChatMenuButton", menu_button={"type": "web_app", "text": "Velora",
                                         "web_app": {"url": WEBAPP_URL}})
    tg("setMyCommands", commands=[
        {"command": "img", "description": "Нарисовать фото: /img описание"},
        {"command": "video", "description": "Снять видео: /video описание"},
        {"command": "edit", "description": "Изменить фото (подпись к фото)"},
        {"command": "new", "description": "Начать разговор заново"},
        {"command": "help", "description": "Что я умею"}])
    if USE_WEBHOOK:
        res = tg("setWebhook", url=f"{WEBAPP_URL}/tg/{WH_SECRET}", secret_token=WH_SECRET,
                 allowed_updates=ALLOWED)
        print("Вебхук:", "установлен" if res.get("ok") else res)
        return
    tg("deleteWebhook")
    offset = 0
    while True:
        try:
            res = tg("getUpdates", offset=offset, timeout=30, allowed_updates=ALLOWED)
            for upd in res.get("result", []):
                if is_payment(upd):
                    handle_payment(upd)
                else:
                    threading.Thread(target=handle_update, args=(upd,), daemon=True).start()
                offset = upd["update_id"] + 1
        except Exception as e:  # noqa: BLE001
            print("poll:", type(e).__name__, str(e)[:200])
            time.sleep(5)


# Фоновые потоки стартуют при импорте, чтобы работало и под gunicorn
threading.Thread(target=cleaner, daemon=True).start()
if BOT_TOKEN and WEBAPP_URL and not MOCK:
    threading.Thread(target=setup_bot, daemon=True).start()
    print("Бот запущен (" + ("вебхук" if USE_WEBHOOK else "polling") + ")")
elif not MOCK:
    print("BOT_TOKEN или WEBAPP_URL не заданы — бот не запущен, работает только сайт")

def startup_report():
    w = []
    if os.getenv("RENDER") and not os.path.abspath(DB_PATH).startswith("/var/data"):
        w.append(f"база {DB_PATH} лежит не на постоянном диске: на Render подписки пропадут при деплое "
                 "(Disk с Mount Path /var/data, DB_PATH=/var/data/velora.db, MEDIA_DIR=/var/data/media)")
    if all(v["rub"] == 1 for v in PLANS.values()) or all(v["stars"] == 1 for v in PLANS.values()):
        w.append("ТЕСТОВЫЕ ЦЕНЫ: везде 1 ₽ / 1 ⭐. Перед релизом поменяйте таблицу PRICES в начале server.py")
    if BETA_GATE and BETA_PASSWORD == "5050" and not os.getenv("BETA_PASSWORD"):
        w.append("пароль беты по умолчанию 5050: задайте BETA_PASSWORD")
    if not (YK_SHOP and YK_KEY):
        w.append("ЮKassa не подключена: нет YOOKASSA_SHOP_ID / YOOKASSA_SECRET_KEY")
    if not (GEMINI_KEY or OR_KEY or LLM_API_URL):
        w.append("нет ключа нейросети: чат пойдёт через бесплатный Pollinations, он нестабилен")
    if UNLIMITED:
        w.append("UNLIMITED=1: лимиты сняты для всех (это режим беты)")
    for x in w:
        print("!!!", x)


startup_report()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), threaded=True)

